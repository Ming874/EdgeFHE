"""EdgeFHE demo cloud console: the whole-board energy probe on COM5 (ESP32-S3 + INA228 on the KV260 12 V input).

The probe prints one line 't_ms,V,I_mA,P_mW,E_J' every 0.1 s; lines starting with '#' are messages
(energy_probe/README.md, energy_probe/firmware/probe/probe.ino). This reader
- sets DTR and RTS to False before it opens the port: toggling either line can hold the ESP32 in reset
  (energy_probe/kv260/enc_compare_v8b_ofhe.py:12-13);
- never writes to the port: no B, E, r or S command, so it never resets the energy accumulator or the display that
  other measurements use, and it never toggles the reset lines to wake a silent probe;
- stamps every sample with the host clock time.time() on arrival (host_s) and appends it to RUN/probe.csv with the
  header host_s,t_ms,V,I_mA,P_mW,E_J (the file demo/evidence/report_html.py reads);
- computes energy windows from E_J differences (the INA228 hardware accumulator). E_J falls when the accumulator was
  reset (a '# run begin' or '# energy reset' message, or an ESP32 reboot); that single step is bridged with the
  trapezoid of P.

Session energy: delta_E = E(window) - P_idle x duration over [SYN - 2 s, FIN + 0.5 s], where P_idle is the median
power of the samples outside every session window. At 10 Hz on about 8 W of idle draw, one form is close to the noise,
so the console always labels the value with '≈'.

pyserial is imported only when a serial port is opened; replays and tests need the standard library only.
Python 3.10 or later.
"""
import os
import statistics
import threading
import time

BAUD = 115200
HEADER = "host_s,t_ms,V,I_mA,P_mW,E_J"
PRE_S, POST_S = 2.0, 0.5             # session window [SYN - 2 s, FIN + 0.5 s]
RESET_TOLERANCE_J = 0.005            # E_J printed with 3 decimals
MAX_STEP_S = 1.0                      # a longer silence inside a window marks it as not covered
REOPEN_S = 5.0
SILENT_S = 3.0
RESET_MESSAGES = ("# run begin", "# energy reset")


def parse_sample(line):
    """'t_ms,V,I_mA,P_mW,E_J' -> (t_ms, V, I_mA, P_mW, E_J), or None for messages and broken lines."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(",")
    if len(parts) != 5:
        return None
    try:
        t_ms = int(float(parts[0]))
        v, i_ma, p_mw, e_j = (float(x) for x in parts[1:])
    except ValueError:
        return None
    return t_ms, v, i_ma, p_mw, e_j


def format_row(host_s, sample):
    t_ms, v, i_ma, p_mw, e_j = sample
    return "%.3f,%d,%.4f,%.2f,%.1f,%.3f" % (host_s, t_ms, v, i_ma, p_mw, e_j)


def load_csv(path, shift=0.0):
    """Samples (host_s, t_ms, V, I_mA, P_mW, E_J) of a probe.csv, sorted by host_s; shift moves host_s (fixtures)."""
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        head = f.readline().strip().split(",")
        try:
            idx = [head.index(n) for n in ("host_s", "t_ms", "V", "I_mA", "P_mW", "E_J")]
        except ValueError:
            raise ValueError("PROBE_CSV_HEADER %s" % ",".join(head))
        for line in f:
            p = line.strip().split(",")
            if len(p) < len(head):
                continue
            try:
                rows.append((float(p[idx[0]]) + shift, int(float(p[idx[1]])), float(p[idx[2]]), float(p[idx[3]]),
                             float(p[idx[4]]), float(p[idx[5]])))
            except ValueError:
                continue
    rows.sort(key=lambda r: r[0])
    return rows


# ------------------------------------------------------------------ energy windows
def _step_energy(a, b):
    """Energy in J between consecutive samples a and b; (joules, reset)."""
    de = b[5] - a[5]
    if de >= -RESET_TOLERANCE_J:
        return max(de, 0.0), False
    return (a[4] + b[4]) / 2.0 * (b[0] - a[0]) / 1000.0, True


def energy_between(samples, t0, t1):
    """Energy over [t0, t1] from E_J differences, with linear interpolation at the edges.
    Returns dict(joules, n, resets, covered, max_gap_s)."""
    if t1 <= t0 or len(samples) < 2:
        return dict(joules=None, n=0, resets=0, covered=False, max_gap_s=None)
    e, resets, n, max_gap = 0.0, 0, 0, 0.0
    first, last = None, None
    for a, b in zip(samples, samples[1:]):
        if b[0] <= t0 or a[0] >= t1:
            continue
        dt = b[0] - a[0]
        if dt <= 0:
            continue
        lo, hi = max(a[0], t0), min(b[0], t1)
        step, reset = _step_energy(a, b)
        e += step * (hi - lo) / dt
        resets += 1 if reset else 0
        max_gap = max(max_gap, dt)
        n += 1
        first = a[0] if first is None else first
        last = b[0]
    covered = first is not None and first <= t0 + 0.15 and last >= t1 - 0.15 and max_gap <= MAX_STEP_S
    return dict(joules=e if n else None, n=n, resets=resets, covered=covered,
                max_gap_s=round(max_gap, 3) if n else None)


def _inside(t, windows):
    for a, b in windows:
        if a <= t <= b:
            return True
    return False


def idle_power_mw(samples, windows, t_lo=None, t_hi=None, minimum=20):
    """Median P (mW) of the samples outside every window, within [t_lo, t_hi] when given; None with fewer samples
    than `minimum`."""
    vals = [s[4] for s in samples if (t_lo is None or s[0] >= t_lo) and (t_hi is None or s[0] <= t_hi)
            and not _inside(s[0], windows)]
    if len(vals) < minimum:
        return None
    return statistics.median(vals)


def session_window(t_syn, t_end):
    return (t_syn - PRE_S, t_end + POST_S)


def session_energy(samples, t_syn, t_end, idle_mw):
    """delta_E ≈ over [SYN - 2 s, FIN + 0.5 s] minus the idle median. Values in J; approx is always True."""
    a, b = session_window(t_syn, t_end)
    e = energy_between(samples, a, b)
    out = dict(window=[round(a, 3), round(b, 3)], duration_s=round(b - a, 3), joules=e["joules"], n=e["n"],
               resets=e["resets"], covered=e["covered"], idle_mw=idle_mw, approx=True, delta_j=None, idle_j=None)
    if e["joules"] is not None and idle_mw is not None:
        out["idle_j"] = idle_mw / 1000.0 * (b - a)
        out["delta_j"] = e["joules"] - out["idle_j"]
    return out


def rate_hz(samples, now=None, span=10.0):
    if not samples:
        return 0.0
    end = now if now is not None else samples[-1][0]
    n = sum(1 for s in samples if end - span <= s[0] <= end)
    return n / span


# ------------------------------------------------------------------ serial reader
def open_serial(port, baud=BAUD):
    """pyserial port with DTR and RTS False before open (the ESP32 reset lines stay untouched)."""
    import serial                                      # pyserial, only on the cloud PC
    s = serial.Serial()
    s.port = port
    s.baudrate = baud
    s.timeout = 1
    s.dtr = False
    s.rts = False
    s.open()
    return s


class ProbeReader:
    """Reads the probe on a thread of its own. on_sample(host_s, sample) and on_status(state, detail) are called on
    that thread. The reader never writes to the port."""

    def __init__(self, port, csv_path=None, on_sample=None, on_status=None, baud=BAUD, opener=None):
        self.port = port
        self.baud = baud
        self.csv_path = csv_path
        self.on_sample = on_sample
        self.on_status = on_status
        self.opener = opener or open_serial
        self.samples = 0
        self.messages = 0
        self.resets_seen = 0
        self.state = "stopped"
        self.detail = ""
        self._stop = threading.Event()
        self._thread = None
        self._csv = None
        self._last_sample = None

    def _status(self, state, detail=""):
        if state == self.state and detail == self.detail:
            return
        self.state, self.detail = state, detail
        if self.on_status is not None:
            try:
                self.on_status(state, detail)
            except Exception:
                pass

    def start(self):
        if self.csv_path:
            new = not os.path.exists(self.csv_path)
            self._csv = open(self.csv_path, "a", encoding="utf-8", newline="\n")
            if new:
                self._csv.write(HEADER + "\n")
                self._csv.flush()
        self._thread = threading.Thread(target=self._run, name="efhe-probe", daemon=True)
        self._thread.start()

    def stop(self, timeout=3.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self._csv is not None:
            try:
                self._csv.close()
            except OSError:
                pass
            self._csv = None
        self._status("stopped", "")

    def _run(self):
        ser = None
        while not self._stop.is_set():
            if ser is None:
                try:
                    ser = self.opener(self.port, self.baud)
                    self._status("open", self.port)
                except Exception as e:                   # missing port, busy port, no pyserial
                    self._status("unavailable", "%s: %s" % (type(e).__name__, e))
                    self._stop.wait(REOPEN_S)
                    continue
            try:
                raw = ser.readline()
            except Exception as e:                       # unplugged while open
                self._status("lost", "%s: %s" % (type(e).__name__, e))
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                self._stop.wait(1.0)
                continue
            now = time.time()
            if not raw:
                if self._last_sample is None or now - self._last_sample > SILENT_S:
                    self._status("silent", self.port)
                continue
            line = raw.decode("ascii", "ignore").strip() if isinstance(raw, bytes) else str(raw).strip()
            if line.startswith("#"):
                self.messages += 1
                if line.startswith(RESET_MESSAGES):
                    self.resets_seen += 1
                continue
            sample = parse_sample(line)
            if sample is None:
                continue
            self.samples += 1
            self._last_sample = now
            self._status("streaming", self.port)
            if self._csv is not None:
                try:
                    self._csv.write(format_row(now, sample) + "\n")
                    self._csv.flush()
                except (OSError, ValueError):
                    pass
            if self.on_sample is not None:
                try:
                    self.on_sample(now, sample)
                except Exception:
                    pass
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
