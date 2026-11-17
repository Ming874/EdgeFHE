# EdgeFHE V8：KV260 整段 CKKS 加密在 FPGA（V8b rev5）/ V8: Full CKKS Encryption in FPGA Logic

V8b rev5 把 CKKS 公鑰加密的 client 端整段做進 AMD Kria KV260（`xck26-sfvc784-2LV-c`）的 FPGA：亂數（ChaCha20 DRBG）、取樣、NTT、與公鑰相乘、組裝密文都在 PL 完成，ARM 只負責正規化、編碼、解碼和網路。資料在板上加密後才送出，雲端（原生 C++ server）只拿到密文，用公開權重算仿射模型後送回加密結果；解密與判斷都在資料擁有者這一端。這是研究原型，不是可部署的安全產品。

**English:** V8b rev5 moves the whole CKKS public-key encryption of the client into the KV260 programmable logic (ChaCha20 DRBG, sampling, NTT, public-key products and ciphertext assembly). A native C++ cloud evaluates public-weight affine models over mTLS and only ever sees ciphertexts.

> **未驗證分支 / Unverified branch（`feat/v8-full-pl-encryption`）**：`v8b_rev5/` 是放在 main 旁邊的獨立子專案。它**還沒經過 repository 維護者審查**，repository 的 CI **不會建置或測試**它，也**沒有從這個分支重新上板**。本頁的上板數字來自 rev4／rev5 原本的上板紀錄（`evidence/`）。本頁所有指令都在 `v8b_rev5/` 裡執行。
> *Not reviewed by the maintainers, not built or tested by CI, not re-run on the board from this branch. Run every command from inside `v8b_rev5/`.*

**名稱怎麼讀 / How to read the name**：

| 部分 | 意思 |
|---|---|
| **V8** | 第 8 代架構，接在 main 的 V3、V4、V5 之後。V6、V7 是中間的開發版，沒有進 main（見[第 4 節](#4-版本順序--version-order)） |
| **b** | V8 的第二個設計。V8a 是 HLS 原型，V8b 是實際上板的版本 |
| **rev5** | V8b 硬體核心（HLS）的第 5 次修訂。rev4 是第一個正式版本（release v1），rev5 加上安全強化 |
| **N = 4096** | CKKS 的環維度，和 main 相同 |

板上的 firmware 名稱 `edgefhe-v8b-r5` 和資料夾名稱 `v8b_rev5/` 都沿用這個版本代號，方便和上板紀錄對照。

*V8 = 8th architecture generation after main's V3–V5; b = second V8 design (V8a was the HLS prototype); rev5 = 5th revision of the V8b hardware core.*

**使用前先記住四件事 / Ground rules**

1. **研究原型**：沒有正式密碼稽核、側通道評估或認證。所有模型都只用公開資料訓練，不能用於臨床、授信、工業或保全決策。
2. **安全估算只是研究估算**：lattice-estimator 最低 128.77 bits 是依標準表做的估算，不是安全認證，也不涵蓋側通道與惡意雲端。
3. **分清楚數字來自哪一版**：效能與能效是 **rev4** 的板上量測；rev5 的資料通道與 rev4 相同，只做了正確性驗收，沒有重新量效能。main 的 V3／V5 數字不能轉移到這裡，反之亦然。
4. **板上動作只由人執行**：安裝 firmware、載入 bitstream、`insmod`、DMA 都由板端操作員執行；本資料夾的工具不會開啟任何裝置。

## 1. 現況 / Current status

| 項目 | 狀態 | 在哪裡驗證 |
|---|---|---|
| rev5 HLS 與系統實作 | routed 通過：200 MHz，WNS +0.087 ns、WHS +0.009 ns | **工具報告**（Vivado 2022.1，`hardware/reports/rev5/`） |
| rev5 上板數學驗收 | 兩次開機（`ce91708d`、`bba35b86`）：983,040 個輸出字與參考值完全相同，slot 最大誤差 1.02e-4；恢復流程（program → AFI → provider → proxy）通過 | **板上**（`evidence/rev5/`） |
| ZOO19 v3 | 19 個模型、12 個領域在 rev5 上執行，獨立審查與複審 PASS | **板上**（`evidence/rev5/zoo19v3_*.json`） |
| 效能、能效、長測 | 延遲、吞吐、能效、6 小時長測 | **板上，rev4**（`evidence/rev4/`、`evidence/CLAIMS_LEDGER.json`） |
| 主機建置與自我測試 | 從這個分支 `git clone` 後在 WSL 從零編譯，4 項自我測試 PASS，最後印出 `EDGEFHE_BUILD_AND_SELFTEST_PASS` | **主機**（x86 WSL，不開裝置） |
| 這個分支 | 未審查；不在 CI 範圍；沒有重新上板 | — |
| 沒測過 | 用這份原始碼重跑 HLS／Vivado、driver 編譯、前端測試 | — |

## 2. 與 main 的差異 / What changed compared with main

**一句話：** main 的 FPGA 只做「一次一個多項式乘法」（op2），而且在目前的介面下比 4 核 ARM 慢；V8b 把整段加密搬進 PL、公鑰快取在 PL、一次呼叫處理一整個 frame，結果 PL 比同板 CPU 快，取樣也換成有安全估算的稠密分布。

*In one line: main offloads one polynomial multiplication per call and is slower than the 4-core ARM; V8b moves the whole encryption into PL with a cached public key and becomes faster than the CPU, with a dense, estimated-secure sampling profile.*

### 2.1 對照表 / Side-by-side

| 項目 | main（V3 凍結基線／V5 對照平台） | 本分支 V8b rev5 |
|---|---|---|
| 環與模數 | N = 4096；q = 998244353、1004535809、469762049（log₂Q ≈ 88.6） | **相同** |
| PL 做什麼 | 只做 NTT／INTT 與多項式運算；CKKS 只用 op2（完整乘法），每次乘法一次 DMA 呼叫 | **整段加密**：ChaCha20 DRBG → 取樣 → NTT → 與公鑰相乘 → 組裝；命令 LOAD／PAIR／INVALIDATE／ENC，一個 frame 最多 8 個密文 |
| 公鑰 | 每次乘法重送並在 PL 重新轉換（main 所說的「組 B 結構劣勢」） | **快取在 PL**：LOAD 一次，之後重複使用 |
| 平行度與時脈 | 4 個蝶形通道，名目 100 MHz | 4 lane × 3 個 RNS limb，**200 MHz**（MMCM） |
| 資源（routed） | LUT 25.04%、DSP 753（60.34%）、BRAM36 62、URAM 0；WNS +0.004 ns | LUT 46.51%、DSP 531（42.55%）、BRAM36 65.5、URAM 42；WNS +0.087 ns |
| 取樣分布 | 稀疏功能 profile（s、u 各 64 個 ±1，e 各 16 個），**沒有**安全位元數 | **稠密**：uniform ternary 私鑰、ternary u、σ = 4.5 常數時間 CDT63 高斯；HEStd_128_classic，lattice-estimator 最低 128.77 bits |
| 亂數來源 | ARM 端作業系統亂數 | OS 熵播種、在 PL 執行的 ChaCha20 DRBG，每 frame 32 B 種子，用後清除 |
| scale | 輸入 2²⁴（V3）／2²⁹（V4、V5） | Δ = 2³⁰ |
| 驅動 | V3 DMA proxy（`edgefhe_rns3_n4096_dma_proxy`） | 5b 系列，**ABI 8**（`software/driver/`） |
| Client 與雲端 | V3 Python 流程；V4／V5 C++ client，兩機 mTLS 只在 127.0.0.1 演練 | 原生 C++ client、金鑰擁有者函式庫、正規化程式、雲端 server `efnet_server`；TLS 1.3 雙向認證＋憑證釘選，**兩機有線端到端已上板量測** |
| 模型 | CDC 糖尿病 4 特徵邏輯斯迴歸 | **ZOO19**：19 個仿射模型（邏輯斯／ridge 迴歸）、12 個領域 |
| 效能結論 | 目前介面下 ARM＋FPGA **比** 4 核 ARM 慢（單批延遲 0.65×、19 批吞吐 0.62×） | PL **比**同板 CPU 快：延遲 1.600×（AI4I）／1.811×（diabetes）；吞吐 1.641×／2.022×（rev4） |
| 其他量測 | 能效未量；V3 6 小時 soak 5,798,822 次 | 加密前處理 OpenFHE／PL 約 9.1–10.7×；單板持續吞吐 832,396 筆／秒；INA260 能效每焦耳筆數 4.274×；6 小時長測 33.8 億筆、最大誤差 4.7e-4（以上皆 rev4） |

### 2.2 新增 / Added

- **整段加密的 HLS 核心**（`hardware/hls/`）與位元精確的 C++ 參考模型（`hardware/ref/`）。ChaCha20 與 CDT63 的標頭和軟體端逐位元相同（`hardware/frozen_math/`）。
- **新的系統設計**（`hardware/system/`）：block design、supervisor RTL、建置 Tcl；routed 報告（`hardware/reports/rev5/`）。
- **新的 firmware 描述**（`hardware/firmware/`）：device-tree overlay、bootgen `.bif`、xmutil `shell.json`、只由人執行的安裝腳本。bitstream 與 `.dtbo` 不進 git，改附在 GitHub Release。
- **rev5 安全強化**（審查項目 H2／H3／H4；改動見 `hardware/REV5_HARDENING.diff`，相對於 rev4）：
  - 種子只送進取樣器，之後用兩個零 token 覆寫控制 FIFO。
  - 每個 ENC frame 結束後，用零金鑰跑一次 ChaCha20，覆寫核心與金鑰暫存器，並比對 RFC 8439 的零向量（不符回報 status 7）。
  - 雜訊、遮罩、訊息與 trit FIFO 用零回填。
  - 每個輸入 beat 的 TKEEP 必須全為 1，否則回報 `ST_LENGTH`。
  - 限制：單一的管線暫存器沒有在原始碼層清除，這是駐留清單，不是實體清零的證明。
- **ABI 8 驅動**（`software/driver/`）與裝置／規格／效能分析套件（`software/bench/`）。
- **原生 C++ client 與雲端 server**（`software/client/`），附 4 項自我測試：傳輸位元組雜湊、呼叫許可、wire 與 padded affine、應用等價。
- **ZOO19 模型庫**（`models/`）：訓練程式、19 個模型與公開測試子集、模型卡；[models/README.md](models/README.md) 列出每個資料集的出處、授權與外部對照。
- **展示用網頁前端**（`software/frontend/`，Vite＋TypeScript）。
- **文件與證據**：
  - 文件（`docs/`）：參數總表、架構與數學審查（附數學檢查與雜訊模擬的結果檔）、安全參數評估（`estimate_rlwe.py` 可重現 `ESTIMATE.json`）、參考基準、研究報告 PDF。
  - 證據（`evidence/`）：rev5 上板驗收、rev4 審查摘要、數字帳本、數字稽核、SBOM。
- **工具**：`tools/build_and_selftest.sh`（一鍵建置＋自我測試）、`tools/verify_repo.py`（逐檔比對 `SHA256SUMS`）、`tools/update_sums.py`（改檔後重算 `SHA256SUMS`）。

### 2.3 修改 / Changed（相對於 main 的同類元件）

- **硬體／PL**：從 op2 黑盒乘法器換成整段加密的資料通道，時脈從 100 MHz 提高到 200 MHz。
- **程式介面**：從「每次乘法一個 ioctl」換成「每個 frame 一次呼叫」，公鑰改為常駐 PL。
- **CKKS 參數**：稀疏功能 profile 換成有安全估算的稠密分布；scale 從 2²⁴／2²⁹ 改成 2³⁰。V8b 與 main 的 V3／V4／V5 **不 wire-compatible**，bitstream 也不同。
- **網路**：V3 的 TLS＋token 改成 TLS 1.3 雙向認證＋憑證釘選，並在兩台機器間實際量測。
- **建置腳本**：Windows 建置腳本改用相對路徑，可用參數或 `VIVADO_BIN` 指定位置；報告標頭的本機路徑改成相對路徑。

### 2.4 沒有帶過來 / Not carried over

- **main 原有的內容都沒有動，也沒有被這裡取代**：V3 凍結基線、V4／V5、dashboard、CDC 資料與訓練、`results/`、`validation/`、release 收據。V8b 不依賴它們。
- **rev4 與 V8a 的硬體原始碼**：被 rev5 取代，差異保留在 `hardware/REV5_HARDENING.diff`。
- **開發過程中的其他檔案**：
  - Windows 原生 server 移植版：依賴另一份舊原始碼與雜湊鎖，無法在這裡測試。
  - live demo 主控台：綁定當時的憑證與開機。
  - 只對某次開機有效的上板交付腳本與 QA 收據。
  - 4 支會讀開發紀錄其他資料夾的分析腳本：只收錄它們的結果檔。

### 2.5 這個分支對 repository 的改動 / Files touched in this branch

| 路徑 | 改動 |
|---|---|
| `v8b_rev5/` | **新增**：本子專案 |
| `README.md`（根目錄） | 開頭加一段分支說明，連到本頁 |
| `release/SHA256SUMS` | 用 main 的 `tools/update_manifest.py` 重新產生，納入新檔案；`tools/verify_repository.py` 通過 |

main 的其他檔案、凍結副本與收據都沒有改動。

### 2.6 和外部研究、現成函式庫比較 / Compared with published work and libraries

數字取自 [docs/reference_points/REFERENCE_POINTS_zh-TW.md](docs/reference_points/REFERENCE_POINTS_zh-TW.md)，文獻數字都標了出處頁碼。只有**同一塊板、同一組參數、同一種工作量**的比較才叫「加速比」；和論文的比較只能叫「定位」。本節的我方數字都是 **rev4** 的板上量測，rev5 的資料通道相同。

**同板比較：和現成函式庫 OpenFHE 比**（同一次開機 `e26925f5`，10 個 session，95% 信賴區間）

時間包含正規化、編碼與加密，每個特徵一個密文。

| 模型／筆數 | OpenFHE 1.5.1（3 執行緒） | 本專案 CPU（3 執行緒） | 本專案 PL | OpenFHE ÷ PL |
|---|---:|---:|---:|---:|
| 工業維護 · 1 筆 | 44.9 ms | 13.4 ms | **4.91 ms** | **9.14×** [9.10, 9.17] |
| 工業維護 · 2048 筆 | 45.4 ms | 13.4 ms | **5.01 ms** | **9.07×** [9.05, 9.08] |
| 糖尿病進展 · 1 筆 | 114.0 ms | 32.5 ms | **10.7 ms** | **10.69×** [10.67, 10.72] |
| 糖尿病進展 · 2048 筆 | 114.5 ms | 34.4 ms | **11.9 ms** | **9.67×** [9.61, 9.72] |

**和其他研究定位：每個密文的加密時間**

| 方案 | 平台 | 參數（N） | 加密／每密文 | 證據層級 |
|---|---|---|---:|---|
| **本專案 PL** | KV260：A53 1.33 GHz＋PL 200 MHz | 4096 | **1.18–1.25 ms** | 我方板上實測 |
| 本專案 CPU（3 執行緒） | KV260 A53 | 4096 | 3.34–3.44 ms | 我方板上實測 |
| OpenFHE 1.5.1（3 執行緒） | KV260 A53 | 4096 | 11.35–11.45 ms | 我方同板實測 |
| SEAL-Embedded | Cortex-A7 494 MHz | 4096 | 91.27 ms | 文獻（轉引 Aloha-HE 表 I） |
| Aloha-HE | Kintex-7 FPGA 200 MHz | 8192（換算到 4096 約 0.86 ms） | 1.87 ms | 作者實機 |
| Aloha-HE | Zynq-7000 PL 130 MHz | 8192（換算到 4096 約 1.40 ms） | 3.04 ms | 作者實機 |
| RISE | 12 nm ASIC 1 GHz | 8192 | 約 20 ms | 作者模擬 |

**這張表能說什麼**

- **比現成軟體快**：同一塊板上，OpenFHE 的加密時間是本專案 PL 的 **9–11 倍**。這是正式的同板因果比較。
- **不是最快、也不是最省面積的加密核心**：
  - 換算到相同參數後，Aloha-HE 約 0.86 ms，比我們的 1.2 ms 略快，兩者在同一個量級。
  - Aloha-HE 只用 56 個 DSP，我們用 531 個，DSP 面積效率約低 12 倍。
- **我們多做的是整套系統**，上表的文獻都沒有這些數據：
  - 兩機含網路與 TLS 的端到端量測：延遲快 1.60–1.81 倍、吞吐快 1.64–2.02 倍。
  - 6 小時長測：33.8 億筆，最大誤差 4.7×10⁻⁴。
  - INA260 實測能效。
  - 19 個模型的應用驗收。
- **同板的相關研究**：Guerrini 等（DATE 2026）也用 KV260，但目前只能取得摘要，沒有可核對的數字，所以不列入比較。

**換算方式與限制**

- 換算以 NTT 工作量 N·log₂N·模數個數為準，只能當推導。
- SEAL-Embedded 與 Aloha-HE 的處理器不同，只能定位。
- 文獻數字沒有變異量，不能做統計比較。

模型準確度和外部資料的對照見 [models/README.md](models/README.md#3-與外部資料對照--comparison-with-external-results)。

### 2.7 和 V1 的詳細差異：優化了什麼 / What changed since V1

**V1** 是第一版上板驗收的 FPGA 設計：N = 4096 的 NTT 加速器，100 MHz，也就是 main 的 V3 與 V5 上板時使用的硬體。注意它和 **release v1**（V8b rev4 的正式發布版）是不同的東西。完整說明、數字出處與 rev4 → rev5 的差異見 [docs/CHANGES_FROM_V1_zh-TW.md](docs/CHANGES_FROM_V1_zh-TW.md)。

**V1 慢在哪裡**：一次 FPGA 乘法呼叫要 4.68 ms，其中 **76%**（3.59 ms）花在驅動逐筆檢查係數，DMA 加 PL 計算只占 0.55 ms。另外還有三個結構問題：

- 加密 4 個特徵要 8 次呼叫。
- 每次都重送公鑰。
- 取樣和組裝留在 ARM。

結果 ARM＋FPGA 比 4 核 ARM 還慢。

**V1 → V8b 的 10 項優化**

| # | 優化 | 解決的問題 |
|---:|---|---|
| 1 | 整段加密搬進 PL（取樣、NTT、與公鑰相乘、組裝） | ARM 仍要做大部分工作 |
| 2 | frame 命令，一次呼叫最多 8 個密文 | 一次乘法一個 ioctl |
| 3 | 公鑰快取在 PL | 每次乘法重送公鑰 |
| 4 | 時脈 100 → 200 MHz（PL 內 MMCM） | 計算吞吐 |
| 5 | Montgomery 乘法管線化 | 200 MHz 時序不過（rev3 WNS −0.212 ns → rev5 +0.087 ns） |
| 6 | ChaCha20 DRBG 與常數時間取樣搬進 PL | 取樣在 ARM、沒有安全等級 |
| 7 | 驅動重寫（ABI 8）：可快取緩衝區、明確 DMA 同步、NEON 向量化檢查 | 76% 的時間花在驅動 |
| 8 | ARM 端持久 worker、單一 TLS 擁有者、有界佇列 | 編碼與網路成為新瓶頸 |
| 9 | 稠密安全參數（σ = 4.5，估算最低 128.77 bits） | 稀疏 profile 無法和文獻比較 |
| 10 | rev5 安全強化（清除種子、金鑰與 FIFO，TKEEP 檢查） | 秘密資料殘留在 PL |

**效果**

| 指標 | V1 | V8b |
|---|---:|---:|
| 和同板 CPU 比：延遲 | 0.65×（較慢） | **1.60–1.81×**（較快） |
| 和同板 CPU 比：吞吐 | 0.62×（較慢） | **1.64–2.02×**（較快） |
| 單位工作時間 | 一次乘法呼叫 4.68 ms | 一個完整密文 1.18–1.25 ms |
| 時序餘裕 | +0.004 ns @100 MHz | +0.087 ns @200 MHz |
| DSP | 753（60.34%） | 531（42.55%） |
| 安全估算 | 無 | 最低 128.77 bits |
| 能效實測 | 無 | 每焦耳筆數 4.274 倍 |
| 模型 | 1 個 | 19 個 |

V8b 的效能是 rev4 的板上量測，rev5 的資料通道相同。V1 與 V8b 的工作負載不完全一樣，逐項條件見上面的文件。

## 3. 資料夾地圖 / Folder map

```text
v8b_rev5/
├── README.md SECURITY.md NOTICE LICENSE   入口、安全邊界、來源與授權（Apache-2.0）
├── CONTRIBUTING.md CITATION.cff           貢獻方式、引用
├── hardware/
│   ├── hls/              rev5 Vitis HLS 核心（v8b.cpp／.hpp、tables、testbench、csim／cosim／export Tcl）
│   ├── frozen_math/      ChaCha20 與 CDT63 標頭（與軟體端逐位元相同）
│   ├── ref/              位元精確的 C++ 參考模型
│   ├── system/           Vivado block design、supervisor RTL、系統建置 Tcl 與啟動腳本
│   ├── firmware/         DTS、bootgen .bif、xmutil shell.json、板端安裝腳本（人執行）
│   ├── reports/rev5/     routed 資源、時序、DRC、CDC、methodology 報告
│   └── REV5_HARDENING.diff   rev4 → rev5 的改動
├── software/
│   ├── driver/           Linux 核心模組（ABI 8）
│   ├── client/           原生 client、金鑰擁有者、正規化、雲端 server、自我測試、凍結 SDK
│   ├── bench/            裝置／規格與效能分析套件
│   └── frontend/         展示用網頁前端（Vite＋TypeScript）
├── models/               README：19 個模型、資料來源與授權、與外部資料對照
│   └── zoo19/            訓練程式、模型權重、公開測試子集、模型卡
├── docs/                 與 V1 的差異、參數、架構與數學審查、安全評估、參考基準、相依套件、參考文獻、研究報告
├── evidence/             rev5 上板驗收、ZOO19 審查、rev4 審查摘要、數字帳本、數字稽核、SBOM
├── tools/                build_and_selftest.sh、verify_repo.py、update_sums.py
└── licenses/             第三方授權原文
```

每個檔案的來源記在 [docs/PROVENANCE.json](docs/PROVENANCE.json)。

## 4. 版本順序 / Version order

每一代都在前一代的基礎上改一件主要的事。數字越大越新。

| 順序 | 版本 | 時間（2026） | 主要改變 | 在哪裡 |
|---|---|---|---|---|
| 1 | **V3** | 10 月上旬 | 使用 **V1 硬體**（第一版上板的 FPGA）：只做 NTT 與多項式乘法（op2），100 MHz；Python 流程；稀疏功能 profile | main（凍結基線） |
| 2 | **V4** | 10-09 前後 | 改用原生 C++17 CKKS client 與 mTLS 協定 | main |
| 3 | **V5** | 10-09～10-14 | 組 A（純 CPU）／組 B（CPU＋FPGA）兩種後端對照；上板結果是 CPU 較快 | main |
| 4 | V6 | 10-09 前後 | CPU 端批次最佳化 | 開發紀錄，未收錄 |
| 5 | V7 | 10-09～10-14 | 換成稠密取樣（ternary 私鑰、σ = 4.5、Δ = 2³⁰）並做安全估算；PAIR4 管線，120 jobs/s，是 CPU 3 執行緒的 2.12× | 開發紀錄，未收錄 |
| 6 | V8a | 10-15 | 「整段加密在 FPGA」的 HLS 原型 | 開發紀錄，未收錄 |
| 7 | V8b rev1–rev3 | 10-15～10-16 | V8b 硬體核心的早期修訂，資源或時序沒過（例如 rev3 在 200 MHz 的 WNS −0.212 ns） | 開發紀錄，未收錄 |
| 8 | V8b rev4 | 10-16 起 | 第一個通過 route（200 MHz）並上板通過的版本；效能、能效、6 小時長測都在這版量測；release v1 | 只收錄差異檔與量測摘要 |
| 9 | **V8b rev5** | 10-29 | rev4 加上安全強化（清除 FIFO 與金鑰、TKEEP 檢查），兩次開機的數學驗收 | **本分支 `v8b_rev5/`** |

各版完整參數見 [docs/PARAMETERS_zh-TW.md](docs/PARAMETERS_zh-TW.md)。

## 5. 從哪裡開始 / Where to start

| 角色 | 先讀 | 接著做 |
|---|---|---|
| 第一次看 | 本頁第 2 節 → [CHANGES_FROM_V1](docs/CHANGES_FROM_V1_zh-TW.md) → [PARAMETERS](docs/PARAMETERS_zh-TW.md) → [研究報告](docs/report/EdgeFHE_Research_Report_20261025_zh-TW.md) | 跑第 7 節的驗證與自我測試 |
| 軟體 | [software/client](software/client/)、[DEPENDENCIES](docs/DEPENDENCIES_zh-TW.md) | `bash tools/build_and_selftest.sh` |
| 硬體 | [ARCHITECTURE_MATH_REVIEW](docs/ARCHITECTURE_MATH_REVIEW_zh-TW.md)、[REV5_HARDENING.diff](hardware/REV5_HARDENING.diff) | 第 7 節的 HLS／Vivado 步驟 |
| 板端操作員 | [hardware/firmware](hardware/firmware/) | 由人執行 `install_fw_r5_user_only.sh`；失敗就停、保留紀錄、不自動重試 |
| 模型 | [models/README.md](models/README.md)（模型一覽、資料來源、外部對照） | 細節見 [MODEL_ZOO](models/zoo19/MODEL_ZOO_zh-TW.md)；訓練程式 `models/zoo19/train_zoo.py` |
| 寫報告 | [CLAIMS_LEDGER](evidence/CLAIMS_LEDGER.json)、[SECURITY_ASSESSMENT](docs/security/SECURITY_ASSESSMENT_zh-TW.md)、[REFERENCE_POINTS](docs/reference_points/REFERENCE_POINTS_zh-TW.md) | 每個數字都標明 rev4 或 rev5、板上或主機 |

## 6. 文件索引 / Document index

| 文件 | 內容 |
|---|---|
| [docs/CHANGES_FROM_V1_zh-TW.md](docs/CHANGES_FROM_V1_zh-TW.md) | 從 V1 到 V8b rev5 的差異、10 項優化與效果；release v1（rev4）和 rev5 的差異 |
| [docs/PARAMETERS_zh-TW.md](docs/PARAMETERS_zh-TW.md) | 各版本的 CKKS 參數、硬體資源、上板結果 |
| [docs/ARCHITECTURE_MATH_REVIEW_zh-TW.md](docs/ARCHITECTURE_MATH_REVIEW_zh-TW.md) | 架構與數學審查（精確整數檢查、雜訊模擬） |
| [docs/security/SECURITY_ASSESSMENT_zh-TW.md](docs/security/SECURITY_ASSESSMENT_zh-TW.md) | 安全參數評估與限制 |
| [docs/reference_points/REFERENCE_POINTS_zh-TW.md](docs/reference_points/REFERENCE_POINTS_zh-TW.md) | 對照基準 |
| [docs/DEPENDENCIES_zh-TW.md](docs/DEPENDENCIES_zh-TW.md) | 相依套件與版本 |
| [docs/report/EdgeFHE_Research_Report_20261025.pdf](docs/report/EdgeFHE_Research_Report_20261025.pdf) | 研究報告（release v1） |
| [models/README.md](models/README.md) | 19 個模型一覽、每個資料集的建立者／DOI／授權、與原始論文的對照 |
| [models/zoo19/MODEL_ZOO_zh-TW.md](models/zoo19/MODEL_ZOO_zh-TW.md) | 模型准入規則、本機與板上誤差、雜訊模型一致性 |
| [SECURITY.md](SECURITY.md)、[NOTICE](NOTICE)、[LICENSE](LICENSE) | 安全邊界、來源與授權 |
| [../README.md](../README.md) | main 的入口（V3／V4／V5） |

## 7. 常用指令 / Quick commands

以下指令都不碰板子。

**驗證檔案完整：**

```bash
python3 tools/verify_repo.py
```

應該印出 `REPO_VERIFY_PASS`。

**建置與自我測試（Linux／WSL）：** 需要 CMake ≥ 3.20、g++-11、OpenSSL 3、Python 3、OpenFHE 1.5.1。

```bash
bash tools/build_and_selftest.sh "$HOME/edgefhe-build" "$HOME/.local/openfhe-v1.5.1/lib/OpenFHE"
```

這支腳本會：

1. 建置金鑰擁有者、正規化程式、client、雲端 server 與 bench 套件。
2. 跑 4 項自我測試。
3. 全部通過時，最後印出 `EDGEFHE_BUILD_AND_SELFTEST_PASS`。

**硬體建置（Vitis HLS／Vivado 2022.1）：**

1. 在 `hardware/hls/` 執行 `vitis_hls -f run_v8b_csim.tcl`（C 模擬），再執行 `vitis_hls -f run_v8b_export.tcl` 匯出 IP。
2. 建置系統：
   - Windows：`hardware/system/run_system_rev5.bat [IP_REPO] [BUILD_DIR]`。
   - Linux：`vivado -mode batch -source build_v8b_system_v3.tcl -tclargs <build> <ip_repo> <sources>`。
3. 用 `hardware/firmware/run_bootgen.bat` 轉換 bitstream，再把 `.dts` 編譯成 `.dtbo`。

**改了 `v8b_rev5/` 的檔案之後**，兩份清單都要更新：

```bash
python3 -B tools/update_sums.py && python3 tools/verify_repo.py
```

```bash
cd .. && python3 tools/update_manifest.py && python3 tools/verify_repository.py
```

## 8. 範例值 / Example values

公開前已把本機資訊換成範例值，所以部分歷史證據檔的 SHA-256 和原件不同：

| 範例值 | 代表 |
|---|---|
| `192.0.2.x` | RFC 5737 文件專用的範例位址 |
| `C:\Xilinx` | AMD 工具的安裝位置 |
| `example` | 使用者名稱 |
| `ubuntu@kria` | KV260 Ubuntu 的預設帳號與主機名稱 |

## 9. 限制 / Limitations

- 只支援仿射深度 1 的模型，沒有密文 sigmoid、rotation、rescale 或 bootstrapping。
- FHE 保護輸入與結果，不保護模型：持有金鑰的人可以靠查詢推測模型。
- 沒有做側通道評估；PL 的清除機制（H2／H3／H4）不等於側通道防護。
- 只用公開資料，沒有實際場域驗證。
- 雲端 server 只在 Linux／WSL 建置；Windows 原生移植版沒有收錄。

## 10. 授權與聲明 / License and notice

- **專案程式碼與文件**：Apache License 2.0（[LICENSE](LICENSE)、[NOTICE](NOTICE)）。
- **第三方元件**：依各自的授權（OpenFHE 為 BSD-2-Clause，OpenSSL 為 Apache-2.0，AMD 工具與產生的 IP 依 AMD 條款），授權原文在 `licenses/`，清單見 `evidence/SBOM.cdx.json`。
- **資料集**：18 個來自 UCI Machine Learning Repository，全部是 CC BY 4.0；另 1 個是 scikit-learn 內附的糖尿病資料集（BSD-3-Clause）。每個資料集的建立者、DOI 與授權見 [models/README.md](models/README.md#4-資料來源與授權--data-sources-and-licences)。本專案清理資料、選取欄位並訓練模型，不代表資料集作者背書。
- **安全問題**：請用 GitHub 的私下漏洞回報，不要把金鑰或憑證貼到公開 issue（見 [SECURITY.md](SECURITY.md)）。
