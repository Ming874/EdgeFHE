# V5 測試資料 / Synthetic test data

**全部是合成資料，不是 CDC 或任何真實個人資料。** 由固定種子（20261013）產生，只用來測試 V5 的讀檔、端到端流程與 CTest。

- `sample_records.csv`：300 筆、四個特徵欄位 `x0`–`x3`、標籤 `y`，以及一個不使用的 `note` 欄位（測試欄位挑選）。
- `sample_model.json`：公開的四權重邏輯迴歸模型，格式與 `training/outputs/model.json` 相同。
- `sample_preprocess.json`：標準化參數，格式與 `training/outputs/edge_preprocess.json` 相同。

真實比較使用 `data/cdc_diabetes/splits/test.csv` 與 `training/outputs/` 的模型，那些檔案在 repository 外面（見 [兩組對照](../../../../docs/COMPARISON.md)）。
