"""Partition measurement campaign on the KV260 (V8b loaded for the whole run, so every window shares one baseline).
  1. idle 30 s
  2. every ARM module alone for 8 s on one core (energy per module), and 3 modules on 4 threads
  3. PL: V8b frame scaling (k = 1, 2, 4, 8; driver phases), sustain k = 8 and k = 1 (energy per ciphertext)
  4. co-run: normalize + encode ("front") on 3 cores while the PL encrypts on core 0 (approximates the V8b client:
     two independent programs; the PL encrypts its own test data, the system rate is the slower of the two)
  5. all-ARM client: normalize + encode + encrypt on 4 threads
usage: python partition_measure.py <out dir>     (KV_PW in the environment)"""
import json, os, re, sys, threading, time
import serial
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kv_console import KV, stream, put_file

OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
pw = os.environ["KV_PW"]
HERE = os.path.dirname(os.path.abspath(__file__))
probe = serial.Serial(); probe.port = "COM5"; probe.baudrate = 115200; probe.timeout = 1
probe.dtr = False; probe.rts = False; probe.open()
lock = threading.Lock(); stop = threading.Event()
rows, esp = [], []
log = {"windows": [], "events": [], "bench": [], "pl_table": [], "phases": []}

def say(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)
def probe_send(s):
    with lock: probe.write((s + "\n").encode())
def alive(sec):
    t0 = time.time(); b = b""
    while time.time() - t0 < sec:
        b += probe.read(4096)
        if b.count(b",") > 8: return True
    return False
def logger():
    while not stop.is_set():
        line = probe.readline().decode(errors="ignore").strip()
        if not line: continue
        if line.startswith("# result"): esp.append({"t": time.time(), "line": line}); continue
        if line.startswith("#"): continue
        p = line.split(",")
        if len(p) == 5:
            try: rows.append([time.time()] + [float(x) for x in p])
            except ValueError: pass

if not alive(2.0):
    probe.rts = True; time.sleep(0.15); probe.rts = False
    if not alive(5.0): raise SystemExit("STOP probe on COM5 sends no data")
probe.reset_input_buffer(); threading.Thread(target=logger, daemon=True).start(); time.sleep(1)
kv = KV("COM6"); kv.login("ubuntu", pw)
def sudo(): kv.run(f"echo '{pw}' | sudo -S -v 2>/dev/null; true", 20, secret=pw)   # secret: masked in any error text
def must(cmd, marker, timeout):
    sudo(); rc, out = kv.run(cmd, timeout); ok = marker in out
    log["events"].append({"t": time.time(), "cmd": cmd, "ok": ok, "tail": out[-600:]})
    say(("OK  " if ok else "FAIL"), cmd[:60], "->", marker)
    if not ok: raise RuntimeError(f"{marker} missing:\n{out[-1500:]}")

def window(kind, name, cmd, pass_tag, row=0, timeout=200, records_per_unit=512):
    """Run one command that prints SUSTAIN_BEGIN ... *SUSTAIN_PASS, bracketing it with probe B/E."""
    probe_send(f"S{kind} {name}"[:26])
    info = {"kind": kind, "name": name, "cmd": cmd, "lines": []}
    def on_line(line, t):
        info["lines"].append(line)
        if line.startswith("SUSTAIN_BEGIN"):
            probe_send("B"); info["t_begin"] = t
        elif "SUSTAIN_PASS" in line or "SUSTAIN_FAIL" in line:
            info["t_end"] = t; kvs = dict(re.findall(r"(\w+)=([-\d.]+)", line)); info["parsed"] = kvs
            units = int(float(kvs.get("ciphertexts", kvs.get("units", 0))))
            probe_send(f"E{row},{name[:8]},{units * records_per_unit}")
    sudo(); info["rc"] = stream(kv, cmd, timeout, on_line)
    log["windows"].append(info)
    say(kind, name, "->", {k: info.get("parsed", {}).get(k) for k in ("seconds", "units", "ciphertexts", "ms_per_unit_per_thread", "mean_ioctl_ms")})
    return info

def idle(name, secs=30):
    probe_send(f"SIDLE {name}"[:26]); t0 = time.time(); time.sleep(secs)
    log["windows"].append({"kind": "idle", "name": name, "t_begin": t0, "t_end": time.time()}); say("idle", name)

try:
    kv.run("mkdir -p ~/partition", 10)
    if not put_file(kv, os.path.join(HERE, "module_bench.cpp"), "~/partition/module_bench.cpp"):
        raise SystemExit("STOP upload of module_bench.cpp failed")
    B = "~/EdgeFHE_repo/v8b_rev5/software/client/vendor/frozen_sdk/src/client/baseline"
    rc, out = kv.run(f"cd {B} && g++-11 -O3 -mcpu=cortex-a53 -std=c++17 -I. ~/partition/module_bench.cpp core.cpp -o ~/partition/module_bench -pthread 2>&1 | grep -c error", 600)
    say("compile errors:", out.strip().splitlines()[-1])
    if out.strip().splitlines()[-1] != "0":
        raise SystemExit("STOP module_bench did not compile:\n" + out[-800:])
    rc, out = kv.run("cd ~/partition && taskset -c 2 ./module_bench bench 40", 900)
    log["bench_raw"] = out     # kept verbatim, so every bench value can be traced
    log["bench"] = [json.loads(s) for s in re.findall(r'\{"module":[^{}]*\}', out)]; say("bench modules:", len(log["bench"]))
    # ---- V8b loaded for everything below ----
    probe_send("SLOADING V8b")
    must("cd ~/edgefhe-v8b-r5 && bash scripts/02_load_pl.sh 2>&1 | tail -n 3", "V8B_R5_PL_LOADED_AFI_PROVIDER_APPLIED", 180)
    must("cd ~/edgefhe-v8b-r5 && bash scripts/04_insmod.sh 2>&1 | tail -n 3", "V8B_PROXY_READY", 60)
    idle("V8b loaded", 30)
    MB = "cd ~/partition && taskset -c {cpus} ./module_bench loop {m} {s} {t}"
    for m in ["normalize", "encode", "front", "rns_message", "prng_chacha20", "sample_cdt63", "sample_ternary", "rns_small", "ntt_mask",
              "pointwise", "intt", "add", "encrypt_all", "decrypt", "decode", "client_ct"]:
        window("ARM1", m, MB.format(cpus=2, m=m, s=8, t=1), "MODULE_SUSTAIN_PASS")
    for m in ["front", "encrypt_all", "client_ct"]:
        window("ARM4", m, MB.format(cpus="0-3", m=m, s=10, t=4), "MODULE_SUSTAIN_PASS")
    # ---- PL ----
    sudo(); rc, out = kv.run("cd ~/edgefhe-v8b-r5 && sudo test/board_enc_check 20 2>&1 | tail -n 12", 300)
    log["pl_table"] = out.splitlines(); say("PL table:\n" + "\n".join(out.splitlines()[-9:]))
    for k in (8, 1):
        info = window("PL", f"sustain k{k}", f"cd ~/edgefhe-v8b-r5 && sudo taskset -c 0 test/board_enc_check sustain 15 {k}", "V8B_BOARD_SUSTAIN_PASS")
        log["phases"].append([l for l in info["lines"] if "driver phases" in l])
    # ---- co-run: normalize + encode ("front") on cores 1-3 + PL encryption driven from core 0 ----
    kv.run("rm -f /tmp/corun_enc.txt; (cd ~/partition && taskset -c 1-3 ./module_bench loop front 34 3 > /tmp/corun_enc.txt 2>&1 &) ; sleep 2", 20)
    window("CORUN", "pl+front3", "cd ~/edgefhe-v8b-r5 && sudo taskset -c 0 test/board_enc_check sustain 30 8", "V8B_BOARD_SUSTAIN_PASS")
    time.sleep(4); rc, out = kv.run("cat /tmp/corun_enc.txt", 20); log["corun_encode"] = out.splitlines(); say("corun encode:", out.strip().splitlines()[-1][:160])
    window("ARMSYS", "client_ct x4", MB.format(cpus="0-3", m="client_ct", s=30, t=4), "MODULE_SUSTAIN_PASS")
finally:
    try:
        probe_send("SRESTORING default app"); sudo()
        rc, out = kv.run("bash ~/edgefhe-v8b-r5/scripts/99_unload.sh >/dev/null 2>&1; sudo xmutil unloadapp >/dev/null 2>&1; "
                         "sudo xmutil loadapp k26-starter-kits 2>&1 | tail -n 1; sudo xmutil listapps | grep ' 0,'", 120)
        log["restore"] = out[-300:]; say("restore:", out.strip().splitlines()[-1] if out.strip() else "?")
        probe_send("SIDLE (default app)")
    finally:
        time.sleep(1); stop.set(); time.sleep(1.2); kv.logout(); probe.close()
        with open(os.path.join(OUT, "probe.csv"), "w") as f:
            f.write("host_s,t_ms,V,I_mA,P_mW,E_J\n")
            for r in rows: f.write(f"{r[0]:.3f},{int(r[1])},{r[2]:.4f},{r[3]:.2f},{r[4]:.1f},{r[5]:.3f}\n")
        log["esp"] = esp
        json.dump(log, open(os.path.join(OUT, "partition_runs.json"), "w"), indent=1, ensure_ascii=False)
        say("saved", OUT, "rows", len(rows))
