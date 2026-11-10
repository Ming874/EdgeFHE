# EdgeFHE V8b 的詳細參數（N = 4096）

- 本文件只涵蓋 V8b（rev4 與 rev5）以及它之前的版本。
- 每個數字都取自原件，原件路徑列在每節末尾。
- 「co-sim」是 RTL 模擬，「Vivado 估計」是工具估計，兩者都**不是**上板量測。
- 「上板」數字都經過獨立審查器 PASS。

---

## 0. 版本系譜

| 線 | 版本 | 日期 | 狀態 | 一句話 |
|---|---|---|---|---|
| 歷史 | V1 fixed-bank 候選 | 2026-10 上旬 | 凍結 | 最早的 PL NTT 證據，只作歷史對照 |
| 歷史 | V7（N4096） | 10-09～10-14 | 已被 V8b 取代 | PAIR4 管線；driver candidate-3 時 120 jobs/s，對 CPU 3 執行緒 56.8 是 2.12× |
| 歷史 | V8a（N4096 HLS） | 10-15 | 已被 V8b 取代 | V8b 的 HLS 前身 |
| **產品線** | **V8b rev4**（N4096） | 10-16 起 | 上板驗收；release v1 | 加密整段在 PL；19 個模型、12 個領域 |
| 產品線 | V8b rev5（N4096） | 10-29 | 上板數學驗收 | rev4 加上 H2／H3／H4 安全強化（清除、TKEEP 檢查） |

---

## 1. CKKS 密碼參數

| 參數 | V8b（N = 4096，rev4／rev5） |
|---|---|
| 環維度 N | 4,096 |
| slots | 2,048 |
| 密文模數 Q | 3 個 NTT 質數：998244353、1004535809、469762049 |
| log₂ Q | 88.606 |
| 特殊模數 P（key switching） | 無 |
| key switching | 不需要 |
| scale Δ | 2^30 |
| 私鑰分布 | uniform ternary |
| 加密亂數 u／v | ternary：ChaCha20 用途 1，byte < 243 → 5 trits |
| 誤差 e | σ 4.5，常數時間 CDT63，K = 42（取樣值的絕對值 ≤ 42） |
| 種子 | OS 熵 → ChaCha20 DRBG，每 frame 32 byte，用後清除 |
| 安全等級設定 | HEStd_128_classic |
| lattice-estimator | 七種攻擊中最低 128.77 bits（ADPS16，ideal proxy） |
| 雲端運算 | 定點整數權重 affine（不需要 rescale） |
| 每個密文大小 | 約 96 KiB（2 × 3 limb × 4096 × 4 B；k ≤ 8 時輸出上限 786,464 B） |

原件：
- `release_20261025/CLAIMS_LEDGER.json`（metadata）
- `v8b_system_20261016/profile/EDGEFHE_V8B_PROFILE.json`
- `security_assessment_20261024/ESTIMATE.json`（本資料夾 `security/ESTIMATE.json`）

---

## 2. V8b 硬體與上板結果（N = 4096）

| 項目 | rev4（release v1 的版本；bitstream d4cfdc01… = v8b_system build_v4） | rev5 |
|---|---|---|
| 身分 magic／revision | 0x45464738／0x00080002，command mask 0x3c0 | 新 app `edgefhe-v8b-r5`（rev4 保留） |
| frame | EF8A：命令 6 LOAD、7 PAIR、8 INVALIDATE、9 ENC，k ≤ 8 | 同左 |
| 輸入／輸出上限 | 262,192／786,464 B | 同左 |
| 結構 | 4 lane × 3 limb；PL 快取公鑰，執行 ENC／PAIR；PL0 200 MHz（MMCM） | 加入：seed 只送取樣器、FIFO 零 token 覆寫、ChaCha 零金鑰清除與 RFC KAT（status 7）、TKEEP 非全 1 → ST_LENGTH |
| LUT | 53,046（45.29%） | 54,468（46.51%） |
| FF | 61,285（26.16%） | 62,291（26.59%） |
| BRAM36 | 65.5（45.49%） | 65.5（45.49%） |
| URAM | 42（65.63%） | 42（65.63%） |
| DSP | 531（42.55%） | 531（42.55%） |
| routed WNS／WHS | +0.035／+0.010 ns | +0.087／+0.009 ns |
| Vivado 功耗估計 | 4.911 W | 4.940 W |
| driver | 5b 系列（ABI 8；candidate-4 起） | 同左 |

**上板結果（release v1 帳本，rev4）**：
- 延遲 CPU／PL：ai4i 1.600×、diabetes 1.811×。
- 吞吐 PL／CPU：ai4i 1.641×、diabetes 2.022×（兩機有線 E2E）。
- 加密 prepare：OpenFHE／PL 約 9.1–10.7×（例如 ai4i 44.893 ms 對 4.912 ms）。
- 單機持續吞吐（不含網路）：832,396 筆／秒。
- **能效**：PL／CPU 每焦耳處理筆數 4.274×。這是 INA260 量 SOM 電源軌，是這個專題唯一的實測功耗資料。
- 6 小時長測：33.8 億筆紀錄，最大誤差 4.7e-4。
- ZOO19：19 個模型、12 個領域全部上板（v3 在 rev5 上驗收）。

原件：
- `release_20261025/documents/README_zh-TW.md`、`CLAIMS_LEDGER.json`
- `v8b_system_20261016/build_v4/`、`hls_rev5_20261029/system/build_rev5/`（utilization／timing／power 報告）
- `v8b_system_20261016/profile/EDGEFHE_V8B_PROFILE.json`

---

## 3. 系統與網路參數

| 項目 | 值 |
|---|---|
| 板子 | Xilinx Kria KV260（XCK26）、4 × Cortex-A53、Ubuntu 22.04.4 LTS、kernel 5.15.0-1027-xilinx-zynqmp、RAM 約 3.9 GB |
| 網路 | 1 GbE；TLS 1.3 雙向認證；server 憑證 DER 雜湊與 CA 都釘選 |
| PC 端 | Windows sink（`ef_sink.exe`）＋ WSL Ubuntu-24.04（雲端、owner）；OpenFHE 1.5.1 |
| 板子 IP | 192.0.2.10 |

---

## 4. 注意事項

- **功耗**：V8b 有 INA260 實測的能效比，量的是 SOM 電源軌，不是整板；Vivado 的功耗數字是工具估計。
- **rev4 與 rev5**：效能、能效與長測都在 rev4 量測；rev5 的資料通道相同，只做正確性驗收，沒有重新量效能。
