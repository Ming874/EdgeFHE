# 標準 OpenFHE C++ client 對照 / Standard OpenFHE client baseline

這個獨立 C++ benchmark呼叫 OpenFHE 的 canonical encoding、KeyGen、Encrypt、plaintext-affine、Decrypt／Decode。**CPU-only，不開啟DMA、不連線開發板，不把其安全參數或ciphertext格式說成固定PL已相容。**

參數：N=4096、2048 slots、`ScalingModSize=29`、`FirstModSize=30`、depth=2、`UNIFORM_TERNARY`、`BV`、`FIXEDMANUAL`、library `HEStd_128_classic` selection。三個moduli由OpenFHE生成；本機1.5.1實際為 `1073692673,536813569,536903681`，與EdgeFHE固定PL不同。Configured security selection不是本專案的新安全認證。

每次處理2048筆四特徵public synthetic inputs，模型 `[1.1,-.7,.9,.4]`／bias `-.35`，計算affine後解密全部slots，最大logit error須≤`5e-4`。沒有ciphertext sigmoid、rotation或bootstrapping。Weights／bias預編碼與context／key初始化不放入hot-path計時；另量KeyGen。Warm-ups=3，量測≥10次；存全部latencies。

```bash
cmake -S validation/openfhe_client -B build/openfhe-client -DCMAKE_BUILD_TYPE=Release -DOpenFHE_DIR=/path/to/openfhe/lib/OpenFHE
cmake --build build/openfhe-client -j 4
./build/openfhe-client/openfhe_client_baseline 1 30 local/openfhe_1.tsv
./build/openfhe-client/openfhe_client_baseline 3 30 local/openfhe_3.tsv
./build/openfhe-client/openfhe_client_baseline 4 30 local/openfhe_4.tsv
```

先建立 `local/`，使用新的TSV檔名。OpenMP設定不保證每個stage都平行，也不保證4 threads更快。必須保存library revision、shared libraries SHA、compiler／CPU／affinity與所有raw samples。此處不下載OpenFHE或自動安裝套件。

本機24-bit scale版本曾因2048-slot logit tolerance失敗；保留失敗，再調29-bit重驗成功。不要提高tolerance掩蓋結果。本機x86的1／3／4 thread量測見 [實驗分析](../../docs/NATIVE_V4.md)；仍需aarch64重做。雖然與V4使用同N／slots／scale，sampler、primes、embedding、ciphertext representation、affinity與pipeline不同，**不直接計算FPGA或兩profile的speedup**。

API參考：[OpenFHE simple-real-numbers](https://github.com/openfheorg/openfhe-development/blob/main/src/pke/examples/simple-real-numbers.cpp)、[advanced-real-numbers](https://github.com/openfheorg/openfhe-development/blob/main/src/pke/examples/advanced-real-numbers.cpp)。
