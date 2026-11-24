"""Stage demo of the whole-board energy probe (ESP32-S3 + INA228 on the KV260 12 V input).
One command runs several short modes. The probe screen shows each mode's records per joule; the PC shows the live
power, a result table and a chart.
  idle (V8b loaded) -> V8b encrypting on the PL -> PL emptied, idle -> OpenFHE 4 threads -> OpenFHE 1 thread
  -> the board's default app is restored
usage (PowerShell, in energy_probe/kv260):
  python stage_demo.py --check          before the talk: probe, console login, sudo and programs (loads nothing)
  python stage_demo.py                  on stage: about 3 minutes with the defaults
  python stage_demo.py --secs 10 --modes v8b,ofhe4
The KV260 password comes from the KV_PW environment variable, or from a hidden prompt when KV_PW is not set;
it is never printed or written to a file.
Ctrl+C never interrupts a program that is using the FPGA: the demo stops after the current step and restores the board.
Records per joule here come from short runs; the reported results are the 10-run measurements."""
import argparse, getpass, json, os, re, signal, statistics as st, sys, threading, time, unicodedata
import serial
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kv_console import KV, stream

V8B_DIR = "~/edgefhe-v8b-r5"
OFHE = "cd ~/openfhe_bench && OMP_NUM_THREADS={t} ./build/openfhe_sustain {s} 8 {t}"
# key: probe label (<= 8 characters), screen row, name on the PC, board command, pass tag, needs V8b loaded, chart colour
MODES = {
    "v8b":   ("V8b", 0, "V8b（FPGA）", f"cd {V8B_DIR} && sudo test/board_enc_check sustain {{s}} 8", "V8B_BOARD_SUSTAIN_PASS", True, "#2a78d6"),
    "ofhe4": ("OpenFHE4", 2, "OpenFHE 4 執行緒", OFHE.replace("{t}", "4"), "OPENFHE_SUSTAIN_PASS", False, "#1baf7a"),
    "ofhe1": ("OpenFHE1", 1, "OpenFHE 單執行緒", OFHE.replace("{t}", "1"), "OPENFHE_SUSTAIN_PASS", False, "#eb6834"),
}
RESTORE = (f"bash {V8B_DIR}/scripts/99_unload.sh >/dev/null 2>&1; lsmod | grep -q '^u_dma_buf' && sudo rmmod u_dma_buf; "
           "sudo xmutil unloadapp >/dev/null 2>&1; sudo xmutil loadapp k26-starter-kits 2>&1 | tail -n 1; sudo xmutil listapps | grep ' 0,'")

ap = argparse.ArgumentParser(description="EdgeFHE energy-probe stage demo")
ap.add_argument("--check", action="store_true", help="check probe, console, sudo and programs; load nothing")
ap.add_argument("--modes", default="v8b,ofhe4,ofhe1", help="comma list of " + ", ".join(MODES))
ap.add_argument("--secs", type=int, default=15, help="seconds of encryption per mode (default 15)")
ap.add_argument("--idle", type=int, default=8, help="seconds of idle measurement after each state change (default 8)")
ap.add_argument("--probe", default="COM5"); ap.add_argument("--console", default="COM6")
ap.add_argument("--out", default=None, help="output folder (default stage_demo_<time> in the current folder)")
ap.add_argument("--no-open", action="store_true", help="do not open the chart when finished")
ap.add_argument("--keep", action="store_true", help="leave the board as it is at the end (no default-app restore)")
args = ap.parse_args()
modes = [m.strip() for m in args.modes.split(",") if m.strip()]
bad = [m for m in modes if m not in MODES]
if bad or not modes:
    raise SystemExit(f"未知的模式：{bad}；可用：{', '.join(MODES)}")
if not 3 <= args.secs <= 120 or not 2 <= args.idle <= 60:
    raise SystemExit("--secs 須在 3–120 秒、--idle 須在 2–60 秒之間")

# ---------------------------------------------------------------- terminal output (one live line + messages)
OUT_LOCK = threading.RLock()
live = {"phase": None, "width": 0}

def dwidth(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)

def pad(s, w, right=False):
    gap = " " * max(0, w - dwidth(s))
    return gap + s if right else s + gap

def clear_live():
    if live["width"]:
        sys.stdout.write("\r" + " " * live["width"] + "\r"); live["width"] = 0

def say(*a):
    with OUT_LOCK:
        clear_live(); print(time.strftime("%H:%M:%S"), *a, flush=True)

def phase(text, dur=None, base=None):
    live["phase"] = {"text": text, "t0": time.time(), "dur": dur, "base": base, "clock": dur is None}

def end_phase():
    live["phase"] = None
    with OUT_LOCK: clear_live(); sys.stdout.flush()

# ---------------------------------------------------------------- probe
rows, results = [], []          # rows: host_s, t_ms, V, I_mA, P_mW, E_J; results: "# result" lines from the ESP32
plock, stop = threading.Lock(), threading.Event()
probe = serial.Serial(); probe.port = args.probe; probe.baudrate = 115200; probe.timeout = 1
probe.dtr = False; probe.rts = False            # never toggle the ESP32 reset lines on open
try:
    probe.open()
except serial.SerialException as e:
    raise SystemExit(f"無法開啟探針 {args.probe}：請關閉 Arduino 序列埠監看視窗或其他使用 {args.probe} 的程式。（{e}）")

def probe_send(s):
    with plock: probe.write((s + "\n").encode())

def probe_alive(sec):
    t0 = time.time(); b = b""
    while time.time() - t0 < sec:
        b += probe.read(4096)
        if b.count(b",") > 8: return True
    return False

def logger():
    while not stop.is_set():
        try:
            line = probe.readline().decode(errors="ignore").strip()
        except serial.SerialException:
            return
        if not line: continue
        if line.startswith("# result"):
            results.append({"t": time.time(), "line": line}); continue
        if line.startswith("#"): continue
        p = line.split(",")
        if len(p) == 5:
            try: rows.append([time.time()] + [float(x) for x in p])
            except ValueError: pass

def recent_W(sec=0.5):
    now = time.time(); v = [r[4] for r in rows[-20:] if r[0] > now - sec - 0.15]
    return st.mean(v) / 1000 if v else None

def mean_W(a, b):
    v = [r[4] for r in rows if a <= r[0] <= b]
    return (st.mean(v) / 1000, len(v)) if v else (float("nan"), 0)

def ticker():
    while not stop.is_set():
        time.sleep(0.5)
        ph = live["phase"]
        if not ph: continue
        el = time.time() - ph["t0"]; P = recent_W()
        s = "  " + ph["text"]
        if ph["dur"]:
            k = min(20, int(20 * el / ph["dur"]))
            s += f"  {'█' * k}{'░' * (20 - k)} {min(el, ph['dur']):4.0f}/{ph['dur']} s"
        elif ph["clock"]:
            s += f"  {el:4.0f} s"
        if P is not None:
            s += f"   整板 {P:5.2f} W"
            if ph["base"]: s += f"（比待機多 {P - ph['base']:+.2f} W）"
        with OUT_LOCK:
            w = dwidth(s); sys.stdout.write("\r" + s + " " * max(0, live["width"] - w)); sys.stdout.flush()
            live["width"] = max(w, live["width"])

# ---------------------------------------------------------------- Ctrl+C: finish the current step, then restore
ABORT = threading.Event()
class Abort(Exception): pass
def on_sigint(sig, frame):
    if not ABORT.is_set():
        ABORT.set(); say("收到 Ctrl+C：目前這一段結束後就停止並還原開發板（不會中斷正在使用 FPGA 的程式）。")
def checkpoint():
    if ABORT.is_set(): raise Abort()
signal.signal(signal.SIGINT, on_sigint)

if not probe_alive(2.0):
    probe.rts = True; time.sleep(0.15); probe.rts = False     # one normal reset (IO0 stays high)
    if not probe_alive(5.0):
        raise SystemExit(f"探針 {args.probe} 沒有輸出資料：請確認 USB 線與 ESP32 電源。")
probe.reset_input_buffer()
threading.Thread(target=logger, daemon=True).start()
threading.Thread(target=ticker, daemon=True).start()
time.sleep(1.2)

pw = os.environ.get("KV_PW") or getpass.getpass("KV260 密碼（輸入時不會顯示，也不會儲存）：")
try:
    kv = KV(args.console)
except serial.SerialException as e:
    raise SystemExit(f"無法開啟 KV260 主控台 {args.console}：請關閉其他使用 {args.console} 的終端機程式。（{e}）")
try:
    kv.login("ubuntu", pw)
except RuntimeError as e:
    raise SystemExit("KV260 登入失敗：" + str(e).replace(pw, "***"))

def sudo():
    rc, out = kv.run(f"echo '{pw}' | sudo -S -v 2>/dev/null; sudo -n true && echo SUDO_OK", 20, secret=pw)
    return "SUDO_OK" in out

def must(cmd, marker, timeout):
    if not sudo(): raise RuntimeError("sudo 驗證失敗")
    rc, out = kv.run(cmd, timeout)
    if marker not in out:
        raise RuntimeError(f"{marker} 未出現：\n{out[-800:]}")
    return out

def active_app():
    rc, out = kv.run("sudo xmutil listapps 2>&1 | grep ' 0,'", 30)
    return out.strip().splitlines()[-1].split()[0] if out.strip() else "?"

# ---------------------------------------------------------------- --check
if args.check:
    ok = True
    P = recent_W(1.0); v = rows[-1] if rows else None
    say(f"探針 {args.probe}：{'正常' if v else '無資料'}" + (f"，{v[2]:.3f} V、{v[3]:.0f} mA、{P:.2f} W" if v else ""))
    s_ok = sudo(); ok &= s_ok
    say("KV260 主控台登入：正常；sudo：" + ("正常" if s_ok else "失敗"))
    rc, out = kv.run(f"for f in {V8B_DIR}/scripts/02_load_pl.sh {V8B_DIR}/scripts/04_insmod.sh {V8B_DIR}/scripts/99_unload.sh "
                     f"{V8B_DIR}/test/board_enc_check ~/openfhe_bench/build/openfhe_sustain; do test -e $f || echo MISSING $f; done; echo FILES_DONE", 20)
    missing = [l for l in out.splitlines() if l.startswith("MISSING")]
    ok &= not missing
    say("板上程式：" + ("齊全" if not missing else "缺少 " + "、".join(m.split()[1] for m in missing)))
    say("目前載入的應用：" + active_app())
    n_state = len({MODES[m][5] for m in modes})
    est = n_state * (20 + args.idle) + len(modes) * (args.secs + 10) + 30
    kv.logout(); stop.set(); time.sleep(1.2); probe.close()
    say(("準備完成。" if ok else "有項目未通過，請先處理。") + f"上台執行 python stage_demo.py，模式 {','.join(modes)}，預計約 {est // 60} 分 {est % 60} 秒。")
    sys.exit(0 if ok else 1)

# ---------------------------------------------------------------- the demo
ts = time.strftime("%Y%m%d_%H%M%S")
OUT = args.out or os.path.join(os.getcwd(), f"stage_demo_{ts}")
os.makedirs(OUT, exist_ok=True)
T0 = time.time()
log = {"started": ts, "modes": modes, "secs": args.secs, "idle_s": args.idle, "windows": [], "runs": []}
state = "default"                               # default | v8b | empty
base = {}                                       # state -> idle power (W)

def idle_window(st_name, label):
    probe_send(f"SIDLE {label}"[:26]); phase(f"待機量測（{label}）", args.idle)
    t0 = time.time()
    while time.time() - t0 < args.idle:
        time.sleep(0.2); checkpoint()
    t1 = time.time(); end_phase()
    P, n = mean_W(t0 + 1.5, t1 - 0.3); base[st_name] = P
    log["windows"].append({"kind": "idle", "state": st_name, "t0": t0, "t1": t1, "P_W": P})
    say(f"待機（{label}）：整板 {P:.2f} W")

def ensure_state(need_v8b):
    global state
    want = "v8b" if need_v8b else "empty"
    if state == want: return
    checkpoint(); t0 = time.time()
    if want == "v8b":
        probe_send("SLOADING V8b"); phase("載入 V8b 硬體與驅動")
        must(f"cd {V8B_DIR} && bash scripts/02_load_pl.sh 2>&1 | tail -n 4", "V8B_R5_PL_LOADED_AFI_PROVIDER_APPLIED", 180)
        must(f"cd {V8B_DIR} && bash scripts/04_insmod.sh 2>&1 | tail -n 4", "V8B_PROXY_READY", 60)
        state = "v8b"; end_phase(); say(f"V8b 已載入（{time.time() - t0:.0f} 秒）")
        log["windows"].append({"kind": "load", "state": "v8b", "t0": t0, "t1": time.time()})
        idle_window("v8b", "V8b loaded")
    else:
        probe_send("SPL EMPTY"); phase("清空 FPGA（OpenFHE 只用 ARM）")
        if state == "v8b":
            must(f"cd {V8B_DIR} && bash scripts/99_unload.sh 2>&1 | tail -n 3", "V8B_R5_UNLOADED", 60)
        else:
            must("sudo xmutil unloadapp 2>&1 | tail -n 1; echo PL_EMPTY_DONE", "PL_EMPTY_DONE", 60)
        state = "empty"; end_phase(); say(f"FPGA 已清空（{time.time() - t0:.0f} 秒）")
        log["windows"].append({"kind": "unload", "state": "empty", "t0": t0, "t1": time.time()})
        idle_window("empty", "PL empty")

def run_mode(key):
    label, row, name, cmd, tag, need_v8b, _ = MODES[key]
    ensure_state(need_v8b); checkpoint()
    probe_send(f"SRUN {label}"[:26]); phase(f"{name}：準備中（產生金鑰）")
    info = {"mode": key, "lines": []}
    def on_line(line, t):
        info["lines"].append(line)
        if line.startswith("SUSTAIN_BEGIN"):
            probe_send("B"); info["t_begin"] = t
            phase(f"{name}：加密中", args.secs, base.get(state))
        elif "SUSTAIN_PASS" in line or "SUSTAIN_FAIL" in line:
            info["t_end"] = t
            kvs = dict(re.findall(r"(\w+)=([-\d.]+)", line)); info["parsed"] = kvs; info["passed"] = tag in line
            info["records"] = int(float(kvs.get("ciphertexts", 0))) * 512      # 4 features per record: 2048 / 4
            n_before = len(results); probe_send(f"E{row},{label},{info['records']}"); info["n_results"] = n_before
    if not sudo(): raise RuntimeError("sudo 驗證失敗")
    info["rc"] = stream(kv, cmd.format(s=args.secs), args.secs + 150, on_line)
    end_phase()
    if not info.get("passed"):
        raise RuntimeError(f"{name} 沒有通過：\n" + "\n".join(info["lines"][-12:]))
    t_wait = time.time()                                    # the probe answers E with one "# result" line
    while len(results) <= info["n_results"] and time.time() - t_wait < 3: time.sleep(0.05)
    m = re.search(r"E_J=([\d.]+) rec_per_J=([\d.]+)", results[-1]["line"]) if len(results) > info["n_results"] else None
    sec = float(info["parsed"].get("seconds", args.secs))
    P, n = mean_W(info["t_begin"] + 1.0, info["t_end"] - 0.5)
    info.update({"name": name, "seconds": sec, "P_W": P, "P_idle_W": base.get(state), "samples": n,
                 "rec_s": info["records"] / sec, "rpj_pc": info["records"] / (P * sec),
                 "rpj_probe": float(m.group(2)) if m else None, "E_probe_J": float(m.group(1)) if m else None})
    log["runs"].append(info)
    d = (info["rpj_probe"] / info["rpj_pc"] - 1) * 100 if m else float("nan")
    say(f"{name}：每焦耳 {info['rpj_probe'] or float('nan'):,.0f} 筆（探針螢幕）；電腦以 0.1 秒功率驗算 {info['rpj_pc']:,.0f} 筆，差 {d:+.2f}%")

def wan(v):
    return f"{v / 1e4:,.0f} 萬" if v >= 1e6 else f"{v / 1e4:,.1f} 萬"

def summary():
    R = [r for r in log["runs"] if r.get("rpj_probe")]
    if not R: return
    print()
    print(f"====== 結果：每種設定各 {args.secs} 秒（示範用短時間量測；正式結果為每種設定 10 輪、每輪 30 秒）======")
    hdr = [("設定", 18, False), ("整板功率", 10, True), ("比待機多", 10, True), ("每秒筆數", 11, True), ("每焦耳筆數", 12, True), ("電腦驗算差", 12, True)]
    print("".join(pad(h, w, r) for h, w, r in hdr))
    for r in R:
        inc = r["P_W"] - r["P_idle_W"] if r["P_idle_W"] else float("nan")
        cells = [r["name"], f"{r['P_W']:.2f} W", f"{inc:+.2f} W", wan(r["rec_s"]), f"{r['rpj_probe']:,.0f}",
                 f"{(r['rpj_probe'] / r['rpj_pc'] - 1) * 100:+.2f}%"]
        print("".join(pad(c, w, rt) for c, (_, w, rt) in zip(cells, hdr)))
    top = max(r["rpj_probe"] for r in R)
    print()
    for r in R:
        k = max(1, round(40 * r["rpj_probe"] / top))
        print("  " + pad(r["name"], 18) + "█" * k + f" {r['rpj_probe']:,.0f}")
    by = {r["mode"]: r for r in R}
    if "v8b" in by:
        for o in ("ofhe4", "ofhe1"):
            if o in by:
                print(f"\n  V8b 的每焦耳筆數為 {by[o]['name']}的 {by['v8b']['rpj_probe'] / by[o]['rpj_probe']:.1f} 倍")
    print(flush=True)

def chart():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker
    from matplotlib import font_manager
    for f in ("Microsoft JhengHei", "Noto Sans TC", "Microsoft YaHei"):
        if any(f in x.name for x in font_manager.fontManager.ttflist): plt.rcParams["font.family"] = f; break
    SURF, INK, INK2, GRID, IDLE = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", "#9a9893"
    plt.rcParams.update({"figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2, "text.color": INK, "font.size": 12,
                         "axes.spines.top": False, "axes.spines.right": False, "axes.unicode_minus": False})
    R = [r for r in log["runs"] if r.get("rpj_probe")]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(13, 7.6), gridspec_kw={"height_ratios": [1.5, 1], "hspace": 0.55})
    a1.plot([r[0] - T0 for r in rows], [r[4] / 1000 for r in rows], lw=1.0, color=INK2, zorder=3)
    for w in log["windows"]:
        if w["kind"] == "idle":
            a1.axvspan(w["t0"] - T0, w["t1"] - T0, color=IDLE, alpha=0.22, lw=0)
            a1.text((w["t0"] + w["t1"]) / 2 - T0, 1.02, "待機", transform=a1.get_xaxis_transform(), ha="center", fontsize=10, color=INK2)
    for r in R:
        c = MODES[r["mode"]][6]
        a1.axvspan(r["t_begin"] - T0, r["t_end"] - T0, color=c, alpha=0.18, lw=0)
        a1.text((r["t_begin"] + r["t_end"]) / 2 - T0, 1.02, r["name"], transform=a1.get_xaxis_transform(), ha="center", fontsize=10.5, color=c, fontweight="bold")
    a1.set_xlabel("時間（秒）"); a1.set_ylabel("整板功率（W）")
    a1.set_title("探針現場量測：KV260 整板功率（12 V 輸入，每 0.1 秒一筆）", loc="left", fontsize=14, fontweight="bold", pad=24)
    a1.yaxis.grid(True, color=GRID); a1.set_axisbelow(True)
    names = [r["name"] for r in R][::-1]; vals = [r["rpj_probe"] for r in R][::-1]
    a2.barh(names, vals, color=[MODES[r["mode"]][6] for r in R][::-1], height=0.5)
    for i, v in enumerate(vals): a2.text(v + max(vals) * 0.01, i, f"{v:,.0f}", va="center", fontsize=12)
    a2.set_xlim(0, max(vals) * 1.15); a2.xaxis.grid(True, color=GRID); a2.set_axisbelow(True)
    a2.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: f"{x / 1000:.0f}K" if x else "0"))
    by = {r["mode"]: r for r in R}
    ttl = "每焦耳筆數（探針晶片累積能量計算）"
    if "v8b" in by and "ofhe4" in by:
        ttl += f"：V8b 為 OpenFHE 4 執行緒的 {by['v8b']['rpj_probe'] / by['ofhe4']['rpj_probe']:.1f} 倍"
    a2.set_title(ttl, loc="left", fontsize=14, fontweight="bold")
    fig.text(0.01, 0.005, f"示範用短時間量測：每種設定 {args.secs} 秒；正式結果為每種設定 10 輪、每輪 30 秒。", fontsize=10, color=INK2)
    path = os.path.join(OUT, "demo.png"); fig.savefig(path, dpi=130, bbox_inches="tight"); plt.close(fig)
    return path

err = None
try:
    say(f"EdgeFHE 探針示範開始：模式 {'、'.join(MODES[m][2] for m in modes)}，每種 {args.secs} 秒。按 Ctrl+C 可在目前這一段結束後停止。")
    for m in modes:                              # clear this demo's rows on the probe screen (0 until measured)
        probe_send(f"E{MODES[m][1]},{MODES[m][0]},0")
    time.sleep(0.3); results.clear()
    for m in modes:
        run_mode(m)
except Abort:
    say("示範已依要求停止。")
except Exception as e:
    err = e; end_phase(); say("示範中斷：" + str(e).replace(pw, "***"))
finally:
    end_phase()
    try:
        if state != "default" and not args.keep:
            probe_send("SRESTORING default app"); phase("還原開發板預設應用")
            sudo(); rc, out = kv.run(RESTORE, 150)
            end_phase(); say("已還原預設應用：" + (out.strip().splitlines()[-1].split()[0] if out.strip() else "?"))
        probe_send("SIDLE (demo done)")
    except Exception as e:
        end_phase(); say("還原時發生問題，請手動執行 xmutil loadapp k26-starter-kits：" + str(e).replace(pw, "***"))
    finally:
        time.sleep(1.0); stop.set(); time.sleep(1.2)
        try: kv.logout()
        except Exception: pass
        probe.close()
        with open(os.path.join(OUT, "probe.csv"), "w") as f:
            f.write("host_s,t_ms,V,I_mA,P_mW,E_J\n")
            for r in rows: f.write(f"{r[0]:.3f},{int(r[1])},{r[2]:.4f},{r[3]:.2f},{r[4]:.1f},{r[5]:.3f}\n")
        log["esp_results"] = results; log["elapsed_s"] = time.time() - T0
        json.dump(log, open(os.path.join(OUT, "demo.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
summary()
if any(r.get("rpj_probe") for r in log["runs"]):
    png = chart(); say(f"圖表與資料：{OUT}（總時間 {log['elapsed_s']:.0f} 秒）")
    if not args.no_open and hasattr(os, "startfile"): os.startfile(png)
sys.exit(1 if err else 0)
