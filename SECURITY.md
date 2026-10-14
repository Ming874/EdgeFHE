# 功能profile、安全與資料邊界

這是II1 N4096/V3功能研究snapshot；沒有完成具體安全估算、side-channel或惡意evaluator驗收。N/Q上限sanity check不能當security proof。

`software/ckks/profile.py`使用s/u fixed weight64、error fixed weight16。正常board caller使用OS `secrets.SystemRandom`；`local_reference.py`的deterministic seed只供host QA，不能拿去部署。輸入scale2^24，affine輸出2^48；目前沒有general rescale/key-switch/rotation/relinearization/bootstrapping。

六例public fixture的features也在PC端，ready會回傳CASES。線上packet只載密文，不等於服務端不知道公開輸入。任意私密資料的版本必須分離input/oracle與public model、避免ID映射features，並重驗schema、metadata、logs與祕密權限。原始實驗報告中的解密輸出是公開QA資料，不應照搬到私密部署。

TLS server certificate pin＋Bearer token不是mTLS。此倉庫不含現用token、join_config、server private key或專屬certificate。要重現network transport，操作者需在受控local目錄產生新憑證／config並完成新身份驗收；本包沒有替既有私密部署做更新。FHE保護計算內容，不能單獨保證evaluator結果正確或隱藏長度／時間／case metadata。

driver目前接受的是nominal、指定reject、長測與同process正常排隊。reset/timeout/quiescence/late callback/remove尚待；已有host lifecycle模型顯示在DMA未真正停止的假設下wipe/free可能不安全。遇故障停止新交易，保存證據，不自動重試／reload／reboot。勿把模型或SHA一致當作物理停止證明。

沒有在此新增公開通報email。請使用團隊既有受信任的聯絡方式處理credential或安全議題，勿把key/token貼進public issue。

## V4 候選的新增邊界 / Native candidate boundary

上方敘述限凍結 V3。V4 在 `software/native_ckks/` 提供真正 client-certificate mTLS、可設定 port、任意 client-side stdin／CSV、無 fixtures 的 public model server、新 scale29 profile。本機測試通過，仍未接受 ARM／PL／實體兩機，也沒有安全 bits、CCA、constant-time 或完整結果完整性證明。完整威脅模型見 [V4 文件的威脅模型一節](docs/NATIVE_V4.md)。Replay ledger只在記憶體，service restart會清空；不是跨重啟防重放。故障 driver draft 不可部署；provider false-success與正在卸載時的owner lifetime仍未解決。
