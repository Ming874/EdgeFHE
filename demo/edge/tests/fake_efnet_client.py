#!/usr/bin/env python3
"""Fake efnet_client for the gateway tests: the same argv, permit check, STOP codes and outputs; no network, no key.

It parses argv like the client's Options (common.hpp:37-44), repeats the option checks of client.cpp:107-135 in the
same order, computes the invocation contract on its own (an independent port of invoke_permit.hpp and client.cpp:
141-150; it deliberately does not import the gateway) and re-validates the PL permit like invoke_permit.hpp:29-43:
fd >= 3, root, a regular file opened read-only, mode 0600, one link, parent pid > 1 and equal to the permit's pid line,
boot_id, and st_dev/st_ino of its own executable (sys.argv[0], the file the gateway started). Then it emulates one run:
DMA interrupts, upload and download bytes, the 0600 score file and the client JSON line on stdout, or the line
'STOP_CPP_E2E_CLIENT_PRESERVE_NO_RETRY <CODE>' on stderr with exit status 1.

The behaviour comes from the file named by --plugin (the real client loads its OS PRNG plugin from that path), as JSON:
  model_json           path of cdc-diabetes-lr4-v1.json (default: ../../model/ next to this file)
  dma_node             PL device path; with --backend pl and no such path the fake stops with OWNER_CREATE
                       (default /dev/edgefhe_v8b_dma), as the real client does before any TLS connection
  interrupts           fake /proc/interrupts: every device call adds 1 to each xilinx-dma-controller line
  irq_extra            extra interrupts on the first DMA line at the end (an interrupt mismatch)
  pl_hz                reported PL clock in Hz (default 199999998)
  profile_hex          the compiled profile id (default c01b69cf..., profile_id.hpp)
  stop                 STOP code to fail with after the permit check and the key load, e.g. "TLS_PEER_PIN"
  logit_offset         added to the true logit in the score file (default 1e-6)
  device_calls_offset  added to device_calls in the JSON
  sleep_s              sleep before the final output (watchdog and SIGTERM tests)
  io                   {"up", "down", "chunk", "pause_s", "delay_s"}: bytes written to /dev/null and read from
                       /dev/zero after the ENC call, so /proc/<pid>/io wchar/rchar grow like an upload and download
  spawn_log            append one JSON line per invocation (pid, ppid, argv)
  signal_log           append one line per SIGTERM/SIGINT/SIGHUP received; the gateway must never send one
  observe_dir          hard-link in.csv and score.txt there before exiting, so a test can see them being wiped
  throughput_requests  requests completed by a --window-ms run (default 40)
  public_error         max_public_error of a synthetic run (default 1.25e-05)
Python 3.10 standard library only; Linux.
"""
import hashlib
import json
import math
import os
import re
import secrets
import signal
import stat
import sys
import time

STOP = 'STOP_CPP_E2E_CLIENT_PRESERVE_NO_RETRY'
BOOT_ID = '/proc/sys/kernel/random/boot_id'
DMA_ACTION = 'xilinx-dma-controller'
PROFILE_HEX = 'c01b69cf3da9fb260dbd4ac15080bd6c1c425d90bfb9015c8bacc16c061fa05c'
HERE = os.path.dirname(os.path.realpath(__file__))
_HEX64 = re.compile(r'[0-9a-f]{64}')
_DOUBLE = re.compile(r'-?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')


class Stop(Exception):
    pass


def need(ok, code):
    if not ok:
        raise Stop(code)


def number(text, low, high):
    """std::from_chars into uint64 over the whole string, then the range (common.hpp:36)."""
    need(re.fullmatch(r'[0-9]+', text or '') is not None, 'OPTION_NUMBER_RANGE')
    value = int(text)
    need(value < 1 << 64 and low <= value <= high, 'OPTION_NUMBER_RANGE')
    return value


def unhex(text):
    need(len(text) == 64, 'DIGEST_HEX_LENGTH')
    need(_HEX64.fullmatch(text) is not None, 'DIGEST_HEX_LOWERCASE')
    return bytes.fromhex(text)


class Options:
    """common.hpp:37-44: --key value pairs; a key twice, or without the -- prefix, is OPTION_DUPLICATE."""

    def __init__(self, argv):
        self.values = {}
        i = 1
        while i < len(argv):
            need(i + 1 < len(argv), 'OPTION_VALUE_MISSING')
            key = argv[i]
            need(key.startswith('--') and key not in self.values, 'OPTION_DUPLICATE')
            self.values[key] = argv[i + 1]
            i += 2

    def take(self, key, fallback=''):
        return self.values.pop(key, fallback)

    def required(self, key):
        value = self.take(key)
        need(value != '', 'OPTION_REQUIRED')
        return value

    def finish(self):
        need(not self.values, 'OPTION_UNKNOWN')


# ------------------------------------------------------------------------- contracts (independent port)
def digest_hex(text):
    return hashlib.sha256(text.encode('latin-1')).hexdigest()


def invocation_contract(strings):
    """invoke_permit.hpp:6-14 over the 16 strings in their order."""
    out = ''
    for s in strings:
        need(s != '' and '\n' not in s and '\r' not in s and '\0' not in s, 'PERMIT_CONFIG_STRING')
        out += s + '\n'
    return digest_hex(out)


def layered(base, mode, capacity, duplex, hash_io, decrypt_thread, inflight, io_cpu, io_isolate, read_every):
    """client.cpp:144-149."""
    if capacity == 2 or mode != 'pipeline':
        depth = base
    else:
        depth = digest_hex('EDGEFHE_SLOTS_V1\n%s\n%d\n' % (base, capacity))
    io = digest_hex('EDGEFHE_DUPLEX_V1\n%s\n' % depth) if duplex else depth
    if hash_io:
        io = digest_hex('EDGEFHE_HASH_IO_V1\n%s\n' % io)
    if decrypt_thread:
        io = digest_hex('EDGEFHE_DECRYPT_THREAD_V1\n%s\n' % io)
    if inflight:
        io = digest_hex('EDGEFHE_INFLIGHT_V1\n%s\n%d\n' % (io, inflight))
    if io_cpu != 'none' or read_every != 1:
        io = digest_hex('EDGEFHE_IO_PLACEMENT_V1\n%s\n%s\n%s\n%d\n' % (io, io_cpu, '1' if io_isolate else '0',
                                                                       read_every))
    return io


def private_contract(base, input_path, output_path):
    """invoke_permit.hpp:17-21."""
    for s in (input_path, output_path):
        need(s != '' and len(s) < 4096 and '\n' not in s and '\r' not in s and '\0' not in s, 'PERMIT_PRIVATE_PATH')
    need(len(base) == 64, 'PERMIT_BASE_CONTRACT')
    return digest_hex('EDGEFHE_PRIVATE_INPUT_V1\n%s\n%s\n%s\n' % (base, input_path, output_path))


def validate_invocation_permit(text, identity, contract):
    """invoke_permit.hpp:29-32 for collected facts: text is the permit file content (bytes)."""
    i = identity
    need(i['root'] and i['regular'] and i['readonly'] and i['uid'] == 0 and i['mode'] == 0o600 and i['links'] == 1
         and len(i['boot']) == 36 and i['parent'] > 1 and i['inode'] > 0, 'PL_PERMIT_IDENTITY')
    canonical = ('EDGEFHE_CPP_MTLS_PL_V1\n%s\n%d\n%d\n%d\n%s\n'
                 % (i['boot'], i['parent'], i['device'], i['inode'], contract)).encode('latin-1')
    ok = len(contract) == 64 and any(unhex(contract)) and len(text) < 1024 and text == canonical
    need(ok, 'PL_PERMIT_INVOCATION_BINDING')


def check_invocation_permit(fd_text, contract, own_exe):
    """invoke_permit.hpp:33-43 on the live descriptor."""
    import fcntl
    fd = number(fd_text, 3, 1024)
    need(fd >= 3 and os.getuid() == 0 and os.geteuid() == 0, 'PL_ROOT_CONTROLLER_REQUIRED')
    try:
        token = os.fstat(fd)
        exe = os.stat(own_exe)
    except OSError:
        raise Stop('PL_PERMIT_STAT')
    need(0 < token.st_size < 1024, 'PL_PERMIT_STAT')
    try:
        text = os.pread(fd, token.st_size, 0)
    except OSError:
        raise Stop('PL_PERMIT_READ')
    need(len(text) == token.st_size, 'PL_PERMIT_READ')
    try:
        with open(BOOT_ID, 'rb') as f:
            line = f.readline()
    except OSError:
        raise Stop('PL_PERMIT_BOOT_READ')
    need(line != b'', 'PL_PERMIT_BOOT_READ')
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    except OSError:
        raise Stop('PL_PERMIT_FLAGS')
    identity = {'root': True, 'regular': stat.S_ISREG(token.st_mode),
                'readonly': (flags & os.O_ACCMODE) == os.O_RDONLY, 'uid': token.st_uid,
                'mode': token.st_mode & 0o7777, 'links': token.st_nlink, 'parent': os.getppid(),
                'device': exe.st_dev, 'inode': exe.st_ino, 'boot': line.rstrip(b'\n').decode('latin-1')}
    validate_invocation_permit(text, identity, contract)
    try:
        os.close(fd)
    except OSError:
        raise Stop('PL_PERMIT_REVOKE_BEFORE_OPEN')


# ------------------------------------------------------------------------------------------------- emulation
def load_behavior(path):
    try:
        with open(path, 'rb') as f:
            value = json.loads(f.read().decode('utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def load_model(behavior):
    path = behavior.get('model_json') or os.path.join(HERE, '..', '..', 'model', 'cdc-diabetes-lr4-v1.json')
    with open(path, 'rb') as f:
        return json.loads(f.read().decode('utf-8'))


def read_private_csv(path, model):
    """client.cpp:39-55: header equal to the features, at most 2048 non-empty rows of from_chars doubles."""
    try:
        with open(path, 'rb') as f:
            data = f.read(2 * 1024 * 1024 + 1)
    except OSError:
        raise Stop('CSV_OPEN')
    need(len(data) <= 2 * 1024 * 1024, 'CSV_MAX_BYTES')
    lines = data.split(b'\n')
    if data.endswith(b'\n'):
        lines.pop()
    need(bool(lines) and data != b'', 'CSV_HEADER')

    def strip(line):
        return line[:-1] if line.endswith(b'\r') else line
    need(strip(lines[0]).decode('latin-1').split(',') == model['features'], 'CSV_FEATURE_ORDER')
    rows = []
    for line in lines[1:]:
        line = strip(line)
        need(len(rows) < 2048 and line != b'', 'CSV_ROW_COUNT')
        fields = line.decode('latin-1').split(',')
        need(len(fields) == len(model['features']), 'CSV_DIMENSIONS')
        row = []
        for field in fields:
            need(_DOUBLE.fullmatch(field) is not None, 'CSV_NUMERIC')
            value = float(field)
            need(math.isfinite(value), 'CSV_NUMERIC')
            row.append(value)
        rows.append(row)
    need(len(rows) > 0, 'CSV_READ')
    return rows


def logit(model, x):
    total = model['bias']
    for f in range(len(model['features'])):
        total += model['weights'][f] * ((x[f] - model['mean'][f]) / model['std'][f])
    return total


def bump_irq(behavior, count, extra=0):
    """Add count to every DMA line of the fake /proc/interrupts (plus extra on the first), atomically."""
    path = behavior.get('interrupts')
    if not path or (count == 0 and extra == 0):
        return
    with open(path, 'r', encoding='ascii') as f:
        lines = f.read().splitlines()
    cpus = len(lines[0].split())
    out, first = [lines[0]], True
    for line in lines[1:]:
        parts = line.split()
        if DMA_ACTION in line and len(parts) > cpus + 1:
            counts = [int(c) for c in parts[1:1 + cpus]]
            counts[0] += count + (extra if first else 0)
            first = False
            line = '%4s %s %s' % (parts[0], ' '.join('%10d' % c for c in counts), ' '.join(parts[1 + cpus:]))
        out.append(line)
    temp = '%s.%d.tmp' % (path, os.getpid())
    with open(temp, 'w', encoding='ascii') as f:
        f.write('\n'.join(out) + '\n')
    os.replace(temp, path)


def emulate_io(spec):
    if not isinstance(spec, dict):
        return
    time.sleep(float(spec.get('delay_s', 0.05)))
    chunk, pause = int(spec.get('chunk', 16401)), float(spec.get('pause_s', 0.002))
    with open('/dev/null', 'wb', buffering=0) as sink:
        left = int(spec.get('up', 394022))
        while left > 0:
            n = min(chunk, left)
            left -= sink.write(bytes(n))
            time.sleep(pause)
    with open('/dev/zero', 'rb', buffering=0) as source:
        left = int(spec.get('down', 98714))
        while left > 0:
            left -= len(source.read(min(chunk, left)))
            time.sleep(pause)


def append_line(path, text):
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(text + '\n')


def write_scores(path, values):
    """client.cpp:56-67: O_EXCL 0600, one %.17g line per row, fsync."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        raise Stop('PRIVATE_OUTPUT_EXISTS_OR_OPEN')
    try:
        os.write(fd, ''.join('%.17g\n' % v for v in values).encode('ascii'))
        os.fsync(fd)
    finally:
        os.close(fd)


def run(argv, behavior_box):
    o = Options(argv)
    ca, cert, key = o.required('--ca'), o.required('--cert'), o.required('--key')
    peer_name = o.required('--peer-name')
    pin_hex = o.required('--peer-sha256')
    pin = unhex(pin_hex)
    need(len(peer_name) <= 253 and '\0' not in peer_name and any(pin), 'TLS_PEER_ID')
    host = o.required('--host')
    port = number(o.required('--port'), 1, 65535)
    timeout = number(o.take('--timeout-ms', '30000'), 100, 120000)
    backend = o.take('--backend', 'cpu')
    need(backend in ('cpu', 'spec', 'pl'), 'BACKEND_NAME')
    workers = number(o.take('--workers', '3'), 1, 3)
    need(workers in (1, 3), 'WORKERS_1_OR_3')
    plugin = o.required('--plugin')
    behavior = load_behavior(plugin)
    behavior_box.update(behavior)
    append_line(behavior.get('spawn_log'), json.dumps({'pid': os.getpid(), 'ppid': os.getppid(), 'argv': argv}))
    model_id = o.required('--model')
    model = load_model(behavior)
    need(model_id == model['model_id'], 'MODEL_UNKNOWN')
    features = len(model['features'])
    rows = number(o.take('--rows', '1'), 1, 2048)
    mode = o.take('--mode', 'serial')
    need(mode in ('serial', 'pipeline'), 'MODE_NAME')
    slots = number(o.take('--slots', '2'), 2, 8)
    need(mode == 'pipeline' or slots == 2, 'SLOTS_PIPELINE_ONLY')
    duplex_text = o.take('--duplex', '0')
    need(duplex_text in ('0', '1'), 'DUPLEX_0_OR_1')
    duplex = duplex_text == '1'
    need(not duplex or mode == 'pipeline', 'DUPLEX_PIPELINE_ONLY')
    hash_text, dec_text = o.take('--hash-io', '0'), o.take('--decrypt-thread', '0')
    need(hash_text in ('0', '1') and dec_text in ('0', '1'), 'O4_O5_FLAGS_0_OR_1')
    hash_io, decrypt_thread = hash_text == '1', dec_text == '1'
    need((not hash_io and not decrypt_thread) or mode == 'pipeline', 'O4_O5_PIPELINE_ONLY')
    inflight = number(o.take('--inflight', '0'), 0, 8)
    capacity = slots if mode == 'pipeline' else 1
    need(inflight == 0 or (duplex and 2 <= inflight <= capacity), 'INFLIGHT_REQUIRES_DUPLEX_2_TO_SLOTS')
    maximum = number(o.take('--requests', '1'), 1, 100000)
    io_cpu = o.take('--io-cpu', 'none')
    need(io_cpu in ('none', 'auto') or re.fullmatch(r'[0-9]{1,4}', io_cpu) is not None, 'IO_CPU_NONE_AUTO_OR_NUMBER')
    isolate_text = o.take('--io-isolate', '0')
    need(isolate_text in ('0', '1'), 'IO_ISOLATE_0_OR_1')
    io_isolate = isolate_text == '1'
    need(not io_isolate or io_cpu != 'none', 'IO_ISOLATE_REQUIRES_IO_CPU')
    read_every = number(o.take('--read-every', '1'), 1, 64)
    need(read_every == 1 or inflight, 'READ_EVERY_REQUIRES_INFLIGHT')
    window_ms = number(o.take('--window-ms', '0'), 0, 60000)
    permit_fd = o.take('--permit-fd')
    input_path, output_path = o.take('--input'), o.take('--scores-output')
    o.finish()
    need(not input_path or (maximum == 1 and window_ms == 0), 'PRIVATE_CSV_SINGLE_REQUEST_ONLY')
    need(not output_path or input_path, 'PRIVATE_OUTPUT_REQUIRES_PRIVATE_INPUT')
    synthetic = not input_path
    started = time.monotonic_ns()
    raw = None if synthetic else read_private_csv(input_path, model)
    if raw is not None:
        rows = len(raw)
    input_ns = time.monotonic_ns() - started
    base = invocation_contract([model_id, str(rows), mode, str(maximum), str(window_ms), str(workers), host,
                                str(port), str(timeout), ca, cert, key, peer_name, pin.hex(), plugin,
                                behavior.get('profile_hex', PROFILE_HEX)])
    io = layered(base, mode, capacity, duplex, hash_io, decrypt_thread, inflight, io_cpu, io_isolate, read_every)
    contract = io if synthetic else private_contract(io, input_path, output_path)
    if backend == 'pl':
        need(permit_fd != '' and (synthetic or output_path != ''), 'PL_REQUIRES_CONTROLLED_INVOCATION')
        check_invocation_permit(permit_fd, contract, os.path.abspath(sys.argv[0]))
    else:
        need(permit_fd == '', 'CPU_SPEC_TAKES_NO_PL_PERMIT')
    if backend == 'pl':                                   # Owner (client.cpp:159) opens the DMA node, then LOADs
        need(os.path.exists(behavior.get('dma_node', '/dev/edgefhe_v8b_dma')), 'OWNER_CREATE')
        bump_irq(behavior, 1)
    if behavior.get('stop'):                              # the TLS connection of the I/O thread
        raise Stop(str(behavior['stop']))
    cold_ns = time.monotonic_ns() - started
    produced = maximum
    if window_ms:
        produced = min(maximum, int(behavior.get('throughput_requests', 40)))
    if raw is not None:
        for row in raw:                                   # ef_normalize_checked: |z| <= 8, no clipping
            for f in range(features):
                z = (row[f] - model['mean'][f]) / model['std'][f]
                need(math.isfinite(z) and abs(z) <= 8.0, 'NORMALIZATION_REFUSED')
    frames = 0 if backend == 'cpu' else 1 + produced * ((features + 3) // 4)
    if backend == 'pl':
        for _ in range(produced):
            bump_irq(behavior, (features + 3) // 4)
    emulate_io(behavior.get('io'))
    time.sleep(float(behavior.get('sleep_s', 0)))
    if backend == 'pl':
        bump_irq(behavior, 0, int(behavior.get('irq_extra', 0)))
    trace = []
    for i in range(produced):
        trace.append({'admitted_ns': i * 1000, 'completed_ns': i * 1000 + 900, 'prepare_ns': 40000,
                      'TLS_roundtrip_and_validate_ns': 52000000, 'decrypt_ns': 3000000,
                      'request_sha256': secrets.token_hex(32), 'response_payload_sha256': secrets.token_hex(32)})
    if not synthetic:
        score = logit(model, raw[0]) + float(behavior.get('logit_offset', 1e-6))
        write_scores(output_path, [score])
    elapsed = max(1, time.monotonic_ns() - started - cold_ns)
    public_error = float(behavior.get('public_error', 1.25e-05))
    report = {
        'status': 'CPP_E2E_PL_NUMERIC_AND_DRAIN_PASS' if backend == 'pl' else 'CPP_E2E_CPU_SPEC_NUMERIC_AND_DRAIN_PASS',
        'backend': backend, 'model': model_id, 'features': features, 'rows': rows, 'mode': mode, 'slots': capacity,
        'duplex': duplex, 'hash_io': hash_io, 'decrypt_thread': decrypt_thread, 'inflight': inflight,
        'io_cpu': io_cpu, 'io_cpu_chosen': -1 if io_cpu == 'none' else 1, 'io_isolate': io_isolate,
        'read_every': read_every, 'io_thread_last_cpu': 1, 'net_softirq_start': None, 'net_softirq_delta': None,
        'owner_workers': workers, 'input_read_ns': input_ns, 'cold_context_and_TLS_ns': cold_ns,
        'completed_requests': produced, 'elapsed_ns': elapsed, 'admission_limit_ns': window_ms * 1000000,
        'records_per_s': produced * rows * 1e9 / elapsed, 'synthetic_input': synthetic,
        'checked_public_scores': produced * rows if synthetic else 0,
        'max_public_error': public_error if synthetic else None,
        'requests_over_5e4': (1 if public_error > 5e-4 else 0) * produced if synthetic else None,
        'spec_frames': frames if backend == 'spec' else 0,
        'device_calls': frames + int(behavior.get('device_calls_offset', 0)),
        'PL_hz': int(behavior.get('pl_hz', 199999998)) if backend == 'pl' else 0,
        'actual_DMA': frames if backend == 'pl' else 0, 'real_PL': backend == 'pl', 'network_included': True,
        'formal_campaign': False, 'automatic_retries': 0, 'trace': trace,
    }
    sys.stdout.write(json.dumps(report, separators=(',', ':')) + '\n')
    sys.stdout.flush()
    return 0


def observe(behavior, argv):
    """Hard-link the private files into observe_dir (same tmpfs) so a test can check the wipe afterwards."""
    target = behavior.get('observe_dir')
    if not target:
        return
    for flag, name in (('--input', 'in.csv'), ('--scores-output', 'score.txt')):
        if flag in argv:
            path = argv[argv.index(flag) + 1]
            if os.path.exists(path):
                os.link(path, os.path.join(target, '%d.%s' % (os.getpid(), name)))


def main(argv):
    behavior = {}

    def record(signum, frame):
        append_line(behavior.get('signal_log'), 'signal %d pid %d' % (signum, os.getpid()))
    for number_ in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number_, record)
    try:
        code = run(argv, behavior)
    except Stop as e:
        sys.stderr.write('%s %s\n' % (STOP, e.args[0]))
        sys.stderr.flush()
        code = 1
    try:
        observe(behavior, argv)
    except OSError:
        pass
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv))
