# 來源與授權

此包整理既有EdgeFHE來源，root LICENSE逐byte沿用既有GitHub工作副本的Apache-2.0文字；不更改既有版權歸屬。

- `software/driver/edgefhe_rns3_n4096_dma_proxy.c`保留`SPDX-License-Identifier: GPL-2.0`與MODULE_LICENSE，root Apache標示不覆蓋它。
- HLS source為既有EdgeFHE算術；generated IP ZIP保留封裝內AMD/HLS生成標示，其使用依相應工具與IP條款。未複製Vivado通用IP安裝目錄或第三方node_modules。
- `validation/vectors/export_vectors.cpp`需要外部OpenFHE（固定revision見openfhe_revision.txt）；OpenFHE原庫及其依賴依各自license，沒有把其庫重新授權為Apache。
- `release/PROVENANCE.json`逐檔記錄exact-copy或path-only adapter及原始SHA；portable wrappers/doc/tools為本次新增。
- 不含Proposal PDF、私鑰／token／個人部署設定、原始286MB長測TSV、未使用的N32768/V2與歷史全功能V1工程。

這是GitHub更新內容準備，不是自動發布或完整工程release認證。

新增 `software/native_ckks/` 與測試／工具沿用本專案授權；`software/driver_candidate_v4/` 保留 GPL-2.0 標示。預編譯 V3 module 的對應 GPL source 位於 `software/driver/`；只收錄原接受的特定 kernel binary。`validation/openfhe_client/` 是使用外部 OpenFHE 的新比較程式，OpenFHE 本體與相依庫未收錄。
