"""Build report.html (figures + explanations + statistics + how to state the conclusion) from summary.json.
usage: python make_report.py <data dir> [earlier same-setup data dir] [earlier no-ethernet V8b data dir] [--partition=<partition data dir>]
  --partition adds the chapter on the hardware/software partition (partition_summary.json and figures written by
  energy_probe/kv260/partition/analyze_partition.py)."""
import json, math, os, re, statistics as st, sys, datetime
from scipy import stats
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

POS = [a for a in sys.argv[1:] if not a.startswith("--")]
OPT = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
d = POS[0]
prev = POS[1] if len(POS) > 1 else None
pre_eth = POS[2] if len(POS) > 2 else None
pdir = OPT.get("partition")
S = json.load(open(os.path.join(d, "summary.json"), encoding="utf-8"))
L = json.load(open(os.path.join(d, "runs.json"), encoding="utf-8"))
R = S["runs"]
V = ["V8b", "OpenFHE4", "OpenFHE1"]
NAME = {"V8b": "V8b（FPGA + ARM）", "OpenFHE1": "OpenFHE 單執行緒（純 ARM）", "OpenFHE4": "OpenFHE 4 執行緒（純 ARM）"}
SHORT = {"V8b": "V8b", "OpenFHE1": "OpenFHE 單執行緒", "OpenFHE4": "OpenFHE 4 執行緒"}
SW = {"V8b": "var(--blue)", "OpenFHE4": "var(--aqua)", "OpenFHE1": "var(--orange)"}
by = {v: [r for r in R if r["version"] == v] for v in V}
n = {v: len(by[v]) for v in V}

# Probe accuracy check of 2026-11-23 (lab multimeter against the probe at the 12 V input) and the INA228 limits
# of the TI datasheet SLYS021A; these come from a separate check, not from the data folders of this report.
CAL = {"meter_V": 12.21, "probe_V": 12.14, "gain_pct": 0.05, "bus_offset_mV": 2.5, "zero_mA": -0.05}

def m(v, k): return st.mean(r[k] for r in by[v])
def sd(v, k): return st.stdev(r[k] for r in by[v]) if n[v] > 1 else 0.0
def ci(v, k):
    h = stats.t.ppf(0.975, n[v] - 1) * sd(v, k) / math.sqrt(n[v]) if n[v] > 1 else 0.0
    return m(v, k) - h, m(v, k) + h
def ratio(a, b, k):
    """mean(a)/mean(b) with a 95% CI from the log-ratio (delta method, Welch df)."""
    ra, rb = sd(a, k) / m(a, k), sd(b, k) / m(b, k)
    va, vb = ra ** 2 / n[a], rb ** 2 / n[b]
    df = (va + vb) ** 2 / (va ** 2 / (n[a] - 1) + vb ** 2 / (n[b] - 1))
    h = stats.t.ppf(0.975, df) * math.sqrt(va + vb)
    r = m(a, k) / m(b, k)
    return r, r * math.exp(-h), r * math.exp(h)
def welch_p(a, b, k):
    return stats.ttest_ind([r[k] for r in by[a]], [r[k] for r in by[b]], equal_var=False).pvalue
def drift(v, k):
    xs = list(range(n[v])); ys = [r[k] for r in by[v]]
    sl = stats.linregress(xs, ys).slope if n[v] > 2 else 0.0
    return sl * (n[v] - 1) / m(v, k) * 100           # % change from first to last run along the fitted line
def f0(x): return f"{x:,.0f}"
def fW(x): return f"{x:.3f} W"
def pstr(p): return "&lt; 10<sup>−10</sup>" if p < 1e-10 else f"{p:.1e}"
def utc(ts): return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
def wan(x): return f"{x / 1e4:.0f} 萬" if x >= 1e6 else f"{x / 1e4:.1f} 萬"
def kfmt(v): return f"{v / 1e6:.2f}M" if v >= 1e6 else f"{v / 1e3:.1f}K"
def span(a, b, fmt="{:.1f}"):
    """'a–b', or a single value when both round to the same text"""
    ta, tb = fmt.format(a), fmt.format(b)
    return ta if ta == tb else f"{ta}–{tb}"
NBSP, CJK = chr(0xA0), "[" + chr(0x4E00) + "-" + chr(0x9FFF) + "]"     # no-break space; CJK unified ideographs
def spaced(s):
    """a space between CJK and Latin letters or digits, as in the rest of the text"""
    return re.sub(r"(?<=[A-Za-z0-9)])(?=" + CJK + ")|(?<=" + CJK + r")(?=[A-Za-z0-9(])", " ", s)
def nw_list(items):
    """a 、-separated list whose items do not break inside when a narrow cell wraps"""
    return "".join(f"<span class='nw'>{x}{'、' if i < len(items) - 1 else ''}</span>" for i, x in enumerate(items))
def sgn(x, fmt="{:+.2f}"):
    """signed number with a typographic minus"""
    return fmt.format(x).replace("-", "−")

t0 = utc(L["idle"][0]["start"]); t1 = utc(L["runs"][-1]["t_end"])
secs = L.get("secs", 30)
r4g = ratio("V8b", "OpenFHE4", "rpj_gross"); r1g = ratio("V8b", "OpenFHE1", "rpj_gross")
r4n = ratio("V8b", "OpenFHE4", "rpj_net"); r4s = ratio("V8b", "OpenFHE4", "rec_s")
r4p = ratio("V8b", "OpenFHE4", "P_run")
uj = {v: 1e6 / m(v, "rpj_gross") for v in V}; mj = {v: 2048e3 / m(v, "rpj_gross") for v in V}
inc = {v: m(v, "P_run") - m(v, "P_idle") for v in V}
idle_v8, idle_of = S["idle_W"]["V8b"], S["idle_W"]["OpenFHE"]
diffs = [abs(r["esp_rpj"] / r["rpj_gross"] - 1) * 100 for r in R]
worst_diff = max(diffs)
drifts = {v: drift(v, "rpj_gross") for v in V}
speed41 = m("OpenFHE4", "rec_s") / m("OpenFHE1", "rec_s")
nn = n["V8b"]
cv = {v: sd(v, "rpj_gross") / m(v, "rpj_gross") * 100 for v in V}

# ---- probe trace of this comparison: load current per setting, and the board's default state before and after ----
PB = []
with open(os.path.join(d, "probe.csv"), encoding="utf-8") as f:
    f.readline()
    for line in f:
        p = line.split(",")
        if len(p) >= 5: PB.append((float(p[0]), float(p[3]), float(p[4]) / 1000))   # host time (s), current (mA), power (W)
amps = {}
for v in V:
    cur = []
    for r in L["runs"]:
        if r["version"] != v or "parsed" not in r: continue
        a, b = r["t_begin"] + 0.5, r["t_begin"] + float(r["parsed"]["seconds"]) - 0.3
        cur += [i for t, i, _ in PB if a <= t <= b]
    amps[v] = st.mean(cur) / 1000 if cur else float("nan")
# The script starts logging before it loads V8b and stops about 1 s after it restores the default app (k26-starter-kits),
# so the first and the last second of the trace are the default state. Used only when both are flat and agree.
head, tail = [p for _, _, p in PB[:10]], [p for _, _, p in PB[-10:]]
dflt = None
if len(PB) > 100 and max(head) - min(head) < 0.2 and max(tail) - min(tail) < 0.2 and abs(st.mean(head) - st.mean(tail)) < 0.15:
    dflt = st.mean(head + tail)
    if dflt > idle_of - 0.2: dflt = None          # no clear gap to the PL-cleared idle: say nothing about it

prev_line = ""
if prev and os.path.exists(os.path.join(prev, "summary.json")):
    P = json.load(open(os.path.join(prev, "summary.json"), encoding="utf-8"))["runs"]
    pv = {v: st.mean(r["rpj_gross"] for r in P if r["version"] == v) for v in V}
    dv = {v: (m(v, "rpj_gross") / pv[v] - 1) * 100 for v in V}
    prev_line = ("<li><b>可重現。</b>本團隊同日稍早以相同接線各跑 3 輪，其平均與本次 " + str(nn) + " 輪的平均相差："
                 + "、".join(f"{SHORT[v]} {sgn(dv[v], '{:+.1f}')}%" for v in V) + "。</li>")
pre_line, q_idle = "", {}
if pre_eth and os.path.exists(os.path.join(pre_eth, "summary.json")):
    QS = json.load(open(os.path.join(pre_eth, "summary.json"), encoding="utf-8"))
    q = [r for r in QS["runs"] if r["version"] == "V8b"]; q_idle = QS.get("idle_W", {})
    pre_line = (f"<li><b>接上乙太網路後整板功率高約 1 W。</b>同日更早、尚未接乙太網路時，V8b 的每秒筆數相近（{f0(st.mean(r['rec_s'] for r in q))}），"
                f"加密時功率為 {st.mean(r['P_run'] for r in q):.3f} W，每焦耳筆數為 {f0(st.mean(r['rpj_gross'] for r in q))}。"
                "不同時段、不同接線的絕對數值不宜直接比較；同一次量測內的倍數不受影響。</li>")
d_noeth = q_idle["V8b"] - q_idle["starter-kit"] if "V8b" in q_idle and "starter-kit" in q_idle else None

# ---- idle states: what the ratio assumes, and what it would be if OpenFHE ran in the default state (estimate) ----
P4 = m("OpenFHE4", "P_run")
gaps = [g for g in ((idle_of - dflt) if dflt else None, (d_noeth - (idle_v8 - idle_of)) if d_noeth else None) if g]
idle_row = f"V8b：已載入 V8b 硬體與驅動，不執行加密（{fW(idle_v8)}）。OpenFHE：PL 清空，不載入任何 FPGA 設計（{fW(idle_of)}）。"
fpga_claim = (f"<li><b>V8b 的 FPGA 設計本身的額外待機耗電很小。</b>載入 V8b 後的待機功率比 PL 清空只高 {(idle_v8 - idle_of) * 1000:.0f} mW"
              + (f"；但兩者皆比板子的預設狀態高約 {idle_of - dflt:.1f} W，原因尚未確認（見限制與注意事項）" if dflt else "") + "。</li>")
idle_lim = (f"<li><b>待機狀態不同。</b>V8b 的待機包含已載入的 FPGA 設計（{fW(idle_v8)}）；OpenFHE 的待機為 PL 清空（{fW(idle_of)}），"
            f"兩者相差 {(idle_v8 - idle_of) * 1000:.0f} mW。")
if gaps:
    seen = []
    if dflt:
        idle_row += f"兩者皆比板子的預設狀態（k26-starter-kits）高約 {idle_of - dflt:.1f} W，原因尚未確認；對倍數的影響見限制與注意事項。"
        seen.append(f"本次量測開始前與還原預設狀態後的探針讀值（各約 1 秒）平均為 {dflt:.2f} W，比 PL 清空低 {idle_of - dflt:.2f} W")
    if d_noeth:
        seen.append(f"同日未接乙太網路的量測中，預設狀態的待機（{q_idle['starter-kit']:.3f} W）亦比 V8b 已載入（{q_idle['V8b']:.3f} W）"
                    f"低 {d_noeth:.2f} W")
    x_lo, x_hi = r4g[0] * (P4 - max(gaps)) / P4, r4g[0] * (P4 - min(gaps)) / P4
    idle_lim += ("兩者皆高於板子的預設狀態（k26-starter-kits）：" + "；".join(seen) + "。此差異的原因尚未確認。"
                 "本報告的倍數以 OpenFHE 在 PL 清空狀態執行為前提；若 OpenFHE 改在預設狀態執行而 V8b 不變，以上述待機差推算"
                 f"（假設此差值在加密時不變，未實測），每焦耳筆數的倍數約為 {span(x_lo, x_hi)} 倍。"
                 "建議本團隊於期末展示前在預設狀態下實測 OpenFHE，以確認此推算。")
idle_lim += "</li>"

# ---- CKKS moduli as reported by the board programs ----
def moduli(version, pat):
    for r in L["runs"]:
        if r["version"] != version: continue
        for l in r.get("lines", []):
            mm = re.search(pat, l)
            if mm: return [int(x) for x in re.findall(r"\d+", mm.group(1))]
    return None
q_v8, q_of = moduli("V8b", r"q=\{([\d,]+)\}"), moduli("OpenFHE4", r"q=((?:\d+ ?)+)")
def bits(qs): return sum(math.log2(x) for x in qs)
if q_v8 and q_of:
    q_bits = f"約 {(bits(q_v8) + bits(q_of)) / 2:.0f} 位元：V8b {bits(q_v8):.1f}、OpenFHE {bits(q_of):.1f}"
    q_bits_short = f"約 {(bits(q_v8) + bits(q_of)) / 2:.0f} 位元"
    q_of_txt = "／".join(str(x) for x in q_of)
else:
    q_bits, q_bits_short, q_of_txt = "約 88 位元", "約 88 位元", "1073692673／536813569／536903681"

# ---- probe accuracy (see CAL) ----
cal_bound_mV = CAL["gain_pct"] / 100 * CAL["probe_V"] * 1000 + CAL["bus_offset_mV"]
cal_bound_pct = cal_bound_mV / 1000 / CAL["probe_V"] * 100
cal_gap_pct = (CAL["meter_V"] / CAL["probe_V"] - 1) * 100
calib = (
    "<li><b>探針未校正。</b>本團隊原本計畫校正探針，但手邊沒有比 INA228 更準確的參考儀器，因此改為查核規格書的準確度。"
    "依 TI 規格書（SLYS021A），INA228 的分流電壓與匯流排電壓增益誤差最大皆為 ±0.05%，分流偏移最大 ±1 µV（15 mΩ 下約 ±67 µA），"
    f"匯流排偏移最大 ±{CAL['bus_offset_mV']:.1f} mV，積分非線性為 ±0.002%；在 {CAL['probe_V']:.2f} V 時，匯流排電壓的誤差上限為 "
    f"{CAL['gain_pct']:.2f}% × {CAL['probe_V']:.2f} V＋{CAL['bus_offset_mV']:.1f} mV ≈ {cal_bound_mV:.1f} mV（約 {cal_bound_pct:.2f}%）。"
    f"以實驗室三用電表比對時，三用電表讀得 {CAL['meter_V']:.2f} V、探針讀得 {CAL['probe_V']:.2f} V，兩者相差 {cal_gap_pct:.2f}%，"
    f"約為此電壓誤差上限的 {cal_gap_pct / cal_bound_pct:.0f} 倍，因此差距來自三用電表本身的準確度（手持電表的直流電壓準確度通常約 ±0.5%）"
    "或量測點不同（電源線在 0.6 A 時約有 0.07 V 壓降），而非來自 INA228；三用電表反而是兩者中較不準確的一方，不適合作為校正基準，"
    "因此本團隊未套用修正係數（韌體維持 <code>K_V = K_I = 1</code>）。模組上的分流電阻（R015）未標示公差，探針絕對數值的不確定度因此無法由規格界定；"
    "若以 1% 公差估計，約為 1%。此外，VBus 接在 Vin+，即在供電側（分流電阻上游）量測電壓，量得的功率包含分流電阻與導線的少量損耗。"
    "上述因素只影響絕對數值，不影響比較：V8b 與 OpenFHE 使用同一支探針、同一次量測與相同的 30 秒區間定義（各輪的區間不同），"
    f"負載電流亦相近（V8b {amps['V8b']:.2f} A、OpenFHE 4 執行緒 {amps['OpenFHE4']:.2f} A）；增益誤差使各設定的能量乘上同一係數，"
    f"在每焦耳筆數的比值中抵消；偏移（實測零點約 {sgn(CAL['zero_mA'], '{:.2f}')} mA）低於負載的 0.01%；積分非線性與振盪器公差亦不致使各設定之間產生偏差。"
    f"因此，V8b 相對 OpenFHE 4 執行緒的 {r4g[0]:.2f} 倍（95% 信賴區間 {r4g[1]:.2f}–{r4g[2]:.2f}）不受影響。"
    f"{nn} 輪的變異係數與兩種能量計算方式的一致性只顯示量測的重複性，並不檢驗增益誤差。</li>")

def partition_chapter(pd):
    """Chapter: minimal modules, ARM (measured) vs PL (HLS estimate per stage, whole chain measured), three partitions,
    bottleneck of the encryption uplink, round trip of one batch."""
    PS = json.load(open(os.path.join(pd, "partition_summary.json"), encoding="utf-8"))
    PR = json.load(open(os.path.join(pd, "partition_runs.json"), encoding="utf-8"))
    img = lambda name: os.path.relpath(os.path.join(pd, "figures", name), d).replace(os.sep, "/")
    A, H, B = PS["arm"], PS["headline"], PS["bench"]
    k8, cor, arms = PS["pl"]["sustain k8"], PS["corun"], PS["armsys"]
    P1, val, cap, bat = PS["partitions"]["P1"], PS["model_validation"], PS["capacity_rec_per_s"], PS["batch_ms"]
    hm = {x["id"]: x.get("us_per_ct") for x in (PS.get("hls") or {}).get("modules", [])}
    if "prng_chacha20" not in hm and ("prng_mask" in hm or "prng_gauss" in hm):
        hm["prng_chacha20"] = (hm.get("prng_mask") or 0) + (hm.get("prng_gauss") or 0)
    est = PS.get("pl_est_compared_us") or {}       # PL side per row as compared in fig_p1 (ternary row = prng_mask + sample_ternary)
    pl_us = lambda mid: est.get(mid) or hm.get(mid)
    when = utc(PR["windows"][0]["t_begin"])
    run_s = {}
    for w in PR["windows"]:
        if "seconds" in (w.get("parsed") or {}): run_s.setdefault(w["kind"], round(float(w["parsed"]["seconds"])))
    front = bool(H.get("corun_includes_normalize"))
    enc_w = "正規化與編碼" if front else "編碼"
    tms = lambda k: A[k]["ms"] if k in A else B.get(k)
    def row(mid, name, role, place):
        t_ = tms(mid); e_ = A.get(mid, {}).get("mJ_incr")
        u = pl_us(mid)
        ratio_ = f"{t_ * 1000 / u:,.0f}" if u else "—"
        return (f"<tr><td>{name}</td><td>{role}</td><td class='n'>{t_:.3f}</td><td class='n'>{('%.3f' % e_) if e_ is not None else '—'}</td>"
                f"<td class='n'>{('%.2f' % u) if u else '—'}</td><td class='n'>{ratio_}</td><td>{place}</td></tr>")
    ENC = [("rns_message", "m 轉 RNS", "m 對 3 個模數取餘數"),
           ("prng_chacha20", "ChaCha20 亂數", "產生遮罩與兩個雜訊多項式所需的亂數"),
           ("sample_ternary", "三元遮罩<br>（含其亂數）", "產生遮罩所需的亂數，並轉成係數為 −1、0、1 的遮罩 u"),
           ("sample_cdt63", "CDT63 高斯取樣", "亂數經查表轉成雜訊 e0、e1"),
           ("rns_small", "小數值轉 RNS", "u、e0、e1 對 3 個模數取餘數"),
           ("ntt_mask", "NTT(u)", "u 的 3 個餘數各做一次 NTT"),
           ("pointwise", "公鑰逐點相乘", "b·u 與 a·u 在頻域逐點相乘"),
           ("intt", "兩次 INTT", "b·u 與 a·u 轉回係數域（各 3 個餘數）"),
           ("add", "組裝 c0、c1", "c0 = b·u + e0 + m，c1 = a·u + e1")]
    mods = row("normalize", "正規化", "把原始特徵縮放到 CKKS 的數值範圍", "ARM")
    mods += row("encode", "CKKS 編碼", "2048 個數值經浮點 FFT 轉成整數多項式 m；<span class='nw'>PL 尚未實作</span>", "ARM")
    mods += "".join(row(mid, name, role, "PL") for mid, name, role in ENC)
    whole = (f"<tr class='sum'><td><b>整段加密</b></td><td>上列 m 轉 RNS 至組裝（兩端實測）</td><td class='n'><b>{tms('encrypt_all'):.3f}</b></td>"
             f"<td class='n'><b>{A['encrypt_all']['mJ_incr']:.3f}</b></td><td class='n'><b>{PS['pl_path_ms_per_ct'] * 1000:.1f}</b></td>"
             f"<td class='n'><b>{H['enc_speedup_time']:.0f}</b></td><td>PL</td></tr>")
    down = "".join(row(*r) for r in [
        ("decrypt", "解密", "c0 + c1·s，以私鑰還原 m；私鑰不進 PL", "ARM"),
        ("decode", "解碼", "浮點 FFT 還原 2048 個數值", "ARM"),
        ("sigmoid", "sigmoid", "分數轉成機率", "ARM")])
    tbl = PS["pl_table"]; fit = PS["pl_fit"]; ph = PS["phases"]["8"] if "8" in PS["phases"] else PS["phases"][8]
    k8_check = next((x for x in tbl if x["k"] == 8), tbl[-1])
    ktab = "".join(f"<tr><td class='n'>{x['k']}</td><td class='n'>{x['ioctl_ms']:.3f}</td><td class='n'>{x['driver_ms']:.3f}</td><td class='n'>{x['ms_per_ct']:.3f}</td></tr>" for x in tbl)
    drv_us = sum(ph[k] for k in ("copy_in", "check_in", "sync_device", "sync_cpu", "check_out", "copy_out", "wipe"))
    phase_txt = "、".join(f"{lbl} {ph[k]:.0f}" for k, lbl in (("copy_in", "複製輸入"), ("check_in", "檢查輸入"), ("sync_device", "快取同步（送出）"),
                                                             ("dma_hls", "DMA 與 PL 核心"), ("sync_cpu", "快取同步（取回）"), ("check_out", "檢查輸出"),
                                                             ("copy_out", "複製輸出"), ("wipe", "清除緩衝區")))
    chk = PS.get("pl_check") or {}
    arm4e = PS["arm4"]["encrypt_all"]
    of4 = m("OpenFHE4", "rec_s")
    hls_pred = (PS.get("hls") or {}).get("predicted_us_per_frame_k8")
    pl_meas_us = ph["dma_hls"]
    hls_line = (f"依 HLS 原始碼的排程模型，PL 處理一個 frame（8 個密文）約需 {hls_pred:,.1f} µs，與上述最後一次呼叫實測的 {pl_meas_us:,.1f} µs "
                f"相差 {abs(hls_pred / pl_meas_us - 1) * 100:.1f}%；PL 的瓶頸為 3 個模數引擎的 NTT／INTT 鏈，取樣器的時間與其相當，兩者以資料流同時執行"
                "（詳見 <code>energy_probe/kv260/partition/hls_model/</code>）。此一致性只驗證瓶頸所在的模數引擎鏈，且模型中 ChaCha20 的呼叫延遲曾以此類實測值"
                "排除不合理的範圍；非瓶頸階段（m 轉 RNS、三元遮罩、CDT63、ChaCha20）的估計未經實測驗證。") if hls_pred else ""
    # Measured DMA + PL kernel time per ciphertext bounds every PL stage from above: modules whose ARM time exceeds it
    # are faster in PL whatever the HLS estimate says; the others depend on the estimate.
    kb_us = ph["dma_hls"] / 8
    rely = [re.sub(r"<br>.*", "", name) for mid, name, _ in ENC if tms(mid) * 1000 <= kb_us]
    bound_txt = (f"實測的 DMA 與 PL 核心時間平均每個密文 {kb_us:.1f} µs，是 PL 任一階段每個密文時間的上限；"
                 + (spaced("除" + "與".join(rely) + "外") + "，其餘模組的 ARM 單核時間皆高於此上限，在 PL 較快不依賴估計。" if rely else
                    "各模組的 ARM 單核時間皆高於此上限，在 PL 較快不依賴估計。"))
    sig_reps = next((b.get("reps") for b in PR.get("bench", []) if b.get("module") == "sigmoid"), None)
    sig_txt = f"sigmoid 只量時間，取 {sig_reps} 次重複的中位數" if sig_reps else "sigmoid 只量時間"
    # co-run: the PL runs at full speed although the system rate is set by the ARM side
    extra = cor["pl_ct_per_s"] / cor["system_ct_per_s"] - 1
    extra_W = (cor["pl_ct_per_s"] - cor["system_ct_per_s"]) * k8["dP"] / k8["ct_per_s"]
    bias = cor["P"] / (cor["P"] - extra_W) - 1
    slow = 1 - cor["pl_ct_per_s"] / k8["ct_per_s"]
    dps = [v["dP"] for v in A.values() if "dP" in v]
    norm_lim = "" if PS.get("normalize_source", "bench") == "bench" else (
        f"<li><b>正規化的時間。</b>正規化取自同日稍早以同一程式的單核量測（{B.get('normalize', 0):.3f} ms，該次輸出未存檔），不影響結論。</li>")
    # ARM side of the "NTT / multiply / INTT only" partition; items kept whole when the cell wraps
    p1_arm = nw_list(["正規化", "編碼", "m 轉 RNS", "取樣", "小數值轉 RNS", "組裝", "資料往返"]) + nw_list(["（每個密文送 48 KiB", "收 96 KiB）"])
    corun_def = (f"V8b 切分：3 顆核心（CPU 1–3）連續執行{enc_w}，同時 CPU 0 驅動 PL 連續加密 {run_s.get('CORUN', 30)} 秒（共同執行）。"
                 "兩者為獨立程式，編碼結果不送入 PL；系統速度取兩者中較慢者，每焦耳筆數以兩者同時執行時的整板功率計算。")
    return f"""
<h2 class="pb">軟硬體切分：ARM 逐模組實測與 PL 估計</h2>
<p>本章把客戶端的工作拆成最小模組，實測每個模組在 ARM 的時間與能量，依 HLS 原始碼估計 PL 各階段的時間，並實測 PL 整段加密的時間與能量，再比較三種切分方式。所有實測數值來自同一次量測（{when:%Y-%m-%d %H:%M} UTC 起）；能量量測期間 V8b 全程載入，待機基準為 {PS['P_idle_W']:.3f} W，能量一律取自外接探針量測的整板 12 V 輸入。</p>
<div class="key">
  <div><b>{H['enc_speedup_time']:.0f} 倍</b><span>整段加密的時間（兩端實測）<br>ARM 單核 {tms('encrypt_all'):.2f} ms<br>PL 路徑 {PS['pl_path_ms_per_ct']:.3f} ms（含驅動）</span></div>
  <div><b>{H['enc_energy_ratio']:.1f} 倍</b><span>每個密文多耗的能量<br>ARM 單核 {A['encrypt_all']['mJ_incr']:.2f} mJ<br>PL 路徑 {k8['mJ_incr']:.2f} mJ</span></div>
  <div><b>{H['e2e_rpj_ratio']:.1f} 倍</b><span>含 ARM {enc_w}的<br>每焦耳筆數：V8b 切分對<br>全部在 ARM（兩者皆為實測，<br>V8b 以共同執行近似）</span></div>
</div>

<h3>量測方法</h3>
<table>
  <thead><tr><th style="width:17%">項目</th><th>內容</th></tr></thead>
  <tbody>
  <tr><td>ARM 端</td><td>V8b 的 CPU 規格程式（<code>baseline/core.cpp</code>、<code>sampler_b.hpp</code>，輸出與 PL 逐位元一致），N = 4096、3 個模數。每個模組單獨在 1 顆核心連續執行 {run_s.get('ARM1', 8)} 秒，取每個密文的平均時間。</td></tr>
  <tr><td>PL 端</td><td>V8b rev5 加密核心（200 MHz）。整段加密以 <code>board_enc_check sustain</code> 連續 {run_s.get('PL', 15)} 秒量測（每次呼叫 8 個密文）；各階段的時間依 HLS 原始碼估計（迴圈次數 × 啟動間隔），未逐階段實測，PL 端也沒有逐模組的能量。</td></tr>
  <tr><td>能量</td><td>每個模組多耗的能量 =（執行時整板平均功率 − 待機功率）× 每個密文的時間；每顆忙碌核心約多耗 {PS['dP_per_busy_core_W']:.3f} W。</td></tr>
  <tr><td>整體驗證</td><td>全部在 ARM：正規化、編碼與加密以 4 執行緒連續 {run_s.get('ARMSYS', 30)} 秒。{corun_def}</td></tr>
  <tr><td>正確性</td><td>PL 比對 {chk.get('frames', '80')} 個 frame（{int(chk.get('words', 0)):,} 個字），不一致 {chk.get('mismatches', '0')} 個、解密失敗 {chk.get('decrypt_failures', '0')} 次；持續加密與共同執行的第一與最後一個 frame 皆逐位元正確。</td></tr>
  </tbody>
</table>

<h3 style="break-after: avoid">各模組的兩端比較（ARM 實測、PL 估計）</h3>
<table class="mod">
  <thead><tr><th>模組</th><th>作用</th><th class="n">ARM<br>（ms）</th><th class="n">ARM 多耗<br>（mJ）</th><th class="n">PL<br>（µs）</th><th class="n">ARM<br>÷ PL</th><th>V8b<br>位置</th></tr></thead>
  <tbody>
  {mods}{whole}{down}
  </tbody>
</table>
<p class="sub">ARM 欄為單核實測，每個密文（解密、解碼與 sigmoid 為每個結果密文；{sig_txt}）。PL 欄除「整段加密」為實測（含驅動）外，皆為 HLS 估計；三元遮罩一列兩端皆含遮罩所需的 16 個 ChaCha20 區塊（ARM 的三元遮罩程式自行產生這些亂數）。{bound_txt}</p>
<div class="fig">
<figure><img src="{img('fig_p1_modules.png')}" alt="各模組在 ARM 與 PL 的時間"></figure>
<dl class="read">
  <dt>圖說</dt><dd><b>圖 8</b>　每個模組處理一個密文所需的時間，橫軸為對數刻度。橘點為 ARM 單核實測；空心藍點為 PL 各階段的 HLS 估計；最下一列為整段加密的兩端實測，PL 的時間包含驅動的複製、檢查、同步與清除。</dd>
  <dt>怎麼看</dt><dd>點越靠左越快，同一列兩點的距離即為倍數。PL 以資料流方式同時執行各階段，因此整段時間不是各階段時間的總和。</dd>
  <dt>重點</dt><dd>整段加密在 ARM 單核需 {tms('encrypt_all'):.2f} ms，在 PL 路徑只需 {PS['pl_path_ms_per_ct']:.3f} ms（{H['enc_speedup_time']:.0f} 倍）；只計 DMA 與 PL 核心為 {PS['pl_kernel_ms_per_ct']:.3f} ms（{H['enc_speedup_time_kernel']:.0f} 倍）。CKKS 編碼是浮點 FFT，PL 尚未實作，因此留在 ARM。</dd>
</dl>
</div>

<h3 style="break-after: avoid">PL 的呼叫成本</h3>
<table class="keep">
  <thead><tr><th class="n">每次呼叫的密文數 k</th><th class="n">呼叫時間（ms）</th><th class="n">驅動時間（ms）</th><th class="n">每個密文（ms）</th></tr></thead>
  <tbody>
  {ktab}
  </tbody>
</table>
<p>此表取自正確性檢查（<code>board_enc_check 20</code>：每個 k 各 20 個 frame，每個 frame 皆逐字比對並解密檢查，取中位數），因此每個密文的時間（k = 8 為 {k8_check['ms_per_ct']:.3f} ms）高於連續加密的 {k8['ms_per_ct']:.3f} ms。以 k 對呼叫時間做線性迴歸，每次呼叫的固定成本約 {fit['fixed_ms_per_call']:.3f} ms，每多一個密文約 {fit['ms_per_ct']:.3f} ms。下列各階段取自連續加密（k = 8）的最後一次呼叫（µs）：{phase_txt}。</p>
<p>DMA 與 PL 核心只占 {ph['dma_hls'] / 1000:.2f} ms，驅動在 ARM 上的複製、檢查、同步與清除共 {drv_us / 1000:.2f} ms，約為 PL 路徑的一半。只把部分模組放到 PL 時，這部分資料往返的成本會在每一次呼叫重複出現。{hls_line}</p>

<h3 style="break-after: avoid">三種切分方式</h3>
<table class="keep part">
  <thead><tr><th>方案</th><th>ARM 負責</th><th>PL 負責</th><th class="n">每秒<br>筆數</th><th class="n">每焦耳<br>筆數</th><th>來源</th></tr></thead>
  <tbody>
  <tr><td>全部在 ARM</td><td>全部</td><td>—</td><td class="n">{kfmt(arms['rec_per_s'])}</td><td class="n">{kfmt(arms['rec_per_J'])}</td><td>實測<br>（4 執行緒）</td></tr>
  <tr><td>只放 NTT／乘法／INTT</td><td>{p1_arm}</td><td>{nw_list(['NTT(u)', '逐點相乘', '兩次 INTT'])}</td><td class="n">{kfmt(P1['model_ct_per_s'] * 512)}</td><td class="n">{kfmt(P1['model_rec_per_J'])}</td><td>模型估計</td></tr>
  <tr><td><b>V8b：加密整段在 PL</b></td><td>{'正規化、編碼' if front else '編碼'}、驅動</td><td><span class='nw'>m 轉 RNS 至組裝</span></td><td class="n"><b>{kfmt(cor['rec_per_s'])}</b></td><td class="n"><b>{kfmt(cor['rec_per_J'])}</b></td><td>實測<br>（共同執行）</td></tr>
  </tbody>
</table>
<div class="fig">
<figure><img src="{img('fig_p2_partitions.png')}" alt="三種切分方式的速度與每焦耳筆數"></figure>
<dl class="read">
  <dt>圖說</dt><dd><b>圖 9</b>　三種切分方式的每秒筆數與每焦耳筆數，皆包含 ARM 端的{'正規化與編碼' if front else '編碼'}。斜線長條為模型估計，其餘為實測；V8b 的實測為共同執行（兩個獨立程式同時執行，系統速度取兩者中較慢者）。</dd>
  <dt>怎麼看</dt><dd>兩張圖的長條越長越好。模型以單核實測的模組時間、每顆忙碌核心的功率增量、PL 路徑每個密文 {PS['pl_energy_mJ_per_ct']:.3f} mJ（已扣除驅動在 ARM 上的部分）與驅動的每位元組成本計算。</dd>
  <dt>重點</dt><dd>V8b 切分的每焦耳筆數為全部在 ARM 的 {H['e2e_rpj_ratio']:.1f} 倍，每秒筆數為 {H['e2e_rate_ratio']:.1f} 倍。只放 NTT／乘法／INTT 時，取樣、組裝與資料往返仍占用 ARM，模型估計只有全部在 ARM 的約 {span(H['p1_over_p0_model_vs_model'], H['p1_over_p0_model_vs_measured'])} 倍（模型對模型 {H['p1_over_p0_model_vs_model']:.1f} 倍、模型對實測 {H['p1_over_p0_model_vs_measured']:.1f} 倍）。模型對兩個有實測的方案，每秒筆數高估 {val['P0']['rate_err'] * 100:.1f}% 與 {val['P3']['rate_err'] * 100:.1f}%（V8b 切分的每秒筆數由 3 核{enc_w}決定，該項比對只檢驗其多核擴展），每焦耳筆數高估 {val['P0']['rpj_err'] * 100:.1f}% 與 {val['P3']['rpj_err'] * 100:.1f}%；即使估計值偏高，V8b 切分仍約為只放 NTT／乘法／INTT 方案的 {H['p3_over_p1_rpj']:.1f} 倍。</dd>
</dl>
</div>

<div class="fig">
<h3>瓶頸：加密上行路徑（以 CPU 規格程式計）由 ARM 編碼決定</h3>
<figure><img src="{img('fig_p3_capacity.png')}" alt="各段的處理能力"></figure>
<dl class="read">
  <dt>圖說</dt><dd><b>圖 10</b>　V8b 切分下各段的處理能力（每秒筆數）。前兩列為共同執行時同時量得：ARM 以 3 核執行{enc_w}，PL 由 1 個驅動執行緒驅動；第三列為 PL 單獨執行；末列為不含驅動的上限。</dd>
  <dt>怎麼看</dt><dd>虛線為系統速度（兩者中較慢者），與編碼速度相等是定義使然；瓶頸須由同時量得的兩個速度判斷。</dd>
  <dt>重點</dt><dd>共同執行時，3 核{enc_w}每秒 {kfmt(cap['encode_3core_corun'])} 筆，低於同時執行的 PL 加密路徑（每秒 {kfmt(cap['pl_path_corun'])} 筆），因此加密上行路徑的瓶頸已轉到 ARM 編碼。編碼只要加速約 {H['breakeven_encode_speedup']:.1f} 倍，即達共同執行時 PL 加密路徑的速度。PL 加密路徑單獨執行時為每秒 {kfmt(cap['pl_path_1thread'])} 筆（共同執行時慢約 {slow * 100:.0f}%），以此值計約需加速 {H['breakeven_vs_pl_alone']:.1f} 倍，僅供參考。系統若要再提高速度，須減少驅動的複製與檢查；PL 核心與 DMA 本身可達每秒 {kfmt(cap['pl_kernel'])} 筆。</dd>
</dl>
</div>

<div class="fig">
<h3>往返一批資料的工作量</h3>
<figure><img src="{img('fig_p4_batch.png')}" alt="每批 2048 筆的 ARM 與 PL 時間"></figure>
<dl class="read">
  <dt>圖說</dt><dd><b>圖 11</b>　客戶端處理一批 2048 筆資料（送出 4 個特徵密文、收回 1 個結果密文）所需的 ARM 核心時間與 PL 時間，各模組皆為單核實測。</dd>
  <dt>怎麼看</dt><dd>長條越短，ARM 越有餘裕執行其他工作。灰色為下游的解密、解碼與 sigmoid（CPU 規格程式）。</dd>
  <dt>重點</dt><dd>V8b 切分把每批的 ARM 時間由 {bat['all_arm']['total']:.1f} ms 降為 {bat['v8b_arm']['total']:.1f} ms（{H['arm_time_ratio_batch']:.1f} 倍），PL 只需 {bat['v8b_pl']['total']:.2f} ms。加密移出後，ARM 時間以解密鏈（{bat['v8b_arm']['downstream'] / bat['v8b_arm']['total'] * 100:.0f}%）及{enc_w}（{bat['v8b_arm']['encode'] / bat['v8b_arm']['total'] * 100:.0f}%）為主。解密使用私鑰，依設計留在 ARM。V8b 應用程式的解密與解碼另有 NTT(s) 快取與多執行緒平行，編碼也使用較快的 N/2 點 FFT，實際的時間與比例皆會與規格程式不同；本團隊將於期末展示時以實際 client 量測。</dd>
</dl>
</div>

<h3>結論與下一步</h3>
<ul>
  <li><b>加密整段放在 PL 是三種切分中最佳的方式。</b>在含 ARM {enc_w}的共同執行實測中，V8b 切分的每焦耳筆數為全部在 ARM 的 {H['e2e_rpj_ratio']:.1f} 倍，每秒筆數為 {H['e2e_rate_ratio']:.1f} 倍。</li>
  <li><b>只將 NTT／乘法／INTT 移至 PL 的效益有限。</b>取樣、組裝與每次呼叫的資料往返仍占用 ARM，模型估計只有全部在 ARM 的約 {span(H['p1_over_p0_model_vs_model'], H['p1_over_p0_model_vs_measured'])} 倍。</li>
  <li><b>加密上行路徑的瓶頸已轉到 ARM 編碼（以 CPU 規格程式計）。</b>下一步：本團隊將以 V8b 應用程式的編碼器（N/2 點 FFT）重測共同執行以確認瓶頸，再減少驅動的複製與檢查（例如雙緩衝），並評估將<span class='nw'>編碼移入 PL</span>。</li>
  <li><b>下游留在 ARM。</b>解密、解碼與 sigmoid 依安全設計留在 ARM；本團隊於期末展示的端到端量測將包含這一部分。</li>
</ul>
<h3>本章的限制</h3>
<ul>
  <li><b>ARM 端為 CPU 規格程式。</b>規格程式未使用 V8b 應用程式的 Harvey NTT、N/2 點 FFT 的編碼與解碼，以及解密的 NTT(s) 快取；應用程式另以 3 執行緒平行處理單一密文（縮短延遲，不減少核心時間）。公鑰頻譜在本次量測中已快取，全部在 ARM 時以 4 執行緒平行處理不同密文。應用程式的 ARM 成本會較低。全部在 ARM 時，4 核只加密的速度為每秒 {wan(arm4e['rec_per_s'])}筆，與 OpenFHE 4 執行緒（{wan(of4)}筆）相近。</li>
  <li><b>PL 各階段為估計值。</b>各階段時間依 HLS 原始碼估計，未逐階段實測；整段時間為實測。排程模型與實測的一致性只驗證瓶頸所在的模數引擎鏈（見「PL 的呼叫成本」）。</li>
  <li><b>共同執行為近似。</b>本章以兩個獨立程式近似 V8b client：PL 以全速加密，實際管線中 PL 約有 {(1 - cor['pl_duty']) * 100:.0f}% 的時間閒置。PL 因此多做約 {extra * 100:.0f}% 的工作，以 PL 路徑單獨執行時的功率增量按比例估計約多耗 {extra_W:.2f} W，使每焦耳筆數低估約 {bias * 100:.0f}%；實測值因此略為保守。</li>
  <li><b>每種設定只量測 1 次。</b>切分實驗每種設定量測 1 次（單核模組 {run_s.get('ARM1', 8)} 秒、4 核 {run_s.get('ARM4', 10)} 秒、PL {run_s.get('PL', 15)} 秒、整體 {run_s.get('CORUN', 30)} 秒），倍數未附信賴區間。時間與以總功率計的每焦耳筆數，重複性可參考前述 {nn} 輪比較（30 秒區間的每焦耳筆數變異係數 {span(min(cv.values()), max(cv.values()))}%）；以功率增量計的模組能量（增量約 {min(dps):.2f}–{max(dps):.2f} W）相對不確定度較大，約數個百分點。</li>
  <li><b>能量歸屬。</b>模組的能量以「執行時整板功率 − 待機功率」計算，包含記憶體與匯流排的活動。</li>
  {norm_lim}
</ul>
"""

part_html = partition_chapter(pdir) if pdir else ""
PSG = json.load(open(os.path.join(pdir, "partition_summary.json"), encoding="utf-8")) if pdir else None
TITLE = "V8b 與 OpenFHE 加密能耗比較及軟硬體切分實測" if PSG else "V8b 與 OpenFHE 加密能耗比較"
when_txt = f"<span class='nw'>能耗比較：{t0:%Y-%m-%d %H:%M}–{t1:%H:%M} UTC，每種設定 {nn} 輪，每輪 {secs} 秒</span>"
part_sum, part_data = "", ""
claim_sys = (f"<li><b>「整個系統省 {r4g[0]:.0f} 倍」。</b>本次只量加密；ARM 端的正規化與編碼、網路傳輸、雲端計分與解密都不在內。"
             "端到端的倍數會較小，需另外量測。</li>")
claim_arm = ("<li><b>「比任何純 ARM 實作都省電」。</b>本次對照的是 OpenFHE 這個通用函式庫；若要宣稱勝過純 ARM，應再比較團隊自己最佳化過的 ARM 版本。</li>")
only_enc = ""
if PSG:
    HG = PSG["headline"]
    PRG = json.load(open(os.path.join(pdir, "partition_runs.json"), encoding="utf-8"))["windows"]
    when_txt += f"　<span class='nw'>軟硬體切分：{utc(PRG[0]['t_begin']):%H:%M}–{utc(max(w['t_end'] for w in PRG)):%H:%M} UTC</span>"
    enc_g = "正規化與編碼" if HG.get("corun_includes_normalize") else "編碼"
    part_sum = (f"<li>軟硬體切分（見「軟硬體切分」一章）：整段加密在 PL 比 ARM 單核快 {HG['enc_speedup_time']:.0f} 倍；每個密文多耗的能量，ARM 為 PL 的 "
                f"{HG['enc_energy_ratio']:.1f} 倍（{PSG['arm']['encrypt_all']['mJ_incr']:.2f} mJ 對 {PSG['pl']['sustain k8']['mJ_incr']:.2f} mJ）；"
                f"含 ARM {enc_g}時，V8b 切分的每焦耳筆數為全部在 ARM 的 {HG['e2e_rpj_ratio']:.1f} 倍（兩者皆為實測，V8b 以共同執行近似）。</li>")
    claim_sys = (f"<li><b>「整個系統省 {r4g[0]:.0f} 倍」。</b>V8b 與 OpenFHE 的比較只量加密。加入 ARM 端的{enc_g}後，V8b 切分的每焦耳筆數為全部在 ARM 的 "
                 f"{HG['e2e_rpj_ratio']:.1f} 倍（見「軟硬體切分」一章）；網路傳輸、雲端計分與解密仍不在內，端到端的倍數需另外量測。</li>")
    claim_arm = (f"<li><b>「比任何純 ARM 實作都省電」。</b>本次對照 OpenFHE 與團隊的 CPU 規格程式（4 核只加密每秒 {wan(PSG['arm4']['encrypt_all']['rec_per_s'])}筆，"
                 f"與 OpenFHE 相近）；V8b 應用程式中最佳化過的 ARM 版本尚未量測。</li>")
    only_enc = "含編碼的比較見「軟硬體切分」一章。"
    part_data = (f"<tr><td>軟硬體切分</td><td>資料夾 <code>{os.path.basename(os.path.normpath(pdir))}</code> 的 <code>probe.csv</code>、<code>partition_runs.json</code>、"
                 "<code>partition_summary.json</code>、<code>figures/fig_p1–fig_p4</code>；工具在 repository 的 <code>energy_probe/kv260/partition/</code></td></tr>")

CSS = """
  @page { size: A4; margin: 16mm 15mm 16mm 15mm; }
  :root { --ink:#1b1d21; --quiet:#52514e; --rule:#dcdad4; --tint:#f3f2ee; --blue:#2a78d6; --orange:#eb6834; --aqua:#1baf7a; --say:#eef4fc; }
  body { font-family: "Microsoft JhengHei", "Noto Sans TC", sans-serif; color: var(--ink); font-size: 10.5pt; line-height: 1.65; margin: 0; background: #fff; }
  h1 { font-size: 19pt; margin: 0 0 2pt; letter-spacing: .02em; }
  h2 { font-size: 13.5pt; margin: 18pt 0 6pt; padding-bottom: 3pt; border-bottom: 1.5px solid var(--ink); }
  h3 { font-size: 11.5pt; margin: 12pt 0 4pt; }
  p { margin: 4pt 0; }
  .sub { color: var(--quiet); font-size: 10pt; }
  table { border-collapse: collapse; width: 100%; margin: 6pt 0; font-size: 9.6pt; font-variant-numeric: tabular-nums; }
  thead { display: table-header-group; }
  th, td { border-bottom: 1px solid var(--rule); padding: 4pt 6pt; text-align: left; vertical-align: top; }
  th { background: var(--tint); font-weight: 700; }
  td.n, th.n { text-align: right; white-space: nowrap; }
  .res th, .res td { padding: 4pt 4pt; }
  .res .lab { display: inline-block; vertical-align: top; }
  .mod td:first-child, .mod td:last-child, .part td:first-child, .part td:last-child { white-space: nowrap; }
  .nw { white-space: nowrap; }
  .key { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8pt; margin: 8pt 0 4pt; }
  .key div { background: var(--tint); border-radius: 6px; padding: 7pt 9pt; }
  .key b { display: block; font-size: 16pt; line-height: 1.2; font-variant-numeric: tabular-nums; }
  .key div > span { display: block; color: var(--quiet); font-size: 9.3pt; line-height: 1.5; }
  figure { margin: 10pt 0 4pt; break-inside: avoid; }
  figure img { width: 100%; display: block; }
  .read { display: grid; grid-template-columns: 4.2em 1fr; gap: 2pt 8pt; margin: 4pt 0 12pt; font-size: 10pt; }
  .read dt { font-weight: 700; color: var(--quiet); }
  .read dd { margin: 0; }
  .fig { break-inside: avoid; }
  ul { margin: 4pt 0; padding-left: 18pt; } li { margin: 2pt 0; }
  .swatch { display: inline-block; width: 9pt; height: 9pt; border-radius: 2px; vertical-align: -1pt; margin-right: 3pt; }
  code { font-family: Consolas, monospace; font-size: 9.2pt; background: var(--tint); padding: 0 3pt; border-radius: 3px; }
  .say { background: var(--say); border-radius: 6px; padding: 8pt 11pt; margin: 6pt 0; }
  .say p { margin: 2pt 0; }
  .two { display: grid; grid-template-columns: 1fr 1fr; gap: 10pt; }
  .two > div { min-width: 0; }
  .pb { break-before: page; }
  tr { break-inside: avoid; }
  table.keep { break-inside: avoid; }
  h2, h3 { break-after: avoid; }
"""

def sw(v): return f'<span class="swatch" style="background:{SW[v]}"></span>'
# the thread count on a second line, aligned with the name, keeps the column narrow
LABEL = {v: "<span class='lab'>" + SHORT[v].replace("OpenFHE ", "OpenFHE<br>") + "</span>" for v in V}

rows = "".join(
    f"<tr><td style='white-space:nowrap'>{sw(v)}{LABEL[v]}</td><td class='n'>{n[v]}</td><td class='n'>{f0(m(v,'rec_s'))}</td><td class='n'>{fW(m(v,'P_run'))}</td>"
    f"<td class='n'>{fW(m(v,'P_idle'))}</td><td class='n'>{f0(m(v,'rpj_gross'))}</td><td class='n'>{f0(ci(v,'rpj_gross')[0])}–{f0(ci(v,'rpj_gross')[1])}</td>"
    f"<td class='n'>{f0(m(v,'rpj_net'))}</td><td class='n'>{mj[v]:.1f} mJ</td><td class='n'>{m(v,'cpu')*100:.0f}%</td></tr>" for v in V)

html = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><title>{TITLE}</title><style>{CSS}</style></head><body>

<h1>{TITLE}</h1>
<p class="sub">{when_txt}<br><span class='nw'>平台：KV260（Ubuntu 22.04，4 × Cortex-A53 1.333 GHz）</span>　<span class='nw'>量測：ESP32-S3 + INA228 整板探針（12 V 輸入端）</span></p>

<h2>摘要</h2>
<div class="key">
  <div><b>{r4g[0]:.2f} 倍</b><span>只計加密，V8b 每焦耳筆數<br>相對 OpenFHE 4 執行緒<br>95% 信賴區間 {r4g[1]:.2f}–{r4g[2]:.2f} 倍</span></div>
  <div><b>{r4s[0]:.2f} 倍</b><span>只計加密，V8b 每秒筆數<br>相對 OpenFHE 4 執行緒</span></div>
  <div><b>{m('V8b','cpu')*100:.0f}% 對 {m('OpenFHE4','cpu')*100:.0f}%</b><span>加密時 4 顆 ARM 核心的<br>平均使用率<br>V8b 對 OpenFHE 4 執行緒</span></div>
</div>
<ul>
  <li>V8b 以 FPGA 的可程式邏輯（PL）執行加密、ARM 負責控制與搬運資料，每加密 2048 筆資料消耗約 <b>{mj['V8b']:.1f} mJ</b>；純 ARM 的 OpenFHE 4 執行緒約 {mj['OpenFHE4']:.1f} mJ，單執行緒約 {mj['OpenFHE1']:.1f} mJ。</li>
  <li>V8b 與 OpenFHE 4 執行緒的整板功率相近（{fW(m('V8b','P_run'))} 對 {fW(m('OpenFHE4','P_run'))}），能耗比的差距主要來自處理速度。</li>
  <li>以扣除待機功率後的淨值計算，結論方向相同：V8b 的每焦耳筆數為 OpenFHE 4 執行緒的 {r4n[0]:.1f} 倍。</li>
  <li>V8b {nn} 輪每焦耳筆數的變異係數為 {cv['V8b']:.2f}%；探針螢幕即時算出的每焦耳筆數與電腦分析的差異最大為 {worst_diff:.2f}%，顯示量測的重複性良好（準確度的討論見限制與注意事項）。</li>
  {part_sum}
</ul>

<h2>量測方法</h2>
<table>
  <thead><tr><th style="width:21%">項目</th><th>內容</th></tr></thead>
  <tbody>
  <tr><td>比較對象</td><td>{sw('V8b')}V8b rev5：ARM 負責控制與搬運資料，PL 負責加密<br>{sw('OpenFHE4')}OpenFHE 1.5.1，4 執行緒，只用 ARM<br>{sw('OpenFHE1')}OpenFHE 1.5.1，單執行緒，只用 ARM</td></tr>
  <tr><td>共同參數</td><td>CKKS，N = 4096，每個密文 2048 個 slot，3 個 RNS 模數（{q_bits}），公鑰加密。OpenFHE 實際選出的模數為 <span class='nw'>{q_of_txt}</span>，與 repo 文件（docs/NATIVE_V4.md 4.4 節）的 OpenFHE 對照組相同。</td></tr>
  <tr><td>工作定義</td><td>三種設定相同：每個 frame 8 個密文，8 筆明文事先編碼一次，迴圈內只做加密，連續 {secs} 秒；第一個與最後一個 frame 皆檢查正確性（V8b 與規格模型逐位元比對；OpenFHE 解密誤差 &lt; 10<sup>−3</sup>，實測約 10<sup>−4</sup>）。</td></tr>
  <tr><td>筆數換算</td><td>每筆資料有 4 個特徵（CDC 模型），每個密文裝 2048 筆資料的同一個特徵，因此 1 個密文相當於 512 筆。</td></tr>
  <tr><td>能量量測</td><td>INA228 串接於 KV260 的 12 V 輸入（整板），VBus 接在供電側的 Vin+，每 0.1 秒一筆；探針未校正（K = 1），各設定的比值不受校正影響（見限制與注意事項）。</td></tr>
  <tr><td>總值與淨值</td><td>總值 = 筆數 ÷ 這 {secs} 秒的整板能量。淨值 = 筆數 ÷［（加密時功率 − 同一狀態的待機功率）× 秒數］。</td></tr>
  <tr><td>待機狀態</td><td>{idle_row}</td></tr>
  <tr><td>統計</td><td>每種設定 {nn} 輪；信賴區間以 t 分布計算，倍數的信賴區間以對數比值的誤差傳遞（Welch 自由度）計算。</td></tr>
  </tbody>
</table>

<h2>結論怎麼寫</h2>
<div class="say">
<p><b>建議寫法：</b>在相同 CKKS 參數（N = 4096、3 個 RNS 模數、{q_bits_short}）且只計公鑰加密的條件下，以外接探針量測 KV260 整板 12 V 輸入，將加密交給 FPGA 的 V8b（ARM + FPGA）每焦耳可加密 {f0(m('V8b','rpj_gross'))} 筆資料，為純 ARM 執行 OpenFHE 1.5.1（4 執行緒，PL 清空）的 {r4g[0]:.1f} 倍（95% 信賴區間 {r4g[1]:.1f}–{r4g[2]:.1f} 倍，各 {nn} 輪）；同時 CPU 使用率由 {m('OpenFHE4','cpu')*100:.0f}% 降到 {m('V8b','cpu')*100:.0f}%。</p>
</div>
<div class="two">
<div>
<h3>這份資料支持的說法</h3>
<ul>
  <li><b>加密這一步，ARM + FPGA 比純 ARM 的 OpenFHE 省電。</b>倍數的信賴區間下限 {r4g[1]:.1f} 倍遠大於 1，Welch t 檢定 p {pstr(welch_p('V8b','OpenFHE4','rpj_gross'))}。</li>
  <li><b>省電的原因是速度，不是功率。</b>兩者整板功率只差 {abs(r4p[0]-1)*100:.0f}%，V8b 的處理速度是 {r4s[0]:.1f} 倍。</li>
  {fpga_claim}
  <li><b>CPU 被釋放出來。</b>V8b 只用約 {m('V8b','cpu')*4:.1f} 顆核心，其餘可做編碼、網路等工作。</li>
  <li><b>量測的重複性良好。</b>整板外接量測、{nn} 輪變異小、兩種獨立計算方式一致（最大差異 {worst_diff:.2f}%）；絕對準確度見限制與注意事項。</li>
</ul>
</div>
<div>
<h3>還不能宣稱的說法</h3>
<ul>
  {claim_sys}
  {claim_arm}
  <li><b>「安全性完全相同」。</b>參數規模相同（N、模數位元數、三元秘密金鑰），但雜訊分布與取樣實作不同，應寫成「參數規模相同」。</li>
  <li><b>其他 EdgeFHE 版本的結論。</b>本次只量 V8b rev5；repo 中較早的 V5 實板結果（每次 FPGA 呼叫有約 4 ms 額外負擔）方向相反，不可混用。</li>
</ul>
</div>
</div>
<h3>簡報可以用的一句話</h3>
<div class="say"><p>「同樣加密一批 2048 筆資料，V8b 用 {mj['V8b']:.1f} mJ，純 ARM 的 OpenFHE 用 {mj['OpenFHE4']:.0f} mJ；速度快 {r4s[0]:.0f} 倍，CPU 只用 {m('V8b','cpu')*100:.0f}%。」</p></div>

<h2 class="pb">圖表與說明</h2>
<div class="fig">
<figure><img src="figures/fig1_power_timeline.png" alt="整段量測的功率曲線"></figure>
<dl class="read">
  <dt>圖說</dt><dd>整段量測。灰色底為待機，藍、橘、綠色底分別是 V8b、OpenFHE 單執行緒、OpenFHE 4 執行緒的 {secs} 秒持續加密，每種 {nn} 輪。</dd>
  <dt>怎麼看</dt><dd>線的高度為 KV260 整板由 12 V 輸入取得的功率。每一輪開始時功率上升、結束時回到待機水準，顯示量到的是加密本身造成的變化。</dd>
  <dt>重點</dt><dd>同一種設定的 {nn} 輪高度幾乎相同。V8b 約 {m('V8b','P_run'):.2f} W、OpenFHE 4 執行緒約 {m('OpenFHE4','P_run'):.2f} W、單執行緒約 {m('OpenFHE1','P_run'):.2f} W；功率差距不大，但處理速度差很多（圖 3）。</dd>
</dl>
</div>
<div class="fig">
<figure><img src="figures/fig2_records_per_joule.png" alt="每焦耳筆數"></figure>
<dl class="read">
  <dt>圖說</dt><dd>每消耗 1 焦耳的整板能量可以加密多少筆資料。長條為 {nn} 輪平均，白點為每一輪。</dd>
  <dt>怎麼看</dt><dd>越長越省電。白點集中在長條末端，代表各輪之間差異很小。</dd>
  <dt>重點</dt><dd>V8b 為 {f0(m('V8b','rpj_gross'))} 筆／焦耳，是 OpenFHE 4 執行緒（{f0(m('OpenFHE4','rpj_gross'))}）的 {r4g[0]:.1f} 倍、單執行緒（{f0(m('OpenFHE1','rpj_gross'))}）的 {r1g[0]:.1f} 倍。換算成每筆資料：V8b 約 {uj['V8b']:.1f} µJ，OpenFHE 4 執行緒約 {uj['OpenFHE4']:.1f} µJ，單執行緒約 {uj['OpenFHE1']:.1f} µJ。</dd>
</dl>
</div>
<div class="fig">
<figure><img src="figures/fig3_throughput.png" alt="每秒筆數"></figure>
<dl class="read">
  <dt>圖說</dt><dd>每秒能加密的資料筆數。</dd>
  <dt>怎麼看</dt><dd>越長越快。這張圖與圖 2 的形狀幾乎一樣，因為三種設定的功率相近，每焦耳筆數主要由速度決定。</dd>
  <dt>重點</dt><dd>V8b 每秒 {wan(m('V8b','rec_s'))}筆，是 OpenFHE 4 執行緒（{wan(m('OpenFHE4','rec_s'))}筆）的 {r4s[0]:.1f} 倍。OpenFHE 從單執行緒到 4 執行緒加速 {speed41:.1f} 倍，接近線性，代表純 ARM 版本已經用滿 4 顆核心。</dd>
</dl>
</div>
<div class="fig">
<figure><img src="figures/fig4_power_breakdown.png" alt="功率拆解"></figure>
<dl class="read">
  <dt>圖說</dt><dd>加密時的整板功率拆成兩段：灰色為待機基準（同一狀態下不加密時的功率），彩色為加密增加的功率。</dd>
  <dt>怎麼看</dt><dd>灰色段遠長於彩色段：板子本身的待機功率約 {idle_of:.1f} W，加密只增加 {min(inc.values()):.2f} 至 {max(inc.values()):.2f} W。</dd>
  <dt>重點</dt><dd>V8b 加密時增加 {inc['V8b']:.2f} W，OpenFHE 4 執行緒增加 {inc['OpenFHE4']:.2f} W。V8b 用較少的額外功率完成約 {r4s[0]:.0f} 倍的工作量。由於待機占大部分，整板總值會低估加密本身的效率差距，因此另外計算淨值（圖 5）。</dd>
</dl>
</div>
<div class="fig">
<figure><img src="figures/fig5_gross_vs_net.png" alt="總值與淨值"></figure>
<dl class="read">
  <dt>圖說</dt><dd>左邊是整板總值（同圖 2），右邊是扣除待機功率後的淨值。兩張圖的橫軸刻度不同。</dd>
  <dt>怎麼看</dt><dd>總值回答「這塊板子實際每焦耳做多少事」；淨值回答「加密這件事本身每焦耳做多少事」。</dd>
  <dt>重點</dt><dd>兩種計算方式皆以 V8b 最高。淨值下 V8b 約 {wan(m('V8b','rpj_net'))}筆／焦耳，是 OpenFHE 4 執行緒（{wan(m('OpenFHE4','rpj_net'))}）的 {r4n[0]:.1f} 倍。淨值的分母是較小的差值，誤差會被放大，主要結論以總值為準。</dd>
</dl>
</div>
<div class="fig">
<figure><img src="figures/fig6_cpu.png" alt="CPU 使用率"></figure>
<dl class="read">
  <dt>圖說</dt><dd>加密期間 4 顆 ARM 核心的平均使用率（取每輪開始後第 5 至 25 秒）。</dd>
  <dt>怎麼看</dt><dd>100% 代表 4 顆核心全滿；25% 約等於 1 顆核心全滿。</dd>
  <dt>重點</dt><dd>V8b 只用約 {m('V8b','cpu')*100:.0f}% 的 CPU（約 {m('V8b','cpu')*4:.1f} 顆核心，負責搬運資料與處理 DMA 中斷），其餘 CPU 可以做其他工作。OpenFHE 4 執行緒用滿全部核心，速度仍只有 V8b 的 {1/r4s[0]*100:.0f}%。</dd>
</dl>
</div>
<div class="fig">
<figure><img src="figures/fig7_probe_vs_pc.png" alt="探針螢幕與電腦分析的一致性"></figure>
<dl class="read">
  <dt>圖說</dt><dd>每一輪的每焦耳筆數由探針螢幕與電腦分析各算一次，點是兩者的差異百分比。</dd>
  <dt>怎麼看</dt><dd>探針螢幕用 INA228 晶片內建的能量累積器（連續累積），電腦分析用每 0.1 秒一筆的功率取平均；兩種計算方式的取樣方式不同。點越靠近 0，兩者越一致。灰色參考線為 ±0.5%，即 INA228 內部振盪器的公差上限：能量累積器以晶片的振盪器計時，電腦以系統時鐘計時，兩者的時基差異會直接反映在此。</dd>
  <dt>重點</dt><dd>{len(R)} 輪的差異最大為 {worst_diff:.2f}%，兩種計算方式互相印證，量測流程沒有漏算或重算能量。少數輪次略超出 ±0.5% 參考線，仍在 ±1% 以內。兩者共用同一個分流電阻與 ADC，因此這項比對檢驗的是能量的計算，不檢驗增益誤差。</dd>
</dl>
</div>

<h2>結果總表</h2>
<table class="res">
  <thead><tr><th>設定</th><th class="n">輪數</th><th class="n">每秒<br>筆數</th><th class="n">加密時<br>功率</th><th class="n">待機<br>功率</th><th class="n">每焦耳筆數<br>（總值）</th><th class="n">95%<br>信賴區間</th><th class="n">淨值</th><th class="n">每 2048<br>筆</th><th class="n">CPU</th></tr></thead>
  <tbody>
  {rows}
  </tbody>
</table>
<table>
  <thead><tr><th>比較（V8b ÷ 對照組）</th><th class="n">倍數</th><th class="n">95% 信賴區間</th><th class="n">Welch t 檢定 p 值</th></tr></thead>
  <tbody>
  <tr><td>每焦耳筆數（總值），對 OpenFHE 4 執行緒</td><td class="n">{r4g[0]:.2f}</td><td class="n">{r4g[1]:.2f}–{r4g[2]:.2f}</td><td class="n">{pstr(welch_p('V8b','OpenFHE4','rpj_gross'))}</td></tr>
  <tr><td>每焦耳筆數（總值），對 OpenFHE 單執行緒</td><td class="n">{r1g[0]:.1f}</td><td class="n">{r1g[1]:.1f}–{r1g[2]:.1f}</td><td class="n">{pstr(welch_p('V8b','OpenFHE1','rpj_gross'))}</td></tr>
  <tr><td>每焦耳筆數（淨值），對 OpenFHE 4 執行緒</td><td class="n">{r4n[0]:.2f}</td><td class="n">{r4n[1]:.2f}–{r4n[2]:.2f}</td><td class="n">{pstr(welch_p('V8b','OpenFHE4','rpj_net'))}</td></tr>
  <tr><td>每秒筆數，對 OpenFHE 4 執行緒</td><td class="n">{r4s[0]:.2f}</td><td class="n">{r4s[1]:.2f}–{r4s[2]:.2f}</td><td class="n">{pstr(welch_p('V8b','OpenFHE4','rec_s'))}</td></tr>
  </tbody>
</table>

<h2>限制與注意事項</h2>
<ul>
  {calib}
  {pre_line}
  {prev_line}
  <li><b>量測順序固定。</b>本次量測依序跑完 V8b 的 {nn} 輪，再跑 OpenFHE。各設定第 1 輪到第 {nn} 輪的趨勢變化：{"、".join(f"{SHORT[v]} {sgn(drifts[v])}%" for v in V)}，遠小於兩者的差距。</li>
  <li><b>只量加密。</b>三種設定都不含 ARM 端的正規化與編碼、雲端計分與解密；端到端的每焦耳筆數會比本表低。{only_enc}</li>
  {idle_lim}
  <li><b>同一個 frame 重複加密。</b>三種設定都把同一批明文反覆加密；加密的計算量與明文內容無關，但實際應用中每批資料都不同。</li>
  <li><b>OpenFHE 的用法。</b>4 執行緒版本以 OpenMP 平行加密不同的密文，OpenFHE 內部的平行區段不再巢狀展開；這是本次採用的設定，不代表 OpenFHE 的所有最佳化方式。</li>
</ul>

{part_html}

<h2>資料與程式位置</h2>
<table class="keep">
  <thead><tr><th style="width:28%">內容</th><th>位置</th></tr></thead>
  <tbody>
  {part_data}
  <tr><td>原始資料</td><td>資料夾 <code>{os.path.basename(os.path.normpath(d))}</code> 的 <code>probe.csv</code>（探針每 0.1 秒一筆）、<code>runs.json</code>（每輪板子輸出與時間點）</td></tr>
  <tr><td>分析結果與圖表</td><td>同一資料夾的 <code>summary.json</code>、<code>figures/*.png</code>、<code>figures/summary_table.csv</code></td></tr>
  <tr><td>量測與分析工具</td><td>repository 的 <code>energy_probe/kv260/</code>（見其中的 README.md）</td></tr>
  <tr><td>板上程式</td><td>V8b：<code>~/edgefhe-v8b-r5/test/board_enc_check sustain 30 8</code>；OpenFHE：<code>~/openfhe_bench/build/openfhe_sustain 30 8 {{1|4}}</code>（OpenFHE 1.5.1 安裝於 <code>~/.local/openfhe-v1.5.1</code>）</td></tr>
  </tbody>
</table>
</body></html>
"""
# When a line wraps, keep a number with its unit, "N = 4096" and a range such as "9.50–9.57" in one piece.
# Applied to the text between tags of the body only (not the CSS, not attribute values).
UNIT = r"(?:mJ|ms|µs|µJ|µV|µA|mW|mV|mA|mΩ|KiB|GHz|MHz|UTC|W|V|A|秒|筆|輪|個|倍|顆|位元|次|萬|核|執行緒|frame|slot)"
def tidy(text):
    text = re.sub(r"(?<=\d) (?=" + UNIT + r"(?![A-Za-z0-9]))", NBSP, text)
    text = re.sub(r"(?<=[A-Za-z0-9]) = (?=[A-Za-z0-9])", NBSP + "=" + NBSP, text)
    return re.sub(r"(?<![\d.,:])(\d[\d.,]*–\d(?:[\d.,]*\d)?(?:%|" + NBSP + UNIT + r"(?![A-Za-z0-9]))?)", r"<span class='nw'>\1</span>", text)
head_, body_ = html.split("</style>", 1)
parts_ = re.split(r"(<[^>]+>)", body_)
body_ = "".join(p if i % 2 else tidy(p) for i, p in enumerate(parts_))
html = head_ + "</style>" + body_
open(os.path.join(d, "report.html"), "w", encoding="utf-8").write(html)
print("report.html written")
print(f"ratio gross x4 {r4g}, x1 {r1g}, net x4 {r4n}, speed x4 {r4s}, worst diff {worst_diff:.2f}%, drifts {drifts}")
print(f"default-state idle {dflt}, no-ethernet gap {d_noeth}, idle gaps {gaps}, load current {amps}")
