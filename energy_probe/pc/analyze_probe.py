"""EdgeFHE 能耗探針：分析 probe_log.py 錄下的 CSV

用法：python analyze_probe.py data/idle.csv          只印統計
      python analyze_probe.py data/idle.csv out.png  另存電壓、電流、功率曲線圖
"""
import csv
import statistics as st
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

if len(sys.argv) < 2:
    sys.exit(__doc__)

rows = []
with open(sys.argv[1]) as f:
    reader = csv.reader(f)
    next(reader)
    for line in reader:
        if len(line) == 5:
            rows.append([float(x) for x in line])
if len(rows) < 2:
    sys.exit("資料不足兩筆")

n = len(rows)
t = [r[0] / 1000 for r in rows]
V = [r[1] for r in rows]
I = [r[2] for r in rows]
P = [r[3] / 1000 for r in rows]          # W
E = [r[4] for r in rows]
dt = [b - a for a, b in zip(t, t[1:])]

print(f"筆數 {n}，涵蓋 {t[-1] - t[0]:.2f} 秒")
print(f"取樣間隔：中位數 {st.median(dt) * 1000:.0f} ms，最短 {min(dt) * 1000:.0f}，最長 {max(dt) * 1000:.0f}，"
      f"超過 150 ms 的空檔 {sum(d > 0.15 for d in dt)} 處")
for name, xs, unit in (("電壓", V, "V"), ("電流", I, "mA"), ("功率", P, "W")):
    print(f"{name}：平均 {st.mean(xs):.4f} {unit}，標準差 {st.pstdev(xs):.4f}，最小 {min(xs):.4f}，最大 {max(xs):.4f}")

acc = E[-1] - E[0]                                                  # INA228 硬體累積
integ = sum((P[k] + P[k + 1]) / 2 * dt[k] for k in range(n - 1))    # 軟體梯形積分
ratio = acc / integ if integ else float("nan")
print(f"能量：INA228 累積 {acc:.3f} J；功率對時間積分 {integ:.3f} J；比值 {ratio:.4f}（差 {abs(1 - ratio) * 100:.2f}%）")

if len(sys.argv) > 2:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(3, 1, figsize=(8, 6), sharex=True)
    for ax, ys, label in zip(axes, (V, I, P), ("V (V)", "I (mA)", "P (W)")):
        ax.plot([x - t[0] for x in t], ys, lw=0.8)
        ax.set_ylabel(label)
        ax.grid(alpha=0.3)
    axes[-1].set_xlabel("time (s)")
    fig.tight_layout()
    fig.savefig(sys.argv[2], dpi=110)
    print(f"曲線圖：{sys.argv[2]}")
