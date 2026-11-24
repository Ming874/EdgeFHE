# EdgeFHE 整板能耗探針

本探針外接在 KV260 的 12 V 輸入端，量測整塊板子的輸入電壓、電流、功率與能量，並在每一輪加密結束時算出「每焦耳筆數」。探針與受測的 KV260 彼此獨立：板上的 INA260 只量 SOM 的 5 V 供電軌（約為整板的一半），本探針量的是整板。

| 元件 | 規格 |
|---|---|
| 電流感測 | INA228 模組（分流電阻 15 mΩ，I²C 位址 0x40），高側串接 12 V 正極，VBus 接 Vin+（在分流電阻上游的供電側量測電壓） |
| 微控制器 | ESP32-S3 N16R8 |
| 顯示 | 2.4 吋 ILI9341（8 腳：GND VCC SCL SDA RES DC CS BL；SCL／SDA 屬 SPI） |

接線、程式、校正與疑難排解的完整說明見[實作手冊](manual/probe_manual.pdf)（原始檔 [probe_manual.html](manual/probe_manual.html)）。

## 資料夾

| 路徑 | 內容 |
|---|---|
| [firmware/probe/probe.ino](firmware/probe/probe.ino) | ESP32-S3 韌體：每 0.1 秒輸出 CSV、顯示面板、序列埠指令 |
| [pc/probe_log.py](pc/probe_log.py) | 電腦端記錄：歸零、記錄指定秒數、存成 CSV、印出平均值 |
| [pc/analyze_probe.py](pc/analyze_probe.py) | 分析一份 CSV：取樣間隔、統計、能量累積器與功率積分是否一致 |
| [kv260/](kv260/README.md) | 經 KV260 主控台自動量測 V8b 與 OpenFHE 的每焦耳筆數，並產生圖表與報告 |
| [kv260/partition/](kv260/partition/README.md) | 軟硬體切分：逐模組實測 ARM 的時間與能量，實測 FPGA 的可程式邏輯（PL）整段加密並依 HLS 原始碼估計 PL 各階段，比較不同的切分方式 |
| [openfhe_bench/](openfhe_bench/openfhe_sustain.cpp) | 板上 OpenFHE 1.5.1 的持續加密程式（與 V8b 相同的工作定義） |

## 快速開始

1. **燒錄韌體**：Arduino IDE 2 安裝 esp32 core（3.3.12）與 Adafruit INA228 Library 3.0.0、Adafruit ILI9341、Adafruit GFX；開發板選 ESP32S3 Dev Module、Flash Size 16MB、PSRAM OPI PSRAM；開啟 `firmware/probe/probe.ino` 上傳。
2. **接線**：I²C SDA 17、SCL 18；SPI SCK 12、MOSI 11、CS 10、DC 14、RES 4；電源路徑見手冊第 3 章。
3. **記錄**：`python pc/probe_log.py`（列出序列埠），`python pc/probe_log.py COM5 30 idle.csv`（記錄 30 秒）。
4. **分析**：`python pc/analyze_probe.py idle.csv idle.png`。

## 序列埠格式

- 輸出：每 0.1 秒一行 `t_ms,V,I_mA,P_mW,E_J`；以 `#` 開頭的行是訊息。
- 指令（每行以換行結尾）：

| 指令 | 作用 |
|---|---|
| `S<文字>` | 更新標題下方的狀態列 |
| `B` | 一輪開始：能量累積器歸零 |
| `E<列>,<名稱>,<筆數>` | 一輪結束：筆數 ÷ 這一輪的能量，顯示在螢幕底部第 0、1 或 2 行，並輸出 `# result …` |
| `r`（單一字元） | 能量歸零 |

電腦開啟序列埠時請把 DTR 與 RTS 設為 False；切換這兩條線可能讓 ESP32 停在重置狀態。

## 注意

- 探針未以參考儀器校正（`K_V = K_I = 1`）。本團隊原本計畫校正探針，但可取得的參考儀器都沒有比 INA228 準確：實驗室三用電表與探針的電壓讀值相差 0.58%，約為 INA228 電壓誤差上限（TI SLYS021A：±0.05%＋2.5 mV，12.14 V 時約 0.07%）的 8 倍，因此此差距來自三用電表本身的準確度或量測點不同，而非 INA228。模組上的分流電阻（R015）未標示公差，探針絕對值的不確定度因此無法由規格書界定（若以 1% 公差估計，約為 1%）；VBus 接在分流電阻上游的 Vin+，量得的功率另含分流電阻與導線的少量損耗。以三用電表求出的修正係數將引入其本身的誤差，因此本團隊未套用修正係數。V8b 與 OpenFHE 以同一支探針、在同一次量測中、以相同的 30 秒區間定義量測，增益誤差（含分流電阻公差）在每焦耳筆數的比值中抵消，偏移與非線性皆低於 0.01%，因此 9.54 倍的結論不受影響（詳見[實作手冊](manual/probe_manual.pdf)第 5 章）。
- 2026-11-23 的量測結果與引用注意事項見 [docs/2026-11-23_team_context.md](../docs/2026-11-23_team_context.md)。
