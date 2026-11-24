"""Figures for the V8b vs OpenFHE records-per-joule measurement (PNG, for the report and the slides).
usage: python make_figs.py <data dir>   (needs runs.json, probe.csv, summary.json from analyze_compare.py)
writes figures/fig1 ... fig7 (numbered and titled, for the report), figures/summary_table.csv, and
figures/deck/fig1, fig2, fig4: slide versions without number or title (the slide title carries it), drawn at the
size of their slide slots (DECK_SLOT), so that the slide shows them at scale 1 and every text is at least 13 pt."""
import csv, json, os, statistics as st, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.colors import to_rgb
from matplotlib.legend_handler import HandlerTuple
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, MaxNLocator
from matplotlib.transforms import blended_transform_factory

d = sys.argv[1]
out = os.path.join(d, "figures"); out_deck = os.path.join(out, "deck"); os.makedirs(out_deck, exist_ok=True)
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

# Text sizes (pt) and marks. Report figures are scaled to the text width of the report. Deck figures are drawn at the
# size of the picture slot on the slide (inches), so their sizes are the sizes seen on the slide.
REP = {"base": 11, "small": 9.5, "value": 11, "bar": 0.2, "dot": 22, "lw": 1.0, "band": 0.22, "yticks": 6}
DECK = {"base": 14, "small": 13, "value": 14, "bar": 0.3, "dot": 40, "lw": 1.2, "band": 0.32, "yticks": 4}
DECK_SLOT = {"fig1": (11.40, 2.82), "fig2": (7.48, 3.12), "fig4": (7.48, 3.12)}
DECK_PAD = 0.06   # inches kept free around the content of a deck figure

runs = summ["runs"]
by = {v: [r for r in runs if r["version"] == v] for v in ORDER}
mean = lambda v, k: st.mean(r[k] for r in by[v])
sd = lambda v, k: st.stdev(r[k] for r in by[v]) if len(by[v]) > 1 else 0.0
nrep = len(by["V8b"])

def fmt(x):
    return f"{x / 1e6:.2f}M" if x >= 1e6 else (f"{x / 1e3:.1f}K" if x >= 1e4 else f"{x:,.0f}")

def compact(x, _=None):
    return f"{x / 1e6:g}M" if abs(x) >= 1e6 else (f"{x / 1e3:g}K" if abs(x) >= 1e4 else f"{x:,.0f}")

def dark(c, k=0.35):
    """Darker shade of a series colour, for text written in that colour (k = 0.35 keeps it at 4.5:1 or more on the
    light tint of the same colour, so the labels stay legible when projected)."""
    return tuple(x * (1 - k) for x in to_rgb(c))

def hbars(ax, key, xlabel, title=None, note=None, scale=1.0, labfmt=fmt, ticks=compact, z=REP):
    y = list(range(len(ORDER)))[::-1]
    top = max(mean(v, key) * scale for v in ORDER)
    for yi, v in zip(y, ORDER):
        m = mean(v, key) * scale
        ax.barh(yi, m, height=z["bar"], color=COL[v], zorder=2)
        dots = [r[key] * scale for r in by[v]]
        ax.scatter(dots, [yi] * len(dots), s=z["dot"], color=SURF, edgecolor=INK2, linewidth=0.8, zorder=3)
        ax.annotate(labfmt(m), xy=(max(dots), yi), xytext=(z["dot"] ** 0.5 / 2 + 4, 0), textcoords="offset points",
                    va="center", ha="left", color=INK, fontsize=z["value"], zorder=4)
    if ticks:
        ax.xaxis.set_major_formatter(FuncFormatter(ticks))
    ax.set_yticks(y); ax.set_yticklabels([NAME[v] for v in ORDER], color=INK)
    ax.set_xlabel(xlabel)
    if title:
        ax.set_title(title, loc="left", color=INK)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8, zorder=0); ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False); ax.tick_params(axis="y", length=0)
    ax.set_xlim(0, top * 1.18)
    if note:
        ax.text(0, -0.30, note, transform=ax.transAxes, fontsize=z["small"], color=INK2, va="top")

def save(fig, name):
    p = os.path.join(out, name); fig.savefig(p, dpi=160, bbox_inches="tight"); plt.close(fig); print("saved", p)

def save_deck(fig, name):
    """Keeps the exact canvas (the slot size): moves the subplot edges until everything drawn fits inside it."""
    w, h = fig.get_size_inches()
    for _ in range(6):
        fig.canvas.draw()
        bb = fig.get_tightbbox(fig.canvas.get_renderer())   # inches
        dl, dr, db, dt = DECK_PAD - bb.x0, w - DECK_PAD - bb.x1, DECK_PAD - bb.y0, h - DECK_PAD - bb.y1
        if max(abs(dl), abs(dr), abs(db), abs(dt)) < 0.002:
            break
        sp = fig.subplotpars
        fig.subplots_adjust(left=sp.left + dl / w, right=sp.right + dr / w, bottom=sp.bottom + db / h, top=sp.top + dt / h)
    fig.canvas.draw()
    bb = fig.get_tightbbox(fig.canvas.get_renderer())
    if bb.x0 < 0 or bb.y0 < 0 or bb.x1 > w or bb.y1 > h:
        print(f"WARNING deck/{name}: content extends beyond the {w:.2f} x {h:.2f} in canvas")
    p = os.path.join(out_deck, name); fig.savefig(p, dpi=200); plt.close(fig); print("saved", p)

# ---- fig 1: power timeline ----
def timeline(ax, z):
    t0 = rows[0][0]
    p = [r[4] / 1000 for r in rows]
    ax.plot([r[0] - t0 for r in rows], p, lw=z["lw"], color=INK2, zorder=3)
    for w in log["idle"]:
        ax.axvspan(w["start"] - t0, w["end"] - t0, color=IDLE, alpha=0.18, lw=0, zorder=1)
        ax.text((w["start"] + w["end"]) / 2 - t0, 1.02, "待機" + ("（V8b 已載入）" if w["version"] == "V8b" else "（PL 清空）"),
                transform=ax.get_xaxis_transform(), ha="center", va="bottom", fontsize=z["small"], color=INK2)
    span, count = {}, {}
    for r in log["runs"]:
        v = r["version"]; a = r["t_begin"] - t0; b = a + float(r["parsed"]["seconds"])
        ax.axvspan(a, b, color=COL[v], alpha=0.16, lw=0, zorder=1)
        s0, s1 = span.get(v, (a, b)); span[v] = (min(s0, a), max(s1, b)); count[v] = count.get(v, 0) + 1
    # instead of a legend, each block of runs is named inside it, in a free band above the highest reading
    lo, hi = min(p), max(p); bottom = lo - 0.04 * (hi - lo)
    ax.set_ylim(bottom, hi + z["band"] * (hi - lo))
    ax.set_yticks([t for t in MaxNLocator(z["yticks"], steps=[1, 2, 2.5, 5, 10]).tick_values(bottom, hi)
                   if bottom <= t <= hi])   # no tick or grid line in the band
    for v, (a, b) in span.items():
        ax.text((a + b) / 2, hi + 0.07 * (hi - lo), f"{NAME[v]}：{count[v]} 輪", ha="center", va="bottom",
                fontsize=z["small"], color=dark(COL[v]), zorder=4)
    ax.text(1.0, 1.02, "每個色塊為一輪 30 秒的持續加密", transform=ax.transAxes, ha="right", va="bottom",
            fontsize=z["small"], color=INK2)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8); ax.set_axisbelow(True)

fig, ax = plt.subplots(figsize=(10, 3.6))   # about as wide as the other figures after cropping, so it prints at their scale
timeline(ax, REP)
ax.set_xlabel("時間（秒）"); ax.set_ylabel("12 V 整板輸入功率（W）")
ax.set_title(f"圖 1　整段量測的整板功率：待機與三種設定各 {nrep} 輪", loc="left", color=INK, pad=22)
save(fig, "fig1_power_timeline.png")

# ---- fig 2: records per joule (gross) ----
fig, ax = plt.subplots(figsize=(9, 3.3))
v8 = mean("V8b", "rpj_gross")
hbars(ax, "rpj_gross", "筆／焦耳", title="圖 2　每焦耳筆數（整板總值，越高越好）",
      note=f"長條＝{nrep} 輪平均，白點＝每一輪。V8b 為 OpenFHE 4 執行緒的 {v8 / mean('OpenFHE4', 'rpj_gross'):.1f} 倍、"
           f"單執行緒的 {v8 / mean('OpenFHE1', 'rpj_gross'):.1f} 倍。")
save(fig, "fig2_records_per_joule.png")

# ---- fig 3: throughput ----
fig, ax = plt.subplots(figsize=(9, 3.3))
hbars(ax, "rec_s", "筆／秒", title="圖 3　每秒筆數（越高越好）",
      note=f"V8b 每秒 {fmt(mean('V8b', 'rec_s'))} 筆，為 OpenFHE 4 執行緒的 {mean('V8b', 'rec_s') / mean('OpenFHE4', 'rec_s'):.1f} 倍。")
save(fig, "fig3_throughput.png")

# ---- fig 4: power breakdown ----
def breakdown(ax, z, deck):
    y = list(range(len(ORDER)))[::-1]
    for yi, v in zip(y, ORDER):
        base = mean(v, "P_idle"); run = mean(v, "P_run"); inc = run - base
        ax.barh(yi, base, height=z["bar"], color=IDLE, alpha=0.55, zorder=2)
        ax.barh(yi, inc, left=base + 0.03, height=z["bar"], color=COL[v], zorder=2)
        lab = f"{run:.2f} W（+{inc:.2f}）" if deck else f"{run:.2f} W（待機 {base:.2f} + 加密 {inc:.2f}）"
        ax.text(run + 0.05, yi, " " + lab, va="center", color=INK, fontsize=z["value"] if deck else 10.5)
    ax.set_yticks(y); ax.set_yticklabels([NAME[v] for v in ORDER], color=INK)
    ax.set_xlim(0, max(mean(v, "P_run") for v in ORDER) * (1.40 if deck else 1.55))
    ax.set_xlabel("整板功率（W）")
    ax.xaxis.grid(True, color=GRID, linewidth=0.8, zorder=0); ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False); ax.tick_params(axis="y", length=0)
    # the coloured segments share one legend entry, whose swatch is split into the three series colours
    handles = [Patch(color=IDLE, alpha=0.55, lw=0), tuple(Patch(color=COL[v], lw=0) for v in ORDER)]
    labels = ["待機基準（同一狀態下不加密時的功率）", "彩色 = 加密增加的功率"]
    kw = {"handler_map": {tuple: HandlerTuple(ndivide=None, pad=0)}, "frameon": False, "ncol": 2, "fontsize": z["small"]}
    if deck:   # one row above the plot, starting at the left edge of the figure (it is wider than the plot area)
        ax.legend(handles, labels, loc="lower left", bbox_to_anchor=(DECK_PAD / ax.figure.get_figwidth(), 1.05), borderaxespad=0,
                  bbox_transform=blended_transform_factory(ax.figure.transFigure, ax.transAxes), borderpad=0,
                  handlelength=2.2, columnspacing=1.4, **kw)
    else:
        ax.legend(handles, labels, loc="lower left", bbox_to_anchor=(0, -0.42), handlelength=2.6, **kw)

fig, ax = plt.subplots(figsize=(9, 3.3))
breakdown(ax, REP, False)
ax.set_title("圖 4　加密時的整板功率 = 待機基準 + 加密增加的功率", loc="left", color=INK)
save(fig, "fig4_power_breakdown.png")

# ---- fig 5: gross vs net, small multiples (one column of row labels, so it prints at the scale of the other figures) ----
fig, axs = plt.subplots(1, 2, figsize=(9.2, 3.3), sharey=True, gridspec_kw={"wspace": 0.28})
hbars(axs[0], "rpj_gross", "筆／焦耳", title="整板總值")
hbars(axs[1], "rpj_net", "筆／焦耳", title="淨值（扣除待機功率）")
axs[1].tick_params(axis="y", labelleft=False)
best_g = max(ORDER, key=lambda v: mean(v, "rpj_gross")); best_n = max(ORDER, key=lambda v: mean(v, "rpj_net"))
fig.suptitle("圖 5　總值與淨值：" + (f"兩種計算方式皆以 {NAME[best_g]}最高" if best_g == best_n else "兩種計算方式的排名不同"),
             x=axs[0].get_position().x0, ha="left", fontsize=13, fontweight="bold", color=INK, y=1.06)
save(fig, "fig5_gross_vs_net.png")

# ---- fig 6: CPU utilization ----
fig, ax = plt.subplots(figsize=(9, 3.3))
hbars(ax, "cpu", "CPU 使用率（%）", title="圖 6　加密時 4 顆 ARM 核心的平均使用率", scale=100, labfmt=lambda x: f"{x:.0f}%", ticks=None)
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
ax.set_xlabel("探針螢幕算出的每焦耳筆數相對於電腦分析的差異（%）")
worst = max(abs(x) for x in yv)
bound = next((b for b in (0.5, 1, 2, 5) if worst < b), round(worst + 0.5))
ax.set_title(f"圖 7　探針螢幕與電腦分析的一致性：每一輪皆在 ±{bound:g}% 以內（最大 {worst:.2f}%）", loc="left", color=INK, pad=16)
ax.xaxis.grid(True, color=GRID, linewidth=0.8, zorder=0); ax.set_axisbelow(True)
ax.spines["left"].set_visible(False); ax.tick_params(axis="y", length=0)
save(fig, "fig7_probe_vs_pc.png")

# ---- slide versions of fig 1, 2 and 4 (no number, no title; drawn at the slot size) ----
with plt.rc_context({"font.size": DECK["base"]}):
    fig, ax = plt.subplots(figsize=DECK_SLOT["fig1"])
    timeline(ax, DECK)
    ax.set_xlabel("時間（秒）"); ax.set_ylabel("整板功率（W）")
    save_deck(fig, "fig1_power_timeline.png")

    fig, ax = plt.subplots(figsize=DECK_SLOT["fig2"])
    hbars(ax, "rpj_gross", "每焦耳筆數（整板總值）", z=DECK)
    ax.xaxis.set_major_locator(MaxNLocator(5))
    ax.set_xlabel(ax.get_xlabel(), loc="left")   # the note takes the right end of the same line
    ax.annotate(f"長條＝{nrep} 輪平均，白點＝每一輪", xy=(1, 0.5), xycoords=(ax.transAxes, ax.xaxis.label),
                ha="right", va="center", fontsize=DECK["small"], color=INK2)
    save_deck(fig, "fig2_records_per_joule.png")

    fig, ax = plt.subplots(figsize=DECK_SLOT["fig4"])
    breakdown(ax, DECK, True)
    save_deck(fig, "fig4_power_breakdown.png")

# ---- table for slides / Excel ----
with open(os.path.join(out, "summary_table.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["設定", "輪數", "每秒筆數", "加密時整板功率W", "待機功率W", "每焦耳筆數_總值", "標準差", "每焦耳筆數_淨值", "CPU使用率%", "探針螢幕每焦耳筆數"])
    for v in ORDER:
        w.writerow([NAME[v], len(by[v]), round(mean(v, "rec_s")), round(mean(v, "P_run"), 3), round(mean(v, "P_idle"), 3),
                    round(mean(v, "rpj_gross")), round(sd(v, "rpj_gross")), round(mean(v, "rpj_net")),
                    round(mean(v, "cpu") * 100, 1), round(mean(v, "esp_rpj"))])
print("saved", os.path.join(out, "summary_table.csv"))
