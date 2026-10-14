# 上板結果 / Board results

每次上板一個資料夾，裡面放板上 `build/comparison/`、`build/trace/` 的輸出與關卡紀錄，再加一份 `NOTE.md` 說明這次做了什麼。

| 資料夾 | 內容 |
|---|---|
| [2026-10-14-kv260/](2026-10-14-kv260/NOTE.md) | **第一次實板**：部署關卡 A–G、實板一致性 `V5_PARITY_PASS device=linux-dma`、階段 2 完整 A/B 量測 `V5_STAGE2_COMPLETE`（`stage2_02/comparison.md`）；同日第二輪：平行編碼再測（`stage2_03/comparison.md`）、每次 FPGA 呼叫拆解（`dma_trace_01/breakdown.md`） |

## 從板子帶回來 / Bringing results back

板子沒有網路時用序列埠傳回（`tools/kv260/README.md`）：先在板上打包（不含 `bin/`、`obj/`，`stage_report.json` 已記錄執行檔的 SHA-256），再用 `receive_file.ps1` 收檔並比對 SHA-256。

```bash
# 板上
tar -czf ~/results.tgz -C ~/Grad/build --exclude=bin --exclude=obj comparison/stage2_01
```

```powershell
# 電腦（Grad 資料夾）
powershell -ExecutionPolicy Bypass -File tools\kv260\receive_file.ps1 -RemotePath /home/ubuntu/results.tgz -LocalFile results.tgz
```

## 合併成一份報表 / Merging into one report

同一塊板子、同一組設定的多份結果可以合併（在 repo 根目錄執行；工具會先檢查資料、模型、機器、綁核、隨機數來源與量測設定是否相同）：

```bash
python3 tools/compare_results.py results/<日期>/stage1_01/e2e_cpu.json results/<日期>/stage2_01/e2e_all.json --output results/<日期>/comparison_all
```

dashboard 會自動顯示 `results/` 底下最新的 `comparison*.json`。

## 規則 / Rules

- 結果檔是證據：不要手改；重跑就用新的資料夾名稱（`_02`、`_03`）。
- `device_times_are_fpga: false` 的結果來自模擬裝置，不能當成 FPGA 數據。
- 對外報告時寫清楚「KV260 實測」與「電腦上量測」。

完整流程見 [上板指南](../docs/BOARD_GUIDE.md) 與 [兩組對照](../docs/COMPARISON.md)。
