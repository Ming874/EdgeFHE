# 開發手冊 / Development guide

修改、建置、檢查與發布到 GitHub 的方法。板上動作（載入 FPGA、`insmod`、開裝置、DMA）只由操作員依 [BOARD_GUIDE](BOARD_GUIDE.md) 執行；A/B 量測見 [COMPARISON](COMPARISON.md)。指令從 repo 根目錄執行、只用相對路徑（不寫死個人磁碟、IP、kernel module 或憑證）；Windows 用 `python`，其他用 `python3`。

**目錄 / Contents**

1. [環境與團隊設定](#1-環境與團隊設定--environments-and-team-setup)
2. [建置與 CPU 測試](#2-建置與-cpu-測試--build-and-cpu-tests)
3. [硬體與驅動建置](#3-硬體與驅動建置--hardware-and-driver-builds)
4. [完整性流程](#4-完整性流程--integrity-workflow)
5. [tools/ 工具一覽](#5-tools-工具一覽--tool-reference)
6. [更新到 GitHub](#6-更新到-github--publishing-to-github)
7. [持續整合](#7-持續整合--continuous-integration)

## 1. 環境與團隊設定 / Environments and team setup

大家共用原始碼、schema、測試向量與 gates，但每次建置各自保存工具版本與產物 SHA-256。**共用原始碼不代表 binary 到處能用**：x86 的 `.so`／執行檔不能用在 aarch64（在板上重建），也沒有跨 kernel 的 `.ko`。

| 角色 | 環境與工具（最低版本） | 工作 |
|---|---|---|
| 全體成員 | Windows：Python 3.10+、Git | Python 檢查、更新清單、A/B 報表；不能建原生 C++（`CMakeLists.txt` 用到 Linux ioctl，非 Linux 直接停止） |
| C++／數學／服務 | Linux／WSL：Python 3.10+、GCC（C++17、pthread）、CMake 3.20+、make、`ar`；TLS 迴路測試要 `openssl` | CMake／CTest、V4 本機流程、V5 後端與主機檢查 |
| 硬體 | Vitis HLS 2022.1、Vivado 2022.1、K26 part 與 board files | CSim／cosim／csynth／IP／BD／P&R |
| 板端操作員 | KV260 Ubuntu aarch64：matching kernel headers、`xmutil`、`dtc`／`fdtoverlay`、`kmod`、`openssl` | 板上重建與量測、驅動編譯、load／probe／DMA 驗收 |
| 向量維護 | C++ 環境＋指定 revision 的 OpenFHE | 重產 16 例向量（其他人不用裝 OpenFHE） |

- V4 的標準 OpenFHE 對照要外部既有安裝；基本 CPU 批次不需 OpenFHE、kernel headers 或 DMA。
- WSL 直接用同一份 checkout；新 WSL distribution 先裝好環境（不假設有 `Ubuntu-24.04`）。路徑可有空白，HLS 例外（第 3.1 節）。
- 不在 repo、依角色自行安裝：Ubuntu image、kernel headers、AMD 工具與授權、OpenFHE 本體與 shared libraries、boot firmware。

**已驗證版本**（[TEAM_ENVIRONMENT](../release/TEAM_ENVIRONMENT.json)）：Windows Python 3.12.14；WSL Ubuntu 24.04／GCC 13.3.0／CMake 3.28.3；歷史板測 Ubuntu 22.04.4／GCC 11.4.0／kernel `5.15.0-1027-xilinx-zynqmp`；part `xck26-sfvc784-2LV-c`。其他版本都沒測過：新環境先跑第 1.1 節、保存版本與 SHA 再進 gates；換工具或 kernel 要有新的結果身分。

### 1.1 第一次離線檢查

```bash
sudo apt-get update                  # 一般主機工具，不是板上部署
sudo apt-get install -y build-essential cmake python3 git
export PYTHONDONTWRITEBYTECODE=1     # 第 4.4 節
python3 tools/check_environment.py --target host --output build/host_environment_01.json
python3 tools/verify_repository.py
python3 tools/offline_check.py
cmake -S . -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native --parallel 2
ctest --test-dir build/native --output-on-failure
```

Windows PowerShell：先 `$env:PYTHONDONTWRITEBYTECODE = '1'`，再用 `python` 跑上面三個 `tools/` 指令；C++ 到 WSL。

通過時依序印：`ENVIRONMENT_PREREQUISITES_PASS target=host no_DMA no_network`；`REPOSITORY_MANIFEST_AND_DOCUMENTS_PASS files=… exact_copies=101 Python=… links=… v5_sources=…`；`ADDRESS_CONSTANTS_AND_BANK_AUDIT_PASS`、`OPENFHE_PUBLIC_PYTHON_ORACLE_PASS cases=16 coefficients=196608`、`OFFLINE_PYTHON_CHECKS_PASS no_DMA`；`100% tests passed, 0 tests failed out of 9`（只是 9 個 CPU 測試，**不是專題完成度**；CTest 沒有硬體測試）。報告檔已存在時 `check_environment.py` 會失敗：換新檔名（如 `_02`），舊檔留作證據。Windows 的 PASS 只涵蓋 Python 檢查。

### 1.2 共同規則

- **exit 0 不等於通過**：要看到明確 PASS 標記（CTest 由 `tools/require_pass.py` 兩者都查）。
- 產物放 `build/`、祕密放 `local/`；輸出用新位置（第 5 節標「新」者不接受已存在路徑），舊結果是證據。祕密不能進 evidence、logs 或 crash dumps。
- 改函式、scale、sampler、twiddle、lane、driver、TLS 或模型＝另立 candidate：保存 source／compiler／artifact SHA、指令、原始輸出、PASS 標記、失敗與訂正；不拿已接受的歷史 JSON 替新來源開綠燈。
- 每個模組有 owner 與 reviewer；CPU CI、HLS／Vivado、板上驗收分開；主機結果不等於板上結果。

## 2. 建置與 CPU 測試 / Build and CPU tests

### 2.1 CMake

用第 1.1 節最後三行（Linux／WSL／KV260）。根 [CMakeLists.txt](../CMakeLists.txt) 建 V3，並以 `EDGEFHE_BUILD_NATIVE_CANDIDATE`（V4）、`EDGEFHE_BUILD_V5`（V5）加入，預設 `ON`；關掉則 CTest 變少（CI 用預設 9 個）。警告即錯誤（`-Wall -Wextra -Werror`，V4／V5 加 `-fno-fast-math`），無法關掉 `-Werror`。只建置：不插模組、不載 FPGA、不連網、不做 DMA。產物（`build/native/` 下）：

- V3：`libedgefhe_backend.so`、`paired_public_vectors`、`mixed_runner`，及只編譯、不跑硬體模式的板上 runners `soak_runner`、`concurrent_runner`、`usercopy_runner`。
- V4（`software/native_ckks/`）：`libedgefhe_native.so`、`edgefhe_native_demo`、`edgefhe_native_selftest`；CPU-only，不建置、不部署故障驅動草稿。
- V5（`software/native_v5/`）：`libedgefhe_v5.a`、`v5_tests`、`v5_parity`、`v5_bench`、`v5_e2e`、`libedgefhe_v5_capi.so`；主機測試用模擬 DMA 裝置。

9 個 CTest 都經 `tools/require_pass.py`（exit 0 **且**含標記）；只跑 V5：`ctest --test-dir build/native -L v5 --output-on-failure`。`.bin` 向量在 `validation/vectors/`。

| CTest | 執行 | 標記 |
|---|---|---|
| `public_cpu_exact`（V3） | `paired_public_vectors vectors.bin --cpu-check` | `CPU_OPENFHE_EXACT_PASS cases=16 coefficients=196608` |
| `mixed_cpu_exact`（V3） | `mixed_runner mixed_vectors.bin --cpu-check` | `MIXED_CPU_ORACLE_PASS cases=25 coefficients=307200` |
| `ckks_cpu_affine`（V3） | `software/ckks/local_reference.py --library libedgefhe_backend.so`（六例 CKKS、無 DMA、無網路、逾時 300 秒） | `HOST_CKKS_AFFINE_REFERENCE_PASS cases=6` |
| `native_complete_math`（V4） | `edgefhe_native_selftest` | `NATIVE_CPP_SELFTEST_PASS` |
| `native_protocol_offline`（V4） | `tests/test_protocol.py`（協定負向測試） | `V4_PROTOCOL_OFFLINE_NEGATIVE_PASS` |
| `v5_unit`（V5） | `v5_tests tests/data` | `V5_TESTS_PASS tests=25` |
| `v5_parity_simulated`（V5） | `v5_parity --cases 32`（模擬裝置） | `V5_PARITY_PASS device=simulated-dma` |
| `v5_e2e_sample`（V5） | `v5_e2e` 合成樣本，`cpu-w1,cpu-w3,fpga-serial,fpga-overlap`，`--qa-seed 7 --power off` | `V5_E2E_PASS` |
| `v5_capi_python`（V5） | `tests/capi_test.py --library libedgefhe_v5_capi.so`（各 context 逐位元相同） | `V5_CAPI_PASS` |

### 2.2 V5 免 CMake 建置

```bash
python3 tools/v5_sources.py --output build/v5
```

直接用 `g++`（板上或階段 3 電腦端適用），印 `V5_BUILD_DONE`；產出第 2.1 節的 V5 檔案，加上 `libedgefhe_native_v4.so`（V4 參考庫，供 C ABI 比對）與 `build.log`。基本旗標 `-std=c++17 -Wall -Wextra -Werror -fno-fast-math -pthread`；同一輸出資料夾可重用（覆寫）。

- `CXX=g++-13 python3 tools/v5_sources.py --output build/v5_gcc13`：換編譯器（預設 `g++`）；兩組由同一編譯器、同一組旗標建出，`run_v5_checks.py`、`run_comparison.py` 也讀 `CXX`。
- `--cxxflags=-O3` 取代預設 `-O2`；值以 `-` 開頭**必須用 `=`**（`--cxxflags -O3` 報 `error: argument --cxxflags: expected one argument`），`run_comparison.py` 同；多個旗標寫 `--cxxflags="-O3 -g"`。
- `--jobs`（預設 4）。`--no-werror` 只拿掉 `-Werror`，只用於新版編譯器多出的 warning；真正的 error 不要硬過。
- 新增函式庫 `.cpp`（`tests/`、`tools/`、`capi/` 以外）要同時加進 `tools/v5_sources.py` 的 `LIBRARY` 與 [software/native_v5/CMakeLists.txt](../software/native_v5/CMakeLists.txt) 的 `add_library(edgefhe_v5 …)`，否則停在 `tools/v5_sources.py LIBRARY is out of date` 或 `CMakeLists.txt does not list …`。

### 2.3 offline_check.py

預設（任何平台）在 `build/offline_checks/` 的副本上跑 bank 與向量稽核，確認 6144 個 bank batches、16 組向量 196608 個係數、向量清單與凍結版相同（標記見第 1.1 節）。`--native`（Linux／WSL）再用 `g++ -std=c++17 -O3 -Wall -Wextra -Werror -pthread` 編譯第 2.1 節的 V3 程式（板上 runners 只編譯），跑 CPU 精確檢查與六例 CKKS reference，印 `OFFLINE_NATIVE_CHECKS_PASS no_DMA no_network board_runners_compile_only`；它是 CMake 以外的另一條路。

### 2.4 V3 backend 與其他流程

- `edgefhe_cpu(op,a,b,out)`：limb-major `[3][4096]` `uint32`、canonical 普通域；`op` 0 正向 NTT、1 反向、2 乘法；`a`、`out` 必填，`op` 2 另需 `b`。`edgefhe_hardware_multiply(fd,a,b,out,times)` 只接受**已過外部 live gates 的 fd**：打包成 coefficient-major AXIS、送 op2 ioctl、再查 canonical 與 padding。都不是完整 Encrypt／Decrypt API（函式對照屬 [ARCHITECTURE](ARCHITECTURE.md)）。
- `software/ckks/local_reference.py --library <新 .so>` 固定 `fd=None`、CPU、不連網，用公開的固定 host QA 種子；正式板上取樣必須用 OS 新鮮亂數。`profile.py` 的歷史 `HISTORY` 常數只屬舊 fixture 套件，不能當新 clone、新開機的即時 DMA 歷史 gate。
- 其他本機流程：V4 見 [software/native_ckks/README.md](../software/native_ckks/README.md) 與 [NATIVE_V4](NATIVE_V4.md)，V5 C ABI 見 [software/native_v5/README.md](../software/native_v5/README.md)，OpenFHE 對照見 [validation/openfhe_client/README.md](../validation/openfhe_client/README.md)。

## 3. 硬體與驅動建置 / Hardware and driver builds

只產生檔案（不 program FPGA、不載入模組），輸出資料夾必須是新的；先跑 `python3 tools/verify_repository.py`。新的 RTL、P&R 或板測要各自驗收，不繼承凍結 bitstream 的板上 PASS。

### 3.1 Vitis HLS 2022.1

在載入 Xilinx 工具的 shell：

```bash
python3 tools/build_hls.py public build/hls_public              # 16 組 OpenFHE TB、csynth、匯出 IP
python3 tools/build_hls.py fixed build/hls_fixed                # 原 tb_fixed_bank：30243 組模算術 pairs、216 個 stage cases
python3 tools/build_hls.py public build/hls_public_rtl --cosim  # 加 RTL cosim
```

- VLNV `edgefhe.local:hls:rns3_ntt_parallel_accel_n4096:1.1`；新 IP 在 `<output>/project/solution1/impl/ip/`。原 TB 的本機 `D:` 路徑改成必填 `EDGEFHE_VECTOR_FILE`，由 `build.tcl` 設定。
- 2022.1 會重寫 include flags：checkout 與輸出路徑**不能有空白**，否則帶引號的絕對 include 被當成相對路徑。
- 不在 PATH 時加 `--tool "E:/Xilinx/Vitis_HLS/2022.1/bin/vitis_hls.bat"`。launcher 以環境變數傳參（2022.1 不轉交 `-tclargs`），要求完成標記 `LOCAL_HLS_EXPORT_COMPLETE_REPORT_REVIEW_REQUIRED`；原始 log 在 `build/hls-launch-*/console.log`。
- 接受條件：TB 明確 PASS、csynth 指定 loop 達 II=1、資源與時序、cosim／報告逐係數與 sidebands 完整核查。

### 3.2 Vivado 2022.1

```bash
python3 tools/unpack_ip.py build/accepted_ip                                 # 凍結 IP ZIP（身分在清單）解到新資料夾
python3 tools/build_vivado.py bd build/vivado_bd build/accepted_ip           # 只重建 BD
python3 tools/build_vivado.py implement build/vivado_impl build/accepted_ip  # 完整合成與 P&R
```

- 標記：`unpack_ip.py` 查過 CRC 與路徑安全後印 `FROZEN_IP_EXTRACT_PASS`；`bd` 印 `PORTABLE_BD_STRUCTURE_PASS_NOT_ROUTED_OR_BOARD_ACCEPTANCE`；`implement` 印 `PORTABLE_IMPLEMENTATION_EXPORTED_COMPLETE_REPORT_AND_BOARD_REVIEW_PENDING`。
- 新 HLS IP：最後一個參數換成新的 `impl/ip`（要有 `component.xml`）。不在 PATH 時加 `--tool "E:/Xilinx/Vivado/2022.1/bin/vivado.bat"`；log、`.jou`、`.Xil` 在 `build/vivado-launch-*`。
- 需要 part、`kv260_som:part0:1.3` board catalog 與對應 IP；不自動安裝 board files 或換版本。
- implementation 先 route、審查 setup／hold 與 DRC 才產出 bit／XSA；check_timing、no_clock、methodology、reset 要人工審查。新 P&R 即使 PASS 也不是原 payload 的板上驗收；打包時只重驗了 BD，沒有新的 P&R 或板測驗收。

### 3.3 重新產生 OpenFHE 公開向量

16 組向量已收錄，離線重驗不用 OpenFHE。要重產時，先自行建置 [openfhe_revision.txt](../validation/vectors/openfhe_revision.txt) 固定的歷史 OpenFHE commit（repo 不含 OpenFHE 或 `.so`），prefix 指向安裝位置：

```bash
cmake -S validation/vectors -B build/vector_generator -DCMAKE_PREFIX_PATH=/your/OpenFHE/install
cmake --build build/vector_generator -j 4
build/vector_generator/export_vectors build/regenerated_vectors.bin  # OpenFHE V1 public NTT vectors: PASS
cmp build/regenerated_vectors.bin validation/vectors/vectors.bin     # 必須逐 byte 相同
```

產生器量測 library 的輸出排列再轉回自然順序；低層精確一致**不等於**完整 CKKS API 相容。`library_binary_sha256.txt` 是歷史 Linux 產生器的依賴身分：新 `.so` 不必同 SHA，但要另存新依賴的版本、結果與身分。

### 3.4 驅動模組（只在 KV260）

操作員在板上依 [BOARD_GUIDE](BOARD_GUIDE.md) 關卡 A、B 準備模組：用 `python3 tools/build_driver.py --output build/driver_native_01` 編譯，或在 kernel 完全相同時採用預編譯 V3 `.ko`（`python3 tools/inspect_prebuilt.py --target` 唯讀核對）；指令、標記與身分規則都在那裡。另外：

- `build_driver.py` 不做 cross-compile（拒絕非 Linux、非 aarch64），需要 `make`、`modinfo` 與 `/lib/modules/$(uname -r)/build`（或 `--kernel-build`）；輸出必須是新的，且不可在 `software/` 下。
- 載入、bind、fresh boot、CMA、IRQ 基準、quiescence、跑完後的 kernel 健康都照 BOARD_GUIDE；沒有一鍵部署、放寬歷史 session 範圍或安全重播歷史 board sessions 的腳本。
- 凍結韌體＝`firmware/ii1_n4096/` 的 bit.bin、DTBO、shell 與 v2 執行期契約；v3 的 32-bit mask 與實際 guard 在 `runtime_contract_v3.json`，不要改寫舊契約冒充 v3。
- `software/board_tests/` 的 usercopy 是工具 QA 通過、實板待驗（`--run` 與一次性腳本的限制見 BOARD_GUIDE 關卡 G）。`software/driver_candidate_v4/` **不可部署**（[README](../software/driver_candidate_v4/README.md)）。

### 3.5 V3 TLS 示範設定

V3 `software/ckks/pc_server.py` 必填 `--config`、`--private-dir`、`--evidence`、`--bind`；範本 [join_config.example.json](../software/ckks/join_config.example.json) 沒有可用的 token 或 IP；新憑證與 token 在 `local/` 產生，傳送與連線由操作員處理。共用欄位、pin 與「TLS＋token 不是 mTLS」的邊界見 BOARD_GUIDE 關卡 H。本包的 CPU demo 直接呼叫 `cloud.evaluate`，不需要網路設定。

## 4. 完整性流程 / Integrity workflow

repo 用雜湊鎖住；任何修改都要以步驟 3 通過收尾（CI 也查）。

### 4.1 改完檔案後的三個指令

```bash
# 1. 只有改了 V5 才需要（Linux／WSL）
python3 tools/run_v5_checks.py --output build/v5_checks/<新資料夾> --receipt release/native_v5/local_acceptance.json
# 2. 任何增刪改之後（Windows 也可）
python3 tools/update_manifest.py
# 3. 全部一致才算完成（Windows 也可）
python3 tools/verify_repository.py
```

| 你改了 | 步驟 |
|---|---|
| `software/native_v5/` 任何非 `.md` 檔（含 `CMakeLists.txt`、`python/`、`tests/data/`）、`tools/v5_sources.py`、`tools/run_v5_checks.py` | 1→2→3 |
| 其他可改的檔（任何 `.md`、其他 `tools/` 腳本如 `run_comparison.py`、根 `CMakeLists.txt`、`.github/`） | 2→3 |
| V3 凍結副本、V4 釘住的檔 | 不能直接改：另立 candidate |

順序不能換：收據本身也列在 `SHA256SUMS`。`offline_check.py`、`unpack_ip.py`、`run_native_cpu_acceptance.py` 會先跑 `verify_repository.py`，清單過期就失敗。

**步驟 1**（`run_v5_checks.py`）要 `g++`（或 `CXX`）與 `ar`，不用 CMake：

| 項目 | 內容 |
|---|---|
| 三種建置 | `-O2`、ASan＋UBSan、TSan（有 `setarch` 時關掉該行程的位址隨機化） |
| 每種都跑 | 單元測試、模擬一致性（32 例＋8 例不重疊）、合成樣本端到端 |
| `-O2` 另跑 | C ABI 對 V4 逐位元比對（`V5_CAPI_PASS contexts=4`）、127.0.0.1 mTLS 兩機演練（`V5_TWO_MACHINE_LOOPBACK_PASS`；沒有 `openssl` 就略過並記錄）、模擬熱路徑量測 |
| 通過 | `V5_LOCAL_HOST_CHECKS_PASS board_DMA=0`，再 `V5_RECEIPT_WRITTEN …` |
| 失敗 | 停下、**不寫收據**，log 與 `v5_check_report.json` 留在輸出資料夾；途中原始碼被改報 `Sources changed while checks were running` |
| `--skip-tsan` | 只在 kernel 跑不了 TSan 時用；收據記 `"tsan": "skipped"` |
| 範圍 | 主機檢查，**不是板上驗收**：不開 DMA 裝置、只用 127.0.0.1、沒有 PL 一致性、模擬時間不是 FPGA 時間 |

**步驟 2**（`update_manifest.py`）先確認凍結副本沒變（否則印 `Frozen source changed: <檔案>` 並停止），再把清單規則（[tools/inventory.py](../tools/inventory.py)，和 `.gitignore` 一致）選出的**所有檔案**寫進 `release/SHA256SUMS`，印 `INVENTORY_REFRESHED_SOURCE_IDENTITIES_UNCHANGED files=…`。排除的是 `.git/`、`build/`、`local/`、`__pycache__/`、`node_modules/`、`.next/`、`.venv*/`、`.vscode/`、下載的 CDC 資料（`data/*/raw/`、`data/*/splits/`）、證據以外的 `*.log`，以及清單本身。先把暫存檔、私人輸出移出 repo，否則會被列入。它不檢查 V4／V5 收據。**步驟 3** 唯讀，標記見第 1.1 節。

### 4.2 各種鎖與失敗處理

| 機制 | 鎖住 | 失敗訊息 | 處理 |
|---|---|---|---|
| [SHA256SUMS](../release/SHA256SUMS) | 每個檔案的 SHA-256 與清單（排除同步驟 2） | `SHA mismatch/missing: <檔案>`、`Inventory mismatch: {…}` | V5 檔從步驟 1，其餘步驟 2、3 |
| [PROVENANCE.json](../release/PROVENANCE.json) | 101 個與開發紀錄 byte-exact 的 V3 凍結副本（雜湊＋大小） | `Frozen source changed`（步驟 2 拒絕、步驟 3 失敗；包括編輯器改了換行或編碼） | 還原原位元組；改算術、driver、firmware 要另立 candidate、更新 provenance、重新驗收 |
| [NATIVE_CANDIDATE.json](../release/NATIVE_CANDIDATE.json)＋`release/native_v4/` | V4：`software/native_ckks/` 16 個檔（`tests/`、README 以外）與 15 份收據；收據另釘住 `validation/openfhe_client/baseline.cpp` | `Candidate source identity`、`Candidate receipt identity`、`Math receipt/source` | 還原；改 V4 要另立 candidate |
| [local_acceptance.json](../release/native_v5/local_acceptance.json) | V5：第 4.1 節表第一列的檔，加上重用的 V4 `core.cpp`、`core.hpp`、`c_api.cpp` | `V5 source changed since its receipt`、`V5 sources without a receipt`（附重跑指令） | 步驟 1→2→3 |

`release/` 由工具維護：除了步驟 1、2 寫的兩個檔，其餘是歷史紀錄與身分，不要手改。`verify_repository.py` 另外檢查：

- 預編譯 V3 `.ko` 的 SHA、ELF（aarch64）、modinfo 與對應 GPL 原始碼；`VERSION.json`、`runtime_contract_v3.json`、韌體匯出清單、IP 匯出紀錄中的驅動、bit.bin、v2 契約、IP ZIP、HLS 原始碼雜湊一致。
- **狀態旗標鎖定**：`VERSION.json`、`ACCEPTANCE_MATRIX.json` 的 `formal_release_accepted`＝`false`；`NATIVE_CANDIDATE.json` 的 profile `edgefhe-functional-sparse-n4096-v4-s29`、scale 29／58、`security_bits`＝null，aarch64、native→PL、formal release＝`false`；V5 收據 `board_DMA_operations`＝0、`device_opened`＝false。
- **發布安全**（`Build/secret artifact`、`Private key`、`GitHub oversized file`）：單檔小於 100 MB；不得有 `join_config.json`、`server_key.pem`、`id_rsa`、`id_ed25519`、`.o`、`.pfx`、`.p12` 或 PEM 私鑰區塊；`.ko` 只能是那個預編譯模組（第 6.3 節）。處理：移出 repo（第 1.2 節）。
- `.py` 語法；`evidence/original/` 以外 `.md` 的相對連結都要存在（`Broken link in <文件>: <連結>`；不查程式碼區塊、`http(s):`、`mailto:`、`#` 錨點）。

雜湊清單只核完整性：**沒有簽章者、沒有信任鏈，不是數位簽章**。

### 4.3 換行與 .gitignore

[.gitattributes](../.gitattributes) 的 `* -text` 讓 Git 不轉換任何換行：clone 到任何 OS 雜湊不變、凍結的 CRLF 來源保持原樣，跨平台編譯器與 Python 都能讀；`*.bin`、`*.dtbo`、`*.zip`、`*.pdf` 另標 binary。編輯器也不要改換行或編碼。[.gitignore](../.gitignore) 排除建置產物（`build/`、`*.o`、`*.ko`、`*.log`、`.Xil/` 等）、`local/`、`__pycache__/`、`*.pyc` 與祕密檔名（`join_config.json`、`server_key.pem`、`server_cert.pem`、`.env`），例外只有 `release/native_v4/*.log`（驗收收據）、`results/**/*.log`（板上紀錄）與那個預編譯 V3 `.ko`。另外排除下載的 CDC 資料（`data/*/raw/`、`data/*/splits/`，用 `tools/prepare_datasets.py` 重建）、`training/build/`、dashboard 的 `node_modules/` 與 `.next/`、個人編輯器設定 `.vscode/`。

### 4.4 Python：只用標準庫、不留 __pycache__

Python（含新寫的）只用標準函式庫，沒有 pip 套件（[requirements.txt](../requirements.txt) 只是說明）。執行前設 `PYTHONDONTWRITEBYTECODE=1`：實測沒設時，`tools/unpack_ip.py` 會留下 `tools/__pycache__/`，`run_native_cpu_acceptance.py` 跑的 V4 腳本會在 `software/native_ckks/`（釘住）、`software/ckks/`（凍結）留下 `__pycache__/`。它不影響驗證（`.gitignore`、清單、收據都略過），但會留在鎖住的資料夾：用 `find . -name __pycache__ -not -path './build/*'`（PowerShell：`Get-ChildItem -Recurse -Directory -Filter __pycache__`）找出刪掉。新程式在匯入同目錄模組前設 `sys.dont_write_bytecode = True`。

### 4.5 看起來重複、但不能刪

- `paired_public_vectors.cpp` 在 `software/reference/`、`software/ckks/`、`software/board_tests/` 各一份相同副本：每個目錄是整包複製到板上的獨立套件，`software/ckks/backend.cpp`、`software/reference/mixed_runner.cpp` 直接 `#include "paired_public_vectors.cpp"`，`software/board_tests/` 經 `soak_mixed_support.hpp` 引入。刪一份該套件就無法編譯（引入它的程式是凍結的）；三份也都是凍結副本，步驟 2 會拒絕。
- `software/driver/Makefile` 與 `software/driver_candidate_v4/Makefile` 相同：Linux 核心模組建置要求每個驅動目錄都有自己的 Makefile。

## 5. tools/ 工具一覽 / Tool reference

「碰裝置」＝開啟或使用 `/dev/edgefhe_rns3_n4096_dma`、載入 FPGA 或模組、做 DMA；沒有工具會自己載入 FPGA 或模組。平台「全部」＝Windows、Linux、WSL、KV260；「新」＝路徑已存在就拒絕。

| 工具 | 用途 | 平台 | 碰裝置 | 參數 → 標記 |
|---|---|---|---|---|
| `build_driver.py` | 板上編譯 V3 驅動 | KV260 | 否 | 第 3.4 節、BOARD_GUIDE 關卡 B |
| `build_hls.py` | HLS launcher | 有 Vitis HLS 2022.1 | 否 | 第 3.1 節 |
| `build_vivado.py` | Vivado launcher | 有 Vivado 2022.1 | 否 | 第 3.2 節 |
| `check_environment.py` | 唯讀清點 Python、g++、cmake、make、記憶體／CMA；`kv260` 另查 aarch64、kernel headers、dtc、fdtoverlay、xmutil、modinfo、openssl | 全部 | 否 | `--target host` 或 `kv260`、`--output`（新）；標記見第 1.1 節 |
| `compare_results.py` | `v5_e2e` 結果→A/B 表（`.md`＋`.json`），先查公平規則 | 全部 | 否 | BOARD_GUIDE 3.5、[COMPARISON](COMPARISON.md) |
| `inspect_prebuilt.py` | 唯讀核對預編譯 V3 `.ko`；`--target` 再比對目前 kernel 與架構 | 全部（`--target` 在 KV260） | 否 | BOARD_GUIDE 關卡 B |
| `inventory.py` | 清單規則：哪些檔案進 `SHA256SUMS`（和 `.gitignore` 一致）；`update_manifest.py`、`verify_repository.py`、`pack_for_board.py` 共用 | 全部 | 否 | 第 4.1 節 |
| `kv260/`（PowerShell） | Windows 序列埠：送指令、傳檔、收檔 | Windows | 只操作主控台；板上動作照 BOARD_GUIDE | [說明](../tools/kv260/README.md) |
| `offline_check.py` | 離線功能檢查 | 全部（`--native`：Linux／WSL） | 否 | 第 2.3 節 |
| `pack_for_board.py` | 打包上板檔案：完整／`--serial`／`--code-only`，附 `BUNDLE_MANIFEST.json`（SHA-256） | 全部 | 否 | BOARD_GUIDE 1.2 |
| `prepare_datasets.py` | 下載 CDC 資料、以固定種子切分並核對 `data/*/manifest.json` | 全部 | 否 | [data/README](../data/README.md) |
| `require_pass.py` | CTest 轉接 | 全部 | 否 | `--marker "<標記>" -- <指令>` → `CPU_TEST_EXIT_AND_EXPLICIT_PASS_ACCEPTED` |
| `run_comparison.py` | A/B 階段 1、2：建置、自檢、`v5_e2e`、報表 | Linux（KV260、WSL） | 階段 2 `--fd`：是（只用操作員開好的 fd，不自己開、不重置、不重試）；其他否 | BOARD_GUIDE 第 3 節、[COMPARISON](COMPARISON.md) |
| `run_native_cpu_acceptance.py` | V4 CPU-only 驗收批次 | Linux／WSL／KV260，要 CMake | 否 | [NATIVE_V4](NATIVE_V4.md) |
| `run_v5_checks.py` | V5 主機檢查與收據 | Linux／WSL | 否 | `--output`（新）、`--receipt`、`--skip-tsan`、`--jobs`；第 4.1 節 |
| `trace_dma_call.py` | 用 ftrace＋kprobe 拆開每次 FPGA 呼叫（不改驅動），事後還原追蹤設定 | KV260（root）；`--parse`、`--self-test` 全部 | 是：只用操作員開好的 fd，不自己開、不重置、不重試 | BOARD_GUIDE 3.3.1 |
| `unpack_ip.py` | 解出凍結 IP ZIP | 全部 | 否 | 第 3.2 節 |
| `update_manifest.py` | 重寫 `SHA256SUMS` | 全部 | 否 | 第 4.1 節 |
| `v5_sources.py` | V5 原始碼清單與 g++ 建置（兩個 `run_*` 共用）；免 CMake 建置 | Linux／WSL／KV260 | 否 | 第 2.2 節 |
| `verify_repository.py` | 唯讀全套檢查 | 全部 | 否 | 第 4.2 節 |

不在 `tools/`：板上 gates 用的 `software/kv260/` 檢查工具與兩機展示用的 `software/native_v5/python/`（`edge_client.py`、`make_demo_tls.py`）見 [BOARD_GUIDE](BOARD_GUIDE.md)。

## 6. 更新到 GitHub / Publishing to GitHub

### 6.1 原則

- 2026-10-14 起，專題資料夾（`Grad/`）本身就是 repo 根目錄：程式碼、文件、`data/` 說明、`training/`、`results/`、`dashboard/` 都在同一個 repo（之前程式碼在 `EdgeFHE-KV260-GitHub/` 子資料夾）。沒有嵌入其他 `.git` 或原 credentials；撰寫時尚未 commit／push。
- 上傳前先決定公開範圍：提案、進度報告、會議紀錄（`docs/` 的 PDF 與 `2026-10-13_*.md`）和上板結果也在 repo 裡。不想公開的列進 `.gitignore`，再跑第 4 節的完整性指令。
- 在既有 GitHub repo 開分支，整理進本資料夾頂層內容，保留原 `.git`。舊 frontend、datasets、V2 不必全刪；保留就移到明確的 legacy 資料夾或獨立分支並註明不屬目前接受的版本。不要混用舊 README 與本版結果，不要用鏡像刪除清空既有 repo。
- 提交前完成第 4 節；保留版本邊界（V3 凍結來源與證據原樣、V4 candidate 與 V5 分開），都還不是正式 release。

### 6.2 指令

內容合併進 Git repo 後，由操作員在 Linux／WSL 逐行執行：

```bash
git switch -c update/ii1-v3-snapshot
python3 tools/verify_repository.py
python3 tools/offline_check.py --native
git status --short
git diff --stat
git add README.md SECURITY.md NOTICE.md LICENSE CMakeLists.txt requirements.txt .gitignore .gitattributes .github hardware firmware software validation evidence docs tools release data training results dashboard
git diff --cached --stat
git commit -m "Consolidate II1 N4096 V3 research snapshot and evidence"
git push -u origin update/ii1-v3-snapshot
```

- 分支名與 commit 訊息是 V3 打包時的範例，內容已含 V4、V5，請調整；`git add` 逐一列出頂層項目，不會加入 `build/`、`local/`。
- 分支已存在就切到既有工作分支，不要盲目重建；`git push` 對外寫入，只由操作員在檢查差異與 remote 後執行。
- 合併 PR 前至少一人確認：程式來源、credentials 已排除、效能限制與部分完成的 gates 都寫清楚。

### 6.3 不能提交的東西

- `build/`、`local/`、新產生的輸出、大型 session logs、長測 TSV（不需要 Git LFS，單檔上限見第 4.2 節）。
- 祕密：私鑰、token、`private_tls`、client／server／CA private key、private CSV。
- 自己編譯、跨 kernel 或 V4 的 `.ko`：唯一允許的是 `firmware/prebuilt/ubuntu-22.04-5.15.0-1027-xilinx-zynqmp/` 的原 V3 `.ko`（附對應 GPL 原始碼與身分，只適用該 kernel）。

`verify_repository.py` 只是最後一道網；提交前自己看一遍 `git diff --cached --stat`。

## 7. 持續整合 / Continuous integration

[.github/workflows/offline.yml](../.github/workflows/offline.yml)（`Fixed-profile offline verification`）：push、pull_request、手動 workflow_dispatch 觸發；`contents: read`；job `cpu-only`、`ubuntu-22.04`、逾時 15 分鐘；`actions/checkout` v4.2.2 釘在其發布 commit。內容是第 1.1 節的 Linux 指令，但沒有 `apt-get` 與 `export` 兩行，環境報告寫到 `build/host_environment.json`。

- 只做主機 CPU：**不跑** `run_v5_checks.py`（sanitizers、mTLS 演練）、`offline_check.py --native`、HLS／Vivado，不碰板子；CI 通過不代表 HLS、P&R、板上、安全、網路隱私或 release 被接受。
- 清單或收據過期時，CI 在 `offline_check.py`（內含 `verify_repository.py`）失敗：push 前先完成第 4 節。
- 撰寫時（2026-10-14）此 workflow 尚未在 GitHub 上執行過。
