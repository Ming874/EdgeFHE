"""Records per joule: V8b rev5 vs OpenFHE 1.5.1 (CPU), same definition (FPGA encrypt only, k=8, 30 s sustain, first/last frame exact).
Whole-board energy from the ESP32 + INA228 probe on the 12 V input (uncalibrated, K=1)."""
import json, os, re, sys, threading, time
import serial
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kv_console import KV, stream

OUT = sys.argv[1]; REPS = int(sys.argv[2]); SECS = 30
os.makedirs(OUT, exist_ok=True)
pw = os.environ["KV_PW"]
probe = serial.Serial(); probe.port = "COM5"; probe.baudrate = 115200; probe.timeout = 1
probe.dtr = False; probe.rts = False; probe.open()          # do not toggle the ESP32 reset lines on open
lock = threading.Lock(); stop = threading.Event()
rows, esp_results = [], []
log = {"events": [], "idle": [], "runs": [], "secs": SECS, "reps": REPS}

def say(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def probe_send(s):
    with lock:
        probe.write((s + "\n").encode())

def logger():
    while not stop.is_set():
        line = probe.readline().decode(errors="ignore").strip()
        if not line:
            continue
        if line.startswith("# result"):
            esp_results.append({"t": time.time(), "line": line}); continue
        if line.startswith("#"):
            continue
        p = line.split(",")
        if len(p) == 5:
            try:
                rows.append([time.time()] + [float(x) for x in p])
            except ValueError:
                pass

def probe_alive(sec):
    t0 = time.time(); b = b""
    while time.time() - t0 < sec:
        b += probe.read(4096)
        if b.count(b",") > 8: return True
    return False
if not probe_alive(2.0):
    probe.rts = True; time.sleep(0.15); probe.rts = False   # one normal reset (IO0 stays high)
    if not probe_alive(5.0): raise SystemExit("STOP probe on COM5 sends no data")
probe.reset_input_buffer()
threading.Thread(target=logger, daemon=True).start()
time.sleep(1)
kv = KV("COM6"); kv.login("ubuntu", pw)

def sudo():
    kv.run(f"echo '{pw}' | sudo -S -v 2>/dev/null; true", 20, secret=pw)

def stat():
    rc, out = kv.run("head -1 /proc/stat", 10)
    f = [int(x) for x in re.findall(r"cpu\s+((?:\d+\s+){6,}\d+)", out)[-1].split()]
    return sum(f), f[3] + f[4]

def util(a, b):
    return 1 - (b[1] - a[1]) / (b[0] - a[0])

def must(cmd, marker, timeout):
    sudo()
    rc, out = kv.run(cmd, timeout)
    ok = marker in out
    log["events"].append({"t": time.time(), "cmd": cmd, "rc": rc, "ok": ok, "tail": out[-800:]})
    say(("OK  " if ok else "FAIL"), cmd[:70], "->", marker)
    if not ok:
        raise RuntimeError(f"{marker} missing:\n{out[-1500:]}")

def idle(label, state):  # label = baseline name used by the runs that follow
    probe_send(f"S{state}")
    a = stat(); t0 = time.time(); time.sleep(SECS); t1 = time.time(); b = stat()
    log["idle"].append({"version": label, "start": t0, "end": t1, "cpu": util(a, b)})
    say(f"idle {label}: cpu {util(a, b) * 100:.1f}%")

def sustain(label, row, rep, cmd, pass_tag, baseline=None, kind="FPGA"):
    probe_send(f"SRUN {label} ({kind})")
    info = {"version": label, "rep": rep, "lines": [], "baseline": baseline or label}
    def on_line(line, t):
        info["lines"].append(line)
        if line.startswith("SUSTAIN_BEGIN"):
            probe_send("B"); info["t_begin"] = t
        elif "SUSTAIN_PASS" in line or "SUSTAIN_FAIL" in line:
            info["t_end"] = t
            kvs = dict(re.findall(r"(\w+)=([-\d.]+)", line))
            info["parsed"] = kvs; info["passed"] = pass_tag in line
            info["records"] = int(kvs["ciphertexts"]) * 2048 // 4
            probe_send(f"E{row},{label},{info['records']}")
    sudo()
    # CPU use sampled on the board 5 s to 25 s after start (inside the 30 s loop)
    bg = "(sleep 5; head -1 /proc/stat > /tmp/cpu_a; sleep 20; head -1 /proc/stat > /tmp/cpu_b) & "
    info["rc"] = stream(kv, bg + cmd, SECS + 120, on_line)
    rc, out = kv.run("sleep 1; cat /tmp/cpu_a /tmp/cpu_b", 15)
    a, b = [[int(x) for x in g.split()] for g in re.findall(r"cpu\s+((?:\d+\s+){6,}\d+)", out)][-2:]
    info["cpu"] = util((sum(a), a[3] + a[4]), (sum(b), b[3] + b[4]))
    log["runs"].append(info)
    say(f"{label} rep {rep}: passed={info.get('passed')} records={info.get('records')} cpu {info['cpu'] * 100:.1f}%")
    if not info.get("passed"):
        raise RuntimeError(f"{label} run failed:\n" + "\n".join(info["lines"][-15:]))

try:
    rc, apps = kv.run("sudo -n true 2>/dev/null; echo '" + pw + "' | sudo -S -v 2>/dev/null; sudo xmutil listapps 2>&1 | grep ' 0,'", 30, secret=pw)
    say("active app before:", apps.strip().splitlines()[-1] if apps.strip() else "?")
    # ---- V8b rev5 ----
    probe_send("SLOADING V8b")
    must("cd ~/edgefhe-v8b-r5 && bash scripts/02_load_pl.sh 2>&1 | tail -n 4", "V8B_R5_PL_LOADED_AFI_PROVIDER_APPLIED", 180)
    must("cd ~/edgefhe-v8b-r5 && bash scripts/04_insmod.sh 2>&1 | tail -n 4", "V8B_PROXY_READY", 60)
    idle("V8b", "IDLE (V8b loaded)")
    for r in range(1, REPS + 1):
        sustain("V8b", 0, r, "cd ~/edgefhe-v8b-r5 && sudo test/board_enc_check sustain 30 8", "V8B_BOARD_SUSTAIN_PASS")
    must("cd ~/edgefhe-v8b-r5 && bash scripts/99_unload.sh 2>&1 | tail -n 3", "V8B_R5_UNLOADED", 60)
    # ---- OpenFHE 1.5.1 on the ARM cores, PL empty ----
    idle("OpenFHE", "IDLE (PL empty)")
    B = "cd ~/openfhe_bench && OMP_NUM_THREADS={t} ./build/openfhe_sustain 30 8 {t}"
    for r in range(1, REPS + 1):
        sustain("OpenFHE1", 1, r, B.format(t=1), "OPENFHE_SUSTAIN_PASS", baseline="OpenFHE", kind="CPU x1")
    for r in range(1, REPS + 1):
        sustain("OpenFHE4", 2, r, B.format(t=4), "OPENFHE_SUSTAIN_PASS", baseline="OpenFHE", kind="CPU x4")
finally:
    try:
        probe_send("SRESTORING default app")
        sudo()
        rc, out = kv.run("bash ~/edgefhe-v8b-r5/scripts/99_unload.sh >/dev/null 2>&1; "
                         "lsmod | grep -q '^u_dma_buf' && sudo rmmod u_dma_buf; sudo xmutil unloadapp >/dev/null 2>&1; "
                         "sudo xmutil loadapp k26-starter-kits 2>&1 | tail -n 1; sudo xmutil listapps | grep ' 0,'", 120)
        log["restore"] = out[-500:]; say("restore:", out.strip().splitlines()[-1] if out.strip() else "?")
        probe_send("SIDLE (default app)")
    finally:
        time.sleep(1); stop.set(); time.sleep(1.2)
        kv.logout(); probe.close()
        with open(os.path.join(OUT, "probe.csv"), "w") as f:
            f.write("host_s,t_ms,V,I_mA,P_mW,E_J\n")
            for r in rows:
                f.write(f"{r[0]:.3f},{int(r[1])},{r[2]:.4f},{r[3]:.2f},{r[4]:.1f},{r[5]:.3f}\n")
        log["esp_results"] = esp_results
        json.dump(log, open(os.path.join(OUT, "runs.json"), "w"), indent=1, ensure_ascii=False)
        say("saved", OUT, "rows", len(rows))
