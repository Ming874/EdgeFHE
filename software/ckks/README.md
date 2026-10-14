# 凍結 V3 功能基線 / Frozen V3 functional baseline

此目錄的原始程式逐 byte 保留，對應既有實板及公開六例 affine 證據。它使用 scale24、固定公開 fixtures、固定 port18086、server-pinned TLS＋token；不是目前推薦的新動態私密展示入口。

新增的完整 C++17 數學、可設定 port、互動 stdin／CSV、滿槽位與 mTLS 位於 [native_ckks](../native_ckks/README.md)。兩個版本的 profile／scale／protocol 不相容，不將 V3 實板 PASS 套用於 V4。本目錄保留以重現與比較，不直接改寫原測試證據的來源。

**English:** Historical, accepted V3 sources are immutable in this package. Use the separate native V4 candidate for the new client/server flow. V4 has local CPU and loopback acceptance; board acceptance is still pending.
