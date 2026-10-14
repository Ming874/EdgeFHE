# 模型監控頁面（Next.js）

顯示資料集、特徵挑選、模型參數與系統限制檢查、驗證與測試表現（ROC、混淆矩陣），以及 CKKS 加密推論和明文的比對結果。

資料直接讀取 `../data/cdc_diabetes/manifest.json` 和 `../training/outputs/*.json`，每次請求都重新讀檔。最下方的兩組對照（組 A 純軟體 vs 組 B FPGA）讀 `../results/` 底下最新的 `comparison*.json`，並標明是實板還是模擬裝置。重跑 `train.py` 或 `ckks_check.py` 之後，頁面每 10 秒會自動更新，也可以手動關閉自動更新。某份結果還沒產生時，頁面會顯示要執行的指令。

## 執行

需要 Node.js 20.9 以上（目前用 Node 26 測試過）。

```bash
cd C:/GitHub/Grad/dashboard
npm install
npm run dev        # 開發模式：http://localhost:3000
# 或
npm run build && npm run start
```

## 技術

- Next.js 16（App Router、Turbopack）、React 19、TypeScript 5.9；版本固定在 `package.json`。
- 圖表用 SVG 和 CSS 自己畫，沒有另外的圖表套件。
- 支援淺色與深色模式，會跟隨系統設定。

| 檔案 | 內容 |
|---|---|
| `lib/data.ts` | 讀取 JSON 與型別定義、特徵中文名稱 |
| `app/page.tsx` | 頁面各區塊 |
| `app/components/charts.tsx` | 指標卡、長條、ROC 曲線、混淆矩陣、每批誤差 |
| `app/components/AutoRefresh.tsx` | 自動更新開關（瀏覽器端元件） |
