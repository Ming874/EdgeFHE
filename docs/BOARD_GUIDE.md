# KV260 上板指南 / KV260 board guide

操作員手冊。

- 本文件中的板端命令全部由開發板操作員執行（載入 FPGA、`insmod`、開裝置）；工具不自己開啟、重置或重試裝置，只用操作員交付、經 `/proc/self/fd` 確認是 `/dev/edgefhe_rns3_n4096_dma` 的 fd。
- 此版保留正式 V3 normal-path 功能；G07／G08 fault/reset 與完整 clean release 尚未接受（[VALIDATION.md](VALIDATION.md)），沒有自動重試／rollback／reset 或一鍵部署；失敗就停、保存證據，不自動重試、reload 或 reboot（[SECURITY.md](../SECURITY.md)）。
- 已接受的 II1/V3 app 正在使用時，不要只為試本手冊去卸載或替換；新的 boot、image、compiler、kernel 或 source 都要新證據。
- 2026-10-14 在歷史驗收環境走過 A、C–G（B 用預編譯模組，重開機後 F、G 再過一次）與階段 2（`V5_STAGE2_COMPLETE`），不是新環境的部署驗收；見 [COMPARISON.md](COMPARISON.md)、[README.md](../README.md)、[ARCHITECTURE.md](ARCHITECTURE.md)。

## 目錄 / Contents

0. [上板當天快速路徑](#0-上板當天快速路徑--board-day-quick-path)
1. [事前準備與環境](#1-事前準備與環境--prerequisites-and-environment)
2. [部署關卡 A 到 H](#2-部署關卡-a-到-h--deployment-gates-a-to-h)（含 [DMA 規則](#dma-規則與請求數)）
3. [執行階段 1 到 3](#3-執行階段-1-到-3--running-stages-1-to-3)
4. [疑難排解](#4-疑難排解--troubleshooting)
5. [版本區分](#5-版本區分--version-distinction)

## 0. 上板當天快速路徑 / Board-day quick path

依序複製貼上，失敗就停並保留輸出。帳號 `ubuntu`；`~/Grad` 就是 repo 根目錄（含 `data/`、`training/outputs/`）。〔2026-10-14 起〕程式碼不再放在 `~/Grad/EdgeFHE-KV260-GitHub/` 子資料夾；板子若還是舊結構，步驟 3 會先把舊的 `~/Grad` 改名保留。

**0 電腦（前一天）**：階段 0 收據 `V5_LOCAL_HOST_CHECKS_PASS`（`tools/run_v5_checks.py`，[DEVELOPMENT.md](DEVELOPMENT.md)）；repo 根目錄（`Grad`）執行 `python tools\pack_for_board.py --serial`（[1.2](#12-帶檔案上板)）。

**1 連線**：USB 線接上後出現兩個「USB Serial Port」，第一個是主控台（2026-10-14 為 COM8），115200 8N1；按 Enter，`kria login:` 以 `ubuntu` 登入。

**2 時鐘（每次開機）**：板子沒有時鐘同步，開機回到 2023 年（2026-10-14 為 2023-11-22），結果檔時間（如 `started_utc`）會錯；改時鐘後 `sudo` 會再問一次密碼。

```powershell
(Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss')
```

```bash
sudo date -u -s '2026-10-14 02:25:37'   # 換成電腦剛印出的 UTC 時間
date -u
```

**3 解開並核對**（第一次或程式更新時），預期 `BAD []`：

```bash
test -d ~/Grad/EdgeFHE-KV260-GitHub && mv ~/Grad ~/Grad_old_$(date -u +%Y%m%d)   # 只在舊結構時；舊結果留在 Grad_old_*
mkdir -p ~/Grad && tar -xzf ~/kv260_serial_*.tar.gz -C ~/Grad && cd ~/Grad
python3 -c "import json,hashlib,pathlib;m=json.load(open('BUNDLE_MANIFEST.json'))['files'];print('BAD',[n for n,h in m.items() if hashlib.sha256(pathlib.Path(n).read_bytes()).hexdigest()!=h])"
```

**4 關卡**：建本次開機的紀錄資料夾 `$P`；第一次（或換 image、kernel、boot 設定）接著做[第 2 節](#2-部署關卡-a-到-h--deployment-gates-a-to-h)的 A–E。F、G 每次開機都做：app 留在 `/lib/firmware/xilinx/edgefhe-ii1-n4096-kv260`，但開機時 FPGA 是預設 app、模組不自動載入。

```bash
cd ~/Grad && export PYTHONDONTWRITEBYTECODE=1
P=build/kv260_preflight_03       # 每次開機換新編號；已存在時 mkdir 會失敗
mkdir -p build && mkdir "$P"
```

F：先存 inventory。

```bash
sudo xmutil listapps | tee "$P/apps_before.txt"
lsmod | grep edgefhe || true     # 應無輸出；有就停（見 G）
```

`k26-starter-kits` 的 `Active_slot` 是 `0,` 時，確認沒人在用才卸載（全是 `-1` 就跳過 `unloadapp`）；預期 `PASS_SLOT_EMPTY_BEFORE_LOAD`，通過才載入：

```bash
sudo xmutil unloadapp | tee "$P/unload.log"     # 2026-10-14：remove from slot 0 returns: 0 (Ok)
sudo xmutil listapps | tee "$P/apps_empty.txt"
python3 software/kv260/semantic_load_gate.py empty --apps "$P/apps_empty.txt"
```

```bash
(
  set -e -o pipefail
  LOAD_CODE=0
  sudo xmutil loadapp edgefhe-ii1-n4096-kv260 > "$P/load_console.log" 2>&1 || LOAD_CODE=$?
  cat "$P/load_console.log"
  sudo xmutil listapps > "$P/apps_loaded.txt"
  cat /sys/class/fpga_manager/fpga0/state > "$P/fpga_state.txt"
  python3 software/kv260/semantic_load_gate.py loaded --exit-code "$LOAD_CODE" --console "$P/load_console.log" --apps "$P/apps_loaded.txt" --fpga-state "$P/fpga_state.txt"
)
```

G：最後一行是 `PASS_EXPLICIT_LOAD_MARKER_TARGET_SLOT_AND_FPGA_STATE` 才載入（其他 kernel 用 B-2 的 `.ko`）；`grep` 應印出 `xilinx-vdma a0000000.dma: Xilinx AXI DMA Engine Driver Probed!!` 與 `edgefhe-rns3-n4096-dma-proxy a0010000.edgefhe-ii1-proxy: EdgeFHE RNS3 parallel N=4096 DMA proxy ready: mode=dmaengine-irq ii1-guarded-v3 tx=... rx=...`：

```bash
sudo insmod firmware/prebuilt/ubuntu-22.04-5.15.0-1027-xilinx-zynqmp/edgefhe_rns3_n4096_dma_proxy.ko
cat /sys/module/edgefhe_rns3_n4096_dma_proxy/version       # ii1-guarded-v3
cat /sys/module/edgefhe_rns3_n4096_dma_proxy/srcversion    # 6BDCA74AD872B2765D36882
ls -l /dev/edgefhe_rns3_n4096_dma                          # crw------- 1 root root 10, 120 ...
ls -l /sys/bus/platform/drivers/edgefhe-rns3-n4096-dma-proxy/   # a0010000.edgefhe-ii1-proxy bind module uevent unbind
sudo dmesg | tail -n 80 > "$P/dmesg_after_insmod.txt"
grep -E 'Engine Driver Probed|DMA proxy ready' "$P/dmesg_after_insmod.txt"
cat /proc/interrupts > "$P/interrupts_before.txt"
```

**5 量測**（一次一個階段，期間板上不做別的事）。階段 1 只用 CPU，不需要 F、G、sudo：

```bash
cd ~/Grad
nohup bash -c 'PYTHONDONTWRITEBYTECODE=1 python3 tools/run_comparison.py --stage 1 --cpus 0-3 \
    --output build/comparison/stage1_01 --label "kv260-$(date -u +%F)" \
    --data data/cdc_diabetes/splits/test.csv --model training/outputs/model.json \
    --preprocess training/outputs/edge_preprocess.json; echo STAGE1_EXIT=$?' > ~/stage1_01.console 2>&1 &
```

階段 2 需要本次 F、G 通過，裝置只有 root 能開（[3.3](#33-階段-2)）：

```bash
sudo -v      # 先輸入密碼，背景的 sudo -n 才不會卡住
nohup sudo -n bash -c 'cd /home/ubuntu/Grad &&
  exec 3<>/dev/edgefhe_rns3_n4096_dma &&
  PYTHONDONTWRITEBYTECODE=1 python3 tools/run_comparison.py --stage 2 --fd 3 --cpus 0-3 \
    --output build/comparison/stage2_01 --label "kv260-$(date -u +%F)" \
    --data data/cdc_diabetes/splits/test.csv --model training/outputs/model.json \
    --preprocess training/outputs/edge_preprocess.json;
  echo STAGE2_EXIT=$?; exec 3>&-; chown -R ubuntu:ubuntu build/comparison' > ~/stage2_01.console 2>&1 &
```

```bash
tail -n 4 ~/stage2_01.console     # 看進度，可重打
pgrep -af run_comparison          # 沒有輸出＝已結束
```

完成時最後兩行是 `V5_STAGE2_COMPLETE output=...`、`STAGE2_EXIT=0`（階段 1：`V5_STAGE1_COMPLETE`、`STAGE1_EXIT=0`）；重跑換新名稱（`stage2_02`）。

**6 帶回**（沒跑的刪掉；最上層是 `stage1_01/`、`stage2_01/`、`kv260_preflight_NN/` 與 console 檔；電腦端見 [3.5](#35-帶回與合併)）：

```bash
R=~/Grad
tar -czf ~/board_results_01.tgz --exclude=bin --exclude=obj \
    -C "$R/build/comparison" stage1_01 stage2_01 -C "$R/build" "$(basename "$P")" \
    -C ~ stage1_01.console stage2_01.console
sha256sum ~/board_results_01.tgz
```

**7 關機**：`pgrep -af run_comparison` 無輸出、結果已帶回並核對後執行，等 `reboot: Power down` 才拔電源；下次開機從步驟 2 開始。

```bash
sync && sudo poweroff
```

## 1. 事前準備與環境 / Prerequisites and environment

### 1.1 實測環境與套件

- 2026-10-14：Ubuntu 22.04.4、`5.15.0-1027-xilinx-zynqmp`（同歷史接受環境；新版本不自動繼承其 probe／DMA PASS）、GCC 11.4.0、GNU Make 4.3、Python 3.10.12；4 × Cortex-A53 1.333 GHz（`userspace` governor）、3.9 GB RAM、CMA 1000 MB、hwmon `ina260_u14/power1`（整板或某一路電源，非單獨 CPU 或 FPGA）。有 `g++`、`make`、`dtc`、`fdtoverlay`、`xmutil`、`modinfo`、`openssl`、kernel headers（`/usr/src/linux-headers-5.15.0-1027-xilinx-zynqmp`，沒用到）；沒有 `cmake`（不需要）與網路（檔案經序列埠傳，階段 3 沒做）。
- 主控台 `ttyPS1` 115200、主機名稱 `kria`；COM 埠同時只能一個程式開（如 PuTTY）；密碼只在主控台輸入，不寫進檔案。序列埠工具在 [tools/kv260/](../tools/kv260/README.md)：`serial.ps1` 送指令並等結束，`send_file.ps1`／`receive_file.ps1` 以 base64 傳收檔並比對 SHA-256，約 11.3 KiB/s（1.9 MB 約 224 秒）。
- 套件需要網路（2026-10-14 除了 cmake 都已裝好）。階段 1、2 只要 `g++`、`ar`、`python3`（`build-essential python3`）；`cmake` 只有 B-2 的選做 CTest 用；階段 3 的 `make_demo_tls.py` 要電腦有 `openssl`。headers 只在該 image 的 vendor repository 提供**當前 kernel** 版本時才裝。`xmutil`／FPGA manager／clock／DMA provider 應由相容的 Kria image 提供；缺少時先確認 image 與 vendor packages，不能假設一般 Ubuntu desktop 就可上板。

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake python3 git device-tree-compiler openssl kmod
apt-cache policy "linux-headers-$(uname -r)"
```

```bash
sudo apt-get install "linux-headers-$(uname -r)"      # 只在上面查得到當前 kernel 版本時
test -f "/lib/modules/$(uname -r)/build/Makefile"
```

### 1.2 帶檔案上板

階段指令從 repo 根目錄（板上 `~/Grad`）執行，用 repo 裡的 `data/`、`training/`；資料放在別處時不依賴原作者的 `$HOME/NCUE_Project` 或 Windows `D:`，改 `--data`、`--model`、`--preprocess`。打包工具是 `tools/pack_for_board.py`，在 repo 根目錄執行；輸出 `build/kv260_<模式>_<時間>.tar.gz`，附 `BUNDLE_MANIFEST.json`（SHA-256），TLS 金鑰永不打包。

| 模式 | 大小 | 內容 | 板上核對 |
|---|---|---|---|
| 不加參數（USB 或 scp） | 約 7 MB | 整份 repo 清單（同 `release/SHA256SUMS`：含 docs、results、dashboard 原始碼、模型與前處理）、CDC 測試集（38,052 筆） | 解到空的 `~/Grad` 後 `python3 tools/verify_repository.py` → `REPOSITORY_MANIFEST_AND_DOCUMENTS_PASS`（打包前已在電腦跑過） |
| `--serial`（約 4 分鐘；2026-10-14 用它） | 約 2 MB | `software/`、`tools/`、`firmware/`、本文件、COMPARISON.md、資料、模型 | 步驟 3 |
| `--code-only` | 約 200 KB | `software/`、`tools/` | 步驟 3 |

序列埠版沒有 `release/`、`evidence/`、`hardware/`、`validation/`：板上不能跑 `verify_repository.py`、`build_driver.py`（要 `release/VERSION.json`）與 CMake／CTest，A 的環境清單、B-1、C–G、階段 1、2 可以；打包前先在電腦跑 `verify_repository.py`。

## 2. 部署關卡 A 到 H / Deployment gates A to H

全部通過才送第一個 DMA；每段保存 command、environment、source／artifact SHA、原始 logs、explicit PASS 與失敗。A–D：環境、驅動模組、硬體描述檔、DMA 記憶體；E：安裝 FPGA app（一次）；F、G：載入 app 與驅動（每次開機，指令在步驟 4）；H：兩機 TLS 邊界（只有階段 3）。C、D 用步驟 4 的 `$P`。

### A. 環境與來源確認

```bash
uname -m
uname -r
cat /etc/os-release
python3 tools/verify_repository.py
python3 tools/check_environment.py --target kv260 --output build/kv260_environment_01.json
```

- 預期 `aarch64`、`REPOSITORY_MANIFEST_AND_DOCUMENTS_PASS`（序列埠版改用步驟 3）、`ENVIRONMENT_PREREQUISITES_PASS`。JSON 僅 inventory，沒有 `/dev/edgefhe*` open／DMA／network／sysfs control writes（`board_device_opens`、`network_connections`、`system_configuration_writes` 都是 0）。
- 沒有 cmake 時印 `ENVIRONMENT_PREREQUISITES_INCOMPLETE`、`MISSING cmake_available`（exit 1，2026-10-14 就是這樣）；V5 不經 CMake（`tools/v5_sources.py` 直接用 g++），唯一的 MISSING 是它就可繼續，其他 MISSING 先處理。
- 不能以其他 kernel 的 `.ko` 補上：`5.15.0-1027-xilinx-zynqmp` 用 B-1（不需 headers），其他 kernel 一定要 matching headers 走 B-2，沒有就停。

### B. 編譯或採用模組

**B-1 kernel 完全相同：已接受的 V3 預編譯模組**

```bash
python3 tools/inspect_prebuilt.py --target
```

預期 `PREBUILT_V3_EXACT_ELF_METADATA_PASS target_match=True no_load no_DMA` 與 JSON（`ii1-guarded-v3`、kernel、vermagic、module_sha256、`"live_kernel_ABI_acceptance": false`）。只讀檔、從不 insmod／modprobe：核對 KO 的 SHA-256、aarch64 ELF、`.modinfo` 的 version／vermagic／srcversion 與 V3 原始碼 SHA-256；`--target` 再要求 Linux aarch64 且 kernel release 相同。身分以 [identity.json](../firmware/prebuilt/ubuntu-22.04-5.15.0-1027-xilinx-zynqmp/identity.json) 為準。限制：kernel 名稱／vermagic 相同是必要條件，不證明不同 kernel build 的 ABI 相同；不要與另一個 V3 或 V4 模組同時載入；C–G 照做。

**B-2 其他 kernel 或自行編譯（完整 repo＋matching headers）**

```bash
cmake -S . -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native --parallel 2
ctest --test-dir build/native --output-on-failure
python3 tools/build_driver.py --output build/driver_native_01
cat build/driver_native_01/module_identity.json
```

- 前三行是選做的 CPU 檢查（cmake ≥ 3.20）：9 項 CPU-only CTest 全過（V3 三、V4 二、V5 四）。
- 預期 `DRIVER_NATIVE_COMPILE_METADATA_PASS no_insmod no_DMA; live identity/probe acceptance still required`。script 將 exact C／Makefile 複製到新 build output，保留 compile.log、KO SHA／version／srcversion／vermagic；不污染來源、不 insmod。vermagic 必須匹配 `uname -r`，version 為 `ii1-guarded-v3`；srcversion／KO SHA 是本次編譯身份，不要求等於歷史 binary，但 source SHA 必須相同（對照 `release/VERSION.json`）。只能在 aarch64（拒絕 x86）；`--output` 要新且不在 `software/` 下。

### C. Device Tree 檔案驗證

```bash
O=firmware/ii1_n4096/edgefhe-ii1-n4096-kv260
python3 software/kv260/fdt_contract.py "$O.dtbo" --output "$P/overlay_semantics.json"
dtc -@ -I dts -O dtb -o "$P/recompiled.dtbo" "$O.dts"
python3 software/kv260/fdt_contract.py "$P/recompiled.dtbo" --output "$P/recompiled_semantics.json"
sudo cat /sys/firmware/fdt > "$P/base.dtb"
fdtoverlay -i "$P/base.dtb" -o "$P/merged.dtb" "$O.dtbo"
python3 software/kv260/verify_merged_fdt.py "$P/base.dtb" "$P/merged.dtb" "$P/merged_report.json"
```

預期 `PASS_OVERLAY_BINARY_SEMANTICS_ONLY`（原始與重編各一）、`PASS_BOOT_FDT_FILE_MERGE_ONLY`；只檢查檔案；`--output`／report 已存在會停，換 `_02`。Base FDT 需有 amba／gic／zynqmp_clk／zynqmp_reset symbols、對應 provider／地址可用；不相容就停止另立 port；不能自行改 phandle／clock ID 或以 fixed-clock 冒充 PS clock。2026-10-14：amba `/axi`、gic `/axi/interrupt-controller@f9010000`、zynqmp_clk／zynqmp_reset 是 `/firmware/zynqmp-firmware/` 下的 `clock-controller`／`reset-controller`；`dtc` 加了 `-q`（不印 warning）。

### D. 記憶體與 cache 要求

```bash
{ grep -E 'MemTotal|CmaTotal|CmaFree' /proc/meminfo; cat /proc/cmdline; } | tee "$P/meminfo.txt"
```

- 2026-10-14：MemTotal 4005128 kB、CmaTotal 1024000 kB（`cma=1000M`）、CmaFree 1014300 kB（約 990 MB）。Coherent TX 196608 B／RX 65536 B 不代表全系統只需 256 KiB（還有其他 DMA、fragmentation、kernel reserve）；不能由 CmaFree 差值歸因 driver memory leak。不足時依該 image 的 boot 方法調整並重開機驗證，不套用未審查的 generic boot-file 修改。
- Driver 用 `dma_set_mask_and_coherent(...32)`、`dma_alloc_coherent`、`dma_wmb/rmb`，檢查 DMA address 16-byte alignment／不重疊／末端 ≤ 0x80000000（實測 tx=0x361c0000、rx=0x361f0000）。PS userspace buffer 是 copy-in/out，malloc buffer 不是 DMA address；不能用 userspace 虛擬位址當 AXI address，也不能用 `/dev/mem` 搶 Linux DMAEngine 的 registers（[Linux DMA API](https://docs.kernel.org/core-api/dma-api-howto.html)）。

### E. 安裝 frozen firmware

**只有 A–D 通過、且操作者確認當次 session 可進入新部署才做**；現有 II1/V3 正在使用時保留它並停止。只建立不存在的 app directory，不覆寫；`cmp` 無輸出＝相同（2026-10-14 五檔相同）：

```bash
(
  set -e -o pipefail
  APP_SOURCE="$PWD/firmware/ii1_n4096"
  APP_DEST=/lib/firmware/xilinx/edgefhe-ii1-n4096-kv260
  test ! -e "$APP_DEST"
  sudo install -d -m 0755 "$APP_DEST"
  for name in edgefhe-ii1-n4096-kv260.bit.bin edgefhe-ii1-n4096-kv260.dtbo shell.json edgefhe-runtime-contract.txt runtime_contract_v3.json; do
    sudo install -m 0644 "$APP_SOURCE/$name" "$APP_DEST/$name"
    cmp "$APP_SOURCE/$name" "$APP_DEST/$name"
  done
)
```

已存在時不能刪除或覆寫後硬闖，先逐檔確認：

```bash
for f in firmware/ii1_n4096/{edgefhe-ii1-n4096-kv260.bit.bin,edgefhe-ii1-n4096-kv260.dtbo,shell.json,edgefhe-runtime-contract.txt,runtime_contract_v3.json}; do cmp "$f" "/lib/firmware/xilinx/edgefhe-ii1-n4096-kv260/${f##*/}" && echo "same: $f"; done
```

v2 runtime contract 保留原檔，v3 supplement 另存；不能把舊 txt 改名義版本以跳過身份 gate。

### F. 空 slot 與明確 load 語意

- 必須是確認沒有在途 DMA、沒有使用中的 proxy／fd 的新驗證時段；G07/G08 尚有 fault/quiescence 缺口，未知或失敗的 session 不走正常 unload 假裝已恢復。
- slot 被占用時先審查 app 與 idle 狀態，可正常卸載才 `sudo xmutil unloadapp` 並重列 inventory；曾觀察 unload 後 starter app 仍 active，不能只看 return 0（2026-10-14 兩次都是 `k26-starter-kits`）。`empty` gate 不過就停，沒有自動第二次 unload／retry；它也要求清單有 `edgefhe-ii1-n4096-kv260`（E 已做）。
- `loaded` gate 要求 exit 0、輸出無 error／failed／failure、有 `edgefhe-ii1-n4096-kv260: loaded to slot 0`、它是唯一 active slot 0 且為 `XRT_FLAT`、FPGA state `operating`。CLI exit 0 與 operating **單獨都不足夠**；load failure 就保存 evidence、停止 module／DMA，不自動 retry／rollback。工具參考 [AMD/Xilinx xmutil](https://github.com/Xilinx/xmutil)。

### G. Module probe 與 live gate

- 只有 F 通過、同次 session 沒有舊 edgefhe module／binding，才載入 B 的模組（B-2 改成 `sudo insmod build/driver_native_01/edgefhe_rns3_n4096_dma_proxy.ko`）；version／srcversion 對照 identity.json 或 `module_identity.json`。
- 需要 probe-ready、correct compatible／addresses／clock、兩個 DMA channels、正確 provider ownership、device 與 binding；**insmod return 0 但沒有 device 不算通過**。device name `edgefhe_rns3_n4096_dma`；sysfs driver 名稱以 source 的 `.driver.name` 為準（`edgefhe-rns3-n4096-dma-proxy`）。2026-10-14 的預期輸出在步驟 4 的註解；`Engine Driver Probed!!` 在 F 載入時就出現，裝置只有 root 能開。
- 名目 100 MHz、clock provider 呈現 99,999,999 Hz 是 1 Hz rounding，不需改 bitstream；仍須確認 driver guard、routed timing、live provider。`"auto_load_supported": false`：每次開機重做 F、G。
- V3 時期 native runners（`software/board_tests/` 的 `soak_runner`、`concurrent_runner`、`usercopy_runner`、`paired_public_vectors`，`software/reference/` 的 `mixed_runner`）沒有適用任意新 clone／boot 的完整 live DMA controller，不要直接執行 `--run`／`--paired`。要用需先建立並 review 本次 source／bit／KO／live-FDT／boot／IRQ／history gate，凍結操作數量、canonical input、timeout 與失敗停止規則再交由操作者跑；歷史一次性 scripts 的固定 paths／history 不可重播成通用部署工具。

### DMA 規則與請求數

送出 DMA 前要先確定請求數與逾時。下列規則寫死在 V5 程式（`software/native_v5/backend/fpga/`、`edge/session.cpp`）與驅動，`stage_report.json` 的 `dma_policy` 也有記：

- 只用 op2（完整乘法）：每個請求輸入 2 × 4096 beats（128 KiB）、輸出 4096 beats（64 KiB），每 beat 16 bytes＝三個 RNS 分量＋0（凍結的 V3 ABI）。
- canonical input：係數小於各自質數、padding 為 0，程式與驅動各檢查一次（否則 `-EINVAL`）；資料為 CDC 測試集（SHA-256 記在結果）＋作業系統亂數，`v5_parity` 的 64 組乘積用公開 QA 種子。
- 每個請求逾時 5000 ms（驅動上限 30000 ms）；同時只有 1 個在途（session 鎖＋驅動鎖），`fpga-overlap` 只重疊 CPU 端打包／解包。
- 第一個失敗就停：不重試（連 EINTR 也不重送）、不重置；`FpgaBackend` 鎖住，管線其他階段也不能再送。DMA 逾時或傳輸錯誤後驅動 fail-closed，之後每個請求回 `-EIO`，直到重新載入 PL（驅動註解：Fail closed until PL reload）。例外：`fpga-overlap` 檢查第 k 個輸出時第 k＋1 個已送出，「輸出內容錯誤」最多多送 1 個；`fpga-serial` 在第 k 個就停。

請求數（CDC 38,052 筆＝19 批，每批最多 2048 筆；預設設定）：**每批**（不是每筆）9 個＝加密 8（4 特徵 × 2 分量）＋解密 1，每個 session 另 1 個 keygen。

| 命令 | 請求數 | 算法 |
|---|---|---|
| `v5_parity --fd 3 --cases 64` | 77 | 64 組乘積（每次 8 組）＋keygen 1＋加密 8＋解密 4 |
| 階段 2 的 `v5_e2e` | 4,610 | `fpga-serial`、`fpga-overlap` 各 2,305＝1＋9 ×（正確性 19＋延遲 33＋吞吐量 204 批） |
| `run_comparison.py --stage 2` | 4,687 | 77＋4,610（模擬一致性不碰裝置）；`--encode-workers` 不改請求數 |
| `trace_dma_call.py`（[3.3.1](#331-組-b拆開每次-fpga-呼叫)） | 218 | 基準與追蹤各 109＝keygen 1＋9 ×（暖機 2＋量測 10 輪） |
| 階段 3 `edge_client.py --backend fpga` | 172 | 1＋9 × 19 |
| 階段 1、`--simulated`、`fpga-simulated` | 0 | 不碰實體裝置 |

延遲 33＝暖機 3＋量測 30 輪，每輪 1 批；吞吐量 204＝重複 3 × 容量 2 種（0、2）× 串流 1＋2＋4＋8＋19＝34 批。改了 `--limit`、`--rounds`、`--warmup`、`--repeats`、`--capacity`、`--stream-batches` 就重算、先寫下再跑。`stage2_02` 結果一致（19 批吞吐量列 171 個、延遲 30 輪 270 個）。失敗時保存 console、`parity_board.log`／`e2e.log`、`stage_report.json`、`sudo dmesg | tail -n 80`，這次 session 不再送 DMA；是否關機由操作員決定，重開是新 session（從時鐘、F、G 重來，在結果 NOTE 記下）。G07／G08 未驗收，unload 或 rmmod 不算「已恢復」。

### H. TLS service 與公開 demo

- **V3 公開 demo（`software/ckks/`）**：`join_config.example.json` 只含欄位模板，沒有可用 secrets。Client／server 必須共用 protocol／server_id／token，server certificate SAN 包含實際服務位址，DER SHA pin 需與 certificate 相符；private key／token 放 `local/`。`python3 software/ckks/pc_server.py --help` 可查看 `--config`／`--private-dir`／`--evidence`／`--bind`（都必填，port 固定 18086），此檔不自動建立 network service。這是 V3 的 server；階段 3 用 V4 的 `software/native_ckks/pc_server.py`，參數不同。
- 目前 V3 service 是 pinned TLS + token，不是 mTLS，公開 fixtures 位於兩端。要做真實私密輸入或 per-device mTLS，需另立 candidate、拆開 models 與 private input／oracle、修改 ready／request schema，再驗正向／負向認證、payload／logs 資料隔離、結果正確性；不能以重新啟動公開 demo 作該驗收。
- **V4／V5（階段 3）**：V4 候選（[software/native_ckks](../software/native_ckks/README.md)）就是另立的那條路（client certificate mTLS、可設定 port、無 fixtures），本機測試通過，ARM、PL、實體兩機未驗收。`make_demo_tls.py` 產生短效憑證（預設 30 天、新的 256-bit token、pin server 憑證 DER SHA-256、SAN 列出每個 `--server-host`）；server 只收密文，audit 只記事件種類與數量。這是功能展示，不是安全驗收：稀疏取樣器、不宣稱安全位元數，沒有 constant-time、結果完整性或跨重啟防重放的證明。

本手冊的步驟 2026-10-14 已在歷史環境（同一個 image 與 kernel）實際走過；換了環境要自己重新取得證據。

## 3. 執行階段 1 到 3 / Running stages 1 to 3

目的與結果讀法見 [COMPARISON.md](COMPARISON.md)；階段 1、2 指令在[步驟 5](#0-上板當天快速路徑--board-day-quick-path)。

### 3.1 共同規則

- `tools/run_comparison.py` 依序：環境清單 → 編譯（`bin/`，兩組同一組旗標）→ `V5_TESTS_PASS` → `V5_PARITY_PASS device=simulated-dma` →（階段 2）`V5_PARITY_PASS device=linux-dma` → `V5_E2E_PASS` → `COMPARISON_WRITTEN`；console 每步印 `PASS <步驟>`（依序 `unit_tests`、`parity_simulated`、`parity_board`（階段 2）、`e2e`、`compare`），失敗印 `FAIL <步驟>` 與 log 尾段後停止，實板一致性不過就不量 FPGA。
- 輸出：每步 `.log`、`bin/build.log`、`environment.json`、`e2e_cpu.json` 或 `e2e_all.json`、`comparison.md`／`.json`、`stage_report.json`（狀態、輸入與原始碼、執行檔 SHA-256、設定、`dma_policy`、每步秒數）。輸出一律放 repo 的 `build/` 或 `local/`，不放進 `software/`；舊輸出是證據，不刪不改。
- `--output` 在編譯之前就建立，失敗後重跑（包括加 `--no-werror`）都要新名稱（`_02`、`_03`）。
- 用預設設定（`--rounds 30 --warmup 3 --repeats 3 --capacity 0,2 --stream-batches 1,2,4,8,all`、`--cxxflags -O2`）；兩階段都 `--cpus 0-3`、不改 governor，才能合併；`--limit N` 只適合試跑，筆數不同不能合併。`--bin <先前輸出>/bin` 重用同一份原始碼的執行檔（省約 2 分鐘，兩階段同一執行檔，記在 `binaries_reused_from`）。

### 3.2 階段 1

只用 CPU（`cpu-w1`～`cpu-w4`），四種設定的密文 SHA-256 要相同；console 第一行 `building V5 tools ...`。還沒在板上單獨跑過：工作是階段 2 的子集，比它（約 4 分鐘）短。階段 2 也量 `cpu-w1`～`cpu-w4`，時間不夠時只跑階段 2 也有完整 A/B 報表（2026-10-14 就是這樣）。

### 3.3 階段 2

六種設定（四種 CPU＋`fpga-serial`、`fpga-overlap`）；前提是本次 F、G 通過，並讀過 [DMA 規則](#dma-規則與請求數)。

- 裝置是 `crw------- root`，一般使用者 `exec 3<>/dev/edgefhe_rns3_n4096_dma` 會 `Permission denied`：要在同一個 `sudo bash -c` 裡開 fd、執行、關 fd，再 `chown -R ubuntu:ubuntu`。sudo 會清掉環境變數，所以裡面再寫 `PYTHONDONTWRITEBYTECODE=1`；`sudo -n` 沒有快取的密碼就失敗；`cd` 用絕對路徑（sudo 裡的 `~` 不是 `/home/ubuntu`）；console 檔由你的 shell 建立、屬於 `ubuntu`。
- `stage2_02` 實測約 4 分 12 秒（編譯約 2 分鐘、單元測試 5.4 秒、模擬一致性 0.5 秒、實板一致性 1.0 秒、e2e 111 秒、報表 0.3 秒）；console 第一行 `nohup: ignoring input`。同一 session 加 `--encode-workers 4` 的 `stage2_03` 是 3 分 52 秒（e2e 88 秒）。
- 電腦上可用 `--stage 2 --simulated`（不加 `--fd`）預演，結果 `V5_STAGE2_COMPLETE_SIMULATED_DEVICE` 是 CPU 時間，不算通過。

選做：先用 77 個請求（約 1 秒）確認 DMA 路徑（2026-10-14 第一次開機的做法），不通過就停。預期 `device=linux-dma backend=fpga-linux-dma-overlap requests=77 ...`、`V5_PARITY_PASS device=linux-dma cases=64 coefficients=786432 client_round=exact`、`parity_exit=0`。跑完階段 2 建議另存 IRQ 與 dmesg，和 `interrupts_before.txt` 對照：

```bash
python3 tools/v5_sources.py --output build/v5       # 約 2 分鐘；最後一行 V5_BUILD_DONE
sudo bash -c 'exec 3<>/dev/edgefhe_rns3_n4096_dma && ./build/v5/v5_parity --fd 3 --cases 64; echo parity_exit=$?; exec 3>&-' 2>&1 | tee "$P/parity_board_01.log"
# 階段 2 跑完後：
cat /proc/interrupts > "$P/interrupts_after.txt"
sudo dmesg | tail -n 80 > "$P/dmesg_after_stage2.txt"
```

#### 3.3.1 組 B：拆開每次 FPGA 呼叫

選做。`tools/trace_dma_call.py` 用核心內建的 ftrace 把一次 op2 呼叫拆成幾段：搬入 128 KiB、逐筆檢查係數、清空輸出區、DMA 設定、DMA＋PL、搬出 64 KiB、擦除緩衝區。完全不改凍結的 V3 驅動。

- **怎麼量**：function_graph 只掛 12 個函式，再加上 DMA 引擎驅動的 `xilinx_dma_*`，從 `edgefhe_ioctl` 往下追三層；四個 kprobe 事件放在 `edgefhe_submit_transfer` 裡沒有函式呼叫可分隔的位置。偏移量取自預編譯模組的反組譯，只有載入模組的 srcversion、核心版本、`.ko` 的 SHA-256 與 kallsyms 間距全部相符時才用，否則退回只看函式的粗分。
- **工作負載**：`v5_e2e --modes latency --backends fpga-serial`，先不追蹤跑一次當基準，再追蹤跑一次；請求數見上表。
- **安全**：追蹤設定先存、事後還原（失敗也還原）；只新增、移除自己的 kprobe 事件（群組 `edgefhe_trace`）；ftrace 已有人在用時拒絕執行。裝置照樣由操作員開，不重試、不重置，第一個失敗就停。
- **前提**：本次 F、G 通過；`--bin` 指向同一份原始碼編好的執行檔，例如階段 2 輸出的 `bin/`。

```bash
sudo -v
nohup sudo -n bash -c 'cd /home/ubuntu/Grad &&
  exec 3<>/dev/edgefhe_rns3_n4096_dma &&
  PYTHONDONTWRITEBYTECODE=1 python3 tools/trace_dma_call.py --fd 3 --bin build/comparison/stage2_01/bin --cpus 0-3 \
    --data data/cdc_diabetes/splits/test.csv --model training/outputs/model.json \
    --preprocess training/outputs/edge_preprocess.json --output build/trace/dma_trace_01;
  echo TRACE_EXIT=$?; exec 3>&-; chown -R ubuntu:ubuntu build/trace' > ~/dma_trace_01.console 2>&1 &
```

完成時印出各段表格、`DMA_TRACE_COMPLETE mode=fine calls=N`、`TRACE_EXIT=0`。輸出：

| 檔案 | 內容 |
|---|---|
| `trace.txt` | 原始追蹤 |
| `breakdown.md`／`.json` | 各段中位數、p10–p90、占比、MB/s |
| `workload_baseline.*`、`workload_traced.*` | 兩次工作負載；用來算每次呼叫的追蹤成本 |
| `trace_report.json` | 模組身分、kprobe 定義、追蹤前狀態、還原錯誤 |

電腦上可重新分析：`python tools/trace_dma_call.py --parse <資料夾>/trace.txt`；解析器自測：`--self-test`。

2026-10-14 實測（接在 `stage2_03` 後，同一個 `sudo bash -c`，`--bin build/comparison/stage2_03/bin`）：

- 共 7 秒（基準與追蹤各 2 秒），`DMA_TRACE_COMPLETE mode=fine calls=109`，還原錯誤 0、遺失事件 0。
- 事後以 root 唯讀確認：`current_tracer` 是 nop、kprobe_events 0 行、過濾清單清空，dmesg 沒有新訊息。
- 結果與解讀見 [COMPARISON.md](COMPARISON.md)「組 B：每次 FPGA 呼叫拆解」。

### 3.4 階段 3

板子加密、經網路把密文送到電腦計算、結果送回板子解密；需要同一個網路。2026-10-14 沒有網路，只在電腦演練過（127.0.0.1 雙向 TLS 的 `software/native_v5/tests/two_machine_loopback.py`，及分開產生的憑證跑 CDC 資料）。

電腦端（Linux、WSL 或 Linux 虛擬機，上板前做；IP 換成板子連得到的電腦 IP，`--server-host` 可重複）。預期 `DEMO_TLS_READY ...`；第三行選做，預期 `V5_TWO_MACHINE_LOOPBACK_PASS clients=2 mtls=True board=0`。再把 `local/demo_tls_01/client/` 複製到板子的 `~/Grad/local/demo_client/`（scp 或序列埠工具）：裡面有 client 私鑰，只放 `local/`，不進 Git 或打包檔，`ca/` 只留電腦。啟動服務（Ctrl-C 結束）預期 `DYNAMIC_CIPHERTEXT_SERVER_READY port=18087`，`--audit` 要新檔案：

```bash
python3 tools/v5_sources.py --output build/v5
python3 software/native_v5/python/make_demo_tls.py --server-host 192.168.1.20 --output local/demo_tls_01
python3 software/native_v5/tests/two_machine_loopback.py --library build/v5/libedgefhe_v5_capi.so --output build/v5_loopback_01
PYTHONDONTWRITEBYTECODE=1 python3 software/native_ckks/pc_server.py \
    --config local/demo_tls_01/server/config.json --private-dir local/demo_tls_01/server \
    --model training/outputs/model.json --library build/v5/libedgefhe_v5_capi.so \
    --audit local/audit_01.jsonl
```

- 要加 `PYTHONDONTWRITEBYTECODE=1`，否則會在 `software/native_ckks/` 留下 `__pycache__`。沒有 `--bind` 時服務綁 `server_url` 的位址（第一個 `--server-host`）；WSL2 預設 NAT＋port forwarding 時那個 Windows IP 不在 WSL 裡，會 `Cannot assign requested address`，要加 `--bind 0.0.0.0`。
- WSL2 擇一（Windows 標準設定，本專題未實測；橋接網路的 Linux 虛擬機不需要）：mirrored networking（`%UserProfile%\.wslconfig` 寫入下面兩行再 `wsl --shutdown`，不用 `--bind`，防火牆仍要允許 TCP 18087）；或 NAT＋port forwarding（系統管理員 PowerShell 執行下面三行並加 `--bind 0.0.0.0`；展示完 `netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=18087`、`Remove-NetFirewallRule -DisplayName "EdgeFHE stage3 18087"`）。

```
[wsl2]
networkingMode=mirrored
```

```powershell
$wslIp = (wsl hostname -I).Trim().Split(' ')[0]
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=18087 connectaddress=$wslIp connectport=18087
New-NetFirewallRule -DisplayName "EdgeFHE stage3 18087" -Direction Inbound -Protocol TCP -LocalPort 18087 -Action Allow
```

板子端（`build/v5/` 沒有就先 `python3 tools/v5_sources.py --output build/v5`）：組 A 不需要 sudo；組 B 需要本次 F、G 通過（172 個請求）。

```bash
cd ~/Grad
A='--library build/v5/libedgefhe_v5_capi.so --config local/demo_client/config.json --server-cert local/demo_client/server_ca.pem --private-dir local/demo_client --model training/outputs/model.json --preprocess training/outputs/edge_preprocess.json --data data/cdc_diabetes/splits/test.csv'
python3 software/native_v5/python/edge_client.py --backend cpu --workers 4 $A --output build/stage3/client_cpu_01.json
sudo bash -c "cd $PWD && exec 3<>/dev/edgefhe_rns3_n4096_dma && PYTHONDONTWRITEBYTECODE=1 python3 software/native_v5/python/edge_client.py --backend fpga --fd 3 $A --output build/stage3/client_fpga_01.json; echo CLIENT_EXIT=\$?; exec 3>&-; chown -R ubuntu:ubuntu build/stage3"
```

- 通過：每次 client 印 `V5_TWO_MACHINE_CLIENT_PASS records=38052 backend=...` 且 exit 0（失敗 `V5_TWO_MACHINE_CLIENT_FAIL`）。server 沒有 PASS 標記，只印 `DYNAMIC_CIPHERTEXT_SERVER_READY`；audit 每批一筆 `ciphertext_affine_completed`（一次 19 筆），不應有 `auth_rejected`、`request_rejected`。
- client 結果檔只有彙總與時間（不寫解密結果）；`--output` 要新檔案。展示完刪掉板子 `local/demo_client/`、電腦 `local/demo_tls_01/`，移除 port forwarding 與防火牆規則。

### 3.5 帶回與合併

每次上板建立 `results/<日期>-kv260/`，放 `stage1_01/`、`stage2_01/`、`kv260_preflight_NN/`、`stage3/` 與一份 `NOTE.md`；`bin/`、`obj/` 不帶（`stage_report.json` 已記 SHA-256）。以下都在電腦的 repo 根目錄（`Grad`）執行：收檔（工具會比對 SHA-256）、用 Windows 內建 `tar` 解開、合併同一塊板子的階段 1、2（PowerShell 或 bash）：

```powershell
powershell -ExecutionPolicy Bypass -File tools\kv260\receive_file.ps1 -RemotePath /home/ubuntu/board_results_01.tgz -LocalFile board_results_01.tgz
mkdir results\2026-10-14-kv260
tar -xzf board_results_01.tgz -C results\2026-10-14-kv260
$D = '2026-10-14-kv260'
python tools/compare_results.py results/$D/stage1_01/e2e_cpu.json results/$D/stage2_01/e2e_all.json --output results/$D/comparison_all
```

```bash
D=2026-10-14-kv260
python3 tools/compare_results.py results/$D/stage1_01/e2e_cpu.json results/$D/stage2_01/e2e_all.json --output results/$D/comparison_all
```

預期 `COMPARISON_WRITTEN ...`。資料、模型、機器、綁核、governor、亂數來源與量測設定要相同，否則公平檢查拒絕（`--allow-unfair` 只用於檢查，不當正式對照）；多個功耗感測器時加 `--sensor <名稱>`（見 `environment.power_sensors`）；`--output` 已存在就換前綴。結果是證據，不手改；`device_times_are_fpga: false` 是模擬裝置，不能當 FPGA 數據；對外寫清楚「KV260 實測」或「電腦上量測」。

## 4. 疑難排解 / Troubleshooting

| 現象 | 做法 |
|---|---|
| 序列埠打不開；不知道哪個是主控台 | 關掉其他開著 COM 埠的程式（如 PuTTY）；對每個埠按 Enter，回 `kria login:` 的就是 |
| 板子時間是 2023 年 | 步驟 2 |
| `sudo date` 後又問密碼；dmesg 有 `OF: overlay: WARNING: memory leak will occur if overlay removed`、`loading out-of-tree module taints kernel`、`module verification failed: signature and/or required key missing - tainting kernel`（未簽章，`"signed_release": false`）、`Please ensure that IP supports buffer length > 23 bits` | 正常 |
| 背景 console 有 `sudo: a password is required` | 先 `sudo -v` 再啟動 |
| 步驟 3 的 `BAD` 不是空的 | 檔案傳壞了：重傳，不執行 |
| 板上 `verify_repository.py` 找不到 `release/...` | 序列埠版：改用步驟 3 |
| `ENVIRONMENT_PREREQUISITES_INCOMPLETE` | 只缺 `cmake_available` 才繼續（A） |
| `FileExistsError`（`check_environment.py`）；`Use a new --output directory; earlier results are evidence`；`... exists: use a new --output`（`compare_results.py`）；C 的 report 已存在 | 換新名稱（`_02`）；舊的不刪 |
| `inspect_prebuilt.py --target`：`does not match the current target kernel/architecture` | 走 B-2；沒有 matching headers 就停 |
| `Slot occupied before load` | 審查後 `sudo xmutil unloadapp`、重列再過 gate（F） |
| `Candidate missing from recognized inventory` | E 沒做或 app 資料夾不完整（用 E 的 `cmp`） |
| `insmod: ERROR: could not insert module ...: File exists` | 這次開機已載入：不再載，也不為重來而 `rmmod` |
| 重開機後沒有 `/dev/edgefhe_rns3_n4096_dma` | 正常：重做 F、G |
| `exec 3<>...` 回 `Permission denied`；`--fd 3 points to ...` 或找不到 `/proc/self/fd/3` | 在同一段 `sudo bash -c` 裡開 fd 並執行工具（3.3） |
| 編譯失敗 | 保留 `bin/build.log`、記下 `g++ --version`；只是新 warning 被 `-Werror` 擋下時，用新的 `--output` 加 `--no-werror` 重跑（兩組仍同一執行檔），真正的 error 不硬過。 |
| `loaded` gate 失敗；insmod 回 0 卻沒有裝置；單元測試或模擬一致性失敗（板上編譯結果和電腦不同，先回報）；實板一致性失敗；量測中 DMA 錯誤（驅動已 fail-closed） | 停止，不做下一步（G、DMA、FPGA 量測），不重試、不重載；保留該步的 log 與 dmesg |
| 量測中必須斷電 | console 還在 `building V5 tools ...` 或 `PASS unit_tests`＝還沒送 DMA，可 `sync && sudo poweroff`；已印 `PASS parity_simulated`＝實板 DMA 已開始，這次不算數並記錄 |
| `pc_server.py`：`Cannot assign requested address` | 加 `--bind 0.0.0.0`（3.4） |
| repo 裡出現 `__pycache__` | 忘了 `PYTHONDONTWRITEBYTECODE=1`（如 `pc_server.py`、`verify_merged_fdt.py`）：刪掉那個快取資料夾 |
| `Fairness check failed` | 用同一設定重跑（3.5） |

## 5. 版本區分 / Version distinction

| 名稱 | 狀態與部署 |
|---|---|
| V3 凍結基線（`firmware/ii1_n4096/`＋`software/driver/`，`ii1-guarded-v3`） | 歷史驗收（實板、兩機固定 6 例、6 小時 soak），2026-10-14 重過 F、G；照 A–G 部署 |
| V3 預編譯模組（`firmware/prebuilt/ubuntu-22.04-5.15.0-1027-xilinx-zynqmp/`，`tools/inspect_prebuilt.py` 只讀核對） | 2026-10-14 實際 insmod；只在 kernel 完全相同時取代 B 的編譯，不取代 F、G 的 live identity 與 probe，不是 V4 driver |
| V4 原生 CKKS（`software/native_ckks/`） | 只在電腦驗證，V4 自己的 DMA 路徑沒上板；下一步先跑不改 runtime 的 CPU batch（[NATIVE_V4.md](NATIVE_V4.md)） |
| V4 驅動草稿（`software/driver_candidate_v4/`） | 沒有編譯驗收，**不可部署**（[README](../software/driver_candidate_v4/README.md)） |
| V5（[software/native_v5](../software/native_v5/README.md)） | 實板一致性通過、階段 2 跑完；用凍結的 V3 DMA ABI（`dma_abi.hpp`：ioctl `0xc0304504`、48 bytes 請求、只用 op2），不需新 bitstream 或驅動；改 ABI 或任何新 HLS 運算＝新 bitstream＝新的板測驗收 |

既有已接受 runtime 保持原狀；同一時間只載入一個 edgefhe 模組，不要 V3、V4 同時載入。
