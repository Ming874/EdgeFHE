# ZOO19 模型庫：19 個在密文上執行的模型 / Model Zoo

這裡是 V8 在 KV260 上實際跑過的 19 個模型：12 個領域，全部用公開資料集訓練。每個模型都是「權重 × 特徵 ＋ 偏差」的仿射模型（邏輯斯迴歸或 ridge 迴歸）。資料在板上加密，雲端只拿到密文，直接在密文上算出分數。換模型只要換模型卡，CKKS 參數與 bitstream 都不用改。

**English:** 19 public-weight affine models (logistic / ridge regression) across 12 domains, all trained on public datasets and run on encrypted inputs on the KV260. Every dataset, its authors, DOI and licence are listed in [section 4](#4-資料來源與授權--data-sources-and-licences).

> 研究用模型，**不能**用於臨床、授信、工業或保全決策。實際導入前，需要該領域的資料、驗證與法規審查。
> *Research models only; not approved for clinical, credit, industrial or security decisions.*

## 數字一覽 / At a glance

| 項目 | 內容 |
|---|---|
| 模型 | **19 個**上線；另有 2 個被排除，檔案保留（見[第 6 節](#6-被排除的模型--excluded-models)） |
| 領域 | 12 個：醫療、能源、金融、工業、營建、不動產、食品、農業、漁業、交通、資訊安全、智慧建築 |
| 資料來源 | 19 個公開資料集：18 個來自 [UCI Machine Learning Repository](https://archive.ics.uci.edu)，1 個來自 scikit-learn |
| 授權 | 18 個 UCI 資料集都是 **CC BY 4.0**（2026-11-11 逐一核對 UCI 頁面），使用時必須標示出處 |
| 模型類型 | 12 個分類（邏輯斯迴歸）、7 個迴歸（ridge 迴歸），仿射深度 1 |
| 加密參數 | 全部共用 N = 4096、Δ = 2³⁰、3 個 NTT 質數，與同一個 bitstream |
| 上板驗收 | 19 個模型在同一次開機（`5a1163a9`，rev4）、同一個 bitstream 上，用真實 PL 與兩機 mTLS 各跑 CPU 與 PL；獨立審查 PASS。ZOO19 v3 另在 rev5 上執行並審查通過（`../evidence/rev5/`） |
| 加密造成的誤差 | 板上最大誤差 3.6×10⁻⁶～5.2×10⁻⁴，全部低於各模型的門檻（迴歸 5×10⁻⁴、分類 2×10⁻³） |

## 1. 一筆資料怎麼被處理 / How one record is processed

```mermaid
flowchart LR
  A["資料擁有者<br/>輸入特徵"] --> B["KV260 ARM<br/>正規化、編碼"]
  B --> C["KV260 FPGA<br/>CKKS 公鑰加密"]
  C -- "只有密文<br/>（mTLS）" --> D["雲端<br/>Σ 權重×密文＋偏差"]
  D -- "加密的分數" --> E["資料擁有者<br/>解密、sigmoid、判斷"]
```

雲端只知道公開的模型權重，看不到輸入，也看不到結果。

## 2. 模型一覽 / Model list

所有表現都在**測試集**上計算。測試集只在最後用一次，λ 與分類切點都用驗證集決定。「板上最大誤差」是 KV260 解密後的結果與明文仿射值的最大差距。

| # | 領域 | 應用 | 任務 | 特徵 | 資料筆數（訓練／驗證／測試） | 測試表現 | 對照：不用模型直接猜 | 板上最大誤差 CPU／PL | 門檻 |
|---:|---|---|---|---:|---|---|---|---|---:|
| 1 | 工業／設備預測維護 | 工業設備故障預測 | 分類 | 4 | 6500／1500／2000 | AUROC 0.903、平衡準確率 76.4% | 猜多數類別 準確率 96.7%（模型 96.1%） | 3.37e-04／3.51e-04 | 2e-03 |
| 2 | 醫療／糖尿病 | 糖尿病一年後進展 | 迴歸 | 10 | 287／66／89 | R² 0.442、RMSE 57.7 | 猜平均值 RMSE 79.5 | 8.21e-05／8.17e-05 | 5e-04 |
| 3 | 醫療／糖尿病 | 糖尿病早期症狀風險 | 分類 | 16 | 353／67／100 | AUROC 0.998、平衡準確率 93.4% | 猜多數類別 準確率 47.0%（模型 93.0%） | 4.23e-04／4.40e-04 | 2e-03 |
| 4 | 金融／信用風險 | 信用卡違約風險 | 分類 | 23 | 19500／4500／6000 | AUROC 0.724、平衡準確率 68.9% | 猜多數類別 準確率 78.5%（模型 80.8%） | 6.80e-05／8.38e-05 | 2e-03 |
| 5 | 金融／鈔票鑑定 | 鈔票真偽（影像小波特徵） | 分類 | 4 | 891／206／275 | AUROC 1.000、平衡準確率 97.5% | 猜多數類別 準確率 56.4%（模型 97.8%） | 5.22e-04／4.89e-04 | 2e-03 |
| 6 | 能源／建築節能 | 建築暖氣負載 | 迴歸 | 8 | 499／115／154 | R² 0.904、RMSE 3.02 | 猜平均值 RMSE 9.82 | 8.74e-05／1.04e-04 | 5e-04 |
| 7 | 能源／發電預測 | 複循環電廠滿載出力 | 迴歸 | 4 | 6219／1435／1914 | R² 0.929、RMSE 4.53 | 猜平均值 RMSE 17 | 1.11e-04／1.20e-04 | 5e-04 |
| 8 | 能源／電網穩定 | 分散式電網不穩定風險 | 分類 | 12 | 6500／1500／2000 | AUROC 0.896、平衡準確率 80.0% | 猜多數類別 準確率 64.8%（模型 79.8%） | 3.62e-06／3.61e-06 | 2e-03 |
| 9 | 醫療／心臟衰竭 | 心衰竭追蹤期死亡風險 | 分類 | 11 | 194／45／60 | AUROC 0.763、平衡準確率 65.2% | 猜多數類別 準確率 63.3%（模型 71.7%） | 1.03e-04／9.55e-05 | 2e-03 |
| 10 | 醫療／孕產婦照護 | 孕產婦高風險 | 分類 | 6 | 659／152／203 | AUROC 0.922、平衡準確率 85.7% | 猜多數類別 準確率 69.0%（模型 85.7%） | 2.34e-04／2.42e-04 | 2e-03 |
| 11 | 醫療／乳癌篩檢 | 乳房腫塊惡性（細胞核特徵） | 分類 | 30 | 369／86／114 | AUROC 0.998、平衡準確率 98.1% | 猜多數類別 準確率 62.3%（模型 98.2%） | 1.42e-04／1.21e-04 | 2e-03 |
| 12 | 營建／材料強度 | 混凝土抗壓強度 | 迴歸 | 8 | 669／155／206 | R² 0.560、RMSE 10.4 | 猜平均值 RMSE 15.7 | 9.63e-05／1.21e-04 | 5e-04 |
| 13 | 不動產／房價估算 | 新店區單位房價 | 迴歸 | 6 | 269／62／83 | R² 0.684、RMSE 6.63 | 猜平均值 RMSE 11.8 | 5.22e-05／5.20e-05 | 5e-04 |
| 14 | 食品／品質分級 | 葡萄酒高品質（≥7） | 分類 | 12 | 4223／974／1300 | AUROC 0.812、平衡準確率 72.8% | 猜多數類別 準確率 79.6%（模型 77.5%） | 1.98e-04／2.54e-04 | 2e-03 |
| 15 | 農業／品種辨識 | 稻米 Cammeo 品種 | 分類 | 7 | 2476／572／762 | AUROC 0.976、平衡準確率 93.0% | 猜多數類別 準確率 59.4%（模型 93.4%） | 2.76e-04／3.27e-04 | 2e-03 |
| 16 | 漁業／年齡估算 | 鮑魚年輪數 | 迴歸 | 9 | 2715／626／836 | R² 0.566、RMSE 2.14 | 猜平均值 RMSE 3.25 | 2.22e-04／2.25e-04 | 5e-04 |
| 17 | 交通／車輛油耗 | 汽車油耗（mpg） | 迴歸 | 8 | 254／59／79 | R² 0.850、RMSE 3.44 | 猜平均值 RMSE 8.99 | 9.32e-05／9.15e-05 | 5e-04 |
| 18 | 資訊安全／釣魚網站 | 釣魚網站偵測 | 分類 | 30 | 7185／1659／2211 | AUROC 0.981、平衡準確率 92.6% | 猜多數類別 準確率 56.2%（模型 92.4%） | 3.90e-04／3.52e-04 | 2e-03 |
| 19 | 智慧建築／人員偵測 | 室內有人偵測 | 分類 | 5 | 13364／3084／4112 | AUROC 0.996、平衡準確率 99.2% | 猜多數類別 準確率 76.5%（模型 99.0%） | 3.49e-04／3.47e-04 | 2e-03 |

**怎麼看這張表**

- **「不用模型直接猜」是最低的對照**：分類模型和「永遠猜多數類別」比，迴歸模型和「永遠猜訓練集平均值」比。模型要明顯好過它才有用。
- **兩個要注意的模型**：
  - #1 工業設備故障：故障只占 3.4%，所以永遠猜「不會故障」就有 96.7% 準確率，比模型的 96.1% 還高。這個模型的價值在 AUROC 0.903 與平衡準確率 76.4%，也就是能把少數的故障找出來。
  - #14 葡萄酒：高品質只占約 20%，準確率同樣略低於猜多數類別，要看 AUROC 0.812 與平衡準確率 72.8%。
- **准入條件**：迴歸要求測試 R² ≥ 0.3；分類要求 AUROC ≥ 0.7，而且平衡準確率 > 0.6。
- **權重限制**：訓練時就限制 |w| ≤ 16、|b| < 4，確保加密運算不會溢位。完整的准入規則見 [zoo19/MODEL_ZOO_zh-TW.md](zoo19/MODEL_ZOO_zh-TW.md)。

## 3. 與外部資料對照 / Comparison with external results

### 3.1 和資料集原始論文比 / Against the original papers

只列出我們能從原始論文摘要直接核對到數字的資料集。驗證方式、特徵數與指標都不同，所以這是**定位**，不是嚴格的勝負比較。

| 資料集 | 原始論文報告的結果 | 本專案（在密文上推論） | 條件差異 |
|---|---|---|---|
| Rice (Cammeo and Osmancik) | Cinar & Koklu（2019）：7 個形態特徵、3,810 粒米。邏輯斯迴歸準確率 **93.02%**，是 7 種方法中最高的（其他 88.58%～92.86%） | 同樣 7 個特徵的邏輯斯迴歸，測試集 762 粒：準確率 **93.4%**、AUROC 0.976 | 資料切分方式不同；我們的結果是加密後再解密得到的 |
| Breast Cancer Wisconsin (Diagnostic) | Street、Wolberg、Mangasarian（1993）：只用 3 個特徵的單一線性分隔面，10-fold 交叉驗證準確率 **97%** | 30 個特徵的邏輯斯迴歸，測試集 114 筆：準確率 **98.2%**、AUROC 0.998 | 驗證方式（交叉驗證 vs 保留測試集）與特徵數不同 |

**解讀**：線性模型在密文上執行，表現和原始論文報告的線性方法在同一個水準。也就是說，加密沒有讓模型變差。其他資料集的原始論文在[第 4 節](#4-資料來源與授權--data-sources-and-licences)列出出處；我們沒能取得可核對的全文數字，所以不寫入比較。

### 3.2 加密有沒有改變結果 / Does encryption change the answer?

| 檢查 | 結果 | 出處 |
|---|---|---|
| 本機：C++ CKKS 管線（CPU 與 PL 逐位元規格） | 每個模型 2,048 列 × 2 輪，解密結果與明文仿射值的誤差 ≤ 5×10⁻⁴ | [zoo19/MODEL_ZOO_zh-TW.md](zoo19/MODEL_ZOO_zh-TW.md) |
| 板上：19 個模型 × CPU／PL | 最大誤差 3.6×10⁻⁶～5.2×10⁻⁴，全部低於各模型的門檻 | 第 2 節表格 |
| 板上誤差和雜訊模型一致嗎 | 38 個量測窗各與 20 次獨立模擬比較：觀測／模擬中位數比的中位數 **0.987**（範圍 0.86–1.23），KS 檢定 p = 0.916 | [zoo19/MODEL_ZOO_zh-TW.md](zoo19/MODEL_ZOO_zh-TW.md) |
| 精度餘裕 | 每個模型的門檻都是加密雜訊標準差的 **18～4,366 倍**（准入要求至少 15 倍） | 同上 |

### 3.3 加密速度和其他系統比 / Encryption speed vs other systems

系統層的對照（同板 OpenFHE、SEAL-Embedded、Aloha-HE、RISE 等）見 [../README.md 的「和外部研究、現成函式庫比較」](../README.md#26-和外部研究現成函式庫比較--compared-with-published-work-and-libraries)。

## 4. 資料來源與授權 / Data sources and licences

| # | 應用 | 資料集（UCI 頁面） | 筆數 | 資料集 DOI | 建立者 | 原始論文 |
|---:|---|---|---:|---|---|---|
| 1 | 工業設備故障預測 | [AI4I 2020 Predictive Maintenance Dataset](https://archive.ics.uci.edu/dataset/601/ai4i+2020+predictive+maintenance+dataset) | 10000 | [10.24432/C5HS5C](https://doi.org/10.24432/C5HS5C) | S. Matzka | S. Matzka，Explainable Artificial Intelligence for Predictive Maintenance Applications，*International Conference on Artificial Intelligence for Industries* 2020，[10.1109/AI4I49448.2020.00023](https://doi.org/10.1109/AI4I49448.2020.00023) |
| 2 | 糖尿病一年後進展 | [scikit-learn `load_diabetes`](https://scikit-learn.org/stable/datasets/toy_dataset.html#diabetes-dataset)（1.5.2 內附） | 442 | — | B. Efron、T. Hastie、I. Johnstone、R. Tibshirani | Least Angle Regression，*Annals of Statistics* 32(2), 2004，[10.1214/009053604000000067](https://doi.org/10.1214/009053604000000067) |
| 3 | 糖尿病早期症狀風險 | [Early Stage Diabetes Risk Prediction](https://archive.ics.uci.edu/dataset/529/early+stage+diabetes+risk+prediction+dataset) | 520 | [10.24432/C5VG8H](https://doi.org/10.24432/C5VG8H) | M. M. F. Islam, Rahatara Ferdousi, Sadikur Rahman, Humayra Yasmin Bushra | M. M. F. Islam, Rahatara Ferdousi, Sadikur Rahman, Humayra Yasmin Bushra，Likelihood Prediction of Diabetes at Early Stage Using Data Mining Techniques，*Computer Vision and Machine Intelligence in Medical Image Analysis* 2019，[10.1007/978-981-13-8798-2_12](https://doi.org/10.1007/978-981-13-8798-2_12) |
| 4 | 信用卡違約風險 | [Default of Credit Card Clients](https://archive.ics.uci.edu/dataset/350/default+of+credit+card+clients) | 30000 | [10.24432/C55S3H](https://doi.org/10.24432/C55S3H) | I-Cheng Yeh | I. Yeh, Che-hui Lien，The comparisons of data mining techniques for the predictive accuracy of probability of default of credit card clients，*Expert systems with applications* 2009，[10.1016/j.eswa.2007.12.020](https://doi.org/10.1016/j.eswa.2007.12.020) |
| 5 | 鈔票真偽（影像小波特徵） | [Banknote Authentication](https://archive.ics.uci.edu/dataset/267/banknote+authentication) | 1372 | [10.24432/C55P57](https://doi.org/10.24432/C55P57) | Volker Lohweg | —（UCI 未列原始論文） |
| 6 | 建築暖氣負載 | [Energy Efficiency](https://archive.ics.uci.edu/dataset/242/energy+efficiency) | 768 | [10.24432/C51307](https://doi.org/10.24432/C51307) | Athanasios Tsanas、Angeliki Xifara | A. Tsanas, Angeliki Xifara，Accurate quantitative estimation of energy performance of residential buildings using statistical machine learning tools，*Energy and Buildings, vol. 49* 2012，[10.1016/j.enbuild.2012.03.003](https://doi.org/10.1016/j.enbuild.2012.03.003) |
| 7 | 複循環電廠滿載出力 | [Combined Cycle Power Plant](https://archive.ics.uci.edu/dataset/294/combined+cycle+power+plant) | 9568 | [10.24432/C5002N](https://doi.org/10.24432/C5002N) | Pınar Tüfekci、Heysem Kaya | Pınar Tüfekci，Prediction of full load electrical power output of a base load operated combined cycle power plant using machine learning methods，*International Journal of Electrical Power & Energy Systems* 2014，[10.1016/j.ijepes.2014.02.027](https://doi.org/10.1016/j.ijepes.2014.02.027) |
| 8 | 分散式電網不穩定風險 | [Electrical Grid Stability Simulated Data](https://archive.ics.uci.edu/dataset/471/electrical+grid+stability+simulated+data) | 10000 | [10.24432/C5PG66](https://doi.org/10.24432/C5PG66) | Vadim Arzamasov | —（UCI 未列原始論文） |
| 9 | 心衰竭追蹤期死亡風險 | [Heart Failure Clinical Records](https://archive.ics.uci.edu/dataset/519/heart+failure+clinical+records) | 299 | [10.24432/C5Z89R](https://doi.org/10.24432/C5Z89R) | D. Chicco, Giuseppe Jurman | D. Chicco, Giuseppe Jurman，Machine learning can predict survival of patients with heart failure from serum creatinine and ejection fraction alone，*BMC Medical Informatics and Decision Making* 2020，[10.1186/s12911-020-1023-5](https://doi.org/10.1186/s12911-020-1023-5) |
| 10 | 孕產婦高風險 | [Maternal Health Risk](https://archive.ics.uci.edu/dataset/863/maternal+health+risk) | 1014 | [10.24432/C5DP5D](https://doi.org/10.24432/C5DP5D) | Marzia Ahmed | Marzia Ahmed, M. A. Kashem, Mostafijur Rahman, S. Khatun，Review and Analysis of Risk Factor of Maternal Health in Remote Area Using the Internet of Things (IoT)，*Lecture Notes in Electrical Engineering, vol 632* 2020，[10.1007/978-981-15-2317-5_30](https://doi.org/10.1007/978-981-15-2317-5_30) |
| 11 | 乳房腫塊惡性（細胞核特徵） | [Breast Cancer Wisconsin (Diagnostic)](https://archive.ics.uci.edu/dataset/17/breast+cancer+wisconsin+diagnostic)（使用 scikit-learn 內附的同一份資料） | 569 | [10.24432/C5DW2B](https://doi.org/10.24432/C5DW2B) | William Wolberg、Olvi Mangasarian、W. Nick Street | W. Street, W. Wolberg, O. Mangasarian，Nuclear feature extraction for breast tumor diagnosis，*Electronic imaging* 1993，[10.1117/12.148698](https://doi.org/10.1117/12.148698) |
| 12 | 混凝土抗壓強度 | [Concrete Compressive Strength](https://archive.ics.uci.edu/dataset/165/concrete+compressive+strength) | 1030 | [10.24432/C5PK67](https://doi.org/10.24432/C5PK67) | I-Cheng Yeh | I. Yeh，Modeling of strength of high-performance concrete using artificial neural networks，*Cement and Concrete Research, Vol. 28, No. 12* 1998，[10.1016/S0008-8846(98)00165-3](https://doi.org/10.1016/S0008-8846(98)00165-3) |
| 13 | 新店區單位房價 | [Real Estate Valuation](https://archive.ics.uci.edu/dataset/477/real+estate+valuation+data+set) | 414 | [10.24432/C5J30W](https://doi.org/10.24432/C5J30W) | I-Cheng Yeh | I. Yeh, Tzu-Kuang Hsu，Building real estate valuation models with comparative approach through case-based reasoning，*Applied Soft Computing* 2018，[10.1016/j.asoc.2018.01.029](https://doi.org/10.1016/j.asoc.2018.01.029) |
| 14 | 葡萄酒高品質（≥7） | [Wine Quality](https://archive.ics.uci.edu/dataset/186/wine+quality) | 6497 | [10.24432/C56S3T](https://doi.org/10.24432/C56S3T) | Paulo Cortez、A. Cerdeira、F. Almeida、T. Matos、J. Reis | P. Cortez, A. Cerdeira, Fernando Almeida, Telmo Matos, J. Reis，Modeling wine preferences by data mining from physicochemical properties，*Decision Support Systems* 2009，[10.1016/j.dss.2009.05.016](https://doi.org/10.1016/j.dss.2009.05.016) |
| 15 | 稻米 Cammeo 品種 | [Rice (Cammeo and Osmancik)](https://archive.ics.uci.edu/dataset/545/rice+cammeo+and+osmancik) | 3810 | [10.24432/C5MW4Z](https://doi.org/10.24432/C5MW4Z) | Ilkay Cinar, M. Koklu | Ilkay Cinar, M. Koklu，Classification of Rice Varieties Using Artificial Intelligence Methods，*International Journal of Intelligent Systems and Applications in Engineering* 2019，[10.18201/ijisae.2019355381](https://doi.org/10.18201/ijisae.2019355381) |
| 16 | 鮑魚年輪數 | [Abalone](https://archive.ics.uci.edu/dataset/1/abalone) | 4177 | [10.24432/C55C7W](https://doi.org/10.24432/C55C7W) | Warwick Nash、Tracy Sellers、Simon Talbot、Andrew Cawthorn、Wes Ford | —（UCI 未列原始論文） |
| 17 | 汽車油耗（mpg） | [Auto MPG](https://archive.ics.uci.edu/dataset/9/auto+mpg) | 392 | [10.24432/C5859H](https://doi.org/10.24432/C5859H) | R. Quinlan | —（UCI 未列原始論文） |
| 18 | 釣魚網站偵測 | [Phishing Websites](https://archive.ics.uci.edu/dataset/327/phishing+websites) | 11055 | [10.24432/C51W2X](https://doi.org/10.24432/C51W2X) | Rami Mohammad、Lee McCluskey | R. Mohammad, F. Thabtah, L. Mccluskey，An assessment of features related to phishing websites using an automated technique，*International Conference for Internet Technology and Secured Transactions* 2012 |
| 19 | 室內有人偵測 | [Occupancy Detection](https://archive.ics.uci.edu/dataset/357/occupancy+detection) | 20560 | [10.24432/C5X01N](https://doi.org/10.24432/C5X01N) | Luis Candanedo | L. Candanedo, V. Feldheim，Accurate occupancy detection of an office room from light, temperature, humidity and CO2 measurements using statistical learning models，*Energy and Buildings, Volume 112* 2016，[10.1016/j.enbuild.2015.11.071](https://doi.org/10.1016/j.enbuild.2015.11.071) |

**授權 / Licences**

| 來源 | 授權 | 說明 |
|---|---|---|
| 18 個 UCI 資料集（#1、#3–#19） | [Creative Commons Attribution 4.0（CC BY 4.0）](https://creativecommons.org/licenses/by/4.0/) | 2026-11-11 逐一核對每個 UCI 資料集頁面的授權標示 |
| #11 Breast Cancer Wisconsin | CC BY 4.0（UCI 原始資料） | 訓練時使用 scikit-learn 內附的同一份資料 |
| #2 糖尿病進展（scikit-learn `load_diabetes`） | 隨 scikit-learn 以 BSD-3-Clause 散布 | 原始資料出自 Efron 等（2004）；資料本身沒有另行標示授權條款 |

**使用這些資料或模型時請標示 / Required attribution**

- 標示資料集名稱、建立者與 DOI（見上表），以及 UCI Machine Learning Repository：Markelle Kelly, Rachel Longjohn, Kolby Nottingham, *The UCI Machine Learning Repository*, https://archive.ics.uci.edu（UCI 官方建議的引用格式）。
- **本專案做過的改動**：清理資料、選取欄位、切分訓練／驗證／測試集，再訓練模型。這些改動與結果不代表資料集作者的立場或背書。
- `*.public_test.csv` 是各資料集測試集的公開子集，用於展示與驗證，同樣適用原資料集的授權。

## 5. 檔案 / Files

| 路徑 | 內容 |
|---|---|
| `zoo19/model-cards.zoo.json` | 19 張模型卡：欄位名稱、單位、正規化參數、決策切點、資料集與下載網址 |
| `zoo19/models_v1/` | 18 個模型（16 個上線、2 個被排除）的權重 `*.json`、公開測試子集 `*.public_test.csv`，以及訓練紀錄 `ZOO_TRAINING.json` |
| `zoo19/models_release_v1/` | 較早的 3 個模型（#1–#3）的權重、測試指標 `*.metrics.json` 與公開測試子集 |
| `zoo19/MODEL_CARD_ADMISSION_ZOO.json` | 模型卡、C++ 目錄、CKKS 預算三方一致的准入結果 |
| `zoo19/MODEL_ZOO_zh-TW.md` | 完整的准入規則、本機與板上誤差、雜訊模型一致性 |
| `zoo19/fetch_sources.py`、`train_zoo.py` | 下載資料（並記錄 SHA-256）與訓練；需要網路與 Python 科學套件 |
| `zoo19/build_cards.py`、`build_doc.py`、`gen_zoo_summary.py`、`qa_native_zoo.py` | 產生模型卡、文件、摘要與原生 QA |

## 6. 被排除的模型 / Excluded models

失敗的結果照樣保留，檔案還在 `zoo19/models_v1/`，但不會部署。

| 模型 | 結果 | 原因 |
|---|---|---|
| `medical-parkinsons-updrs-ridge-v1`（[Parkinsons Telemonitoring](https://archive.ics.uci.edu/dataset/189/parkinsons+telemonitoring)，DOI [10.24432/C5ZS3N](https://doi.org/10.24432/C5ZS3N)，Tsanas、Little 等 2010） | 測試 R² = −0.028 | 依受試者分組切分後，對沒看過的受試者沒有預測力，所以不納入 |
| `finance-banknote-auth-logreg-v1` | 原生 C++ 管線的誤差超過 5×10⁻⁴，被數學檢查擋下 | 加強正則化後做出 v2（表中的 #5），最大誤差降到 2.4×10⁻⁴ |

## 7. 限制 / Limitations

- **只有仿射深度 1**：樹模型、神經網路，以及需要在密文上做比較的模型，都不在目前的參數範圍內。
- **上板是功能驗收**：每個模型、每個後端只跑一個 10 秒的量測窗，使用合成輸入。長時間穩定性只有原本 2 個模型（#1、#2）做過 6 小時長測。
- **模型保護**：FHE 保護輸入和結果，不保護模型；持有金鑰的人可以靠大量查詢推測模型權重。
- **不是醫療或金融產品**：這些都是研究模型，沒有經過任何領域的臨床、授信或法規審查。
