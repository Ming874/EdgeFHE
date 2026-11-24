"""EdgeFHE demo cloud console: the PC evidence console (cloud view) on http://127.0.0.1:8765.

The cloud PC receives and returns ciphertext only and holds no CKKS key. This console shows, live, what the cloud side
sees and what a third party can check:
  線上封包（無金鑰）  the keyless tshark feed (the eavesdropper view): TLS 1.3 record sizes and the labels they imply,
                      marked '≈' until the session is decoded
  解開 TLS 之後        after a session closed: a copy of the capture decrypted with the demo-only TLS key log
                      (efhe_verify.quick_session): EFHEW001 frames that still carry CKKS ciphertext only
  三方雜湊             request_sha256 and response_payload_sha256: capture = server.log live; the board public log is
                      joined after the seal (until then the owner screen shows the same prefixes)
  雲端運算             cloud_ns from server.log and the bit-exact recomputation of the response from the public W, B (G5)
  時序                 the wire phases of each session (capture clock)
  能耗                 the board power from the COM5 probe with SYN/FIN markers and ΔE ≈ per session
  驗證項目             gates G2-G8 per session; G0-G11 after the seal and the verification
  封存                 stop the capture, the server and the probe, then efhe_seal.py (the SHA-256 seal comes before
                      any third-party use of the key log), and later efhe_seal.py --verify

usage:
  python -B demo/console/evidence_console.py --config CFG [--run DIR] [--listen 127.0.0.1:8765] [--no-keylog]
         [--snaplen N]
  python -B demo/console/evidence_console.py --replay DIR [--config CFG] [--port N] [--probe-csv F]
         [--probe-shift S] [--once]

Live mode (--run DIR, default <evidence_root>/<yyyymmdd_HHMMSS>; the directory must be new or empty) first restricts
the NTFS access of the PKI folder (pki_dir and everything below it: the server private key) and of the run folder (the
capture and the demo TLS key log) to the current user and SYSTEM: icacls /inheritance:r /grant:r, read back with
icacls /save. It prints EVIDENCE_PKI_PRIVATE and EVIDENCE_RUN_PRIVATE, or refuses to continue (PKI_ACL, RUN_ACL) when
that fails. EVIDENCE_ROOT_ACL_WARNING (read-only, the run goes on) means that the parent of the run folder still
grants Everyone, Authenticated Users or Users access: the operator's step of README section 6.3.1 is missing.
Then it starts, in order:
  1. dumpcap -i IFACE -f 'host BOARD' -s 0 -B 128 -w DIR/capture.pcapng; its stderr is DIR/capture_stats.txt
     (EVIDENCE_CAPTURE_STARTED). It is stopped with CTRL_BREAK_EVENT, so the pcapng ends with its interface statistics.
  2. the cloud server, then WARNING_DEMO_TLS_KEYLOG_ENABLED and CPP_E2E_SERVER_READY are required in DIR/server.log:
     server "docker": docker run -d --init --name efhe-cloud -p 18443:18443 -v PKI:/pki:ro -v DIR:/evidence IMAGE
                      sh /opt/efhe/run_server.sh --keylog
     server "mock":   demo/evidence/tests/mock_efnet.py server (PC-only tests with the Python TLS 1.3 mock)
     server "none":   a server that the operator started by hand with DIR mounted as /evidence
     The console writes DIR/server.pin and DIR/client.pin (SHA-256 of the certificate DER), which gate G2 compares.
  3. the keyless tshark feed (live_feed.py), with an empty Wireshark configuration and no key log.
  4. the probe reader on the configured port (probe_reader.py: DTR and RTS False before open, nothing is ever sent);
     samples go to DIR/probe.csv.
Then EVIDENCE_CONSOLE_READY http://127.0.0.1:8765. A session opens with the board's SYN and closes with a FIN, or
2 s after a close_notify. After the close, the console copies capture.pcapng to a temporary directory, filters that
one connection into a file of its own (tshark numbers TLS streams apart from TCP streams, so a refused SYN/RST earlier
in the capture would otherwise shift the stream that the decode follows) and decodes it with
efhe_verify.quick_session, retrying for up to 10 s; otherwise the screen shows '封存後解碼', and the session is decoded
from the sealed capture after the seal. A connection without any TLS record (a refused SYN/RST) is not decoded.
server.log lines CPP_E2E_SERVER_CONNECTION_STOP are attached to the connection that was alive at the stop; the accept
deadline of an idle server (TLS_DEADLINE every 30 s with no SYN behind it) is shown as idle, not as a failure.
The 封存 button (POST /api/v1/seal) refuses while a session is open; it stops the feed, dumpcap, the server and the
probe, and runs efhe_seal.py --run DIR (EFHE_CAPTURE_SEALED sha256=...). After board_files.py fetch-public has put
board_public.jsonl and disclosure/ into DIR, the 驗證 button (POST /api/v1/verify) runs efhe_seal.py --run DIR --verify.
Ctrl+C (or Ctrl+Break) stops what the console started, also during the start (dumpcap, tshark and the server run in
process groups of their own and never see the operator's Ctrl+C); an unsealed capture is then sealed by hand with
efhe_seal.py. The console never signals or stops anything on the board.

Replay mode (--replay DIR) renders an existing run directory offline: the keyless fields from DIR/capture.pcapng,
server.log, probe.csv (or --probe-csv, moved by --probe-shift seconds for fixtures recorded at another time), a decode
of every session when tls_keys.log exists, the seal record, verify_report.json and board_public.jsonl. A replay never
writes into DIR; EVIDENCE_REPLAY_ACL_WARNING (read-only) means that DIR holds a key log and still grants Everyone,
Authenticated Users or Users access (README section 6.3.1).

The console never touches the board. It binds 127.0.0.1 only and accepts requests whose Host is 127.0.0.1:PORT or
localhost:PORT; a POST also needs X-EdgeFHE-Console: 1, a JSON body and a same-origin (or absent) Origin. Every
response carries a strict Content-Security-Policy, nosniff and no-store. Copies of the capture live in a temporary
directory outside the run directory and are deleted after each decode; the key log is read in place and never copied.
Windows (the cloud PC) with Python 3.10 or later; pyserial only for the probe. Research demonstration only; not a
medical diagnosis.
"""
import argparse
import collections
import datetime
import hashlib
import http.server
import ipaddress
import json
import math
import os
import queue
import re
import shutil
import signal
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.dirname(HERE)
REPO = os.path.dirname(DEMO)
EVIDENCE = os.path.join(DEMO, "evidence")
for _p in (EVIDENCE, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import efhe_verify  # noqa: E402
import live_feed  # noqa: E402
import probe_reader  # noqa: E402

WEB = os.path.join(HERE, "web")
SEAL_PY = os.path.join(EVIDENCE, "efhe_seal.py")
MOCK_PY = os.path.join(EVIDENCE, "tests", "mock_efnet.py")
MODEL_JSON = os.path.join(DEMO, "model", "cdc-diabetes-lr4-v1.json")
WINDOWS = os.name == "nt"
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
SCHEMA = "edgefhe.demo.console-config.v1"
API = "/api/v1"
CONSOLE_HEADER = "X-EdgeFHE-Console"
CSP = "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'"
REPORT_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
SECURITY_HEADERS = (("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store"),
                    ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
                    ("Cross-Origin-Resource-Policy", "same-origin"))
STATIC = {"index.html": "text/html; charset=utf-8", "console.js": "text/javascript; charset=utf-8",
          "console.css": "text/css; charset=utf-8"}
MAX_BODY = 1024
MAX_DRAIN = 65536
SSE_STREAMS = 8
SSE_BATCH = 256
HEARTBEAT_S = 2.0
DECODE_FIRST_DELAY_S = 0.5
DECODE_RETRY_S = 0.7
ENERGY_DELAY_S = 1.0
IDLE_SPAN_S = 120.0
PROBE_KEEP_S = 180.0
PROBE_VIEW_MARGIN_S = 60.0  # replay and sealed view: samples from the first SYN - 60 s to the last close + 60 s
PROBE_VIEW_MAX = 12000      # at most this many probe rows in GET /api/v1/state (20 min at 10 Hz before thinning)
STOP_LEAD_S = 0.25          # a server stop line never precedes its connection's first packet by more than clock skew
STOP_LAG_S = 2.0            # ... and lies within 2 s of that connection's close
DEADLINE_FRACTION = 0.8     # a TLS_DEADLINE belongs only to a connection older than 0.8 x the server timeout
SERVER_TIMEOUT_MS = 30000   # run_server.sh --timeout-ms (the launcher line repeats it as timeout_ms=)
STATUS_ZH = {"PASS": "通過", "FAIL": "未通過", "FAIL-PENDING": "待重送確認", "INCOMPLETE": "資料不足",
             "BLOCKED": "無法檢查", "SKIP": "略過"}
QUICK_GATES = ("G2", "G3", "G4", "G5", "G6", "G8")
DEFAULTS = dict(iface=None, board_ip=None, port=18443, capture_filter=None, snaplen=0, buffer_mib=128,
                server="docker", image=None, container="efhe-cloud", publish="18443:18443", docker="docker",
                pki_dir=None, keylog=True, mock_connections=1000, evidence_root=r"C:\EdgeFHE_evidence",
                probe_port=None, probe_baud=115200, listen="127.0.0.1:8765", dumpcap=None, tshark=None,
                decode_timeout_s=10.0, schema=SCHEMA)
REFUSALS = {
    "CONFIG_FILE": "本主控台無法讀取設定檔 %(path)s（%(why)s）。",
    "CONFIG_KEY": "設定檔含有未知的設定值「%(key)s」；可用的設定值見 demo/console/console.example.json。",
    "CONFIG_VALUE": "設定值「%(key)s」不正確：%(why)s",
    "CONFIG_MISSING": "即時模式需要設定值「%(key)s」。",
    "TOOL_MISSING": "本主控台找不到 %(tool)s；請安裝 Wireshark（含 Npcap）或在設定檔中指定其路徑。",
    "DOCKER_UNAVAILABLE": "本主控台無法連線到 Docker（%(why)s）；請先啟動 Docker Desktop。",
    "IMAGE_MISSING": "本機沒有映像 %(image)s；請先依執行手冊建置雲端映像（展示網路沒有網際網路，無法下載）。",
    "CONTAINER_RUNNING": "名為 %(name)s 的容器正在執行；本主控台不接管既有的伺服器。請先確認該伺服器已不再使用，再以 docker stop 停止。",
    "PKI": "PKI 目錄 %(path)s 缺少 %(file)s；雲端憑證目錄應由 make_demo_pki.sh 建立。",
    "RUN_EXISTS": "證據資料夾 %(path)s 已存在且不是空的；本主控台不覆寫既有證據，請改用新的資料夾。",
    "RUN_IN_REPO": "證據資料夾 %(path)s 位於 repository 之內；擷取檔與金鑰紀錄不得進入 repository。",
    "RUN_CREATE": "本主控台無法建立證據資料夾 %(path)s（%(why)s）。",
    "RUN_ACL": "本主控台無法將證據資料夾 %(path)s 的存取權限限制為目前使用者與 SYSTEM（%(why)s）；證據資料夾將存放擷取檔與"
               " TLS 金鑰紀錄，因此本主控台不繼續執行。",
    "RUN_CHANGED": "證據資料夾 %(path)s 在本主控台限制存取權限之前已出現其他檔案；本主控台不使用此資料夾，請改用新的資料夾。",
    "PKI_MISSING": "PKI 目錄 %(path)s 不存在；雲端憑證目錄應由 make_demo_pki.sh 建立。",
    "PKI_ACL": "本主控台無法將 PKI 目錄 %(path)s 的存取權限限制為目前使用者與 SYSTEM（%(why)s）；PKI 目錄存放雲端 TLS"
               " 伺服器私鑰，因此本主控台不繼續執行。",
    "CAPTURE_START": "dumpcap 未能開始擷取（%(why)s）。",
    "SERVER_START": "雲端伺服器未能啟動（%(why)s）。",
    "SERVER_REFUSED": "雲端伺服器的啟動程式拒絕啟動：%(why)s",
    "SERVER_NOT_READY": "雲端伺服器在 %(seconds)s 秒內沒有在 server.log 寫出 %(missing)s。",
    "FEED_START": "即時封包來源（tshark）未能開始擷取（%(why)s）。",
    "LISTEN_BIND": "本主控台無法在 %(address)s 上監聽（%(why)s）。",
    "REPLAY_CAPTURE": "重播資料夾 %(path)s 中沒有 capture.pcapng。",
}
_print_lock = threading.Lock()


def say(line):
    with _print_lock:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass


class Refused(Exception):
    def __init__(self, code, **values):
        self.code = code
        self.values = values
        text = REFUSALS.get(code, code) % collections.defaultdict(lambda: "?", values)
        super().__init__(text)


def finite(obj):
    """JSON-safe copy: non-finite floats become None, tuples and sets become lists."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [finite(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return [finite(v) for v in sorted(obj)]
    return obj


def dumps(obj):
    return json.dumps(finite(obj), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def inside(path, root):
    """True when path lies in root. Compared after resolving links and with normcase: Windows paths are
    case-insensitive (the shell may report C:\\ while an argument says c:\\), and ntpath.commonpath keeps the case of
    its first argument. realpath keeps the extended-length and share spellings of the same folder (\\\\?\\C:\\...,
    \\\\?\\UNC\\localhost\\C$\\..., \\\\localhost\\C$\\...), so every existing ancestor of path is also compared with
    root by file identity (os.path.samefile), which holds for any spelling."""
    try:
        p = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        r = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        if os.path.commonpath([p, r]) == r:
            return True
    except ValueError:                                   # another drive or another spelling of the drive
        pass
    cur = os.path.abspath(path)
    while True:
        try:
            if os.path.exists(cur) and os.path.samefile(cur, root):
                return True
        except (OSError, ValueError):
            pass
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def docker_path(path):
    return os.path.abspath(path).replace("\\", "/")


# ------------------------------------------------------------------ private folders (NTFS access control)
# umask has no effect on NTFS: a new folder inherits its parent's DACL, which on C:\ grants every local account read
# access (BUILTIN\Users) and, below the root, modify access (Authenticated Users). The run folder holds the capture and
# the demo TLS key log, and the PKI folder holds the server private key, so the console restricts both to the current
# user and SYSTEM (README section 6.3.1 does the same by hand) and refuses to continue when that fails. It only
# warns (EVIDENCE_ROOT_ACL_WARNING, EVIDENCE_REPLAY_ACL_WARNING) about the evidence root and a replayed folder: those
# are the operator's step of README section 6.3.1, and the console changes only pki_dir and the session's run folder.
SYSTEM_SID = "S-1-5-18"
DENY_ACE_TYPES = frozenset(("D", "OD", "XD"))
SDDL_ALIASES = {"WD": "S-1-1-0", "CO": "S-1-3-0", "CG": "S-1-3-1", "OW": "S-1-3-4", "NU": "S-1-5-2", "IU": "S-1-5-4",
                "SU": "S-1-5-6", "AN": "S-1-5-7", "ED": "S-1-5-9", "PS": "S-1-5-10", "AU": "S-1-5-11",
                "RC": "S-1-5-12", "SY": SYSTEM_SID, "LS": "S-1-5-19", "NS": "S-1-5-20", "BA": "S-1-5-32-544",
                "BU": "S-1-5-32-545", "BG": "S-1-5-32-546", "PU": "S-1-5-32-547", "AO": "S-1-5-32-548",
                "SO": "S-1-5-32-549", "PO": "S-1-5-32-550", "BO": "S-1-5-32-551", "RU": "S-1-5-32-554",
                "RD": "S-1-5-32-555", "NO": "S-1-5-32-556", "AC": "S-1-15-2-1"}
ACL_MAX_ENTRIES = 500


class AclError(Exception):
    pass


def _system_tool(name):
    """A Windows tool from System32 by absolute path (never one found first on PATH)."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.path.join(root, "System32", name)


def current_user_sid():
    rc, out, err = run_quiet([_system_tool("whoami.exe"), "/user", "/fo", "csv", "/nh"], timeout=30)
    m = re.search(r'"(S-1-[0-9]+(?:-[0-9]+)+)"\s*$', out.strip())
    if rc != 0 or not m:
        raise AclError("whoami /user: %s" % ((err or out).strip()[:200] or "rc=%d" % rc))
    return m.group(1)


def acl_table(path, tree=True):
    """[(name, SDDL)] of path and every entry below it, from icacls /save /T (no /C: the first entry that cannot be
    read fails the call); with tree=False, of path alone. name is relative to the parent of path; the first item is
    path itself."""
    tmp = tempfile.mkdtemp(prefix="efhe_acl_")
    try:
        out = os.path.join(tmp, "acl.txt")
        rc, so, se = run_quiet([_system_tool("icacls.exe"), path, "/save", out] + (["/T"] if tree else []) + ["/Q"],
                               timeout=120)
        if rc != 0 or not os.path.isfile(out):
            raise AclError("icacls /save: %s" % ((se or so).strip()[-200:] or "rc=%d" % rc))
        with open(out, "rb") as f:
            text = f.read().decode("utf-16-le", "replace").lstrip("\ufeff")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2 or len(lines) % 2:
        raise AclError("icacls /save: %d lines" % len(lines))
    return list(zip(lines[0::2], lines[1::2]))


def acl_problems(sddl, user_sid, top=False):
    """What keeps the DACL in sddl from being private to user_sid and SYSTEM: SIDs of allow entries for anyone else
    (SDDL aliases such as BU or AU as written), 'NULL_DACL', and for the top folder 'INHERITS' while it still inherits
    from its parent. Deny entries only take access away and are ignored."""
    if "D:" not in sddl or "NO_ACCESS_CONTROL" in sddl:
        return ["NULL_DACL"]
    dacl = sddl.split("D:", 1)[1].split("S:", 1)[0]
    flags = dacl.split("(", 1)[0]
    allowed = (user_sid, SYSTEM_SID, "SY")
    out = [] if not top or "P" in flags else ["INHERITS"]
    for ace in re.findall(r"\(([^()]*)\)", dacl):
        f = ace.split(";")
        if len(f) < 6:
            out.append("ACE:" + ace[:40])
        elif f[0] not in DENY_ACE_TYPES and f[5] not in allowed and f[5] not in out:
            out.append(f[5])
    return out


def make_private(path, code):
    """Restrict path (a folder) and everything below it to the current user and SYSTEM, or raise Refused(code).

    Windows: icacls PATH /inheritance:r /grant:r *USER:(OI)(CI)F *S-1-5-18:(OI)(CI)F drops the inherited entries and
    grants full control to the user and SYSTEM; explicit entries for anyone else on PATH are removed with /remove:g;
    an entry below PATH that still grants anyone else is reset (icacls /reset) so that it inherits from PATH. The
    result is read back with icacls /save /T and checked entry by entry; none of these steps ever widens access.
    Elsewhere: chmod 0700 for folders and 0600 for files. Returns the number of entries checked."""
    path = os.path.abspath(path)
    try:
        if not os.path.isdir(path):
            raise AclError("不是資料夾")
        if not WINDOWS:
            n = 0
            for root, dirs, files in os.walk(path):
                for name, mode in [(root, 0o700)] + [(os.path.join(root, x), 0o600) for x in files]:
                    if not os.path.islink(name) and os.stat(name).st_mode & 0o077:
                        os.chmod(name, mode)
                    if not os.path.islink(name) and os.stat(name).st_mode & 0o077:
                        raise AclError("chmod %s" % name)
                    n += 1
            return n
        icacls = _system_tool("icacls.exe")
        user = current_user_sid()
        rc, so, se = run_quiet([icacls, path, "/inheritance:r", "/grant:r", "*%s:(OI)(CI)F" % user,
                                "*%s:(OI)(CI)F" % SYSTEM_SID, "/Q"], timeout=60)
        if rc != 0:
            raise AclError("icacls /inheritance:r /grant:r: %s" % ((se or so).strip()[-200:] or "rc=%d" % rc))
        table = acl_table(path)
        if len(table) > ACL_MAX_ENTRIES:
            raise AclError("超過 %d 個項目" % ACL_MAX_ENTRIES)
        for sid in acl_problems(table[0][1], user, top=True):
            sid = SDDL_ALIASES.get(sid, sid)
            if not re.fullmatch(r"S-1-[0-9]+(?:-[0-9]+)+", sid):
                raise AclError("%s 的存取控制項目 %s" % (table[0][0], sid))
            rc, so, se = run_quiet([icacls, path, "/remove:g", "*" + sid, "/Q"], timeout=60)
            if rc != 0:
                raise AclError("icacls /remove:g %s: %s" % (sid, (se or so).strip()[-200:] or "rc=%d" % rc))
        parent = os.path.dirname(path)
        for name, sddl in table[1:]:
            if acl_problems(sddl, user):
                rc, so, se = run_quiet([icacls, os.path.join(parent, name), "/reset", "/Q"], timeout=60)
                if rc != 0:
                    raise AclError("icacls /reset %s: %s" % (name, (se or so).strip()[-200:] or "rc=%d" % rc))
        table = acl_table(path)
        bad = [(name, p) for i, (name, sddl) in enumerate(table) for p in acl_problems(sddl, user, top=i == 0)]
        if bad:
            raise AclError("、".join("%s：%s" % (name, p) for name, p in bad[:4]))
        return len(table)
    except (AclError, OSError) as e:
        raise Refused(code, path=path, why=str(e)[:300])


# Groups that hold every local account (or anyone): Everyone, Anonymous, Interactive, Authenticated Users, Users, Guests
WIDE_SIDS = ("S-1-1-0", "S-1-5-7", "S-1-5-4", "S-1-5-11", "S-1-5-32-545", "S-1-5-32-546")


def wide_grants(path, tree=False):
    """Read-only check: the groups of WIDE_SIDS that an allow entry of path (with tree, of path or any entry below it)
    grants access to, as SIDs; [] when private, None when the access list cannot be read (or not on Windows)."""
    if not WINDOWS or not os.path.isdir(path):
        return None
    try:
        user = current_user_sid()
        found = []
        for i, (_name, sddl) in enumerate(acl_table(path, tree=tree)):
            for sid in acl_problems(sddl, user, top=i == 0):
                sid = SDDL_ALIASES.get(sid, sid)
                if sid in WIDE_SIDS and sid not in found:
                    found.append(sid)
        return found
    except (AclError, OSError):
        return None


def warn_wide(marker, path, tree, why):
    """Print marker when path still grants WIDE_SIDS access (the operator's README section 6.3.1 step is missing)."""
    found = wide_grants(path, tree)
    if found:
        say("%s path=%s grants=%s %s操作人員應依 README 第 6.3.1 節以 icacls /inheritance:r 限制此資料夾，並以 "
            "icacls %s /T 確認只剩目前使用者與 SYSTEM。" % (marker, path, ",".join(found), why, path))
    return found


# ------------------------------------------------------------------ configuration
def load_config(path, live=True, overrides=None):
    cfg = dict(DEFAULTS)
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise Refused("CONFIG_FILE", path=path, why=type(e).__name__)
        if not isinstance(data, dict):
            raise Refused("CONFIG_FILE", path=path, why="not a JSON object")
        for k, v in data.items():
            if k.startswith("_"):
                continue
            if k not in DEFAULTS:
                raise Refused("CONFIG_KEY", key=k)
            cfg[k] = v
    for k, v in (overrides or {}).items():
        if v is not None:
            cfg[k] = v
    return validate_config(cfg, live)


def _bad(key, why):
    raise Refused("CONFIG_VALUE", key=key, why=why)


def validate_config(cfg, live=True):
    if cfg.get("schema") not in (None, SCHEMA):
        _bad("schema", "必須是 %s。" % SCHEMA)
    port = cfg.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        _bad("port", "必須是 1 至 65535 的整數。")
    if cfg["server"] not in ("docker", "mock", "none"):
        _bad("server", "必須是 docker、mock 或 none。")
    for key in ("snaplen", "buffer_mib", "mock_connections", "probe_baud"):
        v = cfg.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            _bad(key, "必須是非負整數。")
    if cfg["snaplen"] and not 64 <= cfg["snaplen"] <= 262144:
        _bad("snaplen", "必須是 0（完整封包）或 64 至 262144。")
    if not 1 <= cfg["buffer_mib"] <= 1024:
        _bad("buffer_mib", "必須是 1 至 1024。")
    if not isinstance(cfg.get("keylog"), bool):
        _bad("keylog", "必須是 true 或 false。")
    t = cfg.get("decode_timeout_s")
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not 1 <= t <= 120:
        _bad("decode_timeout_s", "必須是 1 至 120 秒。")
    host, _, lport = str(cfg.get("listen") or "").rpartition(":")
    if host != "127.0.0.1" or not lport.isdigit() or not 0 <= int(lport) <= 65535:
        _bad("listen", "本主控台只在 127.0.0.1 上提供畫面，格式為 127.0.0.1:PORT。")
    cfg["listen_port"] = int(lport)
    for key in ("capture_filter", "iface", "image", "pki_dir", "probe_port", "dumpcap", "tshark", "evidence_root"):
        v = cfg.get(key)
        if v is not None and (not isinstance(v, str) or not v.strip() or "\n" in v or "\r" in v):
            _bad(key, "必須是單行的非空字串，或省略。")
    if cfg.get("board_ip") is not None:
        try:
            cfg["board_ip"] = str(ipaddress.IPv4Address(cfg["board_ip"]))
        except (ipaddress.AddressValueError, ValueError):
            _bad("board_ip", "必須是 IPv4 位址字面值（例如 192.0.2.20）。")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", str(cfg.get("container") or "")):
        _bad("container", "必須是 Docker 容器名稱（英數字、底線、句點或連字號）。")
    m = re.fullmatch(r"(?:(\d{1,3}(?:\.\d{1,3}){3}):)?(\d{1,5}):(\d{1,5})", str(cfg.get("publish") or ""))
    if not m:
        _bad("publish", "格式為 [IP:]HOSTPORT:18443。")
    if cfg["server"] == "docker" and (int(m.group(3)) != 18443 or int(m.group(2)) != port):
        _bad("publish", "容器內的伺服器固定使用 18443；主機端的連接埠必須等於設定值 port（%d）。" % port)
    if live:
        for key in ("iface", "board_ip"):
            if not cfg.get(key):
                raise Refused("CONFIG_MISSING", key=key)
        if cfg["server"] == "docker" and not cfg.get("image"):
            raise Refused("CONFIG_MISSING", key="image")
        if cfg["server"] in ("docker", "mock") and not cfg.get("pki_dir"):
            raise Refused("CONFIG_MISSING", key="pki_dir")
    if not cfg.get("capture_filter") and cfg.get("board_ip"):
        cfg["capture_filter"] = "host %s" % cfg["board_ip"]
    return cfg


def pki_files(pki_dir):
    """The cloud bundle of make_demo_pki.sh: pki_dir itself or pki_dir/cloud (ca.pem, server.pem, server.key,
    client.pin). Returns dict(cloud=dir, server_pem, client_pin)."""
    for d in (pki_dir, os.path.join(pki_dir, "cloud")):
        if os.path.isfile(os.path.join(d, "server.pem")) and os.path.isfile(os.path.join(d, "client.pin")):
            for name in ("ca.pem", "server.key"):
                if not os.path.isfile(os.path.join(d, name)):
                    raise Refused("PKI", path=d, file=name)
            return dict(cloud=os.path.abspath(d), server_pem=os.path.join(d, "server.pem"),
                        client_pin=os.path.join(d, "client.pin"))
    raise Refused("PKI", path=pki_dir, file="server.pem 與 client.pin")


def certificate_pin(pem_path):
    with open(pem_path, encoding="ascii", errors="replace") as f:
        pem = f.read()
    m = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.S)
    if not m:
        raise Refused("PKI", path=os.path.dirname(pem_path), file="server.pem（PEM 憑證）")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(m.group(0))).hexdigest()


def read_pin(path):
    with open(path, encoding="ascii", errors="replace") as f:
        v = f.read().strip().lower()
    if not efhe_verify.is_hex64(v):
        raise Refused("PKI", path=os.path.dirname(path), file="client.pin（64 個小寫十六進位字元）")
    return v


# ------------------------------------------------------------------ server.log lines
def parse_server_line(line, n=0):
    m = efhe_verify.EPOCH_PREFIX.match(line)
    epoch, text = (float(m.group(1)), m.group(2)) if m else (None, line)
    e = dict(n=n, epoch=epoch, text=text[:600], kind="other", data={})
    if text.startswith("{"):
        try:
            j = json.loads(text)
        except ValueError:
            j = None
        if isinstance(j, dict):
            st = j.get("status")
            e["data"] = j
            if st == "CPP_E2E_SERVER_READY":
                e["kind"] = "ready"
            elif st == "CPP_E2E_RESPONSE_WRITTEN_NOT_OWNER_ACCEPTANCE":
                e["kind"] = "response"
                e["data"] = dict(sequence=j.get("sequence"), request_sha256=j.get("request_sha256"),
                                 response_payload_sha256=j.get("response_payload_sha256"), cloud_ns=j.get("cloud_ns"))
            elif st == "CPP_E2E_SERVER_STOPPED":
                e["kind"] = "stopped"
            else:
                e["kind"] = "json"
        return e
    head = text.split(" ", 1)[0]
    if head in ("EDGEFHE_CLOUD_SERVER_START", "EDGEFHE_CLOUD_SERVER_EXIT"):
        e["kind"] = "launcher_start" if head.endswith("START") else "launcher_exit"
        e["data"] = dict(p.split("=", 1) for p in text.split()[1:] if "=" in p)
    elif head == "WARNING_DEMO_TLS_KEYLOG_ENABLED":
        e["kind"] = "keylog_warning"
        rest = text.split()[1:2]
        e["data"] = dict(path=rest[0].split("=", 1)[-1] if rest else None)
    elif head == "CPP_E2E_SERVER_CONNECTION_STOP":
        e["kind"] = "connection_stop"
        e["data"] = dict(code=text.split(" ", 1)[1].strip() if " " in text else "")
    elif head.startswith("STOP_") or head == "EDGEFHE_CLOUD_SERVER_REFUSED":
        e["kind"] = "stop"
    return e


# ------------------------------------------------------------------ events
class EventBus:
    """Envelopes {v, seq, type, t, data} in seq order for the event streams (bounded)."""

    def __init__(self, capacity=20000):
        self._cond = threading.Condition()
        self._buf = collections.deque(maxlen=capacity)
        self._seq = 0
        self._closed = False

    def emit(self, typ, data):
        with self._cond:
            self._seq += 1
            text = dumps(dict(v=1, seq=self._seq, type=typ, t=round(time.time(), 6), data=data))
            self._buf.append((self._seq, text))
            self._cond.notify_all()
            return self._seq

    def last(self):
        with self._cond:
            return self._seq

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self):
        return self._closed

    def after(self, seq, timeout):
        """(events, gap): up to SSE_BATCH (seq, text) pairs after seq; gap is True when older events were dropped."""
        with self._cond:
            if self._seq <= seq and not self._closed and timeout > 0:
                self._cond.wait_for(lambda: self._seq > seq or self._closed, timeout)
            if not self._buf or self._seq <= seq:
                return [], False
            first = self._buf[0][0]
            gap = seq + 1 < first
            start = max(seq + 1 - first, 0)
            return [self._buf[i] for i in range(start, min(len(self._buf), start + SSE_BATCH))], gap


# ------------------------------------------------------------------ processes
class Proc:
    """A child process in a process group of its own, stopped with CTRL_BREAK_EVENT (SIGINT elsewhere)."""

    def __init__(self, name, args, stdout=None, stderr=None, env=None):
        self.name = name
        self.args = args
        self.p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=stdout or subprocess.DEVNULL,
                                  stderr=stderr or subprocess.DEVNULL, env=env, creationflags=NEW_GROUP)

    def alive(self):
        return self.p.poll() is None

    def interrupt(self, wait=20.0):
        if self.p.poll() is None:
            try:
                if WINDOWS:
                    os.kill(self.p.pid, signal.CTRL_BREAK_EVENT)
                else:
                    self.p.send_signal(signal.SIGINT)
            except OSError:
                pass
            try:
                self.p.wait(wait)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(10)
        return self.p.returncode

    def terminate(self, wait=10.0):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(wait)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(10)
        return self.p.returncode


def run_quiet(args, timeout=60):
    """A short tool run (docker CLI) in a process group of its own, so that Ctrl+C in the console window never
    interrupts it halfway."""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=NEW_GROUP)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 999, "", "%s: %s" % (type(e).__name__, e)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def dumpcap_counts(text):
    m = re.findall(r"Packets received/dropped on interface[^:]*:\s*(\d+)/(\d+)", text)
    if m:
        return dict(received=sum(int(a) for a, _b in m), dropped=sum(int(b) for _a, b in m), final=True)
    m = re.findall(r"Packets(?: captured)?: (\d+)", text)
    return dict(received=int(m[-1]) if m else 0, dropped=None, final=False)


# ------------------------------------------------------------------ the console
class Console:
    def __init__(self, cfg, mode, run_dir, port):
        self.cfg = cfg
        self.mode = mode
        self.run_dir = os.path.abspath(run_dir)
        self.port = port
        self.capture_path = os.path.join(self.run_dir, "capture.pcapng")
        if mode == "replay" and not os.path.isfile(self.capture_path) and \
                os.path.isfile(os.path.join(self.run_dir, "capture.pcap")):
            self.capture_path = os.path.join(self.run_dir, "capture.pcap")
        self.stats_path = os.path.join(self.run_dir, "capture_stats.txt")
        self.server_log_path = os.path.join(self.run_dir, "server.log")
        self.keylog_path = os.path.join(self.run_dir, "tls_keys.log")
        self.probe_path = os.path.join(self.run_dir, "probe.csv")
        self.lock = threading.RLock()
        self.bus = EventBus()
        self.tracker = live_feed.SessionTracker(port, cfg.get("board_ip"), on_event=self._on_tracker)
        self.extra = {}
        self.server_lines = collections.deque(maxlen=400)
        self.server_count = 0
        self.responses = []
        self.stops = []
        self.unmatched = []
        self.packets = collections.deque(maxlen=160)
        self.samples = collections.deque(maxlen=36000)
        self.pending_samples = []
        self.dirty = set()
        self.status_dirty = set()
        self.decode_q = queue.Queue()
        self.stopping = threading.Event()
        self.procs = {}
        self.threads = []
        self.tmp = tempfile.mkdtemp(prefix="efhe_console_")
        self.tshark_path = efhe_verify.find_tool("tshark", cfg.get("tshark"))
        self._tshark = None
        self.pins_dir = self.run_dir
        self.httpd = None
        self.model = self._load_model()
        self.board_mtime = None
        self.board_runs = None
        self.status = dict(
            capture=dict(state="idle", file=self.capture_path, iface=cfg.get("iface"),
                         filter=cfg.get("capture_filter"), packets=0, dropped=None, message=""),
            server=dict(state="idle", kind=cfg.get("server"), image=cfg.get("image"), container=cfg.get("container"),
                        port=port, ready=False, keylog_warning=False, launcher={}, idle_deadlines=0, stops=[],
                        message=""),
            feed=dict(state="idle", packets=0, other_packets=0, foreign=0, message=""),
            probe=dict(state="off" if not cfg.get("probe_port") else "idle", port=cfg.get("probe_port"),
                       rate_hz=0.0, samples=0, message=""),
            decoder=dict(state="idle", queue=0, message=""))
        self.seal = dict(state="none", lines=[], sha256=None, message="")
        self.verify = dict(state="none", lines=[], verdict=None, evidence_pass=None, gates={}, message="")
        self.board = dict(state="waiting", file=None, rows=0, runs=0, message="")
        self.last_status_emit = 0.0
        self.last_probe_emit = 0.0
        self.last_stats_read = 0.0
        self.last_proc_check = 0.0
        self.last_board_check = 0.0

    # -------------------------------------------------------------- helpers
    def _load_model(self):
        try:
            with open(MODEL_JSON, "rb") as f:
                raw = f.read()
            m = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            return None
        # bias_int is about -6.7e17, beyond the 2^53 that a JavaScript number holds exactly: the page shows the text
        return dict(model_id=m.get("model_id"), sha256=hashlib.sha256(raw).hexdigest(), features=m.get("features"),
                    weights_int=m.get("weights_int"), bias_int=m.get("bias_int"), bias_int_text=str(m.get("bias_int")),
                    weight_scale=m.get("weight_scale"), output_scale=m.get("output_scale"), q=list(live_feed.Q))

    def tshark(self):
        if self._tshark is None:
            self._tshark = efhe_verify.Tshark(self.tshark_path)
        return self._tshark

    def set_status(self, key, **values):
        with self.lock:
            self.status[key].update(values)
            self.status_dirty.add(key)

    def notice(self, level, text):
        self.bus.emit("notice", dict(level=level, text=text))

    def _x(self, sid):
        x = self.extra.get(sid)
        if x is None:
            x = dict(decode=dict(state="waiting" if self.mode == "live" else "pending", attempts=0),
                     server_log=None, energy=None, board=None, observed_close=None, connection_stop=None)
            self.extra[sid] = x
        return x

    # -------------------------------------------------------------- tracker events (called under self.lock)
    def _on_tracker(self, kind, sess, extra):
        x = self._x(sess.id)
        self.dirty.add(sess.id)
        if kind == "open" and self.mode == "live":
            say("EVIDENCE_SESSION_OPEN id=%s board=%s:%s partial=%d" % (sess.id, sess.board_ip, sess.board_port,
                                                                       1 if sess.partial else 0))
        elif kind == "packet":
            pkt, recs = extra["packet"], extra["records"]
            fl = pkt["flags"]
            if recs or fl["syn"] or fl["fin"] or fl["rst"]:
                flags = "".join(n for n, k in (("SYN ", "syn"), ("FIN ", "fin"), ("RST ", "rst")) if fl[k]).strip()
                item = dict(s=sess.id, frame=pkt["frame"], t=pkt["t"], dir="up" if pkt["dport"] == self.port else "down",
                            tcp_len=pkt["tcp_len"], flags=flags,
                            recs=[[r["len"], r["short"], r["approx"]] for r in recs])
                self.packets.append(item)
                if self.mode == "live":
                    self.bus.emit("packet", item)
        elif kind == "close":
            x["observed_close"] = time.time()
            if self.mode == "live":
                c = sess.counts()
                say("EVIDENCE_SESSION_CLOSED id=%s reason=%s up_limbs=%d down_limbs=%d signature=%s" % (
                    sess.id, sess.close_reason, c["up"]["limbs"], c["down"]["limbs"],
                    sess.signature()["classification"]))
                if not sess.records:                     # a refused SYN/RST: nothing to decrypt
                    x["decode"] = dict(state="notls", attempts=0)
                elif self.cfg.get("keylog", True):
                    self.decode_q.put((sess.id, False))
                    x["decode"] = dict(state="queued", attempts=0)
                else:
                    x["decode"] = dict(state="nokeys", attempts=0)

    def feed_packet(self, pkt):
        with self.lock:
            self.tracker.feed(pkt)
            self.status["feed"]["packets"] = self.tracker.packets
            self.status["feed"]["other_packets"] = self.tracker.other_packets
            self.status["feed"]["foreign"] = self.tracker.foreign
            self.status_dirty.add("feed")

    # -------------------------------------------------------------- server.log
    def server_line(self, line):
        with self.lock:
            self.server_count += 1
            e = parse_server_line(line, self.server_count)
            self.server_lines.append(e)
            st = self.status["server"]
            if e["kind"] == "ready":
                st["ready"] = True
                st["state"] = "ready"
                st["ready_epoch"] = e["epoch"]
            elif e["kind"] == "keylog_warning":
                st["keylog_warning"] = True
            elif e["kind"] == "launcher_start":
                st["launcher"] = e["data"]
            elif e["kind"] == "launcher_exit":
                st["state"] = "exited"
                st["rc"] = e["data"].get("rc")
            elif e["kind"] == "stopped":
                st["stopped"] = e["data"]
            elif e["kind"] == "response":
                self.responses.append(e)
            elif e["kind"] == "connection_stop":
                e["session"], e["idle"] = None, False          # rematch() attaches it to a session, or marks it idle
                self.stops.append(e)
            st["last_epoch"] = e["epoch"]
            self.status_dirty.add("server")
        if self.mode == "live":
            self.bus.emit("server_log", e)
            if e["kind"] == "response":
                d = e["data"]
                say("EVIDENCE_SERVER_RESPONSE request_sha256=%s response_payload_sha256=%s cloud_ns=%s" % (
                    d.get("request_sha256"), d.get("response_payload_sha256"), d.get("cloud_ns")))
        return e

    def _server_timeout_s(self):
        try:
            return int(self.status["server"]["launcher"].get("timeout_ms") or SERVER_TIMEOUT_MS) / 1000.0
        except (TypeError, ValueError):
            return SERVER_TIMEOUT_MS / 1000.0

    def _stop_session(self, e, timeout_s):
        """The session that a CPP_E2E_SERVER_CONNECTION_STOP line belongs to, or None.
        The server logs the stop after it has closed that connection (server.cpp), so the connection was alive at the
        stop: it began before the stop (up to STOP_LEAD_S of clock skew) and it is still open or closed within
        STOP_LAG_S of it. A TLS_DEADLINE belongs to a connection only when that connection is older than the timeout
        (handshake and read deadlines are 30 s); otherwise it is the accept deadline of an idle server, logged every
        30 s with no SYN behind it, and the screen shows it as idle, not as a failure."""
        t = e["epoch"]
        if t is None:
            return None
        deadline = (e["data"].get("code") or "") == "TLS_DEADLINE"
        best, best_d = None, None
        for s in self.tracker.sessions:
            start = s.t_syn or s.t_first
            if deadline and start > t - DEADLINE_FRACTION * timeout_s:
                continue
            if not deadline and start > t + STOP_LEAD_S:
                continue
            d = 0.0 if s.state == "open" else abs(s.end_time() - t)
            if d > STOP_LAG_S:
                continue
            if best is None or d < best_d:
                best, best_d = s.id, d
        return best

    def _match_stops(self):
        """Attach every connection stop to its session (called under self.lock, from rematch())."""
        timeout_s = self._server_timeout_s()
        attached = {}
        for e in self.stops:
            sid = self._stop_session(e, timeout_s)
            e["session"] = sid
            e["idle"] = sid is None and e["data"].get("code") == "TLS_DEADLINE"
            if sid is not None:
                attached[sid] = e
        for s in self.tracker.sessions:
            x = self._x(s.id)
            e = attached.get(s.id)
            new = dict(code=e["data"].get("code"), epoch=e["epoch"], n=e["n"]) if e else None
            if x["connection_stop"] != new:
                x["connection_stop"] = new
                self.dirty.add(s.id)
        st = self.status["server"]
        stops = [dict(n=e["n"], epoch=e["epoch"], code=e["data"].get("code"), session=e["session"], idle=e["idle"])
                 for e in self.stops[-40:]]
        idle = sum(1 for e in self.stops if e["idle"])
        if stops != st["stops"] or idle != st["idle_deadlines"]:
            st["stops"], st["idle_deadlines"] = stops, idle
            self.status_dirty.add("server")

    def rematch(self):
        """Attach server.log responses to sessions: by request_sha256 once decoded, else by time (tentative)."""
        with self.lock:
            by_hash = {}
            for e in self.responses:
                by_hash.setdefault(e["data"].get("request_sha256"), []).append(e)
            used = set()
            for s in self.tracker.sessions:
                x = self._x(s.id)
                req = self._decoded_request(s.id)
                if req and req.get("request_sha256") in by_hash:
                    e = by_hash[req["request_sha256"]][-1]
                    if (x["server_log"] or {}).get("n") != e["n"] or x["server_log"].get("match") != "hash":
                        x["server_log"] = dict(e["data"], n=e["n"], epoch=e["epoch"], match="hash")
                        self.dirty.add(s.id)
                    used.add(e["n"])
            for s in self.tracker.sessions:
                x = self._x(s.id)
                sl = x["server_log"]
                if sl and sl.get("match") == "hash":
                    continue
                if sl and sl.get("match") == "time" and sl.get("n") not in used:
                    used.add(sl["n"])
                    continue
                ph = s.phases()
                lo = (ph["request_first"] or s.t_first) - 2.0
                hi = (s.t_close if s.t_close is not None else time.time()) + 2.0
                for e in self.responses:
                    if e["n"] in used or e["epoch"] is None:
                        continue
                    if lo <= e["epoch"] <= hi and ph["request_first"] is not None:
                        x["server_log"] = dict(e["data"], n=e["n"], epoch=e["epoch"], match="time")
                        used.add(e["n"])
                        self.dirty.add(s.id)
                        break
            self.unmatched = [e for e in self.responses if e["n"] not in used]
            self._match_stops()

    def _decoded_request(self, sid):
        res = (self.extra.get(sid) or {}).get("decode", {}).get("result")
        if not res:
            return None
        for fr in res.get("frames", []):
            if fr.get("kind") == "request":
                return fr
        return None

    def _decoded_response(self, sid):
        res = (self.extra.get(sid) or {}).get("decode", {}).get("result")
        if not res:
            return None
        for fr in res.get("frames", []):
            if fr.get("kind") == "response":
                return fr
        return None

    # -------------------------------------------------------------- decode
    def filter_session(self, src, dst, board_port, t_lo, t_hi):
        """Write the packets of one connection (board_port <-> server port, capture time within [t_lo, t_hi]) of src
        to dst with tshark. The decode then reads a file that holds this connection only: its TCP stream 0 is also TLS
        stream 0, which tshark's 'follow,tls' counts separately from tcp.stream (a TCP stream without TLS, such as a
        refused SYN/RST, shifts the TLS numbering), a later connection from the same board port stays out, and the
        decode time no longer grows with the capture. A source that ends inside a packet (the growing capture) still
        gives every complete packet before that point."""
        ts = self.tshark()
        flt = "tcp.port==%d && tcp.port==%d && frame.time_epoch >= %.6f && frame.time_epoch <= %.6f" % (
            board_port, self.port, t_lo, t_hi)
        args = [ts.path, "-r", src, "-n", "-Y", flt, "-F", "pcapng", "-w", dst]
        r = subprocess.run(args, capture_output=True, env=ts.env, stdin=subprocess.DEVNULL, creationflags=NEW_GROUP)
        if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
            raise efhe_verify.VerifyError("SESSION_FILTER_FAILED rc=%d %s" % (
                r.returncode, r.stderr.decode("utf-8", "replace").strip()[-200:]))
        return dst

    def decode_once(self, sid, copy=True):
        """(quick_session result or None, exact records or None, error text or None) for one session. copy=True
        (live) first copies the growing capture.pcapng; either way the session is filtered into a file of its own in
        the temporary directory, which is deleted afterwards."""
        with self.lock:
            sess = next((s for s in self.tracker.sessions if s.id == sid), None)
            if sess is None:
                return None, None, "unknown session"
            board_port, t0 = sess.board_port, (sess.t_syn or sess.t_first)
            t_hi = (sess.t_close if sess.t_close is not None else time.time()) + live_feed.LATE_PACKET_S
        if not os.path.isfile(self.keylog_path):
            return None, None, "no tls_keys.log"
        src = self.capture_path
        made = []
        try:
            if copy:
                src = os.path.join(self.tmp, "copy_%s.pcapng" % sid)
                made.append(src)
                try:
                    shutil.copyfile(self.capture_path, src)
                except OSError as e:
                    return None, None, "copy: %s" % e
            path = os.path.join(self.tmp, "session_%s.pcapng" % sid)
            made.append(path)
            try:
                self.filter_session(src, path, board_port, t0 - 1.0, t_hi)
                r = efhe_verify.quick_session(path, self.keylog_path, board_port, port=self.port,
                                              tshark_path=self.tshark_path, pins=self.pins_dir)
            except Exception as e:                 # a growing copy can end inside a block, or tshark may be missing
                return None, None, "%s: %s" % (type(e).__name__, str(e)[:200])
            recs = None
            if r.get("complete"):
                try:
                    rows = self.tshark().fields(path, self.port, self.keylog_path, live_feed.KEYED_FIELDS,
                                                "tls && tcp.port==%d && tcp.port==%d" % (board_port, self.port))
                    parsed = [p for p in live_feed.keyed_rows_from_dicts(rows) if p["t"] >= (t0 or 0) - 1.0]
                    recs = live_feed.records_from_keyed(parsed, self.port, board_port)
                except Exception:
                    recs = None
            return r, recs, None
        finally:
            for p in made:
                try:
                    os.remove(p)
                except OSError:
                    pass

    def apply_decode(self, sid, r, recs, attempts, started, after_seal=False):
        summary = summarize_decode(r)
        with self.lock:
            x = self._x(sid)
            sess = next(s for s in self.tracker.sessions if s.id == sid)
            if recs:                                     # dumpcap's records, labelled from the decrypted types
                sess.records = recs
                sess.records_source = "capture"
            done = time.time()
            live = self.mode == "live" and not after_seal  # a replay has no wall-clock relation to the run
            after = done - x["observed_close"] if x.get("observed_close") and live else None
            x["decode"] = dict(state="done", attempts=attempts, ok=bool(r.get("ok")), started=started, finished=done,
                               after_close_s=round(after, 3) if after is not None else None, after_seal=after_seal,
                               result=summary)
            self.dirty.add(sid)
        self.rematch()
        return summary

    def _decode_job(self, sid, after_seal=False):
        """Decode one closed session from a copy of the growing capture, retrying until decode_timeout_s after the
        close; after the seal (after_seal) the capture is complete and one attempt on a copy of it suffices."""
        timeout = float(self.cfg.get("decode_timeout_s") or 10.0)
        with self.lock:
            x = self._x(sid)
            base = time.time() if after_seal else (x.get("observed_close") or time.time())
        deadline = base + timeout
        started = time.time()
        wait = base + DECODE_FIRST_DELAY_S - time.time()
        if wait > 0 and not after_seal:
            self.stopping.wait(wait)
        attempts, last_reason = 0, None
        while not self.stopping.is_set():
            attempts += 1
            with self.lock:
                x["decode"] = dict(state="running", attempts=attempts, reason=last_reason, after_seal=after_seal)
                self.dirty.add(sid)
            r, recs, err = self.decode_once(sid, copy=True)
            if r is not None and r.get("complete"):
                s = self.apply_decode(sid, r, recs, attempts, started, after_seal)
                with self.lock:
                    after = x["decode"].get("after_close_s")
                say("EVIDENCE_SESSION_DECODED id=%s after_close_s=%s after_seal=%d attempts=%d classification=%s "
                    "request_sha256=%s response_payload_sha256=%s gates=%s server_log=%s" % (
                        sid, after, 1 if after_seal else 0, attempts, s.get("classification"), s.get("request_sha256"),
                        s.get("response_payload_sha256"),
                        ",".join("%s:%s" % (g, s["gates"][g]["status"]) for g in QUICK_GATES if g in s["gates"]),
                        (x.get("server_log") or {}).get("match")))
                return
            last_reason = err or (r or {}).get("reason")
            if time.time() + DECODE_RETRY_S > deadline:
                break
            self.stopping.wait(DECODE_RETRY_S)
        with self.lock:
            x["decode"] = dict(state="deferred", attempts=attempts, reason=last_reason, after_seal=after_seal)
            self.dirty.add(sid)
        say("EVIDENCE_SESSION_DECODE_DEFERRED id=%s attempts=%d after_seal=%d reason=%s" % (
            sid, attempts, 1 if after_seal else 0, last_reason))

    def decoder_loop(self):
        self.set_status("decoder", state="ready")
        while not self.stopping.is_set():
            try:
                sid, after_seal = self.decode_q.get(timeout=0.5)
            except queue.Empty:
                continue
            self.set_status("decoder", state="running", queue=self.decode_q.qsize())
            try:
                self._decode_job(sid, after_seal)
            except Exception as e:                       # never let one session stop the decoder
                with self.lock:
                    self._x(sid)["decode"] = dict(state="deferred", attempts=0, reason="%s: %s" % (
                        type(e).__name__, e))
                    self.dirty.add(sid)
            self.set_status("decoder", state="ready", queue=self.decode_q.qsize())

    # -------------------------------------------------------------- probe and energy
    def on_probe_sample(self, host_s, sample):
        row = (host_s,) + tuple(sample)
        with self.lock:
            self.samples.append(row)
            self.pending_samples.append([round(host_s, 3), sample[3], sample[4]])
            self.status["probe"]["samples"] += 1

    def on_probe_status(self, state, detail):
        self.set_status("probe", state=state, message=detail)

    def windows(self, now=None):
        """Energy windows [SYN - 2 s, FIN + 0.5 s] of every session; an open session extends to now."""
        out = []
        for s in self.tracker.sessions:
            start = s.t_syn or s.t_first
            end = s.end_time() if s.state != "open" else (now or time.time())
            out.append(probe_reader.session_window(start, end))
        return out

    def compute_energy(self, force=False):
        """ΔE ≈ per closed session. The idle baseline is the median power of the samples outside every session
        window from IDLE_SPAN_S before the window to 10 s after it (all samples outside windows as a fallback)."""
        with self.lock:
            if not self.samples:
                return
            todo = [s for s in self.tracker.sessions if s.t_close is not None
                    and (force or self._x(s.id)["energy"] is None)]
            now = time.time() if self.mode == "live" else self.samples[-1][0]
            if self.mode == "live":
                todo = [s for s in todo if now >= s.t_close + probe_reader.POST_S + ENERGY_DELAY_S]
            if not todo and not force:
                return
            samples = list(self.samples)
            wins = self.windows(now)
            overall = probe_reader.idle_power_mw(samples, wins, now - IDLE_SPAN_S if self.mode == "live" else None)
            self.status["probe"]["idle_mw"] = overall
            self.status_dirty.add("probe")
            for s in todo:
                start, end = s.t_syn or s.t_first, s.end_time()
                a, b = probe_reader.session_window(start, end)
                idle = probe_reader.idle_power_mw(samples, wins, a - IDLE_SPAN_S, b + 10.0)
                if idle is None:
                    idle = probe_reader.idle_power_mw(samples, wins)
                x = self._x(s.id)
                x["energy"] = finite(probe_reader.session_energy(samples, start, end, idle))
                self.dirty.add(s.id)

    # -------------------------------------------------------------- board public log (after the seal)
    def check_board(self):
        path = os.path.join(self.run_dir, "board_public.jsonl")
        if not os.path.isfile(path):
            return
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if mtime == self.board_mtime:
            return
        self.board_mtime = mtime
        try:
            bp = efhe_verify.parse_board_public(path)
        except OSError as e:
            with self.lock:
                self.board.update(state="error", message=str(e))
            return
        disc = os.path.join(self.run_dir, "disclosure")
        n_disc = len([f for f in os.listdir(disc) if f.endswith(".json")]) if os.path.isdir(disc) else 0
        with self.lock:
            self.board_runs = bp["runs"]
            self.board.update(state="joined", file="board_public.jsonl", rows=bp["rows"], runs=len(bp["runs"]),
                              disclosures=n_disc, message="")
            self._join_board()
        self.bus.emit("board", dict(self.board))

    def _join_board(self):
        runs = self.board_runs or {}
        for s in self.tracker.sessions:
            req = self._decoded_request(s.id)
            resp = self._decoded_response(s.id)
            if not req:
                continue
            rh = req.get("request_sha256")
            found = None
            for run, r in runs.items():
                if rh in r["requests"]:
                    found = (run, r)
                    break
            x = self._x(s.id)
            if found is None:
                x["board"] = dict(found=False)
            else:
                run, r = found
                pair = [p for p in r["pairs"] if p[0] == rh]
                x["board"] = dict(found=True, run=run, request_sha256=rh,
                                  response_payload_sha256=pair[0][1] if pair else None,
                                  response_match=(pair[0][1] == resp.get("response_payload_sha256")) if pair and resp
                                  else None, commitment=sorted(r["commitments"])[0] if r["commitments"] else None)
            self.dirty.add(s.id)

    # -------------------------------------------------------------- state
    def hashes(self, sid):
        req, resp = self._decoded_request(sid), self._decoded_response(sid)
        x = self._x(sid)
        sl, bd = x.get("server_log") or {}, x.get("board") or {}
        cap = dict(request=req.get("request_sha256") if req else None,
                   response=resp.get("response_payload_sha256") if resp else None)
        log = dict(request=sl.get("request_sha256"), response=sl.get("response_payload_sha256"), match=sl.get("match"))
        board = dict(request=bd.get("request_sha256"), response=bd.get("response_payload_sha256"),
                     found=bd.get("found"))

        def same(a, b):
            if a is None or b is None:
                return None
            return a == b
        return dict(capture=cap, server_log=log, board=board,
                    capture_server=dict(request=same(cap["request"], log["request"]),
                                        response=same(cap["response"], log["response"])),
                    capture_board=dict(request=same(cap["request"], board["request"]),
                                       response=same(cap["response"], board["response"])))

    def session_public(self, s):
        d = s.to_dict()
        x = self._x(s.id)
        d.update(decode=x["decode"], server_log=x["server_log"], energy=x["energy"], board_log=x["board"],
                 connection_stop=x["connection_stop"], hashes=self.hashes(s.id))
        return d

    def probe_view(self, samples):
        """Probe rows [host_s, P_mW, E_J] for the page. While a live run is not sealed: the last PROBE_KEEP_S seconds
        (the page draws the last 120 s). In a replay and after the seal: the span of the sessions, from the first SYN
        minus PROBE_VIEW_MARGIN_S to the last close plus PROBE_VIEW_MARGIN_S (every sample when there is no session),
        so that the energy chart follows the recorded session times; thinned to at most PROBE_VIEW_MAX rows."""
        if not samples:
            return []
        if self.mode == "live" and self.seal.get("state") != "sealed":
            cut = samples[-1][0] - PROBE_KEEP_S
            rows = [r for r in samples if r[0] >= cut]
        else:
            spans = [(s.t_syn or s.t_first, s.end_time() if s.state != "open" else samples[-1][0])
                     for s in self.tracker.sessions]
            spans = [(a, b) for a, b in spans if a is not None and b is not None]
            if spans:
                lo = min(a for a, _b in spans) - PROBE_VIEW_MARGIN_S
                hi = max(b for _a, b in spans) + PROBE_VIEW_MARGIN_S
                rows = [r for r in samples if lo <= r[0] <= hi]
            else:
                rows = samples
        step = max(1, -(-len(rows) // PROBE_VIEW_MAX))
        return [[round(r[0], 3), r[4], r[5]] for r in rows[::step]]

    def is_rehearsal(self):
        """True for a PC rehearsal or a PC test, whose times are PC values and never KV260 figures: the run folder
        holds the summary of the sim rehearsal (demo/sim), or the board address is a loopback address (the sim
        container and the mock run the board side on this PC)."""
        if os.path.isfile(os.path.join(self.run_dir, "rehearsal_summary.txt")):
            return True
        ips = [self.cfg.get("board_ip")] + [s.board_ip for s in self.tracker.sessions]
        return any(ip and str(ip).startswith("127.") for ip in ips)

    def state(self):
        with self.lock:
            samples = list(self.samples)
            return finite(dict(
                v=1, seq=self.bus.last(), mode=self.mode, now=time.time(), rehearsal=self.is_rehearsal(),
                # the port that the sessions and the decode use: in a replay the one from server.log (or --port),
                # which may differ from config.port
                port=self.port,
                run=dict(dir=self.run_dir, name=os.path.basename(self.run_dir)),
                config={k: self.cfg.get(k) for k in ("iface", "board_ip", "port", "server", "image", "container",
                                                     "publish", "capture_filter", "probe_port", "keylog", "snaplen",
                                                     "decode_timeout_s")},
                status=self.status, sessions=[self.session_public(s) for s in self.tracker.sessions],
                server_log=list(self.server_lines)[-200:], unmatched_responses=self.unmatched[-20:],
                packets=list(self.packets)[-120:],
                probe=dict(samples=self.probe_view(samples), idle_mw=self.status["probe"].get("idle_mw"),
                           rate_hz=self.status["probe"].get("rate_hz")),
                model=self.model, gate_titles=efhe_verify.TITLES, status_text=STATUS_ZH,
                seal=self.seal, verify=self.verify, board=self.board))

    def publish(self, force_status=False):
        """Coalesced updates: changed sessions, status blocks and probe batches."""
        now = time.time()
        with self.lock:
            dirty = [s for s in self.tracker.sessions if s.id in self.dirty]
            self.dirty.clear()
            sessions = [self.session_public(s) for s in dirty]
            status = None
            if self.status_dirty and (force_status or now - self.last_status_emit >= 0.5):
                status = {k: dict(self.status[k]) for k in self.status_dirty}
                self.status_dirty.clear()
                self.last_status_emit = now
            batch = None
            if self.pending_samples and now - self.last_probe_emit >= 0.5:
                batch = dict(samples=self.pending_samples, idle_mw=self.status["probe"].get("idle_mw"),
                             rate_hz=self.status["probe"].get("rate_hz"))
                self.pending_samples = []
                self.last_probe_emit = now
        for d in sessions:
            self.bus.emit("session", d)
        if status:
            self.bus.emit("status", status)
        if batch:
            self.bus.emit("probe", batch)

    # -------------------------------------------------------------- live supervision
    def tick(self):
        now = time.time()
        with self.lock:
            self.tracker.tick(now)
            st = self.status["server"]
            if st["state"] in ("ready", "serving", "idle"):
                st["state"] = "serving" if self.tracker.open_sessions() else "idle"
        self.rematch()
        if now - self.last_stats_read >= 1.0:
            self.last_stats_read = now
            self._read_stats()
            with self.lock:
                if self.samples:
                    recent = [self.samples[i] for i in range(max(0, len(self.samples) - 200), len(self.samples))]
                    self.status["probe"]["rate_hz"] = round(probe_reader.rate_hz(recent, now), 1)
                    self.status_dirty.add("probe")
            self.compute_energy()
        if now - self.last_proc_check >= 5.0:
            self.last_proc_check = now
            self._check_procs()
        if self.seal["state"] == "sealed" and now - self.last_board_check >= 2.0:
            self.last_board_check = now
            self.check_board()
        self.publish()

    def _read_stats(self):
        if "dumpcap" not in self.procs:
            return
        try:
            with open(self.stats_path, "rb") as f:
                text = f.read().decode("utf-8", "replace")
        except OSError:
            return
        c = dumpcap_counts(text)
        self.set_status("capture", packets=c["received"], dropped=c["dropped"])

    def _check_procs(self):
        p = self.procs.get("dumpcap")
        if p is not None and not p.alive() and self.status["capture"]["state"] == "capturing":
            self.set_status("capture", state="failed", message="dumpcap 已意外結束（代碼 %s）" % p.p.returncode)
            self.notice("error", "dumpcap 已意外結束；本次擷取可能不完整。")
        p = self.procs.get("tshark")
        if p is not None and not p.alive() and self.status["feed"]["state"] == "running":
            self.set_status("feed", state="failed", message="tshark 已意外結束（代碼 %s）" % p.p.returncode)
        p = self.procs.get("mock")
        if p is not None and not p.alive() and self.status["server"]["state"] not in ("stopped", "exited"):
            self.set_status("server", state="exited", message="模擬伺服器已結束（代碼 %s）" % p.p.returncode)
        if self.cfg["server"] == "docker" and self.status["server"]["state"] in ("ready", "serving", "idle"):
            rc, out, _err = run_quiet([self.cfg["docker"], "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}",
                                       self.cfg["container"]], timeout=10)
            if rc == 0 and out.split()[:1] != ["running"]:
                self.set_status("server", state="exited", message="容器狀態：%s" % out.strip())

    def ticker_loop(self):
        while not self.stopping.wait(0.1):
            try:
                self.tick()
            except Exception as e:                       # the screen must keep running
                say("EVIDENCE_CONSOLE_TICK_ERROR %s: %s" % (type(e).__name__, e))

    def tail_loop(self):
        pos, buf = 0, b""
        while not self.stopping.is_set():
            try:
                size = os.path.getsize(self.server_log_path)
            except OSError:
                self.stopping.wait(0.2)
                continue
            if size < pos:
                pos, buf = 0, b""
            if size > pos:
                try:
                    with open(self.server_log_path, "rb") as f:
                        f.seek(pos)
                        data = f.read(size - pos)
                except OSError:
                    data = b""
                pos += len(data)
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").rstrip("\r")
                    if text.strip():
                        self.server_line(text)
            self.stopping.wait(0.2)

    def feed_loop(self, proc):
        for raw in proc.p.stdout:
            if self.stopping.is_set():
                break
            pkt = live_feed.parse_line(raw.decode("utf-8", "replace"))
            if pkt is not None:
                self.feed_packet(pkt)
        if self.status["feed"]["state"] == "running" and not self.stopping.is_set():
            self.set_status("feed", state="ended")

    def feed_stderr_loop(self, proc, started):
        for raw in proc.p.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if "Capturing on" in line:
                started.set()
            else:
                self.set_status("feed", message=line[:200])

    def start_thread(self, target, name, *args):
        t = threading.Thread(target=target, name=name, args=args, daemon=True)
        t.start()
        self.threads.append(t)
        return t

    # -------------------------------------------------------------- live start
    def start_capture(self):
        dumpcap = efhe_verify.find_tool("dumpcap", self.cfg.get("dumpcap"))
        if not dumpcap:
            raise Refused("TOOL_MISSING", tool="dumpcap")
        args = [dumpcap, "-i", self.cfg["iface"]]
        if self.cfg.get("capture_filter"):
            args += ["-f", self.cfg["capture_filter"]]
        args += ["-s", str(self.cfg["snaplen"] or 0), "-B", str(self.cfg["buffer_mib"]), "-w", self.capture_path]
        self._stats_file = open(self.stats_path, "wb")
        self.procs["dumpcap"] = Proc("dumpcap", args, stderr=self._stats_file)
        t_end = time.time() + 15.0
        while time.time() < t_end:
            time.sleep(0.1)
            with open(self.stats_path, "rb") as f:
                text = f.read().decode("utf-8", "replace")
            if "Capturing on" in text:
                self.set_status("capture", state="capturing", started=time.time())
                say("EVIDENCE_CAPTURE_STARTED iface=%s filter=%s snaplen=%d file=%s" % (
                    self.cfg["iface"], json.dumps(self.cfg.get("capture_filter") or ""), self.cfg["snaplen"] or 0,
                    self.capture_path))
                return
            if not self.procs["dumpcap"].alive():
                break
        self.procs["dumpcap"].interrupt(5)
        self._stats_file.close()
        with open(self.stats_path, "rb") as f:
            why = f.read().decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"]
        del self.procs["dumpcap"]
        raise Refused("CAPTURE_START", why=why[0][:200])

    def write_pins(self, pki_dir):
        files = pki_files(pki_dir)
        server_pin, client_pin = certificate_pin(files["server_pem"]), read_pin(files["client_pin"])
        for name, pin in (("server.pin", server_pin), ("client.pin", client_pin)):
            with open(os.path.join(self.run_dir, name), "w", encoding="ascii", newline="\n") as f:
                f.write(pin + "\n")
        say("EVIDENCE_PINS_WRITTEN server_pin=%s client_pin=%s" % (server_pin, client_pin))
        return files

    def start_server(self, files):
        kind = self.cfg["server"]
        if kind == "none":
            self.set_status("server", state="external", message="伺服器由操作人員自行啟動。")
            say("EVIDENCE_SERVER_EXTERNAL 本主控台不啟動伺服器；伺服器須以本證據資料夾作為 /evidence。")
            return
        self.set_status("server", state="starting")
        if kind == "docker":
            self._start_docker(files)
        else:
            self._start_mock()
        need = ["CPP_E2E_SERVER_READY"] + (["WARNING_DEMO_TLS_KEYLOG_ENABLED"] if self.cfg["keylog"] else [])
        t_end = time.time() + 30.0
        seen = set()
        while time.time() < t_end:
            time.sleep(0.1)
            try:
                with open(self.server_log_path, "rb") as f:
                    text = f.read().decode("utf-8", "replace")
            except OSError:
                text = ""
            for line in text.split("\n")[:-1]:              # complete lines only
                e = parse_server_line(line.rstrip("\r"))
                if e["kind"] == "keylog_warning" and "WARNING_DEMO_TLS_KEYLOG_ENABLED" not in seen:
                    seen.add("WARNING_DEMO_TLS_KEYLOG_ENABLED")
                    say(e["text"])
                elif e["kind"] == "ready" and "CPP_E2E_SERVER_READY" not in seen:
                    seen.add("CPP_E2E_SERVER_READY")
                    say("CPP_E2E_SERVER_READY port=%s source=server.log" % e["data"].get("port"))
            if all(n in seen for n in need):
                return
            why = self._server_gone()
            if why:
                raise Refused("SERVER_REFUSED", why=why)
        raise Refused("SERVER_NOT_READY", seconds=30, missing="、".join(n for n in need if n not in seen))

    def _server_gone(self):
        if self.cfg["server"] == "docker":
            rc, out, _e = run_quiet([self.cfg["docker"], "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}",
                                     self.cfg["container"]], timeout=10)
            if rc == 0 and out.split()[:1] in (["exited"], ["dead"]):
                _rc, logs, errs = run_quiet([self.cfg["docker"], "logs", self.cfg["container"]], timeout=20)
                lines = [ln for ln in (logs + "\n" + errs).splitlines() if ln.strip()]
                refusal = [ln for ln in lines if "EDGEFHE_CLOUD_SERVER_REFUSED" in ln or ln.startswith("STOP_")]
                return (refusal or lines[-1:] or ["容器已結束：%s" % out.strip()])[-1][:400]
            return None
        p = self.procs.get("mock")
        if p is not None and not p.alive():
            return "模擬伺服器已結束（代碼 %s）" % p.p.returncode
        return None

    def _start_docker(self, files):
        docker = self.cfg["docker"]
        rc, out, err = run_quiet([docker, "version", "--format", "{{.Server.Version}}"], timeout=30)
        if rc != 0:
            raise Refused("DOCKER_UNAVAILABLE", why=(err or out).strip().splitlines()[-1:][0][:200] if (err or out)
                          .strip() else "rc=%d" % rc)
        rc, _out, _err = run_quiet([docker, "image", "inspect", "--format", "{{.Id}}", self.cfg["image"]])
        if rc != 0:
            raise Refused("IMAGE_MISSING", image=self.cfg["image"])
        name = self.cfg["container"]
        rc, out, _err = run_quiet([docker, "ps", "-a", "--format", "{{.Names}}\t{{.State}}"])
        for line in out.splitlines():
            parts = line.split("\t")
            if parts[0] == name:
                if len(parts) > 1 and parts[1] in ("running", "restarting", "paused"):
                    raise Refused("CONTAINER_RUNNING", name=name)
                run_quiet([docker, "rm", name])
                say("EVIDENCE_OLD_CONTAINER_REMOVED name=%s state=%s" % (name, parts[1] if len(parts) > 1 else "?"))
        args = [docker, "run", "-d", "--init", "--name", name, "-p", self.cfg["publish"],
                "-v", "%s:/pki:ro" % docker_path(files["cloud"]), "-v", "%s:/evidence" % docker_path(self.run_dir),
                self.cfg["image"], "sh", "/opt/efhe/run_server.sh"] + (["--keylog"] if self.cfg["keylog"] else [])
        self.procs["docker"] = None                      # from here on, stop_server() stops and removes `name`
        rc, out, err = run_quiet(args, timeout=60)
        if rc != 0:
            raise Refused("SERVER_START", why=(err or out).strip()[-300:])
        self.container_id = out.strip()
        say("EVIDENCE_SERVER_STARTED kind=docker container=%s image=%s publish=%s keylog=%s" % (
            name, self.cfg["image"], self.cfg["publish"], "on" if self.cfg["keylog"] else "off"))

    def _start_mock(self):
        if not self.cfg["keylog"]:
            raise Refused("CONFIG_VALUE", key="keylog", why="模擬伺服器一律寫出金鑰紀錄。")
        self._mock_err = open(os.path.join(self.tmp, "mock_server_stderr.txt"), "wb")
        args = [sys.executable, "-B", MOCK_PY, "server", "--pki", os.path.abspath(self.cfg["pki_dir"]),
                "--port", str(self.port), "--keylog", self.keylog_path, "--log", self.server_log_path,
                "--connections", str(self.cfg["mock_connections"]), "--host", "127.0.0.1"]
        self.procs["mock"] = Proc("mock", args, stderr=self._mock_err)
        say("EVIDENCE_SERVER_STARTED kind=mock port=%d（測試用的 Python 模擬伺服器，不是展示用的雲端伺服器）" % self.port)

    def start_feed(self):
        if not self.tshark_path:
            raise Refused("TOOL_MISSING", tool="tshark")
        cfgdir = os.path.join(self.tmp, "tshark_profile")
        os.makedirs(cfgdir, exist_ok=True)
        env = dict(os.environ, WIRESHARK_CONFIG_DIR=cfgdir, LC_ALL="C")
        args = live_feed.tshark_live_args(self.tshark_path, self.cfg["iface"], self.cfg.get("capture_filter"),
                                          self.port)
        proc = Proc("tshark", args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.procs["tshark"] = proc
        started = threading.Event()
        self.start_thread(self.feed_stderr_loop, "efhe-feed-err", proc, started)
        if not started.wait(15.0):
            why = self.status["feed"].get("message") or ("exit %s" % proc.p.poll())
            proc.interrupt(5)
            del self.procs["tshark"]
            raise Refused("FEED_START", why=why)
        self.set_status("feed", state="running")
        self.start_thread(self.feed_loop, "efhe-feed", proc)
        say("EVIDENCE_LIVE_FEED_STARTED tshark=%s keys=none" % self.tshark_path)

    def start_probe(self):
        port = self.cfg.get("probe_port")
        if not port:
            say("EVIDENCE_PROBE_OFF 設定檔未指定能耗探針的序列埠。")
            return
        self.probe = probe_reader.ProbeReader(port, self.probe_path, self.on_probe_sample, self.on_probe_status,
                                              baud=self.cfg["probe_baud"])
        self.probe.start()
        say("EVIDENCE_PROBE_STARTED port=%s file=%s（DTR、RTS 於開啟前設為 False；本主控台不傳送任何指令）" % (
            port, self.probe_path))

    # -------------------------------------------------------------- live stop
    def stop_feed(self):
        p = self.procs.pop("tshark", None)
        if p is not None:
            p.interrupt(10)
            self.set_status("feed", state="stopped")

    def stop_capture(self):
        p = self.procs.get("dumpcap")
        if p is None:
            return None
        time.sleep(1.0)                                  # let dumpcap write the last packets
        rc = p.interrupt(20)
        try:
            self._stats_file.close()
        except OSError:
            pass
        self.procs.pop("dumpcap", None)
        try:
            with open(self.stats_path, "rb") as f:
                text = f.read().decode("utf-8", "replace")
            with open(self.stats_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text.replace("\r\n", "\n"))
        except OSError:
            text = ""
        c = dumpcap_counts(text)
        self.set_status("capture", state="stopped", packets=c["received"], dropped=c["dropped"], stopped=time.time(),
                        message="" if c["final"] else "dumpcap 未寫出結束統計（Packets received/dropped）")
        say("EVIDENCE_CAPTURE_STOPPED rc=%s packets=%s dropped=%s statistics=%s" % (
            rc, c["received"], c["dropped"], "final" if c["final"] else "MISSING"))
        return c

    def stop_server(self):
        kind = self.cfg["server"]
        if kind == "docker" and "docker" in self.procs:
            rc, out, err = run_quiet([self.cfg["docker"], "stop", "-t", "10", self.cfg["container"]], timeout=60)
            run_quiet([self.cfg["docker"], "rm", self.cfg["container"]], timeout=60)
            self.procs.pop("docker", None)
            say("EVIDENCE_SERVER_STOPPED kind=docker container=%s rc=%d" % (self.cfg["container"], rc))
        elif kind == "mock" and "mock" in self.procs:
            p = self.procs.pop("mock")
            p.terminate(10)
            try:
                self._mock_err.close()
            except OSError:
                pass
            say("EVIDENCE_SERVER_STOPPED kind=mock")
        else:
            return
        t_end = time.time() + 1.5                         # the tail thread reads the launcher's last line
        while time.time() < t_end and self.status["server"]["state"] != "exited":
            time.sleep(0.1)
        self.set_status("server", state="stopped")

    def stop_probe(self):
        p = getattr(self, "probe", None)
        if p is not None:
            p.stop()
            self.probe = None

    def shutdown(self, reason="exit"):
        """Stop everything this console started (live mode). Never touches the board."""
        if getattr(self, "_shut", False):
            return
        self._shut = True
        if self.mode == "live":
            self.stop_feed()
            self.stop_capture()
            self.stop_server()
            self.stop_probe()
        self.stopping.set()
        self.bus.close()
        if self.httpd is not None:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()
        if self._tshark is not None:
            self._tshark.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -------------------------------------------------------------- seal and verify
    def _run_seal(self, verify):
        args = [sys.executable, "-B", SEAL_PY, "--run", self.run_dir, "--port", str(self.port)]
        if verify:
            args.append("--verify")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        try:
            r = subprocess.run(args, capture_output=True, env=env, timeout=900, stdin=subprocess.DEVNULL,
                               creationflags=NEW_GROUP)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 999, ["%s: %s" % (type(e).__name__, e)]
        lines = [ln for ln in r.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
        lines += [ln for ln in r.stderr.decode("utf-8", "replace").splitlines() if ln.strip()][-5:]
        return r.returncode, lines

    def request_seal(self):
        with self.lock:
            if self.mode != "live":
                return 409, dict(code="REPLAY", message="重播模式不能封存；請以 efhe_seal.py 處理既有的證據資料夾。")
            if self.seal["state"] in ("running", "sealed"):
                return 409, dict(code="SEAL_STATE", message="本次擷取已封存或正在封存。")
            open_s = [s.id for s in self.tracker.open_sessions()]
            if open_s:
                return 409, dict(code="SESSION_OPEN", message="連線 %s 仍在進行中；請待連線結束後再封存。" % "、".join(open_s))
            self.seal.update(state="running", message="正在停止擷取並封存。", lines=[])
        self.bus.emit("seal", dict(self.seal))
        self.start_thread(self._seal_job, "efhe-seal")
        return 202, dict(state="running")

    def _seal_job(self):
        try:
            self.stop_feed()
            c = self.stop_capture()
            self.stop_server()
            self.stop_probe()
            self.compute_energy(force=True)
            self.publish(force_status=True)
            rc, lines = self._run_seal(False)
            sha = None
            for ln in lines:
                if ln.startswith("EFHE_CAPTURE_SEALED"):
                    m = re.search(r"sha256=([0-9a-f]{64})", ln)
                    sha = m.group(1) if m else None
                if ln.startswith("EFHE_"):
                    say(ln)
            with self.lock:
                self.seal.update(state="sealed" if rc == 0 and sha else "failed", rc=rc, lines=lines[-20:],
                                 sha256=sha, capture=c, finished=time.time(),
                                 message="" if rc == 0 else "efhe_seal.py 結束代碼 %s" % rc)
                redo = []
                if self.seal["state"] == "sealed" and self.cfg.get("keylog", True):
                    for s in self.tracker.sessions:     # '封存後解碼': the sealed capture is complete now
                        x = self._x(s.id)
                        if x["decode"].get("state") == "deferred":
                            x["decode"] = dict(state="queued", attempts=0, after_seal=True)
                            self.dirty.add(s.id)
                            redo.append(s.id)
            for sid in redo:
                self.decode_q.put((sid, True))
        except Exception as e:
            with self.lock:
                self.seal.update(state="failed", message="%s: %s" % (type(e).__name__, e))
        self.bus.emit("seal", dict(self.seal))
        self.publish(force_status=True)

    def request_verify(self):
        with self.lock:
            if self.mode != "live":
                return 409, dict(code="REPLAY", message="重播模式不執行驗證；請以 efhe_seal.py --verify 處理證據資料夾。")
            if self.seal["state"] != "sealed":
                return 409, dict(code="NOT_SEALED", message="請先封存本次擷取，再執行驗證。")
            if self.verify["state"] == "running":
                return 409, dict(code="VERIFY_RUNNING", message="驗證正在執行。")
            self.verify.update(state="running", message="正在驗證（efhe_seal.py --verify）。", lines=[])
        self.bus.emit("verify", dict(self.verify))
        self.start_thread(self._verify_job, "efhe-verify")
        return 202, dict(state="running")

    def _verify_job(self):
        try:
            self.check_board()
            rc, lines = self._run_seal(True)
            for ln in lines:
                if ln.startswith("EFHE_"):
                    say(ln)
            self.load_report()
            with self.lock:
                self.verify.update(state="done" if self.verify.get("verdict") else "failed", rc=rc,
                                   lines=lines[-30:], finished=time.time())
        except Exception as e:
            with self.lock:
                self.verify.update(state="failed", message="%s: %s" % (type(e).__name__, e))
        self.bus.emit("verify", dict(self.verify))

    def load_report(self):
        path = os.path.join(self.run_dir, "verify_report.json")
        if not os.path.isfile(path):
            return False
        try:
            with open(path, encoding="utf-8") as f:
                rep = json.load(f)
        except (OSError, ValueError):
            return False
        gates = {g: dict(status=d.get("status"), title=d.get("title")) for g, d in (rep.get("gates") or {}).items()}
        with self.lock:
            self.verify.update(verdict=rep.get("verdict"), evidence_pass=rep.get("evidence_pass"), gates=gates,
                               summary=rep.get("summary"), negative_control=(rep.get("negative_control") or {})
                               .get("detected"), report_html=os.path.isfile(os.path.join(self.run_dir, "report.html")))
        return True

    def load_seal_files(self):
        """Replay: the seal record, the verifier report and the board public log, when present."""
        rec = os.path.join(self.run_dir, "capture.sha256")
        if os.path.isfile(rec):
            with open(rec, encoding="ascii", errors="replace") as f:
                tok = (f.read().split() or [""])[0].lower()
            self.seal.update(state="sealed" if efhe_verify.is_hex64(tok) else "unreadable",
                             sha256=tok if efhe_verify.is_hex64(tok) else None)
        if self.load_report():
            self.verify["state"] = "done"
        self.check_board()


# ------------------------------------------------------------------ decode summary
def _check(gate):
    return (gate.get("checks") or [{}])[0]


def summarize_decode(r):
    """The parts of a quick_session result that the screen shows."""
    conn = r.get("connection") or {}
    gates = r.get("gates") or {}
    frames = []
    for fr in conn.get("frames") or []:
        frames.append({k: fr.get(k) for k in ("kind", "sequence", "features", "rows", "bytes", "model", "model_id",
                                              "key_id", "generation", "request_sha256", "response_payload_sha256",
                                              "t_first", "t_last")})
    out = dict(ok=bool(r.get("ok")), complete=bool(r.get("complete")), reason=r.get("reason"),
               classification=conn.get("classification"), client=conn.get("client"), server=conn.get("server"),
               tls=conn.get("tls"), frames=frames, tshark=r.get("tshark"), gates={})
    req = next((f for f in frames if f["kind"] == "request"), None)
    resp = next((f for f in frames if f["kind"] == "response"), None)
    out["request_sha256"] = req.get("request_sha256") if req else None
    out["response_payload_sha256"] = resp.get("response_payload_sha256") if resp else None
    for name, g in gates.items():
        c = _check(g)
        facts = dict(problems=[str(p) for p in (c.get("problems") or [])][:6],
                     incomplete=[str(p) for p in (c.get("incomplete") or [])][:4])
        if name == "G2":
            facts.update({k: c.get(k) for k in ("suite", "version", "sni", "tickets", "server_leaf_sha256",
                                                 "client_leaf_sha256", "native_crosscheck")})
        elif name == "G3":
            facts.update({k: c.get(k) for k in ("classification", "app_bytes_up", "app_bytes_down", "accounted_bytes",
                                                 "total_bytes")})
        elif name == "G4":
            rows = c.get("requests") or [{}]
            facts.update({k: rows[0].get(k) for k in ("request_id_ok", "request_hash_ok", "session_binding_ok",
                                                      "response_binding_ok", "sequence_ok")})
        elif name == "G5":
            pairs = c.get("pairs") or [{}]
            facts.update(bit_exact=pairs[0].get("bit_exact"), words=pairs[0].get("words"),
                         mismatched_words=pairs[0].get("mismatched_words"))
        elif name == "G6":
            rows = c.get("requests") or [{}]
            row = rows[0] if rows else {}
            facts.update(limbs=row.get("limbs"), uniform_limbs=row.get("uniform_limbs"),
                         noncanonical_words=row.get("noncanonical_words"), reasons=row.get("reasons"),
                         histogram=row.get("histogram"), worst_chi2_p=(row.get("worst_limb") or {}).get("chi2_p"),
                         others={x.get("name"): x.get("status") for x in (g.get("checks") or [])[1:]})
        elif name == "G8":
            facts.update(client_close_notify=c.get("client_close_notify"),
                         app_chunks_after_response=c.get("app_chunks_after_response"))
        out["gates"][name] = dict(status=g.get("status"), title=g.get("title"), facts=facts,
                                  checked=bool(g.get("checks")))
    return finite(out)


# ------------------------------------------------------------------ HTTP
def _content_length(headers):
    lengths = headers.get_all("Content-Length") or []
    if headers.get_all("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].strip().isdigit():
        return None
    return int(lengths[0].strip())


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, address, console):
        self.console = console
        self.streams = threading.BoundedSemaphore(SSE_STREAMS)
        super().__init__(address, _Handler)
        port = self.server_address[1]
        self.hosts = frozenset(("127.0.0.1:%d" % port, "localhost:%d" % port))
        self.origins = frozenset(("http://127.0.0.1:%d" % port, "http://localhost:%d" % port))

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)            # no reverse DNS lookup
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        pass


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "EdgeFHE-Console"
    protocol_version = "HTTP/1.0"
    timeout = 30

    def version_string(self):
        return self.server_version

    def log_message(self, format, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", csp=CSP):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", csp)
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except OSError:
                pass

    def _json(self, code, obj):
        self._send(code, dumps(obj))

    def _screen(self):
        if not self.client_address[0].startswith("127."):
            return "本主控台只接受來自本機迴路介面的連線。"
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0].strip().lower() not in self.server.hosts:
            return "本主控台只接受 Host 標頭為 127.0.0.1:%d 或 localhost:%d 的請求。" % (
                self.server.server_port, self.server.server_port)
        origins = self.headers.get_all("Origin") or []
        if len(origins) > 1 or (origins and origins[0].strip().lower() not in self.server.origins):
            return "本主控台只接受來自本畫面本身的請求。"
        return None

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def _drain(self):
        """Read the request body (at most MAX_DRAIN bytes) before any answer: a connection closed with unread data is
        reset (RST) on Windows, and the page would then see no answer at all. (body, length); body None when the
        length is missing, malformed or above MAX_DRAIN (the connection is then closed after the answer)."""
        n = _content_length(self.headers)
        if n is None or n > MAX_DRAIN:
            self.close_connection = True
            return None, n
        return (self.rfile.read(n) if n else b""), n

    def _handle(self, method):
        body, n = self._drain() if method == "POST" else (b"", 0)
        why = self._screen()
        if why:
            return self._json(403, dict(code="FORBIDDEN", message=why))
        path, _, query = self.path.partition("?")
        if not path.startswith("/") or path.startswith("//"):
            return self._json(404, dict(code="NOT_FOUND"))
        console = self.server.console
        try:
            if method in ("GET", "HEAD"):
                if path in ("/", "/index.html"):
                    return self._static("index.html")
                if path.startswith("/web/") or path in ("/console.js", "/console.css"):
                    return self._static(path.rsplit("/", 1)[-1])
                if path == API + "/state":
                    return self._json(200, console.state())
                if path == API + "/events" and method == "GET":
                    return self._events(query)
                if path == "/evidence/report.html":
                    return self._report()
                return self._json(404, dict(code="NOT_FOUND", message="本主控台找不到此路徑。"))
            if path not in (API + "/seal", API + "/verify"):
                return self._json(404 if path.startswith(API) else 405, dict(code="NOT_FOUND"))
            if (self.headers.get(CONSOLE_HEADER) or "").strip() != "1":
                return self._json(403, dict(code="CONSOLE_HEADER", message="操作請求必須帶有 %s: 1 標頭。" %
                                            CONSOLE_HEADER))
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._json(403, dict(code="CONTENT_TYPE", message="操作請求必須使用 application/json。"))
            if body is None or n > MAX_BODY:
                return self._json(403, dict(code="BODY", message="操作請求必須標明不超過 %d 位元組的內容長度。" % MAX_BODY))
            try:
                obj = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, ValueError):
                obj = None
            if not isinstance(obj, dict):
                return self._json(400, dict(code="BODY_JSON", message="請求內容必須是 JSON 物件。"))
            code, res = console.request_seal() if path.endswith("/seal") else console.request_verify()
            return self._json(code, res)
        except Exception as e:                           # never leak a traceback to the page
            return self._json(500, dict(code="INTERNAL", message="%s" % type(e).__name__))

    def _static(self, name):
        ctype = STATIC.get(name)
        if ctype is None:
            return self._json(404, dict(code="NOT_FOUND"))
        try:
            with open(os.path.join(WEB, name), "rb") as f:
                data = f.read()
        except OSError:
            return self._json(404, dict(code="NOT_FOUND"))
        self._send(200, data, ctype)

    def _report(self):
        path = os.path.join(self.server.console.run_dir, "report.html")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self._json(404, dict(code="NO_REPORT", message="本次執行尚未產生 report.html。"))
        self._send(200, data, "text/html; charset=utf-8", csp=REPORT_CSP)

    def _events(self, query):
        """SSE from the larger of ?since= and Last-Event-ID (a reconnect repeats the original URL)."""
        try:
            params = urllib.parse.parse_qs(query, max_num_fields=8)
            points = [int(v) for v in params.get("since", [])[:1] + [self.headers.get("Last-Event-ID") or "0"]
                      if str(v).strip()]
        except ValueError:
            return self._json(400, dict(code="QUERY", message="since 與 Last-Event-ID 必須是非負整數。"))
        since = max(points) if points else 0
        if not self.server.streams.acquire(blocking=False):
            return self._json(503, dict(code="STREAMS", message="同時開啟的事件串流已達上限。"))
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in SECURITY_HEADERS:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            bus = self.server.console.bus
            seq = max(since, 0)
            while not bus.closed:
                events, gap = bus.after(seq, HEARTBEAT_S)
                if gap:
                    self.wfile.write(("data: %s\n\n" % dumps(dict(v=1, seq=seq, type="resync", data={})))
                                     .encode("utf-8"))
                if events:
                    chunk = "".join("id: %d\ndata: %s\n\n" % (s, text) for s, text in events)
                    self.wfile.write(chunk.encode("utf-8"))
                    seq = events[-1][0]
                else:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.server.streams.release()


def make_server(console, listen_port):
    try:
        httpd = _Server(("127.0.0.1", listen_port), console)
    except OSError as e:
        raise Refused("LISTEN_BIND", address="127.0.0.1:%d" % listen_port, why=str(e))
    console.httpd = httpd
    return httpd


# ------------------------------------------------------------------ replay
def detect_port(run_dir, default=18443):
    path = os.path.join(run_dir, "server.log")
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                e = parse_server_line(line.rstrip("\r\n"))
                if e["kind"] == "ready" and isinstance(e["data"].get("port"), int):
                    return e["data"]["port"]
    return default


def build_replay(run_dir, cfg=None, port=None, probe_csv=None, probe_shift=0.0, decode=True):
    """A Console in replay mode with the whole run loaded (no process is started)."""
    cfg = cfg or validate_config(dict(DEFAULTS), live=False)
    run_dir = os.path.abspath(run_dir)
    port = port or detect_port(run_dir, cfg.get("port") or 18443)
    console = Console(cfg, "replay", run_dir, port)
    if not os.path.isfile(console.capture_path):
        raise Refused("REPLAY_CAPTURE", path=run_dir)
    if not console.tshark_path:
        raise Refused("TOOL_MISSING", tool="tshark")
    cfgdir = os.path.join(console.tmp, "tshark_profile")
    os.makedirs(cfgdir, exist_ok=True)
    env = dict(os.environ, WIRESHARK_CONFIG_DIR=cfgdir, LC_ALL="C")
    r = subprocess.run(live_feed.tshark_file_args(console.tshark_path, console.capture_path, port), env=env,
                       capture_output=True, stdin=subprocess.DEVNULL)
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        pkt = live_feed.parse_line(line)
        if pkt is not None:
            console.feed_packet(pkt)
    with console.lock:
        console.tracker.finish()
    console.set_status("capture", state="file", packets=console.tracker.packets)
    console.set_status("feed", state="file")
    if os.path.isfile(console.server_log_path):
        with open(console.server_log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    console.server_line(line.rstrip("\r\n"))
        console.set_status("server", state="file")
    probe = probe_csv or (console.probe_path if os.path.isfile(console.probe_path) else None)
    if probe:
        for row in probe_reader.load_csv(probe, probe_shift):
            console.samples.append(row)
        console.set_status("probe", state="file", port=os.path.basename(probe), samples=len(console.samples),
                           shift_s=probe_shift)
    if decode and os.path.isfile(console.keylog_path):
        for s in list(console.tracker.sessions):
            if not s.records:
                with console.lock:
                    console._x(s.id)["decode"] = dict(state="notls", attempts=0)
                continue
            t0 = time.time()
            res, recs, err = console.decode_once(s.id, copy=False)
            if res is not None and res.get("complete"):
                console.apply_decode(s.id, res, recs, 1, t0)
            else:
                with console.lock:
                    console._x(s.id)["decode"] = dict(state="deferred", attempts=1,
                                                      reason=err or (res or {}).get("reason"))
    elif not os.path.isfile(console.keylog_path):
        with console.lock:
            for s in console.tracker.sessions:
                console._x(s.id)["decode"] = dict(state="nokeys", attempts=0)
    console.rematch()
    console.compute_energy(force=True)
    console.load_seal_files()
    with console.lock:
        console.dirty.clear()
    return console


# ------------------------------------------------------------------ main
def resolve_run_dir(cfg, run):
    if run:
        path = os.path.abspath(run)
    else:
        path = os.path.join(cfg["evidence_root"], datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    if inside(path, REPO):
        raise Refused("RUN_IN_REPO", path=path)
    if os.path.exists(path) and (not os.path.isdir(path) or os.listdir(path)):
        raise Refused("RUN_EXISTS", path=path)
    created = not os.path.exists(path)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise Refused("RUN_CREATE", path=path, why=str(e))
    try:
        make_private(path, "RUN_ACL")
        if os.listdir(path):                              # written between the check above and the restriction
            raise Refused("RUN_CHANGED", path=path)
    except Refused:
        if created:
            try:
                os.rmdir(path)                            # only while still empty
            except OSError:
                pass
        raise
    say("EVIDENCE_RUN_PRIVATE path=%s access=current-user,SYSTEM" % path)
    warn_wide("EVIDENCE_ROOT_ACL_WARNING", os.path.dirname(path), False,
              "本主控台已限制本次的場次資料夾；其上層資料夾仍允許其他帳號存取，因此其中既有的場次資料夾（含 TLS 金鑰紀錄與"
              "擷取檔）可能仍可被其他帳號讀取。")
    return path


def install_signals(console):
    def handler(signum, _frame):
        console.stop_requested = signum
        console.stopping_main.set()
    console.stopping_main = threading.Event()
    console.stop_requested = None
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def serve(console):
    httpd = console.httpd
    t = threading.Thread(target=httpd.serve_forever, kwargs=dict(poll_interval=0.25), name="efhe-http", daemon=True)
    t.start()
    say("EVIDENCE_CONSOLE_READY http://127.0.0.1:%d mode=%s run=%s" % (httpd.server_port, console.mode,
                                                                      console.run_dir))
    try:
        while not console.stopping_main.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass


def wait_open_sessions(console, limit=60.0):
    t_end = time.time() + limit
    shown = False
    try:
        while time.time() < t_end:
            with console.lock:
                open_s = [s.id for s in console.tracker.open_sessions()]
            if not open_s:
                return
            if not shown:
                say("EVIDENCE_CONSOLE_WAITING 連線 %s 仍在進行中；本主控台等待其結束後才停止擷取與伺服器"
                    "（最多 %d 秒；再按一次 Ctrl+C 立即停止）。" % ("、".join(open_s), limit))
                shown = True
            time.sleep(0.5)
    except KeyboardInterrupt:
        return


def private_pki(cfg):
    """Restrict the configured PKI folder (with everything below it) before the console reads the server key."""
    if cfg["server"] not in ("docker", "mock") or not cfg.get("pki_dir"):
        return
    pki = os.path.abspath(cfg["pki_dir"])
    if not os.path.isdir(pki):
        raise Refused("PKI_MISSING", path=pki)
    n = make_private(pki, "PKI_ACL")
    say("EVIDENCE_PKI_PRIVATE path=%s entries=%d access=current-user,SYSTEM" % (pki, n))


def run_live(cfg, run_arg):
    private_pki(cfg)
    run_dir = resolve_run_dir(cfg, run_arg)
    say("EVIDENCE_RUN_DIR %s" % run_dir)
    console = Console(cfg, "live", run_dir, cfg["port"])
    install_signals(console)
    started = refused = False
    try:
        httpd = make_server(console, cfg["listen_port"])
        console.start_capture()
        files = None
        if cfg["server"] in ("docker", "mock") and cfg.get("pki_dir"):
            files = console.write_pins(cfg["pki_dir"])
        console.start_server(files)
        console.start_feed()
        console.start_probe()
        console.start_thread(console.tail_loop, "efhe-tail")
        console.start_thread(console.ticker_loop, "efhe-ticker")
        console.start_thread(console.decoder_loop, "efhe-decoder")
        started = True
        serve(console)
    except Refused as e:
        say("EVIDENCE_CONSOLE_REFUSED %s %s" % (e.code, e))
        console.shutdown("refused")
        refused = True
        return 2
    except KeyboardInterrupt:
        pass
    finally:
        # dumpcap, tshark and the server run in process groups of their own and never see the operator's Ctrl+C, so
        # the console always stops what it started, also after a Ctrl+C during the start (shutdown runs only once).
        if started and console.seal["state"] not in ("sealed", "running"):
            wait_open_sessions(console)
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)       # a stop in progress is never cut short
        except (ValueError, OSError):
            pass
        console.shutdown("exit")
        if console.seal["state"] == "sealed":
            say("EVIDENCE_CONSOLE_STOPPED sealed=1 sha256=%s" % console.seal.get("sha256"))
        elif not refused and os.path.isfile(console.capture_path):
            say("EVIDENCE_CONSOLE_STOPPED sealed=0 本主控台已停止擷取與伺服器；操作人員應執行 "
                "python -B demo\\evidence\\efhe_seal.py --run %s 封存本次擷取。" % run_dir)
    return 0


def run_replay(args, cfg):
    console = build_replay(args.replay, cfg, port=args.port, probe_csv=args.probe_csv,
                           probe_shift=args.probe_shift or 0.0)
    if os.path.isfile(console.keylog_path):            # read-only: a replay never changes the evidence folder
        warn_wide("EVIDENCE_REPLAY_ACL_WARNING", console.run_dir, True,
                  "此證據資料夾含 TLS 金鑰紀錄與擷取檔，但仍允許其他帳號存取。")
    st = console.state()
    for s in st["sessions"]:
        c = s["counts"]
        dec = s["decode"] or {}
        res = dec.get("result") or {}
        say("EVIDENCE_REPLAY_SESSION id=%s board=%s signature=%s up_limbs=%d down_limbs=%d decode=%s "
            "request_sha256=%s server_log=%s energy_delta_j=%s" % (
                s["id"], s["board"], s["signature"]["classification"], c["up"]["limbs"], c["down"]["limbs"],
                dec.get("state"), res.get("request_sha256"), (s["server_log"] or {}).get("match"),
                None if not s["energy"] else s["energy"].get("delta_j")))
    if args.once:
        console.shutdown()
        return 0
    install_signals(console)
    try:
        make_server(console, console.cfg["listen_port"])
        serve(console)
    except Refused as e:
        say("EVIDENCE_CONSOLE_REFUSED %s %s" % (e.code, e))
        return 2
    finally:
        console.shutdown()
    return 0


def main(argv=None):
    efhe_verify.utf8_stdio()
    ap = argparse.ArgumentParser(description="EdgeFHE demo cloud console (PC evidence console, cloud view).")
    ap.add_argument("--config", help="console.json (see demo/console/console.example.json)")
    ap.add_argument("--run", help="new or empty evidence directory (live mode)")
    ap.add_argument("--replay", help="render an existing run directory offline")
    ap.add_argument("--listen", help="127.0.0.1:PORT (default 127.0.0.1:8765)")
    ap.add_argument("--port", type=int, help="server TCP port (replay; default from server.log, else 18443)")
    ap.add_argument("--probe-csv", help="replay: probe samples (host_s,t_ms,V,I_mA,P_mW,E_J)")
    ap.add_argument("--probe-shift", type=float, help="replay: seconds added to host_s of --probe-csv")
    ap.add_argument("--no-keylog", action="store_true", help="live: run the server without the demo key log")
    ap.add_argument("--snaplen", type=int, help="live: dumpcap snap length (0 = whole packets)")
    ap.add_argument("--once", action="store_true", help="replay: print the sessions and exit without serving")
    a = ap.parse_args(argv)
    overrides = dict(listen=a.listen, snaplen=a.snaplen, keylog=False if a.no_keylog else None)
    try:
        if a.replay:
            cfg = load_config(a.config, live=False, overrides=overrides)
            return run_replay(a, cfg)
        if not a.config:
            ap.error("live mode needs --config")
        cfg = load_config(a.config, live=True, overrides=overrides)
        return run_live(cfg, a.run)
    except Refused as e:
        say("EVIDENCE_CONSOLE_REFUSED %s %s" % (e.code, e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
