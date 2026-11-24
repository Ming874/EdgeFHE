# EdgeFHE 期末展示操作手冊：KV260 加密推論與第三方封包證據

本手冊是期末展示的操作依據。展示情境為糖尿病風險評估：填答者在 KV260 的擁有者畫面填寫四個欄位（自評健康狀況、高血壓、BMI、年齡層）；KV260 在 ARM 上完成標準化與 CKKS 編碼，並以 FPGA（PL）完成公鑰加密；只有密文經 TLS 1.3（雙向認證與雙向憑證釘選）通過路由器送到扮演雲端的電腦；雲端電腦沒有任何 CKKS 金鑰，無法解密，只在密文上計算仿射分數並回傳密文；KV260 解密後只顯示風險等級。雲端電腦以 Wireshark 擷取的封包作為第三方可檢驗的證據，證明網路上只出現密文。

> 本展示為研究用途，並非醫療診斷。擁有者畫面只顯示風險等級，不顯示機率。所有場次只能填寫虛構答案；本原型未經正式的密碼稽核（[v8b_rev5/SECURITY.md](../v8b_rev5/SECURITY.md)）。

本手冊的指令分為三類：未特別標示的 PowerShell 指令在雲端電腦的 repository 根目錄執行；標示「板上」的 `bash` 指令由擁有者操作人員在 KV260 主控台親自輸入；標示「Git Bash」的指令在雲端電腦的 Git Bash 執行。角括號內的值（例如 `<PC_IP>`）由操作人員代入實際的值。閘道的事件、HTTP API、公開紀錄與揭露檔格式的完整定義見 [edge/EVENTS.md](edge/EVENTS.md)。

## 1. 架構與信任邊界

| 區域 | 機器 | 可能出現的資料 | 持有的金鑰 |
|---|---|---|---|
| 擁有者端 | KV260；備援時另加一台只以 micro-USB 主控台連接 KV260 的擁有者筆電 | 明文答案、標準化值、解密後的分數與風險等級 | 每筆表單重新產生的 CKKS 金鑰（只在 ARM 記憶體中）；板端 TLS 私鑰（在板上產生，不離開 KV260） |
| 網路 | 展示路由器（不連接網際網路）；選用的鏡像交換器 | 只有 TLS 1.3 密文，以及 ARP、DHCP、mDNS 等背景流量 | 無 |
| 雲端 | 雲端電腦（Windows 11；Docker Desktop 內的 `ubuntu:22.04` 容器） | 只有 CKKS 密文與公開的模型權重 | 沒有任何 CKKS 金鑰，無法解密；只持有雲端 TLS 憑證與私鑰，以及證據場次中示範專用的 TLS 金鑰紀錄（兩者只保護傳輸，無法解開 CKKS 密文） |

| 連接埠或裝置 | 用途 |
|---|---|
| TCP 18443 | KV260 → 雲端電腦（Docker 轉送到容器內未經修改的 `efnet_server`） |
| 127.0.0.1:8080 | KV260 上的閘道：擁有者畫面與 `/api/v1` |
| 127.0.0.1:8765 | 雲端電腦上的證據主控台（雲端畫面） |
| 127.0.0.1:8780 | 擁有者筆電上的序列橋接（備援） |
| COM5 | 能耗探針，只由雲端電腦的證據主控台開啟（開啟前將 DTR 與 RTS 設為 False） |
| COM6 | KV260 主控台：供設定步驟使用，或於備援時接到擁有者筆電；證據場次期間，雲端電腦不使用此埠 |

每筆表單在擁有者畫面上分為 13 個階段。下表的階段名稱與擁有者畫面在 pl 後端時的名稱相同（cpu 與 spec 後端的第 3、4、8 階段名稱略有不同）；資料量為協定的規格值，擁有者畫面上的即時標記才是板上的實測值：

| # | 階段 | 位置 | 資料狀態 | 資料量 |
|---|---|---|---|---|
| 1 | 表單輸入 | 擁有者畫面（127.0.0.1） | 明文 | 4 個數值 |
| 2 | 驗證與承諾 | 閘道 | 明文；加鹽承諾值公開 | 32 B |
| 3 | CSV 與呼叫許可 | tmpfs（目錄 0700、檔案 0600） | 明文；單次有效的 FPGA 呼叫許可 | 約 30 B |
| 4 | 金鑰產生與公鑰載入 PL | ARM → PL（DMA） | 私鑰只在 ARM 記憶體中；公鑰進入 PL | 公鑰 98,304 B |
| 5 | TLS 1.3 交握 | KV260 ↔ 雲端電腦 | 交握訊息 | 約 3 KB |
| 6 | 標準化 | ARM | 明文（z = (x − μ) / σ） | 32 B |
| 7 | CKKS 編碼 | ARM → PL（板上 DMA 緩衝區） | 明文多項式（Δ = 2^30），只存在於 KV260 板上 | 131,072 B 加 32 B 種子 |
| 8 | PL 公鑰加密（k＝4） | FPGA | 密文（4 個，一次呼叫） | 393,216 B |
| 9 | 上傳密文（請求訊框） | 網路 | 密文與公開標頭 | 請求訊框 393,472 B；TLS 紀錄 1 × 273 加 24 × 16,401；TCP 酬載 394,022 B |
| 10 | 雲端加權加總（仿射運算） | 雲端電腦（沒有任何 CKKS 金鑰） | 密文（Σ W_f·ct_f + B） | 輸入 4 個密文，輸出 1 個 |
| 11 | 下載結果密文（回應訊框） | 網路 | 密文 | 回應訊框 98,560 B；TLS 紀錄 1 × 273 加 6 × 16,401；TCP 酬載 98,714 B |
| 12 | 解密 | ARM | 明文分數（c0 + c1·s） | 8 B |
| 13 | 驗證、分級與清除 | 閘道 | 僅擁有者可見；與明文參考值比對後清除暫存檔 | — |

每筆表單只啟動一次加密程式（`efnet_client --mode serial --backend pl`）：一次公鑰載入（LOAD）與一次加密呼叫（ENC），即 `device_calls = 2`。閘道確認 |logit_ckks − logit_plain| ≤ 2×10⁻³ 之後才顯示風險等級；分數與等級邊界的距離不超過 2×10⁻³ 時，畫面顯示「介於 Lx／Ly，不判定」，不顯示等級。

## 2. 角色

| 角色 | 職責 |
|---|---|
| 擁有者操作人員 | 在擁有者畫面填寫虛構答案；在 KV260 主控台親自輸入所有板上指令與 sudo 密碼 |
| 雲端操作人員 | 操作雲端電腦上的證據主控台、Wireshark、封存與驗證；不接觸擁有者畫面 |
| 解說人員 | 對照擁有者畫面、雲端畫面與 Wireshark，說明每個階段的資料狀態 |
| 觀察者（選用） | 以鏡像交換器在另一台筆電擷取封包（驗證項目 G11）；本團隊建議由團隊以外的人員擔任 |

投影時，擁有者畫面與雲端畫面是兩個獨立的影像來源（HDMI 切換器或兩台顯示器）。雲端電腦不得鏡像、串流或擷取擁有者畫面。

## 3. 必須遵守的規則

1. **任何網路上都不得出現明文。** 閘道只在 127.0.0.1 上提供擁有者畫面；擁有者筆電只經 micro-USB 主控台連接 KV260；KV260 唯一的資料流是到雲端電腦 TCP 18443 的 TLS 1.3 連線。
2. **任何人都不得對執行中的加密程式送出訊號。** FPGA 驅動程式等待加密完成時可被訊號中斷；訊號若在加密已發出後抵達，驅動程式會進入隔離狀態，必須在監督下重新開機才能恢復（[ef_v8b_main.c](../v8b_rev5/software/driver/ef_v8b_main.c) 第 208、334、389 行）。因此：
   - 閘道以新的工作階段（`start_new_session=True`）啟動加密程式，終端機的 Ctrl+C 不會傳到加密程式；閘道從不終止加密程式，逾時時只回報 `CLIENT_OVERDUE`。
   - 閘道收到 SIGTERM、SIGINT、SIGHUP、SIGQUIT（`Ctrl+\`）、SIGUSR1 或 SIGUSR2 時停止接受新表單，等待執行中的表單自行結束後才退出。SIGKILL（`kill -9`）無法延後處理，因此任何人都不得在表單執行期間對閘道或加密程式送出 SIGKILL。
   - 板上的閘道只以 `systemd-run -p KillMode=mixed -p TimeoutStopSec=150` 啟動（第 7.10 節）；操作人員以 `sudo systemctl stop edgefhe-demo` 停止閘道，不得使用 `kill`。
   - 表單執行期間，操作人員不得按 Ctrl+C、拔除電源或重新開機；畫面出現 `CLIENT_OVERDUE` 時，操作人員應繼續等待。
   - PC 演練的手動模式以 Ctrl+C 或 `docker stop -t 120 <容器名稱>` 停止；Docker 預設的 10 秒期限可能強制終止執行中的表單。
3. **容器內的閘道不得以 PID 1 執行。** FPGA 呼叫許可要求加密程式的父程序 PID 大於 1（[invoke_permit.hpp](../v8b_rev5/software/client/invoke_permit.hpp) 第 30 行），因此所有容器一律以 `docker run --init` 啟動。
4. **只填寫虛構答案。** 任何人都不得在本展示中輸入真實個人的健康資料。閘道預設不保存任何私有紀錄（`retain_private: false`）；只有使用虛構答案的證據場次，才由擁有者操作人員依第 7.10 節開啟此設定。
5. **板上動作只由人執行。** 載入 bitstream、`insmod` 與所有使用 FPGA 的指令都由擁有者操作人員親自輸入（[v8b_rev5/README.md](../v8b_rev5/README.md) 的使用原則第 4 點）；本目錄的工具不會自動執行這些動作。是否改由團隊授權的自動化執行，尚待團隊決定（第 15 節）。
6. **每個序列埠同一時間只由一個程式開啟。** COM5（探針）只由證據主控台開啟；COM6（主控台）同一時間只由終端機、`board_files.py` 或擁有者橋接其中之一開啟。雲端電腦連接 COM6 期間，任何人都不得在主控台執行閘道的 `submit` 子指令：該指令會在主控台印出風險等級等擁有者資料。表單一律經擁有者畫面送出。`submit` 預設只印出公開事件（`EVT` 行）與結果標記；加上 `--owner-events` 時另印出答案、標準化值與分數等擁有者事件，此選項只供擁有者操作人員在 KV260 桌面的終端機上進行本機除錯，不得用於連接雲端電腦的主控台。
7. **先封存，再使用金鑰紀錄。** 擷取檔的 SHA-256 先寫入 `capture.sha256` 並當場公開，之後才把 TLS 金鑰紀錄交給第三方驗證；板上虛構表單的揭露檔也只在封存之後取回。
8. **金鑰、證據與實際設定檔都不進入 repository。** PKI 放在 `C:\EdgeFHE_demo\pki`，證據放在 `C:\EdgeFHE_evidence\<場次>`，閘道設定放在 `/etc/edgefhe-demo/gateway.json`。`C:\EdgeFHE_demo` 與 `C:\EdgeFHE_evidence` 在第一次寫入之前，必須依第 6.3.1 節設定為只允許目前使用者與 SYSTEM 存取。[.gitignore](.gitignore) 排除這些檔案類型；提交之前，操作人員應執行第 13 節的衛生檢查。
9. **Python 一律以 `-B` 執行**（板上的閘道以 `-I -B` 執行）。任何程式都不得在 `v8b_rev5/` 內產生檔案；`v8b_rev5/` 的位元組一律不變，CDC 模型只存在於 repository 之外的疊加樹。

## 4. 目錄內容

| 目錄 | 內容 | 主要程式 |
|---|---|---|
| [model/](model/) | CDC 4 特徵邏輯迴歸的模型檔（SHA-256 以 `63cee36f` 開頭，即傳輸時的 model_id）、目錄行、疊加預期值與表單定義 | `convert_cdc_to_v8b.py` |
| [overlay/](overlay/) | 在 repository 之外建立疊加樹：`v8b_rev5/` 加上一行 CDC 目錄項與模型檔，以 `staged_tree_id` 識別 | `make_overlay.py` |
| [edge/](edge/) | KV260 閘道（以 root 執行）：表單驗證、加鹽承諾、呼叫許可、加密程式、清除、公開紀錄與 HTTP API | `edgefhe_gateway.py` |
| [ui/](ui/) | 擁有者畫面：表單、13 個階段、結果、證據列、「誰持有什麼」 | `dev_server.py`（開發用） |
| [owner/](owner/) | 擁有者筆電的序列橋接與板上的序列中繼（備援） | `owner_bridge.py` |
| [pc/](pc/) | 經主控台傳送檔案、CSR 往返簽發、封存後取回板上的公開紀錄 | `board_files.py` |
| [pki/](pki/) | 示範 PKI：ECDSA P-256、DNS SAN `edgefhe-cloud` 與 `edgefhe-kv260`、有效期間 2020-01-01 至 2027-12-31 | `make_demo_pki.sh` |
| [cloud/](cloud/) | 雲端映像、示範專用的 TLS 金鑰紀錄墊片、伺服器啟動程式、迴路演練 | `Dockerfile`、`run_server.sh` |
| [sim/](sim/) | PC 端完整演練映像（以 PL 位元精確軟體模型取代 FPGA） | `rehearse_form.sh` |
| [evidence/](evidence/) | Wireshark 解析器、驗證程式（驗證項目 G0–G11）、封存與報告 | `efhe_seal.py`、`efhe_verify.py` |
| [console/](console/) | 雲端電腦上的證據主控台 | `evidence_console.py` |
| [tests/](tests/) | 整個 `demo/` 的衛生檢查 | `test_hygiene.py` |

## 5. 階段驗收

| 階段驗收 | 內容 | 章節 |
|---|---|---|
| A1 電腦離線檢查 | `CDC_MODEL_CHECK_PASS`；`OVERLAY_CHECK_PASS`，且電腦、兩個映像與板上的 staged_tree_id 相同；`REPO_VERIFY_PASS 281 []`；`HYGIENE_PASS`；`PY310_SYNTAX_PASS`；`REPOSITORY_MANIFEST_AND_DOCUMENTS_PASS` | 6.1、6.2、13 |
| A2 雲端映像 | 三個自我測試標記；`libssl.so.3` 為動態連結；`EDGEFHE_KEYLOG_SHIM_MATCHES_SCLIENT`；全零的探測請求由 G6 偵測（`EFHE_NEGATIVE_CONTROL_DETECTED gate=G6`） | 6.3 |
| A3 PC 演練 | `SIM_FORM_PRESETS_PASS n=19`；`SIM_PERMIT_ACCEPTED_OWNER_CREATE`；`SIM_PERMIT_CORRUPTED_REFUSED`；封存後 `EFHE_EVIDENCE_PASS` | 6.4 |
| A4 板上 | `EDGEFHE_BUILD_AND_SELFTEST_PASS`；`V8B_R5_PL_LOADED_AFI_PROVIDER_APPLIED`、`V8B_PROXY_READY`、`V8B_BOARD_SUSTAIN_PASS`；`APPLICATION_PL_NATIVE_SELFTEST_PASS backend=V8B_PL_DEVICE`；`GATEWAY_PREFLIGHT_PASS` | 7.3、7.8、7.9 |
| A5 經由路由器 | `s_client` 顯示 TLSv1.3 與 `Verification: OK`；cpu、spec、pl 三個後端皆為 `GATEWAY_GATES_PASS` | 7.7、7.9 |
| A6 PL 推論 | 每個預設表單皆為 `CPP_E2E_PL_NUMERIC_AND_DRAIN_PASS`、`device_calls` 2、兩條 DMA 中斷線各 +2、誤差 ≤ 2×10⁻³ 且等級正確；記錄每筆表單的實測時間 | 8 |
| A7 封包證據 | 驗證項目 G0–G10 全部 PASS | 9 |
| A8 視覺化 | 擁有者畫面的 13 個階段、即時標記與重播；雲端畫面中未使用金鑰紀錄的即時 TLS 紀錄（竊聽者所見）、解碼後訊框、雜湊比對、`cloud_ns` 與含場次標記的功率曲線 | 8 |
| A9 穩健性 | 故障演練得到明確的停止代碼，沒有重試、沒有終止加密程式、PL 未進入隔離狀態；完整演練兩次 | 11 |

完成的定義：在 PL 後端經由路由器完成 A6、A7 與 A8，並留下封存的證據資料夾（含 `MANIFEST.sha256` 與 `report.html`）。

## 6. 電腦上的準備與演練

本節的指令都在雲端電腦上執行，不接觸 KV260、序列埠與網路設定。操作人員應依序完成；任何一步失敗即停止並保留輸出。第 6.1 至 6.4 節全部通過之前，操作人員不得開始板上的步驟。

### 6.1 離線檢查（A1）

```powershell
python -B v8b_rev5\tools\verify_repo.py                         # REPO_VERIFY_PASS 281 []
python -I -B demo\model\convert_cdc_to_v8b.py --repo . --check    # CDC_MODEL_CHECK_PASS model=63cee36f… catalogue_after=840bab20… profile=c01b69cf…
python -B demo\model\test_model.py                                # OK
python -B demo\overlay\make_overlay.py --repo . --check           # OVERLAY_CHECK_PASS staged_tree_id=<ID>
python -B demo\overlay\test_overlay.py                            # OK 與 OVERLAY_TESTS_PASS（約 1 分鐘）
python -B demo\evidence\efhe_verify.py --selftest                 # EFHE_VERIFIER_SELFTEST_PASS cases=25
python -B demo\evidence\tests\test_verify_unit.py                 # EVIDENCE_UNIT_TESTS_PASS
python -B demo\ui\tests\check_ui.py                               # UI_STATIC_CHECK_PASS
node --test "demo/ui/tests/*.test.mjs"                            # fail 0
python -B demo\tests\test_hygiene.py                              # HYGIENE_PASS
```

操作人員應記下 `staged_tree_id`（以下稱 `<ID>`）與其前 8 個十六進位字元（以下稱 `<ID8>`）；此值與 [model/EXPECTED.json](model/EXPECTED.json) 所記錄的值相同，電腦、兩個映像與板上的疊加樹都必須得到同一個值。

Node 21 以後的版本把目錄參數當作 CommonJS 模組載入，因此 `node --test demo/ui/tests/` 在 Node 26 上未執行任何測試即失敗（`Cannot find module`）。上述指令以加上引號的萬用字元指定測試檔：PowerShell 與 Git Bash 都把引號內的字串原樣交給 Node，由 Node 自行展開，`demo/ui/tests` 日後新增的 `*.test.mjs` 也會一併執行。CI 或其他文件引用此項測試時，應使用同一行指令。

### 6.2 容器與 Windows 工具鏈測試

```powershell
docker run --rm -v "${PWD}:/repo" -w /repo python:3.10-slim python -B -m unittest discover -s demo/edge/tests -v       # OK
docker run --rm -v "${PWD}:/repo" -w /repo python:3.10-slim python -B -m unittest discover -s demo/edge/tests_http -v  # OK 與 HTTP_API_TESTS_PASS
docker run --rm -v "${PWD}:/repo" -w /repo ubuntu:22.04 bash -c "apt-get update -qq && apt-get install -y -qq g++ libssl-dev >/dev/null && g++ -std=c++17 -I v8b_rev5/software/client demo/edge/tests/contract_vector.cpp -lssl -lcrypto -o /tmp/cv && /tmp/cv | diff - demo/edge/tests/contract_vectors.json && echo CONTRACT_VECTORS_MATCH"
docker run --rm -v "${PWD}:/repo:ro" python:3.10-slim python -c "import ast,pathlib;[ast.parse(p.read_text(encoding='utf-8'),str(p)) for p in pathlib.Path('/repo/demo').rglob('*.py')];print('PY310_SYNTAX_PASS')"
python -B demo\evidence\tests\run_mock_chain.py          # MOCK_CHAIN_PASS uniform=PASS negative_control=FAIL:G6 tampered_keylog=FAIL:G2
python -B demo\console\tests\test_console_replay.py      # CONSOLE_REPLAY_PASS sessions=4（約 45 秒）
python -B demo\owner\tests\test_bridge.py                # OWNER_BRIDGE_TESTS_PASS 與 BOARD_FILES_TESTS_PASS
```

- 第三條指令需要網際網路（`apt-get`）；展示現場的區域網路不連接網際網路，因此所有需要下載的步驟（映像、套件、OpenFHE 原始碼）都必須在展示之前完成。
- `run_mock_chain.py` 與 `test_console_replay.py` 需要 Wireshark 與 Npcap 的迴路介面（擷取權限），`test_bridge.py` 需要 Git Bash 與其中的 OpenSSL，因此這三項測試不適合放入 CI；`test_bridge.py --only bridge` 只需要 Python 標準函式庫，可在 `python:3.10-slim` 內執行。
- 以下操作檢查示範 PKI 的腳本；結束時，操作人員應刪除產生的私鑰（Git Bash）：

```bash
MSYS_NO_PATHCONV=1 bash demo/pki/make_demo_pki.sh all-in-one "$TEMP/pki_check"     # EDGEFHE_DEMO_PKI_READY server_pin=… client_pin=…
openssl verify -attime 1700000000 -CAfile "$TEMP/pki_check/ca/ca.pem" -purpose sslserver "$TEMP/pki_check/ca/server.pem"   # OK
openssl verify -attime 1700000000 -CAfile "$TEMP/pki_check/ca/ca.pem" -purpose sslclient "$TEMP/pki_check/ca/client.pem"   # OK
rm -rf "$TEMP/pki_check"
```

### 6.3 雲端映像、PKI 與金鑰紀錄演練（A2）

#### 6.3.1 PKI 與證據資料夾的存取控制

Windows 的 `C:\` 讓新建的資料夾繼承其權限：`BUILTIN\Users` 可讀取，`NT AUTHORITY\Authenticated Users` 可修改；`umask` 與 `chmod` 對 NTFS 無效。未經處理時，本機的其他帳號可讀取 CA 與雲端伺服器的私鑰、TLS 金鑰紀錄與擷取檔，也可修改已封存的證據。因此，操作人員必須在第一次寫入 `C:\EdgeFHE_demo` 或 `C:\EdgeFHE_evidence` 之前（即本節的映像備份、PKI 與迴路演練之前），以目前使用者的 PowerShell 執行下列指令一次。資料夾由目前使用者建立時，此指令不需要系統管理員權限；資料夾由其他帳號建立時，操作人員應改以系統管理員 PowerShell 執行：

```powershell
foreach ($d in 'C:\EdgeFHE_demo', 'C:\EdgeFHE_evidence') {
  New-Item -ItemType Directory -Force $d | Out-Null
  icacls $d /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" /remove:g "*S-1-1-0" "*S-1-5-11" "*S-1-5-32-545"
  icacls $d
}
```

- `/inheritance:r` 移除自 `C:\` 繼承的權限；`/grant:r` 只授予目前使用者與 SYSTEM（`S-1-5-18`）完全控制，並由其下的資料夾（`CI`）與檔案（`OI`）繼承；`/remove:g` 另外移除 Everyone（`S-1-1-0`）、Authenticated Users（`S-1-5-11`）與 Users（`S-1-5-32-545`）的明確權限。指令以 SID 指定群組，因此不受 Windows 顯示語言影響。
- 每個資料夾的 `icacls` 輸出必須恰好只有兩項權限：`<電腦名稱>\<目前使用者>:(OI)(CI)(F)` 與 `NT AUTHORITY\SYSTEM:(OI)(CI)(F)`，最後一行為 `Successfully processed 1 files; Failed processing 0 files`。出現其他帳號或群組時，操作人員不得繼續。
- 資料夾內既有的檔案與子資料夾（例如先前演練留下、含 TLS 金鑰紀錄與 `capture_dsb.pcapng` 的 `rehearsal_loopback` 與 `rehearsal_sim`）只有繼承而來的權限，因此同時改為只允許這兩個帳號存取；之後新建的場次資料夾也自動繼承相同的權限。操作人員應以 `icacls C:\EdgeFHE_evidence /T` 確認每一項都只列出這兩個帳號（標示為 `(I)`），或以下列唯讀檢查逐項比對 SID（不受顯示語言影響）：

```powershell
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$bad = @(Get-Item -Force 'C:\EdgeFHE_demo', 'C:\EdgeFHE_evidence') + @(Get-ChildItem -Recurse -Force 'C:\EdgeFHE_demo', 'C:\EdgeFHE_evidence') | Where-Object { (Get-Acl -LiteralPath $_.FullName).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object { $_.IdentityReference.Value -notin $me, 'S-1-5-18' } }
if ($bad) { $bad.FullName } else { 'ACL_PRIVATE_OK' }
```

- 輸出 `ACL_PRIVATE_OK` 表示兩個資料夾與其下每一項都只允許目前使用者與 SYSTEM 存取；否則，此檢查逐行列出仍允許其他帳號存取的路徑。列出的路徑帶有明確（非繼承）的權限時，操作人員應執行 `icacls <路徑> /reset /T /C`，使其改為繼承上層資料夾的權限，再重新檢查，直到輸出 `ACL_PRIVATE_OK` 為止。
- Docker Desktop 以目前使用者的身分存取掛載的資料夾，因此設定之後容器仍可寫入，容器寫出的檔案也繼承相同的權限（已在雲端電腦上以雲端映像實測）。
- 證據主控台（第 8.1 節）在每個即時場次開始時，自行把 `pki_dir`（`C:\EdgeFHE_demo\pki\cloud`，含雲端伺服器私鑰）與新的場次資料夾限制為只允許目前使用者與 SYSTEM 存取，並逐項讀回確認；無法完成時，主控台以 `PKI_ACL` 或 `RUN_ACL` 拒絕啟動，不開始擷取。然而 CA 私鑰（`C:\EdgeFHE_demo\pki\ca`）、映像備份、第 6 節的演練資料夾，以及不經證據主控台執行的 `efhe_seal.py`、`run_server.sh` 與 `make_demo_pki.sh`，都不在其範圍內，因此本步驟不可省略。

#### 6.3.2 雲端映像

建置雲端映像並保存離線備份：

```powershell
$C = git rev-parse --short=12 HEAD
docker build --progress=plain -f demo/cloud/Dockerfile --build-arg EDGEFHE_COMMIT=$C -t edgefhe-demo-cloud:$C .
docker run --rm edgefhe-demo-cloud:$C cat /opt/efhe/OVERLAY_RECORD.json      # staged_tree_id 必須等於 <ID>
docker run --rm edgefhe-demo-cloud:$C cat /opt/efhe/BUILD_SELFTEST.txt
docker save edgefhe-demo-cloud:$C -o C:\EdgeFHE_demo\cloud_$C.tar
```

建置紀錄必須出現三個自我測試標記：`CPP_MTLS_SPAN_BYTE_HASH_EQUIVALENCE_PASS bytes=51904512 refused=33`、`CPP_MTLS_INVOCATION_PERMIT_PASS positive=1 refused=14`、`CPP_E2E_WIRE_AND_PADDED_AFFINE_PASS exact_checks=1572899 refusals=78`。任何一個標記缺少，或 `efnet_server` 未動態連結 `libssl.so.3`，建置即失敗。`demo/` 有任何變更之後，操作人員都必須重新建置映像（約需 1 分鐘）：映像內的 `/opt/efhe/BUILD_SHA256` 只確認映像本身未被更動，無法發現映像早於 repository 的變更，因此舊映像仍會通過檢查，卻執行舊的程式與畫面。

#### 6.3.3 CA 與雲端憑證

建立 CA 與雲端憑證（板端私鑰在第 7.4 節由 KV260 自行產生）：

```powershell
docker run --rm -v C:/EdgeFHE_demo:/out edgefhe-demo-cloud:$C bash /opt/efhe/make_demo_pki.sh ca-and-server /out/pki
```

輸出為 `EDGEFHE_DEMO_PKI_CA_SERVER_READY server_pin=<SPIN>`；操作人員應記下 `<SPIN>`。`C:\EdgeFHE_demo\pki\ca` 保存 CA 私鑰與簽發紀錄，`cloud\` 以唯讀方式掛載為雲端容器的 `/pki`，`board\` 由第 7.4 節的簽發步驟建立。容器內的腳本看不到 Windows 端的 git 工作樹，因此操作人員必須維持 `-v C:/EdgeFHE_demo:/out`，不得把 repository 內的路徑掛載為輸出目錄。

#### 6.3.4 金鑰紀錄演練與負向對照

證據資料夾必須是新的或空的，演練約需 3 秒：

```powershell
docker run --rm --init -v C:/EdgeFHE_evidence/rehearsal_loopback:/evidence edgefhe-demo-cloud:$C bash /opt/efhe/rehearse_loopback.sh
python -B demo\evidence\efhe_seal.py --run C:\EdgeFHE_evidence\rehearsal_loopback --expect-negative-control
```

演練輸出 `EDGEFHE_KEYLOG_SHIM_MATCHES_SCLIENT connection=1`、`connection=3`、`CPP_E2E_REMOTE_REJECTION_OBSERVED key NOT_CRYPTO_TEST` 與 `EDGEFHE_LOOPBACK_REHEARSAL_PASS`；封存程式輸出 `EFHE_CAPTURE_SEALED` 與 `EFHE_NEGATIVE_CONTROL_DETECTED gate=G6`。此資料夾的驗證結果為 `FAIL:G1,G3,G4,G6,G8`，全部由刻意送出的全零探測請求造成，屬於預期結果。

### 6.4 PC 端完整演練（A3）

```powershell
docker build --progress=plain -f demo/sim/Dockerfile --build-arg EDGEFHE_COMMIT=$C -t edgefhe-demo-sim:$C .
docker run --rm --init -p 127.0.0.1:8080:8080 -v C:/EdgeFHE_evidence/rehearsal_sim:/evidence edgefhe-demo-sim:$C bash /repo/demo/sim/rehearse_form.sh --auto
python -B demo\evidence\efhe_seal.py --run C:\EdgeFHE_evidence\rehearsal_sim --verify
```

- 第一次建置會編譯 OpenFHE 1.5.1（24 核心的電腦約需 3 分鐘，小型電腦約需 30 至 40 分鐘）；之後的建置沿用 OpenFHE 層，約需 1 分鐘。與雲端映像相同，`demo/` 有任何變更之後，PC 演練映像也必須重新建置（第 6.3.2 節），簡報引用的演練畫面才會是目前的閘道與擁有者畫面。
- 演練依序輸出 `GATEWAY_PREFLIGHT_PASS checks=12 backend=spec`、`GATEWAY_SERVING 0.0.0.0:8080`、`SIM_FORM_PRESETS_PASS n=19`（A、C、D、E、M、F、G、H、K 各兩次，加上 J）、`SIM_PERMIT_ACCEPTED_OWNER_CREATE`、`SIM_PERMIT_CORRUPTED_REFUSED code=PL_PERMIT_INVOCATION_BINDING`、`CAPTURE_STOPPED`，最後為 `SIM_REHEARSAL_PASS`；封存程式最後輸出 `EFHE_EVIDENCE_PASS gates=G0..G10`。
- 手動模式：省略 `--auto`，並以 `--name efhe-sim` 命名容器；PC 扮演擁有者，在 `http://127.0.0.1:8080/` 開啟擁有者畫面（僅限演練，只能填寫虛構答案）。操作人員以 Ctrl+C 或 `docker stop -t 120 efhe-sim` 停止演練，再於電腦上執行封存。
- PC 演練使用 PL 位元精確軟體模型（spec 後端）；在畫面上選擇 pl 後端時，表單會以 `OWNER_CREATE` 停止（容器內沒有 FPGA 裝置），此為預期結果。x86 上每筆表單約需 30 至 45 毫秒，因此即時的上傳與下載標記很少出現。PC 演練的時間與能耗不代表板上的數字：閘道回報演練模式時，擁有者畫面持續顯示「PC 演練（PL 位元精確軟體模型）・非板上實測」，並把時間標示為 PC 演練值；簡報引用演練畫面時，同樣必須標示「PC 演練（PL 位元精確軟體模型）」。
- Windows PowerShell 5.1 會移除 `docker` 參數內層的雙引號；參數含有內層雙引號的指令，操作人員應改在 Git Bash 以 `MSYS_NO_PATHCONV=1` 執行。本手冊的指令都不含內層雙引號。

### 6.5 擁有者畫面的開發伺服器與截圖模式

```powershell
python -B demo\ui\dev_server.py --fixture demo\ui\fixtures\run_M_pl.json --port 8781
```

開發伺服器在 `http://127.0.0.1:8781/` 重播錄製的事件序列；`--preset K` 顯示邊界情形，`--fail TLS_DEADLINE` 顯示停止代碼，`--time-scale 2` 放慢播放，`--no-preload` 從空白畫面開始。開發伺服器的資料是合成資料，畫面會標示「模擬閘道・合成資料」與「開發用合成資料，非實測結果」；簡報截圖必須取自板上的實際執行。

截圖模式以白色 16:9 背景呈現：網址加上 `?shot=all` 顯示全部階段，`?shot=stage=N` 顯示第 N 個階段；在畫面上按 Shift+S 切換全部階段，左右方向鍵切換階段，Esc 離開截圖模式。

## 7. 板上準備（A4、A5）

擁有者操作人員在 KV260 主控台（COM6，115200 8N1）親自輸入本節所有板上指令。主控台登出後，sudo 的認證即失效，因此需要 sudo 的步驟在前景執行，背景工作只做不需要 sudo 的部分。第 7.1 至 7.5 節在可連接網際網路的實驗室網路上完成；第 7.6 節之後，KV260 改接展示路由器。

在雲端電腦上使用主控台工具 `board_files.py` 之前，操作人員應以下列方式設定主控台密碼（擁有者橋接只在擁有者筆電上執行，並以同樣的方式在筆電上設定，第 7.11 節）；密碼只存在於本次 PowerShell 工作階段，不會留在 PowerShell 的歷史紀錄，也不得寫入任何檔案：

```powershell
$env:KV_PW = (New-Object System.Management.Automation.PSCredential 'kv', (Read-Host -AsSecureString '主控台密碼')).GetNetworkCredential().Password
```

### 7.1 相依套件與擁有者畫面的模式

板上：

```bash
unset HISTFILE
dpkg -s libssl-dev cmake g++-11 python3 >/dev/null && echo DEPS_OK
test -f ~/.local/openfhe-v1.5.1/lib/OpenFHE/OpenFHEConfig.cmake && echo OPENFHE_OK
systemctl get-default; command -v firefox chromium chromium-browser; fc-list :lang=zh-tw | head -n 1
grep -i dma /proc/interrupts
```

- 未出現 `DEPS_OK` 時，操作人員應在離開實驗室網路之前執行 `sudo apt-get install -y libssl-dev`；未出現 `OPENFHE_OK` 時，操作人員應依 [DEPENDENCIES_zh-TW.md](../v8b_rev5/docs/DEPENDENCIES_zh-TW.md) 安裝 OpenFHE 1.5.1。
- KV260 桌面模式需要圖形登入、瀏覽器與繁體中文字型；任一項不具備時，擁有者畫面改用擁有者筆電（第 7.11 節）。
- 閘道預期 `/proc/interrupts` 中恰有兩條名稱含 `xilinx-dma-controller` 的中斷線；操作人員應記錄實際的名稱與編號。

### 7.2 取得程式碼

板上：

```bash
cd ~/EdgeFHE_repo && git fetch origin && git checkout <BRANCH> && git pull --ff-only && python3 -B v8b_rev5/tools/verify_repo.py   # REPO_VERIFY_PASS 281 []
```

`<BRANCH>` 為展示使用的分支。板上尚無 repository 時，操作人員應先執行 `git clone <REPOSITORY_URL> ~/EdgeFHE_repo`。KV260 無法連接網際網路時，雲端電腦可經主控台推送 `demo/` 目錄（約需 1.5 分鐘；板上既有 repository 的 `v8b_rev5/` 仍須通過 `verify_repo.py`）：

```powershell
python -B demo\pc\board_files.py push-tree demo /home/ubuntu/EdgeFHE_repo/demo     # BOARD_FILES_TREE_PUSHED
```

### 7.3 疊加樹與建置

板上（每次登入主控台後，操作人員都應先設定 `ID8`）：

```bash
ID8=<ID8>
python3 -B ~/EdgeFHE_repo/demo/overlay/make_overlay.py --repo ~/EdgeFHE_repo --out ~/edgefhe-demo/src-$ID8
nohup bash ~/edgefhe-demo/src-$ID8/tools/build_and_selftest.sh ~/edgefhe-demo/build-$ID8 ~/.local/openfhe-v1.5.1/lib/OpenFHE > ~/edgefhe-demo/build-$ID8.log 2>&1 &
tail -n 1 ~/edgefhe-demo/build-$ID8.log          # 完成時為 EDGEFHE_BUILD_AND_SELFTEST_PASS
cmake --build ~/edgefhe-demo/build-$ID8/app -j 2 --target ef_app_pl_selftest
```

- 疊加程式輸出 `EDGEFHE_DEMO_OVERLAY_READY out=… staged_tree_id=<ID> catalogue=840bab20 profile=c01b69cf changed=1 added=1`；其中的 `<ID>` 必須與第 6.1 節相同，疊加紀錄寫在 `~/edgefhe-demo/src-<ID8>.OVERLAY_RECORD.json`。
- 以相同的 `--out` 重新執行之前，操作人員應先刪除 `~/edgefhe-demo/src-<ID8>` 與 `~/edgefhe-demo/src-<ID8>.OVERLAY_RECORD.json`。
- 建置不需要 sudo，因此可在背景執行。記憶體不足時，操作人員應以 `-j 2` 手動重做 [build_and_selftest.sh](../v8b_rev5/tools/build_and_selftest.sh) 第 16 至 24 行。
- 板上 `v8b_rev5/` 內若有 `__pycache__`，疊加程式會以 `BASE_VERIFY` 停止；操作人員應刪除該目錄。

### 7.4 板端金鑰與 CSR

板上（私鑰在板上產生，權限為 0600，且不離開 KV260）：

```bash
umask 077; mkdir -p ~/edgefhe-demo/pki && cd ~/edgefhe-demo/pki && openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -noenc -keyout client.key -out client.csr -subj /CN=edgefhe-kv260
```

操作人員關閉雲端電腦上的主控台終端機（釋放 COM6）之後，在雲端電腦上執行：

```powershell
python -B demo\pc\board_files.py csr-roundtrip --pki C:\EdgeFHE_demo\pki
```

輸出為 `EDGEFHE_DEMO_CLIENT_SIGNED client_pin=<CPIN>`，最後為 `BOARD_FILES_CSR_ROUNDTRIP_PASS`。此後板上的 `~/edgefhe-demo/pki` 含有 `ca.pem`、`client.pem`、`client.key`（0600）與 `server.pin`，雲端電腦的 `C:\EdgeFHE_demo\pki\cloud` 含有 `client.pin`。簽發程式不會重新簽發；簽發後的推送若失敗，操作人員應依訊息改用 `push-file` 完成。KV260 更換私鑰時，操作人員應刪除 `C:\EdgeFHE_demo\pki\board` 與 `cloud\client.pin`、重新簽發，並重新啟動雲端伺服器。

### 7.5 安裝與閘道設定

板上：

```bash
sudo install -d -m 0755 /opt/edgefhe-demo /etc/edgefhe-demo
sudo cp -r ~/EdgeFHE_repo/demo/edge ~/EdgeFHE_repo/demo/ui ~/EdgeFHE_repo/demo/owner ~/EdgeFHE_repo/demo/model /opt/edgefhe-demo/
sudo chown -R root:root /opt/edgefhe-demo ~/edgefhe-demo/build-$ID8
sudo install -m 0600 -o root -g root /opt/edgefhe-demo/edge/gateway.example.json /etc/edgefhe-demo/gateway.json
sudo sed -i -e "s/-ID8\([/.]\)/-$ID8\1/g" -e 's/"host": "192\.0\.2\.10"/"host": "<PC_IP>"/' -e 's/"peer_sha256": "REPLACE_WITH_THE_SERVER_PIN"/"peer_sha256": "<SPIN>"/' /etc/edgefhe-demo/gateway.json
sudo python3 -I -B /opt/edgefhe-demo/edge/edgefhe_gateway.py preflight --config /etc/edgefhe-demo/gateway.json --backend spec
```

- 操作人員執行 `sed` 之前，應把指令中的 `<PC_IP>` 與 `<SPIN>` 換成實際的值；此指令只改寫路徑中的 `-ID8`、`host` 與 `peer_sha256`，不改動說明文字。最後一行以 spec 後端預先檢查設定檔（不需要 PL，也不開啟網路連線），應輸出 `GATEWAY_PREFLIGHT_PASS checks=12 backend=spec`；第 7.9 節在 PL 載入後再以 pl 後端檢查。
- 設定檔的範本為 [edge/gateway.example.json](edge/gateway.example.json)。代入後，`client` 為 `/home/ubuntu/edgefhe-demo/build-<ID8>/net/efnet_client`，`overlay_record` 為 `/home/ubuntu/edgefhe-demo/src-<ID8>.OVERLAY_RECORD.json`，`host` 為雲端電腦在展示路由器上的 DHCP 保留位址，`peer_sha256` 為第 6.3 節的 `<SPIN>`；`profile_hex` 維持 `c01b69cf…`，`port` 18443、`timeout_ms` 30000、`workers` 3、`peer_name` `edgefhe-cloud`、`workdir` `/run/edgefhe-demo`、`listen` `127.0.0.1:8080`。
- `client`、`plugin`、`ca`、`cert`、`key`、`host` 與 `workdir` 的值會逐字綁定到每次的 FPGA 呼叫許可；閘道每次都重新計算呼叫許可，但設定檔在服務執行期間不得變更。
- 設定檔必須由 root 擁有且權限為 0600；閘道拒絕未知的鍵（以 `_` 開頭的說明鍵除外）。
- `retain_private` 的預設值為 `false`，範本亦同：閘道不保存任何私有紀錄，每筆表單結束時以零覆寫並刪除 `in.csv` 與 `score.txt`，擁有者畫面的第 13 階段也如此說明。驗證項目 G9 的承諾開啟、G10，以及 G7 對實際答案的搜尋都需要揭露檔，而揭露檔只能由私有紀錄寫出；因此 `retain_private` 為 `false` 的場次，這三項的結果為 `INCOMPLETE`，無法得到 `EFHE_EVIDENCE_PASS`。證據場次開啟此設定的方式見第 7.10 節，是否開啟由團隊決定（第 15 節）。

### 7.6 網路與防火牆

第 7.1 至 7.5 節完成之後，KV260 的網路線才改接到展示路由器的 LAN 埠。板上：

```bash
sudo nmcli con add type ethernet ifname eth0 con-name edgefhe-demo ipv4.method auto ipv6.method disabled connection.autoconnect no
sudo nmcli con up edgefhe-demo
ip -4 -br addr show eth0 && ip route
```

KV260 的位址（以下稱 `<BOARD_IP>`）與雲端電腦的位址（`<PC_IP>`）都應使用路由器的 DHCP 保留位址；`gateway.json` 的 `host` 必須等於 `<PC_IP>`。雲端電腦（系統管理員 PowerShell）：

```powershell
New-NetFirewallRule -DisplayName 'EdgeFHE demo cloud 18443' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 18443 -RemoteAddress <BOARD_IP> -Profile Any
```

展示路由器的網路通常被 Windows 歸類為公用網路，因此此規則必須使用 `-Profile Any`。

### 7.7 經由路由器的冒煙測試（A5）

雲端電腦（冒煙測試的資料夾必須是新的；此伺服器不寫金鑰紀錄）：

```powershell
docker run -d --init --name efhe-smoke -p 18443:18443 -v C:/EdgeFHE_demo/pki/cloud:/pki:ro -v C:/EdgeFHE_evidence/smoke:/evidence edgefhe-demo-cloud:$C sh /opt/efhe/run_server.sh
Select-String -Path C:\EdgeFHE_evidence\smoke\server.log -Pattern CPP_E2E_SERVER_READY
```

板上：

```bash
openssl s_client -connect <PC_IP>:18443 -tls1_3 -CAfile ~/edgefhe-demo/pki/ca.pem -cert ~/edgefhe-demo/pki/client.pem -key ~/edgefhe-demo/pki/client.key -servername edgefhe-cloud -verify_hostname edgefhe-cloud -verify_return_error -brief </dev/null
sudo python3 -I -B /opt/edgefhe-demo/edge/edgefhe_gateway.py gates --config /etc/edgefhe-demo/gateway.json --backends cpu,spec
sudo python3 -I -B /opt/edgefhe-demo/edge/edgefhe_gateway.py gates --config /etc/edgefhe-demo/gateway.json --backends pl --expect OWNER_CREATE
```

- `s_client` 必須顯示 `Protocol version: TLSv1.3` 與 `Verification: OK`；操作人員應記錄加密套件（電腦上的演練為 `TLS_AES_256_GCM_SHA384`）。
- 第一個 `gates` 輸出 `GATEWAY_GATES_PASS`（`max_public_error` ≤ 0.002、`requests_over_5e4` 為 0、spec 的 `device_calls` 為 21）；第二個輸出 `GATEWAY_PERMIT_ACCEPTED_OWNER_CREATE`，證明呼叫許可已被加密程式接受。`--expect OWNER_CREATE` 只能在 PL 載入之前使用；FPGA 裝置存在時，閘道以 `EXPECT_REQUIRES_NO_DEVICE` 拒絕。
- 閒置的伺服器每 30 秒記錄一行 `CPP_E2E_SERVER_CONNECTION_STOP TLS_DEADLINE`，此為正常現象。

測試完成後，雲端電腦執行 `docker stop efhe-smoke; docker rm efhe-smoke`。

### 7.8 每次開機：載入 PL（A4）

KV260 每次開機後，時鐘回到 2023 年（[BOARD_GUIDE.md](../docs/BOARD_GUIDE.md)）。示範憑證的有效期間（2020 至 2027 年）涵蓋該時間，因此校正時鐘不影響 TLS，但會使公開紀錄的時間正確；校正後 sudo 會再次詢問密碼。板上（每一行都由擁有者操作人員親自輸入）：

```bash
sudo date -u -s @<PC_EPOCH>      # 選用；雲端電腦以 [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() 取得 <PC_EPOCH>
sudo -v
cd ~/edgefhe-v8b-r5 && bash scripts/02_load_pl.sh 2>&1 | tail -n 4     # V8B_R5_PL_LOADED_AFI_PROVIDER_APPLIED（180 秒以內）
bash scripts/04_insmod.sh 2>&1 | tail -n 4                              # V8B_PROXY_READY
sudo test/board_enc_check sustain 15 8 2>&1 | tail -n 2                 # V8B_BOARD_SUSTAIN_PASS
sudo ~/edgefhe-demo/build-$ID8/app/ef_app_pl_selftest ~/edgefhe-demo/build-$ID8/app/frozen_build/baseline/libedgefhe_os_prng.so /dev/edgefhe_v8b_dma
```

最後一行必須輸出 `APPLICATION_PL_NATIVE_SELFTEST_PASS backend=V8B_PL_DEVICE`。

### 7.9 預檢與 PL 的階段驗收（A4、A5）

PL 的階段驗收對冒煙伺服器執行：雲端電腦以新的資料夾（例如 `C:/EdgeFHE_evidence/smoke_pl`）重新執行第 7.7 節的 `docker run`，完成後同樣以 `docker stop` 與 `docker rm` 移除容器。驗證程式只接受表單連線，因此 `gates` 與 `throughput` 不得在證據場次的擷取期間執行。板上：

```bash
sudo python3 -I -B /opt/edgefhe-demo/edge/edgefhe_gateway.py preflight --config /etc/edgefhe-demo/gateway.json
sudo python3 -I -B /opt/edgefhe-demo/edge/edgefhe_gateway.py gates --config /etc/edgefhe-demo/gateway.json --backends pl
```

- 預檢逐項輸出 `PREFLIGHT_OK` 或 `PREFLIGHT_FAIL`，最後為 `GATEWAY_PREFLIGHT_PASS checks=13 backend=pl`。檢查項目為：uid、python、config_file（root 擁有且他人不可寫入）、workdir_tmpfs、boot_id、binaries（client、plugin 與 `libef_app_*` 的 SHA-256 與擁有者）、overlay_record（profile 與模型相符）、model_and_presets（預設表單逐位元重算）、pki（私鑰權限 0600）、pins（`server.pin` 等於 `peer_sha256`）、certificates（有效期間涵蓋板上時鐘）、dma（字元裝置、沒有其他持有者、兩條 DMA 中斷線）與 route（`ip route get <PC_IP>`）。預檢不開啟任何網路連線。
- `gates` 輸出 `GATEWAY_GATES_PASS backends=pl … pl.device_calls=21 pl.irq_delta=+21/+21 pl.PL_hz=…`；`PL_hz` 必須介於 199.8 與 200 MHz 之間。

### 7.10 啟動閘道服務與擁有者畫面

閘道只在啟動時讀取設定檔，且設定檔在服務執行期間不得變更；變更設定之前，服務必須已經停止（`sudo systemctl stop edgefhe-demo`）。證據場次（第 8、9 節）需要揭露檔，因此擁有者操作人員在啟動服務之前，先決定本次服務是否保存私有紀錄：

- 證據場次：只有在本次服務期間的每一筆表單都使用虛構答案時，擁有者操作人員才可把 `retain_private` 改為 `true`（板上）：

  ```bash
  sudo sed -i 's/"retain_private": false/"retain_private": true/' /etc/edgefhe-demo/gateway.json
  sudo grep -n '"retain_private"' /etc/edgefhe-demo/gateway.json      # "retain_private": true,
  ```

  開啟之後，閘道把每筆送出之表單的鹽值、答案、標準化值與分數保存在 `/run/edgefhe-demo/private`（tmpfs；目錄 0700、檔案 0600，只有 root 可讀），直到 `disclose` 取走該筆紀錄或 KV260 重新開機為止；擁有者畫面的第 13 階段會說明私有紀錄仍保存在 KV260 的 tmpfs，`GET /api/v1/status` 的 `retain_private` 欄位也會顯示 `true`。證據場次的揭露與取回（第 9 節）完成後，操作人員依第 12 節刪除私有紀錄與揭露檔，並把此值改回 `false`。
- 其他場次（演練、解說或故障演練）：維持預設的 `false`，不保存任何私有紀錄。
- 不論 `retain_private` 的值為何，閘道都在記憶體中保存本次服務期間的事件（最多 20,000 筆，含答案、分數與等級等擁有者事件），擁有者畫面的「執行紀錄」因此可再次開啟先前表單的答案與等級；這些事件不寫入任何檔案，服務停止後即消失。不希望下一位填答者看到先前的表單時，操作人員應在沒有表單執行時停止服務，再依本節重新啟動。是否改為在清除暫存檔之後移除該筆表單的擁有者事件，尚待團隊決定（第 15 節）。

啟動服務（板上）：

```bash
sudo systemd-run --unit=edgefhe-demo --collect -p KillMode=mixed -p TimeoutStopSec=150 /usr/bin/python3 -I -B /opt/edgefhe-demo/edge/edgefhe_gateway.py serve --config /etc/edgefhe-demo/gateway.json
journalctl -u edgefhe-demo -n 5 --no-pager       # GATEWAY_SERVING 127.0.0.1:8080
```

- KV260 桌面模式：擁有者操作人員在 KV260 桌面的終端機執行 `firefox --kiosk http://127.0.0.1:8080/`。擁有者筆電模式見第 7.11 節。
- 系統日誌只記錄公開事件（`EVT` 行，不含高頻的 `progress.io`）與 `GATEWAY_HTTP_REFUSED status= code=` 行，從不記錄擁有者資料。`serve` 加上 `--events none` 或 `--quiet` 時不記錄 `EVT` 行；加上 `--progress-ms 0` 時停用即時標記的取樣器。
- 選用：`sudo systemctl stop avahi-daemon` 可減少擷取檔中的背景流量。
- 執行 `sudo systemctl stop edgefhe-demo` 時，擁有者畫面會持續顯示執行中的表單直到其結束；新的表單會收到 503 `GATEWAY_STOPPING`。執行中的表單若在 150 秒內仍未結束，systemd 會強制終止服務內剩餘的程序，因此操作人員只在沒有表單執行時停止服務。

### 7.11 擁有者筆電（備援）

KV260 沒有可用的桌面、瀏覽器或中文字型時，擁有者畫面改由擁有者筆電提供；筆電只經 micro-USB 主控台連接 KV260，絕不經由網路或雲端電腦。

- 筆電需要 Python 3.10 以上、`pip install pyserial`，以及本 repository 的副本（使用 `demo/ui`、`demo/owner` 與 [kv_console.py](../energy_probe/kv260/kv_console.py)）。
- 操作人員將 KV260 的 micro-USB 主控台線從雲端電腦改接到筆電（筆電上顯示為 `COMx`），並以第 7 節開頭的方式在筆電的 PowerShell 設定 `KV_PW`。
- 選用：板上執行 `sudo dmesg -n 1` 以減少主控台上的核心訊息（橋接程式只提示此指令，不會自動執行）。

```powershell
python -B demo\owner\owner_bridge.py --i-am-the-owner-laptop --transport serial:COMx --listen 127.0.0.1:8780
```

- 橋接程式只能在擁有者筆電上執行，絕不在雲端電腦上執行：擁有者畫面會顯示答案、解密後的分數與風險等級。因此，橋接程式只在加上 `--i-am-the-owner-laptop` 時啟動（未加上時為 `OWNER_BRIDGE_USAGE code=OWNER_LAPTOP`）；本機出現雲端電腦的跡象（127.0.0.1:8765 上的證據主控台、名為 `efhe-cloud` 或來自 `edgefhe-demo-cloud` 映像的容器，或 `C:\EdgeFHE_evidence`、`C:\EdgeFHE_demo` 資料夾）時，橋接程式在開啟序列埠之前即以 `OWNER_BRIDGE_REFUSED code=CLOUD_PC` 拒絕。
- 出現 `OWNER_BRIDGE_LINK_UP`、`OWNER_BRIDGE_READY http://127.0.0.1:8780/` 與 `OWNER_BRIDGE_GATEWAY status=200` 之後，操作人員在筆電的瀏覽器開啟 `http://127.0.0.1:8780/`。
- 橋接程式登入主控台，以主控台使用者（不使用 sudo）啟動板上的中繼：`exec python3 -I -B /opt/edgefhe-demo/owner/serial_relay.py --gateway http://127.0.0.1:8080`。
- Ctrl+C 只停止橋接程式；板上的中繼隨之結束，主控台回到登入提示。橋接程式從不對 KV260 送出控制字元。
- 經由序列埠讀取狀態約需 2 秒，第一次重播較長的事件紀錄需要數秒。中繼在 120 秒沒有請求時自行結束，之後橋接程式會重新登入，因此 `KV_PW` 必須保持設定。
- 證據場次結束後，操作人員應停止橋接程式，並把主控台線改接回雲端電腦，才能執行第 9 節的 `fetch-public`。

## 8. 展示流程（A6、A8）

### 8.1 啟動證據主控台

第一次使用前，操作人員應把 [console/console.example.json](console/console.example.json) 複製為 `C:\EdgeFHE_demo\console.json`，並設定 `iface`（`dumpcap -D` 列出的、接到展示路由器的網路卡）、`board_ip`（`<BOARD_IP>`）、`image`（`edgefhe-demo-cloud:<C>`）與 `pki_dir`（`C:\EdgeFHE_demo\pki\cloud`，其中必須已有第 7.4 節寫出的 `client.pin`）；不使用探針時，`probe_port` 設為 `null`。

```powershell
& 'C:\Program Files\Wireshark\dumpcap.exe' -D
$RUN = 'C:\EdgeFHE_evidence\' + (Get-Date -Format yyyyMMdd_HHmmss)
python -B demo\console\evidence_console.py --config C:\EdgeFHE_demo\console.json --run $RUN
```

- 每個證據場次開始之前，操作人員應以第 6.3.1 節的唯讀檢查確認輸出為 `ACL_PRIVATE_OK`；證據主控台只限制 `pki_dir` 與本次的場次資料夾，不處理 `C:\EdgeFHE_evidence` 內的其他資料夾與 CA 私鑰。
- 啟動標記依序為：`EVIDENCE_PKI_PRIVATE`、`EVIDENCE_RUN_PRIVATE`（PKI 目錄與場次資料夾已限制為只允許目前使用者與 SYSTEM 存取，第 6.3.1 節）、`EVIDENCE_RUN_DIR`、`EVIDENCE_CAPTURE_STARTED`（dumpcap 最先啟動）、`EVIDENCE_PINS_WRITTEN`、`EVIDENCE_SERVER_STARTED`、`WARNING_DEMO_TLS_KEYLOG_ENABLED …`、`CPP_E2E_SERVER_READY port=18443 source=server.log`、`EVIDENCE_LIVE_FEED_STARTED`、`EVIDENCE_PROBE_STARTED`（或 `EVIDENCE_PROBE_OFF`），最後為 `EVIDENCE_CONSOLE_READY http://127.0.0.1:8765`。雲端操作人員隨後在雲端電腦的瀏覽器開啟 `http://127.0.0.1:8765/`。
- 擁有者操作人員必須等 `CPP_E2E_SERVER_READY` 出現後才送出第一筆表單。伺服器就緒之前抵達的 SYN 會在擷取檔中留下被拒絕、沒有 TLS 交握的連線：驗證程式因此判定 G1 與 G2 失敗，並把 G7 列為 `BLOCKED`，整個證據資料夾無法得到 `EFHE_EVIDENCE_PASS`（封存程式輸出 `EFHE_EVIDENCE_FAIL verdict=FAIL:G1,G2`）。驗證程式刻意不排除這類連線，因此發生時，操作人員應照常封存並保留該資料夾，再以新的證據資料夾（新的 `$RUN`）重新啟動證據主控台，並重新送出全部表單。
- 證據主控台以 `efhe-cloud` 為名啟動雲端容器（`-p 18443:18443`），不會接管已在執行的同名容器，也從不操作 KV260。
- 每個連線依序出現 `EVIDENCE_SESSION_OPEN`、`EVIDENCE_SESSION_CLOSED`、`EVIDENCE_SESSION_DECODED`（或 `EVIDENCE_SESSION_DECODE_DEFERRED`，畫面顯示「封存後解碼」）與 `EVIDENCE_SERVER_RESPONSE`。
- 不使用證據主控台時，操作人員在另一個 PowerShell 視窗執行 `& 'C:\Program Files\Wireshark\dumpcap.exe' -i <NPF> -f "host <BOARD_IP>" -s 0 -B 128 -w "$RUN\capture.pcapng"`（先在 `C:\EdgeFHE_evidence` 之下建立 `$RUN`，使其繼承第 6.3.1 節的權限；此路徑不經主控台的權限檢查），再執行 `docker run -d --init --name efhe-cloud -p 18443:18443 -v C:/EdgeFHE_demo/pki/cloud:/pki:ro -v "${RUN}:/evidence" edgefhe-demo-cloud:$C sh /opt/efhe/run_server.sh --keylog`，並把 `C:\EdgeFHE_demo\pki\ca\server.pin` 與 `client.pin` 複製到 `$RUN`。

### 8.2 Wireshark（現場說明）

```powershell
& 'C:\Program Files\Wireshark\Wireshark.exe' -i <NPF> -k -f "host <BOARD_IP>" -X "lua_script:$PWD\demo\evidence\efhe.lua"
```

1. 解說人員先在未載入 TLS 金鑰紀錄的狀態下說明：KV260 與雲端電腦之間只有到 18443 的 TLS 1.3 連線；每筆表單上傳 273 加 24 × 16,401 位元組的紀錄、下載 273 加 6 × 16,401 位元組的紀錄；解析器以「≈」標示每筆紀錄可能承載的 RNS 分量。
2. 雲端操作人員再於「Edit → Preferences → Protocols → TLS → (Pre)-Master-Secret log filename」指定 `$RUN\tls_keys.log`，按 Ctrl+R 重新解析：每個訊框以 `EFHEW001` 開頭，features 為 4、rows 為 1，model_id 等於 `cdc-diabetes-lr4-v1.json` 的 SHA-256，內容仍是 CKKS 密文。
3. 解析器的埠號可用 `-o efhe.port:N` 變更。封存之後，`$RUN\capture_dsb.pcapng` 已內嵌金鑰，可直接開啟。

Wireshark 畫面只供現場說明；第三方驗證一律以 dumpcap 寫出並於第 9 節封存的 `capture.pcapng` 為準。

### 8.3 表單順序與解說重點

| 順序 | 預設 | 虛構答案（GenHlth、HighBP、BMI、Age） | 預期結果 | 解說重點 |
|---|---|---|---|---|
| 1 | E | 2、1、30.0、9 | L2 | 13 個階段、即時的 LOAD 與 ENC 標記、上傳與下載標記；結果出現後，以實測時間重播 |
| 2 | E（再一次） | 同上 | L2 | 同一份答案產生不同的密文、key_id、generation 與承諾值；擁有者畫面的雜湊前綴與雲端畫面一致 |
| 3 | K | 4、1、26.0、4 | 介於 L2／L3，不判定 | 分數與等級邊界的距離小於容許誤差時，本系統不判定等級 |
| 4 | J | 3、0、82.0、7 | 本機拒絕（`BMI_ABOVE_MAX`） | 不啟動加密程式；Wireshark 不出現新的 SYN |
| 5 | M | 3、1、31.2、9 | L3 | 主要示範 |
| 6 | F | 4、1、35.0、10 | L4 | — |

- 每筆表單結束後，解說人員比對擁有者畫面證據列的 `request_sha256` 與 `response_payload_sha256` 前 16 個十六進位字元、雲端畫面的 `server.log` 與擷取檔的值。
- 可選擇另外送出一筆 cpu 後端的表單作為對照。
- 預設表單的定義與預期結果見 [model/form_cdc.json](model/form_cdc.json)；年齡層 4、7、9、10 分別為 35–39、50–54、60–64、65–69 歲。
- 每筆 PL 表單都應記錄實測時間（`client.exited` 的 `wall_ns`）；板上的冷啟動時間（金鑰產生、PL 載入與 TLS 交握）以實測值如實呈現。

## 9. 封存與驗證（A7）

封存的順序決定證據的效力，操作人員必須依序執行：

1. 雲端操作人員按雲端畫面的「封存」（仍有連線進行時，此按鈕會拒絕）。證據主控台依序停止即時封包來源、dumpcap、雲端伺服器與探針，再執行 `efhe_seal.py`，輸出 `EFHE_CAPTURE_SEALED sha256=<H>`；解說人員當場展示或拍攝 `<H>`。不使用證據主控台時，操作人員在 dumpcap 視窗按 Ctrl+C（保留 `Packets dropped: 0` 的輸出）、執行 `docker stop efhe-cloud`，再執行 `python -B demo\evidence\efhe_seal.py --run $RUN`。
2. 封存之後，擁有者操作人員才在板上為每筆虛構表單寫出揭露檔（`<request_sha256>` 可從雲端畫面或 `server.log` 取得，也可改用 run 編號）：

   ```bash
   sudo python3 -I -B /opt/edgefhe-demo/edge/edgefhe_gateway.py disclose <request_sha256>     # GATEWAY_DISCLOSURE_WRITTEN
   ```

   揭露檔公開該筆表單的答案與鹽值，因此只能為虛構答案的表單寫出。私有紀錄只在 `retain_private` 為 `true` 的服務期間保存（第 7.10 節），且只存在 tmpfs，重新開機即清除；操作人員必須在重新開機之前完成揭露。`disclose` 寫出揭露檔後，即清除該筆私有紀錄。
3. 雲端操作人員關閉主控台終端機（釋放 COM6）之後取回板上的公開紀錄與揭露檔：

   ```powershell
   python -B demo\pc\board_files.py fetch-public $RUN     # BOARD_FILES_PUBLIC_FETCHED
   ```

   `capture.sha256` 與 `capture.pcapng` 不符時，此指令拒絕執行；它寫出 `$RUN\board_public.jsonl` 與 `$RUN\disclosure\`，且不覆寫既有檔案。
4. 雲端操作人員按雲端畫面的「驗證」，或執行：

   ```powershell
   python -B demo\evidence\efhe_seal.py --run $RUN --verify     # EFHE_EVIDENCE_PASS gates=G0..G10
   ```

   驗證程式自動找到 `board_public.jsonl` 與 `disclosure\`（也可用 `--board-public` 與 `--disclosure` 指定），寫出 `verify_report.json`、`report.html` 與 `MANIFEST.sha256`。選用：`--observer <觀察者擷取檔>` 執行 G11；`--sign-key <SSH 私鑰>` 以 `ssh-keygen -Y sign -n edgefhe-evidence` 簽署清單。

| 驗證項目 | 內容 | 未通過時的處置 |
|---|---|---|
| G0 | 擷取完整性：0 個遺失封包、封存雜湊、沒有 TCP 缺口、封存早於金鑰紀錄的使用 | 改用有線網卡，以新的場次重新擷取並重新送出表單；`seal_order` 為 `FAIL-PENDING`（`EFHE_SEAL_ORDER_WARNING`，資料夾內有 `seal_order.json`）表示含金鑰的 `capture_dsb.pcapng` 在封存之前已存在，即 TLS 金鑰紀錄在封存之前已被使用；此場次無法得到 `EFHE_EVIDENCE_PASS`，操作人員應保留該資料夾並以新的場次重做 |
| G1 | 不需要金鑰的流量盤點與紀錄大小特徵 | 確認證據主控台在第一筆表單之前已就緒，且擷取期間沒有執行 `gates` 或 `throughput`；伺服器就緒之前的連線會使 G1 與 G2 失敗、G7 成為 `BLOCKED`，操作人員應以新的證據資料夾重新送出表單（第 8.1 節） |
| G2 | TLS 1.3：版本、加密套件、沒有 PSK 與 early data、SNI、雙向認證、葉憑證釘選、沒有 AEAD 錯誤 | 確認 `$RUN` 內的 `server.pin`、`client.pin` 與 `tls_keys.log` |
| G3 | 解密後的應用資料全部是通過檢查的 `EFHEW001` 訊框；model_id、profile、features 4、rows 1 | 確認板上與雲端映像的 staged_tree_id 相同 |
| G4 | `request_id`、`request_hash` 與回應的綁定；`session_id` 等於 TLS exporter | 保留證據資料夾，回報開發人員 |
| G5 | 回應與公開仿射函數的重算結果逐位元相同 | 保留證據資料夾，回報開發人員 |
| G6 | 密文係數均在模數範圍內、請求的 RNS 分量呈均勻分布、沒有重複區塊、key_id 與 generation 每次不同 | 保留證據資料夾，回報開發人員 |
| G7 | 任何表單數值的編碼都不出現在擷取檔中（含陽性對照） | `FAIL-PENDING` 表示 4 位元組的偶然相符尚待確認：以同一份虛構答案再送出一次，封存後重新驗證 |
| G8 | 回應之後，板端只送出 close_notify | 保留證據資料夾，回報開發人員 |
| G9 | 擷取檔、`server.log` 與板上公開紀錄的雜湊一致；加鹽承諾可以打開 | 確認已執行 `disclose` 與 `fetch-public`；`commitment_opens` 為 `INCOMPLETE` 時，通常表示該次服務的 `retain_private` 為 `false`，沒有可揭露的私有紀錄（第 7.10 節） |
| G10 | \|logit_ckks − logit_plain\| ≤ 2×10⁻³，且等級一致（僅限已揭露的虛構表單） | 確認揭露檔存在；沒有揭露檔時為 `INCOMPLETE`（同 G9） |
| G11（選用） | 觀察者擷取檔的每條連線酬載雜湊相同 | 未提供觀察者擷取檔時為 `SKIP` |

驗證狀態另有 `INCOMPLETE`（缺少輸入）、`BLOCKED`（無法解密，因此該項無法執行）與 `SKIP`（選用項目未提供）。`EFHE_EVIDENCE_PASS` 要求 G0 至 G10 全部為 PASS。沒有觀察者時，雲端電腦同時是擷取者與金鑰紀錄的持有者；`report.html` 會載明此限制，簡報也應說明。

## 10. 選用：吞吐量與能耗視窗

吞吐量視窗以獨立的證據場次執行（新的 `$RUN`；證據主控台可加上 `--snaplen 128`，每個封包只保存前 128 位元組）。板上：

```bash
sudo python3 -I -B /opt/edgefhe-demo/edge/edgefhe_gateway.py throughput --config /etc/edgefhe-demo/gateway.json --seconds 30
```

輸出為 `GATEWAY_THROUGHPUT_PASS backend=pl records_per_s=… rows=2048 … mode=pipeline slots=8 inflight=8`。解說人員可將本視窗的每秒筆數與每焦耳筆數，與團隊脈絡文件第 8 節（[2026-11-23_team_context.md](../docs/notes/2026-11-23_team_context.md)）的比較結果並列；兩者的量測範圍不同：本視窗包含 TLS、網路與雲端計算，第 8 節只涵蓋加密步驟。單筆表單的能耗增量接近探針的雜訊（10 Hz、整板閒置約 9 W），因此畫面一律以「≈」標示。

## 11. 故障演練與完整演練（A9）

- 操作人員應從第 7.8 節到第 12 節完整演練兩次。
- 故障演練以獨立的場次執行：表單進行中拔除 KV260 的網路線。擁有者畫面應顯示停止代碼（`SOCKET_*` 或 `TLS_DEADLINE`），閘道不重試、不終止加密程式，PL 也不進入隔離狀態；重新接上網路線後，下一筆表單應正常完成，整個恢復過程應在 2 分鐘以內。

## 12. 收尾與金鑰銷毀

板上：

```bash
sudo systemctl stop edgefhe-demo        # 閘道等待執行中的表單結束後才退出
sudo rm -rf /run/edgefhe-demo/private /var/lib/edgefhe-demo/disclosure
sudo sed -i 's/"retain_private": true/"retain_private": false/' /etc/edgefhe-demo/gateway.json
cd ~/edgefhe-v8b-r5 && bash scripts/99_unload.sh 2>&1 | tail -n 3      # V8B_R5_UNLOADED
lsmod | grep -q '^u_dma_buf' && sudo rmmod u_dma_buf
sudo xmutil unloadapp; sudo xmutil loadapp k26-starter-kits; sudo xmutil listapps | grep ' 0,'
sudo nmcli con down edgefhe-demo
```

- 第二行刪除尚未揭露的私有紀錄（tmpfs）與板上的揭露檔（SD 卡）；揭露檔已由第 9 節的 `fetch-public` 複製到證據資料夾，因此操作人員只在該證據資料夾得到 `EFHE_EVIDENCE_PASS` 之後才執行此行。重新開機會清除 `/run/edgefhe-demo/private`，但不會清除 `/var/lib/edgefhe-demo/disclosure`。
- 第三行把 `retain_private` 改回預設的 `false`（第 7.10 節）；設定檔原本即為 `false` 時，此行不改變任何內容。

雲端電腦：

```powershell
docker stop efhe-cloud; docker rm efhe-cloud        # 證據主控台封存時通常已停止並移除此容器
Remove-NetFirewallRule -DisplayName 'EdgeFHE demo cloud 18443'
```

最後一場展示結束後，操作人員應刪除 `C:\EdgeFHE_demo\pki`（CA 與雲端伺服器的私鑰）、板上的 `~/edgefhe-demo/pki/client.key`，以及封存證據資料夾以外的所有 TLS 金鑰紀錄（包括第 6 節演練資料夾內的金鑰紀錄）。封存證據資料夾內的金鑰紀錄是否保留，由團隊決定（第 15 節）。

## 13. 提交前的衛生檢查

```powershell
python -B demo\tests\test_hygiene.py        # HYGIENE_PASS files=… text=… python=… markdown_links=… git=yes
python -B tools\update_manifest.py           # INVENTORY_REFRESHED_SOURCE_IDENTITIES_UNCHANGED files=…
python -B tools\verify_repository.py         # REPOSITORY_MANIFEST_AND_DOCUMENTS_PASS
python -B v8b_rev5\tools\verify_repo.py      # REPO_VERIFY_PASS 281 []
```

- [tools/update_manifest.py](../tools/update_manifest.py) 會把工作樹中每一個清單檔案的雜湊寫入 `release/SHA256SUMS`，不論 git 是否追蹤該檔案。因此，`demo/` 之下被 `.gitignore` 排除的檔案（擷取檔、紀錄檔、金鑰、`runs/` 等）會進入清單，卻不會被提交，使 CI 失敗；`demo/` 以外未追蹤的檔案（例如 `docs/` 內的本機簡報）也是如此。衛生檢查會列出這些檔案，操作人員應先移出，再更新清單。
- 衛生檢查的項目：不得存在的檔案與目錄（名為 `build`、`local`、`runs`、`__pycache__` 的目錄，擷取檔、紀錄檔、金鑰、憑證、實際設定檔與證據輸出）；`.gitignore` 與 [tools/inventory.py](../tools/inventory.py) 的一致性；UTF-8、無 BOM、只用 LF；私鑰與 PEM 區塊；gitleaks `generic-api-key` 規則會回報的值；名稱含 key、token、secret、api、auth 或 passwd 的識別字旁的 64 位十六進位值；127.0.0.1、0.0.0.0 與 RFC 5737 範例位址以外的 IPv4 位址；特定電腦的路徑；私人文件連結；Markdown 相對連結；Python 3.10 的語法與標準函式庫；板端與容器端程式只匯入標準函式庫；`v8b_rev5/` 內的 Python 快取。
- CI 以 Python 3.10 解析每一個 `.py` 檔（[tools/verify_repository.py](../tools/verify_repository.py)），並以 gitleaks 掃描每一次提交（[ci.yml](../.github/workflows/ci.yml)）。下列檢查不需要擷取權限，也不需要網路，可加入 CI：`python3 -B demo/evidence/efhe_verify.py --selftest`、`python3 -B demo/model/convert_cdc_to_v8b.py --repo . --check`、`python3 -B demo/overlay/make_overlay.py --repo . --check` 與 `python3 -B demo/tests/test_hygiene.py`；CI 安裝 Node 時，也可加入 `node --test "demo/ui/tests/*.test.mjs"`（萬用字元需要 Node 21 以上，且必須保留引號，第 6.1 節），不得改用目錄形式的 `node --test demo/ui/tests/`。

## 14. 疑難排解

### 14.1 擁有者畫面與閘道的代碼

閘道從不自動重試。下表的處置都不包括終止加密程式；任何處置都必須等執行中的表單自行結束。

| 代碼 | 意義 | 處置 |
|---|---|---|
| `BUSY`、`PL_LOCK_BUSY` | 另一筆表單正在執行，或另一個程序（例如另一個閘道、`gates` 或 `throughput`）持有執行鎖 `/run/edgefhe-demo.pl.lock`；此路徑是每台機器固定的一個檔案，`gateway.json` 無法變更 | 等待該筆表單或程序結束後再送出 |
| `PL_LOCK_UNSAFE` | 執行鎖 `/run/edgefhe-demo.pl.lock` 無法安全使用：該路徑不是 root 擁有、只有一個連結的一般檔案（例如符號連結），或無法開啟 | 以 `ls -l /run/edgefhe-demo.pl.lock` 檢查；確認沒有閘道、`gates`、`throughput` 或自我測試正在執行之後，以 `sudo rm /run/edgefhe-demo.pl.lock` 刪除該路徑，閘道下一次執行時會自行重建 |
| `REHEARSAL_PL_ON_BOARD` | 以 PC 演練模式（`--rehearsal-listen 0.0.0.0`）執行的閘道偵測到 KV260 的跡象（`/dev/edgefhe_v8b_dma`、設定的 `dma_node` 或 `xlnx,zynqmp` 裝置樹），因此拒絕以 pl 後端執行 | 演練模式只用於 PC 演練；板上一律依第 7.10 節啟動服務（只監聽 127.0.0.1） |
| `PL_DEVICE_BUSY` | 其他程序開啟了 `/dev/edgefhe_v8b_dma` | 依訊息中的 PID 以 `ps -p <PID> -o pid,cmd` 確認持有者（例如仍在執行的自我測試），等待其結束 |
| `GATEWAY_STOPPING` | 閘道已收到停止要求 | 等待服務結束後，依第 7.10 節重新啟動 |
| `GATEWAY_PID1` | 容器內的閘道以 PID 1 執行 | 以 `docker run --init` 重新啟動容器 |
| `ROOT_REQUIRED` | 閘道未以 root 執行 | 以 `sudo` 執行 |
| `BACKEND_NOT_ALLOWED` | 設定檔的 `backends` 不含所選的後端 | 修正 `gateway.json` 的 `backends` |
| `EXPECT_INVALID`、`EXPECT_REQUIRES_NO_DEVICE` | `--expect` 只接受 `OWNER_CREATE`，且只能在 PL 載入之前用於 pl 後端 | 依第 7.7 節，在載入 PL 之前執行 |
| `EXPECTATION_UNMET` | 加密程式沒有以預期的停止代碼結束 | 檢查輸出中的實際代碼 |
| `WORKDIR_UNSAFE`、`RUN_SETUP` | `/run/edgefhe-demo` 不是 root 擁有的 tmpfs 目錄，或閘道無法建立私有檔案 | 執行預檢並檢查 `workdir_tmpfs` |
| `CLIENT_SPAWN` | 閘道無法啟動 `efnet_client` | 執行預檢並檢查 `binaries` |
| `CLIENT_OVERDUE` | 加密程式執行超過 3 × timeout 加 30 秒仍未結束 | 繼續等待，不得終止加密程式；結束後以 `dmesg` 檢查 FPGA 驅動程式 |
| `CLIENT_SIGNALLED` | 加密程式被外部訊號終止 | 以 `dmesg` 檢查驅動程式；驅動程式若已進入隔離狀態，必須在監督下重新開機並重做第 7.8 節 |
| `CLIENT_EXIT`、`CLIENT_OUTPUT`、`CLIENT_REPORT_MISMATCH` | 加密程式的結束碼、輸出或報告與預期不符，閘道不採用此結果 | 保留系統日誌與公開紀錄，重新執行預檢與 `gates` |
| `IRQ_LINES`、`IRQ_DELTA_MISMATCH` | DMA 中斷線的數量，或其增量與裝置呼叫次數不符 | 以 `grep -i dma /proc/interrupts` 確認恰有兩條 `xilinx-dma-controller` 中斷線 |
| `SCORE_FILE` | 閘道無法讀取解密結果檔 | 檢查 `/run/edgefhe-demo` 的空間與權限 |
| `VERIFICATION_FAILED` | CKKS 結果與明文參考值的差異超過 2×10⁻³，畫面不顯示等級 | 保留紀錄，重新執行 `gates` |
| `PROGRESS_SAMPLER` | 即時標記的取樣器發生錯誤（不影響加密程式） | 必要時以 `--progress-ms 0` 重新啟動服務 |
| `RECORD_WRITE`、`WIPE_FAILED` | 閘道無法寫入公開紀錄，或無法清除私有檔案 | 依訊息中的路徑，以 root 處理或手動刪除該檔案 |
| `INTERNAL` | 閘道內部錯誤 | 保留系統日誌，回報開發人員 |

| 停止代碼 | 意義 | 處置 |
|---|---|---|
| `SOCKET_CONNECT*`、其他 `SOCKET_*` | 加密程式無法連線到 `<PC_IP>:18443`，或連線發生錯誤 | 檢查網路線、DHCP 保留位址、防火牆規則（`-Profile Any`）與雲端容器 |
| `TLS_DEADLINE`、`TLS_FATAL_OR_UNEXPECTED_EOF` | 通訊超過 30 秒的時限，或 TLS 連線中斷 | 同上；確認雲端伺服器仍在執行 |
| `TLS_PEER_CHAIN`、`TLS_PEER_PIN`、`TLS_PEER_CERT_REQUIRED` | 雲端憑證未通過驗證，或與釘選值不符 | 比對 `gateway.json` 的 `peer_sha256` 與 `C:\EdgeFHE_demo\pki\ca\server.pin`，確認雲端容器掛載的是同一份 PKI |
| `TLS_HANDSHAKE_EOF` | 交握期間雲端關閉連線，通常表示雲端拒絕了板端憑證 | 確認雲端的 `client.pin` 對應板上的 `client.pem`；重新簽發後必須重新啟動雲端伺服器 |
| `TLS_LOCAL_CREDENTIALS`、`TLS_CA` | 加密程式無法載入板端的憑證、私鑰或 CA 憑證 | 執行預檢並檢查 `pki` |
| `LITERAL_IPV4_REQUIRED` | `host` 不是 IPv4 位址字面值 | 修正 `gateway.json` 的 `host` |
| `OWNER_CREATE` | 加密程式無法建立金鑰與加密環境（例如 PL 尚未載入） | 依第 7.8 節載入 PL |
| `PL_CLOCK_RANGE` | PL 時脈不在 199.8–200 MHz 之間 | 依第 7.8 節重新載入 PL |
| `PL_PERMIT_*`、`PERMIT_*`、`PL_ROOT_CONTROLLER_REQUIRED`、`PL_REQUIRES_CONTROLLED_INVOCATION` | FPGA 呼叫許可未通過加密程式的檢查；加密程式未開啟 FPGA 裝置 | 確認服務以 root 執行、設定檔在服務執行期間未被變更；必要時重新啟動服務 |
| `CPU_SPEC_PL_FRAME_ACCOUNTING`、`CPU_SPEC_TAKES_NO_PL_PERMIT` | 裝置呼叫次數不符，或 cpu／spec 後端收到呼叫許可 | 保留紀錄，重新執行 `gates` |
| `NORMALIZATION_REFUSED` | 標準化後的數值超出 ±8 | 表單檢查應已先拒絕此輸入；確認 `/opt/edgefhe-demo/model` 與 repository 相同 |
| `MODEL_UNKNOWN`、`WIRE_*`、`RESPONSE_*` | 加密程式不認得 CDC 模型，或雲端回應未通過格式與綁定檢查 | 確認板上的建置來自疊加樹，且板上、雲端映像與第 6.1 節的 staged_tree_id 相同（雲端的 `server.log` 可能另有 `MODEL_SOURCE_ID_UNKNOWN` 或 `WIRE_PROFILE_OR_EMPTY_ID`） |
| `OWNER_*`、`CSV_*`、`PRIVATE_*` | 金鑰、加密、解密或私有檔案的步驟發生錯誤 | 保留紀錄，重新執行預檢與 `gates` |
| `PUBLIC_AFFINE_ORACLE` | 公開測試資料的推論誤差超過 2×10⁻³（`gates`） | 保留紀錄，回報開發人員 |

表單拒絕代碼（不啟動任何程序，也不傳送任何資料）：`FORM_FIELDS`（欄位不是恰好四個）、`GENHLTH_VALUE`、`HIGHBP_VALUE`、`AGE_VALUE`（選項不在表單之內）、`BMI_FORMAT`（不是數值，或超過一位小數）、`BMI_BELOW_MIN`（低於 12.0）、`BMI_ABOVE_MAX`（高於 81.0，標準化後的絕對值超過 8）、`AGE_UNDER_18`（未滿 18 歲）。

### 14.2 閘道指令的結束狀態與服務標記

- 結束狀態：0 通過；1 執行或檢查失敗；2 表單在本機被拒絕；3 閘道未啟動執行（`GATEWAY_RUN_NOT_STARTED code=…`：未以 root 執行、忙碌中、執行鎖被占用或無法安全使用、PID 1、FPGA 裝置被占用、後端不允許）；4 用法、設定或模型錯誤（`GATEWAY_CONFIG_INVALID code=…`、`GATEWAY_MODEL_INVALID`）。
- `serve`：成功時為 `GATEWAY_SERVING 127.0.0.1:8080`（PC 演練為 `0.0.0.0:8080`）。`GATEWAY_LISTEN_REFUSED`（結束狀態 4）的 `code=LISTEN_NOT_LOOPBACK` 表示設定的 `listen` 不是 127.0.0.1，`code=REHEARSAL_SWITCHES` 表示 `--rehearsal-listen 0.0.0.0` 與環境變數 `EDGEFHE_REHEARSAL=1` 沒有同時指定，`code=REHEARSAL_ON_BOARD` 表示在有 FPGA 裝置的 KV260 上使用了演練模式；`GATEWAY_SERVE_FAILED code=LISTEN_BIND`（結束狀態 1）表示連接埠已被占用；`GATEWAY_USAGE`（結束狀態 4）表示參數錯誤；`GATEWAY_STOP_REQUESTED signal=…` 之後，閘道等執行中的表單結束才以 0 退出。
- `submit`：成功時為 `GATEWAY_SUBMIT_RESULT run= backend= level= …`，失敗時為 `GATEWAY_SUBMIT_REFUSED`、`GATEWAY_SUBMIT_STOP` 或 `GATEWAY_SUBMIT_FAILED`（`code=`）。標準輸出只有公開事件（`EVT` 行，`--quiet` 時沒有）；`--owner-events` 另印出擁有者事件，只供本機除錯（第 3 節第 6 點）。
- `disclose`：`GATEWAY_DISCLOSURE_NOT_FOUND` 表示私有紀錄已不存在（例如已重新開機或 `retain_private` 為 `false`）。

### 14.3 HTTP API 的拒絕代碼

| HTTP 狀態 | 代碼 | 意義 |
|---|---|---|
| 403 | `HOST_FORBIDDEN`、`CLIENT_FORBIDDEN`、`ORIGIN_FORBIDDEN` | Host 不是 `127.0.0.1:PORT` 或 `localhost:PORT`、用戶端不在本機、Origin 不是同源 |
| 403 | `OWNER_HEADER_REQUIRED`、`CONTENT_TYPE_JSON`、`BODY_LENGTH_REQUIRED`、`BODY_TOO_LARGE` | POST 缺少 `X-EdgeFHE-Owner: 1`、不是 JSON、缺少長度或超過 4 KiB |
| 400 | `BODY_JSON`、`BODY_FIELDS`、`BODY_INCOMPLETE`、`QUERY_INVALID`、表單拒絕代碼、`BACKEND_NOT_ALLOWED` | 內容格式錯誤，或表單被拒絕 |
| 404、405 | `NOT_FOUND`、`RUN_UNKNOWN`、`METHOD_NOT_ALLOWED` | 路徑或執行編號不存在、方法不允許 |
| 409 | `BUSY`、`PL_LOCK_BUSY`、`PL_DEVICE_BUSY` | 另一筆表單正在執行，或 FPGA 裝置被占用 |
| 503 | `GATEWAY_STOPPING`、`ROOT_REQUIRED`、`GATEWAY_PID1`、`WORKDIR_UNSAFE`、`PL_LOCK_UNSAFE`、`REHEARSAL_PL_ON_BOARD`、`STREAMS_BUSY` | 閘道正在停止、無法安全執行（含執行鎖無法安全使用，以及演練模式偵測到 KV260 而拒絕 pl 後端），或同時開啟的事件串流超過 8 條 |

### 14.4 其他工具的代碼

| 工具與標記 | 代碼與處置 |
|---|---|
| 疊加程式：`EDGEFHE_DEMO_OVERLAY_FAIL <CODE>`、`OVERLAY_CHECK_FAIL` | `OUT_EXISTS`、`RECORD_EXISTS`：刪除既有的輸出目錄與紀錄，或改用新的 `--out`；`OUT_INSIDE_REPO`：輸出目錄必須在 repository 之外；`COMMIT_FORMAT`：`--commit` 只接受英數字與 `._-`；`BASE_VERIFY`、`BASE_TREE`、`BASE_HASH`：`v8b_rev5/` 不完整或含有多餘檔案（例如 `__pycache__`）；`EXPECTED_FORMAT`、`EXPECTED_MISMATCH`、`MODEL_JSON`、`CATALOGUE_LAYOUT`、`CATALOGUE_LINE`、`STAGED_*`、`TREE_DIFF`：`demo/model` 與 `v8b_rev5/` 不一致，應重新取得兩者；`NOT_REPO_ROOT`、`RECORD`、`IO`：路徑或檔案系統錯誤 |
| 雲端啟動程式：`EDGEFHE_CLOUD_SERVER_REFUSED <CODE>`（結束狀態 64，未寫入任何證據） | `PIN_MISSING`：板端憑證尚未簽發（第 7.4 節）；`EVIDENCE_EXISTS`：改用新的證據資料夾；`BUILD_INTEGRITY`：重新建置映像；`PKI_MISSING`、`PKI_FORMAT`、`PIN_FORMAT`：以 `-v C:/EdgeFHE_demo/pki/cloud:/pki:ro` 掛載正確的目錄；`EVIDENCE_MISSING`、`EVIDENCE_READONLY`：掛載可寫入的證據資料夾；`USAGE`、`TOOL_MISSING`、`SERVER_MISSING`、`BUILD_RECORD_MISSING`、`SHIM_MISSING`、`LIBRARY_MISSING`：映像不完整，應重新建置 |
| 金鑰紀錄墊片 | `STOP_DEMO_TLS_KEYLOG_OPEN`、`STOP_DEMO_TLS_KEYLOG_RESOLVE`（結束狀態 3）：金鑰紀錄檔已存在或無法建立；`STOP_DEMO_TLS_KEYLOG_WRITE`（結束狀態 4）：寫入失敗，該場次作廢 |
| 迴路演練：`EDGEFHE_LOOPBACK_REHEARSAL_FAIL <CODE>` | `EVIDENCE_NOT_EMPTY`：改用新的或空的資料夾；`PKI_MOUNTED`：不得掛載 `/pki`；`CAPTURE_DROPS`：重新執行；其餘代碼（`SCLIENT_*`、`SHIM_KEYLOG_*`、`PROBE`、`SERVER_*`）：保留輸出並回報開發人員 |
| PC 演練：`SIM_REHEARSAL_FAIL <CODE>`、`SIM_*_FAIL <CODE>` | `NOT_UNDER_INIT`：以 `docker run --init` 執行；`EVIDENCE_NOT_EMPTY`：改用新的或空的資料夾；`ON_BOARD`：PC 演練不得在板上執行；`BUILD_INTEGRITY`、`IMAGE_INCOMPLETE`：重新建置映像；`INTERRUPTED`：演練被中斷，證據不完整；其餘代碼：保留輸出並回報開發人員 |
| 示範 PKI：`EDGEFHE_DEMO_PKI_REFUSED <CODE>`（結束狀態 2）、`EDGEFHE_DEMO_PKI_FAILED <STEP>`（結束狀態 1） | `OUT_EXISTS`：改用新的輸出目錄；`OUT_IN_GIT_WORK_TREE`：輸出目錄不得位於 git 工作樹內；`CLIENT_ALREADY_SIGNED`：本 PKI 已簽發過板端憑證（見第 7.4 節）；`CSR_SIGNATURE`、`CSR_KEY_TYPE`、`CSR_KEY_REUSED`、`CSR_SIZE`：板上必須以第 7.4 節的指令重新產生 CSR；`CA_MISSING`、`CA_MISMATCH`：`sign-client` 必須指向 `ca-and-server` 建立的目錄；`USAGE`：腳本必須以 `bash` 執行 |
| 封存程式：`EFHE_SEAL_REFUSED …`（結束狀態 2）、`EFHE_SEAL_ORDER_WARNING` | `reason=RUN_INSIDE_REPOSITORY`：證據資料夾必須位於 repository 之外；找不到 `capture.pcapng`：確認 `--run` 的路徑；`EFHE_CAPTURE_SEAL_MISMATCH`：擷取檔與封存紀錄不一致，操作人員應保留資料夾並回報開發人員；`EFHE_SEAL_ORDER_WARNING`：封存晚於金鑰紀錄的使用，見第 9 節的 G0 |
| 證據主控台：`EVIDENCE_CONSOLE_REFUSED <CODE>`（結束狀態 2） | `CONTAINER_RUNNING`：先停止同名的容器；`RUN_EXISTS`、`RUN_IN_REPO`、`RUN_CHANGED`：改用 repository 之外的新資料夾；`RUN_CREATE`：主控台無法建立場次資料夾，應檢查路徑與上層資料夾的權限；`RUN_ACL`、`PKI_ACL`：主控台無法把場次資料夾或 PKI 目錄限制為只允許目前使用者與 SYSTEM 存取，操作人員應依第 6.3.1 節處理後再啟動；`PKI`、`PKI_MISSING`：PKI 目錄不存在或不完整（例如 `cloud\client.pin` 尚未存在）；`DOCKER_UNAVAILABLE`、`IMAGE_MISSING`：啟動 Docker Desktop 或載入映像；`SERVER_REFUSED`：見雲端啟動程式的代碼；`CONFIG_*`：修正 `console.json`；`LISTEN_BIND`：8765 埠已被占用 |
| 擁有者橋接：`OWNER_BRIDGE_FAILED code=…`、`OWNER_BRIDGE_USAGE code=…`、`OWNER_BRIDGE_REFUSED code=…` | `OWNER_LAPTOP`：在擁有者筆電上加上 `--i-am-the-owner-laptop` 重新啟動；`CLOUD_PC`：本機是雲端電腦，或帶有雲端電腦的資料夾、容器或證據主控台，橋接程式不得在此執行（第 7.11 節）；`TRANSPORT`：序列埠無法開啟或被其他程式占用；`LINK_TIMEOUT`：登入或中繼未回應，檢查 `KV_PW` 與主控台；`LISTEN_BIND`：8780 埠已被占用；`BRIDGE_SUBMIT_UNCERTAIN`（502／504）：送出結果不明，橋接程式不重送，操作人員應以畫面上的執行紀錄確認 |
| 板上檔案工具：`BOARD_FILES_REFUSED code=…`（結束狀態 2）、`BOARD_FILES_FAILED code=…`（結束狀態 1） | `KV_PW_MISSING`：設定 `KV_PW`；`KV_CONSOLE`：`energy_probe/kv260/kv_console.py` 不是含密碼遮罩的版本；`PORT_OPEN`：關閉占用 COM6 的程式；`NOT_SEALED`、`SEAL_MISMATCH`：先封存擷取檔；`OUTPUT_EXISTS`：不覆寫既有檔案；`OUTPUT_IN_REPOSITORY`：`fetch-file` 的目的地、`fetch-public` 的場次資料夾與 `csr-roundtrip` 的 PKI 目錄都不得位於 repository 之內，應改用 `C:\EdgeFHE_evidence\<場次>` 或 `C:\EdgeFHE_demo\pki`；`KEY_STAYS_ON_BOARD`、`PRIVATE_STAYS_ON_BOARD`：`fetch-file` 不取回任何私鑰、TLS 金鑰紀錄或閘道的私有紀錄（板端 PKI 目錄只允許取回 `ca.pem`、`client.pem`、`client.csr` 與 `server.pin`）；`KEY_CHECK`：KV260 未完成取回前的金鑰檢查，該檔案未被取回；`PKI_SIGNED`：本 PKI 已簽發過板端憑證；`GIT_BASH`：安裝 Git for Windows；`LOGIN`、`SUDO`、`CONSOLE`：檢查主控台與密碼 |

## 15. 展示前的確認事項與待決事項

展示前的確認事項：

- 硬體：展示路由器（展示當天拔除 WAN 線）與網路線；雲端電腦沒有乙太網路埠，建議使用 USB 3 Gigabit 乙太網路轉接器。選用：可設定鏡像埠的交換器與觀察者筆電。
- 路由器：KV260 與雲端電腦的 DHCP 保留位址；關閉 AP 隔離或用戶端隔離。
- 雲端電腦：第 7.6 節的防火牆規則；Docker Desktop 保持執行並關閉 Resource Saver；接上電源並停用睡眠（`powercfg /change standby-timeout-ac 0`）；中斷 FortiClient；確認沒有啟用 Tailscale 出口節點。
- 擁有者畫面：KV260 接上 DisplayPort 螢幕與 USB 鍵盤滑鼠並登入桌面；不可行時準備擁有者筆電（第 7.11 節）。
- 投影：擁有者畫面與雲端畫面是兩個獨立的影像來源。
- 帳號：主控台與 sudo 的密碼由操作人員親自輸入；`KV_PW` 只設定在當次的 PowerShell 工作階段，不寫入任何檔案。

尚待團隊決定的事項：

1. 風險等級的切點與名稱，以及測試資料觀察比例的呈現方式：目前的提案以類別平衡模型的 sigmoid 值 0.30、0.50、0.70 為切點（logit −0.8473、0、+0.8473；此值不是校準過的機率，畫面也不顯示），名稱為較低、中低、中高、較高。擁有者畫面一律只顯示風險等級，不顯示機率。各等級在測試資料中的觀察比例若出現在畫面上，必須標示為資料集統計（測試資料中同一等級群體的比例），不是填答者本人罹患糖尿病的機率，也不是診斷；目前擁有者畫面在結果卡上顯示此比例，並附「資料集統計」標籤與上述說明。團隊尚須決定此比例保留在結果卡，或只顯示在「技術細節」中。變更切點時，操作人員以 `python -I -B demo\model\convert_cdc_to_v8b.py --repo . --levels --write` 重新計算（需要本機的資料切分），再重新執行第 6 節。
2. TLS 金鑰紀錄的保存：[團隊脈絡文件](../docs/notes/2026-11-23_team_context.md)載明金鑰紀錄只用於展示、結束後刪除；然而第三方必須取得金鑰紀錄，才能重新驗證 G2 至 G8。本手冊建議只在封存後、只含虛構答案的證據資料夾內保存，並於最後一場展示結束後銷毀所有私鑰與其他金鑰紀錄副本（第 12 節）。
3. `retain_private` 的設定：本手冊的預設值為 `false`，閘道不保存任何私有紀錄；只有使用虛構答案的證據場次，才由擁有者操作人員依第 7.10 節開啟。團隊尚須決定：證據場次是否開啟此設定（開啟才能以揭露檔完成 G7、G9 與 G10 並得到 `EFHE_EVIDENCE_PASS`；不開啟時，這三項為 `INCOMPLETE`），以及取回的揭露檔是否隨證據資料夾長期保存。
4. 擁有者畫面的模式：KV260 桌面（第 7.10 節）或擁有者筆電（第 7.11 節），依第 7.1 節的檢查結果決定。
5. 觀察者的人選：G11 需要一位以鏡像交換器另行擷取封包的觀察者，本團隊建議由團隊以外的人員擔任。沒有觀察者時，雲端電腦同時是擷取者與金鑰紀錄的持有者，`report.html` 與簡報都必須載明此限制。
6. PL 的啟動方式：本手冊的做法是由擁有者操作人員在 KV260 主控台親自輸入第 7.8 節的每一行指令（載入 bitstream、`insmod`、持續加密檢查與自我測試），符合 [v8b_rev5/README.md](../v8b_rev5/README.md) 使用原則第 4 點；本目錄的工具不會自動執行這些動作。團隊若改為授權自動化執行，應事先以書面記錄授權的範圍（哪些指令、由誰啟動、由誰在場監督）；自動化同樣不得對執行中的加密程式送出訊號（第 3 節第 2 點）。
7. 揭露檔、私有紀錄與執行紀錄的範圍：目前的 `disclose` 接受任何仍保存的私有紀錄，並一律在揭露檔中標示 `fictional: true`；閘道不檢查答案是否為虛構。團隊尚須決定三件事：(a) `disclose` 是否拒絕與預設表單（第 8.3 節）不同的答案，除非操作人員另加 `--confirm-fictional`（若採用，PC 演練的 `drive_presets.py --disclose` 須同步調整）；(b) 私有紀錄是否縮減為 G9 與 G10 所需的欄位（鹽值、承諾所依據的 canonical JSON、`logit_ckks`、請求與回應的雜湊），不再保存 CSV、標準化值、各特徵的貢獻、明文分數與等級；(c) 閘道是否在 `wiped` 事件之後自記憶體移除該筆表單的擁有者事件，或擁有者畫面是否只顯示本頁送出的表單（第 7.10 節）。決定之前，所有場次只填寫虛構答案，證據場次以外一律維持 `retain_private: false`。
8. 畫面上的模型識別碼與報告格式名稱：擁有者畫面顯示「CDC 糖尿病・63cee36f」，證據主控台的「解開 TLS 之後」面板顯示模型名稱 `cdc-diabetes-lr4-v1` 與 model_id 的前 16 個十六進位字元，`report.html` 的頁尾顯示「報告結構版本 edgefhe.demo.verify-report.v1」。這些是識別碼，不是版本之間的比較；團隊尚須決定是否在各畫面統一為「CDC 糖尿病模型（SHA-256 63cee36f…）」，並把報告頁尾改為「報告格式」。
