# EdgeFHE 安全參數與威脅模型評估（B02，2026-10-24）

這是研究原型的安全評估，不是產品安全認證。所有參數都從實際執行的原始碼讀出，估算由 [estimate_rlwe.py](estimate_rlwe.py) 計算，結果在 [ESTIMATE.json](ESTIMATE.json)。

## 1. 實際參數（從程式碼讀出，不是設計文件）

| 項目 | 值 | 出處 |
|---|---|---|
| 方案 | CKKS（RNS），公鑰加密 | `owner_v5_core.hpp`、`baseline/secure.cpp` |
| 環維度 N | 4096（2048 個 slot） | `baseline/core.hpp` |
| 模數 | 998244353 · 1004535809 · 469762049，**log₂Q = 88.6** | `baseline/core.hpp` 的 `primes` |
| 私鑰 s | **均勻三元 {−1, 0, 1}（稠密，不是稀疏）**，來源是作業系統熵 | `secure.cpp` 的 `uniform_ternary()` |
| 金鑰誤差 e、加密誤差 e₀ e₁ | 離散高斯，**σ = 4.5**，支撐 ±55 | `secure.cpp`（OpenFHE DGG）、`cdt63_table.hpp`（PL 端 CDT63） |
| 加密遮罩 u | 均勻三元 | `owner_v5_core.hpp` |
| PL 端亂數 | ChaCha20 串流＋CDT63 查表，種子來自作業系統熵；每個工作有獨立的 key／nonce | V8b 設計、`owner_v5_core.hpp` |
| 金鑰生命週期 | 每個程序產生新金鑰，私鑰只存在 KV260 的記憶體裡 | `owner.cpp` 的 `Context` |

## 2. 安全等級

**主要依據：HE 標準參數表**（HomomorphicEncryption.org Security Standard v1.1，Albrecht 等，2018；三元私鑰、σ≈3.2）。N = 4096 時：
- 128-bit 古典安全的上限是 log q ≤ 109；
- 128-bit 量子安全的上限是 log q ≤ 101。

我方 log₂Q = **88.6**，兩個上限都在範圍內。我方的 σ = 4.5 比表中假設的 3.2 大，誤差越大越難攻擊，所以這個差異對安全是有利的。

**輔助估算**（推導，不是 lattice-estimator）：用 2016 年 core-SVP 模型的 primal uSVP 攻擊，並對小私鑰做縮放。

| 參數點 | 需要的 BKZ 區塊大小 | 古典 core-SVP | 量子 core-SVP |
|---|---:|---:|---:|
| HE 標準 128-bit 的邊界（log q = 109，σ = 3.2） | 327 | 95.5 bits | 86.7 bits |
| **EdgeFHE（log q = 88.6，σ = 4.5）** | 444 | **129.6 bits** | **117.7 bits** |

core-SVP 模型刻意偏保守，所以連 HE 標準自己的 128-bit 邊界點在這個模型下也只算出 95.5 bits。重點是同一把尺下的相對位置：我方參數比 128-bit 邊界高出約 34 bits，屬於留有餘裕的設定。這裡沒有涵蓋 hybrid 和 dual 攻擊，也沒有用精細的 BKZ 成本模型；要正式宣稱安全位數，應該用 lattice-estimator 再確認。

同一次開機的 OpenFHE 對照（B1）用 `HEStd_128_classic` 參數，OpenFHE 自己選出 log Q = 91。這和我方的參數級別一致。

## 3. 威脅模型

| 角色 | 假設 | 能看到什麼 | 保護方式 |
|---|---|---|---|
| 資料擁有者（KV260＋本機主控台） | 可信 | 明文輸入、私鑰、解密結果 | 私鑰只在板上；暫存檔放在記憶體（v2），用完覆寫後刪除；證據檔不含輸入值和分數（每次都有審查） |
| 雲端服務（PC） | 誠實但好奇 | 密文、公開模型身分、密文雜湊、時間 | RLWE 語意安全；雲端沒有私鑰（`owner_secrets_available: false`） |
| 網路攻擊者（區網） | 可以竊聽或竄改 | 加密後的 TLS 流量、流量大小與時間 | TLS 1.3 雙向憑證，釘選對方的葉憑證；回應要對應請求雜湊、序號、模型和金鑰，否則拒絕 |
| 本機其他網頁 | 不可信 | — | 主控台只綁 127.0.0.1，並檢查 Host、Origin 和自訂標頭（DNS rebinding 與跨站請求的測試都通過） |

## 4. 已知限制（必須寫進報告）

1. **CKKS 解密結果不能交還給伺服器**：依 Li–Micciancio（EUROCRYPT 2021，參考 EF21），CKKS 在 IND-CPA^D 模型下，攻擊者如果拿到解密結果，可能還原私鑰。
   - 我方設計中解密結果只留在擁有者端，即時展示也只顯示在本機瀏覽器，所以這條攻擊路徑不成立。
   - 若未來把結果回傳雲端，必須先加噪（noise flooding），或改用 BFV／BGV。
2. **metadata 會外洩**：HE 和 TLS 都不會隱藏以下資訊：
   - 請求的時間與大小（密文大小等於特徵數 × 96 KiB，等於洩漏特徵數）；
   - 模型身分；
   - 請求頻率。
3. **側信道沒有評估**：沒有做功耗、電磁或時序分析。PL 取樣的 CDT 查表、ARM 端的 OpenFHE DGG 都不保證常數時間。
4. **只有 IND-CPA**：沒有完整性保護，惡意雲端可以回傳錯誤的結果。目前只能靠 TLS 綁定和明文參考比對發現錯誤，不能在不知道明文的情況下驗證計算正確。
5. **金鑰保護**：私鑰存在一般 DRAM，沒有 TEE 或安全元件；root 權限可以讀取。研究原型可以接受，產品不行。
6. **亂數**：依賴作業系統熵（`/dev/urandom` 外掛）和 ChaCha20。熵來源失敗時會停止，不會退回確定性亂數；這一點已有程式檢查。
7. **精度**：每把金鑰的精度證書與分類邊界政策只在研究範圍內驗證過，不代表醫療或工業上的保證。

## 5. 結論

- 參數（N = 4096、log Q = 88.6、稠密三元私鑰、σ = 4.5）落在 HE 標準 128-bit 古典與量子安全的範圍內，簡化估算也顯示有餘裕。
- 主要風險不在格密碼參數，而在 metadata、側信道、完整性，以及「解密結果不能回傳雲端」這條使用限制。
- 報告中應寫成：「研究參數符合 HE 標準 128-bit 表；未經第三方或 lattice-estimator 正式評估，不構成產品安全認證」。

## 出處

- Albrecht et al., Homomorphic Encryption Security Standard v1.1, HomomorphicEncryption.org, 2018（表 1：n=4096、三元私鑰）。
- Albrecht, Player, Scott, "On the concrete hardness of Learning with Errors", J. Math. Cryptol. 2015（參考 EF20）。
- Alkim, Ducas, Pöppelmann, Schwabe, "Post-quantum key exchange – a new hope", USENIX Security 2016（core-SVP 模型）。
- Li & Micciancio, "On the Security of Homomorphic Encryption on Approximate Numbers", EUROCRYPT 2021（參考 EF21）。
- Cheon, Kim, Kim, Song, CKKS, ASIACRYPT 2017（參考 EF11）；Cheon et al., Full RNS CKKS, SAC 2018（參考 EF12）。
