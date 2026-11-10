#!/usr/bin/env bash
# Build the native client, owner, cloud server and bench suite on Linux / WSL, then run the self-tests.
# Local only: no network download, no board access, no device opens.
# usage: tools/build_and_selftest.sh [BUILD_DIR] [OPENFHE_CMAKE_DIR]
#   BUILD_DIR          default: $HOME/edgefhe-build (must not exist)
#   OPENFHE_CMAKE_DIR  default: $HOME/.local/openfhe-v1.5.1/lib/OpenFHE (OpenFHE 1.5.1 install with OpenFHEConfig.cmake)
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
OUT=${1:-$HOME/edgefhe-build}
OFHE=${2:-$HOME/.local/openfhe-v1.5.1/lib/OpenFHE}
CXX=${CXX:-g++-11}
for tool in cmake "$CXX" python3; do command -v "$tool" >/dev/null || { echo "missing tool: $tool"; exit 2; }; done
test -f "$OFHE/OpenFHEConfig.cmake" || { echo "OpenFHE not found at $OFHE"; exit 2; }
test ! -e "$OUT" || { echo "build dir exists: $OUT"; exit 2; }
C="$ROOT/software/client"
cmake -S "$C/vendor/application" -B "$OUT/app" -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER="$CXX" -DOpenFHE_DIR="$OFHE" \
      -DFROZEN_CLIENT="$C/vendor/frozen_sdk/src/client"
cmake --build "$OUT/app" -j 4 --target ef_app_owner ef_app_normalize ef_app_v5_equivalence edgefhe_os_prng
cmake -S "$C" -B "$OUT/net" -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER="$CXX" -DAPP_BUILD="$OUT/app"
cmake --build "$OUT/net" -j 4
cmake -S "$ROOT/software/bench" -B "$OUT/bench" -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER="$CXX" -DAPP_BUILD="$OUT/app" -DVENDOR="$C/vendor"
cmake --build "$OUT/bench" -j 4
PLUGIN="$OUT/app/frozen_build/baseline/libedgefhe_os_prng.so"
for t in span permit wire; do "$OUT/net/efnet_${t}_selftest" | tee "$OUT/${t}.log"; done
"$OUT/app/ef_app_v5_equivalence" "$PLUGIN" | tee "$OUT/app_equivalence.log"
grep -q 'CPP_MTLS_SPAN_BYTE_HASH_EQUIVALENCE_PASS bytes=51904512 refused=33' "$OUT/span.log"
grep -q 'CPP_MTLS_INVOCATION_PERMIT_PASS positive=1 refused=14' "$OUT/permit.log"
grep -q 'CPP_E2E_WIRE_AND_PADDED_AFFINE_PASS exact_checks=1572899 refusals=78' "$OUT/wire.log"
grep -q 'APP_V5_EQUIVALENCE_PASS checks=529' "$OUT/app_equivalence.log"
echo EDGEFHE_BUILD_AND_SELFTEST_PASS
