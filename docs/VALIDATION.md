# 驗證與證據 / Validation and evidence

本頁列出哪些已驗證、在電腦或 KV260 實板驗證、證據在哪個檔案（截至 2026-10-14）。證據綁定當時的原始碼、編譯器、kernel、開機與 bitstream；任一項改變就要新證據，PASS 不轉移。

**2026-10-14**：V5 首次上板，部署關卡、op2 實板一致性（V4 數學 ↔ PL，範圍見 [4.3](#43-涵蓋範圍--coverage)）與階段 2 最佳化 ARM-only 對 ARM＋FPGA 比較完成；同日再測平行編碼，並把每次 FPGA 呼叫拆開量測（[5.1、5.2](#51-平行編碼再測--stage-2-with-parallel-encode)）；G07、G08、G12 仍部分完成。入口：[README](../README.md)。

## 目錄 / Contents

1. [證據分層 / Evidence layers](#1-證據分層--evidence-layers)
2. [V3 關卡與數字 / V3 gates and numbers](#2-v3-關卡與數字--v3-gates-and-numbers)
3. [收據 / Receipts](#3-收據--receipts)
4. [2026-10-14 實板 / Board session](#4-2026-10-14-實板--board-session)
5. [階段 2 / Stage 2 results](#5-階段-2--stage-2-results)
6. [缺口與不宣稱 / Gaps and non-claims](#6-缺口與不宣稱--gaps-and-non-claims)

## 1. 證據分層 / Evidence layers

證據分七層，每層只證明自己範圍內的事：檔案完整性（第 3 節）、CPU 主機檢查（1.1）、V3 實板驗收（第 2 節，凍結 bitstream、V3 驅動與 Python 流程）、封裝重驗（2.3）、V4 主機收據（3.1）、V5 主機收據（3.2）、V5 實板紀錄（第 4、5 節，`results/`）。

歷史證據：ACCEPTANCE_MATRIX 逐 byte 保留（原 workspace 路徑與雜湊是當時身分）；[INDEX](../evidence/INDEX.json) 把 31 份原始報告對應到 `evidence/original/`，clone 後不重測。286 MB soak transactions、kernel module、工具 workspace、kernel／host 環境、PC 私密設定、generated Vivado 專案與提案全文不在公開包。

### 1.1 主機檢查 / Host checks

不開裝置、不連網路（建置見 [DEVELOPMENT](DEVELOPMENT.md)）。

| 入口 | 內容 | 通過標記 |
|---|---|---|
| `tools/verify_repository.py` | 清單、雜湊、凍結副本、Python 語法、連結、收據（第 3 節） | `REPOSITORY_MANIFEST_AND_DOCUMENTS_PASS` |
| `tools/offline_check.py` | 上一項＋bank 審查（6144 batches）＋公開向量；Windows 可跑 | `ADDRESS_CONSTANTS_AND_BANK_AUDIT_PASS`、`OPENFHE_PUBLIC_PYTHON_ORACLE_PASS cases=16 coefficients=196608`、`OFFLINE_PYTHON_CHECKS_PASS no_DMA` |
| `--native`（Linux） | 5 個 V3 程式（3 個板端 runner 只編譯）、shared backend、六例 CKKS reference | `CPU_OPENFHE_EXACT_PASS cases=16 coefficients=196608`、`MIXED_CPU_ORACLE_PASS cases=25 coefficients=307200`、`HOST_CKKS_AFFINE_REFERENCE_PASS cases=6`、`OFFLINE_NATIVE_CHECKS_PASS no_DMA no_network board_runners_compile_only` |
| CTest、Actions（ubuntu-22.04） | CPU 測試（V3、V4、V5）；Actions：環境檢查、`offline_check.py`、CMake、CTest | `100% tests passed` |

`local_reference.py` 固定 `fd=None`，仍做 schoolbook／RLWE 消去、CRT no-wrap、全 slots 誤差、三個 slot 直接 embedding、PC 回應比對、分類區間；零或邊界例可為 indeterminate。

### 1.2 規則 / Rules

結束碼 0 不算通過，要有 PASS 標記；失敗保留、修正只追加（如 `FAILURE_AND_CORRECTIONS`）；改版後更新 `SHA256SUMS`／`PROVENANCE.json`，舊 JSON 不替新原始碼背書；結果檔不手改，重跑用新資料夾（`_02`、`_03`）；報告分開寫「KV260 實測」與「電腦上量測」；不同測試（2.2 各列）的數量不能相加。

## 2. V3 關卡與數字 / V3 gates and numbers

[ACCEPTANCE_MATRIX](../evidence/ACCEPTANCE_MATRIX.json)：`CURRENT_ACCEPTANCE_MATRIX_NOT_100_PERCENT`、`formal_release_accepted=false`、範圍 `Frozen II1 N4096/V3; no claims transferred from legacy or N32768`。

### 2.1 關卡 / Gates

| 關卡 | 狀態 | 證據 | 還缺 |
|---|---|---|---|
| G01 | `PASS_FIXED_N4096_FUNCTIONAL_PROFILE` | 固定 6 例 scale／packet／PS–PL 實板功能 | 只限 N4096、2^24，不轉移到 V4／V5（2^29） |
| G02 | `PASS` | 模算術上界與獨立差分（roots／constants；三個 q 的 Montgomery 修正值在 uint64 內，一次減 q 即可） | 換版 RTL 重驗 |
| G03 | `PASS` | butterfly loop II=1；6144 bank batches／49152 位址；53 個 Verilog 檔無 runtime 除法／取餘 | 非整個多項式每 cycle；N8192 未驗 |
| G04 | `PASS` | 16 組公開 OpenFHE 低層向量，Python／C／RTL 各 196,608 係數 | 非完整 CKKS context／key／ciphertext ABI |
| G05 | `PASS_IP_LEVEL` | RTL 精確比對、bounded backpressure／reset | 只到 IP 層級 |
| G06 | `PASS` | 新 IP 匯出與 artifact identity | — |
| G07 | `PARTIAL_DMA_FUNCTIONAL_AND3_TWO_CORNER_SDF_PASS_PHYSICAL_PENDING` | 實際 DMA＋凍結 HLS 9 job／3 abort／4 shared reset；三組 routed FIFO slow/max、fast/min 通過 | upstream board reset、跨 PL reset 的在途 DDR 回應 |
| G08 | `PARTIAL_FORMAL_NORMAL_CONCURRENCY_PASS_RECOVERABLE_FAULT_PREPARED_FATAL_LIFECYCLE_PENDING` | 正常 nominal／拒絕／soak；同 process shared／separate-fd 併發 `PASS_FORMAL_15_ORIGINALS_2000_SAME_PROCESS_TWO_MODE`（9 溫度樣本）；usercopy `LOCAL_TOOL_QA_PASS_BOARD_24_IOCTL_18_DMA_PENDING`；四個 C lifecycle 模型（另 2 個刻意破壞 quiescence 的反例；非 kernel／板子） | 實板 usercopy（下一步：`ii1-v3-usercopy-faults-20261005-v2` 五參數腳本，帶回 15 原檔與 18 個 raw RX packets）、fatal quiescence、late callback、in-flight remove、多 process；[V4 驅動草稿](../software/driver_candidate_v4/README.md)不可部署 |
| G09 | `PASS_NOMINAL25_SHORT1004_AND_RETURNED_NORMAL_SIX_HOUR_SOAK` | 25 公開案例；1004 短壓測（19 次 DMA 前拒絕）；12 原檔 6 小時 5,798,822 jobs、72 次正常 open／close（`PASS_FORMAL_RETURNED_ORIGINALS_NORMAL_SCOPE_TEMPERATURE_UNAVAILABLE`：721 telemetry、0 溫度樣本） | 不含故障、remove、併發 |
| G10 | `PASS_FORMAL_ORIGINAL_TWO_MACHINE_FIXED6` | network 7／board 34／PC 14 原檔；全部 2048 slots、84 DMA | 只限公開 6 例、pinned TLS＋token |
| G11 | `PASS_MEASURED_WITH_BASELINE_LIMITS` | 800 組配對、真實 6 例 diagnostic timing | 無最佳化端到端加速比承諾 |
| G12 | `PARTIAL_ENGINEERING_REVIEW_BUNDLE_NOT_RELEASE` | 工程審查交接包 | 完整 reset／fault／lifecycle、乾淨環境重建、正式 release；正式驅動回歸需當次身分的完整 ioctl／IRQ／kernel／輸出證據 |

其他欄位：IRQ 121（TX）／136（RX）各 5,802,805；`accepted_load_relative_jobs` 5,802,783；`sensor_originals` `PASS_FORMAL_19_READ_ONLY_ORIGINALS_PARTIAL_CONTINUITY`（`ps_temp`、`remote_temp`、`pl_temp`）；`assistant_board_operations` 0。

**2026-10-14 不改變任何關卡**：沒重跑固定 6 例或 reset／故障測試；階段 2 是 G11 以外的新證據，V5 不是新的已接受版本；矩陣是凍結檔。

### 2.2 V3 數字 / V3 numbers

來源 [RESULTS](../release/RESULTS.json)（凍結量測）。

| 項目 | 數字 |
|---|---|
| NTT／ALU 實板 | 25 案例（16 公開＋9 ALU）、307,200 係數、兩 IRQ 各 +25 |
| 混合壓測 | 1004 jobs、19 次 DMA 前拒絕、兩 IRQ 各 +1004 |
| 六小時 soak | 21,600 秒、平均 268.46 jobs/s（重複 25 向量）；71,255,924,736 係數＋23,751,974,912 padding |
| 正常併發 | 同 process 四 threads、兩種 fd mode，mutex 排隊 |
| 兩機 CKKS | 六例 affine、84 DMA（每例 keygen 1、加密 8、診斷解密 4、最終解密 1）、1,032,192 係數；最大觀察誤差 ≈1.15603×10⁻⁵ < 5×10⁻⁴，但保守界線高於此門檻，不保證全域 |
| Routed（10 ns） | LUT 29,328／117,120（25.04%）、FF 18,735／234,240（8.0%）、DSP 753／1,248（60.34%）、BRAM36 等效 62／144（43.06%）、URAM 0；WNS +0.004 ns、WHS +0.009 ns |

800 組配對（3 輪暖機，共 848 DMA）；CPU 為綁核心 3 的單核純量 C++（非三核心或最佳化 OpenFHE）；比值為 P50 相除。

| op | n | ARM P50 μs | FPGA call P50 μs | driver P50 μs | ARM／FPGA |
|---|---:|---:|---:|---:|---:|
| NTT（op0） | 300 | 1852.140 | 2701.120 | 261 | 0.686 |
| INTT（op1） | 350 | 2265.550 | 2692.520 | 253 | 0.841 |
| multiply（op2） | 150 | 6590.730 | 5025.850 | 549 | 1.311 |

multiply 合併三種 pattern，部分沒加速；FPGA call 含 pack／ioctl／unpack，driver 計時不是純 PL；名目 100 MHz，Linux 回報 99,999,999 Hz 只差 1 Hz 呈現，時序看 routed 報告。重用公鑰並去掉診斷解密時四 feature 只需 9 次乘法：只是工作量分析，V3 無此路徑板測。

### 2.3 HLS 與封裝 / HLS and packaging

Fixed-bank butterfly HLS A/B（[assessment](../evidence/original/h/4/fixed-bank-20261003/assessment.json)、[interop acceptance](../evidence/original/h/4/fixed-bank-20261003/openfhe_validation/interop_acceptance.json)）：loop（512 次）2054 → 515 cycles（−74.93%）、II 4 → 1、depth 11 → 5；worst latency 171,183 → 115,779 cycles（−32.37%）；BRAM_18K 126、DSP 639 不變（不證明 ROM 節省），LUT 44,693 → 43,886、FF 23,687 → 23,573。C 模擬 30,243 組運算元 × 4 函式、216 stage 案例、19 transaction、1,118,208 次比較；RTL cosim smoke 7 transaction、86,016 次比較；估計時脈 7.365 ns 超出 10 − 2.7 ns 預算 0.065 ns。不代表 CKKS 或端到端快 4 倍；Montgomery 對 Barrett、PS 對 PL 取樣、ROM 對遞推 twiddle 都沒有 A/B。

封裝重驗（都 `board_operations=0`）：

- [LOCAL_PACKAGING_VALIDATION](../release/LOCAL_PACKAGING_VALIDATION.json)（2026-10-07，`LOCAL_SOURCE_PRESERVATION_CPU_CSIM_II1_EXPORT_AND_BD_PASS`）：CPU 檢查；HLS 2022.1 public CSim 16 例／196,608 係數含 AXI sidebands、csynth 515 cycles／II=1、IP export；53 個 Verilog 檔無 runtime 除法（排除 timescale 與 parameter／localparam 寬度）；BD 重建保留參數、port endpoints、address mappings、excluded 狀態，兩個 excluded `HP0_DDR_HIGH` metadata 由 0x0／1 重算為 0x800000000／4G，12 種 warning 原樣保留。非新 RTL 模擬或 byte-identical bitstream；無新 cosim、P&R、physical reset、板測；GitHub CI 未在遠端執行（待推上後確認）。
- [TEAM_PORTABILITY_VALIDATION](../release/TEAM_PORTABILITY_VALIDATION.json) `TEAM_LOCAL_CMAKE_CPU_AND_PROSE_VALIDATION_PASS`：文件與共用入口；板端步驟只查語法。
- [PROPOSAL_ALIGNMENT_VALIDATION](../release/PROPOSAL_ALIGNMENT_VALIDATION.json) `PROPOSAL_PAGE_REFERENCES_NUMERICS_LANGUAGE_AND_FROZEN_IDENTITIES_PASS`：提案 16 頁對照（SHA-256 在檔內），無新效能宣稱。
- [NATIVE_PORTABILITY_VALIDATION](../release/NATIVE_PORTABILITY_VALIDATION.json) `NATIVE_V4_PACKAGE_CLEAN_LOCAL_PORTABILITY_PASS`：V4 乾淨解壓重驗，`aarch64_or_board_acceptance=false`。

## 3. 收據 / Receipts

收據是記錄通過的檢查、環境與原始碼雜湊的 JSON；[verify_repository.py](../tools/verify_repository.py) 每次拿它比對目前檔案，「通過」只描述列出的那一版（步驟見 [DEVELOPMENT](DEVELOPMENT.md)）。

| 鎖住什麼 | 檢查 |
|---|---|
| [SHA256SUMS](../release/SHA256SUMS)（`.git`、`build/`、`local/`、`__pycache__` 以外每個檔案） | 雜湊相符、清單完全一致 |
| [PROVENANCE](../release/PROVENANCE.json)（101 個與開發紀錄逐 byte 相同的凍結副本） | 雜湊與大小；`tools/update_manifest.py` 拒絕更新被改的副本 |
| `release/VERSION.json`、韌體／IP 紀錄 | V3 驅動原始碼、`bit.bin`、runtime contract、IP ZIP、HLS 原始碼雜湊一致；`formal_release_accepted=false` |
| 預編譯模組 | 只允許原 V3 `.ko`（kernel `5.15.0-1027-xilinx-zynqmp`，SHA 固定，附 identity 與 GPL 原始碼）；拒絕其他 `.ko`、`.o`、`.pfx`／`.p12`、私鑰 |
| `evidence/ACCEPTANCE_MATRIX.json` | `formal_release_accepted=false` |
| V4（3.1） | 3.1 的 profile、scale、`security_bits`、`formal_release`；`aarch64_native`、`V4_native_to_PL` 必須是 false；16 個原始檔、15 個收據檔雜湊；math 收據狀態與 1000 組／12,288,000 係數；OpenFHE `baseline.cpp` 雜湊、1／3／4 設定、slot 誤差 ≤ 5×10⁻⁴ |
| V5（3.2） | `V5_LOCAL_HOST_CHECKS_PASS`、`board_DMA_operations=0`、`device_opened=false`；雜湊相符且每個 V5 原始檔都在收據裡，否則提示重跑 `tools/run_v5_checks.py --receipt release/native_v5/local_acceptance.json` |
| `.md` 相對連結（`evidence/original/` 除外） | 目標存在 |

`.gitattributes` 設 `* -text`，clone 後雜湊不變。

### 3.1 V4 收據 / V4 receipts

[NATIVE_CANDIDATE](../release/NATIVE_CANDIDATE.json)：`edgefhe-native-ckks-v4-s29-candidate-3`、`edgefhe-functional-sparse-n4096-v4-s29`、`LOCAL_CPU_AND_LOOPBACK_ACCEPTED_CANDIDATE_NOT_BOARD_OR_SECURITY_RELEASE`（2026-10-07；x86_64 WSL Ubuntu 24.04、GCC 13.3.0、CMake 3.28.3；不收 x86 執行檔）。N 4096、2048 slots、q＝998244353／1004535809／469762049、scale 2^29 → 2^58、固定權重 64／16、`security_bits=null`；本機完成 encoder 到 sigmoid／分類的完整 C++ PS 數學。記為 false：`aarch64_native`、`V4_native_to_PL`、`V4_two_machine`、`production_security_profile`、`physical_reset_or_fault_recovery`、`driver_candidate_full_kernel_compile`、`formal_release`；板上證據屬於 V5，不回寫。

[`release/native_v4/`](../release/native_v4/) 收據（都 `board_operations=0`）：

| 收據 | 標記 | 重點 |
|---|---|---|
| `math_acceptance` | `NATIVE_LOCAL_MATHEMATICAL_ACCEPTANCE_PASS` | encoder 24 例／98,304 係數 0 差異（slot 誤差 ≤1.632×10⁻⁷）；dense 1000 組／12,288,000 係數三方 0 不符（Kronecker、凍結 NTT、新 Shoup NTT）；12 組完整流程／1,867,776 次比較（affine 誤差 ≤6.669×10⁻⁷）；5 個負向測試 |
| `sanitizer_acceptance` | `NATIVE_V4_S29_FINAL_ASAN_UBSAN_PASS` | 8,192 complex slot、16,384 noise、4,096 CRT 邊界、24,576 scalar-affine 係數、30,000 Barrett 點積 |
| `network_acceptance` | `DYNAMIC_PRIVATE_INPUT_LOCAL_MTLS_PASS` | 只在 127.0.0.1；stdin／CSV、2 port、6 批（1／17／2048 筆）、29 項檢查（mTLS、pin、replay、負向）、只有密文的 schema；logit 誤差 ≤7.60646×10⁻⁷，保守界線 0.0002685753（< 0.0005） |
| `benchmark_cpu` | `PYTHON_CPP_PAIRED_CPU_BENCHMARK_PASS` | 暖機 3、30 對、固定單核、同最佳化路徑；熱路徑 P50 Python 284.313 ms、C++ 12.565 ms（22.63 倍）；C++ 1／3 workers 12.545／8.653 ms；滿 2048 筆 14.071／10.920 ms |
| `openfhe_acceptance` | `OPENFHE_STANDARD_CPU_1_3_4_WORKER_ACCEPTANCE_PASS` | OpenFHE v1.5.1 熱路徑 P50 7.760／6.980／7.269 ms（OpenMP 1／3／4）；slot 誤差 2.428／2.746／2.545×10⁻⁴ |
| `driver_model_acceptance` | `CANDIDATE_LIFECYCLE_HOST_MODEL_PASS` | 4 個正常情境、ASan／UBSan；無完整 kernel 編譯、板測、實體 reset |
| `FAILURE_AND_CORRECTIONS` | `FAILURES_RETAINED_CORRECTIONS_APPEND_ONLY` | OpenFHE scale 2^24 在 5×10⁻⁴ 關卡失敗、舊收據停在 `RUNNING`，不接受；2^29 是新 profile 重測 |
| `clean_*` | 如 `NATIVE_V4_CPU_ONLY_OPERATOR_ACCEPTANCE_PASS` | x86_64 WSL 乾淨重建重驗（操作員流程演練，非 ARM） |

整數 no-wrap 另以 `2U<Q` 檢查；`g=1e-8` 只經本機交叉檢查，非形式化 IEEE 證明。細節見 [NATIVE_V4](NATIVE_V4.md)。

### 3.2 V5 收據 / V5 receipt

[local_acceptance.json](../release/native_v5/local_acceptance.json)（[run_v5_checks.py](../tools/run_v5_checks.py) 產生，`date_utc` 2026-10-14）：`V5_LOCAL_HOST_CHECKS_PASS`；x86_64 WSL2（`6.18.33.2-microsoft-standard-WSL2`）、g++ 13.3.0；`skipped` 無、`tsan` passed。19 項 checks：`-O2`、ASan＋UBSan、TSan 各跑單元測試（`V5_TESTS_PASS tests=25`；第 25 項 `session_parallel_encode_bit_exact`：步驟 ② 用 1／2／4 個執行緒編碼，訊息與整批密文、解密結果逐位元相同）、模擬一致性（overlap 32＋serial 8 例，`V5_PARITY_PASS device=simulated-dma`）、合成資料端到端（`V5_E2E_PASS`）；`-O2` 另跑 C ABI 對 V4（`V5_CAPI_PASS contexts=4`）、127.0.0.1 雙向 TLS（`V5_TWO_MACHINE_LOOPBACK_PASS`）、模擬 bench。`source_sha256` 53 個檔（V5 全部非 `.md` 檔、重用的 V4 `core.cpp`／`core.hpp`／`c_api.cpp`、兩個工具）。收據只在全部通過時寫入（執行中原始碼被改就作廢）；改註解也要重跑，再 `update_manifest.py`、`verify_repository.py`。板上結果不在收據裡，靠 `stage_report.json`（4.4）。

## 4. 2026-10-14 實板 / Board session

第一次把 V5 搬上 KV260；原始輸出在 [results/2026-10-14-kv260/](../results/2026-10-14-kv260/NOTE.md)（摘要 `NOTE.md`）。2026-10-14 攤平成單一 repo 後也列進 `SHA256SUMS`：只證明之後沒有被改，不證明產生過程。板上 UTC 10-13 19:35–19:55 與重開機後 10-14 02:25 起（台灣 10-14）；只用 USB 序列埠（COM8、115200、`ttyPS1`），無網路，檔案以 base64 傳送並核對 SHA-256。Ubuntu 22.04.4、kernel `5.15.0-1027-xilinx-zynqmp`、GCC 11.4、Python 3.10.12（同 V3 驗收）；4 × Cortex-A53 1.333 GHz（`userspace`）、3.9 GB RAM、CMA 1000 MB、`ina260_u14/power1`；無 RTC 同步（開機是 2023-11-22），每次開機 `sudo date` 設成 PC 的 UTC。PL 與驅動是凍結 V3 bitstream（`edgefhe-ii1-n4096-kv260`）與已接受 V3 驅動，沒有新硬體。

### 4.1 部署關卡 / Deployment gates

| 關卡 | 第一次開機（`kv260_preflight_01/`） | 重開機後（`kv260_preflight_02/`） |
|---|---|---|
| A 環境 | `ENVIRONMENT_PREREQUISITES_INCOMPLETE`（`kv260_environment_01.json`）：只缺 cmake；V5 用 `tools/v5_sources.py` 直接 g++ 編譯，根目錄 CMake／CTest 因此沒在板上跑 | — |
| B 模組 | 無現場編譯；預編譯 V3 模組 `PREBUILT_V3_EXACT_ELF_METADATA_PASS target_match=True`（只核對 SHA／ELF／modinfo 與 kernel，非 probe 或 DMA） | — |
| C Device tree | 原 DTBO 與重編 DTBO 都 `PASS_OVERLAY_BINARY_SEMANTICS_ONLY`；合併 `PASS_BOOT_FDT_FILE_MERGE_ONLY`（只查檔案） | — |
| D 記憶體 | `CmaTotal` 1,024,000 kB（`cma=1000M`）、`CmaFree` 1,014,300 kB（約 990 MB） | — |
| E 安裝 app | 5 個檔案 `cmp` 相同（未存檔） | 沿用 |
| F 載入 | 卸載 `k26-starter-kits` 後 `PASS_SLOT_EMPTY_BEFORE_LOAD`；載入後 `PASS_EXPLICIT_LOAD_MARKER_TARGET_SLOT_AND_FPGA_STATE`（slot 0、`operating`） | 卸載 starter app（`remove from slot 0 returns: 0 (Ok)`）、slot 全空，載入後同一 PASS |
| G 驅動 | `version=ii1-guarded-v3`、`srcversion=6BDCA74AD872B2765D36882`（同 [VERSION.json](../release/VERSION.json)）、`DMA proxy ready: mode=dmaengine-irq`；TX／RX 0x361c0000／0x361f0000（< 0x80000000） | 相同 version／srcversion／位址，`DMA proxy ready` |

「—」＝未重做；步驟見 [BOARD_GUIDE](BOARD_GUIDE.md)；D、G 的原始輸出是 `meminfo.txt`、`dmesg_after_insmod.txt`。

### 4.2 板上測試 / Board tests

| 測試（第一次開機） | 結果 | 證據 |
|---|---|---|
| V5 建置（GCC 11.4） | `V5_BUILD_DONE`，6 個產物（含 `libedgefhe_native_v4.so`） | `v5_build.log` |
| 單元測試 | `V5_TESTS_PASS tests=24`（5.3 秒） | `board_host_checks_01/v5_tests.log` |
| 模擬裝置一致性 | `V5_PARITY_PASS device=simulated-dma` | 未存檔（第 5 節有存檔的一次） |
| C ABI 對板上 V4 函式庫 | `V5_CAPI_PASS contexts=4 bit_exact=1`（CPU 與模擬裝置 context，不經實際裝置） | 終端輸出（`NOTE.md`） |
| **實板一致性** | **`V5_PARITY_PASS device=linux-dma cases=64 coefficients=786432 client_round=exact`**；77 次 DMA（64 個乘積＋keygen 1、加密 8、解密 4），無錯誤 | `kv260_preflight_01/parity_board_01.log` |

工具只記總和（`requests=77 pack_ms=14.415 device_ms=386.871 unpack_ms=18.4086 wall_ms=392.745 driver_reported_ms=40.151`；`fpga-linux-dma-overlap`）：平均每次 ioctl 約 **5.02 ms**，驅動回報的 DMA＋PL 約 **0.52 ms**；其餘約 4.5 ms 在驅動計時外；第 5.2 節拆開後確認，主要是驅動從不經快取的緩衝區讀取。這不是效能量測（無暖機、綁核、分位數）。

### 4.3 涵蓋範圍 / Coverage

| 範圍 | 有？ | 說明 |
|---|---|---|
| op2 乘積 | 有 | 64 組隨機 canonical 運算元（公開 QA 種子，非金鑰），每批 8 個；786,432 係數（64 × 3 × 4096）與 `CpuBackend` 相同 |
| 完整 client 回合 | 有 | 新 OS 取樣的 keygen、四特徵加密、四份解密，FPGA 與 CPU 路徑相同 |
| 連到 V4 | 有 | 板上 `cpu_backend_matches_v4`、`client_matches_v4_engine`、`session_matches_v4_engine` 與 C ABI 通過，所以 PL op2 ＝ `CpuBackend` ＝ V4 數學（限測過的案例） |
| 打包路徑 | 有 | 轉置、padding、canonical 檢查、雙緩衝 |
| op0／op1 單獨、ALU | 沒有 | V5 只送 op2（模擬裝置對其他 op 回 `-EOPNOTSUPP`）；NTT／INTT／ALU（op3–op5）實板證據仍是 V3 的 25 案例 |
| 長時間、故障 | 沒有 | 無 V5＋真 DMA 的 soak 或板上故障注入（`fpga_fault_stops_batch` 只用模擬裝置，不算 G08） |
| V4 自己的流程 | 沒有 | `tools/run_native_cpu_acceptance.py` 沒在 ARM 跑；V4 `Engine` 的 fd DMA 路徑沒執行 |

### 4.4 紀錄限制 / Limits

- **原始碼身分**：`stage2_02/stage_report.json` 記錄 23 個原始檔與 6 個執行檔的 SHA-256；和目前檔案比，只有 `software/native_v5/tools/v5_e2e.cpp` 第一行註解的文件名稱不同（事後更新），其餘 22 個相同。第一次開機的測試是手動執行，無 `stage_report.json`，原始碼只靠傳輸時的 SHA-256 核對。
- **IRQ 沒對帳**：第一次只存 DMA 前的 `/proc/interrupts`，重開機後沒存。

## 5. 階段 2 / Stage 2 results

重開機後重做時鐘校正與 F、G（4.1），在 `sudo bash -c` 內開裝置後執行 `tools/run_comparison.py --stage 2 --fd 3 --cpus 0-3`，結果 **`V5_STAGE2_COMPLETE`**；`stage2_02/stage_report.json` 記錄每一步、執行檔與原始碼的 SHA-256 及 DMA 政策。

| 步驟 | 結果 |
|---|---|
| 板上編譯（GCC 11.4，`-Wall -Wextra -Werror`） | 通過，0 個 warning |
| 單元測試 | `V5_TESTS_PASS tests=24` |
| 模擬裝置一致性 | `V5_PARITY_PASS device=simulated-dma cases=16 coefficients=196608 client_round=exact` |
| 實板一致性（第二次） | `V5_PARITY_PASS device=linux-dma cases=64`，77 次 DMA |
| 端到端（六種設定 × 正確性／延遲／吞吐量） | `V5_E2E_PASS`（`device_times_are_fpga: true`）；六種設定的密文與解密結果 SHA-256 完全相同，最大 logit 誤差 3.93×10⁻⁷，0 筆不一致、1 筆在誤差界內 |
| 對照報表 | `COMPARISON_WRITTEN`，公平檢查通過 |

這證明：CDC 測試集全部 38,052 筆、19 批經實際 DMA 與 PL 算出的密文和 CPU 的**逐位元相同**，量測期間沒有 DMA 錯誤。不證明的事同 4.3：soak、故障與 reset 生命週期（G07／G08）、op0／op1 單獨使用、安全等級。效能數字與解讀見 [COMPARISON](COMPARISON.md)；原始檔：[results/2026-10-14-kv260/stage2_02/](../results/2026-10-14-kv260/stage2_02/comparison.md)。

### 5.1 平行編碼再測 / Stage 2 with parallel encode

同一個 session（沒有重開機，F、G 仍有效）。程式更新包經序列埠傳入，117 個檔的 SHA-256 全部相符。執行 `run_comparison.py --stage 2 --fd 3 --cpus 0-3 --encode-workers 4`，結果 **`V5_STAGE2_COMPLETE`**（`stage2_03/`，3 分 52 秒）：

| 步驟 | 結果 |
|---|---|
| 板上編譯（GCC 11.4，`-Werror`） | 通過 |
| 單元測試 | `V5_TESTS_PASS tests=25`（含 `session_parallel_encode_bit_exact`） |
| 實板一致性（第三次） | `V5_PARITY_PASS device=linux-dma cases=64 coefficients=786432 client_round=exact`，77 次 DMA |
| 端到端 | `V5_E2E_PASS`；六種設定的密文與解密結果 SHA-256 相同，最大 logit 誤差 4.47×10⁻⁷，0 筆不一致、1 筆在誤差界內 |
| 對照報表 | `COMPARISON_WRITTEN`，公平檢查通過（`encode_workers` 4，對每個後端相同） |

`stage_report.json` 記錄的 23 個原始檔 SHA-256 和目前 repo 檔案完全相同。

### 5.2 每次呼叫拆解 / Per-call trace

緊接 5.1，在同一個 `sudo bash -c` 裡執行 [trace_dma_call.py](../tools/trace_dma_call.py)：**`DMA_TRACE_COMPLETE mode=fine calls=109`**（`dma_trace_01/`）。

- **執行檔與工具**：用的 `v5_e2e` 和 5.1 的執行檔 SHA-256 相同；工具本身和目前 repo 檔案逐位元相同（以傳輸包的清單比對）。
- **kprobe 標記**：只有在模組身分（核心版本、srcversion、`.ko` SHA-256、kallsyms 間距）全部相符時才使用，這次全部相符。
- **追蹤本身的成本**：每次呼叫 4.677 → 4.735 ms。
- **追蹤後還原**：還原錯誤 0、遺失事件 0。事後以 root 唯讀確認 `current_tracer` 是 nop、kprobe_events 0 行、過濾清單清空，dmesg 沒有新訊息（`post_run_checks.txt`）。
- **DMA 請求**：218 個（基準、追蹤各 109），全部成功。

**這證明**：在這個驅動與這塊板子上，每次 op2 呼叫 4.7 ms 中：

- 3.59 ms 是驅動在不經快取的 TX 緩衝區上逐筆檢查係數。
- 0.45 ms 是從不經快取的 RX 緩衝區搬出。
- 0.56 ms 是 DMA＋PL。

**不證明**：改成可快取記憶體後能省多少。[COMPARISON](COMPARISON.md) 的「約省 3.9 ms」是估算，要新驅動實測。這次也沒有拆開 PL 內部（NTT 與傳輸）的時間。

## 6. 缺口與不宣稱 / Gaps and non-claims

### 6.1 仍缺 / Missing

| 缺口 | 要補什麼 |
|---|---|
| G07、G08、G12 | 見 2.1 |
| 4.3「沒有」的項目 | op0／op1、ALU 各做實板一致性；依 [ARCHITECTURE](ARCHITECTURE.md) 單板管線（H）計畫對 V5＋真 DMA 做 soak、回壓、故障測試；板上跑 `tools/run_native_cpu_acceptance.py`（[NATIVE_V4](NATIVE_V4.md)） |
| 呼叫額外負擔 | ioctl 時間遠大於驅動回報的 DMA＋PL，組成（如緩衝區複製）未拆開（[COMPARISON](COMPARISON.md)） |
| op2 介面 | 每次乘法重送並重轉公鑰（每分量 24 次轉換，CPU 路徑 14 次）；改需新 HLS 運算＝新 bitstream＝新板測 |
| 實體兩機（階段 3） | 只在 127.0.0.1 演練（V4 loopback、V5 `two_machine_loopback.py`）；需 KV260 ↔ PC 實體 mTLS、私密輸入隔離、含網路的端到端、live UI |
| 安全 profile | 安全分布與估算、side-channel、惡意 evaluator、標準 context 相容（[SECURITY](../SECURITY.md)） |
| ROM 對照 | 同 N／q／lanes／clock 的 ROM 對遞推 twiddle |
| 溫度 | 需有溫度的長時間紀錄 |

### 6.2 不宣稱 / Not claimed

- **安全**：都是稀疏功能 profile（`security_bits=null`）；N／Q 上限推不出 128 或 153 位元；OpenFHE 對照的 `HEStd_128_classic` 是另一組參數；不宣稱零洩漏、CCA、constant-time 或結果完整性（[SECURITY](../SECURITY.md)）。
- **正式 release**：`VERSION.json`、`ACCEPTANCE_MATRIX.json`、`NATIVE_CANDIDATE.json` 都是 false（`verify_repository.py` 強制）；測試通過數不是專題完成度，不是 100% 工程完成。
- **模擬時間**：主機上的「FPGA」數字來自模擬裝置（`device_times_are_fpga: false`），是 CPU 時間，V5 收據的模擬 bench 亦同。
- **主機數字**：x86 數字（V4 Python 對 C++ 的 22.63 倍、primes／分布／API 不同的 OpenFHE 對照）不是 KV260 或 FPGA 加速比；CPU oracle 不是 RTL 或板測。
- **延遲**：V3 的 14 次呼叫診斷時間（含 oracle、keygen、額外解密）不是部署延遲、Amdahl 占比或 2048 筆吞吐；端到端比較見第 5 節與 COMPARISON。
- **並行**：正常併發不是四筆 PL 並行；驅動一次一筆，V5 雙緩衝與管線只重疊 CPU 工作；jobs/s 不是 records/s。
- **功耗**：hwmon（INA260）量整板或某一路電源，不是單獨 CPU 或 FPGA。
- **種子**：主機 QA 種子與 `--qa-seed` 的金鑰任何人都能重算，只能測試。
- **CI 與雜湊**：CI 只跑 host CPU，不驗收 HLS、P&R、板子、安全、網路隱私或 release；`SHA256SUMS` 是完整性紀錄，不是簽章，也不證明功能。
- **範圍外**（要做需另定範圍並驗收）：bootstrapping（依提案排除在 PL 外）、任意深度 FHE、T 雙板、M 多方門檻、密文 sigmoid、rotation／rescale／多層、N8192、整個多項式每 cycle。
