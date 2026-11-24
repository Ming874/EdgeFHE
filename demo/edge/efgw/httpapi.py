"""Loopback HTTP API of the board gateway: the owner screen, /api/v1 and Server-Sent Events.

edgefhe_gateway.py serve calls serve(gateway, cfg, ...); the contract is demo/edge/EVENTS.md, sections 4 and 5.

  GET  /  and  GET /ui/<path>   the owner screen from ui_dir: extensions .html .js .mjs .css .svg .json only, no
                                traversal, no hidden names (404). The top-level files are also served at /<file>,
                                so that relative references in index.html resolve from / as well as from /ui/.
  GET  /api/v1/status           gateway.status() (which carries retain_private) plus progress_ms and rehearsal
  GET  /api/v1/runs/<run>       {run, events} from the in-memory event log (404 when unknown)
  POST /api/v1/submit           {"form": {"GenHlth", "HighBP", "BMI", "Age"}, "backend": "pl"|"spec"|"cpu"} (backend
                                optional) -> gateway.submit(): 202 {run}, 400 {code, message[, run]}, 409 busy,
                                503 stopping
  GET  /api/v1/events?since=N   Server-Sent Events: 'id: <seq>' and 'data: <envelope>' per event of both classes,
                                no 'event:' field; resume with since or Last-Event-ID; ': heartbeat' every 2 s

Every request needs exactly one Host header equal to 127.0.0.1:PORT or localhost:PORT and, if present, an Origin of
http://127.0.0.1:PORT or http://localhost:PORT. A POST also needs X-EdgeFHE-Owner: 1, Content-Type application/json
and a Content-Length of at most 4 KiB. A request that breaks these rules gets 403. Every response carries the CSP
"default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'", nosniff and
no-store. The server binds 127.0.0.1 only. --rehearsal-listen 0.0.0.0 binds all interfaces for the PC rehearsal, and
only when the environment also has EDGEFHE_REHEARSAL=1 and the machine shows none of runner.board_markers() (the
fixed /dev/edgefhe_v8b_dma, the configured dma_node, a Zynq UltraScale+ device tree). In that mode the gateway also
refuses the pl backend at admission whenever a board marker has appeared since the start (REHEARSAL_PL_ON_BOARD), so
a rehearsal gateway started before the PL load can never drive the FPGA. Owner-class events leave the process only
through this server. stdout, which is the journal under systemd, carries public events only ('EVT <envelope>'; the
high-rate progress.io is left out), or none with --events none or --quiet.

SIGTERM, SIGINT, SIGHUP, SIGQUIT, SIGUSR1 and SIGUSR2 stop the acceptance of forms (503 GATEWAY_STOPPING). The owner
screen stays served until
the run in flight has finished on its own, and then the server closes and serve() returns. The client is never
signalled: a signal during an issued PL ENC quarantines the driver (ef_v8b_main.c:208,334,389), and with systemd
KillMode=mixed every process left behind by the main process would be killed. Python 3.10 standard library only.
"""
import collections
import http.server
import json
import os
import queue
import re
import signal
import socketserver
import stat
import sys
import threading
import time
import urllib.parse

from . import config
from . import progress
from . import records
from . import runner

API = '/api/v1'
LOOPBACK = '127.0.0.1'
REHEARSAL_ADDRESS = '0.0.0.0'
REHEARSAL_ENV = 'EDGEFHE_REHEARSAL'
OWNER_HEADER = 'X-EdgeFHE-Owner'
MAX_BODY = 4096
DRAIN_LIMIT = 65536
HEARTBEAT_S = 2.0
RETRY_MS = 2000
SSE_STREAMS = 8
SSE_BATCH = 256
FEED_CAPACITY = 20000
STATIC_MAX_BYTES = 8 * 1024 * 1024
STATIC_DEPTH = 4
QUIET_TYPES = ('progress.io',)
EVENT_MODES = ('public', 'none')
CSP = "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'"
SECURITY_HEADERS = (
    ('Content-Security-Policy', CSP),
    ('X-Content-Type-Options', 'nosniff'),
    ('Cache-Control', 'no-store'),
    ('Referrer-Policy', 'no-referrer'),
    ('X-Frame-Options', 'DENY'),
    ('Cross-Origin-Resource-Policy', 'same-origin'),
)
STATIC_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.json': 'application/json; charset=utf-8',
}
INDEX_PATHS = ('/', '/index.html', '/ui', '/ui/')
RUN_PATH = re.compile(r'/api/v1/runs/(r[0-9a-f]{12})')
SEGMENT = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]{0,127}')
SEQ = re.compile(r'[0-9]{1,18}')
LENGTH = re.compile(r'[0-9]{1,12}')
EXIT_PASS, EXIT_FAIL, EXIT_USAGE = 0, 1, 4

MESSAGES = {
    'HOST_FORBIDDEN': '本閘道只接受 Host 標頭為 127.0.0.1:%(port)s 或 localhost:%(port)s 的請求。',
    'CLIENT_FORBIDDEN': '本閘道只接受來自本機迴路介面（127.0.0.1）的連線。',
    'ORIGIN_FORBIDDEN': ('本閘道只接受來自擁有者畫面本身（http://127.0.0.1:%(port)s 或 http://localhost:%(port)s）'
                         '的請求。'),
    'OWNER_HEADER_REQUIRED': '送出表單的請求必須帶有 X-EdgeFHE-Owner: 1 標頭。',
    'CONTENT_TYPE_JSON': '送出表單的請求必須使用 Content-Type: application/json（UTF-8）。',
    'BODY_LENGTH_REQUIRED': '送出表單的請求必須以 Content-Length 標明內容長度，且不得使用分段傳輸。',
    'BODY_TOO_LARGE': '送出表單的請求內容不得超過 %(limit)s 位元組。',
    'BODY_INCOMPLETE': '本閘道未收到完整的請求內容。',
    'BODY_JSON': '請求內容不是有效的 UTF-8 JSON 物件（不接受重複的鍵、NaN 或 Infinity）。',
    'BODY_FIELDS': '請求內容只能包含 form（必要）與 backend（選填）兩個欄位。',
    'QUERY_INVALID': 'since 參數與 Last-Event-ID 標頭必須是非負整數，且各自最多出現一次。',
    'NOT_FOUND': '本閘道找不到此路徑。',
    'RUN_UNKNOWN': '本閘道的記憶體中沒有此次執行（%(run)s）的事件。',
    'METHOD_NOT_ALLOWED': '此路徑不支援 %(method)s 方法。',
    'STREAMS_BUSY': '同時開啟的事件串流已達上限（%(limit)s 條），操作人員應關閉其他擁有者畫面後再試。',
    'INTERNAL': '本閘道處理此請求時發生內部錯誤（%(why)s），且本閘道未對加密程式發送任何訊號。',
}
SERVE_MESSAGES = {
    'LISTEN_NOT_LOOPBACK': '設定值「listen」的位址必須是 127.0.0.1（目前為 %(host)s）；擁有者畫面只在本機迴路介面上提供。',
    'REHEARSAL_SWITCHES': ('本閘道只有在同時指定 --rehearsal-listen 0.0.0.0 與環境變數 EDGEFHE_REHEARSAL=1 時，才會監聽'
                           '所有網路介面；此模式僅供 PC 演練使用。'),
    'REHEARSAL_ON_BOARD': '本閘道偵測到 KV260 的跡象（%(node)s），因此本閘道不以監聽所有網路介面的演練模式執行。',
    'LISTEN_BIND': '本閘道無法在 %(address)s 上監聽（%(why)s）。',
    'PROGRESS_MS': '--progress-ms 必須是 0 至 %(max)s 的整數（0 表示停用進度取樣）。',
    'ARGUMENTS': 'serve 指令不接受這些參數：%(args)s（可用的額外參數為 --events public|none）。',
    'UI_DIR': '擁有者畫面的目錄 %(path)s 不存在；本閘道仍提供 /api/v1，但畫面檔案一律回應 404。',
}
_print_lock = threading.Lock()


def _write(stream, line):
    with _print_lock:
        try:
            stream.write(line + '\n')
            stream.flush()
        except (OSError, ValueError):
            pass


def _out(line):
    _write(sys.stdout, line)


def _say(text):
    _write(sys.stderr, text)


def _text(table, code, values):
    return table[code] % collections.defaultdict(lambda: '?', values)


def _signal_name(number):
    try:
        return signal.Signals(number).name
    except ValueError:
        return str(number)


# ----------------------------------------------------------------------------------------------- event fan-out
class Feed:
    """The gateway's events in seq order for the event streams, at O(1) cost per event.

    records.EventLog.since() scans the whole log, and a stream follows the tail many times per second while a form
    runs (a progress event every 25 ms), so the streams read this copy instead. An EventLog listener appends each
    event (inside the log's lock, in seq order); the events that existed before are merged in once."""

    def __init__(self, events, capacity=FEED_CAPACITY):
        self._cond = threading.Condition()
        self._buf = collections.deque(maxlen=capacity)
        self._last = 0
        self._closed = False
        # Lock order: the listener runs inside the event log's lock and then takes this one, so nothing here may
        # call into the event log while holding self._cond.
        events.add_listener(self._append)
        backlog = events.since(0)
        with self._cond:
            head = self._buf[0]['seq'] if self._buf else None
            older = [e for e in backlog if head is None or e['seq'] < head]
            if older:
                merged = older + list(self._buf)
                self._buf = collections.deque(merged[-capacity:], maxlen=capacity)
            self._last = self._buf[-1]['seq'] if self._buf else 0

    def _append(self, envelope):
        with self._cond:
            if self._buf and envelope['seq'] != self._buf[-1]['seq'] + 1:
                self._buf.clear()           # never expected: the index arithmetic below needs consecutive seqs
            self._buf.append(envelope)
            self._last = envelope['seq']
            self._cond.notify_all()

    def last_seq(self):
        with self._cond:
            return self._last

    @property
    def closed(self):
        return self._closed

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def after(self, seq, timeout):
        """Up to SSE_BATCH events with a seq above seq; waits up to timeout seconds while there is none."""
        with self._cond:
            if self._last <= seq and not self._closed and timeout > 0:
                self._cond.wait_for(lambda: self._last > seq or self._closed, timeout)
            if not self._buf or self._last <= seq:
                return []
            start = max(seq + 1 - self._buf[0]['seq'], 0)
            stop = min(len(self._buf), start + SSE_BATCH)
            return [self._buf[i] for i in range(start, stop)]


class _Printer:
    """Public events as 'EVT <envelope>' lines on stdout (the journal), written by a thread of their own so that a
    slow journal never holds the event log's lock."""

    def __init__(self):
        self._queue = queue.Queue(maxsize=4096)
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name='edgefhe-journal', daemon=True)
        self._thread.start()

    def offer(self, envelope):
        if envelope.get('class') != 'public' or envelope.get('type') in QUIET_TYPES:
            return
        try:
            self._queue.put_nowait(envelope)
        except queue.Full:
            self.dropped += 1

    def _run(self):
        while True:
            envelope = self._queue.get()
            if envelope is None:
                return
            try:
                _out('EVT ' + records.dumps(envelope))
            except Exception:
                pass

    def close(self, timeout):
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            return
        self._thread.join(timeout)
        if self.dropped:
            _say('本閘道因日誌輸出過慢而略過 %d 筆公開事件（事件串流不受影響）。' % self.dropped)


# ------------------------------------------------------------------------------------------- server and policy
class _Context:
    """Everything the request handlers share: the gateway, the policy values and the stream bookkeeping."""

    def __init__(self, gateway, port, bind, progress_ms, ui_dir):
        self.gateway = gateway
        self.port = port
        self.bind = bind
        self.rehearsal = bind != LOOPBACK
        self.progress_ms = progress_ms
        self.hosts = frozenset(('127.0.0.1:%d' % port, 'localhost:%d' % port))
        self.origins = frozenset(('http://127.0.0.1:%d' % port, 'http://localhost:%d' % port))
        self.ui_real = os.path.realpath(ui_dir) if ui_dir else None
        self.feed = None
        self.streams = threading.BoundedSemaphore(SSE_STREAMS)
        self._cond = threading.Condition()
        self._accepting = True
        self._submitting = 0
        self._streaming = 0

    # A submission is counted from before gateway.submit() until it returned, so that a stop request never misses a
    # run whose admission was under way (gateway.wait_idle() covers it only once submit() has returned).
    def begin_submit(self):
        with self._cond:
            if not self._accepting:
                return False
            self._submitting += 1
            return True

    def end_submit(self):
        with self._cond:
            self._submitting -= 1
            self._cond.notify_all()

    def close_submissions(self):
        with self._cond:
            self._accepting = False
            self._cond.wait_for(lambda: self._submitting == 0)

    def stream_opened(self):
        with self._cond:
            self._streaming += 1

    def stream_closed(self):
        with self._cond:
            self._streaming -= 1
            self._cond.notify_all()

    def wait_streams(self, timeout):
        with self._cond:
            return self._cond.wait_for(lambda: self._streaming == 0, timeout)

    def screen(self, handler):
        """The code that refuses this request with 403, or None (Host, peer address and Origin)."""
        if not self.rehearsal and not handler.client_address[0].startswith('127.'):
            return 'CLIENT_FORBIDDEN'
        hosts = handler.headers.get_all('Host') or []
        if len(hosts) != 1 or hosts[0].strip().lower() not in self.hosts:
            return 'HOST_FORBIDDEN'
        origins = handler.headers.get_all('Origin') or []
        if len(origins) > 1 or (origins and origins[0].strip().lower() not in self.origins):
            return 'ORIGIN_FORBIDDEN'
        return None

    def static_file(self, path):
        """(bytes, content type) of a file of ui_dir, or None (404) for any other path."""
        if self.ui_real is None:
            return None
        if path in INDEX_PATHS:
            parts = ['index.html']
        elif path.startswith('/ui/'):
            parts = path[4:].split('/')
        else:
            parts = path[1:].split('/')
            if len(parts) != 1:
                return None
        if not 1 <= len(parts) <= STATIC_DEPTH:
            return None
        names = []
        for part in parts:
            try:
                name = urllib.parse.unquote(part, errors='strict')
            except UnicodeDecodeError:
                return None
            if not SEGMENT.fullmatch(name):
                return None
            names.append(name)
        content_type = STATIC_TYPES.get(os.path.splitext(names[-1])[1])
        if content_type is None:
            return None
        full = os.path.realpath(os.path.join(self.ui_real, *names))
        if not full.startswith(self.ui_real + os.sep):
            return None
        try:
            fd = os.open(full, os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0))
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > STATIC_MAX_BYTES:
                return None
            chunks, left = [], st.st_size
            while left > 0:
                chunk = os.read(fd, min(left, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                left -= len(chunk)
            return b''.join(chunks), content_type
        except OSError:
            return None
        finally:
            os.close(fd)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False          # serve() itself waits, with a bound, for the event streams to end
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, ctx):
        self.ctx = ctx
        super().__init__(address, _Handler)

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)       # no reverse DNS lookup: the demo LAN has no name server
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        _say('GATEWAY_HTTP_ERROR %s' % (sys.exc_info()[0].__name__,))


def _split_target(target):
    """(path, query) of an origin-form request target, or (None, None)."""
    if not target.startswith('/') or target.startswith('//'):
        return None, None
    path, _, query = target.partition('?')
    return path, query


def _json_media(value):
    parts = [p.strip() for p in value.split(';')]
    if parts[0].lower() != 'application/json':
        return False
    for param in parts[1:]:
        if not param:
            continue
        name, _, setting = param.partition('=')
        if name.strip().lower() != 'charset' or setting.strip().strip('"').lower() not in ('utf-8', 'utf8'):
            return False
    return True


def _unique_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError('duplicate key')
        seen[key] = value
    return seen


def _no_constant(name):
    raise ValueError('non-finite number')


_INVALID = object()


def _parse_body(data):
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _INVALID


def _resume_point(query, headers):
    """The seq after which a stream starts: the larger of ?since= and Last-Event-ID (0 without both), or None."""
    try:
        params = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=16)
    except ValueError:
        return None
    since = params.get('since', [])
    ids = [v for v in (headers.get_all('Last-Event-ID') or []) if v.strip()]
    if len(since) > 1 or len(ids) > 1:
        return None
    points = []
    for text in since + ids:
        if not SEQ.fullmatch(text.strip()):
            return None
        points.append(int(text.strip()))
    return max(points) if points else 0


def _sse_record(envelope):
    return ('id: %d\ndata: %s\n\n' % (envelope['seq'], records.dumps(envelope))).encode('utf-8')


def _submit_outside_daemon(gateway, form, backend):
    """gateway.submit() called from a non-daemon thread.

    submit() starts the run on a new thread, and a thread inherits the daemon flag of the thread that created it.
    Request handlers are daemon threads; a daemon run worker would not hold the interpreter open, so a gateway that
    exited for any reason during a run would leave its client to systemd's KillMode=mixed. A non-daemon worker makes
    Python wait for the run (and so for the client) before the process ends."""
    box = {}

    def call():
        try:
            box['reply'] = gateway.submit(form, backend)
        except BaseException as e:      # re-raised in the request handler, which answers 500
            box['error'] = e

    thread = threading.Thread(target=call, name='edgefhe-submit', daemon=False)
    thread.start()
    thread.join()
    if 'error' in box:
        raise box['error']
    return box['reply']


def _content_length(headers):
    """The Content-Length of a request without Transfer-Encoding, or None."""
    lengths = headers.get_all('Content-Length') or []
    if headers.get_all('Transfer-Encoding') or len(lengths) != 1 or not LENGTH.fullmatch(lengths[0].strip()):
        return None
    return int(lengths[0].strip())


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'EdgeFHE-Gateway'
    protocol_version = 'HTTP/1.0'
    timeout = 30
    _status = None
    _body_read = False

    def version_string(self):
        return self.server_version

    def log_message(self, format, *args):
        pass        # no access log: paths and timings would only fill the journal; bodies are never logged

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def end_headers(self):
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    # ------------------------------------------------------------------------------------------------ dispatch
    def _handle(self, method):
        ctx = self.server.ctx
        self._status = None
        self._body_read = method != 'POST'
        try:
            refusal = ctx.screen(self)
            if refusal is not None:
                self._refuse(403, refusal)
                return
            path, query = _split_target(self.path)
            if path is None:
                self._refuse(404, 'NOT_FOUND', log=False)
            elif path == API or path.startswith(API + '/'):
                self._api(method, path, query)
            elif method != 'GET':
                self._refuse(405, 'METHOD_NOT_ALLOWED', method=method)
            else:
                self._static(path)
        except OSError:
            self.close_connection = True                 # the peer went away or stopped reading
        except Exception as e:
            _say('GATEWAY_HTTP_INTERNAL %s' % type(e).__name__)
            self.close_connection = True
            if self._status is None:
                try:
                    self._refuse(500, 'INTERNAL', log=False, why=type(e).__name__)
                except OSError:
                    pass

    def _api(self, method, path, query):
        ctx = self.server.ctx
        run = RUN_PATH.fullmatch(path)
        known = path in (API + '/status', API + '/events', API + '/submit') or run is not None
        wanted = 'POST' if path == API + '/submit' else 'GET'
        if not known:
            self._refuse(404, 'NOT_FOUND', log=False)
        elif method != wanted:
            self._refuse(405, 'METHOD_NOT_ALLOWED', method=method)
        elif path == API + '/submit':
            self._submit()
        elif path == API + '/events':
            self._events(query)
        elif path == API + '/status':
            body = dict(ctx.gateway.status())
            body['progress_ms'] = ctx.progress_ms
            body['rehearsal'] = ctx.rehearsal
            self._json(200, body)
        else:
            events = ctx.gateway.events.since(0, run=run.group(1))
            if not events:
                self._refuse(404, 'RUN_UNKNOWN', log=False, run=run.group(1))
            else:
                self._json(200, {'run': run.group(1), 'events': events})

    # ------------------------------------------------------------------------------------------------- submit
    def _submit(self):
        ctx = self.server.ctx
        headers = self.headers
        if [v.strip() for v in (headers.get_all(OWNER_HEADER) or [])] != ['1']:
            self._refuse(403, 'OWNER_HEADER_REQUIRED')
            return
        types = headers.get_all('Content-Type') or []
        if len(types) != 1 or not _json_media(types[0]):
            self._refuse(403, 'CONTENT_TYPE_JSON')
            return
        length = _content_length(headers)
        if length is None:
            self._refuse(403, 'BODY_LENGTH_REQUIRED')
            return
        if length > MAX_BODY:
            self._refuse(403, 'BODY_TOO_LARGE', limit=MAX_BODY)
            return
        self._body_read = True
        data = self.rfile.read(length) if length else b''
        if len(data) != length:
            self._refuse(400, 'BODY_INCOMPLETE')
            return
        body = _parse_body(data)
        if body is _INVALID or not isinstance(body, dict):
            self._refuse(400, 'BODY_JSON')
            return
        if 'form' not in body or set(body) - {'form', 'backend'}:
            self._refuse(400, 'BODY_FIELDS')
            return
        if not ctx.begin_submit():
            self._json(503, {'code': 'GATEWAY_STOPPING', 'message': runner.describe('GATEWAY_STOPPING')})
            return
        try:
            status, reply = _submit_outside_daemon(ctx.gateway, body['form'], body.get('backend'))
        finally:
            ctx.end_submit()
        self._json(status, reply)

    # ------------------------------------------------------------------------------------------------- events
    def _events(self, query):
        ctx = self.server.ctx
        since = _resume_point(query, self.headers)
        if since is None:
            self._refuse(400, 'QUERY_INVALID')
            return
        if not ctx.streams.acquire(blocking=False):
            self._refuse(503, 'STREAMS_BUSY', limit=SSE_STREAMS)
            return
        ctx.stream_opened()
        try:
            feed = ctx.feed
            if since > feed.last_seq():
                since = 0               # an id of an earlier gateway process: this process's log starts again at 1
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            self.wfile.write(('retry: %d\n: heartbeat\n\n' % RETRY_MS).encode('ascii'))
            beat = time.monotonic() + HEARTBEAT_S
            while True:
                batch = feed.after(since, max(0.0, beat - time.monotonic()))
                if batch:
                    self.wfile.write(b''.join(_sse_record(e) for e in batch))
                    since = batch[-1]['seq']
                now = time.monotonic()
                if now >= beat:
                    self.wfile.write(b': heartbeat\n\n')
                    beat = now + HEARTBEAT_S
                if not batch and feed.closed:
                    break
        finally:
            self.close_connection = True
            ctx.stream_closed()
            ctx.streams.release()

    # ------------------------------------------------------------------------------------------------- static
    def _static(self, path):
        found = self.server.ctx.static_file(path)
        if found is None:
            self._refuse(404, 'NOT_FOUND', log=False)
            return
        data, content_type = found
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------------------------------------------------------------------------------------------- responses
    def _json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _drain(self):
        """Read and drop the body of a refused POST (up to 64 KiB): closing a socket with unread bytes resets the
        connection, and the peer could lose the refusal."""
        if self._body_read:
            return
        self._body_read = True
        length = _content_length(self.headers)
        if length is not None and 0 < length <= DRAIN_LIMIT:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def _refuse(self, status, code, log=True, **values):
        values.setdefault('port', self.server.ctx.port)
        if log:
            _say('GATEWAY_HTTP_REFUSED status=%d code=%s' % (status, code))
        self._drain()
        self._json(status, {'code': code, 'message': _text(MESSAGES, code, values)})


# ------------------------------------------------------------------------------------------------------ serve
def _options(argv):
    """The extra serve options (after edgefhe_gateway.py took its own): --events public|none."""
    mode, rest = 'public', list(argv)
    while rest:
        item = rest.pop(0)
        if item == '--events' and rest and rest[0] in EVENT_MODES:
            mode = rest.pop(0)
        elif item.startswith('--events=') and item.split('=', 1)[1] in EVENT_MODES:
            mode = item.split('=', 1)[1]
        else:
            raise ValueError(_text(SERVE_MESSAGES, 'ARGUMENTS', {'args': ' '.join([item] + rest)}))
    return mode


def _listen_refused(code, **values):
    _say(_text(SERVE_MESSAGES, code, values))
    _out('GATEWAY_LISTEN_REFUSED code=%s' % code)
    return EXIT_USAGE


def serve(gateway, cfg, rehearsal_listen=None, progress_ms=None, argv=None, quiet=False):
    """Serve the owner screen and /api/v1 until one of runner.STOP_SIGNALS; returns the exit status.

    0 after a stop request once the run in flight had finished; 1 when the address cannot be bound or the server
    thread failed; 4 for a refused address, a bad --progress-ms or an unknown argument."""
    try:
        events_mode = _options(argv or [])
    except ValueError as e:
        _say('GATEWAY_USAGE %s' % e)
        return EXIT_USAGE
    try:
        host, port = config.parse_listen(cfg.get('listen', '127.0.0.1:8080'))
    except config.ConfigError as e:
        _say(e.message())
        _out('GATEWAY_CONFIG_INVALID code=%s' % e.code)
        return EXIT_USAGE
    if host != LOOPBACK:
        return _listen_refused('LISTEN_NOT_LOOPBACK', host=host)
    bind = LOOPBACK
    if rehearsal_listen is not None:
        if rehearsal_listen != REHEARSAL_ADDRESS or os.environ.get(REHEARSAL_ENV) != '1':
            return _listen_refused('REHEARSAL_SWITCHES')
        markers = runner.board_markers(cfg)
        if markers:
            return _listen_refused('REHEARSAL_ON_BOARD', node='、'.join(markers))
        bind = REHEARSAL_ADDRESS
        gateway.rehearsal = True        # from now on the pl backend is refused once a board marker appears
    interval = cfg.get('progress_ms', progress.DEFAULT_MS) if progress_ms is None else progress_ms
    if type(interval) is not int or not 0 <= interval <= progress.MAX_MS:
        _say('GATEWAY_USAGE %s' % _text(SERVE_MESSAGES, 'PROGRESS_MS', {'max': progress.MAX_MS}))
        return EXIT_USAGE
    ui_dir = cfg.get('ui_dir')
    if not ui_dir or not os.path.isdir(ui_dir):
        _say(_text(SERVE_MESSAGES, 'UI_DIR', {'path': ui_dir}))

    ctx = _Context(gateway, port, bind, interval, ui_dir)
    try:
        server = _Server((bind, port), ctx)
    except OSError as e:
        _say(_text(SERVE_MESSAGES, 'LISTEN_BIND', {'address': '%s:%d' % (bind, port),
                                                   'why': '%s: %s' % (type(e).__name__, e.strerror or e)}))
        _out('GATEWAY_SERVE_FAILED code=LISTEN_BIND address=%s:%d' % (bind, port))
        return EXIT_FAIL
    ctx.feed = Feed(gateway.events)
    printer = _Printer() if events_mode == 'public' and not quiet else None
    if printer is not None:
        gateway.events.add_listener(printer.offer)
    progress.install(gateway, interval)

    def on_signal(signum):
        gateway.request_stop(signum)
        notice = ('\nGATEWAY_STOP_REQUESTED signal=%s：本閘道不再接受新的表單，並將等待執行中的加密程式自行結束後'
                  '再停止，操作人員不得再次中斷或強制終止本閘道。\n' % _signal_name(signum))
        try:
            os.write(2, notice.encode('utf-8'))
        except OSError:
            pass

    try:
        runner.install_stop_signals(on_signal)
    except ValueError:
        pass                         # not the main thread (an embedding test); request_stop() still works
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.25}, name='edgefhe-http',
                              daemon=True)
    thread.start()
    _out('GATEWAY_SERVING %s:%d' % (bind, port))
    if ctx.rehearsal:
        _say('本閘道以 PC 演練模式監聽所有網路介面（%s:%d）；此模式不得用於板上展示。' % (bind, port))
    else:
        _say('本閘道已在 http://127.0.0.1:%d/ 提供擁有者畫面（僅限本機迴路介面）；進度取樣間隔 %s。'
             % (port, '%d 毫秒' % interval if interval else '已停用'))

    crashed = False
    while not gateway.stopping:
        if not thread.is_alive():
            crashed = True
            break
        time.sleep(0.2)
    gateway.request_stop()
    ctx.close_submissions()
    if gateway.status()['busy']:
        _say('本閘道正在等待執行中的加密程式自行結束；本閘道不會中斷加密程式，擁有者畫面在此期間照常顯示進度。')
    while not gateway.wait_idle(1.0):
        pass
    server.shutdown()
    ctx.feed.close()
    ctx.wait_streams(3.0)
    server.server_close()
    if printer is not None:
        printer.close(5.0)
    if gateway.stop_signal is not None:
        _out('GATEWAY_STOP_REQUESTED signal=%s' % _signal_name(gateway.stop_signal))
    _say('本閘道已停止提供擁有者畫面。')
    return EXIT_FAIL if crashed else EXIT_PASS
