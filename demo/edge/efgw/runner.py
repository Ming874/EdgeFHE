"""The efnet_client process, DMA IRQ counters, private-file wipe and the Gateway shared by the CLI and the HTTP API.

Safety rule (v8b_rev5/software/driver/ef_v8b_main.c:208,334,389): the driver's ENC wait is interruptible, a signal
pending after issue jumps to the fault path, and that path quarantines the PL until a supervised reboot. This module
therefore never signals or kills a client. It starts the client in a new session (start_new_session=True, so a
terminal Ctrl-C never reaches it), waits for it without a timeout, lets a watchdog only report CLIENT_OVERDUE, and
install_stop_signals() turns SIGTERM, SIGINT, SIGHUP, SIGQUIT, SIGUSR1 and SIGUSR2 into "stop accepting work, finish
the run in flight".

One run at a time: an in-process lock plus flock(PL_LOCK_PATH), a fixed path that gateway.json cannot move, across
processes, and for the PL backend a scan of /proc/*/fd that refuses to start while another process holds the DMA node
(matched by device number, so a second node of the same major:minor counts too). Private files live in
<workdir>/<run>/ (0700): in.csv (0600, written here), score.txt (0600, created by the client) and permit (0600). After
the client exited they are overwritten with zeros and unlinked; a STOP code is reported, never retried. A private
record of the form is kept on tmpfs only when gateway.json sets retain_private to true (default false).
Python 3.10 standard library only.
"""
import collections
import errno
import hashlib
import json
import math
import os
import re
import secrets
import signal
import stat
import subprocess
import threading
import time

from . import model as cdc
from . import permit
from . import records

MODEL_ID = cdc.MODEL_ID
STOP_MARK = 'STOP_CPP_E2E_CLIENT_PRESERVE_NO_RETRY'
PASS_STATUS = {'pl': 'CPP_E2E_PL_NUMERIC_AND_DRAIN_PASS', 'spec': 'CPP_E2E_CPU_SPEC_NUMERIC_AND_DRAIN_PASS',
               'cpu': 'CPP_E2E_CPU_SPEC_NUMERIC_AND_DRAIN_PASS'}
PL_HZ_MIN, PL_HZ_MAX = 199800000, 200000000      # client.cpp:305
DMA_ACTION = 'xilinx-dma-controller'
DMA_LINES = 2                                    # the tx and rx channels of the AXI DMA (ef_v8b_main.c:507-509)
CLIENT_ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C'}
PUBLIC_GATE = 2e-3                               # client.cpp:268 for model ids without "ridge"
GATE_ROWS, GATE_REQUESTS = 2048, 20
THROUGHPUT_ROWS, THROUGHPUT_REQUESTS = 2048, 100000
TRACE_EVENT_LIMIT = 32
EXPECTABLE_STOPS = ('OWNER_CREATE',)
# The default-fatal signals an operator or a service manager sends; each one only stops the acceptance of work.
# SIGKILL cannot be caught: never send it to the gateway while a client runs (README, systemd KillMode=mixed).
STOP_SIGNALS = ('SIGTERM', 'SIGINT', 'SIGHUP', 'SIGQUIT', 'SIGUSR1', 'SIGUSR2')
DMA_NODE = '/dev/edgefhe_v8b_dma'                # the V8b driver's node; also checked when gateway.json moves dma_node
PL_LOCK_PATH = '/run/edgefhe-demo.pl.lock'       # one lock per machine, whatever workdir a gateway.json names
DEVICE_TREE_COMPATIBLE = '/proc/device-tree/compatible'
BOARD_COMPATIBLE = b'xlnx,zynqmp'                # the KV260 (Zynq UltraScale+ MPSoC) device tree
wipe_file = records.wipe_file

# Every message below is one complete zh-TW sentence and never states the no-retry rule itself: the owner screen and
# the command line add NO_RETRY_LINE once under the message of every error (demo/edge/EVENTS.md, section 9).
NO_RETRY_LINE = '本系統不會自動重試，也不會中斷加密程式。'
_STOPPED = '加密程式已自行停止此次推論。'
MESSAGES = {
    # gateway
    'BUSY': '本閘道正在處理另一筆請求，操作人員應待該筆請求完成後再送出。',
    'PL_LOCK_BUSY': ('另一個程序正持有本閘道的執行鎖（%(lock)s），因此本閘道不啟動加密程式，操作人員應待該程序結束後'
                     '再送出。'),
    'PL_LOCK_UNSAFE': '本閘道無法安全使用執行鎖 %(lock)s（%(why)s），因此本閘道不啟動加密程式。',
    'PL_DEVICE_BUSY': 'FPGA 裝置 %(node)s 正由其他程序（PID %(pids)s）開啟，因此本閘道不啟動加密程式。',
    'REHEARSAL_PL_ON_BOARD': ('本閘道以 PC 演練模式執行，卻偵測到 KV260 的跡象（%(node)s），因此本閘道拒絕以 FPGA（PL）'
                              '後端執行此次請求。'),
    'GATEWAY_STOPPING': '本閘道已收到停止要求，不再接受新的請求。',
    'GATEWAY_PID1': ('本閘道以 PID 1 執行，而 FPGA 呼叫許可要求加密程式的父程序 PID 大於 1，'
                     '因此操作人員應以 docker run --init 或 shell 包裝啟動本閘道。'),
    'ROOT_REQUIRED': '本閘道必須以 root 執行（sudo），加密程式才能使用 FPGA，私有暫存檔也才能僅限 root 存取。',
    'BACKEND_NOT_ALLOWED': '設定檔未允許加密後端「%(backend)s」，允許的後端為 %(allowed)s。',
    'EXPECT_INVALID': '--expect 只接受 OWNER_CREATE，且只適用於 pl 後端。',
    'EXPECT_REQUIRES_NO_DEVICE': ('--expect OWNER_CREATE 只能在 FPGA 裝置 %(node)s 尚未載入時使用，'
                                  '因此本閘道不啟動加密程式。'),
    'EXPECTATION_UNMET': '本閘道預期加密程式以停止代碼 %(expected)s 結束，實際結果為 %(actual)s。',
    'WORKDIR_UNSAFE': '工作目錄 %(path)s 無法安全使用（%(why)s），因此本閘道不寫入任何私有檔案。',
    'RUN_SETUP': '本閘道無法建立此次執行的私有檔案或呼叫許可（%(why)s），因此本閘道未啟動加密程式。',
    'CLIENT_SPAWN': '本閘道無法啟動加密程式（%(why)s）。',
    'CLIENT_OVERDUE': ('加密程式已執行超過 %(seconds)s 秒，本閘道將持續等待其自行結束，操作人員不得手動終止加密程式'
                       '（中斷進行中的 FPGA 加密會使驅動程式進入隔離狀態）。'),
    'CLIENT_SIGNALLED': ('加密程式遭訊號 %(signal)s 終止（本閘道未發送任何訊號），請操作人員以 dmesg 檢查 FPGA 驅動程式'
                         '的狀態並依操作手冊處理。'),
    'CLIENT_EXIT': '加密程式以非預期的結束碼 %(returncode)s 結束，且未輸出停止代碼。',
    'CLIENT_OUTPUT': '加密程式的輸出無法解析（%(why)s），因此本系統不採用此次結果。',
    'CLIENT_REPORT_MISMATCH': ('加密程式的執行報告與預期不符（%(field)s：預期 %(expected)s，實際 %(actual)s），'
                               '因此本系統不採用此次結果。'),
    'IRQ_LINES': ('/proc/interrupts 中的 xilinx-dma-controller 中斷線為 %(count)s 條（預期 %(expected)s 條，且執行前後'
                  '須相同），因此本系統不採用此次結果。'),
    'IRQ_DELTA_MISMATCH': 'FPGA 的 DMA 中斷增量（%(delta)s）與裝置呼叫次數（%(calls)s）不符，因此本系統不採用此次結果。',
    'SCORE_FILE': '本閘道無法讀取解密結果檔（%(why)s），因此本系統不採用此次結果。',
    'VERIFICATION_FAILED': '%(text)s',
    'PROGRESS_SAMPLER': '進度取樣器發生錯誤（%(why)s），此錯誤不影響加密程式。',
    'RECORD_WRITE': '本閘道無法寫入紀錄 %(path)s（%(why)s）。',
    'WIPE_FAILED': '本閘道無法清除私有檔案 %(path)s（%(why)s），請操作人員以 root 手動刪除該檔案。',
    'INTERNAL': '本閘道發生內部錯誤（%(why)s），且本閘道未對加密程式發送任何訊號。',
    # efnet_client STOP codes (client.cpp, tls.hpp, invoke_permit.hpp, wire.hpp)
    'NORMALIZATION_REFUSED': '加密程式拒絕此筆輸入，因為標準化後的數值超出 ±8 的範圍。',
    'TLS_PEER_CHAIN': '雲端伺服器的憑證未通過驗證（憑證鏈、名稱或 TLS 1.3 版本不符），' + _STOPPED,
    'TLS_PEER_PIN': '雲端伺服器的憑證與設定的釘選值不符，' + _STOPPED,
    'TLS_PEER_CERT_REQUIRED': '雲端伺服器未提供憑證，' + _STOPPED,
    'TLS_HANDSHAKE_EOF': 'TLS 交握期間連線遭對方關閉（雲端伺服器可能拒絕了板端憑證），' + _STOPPED,
    'TLS_DEADLINE': '與雲端伺服器的通訊超過時限（%(timeout_ms)s 毫秒），' + _STOPPED,
    'TLS_FATAL_OR_UNEXPECTED_EOF': 'TLS 連線發生錯誤或遭對方中斷，' + _STOPPED,
    'TLS_LOCAL_CREDENTIALS': '加密程式無法載入板端的憑證或私鑰。',
    'TLS_CA': '加密程式無法載入 CA 憑證。',
    'LITERAL_IPV4_REQUIRED': '設定的雲端位址不是 IPv4 位址字面值。',
    'OWNER_CREATE': '加密程式無法建立金鑰與加密環境（例如 FPGA 裝置尚未載入）。',
    'PL_CLOCK_RANGE': 'FPGA 的時脈不在 199.8–200 MHz 的允許範圍內，因此本系統不採用此次結果。',
    'CPU_SPEC_PL_FRAME_ACCOUNTING': '加密裝置的呼叫次數與預期不符，因此本系統不採用此次結果。',
    'PL_ROOT_CONTROLLER_REQUIRED': 'FPGA 呼叫許可要求本閘道與加密程式皆以 root 執行。',
    'PL_REQUIRES_CONTROLLED_INVOCATION': 'FPGA 後端需要呼叫許可與私有結果檔。',
    'CPU_SPEC_TAKES_NO_PL_PERMIT': 'CPU 與規格模型後端不接受 FPGA 呼叫許可。',
    'MODEL_UNKNOWN': '加密程式不認得本展示模型（板端程式可能未以展示模型的疊加樹建置）。',
    'PUBLIC_AFFINE_ORACLE': '公開測試資料的推論誤差超過容許範圍（2×10⁻³）。',
}
PREFIX_MESSAGES = (
    ('PL_PERMIT_', 'FPGA 呼叫許可未通過加密程式的檢查（%(code)s），因此加密程式未開啟 FPGA 裝置。'),
    ('SOCKET_CONNECT', '加密程式無法連線至雲端伺服器 %(host)s:%(port)s，請操作人員確認網路線、路由器、防火牆與雲端伺服器的'
     '狀態。'),
    ('SOCKET_', '加密程式與雲端伺服器之間的網路連線發生錯誤（%(code)s）。'),
    ('TLS_', 'TLS 連線發生錯誤（%(code)s），' + _STOPPED),
    ('OWNER_', '加密程式在金鑰、加密或解密步驟發生錯誤（%(code)s）。'),
    ('WIRE_', '雲端回應未通過格式或綁定檢查（%(code)s），因此本系統不採用此次結果。'),
    ('RESPONSE_', '雲端回應未通過格式或綁定檢查（%(code)s），因此本系統不採用此次結果。'),
    ('CSV_', '加密程式無法讀取輸入檔（%(code)s）。'),
    ('PRIVATE_', '加密程式無法讀寫私有檔案（%(code)s）。'),
    ('PERMIT_', '呼叫許可的內容不正確（%(code)s）。'),
)
DEFAULT_STOP = '加密程式以停止代碼 %(code)s 結束。'
HTTP_STATUS = {'BACKEND_NOT_ALLOWED': 400, 'EXPECT_INVALID': 400, 'BUSY': 409, 'PL_LOCK_BUSY': 409,
               'PL_DEVICE_BUSY': 409, 'EXPECT_REQUIRES_NO_DEVICE': 409, 'GATEWAY_STOPPING': 503,
               'ROOT_REQUIRED': 503, 'GATEWAY_PID1': 503, 'WORKDIR_UNSAFE': 503, 'PL_LOCK_UNSAFE': 503,
               'REHEARSAL_PL_ON_BOARD': 503}


def describe(code, **ctx):
    """The zh-TW message of a gateway code or an efnet_client STOP code (timeout_ms with thousands separators)."""
    template = MESSAGES.get(code)
    if template is None:
        for prefix, text in PREFIX_MESSAGES:
            if code.startswith(prefix):
                template = text
                break
    values = collections.defaultdict(lambda: '?', ctx)
    values['code'] = code
    if _is_int(values.get('timeout_ms')):
        values['timeout_ms'] = '{:,}'.format(values['timeout_ms'])     # 30000 -> 30,000
    return (template or DEFAULT_STOP) % values


class GatewayError(Exception):
    """A run is refused or failed; code is a gateway code or a client STOP code (see describe)."""

    def __init__(self, code, **ctx):
        self.code = code
        self.ctx = ctx
        super().__init__(code)

    @property
    def http_status(self):
        return HTTP_STATUS.get(self.code, 500)

    def message(self):
        return describe(self.code, **self.ctx)

    def body(self):
        return {'code': self.code, 'message': self.message()}


def _oserror(e):
    return '%s: %s' % (type(e).__name__, e.strerror or e) if isinstance(e, OSError) else str(e)


def _signal_name(number):
    try:
        return signal.Signals(number).name
    except ValueError:
        return str(number)


def _is_int(value):
    return type(value) is int


def _is_hex64(value):
    return isinstance(value, str) and records.HEX64.fullmatch(value) is not None


# ---------------------------------------------------------------------------------------------- DMA counters
def parse_interrupts(text, action=DMA_ACTION):
    """{irq: {'count': total over CPUs, 'desc': chip, hwirq, type and action}} of the lines whose action contains
    `action`, keyed by the IRQ number of the first column of /proc/interrupts."""
    lines = text.splitlines()
    if not lines:
        return {}
    cpus = len(lines[0].split())
    found = {}
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < cpus + 2 or not parts[0].endswith(':'):
            continue
        counts = parts[1:1 + cpus]
        if not all(c.isdigit() for c in counts):
            continue
        desc = ' '.join(parts[1 + cpus:])
        if action in desc:
            found[parts[0][:-1]] = {'count': sum(int(c) for c in counts), 'desc': desc}
    return found


def irq_snapshot(path='/proc/interrupts'):
    try:
        with open(path, 'r', encoding='ascii', errors='replace') as f:
            return parse_interrupts(f.read())
    except OSError:
        return {}


def irq_counts(snapshot):
    return {irq: line['count'] for irq, line in sorted(snapshot.items())}


def irq_delta(before, after):
    return {irq: after[irq]['count'] - before[irq]['count'] for irq in sorted(after) if irq in before}


def _file_identity(st):
    """A device node by its device number (st_rdev: every node of the same major:minor is the same device), any other
    file by its inode (the test benches use a regular file as the DMA node)."""
    kind = stat.S_IFMT(st.st_mode)
    if kind in (stat.S_IFCHR, stat.S_IFBLK):
        return kind, st.st_rdev
    return kind, st.st_dev, st.st_ino


# File systems on which stat() can wait on a server or a user-space daemon (a hung NFS or FUSE mount would stall the
# admission, which holds the run lock); a DMA node never lives there. Matched as prefixes of the mount's type.
REMOTE_FS_PREFIXES = ('fuse', 'nfs', 'cifs', 'smb', '9p', 'ceph', 'glusterfs', 'afs', 'autofs', 'davfs', 'lustre',
                      'virtiofs', 'coda', 'orangefs', 'ncpfs', 'gfs2', 'ocfs2', 'sshfs')


def _remote_mounts(proc_root):
    """Mount points (longest first) of this mount namespace whose type is in REMOTE_FS_PREFIXES, and the local ones,
    as [(mount point, remote)], or None when the mount table cannot be read."""
    try:
        with open(os.path.join(proc_root, 'self', 'mounts'), encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        return None
    mounts = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        point = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), parts[1])
        mounts.append((point, parts[2].lower().startswith(REMOTE_FS_PREFIXES)))
    mounts.reverse()                    # of two mounts on one point, the later (visible) one stays first
    mounts.sort(key=lambda m: len(m[0]), reverse=True)
    return mounts


def _on_remote_fs(path, mounts):
    for point, remote in mounts or ():
        if path == point or path.startswith(point.rstrip('/') + '/') or point == '/':
            return remote
    return False


def dma_holders(nodes, proc_root='/proc'):
    """PIDs of the processes that hold one of the DMA nodes (a path or a list of paths) open, from a scan of
    /proc/*/fd. Each descriptor's link text is read first: the node's own path matches at once; pipes, sockets and
    other non-path targets are skipped; any other absolute path is compared by os.stat() identity (_file_identity: a
    device node by st_rdev, so a second node of the same major:minor counts too; a regular test node by inode) unless
    it lies on a network or FUSE file system, where stat() could block. Nodes that do not exist are ignored."""
    identities, targets = set(), set()
    for node in [nodes] if isinstance(nodes, str) else list(nodes):
        try:
            identities.add(_file_identity(os.stat(node)))
        except OSError:
            continue
        targets.update((node, os.path.realpath(node)))
    if not identities:
        return []
    holders = set()
    try:
        names = os.listdir(proc_root)
    except OSError:
        return []
    mounts = _remote_mounts(proc_root)
    for name in names:
        if not name.isdigit():
            continue
        fd_dir = os.path.join(proc_root, name, 'fd')
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            link = os.path.join(fd_dir, fd)
            try:
                target = os.readlink(link)
            except OSError:
                continue
            held = target in targets
            if not held and target.startswith('/') and not _on_remote_fs(target, mounts):
                try:
                    held = _file_identity(os.stat(link)) in identities
                except OSError:
                    held = False
            if held:
                holders.add(int(name))
                break
    return sorted(holders)


def board_markers(cfg=None, compatible=DEVICE_TREE_COMPATIBLE):
    """What makes this machine look like the board (empty on a PC): the fixed DMA node, the configured DMA node, or a
    Zynq UltraScale+ device tree. The PC rehearsal refuses to listen on all interfaces, or to run the pl backend, while
    any of them is present (httpapi.serve, Gateway._admit)."""
    found = []
    for node in (DMA_NODE, (cfg or {}).get('dma_node')):
        if node and node not in found and os.path.lexists(node):
            found.append(node)
    try:
        with open(compatible, 'rb') as f:
            if BOARD_COMPATIBLE in f.read(4096):
                found.append('%s（%s）' % (compatible, BOARD_COMPATIBLE.decode('ascii')))
    except OSError:
        pass
    return found


# ----------------------------------------------------------------------------------------- files and locking
class PlLock:
    """flock(LOCK_EX | LOCK_NB) on one file for the duration of one run (released by release()).

    The gateway always uses PL_LOCK_PATH, which gateway.json cannot change, so two gateways started with different
    configurations or work directories still exclude each other. acquire() returns False while another process holds
    the lock and raises OSError when the path is not a regular file owned by this user with a single link."""

    def __init__(self, path=PL_LOCK_PATH):
        self.path = path
        self.fd = None

    def acquire(self):
        import fcntl
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1:
                raise OSError(errno.EPERM, 'not a regular file of uid %d with one link' % os.geteuid(), self.path)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def prepare_workdir(path):
    """The work directory exists, is a real directory owned by this user and has mode 0700."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OSError('not a directory')
    if st.st_uid != os.geteuid():
        raise OSError('owned by uid %d' % st.st_uid)
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)


def write_private(path, text):
    """Create a 0600 file exclusively (no symlink is followed) and fsync it."""
    records._write_private(path, text.encode('ascii'))


_SCORE = re.compile(r'-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')


def read_score(path):
    """The one %.17g line that the client wrote (client.cpp:56-67); finite."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= 64:
            raise ValueError('檔案大小 %d 位元組' % st.st_size)
        data = os.read(fd, 128)
    finally:
        os.close(fd)
    text = data.decode('ascii')
    if not text.endswith('\n') or text.count('\n') != 1 or not _SCORE.fullmatch(text[:-1]):
        raise ValueError('格式不是單行數值')
    value = float(text[:-1])
    if not math.isfinite(value):
        raise ValueError('非有限值')
    return value


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


# ----------------------------------------------------------------------------------------------- the client
def _drain(stream, sink):
    fd = stream.fileno()
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            sink.append(chunk)
    except OSError:
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _drain_together(pairs):
    """Read several pipes to EOF in the calling thread (only for pipes whose reader thread could not start)."""
    import selectors
    selector = selectors.DefaultSelector()
    try:
        for stream, sink in pairs:
            try:
                selector.register(stream.fileno(), selectors.EVENT_READ, (stream, sink))
            except (OSError, ValueError):
                continue
        while selector.get_map():
            for key, _ in selector.select():
                stream, sink = key.data
                try:
                    chunk = os.read(key.fd, 65536)
                except OSError:
                    chunk = b''
                if chunk:
                    sink.append(chunk)
                    continue
                selector.unregister(key.fd)
                try:
                    stream.close()
                except OSError:
                    pass
    finally:
        selector.close()


class ClientProcess:
    """One efnet_client: own session, fixed environment, never signalled; wait() has no timeout.

    Once Popen has returned, the constructor does not raise: a reader thread that cannot start (RuntimeError) leaves its
    pipe to wait(), which then drains it in the calling thread. The caller therefore always holds the object and always
    waits for the client before it wipes the private files, removes the permit or releases the lock."""

    def __init__(self, argv, pass_fds=()):
        self.argv = list(argv)
        self.started_ns = time.monotonic_ns()
        self.exited = False
        self.proc = subprocess.Popen(self.argv, shell=False, start_new_session=True, pass_fds=tuple(pass_fds),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=dict(CLIENT_ENV), cwd='/', close_fds=True)
        self.pid = self.proc.pid
        self._out, self._err = [], []
        self._readers = []
        self._unread = [(self.proc.stdout, self._out), (self.proc.stderr, self._err)]
        try:
            for stream, sink in list(self._unread):
                reader = threading.Thread(target=_drain, args=(stream, sink), daemon=True)
                try:
                    reader.start()
                except Exception:
                    continue                    # no thread could start: wait() reads this pipe itself
                self._readers.append(reader)
                self._unread = [pair for pair in self._unread if pair[0] is not stream]
        except BaseException:
            self.wait()                         # never leave a started client behind, then re-raise
            raise

    def wait(self):
        """(returncode, stdout, stderr, wall_ns) once the client has exited on its own."""
        if self._unread:
            unread, self._unread = self._unread, []
            _drain_together(unread)
        while True:
            try:
                returncode = self.proc.wait()
                break
            except InterruptedError:
                continue
        self.exited = True
        wall_ns = time.monotonic_ns() - self.started_ns
        for reader in self._readers:
            reader.join(10.0)
        return returncode, b''.join(self._out), b''.join(self._err), wall_ns


class Watchdog:
    """Calls report() once if stop() was not called within `seconds`. It only reports; it never signals."""

    def __init__(self, seconds, report):
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(seconds, report), daemon=True)
        self._thread.start()

    def _run(self, seconds, report):
        if not self._done.wait(seconds):
            try:
                report()
            except Exception:
                pass

    def stop(self):
        self._done.set()


_STOP_CODE = re.compile(r'[A-Za-z0-9_:.\-]{1,80}')


def parse_client_output(returncode, stdout, stderr):
    """(report, stop, stop_detail, problem): the last stdout JSON line of a clean exit, or the STOP code that the
    client printed on stderr ('STOP_CPP_E2E_CLIENT_PRESERVE_NO_RETRY <CODE>', client.cpp:319)."""
    stop = detail = None
    for line in stderr.decode('utf-8', 'replace').splitlines():
        at = line.find(STOP_MARK)
        if at >= 0:
            rest = line[at + len(STOP_MARK):].strip().split(None, 1)
            stop = rest[0] if rest and _STOP_CODE.fullmatch(rest[0]) else 'UNKNOWN_STOP'
            detail = rest[1] if len(rest) > 1 else ''
    report = problem = None
    if returncode == 0:
        lines = [line for line in stdout.decode('utf-8', 'replace').splitlines() if line.strip()]
        if not lines:
            problem = '標準輸出沒有 JSON'
        else:
            try:
                report = json.loads(lines[-1])
            except ValueError as e:
                problem = 'JSON：%s' % e
            if report is not None and not isinstance(report, dict):
                report, problem = None, 'JSON 不是物件'
    return report, stop, detail, problem


def _device_calls(backend, completed, features=4):
    return 0 if backend == 'cpu' else 1 + completed * ((features + 3) // 4)   # client.cpp:303


def report_mismatches(report, kind, backend, plan):
    """[(field, expected, actual)] for every requirement on the client JSON that fails."""
    flags = plan['flags']
    synthetic = kind != 'form'

    def same(want):
        return lambda v: type(v) is type(want) and v == want

    checks = [('status', PASS_STATUS[backend], same(PASS_STATUS[backend])), ('backend', backend, same(backend)),
              ('model', MODEL_ID, same(MODEL_ID)), ('features', 4, same(4)), ('rows', plan['rows'], same(plan['rows'])),
              ('mode', flags.mode, same(flags.mode)), ('synthetic_input', synthetic, same(synthetic)),
              ('automatic_retries', 0, same(0)), ('real_PL', backend == 'pl', same(backend == 'pl'))]
    completed = report.get('completed_requests')
    if kind == 'throughput':
        checks.append(('completed_requests', '1..%d' % plan['requests'],
                       lambda v: _is_int(v) and 1 <= v <= plan['requests']))
        for key, want in (('slots', flags.slots), ('duplex', flags.duplex), ('hash_io', flags.hash_io),
                          ('decrypt_thread', flags.decrypt_thread), ('inflight', flags.inflight),
                          ('io_cpu', flags.io_cpu), ('io_isolate', flags.io_isolate),
                          ('read_every', flags.read_every)):
            checks.append((key, want, same(want)))
    else:
        checks.append(('completed_requests', plan['requests'], same(plan['requests'])))
    if _is_int(completed):
        calls = _device_calls(backend, completed)
        checks.append(('device_calls', calls, same(calls)))
    if synthetic:
        checks.append(('max_public_error', '<= %g' % PUBLIC_GATE,
                       lambda v: type(v) in (int, float) and math.isfinite(v) and 0 <= v <= PUBLIC_GATE))
        if kind == 'gate':
            checks.append(('requests_over_5e4', 0, same(0)))
            checks.append(('checked_public_scores', plan['rows'] * plan['requests'],
                           same(plan['rows'] * plan['requests'])))
    if backend == 'pl':
        checks.append(('PL_hz', '%d..%d' % (PL_HZ_MIN, PL_HZ_MAX), lambda v: _is_int(v) and PL_HZ_MIN <= v <= PL_HZ_MAX))
    bad = []
    for field, want, ok in checks:
        value = report.get(field, '<missing>')
        if not ok(value):
            bad.append((field, want, value))
    trace = report.get('trace')
    if not bad:
        if not isinstance(trace, list) or len(trace) != completed:
            bad.append(('trace', '%s entries' % completed, len(trace) if isinstance(trace, list) else trace))
        elif not all(isinstance(t, dict) and _is_hex64(t.get('request_sha256'))
                     and _is_hex64(t.get('response_payload_sha256')) for t in trace):
            bad.append(('trace', 'request/response SHA-256', 'malformed'))
    return bad


def _event_report(report):
    """The client JSON for an event; a long trace (throughput) is cut to its first entries."""
    if not isinstance(report, dict):
        return None
    out = dict(report)
    trace = report.get('trace')
    if isinstance(trace, list) and len(trace) > TRACE_EVENT_LIMIT:
        out['trace'] = trace[:TRACE_EVENT_LIMIT]
        out['trace_truncated'] = True
        out['trace_count'] = len(trace)
    return out


def _trace_digest(trace):
    text = ''.join('%s,%s\n' % (t.get('request_sha256'), t.get('response_payload_sha256')) for t in trace)
    return hashlib.sha256(text.encode('ascii', 'replace')).hexdigest()


def install_stop_signals(on_signal):
    """Each of STOP_SIGNALS (SIGTERM, SIGINT, SIGHUP, SIGQUIT, SIGUSR1, SIGUSR2) calls on_signal(signum) and never
    interrupts the run in flight (main thread only)."""
    def handler(signum, frame):
        on_signal(signum)
    for name in STOP_SIGNALS:
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, handler)


# ------------------------------------------------------------------------------------------------ the gateway
class Gateway:
    """Runs forms and public gates through the unchanged client, one at a time, and emits the events.

    The command line calls run_form / run_gate / run_throughput (synchronous); the HTTP API calls submit(), which
    returns at once and runs the form on a worker thread. progress, when set, is called as
    progress(pid, emit, cfg) right after the client started and must return a stop() callable (EVENTS.md)."""

    def __init__(self, cfg, inference=None, events=None, progress=None):
        self.cfg = cfg
        self.inference = inference if inference is not None else cdc.Inference.load(cfg['model_json'],
                                                                                  cfg['form_json'])
        self.events = events if events is not None else records.EventLog()
        self.progress = progress
        self.overdue_s = None           # None: 3 x timeout + 30 s (+ the throughput window)
        self.getpid = os.getpid
        # retain_private (default false): keep <workdir>/private/<run>.json on tmpfs until disclose or a reboot
        self.private = (records.PrivateStore(os.path.join(cfg['workdir'], 'private'))
                        if cfg.get('retain_private', False) is True else None)
        self.rehearsal = False          # httpapi.serve sets it for --rehearsal-listen 0.0.0.0 (the PC rehearsal)
        self.lock_path = PL_LOCK_PATH
        self.stop_signal = None
        self.current = None
        self._stopping = False
        self._busy = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._digests = {}

    # ---------------------------------------------------------------------------------------- state and events
    @property
    def stopping(self):
        return self._stopping

    def request_stop(self, signum=None):
        """Stop accepting work; a run in flight finishes normally (its client is never signalled)."""
        self._stopping = True
        if self.stop_signal is None and signum is not None:
            self.stop_signal = signum

    def wait_idle(self, timeout=None):
        return self._idle.wait(timeout)

    def emit(self, run, etype, cls, data):
        return self.events.emit(run, etype, cls, data)

    def error(self, run, code, fatal=True, **ctx):
        message = describe(code, **ctx)
        self.emit(run, 'error', 'public', {'code': code, 'message': message, 'fatal': fatal, 'stop': False})
        return message

    @staticmethod
    def new_run():
        return 'r' + secrets.token_hex(6)

    def file_sha256(self, path):
        st = os.stat(path)
        key = (path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if key not in self._digests:
            self._digests[key] = file_sha256(path)
        return self._digests[key]

    def status(self):
        cfg, inf = self.cfg, self.inference
        request_bytes, response_bytes = inf.frame_bytes()
        return {
            'v': 1, 'busy': self._busy.locked(), 'run': self.current, 'stopping': self._stopping,
            'default_backend': cfg['default_backend'], 'backends': list(cfg['backends']),
            'retain_private': self.private is not None,
            'model': {'model_id': MODEL_ID, 'sha256': inf.model_sha256, 'features': list(inf.features),
                      'profile_hex': cfg['profile_hex']},
            'cloud': {'host': cfg['host'], 'port': cfg['port'], 'peer_name': cfg['peer_name'], 'protocol': 'TLS 1.3',
                      'mutual_authentication': True, 'certificate_pinning': True},
            'sizes': {'request_frame_bytes': request_bytes, 'response_frame_bytes': response_bytes,
                      'public_key_bytes': cdc.CT_BYTES},
            'form': inf.form, 'last_seq': self.events.last_seq(),
        }

    # -------------------------------------------------------------------------------------------- admission
    def _check_backend(self, backend, expect_stop=None):
        if backend not in self.cfg['backends']:
            raise GatewayError('BACKEND_NOT_ALLOWED', backend=backend, allowed='、'.join(self.cfg['backends']))
        if expect_stop is not None and (expect_stop not in EXPECTABLE_STOPS or backend != 'pl'):
            raise GatewayError('EXPECT_INVALID')

    def dma_nodes(self):
        """The configured DMA node and, when gateway.json moved it, the fixed DMA_NODE as well."""
        nodes = [self.cfg['dma_node']]
        if DMA_NODE not in nodes:
            nodes.append(DMA_NODE)
        return nodes

    def _admit(self, backend, expect_stop=None):
        """Every check that does not depend on the form; returns the held PlLock."""
        cfg = self.cfg
        if self._stopping:
            raise GatewayError('GATEWAY_STOPPING')
        if backend == 'pl' and self.rehearsal:
            markers = board_markers(cfg)
            if markers:
                raise GatewayError('REHEARSAL_PL_ON_BOARD', node='、'.join(markers))
        if os.geteuid() != 0:
            raise GatewayError('ROOT_REQUIRED')
        if backend == 'pl' and self.getpid() == 1:
            raise GatewayError('GATEWAY_PID1')
        if expect_stop == 'OWNER_CREATE':
            present = [node for node in self.dma_nodes() if os.path.exists(node)]
            if present:
                raise GatewayError('EXPECT_REQUIRES_NO_DEVICE', node=present[0])
        if not self._busy.acquire(blocking=False):
            raise GatewayError('BUSY')
        try:
            try:
                prepare_workdir(cfg['workdir'])
            except OSError as e:
                raise GatewayError('WORKDIR_UNSAFE', path=cfg['workdir'], why=_oserror(e))
            lock = PlLock(self.lock_path)
            try:
                acquired = lock.acquire()
            except OSError as e:
                raise GatewayError('PL_LOCK_UNSAFE', lock=lock.path, why=_oserror(e))
            if not acquired:
                raise GatewayError('PL_LOCK_BUSY', lock=lock.path)
            if backend == 'pl':
                holders = dma_holders(self.dma_nodes())
                if holders:
                    lock.release()
                    raise GatewayError('PL_DEVICE_BUSY', node=cfg['dma_node'], pids=', '.join(map(str, holders)))
        except BaseException:
            self._busy.release()
            raise
        self._idle.clear()
        if self._stopping:
            # request_stop() came after the first check: its caller may already have seen _idle set and returned
            # from wait_idle(), so this run must not start.
            self._release(lock)
            raise GatewayError('GATEWAY_STOPPING')
        return lock

    def _release(self, lock):
        lock.release()
        self.current = None
        self._idle.set()
        self._busy.release()

    def _refused(self, run, code):
        message = self.inference.refusal_message(code)
        self.emit(run, 'form.refused', 'owner', {'code': code, 'message': message,
                                                 'field': self.inference.refusal_field(code),
                                                 'note': self.inference.form.get('refusal_note')})
        return message

    # ------------------------------------------------------------------------------------------- entry points
    def submit(self, form, backend=None):
        """HTTP API: (status, body). 202 {run}; 400 {code, message[, run]}; 409 or 503 {code, message}."""
        backend = self.cfg['default_backend'] if backend is None else backend
        try:
            self._check_backend(backend)
        except GatewayError as e:
            return e.http_status, e.body()
        run = self.new_run()
        code = self.inference.refusal(form)
        if code:
            return 400, {'code': code, 'message': self._refused(run, code), 'run': run}
        try:
            lock = self._admit(backend)
        except GatewayError as e:
            return e.http_status, e.body()
        try:
            # Non-daemon whatever thread calls submit(): Python then waits for the run, and so for its client, before
            # the process can end (with systemd KillMode=mixed a client left behind would be killed).
            worker = threading.Thread(target=self._form_worker, args=(run, backend, form, lock),
                                      name='edgefhe-run-' + run, daemon=False)
            worker.start()
        except BaseException:
            self._release(lock)
            raise
        return 202, {'run': run}

    def _form_worker(self, run, backend, form, lock):
        try:
            self._form(run, backend, form, None)
        except Exception as e:
            self.error(run, 'INTERNAL', why=_oserror(e))
        finally:
            self._release(lock)

    def run_form(self, form, backend=None, expect_stop=None):
        """Command line: one form, synchronously. Raises GatewayError when the run cannot start."""
        backend = self.cfg['default_backend'] if backend is None else backend
        self._check_backend(backend, expect_stop)
        run = self.new_run()
        code = self.inference.refusal(form)
        if code:
            message = self._refused(run, code)
            return {'run': run, 'kind': 'form', 'backend': backend, 'ok': False, 'refused': code, 'code': code,
                    'message': message, 'stop': None, 'result': None, 'report': None}
        lock = self._admit(backend, expect_stop)
        try:
            return self._form(run, backend, form, expect_stop)
        finally:
            self._release(lock)

    def run_gate(self, backend, expect_stop=None):
        """Public oracle gate: synthetic --rows 2048 --requests 20, serial, base contract (client.cpp:138,276)."""
        self._check_backend(backend, expect_stop)
        plan = {'kind': 'gate', 'rows': GATE_ROWS, 'requests': GATE_REQUESTS, 'window_ms': 0,
                'flags': permit.FORM_FLAGS}
        return self._synthetic(backend, plan, expect_stop)

    def run_throughput(self, backend, seconds):
        """Synthetic pipeline window with the best measured flags (permit.PIPELINE_BEST) and the layered contract."""
        self._check_backend(backend)
        plan = {'kind': 'throughput', 'rows': THROUGHPUT_ROWS, 'requests': THROUGHPUT_REQUESTS,
                'window_ms': int(seconds) * 1000, 'flags': permit.PIPELINE_BEST}
        return self._synthetic(backend, plan, None)

    def _synthetic(self, backend, plan, expect_stop):
        run = self.new_run()
        lock = self._admit(backend, expect_stop)
        try:
            return self._execute(run, backend, plan, expect_stop)
        finally:
            self._release(lock)

    def _form(self, run, backend, form, expect_stop):
        inf = self.inference
        values = inf.canonical(form)
        canonical_json = inf.canonical_json(form)
        salt_hex, commitment = records.new_commitment(canonical_json)
        csv_text = inf.csv_text(form)
        x = inf.csv_values(csv_text)
        z = inf.z_values(x)
        contributions = inf.contributions(x)
        self.emit(run, 'form.accepted', 'public', {'kind': 'form', 'backend': backend, 'model': MODEL_ID,
                                                   'model_sha256': inf.model_sha256, 'commitment': commitment})
        self.emit(run, 'form.accepted', 'owner', {'features': list(inf.features), 'values': values, 'x': x, 'z': z,
                                                  'contributions': contributions, 'bias': inf.bias,
                                                  'commitment': commitment})
        plan = {'kind': 'form', 'rows': 1, 'requests': 1, 'window_ms': 0, 'flags': permit.FORM_FLAGS,
                'csv_text': csv_text, 'x': x}
        private = {'salt_hex': salt_hex, 'canonical_json': canonical_json, 'form': values, 'csv_text': csv_text,
                   'x': x, 'z': z, 'contributions': contributions}
        return self._execute(run, backend, plan, expect_stop, commitment=commitment, private=private)

    # ------------------------------------------------------------------------------------------------- core
    def client_argv(self, backend, tail):
        cfg = self.cfg
        return [cfg['client'], '--ca', cfg['ca'], '--cert', cfg['cert'], '--key', cfg['key'],
                '--peer-name', cfg['peer_name'], '--peer-sha256', cfg['peer_sha256'], '--host', cfg['host'],
                '--port', str(cfg['port']), '--timeout-ms', str(cfg['timeout_ms']), '--backend', backend,
                '--workers', str(cfg['workers']), '--plugin', cfg['plugin'], '--model', MODEL_ID] + list(tail)

    def overdue_seconds(self, plan):
        if self.overdue_s is not None:
            return self.overdue_s
        return 3 * self.cfg['timeout_ms'] / 1000.0 + 30 + plan['window_ms'] / 1000.0

    def _execute(self, run, backend, plan, expect_stop=None, commitment=None, private=None):
        cfg, inf = self.cfg, self.inference
        kind, flags = plan['kind'], plan['flags']
        outcome = {'run': run, 'kind': kind, 'backend': backend, 'ok': False, 'code': None, 'message': None,
                   'stop': None, 'stop_detail': None, 'report': None, 'result': None, 'refused': None,
                   'irq_delta': None, 'irq_expected': None, 'returncode': None, 'wall_ns': None,
                   'overdue': False, 'commitment': commitment, 'request_sha256': None,
                   'response_payload_sha256': None, 'client_sha256': None, 'retained_private': False, 'wiped': None}
        self.current = run
        run_dir = os.path.join(cfg['workdir'], run)
        input_path = output_path = permit_path = permit_fd = client = None
        run_dir_made = False
        try:
            # 1. private files, contract and permit; no process exists yet
            try:
                os.mkdir(run_dir, 0o700)
                run_dir_made = True
                os.chmod(run_dir, 0o700)
                tail = ['--mode', flags.mode]
                private_paths = None
                if kind == 'form':
                    input_path = os.path.join(run_dir, 'in.csv')
                    output_path = os.path.join(run_dir, 'score.txt')
                    write_private(input_path, plan['csv_text'])
                    private_paths = (input_path, output_path)
                    tail += ['--input', input_path, '--scores-output', output_path]
                else:
                    tail += ['--rows', str(plan['rows']), '--requests', str(plan['requests'])]
                    if plan['window_ms']:
                        tail += ['--window-ms', str(plan['window_ms'])]
                tail += flags.argv()
                contracts = permit.invocation_contract(cfg, MODEL_ID, plan['rows'], plan['requests'],
                                                       plan['window_ms'], flags, private_paths)
                pass_fds = ()
                if backend == 'pl':
                    holders = dma_holders(self.dma_nodes())
                    if holders:
                        raise GatewayError('PL_DEVICE_BUSY', node=cfg['dma_node'], pids=', '.join(map(str, holders)))
                    exe_dev, exe_ino = permit.executable_identity(cfg['client'])
                    text = permit.permit_text(permit.read_boot_id(), self.getpid(), exe_dev, exe_ino,
                                              contracts['contract'])
                    permit_path = os.path.join(run_dir, 'permit')
                    permit_fd = permit.write_permit(permit_path, text)
                    tail += ['--permit-fd', str(permit_fd)]
                    pass_fds = (permit_fd,)
                argv = self.client_argv(backend, tail)
                outcome['client_sha256'] = self.file_sha256(cfg['client'])
            except permit.PermitError as e:
                raise GatewayError('RUN_SETUP', why=str(e))
            except OSError as e:
                raise GatewayError('RUN_SETUP', why=_oserror(e))

            # 2. the client: from Popen on, nothing below skips the wait, and nothing ever signals it (ClientProcess
            #    does not raise once Popen has returned, so `client` is set for the finally clause below)
            irq_before = irq_snapshot(cfg['proc_interrupts'])
            overdue_s = self.overdue_seconds(plan)
            try:
                client = ClientProcess(argv, pass_fds)
            except (OSError, subprocess.SubprocessError) as e:
                raise GatewayError('CLIENT_SPAWN', why=_oserror(e))
            overdue = []

            def report_overdue():
                overdue.append(True)
                self.error(run, 'CLIENT_OVERDUE', fatal=False, seconds='%g' % overdue_s)

            dog = Watchdog(overdue_s, report_overdue)
            stop_progress = None
            try:
                self.emit(run, 'client.spawned', 'public', {
                    'kind': kind, 'backend': backend, 'pid': client.pid, 'argv': argv, 'permit': backend == 'pl',
                    'contract': contracts['contract'], 'client_sha256': outcome['client_sha256'],
                    'overdue_s': overdue_s, 'rows': plan['rows'], 'requests': plan['requests'],
                    'window_ms': plan['window_ms'], 'flags': flags.as_dict(),
                    'irq_before': irq_counts(irq_before), 'dma_lines': {k: v['desc'] for k, v in irq_before.items()}})
                if self.progress is not None:
                    try:
                        stop_progress = self.progress(client.pid,
                                                      lambda etype, data: self.emit(run, etype, 'public', data), cfg)
                    except Exception as e:
                        self.error(run, 'PROGRESS_SAMPLER', fatal=False, why=_oserror(e))
            finally:
                returncode, stdout, stderr, wall_ns = client.wait()
                dog.stop()
                if stop_progress is not None:
                    try:
                        stop_progress()
                    except Exception as e:
                        self.error(run, 'PROGRESS_SAMPLER', fatal=False, why=_oserror(e))
            irq_after = irq_snapshot(cfg['proc_interrupts'])

            # 3. what the client reported
            report, stop, detail, problem = parse_client_output(returncode, stdout, stderr)
            outcome.update(returncode=returncode, wall_ns=wall_ns, overdue=bool(overdue), report=report, stop=stop,
                           stop_detail=detail)
            self.emit(run, 'client.exited', 'public', {
                'returncode': returncode, 'wall_ns': wall_ns, 'overdue': bool(overdue),
                'status': report.get('status') if report else None, 'stop': stop, 'stop_detail': detail,
                'report': _event_report(report)})
            delta = irq_delta(irq_before, irq_after)
            irq_problem, expected = None, None
            if backend == 'pl' and report is not None and not expect_stop:
                calls = report.get('device_calls')
                expected = calls if _is_int(calls) else None
                if len(irq_after) != DMA_LINES or set(irq_before) != set(irq_after):
                    irq_problem = GatewayError('IRQ_LINES', count=len(irq_after), expected=DMA_LINES)
                elif expected is None or any(d != expected for d in delta.values()):
                    irq_problem = GatewayError('IRQ_DELTA_MISMATCH', calls=expected,
                                               delta='/'.join('+%d' % d for d in delta.values()))
            outcome.update(irq_delta=delta, irq_expected=expected)
            self.emit(run, 'irq.delta', 'public', {
                'backend': backend, 'delta': delta, 'before': irq_counts(irq_before), 'after': irq_counts(irq_after),
                'expected': expected, 'passed': None if expected is None and irq_problem is None else irq_problem is None,
                'lines': {k: v['desc'] for k, v in irq_after.items()}})

            if expect_stop:
                if returncode != 0 and stop == expect_stop:
                    outcome.update(ok=True, code=stop, message=describe(stop))
                    return outcome
                actual = stop or (report.get('status') if report else None) or ('結束碼 %d' % returncode)
                raise GatewayError('EXPECTATION_UNMET', expected=expect_stop, actual=actual)
            if returncode != 0 or report is None:
                if stop:
                    raise GatewayError(stop, host=cfg['host'], port=cfg['port'], timeout_ms=cfg['timeout_ms'])
                if returncode < 0:
                    raise GatewayError('CLIENT_SIGNALLED', signal=_signal_name(-returncode))
                if returncode != 0:
                    raise GatewayError('CLIENT_EXIT', returncode=returncode)
                raise GatewayError('CLIENT_OUTPUT', why=problem)
            mismatches = report_mismatches(report, kind, backend, plan)
            if mismatches:
                field, want, got = mismatches[0]
                raise GatewayError('CLIENT_REPORT_MISMATCH', field=field, expected=want, actual=got)
            trace = report['trace']
            if kind == 'form':
                outcome['request_sha256'] = trace[0]['request_sha256']
                outcome['response_payload_sha256'] = trace[0]['response_payload_sha256']
            if irq_problem is not None:
                raise irq_problem

            # 4. the owner result of a form
            if kind == 'form':
                try:
                    logit_ckks = read_score(output_path)
                except (OSError, ValueError, UnicodeDecodeError) as e:
                    raise GatewayError('SCORE_FILE', why=_oserror(e))
                logit_plain = inf.reference(plan['x'])
                result = inf.evaluate(logit_ckks, logit_plain)
                request_bytes, response_bytes = inf.frame_bytes()
                result['evidence'] = {
                    'commitment': commitment, 'request_sha256': outcome['request_sha256'],
                    'response_payload_sha256': outcome['response_payload_sha256'],
                    'device_calls': report['device_calls'], 'PL_hz': report.get('PL_hz'), 'irq_delta': delta,
                    'request_frame_bytes': request_bytes, 'response_frame_bytes': response_bytes}
                outcome['result'] = result
                self.emit(run, 'result', 'owner', result)
                if not result['passed']:
                    raise GatewayError('VERIFICATION_FAILED', text=result['message'])
            outcome.update(ok=True)
        except GatewayError as e:
            outcome.update(ok=False, code=e.code, message=e.message())
            self.emit(run, 'error', 'public', {'code': e.code, 'message': outcome['message'], 'fatal': True,
                                               'stop': e.code == outcome['stop']})
        except Exception as e:
            outcome.update(ok=False, code='INTERNAL', message=describe('INTERNAL', why=_oserror(e)))
            self.emit(run, 'error', 'public', {'code': 'INTERNAL', 'message': outcome['message'], 'fatal': True,
                                               'stop': False})
        finally:
            if client is not None and not client.exited:
                client.wait()
            wiped = self._cleanup(run, run_dir, run_dir_made, permit_fd, permit_path, (input_path, output_path))
            try:
                outcome['retained_private'] = self._records(outcome, plan, private)
            except Exception as e:      # never expected (_records reports its own errors); wiped must still follow
                outcome['retained_private'] = self._private_exists(run)
                self.error(run, 'RECORD_WRITE', fatal=False, path=cfg['public_log'], why=_oserror(e))
            outcome['wiped'] = wiped
            if wiped is not None:       # the last event of the run (EVENTS.md section 3)
                wiped['retained_private'] = outcome['retained_private']
                self.emit(run, 'wiped', 'public', wiped)
            self.current = None
        return outcome

    def _private_exists(self, run):
        try:
            return self.private is not None and os.path.isfile(self.private.path(run))
        except (OSError, ValueError):
            return False

    def _cleanup(self, run, run_dir, run_dir_made, permit_fd, permit_path, private_files):
        """Close and unlink the permit, overwrite and unlink the private files, remove the run directory; returns the
        data of the wiped event (None when no run directory was made), which _execute emits after the records."""
        wiped, problems = [], []
        if permit_fd is not None:
            try:
                os.close(permit_fd)
            except OSError:
                pass
        if permit_path is not None:
            try:
                os.unlink(permit_path)
                wiped.append('permit')
            except FileNotFoundError:
                pass
            except OSError as e:
                problems.append((permit_path, e))
        for path in private_files:
            if path is None:
                continue
            try:
                if wipe_file(path):
                    wiped.append(os.path.basename(path))
            except OSError as e:
                problems.append((path, e))
        removed = False
        if run_dir_made:
            try:
                os.rmdir(run_dir)
                removed = True
            except OSError as e:
                problems.append((run_dir, e))
        for path, e in problems:
            self.error(run, 'WIPE_FAILED', fatal=False, path=path, why=_oserror(e))
        if not run_dir_made:
            return None
        return {'files': wiped, 'run_dir_removed': removed, 'problems': [p for p, _ in problems]}

    def _records(self, outcome, plan, private):
        """Append the public record; save the private record of a form when retain_private is on. Returns True only
        when a private record of this run now exists on tmpfs (the wiped event's retained_private). Never raises: a
        failure of either record is a non-fatal RECORD_WRITE error, and a private file whose write failed is wiped
        (records._write_private), so False is then exact."""
        cfg = self.cfg
        report = outcome['report'] if isinstance(outcome['report'], dict) else {}
        code = outcome['code']
        error = code if code and code != outcome['stop'] else None
        t_board_epoch = round(time.time(), 3)
        try:
            fields = self._public_fields(outcome, plan, report, error, t_board_epoch)
            records.append_jsonl(cfg['public_log'], records.public_record(fields))
        except Exception as e:
            self.error(outcome['run'], 'RECORD_WRITE', fatal=False, path=cfg['public_log'], why=_oserror(e))
        # A private record exists only to open the commitment of a request that reached the wire (disclose).
        if outcome['kind'] != 'form' or private is None or self.private is None or not outcome['request_sha256']:
            return False
        try:
            result = outcome['result'] or {}
            record = {'schema': records.PRIVATE_SCHEMA, 'run': outcome['run'], 'kind': 'form',
                      'backend': outcome['backend'], 'model': MODEL_ID, 'model_sha256': self.inference.model_sha256,
                      'request_sha256': outcome['request_sha256'],
                      'response_payload_sha256': outcome['response_payload_sha256'],
                      'commitment': outcome['commitment'], 'status': report.get('status'), 'stop': outcome['stop'],
                      'error': error, 't_board_epoch': t_board_epoch}
            record.update(private)
            for key in ('logit_ckks', 'logit_plain', 'abs_error', 'max_abs_error', 'passed', 'level', 'boundary',
                        'p_balanced'):
                record[key] = result.get(key)
            self.private.save(record)
        except Exception as e:
            self.error(outcome['run'], 'RECORD_WRITE', fatal=False, path=self.private.directory, why=_oserror(e))
            return self._private_exists(outcome['run'])
        return True

    def _public_fields(self, outcome, plan, report, error, t_board_epoch):
        cfg = self.cfg
        trace = report.get('trace') if isinstance(report.get('trace'), list) else []
        fields = {
            'v': 1, 'kind': outcome['kind'], 'run': outcome['run'], 't_board_epoch': t_board_epoch,
            'backend': outcome['backend'], 'model': MODEL_ID, 'model_sha256': self.inference.model_sha256,
            'profile': cfg['profile_hex'], 'host': cfg['host'], 'port': cfg['port'],
            'client_sha256': outcome['client_sha256'], 'status': report.get('status'), 'stop': outcome['stop'],
            'error': error, 'returncode': outcome['returncode'],
            'overdue': outcome['overdue'], 'wall_ns': outcome['wall_ns'], 'irq_delta': outcome['irq_delta'],
            'irq_expected': outcome['irq_expected'], 'rows': plan['rows'], 'requests': plan['requests'],
            'window_ms': plan['window_ms'], 'mode': plan['flags'].mode,
        }
        for key in ('device_calls', 'PL_hz', 'completed_requests', 'input_read_ns', 'cold_context_and_TLS_ns',
                    'elapsed_ns', 'records_per_s', 'max_public_error', 'requests_over_5e4'):
            if key in report:
                fields[key] = report[key]
        if outcome['kind'] == 'form':
            result = outcome['result']
            fields.update(commitment=outcome['commitment'], request_sha256=outcome['request_sha256'],
                          response_payload_sha256=outcome['response_payload_sha256'],
                          verified=result['passed'] if result else None)
            if trace:
                for key in ('prepare_ns', 'TLS_roundtrip_and_validate_ns', 'decrypt_ns'):
                    fields[key] = trace[0].get(key)
        elif outcome['kind'] == 'gate':
            fields['trace'] = trace
        else:
            fields.update(flags=plan['flags'].as_dict(), trace_count=len(trace), trace_sha256=_trace_digest(trace))
        return fields
