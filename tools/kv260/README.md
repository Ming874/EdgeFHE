# KV260 序列埠工具（Windows PowerShell）

板子沒有網路時，電腦只能透過 USB 序列埠操作板子。這三支腳本讓電腦端可以送指令、傳檔、收檔，2026-10-14 第一次上板全程就是用它們完成的。完整上板流程見 [docs/BOARD_GUIDE.md](../../docs/BOARD_GUIDE.md)。

| 腳本 | 用途 |
|---|---|
| `serial.ps1` | 送一行文字或一個 shell 指令，等到指定輸出或指令結束標記，印出板子的回應 |
| `send_file.ps1` | 電腦 → 板子：以 base64 串流傳檔，最後比對 SHA-256（約 11.3 KiB/s） |
| `receive_file.ps1` | 板子 → 電腦：板子印出 base64，電腦解碼存檔並比對 SHA-256 |

## 找到主控台 / Find the console

USB 線接上後，Windows 會出現兩個「USB Serial Port」（例如 COM8、COM9）。對每個埠送一次 Enter，有回 `kria login:` 的就是 Linux 主控台（115200 8N1，板子端是 `ttyPS1`）：

```powershell
powershell -ExecutionPolicy Bypass -File tools\kv260\serial.ps1 -Port COM8 -Enter -Expect 'login:|\$ $' -TimeoutSec 5
```

## 常用操作 / Common use

```powershell
$S = "tools\kv260\serial.ps1"
# 登入：先送帳號，再送密碼（密碼只打在指令列，不要寫進任何檔案）
powershell -ExecutionPolicy Bypass -File $S -Send ubuntu -Enter -Expect 'Password:'
powershell -ExecutionPolicy Bypass -File $S -Send '<password>' -Enter -Expect '\$ $'
# 執行指令並等它結束（輸出最後有 <<EXIT n>>）
powershell -ExecutionPolicy Bypass -File $S -Command 'uname -a; nproc' -TimeoutSec 30
# 指令含引號、管線或 $ 時，寫進檔案再用 -CommandFile，避免 PowerShell 拆錯參數
powershell -ExecutionPolicy Bypass -File $S -CommandFile cmd.sh -TimeoutSec 600
# 板子卡在等輸入時送 Ctrl-C
powershell -ExecutionPolicy Bypass -File $S -Send ([string][char]3) -Expect '\$ $'
# 傳檔與收檔
powershell -ExecutionPolicy Bypass -File tools\kv260\send_file.ps1 -LocalFile build\kv260_serial_xxx.tar.gz -RemotePath /home/ubuntu/bundle.tgz
powershell -ExecutionPolicy Bypass -File tools\kv260\receive_file.ps1 -RemotePath /home/ubuntu/results.tgz -LocalFile results.tgz
```

要傳上板的檔案用 `python tools\pack_for_board.py --serial`（約 2 MB，傳約 4 分鐘）或 `--code-only`（約 150 KB，傳約 20 秒）產生。

## 注意 / Notes

- 需要 `sudo` 時，板子會印 `[sudo] password for ubuntu:`；用 `-Expect 'password for ubuntu:|__RC=\d+__'` 等它出現，再送密碼。校正時鐘（`sudo date`）之後 sudo 會再問一次密碼。
- 長時間的工作（編譯、階段 2 量測）用 `nohup ... > log 2>&1 &` 放背景，再用 `-Command 'tail log'` 查進度，主控台才不會被占住。
- `-Command`／`-CommandFile` 會在指令後面接 `; echo "__RC=$?__"`。所以結尾是 `&` 的背景指令後面要再接一個指令，例如 `... 2>&1 & sleep 3; pgrep -af run_comparison`；不然會變成 `&;`，造成語法錯誤。
- 等背景工作結束可以在板上跑迴圈，例如 `for i in $(seq 1 108); do grep -q TRACE_EXIT= log && break; sleep 5; done; tail -n 60 log`，搭配 `-TimeoutSec 570`。
- 拔電源前一定要 `sync && sudo poweroff`，等主控台出現 `reboot: Power down`。
- 同一時間只能有一個程式開著這個 COM 埠（例如 PuTTY 開著時，這些腳本會打不開）。
