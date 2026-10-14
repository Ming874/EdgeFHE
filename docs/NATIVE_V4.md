# V4 原生 CKKS 候選 / Native V4 CKKS candidate

V4＝C++17 原生 CKKS client 數學＋只收密文、可接任意輸入的 mTLS 服務。**只在主機(x86_64 WSL)與本機 loopback 驗收**；未以 V4 身分上 KV260、不宣稱安全位元數、`formal_release=false`。

版本 `edgefhe-native-ckks-v4-s29-candidate-3`(2026-10-07)、profile `edgefhe-functional-sparse-n4096-v4-s29`、狀態 `LOCAL_CPU_AND_LOOPBACK_ACCEPTED_CANDIDATE_NOT_BOARD_OR_SECURITY_RELEASE`。逐檔說明、數學契約、最佳化與兩機設定見 [native_ckks README](../software/native_ckks/README.md)；[NATIVE_CANDIDATE.json](../release/NATIVE_CANDIDATE.json) 以 SHA-256 鎖定 16 個來源與 15 份收據。

## 目錄 / Contents

1. [狀態總覽](#1-狀態總覽--status-at-a-glance)
2. [V4 是什麼](#2-v4-是什麼--what-v4-is)
3. [建置與驗收](#3-建置與驗收--build-and-acceptance)
4. [實驗與數據](#4-實驗與數據--experiments-and-numbers)
5. [審查逐項回應](#5-審查逐項回應--review-responses)
6. [威脅模型與私密輸入](#6-威脅模型與私密輸入--threat-model-and-private-input)
7. [完成定義與尚未完成](#7-完成定義與尚未完成--definition-of-done-and-outstanding-work)
8. [證據檔案](#8-證據檔案--evidence-files)

## 1. 狀態總覽 / Status at a glance

收據 `acceptance`（x86_64 WSL Ubuntu 24.04、`board_operations=0`）：
- **通過**：`distinct_dense_cases=1000`、`three_way_exact_coefficients=12288000`（零差異）、`complete_crypto_cases=12`、`loopback_dynamic_batches=6`、`local_mTLS=true`（僅 127.0.0.1）、`ASAN_UBSAN=true`。
- **false**：`aarch64_native`、`V4_native_to_PL`、`V4_two_machine`、`production_security_profile`(`security_bits=null`)、`physical_reset_or_fault_recovery`、`driver_candidate_full_kernel_compile`、`formal_release`。
- 測試用 x86 `.so`(`80408e22…`)未收錄(`x86_binary_included=false`)；`frozen_V3_source_and_firmware_changed=false`。
- 收據限制：V3 板測驗收只綁凍結來源與 scale 2^24，V4 不沿用；本機 CPU 比值非 ARM/FPGA/網路 E2E/安全主張；OpenFHE moduli 與分布不同、密文不互通；driver 草稿不可部署(3.5)。

**2026-10-14 透過 V5 的實板證據**：V5 原樣重用 V4 `core.cpp`，板上記錄的 `core.cpp`、`c_api.cpp` SHA 與鎖定值相同。
- `v5_parity --fd 3 --cases 64` → `V5_PARITY_PASS device=linux-dma cases=64 coefficients=786432 client_round=exact`：64 組隨機乘積（64×3 limbs×4096 係數）實際 PL 與 CPU 路徑（V4 數學）逐位元相同，完整 client 回合（keygen、四特徵加密、解密）FPGA＝CPU；77 次 DMA 請求無錯誤。
- `V5_CAPI_PASS contexts=4 bit_exact=1`：板上(aarch64/GCC 11)以 V4 `core.cpp`＋`c_api.cpp` 編的函式庫與 V5 cpu-w1、cpu-w4、模擬 FPGA 逐位元相同。

故 V4 數學能在板上編譯且與 PL 一致，但 V4 狀態不變：收據雜湊鎖定不改寫；一致性走 V5 `FpgaBackend`/`LinuxDmaDevice`，未用 V4 的 board-fd DMA 路徑(3.3)；V4 CPU-only 批次(3.2)、ARM 數字、實體兩機與私密輸入隔離都還沒有。V5 結果見 [COMPARISON](COMPARISON.md)，證據狀態見 [VALIDATION](VALIDATION.md)。

## 2. V4 是什麼 / What V4 is

獨立於已驗收 V3 基線的候選：client 讀本機動態資料、以 C++ 加密，只把密文送給只有公開模型的服務，解密與 sigmoid 留在 client。保留自訂稀疏研究 profile，非正式安全版本。

- **C++17 `Engine`**：canonical encoder/decoder、OS 取樣(`getrandom`)、RNS、NTT、KeyGen、四特徵 Encrypt、affine、Decrypt、centered CRT(89-bit)、no-wrap 與誤差上界、sigmoid 與分類區間；經固定長度 C ABI 給 Python ctypes；純 C++ 的 `edgefhe_native_demo` 免 Python 跑完整數學。
- **Python**：只做輸入、CSV、JSON/HTTP/TLS 與顯示（兩機 CLI 的網路仍是 Python，浮點數學不回 Python）。對照實作為 `python_baseline.py`（同演算法純 Python）與標準 OpenFHE(4.4)。
- **協定** `edgefhe-dynamic-affine-tls-v4`：可設 port 的密文服務、client-certificate mTLS、pin、Bearer token、request binding、有界服務。
- **參數**：N、slots、q、環與 s/u/e 稀疏權重都同 V3（數值見 README 第 2 節），只改輸入 scale 2^29、輸出 2^58（V3 為 2^24/2^48）與 profile ID，故非 V3 的 wire-compatible 替代品（封包拒絕錯誤 profile/scale）；PL primes/NTT 不變（仍是已驗收 Montgomery HLS），但各輸入範圍的整數界線須重驗。

**資料流**：stdin 或本機 CSV 的四個標準化特徵（每批 1–2048 筆，一 slot 一筆）→ C++ 編碼、取樣、加密成四份 feature-major 密文（公鑰 session 內重用，u/e0/e1 每批重抽）→ 只有密文經 mTLS 送出 → 服務以 C++ 算 affine → 加密 logits → client 解密、解碼、sigmoid；[logit − E_aff, logit + E_aff] 跨 0 時回 `class=None`，不硬判。

**對照凍結 V3**：已驗收 PL＋Linux DMA 基線、Python client、server-pinned TLS＋token（非 mTLS）。

## 3. 建置與驗收 / Build and acceptance

**規則**：先保留凍結 V3 韌體、來源與歷史證據，不改歷史 PASS 的來源；板上操作只由操作者做，V4 工具不做 SSH/SCP/insmod/xmutil/DMA/reboot；在目標架構編譯，WSL x86 或 Windows binaries 不能上板；Linux/WSL 做建置與 CPU 測試，Windows 只做 inventory/SHA/Python protocol tests。需 GCC 或 Clang(C++17)、CMake ≥3.20、Python ≥3.10、pthread；無 pip 相依，不需 Boost/kernel headers/FPGA；loopback TLS QA 另需 `openssl`。主機（第 4 節數字）：WSL Ubuntu 24.04、GCC 13.3.0、CMake 3.28.3、Python 3.12.3；板子：Ubuntu 22.04.4、aarch64、GCC 11.4.0、kernel `5.15.0-1027-xilinx-zynqmp`、Python 3.10.12。只有 exit=0 不算，須見註解中的 marker。

### 3.1 主機驗收

```bash
# A. 整個 repo(repository root)
python3 tools/verify_repository.py   # REPOSITORY_MANIFEST_AND_DOCUMENTS_PASS
python3 tools/inspect_prebuilt.py    # 離線：PREBUILT_V3_EXACT_ELF_METADATA_PASS target_match=False
cmake -S . -B build/team-native-v4 -DCMAKE_BUILD_TYPE=Release
cmake --build build/team-native-v4 -j 4
ctest --test-dir build/team-native-v4 --output-on-failure   # 100% tests passed
# B. 只建 V4
cmake -S software/native_ckks -B build/native-v4 -DCMAKE_BUILD_TYPE=Release
cmake --build build/native-v4 -j 4
ctest --test-dir build/native-v4 --output-on-failure   # NATIVE_CPP_SELFTEST_PASS、V4_PROTOCOL_OFFLINE_NEGATIVE_PASS
python3 software/native_ckks/tests/validate_native.py --build build/native-v4   # NATIVE_LOCAL_MATHEMATICAL_ACCEPTANCE_PASS
python3 software/native_ckks/benchmark.py --library build/native-v4/libedgefhe_native.so --output local/cpu_benchmark_01.json --rounds 30 --warmups 3   # PYTHON_CPP_PAIRED_CPU_BENCHMARK_PASS
python3 software/native_ckks/tests/local_network_test.py --library build/native-v4/libedgefhe_native.so --output local/tls_qa_01   # DYNAMIC_PRIVATE_INPUT_LOCAL_MTLS_PASS
./build/native-v4/edgefhe_native_demo   # stdin 輸入四個 −16～16 的值（不放命令列）；NATIVE_CPU_DYNAMIC_DEMO_PASS
```

- root build 預設含 V5(`EDGEFHE_BUILD_V5=ON`)，CTest 9 項：V3 `public_cpu_exact`、`mixed_cpu_exact`、`ckks_cpu_affine`；V4 `native_complete_math`、`native_protocol_offline`；V5 `v5_unit`、`v5_parity_simulated`、`v5_e2e_sample`、`v5_capi_python`。收據 `clean_root_ctest.log` 是 V5 前的 5 項（`-DEDGEFHE_BUILD_V5=OFF` 重現）。只有 CPU 測試，不主張硬體計時或 kernel probe。
- 產物 `libedgefhe_native.so`、`edgefhe_native_demo`（本機 CPU 示範）、`edgefhe_native_selftest`；`validate_native.py` 預設 `--dense-cases 1000`，報告在 `build/native-v4/native_acceptance.json`；`local/`（含短效憑證、token）不可提交 Git。

### 3.2 KV260 只用 CPU 的批次（操作者執行）

取得 V4 第一份 aarch64 數據。只用 CPU（DMA=0、FPGA load=0、module changes=0、network=0），不必卸載或重載已接受的 PL/V3 driver，不改 kernel 或 PL。開始前：
- 需 `g++`、`cmake` ≥3.20（含 `ctest`）、`python3` ≥3.10。**2026-10-14 板上 `cmake_available=false`**（V5 不需要）；本工具的 configure/build/ctest 都要它，缺了停在 configure；當天板子無網路，先備好離線安裝。
- 板上 repo 須與 manifest 一致，否則第一步 inventory 失敗；板子無 RTC 同步（當天開機時鐘 2023-11-22），先把 `date` 設成正確 UTC。
- `--rounds` ≥10、`--dense-cases` ≥1000、輸出目錄須是新的，否則拒絕執行（不覆寫舊證據）。

```bash
command -v g++ cmake ctest python3   # 必須印出四個路徑
REPO="$HOME/Grad"   # 板上的 repo 根目錄（2026-10-14 之前是 $HOME/Grad/EdgeFHE-KV260-GitHub）
cd "$REPO"
NATIVE_RUN="$HOME/edgefhe-native-v4-cpu.$(date +%Y%m%d-%H%M%S)"
python3 tools/run_native_cpu_acceptance.py --output "$NATIVE_RUN" --rounds 30 --dense-cases 1000
printf '\nEvidence directory: %s\n' "$NATIVE_RUN"
```

工具依序跑 `inventory`（SHA 與環境身分）、`configure`/`build`、`ctest`、`math`（1000 組 dense＋完整加解密）、`benchmark`（30 對＋1/3 worker，見 4.2），marker 同 3.1；板上有 aarch64 OpenFHE 才加 `--openfhe-dir /path/to/lib/OpenFHE`（OpenMP 1/3/4 → `OPENFHE_STANDARD_CLIENT_BENCHMARK_PASS`；不保證用滿核心，存實際 timings 與 CPU affinity）。結束須見 `NATIVE_V4_CPU_ONLY_OPERATOR_ACCEPTANCE_PASS DMA=0 FPGA_load=0 no_kernel_module_changes`。

帶回 console 與 `$NATIVE_RUN/` 的 `cpu_acceptance_report.json`（失敗也寫，`status=FAIL` 加原因）、`benchmark_cpu.json`、`build/native_acceptance.json`、各步 `.log`（與 `openfhe_*.log`/`.tsv`）。失敗即停、保留目錄、不自動重試或 reload；原樣帶回，勿在板上改程式重跑。通過後才可加入 V4 ARM 數據。主機乾淨解壓預演（x86、`--rounds 10`、約 78 秒）已得同一結束 marker，`.so` SHA 與測試版相同(`clean_operator_report.json`)；非 ARM 結果，ARM 耗時未量。

### 3.3 兩機展示

loopback QA＝3.1 的 `local_network_test.py`（每次新目錄，如 `local/tls_qa_new`；內容見 4.1 與第 6 節）自產 loopback 專用憑證與 token；QA 憑證（SAN 僅 `localhost`、`127.0.0.1`）不可用於實際 PC/KV260。

兩機（PC 服務、KV260 client）的設定與憑證由操作者準備；角色、private 目錄與 config 欄位見 README 第 5 節。另須：CA 依角色簽 EKU(serverAuth/clientAuth)，server DER 憑證 SHA-256(pin)與公開模型 SHA 經可信管道交換；`net_config.py` 拒絕 HTTP、帳密、path、query、fragment、port 0 與 1–65535 以外的 port，`server_id` 限 1–64 字元。

```bash
# PC（--audit 須是新路徑）
python3 software/native_ckks/pc_server.py --config local/server_config.json --private-dir local/server --model software/native_ckks/model.example.json --library build/native-v4/libedgefhe_native.so --audit local/server_audit_01.jsonl --bind 0.0.0.0
# KV260（批次可加 --input-csv local/private_input.csv）
python3 software/native_ckks/dynamic_client.py --config local/client_config.json --server-cert local/server_ca.pem --private-dir local/client --library build/native-v4/libedgefhe_native.so --model software/native_ckks/model.example.json
```

先 `/ready`（核對公開模型與 SHA），再從 stdin 或 private CSV 輸入。`dynamic_client.py` 預設 CPU（`--workers` 1 或 3，預設 3），不開 `/dev/edgefhe_rns3_n4096_dma`，沒有跳過 V4 硬體身分 gate 的自動 PL 選項；C++ API 可收已開啟的 board fd，但未對 V4 板測。

兩機驗收：① server 無 client secret、fixtures、明文紀錄；② 兩端公開模型 SHA 一致；③ 封包 byte 身分正確、scale 2^29；④ client 的 uncertainty-aware 結果符合事先存於 client 的明文 oracle；⑤ 記錄新的 V4 artifact/compiler/CPU/網路時脈身分與原始 phase times。本機合成測試不能取代；V5 兩機 client（階段 3）見 [COMPARISON](COMPARISON.md)。

### 3.4 可選的預編譯 V3 模組

`firmware/prebuilt/ubuntu-22.04-5.15.0-1027-xilinx-zynqmp/`：已接受 V3 `.ko` 原始 bytes，附 SHA、version `ii1-guarded-v3`、srcversion `6BDCA74AD872B2765D36882`、vermagic、GPL source。只限 Ubuntu 22.04.4/kernel `5.15.0-1027-xilinx-zynqmp` 原 build（同一已接受系統可免編譯）；非 V4 driver、非新 kernel build、不跨 kernel，其他 kernel/config 依 [BOARD_GUIDE](BOARD_GUIDE.md) 以 matching headers 重編並重驗 probe/DMA。

```bash
python3 tools/inspect_prebuilt.py --target   # 板上唯讀：PREBUILT_V3_EXACT_ELF_METADATA_PASS target_match=True
```

只證明 SHA/ELF/modinfo 與目前 uname 相符（2026-10-14 板上已得），非 live ABI/binding/probe/DMA 驗收，工具不 insmod；在 x86 或其他 kernel 會報 target 不符錯誤（預期，訊息見 `clean_x86_module_rejected.log`）。已接受的 runtime 在跑時，勿為核對而卸載。

### 3.5 故障處理 driver 草稿（不可部署）

`software/driver_candidate_v4/`(`d468a6b0…`)與已接受的 `software/driver/` 分開。起因：V3 的 completion 在 stack、不檢查 termination 錯誤，有條件性風險；host 反例：DMA 未真正停止時 wipe/free 不安全。
- 改動：completion 存在 proxy、隨其存活；檢查兩個 `terminate_sync` 回傳；出錯時保留 callback/storage/module/device/buffers 到有人監督的重開機並拒絕新 job；成功 DMA 後的 output copy `EFAULT` 不再誤判為 IP 失敗。
- host model（ASAN/UBSAN；精確 C lifecycle 片段＋transfer/provider/VFS stubs）：4 個正常 scenario、terminate 錯誤 → quarantine、late callback 落在保留的 storage。
- 未解決：provider 回報成功但 DMA 仍動(false success)，不能取代實體確認；remove 時的 module pin 不保證取消已開始的卸載，卸載進度與 owner lifetime 需 kernel 審查；完整 kernel 編譯、實體 DDR/PL quiescence、實體 fault/reset/resume/remove、fault injection、板上確認都未做；無自動恢復、解除 quarantine 或 PL reload API。
- 規則：不部署；不以 host stub PASS 宣稱恢復；不靠重試或忽略錯誤製造 PASS。下一步：owner lifetime 與跨模組 physical reset/DDR drain 契約 → 實測 provider 同步 → 完整 kernel 編譯與 fault injection → 板上確認。

## 4. 實驗與數據 / Experiments and numbers

x86_64 WSL（環境見第 3 節）、board operations=0；數字由原始 JSON（第 8 節）產生。誤差值是收據值取 6 位有效數字並**無條件進位**（`1.6320811990767195e-07` → 1.63209e-7，可能比四捨五入多 1 個末位）；時間取到 0.001 μs。

### 4.1 數學正確性

| 實驗 | 結果 | 驗證 |
|---|---|---|
| Encoder Python↔C++ | 24 cases、98,304 係數零差異、ties 8、max slot error 1.63209e-7 | 同 embedding、round-to-even；full complex slots、direct polynomial evaluation |
| 三方 dense negacyclic 乘法 | 1000 組、12,288,000 canonical RNS 係數零差異 | 新 Shoup/Barrett NTT、凍結 uint64 modulo NTT、Python 精確 Kronecker 卷積 |
| 完整 client 算術 | 12 cases、1,867,776 次整數/RNS 比對、max affine slot error 6.66928e-7；5 項負向測試；OS 取樣結構與新鮮度 | KeyGen/Encrypt/public affine/Decrypt/centered CRT；獨立整數 sparse noise oracle |
| C++ selftest＋ASAN/UBSAN | 8192 complex slots、16,384 noise 係數、4096 CRT 邊界、24,576 scalar-affine 係數、30,000 Barrett products，全 PASS | 完整 C++ 路徑、精確模乘、常數快路徑、記憶體與 UB |
| 動態 loopback mTLS | 2 port、6 批(1/17/2048 records)、29 checks、max logit error 7.60646e-7 | 無 fixtures 的任意輸入與第 6 節的協定檢查 |

- **獨立 oracle**：Python 以 B=2^80（> N(q−1)^2）打包、一次大整數相乘得完整非循環卷積，再算 c[j]−c[j+N] mod q；不是 NTT round trip、無浮點 FFT，不會與被測 NTT 共用 root/bit-reversal 錯誤。公開固定種子只產生不同的 QA 輸入（stream SHA-256 `8e5af515fe202aed6a7cfbb4618d1cda7fae8d0e2d83e438ccd9359ff767e5af`），部署用 OS `getrandom`、不重用；CPU oracle 不等於新 RTL 或板測。
- **精確與近似**：mod/整數須完全相等，只有 slots 用浮點誤差，分開檢查，不以單一 RoundTrip PASS 推論全部正確。範例模型（輸入絕對值 ≤16、weights [1.1, −0.7, 0.9, 0.4]、bias −0.35）公開 bound 0.0002685752525828072 < 設定 5e-4；每批另驗 2U<Q；浮點 allowance g=1e-8 經 direct check，非 IEEE 形式化證明。

### 4.2 Python 與 C++ 公平配對

warm-up 3、30 對；同一組公開 input/key/mask/error，相同 cached transforms、公鑰重用、scalar affine 快路徑（C++ 另特化 Shoup/Barrett、不經 Python 物件）；affinity [19]、交錯呼叫；計時含 ctypes、轉換、配置，不含 plan 建立、golden oracle、驗證；kernel `6.6.87.2-microsoft-standard-WSL2`。相同 material 只是公開配對，非部署時重用 nonce/隨機數。單位 μs；C++1＝單一 worker；比值＝Python P50 ÷ C++1 P50（非逐對 speedup 中位數）。

| 收據鍵 | Python P50 | Python P95 | C++1 P50 | C++1 P95 | 比值 |
|---|---:|---:|---:|---:|---:|
| `encode_slots_1` | 5039.748 | 5499.872 | 388.931 | 410.355 | 12.958 |
| `encode_slots_128` | 5176.361 | 6420.929 | 441.073 | 515.218 | 11.736 |
| `encode_slots_2048` | 5024.450 | 5562.063 | 875.410 | 1014.891 | 5.740 |
| `decode_slots_2048` | 4246.080 | 4781.601 | 1150.817 | 1453.052 | 3.690 |
| `keygen` | 39670.669 | 40595.176 | 1231.800 | 1361.124 | 32.205 |
| `encrypt4` | 191409.098 | 194426.860 | 6360.260 | 6846.127 | 30.095 |
| `cloud_affine` | 10510.513 | 11412.615 | 723.596 | 871.283 | 14.525 |
| `decrypt`（含 CRT） | 42459.897 | 48729.695 | 1858.649 | 2348.935 | 22.844 |
| `OS_sampling_material` | 9601.481 | 10170.074 | 150.014 | 164.130 | 64.004 |
| `CPU_math_hot_path` | 284312.512 | 290812.537 | 12565.270 | 13687.378 | 22.627 |

hot path＝四特徵 encode＋encrypt＋本機 affine＋decrypt＋decode，公鑰固定；**不含** key setup、OS 取樣、packet/TLS/網路、外部 PC 排程，故不能說端到端部署或 FPGA 快 22.63 倍；sampler 與 KeyGen 另量。decode 一律轉全部 4096 係數/2048 slots；收據的 decode 1/128 slots（Python P50 4329.424/4242.416、C++1 1133.978/1157.844、比值 3.818/3.664）與全槽位相近。

### 4.3 多 worker 與滿槽位

C++3＝三個 RNS limbs 分給 2 個 `std::thread`＋呼叫端，非四筆 DMA 並行（V4 保持一筆 DMA 在途）；另一次交錯配對：affinity [0..19]、warm-up 3、30 對。

| 收據鍵 | C++1 P50/P95 μs | C++3 P50/P95 μs |
|---|---:|---:|
| `CPU_cpp_parallel_hot_path`（1 筆） | 12545.018/13950.075 | 8652.962/9975.019 |
| `CPU_cpp_full_slot_hot_path`（2048 筆） | 14071.429/16274.096 | 10920.418/11911.232 |

第一列 P50 比值 1.4498。滿槽位只支持容量與 CPU 數學正確，不能推出 ARM records/s、TLS 吞吐或雙板線性擴展；thread 建立/join 成本仍在路徑內，原計畫 ARM 實測後再做 persistent worker 與 H 有界管線（V5 已加入）。

### 4.4 標準 OpenFHE 對照

`validation/openfhe_client/baseline.cpp`：OpenFHE v1.5.1 revision `1306d14f8c26bb6150d3e6ad54f28dfe1007689e`、UNIFORM_TERNARY、configured `HEStd_128_classic`、N 4096/batch 2048、scale 29、first 30、depth 2、BV/FIXEDMANUAL；實際 q 1073692673/536813569/536903681。四個滿槽位特徵，cloud 做 ciphertext×plaintext affine，edge 解密解碼；key 與公開 weights 只做一次、在 hot path 外，KeyGen 另測；30 rounds、warm-up 3、affinity [0..19]。

| OpenMP | Hot path P50 μs | P95 μs | max slot error |
|---:|---:|---:|---:|
| 1 | 7759.567 | 9373.348 | 0.000242754 |
| 3 | 6979.763 | 10942.455 | 0.000274566 |
| 4 | 7268.785 | 9506.080 | 0.000254537 |

OpenMP 不保證每階段用滿 workers。標準庫已很有效率，手寫 C++ 不一定最好；FFI/API、affinity 也與 V4 不同，只是獨立 baseline，不作 security-equivalent 或 FPGA speedup 比值。下次 ARM 實驗須保存相同環境資訊、profile 差異、raw phase times 與 pinning。

### 4.5 最佳化、失敗與修正

已實作 PS cached roots/Shoup、Barrett reciprocal、fused inverse normalization、NTT 重用、全 slot 同權重時的 O(N) scalar affine（weights 為常數多項式），一般多項式權重保留 fused NTT fallback（細節見 README 第 3 節）。快路徑係數另與獨立 uint64 scalar modulo 比對；Python baseline 用同一 O(N) 快路徑，避免演算法劣勢放大語言比較；PS 程式不放進 HLS project。

`FAILURE_AND_CORRECTIONS.json`（`FAILURES_RETAINED_CORRECTIONS_APPEND_ONLY`，失敗保留、修正只往後加）：

| 失敗 | 處理 |
|---|---|
| 第一次 `-Werror`(misleading-indentation) | 修正後重建 |
| portable test 既有目錄衝突 | 修正，保留第一次失敗 |
| OpenFHE 缺 pke/binfhe include、const Plaintext API、transitive loader RPATH | 修正 |
| OpenFHE scale 24 未過 5e-4 | 保留失敗；scale 29 是新 profile、新試驗，全部 slots 通過；V3 仍是 scale 24 |
| 舊 scale 24 OpenFHE 收據停在 `RUNNING`(`5830460d…`)，實際在 5e-4 門檻 FAIL | 不接受、不改寫成 PASS |
| CPU Barrett、fused INTT、scalar affine 每次修改 | 都重做精確數學與配對檢查 |

原始失敗 build 留在本機開發封存；修正後的本機測試不構成新的板上或安全驗收。

## 5. 審查逐項回應 / Review responses

外部 AI 審查的 C++ 與動態輸入缺口成立，已處理；「Python 一定是主瓶頸」改用第 4 節量測回答；「不做 bootstrapping 就不完整」「必須有跨 kernel KO」依提案範圍處理；V3 證據保留，另建 V4 候選與實驗。

| 審查項目 | 已落地 | 尚待驗收 |
|---|---|---|
| Port 18086 寫死 | `net_config.py` 從 HTTPS URL 取 port，預設 443；loopback 實測兩個 port，離線另驗 4 種合法寫法與 8 種錯誤 endpoint | V3 原檔保留以便重現；實際 endpoint 由操作者設定 |
| 無完整 C++ encoder/KeyGen/Encrypt/Decrypt | 第 2 節 | V4 本身的 aarch64、native→PL、兩機（第 1 節） |
| 單核心純量弱 baseline | 4.2–4.4，存全部 latencies | 第 7 節 |
| 公開六例 fixtures、假私密展示 | 無 fixtures、任意 request ID、只含密文的 header＋第 6 節的檢查 | 只到本機邊界（第 6 節）；真實資料與兩機見第 7 節；零洩漏、CCA、side-channel 不成立 |
| affine 單層，缺 rescale/rotation/多層 | 4.5 的 O(N)/fused NTT；scale 升到 2^29 並驗 no-wrap，精度改善；sigmoid 在 client | 多層 NN、密文 sigmoid 是新範圍（需 level 管理、relin/rotation keys、標準 profile），MVP 不算完成 |
| 缺 bootstrapping | 依原需求與提案第 15 頁，PL 不做 | 排除項非強制缺口；cloud 深模型或多方 CKKS 另立研究 |
| G07 實體 reset、G08 fatal/remove；無預編譯 KO | 3.5 的 driver 草稿與 host model；3.4 的原接受 V3 `.ko`（只核對不載入） | 見 3.5 與 3.4（非 V4 driver、不跨 kernel，其他 kernel 須重編並驗 probe/DMA） |
| 一鍵部署、乾淨發布 | 3.2 的 CPU-only 工具（新目錄、失敗保留 log）；新 ZIP 乾淨解壓重驗 | 不把不可逆 PL/kernel 操作藏進一鍵腳本；golden SD、全新板子 secrets/image/fault 完整重現待實板 |

G07（實體 reset）、G08（fatal／remove）、G12（乾淨重建與 release）的完整定義與狀態見 [VALIDATION](VALIDATION.md)。

**架構選擇**：已接受 PL 留作固定數學基線（只做 NTT/INTT/多項式 primitives，FFT、Linux 網路、bootstrapping 不進 FPGA），PS 用 native C++ 與有界服務，先量真正的部署 hot path（H/T/M 關卡見 [ARCHITECTURE](ARCHITECTURE.md)）。部署重用公鑰、移除 diagnostic decrypt 與 golden oracle：接 PL 時每 hot batch 9 次（8 次加密乘法＋1 次最終解密），cold key setup 另 1 次；V3 QA 每例 14 次 DMA（keygen 1、加密 8、diagnostic decrypt 4、final decrypt 1），兩者不可比速度。滿槽位靠 O(N) affine(4.5)，免 rotation 與額外 NTT；不同 slot 模型/packing、跨特徵 rotation、矩陣/CNN、多方門檻協定須另定 interface，不混用已接受 profiles。OpenFHE 是 PS 對照與未來安全前端候選（4.4）；正式接 PL 須對齊 primes/psi、NTT permutation/representation、sampler、CRT/levels、serialize context 並重做 HLS/RTL/P&R/板測，不能只換 library 或沿用舊 PASS。順序：H（一個 DMA worker＋有界 CPU/網路佇列）→ 有 ARM 逐段數據才做 T（雙板吞吐）；M（多方）另立。

**與原提案和組長規劃**：V4 補上提案的 C/C++ encoder 方向與互動 client 流程，保留 PS/PL 分工與 Montgomery；V3 交接時提出的公鑰重用、native C++、full slots、動態輸入已在本機做到（板上待驗）。未完成：顯著硬體 E2E speedup、安全資料實板隔離、twiddle ROM A/B、G07/G08/G12。mTLS、153-bit 安全、端到端 2x、1000 組不同 dense 輸入、H/T/M 多板里程碑是後續團隊要求，不能倒寫成原提案未完成。建議以已接受實作為基線、加上組長計畫的 owners/interfaces/H-T-M 管理，用本次數據修正宣稱；文件估分≠測試通過率，延伸項不與原 MVP 混成一個 100%；目前是研究 MVP 與團隊進階 candidate 交接，非 100% 專題驗收。

## 6. 威脅模型與私密輸入 / Threat model and private input

短版政策在 [SECURITY](../SECURITY.md)。本機測試證明的是程式、process 與協定的角色邊界，**不是** KV260/PC 兩機的正式私密展示。

| 資產 | 誰看得到 | V4 做法 |
|---|---|---|
| 原始 records、標準化 features | client PS、本機輸入檔 | 不進 request metadata、server fixture 或 audit |
| CKKS secret s | client 記憶體 | C++ 以 OS 亂數取樣，只用於 client 解密，不傳 server |
| 公開模型 w/bias、profile、record 數 | client 與 server | `/ready` 回傳模型與 SHA；client 與本機可信模型核對 |
| ciphertext、encrypted logits | client、TLS 服務 | 固定 dimensions、canonical q、scale/profile、request ID 與 digest 綁定 |
| client/server TLS 私鑰 | 各自 private 目錄 | mTLS 角色分離；repo 不附私鑰或 token；local QA 只用 7 天憑證 |
| Bearer token | 授權雙方 | 要求新的 256-bit token |
| 解密後 logits/機率 | client 本機顯示 | 不送回 cloud；不回傳解密成敗細節（避免成為 oracle） |

- **假設**：client OS 與 private 目錄可信；OS CSPRNG 正常；CA 與 server pin 經可信管道交換；模型 SHA 來源可信；不以 force unload 或故障後 overlay reload 繞過 DMA ownership；endpoint 可選 port，但不接受 HTTP、credentials、path、query、fragment、無效 port。
- **本機已測**：server 不 import `CASES`/private fixture；stdin/CSV 新值；mTLS client cert、Bearer、CA/hostname、pin（不符不送 `Authorization`）、TLS ≥1.2；拒絕 replay、ledger 滿、非 canonical 係數、錯誤 scale/profile、未知或私密 header 欄位（如 `plaintext`、`features`、`private_key`、`decoded_logit`）、截斷/竄改封包、錯誤 response binding；audit 只記 event、time、bind/port、mtls、public model SHA、完成次數、record 數，不存 body 或秘密。
- **仍看得到**：profile/model ID、payload 大小、record 數、request 時間與頻率、公開 weights/bias、服務成敗；TLS 與 CKKS 都不隱藏，故不宣稱零洩漏。
- **未涵蓋**：自訂 sparse profile 的安全位元數；標準 Gaussian sampler 與 OpenFHE 密碼互通；CPU cache/timing side channels（constant-time 未稽核）；可靠的 secret 記憶體清除；client OS 遭控制；惡意 cloud（可驗證運算、反覆 chosen-ciphertext 對話、錯誤側通道）；跨重啟的持久 replay 防護；正式憑證 rotation/revocation、權限與真實資料集稽核。no-wrap guard 與 model SHA 不是 cloud 計算正確的密碼學證明。
- **服務上限**：最多 4 個 worker；socket timeout 30 s；backlog 有限(`request_queue_size=8`)；request-ID ledger 有界不淘汰，預設 10,000（`--max-requests` 1–100,000），滿了 fail closed；只在記憶體，重啟清空；無無限容量或完整 DoS 保證；synthetic data 不代表真實敏感資料已驗收。
- **可以說**：「支援動態 client 輸入，具有 ciphertext-only API、mTLS 與本機角色隔離測試」；OpenFHE `HEStd_128_classic` 是另一 profile 的功能與效能對照。**不可以說**：安全產品、零洩漏、任何安全位元數（含 128-bit）、全密文 sigmoid、任意深度 FHE、100% 工程完成，或把 OpenFHE 的 configured security level（也不是新的獨立安全估算）轉移給手寫 sparse profile。

## 7. 完成定義與尚未完成 / Definition of done and outstanding work

全部達成才算完成：① 來源與 artifacts 可追溯 ② 精確係數零差異 ③ 浮點誤差在公開界線內 ④ 本機與 ARM/板上證據分開 ⑤ CPU baseline 含合理最佳化與 OpenFHE ⑥ 真實私密輸入與兩機紀錄 ⑦ 正常與故障 lifecycle 都通過 ⑧ 乾淨發布可重現。目前只完成本機 candidate 驗收（第 1 節）。尚待：
- 板上：ARM CPU 批次(3.2)、V4 aarch64 編譯與 ARM benchmarks、V4 native→PL、完整部署計時；有 ARM 原始結果再設計 V4 的 PL 與兩機驗收。
- 效能：公平硬體部署 E2E（ARM 最佳化與 FPGA 同 profile、同安全性、含網路）。
- 兩機與安全：實體兩機動態 mTLS 與私密 input isolation(3.3)、真實敏感資料、live UI；安全 profile 估算（安全分布）與第 6 節「未涵蓋」各項（含惡意 evaluator）。
- 故障：G07/G08（physical reset/fatal/remove/multiprocess）與 3.5 各項。
- 發布：clean image 與 signed release（G12）、真實模型/model card/實際資料標準化、熱與功耗、twiddle ROM A/B、H/T/M。

重複 V3 的 6 小時穩定性測試不能替 V4 新來源背書。範圍外：rotation、rescale、多層模型、多方 CKKS 另立；bootstrapping 依原需求排除在 PL 外。測試通過數不等於專題 100%。

## 8. 證據檔案 / Evidence files

都在 [release/native_v4/](../release/native_v4/)（`NATIVE_CANDIDATE.json` 除外）；完整性流程（manifest、SHA256SUMS）見 [DEVELOPMENT](DEVELOPMENT.md)。

| 檔案 | 內容 |
|---|---|
| `math_acceptance.json`、`benchmark_cpu.json` | 4.1；4.2–4.3（含全部原始 latency） |
| `network_acceptance.json`、`clean_network_acceptance.json` | 4.1；乾淨副本重跑也是 29 checks，max logit error 5.37195e-7 |
| `sanitizer_acceptance.json`、`sanitizer_run.log`、`sanitizer_compile.log` | `NATIVE_V4_S29_FINAL_ASAN_UBSAN_PASS`；`-Wall -Wextra -Werror -fsanitize=address,undefined`；compile log 空檔＝無警告 |
| `openfhe_acceptance.json`、`openfhe_workers_{1,3,4}.{log,tsv}` | `OPENFHE_STANDARD_CPU_1_3_4_WORKER_ACCEPTANCE_PASS`(4.4) |
| `driver_model_acceptance.json` | `CANDIDATE_LIFECYCLE_HOST_MODEL_PASS`(3.5) |
| `connected_local_checks.log` | `NATIVE_V4_S29_FINAL_CONNECTED_LOCAL_PASS`：build、CTest、數學、mTLS、benchmark 串接 |
| `clean_operator_report.json`、`clean_operator_batch.log`、`clean_root_ctest.log` | 3.2 的預演；V5 前 5 項 CTest |
| `clean_original_guards.log` | `PORTABLE_TOOL_NEGATIVE_GATES_PASS cases=12`：不做 DMA、不載 module、只用合成 ELF |
| `clean_x86_module_rejected.log`、`FAILURE_AND_CORRECTIONS.json` | 3.4 的預期錯誤；4.5 |
