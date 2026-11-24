"""KV260 serial-console file tools (PC side): push and fetch files, the board CSR round trip, and the board's public log
and fictional disclosures after the capture is sealed.

usage (repository root; KV_PW holds the board console password, set only in this terminal session):
  python -B demo/pc/board_files.py push-file SRC DST            [--port COM6] [--user ubuntu]
  python -B demo/pc/board_files.py push-tree DIR DST
  python -B demo/pc/board_files.py fetch-file SRC DST
  python -B demo/pc/board_files.py fetch-public RUN_DIR [--public-log PATH] [--disclosure-dir PATH]
  python -B demo/pc/board_files.py csr-roundtrip --pki DIR [--board-pki ~/edgefhe-demo/pki] [--bash PATH]

Every transfer goes through energy_probe/kv260/kv_console.py (the committed version: HISTFILE unset at login, the
password masked in every error text) over the micro-USB console, never over the network:
  push-file     kv_console.put_file (base64 in 900-character lines); the board's SHA-256 must equal the local one
  push-tree     the tree as one tar.gz (without __pycache__, local, runs, build, .git and the files that demo/.gitignore
                keeps out of git: *.pyc *.pcap *.pcapng *.log *.key *.pem *.csr *.srl *.pin), put_file, tar -x at DST,
                then sha256sum -c of every file on the board; files already at DST that the tree lacks stay in place
  fetch-file    base64 on the board, SHA-256 and size compared on the PC; a line damaged by a kernel message makes the
                attempt fail and the transfer is repeated (3 attempts). Key material and the gateway's private records
                never leave the board (BOARD_FILES_REFUSED code=KEY_STAYS_ON_BOARD or PRIVATE_STAYS_ON_BOARD, exit 2):
                refused are names ending in .key .p12 .pfx .p8 .pk8 or containing private, keylog, sslkeylog or
                tls_keys; anything under a pki directory except the public ca.pem, client.pem, client.csr and
                server.pin; anything under .ssh or .gnupg; anything under the gateway workdir /run/edgefhe-demo or a
                private directory. The rules are applied to the path as given (before the console is opened) and again
                to its resolved path on the board (readlink -f, so a link does not get around them); the board also
                checks that the file holds no PEM private key ('PRIVATE KEY-----') before a single byte is sent, and
                the PC checks the received bytes once more. A board that cannot run the check refuses (KEY_CHECK).
  fetch-public  only after the capture is sealed (RUN_DIR/capture.sha256 must match capture.pcapng, efhe_seal.py):
                sudo reads /var/log/edgefhe-demo/public.jsonl and /var/lib/edgefhe-demo/disclosure/*.json into
                RUN_DIR/board_public.jsonl and RUN_DIR/disclosure/, as efhe_seal.py --verify expects them
  csr-roundtrip fetches the board CSR (the board key stays on the board; the CSR passes the same key check), signs it
                with demo/pki/make_demo_pki.sh sign-client in Git Bash (never WSL), pushes ca.pem, client.pem and
                server.pin back, and checks on the board that client.pem matches client.key and carries the printed pin
Board paths must be absolute or start with ~/ and use only letters, digits and . _ + @ - (no quoting is needed then).
PC outputs stay outside the repository (BOARD_FILES_REFUSED code=OUTPUT_IN_REPOSITORY, exit 2, before the console is
opened): the fetch-file destination, the fetch-public run directory and the csr-roundtrip PKI directory. A path is
inside when efhe_verify.inside_repository says so (links resolved, case folded), or when the path or its resolved
form has an existing ancestor that is the repository folder itself (os.path.samestat), which also covers spellings
that realpath keeps, such as \\\\?\\C:\\... and \\\\localhost\\C$\\....
Markers: BOARD_FILES_PUSHED, BOARD_FILES_TREE_PUSHED, BOARD_FILES_FETCHED, BOARD_FILES_PUBLIC_FETCHED,
EDGEFHE_DEMO_CLIENT_SIGNED and BOARD_FILES_CSR_ROUNDTRIP_PASS on success; BOARD_FILES_REFUSED code= (exit 2) and
BOARD_FILES_FAILED code= (exit 1). Python 3.10 standard library plus pyserial (through kv_console).
"""
import argparse
import base64
import binascii
import collections
import hashlib
import inspect
import io
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.dirname(HERE)
ROOT = os.path.dirname(DEMO)
KV_CONSOLE_DIR = os.path.join(ROOT, 'energy_probe', 'kv260')
EVIDENCE_DIR = os.path.join(DEMO, 'evidence')          # efhe_verify.inside_repository (the evidence tools' guard)
PKI_SCRIPT = os.path.join(DEMO, 'pki', 'make_demo_pki.sh')
PUBLIC_LOG = '/var/log/edgefhe-demo/public.jsonl'
DISCLOSURE_DIR = '/var/lib/edgefhe-demo/disclosure'
BOARD_PKI = '~/edgefhe-demo/pki'
CAPTURE, SEAL = 'capture.pcapng', 'capture.sha256'
PUBLIC_OUT, DISCLOSURE_OUT = 'board_public.jsonl', 'disclosure'
REMOTE = re.compile(r'(?:~/|/)(?:[A-Za-z0-9_.+@-]+/)*[A-Za-z0-9_.+@-]+')
NAME = re.compile(r'[A-Za-z0-9_.+@-]+')
B64_LINE = re.compile(r'[A-Za-z0-9+/=]+')
HEX64 = re.compile(r'[0-9a-f]{64}')
DISCLOSURE_NAME = re.compile(r'(?:[0-9a-f]{64}|r[0-9a-f]{12})\.json')
SKIP_DIRS = frozenset(('__pycache__', 'local', 'runs', 'build', '.git', 'node_modules'))
SKIP_SUFFIXES = ('.pyc', '.pcap', '.pcapng', '.log', '.key', '.pem', '.csr', '.srl', '.pin', '.tmp')
# fetch-file: what never leaves the board (key material and the gateway's private records; docstring above)
KEY_SUFFIXES = ('.key', '.p12', '.pfx', '.p8', '.pk8')
KEY_WORDS = ('private', 'keylog', 'sslkeylog', 'tls_keys')
KEY_DIRS = frozenset(('.ssh', '.gnupg'))
PKI_DIR = 'pki'
PKI_PUBLIC = frozenset(('ca.pem', 'client.pem', 'client.csr', 'server.pin'))
PRIVATE_ROOTS = ('/run/edgefhe-demo',)
PEM_PRIVATE = 'PRIVATE KEY-----'        # every PEM private key: PRIVATE, EC PRIVATE, ENCRYPTED PRIVATE, OPENSSH PRIVATE
FETCH_MAX = 64 * 1024 * 1024
PUSH_MAX = 32 * 1024 * 1024
CSR_MAX = 16384
ATTEMPTS = 3
CONSOLE_BYTES_PER_S = 6000.0        # put_file and base64 output over 115,200 baud, with the command round trips
EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2

TEXT = {
    'KV_CONSOLE': '本程式無法載入已提交、具密碼遮蔽功能的 kv_console.py（%s）。',
    'KV_PW_MISSING': ('本程式需要以環境變數 KV_PW 提供 KV260 主控台使用者的密碼；操作人員應只在目前的終端機工作階段設定 '
                      'KV_PW。本程式不接受以參數傳入密碼，也不會儲存密碼。'),
    'PORT_OPEN': ('本程式無法開啟序列埠 %s（%s）；操作人員應確認傳輸線已連接，且沒有其他程式（例如擁有者橋接程式）'
                  '占用此序列埠。'),
    'LOGIN': '本程式無法登入 KV260 主控台（%s）。',
    'REMOTE_PATH': 'KV260 上的路徑「%s」不符合規則：必須是絕對路徑或以 ~/ 開頭，且只使用英數字與 . _ + @ - 字元。',
    'LOCAL_MISSING': '本程式找不到本機檔案或目錄：%s。',
    'LOCAL_TOO_LARGE': '本機檔案 %s 有 %d 位元組，超過序列傳輸的上限 %d 位元組。',
    'OUTPUT_EXISTS': '輸出位置已存在：%s。本程式不覆寫既有檔案；操作人員應先確認並移除後再執行。',
    'OUTPUT_IN_REPOSITORY': ('輸出位置 %s 位於 repository 之內；本程式不把取自 KV260 的檔案、板端公開紀錄、揭露檔或 PKI '
                             '檔案寫入 repository，操作人員應改用 repository 以外的位置（例如 C:\\EdgeFHE_evidence\\<場次> '
                             '或 C:\\EdgeFHE_demo\\pki）。'),
    'PUSH_HASH': 'KV260 上 %s 的 SHA-256 與本機檔案不一致；本程式重試後仍不一致。',
    'TREE_CHECK': 'KV260 上解開後的檔案未全部通過 SHA-256 檢查（%s）。',
    'TREE_NAME': '目錄樹中的名稱「%s」含有序列傳輸不接受的字元；本程式未傳送任何檔案。',
    'TREE_EMPTY': '目錄 %s 中沒有可傳送的檔案。',
    'REMOTE_MISSING': 'KV260 上沒有可讀取的檔案 %s。',
    'REMOTE_TOO_LARGE': 'KV260 上的檔案 %s 有 %d 位元組，超過序列傳輸的上限 %d 位元組。',
    'FETCH_CORRUPTED': '本程式自 KV260 讀取 %s 時，%d 次嘗試的內容都未通過 SHA-256 與長度檢查。',
    'KEY_STAYS_ON_BOARD': ('KV260 上的 %s 屬於金鑰資料（%s）；本程式不取回任何私鑰或 TLS 金鑰紀錄，板端私鑰只存在於 KV260。'
                           'PKI 目錄中只有 ca.pem、client.pem、client.csr 與 server.pin 可以取回。'),
    'PRIVATE_STAYS_ON_BOARD': ('KV260 上的 %s 屬於閘道的私有資料（%s）；本程式不取回擁有者的答案、分數或私有紀錄，'
                               '這些資料只存在於 KV260。'),
    'KEY_CHECK': 'KV260 未完成 %s 的金鑰檢查（readlink 或 grep 沒有輸出）；本程式未取回此檔案。',
    'WHY_SUFFIX': '副檔名為 %s',
    'WHY_WORD': '名稱含有「%s」',
    'WHY_KEY_DIR': '位於金鑰目錄 %s',
    'WHY_PKI': '位於 PKI 目錄，且不是公開檔案',
    'WHY_PEM': '內容含有 PEM 格式的私鑰',
    'WHY_WORKDIR': '位於閘道的工作目錄 %s',
    'WHY_PRIVATE_DIR': '位於私有紀錄目錄 %s',
    'WHY_RESOLVED': '；實際路徑為 %s',
    'NOT_SEALED': ('執行目錄 %s 尚未封存（缺少 capture.sha256 或 capture.pcapng）；本程式只在擷取檔封存之後讀取板端公開紀錄'
                   '與揭露檔。操作人員應先執行 efhe_seal.py --run。'),
    'SEAL_MISMATCH': '執行目錄中的 capture.pcapng 與封存紀錄 capture.sha256 不一致（%s… 與 %s…）；本程式已停止。',
    'SUDO': 'KV260 上的 sudo 驗證失敗；操作人員應確認 KV_PW 是主控台使用者的密碼，且該使用者具有 sudo 權限。',
    'NO_DISCLOSURE_DIR': 'KV260 上沒有揭露檔目錄 %s；本程式只取得公開紀錄。',
    'NO_DISCLOSURES': ('KV260 上沒有揭露檔；驗證程式的承諾檢查與 G10 需要以 edgefhe_gateway.py disclose 寫出的'
                       '虛構表單揭露檔。'),
    'DISCLOSURE_NAME': '本程式略過名稱不符規則的揭露檔：%s。',
    'DISCLOSURE_JSON': 'KV260 上的揭露檔 %s 不是一個 JSON 物件；本程式未寫入任何檔案。',
    'REMOTE_DIR': '本程式無法在 KV260 上建立目錄 %s（權限不足或路徑錯誤）。',
    'PUBLIC_FETCHED': ('本程式已將板端公開紀錄（%d 行）與 %d 個虛構表單揭露檔寫入 %s；揭露檔只含虛構答案。下一步：'
                       'efhe_seal.py --run %s --verify --board-public %s --disclosure %s。'),
    'PKI_MISSING': 'PKI 目錄 %s 不是 make_demo_pki.sh ca-and-server 建立的目錄（缺少 %s）。',
    'PKI_SIGNED': ('PKI 目錄 %s 已簽發過板端憑證（board/ 或 cloud/client.pin 已存在）；make_demo_pki.sh 不覆寫。'
                   '若 KV260 更換了私鑰，操作人員應先移除 board/ 與 cloud/client.pin。'),
    'BOARD_CSR': ('KV260 上缺少 %s/client.csr 或 client.key；操作人員應先在 KV260 上以 openssl req -new -newkey ec '
                  '-pkeyopt ec_paramgen_curve:prime256v1 -noenc 產生私鑰與 CSR（私鑰不離開 KV260）。'),
    'KEY_MODE': ('注意：KV260 上 client.key 的權限為 %s，不是 0600；閘道的 preflight 會拒絕此設定，'
                 '操作人員應在 KV260 上執行 chmod 600。'),
    'CSR_FORMAT': '自 KV260 取得的 CSR 不是 PEM 格式的憑證簽署要求，或超過 %d 位元組。',
    'GIT_BASH': ('本程式找不到 Git Bash（Git for Windows 的 bin\\bash.exe）；操作人員應以 --bash 指定，或設定 '
                 'EDGEFHE_GIT_BASH。本程式不使用 WSL 的 bash。'),
    'SIGN_FAILED': 'make_demo_pki.sh sign-client 失敗（結束碼 %s）：%s',
    'CSR_CHECK': 'KV260 上的憑證檢查未通過：%s。',
    'CSR_DONE': ('板端憑證已傳回 KV260 的 %s（ca.pem、client.pem、server.pin）；client.pem 與 KV260 上的 client.key 相符。'
                 '雲端伺服器須以新的 cloud/client.pin 重新啟動。'),
    'REPLACED': '注意：KV260 上原有的 %s 已被取代。',
    'PUSH_AFTER_SIGN': ('憑證已簽發；操作人員應以 push-file 將 %s 下的 ca.pem、client.pem、server.pin 逐一傳到 KV260 的 %s/，'
                        '且不應再次執行 sign-client。'),
    'ESTIMATE': '本程式預估序列傳輸約需 %d 秒（%d 位元組）。',
}


class Refused(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, code)
        self.code = code
        self.message = message


class Failed(Refused):
    pass


def out(line):
    sys.stdout.write(line + '\n')
    sys.stdout.flush()


def say(text):
    sys.stderr.write(text + '\n')
    sys.stderr.flush()


def _utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def remote_path(text):
    """A board path that needs no shell quoting (a leading ~/ still expands), or Refused."""
    path = text.rstrip('/') if len(text) > 1 else text
    parts = path.split('/')[1:]
    if not REMOTE.fullmatch(path) or any(p in ('.', '..') for p in parts):
        raise Refused('REMOTE_PATH', TEXT['REMOTE_PATH'] % text)
    return path


def stays_on_board(path):
    """(refusal code, reason) when a board path names key material or a private record of the gateway, else None.
    Names are compared without case, which refuses more, never less."""
    parts = [p for p in path.split('/') if p not in ('', '.', '~')]
    if not parts:
        return None
    name, dirs = parts[-1].lower(), [p.lower() for p in parts[:-1]]
    for suffix in KEY_SUFFIXES:
        if name.endswith(suffix):
            return 'KEY_STAYS_ON_BOARD', TEXT['WHY_SUFFIX'] % suffix
    for word in KEY_WORDS:
        if word in name:
            return 'KEY_STAYS_ON_BOARD', TEXT['WHY_WORD'] % word
    for d in dirs:
        if d in KEY_DIRS:
            return 'KEY_STAYS_ON_BOARD', TEXT['WHY_KEY_DIR'] % d
    if PKI_DIR in dirs and name not in PKI_PUBLIC:
        return 'KEY_STAYS_ON_BOARD', TEXT['WHY_PKI']
    if path.startswith('/'):
        full = '/' + '/'.join(parts)
        for root in PRIVATE_ROOTS:
            if full == root or full.startswith(root + '/'):
                return 'PRIVATE_STAYS_ON_BOARD', TEXT['WHY_WORKDIR'] % root
    for d in dirs:
        if 'private' in d:
            return 'PRIVATE_STAYS_ON_BOARD', TEXT['WHY_PRIVATE_DIR'] % d
    return None


def refuse_kept(path, resolved=None):
    """Refused when path (or the board's resolved path for it) must stay on the board."""
    for candidate, note in ((path, ''), (resolved, TEXT['WHY_RESOLVED'] % resolved)):
        found = stays_on_board(candidate) if candidate is not None else None
        if found is not None:
            code, why = found
            raise Refused(code, TEXT[code] % (path, why + note))


def _evidence_guard():
    """efhe_verify.inside_repository, or None when the evidence module cannot be loaded (the identity walk below
    still applies then)."""
    if EVIDENCE_DIR not in sys.path:
        sys.path.append(EVIDENCE_DIR)
    try:
        import efhe_verify
        return efhe_verify.inside_repository
    except Exception:                           # any failure of another module only drops this half of the check
        return None


def _ancestors(path):
    while True:
        yield path
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent


def inside_repository(path):
    """True when a PC path is the repository folder or lies inside it, however the path is spelled (docstring above):
    efhe_verify.inside_repository, then the path and its resolved form walked up to the root, each existing ancestor
    compared with the repository folder by file identity. Refuses more, never less."""
    full = os.path.abspath(path)
    guard = _evidence_guard()
    if guard is not None:
        try:
            if guard(full):
                return True
        except (OSError, ValueError):
            pass
    try:
        repo = os.stat(ROOT)
    except OSError:
        return False
    if not repo.st_ino:                         # no file identity on this file system: the comparison would be void
        return False
    starts = [full]
    try:
        starts.append(os.path.realpath(full))
    except (OSError, ValueError):
        pass
    for start in starts:
        for candidate in _ancestors(start):
            try:
                if os.path.samestat(os.stat(candidate), repo):
                    return True
            except (OSError, ValueError):
                continue
    return False


def refuse_in_repository(path):
    if inside_repository(path):
        raise Refused('OUTPUT_IN_REPOSITORY', TEXT['OUTPUT_IN_REPOSITORY'] % path)


def load_kv_console(path=KV_CONSOLE_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        import kv_console
        ok = ('secret' in inspect.signature(kv_console.KV.run).parameters
              and 'secret' in inspect.signature(kv_console.stream).parameters
              and 'unset HISTFILE' in inspect.getsource(kv_console.KV.login)
              and callable(getattr(kv_console, 'put_file', None)))
    except ImportError as e:
        missing = 'pyserial：pip install pyserial' if getattr(e, 'name', None) == 'serial' else str(e)
        raise Refused('KV_CONSOLE', TEXT['KV_CONSOLE'] % missing)
    except (AttributeError, TypeError, OSError, ValueError) as e:
        raise Refused('KV_CONSOLE', TEXT['KV_CONSOLE'] % type(e).__name__)
    if not ok:
        raise Refused('KV_CONSOLE', TEXT['KV_CONSOLE'] % 'secret masking')
    return kv_console


def find_git_bash(explicit=None, env=None):
    """Git Bash on Windows (never the bash.exe in the Windows system directory, which is WSL), plain bash elsewhere."""
    env = os.environ if env is None else env
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    found = []
    if env.get('EDGEFHE_GIT_BASH'):
        found.append(env['EDGEFHE_GIT_BASH'])
    if os.name == 'nt':
        for base in (env.get('ProgramFiles'), env.get('ProgramW6432'), env.get('ProgramFiles(x86)'),
                     os.path.join(env.get('LOCALAPPDATA', ''), 'Programs') if env.get('LOCALAPPDATA') else None):
            if base:
                found.append(os.path.join(base, 'Git', 'bin', 'bash.exe'))
        git = shutil.which('git')
        if git:
            found.append(os.path.join(os.path.dirname(os.path.dirname(git)), 'bin', 'bash.exe'))
    else:
        found.append(shutil.which('bash'))
    system = os.path.normcase(env.get('SystemRoot', 'C:\\Windows'))
    for candidate in found:
        if candidate and os.path.isfile(candidate) and not os.path.normcase(candidate).startswith(system):
            return candidate
    return None


# ------------------------------------------------------------------------------------------------- the board
class Board:
    """A logged-in console session (kv_console.KV); the password is masked in every error text."""

    def __init__(self, kv, kvc, password):
        self.kv = kv
        self.kvc = kvc
        self.pw = password
        self.retries = 0

    def close(self):
        try:
            self.kv.logout()
        except Exception:
            pass

    def run(self, cmd, timeout=30):
        try:
            return self.kv.run(cmd, timeout, secret=self.pw or None)
        except RuntimeError as e:
            raise Failed('CONSOLE', str(e).replace(self.pw, '***') if self.pw else str(e))

    def sudo(self):
        """Refresh the sudo credentials of this console session (the same pattern as the energy probe scripts)."""
        cmd = ("echo %s | sudo -S -p '' -v 2>/dev/null; sudo -n true 2>/dev/null && echo __EF_SUDO_OK__"
               % shlex.quote(self.pw))
        rc, text = self.run(cmd, 30)
        if '__EF_SUDO_OK__' not in text:
            raise Failed('SUDO', TEXT['SUDO'])

    def mkdir(self, path):
        rc, text = self.run('mkdir -p %s && echo __EF_DIR_OK__' % path, 20)
        if '__EF_DIR_OK__' not in text:
            raise Failed('REMOTE_DIR', TEXT['REMOTE_DIR'] % path)

    def exists(self, path, sudo=False):
        rc, text = self.run('if %stest -e %s; then echo __EF_YES__; else echo __EF_NO__; fi' % (
            'sudo -n ' if sudo else '', path), 20)
        return '__EF_YES__' in text

    def put(self, local, remote):
        """kv_console.put_file with its SHA-256 check, repeated once when a console line was damaged."""
        for _ in range(2):
            try:
                if self.kvc.put_file(self.kv, local, remote):
                    return
            except (RuntimeError, IndexError):
                pass
            self.retries += 1
        raise Failed('PUSH_HASH', TEXT['PUSH_HASH'] % remote)

    def size(self, remote, sudo=False):
        s = 'sudo -n ' if sudo else ''
        rc, text = self.run('if %stest -f %s && %stest -r %s; then echo __EF_SIZE__$(%sstat -c %%s %s); '
                            'else echo __EF_MISSING__; fi' % (s, remote, s, remote, s, remote), 30)
        m = re.search(r'__EF_SIZE__([0-9]+)', text)
        if '__EF_MISSING__' in text or not m:
            return None
        return int(m.group(1))

    def guard(self, remote):
        """Refused unless the board file at remote may leave the board: the name rules on the path that readlink -f
        resolves on the board, and no PEM private key in the content (grep on the board, before any byte is sent).
        Without both answers, or with a resolved path whose length does not match (a kernel message inside the line),
        the file is refused (KEY_CHECK), never fetched."""
        rc, text = self.run('if test -f %s && test -r %s; then _ef_r=$(readlink -f -- %s); '
                            'echo __EF_REAL__${#_ef_r}_$_ef_r; '
                            'echo __EF_KEYMAT__$(grep -c -a -F -e %s -- %s 2>/dev/null); else echo __EF_MISSING__; fi'
                            % (remote, remote, remote, shlex.quote(PEM_PRIVATE), remote), 120)
        if '__EF_MISSING__' in text:
            raise Failed('REMOTE_MISSING', TEXT['REMOTE_MISSING'] % remote)
        real = re.search(r'__EF_REAL__([0-9]+)_(/[^\r\n]*)', text)
        count = re.search(r'__EF_KEYMAT__([0-9]+)', text)
        resolved = real.group(2).rstrip('\r ') if real else None
        if resolved is None or len(resolved) != int(real.group(1)) or count is None:
            raise Refused('KEY_CHECK', TEXT['KEY_CHECK'] % remote)
        if int(count.group(1)):
            raise Refused('KEY_STAYS_ON_BOARD', TEXT['KEY_STAYS_ON_BOARD'] % (remote, TEXT['WHY_PEM']))
        refuse_kept(remote, resolved)

    def fetch(self, remote, sudo=False, limit=FETCH_MAX):
        """The bytes of a board file: base64 lines between markers, checked against the board's size and SHA-256."""
        size = self.size(remote, sudo)
        if size is None:
            raise Failed('REMOTE_MISSING', TEXT['REMOTE_MISSING'] % remote)
        if size > limit:
            raise Refused('REMOTE_TOO_LARGE', TEXT['REMOTE_TOO_LARGE'] % (remote, size, limit))
        s = 'sudo -n ' if sudo else ''
        cmd = ('if %stest -f %s; then echo __EF_BEGIN__; %sbase64 -w 900 %s; echo __EF_END__$(%ssha256sum %s | '
               'cut -c1-64); fi' % (s, remote, s, remote, s, remote))
        timeout = 30 + size * 1.4 / CONSOLE_BYTES_PER_S
        for _ in range(ATTEMPTS):
            lines = []
            try:
                self.kvc.stream(self.kv, cmd, timeout, lambda line, t: lines.append(line), secret=self.pw or None)
            except RuntimeError:
                lines = []
            data, digest = parse_fetch(lines)
            if data is not None and len(data) == size and digest == sha256_bytes(data):
                return data
            self.retries += 1
        raise Failed('FETCH_CORRUPTED', TEXT['FETCH_CORRUPTED'] % (remote, ATTEMPTS))


def parse_fetch(lines):
    """(bytes or None, board SHA-256 or None) of the console lines of one fetch; other console lines are skipped."""
    inside, chunks, digest = False, [], None
    for line in lines:
        text = line.strip()
        if text == '__EF_BEGIN__':
            inside, chunks = True, []
        elif text.startswith('__EF_END__'):
            digest = text[len('__EF_END__'):].strip()
            break
        elif inside and B64_LINE.fullmatch(text):
            chunks.append(text)
    if not inside or digest is None or not HEX64.fullmatch(digest):
        return None, None
    try:
        return base64.b64decode(''.join(chunks), validate=True), digest
    except (binascii.Error, ValueError):
        return None, digest


def open_board(port, user, kv_factory=None, env=None):
    env = os.environ if env is None else env
    password = env.get('KV_PW')
    if password is None:
        raise Refused('KV_PW_MISSING', TEXT['KV_PW_MISSING'])
    kvc = load_kv_console()
    try:
        kv = (kv_factory or kvc.KV)(port)
    except Exception as e:
        raise Failed('PORT_OPEN', TEXT['PORT_OPEN'] % (port, type(e).__name__))
    try:
        kv.login(user, password)
    except Exception as e:
        try:
            kv.s.close()
        except Exception:
            pass
        detail = str(e)[:200]
        raise Failed('LOGIN', TEXT['LOGIN'] % (detail.replace(password, '***') if password else detail))
    return Board(kv, kvc, password)


def estimate(nbytes):
    say(TEXT['ESTIMATE'] % (max(1, int(nbytes * 1.4 / CONSOLE_BYTES_PER_S)), nbytes))


# ---------------------------------------------------------------------------------------------- local checks
def tree_files(top):
    """[(relative posix path, absolute path)] of the regular files to push, sorted, and the count of skipped files."""
    files, skipped = [], 0
    for root, dirs, names in os.walk(top):
        keep = []
        for d in sorted(dirs):
            if d in SKIP_DIRS or os.path.islink(os.path.join(root, d)):
                skipped += 1
            else:
                keep.append(d)
        dirs[:] = keep
        for name in sorted(names):
            full = os.path.join(root, name)
            low = name.lower()
            if low.endswith(SKIP_SUFFIXES) or os.path.islink(full) or not os.path.isfile(full):
                skipped += 1
                continue
            rel = os.path.relpath(full, top).replace(os.sep, '/')
            for part in rel.split('/'):
                if not NAME.fullmatch(part) or part in ('.', '..'):
                    raise Refused('TREE_NAME', TEXT['TREE_NAME'] % rel)
            files.append((rel, full))
    files.sort()
    return files, skipped


def tree_archive(files):
    """(tar.gz bytes, manifest text '<sha256>  <path>' per file, total bytes) with fixed modes and owners."""
    buf = io.BytesIO()
    manifest, total, dirs = [], 0, set()
    with tarfile.open(fileobj=buf, mode='w:gz', format=tarfile.PAX_FORMAT) as tar:
        for rel, full in files:
            parts = rel.split('/')
            for i in range(1, len(parts)):
                d = '/'.join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    info = tarfile.TarInfo(d)
                    info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, int(time.time())
                    tar.addfile(info)
            with open(full, 'rb') as f:
                data = f.read()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755 if rel.endswith('.sh') else 0o644
            info.mtime = int(os.path.getmtime(full))
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            tar.addfile(info, io.BytesIO(data))
            manifest.append('%s  %s\n' % (sha256_bytes(data), rel))
            total += len(data)
    return buf.getvalue(), ''.join(manifest), total


def check_sealed(run_dir):
    """The sealed capture digest of run_dir, or Refused (the public files are fetched only after the seal)."""
    cap, rec = os.path.join(run_dir, CAPTURE), os.path.join(run_dir, SEAL)
    if not (os.path.isfile(cap) and os.path.isfile(rec)):
        raise Refused('NOT_SEALED', TEXT['NOT_SEALED'] % run_dir)
    with open(rec, encoding='ascii', errors='replace') as f:
        sealed = (f.read().split() or [''])[0].lower()
    actual = sha256_file(cap)
    if sealed != actual:
        raise Refused('SEAL_MISMATCH', TEXT['SEAL_MISMATCH'] % (sealed[:16], actual[:16]))
    return actual


def write_new(path, data):
    """Write a new file (never over an existing one) through a .tmp name."""
    if os.path.exists(path):
        raise Refused('OUTPUT_EXISTS', TEXT['OUTPUT_EXISTS'] % path)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)


def board_tmp(suffix):
    return '/tmp/edgefhe_files_%s%s' % (secrets.token_hex(6), suffix)


# --------------------------------------------------------------------------------------------------- commands
def cmd_push_file(board, src, dst):
    size = os.path.getsize(src)
    estimate(size)
    board.mkdir(posixpath.dirname(dst) or '/')
    board.put(src, dst)
    out('BOARD_FILES_PUSHED dst=%s bytes=%d sha256=%s' % (dst, size, sha256_file(src)))


def cmd_push_tree(board, top, dst, files, skipped):
    archive, manifest, total = tree_archive(files)
    tree_id = sha256_bytes(manifest.encode('ascii'))
    estimate(len(archive) + len(manifest))
    work = tempfile.mkdtemp(prefix='edgefhe-push-')
    try:
        local_tgz, local_sums = os.path.join(work, 'tree.tgz'), os.path.join(work, 'tree.sha256')
        with open(local_tgz, 'wb') as f:
            f.write(archive)
        with open(local_sums, 'wb') as f:
            f.write(manifest.encode('ascii'))
        remote_tgz, remote_sums = board_tmp('.tgz'), board_tmp('.sha256')
        board.mkdir(dst)
        board.put(local_tgz, remote_tgz)
        board.put(local_sums, remote_sums)
        rc, text = board.run('tar -xzf %s -C %s -m && (cd %s && sha256sum -c --quiet %s) && echo __EF_TREE_OK__; '
                             'rm -f %s %s' % (remote_tgz, dst, dst, remote_sums, remote_tgz, remote_sums),
                             60 + len(files) // 10)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if '__EF_TREE_OK__' not in text:
        raise Failed('TREE_CHECK', TEXT['TREE_CHECK'] % ' '.join(text.split())[-200:])
    out('BOARD_FILES_TREE_PUSHED dst=%s files=%d bytes=%d skipped=%d tree_sha256=%s' % (
        dst, len(files), total, skipped, tree_id))


def refuse_key_bytes(remote, data):
    """The PC's own check of fetched bytes (the file may have changed after the board's check)."""
    if PEM_PRIVATE.encode('ascii') in data:
        raise Refused('KEY_STAYS_ON_BOARD', TEXT['KEY_STAYS_ON_BOARD'] % (remote, TEXT['WHY_PEM']))


def cmd_fetch_file(board, src, dst):
    board.guard(src)
    data = board.fetch(src)
    refuse_key_bytes(src, data)
    write_new(dst, data)
    out('BOARD_FILES_FETCHED src=%s dst=%s bytes=%d sha256=%s' % (src, dst, len(data), sha256_bytes(data)))


def cmd_fetch_public(board, run_dir, public_log, disclosure_dir):
    board.sudo()
    public = board.fetch(public_log, sudo=True)
    names = []
    if board.exists(disclosure_dir, sudo=True):
        rc, text = board.run("sudo -n find %s -maxdepth 1 -type f -name '*.json' -printf '__EF_NAME__%%f\\n'; "
                             "echo __EF_LIST_END__" % disclosure_dir, 60)
        if '__EF_LIST_END__' not in text:
            raise Failed('CONSOLE', TEXT['NO_DISCLOSURE_DIR'] % disclosure_dir)
        for line in text.splitlines():
            line = line.strip()
            if line.startswith('__EF_NAME__'):
                name = line[len('__EF_NAME__'):]
                if DISCLOSURE_NAME.fullmatch(name):
                    names.append(name)
                else:
                    say(TEXT['DISCLOSURE_NAME'] % name)
    else:
        say(TEXT['NO_DISCLOSURE_DIR'] % disclosure_dir)
    docs = collections.OrderedDict()
    for name in sorted(set(names)):
        docs[name] = board.fetch(disclosure_dir + '/' + name, sudo=True, limit=1024 * 1024)
    rows = 0
    for raw in public.decode('utf-8', 'replace').splitlines():
        if raw.strip():
            rows += 1
    for name, data in docs.items():
        try:
            doc = json.loads(data.decode('utf-8'))      # one JSON object per file (records.write_disclosure)
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            raise Failed('DISCLOSURE_JSON', TEXT['DISCLOSURE_JSON'] % name)
    public_path = os.path.join(run_dir, PUBLIC_OUT)
    disclosure_path = os.path.join(run_dir, DISCLOSURE_OUT)
    if os.path.exists(public_path) or os.path.exists(disclosure_path):
        raise Refused('OUTPUT_EXISTS', TEXT['OUTPUT_EXISTS'] % (public_path if os.path.exists(public_path)
                                                                else disclosure_path))
    write_new(public_path, public)
    os.mkdir(disclosure_path)
    for name, data in docs.items():
        write_new(os.path.join(disclosure_path, name), data)
    if not docs:
        say(TEXT['NO_DISCLOSURES'])
    say(TEXT['PUBLIC_FETCHED'] % (rows, len(docs), run_dir, run_dir, public_path, disclosure_path))
    out('BOARD_FILES_PUBLIC_FETCHED run=%s public_rows=%d public_sha256=%s disclosures=%d' % (
        run_dir, rows, sha256_bytes(public), len(docs)))


def check_pki_dir(pki):
    for rel in (os.path.join('ca', 'ca.key'), os.path.join('ca', 'ca.pem'), os.path.join('cloud', 'server.pem')):
        if not os.path.isfile(os.path.join(pki, rel)):
            raise Refused('PKI_MISSING', TEXT['PKI_MISSING'] % (pki, rel))
    if os.path.exists(os.path.join(pki, 'board')) or os.path.exists(os.path.join(pki, 'cloud', 'client.pin')):
        raise Refused('PKI_SIGNED', TEXT['PKI_SIGNED'] % pki)


def sign_client(bash, script, pki, csr_path):
    """Run make_demo_pki.sh sign-client; returns the client pin (stderr of the script is shown to the operator)."""
    env = dict(os.environ)
    env['MSYS_NO_PATHCONV'] = '1'
    try:
        proc = subprocess.run([bash, script, 'sign-client', pki, csr_path], capture_output=True, timeout=300,
                              env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Failed('SIGN_FAILED', TEXT['SIGN_FAILED'] % ('-', type(e).__name__))
    stdout = proc.stdout.decode('utf-8', 'replace')
    stderr = proc.stderr.decode('utf-8', 'replace')
    for line in stderr.splitlines():
        if line.strip():
            say(line)
    m = re.search(r'^EDGEFHE_DEMO_CLIENT_SIGNED client_pin=([0-9a-f]{64})\s*$', stdout, re.M)
    if proc.returncode != 0 or m is None:
        raise Failed('SIGN_FAILED', TEXT['SIGN_FAILED'] % (proc.returncode, ' '.join(stderr.split())[-300:]))
    return m.group(1)


def cmd_csr_roundtrip(board, pki, board_pki, bash, script):
    if not (board.exists(board_pki + '/client.csr') and board.exists(board_pki + '/client.key')):
        raise Refused('BOARD_CSR', TEXT['BOARD_CSR'] % board_pki)
    rc, text = board.run('stat -c __EF_MODE__%%a %s/client.key' % board_pki, 20)
    m = re.search(r'__EF_MODE__([0-7]+)', text)
    if not m or m.group(1) != '600':
        say(TEXT['KEY_MODE'] % (m.group(1) if m else '?'))
    board.guard(board_pki + '/client.csr')
    csr = board.fetch(board_pki + '/client.csr', limit=CSR_MAX)
    refuse_key_bytes(board_pki + '/client.csr', csr)
    if b'-----BEGIN CERTIFICATE REQUEST-----' not in csr or b'-----END CERTIFICATE REQUEST-----' not in csr:
        raise Failed('CSR_FORMAT', TEXT['CSR_FORMAT'] % CSR_MAX)
    work = tempfile.mkdtemp(prefix='edgefhe-csr-')
    try:
        csr_path = os.path.join(work, 'client.csr')
        with open(csr_path, 'wb') as f:
            f.write(csr)
        pin = sign_client(bash, script, pki, csr_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out('EDGEFHE_DEMO_CLIENT_SIGNED client_pin=%s' % pin)
    try:
        for name in ('ca.pem', 'client.pem', 'server.pin'):
            target = board_pki + '/' + name
            if board.exists(target):
                say(TEXT['REPLACED'] % target)
            board.put(os.path.join(pki, 'board', name), target)
    except Failed as e:                         # signed already: sign-client refuses a second run on this PKI
        raise Failed(e.code, e.message + TEXT['PUSH_AFTER_SIGN'] % (os.path.join(pki, 'board'), board_pki))
    with open(os.path.join(pki, 'board', 'server.pin'), encoding='ascii') as f:
        server_pin = f.read().strip()
    rc, text = board.run(
        '( a=$(openssl x509 -in {d}/client.pem -noout -pubkey 2>/dev/null); '
        'b=$(openssl pkey -in {d}/client.key -pubout 2>/dev/null); '
        'if [ -n "$a" ] && [ "$a" = "$b" ]; then echo __EF_KEY_MATCH__; else echo __EF_KEY_MISMATCH__; fi; '
        'echo __EF_PIN__$(openssl x509 -in {d}/client.pem -outform DER 2>/dev/null | sha256sum | cut -c1-64); '
        'echo __EF_SPIN__$(head -c 64 {d}/server.pin) )'.format(d=board_pki), 60)
    problems = []
    if '__EF_KEY_MATCH__' not in text:
        problems.append('client.pem / client.key')
    got = re.search(r'__EF_PIN__([0-9a-f]{64})', text)
    if not got or got.group(1) != pin:
        problems.append('client pin')
    spin = re.search(r'__EF_SPIN__([0-9a-f]{64})', text)
    if not spin or spin.group(1) != server_pin:
        problems.append('server.pin')
    if problems:
        raise Failed('CSR_CHECK', TEXT['CSR_CHECK'] % ', '.join(problems))
    say(TEXT['CSR_DONE'] % board_pki)
    out('BOARD_FILES_CSR_ROUNDTRIP_PASS client_pin=%s key_match=1 board_pki=%s files=ca.pem,client.pem,server.pin'
        % (pin, board_pki))


# ------------------------------------------------------------------------------------------------------- main
def parse_args(argv):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--port', default='COM6', help='the board console (default COM6)')
    common.add_argument('--user', default='ubuntu', help='the console user (the password comes only from KV_PW)')
    ap = argparse.ArgumentParser(description='KV260 serial-console file tools (PC side).')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('push-file', parents=[common])
    p.add_argument('src')
    p.add_argument('dst')
    p = sub.add_parser('push-tree', parents=[common])
    p.add_argument('dir')
    p.add_argument('dst')
    p = sub.add_parser('fetch-file', parents=[common])
    p.add_argument('src')
    p.add_argument('dst')
    p = sub.add_parser('fetch-public', parents=[common])
    p.add_argument('run_dir')
    p.add_argument('--public-log', default=PUBLIC_LOG)
    p.add_argument('--disclosure-dir', default=DISCLOSURE_DIR)
    p = sub.add_parser('csr-roundtrip', parents=[common])
    p.add_argument('--pki', required=True, help='the ca-and-server directory of make_demo_pki.sh (outside the repo)')
    p.add_argument('--board-pki', default=BOARD_PKI)
    p.add_argument('--bash', help='Git Bash (bin\\bash.exe of Git for Windows)')
    p.add_argument('--script', default=PKI_SCRIPT)
    return ap.parse_args(argv)


def prepare(a):
    """Every local check before the console is opened; returns the bound command."""
    if a.command == 'push-file':
        dst = remote_path(a.dst)
        if not os.path.isfile(a.src):
            raise Refused('LOCAL_MISSING', TEXT['LOCAL_MISSING'] % a.src)
        size = os.path.getsize(a.src)
        if size > PUSH_MAX:
            raise Refused('LOCAL_TOO_LARGE', TEXT['LOCAL_TOO_LARGE'] % (a.src, size, PUSH_MAX))
        return lambda board: cmd_push_file(board, a.src, dst)
    if a.command == 'push-tree':
        dst = remote_path(a.dst)
        if not os.path.isdir(a.dir):
            raise Refused('LOCAL_MISSING', TEXT['LOCAL_MISSING'] % a.dir)
        files, skipped = tree_files(a.dir)
        if not files:
            raise Refused('TREE_EMPTY', TEXT['TREE_EMPTY'] % a.dir)
        if sum(os.path.getsize(f) for _, f in files) > PUSH_MAX:
            raise Refused('LOCAL_TOO_LARGE', TEXT['LOCAL_TOO_LARGE'] % (a.dir, sum(
                os.path.getsize(f) for _, f in files), PUSH_MAX))
        return lambda board: cmd_push_tree(board, a.dir, dst, files, skipped)
    if a.command == 'fetch-file':
        src = remote_path(a.src)
        refuse_kept(src)                        # before the console is opened; the board checks the resolved path
        refuse_in_repository(a.dst)
        if os.path.exists(a.dst):
            raise Refused('OUTPUT_EXISTS', TEXT['OUTPUT_EXISTS'] % a.dst)
        return lambda board: cmd_fetch_file(board, src, a.dst)
    if a.command == 'fetch-public':
        run_dir = os.path.abspath(a.run_dir)
        refuse_in_repository(run_dir)
        check_sealed(run_dir)
        public_log, disclosure_dir = remote_path(a.public_log), remote_path(a.disclosure_dir)
        for name in (PUBLIC_OUT, DISCLOSURE_OUT):
            if os.path.exists(os.path.join(run_dir, name)):
                raise Refused('OUTPUT_EXISTS', TEXT['OUTPUT_EXISTS'] % os.path.join(run_dir, name))
        return lambda board: cmd_fetch_public(board, run_dir, public_log, disclosure_dir)
    pki = os.path.abspath(a.pki)
    board_pki = remote_path(a.board_pki)
    refuse_in_repository(pki)                   # sign-client writes board/ and cloud/client.pin into it
    check_pki_dir(pki)
    if not os.path.isfile(a.script):
        raise Refused('LOCAL_MISSING', TEXT['LOCAL_MISSING'] % a.script)
    bash = find_git_bash(a.bash)
    if bash is None:
        raise Refused('GIT_BASH', TEXT['GIT_BASH'])
    return lambda board: cmd_csr_roundtrip(board, pki, board_pki, bash, a.script)


def main(argv=None, kv_factory=None, env=None):
    _utf8_stdio()
    a = parse_args(argv)
    board = None
    try:
        command = prepare(a)
        board = open_board(a.port, a.user, kv_factory, env)
        command(board)
        if board.retries:
            say('本程式在傳輸中重試了 %d 次（序列主控台上的行受到干擾）；所有檔案均已通過 SHA-256 檢查。' % board.retries)
        return EXIT_OK
    except Failed as e:
        say(e.message)
        out('BOARD_FILES_FAILED code=%s' % e.code)
        return EXIT_FAIL
    except Refused as e:
        say(e.message)
        out('BOARD_FILES_REFUSED code=%s' % e.code)
        return EXIT_REFUSED
    finally:
        if board is not None:
            board.close()


if __name__ == '__main__':
    sys.exit(main())
