"""Event envelope and log, public record whitelist, salted form commitment, private store and disclosure writer.

Data classes (demo/edge/EVENTS.md):
  owner   plaintext answers, normalized values, logits, level: only the owner screen on the loopback interface, the
          root-only private store (tmpfs) and, on explicit request, a 0600 disclosure of a fictional form
  public  everything else: commitment, hashes, timings, counters, STOP codes; the public JSONL holds only the
          whitelisted fields of PUBLIC_FIELDS
Commitment: sha256('EDGEFHE_FORM_COMMIT_V1\\n' + salt_hex + '\\n' + canonical_json + '\\n'), a fresh 32-byte salt per
form; the disclosure reveals salt_hex and canonical_json so that anyone can open the published commitment.
Python 3.10 standard library only.
"""
import collections
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time

ENVELOPE_VERSION = 1
CLASSES = ('owner', 'public')
COMMIT_TAG = 'EDGEFHE_FORM_COMMIT_V1'
COMMIT_RULE = "sha256('EDGEFHE_FORM_COMMIT_V1\\n' + salt_hex + '\\n' + canonical_json + '\\n')"
DISCLOSURE_SCHEMA = 'edgefhe.demo.disclosure.v1'
PRIVATE_SCHEMA = 'edgefhe.demo.private-record.v1'
RUN_ID = re.compile(r'r[0-9a-f]{12}')
HEX64 = re.compile(r'[0-9a-f]{64}')

PUBLIC_FIELDS = (
    'v', 'kind', 'run', 't_board_epoch', 'backend', 'model', 'model_sha256', 'profile', 'host', 'port',
    'client_sha256', 'commitment', 'status', 'stop', 'error', 'verified', 'returncode', 'overdue', 'wall_ns',
    'request_sha256', 'response_payload_sha256', 'device_calls', 'PL_hz', 'irq_delta', 'irq_expected',
    'completed_requests', 'rows', 'requests', 'window_ms', 'mode', 'flags',
    'input_read_ns', 'cold_context_and_TLS_ns', 'prepare_ns', 'TLS_roundtrip_and_validate_ns', 'decrypt_ns',
    'elapsed_ns', 'records_per_s', 'max_public_error', 'requests_over_5e4', 'trace', 'trace_count', 'trace_sha256',
)
TRACE_FIELDS = ('admitted_ns', 'completed_ns', 'prepare_ns', 'TLS_roundtrip_and_validate_ns', 'decrypt_ns',
                'request_sha256', 'response_payload_sha256')
OWNER_FIELDS = (
    'x', 'z', 'contributions', 'bias', 'form', 'values', 'csv_text', 'canonical_json', 'salt_hex', 'logit_ckks',
    'logit_plain', 'abs_error', 'p_balanced', 'level', 'level_code', 'level_name', 'level_label', 'boundary',
    'boundary_text', 'observed_rate', 'plain_level', 'plain_boundary', 'score', 'passed',
)
assert not set(PUBLIC_FIELDS) & set(OWNER_FIELDS)

DISCLOSURE_NOTICE = ('本檔案揭露一筆虛構的展示答案及其推論結果，供第三方開啟公開紀錄中的承諾值並核對推論；'
                     '本檔案不含任何真實個人的資料，且其內容並非醫療診斷。')


# ----------------------------------------------------------------------------------------------------- events
def dumps(obj):
    """Compact JSON for envelopes and stdout lines (UTF-8 text, finite numbers only)."""
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class EventLog:
    """Thread-safe, in-memory event log: seq increases by 1 per event for the whole gateway process.

    emit() returns the envelope {v, seq, run, type, class, t_mono_ns, data}. Listeners run inside the lock, in seq
    order, and must not emit. wait() blocks until an event newer than seq exists (Server-Sent Events)."""

    def __init__(self, capacity=20000):
        self._cond = threading.Condition()
        self._events = collections.deque(maxlen=capacity)
        self._seq = 0
        self._listeners = []

    def add_listener(self, fn):
        with self._cond:
            self._listeners.append(fn)

    def emit(self, run, etype, cls, data):
        if cls not in CLASSES:
            raise ValueError('event class %r' % (cls,))
        dumps(data)  # refuse a non-serializable or non-finite value before it enters the log
        with self._cond:
            self._seq += 1
            envelope = {'v': ENVELOPE_VERSION, 'seq': self._seq, 'run': run, 'type': etype, 'class': cls,
                        't_mono_ns': time.monotonic_ns(), 'data': data}
            self._events.append(envelope)
            for fn in self._listeners:
                try:
                    fn(envelope)
                except Exception:  # a listener (e.g. a closed stdout) never stops a run
                    pass
            self._cond.notify_all()
        return envelope

    def last_seq(self):
        with self._cond:
            return self._seq

    def first_seq(self):
        with self._cond:
            return self._events[0]['seq'] if self._events else self._seq + 1

    def since(self, seq=0, run=None, classes=CLASSES):
        with self._cond:
            return [e for e in self._events
                    if e['seq'] > seq and (run is None or e['run'] == run) and e['class'] in classes]

    def wait(self, seq, timeout, run=None, classes=CLASSES):
        """Events newer than seq; waits up to timeout seconds for the first one (an empty list on timeout)."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq > seq, timeout)
        return self.since(seq, run, classes)


# ------------------------------------------------------------------------------------------------- commitment
def commitment_of(salt_hex, canonical_json):
    if not HEX64.fullmatch(salt_hex or ''):
        raise ValueError('salt_hex must be 64 lowercase hex characters')
    text = COMMIT_TAG + '\n' + salt_hex + '\n' + canonical_json + '\n'
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def new_commitment(canonical_json):
    """(salt_hex, commitment) with a fresh 32-byte salt from the OS."""
    salt_hex = secrets.token_bytes(32).hex()
    return salt_hex, commitment_of(salt_hex, canonical_json)


# --------------------------------------------------------------------------------------------- public records
def public_record(fields):
    """Only the whitelisted public fields, in PUBLIC_FIELDS order; trace entries keep only TRACE_FIELDS."""
    out = {}
    for key in PUBLIC_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if key == 'trace' and isinstance(value, list):
            value = [{k: entry[k] for k in TRACE_FIELDS if k in entry} for entry in value if isinstance(entry, dict)]
        out[key] = value
    return out


def append_jsonl(path, record):
    """Append one JSON line (sorted keys, ASCII) with O_APPEND; the file is created 0644 (public data)."""
    line = (json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
            + '\n').encode('ascii')
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        _write_all(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def wipe_file(path):
    """Overwrite a private file with zero bytes, fsync and unlink it. False if it did not exist."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    except OSError as e:
        if e.errno == errno.ELOOP:      # a symlink is removed, never followed
            os.unlink(path)
            return True
        raise
    try:
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode):
            remaining = st.st_size
            block = bytes(65536)
            while remaining > 0:
                remaining -= os.write(fd, block[:min(len(block), remaining)])
            os.fsync(fd)
    finally:
        os.close(fd)
    os.unlink(path)
    return True


def _write_private(path, data, exclusive=True):
    """Create a 0600 file (exclusively by default; no symlink is followed), write it and fsync it. A file whose write
    or fsync failed (for example ENOSPC on tmpfs) is overwritten with zeros and unlinked before the error propagates,
    so that no partial private file (salt, answers) stays behind."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    written = False
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, data)
        os.fsync(fd)
        written = True
    finally:
        try:
            os.close(fd)
        finally:
            if not written:
                _discard(path)


def _discard(path):
    """Best effort: wipe_file(path), else a plain unlink; never raises."""
    try:
        wipe_file(path)
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass


def _private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise OSError(errno.EPERM, 'not a directory owned by this user', path)
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o700)


class PrivateStore:
    """Owner records of accepted forms, root-only, on the tmpfs work directory (cleared by a reboot).

    A record is needed only to write a disclosure later (it holds the salt that opens the commitment)."""

    def __init__(self, directory):
        self.directory = directory

    def path(self, run):
        if not RUN_ID.fullmatch(run or ''):
            raise ValueError('run id')
        return os.path.join(self.directory, run + '.json')

    def save(self, record):
        _private_dir(self.directory)
        data = (json.dumps(record, sort_keys=True, indent=1, ensure_ascii=False, allow_nan=False)
                + '\n').encode('utf-8')
        _write_private(self.path(record['run']), data)

    def load(self, run):
        with open(self.path(run), 'rb') as f:
            return json.loads(f.read().decode('utf-8'))

    def find(self, key):
        """The record of a run id or of a request_sha256, or None."""
        if RUN_ID.fullmatch(key or ''):
            try:
                return self.load(key)
            except FileNotFoundError:
                return None
        if not HEX64.fullmatch(key or ''):
            return None
        try:
            names = sorted(os.listdir(self.directory))
        except FileNotFoundError:
            return None
        for name in names:
            if name.endswith('.json') and RUN_ID.fullmatch(name[:-5]):
                record = self.load(name[:-5])
                if record.get('request_sha256') == key:
                    return record
        return None

    def remove(self, run):
        return wipe_file(self.path(run))


def disclosure(record):
    """The disclosure document of one private record (fictional demo answers only)."""
    keys = ('run', 'kind', 'backend', 'model', 'model_sha256', 'request_sha256', 'response_payload_sha256',
            'commitment', 'salt_hex', 'canonical_json', 'form', 'csv_text', 'x', 'z', 'contributions',
            'logit_ckks', 'logit_plain', 'abs_error', 'max_abs_error', 'passed', 'level', 'boundary', 'p_balanced',
            'status', 'stop', 'error', 't_board_epoch')
    doc = {'schema': DISCLOSURE_SCHEMA, 'fictional': True, 'notice': DISCLOSURE_NOTICE, 'commitment_rule': COMMIT_RULE}
    for key in keys:
        doc[key] = record.get(key)
    if commitment_of(doc['salt_hex'], doc['canonical_json']) != doc['commitment']:
        raise ValueError('the private record does not open its own commitment')
    return doc


def write_disclosure(directory, record):
    """Write <directory>/<request_sha256 or run>.json, 0600, exclusive. Returns (path, existed_identical)."""
    doc = disclosure(record)
    name = doc['request_sha256'] if HEX64.fullmatch(doc['request_sha256'] or '') else doc['run']
    data = (json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    _private_dir(directory)
    path = os.path.join(directory, name + '.json')
    try:
        _write_private(path, data)
    except FileExistsError:
        with open(path, 'rb') as f:
            if f.read() == data:
                return path, True
        raise
    return path, False
