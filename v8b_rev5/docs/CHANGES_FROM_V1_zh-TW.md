# 從 V1 到 V8b rev5：差異與優化 / What Changed Since V1

這份文件說明 V8b rev5 和第一版上板的 FPGA 設計（以下稱 **V1**）差在哪裡、每一項優化解決了什麼問題，以及效果有多大。

**名詞先講清楚**

| 名稱 | 指的是 |
|---|---|
| **V1** | 第一版上板驗收的 FPGA 設計（開發紀錄中稱為 V1）：N = 4096 的 NTT／多項式運算加速器，bitstream `edgefhe-ii1-n4096-kv260`，100 MHz。main 的 V3 與 V5 上板都使用這個硬體（main 的 `firmware/ii1_n4096/`、`hardware/hls/`） |
| **release v1** | 另一個名詞：V8b **rev4** 的正式發布版本（2026-10-25）。它和 rev5 的差異見[第 6 節](#6-release-v1rev4和-rev5-的差異--release-v1-rev4-vs-rev5) |
| **V8b rev5** | 本資料夾收錄的版本 |

**數字的出處**

- V1 的數字來自 main 的 [docs/VALIDATION.md](../../docs/VALIDATION.md)、[docs/COMPARISON.md](../../docs/COMPARISON.md) 與 `results/2026-10-14-kv260/`，都是 V1 硬體的板上量測。
- V8b 的數字來自本資料夾的 [PARAMETERS_zh-TW.md](PARAMETERS_zh-TW.md)、[reference_points/](reference_points/REFERENCE_POINTS_zh-TW.md) 與 `../evidence/`。效能是 rev4 的板上量測，rev5 的資料通道與 rev4 相同。

兩邊的工作負載不完全一樣，表裡會標明。

---

## 1. 一張表看完 / At a glance

| 項目 | V1 | V8b rev5 | 改變 |
|---|---|---|---|
| FPGA 負責的工作 | NTT／INTT、多項式乘加（op0–op5）；CKKS 只用 op2 乘法 | **整段公鑰加密**：ChaCha20 亂數 → 取樣 → NTT → 與公鑰相乘 → 組裝密文 | FPGA 從「乘法器」變成「加密器」 |
| ARM 負責的工作 | 編碼、取樣、組裝密文、每次乘法都呼叫一次 FPGA | 正規化、編碼、解碼、網路 | 取樣與組裝密文移到 PL |
| 一次呼叫做多少事 | 1 個多項式乘法 | 1 個 frame，最多 **8 個密文** | V1 加密 4 個特徵要 8 次呼叫；V8b 一次呼叫最多完成 8 個密文 |
| 公鑰 | 每次乘法重新送進 PL、重新轉換 | **快取在 PL**，LOAD 一次後重複使用 | 不再重送 |
| PL 時脈 | 100 MHz | **200 MHz**（PL 內 MMCM 倍頻；PS 的 PL0 維持 100 MHz） | 2 倍 |
| 時序餘裕（WNS） | +0.004 ns（@100 MHz） | **+0.087 ns**（@200 MHz） | 時脈加倍、餘裕反而更大 |
| DSP | 753（60.34%） | **531（42.55%）** | 功能更多、DSP 更少 |
| LUT | 29,328（25.04%） | 54,468（46.51%） | 多出取樣器、ChaCha20、管線 |
| BRAM36／URAM | 62／0 | 65.5／42 | 公鑰與資料快取放進 URAM |
| 取樣分布 | 稀疏功能 profile（s、u 各 64 個 ±1，e 各 16 個），**沒有**安全等級 | 稠密：uniform ternary、σ = 4.5 常數時間 CDT63 高斯；lattice-estimator 最低 **128.77 bits** | 從「只能測功能」到「有安全估算」 |
| 亂數 | ARM 端作業系統亂數 | OS 熵播種的 ChaCha20 DRBG，在 PL 內執行，每 frame 32 B 種子、用後清除 | 亂數產生搬進硬體 |
| scale | 2²⁴（V3 軟體）／2²⁹（V4、V5） | 2³⁰ | 精度提高 |
| 驅動 | DMA proxy；在不經快取的緩衝區逐筆檢查係數 | 5b 系列（ABI 8）：可快取緩衝區＋明確的 DMA 同步、NEON 向量化檢查、frame 協定 | 解決驅動瓶頸 |
| 網路 | V3：TLS＋token，固定 6 例；V4／V5 mTLS 只在 127.0.0.1 演練 | TLS 1.3 雙向認證＋憑證釘選，**兩機有線端到端實測** | 從演練到實測 |
| 模型 | CDC 糖尿病 4 特徵邏輯斯迴歸 | **19 個模型、12 個領域** | 應用範圍擴大 |
| 和 CPU 比 | ARM＋FPGA **比** 4 核 ARM 慢（單批延遲 0.65×、吞吐 0.62×） | PL **比**同板 CPU 快（延遲 1.60–1.81×、吞吐 1.64–2.02×） | 從輸給 CPU 到贏過 CPU |

---

## 2. V1 的瓶頸在哪裡 / Where V1 was slow

2026-10-14 在 V1 硬體上的量測（main 的 [COMPARISON](../../docs/COMPARISON.md)）顯示，**FPGA 本身不是瓶頸**：

| 一次 FPGA 乘法呼叫（4.68 ms） | 時間 | 占比 |
|---|---:|---:|
| 驅動在不經快取的緩衝區逐筆檢查係數 | 3.59 ms | 76% |
| 搬出結果 | 0.45 ms | 10% |
| DMA 傳輸＋PL 計算 | 0.55 ms | 12% |
| 其他 | 0.09 ms | 2% |

再加上三個結構問題：

1. **呼叫太碎**：加密 4 個特徵要 8 次乘法，每次都是一趟完整的 ioctl＋DMA。
2. **公鑰重送**：每次乘法都把公鑰整包送進 PL 再做 NTT。每個分量要 24 次轉換，CPU 路徑只要 14 次。
3. **取樣和組裝留在 ARM**：FPGA 只幫忙乘法，其餘步驟仍然吃 ARM 的時間。

結果是 ARM＋FPGA 的單批延遲 93.0 ms、吞吐 36,138 筆／秒，輸給 4 核 ARM 的 60.5 ms、58,558 筆／秒。

---

## 3. 做了哪些優化 / The optimisations

| # | 優化 | 解決的問題 | 做法 | 在哪裡 |
|---:|---|---|---|---|
| 1 | **整段加密搬進 PL** | ARM 仍要做取樣與組裝；呼叫太碎 | 取樣、NTT、與公鑰相乘、組裝密文全部在 PL，ARM 只送「訊息＋種子」、收「密文」 | `hardware/hls/v8b.cpp` |
| 2 | **frame 命令**：LOAD／PAIR／INVALIDATE／ENC | 一次乘法一個 ioctl | 一個 frame 最多 8 個密文，header、generation 與 trailer 一起驗證 | `hardware/hls/`、`software/driver/` |
| 3 | **公鑰快取在 PL** | 每次乘法重送公鑰 | LOAD 一次後留在 URAM；用 generation 與 INVALIDATE 管理有效性 | 同上 |
| 4 | **時脈 100 → 200 MHz** | 計算吞吐 | PS 的 PL0 維持 100 MHz，PL 內用 MMCM 產生 200 MHz；supervisor 用硬體計數器確認倍率是 2.000× 且 MMCM 已鎖定 | `hardware/system/` |
| 5 | **Montgomery 乘法管線化** | 200 MHz 時序不過（rev3 的 WNS −0.212 ns） | rev4 把 Montgomery 的三個乘法指定 DSP 管線延遲，WNS 變成 +0.035 ns；rev5 為 +0.087 ns | `hardware/hls/v8b.cpp` |
| 6 | **硬體亂數與取樣** | 取樣在 ARM、沒有安全等級 | ChaCha20 DRBG（8 級脈動式 double round）＋常數時間 CDT63 高斯＋ternary 拒絕取樣，全部在 PL | `hardware/hls/`、`hardware/frozen_math/` |
| 7 | **驅動重寫（ABI 8）** | 76% 的時間花在驅動檢查 | 改用可快取的緩衝區，加上明確的 CPU／DEVICE DMA 所有權與同步；5b 版本把係數檢查改成 128-bit NEON 向量迴圈，已證明全零時略過送出前的清零；檢查項目一個都沒少 | `software/driver/` |
| 8 | **ARM 端平行與 I/O 最佳化** | 編碼與網路成為新瓶頸 | 預先轉換的頻譜、持久 worker 執行緒、單一 TLS 擁有者、有界佇列與背壓 | `software/client/` |
| 9 | **稠密安全參數** | 稀疏 profile 沒有安全等級，無法和文獻比較 | uniform ternary 私鑰、σ = 4.5，依 HEStd_128_classic 估算最低 128.77 bits | `docs/security/` |
| 10 | **rev5 安全強化** | 秘密資料可能殘留在 PL | 種子只送取樣器並以零覆寫 FIFO；每個 ENC frame 後用零金鑰跑 ChaCha20 清除，並比對 RFC 8439 向量；TKEEP 檢查 | `hardware/REV5_HARDENING.diff` |

---

## 4. 效果 / Results

| 指標 | V1 | V8b | 說明 |
|---|---:|---:|---|
| 單位工作的時間 | 一次多項式乘法呼叫：**4.68 ms** | 一個完整密文（含 ARM 的正規化與編碼）：**1.18–1.25 ms** | 兩邊都是板上實測；V1 加密一個 4 特徵批次要 8 次乘法呼叫 |
| 和同板 CPU 比：延遲 | 0.65×（較慢） | **1.60×／1.81×**（AI4I／糖尿病進展） | V1：CDC 4 特徵、2048 筆單批；V8b：兩機有線端到端 |
| 和同板 CPU 比：吞吐 | 0.62×（較慢） | **1.64×／2.02×** | 同上 |
| 和現成函式庫比 | 沒有量 | OpenFHE 1.5.1 的加密時間是 PL 的 **9.07–10.69 倍** | 同一次開機、10 session、95% 信賴區間 |
| 單板持續吞吐 | 36,138 筆／秒（ARM＋FPGA，CDC 模型） | **832,396 筆／秒**（不含網路） | 模型與計時範圍不同，只能參考 |
| 長時間穩定性 | 6 小時、5,798,822 次 FPGA 工作 | 6 小時、**33.8 億筆**，最大誤差 4.7×10⁻⁴ | 工作單位不同，只能參考 |
| 能效 | 沒有量 | 每焦耳筆數是 CPU 的 **4.274 倍**（INA260，SOM 電源軌） | 專題唯一的實測功耗資料 |
| 應用 | 1 個模型 | **19 個模型**全部上板驗收 | 見 [../models/README.md](../models/README.md) |

---

## 5. 沒有變的 / What stayed the same

- **CKKS 環與模數**：N = 4096、2048 個 slot、三個 NTT 質數 998244353、1004535809、469762049（log₂Q ≈ 88.6）。
- **NTT 結構**：4 個蝶形通道 × 3 個 RNS 分量，Montgomery 模乘，PL 內不用通用除法器。
- **板子與環境**：KV260（XCK26）、Ubuntu 22.04、kernel 5.15.0-1027-xilinx-zynqmp、Vivado／Vitis HLS 2022.1。
- **原則**：板上操作只由人執行；失敗保留、修正只追加；每個數字都標明是板上、主機或文獻。

**相容性**：V8b 和 V1 的 bitstream、驅動 ABI、wire 格式與 CKKS profile 都**不相容**。V1 的驗收證據不轉移給 V8b，反之亦然。

---

## 6. release v1（rev4）和 rev5 的差異 / release v1 (rev4) vs rev5

rev5 只加安全強化，資料通道和 rev4 相同。

| 項目 | rev4（release v1） | rev5 |
|---|---|---|
| 種子路徑 | 取樣器暫存器保留最後一個種子，直到下一個 ENC frame | 種子只送進取樣器，之後用兩個零 token 覆寫控制 FIFO |
| ChaCha20 狀態 | 保留到下一次使用 | 每個 ENC frame 後用零金鑰跑一次，覆寫核心、金鑰與區塊暫存器；第 0 區塊必須等於 RFC 8439 零向量，否則回報 status 7 |
| FIFO | — | 雜訊、遮罩、訊息、trit FIFO 用零回填 |
| 輸入檢查 | — | 每個輸入 beat 的 TKEEP 必須全為 1，否則回報 `ST_LENGTH` |
| LUT／FF | 53,046（45.29%）／61,285（26.16%） | 54,468（46.51%）／62,291（26.59%） |
| DSP／BRAM36／URAM | 531／65.5／42 | 相同 |
| WNS／WHS | +0.035／+0.010 ns | +0.087／+0.009 ns |
| Vivado 功耗估計 | 4.911 W | 4.940 W |
| 板上驗收 | 效能、能效、6 小時長測、19 個模型 | 兩次開機的數學驗收（983,040 個字與參考值完全相同）與恢復流程；ZOO19 v3 執行並審查通過 |
| 效能 | 已量測 | **沒有重新量**（資料通道相同） |

**限制**：單一的管線暫存器沒有在原始碼層清除。這是駐留清單，不是實體清零的證明，也不是側通道防護。

完整的程式碼差異見 [../hardware/REV5_HARDENING.diff](../hardware/REV5_HARDENING.diff)。

