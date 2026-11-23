# V8b 與 OpenFHE 每焦耳筆數量測

電腦同時連接探針（ESP32，預設 COM5）與 KV260 主控台（USB 序列埠，預設 COM6，115200），自動完成載入、待機量測、持續加密與還原，最後產生圖表與報告。

## 需要

- 電腦：Python 3，套件 `pyserial`、`matplotlib`、`scipy`；Arduino IDE 的序列埠監控窗必須關閉。
- 探針：已燒錄 [../firmware/probe/probe.ino](../firmware/probe/probe.ino)。
- KV260：
  - V8b 板上套件位於 `~/edgefhe-v8b-r5`（含 `scripts/02_load_pl.sh`、`04_insmod.sh`、`99_unload.sh` 與 `test/board_enc_check`）。
  - OpenFHE 1.5.1 安裝於 `~/.local/openfhe-v1.5.1`（依 [v8b_rev5/docs/DEPENDENCIES_zh-TW.md](../../v8b_rev5/docs/DEPENDENCIES_zh-TW.md)）。
  - 把 [../openfhe_bench/](../openfhe_bench/openfhe_sustain.cpp) 複製到板上 `~/openfhe_bench`，執行 `cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER=g++-11 -DOpenFHE_DIR=$HOME/.local/openfhe-v1.5.1/lib/OpenFHE && cmake --build build`。

## 執行

```bat
set KV_PW=<KV260 的 ubuntu 密碼>
python enc_compare_v8b_ofhe.py <輸出資料夾> 10
python analyze_compare.py <輸出資料夾>
python make_figs.py <輸出資料夾>
python make_report.py <輸出資料夾>
```

- `enc_compare_v8b_ofhe.py`：載入 V8b、量 30 秒待機與 N 輪 30 秒持續加密；卸載後在 PL 清空的狀態下量 OpenFHE 單執行緒與 4 執行緒各 N 輪；最後還原成 `k26-starter-kits`。每 10 輪約 18 分鐘。密碼只從環境變數讀取，不寫進任何檔案。
- `analyze_compare.py`：每一輪的總值與淨值每焦耳筆數，產生 `summary.json`。
- `make_figs.py`：7 張圖與 `figures/summary_table.csv`。
- `make_report.py`：含 95% 信賴區間、Welch t 檢定與「結論怎麼寫」的 `report.html`；可再用瀏覽器列印成 PDF。

## 定義

- 只量加密：每個 frame 8 個密文，明文事先編碼，迴圈內只做加密，連續 30 秒；第一與最後一個 frame 檢查正確性。
- 筆數 = 密文數 × 2048 ÷ 4（每筆 4 個特徵）。
- 總值 = 筆數 ÷ 這 30 秒的整板能量；淨值 = 筆數 ÷［（加密時功率 − 同一狀態的待機功率）× 秒數］。
- 探針螢幕以 INA228 的能量累積器另算一次，作為交叉驗證。

## 已知限制

- 板上需要 sudo 的步驟都在前景執行；背景工作在主控台登出後會失去 sudo 認證。
- 主控台輸出偶爾夾帶提示字元，`kv_console.py` 以標記字串判斷每個指令的結束。
- 腳本固定使用 COM5（探針）與 COM6（KV260）；不同電腦請修改程式中的序列埠名稱。
