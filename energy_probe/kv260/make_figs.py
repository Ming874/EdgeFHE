"""Figures for the V8b vs OpenFHE records-per-joule measurement (PNG, for slides).
usage: python make_figs.py <data dir>   (needs runs.json, probe.csv, summary.json from analyze_compare.py)"""
import csv, json, os, statistics as st, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter

d = sys.argv[1]
out = os.path.join(d, "figures"); os.makedirs(out, exist_ok=True)
log = json.load(open(os.path.join(d, "runs.json"), encoding="utf-8"))
summ = json.load(open(os.path.join(d, "summary.json"), encoding="utf-8"))
rows = [list(map(float, l.split(","))) for l in open(os.path.join(d, "probe.csv")).read().splitlines()[1:]]

# ---- tokens (reference palette, light mode) ----
SURF, INK, INK2, GRID, IDLE = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", "#9a9893"
COL = {"V8b": "#2a78d6", "OpenFHE1": "#eb6834", "OpenFHE4": "#1baf7a"}
NAME = {"V8b": "V8b（FPGA）", "OpenFHE1": "OpenFHE 單執行緒", "OpenFHE4": "OpenFHE 4 執行緒"}
ORDER = ["V8b", "OpenFHE4", "OpenFHE1"]
for f in ("Microsoft JhengHei", "Noto Sans TC", "Microsoft YaHei"):
    if any(f in x.name for x in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = f; break
plt.rcParams.update({"figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
                     "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
                     "text.color": INK, "font.size": 11, "axes.titlesize": 13, "axes.titleweight": "bold",
                     "axes.spines.top": False, "axes.spines.right": False, "axes.grid": False,
                     "axes.unicode_minus": False})

runs = summ["runs"]
by = {v: [r for r in runs if r["version"] == v] for v in ORDER}
mean = lambda v, k: st.mean(r[k] for r in by[v])
sd = lambda v, k: st.stdev(r[k] for r in by[v]) if len(by[v]) > 1 else 0.0

def fmt(x):
    return f"{x / 1e6:.2f}M" if x >= 1e6 else (f"{x / 1e3:.1f}K" if x >= 1e4 else f"{x:,.0f}")

def compact(x, _=None):
    return f"{x / 1e6:g}M" if abs(x) >= 1e6 else (f"{x / 1e3:g}K" if abs(x) >= 1e4 else f"{x:,.0f}")

def hbars(ax, key, title, xlabel, note=None, scale=1.0, labfmt=fmt, ticks=compact):
    y = list(range(len(ORDER)))[::-1]
    top = max(mean(v, key) * scale for v in ORDER)
    for yi, v in zip(y, ORDER):
        m = mean(v, key) * scale
        ax.barh(yi, m, height=0.2, color=COL[v], zorder=2)
        dots = [r[key] * scale for r in by[v]]
        ax.scatter(dots, [yi] * len(dots), s=22, color=SURF, edgecolor=INK2, linewidth=0.8, zorder=3)
        ax.text(max(dots) + top * 0.02, yi, labfmt(m), va="center", ha="left", color=INK, fontsize=11, zorder=4)
    if ticks:
        ax.xaxis.set_major_formatter(FuncFormatter(ticks))
    ax.set_yticks(y); ax.set_yticklabels([NAME[v] for v in ORDER], color=INK)
    ax.set_xlabel(xlabel); ax.set_title(title, loc="left", color=INK)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8, zorder=0); ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False); ax.tick_params(axis="y", length=0)
    ax.set_xlim(0, max(mean(v, key) * scale for v in ORDER) * 1.18)
    if note:
        ax.text(0, -0.30, note, transform=ax.transAxes, fontsize=9.5, color=INK2, va="top")

def save(fig, name):
    p = os.path.join(out, name); fig.savefig(p, dpi=160, bbox_inches="tight"); plt.close(fig); print("saved", p)

# ---- fig 1: power timeline ----
fig, ax = plt.subplots(figsize=(11, 4.0))
t0 = rows[0][0]
ax.plot([r[0] - t0 for r in rows], [r[4] / 1000 for r in rows], lw=1.0, color=INK2, zorder=3)
for w in log["idle"]:
    ax.axvspan(w["start"] - t0, w["end"] - t0, color=IDLE, alpha=0.18, lw=0, zorder=1)
    ax.text((w["start"] + w["end"]) / 2 - t0, 1.02, "待機" + ("（V8b 已載入）" if w["version"] == "V8b" else "（PL 清空）"),
            transform=ax.get_xaxis_transform(), ha="center", fontsize=9.5, color=INK2)
seen = set()
for r in log["runs"]:
    a = r["t_begin"] - t0; b = a + float(r["parsed"]["seconds"])
    ax.axvspan(a, b, color=COL[r["version"]], alpha=0.16, lw=0, zorder=1,
               label=None if r["version"] in seen else NAME[r["version"]] + "（30 秒持續加密）")
    seen.add(r["version"])
ax.set_xlabel("時間（秒）"); ax.set_ylabel("12 V 整板輸入功率（W）")
ax.set_title(f"圖 1　整段量測的整板功率：待機與三種設定各 {len(by['V8b'])} 輪", loc="left", color=INK, pad=22)
ax.yaxis.grid(True, color=GRID, linewidth=0.8); ax.set_axisbelow(True)
ax.legend(loc="upper left", bbox_to_anchor=(0, -0.16), ncol=3, frameon=False, fontsize=10)
save(fig, "fig1_power_timeline.png")

# ---- fig 2: records per joule (gross) ----
fig, ax = plt.subplots(figsize=(9, 3.3))
v8 = mean("V8b", "rpj_gross")
hbars(ax, "rpj_gross", "圖 2　每焦耳處理筆數（整板總值，越高越好）", "筆／焦耳",
      note=f"長條＝{len(by['V8b'])} 輪平均，白點＝每一輪。V8b 是 OpenFHE 4 執行緒的 {v8 / mean('OpenFHE4', 'rpj_gross'):.1f} 倍、"
           f"單執行緒的 {v8 / mean('OpenFHE1', 'rpj_gross'):.1f} 倍。")
save(fig, "fig2_records_per_joule.png")

# ---- fig 3: throughput ----
fig, ax = plt.subplots(figsize=(9, 3.3))
hbars(ax, "rec_s", "圖 3　每秒處理筆數（越高越好）", "筆／秒",
      note=f"V8b 每秒 {fmt(mean('V8b', 'rec_s'))} 筆，是 OpenFHE 4 執行緒的 {mean('V8b', 'rec_s') / mean('OpenFHE4', 'rec_s'):.1f} 倍。")
save(fig, "fig3_throughput.png")

# ---- fig 4: power breakdown ----
fig, ax = plt.subplots(figsize=(9, 3.3))
y = list(range(len(ORDER)))[::-1]
for yi, v in zip(y, ORDER):
    base = mean(v, "P_idle"); run = mean(v, "P_run"); inc = run - base
    ax.barh(yi, base, height=0.2, color=IDLE, alpha=0.55, zorder=2)
    ax.barh(yi, inc, left=base + 0.03, height=0.2, color=COL[v], zorder=2)
    ax.text(run + 0.05, yi, f" {run:.2f} W（待機 {base:.2f} + 加密 {inc:.2f}）", va="center", color=INK, fontsize=10.5)
ax.set_yticks(y); ax.set_yticklabels([NAME[v] for v in ORDER], color=INK)
ax.set_xlim(0, max(mean(v, "P_run") for v in ORDER) * 1.55)
ax.set_xlabel("整板功率（W）"); ax.set_title("圖 4　加密時的功率 = 待機底數 + 加密多出來的部分", loc="left", color=INK)
ax.xaxis.grid(True, color=GRID, linewidth=0.8, zorder=0); ax.set_axisbelow(True)
ax.spines["left"].set_visible(False); ax.tick_params(axis="y", length=0)
ax.legend(handles=[Patch(color=IDLE, alpha=0.55, label="待機底數（同一狀態下不加密時的功率）"),
                   Patch(color=INK2, label="彩色＝加密時多出來的功率")],
          loc="lower left", bbox_to_anchor=(0, -0.42), ncol=2, frameon=False, fontsize=9.5)
save(fig, "fig4_power_breakdown.png")

# ---- fig 5: gross vs net, small multiples ----
fig, axs = plt.subplots(1, 2, figsize=(12, 3.3), gridspec_kw={"wspace": 0.55})
hbars(axs[0], "rpj_gross", "整板總值", "筆／焦耳")
hbars(axs[1], "rpj_net", "淨值（扣掉待機功率）", "筆／焦耳")
best_g = max(ORDER, key=lambda v: mean(v, "rpj_gross")); best_n = max(ORDER, key=lambda v: mean(v, "rpj_net"))
fig.suptitle("圖 5　總值與淨值：" + (f"兩種算法都是 {NAME[best_g]} 最高" if best_g == best_n else "兩種算法的排名不同"), x=0.06, ha="left", fontsize=13, fontweight="bold", color=INK, y=1.04)
save(fig, "fig5_gross_vs_net.png")

# ---- fig 6: CPU utilization ----
fig, ax = plt.subplots(figsize=(9, 3.3))
hbars(ax, "cpu", "圖 6　加密時 4 顆 ARM 核心的平均使用率", "CPU 使用率（%）", scale=100, labfmt=lambda x: f"{x:.0f}%", ticks=None)
ax.set_xlim(0, 118)
save(fig, "fig6_cpu.png")

# ---- fig 7: probe screen vs PC analysis ----
fig, ax = plt.subplots(figsize=(9, 3.3))
yv = []
for i, v in enumerate(ORDER):
    for j, r in enumerate(by[v]):
        diff = (r["esp_rpj"] / r["rpj_gross"] - 1) * 100
        n = len(by[v]); step = min(0.18, 0.6 / max(n - 1, 1))
        yi = len(ORDER) - 1 - i + (j - (n - 1) / 2) * step
        ax.scatter([diff], [yi], s=40, color=COL[v], edgecolor=SURF, linewidth=1.5, zorder=3)
        yv.append(diff)
ax.axvline(0, color=INK2, lw=1)
for xv in (-0.5, 0.5):
    ax.axvline(xv, color=IDLE, lw=1.2, zorder=1)
ax.text(0.5, 1.0, " ±0.5%", transform=ax.get_xaxis_transform(), color=INK2, fontsize=9.5, va="bottom")
ax.set_yticks(range(len(ORDER))[::-1]); ax.set_yticklabels([NAME[v] for v in ORDER], color=INK)
lim = max(1.0, max(abs(x) for x in yv) * 1.4); ax.set_xlim(-lim, lim)
ax.set_xlabel("探針螢幕算出的每焦耳筆數 相對 電腦分析 的差異（%）")
worst = max(abs(x) for x in yv)
ax.set_title(f"圖 7　探針螢幕與電腦分析的一致性：每一輪都在 ±{0.5 if worst < 0.5 else 1:g}% 內（最大 {worst:.2f}%）", loc="left", color=INK, pad=16)
ax.xaxis.grid(True, color=GRID, linewidth=0.8, zorder=0); ax.set_axisbelow(True)
ax.spines["left"].set_visible(False); ax.tick_params(axis="y", length=0)
save(fig, "fig7_probe_vs_pc.png")

# ---- table for slides / Excel ----
with open(os.path.join(out, "summary_table.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["設定", "輪數", "每秒筆數", "加密時整板功率W", "待機功率W", "每焦耳筆數_總值", "標準差", "每焦耳筆數_淨值", "CPU使用率%", "探針螢幕每焦耳筆數"])
    for v in ORDER:
        w.writerow([NAME[v], len(by[v]), round(mean(v, "rec_s")), round(mean(v, "P_run"), 3), round(mean(v, "P_idle"), 3),
                    round(mean(v, "rpj_gross")), round(sd(v, "rpj_gross")), round(mean(v, "rpj_net")),
                    round(mean(v, "cpu") * 100, 1), round(mean(v, "esp_rpj"))])
print("saved", os.path.join(out, "summary_table.csv"))
