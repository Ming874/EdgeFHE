# docs 索引 / Documentation index

`docs/` 根目錄只放六份技術文件；筆記、過去的簡報與報告依類型放在三個子資料夾。新的筆記請以 `YYYY-MM-DD_主題.md` 命名，放進 `notes/`。

## 技術文件

| 文件 | 內容 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 系統架構、介面（AXI、DMA、封包格式）、設計決策 |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 建置、CTest、完整性流程、工具一覽、更新到 GitHub |
| [BOARD_GUIDE.md](BOARD_GUIDE.md) | KV260 操作員程序：gates A–H、上板當天清單 |
| [COMPARISON.md](COMPARISON.md) | 組 A vs 組 B 對照研究：分工、公平規則、階段、結果 |
| [NATIVE_V4.md](NATIVE_V4.md) | V4 製作與驗收、實驗數據、審查回應、威脅模型 |
| [VALIDATION.md](VALIDATION.md) | 證據分層與驗收狀態：哪些在板上驗證、哪些只在主機 |

## notes/：會議與分析筆記

| 文件 | 內容 |
|---|---|
| [2026-10-13_first_meeting.md](notes/2026-10-13_first_meeting.md) | 與指導教授的第一次會議：準備內容、簡報連結、會議紀錄 |
| [2026-10-13_architecture_analysis.md](notes/2026-10-13_architecture_analysis.md) | 架構分析：分量、平行結構、多方門檻方案、進度與報告用詞 |
| [2026-10-18_professor_feedback_analysis.md](notes/2026-10-18_professor_feedback_analysis.md) | 指導教授四點回饋的分析：研究基準點、可重用模組、切分方法、重新定位 |
| [2026-10-18_references.md](notes/2026-10-18_references.md) | 參考文獻總表（156 筆，逐篇查證） |
| [partition_model_v0.1/](notes/partition_model_v0.1/PARTITION_MODEL.md) | 軟硬體切分模型 v0.1（以 2026-10-14 的量測為本） |
| [2026-11-23_team_context.md](notes/2026-11-23_team_context.md) | 團隊共同脈絡：目前狀態、量測結果、引用規則與待辦（全體成員同步用） |

## slides/：過去的簡報（背景參考）

| 檔案 | 內容 |
|---|---|
| [EdgeFHE_261014.pptx](slides/EdgeFHE_261014.pptx) | 2026-10-14 的簡報 |
| [EdgeFHE_261016.pptx](slides/EdgeFHE_261016.pptx) | 2026-10-16 的簡報 |
| [EdgeFHE_261111.pptx](slides/EdgeFHE_261111.pptx) | 2026-11-11 的規劃簡報：專題價值、學術貢獻與分工規劃 |
| [EdgeFHE_261127.pptx](slides/EdgeFHE_261127.pptx) | 2026-11-27 的進度簡報：外部量測、探針實作、能耗比較與軟硬體切分、期末展示 |

## reports/：提案與報告

| 檔案 | 內容 |
|---|---|
| [Proposal_01.pdf](reports/Proposal_01.pdf) | 原始提案 |
| [EdgeFHE_V4_Progress_Analysis.pdf](reports/EdgeFHE_V4_Progress_Analysis.pdf) | 進度報告（V4，2026-10-09） |
| [PUF-EdgeFHE-深度分析報告.pdf](reports/PUF-EdgeFHE-深度分析報告.pdf) | PUF 與 EdgeFHE 的深度分析報告（背景參考） |
| [EdgeFHE_V8b與OpenFHE能耗比較報告.pdf](reports/EdgeFHE_V8b與OpenFHE能耗比較報告.pdf) | V8b 與 OpenFHE 的加密能耗比較及軟硬體切分實測（2026-11-23）；由 `energy_probe/kv260/make_report.py` 產生，資料在 `energy_probe/data/` |
