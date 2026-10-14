# 模型訓練與 CKKS 驗證

用 CDC 糖尿病資料（`../data/cdc_diabetes`）訓練 4 個特徵的邏輯斯迴歸，再把測試集送進 V4 的 CKKS 程式（`software/native_ckks`），確認加密推論和明文推論的判斷一致。

## 目前結果

| 項目 | 結果 |
|---|---|
| 挑出的 4 個特徵 | 自評健康 `GenHlth`、高血壓 `HighBP`、`BMI`、年齡層 `Age` |
| 測試集 AUC | 0.807（驗證集 0.812） |
| 召回率／特異度 | 74.9%／71.4% |
| CKKS 最壞誤差上界 | 2.6 × 10⁻⁴（系統上限 5 × 10⁻⁴） |
| 明文 vs CKKS（38,052 筆） | 最大差 4.2 × 10⁻⁷；38,051 筆一致、0 筆不一致、1 筆不確定 |

只有 13.9% 是陽性，全部猜「無」就有 86% 準確率，所以主要看 AUC 與召回率。

## 環境

- **訓練**：Python 3.10 以上，加上 numpy 和 scikit-learn（版本固定在 `requirements.txt`）。Windows 或 Linux 都可以，不需要 GPU，訓練約 4 秒。
- **CKKS 驗證**：Linux 或 WSL，加上 g++。只用 Python 標準函式庫，直接呼叫 `software/native_ckks` 的 V4 C++ CKKS 程式。

```bash
cd C:/GitHub/Grad
python -m venv training/.venv
training/.venv/Scripts/python -m pip install -r training/requirements.txt   # Linux: training/.venv/bin/python
```

## 執行

```bash
# 1. 訓練（Windows）
training/.venv/Scripts/python training/train.py

# 2. CKKS 驗證（WSL；第一次會自動編譯 training/build/libedgefhe_native.so）
wsl -- bash -c "cd /mnt/c/GitHub/Grad && python3 -I training/ckks_check.py"
```

`ckks_check.py` 的流程：金鑰只產生一次；每 2048 筆為一批，依序做編碼、作業系統亂數取樣、加密；雲端步驟只拿到密文和公開模型；最後解密、解碼，並與明文 logit 逐筆比對。過程不開啟 FPGA 裝置，也不連網路，所以時間是這台電腦的 CPU 時間，不是 KV260 的數據。

## 產出（`outputs/`）

| 檔案 | 給誰用 | 內容 |
|---|---|---|
| `model.json` | 雲端 | 權重、偏差、輸入上限、誤差上限；格式同 [model.example.json](../software/native_ckks/model.example.json)，`ckks_check.py` 會用 V4 的 `read_model()` 驗證 |
| `edge_preprocess.json` | KV260 | 特徵順序、訓練集平均與標準差、截斷範圍 |
| `training_report.json` | 監控頁面 | 特徵挑選過程、各項指標、ROC 曲線、系統限制檢查 |
| `ckks_check.json` | 監控頁面 | 誤差、判斷一致性、各階段時間、每批結果 |

## 規則

- 平均與標準差只從訓練集算；4 個特徵在驗證集上逐步挑選；測試集只在最後評分一次。
- 系統限制必須全部通過：權重與偏差絕對值 ≤ 16、標準化後的輸入截斷在 ±16、CKKS 最壞誤差 ≤ 5 × 10⁻⁴。不通過時 `train.py` 會直接停止，不會輸出模型。
- 判斷規則固定為「logit > 0 判為有」，對應邊緣端程式的機率 > 0.5，不需要改 KV260 端程式。
