"""Live progress of one efnet_client run for the owner screen: DMA interrupt markers and socket byte counters.

While a client runs, a sampler thread reads two kernel counters every interval_ms (cfg['progress_ms'], 25 ms by
default; --progress-ms 0 disables the sampler):

  /proc/interrupts   the xilinx-dma-controller lines (runner.parse_interrupts, runner.DMA_ACTION; cfg
                     'proc_interrupts'). Every increment of a line since the spawn becomes one progress.irq event:
                     the first is the public-key LOAD (stage 4), every later one an ENC call (stage 8; a CDC form has
                     one, because the client sends up to 4 ciphertexts per ENC frame, owner_v5_core.hpp:8,241-251).
  /proc/<pid>/io     wchar and rchar of the client. They count its read() and write() calls, which carry the TLS
                     records (OpenSSL socket BIOs use read and write; the PL is driven by ioctl,
                     v8b_device_v5.hpp:169), so they serve as upload and download progress (stages 9 and 11). A
                     progress.io event is emitted whenever either value changed since the previous event.

Events (public class, demo/edge/EVENTS.md section 3):
  progress.irq  {irq, count, delta, index, label, t_ns}  count and delta (count minus the count at the spawn) as read
                                                          in the sample; index 1 = LOAD, 2 and later = ENC
  progress.io   {wchar, rchar, t_ns}                      bytes since the sampler's first reading of /proc/<pid>/io
t_ns counts from the client.spawned event. The sampler only reads files. It never signals, waits for or otherwise
touches the client (a signal during an issued PL ENC quarantines the driver, ef_v8b_main.c:208,334,389), and it holds
/proc/<pid> open from the start, so a PID reused after the client's exit is never read in its place.
Python 3.10 standard library only.
"""
import os
import threading
import time

from . import runner

DEFAULT_MS = 25
MAX_MS = 1000
MARKER_LIMIT = 32           # increments reported one by one per line and sample; a larger jump is not a form run
LOAD, ENC = 'LOAD', 'ENC'
_O_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
_O_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)


def label(index):
    """The device call that the index-th interrupt of a run belongs to (client.cpp:159, :303)."""
    return LOAD if index == 1 else ENC


def _irq_key(irq):
    return (0, int(irq), '') if irq.isdigit() else (1, 0, irq)


def increments(base, reported, current, limit=MARKER_LIMIT):
    """New interrupt markers [(index, irq, count, delta)], ordered by index and then IRQ number.

    base: {irq: count at the spawn}; reported: {irq: increments already reported}, updated in place; current: a
    runner.parse_interrupts() snapshot. A line that rose by more than limit since the last sample yields only its
    latest increment."""
    found = []
    for irq in sorted(base, key=_irq_key):
        line = current.get(irq)
        if line is None:
            continue
        count = line['count']
        delta = count - base[irq]
        seen = reported.get(irq, 0)
        if delta <= seen:
            continue
        first = seen + 1 if delta - seen <= limit else delta
        found.extend((index, irq, count, delta) for index in range(first, delta + 1))
        reported[irq] = delta
    found.sort(key=lambda marker: (marker[0], _irq_key(marker[1])))
    return found


def parse_io(text):
    """(wchar, rchar) of a /proc/<pid>/io text."""
    values = {}
    for line in text.splitlines():
        name, sep, value = line.partition(':')
        if sep and name.strip() in ('rchar', 'wchar'):
            values[name.strip()] = int(value.strip())
    return values['wchar'], values['rchar']


def _read_at(dirfd, name, limit=4096):
    fd = os.open(name, os.O_RDONLY | _O_CLOEXEC, dir_fd=dirfd)
    try:
        return os.read(fd, limit).decode('ascii', 'replace')
    finally:
        os.close(fd)


def open_process(pid, proc_root='/proc'):
    """A descriptor of /proc/<pid> if pid is a child of this process, else None.

    A lookup through the descriptor reaches only that process: once it has exited (and even if its PID is reused),
    opening a file through it fails."""
    try:
        fd = os.open(os.path.join(proc_root, str(int(pid))), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    except (OSError, ValueError, TypeError):
        return None
    try:
        text = _read_at(fd, 'stat')
        fields = text[text.rindex(')') + 2:].split()     # 'pid (comm) state ppid ...'; comm may contain ')'
        if int(fields[1]) != os.getpid():
            raise ValueError('not a child of this process')
    except (OSError, ValueError, IndexError):
        os.close(fd)
        return None
    return fd


def _why(e):
    return '%s: %s' % (type(e).__name__, getattr(e, 'strerror', None) or e)


class Sampler:
    """Samples the DMA interrupt lines and /proc/<pid>/io of one running client on a daemon thread.

    emit(type, data) adds a public event. The baseline of the interrupt lines is irq_before (the client.spawned
    counts) or, without it, a reading taken here; t0_ns is the monotonic time of client.spawned. stop() ends the
    thread, takes a last reading (the gateway reads its own counters right after) and releases /proc/<pid>."""

    def __init__(self, pid, emit, interrupts_path='/proc/interrupts', interval_ms=DEFAULT_MS, irq_before=None,
                 t0_ns=None, proc_root='/proc'):
        if type(interval_ms) is not int or not 1 <= interval_ms <= MAX_MS:
            raise ValueError('interval_ms must be an integer from 1 to %d' % MAX_MS)
        self.pid = pid
        self._emit = emit
        self._path = interrupts_path
        self._interval = interval_ms / 1000.0
        self._t0 = time.monotonic_ns() if t0_ns is None else t0_ns
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._failed = False
        self._stopped = False
        if irq_before is None:
            irq_before = runner.irq_counts(runner.irq_snapshot(interrupts_path))
        self._base = {str(irq): count for irq, count in irq_before.items() if type(count) is int}
        self._reported = {}
        self._io_last = (0, 0)
        self._dirfd = open_process(pid, proc_root)
        self._io_base = self._read_io()
        self._thread = threading.Thread(target=self._loop, name='edgefhe-progress-%s' % pid, daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._close()
            raise

    def _read_io(self):
        if self._dirfd is None:
            return None
        try:
            return parse_io(_read_at(self._dirfd, 'io'))
        except (OSError, ValueError, KeyError):
            return None                      # the client has exited, or task I/O accounting is unavailable

    def _loop(self):
        while not self._halt.wait(self._interval):
            if not self.sample() or (not self._base and self._io_base is None):
                return                          # failed, or nothing left to read (stop() still takes a last reading)

    def sample(self):
        """One reading of both counters; emits the new events. False once the sampler failed."""
        with self._lock:
            if self._failed:
                return False
            try:
                now = time.monotonic_ns()
                if self._base:
                    snapshot = runner.irq_snapshot(self._path)
                    for index, irq, count, delta in increments(self._base, self._reported, snapshot):
                        self._emit('progress.irq', {'irq': irq, 'count': count, 'delta': delta, 'index': index,
                                                    'label': label(index), 't_ns': now - self._t0})
                if self._io_base is not None:
                    values = self._read_io()
                    if values is None:
                        self._io_base = None
                    else:
                        moved = (values[0] - self._io_base[0], values[1] - self._io_base[1])
                        if moved != self._io_last:
                            self._io_last = moved
                            self._emit('progress.io', {'wchar': moved[0], 'rchar': moved[1], 't_ns': now - self._t0})
                return True
            except Exception as e:
                self._failed = True
                self._report(e)
                return False

    def _report(self, e):
        try:
            message = runner.describe('PROGRESS_SAMPLER', why=_why(e))
            self._emit('error', {'code': 'PROGRESS_SAMPLER', 'message': message, 'fatal': False, 'stop': False})
        except Exception:
            pass

    def _close(self):
        with self._lock:
            if self._dirfd is not None:
                try:
                    os.close(self._dirfd)
                except OSError:
                    pass
                self._dirfd = None

    def stop(self):
        """End the sampling (the client has exited); idempotent."""
        if self._stopped:
            return
        self._stopped = True
        self._halt.set()
        self._thread.join(2.0)
        try:
            self.sample()
        finally:
            self._close()


def install(gateway, interval_ms, proc_root='/proc'):
    """Set gateway.progress to a sampler factory (EVENTS.md section 4); interval_ms 0 removes it.

    The factory takes the interrupt baseline and the time origin from the client.spawned event of the same PID, which
    the gateway emits right before it calls the factory, so the markers count exactly like the irq.delta event."""
    if not interval_ms:
        gateway.progress = None
        return None
    if type(interval_ms) is not int or not 1 <= interval_ms <= MAX_MS:
        raise ValueError('interval_ms must be an integer from 0 to %d' % MAX_MS)
    spawned = {}

    def remember(envelope):
        if envelope.get('type') == 'client.spawned':
            data = envelope.get('data') or {}
            spawned.clear()
            spawned[data.get('pid')] = envelope

    gateway.events.add_listener(remember)

    def factory(pid, emit, cfg):
        envelope = spawned.get(pid)
        irq_before = t0_ns = None
        if envelope is not None:
            before = envelope['data'].get('irq_before')
            irq_before = before if isinstance(before, dict) else None
            t0_ns = envelope.get('t_mono_ns')
        path = cfg.get('proc_interrupts') or '/proc/interrupts'
        return Sampler(pid, emit, path, interval_ms, irq_before, t0_ns, proc_root).stop

    gateway.progress = factory
    return factory
