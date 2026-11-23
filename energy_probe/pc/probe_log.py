"""EdgeFHE 能耗探針：電腦端記錄腳本（手冊第 4.5 節）

用法：python probe_log.py COM5 30 idle.csv
  COM5      換成 ESP32 的序列埠
  30        記錄秒數
  idle.csv  輸出檔名

不加任何參數執行時，會列出這台電腦目前的序列埠。
"""
import sys
import time

import serial
from serial.tools import list_ports

# 這台 Windows 的預設編碼不是 UTF-8，先統一成 UTF-8，中文輸出才不會出錯
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

if len(sys.argv) != 4:
    print(__doc__)
    print("目前的序列埠：")
    for p in list_ports.comports():
        print(f"  {p.device}  {p.description}")
    sys.exit(1)

port, secs, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
rows = []
with serial.Serial(port, 115200, timeout=1) as s, open(out, "w") as f:
    time.sleep(2)
    s.reset_input_buffer()
    s.write(b"r")                      # 能量歸零
    time.sleep(0.3)
    f.write("t_ms,V,I_mA,P_mW,E_J\n")
    t0 = time.time()
    while time.time() - t0 < secs:
        line = s.readline().decode(errors="ignore").strip()
        parts = line.split(",")
        if line.startswith("#") or len(parts) != 5:
            continue
        f.write(line + "\n")
        rows.append([float(x) for x in parts])

if not rows:
    sys.exit("沒有收到資料：請確認序列埠名稱，以及 ESP32 已上傳 4.3 節的程式")

n = len(rows)


def avg(k):
    return sum(r[k] for r in rows) / n


print(f"筆數 {n}｜平均電壓 {avg(1):.4f} V｜平均電流 {avg(2):.2f} mA｜"
      f"平均功率 {avg(3) / 1000:.4f} W｜累積能量 {rows[-1][4]:.3f} J")
