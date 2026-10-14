# 故障生命週期草稿：不可部署 / Fault lifecycle draft: do not deploy

本目錄與已接受的 [V3 driver](../driver/edgefhe_rns3_n4096_dma_proxy.c) 分開。持久 completion、terminate return 檢查、錯誤時資源 quarantine 與正常 DMA 後 output-copy fault 的修正，只完成 source／host-model 檢查。

未完成完整 kernel 編譯、provider 同步確認、實體 DDR／PL quiescence、reset／resume、in-flight remove 或 module unload 接受。Provider 回傳成功卻未停止 DMA 的反例仍存在；remove-time module pin 也不保證取消已開始的卸載。**不得用它替換原 V3 runtime 或宣稱安全恢復。**

來源 identity 與限制見 [SOURCE_REVIEW](SOURCE_REVIEW.json)，詳細後續驗收見 [V4 手冊](../../docs/NATIVE_V4.md)。這裡不提供自動部署或重試／reset 腳本。

**English:** An isolated source candidate with conditional host-model evidence. It is not a compiled kernel release or a physically validated recovery mechanism. Do not deploy it.
