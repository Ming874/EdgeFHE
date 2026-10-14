# 系統架構、介面與架構決策 / Architecture, interfaces and decisions

KV260（`xck26-sfvc784-2LV-c`）上，ARM（PS）做 CKKS 編碼、取樣、組裝與解碼，FPGA（PL）只做精確整數多項式運算，雲端只拿到密文。本頁記錄架構、固定介面與設計決策（到 2026-10-14）；操作見 [BOARD_GUIDE](BOARD_GUIDE.md)，建置見 [DEVELOPMENT](DEVELOPMENT.md)，量測見 [COMPARISON](COMPARISON.md)，證據見 [VALIDATION](VALIDATION.md)，安全見 [SECURITY](../SECURITY.md)。

標記：【實板】KV260 實板執行並有紀錄；【電腦】只在電腦上（x86 WSL、模擬裝置、127.0.0.1、HLS／Vivado 工具報告）；【未驗】尚未驗證。

## 目錄 / Contents

1. [系統總覽與資料流](#1-系統總覽與資料流--system-overview-and-data-flow)
2. [版本層](#2-版本層--version-layers)
3. [硬體](#3-硬體--hardware)
4. [數學契約](#4-數學契約--mathematical-contract)
5. [固定介面](#5-固定介面--fixed-interfaces)
6. [V5 軟體架構](#6-v5-軟體架構--v5-software-architecture)
7. [架構決策與最佳化順序](#7-架構決策與最佳化順序--architecture-decisions-and-optimization-order)
8. [名詞速查](#8-名詞速查--glossary)

## 1. 系統總覽與資料流 / System overview and data flow

| 位置 | 負責 | 不做 |
|---|---|---|
| PS（4 × Cortex-A53，Ubuntu） | 標準化、canonical 編碼／解碼（浮點 FFT）、OS 亂數取樣、RNS 對應、組裝 c0／c1、CRT、no-wrap 與誤差界、sigmoid 與分類、Linux DMA 驅動 | — |
| PL（名目 100 MHz） | 負循環 NTT／INTT、多項式乘法、加減（[§5.1](#51-pl-運算碼與-axi-stream--pl-opcodes-and-axi-stream)） | SHAKE／PL 亂數、編碼與浮點解碼、bootstrapping、整段加密命令 |
| 雲端（PC） | 公開權重的密文仿射 Σ wᵢ·cᵢ + b | 私鑰、sigmoid（其餘限制見 §4.4） |

| 步驟 | V5 名稱 | 做什麼 | PL 乘法 |
|---|---|---|---:|
| 1 | `standardize` | 依訓練時的平均／標準差標準化並截斷 | 0 |
| 2 | `encode` | 每特徵 1～2048 筆放進槽位、乘 scale 取整成 m；算 no-wrap 上界 | 0 |
| 3 | `sample` | OS 亂數產生該批 u、e0、e1 | 0 |
| 4–5 | `encrypt` | PL 算 b·u、a·u；PS 組 c0 = b·u + e0 + m、c1 = a·u + e1 | 8 |
| 6–8 | `cloud` | 只送密文，雲端算 Σ wᵢ·cᵢ + b（第 7 步）並傳回 | 0 |
| 9 | `decrypt` | PL 算 c1′·s；PS 加 c0′、CRT 成有號整數 | 1 |
| 10 | `decode` | 檢查 no-wrap、除以 scale²、反 FFT、sigmoid；誤差區間全在 0 同側才判 0／1，否則不確定 | 0 |

步驟編號沿用簡報架構圖；每批（1～2048 筆）9 次乘法／DMA，keygen（b = −a·s + e）每個 session 一次、公鑰重用，u、e0、e1 每批重抽。私鑰 s 只在板上（keygen 與解密時以運算元經 DMA 進 PL；V3 driver 每次工作後清空 buffer）。傳輸：V3 是 server-pinned TLS＋Bearer token（不是 mTLS）；V4／V5 兩機是 mTLS，只在 127.0.0.1 演練【電腦】（檢查項目見 [NATIVE_V4](NATIVE_V4.md)）；V5 單板量測的雲端在板上同一程式內以 CPU 計算（V4 `Engine::affine`），沒有網路。

## 2. 版本層 / Version layers

V5 原樣重用 V4 數學（不複製、不修改、結果逐位元相同），只改乘法由誰算、怎麼排程與量測；改進架構就改 V5，V3／V4 的驗收保持有效（資料夾標記見 [README](../README.md)，證據見 [VALIDATION](VALIDATION.md)）。

| 層 | 擁有與修改規則 | 驗證 |
|---|---|---|
| V3 凍結基線 | HLS／IP／BD、韌體與單一 kernel 的 `.ko`、`software/driver/`、`software/ckks/`、C++ 參考與板測程式、`validation/vectors/`；不可改（101 個 byte-exact 副本由 [PROVENANCE](../release/PROVENANCE.json) 鎖定） | 【實板】正常路徑；G07／G08／G12 部分完成 |
| V4 原生候選 | `software/native_ckks/`（`core.cpp` 全部數學、C ABI、`EFCKKS4`＋mTLS、無 fixtures 服務）、`validation/openfhe_client/`、`software/driver_candidate_v4/`（不可部署）；雜湊被 [NATIVE_CANDIDATE](../release/NATIVE_CANDIDATE.json) 與收據釘住，不可直接改 | 【電腦】；【實板】只有間接（`core.cpp` 經 V5 上板，與 PL op2、C ABI 一致）；V4 自己的板端流程、兩機、安全 profile【未驗】 |
| V5 後端與對照平台 | `software/native_v5/`、`tools/run_comparison.py`、`tools/compare_results.py`；可改，改完重跑 `tools/run_v5_checks.py` 更新[收據](../release/native_v5/local_acceptance.json) | 【電腦】主機檢查；【實板】板上編譯與測試、一致性兩次、階段 2；實體兩機、真 DMA 管線 soak／故障【未驗】 |

V3 的 PL 不是歷史 HP2／SHAKE 版 bitstream（DMA 走 HP0）；V3 的公開 fixtures 只證明「PC 用密文算 affine」，不證明服務不知道私密輸入（[SECURITY](../SECURITY.md)）。接受狀態在 [VERSION.json](../release/VERSION.json)（`formal_release_accepted=false`）與 [ACCEPTANCE_MATRIX](../evidence/ACCEPTANCE_MATRIX.json)；歷史 JSON 的路徑是當時身分（[INDEX](../evidence/INDEX.json) 對照），離線檢查不執行歷史 command 欄位。

## 3. 硬體 / Hardware

三個分量引擎各管一個 q、各有 4 條蝶形通道並行（`hardware/hls/rns3_n4096/hls_source/src/`）。

- **結構**：top `rns3_ntt_parallel_accel_n4096()`（`rns_ntt_parallel_rns3.cpp`）有兩個 AXIS 埠與 AXI-Lite `control`（`operation`）；DATAFLOW `unpack_rns3`（128-bit beat → 三個 32-bit limb）→ 三個固定參數的 `process_limb<0..2>` → `pack_rns3`（三個餘數＋0）。`rns_ntt_parallel_core.cpp` 做 twist／load、twiddle 遞推與 cache、banked stages、反轉換正規化與輸出、ALU。
- **II=1**：`RNS_LANES=4`；`bank = index & 3`，cyclic four-bank gather／scatter 每批 4 個蝶形、8 讀 8 寫、每 bank 最多 2 次存取，來源與目的是分開的 dual-port BRAM banks，配合固定 stage 排程 → 指定 butterfly loop II=1（每 stage 512 批、515 cycles）。只有這個 loop：add2 與 B−A 是 II=2、add3 是 II=3（每係數讀 2／3 拍），逐點乘＋bit reversal 每週期 1 係數（避開寫入埠衝突），也不是整個多項式每週期完成。
- **無除法器**：取餘只用乘法、取低 32 位元、位移與一次減法（§4.3）；53 個 Verilog 檔 runtime 除法／取餘 0 個。Twiddle 是固定常數＋遞推／cache。
- **數字**（工具報告【電腦】；此 bitstream 已在 V3 驗收、2026-10-14 V5 再用【實板】）：名目 100 MHz（driver probe 要求 `ap_clk` 在 99.9–100 MHz）；routed 資源、時序與 fixed-bank HLS A/B 見 [VALIDATION §2](VALIDATION.md#2-v3-關卡與數字--v3-gates-and-numbers)；RTL cosim smoke 的乘法 45,986 cycles（約 0.46 ms；其他 op 見 [RESULTS](../release/RESULTS.json)）。

| Block design（`hardware/vivado/recreate_bd.tcl`，凍結 BD v3） | 內容 |
|---|---|
| PS HPM0（128-bit）→ interconnect → AXI-Lite | AXI DMA `0xa0000000`、HLS `0xa0010000`（各 64 KiB）；HLS `AP_CTRL` 在 0x00（bit0 start、bit2 idle），operation 在 0x10 |
| DMA MM2S／S2MM → interconnect（2→1）→ PS HP0（128-bit） | 對應 DDR_LOW `[0, 0x80000000)`（另有 QSPI 區段）；DDR_HIGH、LPS OCM 排除 |
| DMA ↔ HLS | 128-bit AXIS：MM2S → `in_stream`，`out_stream` → S2MM |
| 中斷 | MM2S（TX）`pl_ps_irq0`＝DT SPI 89 → GIC 121；S2MM（RX）`pl_ps_irq1`＝SPI 104 → GIC 136；Linux IRQ 編號不是 GIC 編號，也不固定 |

AXI DMA 7.1：simple mode（無 scatter-gather）、32-bit 位址、128-bit、length 26 bits；100 MHz、width conversion、reset 與位址契約以 BD 與韌體 DTS 為準，`build.tcl` 只管路徑、不載入板子（[DEVELOPMENT](DEVELOPMENT.md)）。韌體 `firmware/ii1_n4096/` 的 proxy 節點 compatible 為 `ncue,edgefhe-rns3-n4096-ii1-dma-proxy`、aperture 末端 `0x80000000`；執行期契約的失敗規則見 §5.2，v2／v3 的 mask 說明見 DEVELOPMENT §3.4。

限制：只有 N=4096、三個固定 q、4 lanes（N=8192 未驗）；op2 是黑盒，每次乘法整包送入並在 PL 重新轉換（§4.5），要省需新 HLS 運算＝新 bitstream＝新板測；DSP 60.34%、BRAM 43.06%、WNS 只有 4 ps，加 lanes 前要重估 DSP／bank／twiddle／route，不能只看 LUT；PL 對非法 packet 的偵測與 driver 的 canonical／長度拒絕是不同層次，不能靠 AXIS sidebands 宣稱任意 malformed stream 可恢復；跨 PL 實體 reset（G07）待驗。

## 4. 數學契約 / Mathematical contract

整數結果要逐係數完全相等（沒有浮點容許誤差），只有解碼成實數時有誤差且上界事先算好。本節是凍結 V3，V4 公式見 [software/native_ckks](../software/native_ckks/README.md) 第 2–3 節。

### 4.1 參數 / Parameters

$R_Q=\mathbb Z_Q[X]/(X^{4096}+1)$、$Q=q_0q_1q_2$；負循環 $X^N=-1$（乘積跨過第 N 項要變號）；三個質數各自運算，再 CRT 重建。

| 參數 | V3 | V4／V5 |
|---|---|---|
| N／槽位 | 4096／2048（只用 slot0，其餘 2047 個為 0） | 每批 1～2048 筆，一槽一筆 |
| q；Q | (998244353, 1004535809, 469762049)；471064322751194440790966273（log₂Q = 88.606054537） | 同 |
| ψ | (350007156, 2062645, 62025685)；$\psi^N=-1$、$\omega=\psi^2$ | 同 |
| scale：輸入／仿射輸出 | 2²⁴／2⁴⁸ | 2²⁹／2⁵⁸ |
| 稀疏取樣 | s、u 各 64 個 ±1；e、e0、e1 各 16 個 | 同 |
| 亂數 | `secrets.SystemRandom()`；固定種子只供公開 host QA | `getrandom()`（細節見 NATIVE_V4） |

PL 不變；V3 沒有 profile ID（封包 schema 見 §5.3），V4 與 V3 不 wire-compatible。功能性稀疏取樣器沒有安全等級，N／Q 推不出 153-bit 或 128-bit。OpenFHE 對照的 moduli（1073692673、536813569、536903681）不同，不能直接接 PL；正式接上要對齊與重做的項目見 [NATIVE_V4](NATIVE_V4.md) §5。

### 4.2 編碼與解碼 / Encoding

`experiment.encode(value,scale)` 明確呼叫 `CKKSEncoder(N,scale)`（不要依類別預設 degree 推 N）。$\zeta=e^{i\pi/N}$，槽位 k 對應 $\zeta^{2k+1}$；`encode()` 補共軛對稱 $v_{N-1-k}=\overline{v_k}$，`fft()` 用負指數、$1/N$ 與 $\zeta^{-j}$ twist，再逐係數四捨五入：

$$m_j=\operatorname{round}\Big(\Delta\operatorname{Re}\Big[\tfrac1N\sum_{k=0}^{N-1}v_k e^{-2\pi i k j/N}\zeta^{-j}\Big]\Big).$$

`decode()` 把 $m_j/\text{scale}$ 乘 $\zeta^j$、做 inverse FFT 再乘 N、取前 N/2 個槽位；`direct_slot()` 用直接的複數求值交叉檢查 slots 0、1、2047。這個 adjacent-odd-root 排序不是 OpenFHE 的 encoding／ciphertext ABI。V4／V5 改用 round-half-to-even 並檢查輸入（見 native_ckks README 第 2 節）。

### 4.3 Montgomery 與 NTT / Montgomery and NTT

$R=2^{32}$、$q'=-q^{-1}\bmod R$；`montgomery_mul`：$t=ab$、$m=(t\bmod R)\,q'\bmod R$、$u=(t+mq)/R$，若 $u\ge q$ 再減 q。`mod R` 取低 32 bits、除以 R 用位移，不合成 `%`／`/`；canonical 輸入且 $q<2^{30}$ 時 $t+(R-1)q<2^{64}$、$u<2q$，一次修正就夠。`multiply_ordinary()` 用兩次 reduction 與 $R^2\bmod q$，讓外部乘積是普通域 $ab\bmod q$。資料係數一律普通域，power／twiddle cache 是 Montgomery 形式，兩者相乘仍得普通域；不能把所有 buffer 都當 Montgomery 係數。PS 的 oracle 可以用除法，但不能當 PL source（V4／V5 的 Shoup／Barrett 屬 PS，不放進 HLS project）。

`load_stream()` 先 twist $a_j\psi^j$，`cyclic_ntt()` 做 bit reversal 與 12 個 stages，`butterfly_stage()` 算 $(u+wv,\,u-wv)\bmod q$；反轉換用 $\omega^{-1}$、$N^{-1}$ 與 $\psi^{-j}$ untwist，回到 natural order：$A_k=\sum_j a_j\psi^j\omega^{jk}$，$a\cdot b=\mathrm{INTT}(\mathrm{NTT}(a)\odot\mathrm{NTT}(b))$。

### 4.4 加解密、CRT、仿射與誤差 / Encryption, CRT, affine and error

$$\begin{aligned}
&pk=(b,a),\ b=-as+e;\qquad c_0=bu+e_0+m,\ \ c_1=au+e_1;\qquad d=c_0+c_1s=m+eu+e_0+e_1s\pmod Q\\
&d_j=\Big(\textstyle\sum_{\ell=0}^{2}r_{\ell,j}\,Q_\ell\,(Q_\ell^{-1}\bmod q_\ell)\Big)\bmod Q,\qquad Q_\ell=Q/q_\ell\\
&W_i=\mathrm{Encode}(w_i,\Delta),\ B=\mathrm{Encode}(b,\Delta^2);\qquad c'_0=B+\textstyle\sum_i c_{0,i}W_i,\ \ c'_1=\sum_i c_{1,i}W_i\\
&U_{aff}=\textstyle\sum_i\|m_i+noise_i\|_1\,\|W_i\|_1+\|B\|_\infty,\qquad 2U_{aff}<Q\\
&E_{aff}=\textstyle\sum_i\big(|w_i|E_{x_i}+|x_i|E_w+E_{x_i}E_w\big)+E_b+g,\quad E_w=\tfrac{N}{2\Delta}+g,\quad E_b=\tfrac{N}{2\Delta^2}+g,\quad g=10^{-8}
\end{aligned}$$

- CRT 結果 $d_j>\lfloor Q/2\rfloor$ 時減 Q（有號整數）。**No-wrap**：先驗 $2U<Q$（$U\ge\|m\|_\infty+\|noise\|_\infty$）才可把 centered 結果和整數 m + noise 精確比對，不能用小數門檻取代；稀疏 noise 的 L1 上界 $16\cdot64+16+16\cdot64=2064$。
- 雲端是「密文 × 明文」，不需 relinearization；只有這一層 affine，沒有一般 rescale、rotation、多層密文乘法或 bootstrapping。以 $\Delta^2$ 解碼後 slot0 近似 $\sum_i x_iw_i+b$。PC response 由獨立 C++ 結果核對；`decode_check()` 比對全部 2048 slots（feature bound 用實際 noise L1）；量化上界 $N/(2\Delta)$；g 是經 direct evaluation 交叉檢查的浮點 allowance，不是 IEEE 誤差的嚴格證明；V3 六例觀察誤差與保守界限見 [VALIDATION](VALIDATION.md) §2.2（不能宣稱全域 5×10⁻⁴）。
- `sigmoid()` 用穩定分支避免 overflow；$|\sigma'|\le1/4$，機率誤差比對 $E_{aff}/4+g$；`classification_interval()` 只有整個區間在 0 同側才給 1／0，跨 0 為 `None`；門檻是機率 0.5，不是歷史示例的 84%。
- V4／V5 沿用同樣的關卡：signed 128-bit CRT、每批先驗 no-wrap、同權重時 $W_i$ 是常數多項式（仿射 O(N)、不需 rotation 或額外 NTT）、誤差界多一個雜訊項且超出模型 `max_logit_error` 就拒絕；細節見 native_ckks README 第 2 節。

### 4.5 乘法次數與 V3 程式對照 / Multiplication counts and V3 map

每批 9 次乘法（加密 8＋解密 1，另有每個 session 一次 keygen；V3 時只是工作量分析，V4 實作【電腦】、V5 量測【實板】）。V3 六例 QA 每例 14 次、共 84 次（含 oracle 與診斷，不能當 client latency 或 Amdahl 的 f），`v5_parity --cases 64` 為 77 次，組成見 [VALIDATION](VALIDATION.md)。

每分量、每次四特徵加密：CPU 路徑讓共用運算元（a、b、各 u）只轉換一次，6 正＋8 反＝14 次；FPGA op2 每乘積 2 正＋1 反，共 24 次（組 B 的結構劣勢）。

| V3 檔案 | 函式與責任 |
|---|---|
| `software/ckks/ckks_canonical.py` | `fft`、`CKKSEncoder.encode/decode`：embedding、量化、decode |
| `profile.py` | `material`、`sparse`、`to_rns`、`validate`、`add`、`neg`、`centered`（CRT）；oracle 與檢查 `integer_sparse_product`（schoolbook，無 NTT、無 mod q）、`noise_integer`、`direct_slot`、`conservative_affine_bound`、`sigmoid`、`classification_interval`；不是 OpenFHE serialization／context 互通 |
| `experiment.py` | `encode`、`sparse_rns_product`（逐 limb 檢查 keygen 與加密乘積）、`decode_check`、`run_case`：encode／material → keygen → 每特徵兩乘法組 c0／c1 → 診斷解密 → packet／evaluator → PC response 精確 oracle → affine 解密／decode |
| `native.py`、`backend.cpp` | ctypes（`fd=None` 強制 CPU）、`Backend.cpu/multiply`（C++ CPU 或已 gate 的 hardware）、`PhaseTimes`；C ABI `edgefhe_cpu`（CPU op0–2）、`edgefhe_hardware_multiply`（Linux op2：pack／ioctl／unpack，每次精確比對；參數見 DEVELOPMENT） |
| `software/reference/paired_public_vectors.cpp` | `Plan.cyclic`、`Plan.transform`、`cpu`：精確 NTT／INTT／multiply oracle 與 CPU-check（歷史「CPU3」＝綁 CPU 3 的單核）；不是完整 OpenFHE client |
| PC 端 `cloud.py`、`ntt_ref.py`、`packets.py`、`transport.py`、`pc_server.py` | `evaluate`、`ntt_negacyclic_mul`：密文 affine，沒有私鑰 API；封包（§5.3）；pinned TLS＋token 的公開六例 QA 服務 |

## 5. 固定介面 / Fixed interfaces

格式全部凍結，改任何一項都要新的板測驗收。

### 5.1 PL 運算碼與 AXI-Stream / PL opcodes and AXI-Stream

| op | 結果 | TX beats | TX 排列 |
|---|---|---:|---|
| 0 | 負循環 forward NTT(A) | 4,096 | A |
| 1 | 負循環 INTT(A) | 4,096 | A |
| 2 | A·B mod (X^N+1, q) | 8,192 | 整個 A，再整個 B |
| 3 | A+B mod q | 8,192 | A_i、B_i 交錯 |
| 4 | A+B+C mod q | 12,288 | A_i、B_i、C_i 交錯 |
| 5 | B−A mod q | 8,192 | A_i、B_i 交錯 |

- RX 一律 4,096 beats。每 beat 128 bits，little-endian `uint32[4] = [limb0, limb1, limb2, 0]`；每個多項式 64 KiB，TX 因此是 64／128／192 KiB。
- 值必須 canonical（每個餘數 0 ≤ v < q）；外部一律普通域、natural order，內部 Montgomery 與 bit reversal 不暴露。輸出 padding 必須是 0；輸出最後一拍 `TLAST=1`，`TKEEP`／`TSTRB` = `0xffff`。
- op0–op5 都有 V3 實板證據（25 組公開案例，[VALIDATION](VALIDATION.md) §2）【實板】；V3 CKKS 與 V5 只用 op2。

### 5.2 DMA ABI 與 driver 行為 / Linux DMA ABI (ioctl)

`_IOWR('E', 0x04, Request)` = `0xc0304504`、`sizeof` 48；driver `software/driver/edgefhe_rns3_n4096_dma_proxy.c` 與 V5 `backend/fpga/dma_abi.hpp` 以編譯期檢查鎖住大小與 offset。

```c
struct Request {          // offset
  uint32_t operation;     //  0 op 0–5
  uint32_t reserved0;     //  4 必須 0
  uint32_t input_words;   //  8 128-bit beats（不是 u32 個數），須符合 op
  uint32_t output_words;  // 12 必須 4096
  uint64_t input_ptr;     // 16 使用者空間位址；大量資料不放在描述裡
  uint64_t output_ptr;    // 24
  uint32_t timeout_ms;    // 32 0 = 5000 ms；上限 30000
  uint32_t elapsed_us;    // 36 回填：DMA＋PL 窗口
  int32_t  status;        // 40 回填：0 或 −errno
  uint32_t reserved;      // 44 必須 0
};
```

- 一次 ioctl：取 mutex（一次一筆，其他執行緒排隊）→ 檢查欄位與指標 → `copy_from_user` 到 coherent TX buffer，逐 beat 檢查 canonical 與 padding → 清 RX、確認 HLS idle、寫 operation、準備 RX／TX 描述 → **`dma_wmb()` 起算、啟動 DMA 與 `ap_start`、等 RX 再等 TX、`dma_rmb()` 止算（只有這段計入 `elapsed_us`）** → `copy_to_user` → 兩個 buffer 清 0、回寫描述。DMAEngine 擁有 AXI DMA；TX 最大 196,608 B、RX 65,536 B。
- 錯誤：不合法輸入 `-EINVAL`、HLS 不 idle `-EBUSY`（都沒送 DMA）；送出 DMA 後逾時、DMA 錯誤或輸出複製失敗 → 終止兩個 channel、標 poisoned，之後一律 `-EIO`，直到受控的完整 PL reset 與重載（DMA reset 無法 reset HLS pipeline）；連 DMA 成功後的輸出複製失敗也會 poison（修正只在不可部署的 `driver_candidate_v4` 草稿）。`EFAULT` 可能在運算前（空指標、`copy_from_user`）或運算後（輸出或描述回寫，運算可能已完成），不能一律當「沒執行」而重試。
- 正常 open／close、錯誤輸入拒絕與多執行緒排隊有板測【實板】；fatal／reset／remove 規則未驗收，遇故障就停、不自動重試（細節見 [SECURITY](../SECURITY.md)，V5 的 DMA 政策見 [BOARD_GUIDE](BOARD_GUIDE.md)）。
- 記憶體規則（32-bit mask、16-byte 對齊、buffer 末端 ≤ `0x80000000` 否則 probe 失敗、copy-in／out、coherent＋barriers、CMA）見 BOARD_GUIDE 關卡 D；coherent 不需通用 cache flush，但不取消記憶體排序需求，改 streaming mapping 才要 map／sync 與 ownership handoff；`dma_alloc_coherent` 不能單憑名稱證明來自 CMA。裝置 `/dev/edgefhe_rns3_n4096_dma` 只有 root 能開（操作員交付 fd）；MMIO 與中斷見 §3。

### 5.3 封包格式 / Packet formats

- DMA：係數優先，每係數一個 beat，每多項式 64 KiB。
- `EFII1C1`（V3，pinned TLS＋token）：`EFII1C1\0`（8 bytes）＋ u32 LE 標頭長度 ＋ JSON 標頭（`schema` `edgefhe.ii1.ckks.affine`、`version` 1、`kind`、`case_id` 0–5、`degree`、`primes`、`scale`、`feature_count`、`polynomial_count`、`payload_sha256`；response 加 `request_sha256`）＋ payload（分量優先 `[poly][3][4096]` u32 LE）＋ 整包 SHA-256；上限 512 KiB。
- `EFCKKS4`（V4；V5 兩機沿用）：同框架與上限，標頭含 `request_id`、`model_id`、`profile_id`、`records` 等（`software/native_ckks/protocol.py`）。
- 每多項式 48 KiB、每份密文 96 KiB；request 8 個多項式＝384 KiB，response 2 個＝96 KiB，另加標頭、digest、TLS；兩者都拒絕未知或私密欄位。封包分量優先、DMA 係數優先，不能省略轉換（V5 每次呼叫都轉置）。

### 5.4 程式關係 / Program relationships

V3 的呼叫鏈見 §4.5（`experiment.py` → `native.py` → `backend.cpp` → ioctl；網路 `packets.py` → `transport.py` → `pc_server.py` → `cloud.py`）。V4 見 [software/native_ckks](../software/native_ckks/README.md) 第 1 節：數學全在 C++（`core.cpp` 的 `Engine`，經 `c_api.cpp` 的 `ef_*` ABI），CLI 與網路是 Python。V5 見 [COMPARISON](COMPARISON.md)；兩機 `python/edge_client.py` 用 `libedgefhe_v5_capi.so`（與 V4 相同的 `ef_*` ABI），原樣沿用 V4 的 `protocol.py`／`transport.py` 與 `software/native_ckks/pc_server.py`。

## 6. V5 軟體架構 / V5 software architecture

V5 把「誰算多項式乘法」做成可替換的後端（組 A `CpuBackend`、組 B `FpgaBackend`），其餘程式兩組共用，所以時間可直接比、密文必須逐位元相同（模組見 [software/native_v5](../software/native_v5/README.md)，設定與公平規則見 [COMPARISON](COMPARISON.md)）。

- **接縫** `PolyBackend::multiply_batch(jobs)`：每個 job 算 out = a × b（canonical RNS、係數域、natural order）；輸出不得與輸入或彼此重疊；每個物件一次一批；失敗丟例外，該批輸出未定義。keygen 1 個乘積、encrypt 把 8 個放同一批、decrypt 1 個；組 B 只送 op2。
- **一次一筆 DMA**：管線 `run_stages`（prepare 1–3 → encrypt 4–5 → cloud 6–8 → decrypt 9 → finish 10）中 encrypt 與 decrypt 共用後端，`EdgeSession` 的鎖加 driver mutex 保證最多一筆 DMA 在途；雙緩衝與管線只重疊 CPU 工作。
- **H 的範圍**：這是 §7.3 P1 在邊緣端的實作；網路段尚未納入（兩機 `edge_client.py` 逐批串行）。

## 7. 架構決策與最佳化順序 / Architecture decisions and optimization order

### 7.1 決策 / Decision

**Keep the accepted II1/V3 implementation as the technical baseline, and adopt the corrected team plan as the delivery roadmap.** 依據是來源、需求邊界、實測與下一步能否驗收，不是哪份文件寫得完整。已實作的 PS 編碼／取樣、PL 模算術、II1 banks 與排除 bootstrapping 較符合目前要求；組長的模組分工、介面凍結、H／T／M 與證據關卡較適合管理後續研究；兩者合併，比照舊架構圖改 PL SHAKE 或追求更多 lanes 更有依據。

- 目前是功能研究 MVP，不是安全產品或 100% 工程 release；組長的 77% 是規劃估分，沒有固定權重不能換成另一個精確百分比。不要為了符合舊圖把 PRNG 搬回 PL，也不要把 mTLS、多板等新 scope 與原 MVP 混成一個完成度。組長先確認 C++ 與模型分工是否強制，再把私密展示、公平效能、reset／fault、clean deployment 各自指派 owner、reviewer 與通過條件。
- 做出這個決策的那一輪只補共用入口與文件，沒改已接受的算術、driver、bitstream，也沒新增研究或板端 PASS；之後 V4、V5 也沒改這三者。

### 7.2 需求、規劃與提案對照 / Requirements, team plan and proposal

來源：使用者技術要求（工程符合狀態，不是法規或密碼認證）；組長規劃書（審查 `TEAM_PLAN_REVIEW_zh-TW.md`，2026-10-06，SHA-256 `9e4dc54c6e75bf5f8667c127c2eb688e7e76e41d98ee8b8e27e498c972b70e90`；完整審查留在開發工作區，此處是可重現的摘要，不執行歷史文件的命令）；原始提案 `Proposal_01.pdf`（16 頁，PDF 頁序，SHA-256 `e2c25c93065868ca18559ab41822b45dc389aca0bbc54390f47114669bd84afa`，不在倉庫內）。

| 主題（來源） | 決策與界線 | 2026-10-14 |
|---|---|---|
| PL 範圍（提案 p1、p4、p5、p15；組長圖的 PL SHAKE 與完整加密；使用者） | 維持 §1 分工、修正舊圖，不把 §1 列為 PL 不做的功能加回 PL，不沿用歷史 SHAKE PASS；PS 取樣沒有資源／速度 A/B，不主張較安全或較快；edge 不做 bootstrapping，一層 affine 也不需要，不代表 cloud 有任意深度 FHE | 不變 |
| C／C++ client（提案 p1、p4；使用者） | V3：C driver（符合）、C++17 NTT／backend，encoder 與 KeyGen／Encrypt／Decrypt 在 Python；若 C++ encoder 強制，移植後逐係數、no-wrap、全槽位重驗 | V4 完成【電腦】；V5 板上與 PL 一致【實板】 |
| HLS 與並行（提案 p4、p6、p16；使用者：N4096／8192、BU II=1；組長：舊 91 MHz、LUT 44%、加 lanes） | V3 是 100 MHz、LUT 25.04%；保留四 lanes，加硬體前另量資源與 routed timing；不代表 N8192、任意 q、整個多項式每 cycle 或所有 ALU II=1；lane 數不是單筆加密的倍數承諾；fixed-bank 只能主張 HLS 迴圈 cycles −74.93%、worst latency −32.37%，不是 CKKS／E2E 快 4 倍 | 不變；N8192【未驗】 |
| 模算術（提案 p16；使用者：PL 禁通用除法） | Montgomery 已達成，不需兩種 reduction；CPU 可用 `%`／`/`；換版 RTL 重驗；沒有 Montgomery 對 Barrett 的 A/B | 不變 |
| Twiddle（提案 p5；使用者：ROM／on-the-fly） | 遞推／cache 已做；同 N／q／lanes 的 ROM 對照、BRAM 節省與 latency 取捨未證明（fixed-bank A/B 的 BRAM18K 都是 126） | 未做（P3） |
| 介面整合（提案 p4–p6；使用者：AXIS、AXI-Lite、TLAST／TKEEP、deadlock-free） | normal path、CSim／RTL sidebands 與 6 小時已接受；malformed、fatal、reset、late callback、remove 的恢復非全域保證（G07／G08） | V5 正常路徑上板無 DMA 錯誤【實板】；缺口不變 |
| 記憶體與交接（使用者：CMA、cache） | coherent API＋barriers、32-bit mask＋aperture guard，另有 CMake、相對路徑、manifest、祕密排除、gate 手冊；須記錄 CMA inventory 與實際 allocator 來源，依 API 所有權不插 generic flush；不能替代 fault／reset 接受或普遍 kernel 相容 | CMA 與 buffer 位址已記錄【實板】；allocator 來源未記錄 |
| 正確性（組長：缺 dense；使用者：標準 oracle；提案 p5：OpenFHE 或 TenSEAL） | 保留 V3 分層驗證；補 ≥1000 組不同 dense A/B，不能用 6 小時重複代替；用 OpenFHE 即可、不需 TenSEAL；不是完整 OpenFHE context／key／ciphertext ABI | V4 1,000 組、12,288,000 係數零誤差＋OpenFHE CPU 對照【電腦】；V5 一致性【實板】 |
| 效能（提案 p4 MVP；組長：舊 1.5×、ARM 3 核、98% overhead、2× 目標；使用者） | V3 只有單核純量 ARM（核心 3）對完整 FPGA 呼叫的有限證據（multiply pooled P50 1.311×，NTT／INTT 較慢，[VALIDATION](VALIDATION.md) §2.2）；舊量測不作新結論，先量部署各段與最佳化 ARM；MVP 效能承諾只部分達成；98% overhead、端到端 2× 不算已達成 | ARM＋FPGA 比最佳化 4 核 ARM 慢（§7.4）【實板】 |
| 兩機與私密資料（組長：mTLS、153-bit、雲端不知輸入；提案 p5：互動輸入 → edge 加密 → cloud → edge 解密、敏感資料與零洩漏） | V3 是公開六例兩機 affine、pinned TLS＋token、兩端有 fixtures、case_id 可對應輸入；真實隔離、標準安全 profile、mTLS 另立 candidate；任意互動輸入、live UI、完整部署資料流程未驗收；不能宣稱零洩漏，TLS 不代替 FHE 安全 profile 或結果完整性 | V4 mTLS、無 fixtures、任意輸入只在本機，V5 兩機只在 127.0.0.1【電腦】；實體隔離與安全 profile【未驗】 |
| 模型（提案 p5：簡單 LR） | cloud 密文 affine＋edge sigmoid 與分類區間，不是全密文 sigmoid；真實模型、標準化、model card 未完整驗收，scope 由教授確認 | V5 以 CDC 四特徵 LR `cdc-diabetes-lr4-v1` 跑完測試集【實板】；model card、scope 待確認 |
| 建置（提案 p5：cross-compilation） | Linux／WSL native CMake 與板端 matching-header module build；優先 native ARM；凍結 cross-toolchain／乾淨 image 一鍵部署未交付 | V5 板上原生編譯【實板】；一鍵部署未交付 |
| 管理（組長：五模組、介面、關卡、H／T／M；使用者：100% 交付） | 採團隊路線，每關有資料與 owner／reviewer；依接受矩陣關卡，不把文件或打包 PASS 升級成工程 release | H 已實作並上板量測；T、M 未開始；G07／G08／G12 仍 partial |

- 提案 p5 允許本機模擬 cloud，PC evaluator 是可接受的展示角色；缺口是私密輸入隔離與互動流程，不是沒租公有雲。明文先在 PC 輸入時 PC 也是受信任端；要主張「明文只在 KV260」須改成板端／感測器直接輸入並驗收。
- 原提案沒有指定 mTLS、153-bit、端到端 2×、1000 組不同 dense 輸入或 H／T／M 多板里程碑（那是後續團隊要求，不能倒寫成原提案未完成），也沒規定 sigmoid 必須在雲端密文域（先確認教授接受的分工）。

### 7.3 最佳化順序與關卡 / Optimization order and gates

**關卡 0：G07／G08／G12 收尾**（原始故障／reset 證據、usercopy exact outputs、完整 clean-build 與身分；不轉移舊 PASS）——仍是 partial【未驗】。

**P0 可信的部署基準**（關卡 1：cloud 沒有 private feature／key，payload／ready／logs 負向檢查，分布與安全估算的身分；關卡 2：≥1000 組不同 dense A/B 的獨立 oracle、full slots、相同 scope 的 ARM-only 與 FPGA timing）。服務端只留模型與公開 profile，移除 private inputs 與可反查 features 的 case_id；先定數學 profile、安全 scope、error 門檻，安全分布、error target、威脅模型另立 candidate。保留公開 QA；部署重用公鑰、oracle 與 diagnostic 移出 hot path，完整結果在獨立的離線／旁路路徑精確比對；8＋1 次乘法不是已量出的加速比。同一份資料／模型／profile 比較 ARM-only 與 ARM＋FPGA，量 encode、sample、keygen 攤提、pack、queue、DMA、unpack、network、cloud、decrypt、decode；保存 monotonic 原始 latency、affinity／頻率、warm-up、P50／P95／信賴區間，只對相同邊界取比值。
狀態：公鑰重用、oracle 移出、8＋1：V4【電腦】、V5【實板】；無私密輸入的服務與負向檢查只有 V4【電腦】，實體兩機與安全估算【未驗】；profile 與 `max_logit_error` 已定；dense oracle 見 §7.2；同範圍逐段量測由 V5 階段 2 完成【實板】（monotonic `steady_clock`，P50／P95／平均），缺 network 與逐輪原始數據、信賴區間。

**P1 批次與 H 單板管線**（關卡 3：正確的 batch ownership、回壓／timeout、吞吐與延遲、原始 traces 與新 soak）。先驗 feature-wise full-slot packing：四份密文最多 2,048 筆，核對尾批 padding、有效 records、record ID；可攤提 packet 與呼叫成本，但容量不是已量出的吞吐（slot0 fixture 也沒驗過）。再用 host-owned buffers 與 bounded queues 重疊「下一批 CPU 編碼／本批單一 DMA worker／上一批 network」，狀態 `FREE → ENCODING → QUEUED → DMA → NETWORK → DECRYPT → DONE → FREE`；不同 batch 可在不同 stage，但 coherent TX／RX 由單一在途 job 獨占，**禁止**為 H 移除 mutex 或同時改寫 DMA buffers。比較串行、只加 CPU threads、真 pipeline；量 steady-state records/s、P50／P95、fill／drain、queue 水位、CPU／memory、cloud 利用率，核對每筆誤差與分類；吞吐升但單筆延遲變長要一起報告；新 pipeline 要重新 soak 與 fault／backpressure 測試；有這些數據才決定要不要 T。
狀態：滿槽位 V4【電腦】、V5 實板逐筆比對【實板】；H 由 V5 實作（§6），階段 2 量了串行、只加 CPU workers 與管線【實板】（§7.4）；回壓與故障注入只用模擬裝置測過。缺：真 DMA 的 soak、回壓、timeout 與原始 traces【未驗】，記憶體用量、含網路的雲端段、管線下單批延遲的報告。

**P2 T 雙板資料平行**（關卡 4：相同 profile 的雙板 strong／weak scaling、network／cloud 瓶頸拆解）。同 profile／source／模型／網路與 TLS，兩板處理不同 batches；strong scaling 固定總筆數 B，$S=T_1(B)/T_p(B)$、$E=S/p$；weak scaling 固定每板 b 筆，量總 p·b 筆與 records/s；coordinator 量 makespan，不拿未同步的兩板 clock 相減；實際 request 384 KiB／response 96 KiB，16 KiB／96 KiB microbenchmark 不能代表整條服務。cloud、Wi-Fi 或 serialization 先飽和就不會近 2×，1.8–2× 只是假設；把單筆的 RNS limbs 拆給兩板不優先（三 limbs 已在單板並行，還多通訊與同步）。狀態：未開始【未驗】。

**P3 依教授確認的延伸**（關卡 5：scope 事先確認、獨立 candidate、數學與端到端接受）。M：先用獨立 CPU processes 驗 joint-key、DKG、partial decryption、fusion，再評估多板；多方是不同信任主體，不必是多塊實體板；不同私鑰的普通 CKKS 密文不能直接相加；OpenFHE 官方 threshold 範例（openfhe-development 的 `src/pke/examples/threshold-fhe.cpp`）不是本倉庫 ABI 的 drop-in 替換。全密文 sigmoid：先定近似 domain、degree、model error 與乘法深度、rescale、noise budget，再另作模型驗收；「client affine＋edge sigmoid」是否達到題目範圍由教授確認。ROM 對 recurrence 需同 N／q／lanes／clock／I/O 邊界的公平實驗。Rotation、rescale、多層模型也不在目前範圍。狀態：都未開始【未驗】。

### 7.4 V4 審查與 2026-10-14 實板之後 / After the V4 review and the board session

V4 審查後的路線（[NATIVE_V4](NATIVE_V4.md) §5）：O(N) scalar affine 與 OpenFHE 對照改變了瓶頸判斷，不沿用診斷時間的 98% 估算；保留 PS 原生／PL 固定算術，先 ARM 逐段計時、再 H（單一 DMA worker 的有界管線），T／M 另定 scope。前兩項已由 V5 在 2026-10-14 完成；安全 profile 與實體兩機仍未完成。

實板結果（數字與解讀見 [COMPARISON](COMPARISON.md)，證據見 [VALIDATION](VALIDATION.md)）：PL op2 與 V4 數學逐位元一致（`V5_PARITY_PASS device=linux-dma`，786,432 係數＋完整 client 回合，77 次請求無錯誤，兩次）。每次 FPGA 乘法的 ioctl 約 4.7 ms（一致性工具順帶換算約 5.02 ms），driver 回報的 DMA＋PL 窗口只有約 0.52–0.55 ms，其餘在窗口外（步驟見 §5.2，尚未拆量），所以 PL 計算不是瓶頸；以每次乘法一個 op2 呼叫的介面，ARM＋FPGA 比最佳化 4 核 ARM 慢（單批延遲 0.65×、19 批吞吐 0.62×）；H 管線在 19 批時吞吐提高 1.7–2.1 倍，1 批沒有好處。

後續方向未決定，依老師建議（候選見 COMPARISON）；任何新 HLS 運算＝新 bitstream＝新板測驗收，改 driver＝新 driver candidate 與驗收（V3 driver 凍結，`driver_candidate_v4` 不可部署）。

## 8. 名詞速查 / Glossary

| 名詞 | 白話 |
|---|---|
| PS／PL | Zynq 晶片上的 ARM 處理器系統／FPGA 可程式邏輯 |
| RNS 分量、CRT | 把模 Q（約 89 位元）的係數拆成三個 30 位元內的餘數分開算；CRT 再合回 |
| Montgomery 乘法 | 不用除法器就能「乘完取 mod q」：只用乘法、取低位元、位移與一次減法 |
| no-wrap | 確認解密後的數不會超過 Q/2 而繞回變錯的整數檢查 |

槽位、批、NTT 等其他名詞見 [COMPARISON](COMPARISON.md)。
