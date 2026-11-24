# EdgeFHE 軟硬體切分模型 v0.1（以量測為本的初版）

產生方式：`python partition_model.py`（只用 Python 標準函式庫，結果可重現，約 1 分鐘）。程式唯讀 repo 內 2026-10-14 的 KV260 量測檔，重跑會覆寫本檔、`model_results.json`、`graph_best.mmd`、`graph_best.dot`。

**數字標記**：**MEASURED（M）**＝直接取自結果檔（附路徑）；**DERIVED（D）**＝由量測值以公式推得（附公式）；**ESTIMATED（E）**＝工程估計（寫明假設）；**ASSUMED（A）**＝情境輸入（未來的驅動或硬體，不是事實）。情境結果是模型**預測**，不是實測；只有第 4 節的「實測」欄是板上量測。

## 0. 摘要

- **校準**：模型照 V5「實際寫法」重建六種設定（cpu-w1–w4、fpga-serial、fpga-overlap），各步驟誤差最大 1.8%、邊緣延遲誤差最大 0.6%；19 批管線吞吐量誤差 cpu-w1 +0.7%、cpu-w3 -2.7%、cpu-w4 +0.0%、fpga-serial -0.5%、fpga-overlap -0.3%（cpu-w2 例外 -12.3%，兩個瓶頸幾乎相等時max-bound 會低估）；CPU 使用率的預測與實測差 ≤ 0.01。fpga-serial：加密 47.36 / 解密 6.45 / 邊緣 71.30 ms（實測 47.27 / 6.46 / 71.26）。
- **現況（S0）輸在驅動，不在 PL**：一次 op2 呼叫 4.68 ms 裡，validate 3.59 ms＋copy_out 0.45 ms 都是 ARM 在讀不經快取的記憶體；PL＋DMA 只有 0.55 ms。更糟的是這 4.1 ms 全是 **ARM 忙碌時間**，所以 S0 卸載也不會空出 ARM。模型在 D0 下的延遲最佳切分＝全部留在 ARM（任何 PL 指派的增益都 < 1%）。
- **加密步驟有 7.90 ms 是與後端無關的序列程式碼**（`residues()` 以 `int64 % q` 把 m、u、e0、e1 轉成 RNS ≈ 7.03 ms，每元素約 47 個週期，像是 64-bit 除法，加 12 次 add() ≈ 0.84 ms），占 cpu-w4 加密 16.17 ms 的 49%；keygen 的量測獨立印證每個元素約 35.4 ns。這段是 ARM 專屬、而且多半可用查表／Barrett 消掉（ESTIMATED，見 W1）。
- **現行程式結構下的 Amdahl 上限（只用 MEASURED/DERIVED）**：照 V5 現在的寫法（IMPL），就算 NTT/INTT/pointwise/add 全部免費、傳輸也免費，邊緣延遲只能從實測 37.40 降到 26.0 ms（最多 1.44×），吞吐量最多 1.72×（瓶頸：ARM cores (4)）。任何只換「乘積由誰算」的方案都跨不過這條線。
- **Amdahl 天花板**：就算所有可上 PL 的節點（NTT/INTT/pointwise/add）都變成 0，邊緣延遲也只能到 19.4 ms（DAG 排程；encode 4 核 8.3 ms＋decode 單執行緒 8.4 ms 占大半），相對同軟體的 all-ARM（28.1 ms）最多 1.45×；吞吐量最多 1.60×。光是把軟體改成資料流排程（不動 PL），模型就預測 cpu-w4 由 37.4 降到約 29.8 ms。
- **修好驅動後（D1/D2，ESTIMATED/ASSUMED）PL 才划算，而且主要贏在吞吐量**：S1–S4 的延遲最佳切分 24.9 / 23.8 / 23.6 / 23.1 ms（比同情境 all-ARM 少 16% / 15% / 16% / 18%），吞吐量 108k / 110k / 110k / 112k 筆/秒（1.58 / 1.49 / 1.49 / 1.52× all-ARM），因為 ARM 才是吞吐瓶頸，每卸載一個乘積可省約 4 ms ARM 核心時間、只付約 0.3–0.5 ms 打包／驅動。延遲最佳解是**混合**的（留 1 個特徵或部分轉換在 ARM 與 PL 並行），不是「全部丟 PL」。
- **ARM 變強，PL 的延遲價值很快消失**：NTT 快 3×（S5）時延遲增益只剩 1.8%（21.91 vs 22.30 ms），吞吐量仍多 13%。8 個乘積以 op2 形式卸載，在 D1 下 ARM 只要快 1.12× 就打平（延遲）；改用 Stone 建議的 K 介面可撐到 1.53×（D1）／1.77×（D2）。
- **Stone min-cut 提出比指定融合運算更好的介面**：只把 u_f 送進 PL（公鑰常駐、NTT 形式），回傳 b·u_f、a·u_f，加法留在 ARM（K 運算；每特徵進 1 出 2 個多項式，而不是進 4 出 2）。列舉法驗證：S7 22.34 ms vs S4 23.05 ms（延遲約好 3%，吞吐量相同；差距小，重點是方法能指出介面）。Stone 自己的解（4×K）makespan 是 22.84 ms，列舉法的 makespan 最佳解（[3xK 1xOA]; dec=O）是 22.34 ms：前者最小化總工作量，後者最小化完成時間。
- **下一步最值得量的不是 PL**：若 encode/decode 的成本真的主要在逐係數 `std::pow` twist（未量測的假設），把它快 3×（W3）會讓 PL 切分的價值變大：延遲 8.2 vs all-ARM 15.2 ms（1.84×），吞吐 274k vs 121k 筆/秒。也就是 Amdahl 的順序：先量、先修 ARM 專屬節點，再談加硬體。

**哪些結論穩、哪些看估計值**：S0 的診斷、序列 RNS 段、Amdahl 天花板、校準誤差都只用 MEASURED/DERIVED，屬穩健結論。S1–S7 的絕對數字依賴 D1 的 ESTIMATED 成本（團隊自己的估計＋本模型加的快取維護）、D2 的 ASSUMED、以及新 HLS 運算的 ESTIMATED 週期數；但「延遲增益小、吞吐增益大、最佳解是混合切分」在 D1 每 byte 成本放大 8× 時仍成立（第 7.2 節）。W1–W3 是 ARM 軟體的假設情境，只用來排定量測優先順序。

## 1. 方法

老師的批評：ARM／FPGA 的分工是憑直覺定的。本模型改成：

1. **拆成最小可實作節點**：把每批（2,048 筆、4 特徵）的 client 工作拆成第 2 節的 DAG。節點的粒度＝今天或下一版能單獨實作的一段程式或一個 PL 運算（例如每個 limb 的 NTT、每個特徵的 RNS 對應）。
2. **量／估每個節點在 ARM 與 PL 上的成本**：ARM 成本由實測的步驟時間與 V5 原始碼裡的工作量（轉換次數、元素數）反推（DERIVED）；PL 成本由 RTL cosim 週期數（MEASURED）組合；PL 做不到的節點標成 ARM-only。
3. **邊上放傳輸成本**：一次 PS→PL→PS 呼叫＝`pack`（使用者空間轉置）＋`pre`（驅動：copy_in、validate、清空、DMA 設定）＋`win`（DMA＋PL kernel；ARM 執行緒睡眠）＋`post`（copy_out、wipe）＋`unpack`。參數由 DMA trace 校準（第 3 節）。
4. **先校準再預測**：照 V5 的實際結構（步驟依序、client 序列段、CpuBackend 兩階段 pool、FpgaBackend 逐筆或雙緩衝）重建六種已量測設定，對上實測（第 4 節）後，才拿去預測新情境。
5. **求最佳切分**：對每個情境窮舉所有雙棲節點的 ARM/PL 指派（4 個特徵對稱→以多重集合列舉，含「k 個特徵上 PL、其餘留 ARM」的混合解），每個指派用 list scheduler 排程：ARM 4 核（encode 每特徵一個任務、decode 單執行緒、CRT 可依係數切 4 塊—每個係數獨立，理由充分）、PL 一個引擎；驅動 mutex 深度 1（現況）或 2（雙緩衝：第 i+1 次的傳輸與第 i 次的 kernel 重疊）。
6. **目標函數**：(1) 單批邊緣延遲（不含雲端）；(2) 長串流穩態吞吐量＝2048 ÷ 每批瓶頸資源時間（ARM 4 核、PL 引擎、驅動 mutex、各管線段、加解密共用的 backend lock，各除以由實測校準的效率 η）；(3) 能耗（整板功率線性模型，由 54 筆實測擬合）。
7. **兩個對照解法**：Stone（IEEE TSE 1977）雙處理器 min-cut（Edmonds-Karp 自寫）與 LogCA（Altaf & Wood, ISCA 2017）式的break-even 分析。

**三種排程模式**：`IMPL`＝V5 現在的寫法（只用於校準）；`STEP`＝保留 V5 的步驟邊界、步驟內依資料相依並行；`DAG`＝只保留資料相依（資料流 runtime）。情境比較一律在**同一模式**下比，所以「資料流重構」和「PL 切分」的效果可以分開看。相同延遲（差 < 1%）時，報告呼叫次數最少的切分（不值得為 < 1% 去加硬體呼叫）。

## 2. 節點表（每批）

資料大小：軟體 RNS 多項式 3×4096×uint32＝48 KiB（limb-major）；DMA 格式每係數一個 128-bit beat＝64 KiB。ARM 成本為**單核**時間（4 核同時跑轉換時再乘 1.063，DERIVED c_be4；4 個 encode 同時跑乘 1.058，DERIVED c_enc4）。

| 節點 | 每批數量 | 輸入 → 輸出 | ARM 成本 ms（標記） | PL 今天能做嗎 | PL kernel 成本（標記） |
|---|---|---|---|---|---|
| STD | 1 | 原始 4×2048 double → z 4×16 KiB | 0.427 (M) | 否（ARM-only） | - |
| ENC_f | 4 | z_f 16 KiB → m_f 32 KiB（int64） | 7.840 (D) = (encode_w1 − NOWRAP)/4 | 否：浮點 FFT＋逐係數 `std::pow` twist＋取整 | what-if：僅 FFT 核心 14336 cyc = 0.143 ms (E)，需倍精度；twist 未計，不採用 |
| NOWRAP | 1 | 4×m_f → 上界 | 0.020 (E) | 否 | - |
| SAMPLE | 1 | OS 亂數 → 12 個三元多項式（各 4 KiB） | 0.108 (M) | 否（政策：PRNG 不搬回 PL） | - |
| VALKEY | 1 | 公鑰 96 KiB 檢查 | 0.037 (E) | 否 | - |
| RNS_m_f | 4 | m_f 32 KiB → 48 KiB | 0.435 (D) = 12288×35.4 ns | 否（未實作；可做 Barrett） | - |
| RNS_t_f | 4 | u,e0,e1 3×4 KiB → 3×48 KiB | 1.322 (D) | 否（三元值可查表） | - |
| NTT_a / NTT_b | 2（各 3 limb） | 48 KiB → 48 KiB | 3×0.704 (D)，可 session 快取 | 是：op0 | 18775 cyc = 0.188 ms (M, cosim，含串流) |
| NTT_u_f | 4 | 48 KiB → 48 KiB | 3×0.704 (D) | 是：op0 | 18775 cyc = 0.188 ms (M) |
| PW_bu_f / PW_au_f | 8 | 2×48 KiB → 48 KiB | 3×0.031 (E) | 只在 op2／F／K 內 | pw_eff 2983 cyc = 0.030 ms (D) |
| INTT_bu_f / INTT_au_f | 8 | 48 KiB → 48 KiB | 3×0.704 (E，假設＝正轉換) | 是：op1 | 17741 cyc = 0.177 ms (M) |
| OP2（黑盒乘積） | 8＋1 | DMA 128 KiB → 64 KiB | - | 是：op2（兩運算元都重送、重轉） | 45986 cyc = 0.460 ms (M) |
| ADD_c0_f | 4 | bu＋e0＋m → c0 | 2×0.070 (D) | 是：op4（被支配，見 3.3） | 12323 cyc = 0.123 ms (M) |
| ADD_c1_f | 4 | au＋e1 → c1 | 0.070 (D) | 是：op3（被支配） | 8228 cyc = 0.082 ms (M) |
| FENC_f（S2+） | 4 | DMA m,u,e0,e1 4×64 KiB → c0,c1 2×64 KiB | - | 新 HLS 運算（公鑰常駐 NTT 形式） | 52066 cyc = 0.521 ms (E) |
| KPAIR_f（S7+） | 4 | DMA u 64 KiB → b·u, a·u 2×64 KiB | - | 新 HLS 運算（Stone 建議） | 47935 cyc = 0.479 ms (E) |
| CLOUD | 1 | 4 密文 384 KiB → 1 密文 96 KiB | 4.546 (M)，不在邊緣範圍；吞吐量模式在板上同行程 | - | - |
| DEC_PRE | 1 | 檢查 c0', c1', s | 0.043 (E) | 否 | - |
| RNS_s | 1 | s 4 KiB → 48 KiB | 0.435 (D)，可 session 快取 | 否 | - |
| NTT_c1 / NTT_s | 2 | 48 KiB → 48 KiB | 3×0.704 (D)；NTT(s) 可快取 | 是：op0 | 18775 cyc = 0.188 ms (M) |
| PW_d / INTT_d | 1 / 1 | → c1'·s | 3×0.031 / 3×0.704 | INTT：op1；或整個 op2 | 2983 cyc = 0.030 ms / 17741 cyc = 0.177 ms |
| ADD_d | 1 | c0'＋c1'·s | 0.070 (D) | 是：op3（被支配） | 8228 cyc = 0.082 ms (M) |
| FDEC（S6） | 1 | DMA c0',c1' → d | - | 新 HLS 運算（私鑰常駐，安全取捨） | 31342 cyc = 0.313 ms (E) |
| CRT | 1 | 48 KiB → 64 KiB（int128） | 0.968 (D)；可依係數切塊並行 | 否（128-bit，未實作） | - |
| NWCHECK | 1 | 64 KiB 比較 | 0.040 (E) | 否 | - |
| DEC_FFT | 1 | 64 KiB → 2048 complex 32 KiB | 8.400 (D)，單執行緒 | 否（同 ENC） | - |
| SIGMOID | 1 | 2048 logit → 機率／判斷 | 0.200 (E) | 否 | - |
| SPMUL（只用於 W2） | 9×3 | 稀疏三元乘積（64 個 ±1） | 0.492／limb (E) | - | - |

ARM 成本的推導（都在 `model_results.json` 的 `parameters`，每項附來源）：

- `t_ntt_limb` = 0.7045 ms (D)：CpuBackend 的 wall time（`results/2026-10-14-kv260/stage2_03/e2e_all.json` latency `backend_stats.wall_ms` ÷ 30 輪 = 36.766 ms/輪，cpu-w1）÷ 每輪 51 個 limb 轉換（加密 18 正＋24 逆、解密 6 正＋3 逆，原始碼 `software/native_v5/backend/cpu/cpu_backend.cpp`）減去 pointwise（E，0.031 ms/limb）。
- 2/3/4 個 worker 的減速 c_be2..4 = 1.014 / 1.041 / 1.063 (D)：同法，任務數取 ceil(任務/worker) 輪。
- `S_enc` = 7.901 ms、`S_dec` = 1.515 ms (D)：各步驟實測減去後端模型的平均（5／6 個後端），殘差 ≤ 0.17 ms。它們是 `Client::encrypt/decrypt`（`software/native_v5/ckks/client.cpp`）的序列部分，與後端無關。
- 每元素 residue 成本 35.4 ns (D)：由 cpu-w1 keygen（7.37 ms，單次冷樣本）減去 1 個乘積的後端模型得到；用它算出的 S_enc 分解（RNS 7.03＋add 0.84 ms）與 op-count 估計（add() ≈ 0.074 ms）一致。

## 3. 邊與傳輸模型

一次呼叫：`t_call = pack(I) + [t_fixed·s + I·(copy_in+validate+maint) + O·clear] + [K·10ns + I·δ + t0] + [t_fixed·(1−s) + O·(copy_out+maint) + wipe] + unpack(O)`，I/O 為輸入／輸出 bytes（或 beats），K 為 kernel 週期。中括號依序是 `pre`（ARM 忙、持有 mutex）、`win`（ARM 睡、PL 忙）、`post`（ARM 忙、持有 mutex）。

| 參數 | D0 現況 V3 驅動 | D1 修正驅動（可快取＋快取維護） | D2 zero-copy（u-dma-buf 類） |
|---|---|---|---|
| copy_in (ns/B) | 0.214 (M) | 0.214 (A＝D0) | 0 (A) |
| validate (ns/B) | 27.38 (M) | 0.381 (E 團隊) | 0：併入 pack (A) |
| clear_out (ns/B) | 0.153 (M) | 0.153 (A) | 0 (A) |
| 快取維護 (ns/B) | 0 | 0.153 (E) | 0.153 (E) |
| copy_out (ns/B) | 6.866 (M) | 0.763 (E 團隊) | 0 (A) |
| wipe | 34.5 us 固定 (M) | 0.132 ns/B (D) | 0.132 ns/B (A) |
| t_fixed（syscall、mutex、檢查、DMA 設定） | 19.6 us (D) | 同 (A) | 同 (A) |
| win 的 t0（DMA 啟動＋IRQ＋喚醒） | 61.4 us (D) | 同 | 同 |
| win 的 δ（每輸入 beat 額外） | 3.19 ns/beat (D) | 同 | 同 |
| pack / unpack (ns/beat) | 8.52 / 44.91 (D) | 同 | pack ×1.3 (A) |
| 每次呼叫固定部分 t_fixed+wipe+t0 | 0.115 ms | 0.081 ms | 0.081 ms |

來源：D0 各段＝`results/2026-10-14-kv260/dma_trace_01/breakdown.json` 的中位數（109 次呼叫）；t_fixed 由未追蹤的整體 4.677 ms（`results/2026-10-14-kv260/dma_trace_01/trace_report.json` baseline）扣掉各段得到；t0、δ 由 V3 benchmark 的 op0/op1/op2 driver p50（`release/RESULTS.json`）與 V5 未追蹤 op2 共 4 點最小平方擬合，殘差 ≤ 1.7 us。D1 的 validate／copy_out 是團隊在 `docs/COMPARISON.md` 的估計（3.59→0.05、0.45→0.05 ms），快取維護（每 64 KiB 約 10 us）是本模型另加的 ESTIMATED。D2 的 1.6 GB/s（128 bit×100 MHz）已內含在 kernel 的 1 beat/cycle 串流裡，所以不會再額外加傳輸時間。

### 3.1 一次 op2 乘積（輸入 128 KiB、輸出 64 KiB）在三種驅動下

| 驅動 | pack | pre | win（PL+DMA） | post | unpack | 合計 ms | 其中 ARM 忙碌 ms |
|---|---|---|---|---|---|---|---|
| D0 | 0.070 | 3.644 | 0.547 | 0.487 | 0.184 | 4.932 | 4.385 |
| D1 | 0.070 | 0.125 | 0.547 | 0.088 | 0.184 | 1.015 | 0.467 |
| D2 | 0.091 | 0.037 | 0.547 | 0.038 | 0.184 | 0.898 | 0.350 |

同一個乘積在 ARM 上（cpu-w1 單核、無共用轉換）約 6.43 ms 核心時間：D0 呼叫本身就花 4.38 ms ARM 忙碌時間，所以 S0 的卸載連 ARM 都省不下來。

### 3.2 t0／δ 擬合點

| 輸入 beats | cosim 週期 | 實測 window us | 模型 us |
|---|---|---|---|
| 4096 | 18775 | 261.0 | 262.2 |
| 4096 | 17741 | 253.0 | 251.8 |
| 8192 | 45986 | 549.0 | 547.3 |
| 8192 | 45986 | 545.7 | 547.3 |

### 3.3 被支配的選項（不進列舉）

把加法丟到 PL（op3/op4）在三種驅動下都被支配：呼叫的 ARM 端成本（pack＋pre＋post＋unpack）已大於 ARM 直接相加，而且結果更晚回來，所以不可能改善延遲或吞吐量。

| 驅動 | 選項 | ARM 直接做 ms | 呼叫的 ARM 端 ms | 呼叫延遲 ms | 被支配 |
|---|---|---|---|---|---|
| D0 | ADD_c1 via op3 | 0.070 | 4.385 | 4.555 | 是 |
| D0 | ADD_c0 via op4 | 0.139 | 6.228 | 6.452 | 是 |
| D1 | ADD_c1 via op3 | 0.070 | 0.467 | 0.637 | 是 |
| D1 | ADD_c0 via op4 | 0.139 | 0.560 | 0.784 | 是 |
| D2 | ADD_c1 via op3 | 0.070 | 0.350 | 0.561 | 是 |
| D2 | ADD_c0 via op4 | 0.139 | 0.414 | 0.679 | 是 |

## 4. 校準：模型先重現實測（IMPL 模式）

### 4.1 每步驟延遲（ms，p50；`results/2026-10-14-kv260/stage2_03/e2e_all.json`）

| 後端 | 準備 ①–③ 實測/模型 | 加密 ④⑤ | 解密 ⑨ | 解碼 ⑩ | 邊緣合計 |
|---|---|---|---|---|---|
| cpu-w1 | 8.86 / 8.85 (-0.1%) | 38.38 / 38.23 (-0.4%) | 7.86 / 7.95 (+1.1%) | 8.63 / 8.64 (+0.1%) | 63.77 / 63.68 (-0.1%) |
| cpu-w2 | 8.86 / 8.85 (-0.1%) | 23.33 / 23.28 (-0.2%) | 5.18 / 5.15 (-0.6%) | 8.63 / 8.64 (+0.1%) | 46.02 / 45.93 (-0.2%) |
| cpu-w3 | 8.85 / 8.85 (-0.0%) | 18.41 / 18.43 (+0.1%) | 3.82 / 3.75 (-1.8%) | 8.64 / 8.64 (-0.0%) | 39.74 / 39.67 (-0.2%) |
| cpu-w4 | 8.85 / 8.85 (+0.1%) | 16.17 / 16.34 (+1.0%) | 3.74 / 3.79 (+1.3%) | 8.65 / 8.64 (-0.1%) | 37.40 / 37.62 (+0.6%) |
| fpga-serial | 8.84 / 8.85 (+0.2%) | 47.27 / 47.36 (+0.2%) | 6.46 / 6.45 (-0.2%) | 8.65 / 8.64 (-0.1%) | 71.26 / 71.30 (+0.1%) |
| fpga-overlap | 8.86 / 8.85 (-0.0%) | 45.98 / 45.58 (-0.9%) | 6.45 / 6.45 (-0.0%) | 8.64 / 8.64 (-0.0%) | 69.91 / 69.52 (-0.6%) |

擬合用到的量測：每個後端的 backend wall time（推 t_ntt_limb、c_be2..4）、加解密步驟（推 S_enc、S_dec 兩個常數）、stage2_02 的單執行緒 encode（推 t_enc_f）。其餘欄位是結構預測：例如 fpga-serial、fpga-overlap 的加密步驟完全由trace 校準的 D0 呼叫模型＋S_enc 預測，誤差 +0.2%／-0.9%。

### 4.2 保留驗證（held-out）

| keygen 後端 | 實測 ms (M) | 模型 ms | 誤差 | 說明 |
|---|---|---|---|---|
| cpu-w1 | 7.370 | 7.370 | +0.0% | 樣本內（用來定 residue 成本） |
| cpu-w2 | 4.561 | 4.572 | +0.2% | 保留驗證（單次冷樣本） |
| cpu-w3 | 3.384 | 3.169 | -6.4% | 保留驗證（單次冷樣本） |
| cpu-w4 | 3.198 | 3.216 | +0.6% | 保留驗證（單次冷樣本） |
| fpga-serial | 5.840 | 5.869 | +0.5% | 保留驗證（單次冷樣本） |
| fpga-overlap | 5.816 | 5.869 | +0.9% | 保留驗證（單次冷樣本） |

| 單次呼叫 | 實測 ms (M) | 模型 ms | 誤差 | 來源 |
|---|---|---|---|---|
| op2 (V5 e2e, ioctl only) | 4.670 | 4.678 | +0.2% | results/2026-10-14-kv260/stage2_03/e2e_all.json |
| op2 (trace baseline, ioctl only) | 4.677 | 4.678 | +0.0% | results/2026-10-14-kv260/dma_trace_01/trace_report.json |
| op0 NTT (V3, pack+ioctl+unpack) | 2.701 | 2.804 | +3.8% | release/RESULTS.json benchmark (V3 software pack/unpack differs from V5) |
| op1 INTT (V3, pack+ioctl+unpack) | 2.693 | 2.793 | +3.7% | release/RESULTS.json benchmark (V3 software pack/unpack differs from V5) |
| op2 multiply (V3, pack+ioctl+unpack) | 5.026 | 4.932 | -1.9% | release/RESULTS.json benchmark (V3 software pack/unpack differs from V5) |

V3 的 op0／op1（NTT／INTT 單獨呼叫）在 D0 模型裡完全沒有被擬合，預測誤差約 +4%（V3 的 pack/unpack 程式不同）。

### 4.3 吞吐量、CPU 使用率、功率（19 批、佇列容量 2；`results/2026-10-14-kv260/stage2_03/e2e_all.json`）

| 後端 | 角色 | 實測 wall/批 ms | 模型 ms | 誤差 | 模型瓶頸 | 報表筆/秒 (M) | CPU 使用率 實測/模型 | W 實測/模型 |
|---|---|---|---|---|---|---|---|---|
| cpu-w1 | fit | 48.78 | 49.14 | +0.7% | backend lock (encrypt+decrypt stages) | 41058 | 0.483 / 0.477 | 3.53 / 3.51 |
| cpu-w2 | held-out | 34.48 | 30.25 | -12.3% | backend lock (encrypt+decrypt stages) | 58081 | 0.682 / 0.679 | 3.71 / 3.74 |
| cpu-w3 | held-out | 30.56 | 29.75 | -2.7% | ARM cores (4) | 65525 | 0.770 / 0.774 | 3.76 / 3.76 |
| cpu-w4 | fit | 30.00 | 30.00 | +0.0% | ARM cores (4) | 66748 | 0.786 / 0.795 | 3.79 / 3.76 |
| fpga-serial | fit | 57.53 | 57.25 | -0.5% | backend lock (encrypt+decrypt stages) | 34812 | 0.420 / 0.416 | 3.56 / 3.54 |
| fpga-overlap | fit | 55.51 | 55.36 | -0.3% | backend lock (encrypt+decrypt stages) | 36080 | 0.438 / 0.432 | 3.60 / 3.55 |

效率 η：η_L = 0.940（backend-lock 受限的 cpu-w1、fpga-serial、fpga-overlap 平均）、η_C = 0.795（ARM 受限的 cpu-w4）。cpu-w2、cpu-w3 是保留驗證；cpu-w2 的 -12.3% 來自兩個界限（lock 30.3、ARM 29.4 ms）幾乎相等，max-bound 忽略了兩者的交互排隊。CPU 使用率的模型值（ARM 核心時間÷(4×實測 wall)）在 6 種設定都與實測差 ≤ 0.009，這直接驗證「D0 驅動的 validate／copy_out 是 ARM 忙碌時間」。串行（容量 0）吞吐量 6 種設定誤差 -2.9% 到 -1.2%。

功率模型 W = 3.14 + 0.195×忙碌核心 + 0.83×PL duty（54 筆，RMSE 0.032 W；PL 項只有 3 個 duty 水準，辨識度低）。


## 5. 情境結果（預測）

基準：實測 cpu-w4 邊緣延遲 37.40 ms、19 批吞吐量 66748 筆/秒（報表值）。延遲是單批邊緣（不含雲端）；吞吐量是板上配置（雲端在同一行程，和實測一致）；「edge-only」欄假設雲端在 PC。

### 5.1 指定情境 S0–S5

| 情境 | 驅動 | 延遲最佳切分（DAG, d1） | 延遲 ms DAG-d1 / DAG-d2 / STEP | 同情境 all-ARM ms | vs all-ARM | vs 實測 cpu-w4 | 吞吐最佳切分 | 筆/秒（板上） | 筆/秒（edge-only） | 吞吐 vs all-ARM | 吞吐瓶頸 | Amdahl 上限 延遲/吞吐 | 筆/焦耳 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S0 | D0 | key=A; [4xA]; dec=A; NTT(s)=A | 29.79 / 29.79 / 30.16 | 29.79 | 1.00× | 1.26× | key=A; [4xA]; dec=O; NTT(s)=A | 70058 | 73659 | 1.03× | ARM cores (4) | 1.53× / 1.72× | 18561 |
| S1 | D1 | key=P; [2xO 1xA 1xN0]; dec=O; NTT(s)=A | 24.88 / 23.48 / 25.49 | 29.79 | 1.20× | 1.50× | key=A; [4xO]; dec=O; NTT(s)=A | 107561 | 116291 | 1.58× | ARM cores (4) | 1.53× / 1.72× | 27076 |
| S2 | D1 | [2xF 1xA 1xN0]; dec=O | 23.83 / 22.67 / 24.24 | 28.08 | 1.18× | 1.57× | [4xF]; dec=O | 110013 | 119163 | 1.49× | ARM cores (4) | 1.45× / 1.60× | 28239 |
| S3 | D1 | [3xF 1xA]; dec=O | 23.64 / 23.08 / 24.17 | 28.08 | 1.19× | 1.58× | [4xF]; dec=O | 110123 | 119291 | 1.49× | ARM cores (4) | 1.45× / 1.60× | 28326 |
| S4 | D2 | [3xF 1xA]; dec=O | 23.05 / 22.33 / 23.53 | 28.08 | 1.22× | 1.62× | [4xF]; dec=O | 112120 | 121638 | 1.52× | ARM cores (4) | 1.45× / 1.60× | 28822 |
| S5 | D1 | [3xA 1xF]; dec=A | 21.91 / 21.80 / 22.22 | 22.30 | 1.02× | 1.71× | [4xF]; dec=O | 110123 | 119291 | 1.13× | ARM cores (4) | 1.15× / 1.21× | 28326 |

### 5.2 延伸情境（what-if）

| 情境 | 驅動 | 延遲最佳切分（DAG, d1） | 延遲 ms DAG-d1 / DAG-d2 / STEP | 同情境 all-ARM ms | vs all-ARM | vs 實測 cpu-w4 | 吞吐最佳切分 | 筆/秒（板上） | 筆/秒（edge-only） | 吞吐 vs all-ARM | 吞吐瓶頸 | Amdahl 上限 延遲/吞吐 | 筆/焦耳 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S6 | D2 | [3xF 1xA]; dec=F | 22.84 / 22.11 / 23.31 | 28.08 | 1.23× | 1.64× | [4xF]; dec=F | 112254 | 121797 | 1.52× | ARM cores (4) | 1.45× / 1.60× | 28905 |
| S7 | D2 | [3xK 1xOA]; dec=O | 22.34 / 22.19 / 23.03 | 28.08 | 1.26× | 1.67× | [4xF]; dec=O | 112120 | 121638 | 1.52× | ARM cores (4) | 1.45× / 1.60× | 28822 |
| W1 | D2 | [4xK]; dec=O | 19.38 / 19.38 / 21.52 | 26.30 | 1.36× | 1.93× | [4xF]; dec=O | 126514 | 138767 | 1.59× | ARM cores (4) | 1.49× / 1.69× | 32382 |
| W2 | D2 | [4xK]; dec=A | 19.01 / 19.01 / 20.55 | 21.49 | 1.13× | 1.97× | [4xF]; dec=O | 126514 | 138767 | 1.23× | ARM cores (4) | 1.21× / 1.31× | 32382 |
| W3 | D2 | [4xK]; dec=O | 8.25 / 8.25 / 10.38 | 15.17 | 1.84× | 4.53× | [4xF]; dec=O | 274193 | 339082 | 2.27× | ARM cores (4) | 2.31× / 2.61× | 67212 |

情境定義：

- **S0**（D0）：current: V3 driver (D0), ops of the frozen bitstream (V5 uses op2 only), key re-sent every call; ARM side as implemented (no session caching)
- **S1**（D1）：D1 fixed driver (cacheable buffers + cache maintenance), same op interface
- **S2**（D1）：S1 + public key resident in PL (NTT form) + fused encrypt-one-feature op (in m,u,e0,e1; out c0,c1); ARM side also caches NTT(a),NTT(b),NTT(s),residues(s) per session (fairness)
- **S3**（D1）：S2 + all fused features of a batch in ONE call (call coalescing)
- **S4**（D2）：S3 + D2 zero-copy user buffers (u-dma-buf like), DMA-bandwidth bound only
- **S5**（D1）：S3 with a stronger ARM baseline: NTT/INTT/pointwise 3x faster (NEON-NTT-like; sensitivity)
- **S6**（D2）：what-if: S4 + secret key resident in PL, fused decrypt d=c0'+c1'*s (security trade-off)
- **S7**（D2）：what-if: S4 + key-resident product-pair op K (in u; out b*u, a*u; adds stay on ARM) -- the interface suggested by Stone's min-cut
- **W1**（D2）：ARM-software what-if on S7 hardware: ternary residues by lookup, m residues by Barrett (ESTIMATED), session caches
- **W2**（D2）：W1 + ARM products as sparse-ternary convolutions (u,s have 64 nonzeros; no NTT) (ESTIMATED)
- **W3**（D2）：W1 + encode/decode 3x faster (hypothesis: per-coefficient std::pow twist dominates; UNMEASURED)

切分記號：`4xA`＝4 個特徵都在 ARM；`F`＝融合加密運算；`K`＝公鑰常駐乘積對；`O`＝op2 黑盒；`OA`＝b·u 用 op2、a·u 留 ARM；`N0`＝NTT(u) 用 op0；`N1`＝兩個 INTT 用 op1；`N01`＝兩者；`dec=`解密乘積的位置；S0–S1 另有 `key=A/P`（公鑰 NTT 在 ARM 或 op0）。S3 以後同類運算的特徵合併成一次呼叫。

### 5.3 誰占滿了 ARM（吞吐最佳切分，每批 ARM 核心 ms，含板上雲端）

| 情境 | encode | decode | rns | cloud | crt | products(ARM) | add | driver | pack/unpack | other | ARM 合計 | PL 引擎 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S0 | 33.2 | 8.6 | 7.5 | 4.5 | 1.0 | 32.2 | 0.9 | 4.1 | 0.3 | 0.5 | 93.0 | 0.55 |
| S1 | 33.2 | 8.6 | 7.5 | 4.5 | 1.0 | 0.0 | 0.9 | 1.9 | 2.3 | 0.5 | 60.6 | 4.93 |
| S3 | 33.2 | 8.6 | 7.1 | 4.5 | 1.0 | 0.0 | 0.1 | 1.8 | 2.3 | 0.5 | 59.1 | 2.90 |
| S4 | 33.2 | 8.6 | 7.1 | 4.5 | 1.0 | 0.0 | 0.1 | 0.5 | 2.5 | 0.5 | 58.1 | 2.90 |
| S7 | 33.2 | 8.6 | 7.1 | 4.5 | 1.0 | 0.0 | 0.1 | 0.5 | 2.5 | 0.5 | 58.1 | 2.90 |
| W1 | 33.2 | 8.6 | 0.5 | 4.5 | 1.0 | 0.0 | 0.1 | 0.5 | 2.5 | 0.5 | 51.5 | 2.90 |
| W3 | 11.1 | 3.0 | 0.5 | 4.5 | 1.0 | 0.0 | 0.1 | 0.5 | 2.5 | 0.5 | 23.8 | 2.90 |
| S0 all-ARM | 33.2 | 8.6 | 7.5 | 4.5 | 1.0 | 39.1 | 0.9 | 0.0 | 0.0 | 0.5 | 95.4 | 0 |

### 5.4 延遲最佳解的關鍵路徑（DAG, d1；前幾大節點）

| 情境 | 關鍵路徑上的主要節點（ms） |
|---|---|
| S0 | DEC_FFT 8.40、ENC_f3 8.30、RNS_t_f0 1.32、INTT_bu_f0 0.78、INTT_bu_f1 0.78 |
| S1 | DEC_FFT 8.40、ENC_f3 8.30、RNS_t_f3 1.32、INTT_bu_f3 0.78、INTT_au_f0 0.78 |
| S3 | DEC_FFT 8.40、ENC_f2 8.30、FENC_ALL [PL] 1.78、FENC_ALL 1.69、RNS_t_f1 1.32 |
| S4 | DEC_FFT 8.40、ENC_f1 8.30、RNS_t_f0 1.32、INTT_bu_f0 0.78、INTT_au_f0 0.78 |
| S5 | DEC_FFT 8.40、ENC_f0 8.30、RNS_t_f0 1.32、FENC_ALL 0.55、RNS_m_f1 0.43 |
| S7 | DEC_FFT 8.40、ENC_f1 8.30、KPAIR_ALL [PL] 1.54、RNS_t_f1 1.32、OP2_bu_f0 [PL] 0.55 |
| W3 | DEC_FFT 2.80、ENC_f0 2.77、KPAIR_ALL 0.55、OP2_d [PL] 0.55、STD 0.43 |

## 6. Stone min-cut vs 列舉法

Stone 模型：每個節點在 ARM／PL 各有執行成本，資料邊被切開時付傳輸成本；min s-t cut＝最小**總工作量**。可融合情境（S2+）用 PL kernel-only 成本＋有方向的傳輸弧（ARM→PL 付 pack＋驅動進＋串流進；PL→ARM 付串流出＋驅動出＋unpack）。S0、S1 每個 PL 節點都是獨立呼叫，資料必經 DDR，所以 cut 退化成逐節點比較。

| 情境 | min-cut 值 ms | PL 節點數 | Stone 解（對應切分） | 其 makespan ms | 其總工作量 ms | 列舉法延遲最佳 | 其 makespan ms | 其總工作量 ms | all-ARM 總工作量 ms |
|---|---|---|---|---|---|---|---|---|---|
| S0 | 90.9 | 0 | key=A; [4xA]; dec=A; NTT(s)=A | 29.79 | 90.9 | key=A; [4xA]; dec=A; NTT(s)=A | 29.79 | 90.9 | 90.9 |
| S1 | 63.4 | 17 | key=P; [4xN01]; dec=N01; NTT(s)=P | 25.77 | 63.4 | key=P; [2xO 1xA 1xN0]; dec=O; NTT(s)=A | 24.88 | 70.4 | 90.9 |
| S2 | 56.7 | 23 | K 型（乘積上 PL、加法留 ARM；本情境沒有此運算，見 S7） | - | - | [2xF 1xA 1xN0]; dec=O | 23.83 | 67.3 | 83.7 |
| S3 | 56.5 | 23 | K 型（乘積上 PL、加法留 ARM；本情境沒有此運算，見 S7） | - | - | [3xF 1xA]; dec=O | 23.64 | 63.2 | 83.7 |
| S4 | 55.9 | 23 | K 型（乘積上 PL、加法留 ARM；本情境沒有此運算，見 S7） | - | - | [3xF 1xA]; dec=O | 23.05 | 62.4 | 83.7 |
| S5 | 56.5 | 23 | K 型（乘積上 PL、加法留 ARM；本情境沒有此運算，見 S7） | - | - | [3xA 1xF]; dec=A | 21.91 | 61.2 | 62.2 |
| S6 | 55.9 | 24 | K 型（乘積上 PL、加法留 ARM；本情境沒有此運算，見 S7） | - | - | [3xF 1xA]; dec=F | 22.84 | 62.1 | 83.7 |
| S7 | 55.9 | 23 | [4xK]; dec=O | 22.84 | 56.2 | [3xK 1xOA]; dec=O | 22.34 | 60.7 | 83.7 |
| W1 | 49.3 | 23 | [4xK]; dec=O | 19.38 | 49.6 | [4xK]; dec=O | 19.38 | 49.6 | 77.1 |
| W3 | 21.6 | 23 | [4xK]; dec=O | 8.25 | 21.9 | [4xK]; dec=O | 8.25 | 21.9 | 49.4 |

讀法：

- 兩者目標不同。Stone 最小化 ARM＋PL＋傳輸的**總和**，不知道 ARM 有 4 核、也不知道 ARM 與 PL 可以同時工作，所以傾向把「單位成本較低」的節點全部搬到 PL；列舉法最小化 **makespan**，會刻意留一部分在 ARM 與 PL 並行（負載平衡）。
- S2–S6 的 Stone 解是「NTT、pointwise、INTT 上 PL，加法留 ARM」，這不是 F 運算（F 把 m、e0、e1 也送進 PL）。因此新增 S7（K 運算）：每特徵只送 u_f，傳輸量從 4 進 2 出降到 1 進 2 出。S7 的列舉最佳延遲 22.34 ms，比 S4 的 23.05 ms 好，這是「用模型而不是直覺」修正介面設計的例子。
- 已知限制：Stone 的弧成本是線性的，無法表達「每次呼叫一次固定成本、合併呼叫可分攤」與「同一輸出被兩個消費者使用只需傳一次」；本模型以平均分攤固定成本近似。W2（稀疏乘積）節點結構不同，不做 Stone。

## 7. Break-even（LogCA 式）與敏感度

把 k 個乘積合併成一次 PL 呼叫，和 cpu-w4（4 核、V5 CpuBackend 的共用轉換）比較單批延遲。`t_fixed*`＝打平時可容忍的每次呼叫固定開銷＝T_cpu(k) − T_PL 的變動部分；> 實際 t_fixed 才划算，負值＝怎樣都不划算。`s*`＝ARM 轉換要快幾倍，CPU 就追上這次呼叫（延遲）。三種呼叫形式：op2（兩運算元都送，現況）、F（融合加密：m,u,e0,e1 → c0,c1；k=1／9 的第 9 個是 FDEC）、K（公鑰常駐乘積對：u → b·u, a·u；k=1／9 的第 9 個是私鑰常駐單乘積）。CPU 對照組在 F、K 形式下也快取公鑰 NTT（公平）。

| 形式 | k | DMA KiB 進/出 | kernel ms | cpu-w4 ms | D0：呼叫 / t_fixed* / s* | D1：呼叫 / t_fixed* / s* | D2：呼叫 / t_fixed* / s* |
|---|---|---|---|---|---|---|---|
| op2 | 1 | 128/64 | 0.460 | 2.28 | 4.93 / -2.54 / 0.46 | 1.01 / 1.35 / 2.25 | 0.90 / 1.46 / 2.54 |
| op2 | 2 | 256/128 | 0.920 | 3.81 | 9.75 / -5.82 / 0.39 | 1.95 / 1.94 / 1.96 | 1.71 / 2.18 / 2.22 |
| op2 | 4 | 512/256 | 1.839 | 4.59 | 19.38 / -14.68 / 0.24 | 3.82 / 0.86 / 1.20 | 3.35 / 1.32 / 1.37 |
| op2 | 8 | 1024/512 | 3.679 | 8.43 | 38.65 / -30.10 / 0.22 | 7.55 / 0.96 / 1.12 | 6.61 / 1.90 / 1.28 |
| op2 | 9 | 1152/576 | 4.139 | 9.96 | 43.47 / -33.39 / 0.23 | 8.48 / 1.56 / 1.17 | 7.43 / 2.62 / 1.34 |
| fused | 1 | 128/64 | 0.313 | 1.60 | 4.79 / -3.07 / 0.33 | 0.87 / 0.81 / 1.84 | 0.75 / 0.93 / 2.13 |
| fused | 2 | 256/128 | 0.521 | 2.38 | 9.35 / -6.85 / 0.25 | 1.55 / 0.91 / 1.54 | 1.32 / 1.15 / 1.81 |
| fused | 4 | 512/256 | 1.041 | 3.95 | 18.58 / -14.52 / 0.21 | 3.02 / 1.01 / 1.31 | 2.55 / 1.48 / 1.55 |
| fused | 8 | 1024/512 | 2.083 | 7.15 | 37.05 / -29.79 / 0.19 | 5.95 / 1.27 / 1.20 | 5.02 / 2.21 / 1.42 |
| fused | 9 | 1152/576 | 2.396 | 8.69 | 41.72 / -32.91 / 0.21 | 6.74 / 2.03 / 1.29 | 5.69 / 3.09 / 1.53 |
| kpair | 1 | 64/64 | 0.313 | 1.53 | 2.93 / -1.28 / 0.52 | 0.76 / 0.85 / 2.01 | 0.67 / 0.94 / 2.27 |
| kpair | 2 | 64/128 | 0.479 | 2.31 | 3.74 / -1.31 / 0.62 | 1.19 / 1.20 / 1.94 | 1.04 / 1.35 / 2.22 |
| kpair | 4 | 128/256 | 0.959 | 3.84 | 7.36 / -3.40 / 0.52 | 2.30 / 1.62 / 1.67 | 2.00 / 1.92 / 1.92 |
| kpair | 8 | 256/512 | 1.917 | 6.94 | 14.61 / -7.56 / 0.47 | 4.52 / 2.50 / 1.53 | 3.93 / 3.09 / 1.77 |
| kpair | 9 | 320/576 | 2.230 | 8.47 | 17.42 / -8.84 / 0.49 | 5.20 / 3.35 / 1.63 | 4.52 / 4.03 / 1.87 |

實際每次呼叫的固定部分：D0 0.115 ms、D1 0.081 ms、D2 0.081 ms。結論：

- D0：所有 k 的 t_fixed* 都是負的——就算固定開銷是 0，D0 的「每 byte」成本（validate 27 ns/B、copy_out 6.9 ns/B）就已經比 4 核 CPU 慢；合併呼叫救不了 D0（成本與 bytes 成正比，不是固定成本問題）。
- D1／D2：t_fixed* 介於 0.81–4.03 ms，遠大於實際約 0.081 ms，所以固定開銷已不是問題；限制變成「每 byte 的打包、unpack 與串流」以及 ARM 本身多快。op2 形式在 k=4–8 時 s* 只有 1.12–1.37：ARM 轉換再快一點點，op2 卸載就不划算；K 形式傳得最少，s* 最高（D1 1.53–2.01，D2 1.77–2.27）。
- 吞吐量的 break-even（ARM 核心時間）寬鬆得多：見 `model_results.json` 的 `tfixed_breakeven_armtime_ms`（op2、k=8、D1 約 28.7 ms）。

### 7.1 全模型對 ARM 轉換速度的敏感度（DAG, d1）

| 情境 | ARM NTT 加速 | 最佳延遲 ms | all-ARM ms | PL 延遲增益 | 最佳切分 | 最佳筆/秒 | all-ARM 筆/秒 | PL 吞吐增益 |
|---|---|---|---|---|---|---|---|---|
| S1 | 1.0× | 24.88 | 29.79 | 16.5% | key=P; [2xO 1xA 1xN0]; dec=O; NTT(s)=A | 107561 | 68257 | 58% |
| S1 | 1.5× | 23.61 | 26.36 | 10.4% | key=P; [1xA 1xN0 1xN1 1xO]; dec=O; NTT(s)=A | 107561 | 79046 | 36% |
| S1 | 2.0× | 23.16 | 24.63 | 5.9% | key=P; [2xA 1xN0 1xO]; dec=A; NTT(s)=P | 107561 | 85830 | 25% |
| S1 | 3.0× | 22.37 | 22.90 | 2.3% | key=A; [2xA 1xN0 1xO]; dec=A; NTT(s)=P | 107561 | 93887 | 15% |
| S1 | 4.0× | 21.85 | 22.06 | 0.9% | key=A; [3xA 1xO]; dec=A; NTT(s)=A | 107561 | 98511 | 9% |
| S3 | 1.0× | 23.64 | 28.08 | 15.8% | [3xF 1xA]; dec=O | 110123 | 73805 | 49% |
| S3 | 1.5× | 22.91 | 25.26 | 9.3% | [2xF 1xA 1xN0]; dec=A | 110123 | 84073 | 31% |
| S3 | 2.0× | 22.54 | 23.78 | 5.2% | [2xA 2xF]; dec=A | 110123 | 90359 | 22% |
| S3 | 3.0× | 21.91 | 22.30 | 1.8% | [3xA 1xF]; dec=A | 110123 | 97661 | 13% |
| S3 | 4.0× | 21.34 | 21.60 | 1.2% | [3xA 1xF]; dec=A | 110123 | 101773 | 8% |
| S4 | 1.0× | 23.05 | 28.08 | 17.9% | [3xF 1xA]; dec=O | 112120 | 73805 | 52% |
| S4 | 1.5× | 22.63 | 25.26 | 10.4% | [2xF 1xA 1xN0]; dec=O | 112120 | 84073 | 33% |
| S4 | 2.0× | 22.21 | 23.78 | 6.6% | [2xA 2xF]; dec=A | 112120 | 90359 | 24% |
| S4 | 3.0× | 21.67 | 22.30 | 2.8% | [2xA 2xF]; dec=A | 112120 | 97661 | 15% |
| S4 | 4.0× | 21.28 | 21.60 | 1.5% | [3xA 1xF]; dec=A | 112120 | 101773 | 10% |
| S7 | 1.0× | 22.34 | 28.08 | 20.5% | [3xK 1xOA]; dec=O | 112120 | 73805 | 52% |
| S7 | 1.5× | 22.11 | 25.26 | 12.5% | [3xK 1xA]; dec=A | 112120 | 84073 | 33% |
| S7 | 2.0× | 21.58 | 23.78 | 9.2% | [3xK 1xA]; dec=A | 112120 | 90359 | 24% |
| S7 | 3.0× | 20.96 | 22.30 | 6.0% | [3xK 1xA]; dec=A | 112120 | 97661 | 15% |
| S7 | 4.0× | 20.69 | 21.60 | 4.2% | [3xK 1xA]; dec=A | 112120 | 101773 | 10% |

延遲方面，ARM 轉換快 3–4× 時 PL 增益掉到 0.9–6.0%（本模型的平手門檻是 1%）；吞吐量方面仍有 8–15%，因為 encode／decode 等 ARM 專屬節點占滿核心，卸載任何工作都能換到核心時間。

### 7.2 D1 估計值的敏感度（S3；validate、copy_out、快取維護的每 byte 成本同乘一個倍數）

| D1 成本倍數 | 最佳延遲 ms | 最佳切分 | all-ARM ms | 最佳 T ms/批 | all-ARM T ms/批 |
|---|---|---|---|---|---|
| 0.5× | 23.25 | [3xF 1xA]; dec=O | 28.08 | 18.41 | 27.75 |
| 1.0× | 23.64 | [3xF 1xA]; dec=O | 28.08 | 18.60 | 27.75 |
| 2.0× | 24.27 | [2xF 1xA 1xN1]; dec=O | 28.08 | 18.97 | 27.75 |
| 4.0× | 25.25 | [2xA 2xF]; dec=A | 28.08 | 19.70 | 27.75 |
| 8.0× | 26.24 | [2xA 1xF 1xN1]; dec=A | 28.08 | 21.17 | 27.75 |

D1 每 byte 成本即使比團隊估計差 8×，PL 在吞吐量上仍勝 1.31×，延遲增益則從 16% 縮到 7%：「延遲小贏、吞吐大贏」不依賴 D1 估計的精確值。

## 8. 瓶頸與 Amdahl 討論

- **延遲的地板是 ARM 專屬節點**：DAG 排程下，所有可上 PL 的節點成本歸零時，邊緣延遲仍有 19.4 ms：STD 0.43、ENC 8.3（4 核並行，c_enc4=1.058）、RNS 與序列 glue、CRT，最後 DEC_FFT 8.4 ms 單執行緒收尾。S1–S4、S7 的最佳解離地板只剩 2.9–5.5 ms。
- **吞吐量的地板也是 ARM**：S4 吞吐最佳切分每批 ARM 核心時間約 58 ms，其中 encode 33.2、decode 8.6、RNS 7.1、板上雲端 4.5 ms；PL 引擎每批只忙 2.90 ms（利用率 16%）。新的瓶頸資源是 ARM 4 核，新的瓶頸節點是 ENC_f 與 DEC_FFT。
- **所以加寬 PL（更多 lanes）幾乎沒有用**：PL 引擎利用率只有 16%；kernel 再快 2× 最多省下每批 1.5 ms 的 PL 時間，而且 PL 不在吞吐瓶頸上；在延遲上也只影響關鍵路徑上的那一次呼叫。
- **資料流重構本身的價值**：S0 的 all-ARM 在 IMPL（現行寫法）模型是 37.6 ms（實測 37.4），在 DAG 排程是 29.8 ms；差異來自 RNS／CRT 並行、公鑰與 s 的轉換和 encode 重疊、拿掉步驟邊界。這部分不需要任何硬體。
- **W1–W3 說明順序**：只修 RNS（W1）就讓延遲地板降 1.7 ms；若 encode/decode 真能快 3×（W3，未量測），地板降到 6.6 ms，PL 切分的延遲增益從 S7 的 1.26× 變成 1.84×、吞吐增益從 1.52× 變成 2.27×。PL 的價值取決於先把 ARM 專屬的部分做快。
- **傳輸量還能再砍（未建模）**：u_f、e0_f、e1_f 都是三元多項式（u 只有 64 個非零），現在每個仍以 64 KiB 的 RNS DMA 格式送；改成壓縮格式（2 bit/係數＝1 KiB，或 64 個索引＋符號）可讓 K／F 運算的輸入小 1–2 個數量級，代價是 PL 端要加解碼器。
- **稀疏三元乘積（W2，ESTIMATED）**：u 與 s 只有 64 個非零 ±1，b·u 可直接寫成 64 次帶號負循環平移相加，ARM 上約 0.49 ms/limb，比 NTT 路徑每乘積攤提約 1.3 ms/limb 便宜；all-ARM 延遲因此由 26.3 降到 21.5 ms，PL 的延遲增益只剩 12%。這需要 constant-time 審查（位移量取決於秘密的非零位置）。

## 9. 還缺的量測（把 DERIVED/ESTIMATED 換成 MEASURED）

| 缺什麼（現在的標記） | 怎麼量（工具） | 次數 | 統計量 |
|---|---|---|---|
| ARM 每 limb NTT／INTT／pointwise（目前 D/E：由 backend wall 反推，INTT＝NTT 是假設） | 微基準直接呼叫 `Plan::transform`、`Plan::product`；`perf stat -e cycles,instructions,L1-dcache-load-misses,l2d_cache_refill`；綁核、暖機 | 1 核與 4 核同時跑（量 c_be4）；每種 1000 次 × 5 輪 | p50、p95、cycles/butterfly |
| encode() 拆成 FFT／`std::pow` twist／取整；decode() 拆成 twist／IFFT／sigmoid（目前只有整步驟） | `perf record -g` 看熱點＋在 V4 `encode()`／`decode()` 內加 steady_clock 分段（不改數學） | 30 輪 × 3 次 | p50；各段占比 |
| residues()、add()、validate()、centered() 的每元素成本（目前由 keygen 單次冷樣本推） | 微基準；SDIV 版 vs 查表／Barrett 版 | 每種 10⁴ 次 × 5 輪 | ns/元素 p50 |
| PL kernel-only 時間（目前用 cosim 週期） | PL 內加 AXI Timer 或在 HLS top 加 free-running counter，記 ap_start→ap_done、第一個輸入 beat→TLAST；或 ILA | 每個 op 1000 次 | 週期 p50／最大；對照 cosim |
| DMA 視窗的 t0 與 δ（IRQ→完成→喚醒、MM2S 停頓） | 現有 `trace_dma_call.py` 加 kprobe 於 DMA callback 與 wait 返回；op3（add2，kernel 幾乎只有串流）掃不同大小 | 每點 200 次 | 中位數＋p90；線性擬合 |
| D1／D2 的每 byte 成本（目前 ESTIMATED/ASSUMED） | 新驅動候選：可快取 TX/RX＋`dma_sync_single_for_device/cpu`；u-dma-buf 版本；同一 trace 工具分段 | 64 KiB–1 MiB × 每點 200 次 | us 中位數、MB/s |
| 快取維護成本（dma_sync_* clean/invalidate） | kprobe 計時 `dma_sync_single_*`；或使用者空間 `DC CVAC` 迴圈 | 64 KiB–1 MiB | us/64 KiB |
| pack/unpack 的 bytes 線性度與 NEON 版本 | 微基準 1–16 個多項式 | 1000 次 | ns/beat |
| 新 HLS 運算（F、K、FDEC）的週期 | C/RTL cosim＋板上 AXI Timer | 每運算 100 次 | 週期 |
| 4 核同時負載下的效率 η_C、η_L（目前各由 1–3 個設定擬合） | 更長串流（≥ 100 批）＋每段 busy/work 時間序列，量填充／排空與佇列水位 | ≥ 3 次 | 穩態 wall/批 |
| 分電源功率（目前只有整板 INA260） | 外接功率計或分軌量測（PS／PL）；與吞吐量同步取樣 | ≥ 3 次 | W 平均、J/批 |
| 稀疏三元乘積（W2）與 NEON NTT（S5）實作 | 原型＋同上微基準；constant-time 檢查（dudect 類測試） | 1000 次 | p50、時間與秘密是否相關 |

## 10. 限制

- PL 的 kernel 時間只有 cosim 週期（MEASURED 於 RTL 模擬，不是板上 kernel-only）；新運算（F、K、FDEC）的週期是由現有運算組合的 ESTIMATED，沒有考慮資源（DSP 已用 60%、BRAM 43%）與時序（WNS 4 ps）是否放得下常駐金鑰（2×48 KiB NTT 形式約 21 個 BRAM36，或 3 個 URAM）。
- D1 是團隊估計＋本模型的快取維護估計；D2 是假設。兩者都需要新驅動候選與板測驗收才能變成事實；V3 驅動凍結不改。
- 吞吐量用「瓶頸界限 ÷ 校準效率 η」，η_C 只由 cpu-w4 一個設定擬合；兩個資源同時接近飽和時會低估（cpu-w2 −12%）。PL 重的切分其 ARM 空轉比例可能和 cpu-w4 不同。
- list scheduler 是非搶佔、HEFT 式的啟發式，不保證最佳 makespan（列舉的是切分，排程是啟發式）；ARM 的記憶體頻寬競爭只用固定的 c_be4、c_enc4 係數表示，PL DMA 與 ARM 搶 DDR 的效應沒有建模。
- DAG/STEP 模式假設軟體可以依資料相依並行（RNS 每特徵並行、CRT 切 4 塊），這是軟體改動，不是現狀；IMPL 才是現狀。DAG 模式沒有計入執行緒喚醒／同步（每次約 20–50 us），細粒度任務多的切分可能低估幾個百分點。
- 能耗模型的 PL 項辨識度低（實測只有 3 個 duty 水準），整板 INA260 也不能分開 CPU 與 FPGA。
- W1–W3 的 ARM 軟體改善（查表 residue、稀疏乘積、encode/decode 加速）全是 ESTIMATED/假設，只用來排優先順序；稀疏乘積需要 constant-time 審查；私鑰常駐 PL（S6）是安全取捨。
- keygen 是單次冷樣本（非 p50），由它推得的 residue 成本有雜訊；cpu-w3 keygen 誤差 −6% 即屬此類。
- 所有結論限於 N=4096、3 limbs、4 特徵、2048 筆/批的固定 profile；安全等級未宣稱（`security_bits=null`）。

## 11. 檔案與重現

- `partition_model.py`：模型本體（`--repo` 指定 repo 路徑，`--quick` 只跑 DAG-d1）。
- `model_results.json`：全部參數（含標記與來源）、校準、每情境前 5 名切分、Stone、break-even、敏感度。
- `graph_best.mmd`／`graph_best.dot`：EdgeFHE S4 optimal (DAG, depth 1): key=C; features=[3xF 1xA]; dec=O; NTT(s)=C ; edge latency 23.05 ms
- 讀取的量測檔：`results/2026-10-14-kv260/stage2_03/e2e_all.json`、`results/2026-10-14-kv260/stage2_02/e2e_all.json`、`results/2026-10-14-kv260/dma_trace_01/breakdown.json , results/2026-10-14-kv260/dma_trace_01/trace_report.json`、`release/RESULTS.json`

