"""Tests of the owner-laptop bridge, the serial relay and the board file tools.

usage (repository root): python -B demo/owner/tests/test_bridge.py [--only bridge|files] [-q]

Bridge group, OWNER_BRIDGE_TESTS_PASS when every test passed and none was skipped:
  * the EF1 codec: round trips, a CRC over the sequence number and the payload of every line, the 1,000-character
    limit of a request line, the relay's path whitelist, the same screen policy as the gateway (efgw/httpapi.py);
  * the relay's output queue: progress.io coalesced latest-wins, go-back-N retransmission, a reset after release;
  * owner_bridge.py with the cmd: transport -> serial_relay.py (a child process) -> a stub gateway HTTP server: the
    static owner screen and the gateway's Host, Origin and owner-header rules on the bridge, the status in several
    parts, a submit and its event stream end to end (the relay sends Host 127.0.0.1:PORT, X-EdgeFHE-Owner: 1 and
    Content-Type application/json, and never the browser's Origin), 409 while busy, /runs/<run>, since and
    Last-Event-ID, a refused stream;
  * line faults: a CRC-corrupted line dropped and resynced, a line split by a kernel message, a lost last line found
    by the heartbeat, console noise in both directions, a damaged and a lost request line resent, a request line over
    1,000 characters refused on both sides, the relay restarted under a running bridge, an unreachable gateway;
  * the serial transport through the real kv_console.KV on a fake console (a getty-style login, then bash): login,
    the relay started with exec, a request through it, a relay that is already running reused without a login, and
    'bye' back to the login prompt;
  * the cloud-PC rule: main() refuses without --i-am-the-owner-laptop and on any sign of the cloud PC before it binds
    a port or starts the relay; the signs (the real evidence console's HTTP handler on a free port, Docker container
    names and images, the cloud PC's folders).
Board files group, BOARD_FILES_TESTS_PASS: board_files.py against the same fake console with a fake sudo: push-file,
push-tree (excluded files stay behind), fetch-file (a damaged console byte, then a retry), fetch-file refusals (key
names, the PKI directory, the gateway's private records, a PEM private key under an innocent name, a link to the key;
the public PKI files still come back), fetch-public (refused before the seal and on a seal mismatch; the public log and
the disclosures after it), PC outputs inside the repository refused for fetch-file, fetch-public and csr-roundtrip
under every spelling (case, \\\\?\\ and the admin share), csr-roundtrip with make_demo_pki.sh ca-and-server and
sign-client (the board key never leaves the fake board).
Needs bash (Git Bash on Windows) and openssl for the fake console; pyserial is not needed. Every file lives in a
temporary directory outside the repository.
"""
import argparse
import contextlib
import hashlib
import http.client
import http.server
import importlib.util
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.parse

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
OWNER = os.path.dirname(HERE)
DEMO = os.path.dirname(OWNER)
ROOT = os.path.dirname(DEMO)
RELAY = os.path.join(OWNER, 'serial_relay.py')
BOARD_FILES = os.path.join(DEMO, 'pc', 'board_files.py')
FORM_JSON = os.path.join(DEMO, 'model', 'form_cdc.json')
UI_DIR = os.path.join(DEMO, 'ui')
PKI_SCRIPT = os.path.join(DEMO, 'pki', 'make_demo_pki.sh')
KV_DIR = os.path.join(ROOT, 'energy_probe', 'kv260')
EDGE = os.path.join(DEMO, 'edge')
for path in (OWNER, KV_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)


def _serial_stand_in():
    """kv_console imports pyserial; the tests never open a port, so a stand-in module is enough without it."""
    try:
        import serial  # noqa: F401
    except ImportError:
        stub = types.ModuleType('serial')

        class Serial:
            def __init__(self, *args, **kwargs):
                raise OSError('pyserial is not installed')
        stub.Serial = Serial
        stub.SerialException = OSError
        sys.modules['serial'] = stub


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_serial_stand_in()
import serial_relay as ef1  # noqa: E402
import owner_bridge as ob  # noqa: E402
import kv_console  # noqa: E402
board_files = load_module('edgefhe_board_files', BOARD_FILES)
BUSY_MESSAGE = '本閘道正在處理另一筆請求，操作人員應待該筆請求完成後再送出。'      # the gateway's text (efgw/runner.py)

with open(FORM_JSON, encoding='utf-8') as _f:
    FORM = json.load(_f)
M = {'GenHlth': 3, 'HighBP': 1, 'BMI': 31.2, 'Age': 9}
RUN = re.compile(r'r[0-9a-f]{12}')
LOG = []                                    # what the bridge printed (markers and operator texts)
ob.out = lambda line: LOG.append(line)
ob.say = lambda text: LOG.append(text)
FAST = {'hello_repeat_s': 0.3, 'ack_every_s': 0.5, 'nak_repeat_s': 0.8, 'q_resend_s': 1.0, 'alive_s': 3.0,
        'dead_s': 3.0, 'silent_start_s': 10.0, 'retry_s': 0.3}


def git_bash():
    return board_files.find_git_bash()


def bash_path(path):
    """A path as Git Bash (MSYS) or a POSIX bash spells it."""
    path = os.path.abspath(path)
    if os.name != 'nt':
        return path
    drive, rest = os.path.splitdrive(path)
    return '/' + drive[0].lower() + rest.replace('\\', '/')


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


# ------------------------------------------------------------------------------------------------ http client
class Reply:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode('utf-8'))

    def code(self):
        try:
            return self.json().get('code')
        except ValueError:
            return None


def call(port, method, path, body=None, headers=(), host=None, timeout=60):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader('Host', host or '127.0.0.1:%d' % port)
        names = set()
        for name, value in headers:
            conn.putheader(name, value)
            names.add(name.lower())
        if body is not None and 'content-length' not in names:
            conn.putheader('Content-Length', str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        return Reply(resp.status, dict((k.lower(), v) for k, v in resp.getheaders()), resp.read())
    finally:
        conn.close()


def submit(port, form, backend='pl', origin=True, headers=None, raw=None):
    """POST /api/v1/submit as the owner screen sends it (fetch adds a same-origin Origin to a POST)."""
    data = raw if raw is not None else json.dumps({'form': form, 'backend': backend}).encode('utf-8')
    if headers is None:
        headers = [('Content-Type', 'application/json'), (ob.OWNER_HEADER, '1')]
        if origin:
            headers.append(('Origin', 'http://127.0.0.1:%d' % port))
    return call(port, 'POST', '/api/v1/submit', data, headers)


class SSE:
    """GET /api/v1/events on the bridge, one Server-Sent Events block at a time."""

    def __init__(self, port, query='?since=0', headers=(), timeout=60):
        self.conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
        self.conn.putrequest('GET', '/api/v1/events' + query, skip_host=True, skip_accept_encoding=True)
        self.conn.putheader('Host', '127.0.0.1:%d' % port)
        for name, value in headers:
            self.conn.putheader(name, value)
        self.conn.endheaders()
        self.resp = self.conn.getresponse()
        self.status = self.resp.status
        self.headers = dict((k.lower(), v) for k, v in self.resp.getheaders())

    def block(self):
        block = {'comments': []}
        while True:
            raw = self.resp.readline()
            if not raw:
                raise EOFError('the event stream was closed')
            line = raw.decode('utf-8').rstrip('\n').rstrip('\r')
            if line == '':
                if len(block) > 1 or block['comments']:
                    return block
                continue
            if line.startswith(':'):
                block['comments'].append(line[1:].strip())
                continue
            name, _, value = line.partition(':')
            block[name] = value[1:] if value.startswith(' ') else value

    def envelope(self):
        while True:
            block = self.block()
            if 'data' in block:
                env = json.loads(block['data'])
                if int(block['id']) != env['seq'] or 'event' in block:
                    raise AssertionError('SSE block %r' % block)
                return env

    def until(self, run, last='wiped', limit_s=60):
        got, deadline = [], time.monotonic() + limit_s
        while time.monotonic() < deadline:
            got.append(self.envelope())
            if got[-1]['run'] == run and got[-1]['type'] == last:
                return got
        raise AssertionError('no %s event for %s' % (last, run))

    def close(self):
        self.resp.close()
        self.conn.close()


# ----------------------------------------------------------------------------------------------- stub gateway
class StubGateway:
    """A stand-in for the board gateway's loopback API (demo/edge/EVENTS.md section 5) with the gateway's Host, Origin
    and owner-header rules; it records every request it receives. A submitted form plays a run of events."""

    def __init__(self, heartbeat_s=0.5):
        self.cond = threading.Condition()
        self.events = []
        self.requests = []
        self.busy = False
        self.runs = 0
        self.heartbeat_s = heartbeat_s
        self.refuse_streams = None
        self.progress = 40
        self.pause_s = 0.05
        self.hold = threading.Event()
        self.hold.set()
        self.closed = False
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _StubHandler)
        self.server.daemon_threads = True
        self.server.stub = self
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True).start()

    def close(self):
        self.closed = True
        self.hold.set()
        with self.cond:
            self.cond.notify_all()
        self.server.shutdown()
        self.server.server_close()

    def last_seq(self):
        with self.cond:
            return len(self.events)

    def emit(self, run, etype, cls, data):
        with self.cond:
            env = {'v': 1, 'seq': len(self.events) + 1, 'run': run, 'type': etype, 'class': cls,
                   't_mono_ns': time.monotonic_ns(), 'data': data}
            self.events.append(env)
            self.cond.notify_all()
            return env

    def of_run(self, run):
        with self.cond:
            return [e for e in self.events if e['run'] == run]

    def recorded(self, method=None, path=None):
        with self.cond:
            return [r for r in self.requests if (method is None or r['method'] == method)
                    and (path is None or r['path'].split('?')[0] == path)]

    def status(self):
        return {'v': 1, 'busy': self.busy, 'run': None, 'stopping': False, 'default_backend': 'pl',
                'backends': ['pl', 'spec', 'cpu'], 'form': FORM, 'last_seq': self.last_seq(), 'progress_ms': 25,
                'rehearsal': False, 'retain_private': False}

    def play(self, run, form, backend):
        try:
            commitment = hashlib.sha256(('stub ' + run).encode('ascii')).hexdigest()
            self.emit(run, 'form.accepted', 'public', {'kind': 'form', 'backend': backend, 'model': 'cdc-diabetes-lr4-v1',
                                                       'model_sha256': '63cee36f' + '0' * 56, 'commitment': commitment})
            self.emit(run, 'form.accepted', 'owner', {'features': ['GenHlth', 'HighBP', 'BMI', 'Age'], 'values': form,
                                                      'x': [3.0, 1.0, 31.2, 9.0], 'z': [0.39, 1.13, 0.43, 0.32],
                                                      'contributions': [0.27, 0.5, 0.21, 0.18], 'bias': -0.578,
                                                      'commitment': commitment, 'note': '虛構答案・僅本機'})
            self.emit(run, 'client.spawned', 'public', {'kind': 'form', 'backend': backend, 'pid': 4242, 'argv': [
                '/home/ubuntu/edgefhe-demo/build-ID8/net/efnet_client', '--backend', backend, '--mode', 'serial'],
                'permit': True, 'irq_before': {'61': 5, '62': 5}})
            for irq in ('61', '62'):
                self.emit(run, 'progress.irq', 'public', {'irq': irq, 'count': 6, 'delta': 1, 'index': 1,
                                                          'label': 'LOAD', 't_ns': 1000})
            for k in range(1, self.progress + 1):
                self.emit(run, 'progress.io', 'public', {'wchar': k * 394022 // self.progress, 'rchar': 0,
                                                         't_ns': k * 25000000})
                time.sleep(0.002)
            for irq in ('61', '62'):
                self.emit(run, 'progress.irq', 'public', {'irq': irq, 'count': 7, 'delta': 2, 'index': 2,
                                                          'label': 'ENC', 't_ns': 2000})
            for k in range(1, 11):
                self.emit(run, 'progress.io', 'public', {'wchar': 394022, 'rchar': k * 9871, 't_ns': 2 * 10 ** 9 + k})
            time.sleep(self.pause_s)
            self.hold.wait(60)
            self.emit(run, 'client.exited', 'public', {
                'returncode': 0, 'wall_ns': 4100000000, 'status': 'CPP_E2E_PL_NUMERIC_AND_DRAIN_PASS', 'stop': None,
                'report': {'status': 'CPP_E2E_PL_NUMERIC_AND_DRAIN_PASS', 'note': 'n' * 900, 'trace': [
                    {'request_sha256': 'ab' * 32, 'response_payload_sha256': 'cd' * 32, 'prepare_ns': 123456789}]}})
            self.emit(run, 'irq.delta', 'public', {'backend': backend, 'delta': {'61': 2, '62': 2}, 'expected': 2,
                                                   'passed': True})
            self.emit(run, 'result', 'owner', {'logit_ckks': 0.648691, 'logit_plain': 0.648683, 'abs_error': 8e-06,
                                               'passed': True, 'level': 3, 'level_label': '第 3 級（偏高）',
                                               'boundary': None, 'boundary_text': '介於 L2／L3，不判定',
                                               'message': None})
            self.emit(run, 'wiped', 'public', {'files': ['in.csv', 'score.txt', 'permit'], 'run_dir_removed': True,
                                               'problems': [], 'retained_private': False})
        finally:
            with self.cond:
                self.busy = False
                self.cond.notify_all()


class _StubHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def _json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        stub = self.server.stub
        body = b''
        if method == 'POST':
            body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        with stub.cond:
            stub.requests.append({'method': method, 'path': self.path, 'headers': list(self.headers.items()),
                                  'body': body})
        mine = ('127.0.0.1:%d' % stub.port, 'localhost:%d' % stub.port)
        hosts = self.headers.get_all('Host') or []
        if len(hosts) != 1 or hosts[0] not in mine:
            return self._json(403, {'code': 'HOST_FORBIDDEN', 'message': '本閘道只接受本機的 Host。'})
        origins = self.headers.get_all('Origin') or []
        if origins and origins[0] not in ['http://' + m for m in mine]:
            return self._json(403, {'code': 'ORIGIN_FORBIDDEN', 'message': '本閘道只接受擁有者畫面本身的請求。'})
        path, _, query = self.path.partition('?')
        run = re.fullmatch(r'/api/v1/runs/(r[0-9a-f]{12})', path)
        if path == '/api/v1/status' and method == 'GET':
            return self._json(200, stub.status())
        if path == '/api/v1/submit' and method == 'POST':
            if self.headers.get_all(ob.OWNER_HEADER) != ['1']:
                return self._json(403, {'code': 'OWNER_HEADER_REQUIRED', 'message': '缺少擁有者標頭。'})
            if self.headers.get('Content-Type') != 'application/json':
                return self._json(403, {'code': 'CONTENT_TYPE_JSON', 'message': '內容類型必須是 JSON。'})
            payload = json.loads(body.decode('utf-8'))
            with stub.cond:
                if stub.busy:
                    return self._json(409, {'code': 'BUSY', 'message': BUSY_MESSAGE})
                stub.busy = True
                stub.runs += 1
                rid = 'r%012x' % (0xabc000 + stub.runs)
            threading.Thread(target=stub.play, args=(rid, payload['form'], payload.get('backend') or 'pl'),
                             daemon=True).start()
            return self._json(202, {'run': rid})
        if run and method == 'GET':
            events = stub.of_run(run.group(1))
            if not events:
                return self._json(404, {'code': 'RUN_UNKNOWN', 'message': '本閘道的記憶體中沒有此次執行的事件。'})
            return self._json(200, {'run': run.group(1), 'events': events})
        if path == '/api/v1/events' and method == 'GET':
            return self._events(query)
        return self._json(404, {'code': 'NOT_FOUND', 'message': '本閘道找不到此路徑。'})

    def _events(self, query):
        stub = self.server.stub
        if stub.refuse_streams is not None:
            return self._json(*stub.refuse_streams)
        since = int((urllib.parse.parse_qs(query).get('since') or ['0'])[0])
        lei = self.headers.get('Last-Event-ID')
        if lei:
            since = max(since, int(lei))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.end_headers()
        try:
            self.wfile.write(b'retry: 2000\n: heartbeat\n\n')
            beat = time.monotonic() + stub.heartbeat_s
            while not stub.closed:
                with stub.cond:
                    batch = stub.events[since:]
                    if not batch:
                        stub.cond.wait(max(0.01, beat - time.monotonic()))
                        batch = stub.events[since:]
                if batch:
                    self.wfile.write(b''.join(('id: %d\ndata: %s\n\n' % (e['seq'], json.dumps(
                        e, ensure_ascii=False, separators=(',', ':')))).encode('utf-8') for e in batch))
                    since = batch[-1]['seq']
                if time.monotonic() >= beat:
                    self.wfile.write(b': heartbeat\n\n')
                    beat = time.monotonic() + stub.heartbeat_s
        except OSError:
            pass


# ------------------------------------------------------------------------------------------- tamper transport
def _event_type(msg):
    try:
        return json.loads(msg.get('data') or '').get('type')
    except (ValueError, AttributeError):
        return None


def flip(raw):
    """One base64 character of an EF1 line replaced: the line keeps its shape, the CRC no longer matches."""
    i = raw.rindex(b' ') + 1 + (len(raw) - raw.rindex(b' ')) // 2
    return raw[:i] + (b'B' if raw[i:i + 1] == b'A' else b'A') + raw[i + 1:]


class Tamper:
    """A transport wrapper that damages chosen lines the way a noisy console does. read_rules get (seq, message,
    raw line) of each valid E line and return replacement lines or None; write_rules get each written Q line and return
    the bytes to write instead (b'' drops the line). A rule fires once."""

    def __init__(self, inner, read_rules=(), write_rules=()):
        self.inner = inner
        self.kind = inner.kind
        self.read_rules = [[rule, False] for rule in read_rules]
        self.write_rules = [[rule, False] for rule in write_rules]
        self.buf = b''
        self.fired = []

    def describe(self):
        return 'tamper+' + self.inner.describe()

    def open(self):
        self.buf = b''
        return self.inner.open()

    def close(self):
        self.inner.close()

    def read(self):
        data = self.inner.read()
        if not data:
            return data
        self.buf += data
        lines = self.buf.split(b'\n')
        self.buf = lines.pop()
        out = []
        for raw in lines:
            out.extend(self._read_line(raw))
        return b''.join(line + b'\n' for line in out)

    def _read_line(self, raw):
        try:
            seq, msg = ef1.decode_e(raw.decode('ascii').rstrip('\r'))
        except (ef1.LineError, UnicodeDecodeError):
            return [raw]
        for entry in self.read_rules:
            if not entry[1]:
                replaced = entry[0](seq, msg, raw)
                if replaced is not None:
                    entry[1] = True
                    self.fired.append((getattr(entry[0], 'name', 'read'), seq))
                    return replaced
        return [raw]

    def write(self, data):
        try:
            seq, msg = ef1.decode_q(data.decode('ascii').rstrip('\n'))
        except (ef1.LineError, UnicodeDecodeError):
            seq, msg = None, None
        if msg is not None:
            for entry in self.write_rules:
                if not entry[1]:
                    replaced = entry[0](seq, msg, data)
                    if replaced is not None:
                        entry[1] = True
                        self.fired.append((getattr(entry[0], 'name', 'write'), seq))
                        if replaced:
                            self.inner.write(replaced)
                        return
        self.inner.write(data)


def rule(name, fn):
    fn.name = name
    return fn


def corrupt_nth_event(n):
    seen = []

    def fn(seq, msg, raw):
        if msg.get('t') == 'ev' and seq not in seen:
            seen.append(seq)
            if len(seen) == n:
                return [flip(raw)]
        return None
    return rule('corrupt_event_%d' % n, fn)


def split_event(etype):
    """The line of the first etype event cut in two by a kernel message, as a console without dmesg -n 1 does."""
    def fn(seq, msg, raw):
        if msg.get('t') == 'ev' and _event_type(msg) == etype:
            half = len(raw) // 2
            return [raw[:half] + b'\r', b'[ 4242.424242] xilinx-dma a0000000.dma: noise on the console\r', raw[half:]]
        return None
    return rule('split_' + etype, fn)


def noise_before_event(etype):
    def fn(seq, msg, raw):
        if msg.get('t') == 'ev' and _event_type(msg) == etype:
            return [b'[   17.000001] random: crng init done\r', b'ubuntu@kv260:~$ \r', raw]
        return None
    return rule('noise_' + etype, fn)


def drop_event(etype):
    def fn(seq, msg, raw):
        if msg.get('t') == 'ev' and _event_type(msg) == etype:
            return []
        return None
    return rule('drop_' + etype, fn)


def damage_q(op):
    def fn(seq, msg, data):
        if msg.get('op') == op:
            return flip(data.rstrip(b'\n')) + b'\n'
        return None
    return rule('damage_q_' + op, fn)


def drop_q(op):
    def fn(seq, msg, data):
        return b'' if msg.get('op') == op else None
    return rule('drop_q_' + op, fn)


def relay_argv(gateway_port, *extra):
    return [sys.executable, '-B', RELAY, '--gateway', 'http://127.0.0.1:%d' % gateway_port, '--heartbeat-s', '0.5'] + \
        list(extra)


def tune(link, **changes):
    for name, value in dict(FAST, **changes).items():
        setattr(link, name, value)


class Harness:
    """owner_bridge.Bridge on 127.0.0.1:<free port> -> cmd: serial_relay.py -> the gateway at gateway_port."""

    def __init__(self, gateway_port, read_rules=(), write_rules=(), argv=None):
        self.errors = tempfile.TemporaryFile()
        self.cmd = ob.CmdTransport(argv or relay_argv(gateway_port), stderr=self.errors)
        self.transport = Tamper(self.cmd, read_rules, write_rules) if (read_rules or write_rules) else self.cmd
        self.bridge = ob.Bridge(self.transport, port=0, ui_dir=UI_DIR)
        self.link = self.bridge.link
        tune(self.link)
        self.bridge.bind()
        self.port = self.bridge.port
        if not self.bridge.start(30):
            self.close()
            raise AssertionError('the link did not come up')

    def stats(self):
        return self.link.snapshot()['stats']

    def relay_stats(self, after=None):
        """The relay's counters from a heartbeat written after this call (or after `after` seconds)."""
        time.sleep(1.2 if after is None else after)
        return self.link.snapshot()['relay_stats']

    def write_raw(self, line):
        self.cmd.write(line.encode('ascii') + b'\n')

    def close(self):
        try:
            self.bridge.close()
        finally:
            self.errors.close()


def assert_secure(test, headers):
    test.assertEqual(headers.get('content-security-policy'), ob.CSP)
    test.assertEqual(headers.get('x-content-type-options'), 'nosniff')
    test.assertEqual(headers.get('cache-control'), 'no-store')
    test.assertNotIn('access-control-allow-origin', headers)


def compare_run(test, stub, run, got, coalesced=None):
    """The relayed events of run against the stub's: every event other than progress.io identical and in order, the
    progress.io events a subsequence ending with the last one; with coalesced, exactly the missing ones."""
    test.assertTrue(got, 'no events')
    seqs = [e['seq'] for e in got]
    test.assertEqual(seqs, sorted(set(seqs)), 'seqs not strictly increasing')
    mine = [e for e in got if e['run'] == run]
    want = stub.of_run(run)
    test.assertEqual([e for e in mine if e['type'] != 'progress.io'],
                     [e for e in want if e['type'] != 'progress.io'])
    io_got = [e for e in mine if e['type'] == 'progress.io']
    io_want = [e for e in want if e['type'] == 'progress.io']
    by_seq = dict((e['seq'], e) for e in io_want)
    for e in io_got:
        test.assertEqual(e, by_seq.get(e['seq']))
    test.assertTrue(io_got and io_got[-1] == io_want[-1], 'the last progress.io must arrive (latest wins)')
    if coalesced is not None:
        test.assertEqual(len(io_got) + coalesced, len(io_want))


# -------------------------------------------------------------------------------------------------- fake console
SHELL_INIT = r'''
stty() { :; }
sudo() {
  local validate=0
  while [ $# -gt 0 ]; do
    case $1 in
      -S) IFS= read -r _fake_pw || return 1
          [ "$_fake_pw" = "$FAKE_SUDO_PW" ] || { echo 'Sorry, try again.' >&2; return 1; }
          : > "$FAKE_SUDO_STAMP"; shift ;;
      -p) shift 2 ;;
      -n) [ -f "$FAKE_SUDO_STAMP" ] || { echo 'sudo: a password is required' >&2; return 1; }; shift ;;
      -v) validate=1; shift ;;
      --) shift; break ;;
      *) break ;;
    esac
  done
  [ "$validate" = 1 ] && return 0
  "$@"
}
cd ~
'''


class FakeConsole:
    """A stand-in for the KV260 serial console behind kv_console.KV: a getty-style login, then bash reading the typed
    lines (CR becomes LF as with ICRNL; output LF becomes CR LF as with ONLCR). When the shell ends (after 'exit', or
    when the relay started with exec stops), the login prompt comes back. /tmp/ in a command is the board's own
    temporary directory. damage_once() drops one byte of the next long output chunk and puts a kernel line in."""

    def __init__(self, bash, base):
        self.bash = bash
        self.home = os.path.join(base, 'home')
        self.tmp = os.path.join(base, 'tmp')
        os.makedirs(self.home)
        os.makedirs(self.tmp)
        self.password = 'fake-console-' + secrets.token_hex(4)
        self.stamp = os.path.join(base, 'sudo.stamp')
        self.cond = threading.Condition()
        self.outbuf = bytearray()
        self.pending = b''
        self.state = 'login'
        self.user = None
        self.proc = None
        self.logins = 0
        self.login_lines = []
        self.damage = False
        self.timeout = 0.2

    # the part of serial.Serial that kv_console uses
    def read(self, n=4096):
        with self.cond:
            if not self.outbuf:
                self.cond.wait(self.timeout)
            data = bytes(self.outbuf[:n])
            del self.outbuf[:n]
            return data

    def reset_input_buffer(self):
        with self.cond:
            self.outbuf.clear()

    def close(self):
        pass                                # the port closes; the console session itself stays as it is

    def write(self, data):
        data = data.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
        with self.cond:
            self.pending += data
            lines = self.pending.split(b'\n')
            self.pending = lines.pop()
        for line in lines:
            self._line(line)
        return len(data)

    def damage_once(self):
        self.damage = True

    def _emit(self, data):
        with self.cond:
            self.outbuf += data
            self.cond.notify_all()

    def _line(self, line):
        if self.state == 'login':
            if line:
                self.login_lines.append(line)
                self.user = line.decode('ascii', 'replace')
                self.state = 'password'
                self._emit(b'Password: ')
            else:
                self._emit(b'\r\nkv260 login: ')
        elif self.state == 'password':
            if line.decode('ascii', 'replace') == self.password and self.user == 'ubuntu':
                self.state = 'shell'
                self.logins += 1
                self._start_shell()
                self._emit(b'\r\nWelcome to the fake KV260 console\r\nubuntu@kv260:~$ ')
            else:
                self.state = 'login'
                self._emit(b'\r\nLogin incorrect\r\nkv260 login: ')
        else:
            proc = self.proc
            if proc is None:
                return
            if not line:
                self._emit(b'ubuntu@kv260:~$ ')
            if not line.startswith(b'@EF1'):
                line = line.replace(b'/tmp/', bash_path(self.tmp).encode('ascii') + b'/')
            try:
                proc.stdin.write(line + b'\n')
                proc.stdin.flush()
            except OSError:
                pass

    def _start_shell(self):
        env = dict(os.environ)
        env.update({'HOME': bash_path(self.home), 'FAKE_SUDO_PW': self.password,
                    'FAKE_SUDO_STAMP': bash_path(self.stamp), 'LC_ALL': 'C'})
        env.pop('KV_PW', None)
        env.pop('MSYS_NO_PATHCONV', None)       # Git's openssl and Python are native: /c/... arguments need converting
        self.proc = subprocess.Popen([self.bash, '--noprofile', '--norc', '-s'], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=self.home,
                                     bufsize=0)
        self.proc.stdin.write(SHELL_INIT.encode('ascii'))
        self.proc.stdin.flush()
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        while True:
            try:
                chunk = os.read(proc.stdout.fileno(), 65536)
            except OSError:
                chunk = b''
            if not chunk:
                break
            chunk = chunk.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
            if self.damage and len(chunk) > 600:
                self.damage = False
                i = len(chunk) // 2
                chunk = chunk[:i] + b'\r\n[ 99.000001] xilinx-dma: lost byte\r\n' + chunk[i + 1:]
            self._emit(chunk)
        proc.wait()
        with self.cond:
            if self.proc is proc:
                self.proc = None
                self.state = 'login'
        self._emit(b'\r\nkv260 login: ')

    def shutdown(self):
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def fake_kv(console):
    kv = kv_console.KV.__new__(kv_console.KV)
    kv.s = console
    return kv


# ============================================================================================================
class Codec(unittest.TestCase):
    def test_round_trips_and_crc(self):
        msg = {'t': 'ev', 'sub': 3, 'id': '17', 'data': '{"type":"result","note":"第 3 級（偏高）—"}'}
        for seq in (0, 1, 42, 10 ** 9):
            line = ef1.encode_e(seq, msg)
            self.assertTrue(line.isascii() and line.startswith('@EF1 E %d ' % seq))
            self.assertEqual(ef1.decode_e(line), (seq, msg))
            q = ef1.encode_q(seq, {'op': 'req', 'b': 'a' * 16, 'path': '/api/v1/status', 'body': '中文'})
            self.assertEqual(ef1.decode_q(q)[0], seq)
        line = ef1.encode_e(7, msg)
        head = len('@EF1 E ')
        for i in range(head, len(line), 7):           # any single changed character is caught
            if line[i] == ' ':
                continue
            bad = line[:i] + ('1' if line[i] != '1' else '2') + line[i + 1:]
            with self.assertRaises(ef1.LineError):
                ef1.decode_e(bad)
        moved = line.replace('@EF1 E 7 ', '@EF1 E 8 ', 1)  # the sequence number is covered by the CRC
        with self.assertRaises(ef1.LineError) as e:
            ef1.decode_e(moved)
        self.assertEqual(e.exception.code, 'CRC')
        q = ef1.encode_q(5, {'op': 'ack', 'b': 'b' * 16, 'ea': 3})
        with self.assertRaises(ef1.LineError) as e:
            ef1.decode_q(q.replace('@EF1 Q 5 ', '@EF1 Q 6 ', 1))
        self.assertEqual(e.exception.code, 'CRC')

    def test_request_line_limit(self):
        self.assertEqual(ef1.MAX_Q_LINE, 1000)
        long_q = ef1.encode_q(1, {'op': 'req', 'b': 'c' * 16, 'path': '/api/v1/status', 'pad': 'x' * 900})
        self.assertGreater(len(long_q), 1000)
        with self.assertRaises(ef1.LineError) as e:
            ef1.decode_q(long_q)
        self.assertEqual(e.exception.code, 'TOO_LONG')

    def test_body_parts_and_whitelist(self):
        text = json.dumps(FORM, ensure_ascii=False)
        parts = ef1.body_parts(text)
        self.assertGreater(len(parts), 2)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(ef1.dumps(p)) <= ef1.PART_BYTES for p in parts))
        self.assertEqual(ef1.body_parts(''), [''])
        ok = [('GET', '/api/v1/status', None), ('GET', '/api/v1/runs/r0123456789ab', None),
              ('POST', '/api/v1/submit', '{}')]
        for method, path, body in ok:
            self.assertIsNone(ef1.request_refusal(method, path, body), path)
        refused = [('GET', '/api/v1/events', None), ('GET', '/', None), ('GET', '/api/v1/runs/../x', None),
                   ('POST', '/api/v1/status', '{}'), ('PUT', '/api/v1/submit', '{}'), ('GET', '/api/v1/status', 'x'),
                   ('POST', '/api/v1/submit', 'x' * 4097), ('POST', '/api/v1/submit', None)]
        for method, path, body in refused:
            self.assertIsNotNone(ef1.request_refusal(method, path, body), (method, path))
        for good in ('http://127.0.0.1:8080', 'http://localhost:8080/'):
            ef1.parse_gateway(good)
        for bad in ('http://192.0.2.10:8080', 'https://127.0.0.1:8080', 'http://127.0.0.1', 'http://127.0.0.1:8080/x'):
            with self.assertRaises(ValueError):
                ef1.parse_gateway(bad)

    def test_screen_policy_matches_the_gateway(self):
        if EDGE not in sys.path:
            sys.path.insert(0, EDGE)
        from efgw import httpapi
        self.assertEqual(ob.CSP, httpapi.CSP)
        self.assertEqual(ob.SECURITY_HEADERS, httpapi.SECURITY_HEADERS)
        self.assertEqual(ob.STATIC_TYPES, httpapi.STATIC_TYPES)
        self.assertEqual(ob.INDEX_PATHS, httpapi.INDEX_PATHS)
        self.assertEqual(ob.SEGMENT.pattern, httpapi.SEGMENT.pattern)
        self.assertEqual((ob.MAX_BODY, ob.STATIC_DEPTH, ob.HEARTBEAT_S, ob.RETRY_MS),
                         (httpapi.MAX_BODY, httpapi.STATIC_DEPTH, httpapi.HEARTBEAT_S, httpapi.RETRY_MS))
        self.assertEqual(ob.OWNER_HEADER, httpapi.OWNER_HEADER)
        self.assertLessEqual(ob.STREAMS_MAX, httpapi.SSE_STREAMS)
        self.assertEqual(ef1.MAX_BODY, httpapi.MAX_BODY)

    def test_kv_console_is_the_committed_version(self):
        self.assertEqual(ob.kv_console_problems(kv_console), [])
        old = types.ModuleType('old_kv_console')
        old.KV = type('KV', (), {'run': lambda self, cmd, timeout=30: None, 'login': lambda self, u, p: None})
        old.stream = lambda kv, cmd, timeout, on_line: None
        self.assertTrue(ob.kv_console_problems(old))


class RelayQueue(unittest.TestCase):
    """The relay's output queue in-process (no thread, no gateway)."""

    def setUp(self):
        self.relay = ef1.Relay(('127.0.0.1', 9), write=lambda data: None)
        self.epoch = 'e' * 16
        self.relay._hello(self.epoch)

    def drain(self):
        out = []
        with self.relay.cond:
            while True:
                lines = self.relay._next_lines()
                if lines is None:
                    return out
                out.extend(ef1.decode_e(line) for line in lines)

    def ev(self, n, etype):
        data = json.dumps({'seq': n, 'type': etype})
        key = (1, 'progress') if etype == 'progress.io' else None
        self.relay.put(self.epoch, {'t': 'ev', 'sub': 1, 'id': str(n), 'data': data}, key)

    def test_progress_coalesced_latest_wins_only_under_backlog(self):
        types_ = ['progress.io', 'progress.io', 'result', 'progress.io', 'progress.io', 'progress.io']
        for n, etype in enumerate(types_, 1):
            self.ev(n, etype)
        got = self.drain()                              # a short queue: every sample goes out, exact markers
        self.assertEqual([m['id'] for _, m in got[1:]], ['1', '2', '3', '4', '5', '6'])
        self.assertEqual(self.relay.stats['coalesced'], 0)
        self.relay.coalesce_backlog = 0                 # as if the console were far behind
        for n, etype in enumerate(types_, 7):
            self.ev(n, etype)
        got = self.drain()
        self.assertEqual([m['t'] for _, m in got], ['ev', 'ev', 'ev'])
        self.assertEqual([m['id'] for _, m in got], ['8', '9', '12'])
        self.assertEqual([s for s, _ in got], [8, 9, 10])
        self.assertEqual(self.relay.stats['coalesced'], 3)

    def test_go_back_n_and_reset(self):
        for n in range(1, 6):
            self.ev(n, 'result')
        self.assertEqual([s for s, _ in self.drain()], [1, 2, 3, 4, 5, 6])
        with self.relay.cond:
            self.relay._acked(2)
            self.relay._nak(4)
        self.assertEqual([s for s, _ in self.drain()], [4, 5, 6])
        with self.relay.cond:
            self.relay._nak(4)                          # the same retransmission is already under way
        self.assertEqual(self.drain(), [])
        with self.relay.cond:
            self.relay._nak(2)                          # released by the ack of 2: the laptop must start again
        got = self.drain()
        self.assertEqual([(s, m['t'], m['why']) for s, m in got], [(0, 'reset', 'LINES_RELEASED')])
        self.relay.heartbeat()
        hb = self.drain()[0][1]
        self.assertEqual((hb['t'], hb['eseq'], hb['b']), ('hb', 6, self.epoch))

    def test_stale_epoch_lines_dropped(self):
        self.ev(1, 'result')
        self.relay._hello('f' * 16)                     # a new epoch drops what the old one still had queued
        got = self.drain()
        self.assertEqual([(s, m['t'], m['b']) for s, m in got], [(1, 'hello', 'f' * 16)])


class BridgeRoundTrip(unittest.TestCase):
    """owner_bridge.py (cmd: transport) -> serial_relay.py (child process) -> stub gateway."""

    @classmethod
    def setUpClass(cls):
        cls.stub = StubGateway()
        cls.h = Harness(cls.stub.port)

    @classmethod
    def tearDownClass(cls):
        cls.h.close()
        cls.stub.close()

    def test_static_owner_screen_and_screening(self):
        p, before = self.h.port, len(self.stub.requests)
        r = call(p, 'GET', '/')
        self.assertEqual(r.status, 200)
        assert_secure(self, r.headers)
        with open(os.path.join(UI_DIR, 'index.html'), 'rb') as f:
            self.assertEqual(r.body, f.read())
        for path in ('/ui/app.js', '/lib.mjs', '/ui/style.css?shot=all', '/ui/', '/index.html'):
            self.assertEqual(call(p, 'GET', path).status, 200, path)
        for path in ('/ui/../owner/owner_bridge.py', '/ui/%2e%2e/x.js', '/ui/tests/check_ui.py', '/../../etc/passwd',
                     '/ui/.hidden.js', '/ui/fixtures/../../model/form_cdc.json', '/ui/%2Fetc%2Fpasswd.js',
                     '/README.md'):
            self.assertEqual(call(p, 'GET', path).status, 404, path)
        self.assertEqual(call(p, 'GET', '/api/v1/status', host='evil:%d' % p).code(), 'HOST_FORBIDDEN')
        r = call(p, 'GET', '/api/v1/status', headers=[('Origin', 'http://evil.example')])
        self.assertEqual((r.status, r.code()), (403, 'ORIGIN_FORBIDDEN'))
        self.assertEqual(submit(p, M, headers=[('Content-Type', 'application/json')]).code(), 'OWNER_HEADER_REQUIRED')
        r = submit(p, M, headers=[('Content-Type', 'text/plain'), (ob.OWNER_HEADER, '1')])
        self.assertEqual((r.status, r.code()), (403, 'CONTENT_TYPE_JSON'))
        r = submit(p, M, raw=b'{' + b' ' * 5000 + b'}')
        self.assertEqual((r.status, r.code()), (403, 'BODY_TOO_LARGE'))
        r = submit(p, M, headers=[('Content-Type', 'application/json'), (ob.OWNER_HEADER, '1'),
                                  ('Origin', 'http://127.0.0.1:%d' % self.stub.port)])
        self.assertEqual(r.code(), 'ORIGIN_FORBIDDEN')        # the gateway's own origin is cross-origin here
        self.assertEqual(call(p, 'GET', '/api/v1/submit').status, 405)
        self.assertEqual(call(p, 'GET', '/api/v1/nothing').status, 404)
        self.assertEqual(call(p, 'GET', '/api/v1/events?since=x').code(), 'QUERY_INVALID')
        self.assertIn('linked', call(p, 'GET', ob.BRIDGE_STATUS).json())
        self.assertEqual(len(self.stub.requests), before, 'a refused request reached the gateway')

    def test_status_relayed_in_parts(self):
        r = call(self.h.port, 'GET', '/api/v1/status')
        self.assertEqual(r.status, 200)
        assert_secure(self, r.headers)
        self.assertEqual(r.headers['content-type'], 'application/json; charset=utf-8')
        body = r.json()
        self.assertEqual(body['form'], FORM)                  # zh-TW labels intact
        self.assertEqual((body['rehearsal'], body['retain_private']), (False, False))     # the status contract fields
        self.assertGreater(len(r.body), 3 * ef1.PART_BYTES)    # so it crossed the console in several resp parts
        seen = self.stub.recorded('GET', '/api/v1/status')[-1]
        headers = dict((k.lower(), v) for k, v in seen['headers'])
        self.assertEqual(headers['host'], '127.0.0.1:%d' % self.stub.port)
        self.assertNotIn('origin', headers)

    def test_submit_and_event_stream_round_trip(self):
        p = self.h.port
        stream = SSE(p, '?since=%d' % self.stub.last_seq())
        try:
            self.assertEqual(stream.status, 200)
            self.assertEqual(stream.headers['content-type'], 'text/event-stream; charset=utf-8')
            assert_secure(self, stream.headers)
            first = stream.block()
            self.assertEqual((first.get('retry'), first['comments']), ('2000', ['heartbeat']))
            r = submit(p, M)
            self.assertEqual(r.status, 202, r.body)
            run = r.json()['run']
            self.assertRegex(run, RUN)
            got = stream.until(run)
        finally:
            stream.close()
        compare_run(self, self.stub, run, got)
        result = [e for e in got if e['type'] == 'result'][0]['data']
        self.assertEqual((result['level_label'], result['boundary_text']), ('第 3 級（偏高）', '介於 L2／L3，不判定'))
        self.assertIs([e for e in got if e['type'] == 'wiped'][-1]['data']['retained_private'], False)
        posted = self.stub.recorded('POST', '/api/v1/submit')[-1]
        headers = [(k.lower(), v) for k, v in posted['headers']]
        names = [k for k, _ in headers]
        self.assertEqual(dict(headers)['host'], '127.0.0.1:%d' % self.stub.port)
        self.assertEqual(names.count('x-edgefhe-owner'), 1)
        self.assertEqual(dict(headers)['x-edgefhe-owner'], '1')
        self.assertEqual(dict(headers)['content-type'], 'application/json')
        self.assertNotIn('origin', names)                     # the browser sent one; the relay never forwards it
        self.assertEqual(json.loads(posted['body'].decode('utf-8')), {'form': M, 'backend': 'pl'})

    def test_busy_submit_relayed_409_and_run_events(self):
        p = self.h.port
        self.stub.hold.clear()
        try:
            r = submit(p, M)
            self.assertEqual(r.status, 202, r.body)
            run = r.json()['run']
            again = submit(p, M)
            self.assertEqual((again.status, again.code()), (409, 'BUSY'))
            self.assertEqual(again.json()['message'], BUSY_MESSAGE)
            assert_secure(self, again.headers)
        finally:
            self.stub.hold.set()
        self.assertTrue(wait_for(lambda: self.stub.of_run(run) and self.stub.of_run(run)[-1]['type'] == 'wiped', 30))
        r = call(p, 'GET', '/api/v1/runs/%s' % run)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json(), {'run': run, 'events': self.stub.of_run(run)})
        unknown = call(p, 'GET', '/api/v1/runs/r000000000000')
        self.assertEqual((unknown.status, unknown.code()), (404, 'RUN_UNKNOWN'))

    def test_resume_arguments_forwarded(self):
        if self.stub.last_seq() < 6:
            r = submit(self.h.port, M)
            self.assertEqual(r.status, 202)
            run = r.json()['run']
            self.assertTrue(wait_for(lambda: self.stub.of_run(run)[-1]['type'] == 'wiped', 30))
        stream = SSE(self.h.port, '?since=3', headers=[('Last-Event-ID', '5'),
                                                       ('Origin', 'http://127.0.0.1:%d' % self.h.port)])
        try:
            self.assertEqual(stream.status, 200)
            first = stream.envelope()                       # the gateway resumes after the larger point (5)
            self.assertGreaterEqual(first['seq'], 6)        # a replayed burst may coalesce waiting progress.io
            self.assertEqual(first, self.stub.events[first['seq'] - 1])
        finally:
            stream.close()
        seen = self.stub.recorded('GET', '/api/v1/events')[-1]
        headers = dict((k.lower(), v) for k, v in seen['headers'])
        self.assertEqual((seen['path'], headers.get('last-event-id')), ('/api/v1/events?since=3', '5'))
        self.assertNotIn('origin', headers)
        self.assertEqual(headers['host'], '127.0.0.1:%d' % self.stub.port)

    def test_refused_stream_relayed(self):
        self.stub.refuse_streams = (503, {'code': 'STREAMS_BUSY', 'message': '同時開啟的事件串流已達上限（8 條）。'})
        try:
            r = call(self.h.port, 'GET', '/api/v1/events?since=0')
        finally:
            self.stub.refuse_streams = None
        self.assertEqual((r.status, r.code()), (503, 'STREAMS_BUSY'))
        self.assertEqual(r.json()['message'], '同時開啟的事件串流已達上限（8 條）。')
        assert_secure(self, r.headers)


class LinkFaults(unittest.TestCase):
    """Damaged, split, lost and overlong lines in both directions, and a relay that goes away."""

    @classmethod
    def setUpClass(cls):
        cls.stub = StubGateway()

    @classmethod
    def tearDownClass(cls):
        cls.stub.close()

    def harness(self, *args, **kwargs):
        h = Harness(self.stub.port, *args, **kwargs)
        self.addCleanup(h.close)
        return h

    def run_form(self, h):
        stream = SSE(h.port, '?since=%d' % self.stub.last_seq())
        try:
            stream.block()
            r = submit(h.port, M)
            self.assertEqual(r.status, 202, r.body)
            run = r.json()['run']
            return run, stream.until(run, limit_s=60)
        finally:
            stream.close()

    def test_crc_corrupted_line_dropped_and_resynced(self):
        self.stub.pause_s = 0.6             # the first retransmission is over before the next fault
        try:
            h = self.harness(read_rules=[corrupt_nth_event(3), split_event('client.exited'),
                                         noise_before_event('result')])
            run, got = self.run_form(h)
        finally:
            self.stub.pause_s = 0.05
        relay_stats = h.relay_stats()
        compare_run(self, self.stub, run, got, coalesced=relay_stats.get('coalesced', 0))
        fired = [name for name, _ in h.transport.fired]
        self.assertEqual(sorted(fired), sorted(['corrupt_event_3', 'split_client.exited', 'noise_result']))
        stats = h.stats()
        self.assertGreaterEqual(stats.get('crc_bad', 0), 2)           # the corrupted line and the split line's head
        self.assertGreaterEqual(stats.get('ignored', 0), 4)           # kernel lines, a prompt, the split line's tail
        self.assertGreaterEqual(stats.get('resynced', 0), 2)          # each loss repaired by one retransmission
        self.assertGreaterEqual(stats.get('nak', 0), 2)
        self.assertGreaterEqual(relay_stats.get('resent', 0), 2)
        self.assertEqual(stats.get('epochs'), 1)                      # repaired in place, no new epoch
        self.assertEqual(call(h.port, 'GET', '/api/v1/status').status, 200)

    def test_lost_last_line_found_by_the_heartbeat(self):
        h = self.harness(read_rules=[drop_event('wiped')])          # no later line would reveal this loss
        run, got = self.run_form(h)
        compare_run(self, self.stub, run, got)
        self.assertEqual([name for name, _ in h.transport.fired], ['drop_wiped'])
        stats = h.stats()
        self.assertEqual((stats.get('crc_bad', 0), stats.get('gap', 0)), (0, 0))
        self.assertGreaterEqual(stats.get('nak', 0), 1)               # asked for because hb.eseq ran ahead
        self.assertGreaterEqual(stats.get('resynced', 0), 1)

    def test_damaged_and_lost_request_lines_resent(self):
        h = self.harness(write_rules=[damage_q('req'), drop_q('sub')])
        r = call(h.port, 'GET', '/api/v1/status')
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()['form'], FORM)
        run, got = self.run_form(h)                   # its 'sub' line was lost once and resent by the timer
        compare_run(self, self.stub, run, got)
        relay_stats = h.relay_stats()
        self.assertGreaterEqual(relay_stats.get('q_bad', 0), 1)
        self.assertGreaterEqual(relay_stats.get('qnak', 0), 1)
        self.assertGreaterEqual(h.stats().get('q_resent', 0), 2)
        self.assertEqual(sorted(name for name, _ in h.transport.fired), ['damage_q_req', 'drop_q_sub'])

    def test_request_line_over_1000_characters_refused(self):
        h = self.harness()
        before = len(self.stub.recorded('POST'))
        body = json.dumps({'form': M, 'backend': 'pl', 'pad': 'x' * 900}).encode('utf-8')
        self.assertLess(len(body), ob.MAX_BODY)
        r = submit(h.port, M, raw=body)
        self.assertEqual((r.status, r.code()), (413, 'BRIDGE_LINE_TOO_LONG'))
        self.assertIn('1000', r.json()['message'])
        self.assertEqual(h.stats().get('q_too_long'), 1)
        epoch = h.link.epoch
        crafted = ef1.encode_q(h.link.q_seq + 1, {'op': 'req', 'b': epoch, 'ea': 0, 'method': 'GET',
                                                  'path': '/api/v1/status', 'pad': 'y' * 800})
        self.assertGreater(len(crafted), ef1.MAX_Q_LINE)
        h.write_raw(crafted)                                  # well formed, but over the limit: never executed
        h.write_raw('@EF1 Q 9 ' + 'A' * 1191)
        h.write_raw('@EF1 Q 9 ' + 'A' * 70000)                # longer than the relay's read buffer
        relay_stats = wait_for(lambda: h.link.snapshot()['relay_stats'].get('q_too_long', 0) >= 3 and
                               h.link.snapshot()['relay_stats'], 10)
        self.assertTrue(relay_stats, h.link.snapshot())
        self.assertEqual(relay_stats.get('q_too_long'), 3)
        self.assertEqual(h.link.snapshot()['relay_errors'].get('Q_LINE_TOO_LONG'), 3)
        self.assertEqual(len(self.stub.recorded('POST')), before)
        self.assertEqual(call(h.port, 'GET', '/api/v1/status').status, 200)       # the link carries on

    def test_console_noise_ignored_both_ways(self):
        h = self.harness(read_rules=[noise_before_event('form.accepted')])
        for line in ('ubuntu@kv260:~$ ', '[   77.123456] xilinx-dma a0000000.dma: channel error', 'login: ',
                     '@EF1 X 1 not-a-line', '@EF1 Q 1 !!!not-base64!!!'):
            h.write_raw(line)
        relay_stats = h.relay_stats()
        self.assertGreaterEqual(relay_stats.get('ignored', 0), 4)
        self.assertGreaterEqual(relay_stats.get('q_bad', 0), 1)
        run, got = self.run_form(h)
        compare_run(self, self.stub, run, got)
        self.assertGreaterEqual(h.stats().get('ignored', 0), 2)

    def test_relay_restart_reattaches(self):
        h = self.harness()
        stream = SSE(h.port, '?since=%d' % self.stub.last_seq())
        stream.block()
        first_pid = h.cmd.proc.pid
        h.cmd.proc.kill()                                     # the local test relay, never an encryption client
        with self.assertRaises((EOFError, OSError)):
            while True:
                stream.block()
        stream.close()

        def relinked():
            proc = h.cmd.proc
            return h.link.snapshot()['linked'] and proc is not None and proc.pid != first_pid
        self.assertTrue(wait_for(relinked, 20), h.link.snapshot())
        self.assertEqual(call(h.port, 'GET', '/api/v1/status').status, 200)
        stats = h.stats()
        self.assertGreaterEqual(stats.get('link_down', 0), 1)
        self.assertGreaterEqual(stats.get('epochs', 0), 2)
        self.assertTrue(any(line.startswith('OWNER_BRIDGE_LINK_DOWN reason=TRANSPORT_CLOSED') for line in LOG))

    def test_gateway_unreachable(self):
        h = self.harness(argv=relay_argv(free_port()))
        r = call(h.port, 'GET', '/api/v1/status')
        self.assertEqual((r.status, r.code()), (502, 'RELAY_GATEWAY_UNREACHABLE'))
        self.assertIn('edgefhe-demo', r.json()['message'])
        r = call(h.port, 'GET', '/api/v1/events?since=0')
        self.assertEqual((r.status, r.code()), (502, 'RELAY_GATEWAY_UNREACHABLE'))
        r = submit(h.port, M)
        self.assertEqual((r.status, r.code()), (502, 'RELAY_GATEWAY_UNREACHABLE'))


class SerialConsole(unittest.TestCase):
    """SerialTransport through the real kv_console.KV on a fake console."""

    def setUp(self):
        self.bash = git_bash()
        if self.bash is None:
            self.skipTest('bash (Git Bash on Windows) is not available')
        self.base = tempfile.mkdtemp(prefix='edgefhe-console-')
        self.console = FakeConsole(self.bash, self.base)
        self.stub = StubGateway()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.addCleanup(self.console.shutdown)
        self.addCleanup(self.stub.close)

    def transport(self, env):
        return ob.SerialTransport('FAKE', relay=bash_path(RELAY), python=bash_path(sys.executable),
                                  gateway='http://127.0.0.1:%d' % self.stub.port,
                                  kv_factory=lambda port: fake_kv(self.console), probe_s=3.0, env=env)

    def bridge(self, env):
        b = ob.Bridge(self.transport(env), port=0, ui_dir=UI_DIR)
        tune(b.link, dead_s=7.0, alive_s=6.0)
        b.bind()
        return b

    def test_login_exec_relay_reuse_and_bye(self):
        env = {'KV_PW': self.console.password}
        first = self.bridge(env)
        try:
            self.assertTrue(first.start(40), first.link.snapshot())
            self.assertEqual(self.console.logins, 1)
            self.assertEqual(self.console.login_lines, [b'ubuntu'])     # only the user name met the login prompt
            r = call(first.port, 'GET', '/api/v1/status')
            self.assertEqual(r.status, 200)
            self.assertEqual(r.json()['form'], FORM)
            self.assertIn(ob.OPERATOR['RELAY_STARTED'] % bash_path(RELAY), LOG)
        finally:
            with first.link.lock:                             # the bridge goes away without 'bye' (a crash)
                first.link.closing = True
                first.link.lock.notify_all()
            first.server.shutdown()
            first.server.server_close()
        time.sleep(0.5)
        second = self.bridge({})                              # no KV_PW: the running relay needs no login
        try:
            self.assertTrue(second.start(40), second.link.snapshot())
            self.assertEqual(self.console.logins, 1)
            self.assertIn(ob.OPERATOR['RELAY_ATTACHED'], LOG)
            self.assertEqual(call(second.port, 'GET', '/api/v1/status').status, 200)
        finally:
            second.close()
        self.assertTrue(wait_for(lambda: self.console.state == 'login', 10), self.console.state)
        self.assertEqual(self.console.login_lines, [b'ubuntu'])

    def test_missing_password_and_missing_relay(self):
        t = self.transport({})
        with self.assertRaises(ob.TransportError) as e:
            t.open()
        self.assertIn('KV_PW', str(e.exception))
        self.assertEqual(self.console.login_lines, [])
        t = ob.SerialTransport('FAKE', relay='/opt/edgefhe-demo/owner/no_such_relay.py', python='python3',
                               kv_factory=lambda port: fake_kv(self.console), probe_s=0.5,
                               env={'KV_PW': self.console.password})
        with self.assertRaises(ob.TransportError) as e:
            t.open()
        self.assertIn('no_such_relay.py', str(e.exception))
        t.close()                                             # logged in, relay never started: logout
        self.assertTrue(wait_for(lambda: self.console.state == 'login', 10))


class CloudPcRule(unittest.TestCase):
    """The bridge never runs on the cloud PC: an explicit acknowledgment, and a refusal on any sign of the cloud PC."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='edgefhe-rule-')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.marker = os.path.join(self.base, 'relay_started')
        self.transport = 'cmd:' + json.dumps([sys.executable, '-c', 'open(%r, "w").close()' % self.marker])

    def main(self, *argv, signs=lambda: []):
        port = free_port()
        start = len(LOG)
        code = ob.main(list(argv) + ['--transport', self.transport, '--listen', '127.0.0.1:%d' % port], signs=signs)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', port))                 # the bridge never listened on its port
        return code, LOG[start:]

    def test_main_requires_the_acknowledgment(self):
        code, log = self.main()
        self.assertEqual(code, ob.EXIT_USAGE)
        self.assertIn('OWNER_BRIDGE_USAGE code=OWNER_LAPTOP', log)
        self.assertIn(ob.OPERATOR['USAGE_OWNER_LAPTOP'], log)
        self.assertIn(ob.OWNER_LAPTOP_FLAG, ob.OPERATOR['USAGE_OWNER_LAPTOP'])
        self.assertFalse(os.path.exists(self.marker), 'the relay was started')

    def test_main_refuses_on_the_cloud_pc(self):
        calls = []

        def signs():
            calls.append(1)
            return [('CONTAINER', ob.OPERATOR['SIGN_CONTAINER'] % ('efhe-cloud', 'edgefhe-demo-cloud:dev')),
                    ('FOLDER', ob.OPERATOR['SIGN_DIR'] % 'C:\\EdgeFHE_evidence')]
        code, log = self.main(ob.OWNER_LAPTOP_FLAG, signs=signs)
        self.assertEqual((code, calls), (ob.EXIT_USAGE, [1]))
        self.assertIn('OWNER_BRIDGE_REFUSED code=CLOUD_PC signs=CONTAINER,FOLDER', log)
        text = [line for line in log if line.startswith('本機有雲端電腦的跡象')]
        self.assertEqual(len(text), 1, log)
        self.assertIn('efhe-cloud', text[0])
        self.assertIn('C:\\EdgeFHE_evidence', text[0])
        self.assertFalse(os.path.exists(self.marker), 'the relay was started')
        self.assertFalse(any(line.startswith('OWNER_BRIDGE_READY') for line in log))
        self.assertIn('雲端電腦', ob.OPERATOR['READY'])

    def test_signs(self):
        quiet = ob.cloud_pc_signs(console=lambda address: False, containers=lambda: [('web', 'nginx:1.27')],
                                  isdir=lambda path: False)
        self.assertEqual(quiet, [])
        seen = []
        found = ob.cloud_pc_signs(
            console=lambda address: seen.append(address) or True,
            containers=lambda: [('efhe-cloud', 'sha256:' + 'ab' * 32), ('web', 'nginx:1.27'),
                                ('other', 'edgefhe-demo-cloud:4b1e0c2d9a77'), ('sim', 'edgefhe-demo-sim:dev')],
            isdir=lambda path: path == 'C:\\EdgeFHE_demo')
        self.assertEqual(seen, [ob.CLOUD_CONSOLE])
        self.assertEqual([code for code, _ in found], ['CONSOLE', 'CONTAINER', 'CONTAINER', 'FOLDER'])
        self.assertIn('127.0.0.1:8765', found[0][1])
        self.assertIn('edgefhe-demo-cloud:4b1e0c2d9a77', found[2][1])
        for image, repo in (('edgefhe-demo-cloud:dev', 'edgefhe-demo-cloud'),
                            ('registry.local:5000/team/EdgeFHE-Demo-Cloud:abc', 'edgefhe-demo-cloud'),
                            ('edgefhe-demo-cloud@sha256:' + 'cd' * 32, 'edgefhe-demo-cloud'),
                            ('edgefhe-demo-sim:dev', 'edgefhe-demo-sim'), ('', '')):
            self.assertEqual(ob.image_repository(image), repo, image)

    def test_docker_ps(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return types.SimpleNamespace(returncode=0, stdout=b'efhe-cloud\tedgefhe-demo-cloud:dev\r\nweb\tnginx\n\n')
        self.assertEqual(ob.docker_containers(run=run, which=lambda name: '/usr/bin/docker'),
                         [('efhe-cloud', 'edgefhe-demo-cloud:dev'), ('web', 'nginx')])
        self.assertEqual(calls[0][0], ['/usr/bin/docker', 'ps', '--no-trunc', '--format', '{{.Names}}\t{{.Image}}'])
        self.assertEqual(calls[0][1]['stdin'], subprocess.DEVNULL)
        self.assertEqual(ob.docker_containers(run=run, which=lambda name: None), [])        # no docker command

        def engine_down(argv, **kwargs):
            return types.SimpleNamespace(returncode=1, stdout=b'')

        def hangs(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'))
        for broken in (engine_down, hangs):
            self.assertEqual(ob.docker_containers(run=broken, which=lambda name: 'docker'), [])

    def test_probe_finds_the_evidence_console_only(self):
        console_dir = os.path.join(DEMO, 'console')
        if console_dir not in sys.path:
            sys.path.insert(0, console_dir)
        import evidence_console
        # the real console's HTTP layer; a foreign Host is refused before the console object is ever used
        real = evidence_console._Server(('127.0.0.1', 0), None)
        threading.Thread(target=real.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True).start()
        self.addCleanup(real.server_close)
        self.addCleanup(real.shutdown)
        self.assertTrue(ob.probe_console(('127.0.0.1', real.server_address[1])))
        other = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _StubHandler)    # some other local web server
        other.daemon_threads = True
        threading.Thread(target=other.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        self.assertFalse(ob.probe_console(('127.0.0.1', other.server_address[1])))
        self.assertFalse(ob.probe_console(('127.0.0.1', free_port()), timeout=0.5))      # nothing listening


class BoardFiles(unittest.TestCase):
    """board_files.py against the fake console (bash with a fake sudo)."""

    def setUp(self):
        self.bash = git_bash()
        if self.bash is None:
            self.skipTest('bash (Git Bash on Windows) is not available')
        self.base = tempfile.mkdtemp(prefix='edgefhe-files-')
        self.console = FakeConsole(self.bash, os.path.join(self.base, 'board'))
        self.local = os.path.join(self.base, 'pc')
        os.makedirs(self.local)
        self.addCleanup(shutil.rmtree, self.base, True)
        self.addCleanup(self.console.shutdown)

    def board(self, *parts):
        return os.path.join(self.console.home, *parts)

    def run_tool(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = board_files.main(list(argv), kv_factory=lambda port: fake_kv(self.console),
                                    env={'KV_PW': self.console.password})
        return code, stdout.getvalue(), stderr.getvalue()

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)

    def read(self, path):
        with open(path, 'rb') as f:
            return f.read()

    def test_push_file_and_fetch_file_with_a_damaged_line(self):
        blob = os.urandom(5000)
        src = os.path.join(self.local, 'blob.bin')
        self.write(src, blob)
        code, out, err = self.run_tool('push-file', src, '~/data/blob.bin')
        self.assertEqual(code, 0, out + err)
        self.assertIn('BOARD_FILES_PUSHED dst=~/data/blob.bin bytes=5000 sha256=%s' % hashlib.sha256(blob).hexdigest(),
                      out)
        self.assertEqual(self.read(self.board('data', 'blob.bin')), blob)
        board_blob = os.urandom(20000)
        self.write(self.board('data', 'board.bin'), board_blob)
        self.console.damage_once()                            # one lost byte inside the base64 output
        dst = os.path.join(self.local, 'fetched.bin')
        code, out, err = self.run_tool('fetch-file', '~/data/board.bin', dst)
        self.assertEqual(code, 0, out + err)
        self.assertIn('BOARD_FILES_FETCHED', out)
        self.assertEqual(self.read(dst), board_blob)
        self.assertIn('重試了 1 次', err)
        code, out, err = self.run_tool('fetch-file', '~/data/board.bin', dst)
        self.assertEqual((code, out.strip()), (2, 'BOARD_FILES_REFUSED code=OUTPUT_EXISTS'))
        code, out, err = self.run_tool('fetch-file', '~/data/missing.bin', os.path.join(self.local, 'x'))
        self.assertEqual((code, out.strip()), (1, 'BOARD_FILES_FAILED code=REMOTE_MISSING'))
        code, out, err = self.run_tool('push-file', src, '~/data/with space.bin')
        self.assertEqual((code, out.strip()), (2, 'BOARD_FILES_REFUSED code=REMOTE_PATH'))
        self.assertEqual(self.console.state, 'login')         # every session logged out

    def link(self, target, link):
        """A symbolic link on the fake board (on Windows an MSYS system link, which Git Bash's readlink -f follows)."""
        env = dict(os.environ, MSYS='winsymlinks:sys')
        made = subprocess.run([self.bash, '-c', 'ln -s "$1" "$2"', 'link', bash_path(target), bash_path(link)],
                              capture_output=True, env=env, timeout=30)
        self.assertEqual(made.returncode, 0, made.stderr)

    def test_fetch_file_keeps_keys_and_private_records_on_the_board(self):
        pem_key = (b'-----BEGIN ' + b'EC PRIVATE KEY-----\n' + b'MHcCAQEEIB' + b'Q' * 54 + b'\n-----END '
                   + b'EC PRIVATE KEY-----\n')
        pki = self.board('edgefhe-demo', 'pki')
        self.write(os.path.join(pki, 'client.key'), b'board key bytes that only the name rules recognise\n')
        self.write(os.path.join(pki, 'ca.pem'), b'-----BEGIN ' + b'CERTIFICATE-----\nMIIB\n-----END ' +
                   b'CERTIFICATE-----\n')
        self.write(os.path.join(pki, 'notes.txt'), b'pki notes\n')
        self.write(self.board('data', 'notes.txt'), b'harmless line\n' + pem_key)
        self.write(self.board('state', 'private', 'r0123456789ab.json'), b'{"form": "owner answers"}\n')
        self.write(self.board('data', 'ok.txt'), b'nothing secret\n')
        local = [('~/edgefhe-demo/pki/client.key', 'KEY_STAYS_ON_BOARD'),
                 ('/home/ubuntu/edgefhe-demo/pki/client.key', 'KEY_STAYS_ON_BOARD'),
                 ('~/EDGEFHE-DEMO/PKI/Client.KEY', 'KEY_STAYS_ON_BOARD'),
                 ('~/edgefhe-demo/pki/notes.txt', 'KEY_STAYS_ON_BOARD'),
                 ('~/backup/server_private.pem', 'KEY_STAYS_ON_BOARD'),
                 ('~/backup/edgefhe.p12', 'KEY_STAYS_ON_BOARD'),
                 ('~/runs/tls_keys.log', 'KEY_STAYS_ON_BOARD'),
                 ('~/.ssh/id_ed25519', 'KEY_STAYS_ON_BOARD'),
                 ('/run/edgefhe-demo/private/r0123456789ab.json', 'PRIVATE_STAYS_ON_BOARD'),
                 ('/run/edgefhe-demo/r0123456789ab/in.csv', 'PRIVATE_STAYS_ON_BOARD'),
                 ('~/state/private/r0123456789ab.json', 'PRIVATE_STAYS_ON_BOARD')]
        for src, want in local:
            dst = os.path.join(self.local, 'refused.bin')
            code, out, err = self.run_tool('fetch-file', src, dst)
            self.assertEqual((code, out.strip()), (2, 'BOARD_FILES_REFUSED code=%s' % want), src)
            self.assertIn('只存在於 KV260', err, src)
            self.assertFalse(os.path.exists(dst), src)
        self.assertEqual(self.console.logins, 0)              # refused before the console was opened
        for name in ('ca.pem', 'client.csr', 'server.pin', 'client.pem'):
            self.assertIsNone(board_files.stays_on_board('~/edgefhe-demo/pki/' + name), name)
        for path in ('~/data/board.bin', '/var/log/edgefhe-demo/public.jsonl', '~/EdgeFHE_repo/demo/README.md',
                     '/run/edgefhe-demo-other/x.json', '/opt/edgefhe-demo/ui/app.js'):
            self.assertIsNone(board_files.stays_on_board(path), path)

        dst = os.path.join(self.local, 'notes.txt')           # a PEM private key under an innocent name
        code, out, err = self.run_tool('fetch-file', '~/data/notes.txt', dst)
        self.assertEqual((code, out.strip()), (2, 'BOARD_FILES_REFUSED code=KEY_STAYS_ON_BOARD'))
        self.assertIn('PEM', err)
        self.assertFalse(os.path.exists(dst))
        self.link(os.path.join(pki, 'client.key'), self.board('data', 'innocent.txt'))
        self.link(self.board('state', 'private', 'r0123456789ab.json'), self.board('data', 'record.json'))
        for src, want in (('~/data/innocent.txt', 'KEY_STAYS_ON_BOARD'),
                          ('~/data/record.json', 'PRIVATE_STAYS_ON_BOARD')):
            dst = os.path.join(self.local, 'linked.bin')
            code, out, err = self.run_tool('fetch-file', src, dst)
            self.assertEqual((code, out.strip()), (2, 'BOARD_FILES_REFUSED code=%s' % want), src + err)
            self.assertIn('實際路徑', err)                 # caught on the resolved path
            self.assertFalse(os.path.exists(dst))
        for src, data in (('~/edgefhe-demo/pki/ca.pem', self.read(os.path.join(pki, 'ca.pem'))),
                          ('~/data/ok.txt', b'nothing secret\n')):
            dst = os.path.join(self.local, src.rsplit('/', 1)[-1])
            code, out, err = self.run_tool('fetch-file', src, dst)
            self.assertEqual(code, 0, out + err)
            self.assertIn('BOARD_FILES_FETCHED', out)
            self.assertEqual(self.read(dst), data)
        self.assertEqual(self.console.state, 'login')

    def test_outputs_stay_outside_the_repository(self):
        """fetch-file, fetch-public and csr-roundtrip refuse a PC output inside the repository however it is spelled
        (drive letter and folder case, the extended-length and admin-share forms), before the console is opened."""
        target = os.path.join(ROOT, 'demo', 'pc', 'no_such_output_' + secrets.token_hex(4))
        spellings = [target]
        if os.name == 'nt':
            bs = '\\'
            spellings += [target.swapcase(), target[0].swapcase() + target[1:], bs * 2 + '?' + bs + target.swapcase(),
                          board_files.__file__.upper()]
            if os.path.isdir(bs * 2 + 'localhost' + bs + ROOT[0] + '$' + bs):     # the admin share, when it is shared
                spellings.append(bs * 2 + 'localhost' + bs + ROOT[0] + '$' + target[2:])
        self.write(self.board('data', 'ok.txt'), b'nothing secret\n')
        for path in spellings:
            self.assertTrue(board_files.inside_repository(path), path)
            for argv in (('fetch-file', '~/data/ok.txt', path), ('fetch-public', path),
                         ('csr-roundtrip', '--pki', path, '--bash', self.bash)):
                code, out, err = self.run_tool(*argv)
                self.assertEqual((code, out.strip()), (2, 'BOARD_FILES_REFUSED code=OUTPUT_IN_REPOSITORY'), argv)
                self.assertIn('repository', err, argv)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self.console.logins, 0)              # refused before the console was opened
        for path in (self.local, os.path.join(self.local, 'new', 'x.bin'), ROOT + '_other', os.path.dirname(ROOT)):
            self.assertFalse(board_files.inside_repository(path), path)
        if os.name == 'nt':
            self.assertFalse(board_files.inside_repository(self.local.swapcase()))
        dst = os.path.join(self.local, 'ok.txt')                # outside the repository the fetch still works
        code, out, err = self.run_tool('fetch-file', '~/data/ok.txt', dst)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.read(dst), b'nothing secret\n')

    def test_push_tree_leaves_excluded_files_behind(self):
        tree = os.path.join(self.local, 'demo')
        files = {'a.txt': b'alpha\n', 'bin.dat': os.urandom(3000), 'sub/run.sh': b'#!/bin/sh\necho hi\n',
                 'sub/deep/x.json': json.dumps({'k': '中文'}).encode('utf-8')}
        excluded = {'__pycache__/m.cpython-313.pyc': b'x', 'local/secret.txt': b'x', 'tls_keys.log': b'x',
                    'capture.pcapng': b'x', 'server.key': b'x', 'notes.pyc': b'x', 'runs/r1/out.json': b'x'}
        for rel, data in list(files.items()) + list(excluded.items()):
            self.write(os.path.join(tree, *rel.split('/')), data)
        code, out, err = self.run_tool('push-tree', tree, '~/EdgeFHE_repo/demo')
        self.assertEqual(code, 0, out + err)
        self.assertRegex(out, r'BOARD_FILES_TREE_PUSHED dst=~/EdgeFHE_repo/demo files=4 bytes=\d+ skipped=7 '
                              r'tree_sha256=[0-9a-f]{64}')
        for rel, data in files.items():
            self.assertEqual(self.read(self.board('EdgeFHE_repo', 'demo', *rel.split('/'))), data, rel)
        for rel in excluded:
            self.assertFalse(os.path.exists(self.board('EdgeFHE_repo', 'demo', *rel.split('/'))), rel)
        self.assertEqual(os.listdir(self.console.tmp), [])    # the archive and its list were removed on the board

    def test_fetch_public_only_after_the_seal(self):
        log = b''.join(json.dumps({'v': 1, 'kind': 'form', 'run': 'r%012x' % i, 'request_sha256': '%064x' % i},
                                  sort_keys=True).encode('ascii') + b'\n' for i in range(3))
        self.write(self.board('var', 'log', 'edgefhe-demo', 'public.jsonl'), log)
        docs = {'%064x' % 7 + '.json': {'fictional': True, 'request_sha256': '%064x' % 7, 'notice': '虛構表單'},
                'r0123456789ab.json': {'fictional': True, 'run': 'r0123456789ab'}}
        for name, doc in docs.items():
            self.write(self.board('var', 'lib', 'edgefhe-demo', 'disclosure', name),
                       (json.dumps(doc, ensure_ascii=False, indent=1) + '\n').encode('utf-8'))
        self.write(self.board('var', 'lib', 'edgefhe-demo', 'disclosure', 'notes.json'), b'{}\n')
        run = os.path.join(self.local, 'run1')
        os.makedirs(run)
        args = ['fetch-public', run, '--public-log', '~/var/log/edgefhe-demo/public.jsonl',
                '--disclosure-dir', '~/var/lib/edgefhe-demo/disclosure']
        code, out, err = self.run_tool(*args)
        self.assertEqual((code, out.strip()), (2, 'BOARD_FILES_REFUSED code=NOT_SEALED'))
        self.assertEqual(self.console.logins, 0)              # refused before the console was opened
        capture = os.urandom(4096)
        self.write(os.path.join(run, 'capture.pcapng'), capture)
        self.write(os.path.join(run, 'capture.sha256'), ('%s  capture.pcapng\n' % ('0' * 64)).encode('ascii'))
        code, out, err = self.run_tool(*args)
        self.assertEqual((code, out.strip()), (2, 'BOARD_FILES_REFUSED code=SEAL_MISMATCH'))
        self.write(os.path.join(run, 'capture.sha256'),
                   ('%s  capture.pcapng\n' % hashlib.sha256(capture).hexdigest()).encode('ascii'))
        code, out, err = self.run_tool(*args)
        self.assertEqual(code, 0, out + err)
        self.assertIn('BOARD_FILES_PUBLIC_FETCHED run=%s public_rows=3 public_sha256=%s disclosures=2' % (
            run, hashlib.sha256(log).hexdigest()), out)
        self.assertEqual(self.read(os.path.join(run, 'board_public.jsonl')), log)
        self.assertEqual(sorted(os.listdir(os.path.join(run, 'disclosure'))), sorted(docs))
        for name in docs:
            self.assertEqual(self.read(os.path.join(run, 'disclosure', name)),
                             self.read(self.board('var', 'lib', 'edgefhe-demo', 'disclosure', name)))
        self.assertIn('notes.json', err)                      # a name outside the gateway's rule is skipped
        code, out, err = self.run_tool(*args)
        self.assertEqual((code, out.strip()), (2, 'BOARD_FILES_REFUSED code=OUTPUT_EXISTS'))

    def test_csr_roundtrip_keeps_the_board_key_on_the_board(self):
        if shutil.which('openssl') is None and not os.path.isfile(os.path.join(
                os.path.dirname(self.bash), '..', 'usr', 'bin', 'openssl.exe')):
            self.skipTest('openssl is not available')
        pki = os.path.join(self.base, 'pki')
        env = dict(os.environ, MSYS_NO_PATHCONV='1')
        made = subprocess.run([self.bash, PKI_SCRIPT, 'ca-and-server', pki], capture_output=True, env=env,
                              timeout=180)
        self.assertEqual(made.returncode, 0, made.stderr.decode('utf-8', 'replace'))
        self.assertIn(b'EDGEFHE_DEMO_PKI_CA_SERVER_READY', made.stdout)
        board_pki = self.board('edgefhe-demo', 'pki')
        os.makedirs(board_pki)
        key = subprocess.run([self.bash, '-c', 'cd "$1" && umask 077 && openssl req -new -newkey ec -pkeyopt '
                              'ec_paramgen_curve:prime256v1 -noenc -keyout client.key -out client.csr '
                              '-subj /CN=edgefhe-kv260 2>&1', 'make-csr', bash_path(board_pki)],
                             capture_output=True, env=env, timeout=60)
        self.assertEqual(key.returncode, 0, key.stdout)
        key_bytes = self.read(os.path.join(board_pki, 'client.key'))
        code, out, err = self.run_tool('csr-roundtrip', '--pki', os.path.join(self.base, 'missing'))
        self.assertEqual((code, out.strip()), (2, 'BOARD_FILES_REFUSED code=PKI_MISSING'))
        code, out, err = self.run_tool('csr-roundtrip', '--pki', pki, '--board-pki', '~/edgefhe-demo/pki',
                                       '--bash', self.bash)
        self.assertEqual(code, 0, out + err)
        pin = re.search(r'^EDGEFHE_DEMO_CLIENT_SIGNED client_pin=([0-9a-f]{64})$', out, re.M)
        self.assertTrue(pin, out)
        self.assertIn('BOARD_FILES_CSR_ROUNDTRIP_PASS client_pin=%s key_match=1' % pin.group(1), out)
        for name in ('ca.pem', 'client.pem', 'server.pin'):
            self.assertEqual(self.read(os.path.join(board_pki, name)), self.read(os.path.join(pki, 'board', name)))
        self.assertEqual(self.read(os.path.join(pki, 'cloud', 'client.pin')).decode('ascii').strip(), pin.group(1))
        self.assertEqual(self.read(os.path.join(board_pki, 'client.key')), key_bytes)
        for root, _, names in os.walk(pki):                   # the board key exists only on the board
            for name in names:
                self.assertNotEqual(self.read(os.path.join(root, name)), key_bytes, name)
        code, out, err = self.run_tool('csr-roundtrip', '--pki', pki, '--bash', self.bash)
        self.assertEqual((code, out.strip()), (2, 'BOARD_FILES_REFUSED code=PKI_SIGNED'))


# ------------------------------------------------------------------------------------------------------- main
BRIDGE_GROUP = (Codec, RelayQueue, BridgeRoundTrip, LinkFaults, SerialConsole, CloudPcRule)
FILES_GROUP = (BoardFiles,)


def run_group(classes, verbosity):
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite([loader.loadTestsFromTestCase(c) for c in classes])
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    if not result.wasSuccessful():
        return 'FAIL'
    if result.skipped or result.testsRun == 0:
        return 'INCOMPLETE'
    return 'PASS'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    ap.add_argument('--only', choices=('bridge', 'files'))
    ap.add_argument('-q', '--quiet', action='store_true')
    args = ap.parse_args(argv)
    verbosity = 1 if args.quiet else 2
    verdicts = []
    if args.only in (None, 'bridge'):
        verdicts.append(run_group(BRIDGE_GROUP, verbosity))
        print('OWNER_BRIDGE_TESTS_%s' % verdicts[-1], flush=True)
    if args.only in (None, 'files'):
        verdicts.append(run_group(FILES_GROUP, verbosity))
        print('BOARD_FILES_TESTS_%s' % verdicts[-1], flush=True)
    return 0 if all(v == 'PASS' for v in verdicts) else 1


if __name__ == '__main__':
    sys.exit(main())
