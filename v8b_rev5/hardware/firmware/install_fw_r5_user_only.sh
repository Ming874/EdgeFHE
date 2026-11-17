#!/bin/bash
# USER ONLY (KV260, Tera Term UART or SSH shell typed by the user). Installs the rev5 firmware app as a NEW directory
# /lib/firmware/xilinx/edgefhe-v8b-r5. No PL programming, no overlay, no module, no DMA. The accepted rev4 app
# /lib/firmware/xilinx/edgefhe-v8b is never written; its file hashes are compared before and after.
set -euo pipefail
D=/lib/firmware/xilinx/edgefhe-v8b-r5
OLD=/lib/firmware/xilinx/edgefhe-v8b
HERE="$(cd "$(dirname "$0")" && pwd)"
test "$(uname -m)" = aarch64 || { echo 'STOP_NOT_ARM_BOARD'; exit 1; }
cd "$HERE"
sha256sum -c --quiet SHA256SUMS || { echo 'STOP_PACKAGE_SHA256_MISMATCH'; exit 1; }
if [ -e "$D" ]; then echo 'STOP_TARGET_EXISTS_PRESERVE_NOTHING_CHANGED'; ls -la "$D"; sha256sum "$D"/* || true; exit 1; fi
test -d "$OLD" || { echo 'STOP_ACCEPTED_REV4_APP_MISSING'; exit 1; }
BEFORE="$(cd "$OLD" && sha256sum -- *)"
echo 'EDGEFHE_R5_FW_INSTALL_BEGIN (sudo may ask for the ubuntu password)'
sudo install -d -o root -g root -m 0755 "$D"
for f in edgefhe-v8b-r5.bit.bin edgefhe-v8b-r5.dtbo shell.json; do
  sudo install -o root -g root -m 0644 "firmware/$f" "$D/$f"
done
sync
AFTER="$(cd "$OLD" && sha256sum -- *)"
test "$BEFORE" = "$AFTER" || { echo 'STOP_REV4_APP_CHANGED_REVIEW_REQUIRED'; exit 1; }
(cd "$D" && sha256sum -c --quiet "$HERE/INSTALLED_SHA256SUMS") || { echo 'STOP_INSTALLED_SHA256_MISMATCH'; exit 1; }
test "$(ls -A "$D" | sort | tr '\n' ' ')" = 'edgefhe-v8b-r5.bit.bin edgefhe-v8b-r5.dtbo shell.json ' || { echo 'STOP_INSTALLED_FILE_LIST'; exit 1; }
stat -c '%U:%G %a %s %n' "$D" "$D"/*
echo "REV4_APP_UNCHANGED"
echo "$BEFORE"
echo 'EDGEFHE_R5_FIRMWARE_INSTALLED_NOT_LOADED boot_id='"$(cat /proc/sys/kernel/random/boot_id)"
