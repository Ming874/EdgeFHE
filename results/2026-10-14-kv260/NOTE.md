# KV260 實板紀錄：2026-10-14（板上時間 2026-10-13 19:35–19:55 UTC，以及重新開機後 2026-10-14 02:25 起）

第一次把 V5 搬上實板。連線只用 USB 序列埠（COM8，115200，`ttyPS1`），板子沒有網路，所有檔案都經序列埠以 base64 傳送並核對 SHA-256。

## 環境

Ubuntu 22.04.4、核心 `5.15.0-1027-xilinx-zynqmp`（與歷史驗收環境相同）、GCC 11.4、Python 3.10.12、4 × Cortex-A53 1.333 GHz（`userspace` governor）、3.9 GB RAM、CMA 1000 MB、功耗感測器 `ina260_u14/power1`。板子沒有 RTC 同步，開機時鐘是 2023-11-22，先以 `sudo date` 設成 PC 的 UTC 時間。

## 結果

| 項目 | 結果 | 證據 |
|---|---|---|
| A 環境 | 只缺 cmake（不需要） | `kv260_environment_01.json` |
| 預編譯驅動身分 | `PREBUILT_V3_EXACT_ELF_METADATA_PASS target_match=True` | 板上執行 `tools/inspect_prebuilt.py --target` |
| C Device tree | 原檔與重新編譯都 `PASS_OVERLAY_BINARY_SEMANTICS_ONLY`；合併 `PASS_BOOT_FDT_FILE_MERGE_ONLY` | `kv260_preflight_01/*semantics.json`、`merged_report.json` |
| D 記憶體 | CMA 1000 MB，可用約 990 MB | `kv260_preflight_01/meminfo.txt` |
| E 安裝 app | 5 個檔案 `cmp` 相同 | — |
| F 載入 | 卸載 k26-starter-kits 後 `PASS_SLOT_EMPTY_BEFORE_LOAD`；載入後 `PASS_EXPLICIT_LOAD_MARKER_TARGET_SLOT_AND_FPGA_STATE` | `apps_*.txt`、`load_console.log` |
| G 驅動 | `version=ii1-guarded-v3`、`srcversion=6BDCA74AD872B2765D36882`，`DMA proxy ready: mode=dmaengine-irq` | `dmesg_after_insmod.txt` |
| 板上單元測試（GCC 11、aarch64） | `V5_TESTS_PASS tests=24`（5.3 秒） | `board_host_checks_01/v5_tests.log` |
| C ABI 對 V4 函式庫 | `V5_CAPI_PASS contexts=4 bit_exact=1` | 終端輸出 |
| **CPU ↔ 實際 FPGA 一致性** | **`V5_PARITY_PASS device=linux-dma cases=64 coefficients=786432 client_round=exact`** | `kv260_preflight_01/parity_board_01.log` |

一致性測試共 77 次 DMA 請求，沒有任何錯誤。每次 FPGA 呼叫約 5.02 ms（程式看到的 ioctl 時間），其中驅動自己量到的 DMA＋PL 時間約 0.52 ms；其餘約 4.5 ms 是系統呼叫、資料複製等額外負擔。

## 第一次階段 2 嘗試（中止）

`run_comparison.py --stage 2` 在板上重新編譯時，因使用者需要斷電而中止。中止時還在編譯，**沒有送出任何 DMA**（當時的輸出沒有可用結果，已移出 repo）。之後以 `sync && sudo poweroff` 正常關機，主控台顯示 `reboot: Power down`。

## 重新開機後：階段 2 完整 A/B 量測（`stage2_02/`）

重新開機後時鐘又回到 2023 年，先 `sudo date` 校正（校正後 sudo 會再問一次密碼），再重做 F、G（`kv260_preflight_02/`：`PASS_EXPLICIT_LOAD_MARKER_TARGET_SLOT_AND_FPGA_STATE`，驅動 version／srcversion 相同，`DMA proxy ready`）。裝置節點 `crw------- root`，所以在 `sudo bash -c 'exec 3<>/dev/edgefhe_rns3_n4096_dma && python3 tools/run_comparison.py --stage 2 --fd 3 --cpus 0-3 ...'` 裡執行。結果 **`V5_STAGE2_COMPLETE`**：板上重新編譯、單元測試、模擬一致性、**實板一致性第二次通過**、六種設定的三種模式、對照報表全部完成（`stage2_02/comparison.md`）。

| 項目 | 組 A 最佳（cpu-w4） | 組 B 最佳（fpga-overlap） | B ÷ A |
|---|---|---|---|
| 正確性（全 38,052 筆） | 六種設定密文 SHA-256 全部相同，最大 logit 誤差 3.93×10⁻⁷，0 筆不一致、1 筆在誤差界內 | 同左 | — |
| 單批（2048 筆）邊緣延遲 p50 | 60.45 ms（cpu-w1 86.76 ms） | 93.04 ms（fpga-serial 94.38 ms） | 0.65× |
| 加密步驟 p50 | 16.17 ms（1→4 核加速 2.37×，效率 0.59） | 46.00 ms | — |
| 19 批管線吞吐量 | 58,558 筆/秒 | 36,138 筆/秒 | 0.62× |
| 能源效率（INA260 整板功率約 3.4–3.7 W） | 15,743 筆/焦耳 | 10,081 筆/焦耳 | 0.64× |

每次 FPGA 乘法呼叫（程式看到的 ioctl）約 4.7 ms，其中驅動回報的 DMA＋PL 只有 0.54 ms，打包 0.07 ms、解包 0.18 ms。也就是說，用目前的 op2 介面，FPGA 本身算得快，時間大多花在每次呼叫的額外負擔；這和簡報「瓶頸是搬運，不是計算」一致。這 4.1 ms 的組成（例如 DMA 緩衝區的複製方式）還沒拆開量測，是組 B 下一步的目標。管線化（佇列容量 2）在 19 批時讓每種設定的吞吐量提高 1.7–2.1 倍；只有 1 批時沒有好處。

## 同日第二輪：平行編碼與每次呼叫拆解（04:21–04:27 UTC）

板子沒有重開（F、G 仍有效，驅動 srcversion 相同）。程式更新包 `kv260_code-only_20261014-121525.tar.gz` 經序列埠傳入，117 個檔的 SHA-256 全部相符。之後在同一個 `sudo bash -c` 裡依序跑下面兩項，前一項通過才跑下一項。

### 階段 2 再測：步驟 ② 用 4 個執行緒編碼（`stage2_03/`）

`run_comparison.py --stage 2 --fd 3 --cpus 0-3 --encode-workers 4` → **`V5_STAGE2_COMPLETE`**，共 3 分 52 秒：

- 板上 25 項單元測試全過（含新的平行編碼逐位元測試）。
- 實板一致性 `V5_PARITY_PASS device=linux-dma`。
- e2e 88 秒（上次 111 秒）。

六種設定的密文 SHA-256 相同；最大 logit 誤差 4.47×10⁻⁷，0 筆不一致、1 筆在誤差界內。

| 設定 | ② 編碼 p50（ms） | 邊緣延遲 p50（ms） |
|---|---|---|
| cpu-w1 | 31.40 → 8.33 | 86.76 → 63.77 |
| cpu-w4 | 31.36 → 8.31 | 60.45 → 37.40 |
| fpga-serial | 31.41 → 8.30 | 94.38 → 71.26 |
| fpga-overlap | 31.38 → 8.32 | 93.04 → 69.91 |

（箭頭左邊是 `stage2_02`，右邊是 `stage2_03`。）

- 編碼 4 核加速 3.78 倍。
- 19 批管線吞吐量：cpu-w4 從 58,558 升到 66,748 筆/秒；fpga-overlap 從 36,138 變成 36,080，幾乎不變，因為它的瓶頸是加密段的 FPGA 呼叫。
- 每焦耳筆數：A 從 15,743 升到 17,624，B 從 10,081 變成 10,034。
- 最佳 B ÷ 最佳 A：延遲從 0.65× 降到 0.54×，吞吐量從 0.62× 降到 0.54×。兩組共用的步驟變快後，B 每次呼叫的額外負擔顯得更大。

### 組 B：每次 FPGA 呼叫拆解（`dma_trace_01/`）

`trace_dma_call.py --fd 3`，用 ftrace function_graph 加 4 個 kprobe 標記。模組 srcversion、核心版本、`.ko` 的 SHA-256 與 kallsyms 間距全部相符。工作負載：v5_e2e latency、fpga-serial、暖機 2＋量測 10 輪。每次呼叫：不追蹤 4.677 ms、追蹤 4.735 ms，追蹤本身的成本 58 µs。109 次呼叫全部完整拆解，各段中位數：

| 段 | 做什麼 | µs | 占比 |
|---|---|---:|---:|
| 檢查係數 | 驅動逐筆檢查 canonical：從不經快取的 TX 緩衝區讀 32,768 次 32 位元，每次約 110 ns | 3,589 | 76.0% |
| DMA＋PL | 驅動 `elapsed_us` 量的那段 | 563 | 11.9% |
| 搬出 | `copy_to_user` 64 KiB，從不經快取的 RX 緩衝區讀，146 MB/s | 450 | 9.6% |
| 擦除 | 192 KiB＋64 KiB 寫入 | 35 | 0.7% |
| 搬入 | `copy_from_user` 128 KiB 寫入，4.7 GB/s | 28 | 0.9% |
| DMA 設定 | 暫存器、兩個描述子 | 18 | 0.4% |
| 清空輸出區 | 64 KiB 寫入 | 10 | 0.2% |
| 其他 | 進出 ioctl、鎖、請求檢查 | 10 | 0.3% |
| **合計**（`edgefhe_ioctl`） | | **4,705** | |

不經快取的記憶體，寫入很快（合併寫入），讀取卻要每次等 DRAM。多出的 4.1 ms 幾乎全是兩段讀取：檢查 3.59 ms＋搬出 0.45 ms。

事後檢查（`dma_trace_01/post_run_checks.txt`）：

- 追蹤狀態還原：`current_tracer` 是 nop；kprobe_events 0 行；過濾清單已清空；trace_clock、buffer_size_kb、max_graph_depth 都回到原值。
- 還原錯誤 0、遺失事件 0。
- dmesg 在驅動載入後沒有新訊息。

本輪 DMA 請求：階段 2 共 4,687 個，拆解 218 個，全部成功。

## 下次上板

FPGA app 已安裝在 `/lib/firmware/xilinx/edgefhe-ii1-n4096-kv260`，程式在 `~/Grad`。每次開機後要：校正時鐘 → 重做 F（載入）與 G（insmod）→ 在 `sudo bash -c '…'` 裡開 fd 執行。完整步驟見 [docs/BOARD_GUIDE.md](../../docs/BOARD_GUIDE.md)。

板子已在 2026-10-14 約 04:47 UTC（開機後 8,748 秒）以 `sync && sudo poweroff` 正常關機（主控台顯示 `reboot: Power down`）。

同日稍晚，電腦端把程式碼從 `EdgeFHE-KV260-GitHub/` 子資料夾攤平到 repo 根目錄；板子上仍是舊結構，程式在 `~/Grad/EdgeFHE-KV260-GitHub/`，本次的 `build/` 結果也在那裡。下次上板照 BOARD_GUIDE 步驟 3：先把舊的 `~/Grad` 改名保留（`~/Grad_old_<日期>`），再解開新的打包檔。上面紀錄裡的路徑都是當時的舊路徑。
