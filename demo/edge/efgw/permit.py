"""Invocation contracts and the single-use PL permit file (Python 3.10 standard library only).

Mirrors v8b_rev5/software/client/invoke_permit.hpp and the contract layers of client.cpp:141-150 byte for byte:

  base      sha256 of 16 strings, each + LF (invoke_permit.hpp:9-10): model, rows, mode, requests, window_ms,
            workers, host, port, timeout_ms, ca, cert, key, peer_name, peer_sha256, plugin, profile_hex
  layers    EDGEFHE_SLOTS_V1 (pipeline with slots != 2), EDGEFHE_DUPLEX_V1, EDGEFHE_HASH_IO_V1,
            EDGEFHE_DECRYPT_THREAD_V1, EDGEFHE_INFLIGHT_V1, EDGEFHE_IO_PLACEMENT_V1, in that order (client.cpp:144-149)
  private   EDGEFHE_PRIVATE_INPUT_V1 over the layered contract and the literal CSV and score paths
            (invoke_permit.hpp:17-21); a synthetic run uses the layered contract itself (client.cpp:150)
  permit    'EDGEFHE_CPP_MTLS_PL_V1\\n<boot_id>\\n<controller pid>\\n<st_dev>\\n<st_ino>\\n<contract>\\n'
            (invoke_permit.hpp:26-28), where the controller is the client's direct parent and st_dev/st_ino
            identify the client executable (the client compares them with stat("/proc/self/exe"))

tests/contract_vectors.json, printed by tests/contract_vector.cpp from the real header, pins the equality.
"""
import hashlib
import os
import re
import stat

PERMIT_TAG = 'EDGEFHE_CPP_MTLS_PL_V1'
BOOT_ID_PATH = '/proc/sys/kernel/random/boot_id'
PERMIT_MAX_BYTES = 1024
MODES = ('serial', 'pipeline')
_HEX64 = re.compile(r'[0-9a-f]{64}')
_BOOT = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


class PermitError(Exception):
    def __init__(self, code, detail=''):
        self.code = code
        self.detail = detail
        super().__init__('%s %s' % (code, detail) if detail else code)


def _need(condition, code, detail=''):
    if not condition:
        raise PermitError(code, detail)


def digest_hex(text):
    """hex(digest(text)) of common.hpp for an ASCII string."""
    return hashlib.sha256(text.encode('ascii')).hexdigest()


def _clean(value, code):
    _need(type(value) is str and value != '' and not any(c in value for c in '\n\r\0'), code, repr(value))
    _need(all(32 <= ord(c) < 127 for c in value), code, 'non-ASCII ' + repr(value))
    return value


class ClientFlags:
    """The efnet_client options that shape the contract layers, validated like client.cpp:112-133."""

    def __init__(self, mode='serial', slots=2, duplex=False, hash_io=False, decrypt_thread=False, inflight=0,
                 io_cpu='none', io_isolate=False, read_every=1):
        self.mode = mode
        self.slots = slots
        self.duplex = duplex
        self.hash_io = hash_io
        self.decrypt_thread = decrypt_thread
        self.inflight = inflight
        self.io_cpu = io_cpu
        self.io_isolate = io_isolate
        self.read_every = read_every
        self.validate()

    @property
    def capacity(self):
        return self.slots if self.mode == 'pipeline' else 1

    def validate(self):
        pipeline = self.mode == 'pipeline'
        _need(self.mode in MODES, 'MODE_NAME')
        _need(type(self.slots) is int and 2 <= self.slots <= 8, 'OPTION_NUMBER_RANGE', 'slots')
        _need(pipeline or self.slots == 2, 'SLOTS_PIPELINE_ONLY')
        for name in ('duplex', 'hash_io', 'decrypt_thread', 'io_isolate'):
            _need(type(getattr(self, name)) is bool, 'FLAG_TYPE', name)
        _need(not self.duplex or pipeline, 'DUPLEX_PIPELINE_ONLY')
        _need((not self.hash_io and not self.decrypt_thread) or pipeline, 'O4_O5_PIPELINE_ONLY')
        _need(type(self.inflight) is int and 0 <= self.inflight <= 8, 'OPTION_NUMBER_RANGE', 'inflight')
        _need(self.inflight == 0 or (self.duplex and 2 <= self.inflight <= self.capacity),
              'INFLIGHT_REQUIRES_DUPLEX_2_TO_SLOTS')
        _need(type(self.io_cpu) is str and (self.io_cpu in ('none', 'auto')
                                            or re.fullmatch(r'[0-9]{1,4}', self.io_cpu) is not None),
              'IO_CPU_NONE_AUTO_OR_NUMBER')
        _need(not self.io_isolate or self.io_cpu != 'none', 'IO_ISOLATE_REQUIRES_IO_CPU')
        _need(type(self.read_every) is int and 1 <= self.read_every <= 64, 'OPTION_NUMBER_RANGE', 'read_every')
        _need(self.read_every == 1 or self.inflight > 0, 'READ_EVERY_REQUIRES_INFLIGHT')

    def argv(self):
        """Client options other than --mode; only the values that differ from the client defaults."""
        out = []
        if self.slots != 2:
            out += ['--slots', str(self.slots)]
        for flag, value in (('--duplex', self.duplex), ('--hash-io', self.hash_io),
                            ('--decrypt-thread', self.decrypt_thread)):
            if value:
                out += [flag, '1']
        if self.inflight:
            out += ['--inflight', str(self.inflight)]
        if self.io_cpu != 'none':
            out += ['--io-cpu', self.io_cpu, '--io-isolate', '1' if self.io_isolate else '0']
        if self.read_every != 1:
            out += ['--read-every', str(self.read_every)]
        return out

    def as_dict(self):
        return {'mode': self.mode, 'slots': self.slots, 'duplex': self.duplex, 'hash_io': self.hash_io,
                'decrypt_thread': self.decrypt_thread, 'inflight': self.inflight, 'io_cpu': self.io_cpu,
                'io_isolate': self.io_isolate, 'read_every': self.read_every}


FORM_FLAGS = ClientFlags()
# Best measured PL configuration (v8b_rev5/evidence/CLAIMS_LEDGER.json, current_best/pl/config): placement B.
PIPELINE_BEST = ClientFlags(mode='pipeline', slots=8, duplex=True, hash_io=True, decrypt_thread=True, inflight=8,
                            io_cpu='auto', io_isolate=False, read_every=1)


def base_contract(model, rows, mode, requests, window_ms, workers, host, port, timeout_ms, ca, cert, key,
                  peer_name, peer_sha256, plugin, profile_hex):
    """invocation_contract() of invoke_permit.hpp:6-14."""
    _need(_HEX64.fullmatch(peer_sha256 or '') is not None, 'DIGEST_HEX_LOWERCASE', 'peer_sha256')
    _need(_HEX64.fullmatch(profile_hex or '') is not None, 'DIGEST_HEX_LOWERCASE', 'profile_hex')
    fields = [model, str(rows), mode, str(requests), str(window_ms), str(workers), host, str(port), str(timeout_ms),
              ca, cert, key, peer_name, peer_sha256, plugin, profile_hex]
    return digest_hex(''.join(_clean(s, 'PERMIT_CONFIG_STRING') + '\n' for s in fields))


def layered_contract(base, flags):
    """client.cpp:144-149: the depth and I/O layers over the base contract."""
    _need(_HEX64.fullmatch(base or '') is not None, 'PERMIT_BASE_CONTRACT')
    flags.validate()
    if flags.capacity == 2 or flags.mode != 'pipeline':
        depth = base
    else:
        depth = digest_hex('EDGEFHE_SLOTS_V1\n' + base + '\n' + str(flags.capacity) + '\n')
    io = digest_hex('EDGEFHE_DUPLEX_V1\n' + depth + '\n') if flags.duplex else depth
    if flags.hash_io:
        io = digest_hex('EDGEFHE_HASH_IO_V1\n' + io + '\n')
    if flags.decrypt_thread:
        io = digest_hex('EDGEFHE_DECRYPT_THREAD_V1\n' + io + '\n')
    if flags.inflight:
        io = digest_hex('EDGEFHE_INFLIGHT_V1\n' + io + '\n' + str(flags.inflight) + '\n')
    if flags.io_cpu != 'none' or flags.read_every != 1:
        io = digest_hex('EDGEFHE_IO_PLACEMENT_V1\n' + io + '\n' + flags.io_cpu + '\n'
                        + ('1' if flags.io_isolate else '0') + '\n' + str(flags.read_every) + '\n')
    return io


def private_contract(io_contract, input_path, output_path):
    """private_invocation_contract() of invoke_permit.hpp:17-21."""
    for path in (input_path, output_path):
        _clean(path, 'PERMIT_PRIVATE_PATH')
        _need(len(path) < 4096, 'PERMIT_PRIVATE_PATH')
    _need(len(io_contract) == 64, 'PERMIT_BASE_CONTRACT')
    return digest_hex('EDGEFHE_PRIVATE_INPUT_V1\n' + io_contract + '\n' + input_path + '\n' + output_path + '\n')


def invocation_contract(cfg, model, rows, requests, window_ms, flags, private_paths=None):
    """{'base', 'io', 'contract'} for one client invocation built from the gateway configuration."""
    base = base_contract(model, rows, flags.mode, requests, window_ms, cfg['workers'], cfg['host'], cfg['port'],
                         cfg['timeout_ms'], cfg['ca'], cfg['cert'], cfg['key'], cfg['peer_name'],
                         cfg['peer_sha256'], cfg['plugin'], cfg['profile_hex'])
    io = layered_contract(base, flags)
    contract = private_contract(io, private_paths[0], private_paths[1]) if private_paths else io
    return {'base': base, 'io': io, 'contract': contract}


def permit_text(boot_id, controller_pid, exe_dev, exe_ino, contract):
    """permit_canonical() of invoke_permit.hpp:26-28."""
    _need(_BOOT.fullmatch(boot_id or '') is not None, 'PL_PERMIT_BOOT_READ', repr(boot_id))
    _need(type(controller_pid) is int and controller_pid > 1, 'PL_PERMIT_IDENTITY', 'controller pid must be > 1')
    _need(type(exe_ino) is int and exe_ino > 0 and type(exe_dev) is int and exe_dev >= 0, 'PL_PERMIT_STAT')
    _need(_HEX64.fullmatch(contract or '') is not None and contract != '0' * 64, 'PL_PERMIT_INVOCATION_BINDING')
    text = '%s\n%s\n%d\n%d\n%d\n%s\n' % (PERMIT_TAG, boot_id, controller_pid, exe_dev, exe_ino, contract)
    _need(len(text) < PERMIT_MAX_BYTES, 'PL_PERMIT_STAT', 'permit longer than 1023 bytes')
    return text


def read_boot_id(path=BOOT_ID_PATH):
    """The first line of boot_id (std::getline in invoke_permit.hpp:37)."""
    try:
        with open(path, 'r', encoding='ascii') as f:
            boot = f.readline().rstrip('\n')
    except (OSError, ValueError) as e:
        raise PermitError('PL_PERMIT_BOOT_READ', str(e))
    _need(_BOOT.fullmatch(boot) is not None, 'PL_PERMIT_BOOT_READ', repr(boot))
    return boot


def executable_identity(path):
    """(st_dev, st_ino) of the client executable, the values the client reads from stat("/proc/self/exe")."""
    st = os.stat(path)
    _need(stat.S_ISREG(st.st_mode), 'PL_PERMIT_STAT', 'client is not a regular file')
    return st.st_dev, st.st_ino


def write_permit(path, text):
    """Create the permit with O_CREAT|O_EXCL|O_NOFOLLOW, fchmod 0600, fsync, then reopen it read-only.

    Returns the read-only descriptor (>= 3, close-on-exec in this process; subprocess pass_fds makes it inheritable
    in the client only). The caller unlinks the file only after the client exited, so its link count stays 1 while
    the client checks it (invoke_permit.hpp:30)."""
    data = text.encode('ascii')
    _need(0 < len(data) < PERMIT_MAX_BYTES, 'PL_PERMIT_STAT', 'permit size')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    rfd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if rfd < 3:
            import fcntl
            high = fcntl.fcntl(rfd, fcntl.F_DUPFD_CLOEXEC, 3)
            os.close(rfd)
            rfd = high
        st = os.fstat(rfd)
        _need(stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o600
              and st.st_nlink == 1 and st.st_size == len(data), 'PL_PERMIT_IDENTITY', 'permit file attributes')
    except BaseException:
        os.close(rfd)
        raise
    return rfd
