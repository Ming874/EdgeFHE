"""Owner-screen development server: serves demo/ui and a mock of the gateway API that replays a recorded fixture.

usage (repository root):
  python -B demo/ui/dev_server.py --fixture demo/ui/fixtures/run_M_pl.json --port 8781
      [--preset K] [--time-scale 1.0] [--no-preload] [--fail TLS_DEADLINE] [--rehearsal] [--retain-private]

The mock answers the paths of demo/edge/EVENTS.md section 5 on 127.0.0.1 only, with the same checks as the gateway:
Host 127.0.0.1:PORT or localhost:PORT; a POST needs X-EdgeFHE-Owner: 1, Content-Type application/json, a body of at
most 4 KiB and an Origin that is absent or same-origin; every response carries the gateway's CSP, nosniff and no-store.
At start it preloads the fixture's run (or, with --preset, a run derived for that preset). A submitted form is
validated with the gateway's own rules (demo/edge/efgw/model.py): a refused form gets 400 and a form.refused event; an
accepted form replays the fixture's event sequence and timing with the owner data, commitment, level and boundary
recomputed for that form, fresh placeholder hashes, and the backend's device calls. --fail CODE ends the preloaded
run and later runs with that STOP code instead. The status carries "dev_fixture", so the owner screen shows that the data is synthetic.
The status also carries the gateway's "rehearsal" and "retain_private" fields (both false unless --rehearsal or
--retain-private is given), and every wiped event carries "retained_private". --rehearsal makes spec the default
backend, as in the PC rehearsal image, so the screen shows its PC-rehearsal labels.
Python 3.10 standard library; nothing is written to disk. Ctrl+C stops the server.
"""
import argparse
import copy
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.dirname(HERE)
EDGE = os.path.join(DEMO, 'edge')
MODEL_JSON = os.path.join(DEMO, 'model', 'cdc-diabetes-lr4-v1.json')
FORM_JSON = os.path.join(DEMO, 'model', 'form_cdc.json')
if EDGE not in sys.path:
    sys.path.insert(0, EDGE)
from efgw import model as cdc  # noqa: E402
from efgw import records, runner  # noqa: E402

CSP = ("default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'")
TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
         '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml',
         '.json': 'application/json; charset=utf-8'}
MAX_BODY = 4096
HEARTBEAT_S = 2.0
RUN_ID = re.compile(r'r[0-9a-f]{12}')
UPLOAD_STARTED_BYTES = 32768


class EventLog:
    """In-memory envelopes with gateway-style seq numbers; wait() blocks until an event newer than seq exists."""

    def __init__(self):
        self.cond = threading.Condition()
        self.events = []

    def append(self, run, etype, cls, t_mono_ns, data):
        with self.cond:
            env = {'v': 1, 'seq': len(self.events) + 1, 'run': run, 'type': etype, 'class': cls,
                   't_mono_ns': int(t_mono_ns), 'data': data}
            self.events.append(env)
            self.cond.notify_all()
            return env

    def last_seq(self):
        with self.cond:
            return len(self.events)

    def since(self, seq, run=None):
        with self.cond:
            return [e for e in self.events[max(0, seq):] if run is None or e['run'] == run]

    def wait(self, seq, timeout):
        with self.cond:
            self.cond.wait_for(lambda: len(self.events) > seq, timeout)
            return self.events[max(0, seq):]


class Mock:
    """The fixture, the gateway's form rules and the replay of one run at a time."""

    def __init__(self, fixture_path, time_scale, fail, rehearsal=False, retain=False):
        with open(fixture_path, 'rb') as f:
            self.fixture = json.loads(f.read().decode('utf-8'))
        self.fixture_name = os.path.basename(fixture_path)
        self.inference = cdc.Inference.load(MODEL_JSON, FORM_JSON)
        events = self.fixture['events']
        self.template_run = events[0]['run']
        self.template = [e for e in events if e['run'] == self.template_run]
        self.t0 = self.template[0]['t_mono_ns']
        self.time_scale = time_scale
        self.fail = fail
        self.retain = retain
        self.log = EventLog()
        self.lock = threading.Lock()
        self.current = None
        self.ui = {}
        status = copy.deepcopy(self.fixture['status'])
        status['form'] = self.inference.form
        status['rehearsal'] = rehearsal
        status['retain_private'] = retain
        if rehearsal:
            # The PC rehearsal image runs the PL bit-exact software model by default (demo/sim/gateway.sim.json).
            status['default_backend'] = 'spec'
            status['backends'] = ['spec', 'cpu', 'pl']
        self.status_base = status

    # ------------------------------------------------------------------------------------------- derivation
    def template_data(self, etype, cls=None):
        for e in self.template:
            if e['type'] == etype and (cls is None or e['class'] == cls):
                return copy.deepcopy(e['data'])
        return None

    def derive(self, run, form, backend):
        """[(offset_ns, type, class, data)] for an accepted form, timed like the fixture's run."""
        inf = self.inference
        values = inf.canonical(form)
        canonical_json = inf.canonical_json(form)
        _salt, commitment = records.new_commitment(canonical_json)
        x = inf.csv_values(inf.csv_text(form))
        z = inf.z_values(x)
        contributions = inf.contributions(x)
        pl = backend == 'pl'
        out = []
        for e in self.template:
            off = e['t_mono_ns'] - self.t0
            etype, cls, data = e['type'], e['class'], copy.deepcopy(e['data'])
            if etype == 'form.accepted' and cls == 'public':
                data.update(backend=backend, commitment=commitment)
            elif etype == 'form.accepted':
                data.update(values=values, x=x, z=z, contributions=contributions, bias=inf.bias, commitment=commitment)
            elif etype == 'client.spawned':
                data = self.spawned(data, run, backend)
            elif etype == 'progress.irq' and not pl:
                continue
            elif etype == 'progress.io' and self.fail and data.get('wchar', 0) >= UPLOAD_STARTED_BYTES:
                continue
            elif etype == 'progress.irq' and self.fail and data.get('label') == 'ENC':
                continue
            elif etype == 'client.exited':
                if self.fail:
                    out.append((off, etype, cls, {'returncode': 1, 'wall_ns': data['wall_ns'], 'overdue': False,
                                                  'status': None, 'stop': self.fail, 'stop_detail': '',
                                                  'report': None}))
                    out.append((off + 150000, 'irq.delta', 'public', self.irq_delta(backend, failed=True)))
                    message = runner.describe(self.fail, host=self.status_base['cloud']['host'],
                                              port=self.status_base['cloud']['port'], timeout_ms=30000)
                    out.append((off + 300000, 'error', 'public', {'code': self.fail, 'message': message,
                                                                  'fatal': True, 'stop': True}))
                    out.append((off + 1200000, 'wiped', 'public', self.wiped(backend, reached_wire=False)))
                    return commitment, out
                data = self.exited(data, backend)
            elif etype == 'irq.delta':
                data = self.irq_delta(backend)
            elif etype == 'result':
                error = data['logit_ckks'] - data['logit_plain']
                plain = inf.reference(x)
                result = inf.evaluate(plain + error, plain)
                request, response = inf.frame_bytes()
                report = out_report(out)
                hashes = report['trace'][0]
                result['evidence'] = {
                    'commitment': commitment, 'request_sha256': hashes['request_sha256'],
                    'response_payload_sha256': hashes['response_payload_sha256'],
                    'device_calls': report['device_calls'], 'PL_hz': report['PL_hz'],
                    'irq_delta': self.irq_delta(backend)['delta'], 'request_frame_bytes': request,
                    'response_frame_bytes': response}
                data = result
            elif etype == 'wiped':
                data = self.wiped(backend)
            out.append((off, etype, cls, data))
        return commitment, out

    def spawned(self, data, run, backend):
        argv = [a.replace('/' + self.template_run + '/', '/' + run + '/') for a in data['argv']]
        if '--backend' in argv:
            argv[argv.index('--backend') + 1] = backend
        if backend != 'pl' and '--permit-fd' in argv:
            i = argv.index('--permit-fd')
            del argv[i:i + 2]
        data.update(argv=argv, backend=backend, permit=backend == 'pl', pid=data['pid'] + 1 + secrets.randbelow(400))
        return data

    def exited(self, data, backend):
        report = data['report']
        calls = 0 if backend == 'cpu' else 2
        report.update(status=runner.PASS_STATUS[backend], backend=backend, device_calls=calls,
                      spec_frames=calls if backend == 'spec' else 0, actual_DMA=calls if backend == 'pl' else 0,
                      PL_hz=report['PL_hz'] if backend == 'pl' else 0, real_PL=backend == 'pl')
        for entry in report['trace']:
            entry['request_sha256'] = secrets.token_hex(32)
            entry['response_payload_sha256'] = secrets.token_hex(32)
        data.update(status=report['status'], report=report)
        return data

    def irq_delta(self, backend, failed=False):
        data = self.template_data('irq.delta')
        if backend != 'pl' or failed:
            data['after'] = dict(data['before'])
            data['delta'] = dict((k, 0) for k in data['before'])
            if failed and backend == 'pl':
                data['delta'] = dict((k, 1) for k in data['before'])
                data['after'] = dict((k, v + 1) for k, v in data['before'].items())
            data.update(expected=None, passed=None)
        data['backend'] = backend
        return data

    def wiped(self, backend, reached_wire=True):
        """The wiped event; like the gateway, a private record is kept only for a request that reached the wire."""
        files = ['permit', 'in.csv', 'score.txt'] if backend == 'pl' else ['in.csv', 'score.txt']
        return {'files': files, 'run_dir_removed': True, 'problems': [],
                'retained_private': bool(self.retain and reached_wire)}

    # ------------------------------------------------------------------------------------------------ runs
    def refuse(self, run, code, t_mono_ns):
        inf = self.inference
        message = inf.refusal_message(code)
        self.log.append(run, 'form.refused', 'owner', t_mono_ns, {
            'code': code, 'message': message, 'field': inf.refusal_field(code), 'note': inf.form.get('refusal_note')})
        return message

    def preload(self, preset_id):
        """Add the fixture's own run, or a run derived for preset_id, instantly with the fixture's timestamps."""
        backend = self.status_base['default_backend']
        if preset_id is None:
            preset_id = self.fixture.get('preset')
        if preset_id == self.fixture.get('preset') and backend == 'pl' and not self.fail:
            for e in self.template:
                data = copy.deepcopy(e['data'])
                if e['type'] == 'wiped':
                    data['retained_private'] = bool(self.retain)
                self.log.append(e['run'], e['type'], e['class'], e['t_mono_ns'], data)
            run = self.template_run
        else:
            preset = [p for p in self.inference.presets() if p['id'] == preset_id]
            if not preset:
                raise SystemExit('UI_DEV_SERVER_FAILED unknown preset %s' % preset_id)
            run = 'r' + secrets.token_hex(6)
            code = self.inference.refusal(preset[0]['form'])
            if code:
                self.refuse(run, code, self.t0)
                return run
            _commitment, events = self.derive(run, preset[0]['form'], backend)
            for off, etype, cls, data in events:
                self.log.append(run, etype, cls, self.t0 + off, data)
        ui = self.fixture.get('ui') or {}
        if 'submit_to_commitment_ns' in ui:
            self.ui[run] = {'submit_to_commitment_ns': ui['submit_to_commitment_ns']}
        return run

    def status(self):
        st = copy.deepcopy(self.status_base)
        with self.lock:
            st['busy'] = self.current is not None
            st['run'] = self.current
        st['last_seq'] = self.log.last_seq()
        st['dev_fixture'] = {'file': self.fixture_name, 'synthetic': bool(self.fixture.get('synthetic')),
                             'ui': copy.deepcopy(self.ui)}
        return st

    def submit(self, form, backend):
        status = self.status_base
        backend = status['default_backend'] if backend is None else backend
        if backend not in status['backends']:
            return 400, {'code': 'BACKEND_NOT_ALLOWED', 'message': runner.describe(
                'BACKEND_NOT_ALLOWED', backend=backend, allowed='、'.join(status['backends']))}
        run = 'r' + secrets.token_hex(6)
        code = self.inference.refusal(form)
        if code:
            return 400, {'code': code, 'message': self.refuse(run, code, time.monotonic_ns()), 'run': run}
        with self.lock:
            if self.current is not None:
                return 409, {'code': 'BUSY', 'message': runner.describe('BUSY')}
            self.current = run
        _commitment, events = self.derive(run, form, backend)
        threading.Thread(target=self.replay, args=(run, events), daemon=True, name='replay-' + run).start()
        return 202, {'run': run}

    def replay(self, run, events):
        try:
            start = time.monotonic()
            base = time.monotonic_ns()
            for off, etype, cls, data in events:
                delay = start + off / 1e9 * self.time_scale - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self.log.append(run, etype, cls, base + off, data)
        finally:
            with self.lock:
                self.current = None


def out_report(out):
    for _off, etype, _cls, data in reversed(out):
        if etype == 'client.exited':
            return data['report']
    raise ValueError('client.exited before result')


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'EdgeFHE-UI-dev'
    sys_version = ''
    mock = None
    ui_dir = HERE
    port = 0

    def log_message(self, fmt, *args):
        sys.stderr.write('UI_DEV_SERVER %s %s\n' % (self.command, fmt % args))

    def headers_common(self):
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')

    def reply(self, code, body, ctype='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else (json.dumps(body, ensure_ascii=False) + '\n').encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.headers_common()
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def host_ok(self):
        host = self.headers.get('Host', '')
        return host in ('127.0.0.1:%d' % self.port, 'localhost:%d' % self.port)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.host_ok():
            return self.reply(403, {'code': 'HOST', 'message': '主機名稱不符。'})
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        if path in ('/', '/index.html'):
            return self.static('index.html')
        if path.startswith('/ui/'):
            return self.static(path[4:])
        if path == '/api/v1/status':
            return self.reply(200, self.mock.status())
        if path.startswith('/api/v1/runs/'):
            run = path[len('/api/v1/runs/'):]
            events = self.mock.log.since(0, run) if RUN_ID.fullmatch(run) else []
            return self.reply(200, {'run': run, 'events': events}) if events else self.reply(404, {'code': 'NOT_FOUND'})
        if path == '/api/v1/events':
            return self.events(urllib.parse.parse_qs(url.query))
        return self.reply(404, {'code': 'NOT_FOUND'})

    def static(self, rel):
        rel = urllib.parse.unquote(rel)
        ext = os.path.splitext(rel)[1].lower()
        parts = rel.split('/')
        if ext not in TYPES or '\\' in rel or any(p in ('', '.', '..') for p in parts):
            return self.reply(404, {'code': 'NOT_FOUND'})
        root = os.path.realpath(self.ui_dir)
        full = os.path.realpath(os.path.join(root, *parts))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self.reply(404, {'code': 'NOT_FOUND'})
        with open(full, 'rb') as f:
            return self.reply(200, f.read(), TYPES[ext])

    def events(self, query):
        since = 0
        for value in query.get('since', []) + [self.headers.get('Last-Event-ID', '')]:
            if value.isdigit():
                since = max(since, int(value))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.headers_common()
        self.end_headers()
        last = since
        try:
            while True:
                batch = self.mock.log.wait(last, HEARTBEAT_S)
                if not batch:
                    self.wfile.write(b': heartbeat\n\n')
                for env in batch:
                    line = json.dumps(env, ensure_ascii=False, separators=(',', ':'))
                    self.wfile.write(('id: %d\ndata: %s\n\n' % (env['seq'], line)).encode('utf-8'))
                    last = env['seq']
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def do_POST(self):
        if not self.host_ok():
            return self.reply(403, {'code': 'HOST', 'message': '主機名稱不符。'})
        if urllib.parse.urlsplit(self.path).path != '/api/v1/submit':
            return self.reply(404, {'code': 'NOT_FOUND'})
        origin = self.headers.get('Origin')
        allowed = ('http://127.0.0.1:%d' % self.port, 'http://localhost:%d' % self.port)
        if self.headers.get('X-EdgeFHE-Owner') != '1' or (origin is not None and origin not in allowed):
            return self.reply(403, {'code': 'OWNER_HEADER_OR_ORIGIN', 'message': '請求缺少擁有者標頭或來源不符。'})
        if not (self.headers.get('Content-Type') or '').split(';')[0].strip().lower() == 'application/json':
            return self.reply(403, {'code': 'CONTENT_TYPE', 'message': '請求的內容類型必須為 application/json。'})
        try:
            length = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self.reply(413 if length > MAX_BODY else 411, {'code': 'BODY_SIZE'})
        try:
            body = json.loads(self.rfile.read(length).decode('utf-8'))
            form = body['form']
            backend = body.get('backend')
        except (ValueError, KeyError, TypeError, AttributeError):
            return self.reply(400, {'code': 'FORM_FIELDS', 'message': self.mock.inference.refusal_message('FORM_FIELDS')})
        status, reply = self.mock.submit(form, backend)
        return self.reply(status, reply)


def main(argv=None):
    p = argparse.ArgumentParser(description='EdgeFHE owner-screen development server (127.0.0.1 only).')
    p.add_argument('--fixture', default=os.path.join(HERE, 'fixtures', 'run_M_pl.json'))
    p.add_argument('--port', type=int, default=8781)
    p.add_argument('--preset', help='preload a run derived for this preset id instead of the fixture run (e.g. K)')
    p.add_argument('--time-scale', type=float, default=1.0, help='replay pacing factor (2.0 = twice as slow)')
    p.add_argument('--no-preload', action='store_true')
    p.add_argument('--fail', metavar='STOP_CODE', help='end submitted runs with this client STOP code')
    p.add_argument('--rehearsal', action='store_true',
                   help='status.rehearsal = true and spec as the default backend, like the PC rehearsal gateway')
    p.add_argument('--retain-private', action='store_true',
                   help='status.retain_private = true; wiped events say that the private record is kept')
    args = p.parse_args(argv)
    if not 1 <= args.port <= 65535 or not 0.01 <= args.time_scale <= 1000:
        p.error('port or time scale out of range')
    if args.fail and not re.fullmatch(r'[A-Z0-9_]{3,60}', args.fail):
        p.error('--fail takes a STOP code such as TLS_DEADLINE')
    mock = Mock(args.fixture, args.time_scale, args.fail, rehearsal=args.rehearsal, retain=args.retain_private)
    preloaded = None if args.no_preload else mock.preload(args.preset)
    Handler.mock = mock
    Handler.port = args.port
    server = http.server.ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    print('UI_DEV_SERVER_READY http://127.0.0.1:%d/ fixture=%s synthetic=%d preload=%s events=%d'
          % (args.port, mock.fixture_name, 1 if mock.fixture.get('synthetic') else 0, preloaded or '-',
             mock.log.last_seq()), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print('UI_DEV_SERVER_STOPPED', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
