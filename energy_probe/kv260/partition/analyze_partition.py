"""Partition analysis from partition_measure.py data (+ optional HLS stage estimates).
usage: python analyze_partition.py <data dir> [hls_stage_estimates.json]
writes <data dir>/partition_summary.json and <data dir>/figures/fig_p1..p4 *.png

Definitions (every figure and number in the deck and the report comes from here):
  ARM module time   : V8b CPU specification code (baseline/core.cpp, sampler_b.hpp), one core, ms per ciphertext
  ARM module energy : (mean board power while the module runs alone on one core - idle power) x module time
  PL path           : board_enc_check sustain k=8, wall time per ciphertext incl. the driver's copy/check/sync/wipe
  PL kernel         : dma_hls driver phase / 8 (DMA + HLS kernel only)
  co-run            : normalize + encode ("front") on cores 1-3 while core 0 drives the PL (two independent programs;
                      the PL encrypts its own test data); system rate = min(front rate, PL rate)
  all-ARM system    : normalize + encode + encrypt on 4 threads (client_ct x4)
"""
import json, os, re, statistics as st, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager, ticker
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

D = sys.argv[1]
HLS = json.load(open(sys.argv[2], encoding="utf-8")) if len(sys.argv) > 2 and os.path.exists(sys.argv[2]) else None
L = json.load(open(os.path.join(D, "partition_runs.json"), encoding="utf-8"))
rows = [list(map(float, l.split(","))) for l in open(os.path.join(D, "probe.csv")).read().splitlines()[1:]]
FIG = os.path.join(D, "figures"); os.makedirs(FIG, exist_ok=True)
REC_PER_CT = 512            # 2048 slots / 4 features
NORMALIZE_FALLBACK_MS = 0.011   # only for older campaigns whose bench output lacks a normalize line (not archived; 2026-11-23 single-core bench)

def mean_p(a, b):
    ps = [r[4] / 1000 for r in rows if a <= r[0] <= b]
    return (st.mean(ps), len(ps)) if ps else (float("nan"), 0)

W = L["windows"]
idle = [w for w in W if w["kind"] == "idle"][0]
P_idle, n_idle = mean_p(idle["t_begin"] + 2, idle["t_end"] - 1)
bench = {b["module"]: b["median_ms"] for b in L["bench"]}
normalize_src = "bench"
if "normalize" not in bench:
    bench["normalize"] = NORMALIZE_FALLBACK_MS; normalize_src = "earlier same-day bench, output not archived (0.011 ms)"

def win(kind, name):
    for w in W:
        if w["kind"] == kind and w["name"] == name:
            return w
    return None

def run_stats(w):
    sec = float(w["parsed"]["seconds"]); a = w["t_begin"]
    P, n = mean_p(a + 0.5, a + sec - 0.3)
    return sec, P, n

# ---- ARM modules on one core ----
arm = {}
for w in W:
    if w["kind"] != "ARM1": continue
    sec, P, n = run_stats(w)
    t_unit = float(w["parsed"]["ms_per_unit_per_thread"])
    arm[w["name"]] = {"ms": t_unit, "bench_ms": bench.get(w["name"]), "P": P, "dP": P - P_idle, "mJ_incr": (P - P_idle) * t_unit, "n": n}
arm4 = {}
for w in W:
    if w["kind"] != "ARM4": continue
    sec, P, n = run_stats(w)
    t_unit = float(w["parsed"]["ms_per_unit_per_thread"])
    rate = 4 * 1000 / t_unit
    arm4[w["name"]] = {"ms_per_thread": t_unit, "units_per_s": rate, "rec_per_s": rate * REC_PER_CT, "P": P, "dP": P - P_idle,
                       "mJ_gross": P / rate * 1000, "mJ_incr": (P - P_idle) / rate * 1000, "rec_per_J": rate * REC_PER_CT / P}

# ---- PL ----
pl = {}
for name in ("sustain k8", "sustain k1"):
    w = win("PL", name)
    if not w: continue
    sec, P, n = run_stats(w)
    cts = float(w["parsed"]["ciphertexts"]); rate = cts / sec
    pl[name] = {"ct_per_s": rate, "rec_per_s": rate * REC_PER_CT, "ms_per_ct": 1000 / rate, "P": P, "dP": P - P_idle,
                "mJ_gross": P / rate * 1000, "mJ_incr": (P - P_idle) / rate * 1000, "rec_per_J": rate * REC_PER_CT / P,
                "mean_ioctl_ms": float(w["parsed"]["mean_ioctl_ms"]), "mean_driver_ms": float(w["parsed"]["mean_driver_ms"]),
                "first_last_exact": w["parsed"].get("first_last_exact")}
phases = {}
for lines in L.get("phases", []):
    for l in lines:
        m = re.search(r"\(k=(\d+) tx=(\d+) B rx=(\d+) B\)", l)
        vals = dict((k, float(v)) for k, v in re.findall(r"(\w+)=([\d.]+)", l.split("(us):")[1].split("(k=")[0]))
        if m: phases[int(m.group(1))] = {"tx": int(m.group(2)), "rx": int(m.group(3)), **vals}
table, check = [], None
for l in L.get("pl_table", []):
    p = l.split()
    if len(p) >= 6 and p[0].isdigit() and p[1].isdigit():
        table.append({"k": int(p[0]), "ioctl_ms": float(p[2]), "driver_ms": float(p[3]), "ms_per_ct": float(p[4])})
    if "V8B_BOARD_ENC_CHECK_PASS" in l:
        check = dict(re.findall(r"(\w+)=([\w.^()]+)", l))
fit = None
if len(table) >= 2:
    xs = [t["k"] for t in table]; ys = [t["ioctl_ms"] for t in table]
    mx, my = st.mean(xs), st.mean(ys)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs); a = my - b * mx
    fit = {"fixed_ms_per_call": a, "ms_per_ct": b}

# ---- co-run (PL on core 0, encode on cores 1-3) and all-ARM system ----
cor = next((w for w in W if w["kind"] == "CORUN"), None)     # "pl+front3" (normalize + encode) or the older "pl+encode3"
corun_front = bool(cor) and "front" in cor["name"]
corun = None
if cor:
    sec, P, n = run_stats(cor)
    pl_rate = float(cor["parsed"]["ciphertexts"]) / sec
    enc = [l for l in L.get("corun_encode", []) if "MODULE_SUSTAIN_PASS" in l]
    ekv = dict(re.findall(r"(\w+)=([-\d.]+)", enc[-1])) if enc else {}
    enc_rate = float(ekv["units"]) / float(ekv["seconds"]) if ekv else float("nan")
    sys_rate = min(pl_rate, enc_rate)
    corun = {"pl_ct_per_s": pl_rate, "encode_ct_per_s": enc_rate, "encode_ms_per_thread": float(ekv.get("ms_per_unit_per_thread", "nan")),
             "system_ct_per_s": sys_rate, "rec_per_s": sys_rate * REC_PER_CT, "P": P, "dP": P - P_idle,
             "mJ_gross_per_ct": P / sys_rate * 1000, "rec_per_J": sys_rate * REC_PER_CT / P,
             "pl_duty": sys_rate / pl_rate, "first_last_exact": cor["parsed"].get("first_last_exact")}
armsys = None
w = win("ARMSYS", "client_ct x4")
if w:
    sec, P, n = run_stats(w)
    rate = 4 * 1000 / float(w["parsed"]["ms_per_unit_per_thread"])
    armsys = {"ct_per_s": rate, "rec_per_s": rate * REC_PER_CT, "P": P, "dP": P - P_idle, "mJ_gross_per_ct": P / rate * 1000,
              "rec_per_J": rate * REC_PER_CT / P, "ms_per_thread": float(w["parsed"]["ms_per_unit_per_thread"])}

# ---- model: throughput and energy per ciphertext for each partition ----
ENC_NODES = ["rns_message", "sample_all", "rns_small", "ntt_mask", "pointwise", "intt", "add"]
def t(m): return arm[m]["ms"] if m in arm else bench.get(m, float("nan"))
def e(m): return arm[m]["mJ_incr"] if m in arm else float("nan")
if "sample_all" not in arm:   # the sampler was measured as three parts in the energy runs; time from the bench
    parts_t = t("prng_chacha20") + t("sample_cdt63") + t("sample_ternary")
    arm["sample_all"] = {"ms": bench["sample_all"], "bench_ms": bench["sample_all"],
                         "mJ_incr": (e("prng_chacha20") + e("sample_cdt63") + e("sample_ternary")) * bench["sample_all"] / parts_t}
dPcore = st.median(v["dP"] for v in arm.values() if "dP" in v)
k8 = pl["sustain k8"]; ph = phases[8]
DRV = ("copy_in", "check_in", "sync_device", "sync_cpu", "check_out", "copy_out", "wipe")
drv_arm_ms = sum(ph[k] for k in DRV) / 8 / 1000
pl_kernel_ms = ph["dma_hls"] / 8 / 1000
pl_path_ms = k8["ms_per_ct"]
e_pl = k8["mJ_incr"] - drv_arm_ms * dPcore          # PL + DMA + DDR share of the PL path energy (driver core work removed)
front = t("front") if "front" in arm else t("normalize") + t("encode")     # ARM work before the PL: normalize + encode
hm = {mm["id"]: mm for mm in HLS.get("modules", [])} if HLS else {}

def model(arm_ms, rate, pl_energy):
    mj = P_idle / rate * 1000 + arm_ms * dPcore + pl_energy
    return mj, REC_PER_CT / (mj / 1000)

parts = {}
arm_work0 = front + t("encrypt_all")
r0 = 4 * 1000 / arm_work0; mj0, rpj0 = model(arm_work0, r0, 0.0)
parts["P0"] = {"name": "全部在 ARM", "arm_ms": arm_work0, "pl_ms": 0.0, "model_ct_per_s": r0, "model_mJ_gross_per_ct": mj0, "model_rec_per_J": rpj0,
               "measured": armsys}
arm_work3 = front + drv_arm_ms
r3 = min(3 * 1000 / front, 1000 / pl_path_ms, 1000 / pl_kernel_ms); mj3, rpj3 = model(arm_work3, r3, e_pl)
parts["P3"] = {"name": "V8b：編碼在 ARM、加密整段在 PL", "arm_ms": arm_work3, "pl_ms": pl_kernel_ms, "model_ct_per_s": r3,
               "model_mJ_gross_per_ct": mj3, "model_rec_per_J": rpj3, "measured": corun}
# P1: only NTT(u), pointwise and the two INTTs in PL; ARM keeps encode, m->RNS, sampling, small->RNS, add.
# Transfers per ciphertext: u in (3 limbs x 4096 x 4 B = 48 KiB), b*u and a*u out (96 KiB), costed with the measured
# per-byte driver phases plus the per-call fixed cost from the k-scaling fit (8 ciphertexts per call).
ns_in = (ph["copy_in"] + ph["check_in"]) * 1000 / ph["tx"]; ns_out = (ph["check_out"] + ph["copy_out"]) * 1000 / ph["rx"]
ns_sync_wipe = (ph["sync_device"] + ph["sync_cpu"] + ph["wipe"]) * 1000 / (ph["tx"] + ph["rx"])
io1_ms = (48 * 1024 * (ns_in + ns_sync_wipe) + 96 * 1024 * (ns_out + ns_sync_wipe)) / 1e6 + (fit["fixed_ms_per_call"] / 8 if fit else 0)
arm_work1 = front + t("rns_message") + t("sample_all") + t("rns_small") + t("add") + io1_ms
pl1 = (sum(hm[x]["us_per_ct"] for x in ("ntt_mask", "pointwise", "intt") if x in hm) / 1000) if all(x in hm for x in ("ntt_mask", "pointwise", "intt")) else pl_kernel_ms
r1 = min(4 * 1000 / arm_work1, 1000 / max(pl1, 1e-9)); mj1, rpj1 = model(arm_work1, r1, e_pl)
parts["P1"] = {"name": "只把 NTT／乘法／INTT 放 PL（模型估計）", "arm_ms": arm_work1, "pl_ms": pl1, "pl_ms_source": "HLS estimate" if hm else "PL kernel bound",
               "io_ms": io1_ms, "model_ct_per_s": r1, "model_mJ_gross_per_ct": mj1, "model_rec_per_J": rpj1,
               "note": "PL energy taken as the whole-chain PL energy (upper bound)"}
validation = {
    "P0": {"rate_err": r0 / armsys["ct_per_s"] - 1, "rpj_err": rpj0 / armsys["rec_per_J"] - 1},
    "P3": {"rate_err": r3 / corun["system_ct_per_s"] - 1, "rpj_err": rpj3 / corun["rec_per_J"] - 1}}

# ---- bottleneck: capacity of each stage of the V8b partition (records per second) ----
# The system rate of the co-run is min(encode, PL) by definition, so the evidence for the bottleneck is that the 3-core
# encode rate is below the PL rate measured AT THE SAME TIME (pl_path_corun). The break-even speed-up uses that same
# concurrent PL rate; the PL path alone (pl_path_1thread) is reported for reference.
capacity = {"encode_3core": 3 * 1000 / front * REC_PER_CT, "encode_3core_corun": corun["encode_ct_per_s"] * REC_PER_CT,
            "pl_path_corun": corun["pl_ct_per_s"] * REC_PER_CT, "pl_path_1thread": k8["rec_per_s"],
            "pl_kernel": 1000 / pl_kernel_ms * REC_PER_CT, "system_measured": corun["rec_per_s"]}
breakeven_encode_speedup = corun["pl_ct_per_s"] / corun["encode_ct_per_s"]
breakeven_vs_alone = k8["ct_per_s"] / corun["encode_ct_per_s"]

# ---- one 2048-record batch, round trip on the client (4 feature ciphertexts out, 1 result ciphertext back) ----
down = t("decrypt") + t("decode") + bench.get("sigmoid", 0.0)
batch = {"all_arm": {"encode": 4 * front, "encrypt": 4 * t("encrypt_all"), "downstream": down},
         "v8b_arm": {"encode": 4 * front, "driver": 4 * drv_arm_ms, "downstream": down},
         "v8b_pl": {"pl": 4 * pl_kernel_ms}}
for v in batch.values(): v["total"] = sum(v.values())

H = {"enc_speedup_time": t("encrypt_all") / pl_path_ms, "enc_speedup_time_kernel": t("encrypt_all") / pl_kernel_ms,
     "enc_energy_ratio": e("encrypt_all") / k8["mJ_incr"], "e2e_rpj_ratio": corun["rec_per_J"] / armsys["rec_per_J"],
     "e2e_rate_ratio": corun["rec_per_s"] / armsys["rec_per_s"], "p3_over_p1_rpj": corun["rec_per_J"] / rpj1,
     "encode_share_v8b_arm": front / arm_work3, "breakeven_encode_speedup": breakeven_encode_speedup,
     "breakeven_vs_pl_alone": breakeven_vs_alone,
     "p1_over_p0_model_vs_measured": rpj1 / armsys["rec_per_J"], "p1_over_p0_model_vs_model": rpj1 / rpj0,
     "arm_time_ratio_batch": batch["all_arm"]["total"] / batch["v8b_arm"]["total"],
     "corun_includes_normalize": corun_front}

S = {"P_idle_W": P_idle, "n_idle": n_idle, "dP_per_busy_core_W": dPcore, "normalize_source": normalize_src, "bench": bench,
     "arm": arm, "arm4": arm4, "pl": pl, "phases": phases, "pl_table": table, "pl_check": check, "pl_fit": fit,
     "driver_arm_ms_per_ct": drv_arm_ms, "pl_kernel_ms_per_ct": pl_kernel_ms, "pl_path_ms_per_ct": pl_path_ms, "pl_energy_mJ_per_ct": e_pl,
     "corun": corun, "armsys": armsys, "partitions": parts, "model_validation": validation, "capacity_rec_per_s": capacity,
     "batch_ms": batch, "headline": H, "hls": HLS}
json.dump(S, open(os.path.join(D, "partition_summary.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)

print(f"idle {P_idle:.3f} W (n={n_idle}); median dP per busy core {dPcore:.3f} W; normalize from {normalize_src}")
print(f"{'module':16s} {'ARM ms':>8s} {'dP W':>7s} {'ARM mJ':>8s}  {'PL us (HLS est.)':>16s}")
for m, v in arm.items():
    plu = hm.get(m, {}).get("us_per_ct")
    print(f"{m:16s} {v['ms']:8.3f} {v.get('dP', float('nan')):7.3f} {v['mJ_incr']:8.3f}  {('%.2f' % plu) if plu is not None else '-':>16s}")
for k, v in arm4.items(): print(f"ARM4 {k}: {v['units_per_s']:.0f} ct/s, {v['rec_per_s']/1e3:.1f}K rec/s, P {v['P']:.3f} W, {v['rec_per_J']:.0f} rec/J")
for k, v in pl.items(): print(f"PL {k}: {v['ct_per_s']:.0f} ct/s, {v['ms_per_ct']:.4f} ms/ct, dP {v['dP']:.3f} W, {v['mJ_incr']:.3f} mJ/ct incr, {v['mJ_gross']:.3f} mJ/ct gross, {v['rec_per_J']:.0f} rec/J")
print(f"driver ARM {drv_arm_ms:.4f} ms/ct, PL kernel {pl_kernel_ms:.4f} ms/ct, PL path {pl_path_ms:.4f} ms/ct, PL energy {e_pl:.3f} mJ/ct; fit {fit}")
print("check:", check); print("co-run:", corun); print("all-ARM system:", armsys)
for k, p in parts.items():
    meas = p.get("measured")
    print(k, p["name"], f"ARM {p['arm_ms']:.3f} ms/ct, PL {p['pl_ms']:.4f} ms/ct, model {p['model_ct_per_s']:.0f} ct/s ({p['model_ct_per_s']*512/1e3:.0f}K rec/s), {p['model_rec_per_J']:.0f} rec/J",
          "| measured:", f"{(meas.get('system_ct_per_s') or meas.get('ct_per_s')):.0f} ct/s, {meas['rec_per_J']:.0f} rec/J" if meas else "-")
print("validation:", validation); print("capacity:", {k: round(v) for k, v in capacity.items()}, "break-even encode speed-up", round(breakeven_encode_speedup, 2))
print("batch ms:", {k: {kk: round(vv, 3) for kk, vv in v.items()} for k, v in batch.items()}); print("headline:", {k: round(v, 3) for k, v in H.items()})

# ======================= figures (sized for the slide boxes; text >= 10 pt on the slide) =======================
SURF, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
C_ARM, C_PL, C_EST, C_PL2, C_DOWN = "#eb6834", "#2a78d6", "#9a9893", "#9cc3ee", "#c9c7c1"
for f in ("Microsoft JhengHei", "Noto Sans TC"):
    if any(f in x.name for x in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = f; break
plt.rcParams.update({"figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF, "axes.edgecolor": GRID,
                     "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2, "text.color": INK, "font.size": 12.5,
                     "axes.titlesize": 14, "axes.titleweight": "bold", "axes.spines.top": False, "axes.spines.right": False,
                     "axes.unicode_minus": False, "axes.formatter.use_mathtext": False})
def save(fig, name):
    fig.savefig(os.path.join(FIG, name), dpi=180, bbox_inches="tight"); plt.close(fig); print("saved", name)
def plain(x, _):
    return f"{x:g}"
def kfmt(v):
    return f"{v / 1e6:.2f}M" if v >= 1e6 else f"{v / 1e3:.1f}K" if v >= 1e3 else f"{v:.0f}"
def quiet(ax, axis="x"):
    getattr(ax, axis + "axis").grid(True, color=GRID, lw=0.8); ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False); ax.tick_params(axis="y", length=0)

# --- fig_p1: per module, ARM (measured, one core) vs PL (HLS estimate per stage; whole chain measured) ---
NAMES = [("encode", "CKKS 編碼（浮點 FFT）"), ("rns_message", "m 轉 RNS"), ("prng_chacha20", "ChaCha20 亂數"),
         ("sample_ternary", "三元遮罩（含其亂數）"), ("sample_cdt63", "CDT63 高斯取樣"), ("rns_small", "小數值轉 RNS"),
         ("ntt_mask", "NTT(u)"), ("pointwise", "公鑰逐點相乘"), ("intt", "兩次 INTT"), ("add", "組裝 c0、c1")]
pl_est = {k: v.get("us_per_ct") for k, v in hm.items()}
if "prng_chacha20" not in pl_est and ("prng_mask" in pl_est or "prng_gauss" in pl_est):
    pl_est["prng_chacha20"] = (pl_est.get("prng_mask") or 0) + (pl_est.get("prng_gauss") or 0)
# The ARM ternary module (sampler_b::ternary) generates its own 16 ChaCha20 mask blocks; the PL sample_ternary stage
# does not (prng_mask does), so the PL side of that row is prng_mask + sample_ternary to compare the same work.
if pl_est.get("sample_ternary") and pl_est.get("prng_mask"):
    pl_est["sample_ternary"] = pl_est["sample_ternary"] + pl_est["prng_mask"]
S["pl_est_compared_us"] = pl_est
json.dump(S, open(os.path.join(D, "partition_summary.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
fig, ax = plt.subplots(figsize=(7.3, 3.2))      # about the deck slot (7.52 x 3.18 in), so the deck shows it near 1:1
ys = list(range(len(NAMES) + 1))[::-1]
for y, (mid, label) in zip(ys[:-1], NAMES):
    a = t(mid); b = pl_est.get(mid); b = b / 1000 if b else None
    if b: ax.plot([b, a], [y, y], color=GRID, lw=2.2, zorder=1)
    ax.scatter([a], [y], s=64, color=C_ARM, edgecolor=SURF, linewidth=1.4, zorder=3)
    if b:
        ax.scatter([b], [y], s=64, facecolor=SURF, edgecolor=C_PL, linewidth=2, zorder=3)
        ax.text(a * 1.3, y, f"×{a / b:.0f}", va="center", fontsize=11.5, color=INK2)
    else:
        ax.text(a * 1.3, y, "PL 未實作", va="center", fontsize=11.5, color=INK2)
yb = ys[-1]; a = t("encrypt_all")
ax.plot([pl_path_ms, a], [yb, yb], color=GRID, lw=2.2, zorder=1)
ax.scatter([a], [yb], s=86, color=C_ARM, edgecolor=SURF, linewidth=1.4, zorder=3)
ax.scatter([pl_path_ms], [yb], s=86, color=C_PL, edgecolor=SURF, linewidth=1.4, zorder=3)
ax.text(a * 1.3, yb, f"×{a / pl_path_ms:.0f}", va="center", fontsize=11.5, color=INK, fontweight="bold")
ax.set_yticks(ys); ax.set_yticklabels([l for _, l in NAMES] + ["整段加密（兩端實測）"])
ax.get_yticklabels()[-1].set_fontweight("bold")
ax.set_xscale("log"); ax.xaxis.set_major_formatter(ticker.FuncFormatter(plain)); ax.xaxis.set_minor_formatter(ticker.NullFormatter())
ax.set_xlabel("每個密文的時間（ms，對數刻度）"); quiet(ax)
ax.legend(handles=[Line2D([], [], marker="o", ls="", color=C_ARM, label="ARM 單核（實測）"),
                   Line2D([], [], marker="o", ls="", markerfacecolor=SURF, markeredgecolor=C_PL, markeredgewidth=2, label="PL 各階段（HLS 估計）"),
                   Line2D([], [], marker="o", ls="", color=C_PL, label="PL 整段（實測，含驅動）")],
          loc="upper left", bbox_to_anchor=(0.0, 1.0), ncol=1, frameon=True, framealpha=0.92, edgecolor=SURF, facecolor=SURF,
          fontsize=11, handletextpad=0.2, borderaxespad=0.2, labelspacing=0.3)
xs_all = [pl_path_ms] + [x / 1000 for x in pl_est.values() if x] + [t(mid) for mid, _ in NAMES]
ax.set_xlim(min(xs_all) * 0.12, t("encrypt_all") * 4)     # room on the left for the legend (top rows are empty there)
save(fig, "fig_p1_modules.png")

# --- fig_p2: partitions, throughput and records per joule (both include the ARM encode) ---
order = ["P0", "P1", "P3"]
lab = {"P0": "全部在 ARM\n（4 核，實測）", "P1": "只放 NTT／乘法／INTT\n（模型估計）", "P3": "V8b：加密整段在 PL\n（共同執行實測）"}
col = {"P0": C_ARM, "P1": C_EST, "P3": C_PL}
def val(k, what):
    p = parts[k]; m = p.get("measured")
    if what == "rate": return m["rec_per_s"] if m else p["model_ct_per_s"] * REC_PER_CT
    return m["rec_per_J"] if m else p["model_rec_per_J"]
fig, axs = plt.subplots(1, 2, figsize=(12.6, 3.7), gridspec_kw={"wspace": 0.68})
for ax, what, title, xl in ((axs[0], "rate", "每秒筆數（含 ARM 編碼）", "筆／秒"), (axs[1], "rpj", "每焦耳筆數（整板，含 ARM 編碼）", "筆／焦耳")):
    y = list(range(len(order)))[::-1]; vals = [val(k, what) for k in order]
    for yi, k, v in zip(y, order, vals):
        ax.barh(yi, v, height=0.5, color=col[k] if k != "P1" else SURF, hatch="///" if k == "P1" else None,
                edgecolor=C_EST if k == "P1" else SURF, linewidth=1.2 if k == "P1" else 0, zorder=2)
        ax.text(v + max(vals) * 0.02, yi, kfmt(v), va="center", fontsize=16, fontweight="bold" if k == "P3" else "normal")
    ax.set_yticks(y); ax.set_yticklabels([lab[k] for k in order], fontsize=15)
    ax.tick_params(axis="x", labelsize=15)
    ax.set_xlim(0, max(vals) * 1.28); ax.set_title(title, loc="left", fontsize=17)
    ax.xaxis.set_major_locator(ticker.MaxNLocator(4))
    ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: (f"{x / 1e6:g}M" if x >= 1e6 else f"{x / 1e3:g}K") if x else "0"))
    ax.set_xlabel(xl, fontsize=15); quiet(ax)
save(fig, "fig_p2_partitions.png")

# --- fig_p3: capacity of each stage of the V8b partition (the slowest stage sets the system rate) ---
enc_lab = "ARM 正規化與編碼（3 核）" if corun_front else "ARM 編碼（3 核）"
cap_rows = [(enc_lab, capacity["encode_3core_corun"], C_ARM, "共同執行"),
            ("PL 加密路徑（含驅動）", capacity["pl_path_corun"], C_PL, "共同執行"),
            ("PL 加密路徑（含驅動）", capacity["pl_path_1thread"], C_PL2, "單獨執行"),
            ("PL 核心與 DMA", capacity["pl_kernel"], C_PL2, "不含驅動")]
fig, ax = plt.subplots(figsize=(7.4, 3.1))      # about the deck slot (7.52 x 3.12 in)
yy = list(range(len(cap_rows)))[::-1]
for i, (y, (name, v, c, cond)) in enumerate(zip(yy, cap_rows)):
    ax.barh(y, v, height=0.52, color=c, zorder=2, hatch=None if cond == "共同執行" else None)
    ax.text(v + 0.05e6, y, kfmt(v) + ("　← 系統速度（兩者較慢者）" if i == 0 else ""), va="center", fontsize=12.5,
            fontweight="bold" if i < 2 else "normal")
ax.axvline(capacity["system_measured"], color=INK2, lw=1, ls=(0, (3, 3)), zorder=1)
ax.set_yticks(yy); ax.set_yticklabels([f"{r[0]}\n{r[3]}" for r in cap_rows], fontsize=11.5)
ax.set_xlim(0, max(r[1] for r in cap_rows) * 1.45)
ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: kfmt(x) if x else "0")); ax.set_xlabel("每秒筆數上限"); quiet(ax)
save(fig, "fig_p3_capacity.png")

# --- fig_p4: one 2048-record batch on the client, ARM core time and PL time (report) ---
C_ENC2 = "#f4a582"
segs = [("全部在 ARM\n（ARM 核心時間）", [("enc", batch["all_arm"]["encode"]), ("crypt", batch["all_arm"]["encrypt"]), ("down", batch["all_arm"]["downstream"])]),
        ("V8b\n（ARM 核心時間）", [("enc", batch["v8b_arm"]["encode"]), ("drv", batch["v8b_arm"]["driver"]), ("down", batch["v8b_arm"]["downstream"])]),
        ("V8b\n（PL 時間）", [("pl", batch["v8b_pl"]["pl"])])]
SEG = {"enc": ("正規化與編碼", C_ARM), "crypt": ("加密（ARM）", C_ENC2), "drv": ("驅動（複製、檢查、同步、清除）", "#d9a07f"),
       "down": ("解密、解碼、sigmoid（規格程式）", C_DOWN), "pl": ("DMA 與 PL 加密", C_PL)}
fig, ax = plt.subplots(figsize=(10.4, 3.7))
yy = [2, 1, 0]
for y, (name, parts_) in zip(yy, segs):
    x0 = 0
    for key, w_ in parts_:
        c = SEG[key][1]
        ax.barh(y, w_, left=x0, height=0.5, color=c, edgecolor=SURF, linewidth=2, zorder=2)
        if w_ >= 4.0: ax.text(x0 + w_ / 2, y, f"{w_:.1f}", ha="center", va="center", fontsize=12, color="white" if c in (C_ARM, C_PL) else INK)
        x0 += w_
    small = [(SEG[k][0].split("（")[0], w_) for k, w_ in parts_ if w_ < 4.0] if len(parts_) > 1 else []
    tail = "、".join(f"{lbl} {w_:.2f}" for lbl, w_ in small)
    ax.text(x0 + 0.7, y, (f"合計 {x0:.1f} ms" if x0 >= 1 else f"合計 {x0:.2f} ms") + (f"（{tail}）" if tail else ""), va="center", fontsize=12, fontweight="bold")
ax.set_yticks(yy); ax.set_yticklabels([s_[0] for s_ in segs])
ax.set_xlim(0, batch["all_arm"]["total"] * 1.38); ax.set_xlabel("每批 2048 筆（4 個特徵密文、1 個結果密文）的時間（ms）"); quiet(ax)
ax.legend(handles=[Patch(color=SEG[k][1], label=SEG[k][0]) for k in ("enc", "crypt", "drv", "down", "pl")],
          loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=3, frameon=False, fontsize=11)
save(fig, "fig_p4_batch.png")
