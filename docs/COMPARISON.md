# 兩組對照 / Group A vs group B comparison

**一句話**：同一份 CDC 測試資料、同一個模型、同一組金鑰與參數、同一段計時，只換「多項式乘法由誰算」——**組 A 純軟體**（KV260 的 ARM 四核心，`CpuBackend`）對上**組 B 軟硬體協同**（ARM＋FPGA，經 DMA 的 `FpgaBackend`）——回答 FPGA 在什麼條件下值得用：快多少、代價多少、從多長的資料流開始划算。

**狀態（2026-10-14）**：階段 0（主機檢查）完成；第一次上板，部署 gates（A 只缺不需要的 cmake、C–G 通過，B 改用預編譯模組）、CPU↔FPGA 實板一致性、階段 2 完整 A/B 量測都已完成。**結論：用目前的 FPGA 介面，4 核 CPU（組 A）比 FPGA（組 B）快——單批延遲 60.5 vs 93.0 ms、吞吐量 58,558 vs 36,138 筆/秒；每次 FPGA 呼叫 4.7 ms 裡 DMA＋PL 只占 0.54 ms，瓶頸在呼叫的額外負擔。** 詳見[實板對照結果](#實板對照結果--board-results)。

程式在 [software/native_v5](../software/native_v5/README.md)（資料夾內容、工具用法、兩組可改進的方向見該 README），V3／V4 已驗收的檔案一律不動；上板步驟全在 [BOARD_GUIDE.md](BOARD_GUIDE.md)；專案總覽見 [README.md](../README.md)。

**目錄**

1. [架構與分工](#架構與分工--architecture-and-ownership)
2. [公平規則](#公平規則--fairness-rules)
3. [階段與狀態](#階段與狀態--stages-and-status)
4. [量測與報表怎麼讀](#量測與報表怎麼讀--reading-the-measurements)
5. [實板對照結果](#實板對照結果--board-results)
6. [階段 4 待辦](#階段-4-待辦--stage-4-backlog)
7. [已知限制](#已知限制--known-limits)
8. [結果檔格式](#結果檔格式--result-schema)

## 架構與分工 / Architecture and ownership

兩組跑同一套程式，只有 CKKS client 把多項式乘法交給的**後端**（`PolyBackend`）不同；數學原樣重用已驗收的 V4 `core.cpp`（[NATIVE_V4.md](NATIVE_V4.md)），乘積、金鑰、密文都與 V4 逐位元相同（由測試保證）。呼叫順序：`run_comparison.py`（階段 1、2）→ `v5_e2e` → `edge`（步驟 1–10，串行或管線）→ `ckks/client` → `backend/factory` 依名稱建立的後端（組 B 再經 `DmaDevice`：操作員交付 fd 的實體 DMA，或模擬裝置）；階段 3 由 `capi`＋`python/edge_client.py` 呼叫同一個 client。

| 誰 | 只改 | 量測設定 |
|---|---|---|
| **組 A** | `backend/cpu/` | `cpu-w1`～`cpu-w4`＝`CpuBackend` 用 1～4 個執行緒（含呼叫者；KV260 有 4 個 Cortex-A53；工廠可到 `cpu-w64`）；工作切成（分量×運算元）、（分量×乘積）讓 4 核都用上（共用運算元只轉換一次，見已知限制） |
| **組 B** | `backend/fpga/` | `fpga-serial`：逐一打包 → DMA → 解包；`fpga-overlap`：雙緩衝，第 i 個乘積在驅動裡時，輔助執行緒打包第 i+1 個、解包第 i−1 個 |
| **共用** | 其餘：`common/`、`runtime/`、`ckks/`、`edge/`、`tools/`（含 `v5_e2e`）、`capi/`、`python/`、根目錄 `tools/` | 定義「同計時範圍」與「怎麼比」，兩組協議後才改 |

**新增後端**要改四處：① 實作 `PolyBackend`（`multiply_batch`、`name`；輸出不可與輸入或彼此重疊，一次一批）；② `backend/factory.cpp` 加分支，用 DMA 時 `backend_needs_device()` 也要認得；③ `.cpp` 加進 `tools/v5_sources.py` 的 `LIBRARY` 與 `CMakeLists.txt`（板上用前者、CI 用 CMake，不一致時前者拒絕建置）；④ 名稱加進 `tools/run_comparison.py` 的 `CPU_BACKENDS`／`FPGA_BACKENDS`，板上才會量。`cpu-` 開頭算組 A、`fpga-` 算組 B，其他標 `?`、不參加「最佳 A／B」；`cpu-w` 後只能接核心數。只有 `v5_e2e` 經工廠建立後端，`v5_parity`、`v5_bench`、C ABI（階段 3）不會自動支援。改完重跑主機檢查、更新收據與 manifest（[DEVELOPMENT.md](DEVELOPMENT.md)）。

## 公平規則 / Fairness rules

來自簡報「對照五面向」頁，加上程式檢查；`compare_results.py` 先檢查，不符就拒絕產生報表（`--allow-unfair` 強制時報表寫 `Fairness checks: FAILED`）。

| 規則 | 程式怎麼保證 | 報表工具：不同就拒絕／處理 |
|---|---|---|
| 同資料 | 記錄資料、模型、前處理的 SHA-256（資料指紋）與筆數 | 這四項 |
| 同參數 | 所有後端同一個執行檔；兩組同一編譯器與旗標（`-std=c++17 -Wall -Wextra -Werror -fno-fast-math -pthread`＋`--cxxflags`，預設 `-O2`；`--no-werror` 只拿掉 `-Werror`）；旗標與原始檔、執行檔的 SHA-256 記在 `stage_report.json` | CKKS profile（N、質數、scale、取樣器、誤差界） |
| 同金鑰與隨機數 | 同一份金鑰材料，公鑰 SHA-256 不同就整份 FAIL；`correctness` 連每批隨機數也共用，密文與解密結果必須**逐位元相同**——兩組互為標準答案 | OS 亂數與 QA 種子混用 |
| 同計時範圍 | 同一份 `EdgeSession`，步驟 1–10 各自計時，雲端另列（Edge 不含雲端）；keygen 不計入 | （共用程式保證） |
| 同量測方法 | 延遲每輪輪換後端順序，漂移平均分攤；吞吐量取 wall time 居中那次；步驟 ② 的編碼執行緒數（`--encode-workers`）對同一次執行的每個後端都一樣 | `batch_records`、`rounds`、`warmup`、`repeats`、`cloud_workers`、`encode_workers`（舊檔沒有這欄時視為 1） |
| 固定核心 | `--cpus 0-3` 在建立任何執行緒前綁定，之後的執行緒（執行緒池、管線、功耗取樣）都繼承；記錄 `affinity`、`pinned` | 機器架構、CPU 型號、核心數、`affinity`、`pinned` |
| 同時脈設定 | 記錄每核 cpufreq（governor、目前／最低／最高頻率） | governor |
| 成本 | 吞吐量都記錄 CPU 使用率；有 hwmon 感測器就記功耗與每焦耳筆數 | 只用有效讀數；沒有就寫明不報能耗 |
| 真假 FPGA 分開 | 沒有 `--fd` 時 FPGA 列跑模擬裝置，標 `device_times_are_fpga: false` | 開頭警告「不是 FPGA 時間」（實板與模擬混合也警告）；模擬列標 `(simulated)`；有實板列時永不當「最佳 B」 |
| 證據不覆蓋、失敗不算數 | `v5_e2e`、`run_comparison.py`、`compare_results.py` 都不覆蓋既有輸出；`v5_e2e` 失敗只留失敗紀錄（不留半套數字） | `final_status` 不是 `V5_E2E_PASS` 的檔案 |

## 階段與狀態 / Stages and status

| 階段 | 在哪裡／誰主導 | 產出 | 通過條件 | 狀態（2026-10-14） |
|---|---|---|---|---|
| 0 架構與主機驗證 | 電腦（Linux／WSL）／任何人 | 收據 [local_acceptance.json](../release/native_v5/local_acceptance.json) | `V5_LOCAL_HOST_CHECKS_PASS` | **完成**（x86 WSL） |
| 1 ARM 基準 | KV260 只用 CPU／組 A | `e2e_cpu.json`、`comparison.*`、`stage_report.json` | `V5_STAGE1_COMPLETE`；四種 CPU 設定逐位元相同 | 尚未單獨執行（階段 2 已量同樣的 `cpu-w1`～`cpu-w4`） |
| 2 FPGA 串接與對照 | KV260，操作員先過部署 gates／組 B | `parity_board.log`、`e2e_all.json`、`comparison.*`、`stage_report.json` | `V5_PARITY_PASS device=linux-dma`；六種設定逐位元相同；`V5_STAGE2_COMPLETE` | **完成**：實板一致性三次通過；A/B 量測 `V5_STAGE2_COMPLETE` 兩次（`stage2_02`、平行編碼的 `stage2_03`）；每次呼叫拆解 `DMA_TRACE_COMPLETE` |
| 3 兩機展示 | 電腦（雲端，只碰密文）＋KV260（client）／共用 | 兩份 client 結果（CPU、FPGA 各一） | 每次 client 執行印出 `V5_TWO_MACHINE_CLIENT_PASS`；雲端只印 `DYNAMIC_CIPHERTEXT_SERVER_READY`（就緒，無 PASS 標記） | 未完成：板子沒有網路；主機 127.0.0.1 雙向 TLS 迴路已演練 |
| 4 工程收尾 | KV260／共用 | 功耗、OpenFHE、soak、G07／G08／G12 | 各自的驗收條件 | 未開始（[待辦](#階段-4-待辦--stage-4-backlog)） |

指令：階段 0 見 [software/native_v5 README](../software/native_v5/README.md)「建置與檢查」，階段 1–3 見 [BOARD_GUIDE.md](BOARD_GUIDE.md)。`run_comparison.py` 依序：環境清單 → 編譯（或 `--bin` 重用同一份原始碼先前編好的執行檔）→ 單元測試 → 模擬一致性 → 實板一致性（階段 2 帶 `--fd`）→ `v5_e2e` 三種模式 → `compare_results.py`；任一步失敗就停，所以實板一致性沒過就沒有 FPGA 量測。腳本與工具都不開、不重置、不重試裝置：操作員開好 fd，腳本只經 `/proc/self/fd` 確認它指向 `/dev/edgefhe_rns3_n4096_dma`。主機可用 `--stage 2 --simulated` 演練（狀態 `V5_STAGE2_COMPLETE_SIMULATED_DEVICE`，不算通過）。

## 量測與報表怎麼讀 / Reading the measurements

**步驟**：①–③ `standardize`、`encode`、`sample`（`prepare` 段）→ ④⑤ `encrypt`（後端 8 個乘積＝4 特徵 × 2）→ ⑥–⑧ `cloud`（Σ w×c + b，行程內 CPU）→ ⑨ `decrypt`（後端 1 個乘積）→ ⑩ `decode`（解碼、sigmoid、判斷；`finish` 段）。每個乘積是組 B 的一次 DMA 請求，所以**每批** 9 次，不是每筆 9 次；keygen 另做 1 個乘積（建立 session 時，記為 `keygen_ms`）。加密與解密共用後端，以鎖保證同時只有一批在後端。一份密文有 2048 個 slot，一批最多 2048 筆一起加密，所以「一筆」的延遲就是那一批的延遲。

`v5_e2e` 預設三種模式全開（欄位見[結果檔格式](#結果檔格式--result-schema)）：

| 模式（簡報面向） | 做什麼 |
|---|---|
| `correctness`（正確性） | 整份資料逐批串行走過每個後端：和明文 logit 比誤差與決策，有標籤時算 AUC 與混淆矩陣，並比對全部密文與解密結果的 SHA-256 |
| `latency`（單筆延遲） | 一批（`--batch-records` 預設 2048，可 1–2048）跑 `--warmup` 3＋`--rounds` 30 輪：各步驟、Total、Edge 的 p50／p95／平均，與量測輪累計的後端統計 |
| `throughput`（吞吐量、擴展性、成本） | 串行（capacity 0）與管線（`--capacity` 預設 `0,2`）× 串流長度（`--stream-batches` 預設整份；`run_comparison.py` 傳 `1,2,4,8,all`），各 `--repeats` 3 次，取居中那次（後端統計也是那一次的，不是總和） |

其他選項：`--backends`（預設六種；階段 1 只傳四種 CPU）、`--cloud-workers` 1 或 3、`--encode-workers` 1–4（步驟 ② 四個特徵分給幾個執行緒編碼，預設 1＝逐一編碼；每個特徵仍呼叫 V4 `encode()`，結果逐位元相同；`run_comparison.py` 同名選項會傳下去）、`--cpus`（預設不綁）、`--power auto`、`--power-interval-ms 20`、`--qa-seed`、`--fd`。管線 5 段各一個執行緒，段間是有界佇列：第 k 段做第 i 批時，第 k−1 段已在做第 i+1 批；佇列滿了上一段就等（回壓），不會無限堆積；只有 1 批時沒東西可重疊。

### 名詞與指標

| 名詞／指標 | 意思 |
|---|---|
| 分量、NTT | 分量（limb）＝大數在三個質數下的餘數，各算一份；NTT＝相乘前後的換算 |
| logit、誤差界、無法判定 | logit＝sigmoid 前的分數，> 0 判 1；誤差界＝事先算好的最大允許誤差；離 0 比誤差界近記「無法判定」（`uncertain`），不算錯；通過＝全在誤差界內且「不一致」為 0 |
| wall time、p50／p95 | 實際經過的時間；中位數／只有約 5% 的輪次更慢（30 輪時是第二慢那輪） |
| Edge、Total、步驟時間 | Edge＝步驟 1–5 與 9–10 加總（不含雲端 6–8）；Total＝一批的 wall time（含行程內雲端）；`encrypt`／`decrypt` 不含等後端 |
| `busy_ms`／`work_ms` | 段內時間（含等共用後端）／步驟自己的時間，差 ≈ 等後端；`encrypt`＋`decrypt` 的 `work_ms` 接近 `wall_ms`＝後端滿載，是瓶頸 |
| `queue_high_water_after` | 該段後面佇列最多排幾批（≤ capacity）；到 capacity＝下一段曾跟不上 |
| Records/s、Gain vs serial | 筆數 ÷ 居中那次的 wall time；管線 ÷ 同後端同長度的串行，> 1 才划算 |
| CPU util | 行程 CPU 時間（所有執行緒 user＋system）÷（wall time × 綁定核心數）；1.0＝全程滿載 |
| Mean W、Records/J | 平均功率；筆數 ÷（平均功率 × 時間）；讀數少於 5 筆或功率為 0 記 `null`，不寫假的 0（依 `--power-interval-ms` 取樣、頭尾各加一筆，太短要用更長的串流） |
| Device call、Driver-reported | 組 B 每個乘積：程式看到的整個 ioctl／驅動量的 DMA＋PL；差值＝呼叫與搬運的額外負擔 |
| Pack、Unpack、KiB moved | 軟體「分量優先」、DMA「係數優先」，每次呼叫都要轉置；`fpga-overlap` 時與 device call 重疊，三者相加可超過總時間；每個乘積搬 192 KiB（輸入 128＋輸出 64） |
| Speedup、Efficiency | 相對同檔 `cpu-w1` 的 Edge p50；組 A 擴展性表用 encrypt：1→N 核加速比，效率＝加速比 ÷ 核心數 |
| 最佳 A vs 最佳 B | 延遲＝A 的 Edge p50 ÷ B 的（> 1＝B 較快）；吞吐量、每焦耳＝B ÷ A，在兩組都量過的最長串流各取最好的一列 |

### `compare_results.py` 的報表

`comparison.md`（貼進報告）＋`comparison.json`（同樣數字，給 dashboard）。開頭列筆數、模型、機器、affinity（是否綁核）、governor、隨機數來源、檔案與公平檢查結果；**1. Correctness (gate)**：Pass、Max logit diff、Disagree、Uncertain、Cipher SHA-256 前 16 碼（同檔內必須相同；不同檔各用新的 OS 亂數，不同是正常的）；**2. Latency**：Edge p50/p95、Encrypt、Decrypt、Cloud、Total、Speedup，組 A 擴展性表，組 B 每個乘積的分解；**3. Throughput**：上表的吞吐量與功耗指標，加上看管線從幾批開始划算的 Stream length 表。兩段最後各有「最佳 A vs 最佳 B」。同一後端出現在多個檔案時列標「後端 (資料夾/檔名)」，例如 `cpu-w4 (stage1_01/e2e_cpu.json)`。

合併同一塊板子的階段 1、2：結果依 [BOARD_GUIDE.md](BOARD_GUIDE.md) 帶回 `results/<日期>/`，在 repo 根目錄執行下列指令；兩次執行的設定、綁核、governor 必須相同；多個功耗感測器時加 `--sensor <名稱>`（見 `environment.power_sensors`，預設第一個有可用讀數的）。

```bash
python3 tools/compare_results.py results/<日期>/stage1_01/e2e_cpu.json results/<日期>/stage2_01/e2e_all.json \
    --output results/<日期>/comparison_all
```

## 實板對照結果 / Board results

### 部署與一致性（2026-10-14 第一次上板）

| 項目 | 結果 |
|---|---|
| 部署 gates | A 只缺不需要的 cmake（`ENVIRONMENT_PREREQUISITES_INCOMPLETE`），C–G 通過；B（在板上編譯驅動模組）改用已驗收、與核心 `5.15.0-1027-xilinx-zynqmp` 完全相符的預編譯模組 |
| 板上單元測試（aarch64、GCC 11） | 24／24 通過；加入平行編碼測試後 `stage2_03` 為 25／25 |
| C ABI 對 V4 函式庫（aarch64、GCC 11） | 逐位元相同 |
| CPU ↔ FPGA 實板一致性 | `v5_parity --fd 3 --cases 64` → `V5_PARITY_PASS device=linux-dma`：786,432 個係數（64 組 × 3 個分量 × 4096 個係數）逐位元相同，外加一次完整 client 回合（keygen、四特徵加密、解密）完全相同；共 77 次 DMA 請求（64 組＋keygen 1＋加密 8＋解密 4） |

### 階段 2 完整 A/B 量測

`V5_STAGE2_COMPLETE`（2026-10-14）。

**條件**：KV260、Ubuntu 22.04.4、核心 `5.15.0-1027-xilinx-zynqmp`、GCC 11.4（`-O2`，兩組同一個執行檔）、4 × Cortex-A53 1.333 GHz（`userspace` governor）、綁核 0–3；CDC 測試集 38,052 筆（19 批 × 2048 筆）、OS 亂數；延遲 3＋30 輪、吞吐量重複 3 次取中位數、佇列容量 0（串行）與 2（管線）；功耗來自 `ina260_u14/power1`（整板）。原始檔在 [results/2026-10-14-kv260/stage2_02/](../results/2026-10-14-kv260/stage2_02/comparison.md)。

**正確性**：六種設定（cpu-w1～w4、fpga-serial、fpga-overlap）的密文與解密結果 SHA-256 **完全相同**；最大 logit 誤差 3.93×10⁻⁷（上界 2.6×10⁻⁴），0 筆不一致、1 筆落在誤差界內。也就是實際 FPGA 在整份資料上和 CPU 逐位元一致。

| 面向 | 組 A 最佳（cpu-w4） | 組 B 最佳（fpga-overlap） | B ÷ A |
|---|---|---|---|
| 單批（2048 筆）邊緣延遲 p50 | 60.45 ms | 93.04 ms | 0.65× |
| 19 批管線吞吐量 | 58,558 筆/秒 | 36,138 筆/秒 | 0.62× |
| 能源效率（整板約 3.3–3.7 W） | 15,743 筆/焦耳 | 10,081 筆/焦耳 | 0.64× |

**每個步驟（p50，ms）**

| 設定 | ① 標準化 | ② 編碼 | ③ 取樣 | ④⑤ 加密 | ⑥–⑧ 雲端 | ⑨ 解密 | ⑩ 判斷 | 邊緣合計 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| cpu-w1 | 0.43 | 31.40 | 0.11 | 38.34 | 4.56 | 7.86 | 8.63 | 86.76 |
| cpu-w2 | 0.43 | 31.40 | 0.11 | 23.37 | 4.57 | 5.17 | 8.62 | 69.08 |
| cpu-w3 | 0.43 | 31.34 | 0.11 | 18.48 | 4.58 | 3.83 | 8.64 | 62.90 |
| cpu-w4 | 0.43 | 31.36 | 0.11 | 16.17 | 4.58 | 3.75 | 8.64 | 60.45 |
| fpga-serial | 0.43 | 31.41 | 0.11 | 47.35 | 4.59 | 6.45 | 8.63 | 94.38 |
| fpga-overlap | 0.43 | 31.38 | 0.11 | 46.00 | 4.58 | 6.44 | 8.65 | 93.04 |

- **組 A 擴展**：加密步驟 1→4 核加速 2.37 倍（效率 0.59）；整個邊緣延遲 1.44 倍，因為編碼等單執行緒步驟不會變快。
- **組 B 分解（每次乘法）**：打包 0.07 ms、整個 ioctl 4.67–4.72 ms、解包 0.18 ms；其中驅動回報的 DMA＋PL 只有 0.54–0.55 ms（約 11%），每次搬 192 KiB。
- **管線**：佇列容量 2 時，19 批的吞吐量對每種設定都提高 1.73–2.12 倍；只有 1 批時沒有好處（2 批約 1.3 倍）。

### 步驟 ② 平行編碼後（`stage2_03`）

同一天、同一塊板子、同一個 session，只加 `--encode-workers 4`：`V5_STAGE2_COMPLETE`。六種設定的密文 SHA-256 相同；最大 logit 誤差 4.47×10⁻⁷，0 筆不一致、1 筆在誤差界內。原始檔在 [results/2026-10-14-kv260/stage2_03/](../results/2026-10-14-kv260/stage2_03/comparison.md)。

| 設定 | ② 編碼 p50（`stage2_02` → `stage2_03`，ms） | 邊緣延遲 p50（ms） |
|---|---:|---:|
| cpu-w1 | 31.40 → 8.33 | 86.76 → 63.77 |
| cpu-w2 | 31.40 → 8.33 | 69.08 → 46.02 |
| cpu-w3 | 31.34 → 8.32 | 62.90 → 39.74 |
| cpu-w4 | 31.36 → 8.31 | 60.45 → 37.40 |
| fpga-serial | 31.41 → 8.30 | 94.38 → 71.26 |
| fpga-overlap | 31.38 → 8.32 | 93.04 → 69.91 |

| 面向 | 組 A 最佳（cpu-w4） | 組 B 最佳（fpga-overlap） | B ÷ A |
|---|---|---|---|
| 單批邊緣延遲 p50 | 37.40 ms | 69.91 ms | 0.54× |
| 19 批管線吞吐量 | 66,748 筆/秒 | 36,080 筆/秒 | 0.54× |
| 能源效率 | 17,624 筆/焦耳 | 10,034 筆/焦耳 | 0.57× |

編碼在 4 核上加速 3.78 倍，兩組的邊緣延遲都少了約 23 ms。組 A 的吞吐量提高 14%。組 B 的吞吐量不變，因為它的瓶頸是加密段的 FPGA 呼叫，不是準備段。共用步驟變快之後，B 每次呼叫的額外負擔占比更高，所以 B ÷ A 反而下降。

### 組 B：每次 FPGA 呼叫拆解（`dma_trace_01`）

用 [trace_dma_call.py](../tools/trace_dma_call.py) 量，不改凍結的 V3 驅動。量法：ftrace function_graph 加 4 個 kprobe 標記，標記位置取自預編譯模組的反組譯，模組身分全部相符；做法與安全措施見 [BOARD_GUIDE.md](BOARD_GUIDE.md) 3.3.1。

- 工作負載：v5_e2e latency、fpga-serial、暖機 2＋量測 10 輪。
- 每次呼叫：不追蹤 4.677 ms，追蹤 4.735 ms，追蹤本身多 58 µs。
- 109 次呼叫全部完整拆解，下表是中位數。

| 段 | 做什麼 | µs | 占比 |
|---|---|---:|---:|
| 檢查係數 | 驅動確認每個係數小於質數、padding 為 0：從**不經快取**的 TX 緩衝區讀 32,768 次 32 位元，每次約 110 ns | 3,589 | 76.0% |
| DMA＋PL | 驅動 `elapsed_us` 量的那段 | 563 | 11.9% |
| 搬出 | `copy_to_user` 64 KiB，從不經快取的 RX 緩衝區讀，146 MB/s | 450 | 9.6% |
| 擦除 | `memzero_explicit` 192 KiB＋64 KiB | 35 | 0.7% |
| 搬入 | `copy_from_user` 128 KiB，寫入 4.7 GB/s | 28 | 0.9% |
| DMA 設定 | AXI-Lite 暫存器，兩個描述子準備與送出 | 18 | 0.4% |
| 清空輸出區 | `memset` 64 KiB | 10 | 0.2% |
| 其他 | 進出 ioctl、鎖、請求檢查 | 10 | 0.3% |
| **合計**（`edgefhe_ioctl`） | | **4,705** | |

為什麼會這樣：

- 驅動的兩塊緩衝區用 `dma_alloc_coherent` 配置，而 device tree 沒有宣告 `dma-coherent`（[fdt_contract.py](../software/kv260/fdt_contract.py) 明確要求不能有）。所以在 ARM64 上這兩塊是「不經快取」的記憶體。
- 對這種記憶體，寫入可以合併，所以很快；讀取卻每次都要等 DRAM。
- 檢查係數的迴圈每讀一個 32 位元字就立刻比較、分支，in-order 的 A53 只能一個一個等。
- 多出的 4.1 ms 幾乎全是兩段讀取：檢查 3.59 ms＋搬出 0.45 ms。複製本身、系統呼叫、DMA 設定加起來不到 0.1 ms。

**解讀 / What it means**

1. **FPGA 不是瓶頸，驅動讀取不經快取的記憶體才是。** 用目前的 V3 驅動與 op2 介面，4 核 CPU 仍比 FPGA 快。每次呼叫 4.7 ms 裡，PL＋DMA 只占 0.56 ms，76% 花在驅動檢查係數。另外 op2 每次都重送並重新轉換公鑰：每個分量 24 次轉換，CPU 路徑只要 14 次。這和簡報「瓶頸在搬運，不在計算」的判斷一致，而且現在能指出是哪一行程式。
2. **編碼已經平行化**：步驟 ② 從 31.4 ms 降到 8.3 ms。組 A 現在最大的兩項是加密 16.2 ms 和解碼 8.6 ms；解碼只有一個多項式，無法照特徵拆開。
3. **估算，尚未實測**：如果新版驅動在可快取的記憶體上做同樣的檢查、改從可快取的緩衝區搬出（串流 DMA 加上明確的快取維護），每次呼叫約可省 3.9 ms：
   - 檢查約 3.59 → 0.05 ms。
   - 搬出約 0.45 → 0.05 ms。

   每批 9 次呼叫，fpga-overlap 的單批邊緣延遲會從 69.9 ms 降到約 34 ms，和 cpu-w4 的 37.4 ms 相當。要新驅動實測才算數。
4. **下一步**（依優先順序）：
   1. 另立新版驅動候選（V3 凍結不改）：ioctl ABI 不變，所以 V5 不用改；檢查在可快取的記憶體做，輸出走可快取的緩衝區。需要新的身分、建置與板測驗收。
   2. 評估合併呼叫（簡報問題一選項 C）：需要新的 HLS 運算與板測驗收。
   3. 長時間 soak 與故障測試。

## 階段 4 待辦 / Stage 4 backlog

| 項目 | 做什麼 |
|---|---|
| 功耗 | 板上有 hwmon 感測器時階段 1／2 自動記錄；沒有時以外接功率計量同一段吞吐量（同樣的 `--repeats`） |
| OpenFHE 參考 | 組 A 的業界對照：板上建置 OpenFHE 後執行 [validation/openfhe_client](../validation/openfhe_client/README.md)；參數不同，只當參考、不算加速比 |
| 管線長時間測試 | `v5_e2e --modes throughput --repeats 50` 搭配故障紀錄；接上真實 DMA 後依架構決策 P1（H：單板管線）重做 soak、回壓與故障測試（[ARCHITECTURE.md](ARCHITECTURE.md)） |
| G07／G08／G12 | 仍是 partial（[VALIDATION.md](VALIDATION.md)） |
| 階段 2 之後的平行化 | 加寬運算／管線重疊／合併呼叫，依老師建議再定；新 HLS 運算＝新 bitstream＝新的板測驗收 |

## 已知限制 / Known limits

- **模擬時間是 CPU 時間**，不是 FPGA 時間（標記方式見[公平規則](#公平規則--fairness-rules)）；模擬裝置只模擬完整乘法 op2，其他運算回傳 `-EOPNOTSUPP`。
- **op2 結構劣勢**：FPGA 只有完整乘法，每次重送並重新轉換公鑰；加密一批每個分量 24 次轉換（8 個乘積 ×（2 正＋1 逆）），CPU 路徑 14 次（6 個不同運算元各正轉一次＋8 個乘積各逆轉一次）。要消除需新的 HLS 運算與板測驗收。
- **一次一個 DMA 請求**：雙緩衝與管線只重疊 CPU 端工作（打包、解包、其他步驟），不能讓兩個請求同時在 FPGA 上。
- **功耗**來自板上 hwmon，量整板或某一路電源，不是單獨的 CPU 或 FPGA。
- **單板量測的雲端**在行程內用 CPU 算，兩組相同，不含網路（網路只在階段 3）。
- **取樣器**是稀疏功能 profile（`fixed-weight-ternary-s64-u64-e16`），沒有安全位元數宣稱（`security_bits: null`）；見 [SECURITY.md](../SECURITY.md)。
- **`--qa-seed`** 用公開種子，金鑰任何人都能重算，**只能用於測試**。

## 結果檔格式 / Result schema

```text
v5_e2e（schema edgefhe-v5-e2e-1）：schema tool label
 environment: system release machine cpu_model online_cpus affinity pinned compiler
  cpufreq[]{cpu governor cur_khz min_khz max_khz} power_sensors[]
 inputs: data model preprocess（各附 *_sha256） model_id records limit labelled features
 profile: degree=4096 slots=2048 primes[3] input_scale_bits=29 output_scale_bits=58
  sampler security_bits=null error_bound
 device: name=none|simulated-dma|linux-dma device_times_are_fpga opened_by_tool=false
 randomness: "os-getrandom" 或 "qa-seed:<S> (public seed; QA only)"
 settings: backends modes batch_records rounds warmup repeats cloud_workers cloud power_interval_ms
 keygen[]: backend keygen_ms public_key_sha256（必須全部相同）
 correctness: batches bit_exact_across_backends backends[]{backend name records max_abs_logit_diff
  mean_abs_logit_diff all_within_bound decisions{agree disagree uncertain}
  labels{auc_ckks auc_plain accuracy_certain confusion} cipher_sha256 decrypted_sha256 pass}
 latency: records_per_batch timed_region results[]{backend name steps[]{step slide time}
  total edge_only backend_stats}（time、total、edge_only = p50_us p95_us mean_us）
 throughput: batch_records cpus_for_utilization power_available results[]{backend name pipeline
  capacity batches records wall_ms_repeats wall_ms records_per_s batches_per_s cpu_utilization
  stages[]{stage busy_ms work_ms queue_high_water_after（最後一段沒有）}
  power[]{sensor samples mean_w min_w max_w energy_j records_per_j（不足時三項為 null＋note）}
  backend_stats}
 backend_stats: batches jobs wall_ms pack_ms device_ms unpack_ms driver_reported_ms
  bytes_to_device bytes_from_device forward_transforms inverse_transforms
 notes[] final_status=V5_E2E_PASS|V5_E2E_FAIL；失敗時整份只剩 schema final_status failure

stage_report.json（run_comparison.py，schema edgefhe-v5-stage-1）
 stage label status=RUNNING→V5_STAGE1_COMPLETE|V5_STAGE2_COMPLETE（--simulated 加 _SIMULATED_DEVICE）|FAIL＋failure
 started_utc finished_utc device（階段 2 裝置路徑；演練 simulated；階段 1 null） device_opened_by_script=false
 inputs{data model preprocess: path sha256}
 settings{cpus rounds warmup repeats capacity stream_batches limit cxxflags no_werror}
 source_sha256（V5 函式庫、工具、C ABI、V4 參考函式庫的每個 .cpp）
 dma_policy{timeout_ms_per_request=5000 retry=false reset=false stop_on_first_failure=true requests_in_flight=1}
 steps[]{name exit marker pass seconds} binaries_sha256 binaries_reused_from（--bin） results{e2e comparison}
 同資料夾：environment.json、bin/（build.log、執行檔；--bin 時沒有）、unit_tests／parity_simulated／parity_board／e2e／compare 的 .log

comparison.json（compare_results.py）
 real_fpga fpga_device=none|real|simulated|mixed has_group_b fairness_problems inputs environment files
 correctness latency throughput energy（最佳 A、最佳 B 與比值） correctness_rows latency_rows throughput_rows
```

`name` 是後端自報名稱（如 `cpu-w4`、`fpga-linux-dma-overlap`、`fpga-simulated-dma-serial`），看得出實板或模擬；`backend_stats` 組 A 填轉換次數、組 B 填時間與搬運量。
