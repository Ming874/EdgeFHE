# EdgeFHE 跨產業應用模型庫（2026-10-28）

共 **19 個模型、12 個領域**。所有模型都使用**同一組 CKKS 參數與同一個 bitstream**，換模型只要換模型卡，不需要改硬體。

## 每個模型都通過的關卡

1. **訓練方法**：只用訓練集擬合，驗證集選 λ 和分類切點，測試集只在最後使用一次。可能洩漏答案的欄位已排除，例如列號、時間戳、追蹤天數、孿生目標；帕金森資料依受試者分組切分。
2. **數學准入（訓練時就強制）**：λ 只能從符合下列條件的選項中挑選：
   - |w| ≤ 16、|b| < 4；
   - 在 |z| ≤ 8 時，CRT 至少還有 10 bits 的空間；
   - 部署精度門檻 ≥ 15σ；
   - 6σ ≤ 5e-4，對齊原生 QA 的誤差上限。
3. **實用性**：迴歸模型測試 R² ≥ 0.3；分類模型 AUROC ≥ 0.7，且平衡準確率 > 0.6。
4. **三方准入**（`validate_model_cards.py`）：模型卡、生成的 C++ 目錄、CKKS 預算三方一致，另有 9 個負控制全部擋下。
5. **審查過的 C++ CKKS 管線**：CPU 與 PL 逐位元規格兩種後端，各跑 2048 列 × 2 輪合成輸入，解密結果與明文仿射值的誤差 ≤ 5e-4。
6. **上板驗收**（stage `ZOO19_MODELS_5A11_V2`，boot `5a1163a9`）：19 個模型在同一次開機、同一個 bitstream 下，用 KV260 真實 PL 與兩機 mTLS（1 Gbps），各跑 CPU 與 PL 兩個 10 秒窗，共 39 條連線、42739 次 DMA；凍結審查器結果 `ZOO19_ORIGINALS_INDEPENDENT_REVIEW_PASS`。每個窗的最大誤差都低於該模型的 client 精度門檻（ridge 5e-4，其他 2e-3）。
7. **板上誤差與雜訊模型一致**（`completion_20261028/zoo_board_consistency.py`）：38 個窗各與 20 次相同請求數的獨立雜訊模擬比較，觀測/模擬中位數比值的中位數 0.987，範圍 0.86–1.23，PIT KS p = 0.916；結果 `ZOO19_BOARD_ERROR_CONSISTENT_WITH_NOISE_MODEL`。

| 領域 › 類別 | 應用 | 任務 | 特徵 | 測試表現 | 本機最大誤差 CPU / PL 規格 | 板上最大誤差 CPU / PL | 精度門檻 | 門檻/σ | CRT 空間 | 狀態 |
|---|---|---|---:|---|---|---|---:|---:|---:|---|
| 工業 › 設備預測維護 | 工業設備故障預測 | 分類 | 4 | 先前已審查 | 2.52e-04 / 2.02e-04 | 3.37e-04 / 3.51e-04 | 2e-03 | 47 | 22.1 | 已上板接受 |
| 醫療 › 糖尿病 | 糖尿病一年後進展 | 迴歸 | 10 | 先前已審查 | 6.20e-05 / 5.32e-05 | 8.21e-05 / 8.17e-05 | 5e-04 | 47 | 23.6 | 已上板接受 |
| 醫療 › 糖尿病 | 糖尿病早期症狀風險 | 分類 | 16 | 先前已審查 | 2.46e-04 / 2.92e-04 | 4.23e-04 / 4.40e-04 | 2e-03 | 36 | 21.1 | 已上板接受 |
| 金融 › 信用風險 | 信用卡違約風險 | 分類 | 23 | AUROC 0.724 | 4.38e-05 / 5.15e-05 | 6.80e-05 / 8.38e-05 | 2e-03 | 209 | 23.6 | 已上板接受 |
| 金融 › 鈔票鑑定 | 鈔票真偽（影像小波特徵） | 分類 | 4 | AUROC 1.000 | 2.41e-04 / 2.27e-04 | 5.22e-04 / 4.89e-04 | 2e-03 | 36 | 21.8 | 已上板接受 |
| 能源 › 建築節能 | 建築暖氣負載 | 迴歸 | 8 | R² 0.904 | 5.25e-05 / 5.87e-05 | 8.74e-05 / 1.04e-04 | 5e-04 | 42 | 23.8 | 已上板接受 |
| 能源 › 發電預測 | 複循環電廠滿載出力 | 迴歸 | 4 | R² 0.929 | 6.43e-05 / 6.47e-05 | 1.11e-04 / 1.20e-04 | 5e-04 | 40 | 24.4 | 已上板接受 |
| 能源 › 電網穩定 | 分散式電網不穩定風險 | 分類 | 12 | AUROC 0.896 | 2.72e-06 / 2.03e-06 | 3.62e-06 / 3.61e-06 | 2e-03 | 4366 | 28.0 | 已上板接受 |
| 醫療 › 心臟衰竭 | 心衰竭追蹤期死亡風險 | 分類 | 11 | AUROC 0.763 | 6.94e-05 / 5.04e-05 | 1.03e-04 / 9.55e-05 | 2e-03 | 178 | 23.5 | 已上板接受 |
| 醫療 › 孕產婦照護 | 孕產婦高風險 | 分類 | 6 | AUROC 0.922 | 1.49e-04 / 1.35e-04 | 2.34e-04 / 2.42e-04 | 2e-03 | 67 | 22.5 | 已上板接受 |
| 醫療 › 乳癌篩檢 | 乳房腫塊惡性（細胞核特徵） | 分類 | 30 | AUROC 0.998 | 8.92e-05 / 1.07e-04 | 1.42e-04 / 1.21e-04 | 2e-03 | 128 | 22.2 | 已上板接受 |
| 營建 › 材料強度 | 混凝土抗壓強度 | 迴歸 | 8 | R² 0.560 | 5.10e-05 / 6.14e-05 | 9.63e-05 / 1.21e-04 | 5e-04 | 41 | 23.6 | 已上板接受 |
| 不動產 › 房價估算 | 新店區單位房價 | 迴歸 | 6 | R² 0.684 | 3.44e-05 / 2.82e-05 | 5.22e-05 / 5.20e-05 | 5e-04 | 84 | 24.6 | 已上板接受 |
| 食品 › 品質分級 | 葡萄酒高品質（≥7） | 分類 | 12 | AUROC 0.812 | 1.53e-04 / 1.19e-04 | 1.98e-04 / 2.54e-04 | 2e-03 | 75 | 22.1 | 已上板接受 |
| 農業 › 品種辨識 | 稻米 Cammeo 品種 | 分類 | 7 | AUROC 0.976 | 1.99e-04 / 1.75e-04 | 2.76e-04 / 3.27e-04 | 2e-03 | 58 | 22.1 | 已上板接受 |
| 漁業 › 年齡估算 | 鮑魚年輪數 | 迴歸 | 9 | R² 0.566 | 1.25e-04 / 1.21e-04 | 2.22e-04 / 2.25e-04 | 5e-04 | 18 | 22.5 | 已上板接受 |
| 交通 › 車輛油耗 | 汽車油耗（mpg） | 迴歸 | 8 | R² 0.850 | 6.22e-05 / 5.39e-05 | 9.32e-05 / 9.15e-05 | 5e-04 | 41 | 23.7 | 已上板接受 |
| 資訊安全 › 釣魚網站 | 釣魚網站偵測 | 分類 | 30 | AUROC 0.981 | 2.16e-04 / 2.14e-04 | 3.90e-04 / 3.52e-04 | 2e-03 | 41 | 20.9 | 已上板接受 |
| 智慧建築 › 人員偵測 | 室內有人偵測 | 分類 | 5 | AUROC 0.996 | 1.98e-04 / 2.28e-04 | 3.49e-04 / 3.47e-04 | 2e-03 | 48 | 22.6 | 已上板接受 |

## 被排除或更正的模型（失敗保留）

- `medical-parkinsons-updrs-ridge-v1`：ADMISSIBLE_BUT_NOT_USEFUL_EXCLUDED；測試 R² = -0.028。依受試者分組切分後，對沒見過的受試者沒有預測力，所以不納入。
- `finance-banknote-auth-logreg-v1`：原生 C++ 管線的誤差超過 5e-4，被數學檢查擋下。改加更強的正則化後產生 v2（AUROC 1.000，最大誤差 2.4e-4）。v1 的檔案保留，但不使用。

## 資料來源與授權（CC BY 4.0 須標示出處）

- Default of Credit Card Clients（UCI id 350）：https://archive.ics.uci.edu/static/public/350/data.csv，SHA-256 `45bcf4df62ff2e23…`
- Banknote Authentication（UCI id 267）：https://archive.ics.uci.edu/static/public/267/data.csv，SHA-256 `0787e50299c198e9…`
- Energy Efficiency（UCI id 242）：https://archive.ics.uci.edu/static/public/242/data.csv，SHA-256 `db44dbe453acd464…`
- Combined Cycle Power Plant（UCI id 294）：https://archive.ics.uci.edu/static/public/294/data.csv，SHA-256 `3c1fc11025f8424f…`
- Electrical Grid Stability Simulated Data （UCI id 471）：https://archive.ics.uci.edu/static/public/471/data.csv，SHA-256 `ec7af733b64aa82d…`
- Heart Failure Clinical Records（UCI id 519）：https://archive.ics.uci.edu/static/public/519/data.csv，SHA-256 `54349729933cffdd…`
- Parkinsons Telemonitoring（UCI id 189）：https://archive.ics.uci.edu/static/public/189/data.csv，SHA-256 `689fd221159cd78b…`
- Maternal Health Risk（UCI id 863）：https://archive.ics.uci.edu/static/public/863/data.csv，SHA-256 `a1f7025719f84715…`
- Concrete Compressive Strength（UCI id 165）：https://archive.ics.uci.edu/static/public/165/data.csv，SHA-256 `8d4b15b6fc68cd93…`
- Real Estate Valuation（UCI id 477）：https://archive.ics.uci.edu/static/public/477/data.csv，SHA-256 `0730a5687ec285a3…`
- Wine Quality（UCI id 186）：https://archive.ics.uci.edu/static/public/186/data.csv，SHA-256 `428bd8df7313b159…`
- Rice (Cammeo and Osmancik)（UCI id 545）：https://archive.ics.uci.edu/static/public/545/data.csv，SHA-256 `a0d9b19693f0bde9…`
- Abalone（UCI id 1）：https://archive.ics.uci.edu/static/public/1/data.csv，SHA-256 `25f3dc964447fcb3…`
- Auto MPG（UCI id 9）：https://archive.ics.uci.edu/static/public/9/data.csv，SHA-256 `240f4657d747fe10…`
- Phishing Websites（UCI id 327）：https://archive.ics.uci.edu/static/public/327/data.csv，SHA-256 `35dd9aa6870643c2…`
- Occupancy Detection （UCI id 357）：https://archive.ics.uci.edu/static/public/357/data.csv，SHA-256 `3648f2aa7026c24b…`
- Breast Cancer Wisconsin (Diagnostic)（UCI id 17）：使用 scikit-learn 內建的副本。

使用時須標示 UCI Machine Learning Repository 及各資料集作者。各資料集的授權仍以 UCI 頁面為準；本機只記錄了 API 中繼資料的雜湊。

## 尚未完成與限制

- 上板驗收是**功能驗收**：每個模型、每個後端只有一個 10 秒窗，使用合成輸入。前端模型庫頁顯示的 PL 筆/秒不構成加速比或效能宣稱；長時間穩定性只有原本 2 個模型做過六小時長測。
- 模型卡的 `slot_sigma` 是每個 slot 的標準差，不是窗內最大誤差的上限：板上窗最大值為它的 7.1–10.0 倍，是否與雜訊模型相符見上方第 7 點。准入依據的是精度門檻 ≥ 15σ。
- 這些都是研究用模型，**不代表已核准用於臨床、金融授信、工業或保全部署**。各領域實際導入前，需要該領域的資料、驗證與法規審查。
- 只支援仿射深度 1 的模型。樹模型、神經網路和需要在密文上做比較的模型，不在目前的參數與範圍內。
