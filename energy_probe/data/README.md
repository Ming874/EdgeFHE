# 探針實測資料（2026-11-23）

每個資料夾是一次量測場次，由 [../kv260/](../kv260/README.md) 或 [../pc/](../pc/probe_log.py) 的工具產生。`probe.csv` 是探針每 0.1 秒一筆的原始紀錄（`host_s,t_ms,V,I_mA,P_mW,E_J`），`runs.json` 是每一輪的時間、主控台輸出與探針回傳的結果，其他檔案由分析程式產生。探針未經參考儀器校正（K = 1），引用前請先讀[團隊脈絡文件](../../docs/notes/2026-11-23_team_context.md)的引用規則。

| 資料 | 內容 | 產生工具 |
|---|---|---|
| `v8b_vs_openfhe_10x_20261123_185907/` | **正式引用**：V8b 與 OpenFHE（單執行緒、4 執行緒）各 10 輪、每輪 30 秒，只計加密；V8b 每焦耳筆數為 OpenFHE 4 執行緒的 9.54 倍。含 `report.html`／`report.pdf` 與 `figures/` | `enc_compare_v8b_ofhe.py`、`analyze_compare.py`、`make_figs.py`、`make_report.py` |
| `partition_20261123_212949/` | **正式引用**：軟硬體切分。ARM 逐模組實測、PL 整段加密實測、共同執行與全部在 ARM 的整體實測 | [../kv260/partition/](../kv260/partition/README.md) |
| `v8b_vs_openfhe_20261123_184118/` | 同日稍早的 3 輪量測，用來檢查重複性（與 10 輪平均相差不到 1%） | 同第一列 |
| `enc_compare_20261123_143305/` | V8b rev5 與 edgefhe2 的每焦耳筆數比較，各 3 輪（未接乙太網路） | `enc_compare.py` |
| `ina260_vs_probe_20261123_141322.json` | 板上 INA260（只量 SOM 5 V）與本探針同時量測待機與 ARM 滿載：INA260 只涵蓋整板的 47–49% | 一次性的比對腳本 |
| `states_20261123_141031.*` | 待機、ARM 4 核滿載、恢復三個階段的整板功率 | 一次性的量測腳本 |
| `idle_20261123_140144.*` | 探針的第一次待機紀錄與取樣分析 | `pc/probe_log.py`、`pc/analyze_probe.py` |

同日 19:41 的切分場次（`partition_20261123_194135`）已由 21:29 的場次取代，不收錄於此。
