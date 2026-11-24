"""EdgeFHE owner-laptop bridge: the owner screen on 127.0.0.1:8780, with /api/v1 relayed over the KV260 serial console.

usage (owner laptop, repository root; KV_PW holds the board console password, set only in this terminal session):
  python -B demo/owner/owner_bridge.py --i-am-the-owner-laptop --transport serial:COM6 [--listen 127.0.0.1:8780]
         [--ui-dir demo/ui] [--user ubuntu] [--relay /opt/edgefhe-demo/owner/serial_relay.py] [--python python3]
         [--gateway http://127.0.0.1:8080]
  python -B demo/owner/owner_bridge.py --i-am-the-owner-laptop
         --transport 'cmd:["python","-B","demo/owner/serial_relay.py"]' ...
         (the relay as a local child process on stdin/stdout: tests and rehearsals; a JSON array or a POSIX-style
         command line)

Rule: this program runs on the owner laptop only, never on the cloud PC. The owner screen shows the answers, the
decrypted score and the risk level, and none of them may appear on the cloud PC, even though the cloud PC's console
cable (COM6) is attached to the board during the setup steps. The program therefore starts only with the explicit
acknowledgment --i-am-the-owner-laptop (otherwise OWNER_BRIDGE_USAGE code=OWNER_LAPTOP), and it refuses
(OWNER_BRIDGE_REFUSED code=CLOUD_PC signs=...) when this machine shows any sign of being the cloud PC: the PC evidence
console answering on 127.0.0.1:8765 (Server: EdgeFHE-Console; a HEAD request with a foreign Host, which the console
refuses without doing anything), a running Docker container named efhe-cloud or made from an edgefhe-demo-cloud
image (docker ps; skipped when there is no docker command or engine), or the cloud PC's folders C:\\EdgeFHE_evidence
or C:\\EdgeFHE_demo. Every check runs before the listening socket, the serial port or the relay is opened. The cloud
server port 18443 is never probed: a probe would leave a connection in the evidence server.log.

Fallback for a KV260 without a usable desktop: the owner laptop is connected to the board only by the micro-USB
serial console, never through the cloud PC or the network. This program serves the same owner screen (demo/ui) and
the same /api/v1 paths as the board gateway, and carries every API request and every event stream over the console:
serial:COMn logs in with energy_probe/kv260/kv_console.py (the committed version with secret masking and HISTFILE
unset; the password comes only from the environment variable KV_PW) and starts demo/owner/serial_relay.py on the board
as the console user, without sudo, by typing
  exec python3 -I -B /opt/edgefhe-demo/owner/serial_relay.py --gateway http://127.0.0.1:8080
A relay that is already running on the console (for example after this program was restarted) is detected by its
heartbeat and reused without a login. The line protocol EF1 is documented in serial_relay.py: CRC-checked lines,
go-back-N retransmission in both directions, other console lines (kernel messages, prompts) ignored and counted.
Once per session the program suggests 'sudo dmesg -n 1' to the operator; it never runs it. It never sends a control
character to the board: Ctrl+C stops only this program, and the encryption client stays under the gateway.

HTTP (127.0.0.1 only; the same rules as demo/edge/efgw/httpapi.py, with this program's port): Host must be
127.0.0.1:PORT or localhost:PORT, an Origin must be absent or same-origin, a POST needs X-EdgeFHE-Owner: 1,
Content-Type application/json and at most 4 KiB; every response carries the gateway's CSP, nosniff and no-store.
The relay adds the owner header for the gateway and never forwards the browser's Origin. GET /bridge/v1/status shows
the link counters (no owner data). Markers: OWNER_BRIDGE_READY http://127.0.0.1:PORT/, OWNER_BRIDGE_LINK_UP,
OWNER_BRIDGE_LINK_DOWN, OWNER_BRIDGE_GATEWAY, OWNER_BRIDGE_STOPPED, OWNER_BRIDGE_FAILED code=, OWNER_BRIDGE_USAGE,
OWNER_BRIDGE_REFUSED code=CLOUD_PC. Exit status: 0 after Ctrl+C, 1 the link or the listening socket failed, 2 usage
or the cloud-PC refusal. Python 3.10 standard library; pyserial only for serial:COMn (through kv_console).
"""
import argparse
import collections
import http.server
import inspect
import json
import os
import queue
import re
import secrets
import select
import shlex
import shutil
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
import urllib.parse

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.dirname(HERE)
ROOT = os.path.dirname(DEMO)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import serial_relay as ef1  # noqa: E402

KV_CONSOLE_DIR = os.path.join(ROOT, 'energy_probe', 'kv260')
DEFAULT_UI = os.path.join(DEMO, 'ui')
DEFAULT_RELAY = '/opt/edgefhe-demo/owner/serial_relay.py'
DEFAULT_GATEWAY = 'http://127.0.0.1:8080'
DEFAULT_PORT = 8780
API = '/api/v1'
BRIDGE_STATUS = '/bridge/v1/status'
OWNER_HEADER = 'X-EdgeFHE-Owner'
JSON_TYPE = 'application/json; charset=utf-8'
MAX_BODY = 4096
DRAIN_LIMIT = 65536
HEARTBEAT_S = 2.0
RETRY_MS = 2000
STREAMS_MAX = 4
SUB_QUEUE_MAX = 20000
SUB_OPEN_TIMEOUT_S = 20.0
REQUEST_TIMEOUT_S = 40.0
MAX_RESPONSE = ef1.MAX_RESPONSE
MAX_E_LINE = 16 * 1024 * 1024
STATIC_MAX_BYTES = 8 * 1024 * 1024
STATIC_DEPTH = 4
LINK_START_S = 30.0
OWNER_LAPTOP_FLAG = '--i-am-the-owner-laptop'
CLOUD_CONSOLE = ('127.0.0.1', 8765)         # demo/console/evidence_console.py (Server: EdgeFHE-Console)
CLOUD_CONSOLE_SERVER = 'edgefhe-console'
CLOUD_CONTAINER = 'efhe-cloud'              # the evidence console's docker run --name
CLOUD_IMAGE = 'edgefhe-demo-cloud'          # demo/cloud/Dockerfile
CLOUD_DIRS = ('C:\\EdgeFHE_evidence', 'C:\\EdgeFHE_demo')    # evidence root, PKI and image backup (README section 6)
CLOUD_PROBE_S = 1.5
DOCKER_PS_S = 8.0
CSP ="default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'"
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
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2

MESSAGES = {
    'HOST_FORBIDDEN': '本橋接程式只接受 Host 標頭為 127.0.0.1:%(port)s 或 localhost:%(port)s 的請求。',
    'CLIENT_FORBIDDEN': '本橋接程式只接受來自本機迴路介面（127.0.0.1）的連線。',
    'ORIGIN_FORBIDDEN': ('本橋接程式只接受來自擁有者畫面本身（http://127.0.0.1:%(port)s 或 http://localhost:%(port)s）'
                         '的請求。'),
    'OWNER_HEADER_REQUIRED': '送出表單的請求必須帶有 X-EdgeFHE-Owner: 1 標頭。',
    'CONTENT_TYPE_JSON': '送出表單的請求必須使用 Content-Type: application/json（UTF-8）。',
    'BODY_LENGTH_REQUIRED': '送出表單的請求必須以 Content-Length 標明內容長度，且不得使用分段傳輸。',
    'BODY_TOO_LARGE': '送出表單的請求內容不得超過 %(limit)s 位元組。',
    'BODY_INCOMPLETE': '本橋接程式未收到完整的請求內容。',
    'BODY_JSON': '請求內容不是有效的 UTF-8 文字。',
    'QUERY_INVALID': 'since 參數與 Last-Event-ID 標頭必須是非負整數，且各自最多出現一次。',
    'NOT_FOUND': '本橋接程式找不到此路徑。',
    'METHOD_NOT_ALLOWED': '此路徑不支援 %(method)s 方法。',
    'BRIDGE_STREAMS_BUSY': ('本橋接程式同時開啟的事件串流已達上限（%(limit)s 條）；操作人員應先關閉其他擁有者畫面，'
                            '再重新整理此畫面。'),
    'BRIDGE_LINK_DOWN': ('本橋接程式目前未與 KV260 上的序列中繼程式連線，正在重新建立連線；操作人員應待連線恢復後'
                         '再操作。'),
    'BRIDGE_LINK_LOST': '此請求傳送期間序列連線中斷，本橋接程式未收到回應；操作人員應待連線恢復後再操作。',
    'BRIDGE_TIMEOUT': 'KV260 上的閘道未在 %(limit)s 秒內回應此請求。',
    'BRIDGE_SUBMIT_UNCERTAIN': ('此筆表單送出期間序列連線中斷或逾時，本橋接程式無法確認閘道是否已收到；'
                                '操作人員應先查看畫面上的執行紀錄，不應立即重新送出。'),
    'BRIDGE_LINE_TOO_LONG': '此請求編碼後超過序列協定每行 %(limit)s 字元的上限；本橋接程式未送出此請求。',
    'BRIDGE_RESPONSE_TOO_LARGE': '閘道的回應超過 %(limit)s 位元組；本橋接程式未轉送此回應。',
    'BRIDGE_PROTOCOL': '序列中繼程式的回應不符合協定；本橋接程式未轉送此回應。',
    'BRIDGE_STOPPING': '本橋接程式正在停止；操作人員應待本程式結束後再重新啟動。',
    'INTERNAL': '本橋接程式處理此請求時發生內部錯誤（%(why)s）。',
}
OPERATOR = {
    'KV_CONSOLE_MISSING': '本橋接程式無法載入 kv_console.py（%s）：%s。',
    'KV_CONSOLE_OUTDATED': ('本橋接程式需要已提交、具密碼遮蔽功能的 kv_console.py（缺少：%s）；'
                            '操作人員應先將 energy_probe/kv260/kv_console.py 更新為 repository 中已提交的版本。'),
    'KV_PW_MISSING': ('本橋接程式需要以環境變數 KV_PW 提供 KV260 主控台使用者的密碼；操作人員應只在目前的終端機工作階段'
                      '設定 KV_PW。本程式不接受以參數傳入密碼，也不會儲存密碼。'),
    'PORT_OPEN': '本橋接程式無法開啟序列埠 %s（%s）；操作人員應確認傳輸線已連接，且沒有其他程式占用此序列埠。',
    'LOGIN_FAILED': '本橋接程式無法登入 KV260 主控台（%s）。',
    'RELAY_MISSING': ('KV260 上找不到序列中繼程式 %s 或直譯器 %s；操作人員應先依執行手冊將 demo/owner 複製到 '
                      '/opt/edgefhe-demo/。'),
    'CONSOLE_FAILED': '本橋接程式在 KV260 主控台上執行檢查時失敗（%s）。',
    'CMD_START': '本橋接程式無法啟動中繼指令 %s（%s）。',
    'DMESG_HINT': ('建議：KV260 的核心訊息會出現在序列主控台上，可能與中繼資料交錯；本程式會丟棄並重送受影響的行。'
                   '若要減少干擾，操作人員可在啟動本程式之前，於 KV260 上自行執行 sudo dmesg -n 1；本程式不會代為執行此指令。'),
    'RELAY_ATTACHED': '本橋接程式偵測到 KV260 上已有序列中繼程式在執行，將直接沿用（不重新登入）。',
    'RELAY_STARTED': '本橋接程式已登入 KV260 主控台，並以主控台使用者身分啟動序列中繼程式 %s。',
    'CMD_STARTED': '本橋接程式已啟動中繼指令（%s）。',
    'LINK_UP': '本橋接程式已與 KV260 上的序列中繼程式建立連線。',
    'LINK_DOWN': '本橋接程式與序列中繼程式的連線中斷（%s）；本程式將每隔數秒嘗試重新連線。',
    'READY': ('本橋接程式已在 http://127.0.0.1:%d/ 提供擁有者畫面；操作人員應以本機瀏覽器開啟。此畫面只在本機迴路介面上'
              '提供，資料只經由序列主控台往返 KV260。本程式只可在擁有者筆電上執行，絕不可在雲端電腦上執行。'
              '本程式不會傳送中斷字元（Ctrl+C）至 KV260；按 Ctrl+C 只結束本程式。'),
    'GATEWAY_OK': 'KV260 上的閘道已回應（HTTP %d）。',
    'GATEWAY_FAIL': 'KV260 上的閘道目前沒有回應（%s）；操作人員應確認 edgefhe-demo 服務正在執行。擁有者畫面會自動重新連線。',
    'STOPPED': '本橋接程式已停止，並已通知 KV260 上的序列中繼程式結束。',
    'USAGE_OWNER_LAPTOP': ('本橋接程式只在擁有者筆電上執行，絕不在雲端電腦上執行，因為擁有者畫面顯示的答案、解密後的分數與'
                           '風險等級不得出現在雲端電腦上。操作人員確認本機是擁有者筆電之後，應加上 '
                           '--i-am-the-owner-laptop 重新啟動本程式。'),
    'CLOUD_PC': ('本機有雲端電腦的跡象（%s）；本橋接程式拒絕執行，因為擁有者畫面顯示的答案、解密後的分數與風險等級不得'
                 '出現在雲端電腦上。操作人員應改在擁有者筆電上執行本程式，並將 KV260 的主控台線改接到該筆電。'
                 '本程式未開啟任何序列埠或監聽埠。'),
    'SIGN_CONSOLE': '證據主控台正在 %s:%d 上執行',
    'SIGN_CONTAINER': 'Docker 容器 %s（映像 %s）正在執行',
    'SIGN_DIR': '雲端電腦的資料夾 %s 存在',
    'UI_DIR': '擁有者畫面的目錄 %s 不存在；本程式仍轉送 /api/v1，但畫面檔案一律回應 404。',
    'LISTEN_BIND': '本橋接程式無法在 %s 上監聽（%s）。',
    'USAGE_LISTEN': '--listen 必須是 127.0.0.1:PORT；擁有者畫面只在本機迴路介面上提供。',
    'USAGE_TRANSPORT': '--transport 必須是 serial:COMn 或 cmd:<指令>（指令可寫成 JSON 陣列）。',
    'USAGE_GATEWAY': '--gateway 必須是 http://127.0.0.1:PORT 或 http://localhost:PORT。',
    'LINK_TIMEOUT': '本橋接程式在 %d 秒內未能與序列中繼程式建立連線。',
}
_print_lock = threading.Lock()


def _write(stream, text):
    with _print_lock:
        try:
            stream.write(text + '\n')
            stream.flush()
        except (OSError, ValueError):
            pass


def out(line):
    _write(sys.stdout, line)


def say(text):
    _write(sys.stderr, text)


def _utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


def _text(table, code, values):
    return table[code] % collections.defaultdict(lambda: '?', values)


# -------------------------------------------------------------------------------------------------- transports
class TransportError(Exception):
    """The transport cannot be opened or stopped working; str() is a zh-TW text for the operator."""


class TransportClosed(TransportError):
    pass


def load_kv_console(path=KV_CONSOLE_DIR):
    """energy_probe/kv260/kv_console.py, refused unless it is the committed version with secret masking."""
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        import kv_console
    except ImportError as e:
        why = 'pyserial：pip install pyserial' if getattr(e, 'name', None) == 'serial' else str(e)
        raise TransportError(OPERATOR['KV_CONSOLE_MISSING'] % (path, why))
    problems = kv_console_problems(kv_console)
    if problems:
        raise TransportError(OPERATOR['KV_CONSOLE_OUTDATED'] % ', '.join(problems))
    return kv_console


def kv_console_problems(module):
    found = []
    try:
        if 'secret' not in inspect.signature(module.KV.run).parameters:
            found.append('KV.run(secret=)')
        if 'secret' not in inspect.signature(module.stream).parameters:
            found.append('stream(secret=)')
        if 'unset HISTFILE' not in inspect.getsource(module.KV.login):
            found.append('KV.login: unset HISTFILE')
        if not callable(getattr(module, 'put_file', None)):
            found.append('put_file')
    except (AttributeError, TypeError, OSError, ValueError):
        found.append('kv_console API')
    return found


class CmdTransport:
    """The relay as a local child process on stdin/stdout (tests, rehearsals, or a wrapper such as ssh)."""

    kind = 'cmd'

    def __init__(self, argv, stderr=None):
        self.argv = list(argv)
        self.stderr = stderr
        self.proc = None
        self._chunks = None

    def describe(self):
        return 'cmd:' + ' '.join(self.argv[:1])

    def open(self):
        self.close()
        try:
            self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                                         bufsize=0)
        except OSError as e:
            raise TransportError(OPERATOR['CMD_START'] % (self.argv[:1], type(e).__name__))
        self._chunks = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc, self._chunks), name='ef1-cmd-read', daemon=True).start()
        say(OPERATOR['CMD_STARTED'] % os.path.basename(self.argv[0]))
        return 'started'

    @staticmethod
    def _pump(proc, chunks):
        fd = proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except (OSError, ValueError):
                data = b''
            chunks.put(data)
            if not data:
                return

    def read(self):
        chunks = self._chunks
        if chunks is None:
            raise TransportClosed('closed')
        try:
            data = chunks.get(timeout=0.25)
        except queue.Empty:
            return b''
        if not data:
            chunks.put(b'')
            raise TransportClosed('EOF')
        return data

    def write(self, data):
        proc = self.proc
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (AttributeError, OSError, ValueError):
            raise TransportClosed('write')

    def close(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()              # end of input: the relay stops by itself
        except OSError:
            pass
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.terminate()                # the local relay only, never the board's encryption client
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        try:
            proc.stdout.close()
        except OSError:
            pass


class SerialTransport:
    """The KV260 serial console through kv_console.KV: reuse a running relay, or log in and start one."""

    kind = 'serial'

    def __init__(self, port, user='ubuntu', relay=DEFAULT_RELAY, python='python3', gateway=DEFAULT_GATEWAY,
                 kv_factory=None, probe_s=4.5, env=None):
        self.port = port
        self.user = user
        self.relay = relay
        self.python = python
        self.gateway = gateway
        self.kv_factory = kv_factory
        self.probe_s = probe_s
        self.env = os.environ if env is None else env
        self.kv = None
        self.logged_in = False
        self.relay_ran = False

    def describe(self):
        return 'serial:%s' % self.port

    def open(self):
        self.close()
        kvc = load_kv_console()
        factory = self.kv_factory or kvc.KV
        try:
            self.kv = factory(self.port)
        except Exception as e:              # serial.SerialException and its relatives
            raise TransportError(OPERATOR['PORT_OPEN'] % (self.port, type(e).__name__))
        if self._relay_running():
            self.relay_ran = True
            say(OPERATOR['RELAY_ATTACHED'])
            return 'attached'
        password = self.env.get('KV_PW')
        if password is None:
            raise TransportError(OPERATOR['KV_PW_MISSING'])
        try:
            self.kv.login(self.user, password)      # kv_console never shows the password (no echo at the prompt)
        except Exception as e:
            raise TransportError(OPERATOR['LOGIN_FAILED'] % (str(e)[:200].replace(password, '***') if password
                                                             else type(e).__name__))
        self.logged_in = True
        check = 'test -r %s && command -v %s >/dev/null && echo __EF_RELAY_OK__' % (
            shlex.quote(self.relay), shlex.quote(self.python))
        try:
            rc, text = self.kv.run(check, 15)
        except Exception as e:
            raise TransportError(OPERATOR['CONSOLE_FAILED'] % type(e).__name__)
        if '__EF_RELAY_OK__' not in text:
            raise TransportError(OPERATOR['RELAY_MISSING'] % (self.relay, self.python))
        line = 'exec %s -I -B %s --gateway %s' % (shlex.quote(self.python), shlex.quote(self.relay),
                                                 shlex.quote(self.gateway))
        self.write((line + '\r\n').encode('ascii'))
        self.relay_ran = True
        say(OPERATOR['RELAY_STARTED'] % self.relay)
        return 'started'

    def _relay_running(self):
        """True when an EF1 line with a valid CRC arrives within probe_s (a relay heartbeats every 2 s); nothing is
        written meanwhile, so a shell or login prompt never receives a stray line."""
        deadline = time.monotonic() + self.probe_s
        buf = b''
        while time.monotonic() < deadline:
            try:
                buf += self.kv.s.read(4096)
            except Exception as e:
                raise TransportError(OPERATOR['PORT_OPEN'] % (self.port, type(e).__name__))
            lines = buf.split(b'\n')
            buf = lines.pop()
            for raw in lines:
                text = raw.decode('ascii', 'replace').strip('\r')
                i = text.find(ef1.E_PREFIX)
                if i >= 0:
                    try:
                        ef1.decode_e(text[i:])
                        return True
                    except ef1.LineError:
                        pass
            if len(buf) > MAX_E_LINE:
                buf = b''
        return False

    def read(self):
        kv = self.kv
        if kv is None:
            raise TransportClosed('closed')
        try:
            return kv.s.read(4096)
        except Exception as e:
            raise TransportClosed(type(e).__name__)

    def write(self, data):
        kv = self.kv
        if kv is None:
            raise TransportClosed('closed')
        try:
            kv.s.write(data)
        except Exception as e:
            raise TransportClosed(type(e).__name__)

    def close(self):
        kv, self.kv = self.kv, None
        if kv is None:
            return
        try:
            if self.logged_in and not self.relay_ran:
                kv.logout()                 # 'stty echo; exit' at the shell that this program opened
            else:
                kv.s.close()                # the relay's exec ended the shell: a login prompt follows its exit
        except Exception:
            pass
        self.logged_in = self.relay_ran = False


def parse_transport(spec, user, relay, python, gateway):
    if spec.startswith('serial:') and len(spec) > len('serial:'):
        return SerialTransport(spec[len('serial:'):], user=user, relay=relay, python=python, gateway=gateway)
    if spec.startswith('cmd:'):
        text = spec[len('cmd:'):].strip()
        if text.startswith('['):
            argv = json.loads(text)
        else:
            argv = shlex.split(text, posix=True)
        if not argv or not all(isinstance(a, str) and a for a in argv):
            raise ValueError(spec)
        return CmdTransport(argv)
    raise ValueError(spec)


# -------------------------------------------------------------------------------------------------------- link
class LinkError(Exception):
    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


class _Pending:
    def __init__(self):
        self.done = threading.Event()
        self.parts = []
        self.size = 0
        self.status = None
        self.ctype = None
        self.error = None

    def add(self, msg):
        """One 'resp' part in order; True once the response is complete (or failed)."""
        if msg.get('part') != len(self.parts):
            self.fail('PROTOCOL')
            return True
        if msg.get('part') == 0:
            self.status, self.ctype = msg.get('status'), msg.get('ctype')
        body = msg.get('body')
        body = body if isinstance(body, str) else ''
        self.parts.append(body)
        self.size += len(body)
        if self.size > MAX_RESPONSE:
            self.fail('RESPONSE_TOO_LARGE')
            return True
        if not msg.get('more'):
            self.done.set()
            return True
        return False

    def fail(self, code):
        if not self.done.is_set():
            self.error = code
            self.done.set()


class Subscription:
    """One relayed event stream: items ('open', status), ('ev', id, data) and finally ('end', status, ctype, body,
    code)."""

    def __init__(self, link):
        self.link = link
        self.sid = None
        self._cond = threading.Condition()
        self._items = collections.deque()
        self._ended = False

    def deliver(self, item):
        with self._cond:
            if self._ended:
                return
            if item[0] != 'end' and len(self._items) >= SUB_QUEUE_MAX:
                item = ('end', None, None, None, 'BRIDGE_BACKLOG')     # the browser stopped reading
            if item[0] == 'end':
                self._ended = True
            self._items.append(item)
            self._cond.notify_all()

    def get(self, timeout):
        with self._cond:
            if not self._items:
                self._cond.wait(timeout)
            return self._items.popleft() if self._items else None

    def close(self):
        self.link.unsubscribe(self)


class Link:
    """The laptop side of EF1 over one transport: epochs, ordered delivery, retransmission and re-attachment."""

    hello_repeat_s = 1.0
    ack_every_s = 2.0
    nak_repeat_s = 2.0
    q_resend_s = 3.0
    alive_s = 6.0           # a relay line within this window: a hello may be typed
    dead_s = 7.0            # no relay line for this long: the transport is reopened
    silent_start_s = 15.0   # after (re)opening, the relay's first line must come within this time
    retry_s = 3.0

    def __init__(self, transport, clock=time.monotonic):
        self.transport = transport
        self.clock = clock
        self.lock = threading.Condition()
        self.wlock = threading.Lock()
        self.stopping = False   # no new request; the reader still runs until the relay's 'bye'
        self.closing = False    # the link threads end
        self.attached = False
        self.attached_at = 0.0
        self.dead = None
        self.epoch = None
        self.linked = False
        self.e_next = 1
        self.q_seq = 0
        self.q_unacked = collections.OrderedDict()
        self.qnak_seen = (None, 0.0)
        self.nak_from = None
        self.nak_at = 0.0
        self.hello_at = 0.0
        self.ack_at = 0.0
        self.last_rx = None
        self.relay = {}
        self.relay_stats = {}
        self.relay_errors = collections.Counter()
        self.pending = {}
        self.subs = {}
        self.stats = collections.Counter()
        self.byes = threading.Event()
        self._opened_first = False
        self._was_up = False

    # ------------------------------------------------------------------------------------------- lifecycle
    def start(self):
        """Open the transport (raises TransportError) and start the link threads."""
        self.transport.open()
        self._opened_first = True
        threading.Thread(target=self._supervise, name='ef1-link', daemon=True).start()
        threading.Thread(target=self._tick_loop, name='ef1-timer', daemon=True).start()

    def wait_linked(self, timeout):
        deadline = self.clock() + timeout
        with self.lock:
            while not self.linked:
                left = deadline - self.clock()
                if left <= 0 or self.closing:
                    return False
                self.lock.wait(min(left, 0.5))
            return True

    def close(self):
        """Tell the relay to stop ('bye', answered by its own 'bye'), then end the link threads and the transport."""
        with self.lock:
            self.stopping = True
            epoch, attached = self.epoch, self.attached and self.dead is None
            if attached and epoch is not None:
                bye = self._q0({'op': 'bye'}, epoch)
        if attached and epoch is not None:
            # A second 'bye' only when the first went unanswered: after the relay's own 'bye' the console is back at
            # a shell or login prompt, and nothing more may be typed there.
            self.byes.clear()
            for _ in range(2):
                self._send([bye], force=True)
                if self.byes.wait(1.5):
                    break
        with self.lock:
            self.closing = True
            self.lock.notify_all()
        self._fail_all('BRIDGE_STOPPING')
        try:
            self.transport.close()
        except Exception:
            pass

    def _supervise(self):
        first, last_error = True, None
        while not self.closing:
            if not (first and self._opened_first):
                try:
                    self.transport.open()
                except TransportError as e:
                    if str(e) != last_error:            # a cable left out: one message, not one every few seconds
                        last_error = str(e)
                        say(last_error)
                    self._sleep(self.retry_s)
                    continue
            first, last_error = False, None
            with self.lock:
                self.attached, self.attached_at, self.dead, self.last_rx = True, self.clock(), None, None
            self._new_epoch()
            reason = self._read_loop()
            with self.lock:
                self.attached = False
                self.linked = False
                was_up, self._was_up = self._was_up, False
                self.lock.notify_all()
            self._fail_all('LINK_LOST')
            if self.closing:
                return
            self.stats['link_down'] += 1
            if was_up:
                out('OWNER_BRIDGE_LINK_DOWN reason=%s' % reason)
                say(OPERATOR['LINK_DOWN'] % reason)
            try:
                self.transport.close()
            except Exception:
                pass
            self._sleep(self.retry_s)

    def _sleep(self, seconds):
        with self.lock:
            if not self.closing:
                self.lock.wait(seconds)

    def _read_loop(self):
        buf = b''
        while True:
            with self.lock:
                if self.closing:
                    return 'CLOSING'
                if self.dead is not None:
                    return self.dead
            try:
                data = self.transport.read()
            except TransportError:
                return 'TRANSPORT_CLOSED'
            if not data:
                continue
            buf += data
            while True:
                i = buf.find(b'\n')
                if i < 0:
                    break
                line, buf = buf[:i], buf[i + 1:]
                self._on_line(line.decode('ascii', 'replace'))
            if len(buf) > MAX_E_LINE:
                buf = b''
                self.stats['overlong'] += 1

    # -------------------------------------------------------------------------------------------- sending
    def _q0(self, msg, epoch=None):
        """An unnumbered Q line (under the lock, or with an explicit epoch)."""
        full = dict(msg)
        full['b'] = self.epoch if epoch is None else epoch
        full['ea'] = self.e_next - 1
        return ef1.encode_q(0, full)

    def _send(self, lines, force=False):
        """Write Q lines. Once the bridge stops or the relay is gone, only a forced line (the 'bye') is written: a
        stray line would otherwise be typed into the shell or the login prompt that follows the relay."""
        if not lines:
            return
        with self.lock:
            if not force and (self.stopping or self.closing or self.dead is not None):
                return
        with self.wlock:
            for line in lines:
                try:
                    self.transport.write((line + '\n').encode('ascii'))
                except TransportError:
                    with self.lock:
                        if self.dead is None:
                            self.dead = 'TRANSPORT_CLOSED'
                    return
                self.stats['q_lines'] += 1

    def _send_numbered(self, msg, register):
        with self.lock:
            if self.stopping or self.closing:
                raise LinkError('BRIDGE_STOPPING')
            if not self.linked:
                raise LinkError('LINK_DOWN')
            seq = self.q_seq + 1
            full = dict(msg)
            full['b'], full['ea'] = self.epoch, self.e_next - 1
            line = ef1.encode_q(seq, full)
            if len(line) > ef1.MAX_Q_LINE:
                self.stats['q_too_long'] += 1
                raise LinkError('LINE_TOO_LONG')
            self.q_seq = seq
            self.q_unacked[seq] = [line, self.clock()]
            register(seq)
        self._send([line])
        return seq

    def _q_resend(self, start, now):
        """Under the lock: the unacknowledged Q lines from start (all when None), marked as sent now."""
        lines = []
        for seq, entry in self.q_unacked.items():
            if start is None or seq >= start:
                entry[1] = now
                lines.append(entry[0])
        self.stats['q_resent'] += len(lines)
        return lines

    def _want_resend(self):
        """Under the lock: a nak for the next expected E line, unless one is already pending."""
        if self.epoch is None:
            return []
        now = self.clock()
        if self.nak_from == self.e_next and now - self.nak_at < self.nak_repeat_s:
            return []
        self.nak_from, self.nak_at = self.e_next, now
        self.stats['nak'] += 1
        return [self._q0({'op': 'nak', 'from': self.e_next})]

    # ------------------------------------------------------------------------------------------- receiving
    def _on_line(self, text):
        i = text.find(ef1.E_PREFIX)
        if i < 0:
            if text.strip():
                self.stats['ignored'] += 1
            return
        if i > 0:
            self.stats['ignored'] += 1          # console text in front of the line (a kernel message, a prompt)
        lines, deliver, new_epoch = [], None, None
        try:
            seq, msg = ef1.decode_e(text[i:].rstrip('\r'))
        except ef1.LineError as e:
            with self.lock:
                self.stats['crc_bad' if e.code == 'CRC' else 'bad_lines'] += 1
                lines = self._want_resend()
            self._send(lines)
            return
        now = self.clock()
        with self.lock:
            self.last_rx = now
            kind, epoch = msg.get('t'), msg.get('b')
            if epoch == self.epoch and ef1.is_count(msg.get('qa')):
                for q in [q for q in self.q_unacked if q <= msg['qa']]:
                    del self.q_unacked[q]
            if seq == 0:
                lines, new_epoch = self._control(kind, epoch, msg, now)
            elif epoch != self.epoch:
                self.stats['stale'] += 1
            elif seq < self.e_next:
                self.stats['dup'] += 1
            elif seq > self.e_next:
                self.stats['gap'] += 1
                lines = self._want_resend()
            else:
                self.e_next += 1
                if self.nak_from is not None and self.e_next > self.nak_from:
                    self.nak_from = None
                    self.stats['resynced'] += 1
                deliver = msg
                if kind == 'hello':
                    if seq != 1:
                        new_epoch = 'PROTOCOL'
                    else:
                        self.linked = True
                        self._was_up = True
                        self.relay = dict((k, msg.get(k)) for k in ('v', 'r', 'pid', 'gateway', 'max_q'))
                        self.ack_at = now
                        self.lock.notify_all()
                elif seq == 1:
                    new_epoch = 'PROTOCOL'
        self._send(lines)
        if new_epoch is not None:
            self.stats['resets'] += 1
            self._new_epoch()
            return
        if deliver is not None:
            if deliver.get('t') == 'hello':
                out('OWNER_BRIDGE_LINK_UP relay=%s pid=%s transport=%s' % (
                    deliver.get('r'), deliver.get('pid'), self.transport.describe()))
                say(OPERATOR['LINK_UP'])
            else:
                self._deliver(deliver)

    def _control(self, kind, epoch, msg, now):
        """Under the lock: an unnumbered E message; returns (Q lines to send, reason for a new epoch or None)."""
        if kind == 'hb':
            self.relay_stats = msg.get('stats') if isinstance(msg.get('stats'), dict) else {}
            if self.linked and epoch != self.epoch:
                return [], 'RELAY_LOST_EPOCH'           # the relay restarted, or another epoch took over
            if self.linked and ef1.is_count(msg.get('eseq')):
                if msg['eseq'] >= self.e_next:
                    return self._want_resend(), None   # a lost last line: nothing else would reveal it
                if self.nak_from is not None and msg['eseq'] == self.e_next - 1:
                    self.nak_from = None                # the bad line was a control line: nothing is missing
            return [], None
        if epoch != self.epoch:
            return [], None
        if kind == 'qnak' and ef1.is_count(msg.get('from')):
            start = msg['from']
            if self.qnak_seen[0] == start and now - self.qnak_seen[1] < 0.5:
                return [], None
            self.qnak_seen = (start, now)
            self.stats['qnak'] += 1
            return self._q_resend(start, now), None
        if kind == 'reset':
            return [], 'RELAY_RESET'
        if kind == 'err':
            self.relay_errors[str(msg.get('code'))] += 1
        elif kind == 'bye':
            self.byes.set()
            if not (self.stopping or self.closing) and self.dead is None:
                self.dead = 'RELAY_EXIT_%s' % msg.get('why')
        return [], None

    def _deliver(self, msg):
        kind = msg.get('t')
        if kind == 'resp':
            with self.lock:
                pending = self.pending.get(msg.get('id'))
            if pending is None:
                self.stats['orphans'] += 1
            elif pending.add(msg):
                with self.lock:
                    self.pending.pop(msg.get('id'), None)
            return
        if kind in ('open', 'ev', 'end'):
            with self.lock:
                sub = self.subs.get(msg.get('sub'))
                if kind == 'end' and sub is not None:
                    del self.subs[msg.get('sub')]
            if sub is None:
                self.stats['orphans'] += 1
            elif kind == 'open':
                sub.deliver(('open', msg.get('status')))
            elif kind == 'ev':
                data = msg.get('data')
                sub.deliver(('ev', msg.get('id'), data if isinstance(data, str) else ''))
            else:
                sub.deliver(('end', msg.get('status'), msg.get('ctype'), msg.get('body'), msg.get('code')))
            return
        self.stats['unknown'] += 1

    def _new_epoch(self):
        with self.lock:
            self.epoch = secrets.token_hex(8)
            self.linked = False
            self.e_next, self.q_seq = 1, 0
            self.q_unacked.clear()
            self.nak_from, self.hello_at, self.qnak_seen = None, 0.0, (None, 0.0)
            pending, subs = list(self.pending.values()), list(self.subs.values())
            self.pending.clear()
            self.subs.clear()
            self.stats['epochs'] += 1
            self.byes.clear()
            self.lock.notify_all()
        for p in pending:
            p.fail('LINK_RESET')
        for s in subs:
            s.deliver(('end', None, None, None, 'LINK_RESET'))

    def _fail_all(self, code):
        with self.lock:
            pending, subs = list(self.pending.values()), list(self.subs.values())
            self.pending.clear()
            self.subs.clear()
        for p in pending:
            p.fail(code)
        for s in subs:
            s.deliver(('end', None, None, None, code))

    # -------------------------------------------------------------------------------------------- timers
    def _tick_loop(self):
        while True:
            with self.lock:
                if self.closing:
                    return
                self.lock.wait(0.1)
            try:
                self._tick()
            except Exception as e:              # a timer must survive anything a transport does
                self.stats['tick_' + type(e).__name__] += 1

    def _tick(self):
        now, lines = self.clock(), []
        with self.lock:
            if self.stopping or self.closing or not self.attached or self.epoch is None or self.dead is not None:
                return
            if self.last_rx is None:
                if now - self.attached_at > self.silent_start_s:
                    self.dead = 'RELAY_SILENT'
                return
            if now - self.last_rx > self.dead_s:
                self.dead = 'RELAY_SILENT'
                return
            if not self.linked:
                if now - self.last_rx < self.alive_s and now - self.hello_at >= self.hello_repeat_s:
                    self.hello_at = now
                    lines.append(self._q0({'op': 'hello'}))
            else:
                if now - self.ack_at >= self.ack_every_s:
                    self.ack_at = now
                    lines.append(self._q0({'op': 'ack'}))
                if self.nak_from is not None and now - self.nak_at >= self.nak_repeat_s:
                    self.nak_at = now
                    self.stats['nak'] += 1
                    lines.append(self._q0({'op': 'nak', 'from': self.nak_from}))
                if self.q_unacked:
                    first = next(iter(self.q_unacked.values()))
                    if now - first[1] >= self.q_resend_s:
                        lines.extend(self._q_resend(None, now))
        self._send(lines)

    # ---------------------------------------------------------------------------------------- upper layer
    def request(self, method, path, body=None, timeout=REQUEST_TIMEOUT_S):
        """(status, content type, text) of one relayed request, or LinkError."""
        pending = _Pending()
        msg = {'op': 'req', 'method': method, 'path': path}
        if body is not None:
            msg['body'] = body
        rid = self._send_numbered(msg, lambda seq: self.pending.__setitem__(seq, pending))
        if not pending.done.wait(timeout):
            with self.lock:
                self.pending.pop(rid, None)
            raise LinkError('TIMEOUT')
        if pending.error:
            raise LinkError(pending.error)
        return pending.status, pending.ctype, ''.join(pending.parts)

    def subscribe(self, since=None, lei=None):
        sub = Subscription(self)
        msg = {'op': 'sub'}
        if since is not None:
            msg['since'] = since
        if lei is not None:
            msg['lei'] = lei

        def register(seq):
            sub.sid = seq
            self.subs[seq] = sub
        self._send_numbered(msg, register)
        return sub

    def unsubscribe(self, sub):
        with self.lock:
            active = sub.sid is not None and self.subs.get(sub.sid) is sub
            if active:
                del self.subs[sub.sid]
        if active:
            try:
                self._send_numbered({'op': 'unsub', 'sub': sub.sid}, lambda seq: None)
            except LinkError:
                pass

    def snapshot(self):
        with self.lock:
            return {'transport': self.transport.describe(), 'attached': self.attached, 'linked': self.linked,
                    'relay': dict(self.relay), 'e_next': self.e_next, 'q_seq': self.q_seq,
                    'q_unacked': len(self.q_unacked), 'pending': len(self.pending), 'streams': len(self.subs),
                    'stats': dict(sorted(self.stats.items())), 'relay_stats': dict(self.relay_stats),
                    'relay_errors': dict(self.relay_errors)}


# -------------------------------------------------------------------------------------------------------- http
def _split_target(target):
    if not target.startswith('/') or target.startswith('//'):
        return None, None
    path, _, query = target.partition('?')
    return path, query


def _json_media(value):
    parts = [p.strip() for p in (value or '').split(';')]
    if parts[0].lower() != 'application/json':
        return False
    for param in parts[1:]:
        if not param:
            continue
        name, _, setting = param.partition('=')
        if name.strip().lower() != 'charset' or setting.strip().strip('"').lower() not in ('utf-8', 'utf8'):
            return False
    return True


def _content_length(headers):
    lengths = headers.get_all('Content-Length') or []
    if headers.get_all('Transfer-Encoding') or len(lengths) != 1 or not LENGTH.fullmatch(lengths[0].strip()):
        return None
    return int(lengths[0].strip())


def _resume_args(query, headers):
    """(since, Last-Event-ID), each an int or None, or None for an invalid request (the gateway takes the larger)."""
    try:
        params = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=16)
    except ValueError:
        return None
    since = params.get('since', [])
    ids = [v for v in (headers.get_all('Last-Event-ID') or []) if v.strip()]
    if len(since) > 1 or len(ids) > 1:
        return None
    found = []
    for text in (since[0] if since else None, ids[0] if ids else None):
        if text is None:
            found.append(None)
        elif SEQ.fullmatch(text.strip()):
            found.append(int(text.strip()))
        else:
            return None
    return found[0], found[1]


def sse_block(event_id, data):
    lines = []
    if event_id is not None and SEQ.fullmatch(str(event_id)):
        lines.append('id: %s\n' % event_id)
    for part in data.split('\n'):
        lines.append('data: %s\n' % part.replace('\r', ''))
    lines.append('\n')
    return ''.join(lines).encode('utf-8')


LIMITS = {
    'BRIDGE_LINE_TOO_LONG': ef1.MAX_Q_LINE,
    'BRIDGE_RESPONSE_TOO_LARGE': MAX_RESPONSE,
    'BRIDGE_TIMEOUT': int(REQUEST_TIMEOUT_S),
}
LINK_REFUSALS = {
    'LINK_DOWN': (503, 'BRIDGE_LINK_DOWN'),
    'LINK_LOST': (502, 'BRIDGE_LINK_LOST'),
    'LINK_RESET': (502, 'BRIDGE_LINK_LOST'),
    'TIMEOUT': (504, 'BRIDGE_TIMEOUT'),
    'LINE_TOO_LONG': (413, 'BRIDGE_LINE_TOO_LONG'),
    'RESPONSE_TOO_LARGE': (502, 'BRIDGE_RESPONSE_TOO_LARGE'),
    'PROTOCOL': (502, 'BRIDGE_PROTOCOL'),
    'BRIDGE_STOPPING': (503, 'BRIDGE_STOPPING'),
}


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = os.name != 'nt'       # on Windows SO_REUSEADDR would let a second server share the port
    request_queue_size = 16

    def __init__(self, address, bridge):
        self.bridge = bridge
        super().__init__(address, _Handler)

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)        # no reverse DNS lookup
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        say('OWNER_BRIDGE_HTTP_ERROR %s' % (sys.exc_info()[0].__name__,))


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'EdgeFHE-Owner-Bridge'
    protocol_version = 'HTTP/1.0'
    timeout = 30
    _status = None
    _body_read = False

    def version_string(self):
        return self.server_version

    def log_message(self, format, *args):
        pass                    # no access log: owner data never reaches a log

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

    def _handle(self, method):
        bridge = self.server.bridge
        self._status = None
        self._body_read = method != 'POST'
        try:
            refusal = bridge.screen(self)
            if refusal is not None:
                self._refuse(403, refusal)
                return
            path, query = _split_target(self.path)
            if path is None:
                self._refuse(404, 'NOT_FOUND')
            elif path == API or path.startswith(API + '/'):
                self._api(method, path, query)
            elif method != 'GET':
                self._refuse(405, 'METHOD_NOT_ALLOWED', method=method)
            elif path == BRIDGE_STATUS:
                self._json(200, bridge.link.snapshot())
            else:
                self._static(path)
        except OSError:
            self.close_connection = True
        except Exception as e:
            say('OWNER_BRIDGE_HTTP_INTERNAL %s' % type(e).__name__)
            self.close_connection = True
            if self._status is None:
                try:
                    self._refuse(500, 'INTERNAL', why=type(e).__name__)
                except OSError:
                    pass

    def _api(self, method, path, query):
        known = path in (API + '/status', API + '/events', API + '/submit') or RUN_PATH.fullmatch(path)
        wanted = 'POST' if path == API + '/submit' else 'GET'
        if not known:
            self._refuse(404, 'NOT_FOUND')
        elif method != wanted:
            self._refuse(405, 'METHOD_NOT_ALLOWED', method=method)
        elif path == API + '/submit':
            self._submit()
        elif path == API + '/events':
            self._events(query)
        else:
            self._relayed('GET', path, None)

    def _relayed(self, method, path, body):
        try:
            status, ctype, text = self.server.bridge.link.request(method, path, body)
        except LinkError as e:
            self._link_refuse(e.code, method)
            return
        self._reply(status, ctype, text)

    def _submit(self):
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
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            self._refuse(400, 'BODY_JSON')
            return
        self._relayed('POST', API + '/submit', text)

    def _events(self, query):
        bridge = self.server.bridge
        resume = _resume_args(query, self.headers)
        if resume is None:
            self._refuse(400, 'QUERY_INVALID')
            return
        if not bridge.streams.acquire(blocking=False):
            self._refuse(503, 'BRIDGE_STREAMS_BUSY', limit=STREAMS_MAX)
            return
        try:
            try:
                sub = bridge.link.subscribe(resume[0], resume[1])
            except LinkError as e:
                self._link_refuse(e.code, 'GET')
                return
            try:
                self._stream(sub)
            finally:
                sub.close()
        finally:
            bridge.streams.release()

    def _stream(self, sub):
        first = sub.get(SUB_OPEN_TIMEOUT_S)
        if first is None:
            self._link_refuse('TIMEOUT', 'GET')
            return
        if first[0] == 'end':
            status, ctype, body, code = first[1:]
            if type(status) is int and isinstance(body, str):
                self._reply(status, ctype, body)
            else:
                self._link_refuse(code if code in LINK_REFUSALS else 'LINK_LOST', 'GET')
            return
        if first[0] != 'open':
            self._link_refuse('PROTOCOL', 'GET')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        self.wfile.write(('retry: %d\n: heartbeat\n\n' % RETRY_MS).encode('ascii'))
        beat = time.monotonic() + HEARTBEAT_S
        self.close_connection = True
        while True:
            item = sub.get(max(0.05, beat - time.monotonic()))
            if item is not None:
                if item[0] == 'end':
                    return
                if item[0] == 'ev':
                    self.wfile.write(sse_block(item[1], item[2]))
            now = time.monotonic()
            if now >= beat:
                if self._peer_gone():
                    return                      # the relay's stream (and the gateway's slot) is released at once
                self.wfile.write(b': heartbeat\n\n')
                beat = now + HEARTBEAT_S

    def _peer_gone(self):
        """True when the browser closed its side: an event stream request sends nothing after its headers, so a
        readable socket means EOF. A write alone would notice it only at the second heartbeat."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b''
        except (OSError, ValueError):
            return True

    def _static(self, path):
        found = self.server.bridge.static_file(path)
        if found is None:
            self._refuse(404, 'NOT_FOUND')
            return
        data, content_type = found
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _reply(self, status, ctype, text):
        """A relayed answer: the gateway's status and JSON body as they came."""
        if type(status) is not int or not 100 <= status <= 599 or not isinstance(text, str):
            self._link_refuse('PROTOCOL', 'GET')
            return
        data = text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', ctype if _json_media(ctype) else JSON_TYPE)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', JSON_TYPE)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _drain(self):
        if self._body_read:
            return
        self._body_read = True
        length = _content_length(self.headers)
        if length is not None and 0 < length <= DRAIN_LIMIT:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def _refuse(self, status, code, **values):
        values.setdefault('port', self.server.bridge.port)
        self._drain()
        self._json(status, {'code': code, 'message': _text(MESSAGES, code, values)})

    def _link_refuse(self, link_code, method):
        status, code = LINK_REFUSALS.get(link_code, (502, 'BRIDGE_LINK_LOST'))
        limit = LIMITS.get(code)
        if method == 'POST' and link_code in ('TIMEOUT', 'LINK_LOST', 'LINK_RESET', 'PROTOCOL'):
            code = 'BRIDGE_SUBMIT_UNCERTAIN'        # the gateway may have started the run: never resend by itself
        self._refuse(status, code, limit=limit)


class Bridge:
    """The owner screen on 127.0.0.1 with /api/v1 over a Link."""

    def __init__(self, transport, port=DEFAULT_PORT, ui_dir=DEFAULT_UI, host='127.0.0.1'):
        self.link = Link(transport)
        self.host = host
        self.port = port
        self.ui_real = os.path.realpath(ui_dir) if ui_dir and os.path.isdir(ui_dir) else None
        self.streams = threading.BoundedSemaphore(STREAMS_MAX)
        self.server = None
        self._thread = None
        self._policy(port)

    def _policy(self, port):
        self.port = port
        self.hosts = frozenset(('127.0.0.1:%d' % port, 'localhost:%d' % port))
        self.origins = frozenset(('http://127.0.0.1:%d' % port, 'http://localhost:%d' % port))

    def bind(self):
        self.server = _Server((self.host, self.port), self)
        self._policy(self.server.server_address[1])

    def start(self, link_timeout=LINK_START_S):
        """Open the transport (TransportError), wait for the link, then serve; True when the link came up."""
        self.link.start()
        linked = self.link.wait_linked(link_timeout)
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.25},
                                        name='owner-bridge-http', daemon=True)
        self._thread.start()
        return linked

    def close(self):
        if self.server is not None:
            if self._thread is not None:
                self.server.shutdown()
            self.server.server_close()
        self.link.close()

    def screen(self, handler):
        if not handler.client_address[0].startswith('127.'):
            return 'CLIENT_FORBIDDEN'
        hosts = handler.headers.get_all('Host') or []
        if len(hosts) != 1 or hosts[0].strip().lower() not in self.hosts:
            return 'HOST_FORBIDDEN'
        origins = handler.headers.get_all('Origin') or []
        if len(origins) > 1 or (origins and origins[0].strip().lower() not in self.origins):
            return 'ORIGIN_FORBIDDEN'
        return None

    def static_file(self, path):
        """(bytes, content type) of a file of the owner-screen directory (the gateway's rules), or None."""
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
        if not os.path.normcase(full).startswith(os.path.normcase(self.ui_real + os.sep)):
            return None
        try:
            fd = os.open(full, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
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


# ------------------------------------------------------------------------------------------ the cloud-PC rule
def probe_console(address=CLOUD_CONSOLE, timeout=CLOUD_PROBE_S):
    """True when the PC evidence console answers on address. The request is a HEAD with a foreign Host, which the
    console refuses (403) before any handler runs; only the Server header of the answer is read."""
    try:
        conn = socket.create_connection(address, timeout=timeout)
    except OSError:
        return False
    head = b''
    try:
        conn.settimeout(timeout)
        conn.sendall(b'HEAD / HTTP/1.0\r\nHost: owner-bridge-check\r\nConnection: close\r\n\r\n')
        while b'\r\n\r\n' not in head and len(head) < 16384:
            chunk = conn.recv(4096)
            if not chunk:
                break
            head += chunk
    except OSError:
        pass
    finally:
        conn.close()
    for line in head.split(b'\r\n')[1:]:
        name, sep, value = line.partition(b':')
        if sep and name.strip().lower() == b'server':
            return value.strip().lower().startswith(CLOUD_CONSOLE_SERVER.encode('ascii'))
    return False


def docker_containers(run=subprocess.run, which=shutil.which, timeout=DOCKER_PS_S):
    """[(name, image)] of the running Docker containers; [] without a docker command or a running engine."""
    docker = which('docker')
    if not docker:
        return []
    try:
        proc = run([docker, 'ps', '--no-trunc', '--format', '{{.Names}}\t{{.Image}}'], stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    found = []
    for line in proc.stdout.decode('utf-8', 'replace').splitlines():
        name, _, image = line.strip().partition('\t')
        if name:
            found.append((name, image.strip()))
    return found


def image_repository(image):
    """'edgefhe-demo-cloud' of 'edgefhe-demo-cloud:dev', 'host:5000/edgefhe-demo-cloud:abc' or '...@sha256:...'."""
    last = image.split('@', 1)[0].rsplit('/', 1)[-1]
    return last.split(':', 1)[0].lower()


def cloud_pc_signs(console=probe_console, containers=docker_containers, isdir=os.path.isdir, dirs=CLOUD_DIRS):
    """[(code, zh-TW text)] of the signs that this machine is the cloud PC; empty on the owner laptop."""
    signs = []
    if console(CLOUD_CONSOLE):
        signs.append(('CONSOLE', OPERATOR['SIGN_CONSOLE'] % CLOUD_CONSOLE))
    for name, image in containers():
        if name == CLOUD_CONTAINER or image_repository(image) == CLOUD_IMAGE:
            signs.append(('CONTAINER', OPERATOR['SIGN_CONTAINER'] % (name, image or '?')))
    for path in dirs:
        if isdir(path):
            signs.append(('FOLDER', OPERATOR['SIGN_DIR'] % path))
    return signs


# -------------------------------------------------------------------------------------------------------- main
def parse_listen(text):
    host, sep, port = text.rpartition(':')
    if sep != ':' or host != '127.0.0.1' or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(text)
    return host, int(port)


def main(argv=None, signs=cloud_pc_signs):
    _utf8_stdio()
    ap = argparse.ArgumentParser(description='EdgeFHE owner-laptop bridge (owner screen over the serial console).')
    ap.add_argument(OWNER_LAPTOP_FLAG, dest='owner_laptop', action='store_true',
                    help='required: this machine is the owner laptop, never the cloud PC (the rule in the docstring)')
    ap.add_argument('--transport', required=True, help='serial:COMn, or cmd:<command> (a JSON array or a command line)')
    ap.add_argument('--listen', default='127.0.0.1:%d' % DEFAULT_PORT)
    ap.add_argument('--ui-dir', default=DEFAULT_UI)
    ap.add_argument('--user', default='ubuntu', help='board console user (the password comes only from KV_PW)')
    ap.add_argument('--relay', default=DEFAULT_RELAY, help='serial_relay.py on the board')
    ap.add_argument('--python', default='python3', help='Python 3.10 on the board')
    ap.add_argument('--gateway', default=DEFAULT_GATEWAY, help='the board gateway as the relay reaches it')
    ap.add_argument('--link-timeout', type=float, default=LINK_START_S)
    args = ap.parse_args(argv)
    if not args.owner_laptop:
        say(OPERATOR['USAGE_OWNER_LAPTOP'])
        out('OWNER_BRIDGE_USAGE code=OWNER_LAPTOP')
        return EXIT_USAGE
    try:
        host, port = parse_listen(args.listen)
    except ValueError:
        say(OPERATOR['USAGE_LISTEN'])
        out('OWNER_BRIDGE_USAGE code=LISTEN')
        return EXIT_USAGE
    try:
        ef1.parse_gateway(args.gateway)
    except ValueError:
        say(OPERATOR['USAGE_GATEWAY'])
        out('OWNER_BRIDGE_USAGE code=GATEWAY')
        return EXIT_USAGE
    try:
        transport = parse_transport(args.transport, args.user, args.relay, args.python, args.gateway)
    except ValueError:
        say(OPERATOR['USAGE_TRANSPORT'])
        out('OWNER_BRIDGE_USAGE code=TRANSPORT')
        return EXIT_USAGE
    found = signs()                     # before the listening socket, the serial port or the relay is opened
    if found:
        say(OPERATOR['CLOUD_PC'] % '；'.join(text for _, text in found))
        out('OWNER_BRIDGE_REFUSED code=CLOUD_PC signs=%s' % ','.join(code for code, _ in found))
        return EXIT_USAGE
    if not os.path.isdir(args.ui_dir):
        say(OPERATOR['UI_DIR'] % args.ui_dir)
    if transport.kind == 'serial':
        say(OPERATOR['DMESG_HINT'])
    bridge = Bridge(transport, port=port, ui_dir=args.ui_dir, host=host)
    try:
        bridge.bind()
    except OSError as e:
        say(OPERATOR['LISTEN_BIND'] % (args.listen, type(e).__name__))
        out('OWNER_BRIDGE_FAILED code=LISTEN_BIND address=%s' % args.listen)
        return EXIT_FAIL
    try:
        linked = bridge.start(args.link_timeout)
    except TransportError as e:
        say(str(e))
        out('OWNER_BRIDGE_FAILED code=TRANSPORT transport=%s' % transport.describe())
        bridge.close()
        return EXIT_FAIL
    if not linked:
        say(OPERATOR['LINK_TIMEOUT'] % args.link_timeout)
        out('OWNER_BRIDGE_FAILED code=LINK_TIMEOUT transport=%s' % transport.describe())
        bridge.close()
        return EXIT_FAIL
    out('OWNER_BRIDGE_READY http://127.0.0.1:%d/' % bridge.port)
    say(OPERATOR['READY'] % bridge.port)
    try:
        status, _, _ = bridge.link.request('GET', API + '/status', timeout=60)
        out('OWNER_BRIDGE_GATEWAY status=%d' % status)
        if status == 200:
            say(OPERATOR['GATEWAY_OK'] % status)
        else:
            say(OPERATOR['GATEWAY_FAIL'] % ('HTTP %d' % status))
    except LinkError as e:
        out('OWNER_BRIDGE_GATEWAY status=- code=%s' % e.code)
        say(OPERATOR['GATEWAY_FAIL'] % e.code)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
        snap = bridge.link.snapshot()['stats']
        out('OWNER_BRIDGE_STOPPED crc_bad=%d ignored=%d resynced=%d epochs=%d' % (
            snap.get('crc_bad', 0), snap.get('ignored', 0), snap.get('resynced', 0), snap.get('epochs', 0)))
        say(OPERATOR['STOPPED'])
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
