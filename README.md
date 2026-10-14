# EdgeFHE：KV260 邊緣端 CKKS 算術加速器 / Edge CKKS Arithmetic Accelerator

EdgeFHE 讓一塊 AMD Kria KV260 開發板（`xck26-sfvc784-2LV-c`）自己完成 CKKS 同態加密的 client 端工作：ARM 處理器負責 CKKS 編碼／解碼、用作業系統亂數取樣、組裝密文；FPGA 負責最重的多項式乘法（三個 RNS 分量的 NTT／INTT）。資料在板上加密後才送出，雲端只拿到密文，用公開模型算「Σ 權重×密文＋偏差」後送回加密結果；解密、sigmoid 與判斷都在板上，私鑰只存在板上。這是功能研究版本，不是可部署的安全產品。

**English:** EdgeFHE is a hardware/software co-designed CKKS client on the AMD Kria KV260: the ARM cores encode, sample and assemble ciphertexts, the FPGA runs the NTT polynomial multiplication, and the cloud only ever sees ciphertexts.

這個資料夾就是整個專題：程式碼、硬體、文件、資料、訓練、上板結果和 dashboard 都在同一個 repository，沒有另外的子 repo（2026-10-14 起；之前程式碼放在 `EdgeFHE-KV260-GitHub/` 子資料夾）。

**使用前先記住四件事 / Ground rules**

1. **正式 release 尚未接受**（`formal_release_accepted=false`）。不得宣稱 100% 完成、特定安全位元數、零洩漏、密文 sigmoid 或任意深度 FHE。
2. **取樣器是稀疏的功能 profile，沒有任何安全位元數宣稱。** 固定種子（`--qa-seed`）的金鑰任何人都能重算，只能用於測試。
3. **一律分清「已在板上驗證」與「只在主機驗證」。** 主機上的「FPGA」數字來自模擬裝置，是 CPU 時間；V3 的實板 PASS 不轉移給 V4／V5。
4. **板上動作只由板端操作員執行**（載入 FPGA、`insmod`、開啟裝置、DMA）。失敗就停、保留紀錄、不自動重試；程式只接受操作員通過 gates 後交付的 fd。

## 1. 現況 / Current status

更新日期 2026-10-14；證據與關卡細節見 [VALIDATION](docs/VALIDATION.md)，上板紀錄見 [results/2026-10-14-kv260](results/2026-10-14-kv260/NOTE.md)。

| 項目 | 狀態 | 在哪裡驗證 |
|---|---|---|
| V3 凍結基線 | 正常路徑已接受（歷史關卡） | **板上**：實板＋兩機（固定 6 例）、6 小時 5,798,822 次 soak；G07／G08／G12 部分完成 |
| V4 原生候選 | 主機收據 | **只在主機**：x86 WSL 與 loopback mTLS；[NATIVE_CANDIDATE](release/NATIVE_CANDIDATE.json) 的 aarch64、native→PL 欄位仍是 `false` |
| V5 主機檢查 | 通過 | **主機**（x86 WSL）：最佳化版、ASan/UBSan、TSan、CTest 9/9、C ABI 對 V4、mTLS loopback（[收據](release/native_v5/local_acceptance.json)） |
| V5 第一次上板 | 通過 | **板上**：部署關卡 A（只缺不需要的 cmake）、預編譯驅動身分、C–G；aarch64 單元測試 24 項、C ABI；`v5_parity --fd 3 --cases 64` → `V5_PARITY_PASS device=linux-dma`，CPU↔FPGA 786,432 個係數逐位元相同 |
| A/B 完整量測（階段 2） | 完成 | **板上** `V5_STAGE2_COMPLETE`：六種設定逐位元相同；目前的 FPGA 介面下 4 核 CPU 較快（單批 60.5 vs 93.0 ms、吞吐量 58,558 vs 36,138 筆/秒），每次 FPGA 呼叫 4.7 ms 裡 DMA＋PL 只占 0.54 ms。詳見 [COMPARISON](docs/COMPARISON.md) |
| 平行編碼與每次呼叫拆解 | 完成 | **板上**：步驟 ② 四執行緒編碼 31.4 → 8.3 ms（`--encode-workers 4`，逐位元相同），單批 cpu-w4 37.4 ms、fpga-overlap 69.9 ms。用 ftrace 拆開每次 FPGA 呼叫（不改驅動）：3.59 ms（76%）是驅動在不經快取的緩衝區逐筆檢查係數，0.45 ms 是搬出，DMA＋PL 0.56 ms |

### 階段 / Stages

| 階段 | 目標 | 狀態 |
|---|---|---|
| 0 主機驗證 | V5 模組化、主機檢查與收據 | 完成（x86 WSL） |
| 1 ARM 基準 | V5 在板上編譯、自檢、量測組 A | 編譯與單元測試已過；量測尚未產出 |
| 2 FPGA 串接與對照 | 實板一致性；兩組六種設定同場量測 | 完成（2026-10-14） |
| 3 兩機展示 | KV260 ↔ 電腦，真實網路 mTLS | 只在 127.0.0.1 演練過 |
| 4 工程收尾 | G07／G08／G12、安全評估、功耗、ROM 對照 | 未開始 |

### 下一步 / Next

- 另立新版驅動：在可快取的記憶體上檢查與搬出。估計每次呼叫可省約 3.9 ms，組 B 單批可到約 34 ms；這是估算，要實測（[COMPARISON](docs/COMPARISON.md)）。
- 評估合併呼叫（會議簡報問題一的選項 C）。
- 長時間 soak 與故障測試。

### 已知缺口 / Known gaps

- **G07** 實體 reset、**G08** 致命故障生命週期、**G12** 乾淨環境重建與正式 release 都只部分完成；故障處理草稿 `software/driver_candidate_v4/` 不可部署。
- **階段 3** 兩機展示還沒在真實網路上跑過（板子目前沒有網路）。
- **安全 profile**：稀疏取樣器沒有安全估算；side-channel、惡意 evaluator、constant-time 都未驗收。
- **組 B 結構劣勢**：現有 FPGA 只有完整乘法（op2），每次都重送並轉換公鑰（每分量 24 次轉換，CPU 路徑 14 次）；要消除需要新的 HLS 運算與新的板測驗收。
- **不在目前範圍**：多方門檻 CKKS、密文 sigmoid、rotation／rescale／多層模型、bootstrapping。

## 2. 資料夾地圖 / Folder map

標記：**[凍結]** 與開發紀錄 byte-exact，由 `release/PROVENANCE.json` 鎖定；**[釘住]** 原始碼雜湊被收據釘住，不可直接改；**[可改]** 改完照第 6 節更新收據與清單；**[證據]** 實測紀錄，不手改。

```text
Grad/                                repository 根目錄
├── README.md SECURITY.md NOTICE.md  [可改] 入口、安全邊界、來源說明
├── LICENSE                          [凍結] Apache-2.0 原文
├── CMakeLists.txt requirements.txt  [可改] 根建置（9 個 CPU-only CTest）；Python 只用標準庫
├── hardware/hls/                    [凍結] HLS NTT 核心（3 分量 × 4 蝶形通道、Montgomery）
├── hardware/ip/ hardware/vivado/    [凍結] 已接受的 IP、block design 重建腳本
├── firmware/ii1_n4096/              [凍結] bitstream、DTBO、執行期契約
├── firmware/prebuilt/               [凍結] 已接受的 V3 .ko，只適用單一 kernel
├── software/driver/                 [凍結] 已接受的 V3 Linux DMA 驅動
├── software/driver_candidate_v4/    [草稿] 故障生命週期草稿，不可部署
├── software/ckks/                   [凍結] V3 Python 流程＋backend.cpp
├── software/reference/              [凍結] C++ 精確 NTT 參考實作
├── software/board_tests/            [凍結] 板上 soak／併發／usercopy 測試程式
├── software/kv260/                  [凍結] 只讀檔案的 FDT／載入紀錄檢查
├── software/native_ckks/            [釘住] V4：C++17 CKKS client、C ABI、mTLS 協定
├── software/native_v5/              [可改] V5：組 A cpu／組 B fpga 後端、管線、量測工具、C ABI
├── validation/vectors/              [凍結] 16 組 OpenFHE 公開向量與獨立 Python 驗證
├── validation/openfhe_client/       [釘住] 標準 OpenFHE CPU 對照
├── evidence/                        [凍結] 驗收矩陣、索引與原始紀錄
├── release/                         [工具維護] 版本、來源、收據、SHA256SUMS
├── tools/                           [可改] 檢查、建置、A/B 對照、上板打包；kv260/ 是 Windows 序列埠工具
├── docs/                            [可改] 六份技術文件（第 5 節）＋提案、進度報告、會議與分析筆記
├── data/                            [可改] CDC 糖尿病資料的說明與 manifest；原始檔與切分不進 git
├── training/                        [可改] 訓練 4 特徵邏輯斯迴歸，驗證 CKKS 推論與明文一致
├── results/                         [證據] 從 KV260 帶回的量測結果，每次上板一個資料夾
├── dashboard/                       [可改] Next.js 本機網頁：把 data／training／results 畫成圖
└── .github/workflows/offline.yml    [可改] CPU-only CI，不碰板子
```

- **看起來重複、但不能刪**：`software/` 下 `reference`、`ckks`、`board_tests` 三份相同的 `paired_public_vectors.cpp`，各被同目錄的凍結程式 `#include`；兩個驅動目錄的 `Makefile` 相同是核心模組建置的要求。
- **不進 git**：CDC 原始檔與切分（`data/*/raw/`、`data/*/splits/`，用 `tools/prepare_datasets.py` 依固定種子重建，`data/cdc_diabetes/manifest.json` 有 SHA-256）、`build/`、`dashboard/node_modules/`；私鑰與 token 放 `local/`。Ubuntu image、kernel headers、AMD 工具、OpenFHE 本體都不在這裡。
- **dashboard**：`cd dashboard && npm install && npm run dev`，打開 http://localhost:3000。顯示資料集與特徵挑選、模型表現（ROC、混淆矩陣）、CKKS 加密推論與明文的比對，以及 `results/` 最新的 A/B 對照報表。它只讀檔案、不碰板子，是開會與報告時看結果用的（[說明](dashboard/README.md)）。

## 3. 版本層 / Version layers

| 層 | 內容 | 驗證到哪裡 | 能否修改 |
|---|---|---|---|
| **V3 凍結基線** | HLS NTT 核心、DMA 驅動、韌體、Python 流程（scale 2^24、TLS＋token） | 板上：實板＋兩機、6 小時 soak | **不可**：101 個 byte-exact 副本由 [PROVENANCE](release/PROVENANCE.json) 鎖定 |
| **V4 原生候選** | 完整 C++17 CKKS client（scale 2^29）、mTLS 協定、OpenFHE 對照 | 只在主機：x86 WSL＋loopback | **不可直接改**：雜湊被 [NATIVE_CANDIDATE](release/NATIVE_CANDIDATE.json) 與 `release/native_v4/` 收據釘住 |
| **V5 後端與對照平台** | 組 A／組 B 後端、執行緒池、雙緩衝 DMA、N 段管線、步驟 1–10 量測、C ABI、兩機 client | 主機全套檢查；板上：單元測試、C ABI、實板一致性、A/B 量測 | **可改**：改完重跑 `tools/run_v5_checks.py` 更新[收據](release/native_v5/local_acceptance.json) |

V5 原樣重用 V4 的數學原始碼（不複製、不修改），結果與 V4 逐位元相同；所以要改進架構就改 V5，V3／V4 的驗收證據保持有效。V3 與 V4 的 profile、scale 與協定互不相容。

## 4. 從哪裡開始 / Where to start

| 角色 | 先讀 | 接著做 |
|---|---|---|
| 新組員 | 本頁 → [ARCHITECTURE](docs/ARCHITECTURE.md) → [DEVELOPMENT](docs/DEVELOPMENT.md)；沒有數學背景先看[數學背景入門](https://claude.ai/code/artifact/27db100b-b8f2-46e8-9d6a-26e78e9b0383)（線上文件） | 跑第 6 節的檢查（Python 部分 Windows 也能跑） |
| 組 A（純軟體平行） | [COMPARISON](docs/COMPARISON.md)、[V5 README](software/native_v5/README.md) 第 5 節 | 改 `software/native_v5/backend/cpu/`，新後端到 `backend/factory.cpp` 註冊；用 `v5_e2e` 比較 `cpu-w1`～`cpu-w4` |
| 組 B（軟硬體協同） | [COMPARISON](docs/COMPARISON.md)、[V5 README](software/native_v5/README.md)、[ARCHITECTURE](docs/ARCHITECTURE.md) 的介面 | 改 `backend/fpga/`；實板量測由操作員跑 `run_comparison.py --stage 2`；新 HLS 運算＝新 bitstream＝新板測驗收 |
| 板端操作員 | [BOARD_GUIDE](docs/BOARD_GUIDE.md)（gates A–H、上板當天清單）、[序列埠工具](tools/kv260/README.md) | 依序過 gates，失敗就停；通過後才開裝置，把 fd 交給工具 |
| 寫報告與簡報 | [VALIDATION](docs/VALIDATION.md)、[COMPARISON](docs/COMPARISON.md)、[ARCHITECTURE](docs/ARCHITECTURE.md)（設計決策）、[SECURITY](SECURITY.md)、[results](results/README.md)、dashboard | 每個數字標明「板上」或「只在主機」；不寫安全位元數 |

共用的 `common/`、`runtime/`、`ckks/`、`edge/`、`tools/v5_e2e.cpp` 定義「同一段計時範圍」，兩組協議後才改。

## 5. 文件索引 / Document index

| 文件 | 內容 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系統架構、介面（AXI、DMA、封包格式）、設計決策 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 建置、CTest、完整性流程、工具一覽、更新到 GitHub |
| [docs/BOARD_GUIDE.md](docs/BOARD_GUIDE.md) | KV260 操作員程序：gates A–H、上板當天清單 |
| [docs/COMPARISON.md](docs/COMPARISON.md) | 組 A vs 組 B 對照研究：分工、公平規則、階段、結果 |
| [docs/NATIVE_V4.md](docs/NATIVE_V4.md) | V4 製作與驗收、實驗數據、審查回應、威脅模型 |
| [docs/VALIDATION.md](docs/VALIDATION.md) | 證據分層與驗收狀態：哪些在板上驗證、哪些只在主機 |
| [SECURITY.md](SECURITY.md) | 功能 profile、安全與資料邊界 |
| [NOTICE.md](NOTICE.md)、[LICENSE](LICENSE) | 來源與授權 |
| [software/native_v5/README.md](software/native_v5/README.md) | V5 資料夾、工具（`v5_e2e`、`v5_parity`、`v5_bench`）、保證與邊界 |
| [software/native_ckks/README.md](software/native_ckks/README.md) | V4 數學、公式與檔案架構 |
| [software/ckks/README.md](software/ckks/README.md) | 凍結的 V3 Python 流程 |
| [software/driver_candidate_v4/README.md](software/driver_candidate_v4/README.md) | 故障生命週期草稿（不可部署） |
| [validation/openfhe_client/README.md](validation/openfhe_client/README.md) | 標準 OpenFHE CPU 對照 |
| [data/README.md](data/README.md) | CDC 糖尿病資料與訓練／驗證／測試切分（177,576／38,052／38,052 筆） |
| [training/README.md](training/README.md) | 訓練模型（scikit-learn），並用 V4 的 CKKS 程式驗證加密推論與明文一致 |
| [results/README.md](results/README.md) | 上板結果與電腦上的流程演練、怎麼帶回與合併 |
| [dashboard/README.md](dashboard/README.md) | 監控頁面的內容與執行方式 |
| [tools/kv260/README.md](tools/kv260/README.md) | Windows 序列埠：送指令、傳檔、收檔 |
| [docs/Proposal_01.pdf](docs/Proposal_01.pdf) | 原始提案 |
| [docs/EdgeFHE_V4_Progress_Analysis.pdf](docs/EdgeFHE_V4_Progress_Analysis.pdf) | 進度報告（V4，2026-10-09） |
| [docs/2026-10-13_first_meeting.md](docs/2026-10-13_first_meeting.md) | 與指導教授的第一次會議：準備內容、簡報連結、會議紀錄 |
| [docs/2026-10-13_architecture_analysis.md](docs/2026-10-13_architecture_analysis.md) | 分析筆記：架構、平行化、進度與報告用詞檢查 |

## 6. 常用指令 / Quick commands

從 repository 根目錄執行；Windows 用 `python`，Linux／WSL 用 `python3`。這些指令都不碰板子。

```bash
python3 tools/verify_repository.py   # 清單、凍結副本、收據、文件連結 → REPOSITORY_MANIFEST_AND_DOCUMENTS_PASS
python3 tools/offline_check.py       # 加 bank 稽核與 16 組 OpenFHE 向量 → OFFLINE_PYTHON_CHECKS_PASS no_DMA
# 只在 Linux／WSL：9 個 CPU-only CTest（V3 3、V4 2、V5 4；V5 用模擬裝置）
cmake -S . -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native --parallel 2
ctest --test-dir build/native --output-on-failure
```

CTest 100% 只代表 CPU 測試通過，不是專題完成度；x86 編出的 `.so`／執行檔不能拿到板上用。

**改了檔案之後的三個完整性指令 / Three integrity commands：**

```bash
# 1. 改了 V5 才需要（software/native_v5 的非 .md 檔、tools/v5_sources.py、tools/run_v5_checks.py）；Linux／WSL
python3 tools/run_v5_checks.py --output build/v5_checks/<新資料夾> --receipt release/native_v5/local_acceptance.json
# 2. 任何檔案增刪改之後：更新 release/SHA256SUMS（凍結副本被改會拒絕）
python3 tools/update_manifest.py
# 3. 全部一致才算完成
python3 tools/verify_repository.py
```

`release/SHA256SUMS` 涵蓋所有會進 git 的檔案（含 docs、data 說明、training、results、dashboard 原始碼），範圍規則在 [tools/inventory.py](tools/inventory.py)。雜湊清單是完整性紀錄，不是數位簽章。不要提交 `build/`、`local/`、私鑰、token 或自行編譯的 `.ko`。HLS／Vivado 與驅動建置、工具一覽、更新 GitHub 的步驟見 [DEVELOPMENT](docs/DEVELOPMENT.md)。

## 7. 授權與聲明 / License and notice

根目錄 [LICENSE](LICENSE) 為 Apache-2.0。Linux 驅動（`software/driver/`、`software/driver_candidate_v4/`）保留 `SPDX-License-Identifier: GPL-2.0`；唯一收錄的預編譯 `.ko` 是原本接受的 V3 特定 kernel 版本，對應 GPL 原始碼在 `software/driver/`。HLS 產生的 IP 與第三方依賴保留原標示；OpenFHE 是外部依賴，沒有收錄。詳見 [NOTICE](NOTICE.md)。安全問題請用團隊既有的受信任聯絡方式，不要把金鑰或 token 貼到公開 issue（見 [SECURITY](SECURITY.md)）。
