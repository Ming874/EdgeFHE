#!/usr/bin/env python3
"""PC rehearsal driver of the EdgeFHE demo (edgefhe-demo-sim image): the form presets through the gateway HTTP API,
the two direct-child permit checks and the fictional disclosures. Research demonstration only; not a medical diagnosis.

rehearse_form.sh runs it inside the rehearsal container as root with python3 -I -B (docker run --init, so that no
process of the rehearsal is PID 1); Python 3.10 standard library only. Nothing here ever signals or kills a client or
the gateway: a watchdog only reports, exactly as on the board, where a signal during an issued PL ENC quarantines the
driver (v8b_rev5/software/driver/ef_v8b_main.c:208,334,389).

usage: python3 -I -B drive_presets.py [MODE] [--config CFG] [--capture CAP] [--server-log LOG] [--requests FILE]
  (no mode)          POST every accepted preset of form_cdc.json in two rounds and every refused preset once between
                     them (A C D E M F G H K, J, A C D E M F G H K: 19 posts) to /api/v1/submit with the configured
                     default backend (spec), follow each run in /api/v1/runs/<run> and on the Server-Sent Events stream,
                     and check for every accepted form: the event order of demo/edge/EVENTS.md section 3, client status
                     CPP_E2E_CPU_SPEC_NUMERIC_AND_DRAIN_PASS with device_calls 2, |logit_ckks - logit_plain| <= 2e-3,
                     logit_plain, level and boundary exactly as form_cdc.json expects (K: boundary L2/L3, no level),
                     the private files wiped, exactly one new TCP SYN to the cloud port in the capture and exactly one
                     server.log response line with the same request and response hashes; for J: 400 BMI_ABOVE_MAX, one
                     form.refused event, no client, no new SYN and no new server.log line; fresh request hashes and
                     commitments for repeated presets; the SSE stream equal to the run logs. Writes the accepted
                     requests to --requests.                                   -> SIM_FORM_PRESETS_PASS n=19 ...
  --permit-check     edgefhe_gateway.py submit --backend pl --expect OWNER_CREATE: the gateway command line issues the
                     PL permit and is the client's direct parent; the client accepts the permit and stops with
                     OWNER_CREATE (no FPGA device exists) before any network connection
                                                                               -> SIM_PERMIT_ACCEPTED_OWNER_CREATE
  --permit-negative  this process is the client's direct parent: the unaltered permit stops with OWNER_CREATE (control),
                     the same permit with one contract byte changed stops with PL_PERMIT_INVOCATION_BINDING; neither
                     opens a network connection                                -> SIM_PERMIT_CORRUPTED_REFUSED
  --write-config     the effective gateway.json: demo/sim/gateway.sim.json with peer_sha256 set to this run's server pin
                     (--server-pin), written 0600 to --out and validated with efgw.config -> SIM_GATEWAY_CONFIG_WRITTEN
  --disclose         after the capture has stopped: 'edgefhe_gateway.py disclose' for every accepted form of the public
                     log, each disclosure checked against the public record and copied to --out (the evidence
                     disclosure/ folder); with --requests the set must equal the driven forms -> SIM_DISCLOSURES_WRITTEN
  --selftest         offline checks of the capture reader, the server.log and SSE parsers, the template and the preset
                     plan (image build)                                        -> SIM_DRIVER_SELFTEST_PASS
Output: marker lines on stdout, zh-TW notes on stderr; a failure ends with '<MARKER>_FAIL <CODE> <zh-TW reason>' and
exit status 1; exit status 2 = usage.
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.dirname(HERE)
EDGE = os.path.join(DEMO, 'edge')
GATEWAY = os.path.join(EDGE, 'edgefhe_gateway.py')
TEMPLATE = os.path.join(HERE, 'gateway.sim.json')
FORM_JSON = os.path.join(DEMO, 'model', 'form_cdc.json')
sys.dont_write_bytecode = True
if EDGE not in sys.path:
    sys.path.insert(0, EDGE)
from efgw import config, model, permit, records, runner  # noqa: E402

PIN_PLACEHOLDER = 'REPLACED_AT_RUNTIME_WITH_THE_SERVER_PIN'
DEFAULT_CONFIG = '/etc/edgefhe-demo/gateway.json'
DEFAULT_CAPTURE = '/evidence/capture.pcapng'
DEFAULT_SERVER_LOG = '/evidence/server.log'
SIM_BACKEND = 'spec'
FORM_DEVICE_CALLS = 2       # one LOAD and one ENC of k = 4 ciphertexts (client.cpp:303, owner_v5_core.hpp:8)
REQUEST_FRAME_BYTES, RESPONSE_FRAME_BYTES = 393472, 98560
RUN_TIMEOUT_S = 180.0       # longer than the gateway watchdog (3 x timeout + 30 s); reaching it is reported, no signal
REFUSAL_WAIT_S = 3.0        # a client spawned by mistake would have connected within this time
SERVER_LOG_WAIT_S = 10.0
HEX64 = re.compile(r'[0-9a-f]{64}\Z')
RUN_ID = re.compile(r'r[0-9a-f]{12}\Z')
EPOCH_LINE = re.compile(r'([0-9]{9,11}\.[0-9]{9}) (.*)\Z')
MARKERS = {'presets': 'SIM_FORM_PRESETS', 'permit-check': 'SIM_PERMIT_CHECK', 'permit-negative': 'SIM_PERMIT_NEGATIVE',
           'write-config': 'SIM_GATEWAY_CONFIG', 'disclose': 'SIM_DISCLOSURES', 'selftest': 'SIM_DRIVER_SELFTEST'}


class Failure(Exception):
    """A rehearsal check failed: code (ASCII) and a zh-TW reason."""

    def __init__(self, code, text):
        super().__init__(code)
        self.code = code
        self.text = text


def emit(line):
    sys.stdout.write(line + '\n')
    sys.stdout.flush()


def note(text):
    sys.stderr.write(text + '\n')
    sys.stderr.flush()


def need(condition, code, text):
    if not condition:
        raise Failure(code, text)


def read_json(path):
    def no_duplicates(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('duplicate key %r' % key)
            out[key] = value
        return out
    with open(path, 'rb') as f:
        return json.loads(f.read().decode('utf-8'), object_pairs_hook=no_duplicates)


def load_config(path):
    try:
        return config.load(path)
    except config.ConfigError as e:
        raise Failure('CONFIG', '本驅動程式無法載入閘道設定檔 %s：%s' % (path, e.message()))


def ms(ns):
    return '%.1f' % (ns / 1e6) if isinstance(ns, (int, float)) and not isinstance(ns, bool) else '-'


def short(value):
    return value[:16] if isinstance(value, str) else '-'


# ------------------------------------------------------------------------------------------- capture reader
PCAP_MAGIC = {b'\xd4\xc3\xb2\xa1': '<', b'\xa1\xb2\xc3\xd4': '>', b'\x4d\x3c\xb2\xa1': '<', b'\xa1\xb2\x3c\x4d': '>'}


def _ipv4_of(frame, linktype):
    """The IPv4 packet inside one captured frame, or None (Ethernet, Linux cooked v1/v2, BSD loopback, raw IP)."""
    if linktype == 1:                                        # DLT_EN10MB (tcpdump on Linux lo)
        offset, ethertype = 14, frame[12:14]
        while ethertype in (b'\x81\x00', b'\x88\xa8') and len(frame) >= offset + 4:
            ethertype, offset = frame[offset + 2:offset + 4], offset + 4
        return frame[offset:] if ethertype == b'\x08\x00' else None
    if linktype == 113:                                      # DLT_LINUX_SLL
        return frame[16:] if frame[14:16] == b'\x08\x00' else None
    if linktype == 276:                                      # DLT_LINUX_SLL2
        return frame[20:] if frame[0:2] == b'\x08\x00' else None
    if linktype == 0:                                        # DLT_NULL: address family in host byte order
        return frame[4:] if frame[:4] in (b'\x02\x00\x00\x00', b'\x00\x00\x00\x02') else None
    if linktype in (12, 14, 101, 228):                       # raw IP
        return frame if frame[:1] and frame[0] >> 4 == 4 else None
    return None


def capture_syns(path, port):
    """(SYN count per client port, complete frames) of the TCP segments with SYN set and ACK clear to `port` over IPv4
    in the classic pcap file that tcpdump writes. A file that is still growing is fine: a record cut short at its end
    is ignored. An empty file (tcpdump writes its header with the first packet) has no SYN."""
    with open(path, 'rb') as f:
        data = f.read()
    if len(data) < 24:
        return {}, 0
    endian = PCAP_MAGIC.get(data[:4])
    if endian is None:
        raise Failure('CAPTURE_FORMAT', '擷取檔 %s 不是 tcpdump 寫出的 pcap 格式。' % path)
    linktype = struct.unpack(endian + 'I', data[20:24])[0] & 0x0FFFFFFF
    syns, frames, offset = {}, 0, 24
    while offset + 16 <= len(data):
        incl = struct.unpack(endian + 'I', data[offset + 8:offset + 12])[0]
        if incl > 0x4000000 or offset + 16 + incl > len(data):
            break
        frame = data[offset + 16:offset + 16 + incl]
        offset += 16 + incl
        frames += 1
        ip = _ipv4_of(frame, linktype)
        if not ip or len(ip) < 20 or ip[0] >> 4 != 4 or ip[9] != 6:
            continue
        if struct.unpack('!H', ip[6:8])[0] & 0x1FFF:      # a later fragment carries no TCP header
            continue
        tcp = ip[(ip[0] & 0x0F) * 4:]
        if len(tcp) < 14:
            continue
        sport, dport = struct.unpack('!HH', tcp[:4])
        flags = tcp[13]
        if dport == port and flags & 0x02 and not flags & 0x10:
            syns[sport] = syns.get(sport, 0) + 1
    return syns, frames


def syn_total(syns):
    return sum(syns.values())


# ------------------------------------------------------------------------------------------ server.log reader
def parse_server_log(text):
    """{'responses': {request_sha256: [row, ...]}, 'stops': [code, ...], 'lines', 'prefixed', 'ready', 'exit'} of the
    lines '<epoch> <text>' that run_server.sh writes (efnet_server JSON status lines and launcher lines)."""
    out = {'responses': {}, 'stops': [], 'lines': 0, 'prefixed': 0, 'ready': False, 'keylog': False, 'exit': None}
    for raw in text.splitlines():
        if not raw.strip():
            continue
        out['lines'] += 1
        m = EPOCH_LINE.match(raw)
        if m:
            out['prefixed'] += 1
        body = m.group(2) if m else raw
        if body.startswith('{'):
            try:
                obj = json.loads(body)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get('status') == 'CPP_E2E_RESPONSE_WRITTEN_NOT_OWNER_ACCEPTANCE':
                out['responses'].setdefault(obj.get('request_sha256'), []).append(obj)
            elif obj.get('status') == 'CPP_E2E_SERVER_READY':
                out['ready'] = True
        elif body.startswith('CPP_E2E_SERVER_CONNECTION_STOP'):
            out['stops'].append(body.split(' ', 1)[1].strip() if ' ' in body else '')
        elif body.startswith('WARNING_DEMO_TLS_KEYLOG_ENABLED'):
            out['keylog'] = True
        elif body.startswith('EDGEFHE_CLOUD_SERVER_EXIT'):
            out['exit'] = body
    return out


def read_server_log(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return parse_server_log(f.read())
    except FileNotFoundError:
        raise Failure('SERVER_LOG', '本驅動程式找不到雲端伺服器的紀錄 %s。' % path)


def response_rows(log):
    return sum(len(v) for v in log['responses'].values())


def real_stops(log):
    """Connection stops other than the idle accept deadline (one every 30 s without a client, server.cpp:17-19)."""
    return [s for s in log['stops'] if s != 'TLS_DEADLINE']


# ------------------------------------------------------------------------------------------------ HTTP client
class Api:
    """The gateway API as the owner screen uses it: Host 127.0.0.1:PORT (http.client), no Origin, and for POST the
    owner header and a JSON body."""

    def __init__(self, port, timeout=30.0):
        self.port = port
        self.timeout = timeout

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=self.timeout)
        headers, data = {}, None
        if body is not None:
            data = json.dumps(body, separators=(',', ':'), ensure_ascii=True).encode('ascii')
            headers = {'Content-Type': 'application/json', 'X-EdgeFHE-Owner': '1'}
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw
        except (OSError, http.client.HTTPException) as e:
            raise Failure('GATEWAY_UNREACHABLE', '本驅動程式無法連線至閘道 127.0.0.1:%d（%s: %s）。'
                          % (self.port, type(e).__name__, e))
        finally:
            conn.close()

    def json(self, method, path, body=None):
        status, headers, raw = self.request(method, path, body)
        try:
            return status, json.loads(raw.decode('utf-8')) if raw else None
        except ValueError:
            raise Failure('GATEWAY_RESPONSE', '閘道對 %s %s 的回應不是 JSON（HTTP %d）。' % (method, path, status))


def wait_gateway(api, seconds=30.0):
    deadline = time.monotonic() + seconds
    while True:
        try:
            status, body = api.json('GET', '/api/v1/status')
            if status == 200 and isinstance(body, dict):
                return body
        except Failure:
            if time.monotonic() > deadline:
                raise
        if time.monotonic() > deadline:
            raise Failure('GATEWAY_STATUS', '閘道在 %d 秒內未回應 /api/v1/status。' % seconds)
        time.sleep(0.2)


def wait_idle(api, seconds=RUN_TIMEOUT_S):
    deadline = time.monotonic() + seconds
    while True:
        status, body = api.json('GET', '/api/v1/status')
        if status == 200 and not body.get('busy'):
            return body
        if time.monotonic() > deadline:
            raise Failure('GATEWAY_BUSY', '閘道在 %d 秒內仍在處理前一筆表單；本驅動程式不中斷任何程序。' % seconds)
        time.sleep(0.1)


def run_events(api, run):
    status, body = api.json('GET', '/api/v1/runs/' + run)
    need(status == 200 and isinstance(body, dict) and body.get('run') == run and isinstance(body.get('events'), list),
         'RUN_EVENTS', '閘道未回傳執行 %s 的事件（HTTP %d）。' % (run, status))
    return body['events']


def wait_run(api, run, seconds=RUN_TIMEOUT_S):
    """The events of an accepted run once the gateway is idle again after its 'wiped' event."""
    deadline = time.monotonic() + seconds
    warned = False
    while True:
        events = run_events(api, run)
        if events and events[-1].get('type') == 'wiped':
            break
        if time.monotonic() > deadline and not warned:
            note('執行 %s 已超過 %d 秒仍未結束；本驅動程式不會中斷加密程式，將持續等待並判定此次演練未通過。' % (run, seconds))
            warned = True
        if warned and time.monotonic() > deadline + 2 * seconds:
            raise Failure('RUN_TIMEOUT', '執行 %s 在 %d 秒內未結束。' % (run, 3 * seconds))
        time.sleep(0.1)
    wait_idle(api)
    events = run_events(api, run)
    need(not warned, 'RUN_TIMEOUT', '執行 %s 超過 %d 秒才結束。' % (run, seconds))
    return events


# ------------------------------------------------------------------------------------------- Server-Sent Events
class SseParser:
    """Incremental parser of a text/event-stream: complete events as (id, data text); comment lines are counted."""

    def __init__(self):
        self.comments = 0
        self._data, self._id = [], None

    def feed(self, line):
        if line == '':
            if not self._data:
                self._id = None
                return None
            event = (self._id, '\n'.join(self._data))
            self._data, self._id = [], None
            return event
        if line.startswith(':'):
            self.comments += 1
            return None
        field, _, value = line.partition(':')
        if value.startswith(' '):
            value = value[1:]
        if field == 'data':
            self._data.append(value)
        elif field == 'id':
            self._id = value
        return None


class EventStream(threading.Thread):
    """GET /api/v1/events?since=N on its own connection: every envelope in arrival order."""

    def __init__(self, port, since):
        super().__init__(name='sim-sse', daemon=True)
        self.envelopes, self.error = [], None
        self.parser = SseParser()
        self._closing = threading.Event()
        self.conn = http.client.HTTPConnection('127.0.0.1', port, timeout=15.0)
        try:
            self.conn.request('GET', '/api/v1/events?since=%d' % since, headers={'Accept': 'text/event-stream'})
            self.resp = self.conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            raise Failure('SSE', '本驅動程式無法開啟閘道的事件串流（%s: %s）。' % (type(e).__name__, e))
        ctype = self.resp.getheader('Content-Type', '')
        need(self.resp.status == 200 and ctype.startswith('text/event-stream'), 'SSE',
             '閘道的事件串流回應不正確（HTTP %d，%s）。' % (self.resp.status, ctype))

    def run(self):
        try:
            while not self._closing.is_set():
                raw = self.resp.readline()
                if not raw:
                    if not self._closing.is_set():
                        self.error = '閘道提前關閉了事件串流'
                    return
                event = self.parser.feed(raw.decode('utf-8').rstrip('\r\n'))
                if event is not None:
                    envelope = json.loads(event[1])
                    if event[0] != str(envelope.get('seq')):
                        self.error = '事件的 id %s 與 seq %s 不一致' % (event[0], envelope.get('seq'))
                        return
                    self.envelopes.append(envelope)
        except (OSError, ValueError, http.client.HTTPException) as e:
            if not self._closing.is_set():
                self.error = '%s: %s' % (type(e).__name__, e)

    def last_seq(self):
        return self.envelopes[-1]['seq'] if self.envelopes else 0

    def close(self):
        self._closing.set()
        try:
            self.conn.sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        self.conn.close()
        self.join(5.0)


# ----------------------------------------------------------------------------------------------- preset plan
def preset_plan(form):
    """(accepted presets, refused presets, posts): two rounds of the accepted presets with the refused ones between."""
    presets = list(form.get('presets') or [])
    accepted = [p for p in presets if not p['expected']['refused']]
    refused = [p for p in presets if p['expected']['refused']]
    posts = [(p, 1) for p in accepted] + [(p, 0) for p in refused] + [(p, 2) for p in accepted]
    return accepted, refused, posts


def level_text(level, boundary):
    if boundary:
        return 'L%d/L%d' % tuple(boundary)
    return 'L%d' % level if level is not None else '-'


def observe(args, cfg):
    syns, frames = capture_syns(args.capture, cfg['port'])
    return {'syns': syns, 'frames': frames, 'log': read_server_log(args.server_log)}


def wait_syn_total(args, cfg, want, seconds):
    deadline = time.monotonic() + seconds
    while True:
        syns, _ = capture_syns(args.capture, cfg['port'])
        if syn_total(syns) >= want or time.monotonic() > deadline:
            return syns
        time.sleep(0.1)


def check_status(status, cfg, inference):
    need(status.get('v') == 1 and status.get('rehearsal') is True, 'STATUS',
         '閘道未以 PC 演練模式執行（status.rehearsal 不是 true）。')
    need(status.get('default_backend') == SIM_BACKEND and SIM_BACKEND in status.get('backends', []), 'STATUS',
         '閘道的預設加密後端應為 spec（PL 位元精確軟體模型），實際為 %s。' % status.get('default_backend'))
    m = status.get('model') or {}
    need(m.get('model_id') == model.MODEL_ID and m.get('sha256') == model.MODEL_SHA256
         and m.get('profile_hex') == cfg['profile_hex'], 'STATUS', '閘道回報的模型或設定檔識別碼不符。')
    cloud = status.get('cloud') or {}
    need(cloud.get('host') == cfg['host'] and cloud.get('port') == cfg['port'] and cloud.get('protocol') == 'TLS 1.3'
         and cloud.get('mutual_authentication') is True and cloud.get('certificate_pinning') is True, 'STATUS',
         '閘道回報的雲端連線設定不符。')
    sizes = status.get('sizes') or {}
    need((sizes.get('request_frame_bytes'), sizes.get('response_frame_bytes')) == inference.frame_bytes()
         == (REQUEST_FRAME_BYTES, RESPONSE_FRAME_BYTES), 'STATUS', '閘道回報的訊框大小不符。')
    need(not status.get('busy') and not status.get('stopping'), 'STATUS', '閘道在演練開始時不是閒置狀態。')


def check_ui(api):
    status, headers, raw = api.request('GET', '/')
    need(status == 200 and headers.get('content-type', '').startswith('text/html') and b'<html' in raw.lower(),
         'UI', '閘道未在 / 提供擁有者畫面（HTTP %d）。' % status)
    csp = headers.get('content-security-policy', '')
    need("default-src 'self'" in csp and headers.get('x-content-type-options') == 'nosniff'
         and 'no-store' in headers.get('cache-control', ''), 'UI', '擁有者畫面的回應缺少 CSP、nosniff 或 no-store 標頭。')
    return len(raw)


# ------------------------------------------------------------------------------------------ accepted-run checks
EXPECTED_ORDER = [('form.accepted', 'public'), ('form.accepted', 'owner'), ('client.spawned', 'public'),
                  ('client.exited', 'public'), ('irq.delta', 'public'), ('result', 'owner'), ('wiped', 'public')]
PROGRESS = ('progress.irq', 'progress.io')


def check_envelopes(run, events):
    last = 0
    for e in events:
        need(e.get('v') == 1 and e.get('run') == run and e.get('class') in ('owner', 'public')
             and isinstance(e.get('seq'), int) and e['seq'] > last and isinstance(e.get('data'), dict), 'EVENTS',
             '執行 %s 的事件信封不符合 EVENTS.md 第 2 節（seq %s）。' % (run, e.get('seq')))
        last = e['seq']


def check_accepted(preset, run, events, cfg, inference):
    """The checked result of one accepted form: every requirement of the module docstring except capture and log."""
    pid, want = preset['id'], preset['expected']
    check_envelopes(run, events)
    errors = [e['data'].get('code') for e in events if e['type'] == 'error']
    need(not errors, 'RUN_ERROR', '預設組 %s 的執行 %s 出現錯誤事件：%s。' % (pid, run, ','.join(map(str, errors))))
    core = [(e['type'], e['class']) for e in events if e['type'] not in PROGRESS]
    need(core == EXPECTED_ORDER, 'EVENT_ORDER', '預設組 %s 的事件順序不符：%s。' % (pid, ' '.join(t for t, _ in core)))
    by_type = {}
    for e in events:
        by_type.setdefault(e['type'], []).append(e)
    spawned, exited = by_type['client.spawned'][0], by_type['client.exited'][0]
    for e in events:
        if e['type'] in PROGRESS:
            need(spawned['seq'] < e['seq'] < exited['seq'], 'EVENT_ORDER',
                 '預設組 %s 的進度事件不在加密程式執行期間。' % pid)
    public, owner = by_type['form.accepted'][0]['data'], by_type['form.accepted'][1]['data']
    commitment = public.get('commitment')
    need(public.get('kind') == 'form' and public.get('backend') == SIM_BACKEND and public.get('model') == model.MODEL_ID
         and public.get('model_sha256') == model.MODEL_SHA256 and isinstance(commitment, str)
         and HEX64.match(commitment) and owner.get('commitment') == commitment, 'FORM_ACCEPTED',
         '預設組 %s 的 form.accepted 事件不符。' % pid)
    need(owner.get('values') == inference.canonical(preset['form']), 'FORM_ACCEPTED',
         '預設組 %s 被接受的答案與預設值不同。' % pid)
    s = spawned['data']
    argv = s.get('argv') or []
    need(s.get('kind') == 'form' and s.get('backend') == SIM_BACKEND and s.get('permit') is False
         and argv[:1] == [cfg['client']] and '--permit-fd' not in argv
         and any(argv[i:i + 2] == ['--backend', SIM_BACKEND] for i in range(len(argv)))
         and s.get('rows') == 1 and s.get('requests') == 1 and s.get('window_ms') == 0, 'CLIENT_SPAWNED',
         '預設組 %s 的 client.spawned 事件不符（後端、參數或呼叫許可）。' % pid)
    x = exited['data']
    report = x.get('report') or {}
    trace = report.get('trace') or []
    need(x.get('returncode') == 0 and x.get('overdue') is False and x.get('stop') is None
         and x.get('status') == runner.PASS_STATUS[SIM_BACKEND], 'CLIENT_EXITED',
         '預設組 %s 的加密程式未以 %s 結束（結束碼 %s，停止代碼 %s）。'
         % (pid, runner.PASS_STATUS[SIM_BACKEND], x.get('returncode'), x.get('stop')))
    need(report.get('backend') == SIM_BACKEND and report.get('model') == model.MODEL_ID and report.get('features') == 4
         and report.get('rows') == 1 and report.get('mode') == 'serial' and report.get('completed_requests') == 1
         and report.get('device_calls') == FORM_DEVICE_CALLS and report.get('spec_frames') == FORM_DEVICE_CALLS
         and report.get('real_PL') is False and report.get('automatic_retries') == 0
         and report.get('synthetic_input') is False and len(trace) == 1, 'CLIENT_REPORT',
         '預設組 %s 的加密程式報告不符（device_calls 應為 2，實際為 %s）。' % (pid, report.get('device_calls')))
    request_sha256, response_sha256 = trace[0].get('request_sha256'), trace[0].get('response_payload_sha256')
    need(isinstance(request_sha256, str) and HEX64.match(request_sha256) and isinstance(response_sha256, str)
         and HEX64.match(response_sha256), 'CLIENT_REPORT', '預設組 %s 的請求或回應雜湊格式不正確。' % pid)
    d = by_type['irq.delta'][0]['data']
    need(d.get('backend') == SIM_BACKEND and d.get('expected') is None and d.get('passed') is None, 'IRQ_DELTA',
         '預設組 %s 的 irq.delta 事件不符（spec 後端不檢查 FPGA 中斷）。' % pid)
    r = by_type['result'][0]['data']
    ckks, plain = r.get('logit_ckks'), r.get('logit_plain')
    need(isinstance(ckks, float) and isinstance(plain, float), 'RESULT', '預設組 %s 的結果缺少 logit。' % pid)
    error = abs(ckks - plain)
    need(r.get('passed') is True and r.get('abs_error') == error and error <= r.get('max_abs_error', 0) <= 2e-3,
         'RESULT_ERROR', '預設組 %s 的 CKKS 結果與明文參考值相差 %.3g，超過容許的 2×10⁻³。' % (pid, error))
    need(plain == want['logit_plain'], 'RESULT_PLAIN',
         '預設組 %s 的明文參考 logit %r 與 form_cdc.json 的 %r 不同。' % (pid, plain, want['logit_plain']))
    need(r.get('level') == want['level'] and r.get('boundary') == want['boundary'], 'RESULT_LEVEL',
         '預設組 %s 的風險等級應為 %s，閘道回傳 %s。'
         % (pid, level_text(want['level'], want['boundary']), level_text(r.get('level'), r.get('boundary'))))
    if want['boundary']:
        need(r.get('boundary_text') == inference.boundary_text(want['boundary']) and r.get('level_code') is None,
             'RESULT_LEVEL', '預設組 %s 應顯示「%s」且不顯示等級。' % (pid, inference.boundary_text(want['boundary'])))
    else:
        item = inference.level_item(want['level'])
        need(r.get('level_code') == item['code'] and r.get('observed_rate') == item['observed_rate']
             and r.get('boundary_text') is None, 'RESULT_LEVEL', '預設組 %s 的等級顯示欄位不符。' % pid)
    ev = r.get('evidence') or {}
    need(ev.get('commitment') == commitment and ev.get('request_sha256') == request_sha256
         and ev.get('response_payload_sha256') == response_sha256 and ev.get('device_calls') == FORM_DEVICE_CALLS
         and ev.get('request_frame_bytes') == REQUEST_FRAME_BYTES
         and ev.get('response_frame_bytes') == RESPONSE_FRAME_BYTES, 'RESULT_EVIDENCE',
         '預設組 %s 的證據欄位（承諾值、雜湊、裝置呼叫次數或訊框大小）不符。' % pid)
    w = by_type['wiped'][0]['data']
    need('in.csv' in w.get('files', []) and 'score.txt' in w.get('files', []) and w.get('run_dir_removed') is True
         and not w.get('problems'), 'WIPE', '預設組 %s 的私有檔案未完整清除：%s。' % (pid, w))
    io = [e['data'] for e in events if e['type'] == 'progress.io']
    first = trace[0]
    return {'preset': pid, 'run': run, 'commitment': commitment, 'request_sha256': request_sha256,
            'response_payload_sha256': response_sha256, 'abs_error': error, 'logit_ckks': ckks, 'level': r['level'],
            'boundary': r['boundary'], 'wall_ns': x.get('wall_ns'), 'cold_ns': report.get('cold_context_and_TLS_ns'),
            'prepare_ns': first.get('prepare_ns'), 'roundtrip_ns': first.get('TLS_roundtrip_and_validate_ns'),
            'decrypt_ns': first.get('decrypt_ns'), 'progress_io': len(io),
            'max_wchar': max([p.get('wchar', 0) for p in io] or [0])}


def wait_server_row(args, request_sha256):
    deadline = time.monotonic() + SERVER_LOG_WAIT_S
    while True:
        log = read_server_log(args.server_log)
        rows = log['responses'].get(request_sha256, [])
        if rows or time.monotonic() > deadline:
            return log, rows
        time.sleep(0.1)


def post_accepted(api, args, cfg, inference, preset, number, rnd, before):
    status, body = api.json('POST', '/api/v1/submit', {'form': preset['form'], 'backend': SIM_BACKEND})
    need(status == 202 and isinstance(body, dict) and RUN_ID.match(body.get('run') or ''), 'SUBMIT',
         '閘道未接受預設組 %s（HTTP %d：%s）。' % (preset['id'], status, body))
    run = body['run']
    rec = check_accepted(preset, run, wait_run(api, run), cfg, inference)
    log, rows = wait_server_row(args, rec['request_sha256'])
    need(len(rows) == 1 and rows[0].get('response_payload_sha256') == rec['response_payload_sha256']
         and rows[0].get('sequence') == 1, 'SERVER_LOG',
         '預設組 %s：雲端伺服器的紀錄中應恰有一行相同請求與回應雜湊的回應（實際 %d 行）。' % (preset['id'], len(rows)))
    need(response_rows(log) == response_rows(before['log']) + 1 and real_stops(log) == real_stops(before['log']),
         'SERVER_LOG', '預設組 %s：雲端伺服器的紀錄出現預期之外的回應或連線中止。' % preset['id'])
    syns = wait_syn_total(args, cfg, syn_total(before['syns']) + 1, 5.0)
    new = syn_total(syns) - syn_total(before['syns'])
    need(new == 1, 'CAPTURE_SYN', '預設組 %s 應在擷取中產生恰好一個新的 TCP SYN，實際為 %d 個。' % (preset['id'], new))
    rec.update(number=number, round=rnd, cloud_ns=rows[0].get('cloud_ns'))
    emit('SIM_FORM n=%d preset=%s round=%d run=%s level=%s abs_error=%.3e request_sha256=%s '
         'response_payload_sha256=%s commitment=%s wall_ms=%s cold_ms=%s prepare_ms=%s roundtrip_ms=%s decrypt_ms=%s '
         'cloud_ms=%s progress_io=%d max_wchar=%d syn_new=1'
         % (number, rec['preset'], rnd, run, level_text(rec['level'], rec['boundary']), rec['abs_error'],
            short(rec['request_sha256']), short(rec['response_payload_sha256']), short(rec['commitment']),
            ms(rec['wall_ns']), ms(rec['cold_ns']), ms(rec['prepare_ns']), ms(rec['roundtrip_ns']),
            ms(rec['decrypt_ns']), ms(rec['cloud_ns']), rec['progress_io'], rec['max_wchar']))
    return rec


def post_refused(api, args, cfg, inference, preset, number, before):
    want = preset['expected']['refused']
    status, body = api.json('POST', '/api/v1/submit', {'form': preset['form'], 'backend': SIM_BACKEND})
    need(status == 400 and isinstance(body, dict) and body.get('code') == want and body.get('message')
         and RUN_ID.match(body.get('run') or ''), 'REFUSAL',
         '預設組 %s 應以 HTTP 400 %s 被拒絕，實際為 HTTP %d：%s。' % (preset['id'], want, status, body))
    run = body['run']
    time.sleep(REFUSAL_WAIT_S)
    events = run_events(api, run)
    check_envelopes(run, events)
    need([(e['type'], e['class']) for e in events] == [('form.refused', 'owner')]
         and events[0]['data'].get('code') == want and events[0]['data'].get('field') == inference.refusal_field(want)
         and events[0]['data'].get('message') == inference.refusal_message(want), 'REFUSAL',
         '預設組 %s 的事件應只有一個 form.refused（%s），實際為：%s。'
         % (preset['id'], want, ' '.join(e['type'] for e in events)))
    syns, _ = capture_syns(args.capture, cfg['port'])
    log = read_server_log(args.server_log)
    syn_new = syn_total(syns) - syn_total(before['syns'])
    lines_new = response_rows(log) - response_rows(before['log']) + len(real_stops(log)) - len(real_stops(before['log']))
    need(syn_new == 0, 'REFUSAL_NETWORK', '預設組 %s 被拒絕後，擷取中出現了 %d 個新的 TCP SYN。' % (preset['id'], syn_new))
    need(lines_new == 0, 'REFUSAL_NETWORK', '預設組 %s 被拒絕後，雲端伺服器的紀錄出現了新的連線。' % preset['id'])
    status_now = api.json('GET', '/api/v1/status')[1]
    need(not status_now.get('busy'), 'REFUSAL', '預設組 %s 被拒絕後，閘道不應處於忙碌狀態。' % preset['id'])
    emit('SIM_FORM_REFUSED n=%d preset=%s run=%s code=%s events=form.refused syn_new=0 server_lines_new=0 '
         'waited_s=%.1f' % (number, preset['id'], run, want, REFUSAL_WAIT_S))
    return {'preset': preset['id'], 'run': run, 'refused': want, 'number': number}


def check_stream(stream, runs, start_seq, api):
    """Every event of every driven run arrived on the SSE stream, in order, and nothing else did."""
    last = max(e['seq'] for run in runs for e in run_events(api, run))
    deadline = time.monotonic() + 5.0
    while stream.last_seq() < last and stream.error is None and time.monotonic() < deadline:
        time.sleep(0.1)
    need(stream.error is None, 'SSE', '閘道的事件串流中斷：%s。' % stream.error)
    got = stream.envelopes
    seqs = [e['seq'] for e in got]
    need(seqs == list(range(start_seq + 1, start_seq + 1 + len(got))) and seqs and seqs[-1] >= last, 'SSE',
         '事件串流的序號不連續或不完整（%s..%s，預期至 %d）。' % (seqs[:1], seqs[-1:], last))
    need(set(e['run'] for e in got) == set(runs), 'SSE', '事件串流含有本次演練以外的執行。')
    for run in runs:
        need([e for e in got if e['run'] == run] == run_events(api, run), 'SSE',
             '執行 %s 在事件串流中的事件與 /api/v1/runs 不同。' % run)
    return len(got), stream.parser.comments


def run_presets(args):
    cfg = load_config(args.config)
    _, port = config.parse_listen(cfg['listen'])
    api = Api(port)
    inference = model.Inference.load(cfg['model_json'], cfg['form_json'])
    status = wait_gateway(api)
    check_status(status, cfg, inference)
    ui_bytes = check_ui(api)
    need(status['form'].get('presets') == read_json(FORM_JSON).get('presets'), 'PRESETS',
         '閘道提供的預設組與 demo/model/form_cdc.json 不同。')
    accepted, refused, posts = preset_plan(status['form'])
    need(accepted and refused, 'PRESETS', 'form_cdc.json 應同時含有可接受與應拒絕的預設組。')
    before = observe(args, cfg)
    need(syn_total(before['syns']) == 0 and response_rows(before['log']) == 0 and before['log']['ready'], 'START',
         '演練開始前，擷取或雲端伺服器的紀錄中已有連線，或伺服器尚未就緒。')
    emit('SIM_PRESETS_PLAN posts=%d accepted=%d refused=%d rounds=2 order=%s backend=%s ui_bytes=%d'
         % (len(posts), len(accepted), len(refused), ','.join(p['id'] for p, _ in posts), SIM_BACKEND, ui_bytes))
    stream = EventStream(port, status['last_seq'])
    stream.start()
    done, runs = [], []
    try:
        for number, (preset, rnd) in enumerate(posts, 1):
            wait_idle(api)
            before = observe(args, cfg)
            if preset['expected']['refused']:
                rec = post_refused(api, args, cfg, inference, preset, number, before)
            else:
                rec = post_accepted(api, args, cfg, inference, preset, number, rnd, before)
            done.append(rec)
            runs.append(rec['run'])
        sse_events, heartbeats = check_stream(stream, runs, status['last_seq'], api)
    finally:
        stream.close()
    forms = [r for r in done if 'refused' not in r]
    for key in ('request_sha256', 'response_payload_sha256', 'commitment'):
        values = [r[key] for r in forms]
        need(len(set(values)) == len(values), 'FRESHNESS', '重複送出的預設組產生了相同的 %s。' % key)
    syns, frames = capture_syns(args.capture, cfg['port'])
    need(syn_total(syns) == len(forms) == len(syns), 'CAPTURE_SYN',
         '擷取中的 TCP SYN 數（%d）應等於被接受的表單數（%d）。' % (syn_total(syns), len(forms)))
    log = read_server_log(args.server_log)
    need(response_rows(log) == len(forms) and not real_stops(log), 'SERVER_LOG',
         '雲端伺服器的紀錄應恰有 %d 行回應且沒有連線中止，實際為 %d 行、中止 %s。'
         % (len(forms), response_rows(log), real_stops(log)))
    if args.requests:
        os.makedirs(os.path.dirname(args.requests) or '.', exist_ok=True)
        with open(args.requests, 'w', encoding='ascii', newline='\n') as f:
            for r in forms:
                f.write('%s %s %s\n' % (r['request_sha256'], r['run'], r['preset']))
    levels = []
    for p in accepted:
        levels.append('%s:%s' % (p['id'], level_text(p['expected']['level'], p['expected']['boundary'])))
    walls = [r['wall_ns'] for r in forms]
    emit('SIM_FORM_PRESETS_PASS n=%d accepted=%d refused=%d connections=%d backend=%s levels=%s max_abs_error=%.3e '
         'device_calls=%d sse_events=%d sse_heartbeats=%d capture_frames=%d wall_ms_median=%s wall_ms_max=%s'
         % (len(done), len(forms), len(done) - len(forms), syn_total(syns), SIM_BACKEND, ','.join(levels),
            max(r['abs_error'] for r in forms), FORM_DEVICE_CALLS, sse_events, heartbeats, frames,
            ms(statistics.median(walls)), ms(max(walls))))
    note('本驅動程式已依序送出 %d 筆虛構表單：%d 筆的風險等級、CKKS 誤差與雜湊三方一致，%d 筆在本機被拒絕且未產生任何網路連線。'
         % (len(done), len(forms), len(done) - len(forms)))
    return 0


# ------------------------------------------------------------------------------------------------ permit checks
def main_preset(cfg):
    inference = model.Inference.load(cfg['model_json'], cfg['form_json'])
    presets = [p for p in inference.presets() if p.get('role') == 'main' and not p['expected']['refused']]
    need(len(presets) == 1, 'PRESETS', 'form_cdc.json 應恰有一個主要示範預設組。')
    return inference, presets[0]


def no_device(cfg):
    need(not os.path.exists(cfg['dma_node']), 'DEVICE_PRESENT',
         'FPGA 裝置 %s 存在。呼叫許可檢查只能在 FPGA 尚未載入時執行（PC 演練映像內不應有此裝置）。' % cfg['dma_node'])
    need(os.getpid() != 1 and os.geteuid() == 0, 'CONTROLLER',
         '呼叫許可檢查須以 root 執行，且本程序不得為 PID 1（請以 docker run --init 啟動演練）。')


def wait_reporting(proc, what):
    """communicate() without a timeout; a timer only reports a long run and never signals."""
    timer = threading.Timer(RUN_TIMEOUT_S, lambda: note('%s 已執行超過 %d 秒；本驅動程式不會中斷它，將持續等待其自行結束。'
                                                        % (what, RUN_TIMEOUT_S)))
    timer.daemon = True
    timer.start()
    try:
        return proc.communicate()
    finally:
        timer.cancel()


def run_permit_check(args):
    cfg = load_config(args.config)
    no_device(cfg)
    inference, preset = main_preset(cfg)
    before, _ = capture_syns(args.capture, cfg['port'])
    form = preset['form']
    argv = [sys.executable, '-I', '-B', GATEWAY, 'submit', '--config', args.config, '--genhlth', str(form['GenHlth']),
            '--highbp', str(form['HighBP']), '--bmi', '%.1f' % form['BMI'], '--age', str(form['Age']),
            '--backend', 'pl', '--expect', 'OWNER_CREATE']
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd='/')
    out, err = wait_reporting(proc, '閘道的 submit 指令')
    out, err = out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')
    markers = [line for line in out.splitlines() if line.startswith('GATEWAY_')]
    accepted = [line for line in markers if line.startswith('GATEWAY_PERMIT_ACCEPTED_OWNER_CREATE ')]
    need(proc.returncode == 0 and len(accepted) == 1, 'GATEWAY_SUBMIT',
         '閘道的 submit --backend pl --expect OWNER_CREATE 應以 GATEWAY_PERMIT_ACCEPTED_OWNER_CREATE 結束（結束碼 %d，%s）。%s'
         % (proc.returncode, ' '.join(markers) or '無標記', err.strip()[-300:]))
    fields = dict(item.split('=', 1) for item in accepted[0].split()[1:] if '=' in item)
    run = fields.get('run', '')
    need(RUN_ID.match(run) and fields.get('backend') == 'pl', 'GATEWAY_SUBMIT', '閘道的結果標記格式不正確：%s' % accepted[0])
    events = [json.loads(line[4:]) for line in out.splitlines() if line.startswith('EVT ')]
    by_type = {}
    for e in events:
        if e.get('run') == run:
            by_type.setdefault(e['type'], []).append(e['data'])
    need(sorted(by_type) == sorted(['form.accepted', 'client.spawned', 'client.exited', 'irq.delta', 'wiped']),
         'EVENTS', '呼叫許可檢查的事件種類不符：%s。' % ' '.join(sorted(by_type)))
    spawned, exited, wiped = by_type['client.spawned'][0], by_type['client.exited'][0], by_type['wiped'][0]
    workdir = cfg['workdir']
    paths = (os.path.join(workdir, run, 'in.csv'), os.path.join(workdir, run, 'score.txt'))
    contract = permit.invocation_contract(cfg, model.MODEL_ID, 1, 1, 0, permit.FORM_FLAGS, paths)['contract']
    argv_client = spawned.get('argv') or []
    need(spawned.get('backend') == 'pl' and spawned.get('permit') is True and spawned.get('contract') == contract
         and '--permit-fd' in argv_client and argv_client[:1] == [cfg['client']], 'CLIENT_SPAWNED',
         '呼叫許可檢查的 client.spawned 事件不符（後端、呼叫許可或合約）。')
    need(exited.get('stop') == 'OWNER_CREATE' and exited.get('returncode') not in (0, None) and exited.get('report') is None,
         'CLIENT_EXITED', '加密程式應在接受呼叫許可後以 OWNER_CREATE 停止，實際停止代碼為 %s。' % exited.get('stop'))
    need('permit' in wiped.get('files', []) and 'in.csv' in wiped.get('files', []) and wiped.get('run_dir_removed') is True,
         'WIPE', '呼叫許可檢查之後，呼叫許可或私有檔案未完整清除：%s。' % wiped)
    time.sleep(1.0)
    after, _ = capture_syns(args.capture, cfg['port'])
    need(syn_total(after) == syn_total(before), 'NETWORK', '呼叫許可檢查期間，擷取中出現了新的 TCP SYN。')
    emit('SIM_PERMIT_ACCEPTED_OWNER_CREATE run=%s preset=%s backend=pl stop=OWNER_CREATE contract=%s gateway_rc=0 '
         'client_rc=%s new_connections=0' % (run, preset['id'], contract[:16], exited.get('returncode')))
    note('閘道指令列簽發的 FPGA 呼叫許可已被加密程式接受；加密程式因容器內沒有 FPGA 裝置而以 OWNER_CREATE 停止，未開啟任何網路連線。')
    return 0


def flip_contract_byte(text, contract):
    """The permit text with one hexadecimal digit of the contract line changed (same length, still hexadecimal)."""
    at = len(text) - 1 - len(contract) // 2
    need(text[len(text) - 1 - len(contract):len(text) - 1] == contract and text.endswith('\n'), 'PERMIT_LAYOUT',
         '呼叫許可的最後一行不是合約。')
    digit = text[at]
    return text[:at] + ('1' if digit == '0' else '0') + text[at + 1:], at


def run_client_with_permit(gateway, cfg, run_dir, label, text, inp, out):
    path = os.path.join(run_dir, 'permit-' + label)
    fd = permit.write_permit(path, text)
    try:
        argv = gateway.client_argv('pl', ['--mode', 'serial', '--input', inp, '--scores-output', out,
                                          '--permit-fd', str(fd)])
        client = runner.ClientProcess(argv, (fd,))
        dog = runner.Watchdog(RUN_TIMEOUT_S, lambda: note('加密程式已執行超過 %d 秒；本驅動程式不會中斷它。' % RUN_TIMEOUT_S))
        try:
            returncode, stdout, stderr, _ = client.wait()
        finally:
            dog.stop()
    finally:
        os.close(fd)
        os.unlink(path)
    report, stop, _, _ = runner.parse_client_output(returncode, stdout, stderr)
    return {'label': label, 'returncode': returncode, 'stop': stop, 'status': report.get('status') if report else None,
            'pid': client.pid}


def run_permit_negative(args):
    cfg = load_config(args.config)
    no_device(cfg)
    inference, preset = main_preset(cfg)
    runner.prepare_workdir(cfg['workdir'])
    lock = runner.PlLock(os.path.join(cfg['workdir'], 'pl.lock'))
    need(lock.acquire(), 'LOCK_BUSY', '另一個程序正持有閘道的執行鎖；本驅動程式不啟動加密程式。')
    run_dir = os.path.join(cfg['workdir'], 'r' + secrets.token_hex(6))
    results, offset = [], None
    try:
        before, _ = capture_syns(args.capture, cfg['port'])
        os.mkdir(run_dir, 0o700)
        inp, out = os.path.join(run_dir, 'in.csv'), os.path.join(run_dir, 'score.txt')
        runner.write_private(inp, inference.csv_text(preset['form']))
        contract = permit.invocation_contract(cfg, model.MODEL_ID, 1, 1, 0, permit.FORM_FLAGS, (inp, out))['contract']
        exe_dev, exe_ino = permit.executable_identity(cfg['client'])
        text = permit.permit_text(permit.read_boot_id(), os.getpid(), exe_dev, exe_ino, contract)
        flipped, offset = flip_contract_byte(text, contract)
        need(len(flipped) == len(text) and sum(a != b for a, b in zip(flipped, text)) == 1, 'PERMIT_LAYOUT',
             '竄改後的呼叫許可應只差一個位元組。')
        gateway = runner.Gateway(cfg)
        for label, permit_text in (('control', text), ('flipped', flipped)):
            results.append(run_client_with_permit(gateway, cfg, run_dir, label, permit_text, inp, out))
    finally:
        for name in ('in.csv', 'score.txt'):
            try:
                runner.wipe_file(os.path.join(run_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(run_dir)
        except OSError:
            pass
        lock.release()
    control, bad = results
    need(control['stop'] == 'OWNER_CREATE' and control['returncode'] != 0, 'CONTROL',
         '未竄改的呼叫許可應被加密程式接受並以 OWNER_CREATE 停止，實際停止代碼為 %s。' % control['stop'])
    need(bad['stop'] == 'PL_PERMIT_INVOCATION_BINDING' and bad['returncode'] != 0, 'NOT_REFUSED',
         '竄改一個位元組的呼叫許可應以 PL_PERMIT_INVOCATION_BINDING 被拒絕，實際停止代碼為 %s。' % bad['stop'])
    time.sleep(1.0)
    after, _ = capture_syns(args.capture, cfg['port'])
    need(syn_total(after) == syn_total(before), 'NETWORK', '呼叫許可的負對照檢查期間，擷取中出現了新的 TCP SYN。')
    emit('SIM_PERMIT_CORRUPTED_REFUSED code=PL_PERMIT_INVOCATION_BINDING control=OWNER_CREATE flipped_offset=%d '
         'controller_pid=%d control_client_pid=%d flipped_client_pid=%d new_connections=0'
         % (offset, os.getpid(), control['pid'], bad['pid']))
    note('本驅動程式作為加密程式的直接父程序：未竄改的呼叫許可被接受（OWNER_CREATE），竄改一個位元組的呼叫許可被拒絕'
         '（PL_PERMIT_INVOCATION_BINDING）；兩次皆未開啟任何網路連線。')
    return 0


# ------------------------------------------------------------------------------------------- config, disclosure
def write_config(template_path, pin, out_path):
    need(isinstance(pin, str) and HEX64.match(pin) is not None, 'PIN', '--server-pin 必須是 64 個小寫十六進位字元。')
    try:
        doc = read_json(template_path)
    except (OSError, ValueError) as e:
        raise Failure('TEMPLATE', '本驅動程式無法讀取設定範本 %s（%s）。' % (template_path, e))
    need(isinstance(doc, dict) and doc.get('peer_sha256') == PIN_PLACEHOLDER, 'TEMPLATE',
         '設定範本的 peer_sha256 必須是預留字串 %s。' % PIN_PLACEHOLDER)
    doc['peer_sha256'] = pin
    data = (json.dumps(doc, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    os.makedirs(os.path.dirname(out_path), mode=0o755, exist_ok=True)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    cfg = load_config(out_path)
    return cfg, hashlib.sha256(data).hexdigest()


def run_write_config(args):
    need(args.server_pin and args.out, 'USAGE', '--write-config 需要 --server-pin 與 --out。')
    cfg, digest = write_config(args.template, args.server_pin, args.out)
    emit('SIM_GATEWAY_CONFIG_WRITTEN path=%s sha256=%s peer_sha256=%s default_backend=%s backends=%s workdir=%s '
         'listen=%s host=%s port=%d' % (args.out, digest, cfg['peer_sha256'], cfg['default_backend'],
                                        ','.join(cfg['backends']), cfg['workdir'], cfg['listen'], cfg['host'],
                                        cfg['port']))
    return 0


def public_forms(path):
    """{request_sha256: public record} of the accepted forms in the gateway public log (none when it does not exist)."""
    forms = {}
    if not os.path.exists(path):
        return forms
    with open(path, 'r', encoding='ascii') as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get('kind') == 'form' and isinstance(rec.get('request_sha256'), str):
                need(rec['request_sha256'] not in forms, 'PUBLIC_LOG', '公開紀錄中有重複的 request_sha256。')
                forms[rec['request_sha256']] = rec
    return forms


def run_disclose(args):
    cfg = load_config(args.config)
    need(args.out, 'USAGE', '--disclose 需要 --out（證據資料夾的 disclosure 目錄）。')
    forms = public_forms(cfg['public_log'])
    if args.requests:
        with open(args.requests, encoding='ascii') as f:
            driven = set(line.split()[0] for line in f if line.strip())
        need(driven == set(forms), 'REQUESTS', '公開紀錄中的表單（%d 筆）與本次送出的表單（%d 筆）不一致。'
             % (len(forms), len(driven)))
    os.makedirs(args.out, mode=0o700, exist_ok=True)
    written = 0
    for request_sha256, rec in sorted(forms.items(), key=lambda kv: kv[1].get('t_board_epoch', 0)):
        proc = subprocess.Popen([sys.executable, '-I', '-B', GATEWAY, 'disclose', '--config', args.config,
                                 request_sha256], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd='/')
        out, err = wait_reporting(proc, '閘道的 disclose 指令')
        lines = [line for line in out.decode('utf-8', 'replace').splitlines()
                 if line.startswith('GATEWAY_DISCLOSURE_WRITTEN ')]
        need(proc.returncode == 0 and len(lines) == 1, 'DISCLOSE', '閘道無法揭露請求 %s：%s'
             % (request_sha256[:16], err.decode('utf-8', 'replace').strip()[-300:]))
        source = os.path.join(cfg['disclosure_dir'], request_sha256 + '.json')
        doc = read_json(source)
        need(doc.get('fictional') is True and doc.get('request_sha256') == request_sha256
             and doc.get('run') == rec.get('run') and doc.get('commitment') == rec.get('commitment')
             and records.commitment_of(doc.get('salt_hex'), doc.get('canonical_json')) == doc.get('commitment')
             and doc.get('response_payload_sha256') == rec.get('response_payload_sha256'), 'DISCLOSURE',
             '請求 %s 的揭露檔與公開紀錄不一致，或承諾值無法打開。' % request_sha256[:16])
        shutil.copyfile(source, os.path.join(args.out, request_sha256 + '.json'))
        written += 1
    need(written > 0 or not args.requests, 'DISCLOSE', '沒有任何可揭露的表單。')
    emit('SIM_DISCLOSURES_WRITTEN n=%d dir=%s fictional=1 commitments_open=%d' % (written, args.out, written))
    note('本驅動程式已將 %d 筆虛構表單的揭露檔（答案、鹽值與兩個 logit）複製到證據資料夾；此步驟在擷取停止之後才執行。' % written)
    return 0


# ----------------------------------------------------------------------------------------------------- selftest
def _pcap(linktype, frames, endian='<', truncate=0):
    data = bytearray(PCAP_MAGIC_BY_ENDIAN[endian] + struct.pack(endian + 'HHiIII', 2, 4, 0, 0, 262144, linktype))
    for i, frame in enumerate(frames):
        data += struct.pack(endian + 'IIII', 1795400000 + i, 0, len(frame), len(frame)) + frame
    return bytes(data[:len(data) - truncate]) if truncate else bytes(data)


PCAP_MAGIC_BY_ENDIAN = {'<': b'\xd4\xc3\xb2\xa1', '>': b'\xa1\xb2\xc3\xd4'}


def _ipv4_tcp(sport, dport, flags, payload=b'', frag=0):
    tcp = struct.pack('!HHIIBBHHH', sport, dport, 1, 0, 5 << 4, flags, 65535, 0, 0) + payload
    total = 20 + len(tcp)
    return struct.pack('!BBHHHBBH4s4s', 0x45, 0, total, 1, frag, 64, 6, 0, b'\x7f\x00\x00\x01',
                       b'\x7f\x00\x00\x01') + tcp


def selftest():
    cases = 0
    eth = b'\x00' * 12 + b'\x08\x00'
    frames = [eth + _ipv4_tcp(40000, 18443, 0x02), eth + _ipv4_tcp(18443, 40000, 0x12),
              eth + _ipv4_tcp(40000, 18443, 0x10, b'x' * 300), eth + _ipv4_tcp(40100, 8080, 0x02),
              eth + _ipv4_tcp(40002, 18443, 0x02), eth + _ipv4_tcp(40000, 18443, 0x02),
              eth + _ipv4_tcp(40004, 18443, 0x02, frag=0x0010), b'\x00' * 12 + b'\x86\xdd' + b'\x60' + b'\x00' * 60,
              eth + _ipv4_tcp(40006, 18443, 0x02)]
    tmp = tempfile.mkdtemp(prefix='sim-selftest-')
    try:
        path = os.path.join(tmp, 'capture.pcapng')
        for endian in ('<', '>'):
            with open(path, 'wb') as f:
                f.write(_pcap(1, frames, endian, truncate=10))
            syns, count = capture_syns(path, 18443)
            need(syns == {40000: 2, 40002: 1} and count == len(frames) - 1, 'SELFTEST',
                 'pcap 讀取器（Ethernet，%s）的結果不符：%s %d' % (endian, syns, count))
            cases += 1
        sll2 = [b'\x08\x00' + b'\x00' * 18 + _ipv4_tcp(41000, 18443, 0x02)]
        with open(path, 'wb') as f:
            f.write(_pcap(276, sll2))
        need(capture_syns(path, 18443) == ({41000: 1}, 1), 'SELFTEST', 'pcap 讀取器（Linux cooked v2）的結果不符。')
        with open(path, 'wb') as f:
            f.write(b'')
        need(capture_syns(path, 18443) == ({}, 0), 'SELFTEST', '空白擷取檔應沒有任何 SYN。')
        with open(path, 'wb') as f:
            f.write(b'\x0a\x0d\x0d\x0a' + b'\x00' * 40)
        try:
            capture_syns(path, 18443)
            raise Failure('SELFTEST', 'pcapng 檔應被拒絕（tcpdump 寫出的是 pcap）。')
        except Failure as e:
            need(e.code == 'CAPTURE_FORMAT', 'SELFTEST', 'pcapng 檔應以 CAPTURE_FORMAT 被拒絕。')
        cases += 3
        request = 'a' * 64
        log = parse_server_log(
            '1795400000.000000001 EDGEFHE_CLOUD_SERVER_START listen=127.0.0.1\n'
            '1795400000.000000002 WARNING_DEMO_TLS_KEYLOG_ENABLED path=/evidence/tls_keys.log\n'
            '1795400000.000000003 {"status":"CPP_E2E_SERVER_READY","port":18443,"test_injection":false}\n'
            '1795400001.000000000 {"status":"CPP_E2E_RESPONSE_WRITTEN_NOT_OWNER_ACCEPTANCE","sequence":1,'
            '"request_sha256":"%s","response_payload_sha256":"%s","cloud_ns":5}\n'
            '1795400031.000000000 CPP_E2E_SERVER_CONNECTION_STOP TLS_DEADLINE\n'
            '1795400032.000000000 CPP_E2E_SERVER_CONNECTION_STOP WIRE_KEY_GENERATION_CHANGE\n'
            'unprefixed line\n'
            '1795400040.000000000 EDGEFHE_CLOUD_SERVER_EXIT rc=143 stop_signal=TERM\n' % (request, 'b' * 64))
        need(log['ready'] and log['keylog'] and response_rows(log) == 1 and log['responses'][request][0]['sequence'] == 1
             and real_stops(log) == ['WIRE_KEY_GENERATION_CHANGE'] and log['lines'] == 8 and log['prefixed'] == 7
             and log['exit'].endswith('stop_signal=TERM'), 'SELFTEST', 'server.log 解析結果不符：%s' % log)
        cases += 1
        parser, got = SseParser(), []
        for line in ('retry: 2000', ': heartbeat', 'id: 7', 'data: {"seq": 7}', '', ': heartbeat', 'id: 8',
                     'data: {"seq":', 'data: 8}', '', ''):
            event = parser.feed(line)
            if event:
                got.append(event)
        need(got == [('7', '{"seq": 7}'), ('8', '{"seq":\n8}')] and parser.comments == 2, 'SELFTEST',
             'SSE 解析結果不符：%s' % got)
        cases += 1
        out = os.path.join(tmp, 'etc', 'gateway.json')
        cfg, _ = write_config(TEMPLATE, 'c' * 64, out)
        need(cfg['peer_sha256'] == 'c' * 64 and cfg['default_backend'] == SIM_BACKEND and 'pl' in cfg['backends']
             and cfg['host'] == '127.0.0.1' and cfg['listen'] == '127.0.0.1:8080' and cfg['retain_private'] is True
             and cfg['workdir'].startswith('/dev/shm/') and os.stat(out).st_mode & 0o777 == 0o600, 'SELFTEST',
             '設定範本產生的閘道設定不符。')
        try:
            write_config(TEMPLATE, 'c' * 64, out)
            raise Failure('SELFTEST', '既有的閘道設定檔不應被覆寫。')
        except FileExistsError:
            pass
        cases += 2
        form = read_json(FORM_JSON)
        accepted, refused, posts = preset_plan(form)
        need([p['id'] for p in accepted] == list('ACDEMFGHK') and [p['id'] for p in refused] == ['J']
             and len(posts) == 19 and refused[0]['expected']['refused'] == 'BMI_ABOVE_MAX'
             and [p['id'] for p in accepted if p['expected']['boundary']] == ['K'], 'SELFTEST',
             '預設組計畫不符：%s' % [p['id'] for p, _ in posts])
        inference = model.Inference.load(os.path.join(DEMO, 'model', 'cdc-diabetes-lr4-v1.json'), FORM_JSON)
        for p in accepted:
            x = inference.csv_values(inference.csv_text(p['form']))
            need(inference.reference(x) == p['expected']['logit_plain']
                 and list(inference.classify(p['expected']['logit_plain'])) == [p['expected']['level'],
                                                                              p['expected']['boundary']],
                 'SELFTEST', '預設組 %s 的明文參考值無法重現。' % p['id'])
        text = permit.permit_text('0' * 8 + '-0000-0000-0000-' + '0' * 12, 4242, 66, 77, 'd' * 64)
        flipped, at = flip_contract_byte(text, 'd' * 64)
        need(len(flipped) == len(text) and sum(a != b for a, b in zip(flipped, text)) == 1 and flipped[at] == '0',
             'SELFTEST', '呼叫許可的位元組竄改不符。')
        cases += 3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    emit('SIM_DRIVER_SELFTEST_PASS cases=%d' % cases)
    return 0


# --------------------------------------------------------------------------------------------------------- main
def build_parser():
    p = argparse.ArgumentParser(prog='drive_presets.py', description='EdgeFHE demo PC rehearsal driver')
    mode = p.add_mutually_exclusive_group()
    for flag in ('permit-check', 'permit-negative', 'write-config', 'disclose', 'selftest'):
        mode.add_argument('--' + flag, dest='mode', action='store_const', const=flag)
    p.set_defaults(mode='presets')
    p.add_argument('--config', default=DEFAULT_CONFIG, help='effective gateway.json (default %(default)s)')
    p.add_argument('--capture', default=DEFAULT_CAPTURE, help='growing tcpdump capture (default %(default)s)')
    p.add_argument('--server-log', default=DEFAULT_SERVER_LOG, help='cloud server.log (default %(default)s)')
    p.add_argument('--requests', help='accepted requests: written by the preset run, read by --disclose')
    p.add_argument('--template', default=TEMPLATE, help='--write-config: the template (default %(default)s)')
    p.add_argument('--server-pin', help='--write-config: lowercase SHA-256 pin of this run\'s server certificate')
    p.add_argument('--out', help='--write-config: the gateway.json to create; --disclose: the disclosure folder')
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    handlers = {'presets': run_presets, 'permit-check': run_permit_check, 'permit-negative': run_permit_negative,
                'write-config': run_write_config, 'disclose': run_disclose, 'selftest': lambda a: selftest()}
    try:
        return handlers[args.mode](args)
    except Failure as e:
        emit('%s_FAIL %s %s' % (MARKERS[args.mode], e.code, e.text))
        return 1
    except (OSError, ValueError, KeyError, TypeError, model.ModelError, permit.PermitError) as e:
        emit('%s_FAIL INTERNAL 本驅動程式發生未預期的錯誤（%s: %s）；本驅動程式未對任何程序發送訊號。'
             % (MARKERS[args.mode], type(e).__name__, e))
        return 1


if __name__ == '__main__':
    sys.exit(main())
