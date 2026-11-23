# 團隊共同脈絡（2026-11-23）

本文件整理專案對話中累積的背景、決策、慣例與現況，供全體成員同步。內容以模組稱呼負責人，不列個人姓名；也不含帳號、密碼、IP 位址或私人文件連結，這些資訊請在團隊內部管道取得。引用任何數字時，請一併註明出處（V1／V3／V5 或 V8b、哪一天的量測）。

## 1. 指導教授的四項要求（2026-10-15）

1. 以頂級會議的外部基準比較，而不是「自己的序列版對自己的平行版」。
2. 重用已驗證的模組，不必每個底層元件都自己寫。
3. 以節點圖、實測與最佳化求解決定 ARM／FPGA 的切分，並說明延遲隱藏。
4. 與現有研究比較並重新定位。

分析與對策見 [2026-10-18_professor_feedback_analysis.md](2026-10-18_professor_feedback_analysis.md)；156 筆已查證的參考資料見 [2026-10-18_references.md](2026-10-18_references.md)；校正後的切分模型見 [partition_model_v0.1/](partition_model_v0.1/)。

重點：Guerrini 等人（DATE 2026）已在同一塊 KV260 上以 PL 執行 NTT，因此「ARM＋FPGA 的 client 端 CKKS」本身不是新貢獻。建議定位為「以量測驅動、考量通訊成本的 edge MPSoC client 端 CKKS 軟硬體切分方法」。前提是採用標準 128 位元參數，並在 A53 上量測 SEAL-Embedded／OpenFHE 基準。

## 2. 分工與應用

| 模組 | 內容 |
|---|---|
| 統籌整合 | 介面規格、每週整合、階段驗收；另負責 ARM 軟體（CKKS client）與應用展示 |
| FPGA 加速器 | 硬體運算與 v1 參考版本 |
| 資料搬移與排程 | 驅動程式、DMA、非同步排程 |
| 量測、切分模型與基準 | 逐步驟量測、切分模型、外部基準與功耗 |

- 應用：KV260 上的健康風險評分閘道，模型為 CDC 糖尿病 4 特徵邏輯斯迴歸（GenHlth、HighBP、BMI、Age）。延伸目標為多台裝置的加密統計。
- ESP32 能耗探針模組的負責人尚未決定。
- 規劃報告的講者依章節分配：研究動機、應用情境與相關研究、研究方法與驗證、架構分工與時程，各由一位成員報告；研究方法與驗證由統籌報告。

## 3. 簡報與文件慣例

- 投影片與講稿不提到其他版本（「這一版」「前兩版」「改由……取代」）；過去的簡報只作內部參考。大綱寫成完整的報告結構。
- 每個句子都有明確主詞，使用正式的學術用語。寫「階段驗收」，不寫「關卡」；時程只寫期間（「2 週」），不寫「第 1–2 週」。
- 講者分工寫在大綱卡片內；新增頁面後，重新檢查大綱的頁碼範圍。
- 成員模組在分工頁排成同一欄，每張卡片樣式相同。
- 簡報須回答「為何採用 ARM＋FPGA，而不是只用 ARM 或個人電腦」。
- 多人同時編輯簡報時：修改前重新讀取該頁；只改需要的文字，不覆蓋整頁；有人正在編輯的文字框先跳過；完成後檢查版本紀錄，確認其他人的修改仍在。

## 4. 報告與展示時程

- **11/27 報告**：每焦耳處理筆數的實測結果，以及能耗探針的接線方式。簡報檔為 `EdgeFHE_261127.pptx`（以 `EdgeFHE_261111` 為基礎，在第 21 頁後插入 9 頁實測內容）。
- **期末展示**：KV260 經路由器連到一台代替雲端的電腦，產生並傳送密文；以 Wireshark 擷取封包證明線上只有密文；雲端以仿射模型推論後回傳，KV260 解密並顯示結果。切分與待辦見第 9 節。

## 5. Repository 狀態與維護規則

- main 包含 V3 凍結基線、V4／V5，以及 `v8b_rev5/`（未經審查的獨立子專案，2026-11-17 由 `feat/v8-full-pl-encryption` 併入）。
- 新增或修改檔案後，執行 `python tools/update_manifest.py`，再執行 `python tools/verify_repository.py`。修改 `v8b_rev5/` 內的檔案時，另外執行 `v8b_rev5/tools/update_sums.py` 與 `verify_repo.py`，且樹中不能有 `node_modules`。
- `.gitleaksignore` 以三個指紋排除 `v8b_rev5/software/client/PROFILE_DESCRIPTOR.json` 中的 SHA-256 誤判；指紋依賴 commit `a83fab4`，該 commit 不可 squash 或改寫。
- dependabot 的 PR 必須更新 `release/SHA256SUMS` 才會通過 CI；V8b 前端沒有 `eslint.config`，`npm run lint` 必定失敗。
- `docs/EdgeFHE_261014.pptx`、`docs/EdgeFHE_261016.pptx` 與 PUF 分析報告 PDF 尚未加入 git（repository 為公開，是否提交尚未決定）。

## 6. 引用數字時的注意事項

- V8b 未經審查。`v8b_rev5/` 文件中的效能、能效與長測數字來自 rev4；rev5 只做了正確性驗收。2026-11-23 本團隊以 rev5 實測了加密吞吐量與能效（第 8 節）。
- 「lattice-estimator 最低 128.77 bits」在 repository 中沒有可重現的腳本或輸出；repository 只有簡化估算 `estimate_rlwe.py`（129.6 bits）。
- 板上 INA260（`ina260_u14`）只量 SOM 的 5 V 供電軌，約為整板功耗的 47–49%。以 INA260 計算的能效不是整板數字；整板能耗須以外接探針量測。
- `v8b_rev5` 的 ZOO19 模型庫沒有 CDC 4 特徵模型；CDC 模型在 `training/outputs/model.json`（`cdc-diabetes-lr4-v1`）與 `edge_preprocess.json`（正規化參數）。
- 規劃報告中「4.91／5.01 ms」來自 AI4I 模型，不是 CDC 模型。
- CDC 特徵的驗證集 AUC：只用 BMI 0.686；BMI＋HighBP 0.757；再加 Age 0.775；4 特徵 0.812；全部 21 個特徵 0.824。4 特徵模型的測試集 AUC 為 0.807。
- 原始資料 `data/cdc_diabetes/raw/` 與 `splits/` 不在 git 中；依 `manifest.json` 的來源與 SHA-256 下載後可驗證。

## 7. 外部量測：ESP32 整板能耗探針

方向於 2026-11-20 決定：ESP32 改作整板能耗探針，不讀取血壓計或體重計等健康裝置。

| 項目 | 內容 |
|---|---|
| 電流感測 | INA228 模組（Adafruit 相容，分流電阻 15 mΩ，I²C 位址 0x40），高側串接 KV260 的 12 V 正極；VBus 接 Vin+；`ADCRANGE=1`（±40.96 mV，最大約 2.7 A） |
| 微控制器 | ESP32-S3 N16R8；Arduino 設定 ESP32S3 Dev Module、16MB、OPI PSRAM |
| 顯示 | 2.4 吋 ILI9341，8 腳模組（GND VCC SCL SDA RES DC CS BL）；其 SCL／SDA 屬 SPI |
| 腳位 | I²C：SDA 17、SCL 18。SPI：SCK 12、MOSI 11、CS 10、DC 14、RES 4。GPIO 26–37 保留給快閃記憶體與 PSRAM |
| 函式庫 | Arduino ESP32 core 3.3.12；Adafruit INA228 Library 3.0.0、Adafruit ILI9341 1.6.3、Adafruit GFX 1.12.6、Adafruit BusIO 1.17.4 |
| 輸出 | 每 0.1 秒一筆 CSV：`t_ms,V,I_mA,P_mW,E_J` |
| 序列埠指令 | `S<文字>` 狀態列；`B` 一輪開始（能量歸零）；`E<列>,<名稱>,<筆數>` 一輪結束並計算每焦耳筆數；單一字元 `r` 能量歸零 |

- 電腦開啟探針的序列埠時，DTR 與 RTS 都要設為 False；切換這兩條線可能讓 ESP32 停在重置狀態。
- 探針尚未以參考儀器校正（`K_V = K_I = 1`）。INA228 量電壓的誤差上限約 ±0.05%＋2.5 mV；電流誤差主要來自分流電阻公差。手持電表的精度通常不足以校正 INA228，只適合作外部驗證；正式校正需桌上型精密電表或已知阻值的負載。
- 探針韌體、實作手冊、記錄與分析腳本，以及 V8b／OpenFHE 的比較工具都在 [energy_probe/](../energy_probe/README.md)。
- V8b 的板上套件（板上 `~/edgefhe-v8b-r5/BOARD_STEPS_zh-TW.md` 步驟 7，不在 repository）另外規劃了 INA226＋ESP32-S3 的整板量測，與本探針重疊；兩者擇一前需與 V8b 作者協調。

## 8. 2026-11-23 實測結果（探針未校正；同一次量測內的倍數不受校正影響）

整板待機與 CPU 負載（未接乙太網路）：

| 狀態 | 12 V 整板 | 板上 INA260（SOM） |
|---|---|---|
| 待機 | 7.13–7.16 W | 3.38 W |
| ARM 四核滿載 | 8.06–8.07 W | 3.95 W |

INA260 只看到滿載增量的 62%，因此不能用來校正外接探針，只適合對照趨勢與拆分 SOM／載板功耗。

加密能效：三種設定使用相同定義，即 CKKS N = 4096、3 個 RNS 模數、每個 frame 8 個密文、明文事先編碼、迴圈只做加密、連續 30 秒、第一與最後一個 frame 皆檢查正確性；1 個密文換算為 512 筆（每筆 4 個特徵）。以下為接上乙太網路後、每種設定 10 輪的結果：

| 設定 | 每秒筆數 | 加密時整板功率 | 每焦耳筆數 | CPU |
|---|---|---|---|---|
| V8b rev5（ARM＋FPGA） | 1,791,652 | 9.931 W | 180,411 ± 873 | 13% |
| OpenFHE 1.5.1，4 執行緒（ARM） | 195,710 | 10.348 W | 18,913 ± 45 | 100% |
| OpenFHE 1.5.1，單執行緒（ARM） | 49,955 | 9.221 W | 5,417 ± 19 | 25% |

- V8b 的每焦耳筆數為 OpenFHE 4 執行緒的 9.54 倍（95% 信賴區間 9.50–9.57），每秒筆數為 9.15 倍；兩者整板功率相近，差距來自速度。
- 結論只適用於加密這一步；端到端還包含 ARM 端的編碼、網路、雲端與解密。對照組是 OpenFHE 函式庫，不代表所有純 ARM 實作。
- 接上乙太網路後，整板功率約高 1 W；不同時段、不同接線的絕對數值不宜直接比較。
- 同日稍早（未接乙太網路、各 3 輪）另比較了 V8b 與 `edgefhe2`（同一定義）：V8b 201,141 筆／焦耳，`edgefhe2` 147,612 筆／焦耳。

## 9. KV260 板上狀態（2026-11-23）

- Ubuntu 22.04，核心 `5.15.0-1027-xilinx-zynqmp`，4 × Cortex-A53 1.333 GHz（userspace governor）。主控台為 USB 序列埠（115200）；登入資訊在團隊內部管道。
- `/lib/firmware/xilinx`：`edgefhe`（v1，單次測試）、`edgefhe2`、`edgefhe-v8b-r5`、`k26-starter-kits`。家目錄有 `~/board`、`~/board_v2`、`~/edgefhe-v8b-r5` 三套板上程式；三者都不在 repository 中，repository 的 V5 程式（`~/Grad`）已不在板上。
- OpenFHE 1.5.1 已安裝於 `~/.local/openfhe-v1.5.1`（依 `v8b_rev5/docs/DEPENDENCIES_zh-TW.md` 下載並核對 commit `1306d14` 與 tag，3 核編譯約 15 分鐘）。量測程式在 `~/openfhe_bench`，`edgefhe2` 的持續加密版本在 `~/edgefhe_power`。
- 板上 `sudo` 的認證在主控台登出後失效；需要 sudo 的步驟在前景執行，背景工作只做不需 sudo 的部分。
- 有線網路使用另建的 NetworkManager 設定，開機不自動啟用；重開機後回到原本的自動設定。

## 10. 期末展示的軟硬體切分

| 步驟 | 執行位置 | 最佳實踐 |
|---|---|---|
| 問卷輸入、正規化、CKKS 編碼 | KV260 ARM | client 以 `--mode pipeline --workers 3` 執行，使編碼、加密與傳輸重疊 |
| 金鑰與私鑰 | KV260 ARM | 私鑰只存在記憶體，結束時清除；公鑰一次載入 PL |
| 公鑰加密 | KV260 PL（V8b rev5） | 每次呼叫 8 個密文；DMA 搭配中斷；每 frame 以作業系統熵播種 ChaCha20 |
| 傳輸 | ARM → 路由器 → 雲端 | TLS 1.3 雙向認證與憑證釘選；每批 2048 筆為 4 個密文，每個約 96 KiB |
| 雲端推論 | 雲端電腦（`efnet_server`） | 只做深度 1 的仿射運算，不持有私鑰，只記錄操作 |
| 解密、sigmoid、分級 | KV260 ARM | 檢查解密誤差；畫面只顯示風險等級 |
| 量測 | ESP32 探針、Wireshark | 探針量端到端每焦耳筆數；Wireshark 在雲端電腦擷取封包 |

- 端到端的瓶頸預期在 ARM 編碼：PL 加密每個密文約 0.29 ms（11-23 實測），V8b 文件（rev4）記錄含 ARM 正規化與編碼的路徑為每密文 1.18–1.25 ms。需以同一版程式實測確認。
- TLS 會遮住內容；展示版 client 需增加輸出 TLS 金鑰紀錄的選項，Wireshark 載入後才能顯示內層仍是 CKKS 密文。金鑰紀錄只用於展示，結束後刪除。

期末前待辦：在 KV260 建置 V8b client（`tools/build_and_selftest.sh`）；在雲端電腦建置 `efnet_server`；將 CDC 4 特徵模型轉為 ZOO19 格式並重新驗算；加入 TLS 金鑰紀錄選項；以 pipeline 模式量測端到端能耗；經路由器完成一次兩機演練並保存封包、client 紀錄與探針資料。

## 11. 尚待決定的事項

- ESP32 能耗探針模組的負責人。
- 舊簡報與 PUF 報告是否提交（repository 為公開）。
- 探針的參考儀器校正（借用系上實驗室的桌上型電表）。
- 與 V8b 作者協調整板量測方案（INA226 方案與本探針擇一）。
