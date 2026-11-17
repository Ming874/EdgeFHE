# 本機依賴建置 / Local dependency build

以下僅供另一台 **Ubuntu／WSL 本機 PC** 的重現者執行。不是開發板指令，AI 本輪未執行安裝或網路下載。現有本機 QA 使用已安裝的依賴，因此不能把它宣稱成全新作業系統的依賴安裝驗收。

## Ubuntu 本機：準備工具

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends build-essential gcc-11 g++-11 cmake git python3 libssl-dev
```

## Ubuntu 本機：固定 OpenFHE 來源

下列 commit 來自本輪實際 QA 所用的公開 OpenFHE 來源樹。保留標籤與 commit 雙重比對，不能只使用可移動的分支。既有同名目錄會讓 git clone 停止；不要覆蓋舊結果。

```bash
set -euo pipefail
mkdir -p "$HOME/edgefhe-dependencies"
cd "$HOME/edgefhe-dependencies"
git clone --branch v1.5.1 --recursive https://github.com/openfheorg/openfhe-development.git openfhe-1.5.1
cd openfhe-1.5.1
test "$(git rev-parse HEAD)" = 1306d14f8c26bb6150d3e6ad54f28dfe1007689e
test "$(git describe --tags --exact-match HEAD)" = v1.5.1
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=gcc-11 -DCMAKE_CXX_COMPILER=g++-11 -DCMAKE_INSTALL_PREFIX="$HOME/.local/openfhe-v1.5.1" -DBUILD_UNITTESTS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_BENCHMARKS=OFF -DWITH_OPENMP=ON -DNATIVE_SIZE=64
cmake --build build --parallel 3
cmake --install build
test -f "$HOME/.local/openfhe-v1.5.1/lib/OpenFHE/OpenFHEConfig.cmake"
printf 'F_LOCAL_DEPENDENCY_INSTALL_COMPLETE_RUN_RELEASE_SELFTEST_NEXT\n'
```

這個完成標記只表示檔案安裝，仍須執行 REPRODUCE_zh-TW.md 的精確自測。請保存編譯器、OpenSSL、CMake、Python 版本與 commit。不要把 x86 產物搬到 ARM，或以 x86 效能替代板端數字。

前端另需 package-lock.json 指定的 Node/npm 套件；從 frontend_showcase 的 web 目錄執行 `npm ci`、`npm test`、`npm run build`。這是另一組本機依賴，依 lockfile 安裝並保存工具版本；不需要上板或私密資料。瀏覽器展示需要公開結果頁或受控的本機主控台，不能把歷史固定憑證當作正式身分。

English: These commands are for a separate local Ubuntu/WSL reproduction PC. The OpenFHE tag and commit match the locally used source. Dependency installation on a fresh OS is not yet independently accepted. Run the release self-tests afterward; do not transfer x86 binaries or performance claims to ARM.
