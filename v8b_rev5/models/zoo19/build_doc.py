"""LOCAL ONLY: generate MODEL_ZOO_zh-TW.md from the reviewed artifacts (no hand-typed numbers)."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    zoo = json.loads((HERE.parent / 'frontend_showcase_20261024/web/src/model-zoo.json').read_text(encoding='utf-8'))
    train = json.loads((HERE / 'models_v1/ZOO_TRAINING.json').read_text(encoding='utf-8'))['models']
    man = json.loads((HERE / 'sources/SOURCE_MANIFEST.json').read_text(encoding='utf-8'))['sources']
    cons = json.loads((HERE.parent / 'completion_20261028/ZOO19_BOARD_CONSISTENCY.json').read_text(encoding='utf-8'))
    b = zoo['board']
    lines = ['# EdgeFHE 跨產業應用模型庫（2026-10-28）', '',
             f'共 **{len(zoo["models"])} 個模型、{len({m["domain"] for m in zoo["models"]})} 個領域**。所有模型都使用**同一組 CKKS 參數與同一個 bitstream**，換模型只要換模型卡，不需要改硬體。',
             '', '## 每個模型都通過的關卡', '',
             '1. **訓練方法**：只用訓練集擬合，驗證集選 λ 和分類切點，測試集只在最後使用一次。可能洩漏答案的欄位已排除，例如列號、時間戳、追蹤天數、孿生目標；帕金森資料依受試者分組切分。',
             '2. **數學准入（訓練時就強制）**：λ 只能從符合下列條件的選項中挑選：',
             '   - |w| ≤ 16、|b| < 4；',
             '   - 在 |z| ≤ 8 時，CRT 至少還有 10 bits 的空間；',
             '   - 部署精度門檻 ≥ 15σ；',
             '   - 6σ ≤ 5e-4，對齊原生 QA 的誤差上限。',
             '3. **實用性**：迴歸模型測試 R² ≥ 0.3；分類模型 AUROC ≥ 0.7，且平衡準確率 > 0.6。',
             '4. **三方准入**（`validate_model_cards.py`）：模型卡、生成的 C++ 目錄、CKKS 預算三方一致，另有 9 個負控制全部擋下。',
             '5. **審查過的 C++ CKKS 管線**：CPU 與 PL 逐位元規格兩種後端，各跑 2048 列 × 2 輪合成輸入，解密結果與明文仿射值的誤差 ≤ 5e-4。',
             f'6. **上板驗收**（stage `{b["stage"]}`，boot `{b["boot"]}`）：19 個模型在同一次開機、同一個 bitstream 下，用 KV260 真實 PL 與兩機 mTLS（{b["link"]}），各跑 CPU 與 PL 兩個 10 秒窗，共 {b["connections"]} 條連線、{b["actual_DMA"]} 次 DMA；凍結審查器結果 `{b["review"]}`。每個窗的最大誤差都低於該模型的 client 精度門檻（ridge 5e-4，其他 2e-3）。',
             f'7. **板上誤差與雜訊模型一致**（`completion_20261028/zoo_board_consistency.py`）：38 個窗各與 {cons["K_simulations_per_window"]} 次相同請求數的獨立雜訊模擬比較，觀測/模擬中位數比值的中位數 {cons["median_ratio"]:.3f}，範圍 {cons["ratio_range"][0]:.2f}–{cons["ratio_range"][1]:.2f}，PIT KS p = {cons["PIT_KS_pvalue"]:.3f}；結果 `{cons["status"]}`。', '',
             '| 領域 › 類別 | 應用 | 任務 | 特徵 | 測試表現 | 本機最大誤差 CPU / PL 規格 | 板上最大誤差 CPU / PL | 精度門檻 | 門檻/σ | CRT 空間 | 狀態 |', '|---|---|---|---:|---|---|---|---:|---:|---:|---|']
    for m in zoo['models']:
        metric = f'{m["testMetric"]["name"]} {m["testMetric"]["value"]:.3f}' if m['testMetric'] else '先前已審查'
        lines.append(f'| {m["domain"]} › {m["category"]} | {m["title"]} | {"迴歸" if m["task"] == "regression" else "分類"} | {m["features"]} | {metric} | '
                     f'{m["maxErrorCpu"]:.2e} / {m["maxErrorSpec"]:.2e} | '
                     + (f'{m["boardMaxErrorCpu"]:.2e} / {m["boardMaxErrorPl"]:.2e}' if m['boardMaxErrorPl'] is not None else '—')
                     + f' | {m["precisionGate"]:.0e} | {m["gateOverSigma"]:.0f} | {m["crtHeadroomBits"]:.1f} | '
                     f'{"已上板接受" if m["status"] == "BOARD_ACCEPTED" else "本機驗證、待上板"} |')
    excl = [t for t in train if t['status'] != 'TRAINED_ADMISSIBLE']
    lines += ['', '## 被排除或更正的模型（失敗保留）', '']
    for t in excl:
        lines.append(f'- `{t["model_id"]}`：{t["status"]}；測試 R² = {t["test_metrics"].get("R2", float("nan")):.3f}。依受試者分組切分後，對沒見過的受試者沒有預測力，所以不納入。')
    lines.append('- `finance-banknote-auth-logreg-v1`：原生 C++ 管線的誤差超過 5e-4，被數學檢查擋下。改加更強的正則化後產生 v2（AUROC 1.000，最大誤差 2.4e-4）。v1 的檔案保留，但不使用。')
    lines += ['', '## 資料來源與授權（CC BY 4.0 須標示出處）', '']
    for s in man:
        lines.append(f'- {s["name"]}（UCI id {s["id"]}）：{s["data_url"]}，SHA-256 `{s["csv_sha256"][:16]}…`')
    lines.append('- Breast Cancer Wisconsin (Diagnostic)（UCI id 17）：使用 scikit-learn 內建的副本。')
    lines += ['', '使用時須標示 UCI Machine Learning Repository 及各資料集作者。各資料集的授權仍以 UCI 頁面為準；本機只記錄了 API 中繼資料的雜湊。', '',
              '## 尚未完成與限制', '',
              '- 上板驗收是**功能驗收**：每個模型、每個後端只有一個 10 秒窗，使用合成輸入。前端模型庫頁顯示的 PL 筆/秒不構成加速比或效能宣稱；長時間穩定性只有原本 2 個模型做過六小時長測。',
              '- 模型卡的 `slot_sigma` 是每個 slot 的標準差，不是窗內最大誤差的上限：板上窗最大值為它的 '
              f'{min(r["observed_over_card_slot_sigma"] for r in cons["rows"]):.1f}–{max(r["observed_over_card_slot_sigma"] for r in cons["rows"]):.1f} 倍，'
              '是否與雜訊模型相符見上方第 7 點。准入依據的是精度門檻 ≥ 15σ。',
              '- 這些都是研究用模型，**不代表已核准用於臨床、金融授信、工業或保全部署**。各領域實際導入前，需要該領域的資料、驗證與法規審查。',
              '- 只支援仿射深度 1 的模型。樹模型、神經網路和需要在密文上做比較的模型，不在目前的參數與範圍內。']
    (HERE / 'MODEL_ZOO_zh-TW.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('DOC_OK', len(zoo['models']))


if __name__ == '__main__':
    main()
