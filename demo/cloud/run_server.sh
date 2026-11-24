#!/bin/sh
# EdgeFHE demo cloud launcher, inside the edgefhe-demo-cloud image: runs the unchanged efnet_server with the fixed demo
# flags. The cloud receives and returns ciphertext only and holds no key of any kind; the TLS key log (--keylog) is a
# demo-only aid that lets a third party decrypt the capture and see that only EFHEW001 ciphertext frames crossed it.
#
# usage: sh /opt/efhe/run_server.sh [--keylog] [--loopback] [--connections N]
#   --keylog         preload /opt/efhe/libefhe_keylog.so (demo only): the session keys of every TLS connection are
#                    written to /evidence/tls_keys.log
#   --loopback       listen on 127.0.0.1 (rehearsals inside one container); default 0.0.0.0, published with -p 18443:18443
#   --connections N  connections served before the server stops by itself (1..10000, default 10000)
# Fixed server flags: --port 18443 --timeout-ms 30000 --ca /pki/ca.pem --cert /pki/server.pem --key /pki/server.key
#   --peer-name edgefhe-kv260 --peer-sha256 <contents of /pki/client.pin>; /pki is the cloud/ directory written by
#   make_demo_pki.sh, mounted read-only.
# Outputs (none of them may exist beforehand):
#   /evidence/server.log             every line of the server and of this launcher, prefixed with `date +%s.%N` (the PC
#                                    clock as the container sees it); the same lines go to stdout (docker logs)
#   /evidence/cloud_binaries.sha256  SHA-256 of the binaries of this run (sha256sum -c format)
#   /evidence/tls_keys.log           NSS key log, only with --keylog
# Exit status: that of efnet_server (0 all connections clean, 2 some connection refused, 1 server stop, 3 or 4 key-log
#   shim stop, 128+N after signal N); 64 = refused by this launcher before anything was written to /evidence
#   (stderr: EDGEFHE_CLOUD_SERVER_REFUSED <CODE> <reason>).
# Signals: TERM, INT or HUP (docker stop) stop the server; the log then ends with EDGEFHE_CLOUD_SERVER_EXIT.
# Written for POSIX sh (dash and bash), because the runbook starts it with `sh`.
set -eu
umask 077

SERVER=/build/efnet_server
SHIM=/opt/efhe/libefhe_keylog.so
BUILD_RECORD=/opt/efhe/BUILD_SHA256
OVERLAY_RECORD=/opt/efhe/OVERLAY_RECORD.json
SELF=/opt/efhe/run_server.sh
PKI=/pki
EVIDENCE=/evidence
LOG=$EVIDENCE/server.log
BINARIES=$EVIDENCE/cloud_binaries.sha256
KEYLOG_FILE=$EVIDENCE/tls_keys.log
PORT=18443
TIMEOUT_MS=30000
PEER_NAME=edgefhe-kv260

usage() {
  cat <<'EOF'
用法：sh /opt/efhe/run_server.sh [--keylog] [--loopback] [--connections N]
  --keylog         本啟動程式載入示範專用的金鑰紀錄墊片，將每一條 TLS 連線的工作階段金鑰寫入 /evidence/tls_keys.log。
  --loopback       伺服器只在 127.0.0.1 上接聽（僅供同一容器內的演練）；預設在 0.0.0.0 上接聽。
  --connections N  伺服器處理 N 條連線後自行結束（1 至 10000，預設 10000）。
本啟動程式從 /pki 讀取 ca.pem、server.pem、server.key、client.pin，並把 server.log 與 cloud_binaries.sha256 寫入 /evidence；
這些證據檔不得預先存在。雲端只接收與回傳密文，不持有任何金鑰。
EOF
}

refuse() {
  code=$1
  shift
  printf 'EDGEFHE_CLOUD_SERVER_REFUSED %s %s\n' "$code" "$*" >&2
  exit 64
}

stamp() {  # prefix every line with the current time `date +%s.%N`
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s %s\n' "$(date +%s.%N)" "$line"
  done
}

ldd_path() {  # ldd_path SONAME : the file the dynamic loader uses for SONAME in efnet_server
  ldd "$SERVER" | sed -n "s/^[[:space:]]*$1 => \(\/[^ ]*\) .*/\1/p" | sed -n 1p
}

KEYLOG=0
LISTEN=0.0.0.0
CONNECTIONS=10000
while [ $# -gt 0 ]; do
  case $1 in
    --keylog) KEYLOG=1 ;;
    --loopback) LISTEN=127.0.0.1 ;;
    --connections)
      [ $# -ge 2 ] || refuse USAGE "--connections 需要一個 1 至 10000 的整數。"
      CONNECTIONS=$2
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      refuse USAGE "本啟動程式不接受此參數：$1"
      ;;
  esac
  shift
done
case $CONNECTIONS in
  '' | *[!0-9]* | 0*) refuse USAGE "--connections 必須是 1 至 10000 的整數；目前的值為：$CONNECTIONS。" ;;
esac
if [ "${#CONNECTIONS}" -gt 5 ] || [ "$CONNECTIONS" -gt 10000 ]; then
  refuse USAGE "--connections 必須是 1 至 10000 的整數；目前的值為：$CONNECTIONS。"
fi

for tool in date sha256sum tee mkfifo mktemp openssl ldd sed cut readlink; do
  command -v "$tool" >/dev/null 2>&1 || refuse TOOL_MISSING "本啟動程式找不到必要的工具：$tool。"
done
[ -x "$SERVER" ] || refuse SERVER_MISSING "本啟動程式找不到 $SERVER；此程式只能在 edgefhe-demo-cloud 映像內執行。"
[ -f "$BUILD_RECORD" ] && [ -f "$OVERLAY_RECORD" ] ||
  refuse BUILD_RECORD_MISSING "本啟動程式找不到 $BUILD_RECORD 或 $OVERLAY_RECORD；此程式只能在 edgefhe-demo-cloud 映像內執行。"
if ! (cd / && sha256sum --check --strict --quiet "$BUILD_RECORD") >/dev/null 2>&1; then
  refuse BUILD_INTEGRITY "映像內的程式與建置時記錄的 SHA-256（$BUILD_RECORD）不一致；操作人員應重新建置映像。"
fi
if [ "$KEYLOG" = 1 ] && [ ! -f "$SHIM" ]; then
  refuse SHIM_MISSING "本啟動程式找不到金鑰紀錄墊片 $SHIM。"
fi

for f in ca.pem server.pem server.key; do
  if [ ! -f "$PKI/$f" ] || [ ! -r "$PKI/$f" ]; then
    refuse PKI_MISSING "本啟動程式找不到 $PKI/$f；操作人員應以 -v <PKI>/cloud:/pki:ro 掛載 make_demo_pki.sh 建立的 cloud 目錄。"
  fi
done
if [ ! -f "$PKI/client.pin" ] || [ ! -r "$PKI/client.pin" ]; then
  refuse PIN_MISSING "本啟動程式找不到 $PKI/client.pin：板端憑證尚未以 make_demo_pki.sh sign-client 簽發。雲端伺服器須等 sign-client 寫出 cloud/client.pin 之後才能啟動。"
fi
CLIENT_PIN=$(cat "$PKI/client.pin")
case $CLIENT_PIN in
  '' | *[!0-9a-f]*) refuse PIN_FORMAT "$PKI/client.pin 必須恰為 64 個小寫十六進位字元，後接一個換行。" ;;
esac
[ "${#CLIENT_PIN}" -eq 64 ] || refuse PIN_FORMAT "$PKI/client.pin 必須恰為 64 個小寫十六進位字元，後接一個換行。"
openssl x509 -in "$PKI/server.pem" -noout >/dev/null 2>&1 ||
  refuse PKI_FORMAT "$PKI/server.pem 不是可讀取的 PEM 憑證。"
SERVER_PIN=$(openssl x509 -in "$PKI/server.pem" -outform DER | sha256sum | cut -c1-64)

if [ ! -d "$EVIDENCE" ]; then
  refuse EVIDENCE_MISSING "本啟動程式找不到 $EVIDENCE；操作人員應以 -v <證據資料夾>:$EVIDENCE 掛載本次的證據資料夾。"
fi
[ -w "$EVIDENCE" ] || refuse EVIDENCE_READONLY "$EVIDENCE 無法寫入。"
for f in "$LOG" "$BINARIES" "$KEYLOG_FILE"; do
  if [ -e "$f" ] || [ -L "$f" ]; then
    refuse EVIDENCE_EXISTS "證據檔已存在：$f。本啟動程式不覆寫既有證據；操作人員應改用新的證據資料夾。"
  fi
done

LIBCLOUD=$(ldd_path 'libefnet_cloud\.so')
LIBSSL=$(ldd_path 'libssl\.so\.3')
LIBCRYPTO=$(ldd_path 'libcrypto\.so\.3')
if [ -z "$LIBCLOUD" ] || [ -z "$LIBSSL" ] || [ -z "$LIBCRYPTO" ]; then
  refuse LIBRARY_MISSING "efnet_server 找不到 libefnet_cloud.so、libssl.so.3 或 libcrypto.so.3。"
fi
SERVER_SHA256=$(sha256sum "$SERVER" | cut -c1-64)
STAGED_TREE_ID=$(sed -n 's/^# staged_tree_id=//p' "$BUILD_RECORD")
COMMIT=$(sed -n 's/^# edgefhe_commit=//p' "$BUILD_RECORD")
if [ "$KEYLOG" = 1 ]; then KEYLOG_STATE=on; else KEYLOG_STATE=off; fi

set -C  # noclobber: neither evidence file may be overwritten, even by a concurrent launcher
{
  printf '# EdgeFHE demo cloud: binaries of this server run (sha256sum -c format; lines starting with # are comments)\n'
  printf '# keylog=%s listen=%s port=%s connections=%s staged_tree_id=%s commit=%s\n' \
    "$KEYLOG_STATE" "$LISTEN" "$PORT" "$CONNECTIONS" "$STAGED_TREE_ID" "$COMMIT"
  sha256sum "$SERVER" "$(readlink -f "$LIBCLOUD")" "$(readlink -f "$LIBSSL")" "$(readlink -f "$LIBCRYPTO")"
  if [ "$KEYLOG" = 1 ]; then sha256sum "$SHIM"; fi
  sha256sum "$SELF" "$OVERLAY_RECORD" "$BUILD_RECORD"
} >"$BINARIES"
: >"$LOG"
set +C

# The logger reads a FIFO, so this shell can wait for the server and forward TERM at once; it ignores the signals
# itself and ends only at EOF, after the last server line. tee -p keeps the file copy if stdout goes away.
WORK=$(mktemp -d /tmp/efhe_run_server.XXXXXX)
mkfifo -m 600 "$WORK/log"
(
  trap '' TERM HUP INT
  stamp <"$WORK/log" | tee -p -a "$LOG"
) &
LOGGER=$!
exec 3>"$WORK/log"
rm -rf "$WORK"
emit() { printf '%s\n' "$*" >&3; }

emit "EDGEFHE_CLOUD_SERVER_START listen=$LISTEN port=$PORT connections=$CONNECTIONS timeout_ms=$TIMEOUT_MS keylog=$KEYLOG_STATE peer_name=$PEER_NAME server_pin=$SERVER_PIN client_pin=$CLIENT_PIN efnet_server_sha256=$SERVER_SHA256 staged_tree_id=$STAGED_TREE_ID commit=$COMMIT"
if [ "$KEYLOG" = 1 ]; then
  env LD_PRELOAD="$SHIM" EFHE_DEMO_KEYLOG="$KEYLOG_FILE" "$SERVER" --listen "$LISTEN" --port "$PORT" \
    --connections "$CONNECTIONS" --timeout-ms "$TIMEOUT_MS" --ca "$PKI/ca.pem" --cert "$PKI/server.pem" \
    --key "$PKI/server.key" --peer-name "$PEER_NAME" --peer-sha256 "$CLIENT_PIN" >&3 2>&3 3>&- &
else
  env -u LD_PRELOAD -u EFHE_DEMO_KEYLOG "$SERVER" --listen "$LISTEN" --port "$PORT" \
    --connections "$CONNECTIONS" --timeout-ms "$TIMEOUT_MS" --ca "$PKI/ca.pem" --cert "$PKI/server.pem" \
    --key "$PKI/server.key" --peer-name "$PEER_NAME" --peer-sha256 "$CLIENT_PIN" >&3 2>&3 3>&- &
fi
SERVER_PID=$!
STOP_SIGNAL=""
trap 'STOP_SIGNAL=TERM; kill -TERM "$SERVER_PID" 2>/dev/null || :' TERM
trap 'STOP_SIGNAL=INT; kill -TERM "$SERVER_PID" 2>/dev/null || :' INT
trap 'STOP_SIGNAL=HUP; kill -TERM "$SERVER_PID" 2>/dev/null || :' HUP
RC=0
while :; do
  if wait "$SERVER_PID" 2>/dev/null; then RC=0; else RC=$?; fi  # 2>/dev/null: no job-status text from dash
  kill -0 "$SERVER_PID" 2>/dev/null || break  # a trapped signal interrupts wait; the server may still be running
done
trap - TERM INT HUP
emit "EDGEFHE_CLOUD_SERVER_EXIT rc=$RC stop_signal=${STOP_SIGNAL:-none}"
exec 3>&-
wait "$LOGGER" || :
exit "$RC"
