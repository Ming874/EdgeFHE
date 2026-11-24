#!/usr/bin/env python3
"""EdgeFHE owner-laptop serial relay (board side): EF1 lines on stdin/stdout <-> the loopback gateway API.

usage (on the KV260, as the console user, without sudo; owner_bridge.py types this line after kv_console.KV.login):
  exec python3 -I -B /opt/edgefhe-demo/owner/serial_relay.py --gateway http://127.0.0.1:8080
       [--heartbeat-s 2] [--idle-exit-s 120]

The owner laptop is connected to the board only by the micro-USB serial console. owner_bridge.py on the laptop serves
the owner screen on 127.0.0.1:8780 and turns every /api/v1 request into one console line; this program performs the
request against the board gateway on 127.0.0.1, adding the owner header, and writes the answer back as console lines.
Both machines belong to the owner domain. The relay opens no socket other than to the gateway on the loopback
interface, forwards only the paths of the owner screen and never signals any process (the encryption client belongs to
the gateway, and a signal during an issued PL ENC quarantines the driver).

Protocol EF1 (ASCII lines; every other console line is ignored and counted):
  laptop -> board  @EF1 Q <qseq> <b64>          at most 1,000 characters before the line feed (the tty's canonical
                                                line limit is 4,095); <b64> is base64 of {"c": <crc>, "m": <msg>}, where
                                                <msg> is the message as JSON text and <crc> the crc32 of "<qseq> <msg>"
  board -> laptop  @EF1 E <eseq> <crc32> <b64>  any length; <b64> is base64 of the message JSON and <crc32> the crc32
                                                of "<eseq> <b64>"
A crc32 is 8 lowercase hex digits; every JSON text is ASCII (non-ASCII characters are escaped). Sequence number 0 marks
a control message; 1, 2, ... number the messages of one link epoch, separately in each direction. The laptop chooses
the epoch (16 hex digits, field "b") with 'hello'; the relay then drops the previous epoch (its streams and queued
lines) and starts again at E line 1, its own 'hello'.
  Q control   hello {b} | ack {b, ea} | nak {b, ea, from} | bye {b}
  Q numbered  req {method, path[, body]} | sub {[since][, lei]} | unsub {sub}, each with b and ea; the qseq of a req
              or sub is its id
  E control   hb {r, up_s, subs, requests, eseq, backlog, stats} every heartbeat (eseq: the last E line written) |
              ack | qnak {from} | reset {why} | err {code} | bye {why} as the relay stops
  E numbered  hello {v, r, pid, gateway, max_q, heartbeat_s} | resp {id, part, more, body[, status, ctype]} |
              open {sub, status} | ev {sub, id, data} | end {sub, code[, status, ctype, body]}
Every E line carries b (the epoch, or null) and qa (the last Q line taken in order); every Q line carries ea (the last
E line received in order). Both directions retransmit go-back-N. The laptop answers a bad CRC or a gap with
nak {from}, and the relay writes its kept lines again from there (kept until acknowledged, at most 4,096 lines or
8 MiB; a nak for a released line gets 'reset', and the laptop starts a new epoch). The relay answers a bad or missing
Q line with qnak {from}, and the laptop resends its unacknowledged lines. Every event is relayed as it came, except
while more than about 8 KiB of lines wait for the console (a replay of a long event log, or a sampler faster than
115,200 baud): then a progress.io event still waiting is replaced by the next progress.io of the same stream (latest
wins), so the owner screen never falls behind; the live markers of a form run at the normal rate stay exact. The relay
exits on 'bye', at the end of its input, or after --idle-exit-s seconds without a valid Q line (0 keeps it running).

Forwarded: GET /api/v1/status, GET /api/v1/runs/<run>, POST /api/v1/submit (a body of at most 4 KiB; the relay adds
X-EdgeFHE-Owner: 1 and Content-Type: application/json, sends Host <host>:<port> of --gateway and never an Origin) and
GET /api/v1/events as a stream (since, Last-Event-ID). Every other request is refused here. Python 3.10 standard
library only; owner_bridge.py imports the codec of this file.
"""
import argparse
import base64
import binascii
import collections
import http.client
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import zlib

sys.dont_write_bytecode = True

PROTOCOL_VERSION = 1
Q_PREFIX = '@EF1 Q '
E_PREFIX = '@EF1 E '
MAX_Q_LINE = 1000               # characters of a laptop -> board line, without the line feed
HEARTBEAT_S = 2.0
IDLE_EXIT_S = 120.0
MAX_BODY = 4096                 # the gateway's request body limit
PART_BYTES = 3072               # JSON text of one response part
MAX_RESPONSE = 4 * 1024 * 1024
MAX_SSE_LINE = 1024 * 1024
SUBS_MAX = 4
REQUESTS_MAX = 4
SENT_LINES_MAX = 4096
SENT_BYTES_MAX = 8 * 1024 * 1024
BACKLOG_MAX = 2 * 1024 * 1024
COALESCE_BACKLOG = 8192         # queued bytes (about 1 s of console time) above which progress.io is coalesced
REQUEST_TIMEOUT_S = 30.0
STREAM_IDLE_S = 10.0            # the gateway writes a heartbeat every 2 s
QNAK_REPEAT_S = 0.5
REWIND_GUARD_S = 1.0
READ_CHUNK = 65536
OVERLONG = 64 * 1024
OWNER_HEADER = 'X-EdgeFHE-Owner'
JSON_TYPE = 'application/json; charset=utf-8'
API = '/api/v1'
STATUS_PATH = API + '/status'
SUBMIT_PATH = API + '/submit'
EVENTS_PATH = API + '/events'
RUN_PATH = re.compile(r'/api/v1/runs/r[0-9a-f]{12}')
SEQ = re.compile(r'0|[1-9][0-9]{0,11}')
CRC = re.compile(r'[0-9a-f]{8}')
EPOCH = re.compile(r'[0-9a-f]{16}')
Q_CONTROL = ('hello', 'ack', 'nak', 'bye')
Q_NUMBERED = ('req', 'sub', 'unsub')
COALESCE_TYPES = ('progress.io',)
GATEWAY_HOSTS = ('127.0.0.1', 'localhost')
EXIT_OK, EXIT_USAGE = 0, 2

MESSAGES = {
    'RELAY_GATEWAY_UNREACHABLE': ('KV260 上的序列中繼程式無法連線至本機閘道 %(gateway)s（%(why)s）；'
                                  '操作人員應確認 edgefhe-demo 服務正在執行。'),
    'RELAY_PATH_REFUSED': '序列中繼程式只轉送擁有者畫面使用的 /api/v1 路徑；本程式未轉送此請求。',
    'RELAY_METHOD_REFUSED': '序列中繼程式只轉送 GET 與 POST 請求；本程式未轉送此請求。',
    'RELAY_REQUEST_INVALID': '序列中繼程式收到格式不正確的請求；本程式未轉送此請求。',
    'RELAY_BODY_TOO_LARGE': '送出表單的請求內容不得超過 %(limit)s 位元組；序列中繼程式未轉送此請求。',
    'RELAY_BUSY': '序列中繼程式同時處理的請求已達上限（%(limit)s 件）；操作人員應待進行中的請求完成後再操作。',
    'RELAY_STREAMS_BUSY': ('序列中繼程式同時轉送的事件串流已達上限（%(limit)s 條）；操作人員應先關閉其他擁有者畫面，'
                           '再重新整理此畫面。'),
    'RELAY_BAD_RESPONSE': '閘道的回應不是 UTF-8 文字或超過 %(limit)s 位元組；序列中繼程式未轉送此回應。',
    'RELAY_INTERNAL': '序列中繼程式處理此請求時發生內部錯誤（%(why)s）；本程式未對任何程式發送訊號。',
}
BANNER = ('本程式（EdgeFHE 序列中繼）已啟動：此主控台目前承載擁有者筆電與閘道 %s 之間的 EF1 協定行；'
          '本程式只轉送擁有者畫面的請求，不對任何程式發送訊號。按 Ctrl+C 可結束本程式（不影響閘道與加密程式）。\n')


# ------------------------------------------------------------------------------------------------------ codec
class LineError(ValueError):
    """A console line that is not a valid EF1 line; code is NOT_EF1, TOO_LONG, FORMAT, BASE64, JSON, CRC or MESSAGE."""

    def __init__(self, code):
        ValueError.__init__(self, code)
        self.code = code


def crc32_hex(text):
    return '%08x' % (zlib.crc32(text.encode('ascii')) & 0xffffffff)


def dumps(obj):
    return json.dumps(obj, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def _no_constant(name):
    raise ValueError('non-finite number')


def loads(text):
    try:
        return json.loads(text, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise LineError('JSON')


def _b64(text):
    return base64.b64encode(text.encode('ascii')).decode('ascii')


def _unb64(text):
    try:
        return base64.b64decode(text.encode('ascii'), validate=True).decode('ascii')
    except (binascii.Error, ValueError):        # UnicodeError is a ValueError
        raise LineError('BASE64')


def encode_e(seq, msg):
    """'@EF1 E <seq> <crc32> <b64>' (no line feed)."""
    b64 = _b64(dumps(msg))
    return '%s%d %s %s' % (E_PREFIX, seq, crc32_hex('%d %s' % (seq, b64)), b64)


def decode_e(line):
    """(seq, message) of a board -> laptop line that starts with '@EF1 E ', or LineError."""
    if not line.startswith(E_PREFIX):
        raise LineError('NOT_EF1')
    parts = line[len(E_PREFIX):].split(' ')
    if len(parts) != 3 or not SEQ.fullmatch(parts[0]) or not CRC.fullmatch(parts[1]):
        raise LineError('FORMAT')
    if crc32_hex('%s %s' % (parts[0], parts[2])) != parts[1]:
        raise LineError('CRC')
    msg = loads(_unb64(parts[2]))
    if not isinstance(msg, dict):
        raise LineError('FORMAT')
    return int(parts[0]), msg


def encode_q(seq, msg):
    """'@EF1 Q <seq> <b64>' (no line feed); the caller checks the length against MAX_Q_LINE."""
    inner = dumps(msg)
    return '%s%d %s' % (Q_PREFIX, seq, _b64(dumps({'c': crc32_hex('%d %s' % (seq, inner)), 'm': inner})))


def decode_q(line):
    """(seq, message) of a laptop -> board line, or LineError (TOO_LONG before anything else)."""
    if len(line) > MAX_Q_LINE:
        raise LineError('TOO_LONG')
    if not line.startswith(Q_PREFIX):
        raise LineError('NOT_EF1')
    parts = line[len(Q_PREFIX):].split(' ')
    if len(parts) != 2 or not SEQ.fullmatch(parts[0]):
        raise LineError('FORMAT')
    outer = loads(_unb64(parts[1]))
    if (not isinstance(outer, dict) or sorted(outer) != ['c', 'm'] or not isinstance(outer['c'], str)
            or not isinstance(outer['m'], str)):
        raise LineError('FORMAT')
    if crc32_hex('%s %s' % (parts[0], outer['m'])) != outer['c']:
        raise LineError('CRC')
    msg = loads(outer['m'])
    if not isinstance(msg, dict):
        raise LineError('FORMAT')
    return int(parts[0]), msg


def check_q(seq, msg):
    """(op, epoch) of a decoded Q message, or LineError('MESSAGE')."""
    op, epoch = msg.get('op'), msg.get('b')
    if not isinstance(epoch, str) or not EPOCH.fullmatch(epoch):
        raise LineError('MESSAGE')
    if op in Q_CONTROL:
        if seq != 0:
            raise LineError('MESSAGE')
    elif op in Q_NUMBERED:
        if seq == 0:
            raise LineError('MESSAGE')
    else:
        raise LineError('MESSAGE')
    for name in ('ea', 'from', 'since', 'lei', 'sub'):
        if name in msg and not is_count(msg[name]):
            raise LineError('MESSAGE')
    return op, epoch


def is_count(value):
    return type(value) is int and 0 <= value < 10 ** 18


def error_body(code, **values):
    return dumps({'code': code, 'message': MESSAGES[code] % collections.defaultdict(lambda: '?', values)})


def body_parts(text, limit=PART_BYTES):
    """text cut into pieces whose JSON string form stays within about limit characters (at least one piece)."""
    parts, i = [], 0
    while i < len(text):
        n = min(len(text) - i, limit)
        while n > 16 and len(dumps(text[i:i + n])) > limit:
            n //= 2
        parts.append(text[i:i + n])
        i += n
    return parts or ['']


def request_refusal(method, path, body):
    """(status, code) when the relay must not forward this request, else None."""
    if method == 'GET':
        if not isinstance(path, str) or not (path == STATUS_PATH or RUN_PATH.fullmatch(path)):
            return 404, 'RELAY_PATH_REFUSED'
        if body is not None:
            return 400, 'RELAY_REQUEST_INVALID'
        return None
    if method == 'POST':
        if path != SUBMIT_PATH:
            return 404, 'RELAY_PATH_REFUSED'
        if not isinstance(body, str):
            return 400, 'RELAY_REQUEST_INVALID'
        try:
            size = len(body.encode('utf-8'))
        except UnicodeError:
            return 400, 'RELAY_REQUEST_INVALID'
        if size > MAX_BODY:
            return 413, 'RELAY_BODY_TOO_LARGE'
        return None
    return 405, 'RELAY_METHOD_REFUSED'


def parse_gateway(url):
    """(host, port) of http://127.0.0.1:PORT or http://localhost:PORT, else ValueError."""
    parts = urllib.parse.urlsplit(url)
    port = parts.port                       # ValueError for a bad port
    if (parts.scheme != 'http' or parts.hostname not in GATEWAY_HOSTS or port is None or not 0 < port < 65536
            or parts.path not in ('', '/') or parts.query or parts.fragment or parts.username or parts.password):
        raise ValueError(url)
    return parts.hostname, port


def _event_type(text):
    try:
        envelope = json.loads(text)
    except ValueError:
        return None
    return envelope.get('type') if isinstance(envelope, dict) else None


# ------------------------------------------------------------------------------------------------------ relay
class _Item:
    __slots__ = ('epoch', 'msg', 'key', 'size')

    def __init__(self, epoch, msg, key, size):
        self.epoch = epoch
        self.msg = msg
        self.key = key
        self.size = size


class _Upstream:
    """One event stream from the gateway; close() unblocks its reader thread from any thread."""

    def __init__(self, sid):
        self.sid = sid
        self.sock = None
        self.closed = False
        self._lock = threading.Lock()

    def attach(self, sock):
        with self._lock:
            if self.closed:
                return False
            self.sock = sock
            return True

    def close(self):
        with self._lock:
            self.closed = True
            sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class Relay:
    """The board side of EF1. write(bytes) puts whole lines on the console; run(lines) returns the stop reason."""

    def __init__(self, gateway, write, heartbeat_s=HEARTBEAT_S, idle_exit_s=IDLE_EXIT_S, clock=time.monotonic):
        self.host, self.port = gateway
        self.url = 'http://%s:%d' % (self.host, self.port)
        self._write = write
        self.heartbeat_s = heartbeat_s
        self.idle_exit_s = idle_exit_s
        self.clock = clock
        self.instance = secrets.token_hex(8)
        self.started = clock()
        self.cond = threading.Condition()
        self.done = threading.Event()
        self.stop_reason = None
        self.epoch = None
        self.eseq = 0
        self.q_next = 1
        self.sent = collections.deque()          # (eseq, line) written and not yet acknowledged
        self.sent_bytes = 0
        self.rewind = None
        self.rewound = (None, 0.0)
        self.control = collections.deque()       # unnumbered messages, written before numbered ones
        self.data = collections.deque()          # _Item of numbered messages
        self.backlog = 0
        self.subs = {}
        self.requests = 0
        self.stats = collections.Counter()
        self.last_q = clock()
        self.qnak_at = (None, 0.0)
        self.coalesce_backlog = COALESCE_BACKLOG
        self._writer = None

    # ----------------------------------------------------------------------------------------------- output
    def _stamp(self, msg):
        out = dict(msg)
        out['b'] = self.epoch
        out['qa'] = self.q_next - 1
        return out

    def _next_lines(self):
        """Under the lock: the next lines to write, or None when nothing is waiting."""
        if self.rewind is not None:
            start, self.rewind = self.rewind, None
            lines = [line for seq, line in self.sent if seq >= start]
            self.stats['resent'] += len(lines)
            return lines
        if self.control:
            msg = self._stamp(self.control.popleft())
            if msg['t'] == 'hb':
                msg['eseq'] = self.eseq         # every line up to eseq is already written: the laptop sees a lost tail
            return [encode_e(0, msg)]
        while self.data:
            item = self.data.popleft()
            self.backlog -= item.size
            if item.epoch != self.epoch:
                continue
            self.eseq += 1
            line = encode_e(self.eseq, self._stamp(item.msg))
            self.sent.append((self.eseq, line))
            self.sent_bytes += len(line)
            while len(self.sent) > SENT_LINES_MAX or self.sent_bytes > SENT_BYTES_MAX:
                self.sent_bytes -= len(self.sent.popleft()[1])
                self.stats['released'] += 1
            return [line]
        return None

    def _write_loop(self):
        try:
            while True:
                with self.cond:
                    lines = self._next_lines()
                    while lines is None:
                        if self.done.is_set():
                            return
                        self.cond.wait(0.5)
                        lines = self._next_lines()
                    self.stats['lines_out'] += len(lines)
                for line in lines:
                    self._write((line + '\n').encode('ascii'))
        except OSError:
            self.stop('OUTPUT_CLOSED')

    def _control_msg(self, msg):
        with self.cond:
            self.control.append(msg)
            self.cond.notify_all()

    def put(self, epoch, msg, key=None):
        """Queue a numbered message of epoch; False when the epoch is gone or an event stream overflows. A message
        with a key replaces a waiting message with the same key at the tail, but only while the backlog is large."""
        size = len(msg.get('data') or msg.get('body') or '') + 160
        with self.cond:
            if epoch != self.epoch or self.done.is_set():
                return False
            tail = self.data[-1] if self.data else None
            if (key is not None and tail is not None and tail.key == key and tail.epoch == epoch
                    and self.backlog > self.coalesce_backlog):
                self.data.pop()                  # latest wins: the replaced progress sample was never written
                self.backlog -= tail.size
                self.stats['coalesced'] += 1
            elif msg.get('t') == 'ev' and self.backlog + size > BACKLOG_MAX:
                kept = [i for i in self.data if not (i.msg.get('t') == 'ev' and i.msg.get('sub') == msg['sub'])]
                self.data = collections.deque(kept)
                self.backlog = sum(i.size for i in kept)
                self.stats['backlog_ends'] += 1
                return False
            self.data.append(_Item(epoch, msg, key, size))
            self.backlog += size
            self.cond.notify_all()
            return True

    def heartbeat(self):
        with self.cond:
            stats = dict((k, v) for k, v in sorted(self.stats.items()) if v)
            self.control.append({'t': 'hb', 'r': self.instance, 'up_s': round(self.clock() - self.started, 1),
                                 'subs': len(self.subs), 'requests': self.requests, 'eseq': self.eseq,
                                 'backlog': self.backlog, 'stats': stats})
            self.cond.notify_all()

    # ------------------------------------------------------------------------------------------------ input
    def on_line(self, text):
        """One console line (without its line feed)."""
        text = text.rstrip('\r\n')
        if not text.strip():
            return
        if len(text) > MAX_Q_LINE:
            with self.cond:
                self.stats['q_too_long'] += 1
            self._control_msg({'t': 'err', 'code': 'Q_LINE_TOO_LONG', 'length': len(text), 'limit': MAX_Q_LINE})
            return
        if not text.startswith(Q_PREFIX):
            with self.cond:
                self.stats['ignored'] += 1
            return
        try:
            seq, msg = decode_q(text)
            op, epoch = check_q(seq, msg)
        except LineError as e:
            with self.cond:
                self.stats['q_bad'] += 1
                self.stats['q_bad_' + e.code.lower()] += 1
            self._qnak()
            return
        self.last_q = self.clock()
        if op == 'hello':
            self._hello(epoch)
            return
        if op == 'bye':
            self._bye(epoch)
            return
        gap = False
        with self.cond:
            if epoch != self.epoch:
                self.stats['q_stale'] += 1
                return
            self._acked(msg.get('ea'))
            if op == 'ack':
                return
            if op == 'nak':
                self._nak(msg.get('from'))
                return
            if seq != self.q_next:
                if seq < self.q_next:
                    self.stats['q_dup'] += 1
                    self.control.append({'t': 'ack'})
                    self.cond.notify_all()
                    return
                self.stats['q_gap'] += 1
                gap = True
            else:
                self.q_next += 1
                self.control.append({'t': 'ack'})
                self.cond.notify_all()
        if gap:
            self._qnak()
            return
        self._dispatch(epoch, seq, op, msg)

    def _qnak(self):
        with self.cond:
            if self.epoch is None:
                return
            now = self.clock()
            if self.qnak_at[0] == self.q_next and now - self.qnak_at[1] < QNAK_REPEAT_S:
                return
            self.qnak_at = (self.q_next, now)
            self.stats['qnak'] += 1
            self.control.append({'t': 'qnak', 'from': self.q_next})
            self.cond.notify_all()

    def _hello(self, epoch):
        with self.cond:
            if epoch == self.epoch:
                self.stats['hello_repeat'] += 1
                if self.sent and self.sent[0][0] == 1:
                    self.rewind = 1
                    self.cond.notify_all()
                return
            old = list(self.subs.values())
            self.subs.clear()
            self.epoch, self.eseq, self.q_next = epoch, 0, 1
            self.sent.clear()
            self.sent_bytes = 0
            self.rewind, self.rewound, self.qnak_at = None, (None, 0.0), (None, 0.0)
            self.data.clear()
            self.backlog = 0
            self.stats['epochs'] += 1
            hello = {'t': 'hello', 'v': PROTOCOL_VERSION, 'r': self.instance, 'pid': os.getpid(),
                     'gateway': self.url, 'max_q': MAX_Q_LINE, 'heartbeat_s': self.heartbeat_s}
            self.data.append(_Item(epoch, hello, None, 200))
            self.backlog = 200
            self.cond.notify_all()
        for sub in old:
            sub.close()

    def _bye(self, epoch):
        with self.cond:
            if self.epoch is not None and epoch != self.epoch:
                self.stats['q_stale'] += 1
                return
        self.stop('BYE')

    def _acked(self, ea):
        """Under the lock: release the lines up to ea."""
        if not is_count(ea):
            return
        while self.sent and self.sent[0][0] <= ea:
            self.sent_bytes -= len(self.sent.popleft()[1])

    def _nak(self, start):
        """Under the lock: write the kept lines again from start, or tell the laptop to start a new epoch."""
        self.stats['nak_in'] += 1
        if not is_count(start) or start < 1 or start > self.eseq:
            return                              # start == eseq + 1: every written line arrived
        now = self.clock()
        if self.rewound[0] == start and now - self.rewound[1] < REWIND_GUARD_S:
            return                              # that retransmission is already on its way
        oldest = self.sent[0][0] if self.sent else self.eseq + 1
        if start < oldest:
            self.stats['resets'] += 1
            self.control.append({'t': 'reset', 'why': 'LINES_RELEASED', 'from': start, 'oldest': oldest})
        else:
            self.rewound = (start, now)
            self.rewind = start if self.rewind is None else min(self.rewind, start)
        self.cond.notify_all()

    # -------------------------------------------------------------------------------------------- requests
    def _dispatch(self, epoch, seq, op, msg):
        if op == 'req':
            self._start_request(epoch, seq, msg)
        elif op == 'sub':
            self._start_stream(epoch, seq, msg)
        else:
            with self.cond:
                sub = self.subs.get(msg.get('sub'))
            if sub is not None:
                sub.close()

    def respond(self, epoch, rid, status, text, ctype=JSON_TYPE):
        parts = body_parts(text)
        for k, part in enumerate(parts):
            msg = {'t': 'resp', 'id': rid, 'part': k, 'more': k + 1 < len(parts), 'body': part}
            if k == 0:
                msg['status'] = status
                msg['ctype'] = ctype
            if not self.put(epoch, msg):
                return

    def _start_request(self, epoch, rid, msg):
        method, path, body = msg.get('method'), msg.get('path'), msg.get('body')
        refusal = request_refusal(method, path, body)
        if refusal is not None:
            self.respond(epoch, rid, refusal[0], error_body(refusal[1], limit=MAX_BODY))
            return
        with self.cond:
            busy = self.requests >= REQUESTS_MAX
            if not busy:
                self.requests += 1
                self.stats['requests'] += 1
        if busy:
            self.respond(epoch, rid, 503, error_body('RELAY_BUSY', limit=REQUESTS_MAX))
            return
        threading.Thread(target=self._request, args=(epoch, rid, method, path, body), name='ef1-request',
                         daemon=True).start()

    def _request(self, epoch, rid, method, path, body):
        try:
            status, ctype, text = self._http(method, path, body)
        except Exception as e:                  # a thread must never print a traceback onto the console
            status, ctype, text = 502, JSON_TYPE, error_body('RELAY_INTERNAL', why=type(e).__name__)
        finally:
            with self.cond:
                self.requests -= 1
        self.respond(epoch, rid, status, text, ctype)

    def _connection(self, timeout):
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _http(self, method, path, body):
        conn = self._connection(REQUEST_TIMEOUT_S)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader('Host', '%s:%d' % (self.host, self.port))
            if method == 'POST':
                data = body.encode('utf-8')
                conn.putheader(OWNER_HEADER, '1')
                conn.putheader('Content-Type', 'application/json')
                conn.putheader('Content-Length', str(len(data)))
                conn.endheaders(data)
            else:
                conn.endheaders()
            resp = conn.getresponse()
            raw = resp.read(MAX_RESPONSE + 1)
            status, ctype = resp.status, resp.getheader('Content-Type') or ''
        except (OSError, http.client.HTTPException) as e:
            return 502, JSON_TYPE, error_body('RELAY_GATEWAY_UNREACHABLE', gateway=self.url, why=type(e).__name__)
        finally:
            conn.close()
        try:
            if len(raw) > MAX_RESPONSE:
                raise UnicodeError('too large')
            return status, ctype, raw.decode('utf-8')
        except UnicodeError:
            return 502, JSON_TYPE, error_body('RELAY_BAD_RESPONSE', limit=MAX_RESPONSE)

    # --------------------------------------------------------------------------------------------- streams
    def _start_stream(self, epoch, sid, msg):
        since, lei = msg.get('since'), msg.get('lei')
        with self.cond:
            busy = len(self.subs) >= SUBS_MAX
            if not busy:
                sub = self.subs[sid] = _Upstream(sid)
                self.stats['subs'] += 1
        if busy:
            self.put(epoch, {'t': 'end', 'sub': sid, 'code': 'RELAY_STREAMS_BUSY', 'status': 503, 'ctype': JSON_TYPE,
                             'body': error_body('RELAY_STREAMS_BUSY', limit=SUBS_MAX)})
            return
        threading.Thread(target=self._stream, args=(epoch, sub, since, lei), name='ef1-stream', daemon=True).start()

    def _stream(self, epoch, sub, since, lei):
        """One event stream: 'open' once the gateway answered 200, the events, then always one 'end'."""
        end = {'t': 'end', 'sub': sub.sid}
        conn = self._connection(STREAM_IDLE_S)
        try:
            end.update(self._follow(epoch, sub, since, lei, conn))
        except socket.timeout:
            end['code'] = 'UPSTREAM_TIMEOUT'
        except (OSError, http.client.HTTPException) as e:
            end.update(code='RELAY_GATEWAY_UNREACHABLE', status=502, ctype=JSON_TYPE,
                       body=error_body('RELAY_GATEWAY_UNREACHABLE', gateway=self.url, why=type(e).__name__))
        except Exception as e:                  # never a traceback on the console
            end.update(code='RELAY_INTERNAL', status=502, ctype=JSON_TYPE,
                       body=error_body('RELAY_INTERNAL', why=type(e).__name__))
        finally:
            unsubscribed = sub.closed
            sub.close()
            conn.close()
            with self.cond:
                if self.subs.get(sub.sid) is sub:
                    del self.subs[sub.sid]
        if unsubscribed:
            end = {'t': 'end', 'sub': sub.sid, 'code': 'UNSUBSCRIBED'}
        self.put(epoch, end)

    def _follow(self, epoch, sub, since, lei, conn):
        """The fields of the 'end' message after the stream stopped."""
        conn.connect()
        if not sub.attach(conn.sock):
            return {'code': 'UNSUBSCRIBED'}
        path = EVENTS_PATH if since is None else '%s?since=%d' % (EVENTS_PATH, since)
        conn.putrequest('GET', path, skip_host=True, skip_accept_encoding=True)
        conn.putheader('Host', '%s:%d' % (self.host, self.port))
        conn.putheader('Accept', 'text/event-stream')
        if lei is not None:
            conn.putheader('Last-Event-ID', str(lei))
        conn.endheaders()
        resp = conn.getresponse()
        if resp.status != 200:
            raw = resp.read(65536)
            return {'code': 'UPSTREAM_REFUSED', 'status': resp.status,
                    'ctype': resp.getheader('Content-Type') or JSON_TYPE, 'body': raw.decode('utf-8', 'replace')}
        if not self.put(epoch, {'t': 'open', 'sub': sub.sid, 'status': 200}):
            return {'code': 'EPOCH_CLOSED'}
        return {'code': self._pump(epoch, sub, resp)}

    def _pump(self, epoch, sub, resp):
        """Forward the Server-Sent Events of one stream; returns the end code."""
        event_id, data = None, []
        while True:
            raw = resp.readline(MAX_SSE_LINE + 1)
            if sub.closed:
                return 'UNSUBSCRIBED'
            if not raw:
                return 'UPSTREAM_CLOSED'
            if len(raw) > MAX_SSE_LINE:
                return 'UPSTREAM_LINE_TOO_LONG'
            line = raw.decode('utf-8', 'replace')
            line = line[:-1] if line.endswith('\n') else line
            line = line[:-1] if line.endswith('\r') else line
            if line == '':
                if data:
                    text = '\n'.join(data)
                    key = (sub.sid, 'progress') if _event_type(text) in COALESCE_TYPES else None
                    if not self.put(epoch, {'t': 'ev', 'sub': sub.sid, 'id': event_id, 'data': text}, key):
                        return 'RELAY_BACKLOG'
                event_id, data = None, []
                continue
            if line.startswith(':'):
                continue
            name, _, value = line.partition(':')
            value = value[1:] if value.startswith(' ') else value
            if name == 'data':
                data.append(value)
            elif name == 'id':
                event_id = value

    # ----------------------------------------------------------------------------------------------- driver
    def stop(self, reason):
        """Stop serving: queued numbered lines are dropped, and a last 'bye' {why} tells the laptop."""
        with self.cond:
            if self.stop_reason is None:
                self.stop_reason = reason
                self.control.append({'t': 'bye', 'why': reason})
            self.data.clear()
            self.backlog = 0
            subs = list(self.subs.values())
            self.done.set()
            self.cond.notify_all()
        for sub in subs:
            sub.close()

    def _read_loop(self, lines):
        try:
            for text in lines:
                if self.done.is_set():
                    break
                self.on_line(text)
        except (OSError, ValueError):
            pass
        self.stop('INPUT_CLOSED')

    def run(self, lines):
        """Serve until bye, the end of the input or the idle limit; returns the stop reason."""
        self._writer = threading.Thread(target=self._write_loop, name='ef1-writer', daemon=True)
        self._writer.start()
        threading.Thread(target=self._read_loop, args=(lines,), name='ef1-reader', daemon=True).start()
        self.heartbeat()
        beat = self.clock() + self.heartbeat_s
        while not self.done.wait(max(0.05, min(beat - self.clock(), 1.0))):
            now = self.clock()
            if now >= beat:
                self.heartbeat()
                beat = now + self.heartbeat_s
            if self.idle_exit_s and now - self.last_q > self.idle_exit_s:
                self.stop('IDLE')
        self._writer.join(3.0)                  # the queued control lines (for example 'bye') still go out
        return self.stop_reason


# ------------------------------------------------------------------------------------------------- console io
def fd_lines(fd):
    """The lines read from a file descriptor (raw os.read: no buffered object, whose lock a daemon thread would keep
    at interpreter shutdown), without the line feed; a line above 64 KiB is cut and the rest of it dropped."""
    buf, skipping = b'', False
    while True:
        chunk = os.read(fd, READ_CHUNK)
        if not chunk:
            if buf and not skipping:
                yield buf.decode('ascii', 'replace')
            return
        buf += chunk
        while True:
            i = buf.find(b'\n')
            if i < 0:
                break
            line, buf = buf[:i], buf[i + 1:]
            if skipping:
                skipping = False
                continue
            yield line.decode('ascii', 'replace')
        if len(buf) > OVERLONG:
            if not skipping:
                yield buf[:MAX_Q_LINE + 1].decode('ascii', 'replace')     # counted as too long
            buf, skipping = b'', True


def fd_writer(fd):
    lock = threading.Lock()

    def write(data):
        with lock:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
    return write


def quiet_tty(fd):
    """Switch off the echo of a terminal (the console keeps its line discipline); returns what to restore."""
    try:
        import termios
    except ImportError:
        return None
    try:
        if not os.isatty(fd):
            return None
        saved = termios.tcgetattr(fd)
        quiet = termios.tcgetattr(fd)
        quiet[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
        return saved
    except (OSError, termios.error):
        return None


def restore_tty(fd, saved):
    if saved is None:
        return
    try:
        import termios
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    except Exception:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description='EdgeFHE serial relay (board side of the owner-laptop bridge).')
    ap.add_argument('--gateway', default='http://127.0.0.1:8080',
                    help='the board gateway: http://127.0.0.1:PORT or http://localhost:PORT')
    ap.add_argument('--heartbeat-s', type=float, default=HEARTBEAT_S)
    ap.add_argument('--idle-exit-s', type=float, default=IDLE_EXIT_S)
    args = ap.parse_args(argv)
    try:
        gateway = parse_gateway(args.gateway)
    except ValueError:
        sys.stderr.write('SERIAL_RELAY_USAGE --gateway 必須是 http://127.0.0.1:PORT 或 http://localhost:PORT；'
                         '序列中繼程式只連線至本機閘道。\n')
        return EXIT_USAGE
    if not 0.1 <= args.heartbeat_s <= 10 or not (args.idle_exit_s == 0 or 5 <= args.idle_exit_s <= 86400):
        sys.stderr.write('SERIAL_RELAY_USAGE --heartbeat-s 須介於 0.1 至 10 秒；--idle-exit-s 須為 0 或 5 至 86400 秒。\n')
        return EXIT_USAGE
    saved = quiet_tty(0)
    try:
        if os.isatty(1):
            os.write(2, (BANNER % ('http://%s:%d' % gateway)).encode('utf-8'))
        relay = Relay(gateway, fd_writer(1), args.heartbeat_s, args.idle_exit_s)
        try:
            relay.run(fd_lines(0))
        except KeyboardInterrupt:
            relay.stop('INTERRUPTED')
    finally:
        restore_tty(0, saved)
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
