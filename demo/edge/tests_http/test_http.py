"""Tests of the loopback HTTP API, the event stream and the progress sampler (Python 3.10 standard library only).

usage (PowerShell, repository root):
  docker run --rm -v "${PWD}:/repo" -w /repo python:3.10-slim python -B -m unittest discover -s demo/edge/tests_http -v

The gateway runs as 'edgefhe_gateway.py serve' in a child process, so that it is not PID 1 in the container (the PL
permit requires a parent PID above 1), on the temporary board of demo/edge/tests/test_core.py (Bench: the fake client,
a tmpfs work directory, a fake /proc/interrupts with two DMA lines and a fake device node). Covered:
  * preset M through POST /api/v1/submit, live over Server-Sent Events: form.accepted, client.spawned, progress.irq
    (LOAD, then ENC, on both DMA lines), progress.io (wchar reaches the 393,472-byte request frame), client.exited,
    irq.delta, result L3 and wiped, consecutive seqs, the same events from /api/v1/runs/<run>, resume with since
    and Last-Event-ID, public events only on stdout;
  * Host 'evil:PORT', a missing or wrong owner header, a cross Origin, a wrong content type and a body above 4 KiB
    (403), malformed bodies (400), wrong methods (405), the security headers on every response;
  * a second submit while busy (409); a stop request during a run (503 for new forms, the run and its stream finish,
    no signal reaches the client, exit status 0);
  * static files, query strings and path traversal (404), the stream limit, --progress-ms 0, --events none, the
    2 s heartbeat;
  * the bind rules: 127.0.0.1 only; 0.0.0.0 only with --rehearsal-listen 0.0.0.0 and EDGEFHE_REHEARSAL=1 and without an
    FPGA device node; any other address exits with status 4; a port in use exits with status 1; a rehearsal gateway
    refuses the pl backend once the device node appears (503 REHEARSAL_PL_ON_BOARD);
  * retain_private: false by default (status and the wiped event say so, no private record is left), true when the
    configuration sets it; serve --quiet prints no EVT line;
  * the sampler on its own: LOAD/ENC markers and byte counters of a child process, read through /proc/<pid>;
  * a run started through the HTTP path runs on a non-daemon thread, so Python waits for its client before exiting.
After the module's tests, the line HTTP_API_TESTS_PASS is printed when every test passed and none was skipped.
"""
import email.message
import http.client
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EDGE = os.path.dirname(HERE)
GATEWAY = os.path.join(EDGE, 'edgefhe_gateway.py')
CORE_TESTS = os.path.join(EDGE, 'tests', 'test_core.py')
FAKE = os.path.join(EDGE, 'tests', 'fake_efnet_client.py')

sys.dont_write_bytecode = True
if EDGE not in sys.path:
    sys.path.insert(0, EDGE)
from efgw import httpapi, progress, records, runner  # noqa: E402


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = load_module('edgefhe_gateway_core_tests', CORE_TESTS)     # Bench, presets, the fake /proc/interrupts
M, J = core.M, core.J
LINUX = sys.platform.startswith('linux')
LINUX_ROOT = core.LINUX_ROOT
REQUEST_FRAME, RESPONSE_FRAME = 393472, 98560
IO = {'up': 394022, 'down': 98714, 'chunk': 16401, 'pause_s': 0.002, 'delay_s': 0.05}
OWNER = (('Content-Type', 'application/json'), ('X-EdgeFHE-Owner', '1'))
RUN = re.compile(r'r[0-9a-f]{12}')
ORDER = [('form.accepted', 'public'), ('form.accepted', 'owner'), ('client.spawned', 'public'),
         ('client.exited', 'public'), ('irq.delta', 'public'), ('result', 'owner'), ('wiped', 'public')]
OWNER_KEYS = ('"x":', '"z":', '"values":', '"contributions":', '"logit_ckks":', '"logit_plain":', '"p_balanced":',
              '"level":', '"salt_hex":', '"canonical_json":')
UI_FILES = {
    'index.html': '<!doctype html>\n<meta charset="utf-8">\n<title>EdgeFHE</title>\n'
                  '<script type="module" src="app.js"></script>\n',
    'app.js': "import { level } from './lib.mjs';\ndocument.title = level(3);\n",
    'lib.mjs': "export function level(n) { return 'L' + n; }\n",
    'style.css': 'body { font-size: 18px; }\n',
    'mark.svg': '<svg viewBox="0 0 1 1"></svg>\n',
    'fixtures/run.json': '{"events": []}\n',
    'notes.py': 'print("not served")\n',
    'README.md': 'not served\n',
    '.hidden.js': 'not served\n',
}
OUTSIDE = 'outside the owner screen directory\n'
HELPER = '\n'.join([
    'import importlib.util, sys, time',
    'spec = importlib.util.spec_from_file_location("fake_client", sys.argv[1])',
    'fake = importlib.util.module_from_spec(spec)',
    'spec.loader.exec_module(fake)',
    'behavior = {"interrupts": sys.argv[2]}',
    'sys.stdin.readline()',
    'fake.bump_irq(behavior, 1)',
    'time.sleep(0.15)',
    'fake.bump_irq(behavior, 1)',
    'fake.emulate_io({"up": 400000, "down": 100000, "chunk": 16401, "pause_s": 0.002, "delay_s": 0})',
    'time.sleep(0.3)',
])


# ---------------------------------------------------------------------------------------------------- helpers
def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def make_ui(base):
    """An owner-screen directory with served and unserved files, and two symbolic links out of it."""
    ui = os.path.join(base, 'ui')
    for name, text in UI_FILES.items():
        path = os.path.join(ui, *name.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
    outside = os.path.join(base, 'outside.js')
    with open(outside, 'w', encoding='utf-8', newline='\n') as f:
        f.write(OUTSIDE)
    if hasattr(os, 'symlink'):
        os.symlink(outside, os.path.join(ui, 'link.js'))
        os.symlink(base, os.path.join(ui, 'up'))
    return ui


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


def call(port, method, path, body=None, headers=(), host=None, timeout=20):
    """One request with full control of the headers: host None sends Host 127.0.0.1:port, else each listed value."""
    hosts = ['127.0.0.1:%d' % port] if host is None else ([host] if isinstance(host, str) else list(host))
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for value in hosts:
            conn.putheader('Host', value)
        names = set()
        for name, value in headers:
            conn.putheader(name, value)
            names.add(name.lower())
        if body is not None and 'content-length' not in names:
            conn.putheader('Content-Length', str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        return Reply(resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read())
    finally:
        conn.close()


def submit(port, form, backend=None, headers=OWNER, **kw):
    payload = {'form': form}
    if backend is not None:
        payload['backend'] = backend
    return call(port, 'POST', '/api/v1/submit', json.dumps(payload).encode('utf-8'), headers, **kw)


class Stream:
    """GET /api/v1/events, read one Server-Sent Events block at a time."""

    def __init__(self, port, query='?since=0', headers=(), timeout=20):
        self.conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
        self.conn.putrequest('GET', '/api/v1/events' + query, skip_host=True, skip_accept_encoding=True)
        self.conn.putheader('Host', '127.0.0.1:%d' % port)
        for name, value in headers:
            self.conn.putheader(name, value)
        self.conn.endheaders()
        self.resp = self.conn.getresponse()
        self.status = self.resp.status
        self.headers = {k.lower(): v for k, v in self.resp.getheaders()}

    def block(self):
        """{'comments': [...], field: value} of the next block; EOFError once the server closed the stream."""
        block = {'comments': []}
        while True:
            raw = self.resp.readline()
            if not raw:
                raise EOFError('the event stream was closed')
            line = raw.decode('utf-8')
            line = line[:-1] if line.endswith('\n') else line
            line = line[:-1] if line.endswith('\r') else line
            if line == '':
                if len(block) > 1 or block['comments']:
                    block['at'] = time.monotonic()
                    return block
                continue
            if line.startswith(':'):
                block['comments'].append(line[1:].strip())
                continue
            name, _, value = line.partition(':')
            block[name] = value[1:] if value.startswith(' ') else value

    def envelope(self):
        """The next event (heartbeats are skipped)."""
        while True:
            block = self.block()
            if 'data' in block:
                envelope = json.loads(block['data'])
                if int(block['id']) != envelope['seq'] or 'event' in block:
                    raise AssertionError('SSE block %r' % block)
                return envelope

    def until(self, run, last='wiped', limit_s=60):
        """Every event read up to and including the event `last` of run."""
        got, deadline = [], time.monotonic() + limit_s
        while time.monotonic() < deadline:
            got.append(self.envelope())
            if got[-1]['run'] == run and got[-1]['type'] == last:
                return got
        raise AssertionError('no %s event for %s' % (last, run))

    def close(self):
        self.resp.close()           # an HTTP/1.0 response owns the socket; closing the connection alone keeps it open
        self.conn.close()


class Served:
    """edgefhe_gateway.py serve in a child process; stdout and stderr are collected line by line."""

    def __init__(self, bench, *extra, env=None, listen=None, wait=True):
        self.port = free_port()
        bench.write_config(listen=listen or '127.0.0.1:%d' % self.port)
        environment = dict(os.environ)
        environment.pop(httpapi.REHEARSAL_ENV, None)
        environment.update(env or {})
        command = [sys.executable, '-I', '-B', GATEWAY, 'serve', '--config', bench.cfg_path] + list(extra)
        self.proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        self.out, self.err = [], []
        self._cond = threading.Condition()
        self._open = 2
        self._readers = [threading.Thread(target=self._read, args=(self.proc.stdout, self.out), daemon=True),
                         threading.Thread(target=self._read, args=(self.proc.stderr, self.err), daemon=True)]
        for reader in self._readers:
            reader.start()
        self.serving = self.wait_line('GATEWAY_SERVING ', 30) if wait else None

    def _read(self, stream, sink):
        for raw in iter(stream.readline, b''):
            with self._cond:
                sink.append(raw.decode('utf-8', 'replace').rstrip('\n'))
                self._cond.notify_all()
        with self._cond:
            self._open -= 1
            self._cond.notify_all()

    def wait_line(self, prefix, timeout):
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for line in self.out:
                    if line.startswith(prefix):
                        return line
                left = deadline - time.monotonic()
                if left <= 0 or self._open < 2:
                    return None
                self._cond.wait(left)

    def text(self):
        with self._cond:
            return '\n'.join(self.out + ['--- stderr'] + self.err)

    def printed(self):
        with self._cond:
            return [json.loads(line[4:]) for line in self.out if line.startswith('EVT ')]

    def stop(self, timeout=60):
        """SIGTERM to the gateway (never to its client); the exit status once it has returned."""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        code = self.proc.wait(timeout)
        for reader in self._readers:
            reader.join(10)
        return code

    def close(self):
        try:
            if self.proc.poll() is None:
                self.stop(90)
        except subprocess.TimeoutExpired:
            self.proc.kill()        # a hung test gateway only; its fake client runs in a session of its own
            self.proc.wait()
        for stream in (self.proc.stdout, self.proc.stderr):
            stream.close()


class Secure:
    def assertSecure(self, headers):
        self.assertEqual(headers.get('content-security-policy'), httpapi.CSP)
        self.assertEqual(httpapi.CSP, "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                                      "style-src 'self'; script-src 'self'")
        self.assertEqual(headers.get('x-content-type-options'), 'nosniff')
        self.assertEqual(headers.get('cache-control'), 'no-store')
        self.assertNotIn('access-control-allow-origin', headers)


# ------------------------------------------------------------------------------------------------ pure checks
class Pure(unittest.TestCase):
    def test_feed_follows_the_event_log(self):
        log = records.EventLog(capacity=1000)
        for i in range(3):
            log.emit('r000000000001', 'progress.io', 'public', {'i': i})
        feed = httpapi.Feed(log)                            # seeded with the three earlier events
        self.assertEqual([e['seq'] for e in feed.after(0, 0)], [1, 2, 3])
        self.assertEqual(feed.after(3, 0.05), [])
        got = []
        waiter = threading.Thread(target=lambda: got.extend(feed.after(3, 10.0)))
        waiter.start()
        time.sleep(0.05)
        log.emit('r000000000001', 'wiped', 'public', {})
        waiter.join(10)
        self.assertEqual([e['seq'] for e in got], [4])
        self.assertIs(got[0], log.since(3)[0])
        for i in range(httpapi.SSE_BATCH + 10):
            log.emit('r000000000002', 'progress.io', 'public', {'i': i})
        self.assertEqual([e['seq'] for e in feed.after(0, 0)], list(range(1, httpapi.SSE_BATCH + 1)))
        last = feed.last_seq()
        self.assertEqual(last, log.last_seq())
        self.assertEqual([e['seq'] for e in feed.after(last - 2, 0)], [last - 1, last])
        small = httpapi.Feed(log, capacity=5)
        self.assertEqual([e['seq'] for e in small.after(0, 0)], list(range(last - 4, last + 1)))
        waiter = threading.Thread(target=lambda: got.append(feed.after(last, 30.0)))
        started = time.monotonic()
        waiter.start()
        feed.close()
        waiter.join(10)
        self.assertFalse(waiter.is_alive())
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(feed.closed)

    def test_interrupt_increments(self):
        def snap(a, b):
            return {'61': {'count': a, 'desc': 'GICv2 121 Level xilinx-dma-controller'},
                    '62': {'count': b, 'desc': 'GICv2 136 Level xilinx-dma-controller'}}
        base, reported = {'61': 6, '62': 6}, {}
        self.assertEqual(progress.increments(base, reported, snap(6, 6)), [])
        self.assertEqual(progress.increments(base, reported, snap(7, 6)), [(1, '61', 7, 1)])
        self.assertEqual(progress.increments(base, reported, snap(8, 8)),
                         [(1, '62', 8, 2), (2, '61', 8, 2), (2, '62', 8, 2)])
        self.assertEqual(progress.increments(base, reported, snap(8, 8)), [])
        self.assertEqual(progress.increments(base, reported, {}), [])
        self.assertEqual(progress.increments(base, {}, snap(106, 6)), [(100, '61', 106, 100)])
        self.assertEqual(progress.increments({'9': 0, '10': 0}, {}, {'9': {'count': 1}, '10': {'count': 1}}),
                         [(1, '9', 1, 1), (1, '10', 1, 1)])
        self.assertEqual([progress.label(i) for i in (1, 2, 3)], ['LOAD', 'ENC', 'ENC'])

    def test_proc_io_text(self):
        text = ('rchar: 1200\nwchar: 394500\nsyscr: 10\nsyscw: 30\nread_bytes: 0\nwrite_bytes: 0\n'
                'cancelled_write_bytes: 0\n')
        self.assertEqual(progress.parse_io(text), (394500, 1200))
        with self.assertRaises(KeyError):
            progress.parse_io('syscr: 1\n')

    def test_request_helpers(self):
        self.assertEqual(httpapi._split_target('/ui/app.js?shot=all'), ('/ui/app.js', 'shot=all'))
        for target in ('//evil/x', 'http://evil/x', '*', ''):
            self.assertEqual(httpapi._split_target(target), (None, None), target)
        for value, ok in (('application/json', True), ('application/json; charset=utf-8', True),
                          ('Application/JSON;charset="UTF-8"', True), ('application/json; charset=latin-1', False),
                          ('text/plain', False), ('application/json; boundary=x', False),
                          ('application/jsonx', False), ('', False)):
            self.assertEqual(httpapi._json_media(value), ok, value)
        headers = email.message.Message()
        self.assertEqual(httpapi._resume_point('', headers), 0)
        self.assertEqual(httpapi._resume_point('since=5', headers), 5)
        headers['Last-Event-ID'] = '9'
        self.assertEqual(httpapi._resume_point('since=5', headers), 9)
        self.assertEqual(httpapi._resume_point('', headers), 9)
        for query in ('since=-1', 'since=x', 'since=1&since=2', 'since=1e3'):
            self.assertIsNone(httpapi._resume_point(query, email.message.Message()), query)
        two = email.message.Message()
        two['Last-Event-ID'] = '1'
        two['Last-Event-ID'] = '2'
        self.assertIsNone(httpapi._resume_point('', two))
        for data in (b'{"form": {}, "form": {}}', b'{"form": {"BMI": NaN}}', b'{"form": Infinity}', b'\xff',
                     b'[' * 5000, b'', b'{"form":'):
            self.assertIs(httpapi._parse_body(data), httpapi._INVALID, data[:20])
        self.assertEqual(httpapi._parse_body(b'{"form": {"BMI": 31.2}}'), {'form': {'BMI': 31.2}})
        self.assertEqual(httpapi._options([]), 'public')
        self.assertEqual(httpapi._options(['--events', 'none']), 'none')
        self.assertEqual(httpapi._options(['--events=none']), 'none')
        for bad in (['--events', 'owner'], ['--events'], ['--bogus'], ['none']):
            with self.assertRaises(ValueError):
                httpapi._options(bad)

    def test_screen_policy(self):
        class Handler:
            def __init__(self, address, pairs):
                self.client_address = (address, 40000)
                self.headers = email.message.Message()
                for name, value in pairs:
                    self.headers[name] = value

        loopback = httpapi._Context(None, 8080, '127.0.0.1', 25, None)
        rehearsal = httpapi._Context(None, 8080, '0.0.0.0', 25, None)
        good = [('Host', '127.0.0.1:8080')]
        cases = [
            (loopback, '127.0.0.1', good, None), (loopback, '127.0.0.5', [('Host', 'localhost:8080')], None),
            (loopback, '127.0.0.1', [('Host', 'LOCALHOST:8080')], None),
            (loopback, '192.0.2.7', good, 'CLIENT_FORBIDDEN'), (rehearsal, '192.0.2.7', good, None),
            (loopback, '127.0.0.1', [('Host', 'evil:8080')], 'HOST_FORBIDDEN'),
            (loopback, '127.0.0.1', [('Host', '127.0.0.1:8081')], 'HOST_FORBIDDEN'),
            (loopback, '127.0.0.1', [('Host', '127.0.0.1')], 'HOST_FORBIDDEN'),
            (loopback, '127.0.0.1', [], 'HOST_FORBIDDEN'),
            (loopback, '127.0.0.1', good + [('Host', 'evil:8080')], 'HOST_FORBIDDEN'),
            (rehearsal, '192.0.2.7', [('Host', '192.0.2.1:8080')], 'HOST_FORBIDDEN'),
            (loopback, '127.0.0.1', good + [('Origin', 'http://127.0.0.1:8080')], None),
            (loopback, '127.0.0.1', good + [('Origin', 'http://localhost:8080')], None),
            (loopback, '127.0.0.1', good + [('Origin', 'http://evil.example')], 'ORIGIN_FORBIDDEN'),
            (loopback, '127.0.0.1', good + [('Origin', 'null')], 'ORIGIN_FORBIDDEN'),
            (loopback, '127.0.0.1', good + [('Origin', 'https://127.0.0.1:8080')], 'ORIGIN_FORBIDDEN'),
            (loopback, '127.0.0.1', good + [('Origin', 'http://127.0.0.1:8081')], 'ORIGIN_FORBIDDEN'),
            (loopback, '127.0.0.1', good + [('Origin', 'http://127.0.0.1:8080'), ('Origin', 'http://x')],
             'ORIGIN_FORBIDDEN'),
        ]
        for ctx, address, pairs, want in cases:
            self.assertEqual(ctx.screen(Handler(address, pairs)), want, (ctx.bind, address, pairs))

    @unittest.skipUnless(hasattr(os, 'symlink') and os.name == 'posix', 'symbolic links')
    def test_static_resolution(self):
        base = tempfile.mkdtemp()
        try:
            ctx = httpapi._Context(None, 8080, '127.0.0.1', 25, make_ui(base))
            for path in ('/', '/index.html', '/ui', '/ui/', '/ui/index.html'):
                self.assertEqual(ctx.static_file(path)[1], 'text/html; charset=utf-8', path)
            self.assertEqual(ctx.static_file('/app.js'), ctx.static_file('/ui/app.js'))
            self.assertEqual(ctx.static_file('/ui/fixtures/run.json'),
                             (UI_FILES['fixtures/run.json'].encode('utf-8'), 'application/json; charset=utf-8'))
            self.assertEqual(ctx.static_file('/ui/mark.svg')[1], 'image/svg+xml')
            self.assertEqual(ctx.static_file('/ui/up/ui/app.js'), ctx.static_file('/ui/app.js'))   # resolves inside
            for path in ('/ui/../outside.js', '/ui/%2e%2e/outside.js', '/ui/..%2foutside.js', '/ui/link.js',
                         '/ui/up/outside.js', '/ui/.hidden.js', '/ui/notes.py', '/ui/README.md',
                         '/fixtures/run.json', '/ui//app.js', '/ui/app.js/', '/ui/%c0%ae%c0%ae/app.js',
                         '/ui/a/b/c/d/e.js', '/ui/app.js%00', '/ui/APP.JS', '/ui/missing.js', '/ui/fixtures'):
                self.assertIsNone(ctx.static_file(path), path)
            self.assertIsNone(httpapi._Context(None, 8080, '127.0.0.1', 25, None).static_file('/'))
        finally:
            shutil.rmtree(base)


# --------------------------------------------------------------------------------------------- the sampler
@unittest.skipUnless(LINUX, 'reads /proc/<pid>/io (run in the python:3.10-slim container)')
class Sampling(unittest.TestCase):
    def test_markers_and_byte_counters_of_a_child(self):
        tmp = tempfile.mkdtemp(dir=core.SHM)
        try:
            path = os.path.join(tmp, 'interrupts')
            with open(path, 'w', encoding='ascii') as f:
                f.write(core.INTERRUPTS % (5, 5))
            helper = subprocess.Popen([sys.executable, '-I', '-B', '-c', HELPER, FAKE, path], stdin=subprocess.PIPE)
            events, lock = [], threading.Lock()

            def emit(etype, data):
                with lock:
                    events.append((etype, data))

            sampler = progress.Sampler(helper.pid, emit, path, 25)
            helper.stdin.write(b'go\n')         # the counters move only after the sampler took its baseline
            helper.stdin.close()
            self.assertEqual(helper.wait(60), 0)
            sampler.stop()
            sampler.stop()
            irq = [d for t, d in events if t == 'progress.irq']
            self.assertEqual([(d['irq'], d['index'], d['label'], d['delta'], d['count']) for d in irq],
                             [('61', 1, 'LOAD', 1, 7), ('62', 1, 'LOAD', 1, 7), ('61', 2, 'ENC', 2, 8),
                              ('62', 2, 'ENC', 2, 8)])
            io = [d for t, d in events if t == 'progress.io']
            self.assertTrue(io)
            self.assertEqual([d['wchar'] for d in io], sorted(d['wchar'] for d in io))
            self.assertGreaterEqual(io[-1]['wchar'], 400000)
            self.assertGreaterEqual(io[-1]['rchar'], 100000)
            times = [d['t_ns'] for _, d in events]
            self.assertEqual(times, sorted(times))
            self.assertGreaterEqual(times[0], 0)
            self.assertEqual([t for t, _ in events if t not in ('progress.irq', 'progress.io')], [])
            self.assertEqual([sorted(d) for d in irq[:1]], [['count', 'delta', 'index', 'irq', 'label', 't_ns']])
            self.assertEqual(sorted(io[0]), ['rchar', 't_ns', 'wchar'])
        finally:
            shutil.rmtree(tmp)

    def test_only_a_live_child_is_read(self):
        self.assertIsNone(progress.open_process(os.getpid()))
        self.assertIsNone(progress.open_process(-1))
        done = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'pass'])
        done.wait()                                          # reaped: /proc/<pid> is gone
        events = []
        sampler = progress.Sampler(done.pid, lambda t, d: events.append(t), '/nonexistent/interrupts', 5)
        time.sleep(0.05)
        sampler.stop()
        self.assertEqual(events, [])
        with self.assertRaises(ValueError):
            progress.Sampler(done.pid, lambda t, d: None, '/nonexistent/interrupts', 0)


# ------------------------------------------------------------------------------------------- HTTP over TCP
@unittest.skipUnless(LINUX_ROOT, 'needs root on Linux (run in the python:3.10-slim container)')
class Http(Secure, unittest.TestCase):
    def setUp(self):
        self.bench = core.Bench()
        self.ui = make_ui(self.bench.base)
        self.bench.write_config(ui_dir=self.ui, progress_ms=25)
        self.served = []

    def tearDown(self):
        for served in self.served:
            served.close()
        self.bench.close()

    def serve(self, *extra, **kw):
        served = Served(self.bench, *extra, **kw)
        self.served.append(served)
        self.assertIsNotNone(served.serving, served.text())
        return served

    def assertZeroed(self, names):
        observed = self.bench.observed()
        self.assertEqual(sorted(name.split('.', 1)[1] for name in observed), sorted(names))
        for name, data in observed.items():
            self.assertTrue(data and data == bytes(len(data)), name)

    def test_preset_m_live_events_progress_and_resume(self):
        b = self.bench
        b.behave(io=dict(IO), sleep_s=0.3)
        s = self.serve()
        self.assertEqual(s.serving, 'GATEWAY_SERVING 127.0.0.1:%d' % s.port)
        stream = Stream(s.port)
        self.assertEqual(stream.status, 200)
        self.assertEqual(stream.headers['content-type'], 'text/event-stream; charset=utf-8')
        self.assertSecure(stream.headers)
        first = stream.block()
        self.assertEqual((first.get('retry'), first['comments']), ('2000', ['heartbeat']))
        r = submit(s.port, M, 'pl')
        self.assertEqual(r.status, 202, r.body)
        self.assertSecure(r.headers)
        run = r.json()['run']
        self.assertRegex(run, RUN)
        got = stream.until(run)
        stream.close()
        seqs = [e['seq'] for e in got]
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))))
        mine = [e for e in got if e['run'] == run]
        self.assertEqual(len(mine), len(got))
        for e in mine:
            self.assertEqual(sorted(e), ['class', 'data', 'run', 'seq', 't_mono_ns', 'type', 'v'])
        kinds = [(e['type'], e['class']) for e in mine]
        self.assertEqual([k for k in kinds if not k[0].startswith('progress.')], ORDER)
        spawned = kinds.index(('client.spawned', 'public'))
        exited = kinds.index(('client.exited', 'public'))
        live = [e for e in mine if e['type'].startswith('progress.')]
        self.assertTrue(all(spawned < mine.index(e) < exited and e['class'] == 'public' for e in live))
        irq = [e['data'] for e in live if e['type'] == 'progress.irq']
        self.assertEqual([(d['irq'], d['index'], d['label']) for d in irq],
                         [('61', 1, 'LOAD'), ('62', 1, 'LOAD'), ('61', 2, 'ENC'), ('62', 2, 'ENC')])
        io = [e['data'] for e in live if e['type'] == 'progress.io']
        self.assertTrue(io)
        self.assertEqual([d['wchar'] for d in io], sorted(d['wchar'] for d in io))
        self.assertGreaterEqual(io[-1]['wchar'], REQUEST_FRAME)
        self.assertGreaterEqual(io[-1]['rchar'], RESPONSE_FRAME)
        times = [e['data']['t_ns'] for e in live]
        self.assertEqual(times, sorted(times))
        self.assertGreaterEqual(times[0], 0)
        delta = mine[kinds.index(('irq.delta', 'public'))]['data']
        self.assertEqual((delta['delta'], delta['passed']), ({'61': 2, '62': 2}, True))
        self.assertEqual({d['irq']: d['delta'] for d in irq}, delta['delta'])
        wiped = mine[kinds.index(('wiped', 'public'))]['data']
        self.assertEqual((wiped['retained_private'], wiped['run_dir_removed']), (False, True))
        result = mine[kinds.index(('result', 'owner'))]['data']
        self.assertEqual((result['level'], result['level_code'], result['boundary'], result['passed']),
                         (3, 'L3', None, True))
        self.assertEqual((result['evidence']['request_frame_bytes'], result['evidence']['response_frame_bytes']),
                         (REQUEST_FRAME, RESPONSE_FRAME))
        self.assertEqual(result['evidence']['device_calls'], 2)

        runs = call(s.port, 'GET', '/api/v1/runs/' + run)
        self.assertEqual(runs.status, 200)
        self.assertEqual(runs.json(), {'run': run, 'events': mine})
        at = mine[exited]['seq']
        for query, headers in (('?since=%d' % at, ()), ('', (('Last-Event-ID', str(at)),)),
                               ('?since=1', (('Last-Event-ID', str(at)),))):
            again = Stream(s.port, query, headers)
            self.assertEqual(again.envelope()['seq'], at + 1, (query, headers))
            again.close()
        stale = Stream(s.port, '?since=987654')         # an id from an earlier gateway process
        self.assertEqual(stale.envelope()['seq'], 1)
        stale.close()
        self.assertEqual(call(s.port, 'GET', '/api/v1/events?since=-3').status, 400)

        self.assertEqual(s.stop(), 0, s.text())
        self.assertIn('GATEWAY_STOP_REQUESTED signal=SIGTERM', s.out)
        printed = s.printed()
        self.assertEqual({e['class'] for e in printed}, {'public'})
        types = [e['type'] for e in printed]
        for wanted in ('form.accepted', 'client.spawned', 'progress.irq', 'client.exited', 'irq.delta', 'wiped'):
            self.assertIn(wanted, types)
        self.assertNotIn('progress.io', types)
        self.assertNotIn('result', types)
        for line in s.out + s.err:
            for key in OWNER_KEYS:
                self.assertNotIn(key, line)
        self.assertEqual(b.signals(), [])
        self.assertEqual(len(b.spawns()), 1)
        self.assertZeroed(['in.csv', 'score.txt'])

    def test_requests_are_checked(self):
        b = self.bench
        s = self.serve()
        p = s.port
        J_BODY = json.dumps({'form': J}).encode('utf-8')

        def post(headers=OWNER, body=J_BODY, **kw):
            return call(p, 'POST', '/api/v1/submit', body, headers, **kw)

        checks = [
            (call(p, 'GET', '/', host='evil:%d' % p), 403, 'HOST_FORBIDDEN'),
            (call(p, 'GET', '/api/v1/status', host='evil:8080'), 403, 'HOST_FORBIDDEN'),
            (call(p, 'GET', '/api/v1/events', host='evil:%d' % p), 403, 'HOST_FORBIDDEN'),
            (call(p, 'GET', '/api/v1/status', host='127.0.0.1:%d' % (p + 1)), 403, 'HOST_FORBIDDEN'),
            (call(p, 'GET', '/api/v1/status', host=[]), 403, 'HOST_FORBIDDEN'),
            (call(p, 'GET', '/api/v1/status', host=['127.0.0.1:%d' % p, 'evil:%d' % p]), 403, 'HOST_FORBIDDEN'),
            (call(p, 'GET', '/api/v1/status', headers=(('Origin', 'http://evil.example'),)), 403,
             'ORIGIN_FORBIDDEN'),
            (post(host='evil:%d' % p), 403, 'HOST_FORBIDDEN'),
            (post(headers=(('Content-Type', 'application/json'),)), 403, 'OWNER_HEADER_REQUIRED'),
            (post(headers=(('Content-Type', 'application/json'), ('X-EdgeFHE-Owner', '0'))), 403,
             'OWNER_HEADER_REQUIRED'),
            (post(headers=OWNER + (('X-EdgeFHE-Owner', '1'),)), 403, 'OWNER_HEADER_REQUIRED'),
            (post(headers=OWNER + (('Origin', 'http://evil.example'),)), 403, 'ORIGIN_FORBIDDEN'),
            (post(headers=OWNER + (('Origin', 'null'),)), 403, 'ORIGIN_FORBIDDEN'),
            (post(headers=OWNER + (('Origin', 'http://127.0.0.1:%d' % (p + 1)),)), 403, 'ORIGIN_FORBIDDEN'),
            (post(headers=(('Content-Type', 'text/plain'), ('X-EdgeFHE-Owner', '1'))), 403, 'CONTENT_TYPE_JSON'),
            (post(headers=(('Content-Type', 'application/json; charset=latin-1'), ('X-EdgeFHE-Owner', '1'))), 403,
             'CONTENT_TYPE_JSON'),
            (post(headers=(('X-EdgeFHE-Owner', '1'),)), 403, 'CONTENT_TYPE_JSON'),
            (post(body=b' ' * 5000), 403, 'BODY_TOO_LARGE'),
            (post(headers=OWNER + (('Transfer-Encoding', 'chunked'),)), 403, 'BODY_LENGTH_REQUIRED'),
            (post(body=b'{"form":'), 400, 'BODY_JSON'),
            (post(body=b'[1, 2]'), 400, 'BODY_JSON'),
            (post(body=b'{"form": {}, "form": {}}'), 400, 'BODY_JSON'),
            (post(body=b'{"form": {"GenHlth": 3, "HighBP": 1, "BMI": NaN, "Age": 9}}'), 400, 'BODY_JSON'),
            (post(body=json.dumps({'form': M, 'extra': 1}).encode('utf-8')), 400, 'BODY_FIELDS'),
            (post(body=json.dumps({'backend': 'pl'}).encode('utf-8')), 400, 'BODY_FIELDS'),
            (post(body=json.dumps({'form': M, 'backend': 'gpu'}).encode('utf-8')), 400, 'BACKEND_NOT_ALLOWED'),
            (post(body=json.dumps({'form': [3, 1, 31.2, 9]}).encode('utf-8')), 400, 'FORM_FIELDS'),
            (post(headers=OWNER + (('Origin', 'http://localhost:%d' % p),)), 400, 'BMI_ABOVE_MAX'),
            (post(headers=(('Content-Type', 'application/json; charset=utf-8'), ('X-EdgeFHE-Owner', '1'),
                           ('Origin', 'http://127.0.0.1:%d' % p))), 400, 'BMI_ABOVE_MAX'),
            (call(p, 'GET', '/api/v1/submit'), 405, 'METHOD_NOT_ALLOWED'),
            (call(p, 'POST', '/api/v1/status', b'{}', OWNER), 405, 'METHOD_NOT_ALLOWED'),
            (call(p, 'POST', '/', b'{}', OWNER), 405, 'METHOD_NOT_ALLOWED'),
            (call(p, 'GET', '/api/v1/status', host='localhost:%d' % p), 200, None),
        ]
        for i, (reply, status, code) in enumerate(checks):
            self.assertEqual((reply.status, reply.code()), (status, code), (i, reply.body))
            self.assertSecure(reply.headers)
            if status == 403:
                self.assertTrue(reply.json()['message'])
        refused = post()
        self.assertEqual(refused.status, 400)
        body = refused.json()
        form = core.read_json(core.FORM_JSON)
        self.assertEqual((body['code'], body['message']), ('BMI_ABOVE_MAX', form['refusals']['BMI_ABOVE_MAX']))
        self.assertRegex(body['run'], RUN)
        events = call(p, 'GET', '/api/v1/runs/' + body['run']).json()['events']
        self.assertEqual([(e['type'], e['class']) for e in events], [('form.refused', 'owner')])
        self.assertEqual(b.spawns(), [])
        self.assertFalse(os.path.exists(os.path.join(b.workdir, body['run'])))
        unsupported = call(p, 'PUT', '/api/v1/submit', b'{}', OWNER)
        self.assertEqual(unsupported.status, 501)
        self.assertSecure(unsupported.headers)

    def test_busy_409_and_a_stop_request_waits_for_the_run(self):
        b = self.bench
        b.behave(sleep_s=2.0)
        s = self.serve()
        stream = Stream(s.port)
        first = submit(s.port, M, 'pl')
        self.assertEqual(first.status, 202, first.body)
        run = first.json()['run']
        second = submit(s.port, M, 'pl')
        self.assertEqual((second.status, second.code()), (409, 'BUSY'))
        deadline = time.monotonic() + 30
        while not b.spawns() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(b.spawns()), 1)
        os.kill(s.proc.pid, signal.SIGTERM)                 # the gateway's own PID; the client is never signalled
        asked = time.monotonic()
        status = {}
        while time.monotonic() < asked + 10:
            status = call(s.port, 'GET', '/api/v1/status').json()
            if status['stopping']:
                break
            time.sleep(0.05)
        self.assertEqual((status.get('stopping'), status.get('busy'), status.get('run')), (True, True, run))
        third = submit(s.port, M, 'pl')
        self.assertEqual((third.status, third.code()), (503, 'GATEWAY_STOPPING'))
        got = stream.until(run)
        result = [e['data'] for e in got if e['type'] == 'result']
        self.assertEqual([r['level'] for r in result], [3])
        with self.assertRaises(EOFError):
            for _ in range(20):
                stream.block()
        stream.close()
        self.assertEqual(s.proc.wait(60), 0, s.text())
        self.assertGreater(time.monotonic() - asked, 1.0, 'the gateway returned before its client finished')
        s.stop()
        self.assertIn('GATEWAY_STOP_REQUESTED signal=SIGTERM', s.out)
        self.assertIn('wiped', [e['type'] for e in s.printed() if e['run'] == run])
        self.assertEqual(b.signals(), [])
        self.assertEqual(len(b.spawns()), 1)
        self.assertZeroed(['in.csv', 'score.txt'])
        self.assertEqual(os.listdir(b.workdir), [])         # retain_private is false by default: no private record

    def test_static_files_status_and_traversal(self):
        s = self.serve()
        p = s.port
        index = UI_FILES['index.html'].encode('utf-8')
        for path in ('/', '/?shot=all', '/?stage=4', '/index.html', '/ui/', '/ui', '/ui/index.html'):
            r = call(p, 'GET', path)
            self.assertEqual((r.status, r.headers['content-type'], r.body), (200, 'text/html; charset=utf-8', index),
                             path)
            self.assertSecure(r.headers)
        for path, name, kind in (('/ui/app.js', 'app.js', 'text/javascript; charset=utf-8'),
                                 ('/app.js', 'app.js', 'text/javascript; charset=utf-8'),
                                 ('/ui/lib.mjs', 'lib.mjs', 'text/javascript; charset=utf-8'),
                                 ('/lib.mjs?v=1', 'lib.mjs', 'text/javascript; charset=utf-8'),
                                 ('/ui/style.css', 'style.css', 'text/css; charset=utf-8'),
                                 ('/ui/mark.svg', 'mark.svg', 'image/svg+xml'),
                                 ('/ui/fixtures/run.json', 'fixtures/run.json', 'application/json; charset=utf-8')):
            r = call(p, 'GET', path)
            self.assertEqual((r.status, r.headers['content-type'], r.body),
                             (200, kind, UI_FILES[name].encode('utf-8')), path)
        with open(GATEWAY, 'rb') as f:
            gateway_source = f.read(200)
        for path in ('/ui/../edgefhe_gateway.py', '/../edgefhe_gateway.py', '/ui/%2e%2e/%2e%2e/edgefhe_gateway.py',
                     '/ui/..%2f..%2fedgefhe_gateway.py', '/%2e%2e/edgefhe_gateway.py', '/ui/fixtures/../app.js',
                     '/ui//app.js', '/ui/.hidden.js', '/.hidden.js', '/ui/notes.py', '/notes.py', '/ui/README.md',
                     '/ui/link.js', '/link.js', '/ui/up/outside.js', '/up/outside.js', '/fixtures/run.json',
                     '/ui/app.js%00.html', '/ui/app.js/', '/ui/missing.js', '/etc/passwd', '/ui/%c0%ae%c0%ae/x.js',
                     '/ui/a/b/c/d/e.js', '//127.0.0.1/ui/app.js', '/favicon.ico', '/ui/APP.JS', '/api/v2/status',
                     '/api/v1/nothing', '/api/v1/runs/../status', '/api/v1/runs/r000000000000', '/api/v1/status/'):
            r = call(p, 'GET', path)
            self.assertEqual(r.status, 404, path)
            self.assertIn(r.code(), ('NOT_FOUND', 'RUN_UNKNOWN'), path)
            self.assertNotIn(OUTSIDE.encode('utf-8'), r.body, path)
            self.assertNotIn(gateway_source, r.body, path)
            self.assertSecure(r.headers)
        status = call(p, 'GET', '/api/v1/status')
        self.assertEqual((status.status, status.headers['content-type']), (200, 'application/json; charset=utf-8'))
        body = status.json()
        self.assertEqual([x['id'] for x in body['form']['presets']], list('ACDEMFGHKJ'))
        self.assertEqual((body['progress_ms'], body['rehearsal'], body['busy'], body['stopping']), (25, False, False,
                                                                                                    False))
        self.assertIs(body['retain_private'], False)
        self.assertEqual((body['cloud']['host'], body['sizes']['request_frame_bytes']), ('127.0.0.1', REQUEST_FRAME))

    def test_stream_limit_progress_off_events_none_and_heartbeat(self):
        b = self.bench
        b.behave(io=dict(IO), sleep_s=0.1)
        s = self.serve('--progress-ms', '0', '--events', 'none')
        self.assertEqual(call(s.port, 'GET', '/api/v1/status').json()['progress_ms'], 0)
        streams = [Stream(s.port) for _ in range(httpapi.SSE_STREAMS)]
        for stream in streams:
            self.assertEqual(stream.status, 200)
            stream.block()
        extra = call(s.port, 'GET', '/api/v1/events')
        self.assertEqual((extra.status, extra.code()), (503, 'STREAMS_BUSY'))
        for stream in streams[1:]:
            stream.close()
        stream = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:          # a closed stream frees its slot at its next write
            candidate = Stream(s.port)
            if candidate.status == 200:
                stream = candidate
                break
            candidate.close()
            time.sleep(0.2)
        self.assertIsNotNone(stream, 'the stream slots were not released')
        stream.block()
        r = submit(s.port, M, 'pl')
        self.assertEqual(r.status, 202, r.body)
        run = r.json()['run']
        got = stream.until(run)
        self.assertEqual([(e['type'], e['class']) for e in got if e['run'] == run], ORDER)
        beats = []
        while len(beats) < 3:
            block = stream.block()
            if block['comments'] == ['heartbeat'] and 'data' not in block:
                beats.append(block['at'])
        gaps = [later - earlier for earlier, later in zip(beats, beats[1:])]
        self.assertTrue(all(1.5 <= gap <= 3.0 for gap in gaps), gaps)
        stream.close()
        streams[0].close()
        self.assertEqual(s.stop(), 0, s.text())
        self.assertEqual([line for line in s.out if line.startswith('EVT ')], [])
        self.assertEqual(b.signals(), [])

    def test_quiet_prints_no_events_and_retention_is_reported(self):
        """correctness#3: serve --quiet prints no EVT line; with retain_private true, status and wiped say so."""
        b = self.bench
        b.write_config(retain_private=True)
        s = self.serve('--quiet')
        self.assertIs(call(s.port, 'GET', '/api/v1/status').json()['retain_private'], True)
        stream = Stream(s.port)
        r = submit(s.port, M, 'cpu')
        self.assertEqual(r.status, 202, r.body)
        run = r.json()['run']
        wiped = stream.until(run)[-1]['data']
        stream.close()
        self.assertIs(wiped['retained_private'], True)
        self.assertEqual(os.listdir(os.path.join(b.workdir, 'private')), [run + '.json'])
        self.assertEqual(s.stop(), 0, s.text())
        self.assertEqual([line for line in s.out if line.startswith('EVT ')], [])
        self.assertEqual(b.signals(), [])


@unittest.skipUnless(LINUX_ROOT, 'needs root on Linux (run in the python:3.10-slim container)')
class InProcess(unittest.TestCase):
    """The gateway object in this process (cpu backend: no permit, so PID 1 is acceptable here)."""

    def test_the_run_worker_holds_the_interpreter_open(self):
        bench = core.Bench(sleep_s=1.0)
        try:
            gateway = runner.Gateway(bench.load())
            box = {}

            def handler():                          # a daemon thread, like every HTTP request handler
                box['reply'] = httpapi._submit_outside_daemon(gateway, M, 'cpu')

            thread = threading.Thread(target=handler, daemon=True)
            thread.start()
            thread.join(30)
            status, body = box['reply']
            self.assertEqual(status, 202, body)
            workers = [t for t in threading.enumerate() if t.name == 'edgefhe-run-' + body['run']]
            self.assertEqual(len(workers), 1)
            self.assertFalse(workers[0].daemon, 'a daemon run worker would not keep Python alive for its client')
            self.assertTrue(gateway.wait_idle(60))
            self.assertEqual([e['type'] for e in gateway.events.since(0, run=body['run'])][-1], 'wiped')
            self.assertEqual(bench.signals(), [])
        finally:
            bench.close()


# ------------------------------------------------------------------------------------------------ bind rules
@unittest.skipUnless(LINUX, 'POSIX signals and /dev/shm (run in the python:3.10-slim container)')
class Listen(Secure, unittest.TestCase):
    def setUp(self):
        self.bench = core.Bench()
        self.bench.write_config(ui_dir=make_ui(self.bench.base))

    def tearDown(self):
        self.bench.close()

    def serve_once(self, *extra, **kw):
        listen = kw.pop('listen', None) or '127.0.0.1:%d' % free_port()
        self.bench.write_config(listen=listen)
        environment = dict(os.environ)
        environment.pop(httpapi.REHEARSAL_ENV, None)
        environment.update(kw.pop('env', {}))
        command = [sys.executable, '-I', '-B', GATEWAY, 'serve', '--config', self.bench.cfg_path] + list(extra)
        return subprocess.run(command, capture_output=True, text=True, timeout=60, env=environment)

    def test_other_addresses_are_refused(self):
        on = {httpapi.REHEARSAL_ENV: '1'}
        cases = [
            ((), {'listen': '192.0.2.10:8080'}, 'GATEWAY_LISTEN_REFUSED code=LISTEN_NOT_LOOPBACK'),
            ((), {'listen': '127.0.0.2:8080'}, 'GATEWAY_LISTEN_REFUSED code=LISTEN_NOT_LOOPBACK'),
            ((), {'listen': '0.0.0.0:8080'}, 'GATEWAY_CONFIG_INVALID code=CONFIG_LISTEN'),
            (('--rehearsal-listen', '0.0.0.0'), {}, 'GATEWAY_LISTEN_REFUSED code=REHEARSAL_SWITCHES'),
            (('--rehearsal-listen', '0.0.0.0'), {'env': {httpapi.REHEARSAL_ENV: '0'}},
             'GATEWAY_LISTEN_REFUSED code=REHEARSAL_SWITCHES'),
            (('--rehearsal-listen', '192.0.2.10'), {'env': on}, 'GATEWAY_LISTEN_REFUSED code=REHEARSAL_SWITCHES'),
            (('--rehearsal-listen', '127.0.0.1'), {'env': on}, 'GATEWAY_LISTEN_REFUSED code=REHEARSAL_SWITCHES'),
            (('--rehearsal-listen', '0.0.0.0'), {'env': on}, 'GATEWAY_LISTEN_REFUSED code=REHEARSAL_ON_BOARD'),
            (('--progress-ms', '5000'), {}, None),
            (('--progress-ms', '-1'), {}, None),
            (('--bogus',), {}, None),
            (('--events', 'owner'), {}, None),
        ]
        for extra, kw, marker in cases:
            r = self.serve_once(*extra, **kw)
            self.assertEqual(r.returncode, 4, (extra, kw, r.stdout, r.stderr))
            self.assertNotIn('GATEWAY_SERVING', r.stdout, (extra, kw))
            if marker:
                self.assertIn(marker, r.stdout.splitlines(), (extra, kw, r.stdout))
            else:
                self.assertIn('GATEWAY_USAGE', r.stderr, (extra, kw))

    def test_a_port_in_use_fails(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
            busy.bind(('127.0.0.1', 0))
            busy.listen(1)
            port = busy.getsockname()[1]
            r = self.serve_once(listen='127.0.0.1:%d' % port)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('GATEWAY_SERVE_FAILED code=LISTEN_BIND address=127.0.0.1:%d' % port, r.stdout)

    def test_rehearsal_bind_needs_both_switches_and_no_device(self):
        late_node = os.path.join(self.bench.base, 'no_device_here')
        self.bench.write_config(dma_node=late_node)
        s = Served(self.bench, '--rehearsal-listen', '0.0.0.0', env={httpapi.REHEARSAL_ENV: '1'})
        try:
            self.assertEqual(s.serving, 'GATEWAY_SERVING 0.0.0.0:%d' % s.port, s.text())
            r = call(s.port, 'GET', '/api/v1/status')
            self.assertEqual((r.status, r.json()['rehearsal'], r.json()['retain_private']), (200, True, False))
            self.assertSecure(r.headers)
            self.assertEqual(call(s.port, 'GET', '/', host='192.0.2.1:%d' % s.port).status, 403)
            # security#3: the node appears after the start (the PL was loaded): the pl backend is refused at admission
            open(late_node, 'w').close()
            refused = submit(s.port, M, 'pl')
            self.assertEqual((refused.status, refused.code()), (503, 'REHEARSAL_PL_ON_BOARD'), refused.body)
            self.assertIn(late_node, refused.json()['message'])
            self.assertEqual(self.bench.spawns(), [])
            self.assertEqual(s.stop(), 0, s.text())
        finally:
            s.close()


# ------------------------------------------------------------------------------------------------ the marker
class _MarkedSuite(unittest.TestSuite):
    """Prints HTTP_API_TESTS_PASS after the module's tests when every test passed and none was skipped."""

    def run(self, result, debug=False):
        before = (len(result.failures), len(result.errors), len(result.skipped), len(result.unexpectedSuccesses),
                  result.testsRun)
        super().run(result, debug)
        failed = (len(result.failures) - before[0] + len(result.errors) - before[1]
                  + len(result.unexpectedSuccesses) - before[3])
        skipped = len(result.skipped) - before[2]
        ran = result.testsRun - before[4]
        if failed:
            line = 'HTTP_API_TESTS_FAIL tests=%d failed=%d' % (ran, failed)
        elif skipped:
            line = 'HTTP_API_TESTS_INCOMPLETE tests=%d skipped=%d' % (ran, skipped)
        else:
            line = 'HTTP_API_TESTS_PASS tests=%d' % ran
        sys.stderr.flush()
        sys.stdout.write('\n' + line + '\n')     # on a line of its own, also after the runner's row of dots
        sys.stdout.flush()
        return result


def load_tests(loader, tests, pattern):
    suite = _MarkedSuite()
    suite.addTests(tests)
    return suite


if __name__ == '__main__':
    unittest.main(verbosity=2)
