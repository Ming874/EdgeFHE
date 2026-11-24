#!/usr/bin/env bash
# Loopback rehearsal of the demo cloud inside one edgefhe-demo-cloud container (PC only: no board, no LAN, no internet).
# It proves that the demo-only key-log shim writes the same TLS 1.3 secrets as openssl s_client, and it records a
# negative control for the evidence verifier (efnet_probe --case key sends an all-zero request body, probe.cpp:11-14).
#
# usage: docker run --rm --init -v C:/EdgeFHE_evidence/rehearsal_loopback:/evidence edgefhe-demo-cloud:<tag> \
#          bash /opt/efhe/rehearse_loopback.sh
#
# Steps (the server serves exactly three connections, then stops by itself):
#   1. all-in-one demo PKI in /tmp/pki (make_demo_pki.sh); /pki -> /tmp/pki/cloud for run_server.sh
#   2. tcpdump -i lo -s 0 -w /evidence/capture.pcapng port 18443, started first
#   3. run_server.sh --loopback --keylog --connections 3
#   4. connection 1: openssl s_client handshake; the server's five key-log lines for its client_random must equal the
#      lines of s_client -keylogfile (EDGEFHE_KEYLOG_SHIM_MATCHES_SCLIENT)
#   5. connection 2: efnet_probe --case key: one all-zero request is answered, the key change after it is refused
#      (CPP_E2E_REMOTE_REJECTION_OBSERVED key NOT_CRYPTO_TEST)
#   6. connection 3: a second s_client handshake; the shim still logs after a refused connection
#   7. stop tcpdump; copy the pins and the s_client key logs to /evidence
# /evidence must be empty. It receives: capture.pcapng (tcpdump writes the classic pcap format under this name),
#   capture_stats.txt (tcpdump statistics, including dropped packets), server.log, tls_keys.log, cloud_binaries.sha256,
#   server.pin, client.pin, sclient_keys.log and rehearsal_summary.txt. The private keys stay in /tmp/pki and disappear
#   with the container. Only the cloud server and tcpdump are ever signalled; s_client and efnet_probe end by themselves.
# Output: marker lines on stdout; the last one is EDGEFHE_LOOPBACK_REHEARSAL_PASS, or EDGEFHE_LOOPBACK_REHEARSAL_FAIL
#   <CODE> <reason> with exit status 1.
set -euo pipefail
umask 077
export LC_ALL=C

readonly EVIDENCE=/evidence PKI=/tmp/pki PORT=18443 CONNECTIONS=3
readonly SERVER_LOG=/evidence/server.log KEYLOG=/evidence/tls_keys.log
readonly LABELS='CLIENT_HANDSHAKE_TRAFFIC_SECRET CLIENT_TRAFFIC_SECRET_0 EXPORTER_SECRET SERVER_HANDSHAKE_TRAFFIC_SECRET SERVER_TRAFFIC_SECRET_0'
TCPDUMP_PID="" SERVER_PID=""
SUMMARY=() SCLIENT_RANDOMS=()

note() { printf '%s\n' "$*"; SUMMARY+=("$*"); }

stop_background() {  # only the capture and the cloud server are signalled; the TLS clients are never signalled
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill -TERM "$SERVER_PID" 2>/dev/null || :
    wait "$SERVER_PID" 2>/dev/null || :
  fi
  SERVER_PID=""
  if [ -n "$TCPDUMP_PID" ] && kill -0 "$TCPDUMP_PID" 2>/dev/null; then
    kill -INT "$TCPDUMP_PID" 2>/dev/null || :
    wait "$TCPDUMP_PID" 2>/dev/null || :
  fi
  TCPDUMP_PID=""
}

fail() {
  local code=$1 f
  shift
  trap - EXIT
  stop_background
  for f in /tmp/pki_messages.txt /tmp/run_server.out /tmp/sclient1.txt /tmp/probe.err /tmp/sclient3.txt; do
    if [ -s "$f" ]; then printf -- '--- %s (last lines)\n' "$f" >&2; tail -n 15 "$f" >&2; fi
  done
  printf 'EDGEFHE_LOOPBACK_REHEARSAL_FAIL %s %s\n' "$code" "$*"
  exit 1
}
trap 'fail UNEXPECTED_EXIT "本演練意外結束（指令失敗或被中斷，結束代碼 $?）；/evidence 內的檔案不完整。"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_for() {  # wait_for SECONDS COMMAND... : poll every 0.1 s until COMMAND succeeds
  local tenths=$(($1 * 10)) i
  shift
  for ((i = 0; i < tenths; i++)); do
    if "$@"; then return 0; fi
    sleep 0.1
  done
  return 1
}

server_log_has() { [ -f "$SERVER_LOG" ] && grep -qF -- "$1" "$SERVER_LOG"; }
keylog_count() { awk -v cr="$1" '$2 == cr' "$KEYLOG" 2>/dev/null | wc -l; }
keylog_has_five() { [ "$(keylog_count "$1")" -ge 5 ]; }

sclient() {  # sclient N KEYLOG : TLS 1.3 handshake N with the board credentials; empty stdin, so s_client closes cleanly
  local n=$1 rc=0 suite
  openssl s_client -connect "127.0.0.1:$PORT" -tls1_3 -CAfile "$PKI/board/ca.pem" -cert "$PKI/board/client.pem" \
    -key "$PKI/board/client.key" -servername edgefhe-cloud -verify_hostname edgefhe-cloud -verify_return_error \
    -keylogfile "$2" </dev/null >"/tmp/sclient$n.txt" 2>&1 || rc=$?
  [ "$rc" -eq 0 ] || fail SCLIENT_HANDSHAKE "連線 $n：openssl s_client 的結束代碼為 $rc。"
  grep -qx 'Verification: OK' "/tmp/sclient$n.txt" ||
    fail SCLIENT_VERIFY "連線 $n：openssl s_client 未回報 Verification: OK。"
  suite=$(sed -n 's/^\(New\|Reused\), TLSv1\.3, Cipher is \([A-Z0-9_]*\)$/\2/p' "/tmp/sclient$n.txt" | sed -n 1p)
  [ -n "$suite" ] || fail SCLIENT_PROTOCOL "連線 $n：openssl s_client 未協商出 TLS 1.3。"
  note "SCLIENT_HANDSHAKE_OK connection=$n protocol=TLSv1.3 cipher=$suite verification=OK peer=edgefhe-cloud"
}

compare_keylog() {  # compare_keylog N SCLIENT_KEYLOG : the shim's lines for this client_random equal s_client's
  local n=$1 file=$2 cr labels
  local -a lines=()
  mapfile -t lines < <(grep -v '^#' "$file" | sed '/^$/d')
  [ "${#lines[@]}" -eq 5 ] || fail SCLIENT_KEYLOG "連線 $n：s_client 的金鑰紀錄應有 5 行，實際為 ${#lines[@]} 行。"
  cr=$(printf '%s\n' "${lines[@]}" | awk '{print $2}' | sort -u)
  [[ $cr =~ ^[0-9a-f]{64}$ ]] || fail SCLIENT_KEYLOG "連線 $n：s_client 的金鑰紀錄不屬於單一的 client_random。"
  labels=$(printf '%s\n' "${lines[@]}" | awk '{print $1}' | sort | tr '\n' ' ')
  [ "$labels" = "$LABELS " ] || fail SCLIENT_KEYLOG "連線 $n：s_client 的金鑰紀錄標籤不符：$labels"
  # The server logs CLIENT_TRAFFIC_SECRET_0 when it reads the client Finished, possibly after s_client has exited.
  wait_for 10 keylog_has_five "$cr" ||
    fail SHIM_KEYLOG_MISSING "連線 $n：伺服器的金鑰紀錄在 10 秒內未出現 client_random $cr 的 5 行。"
  [ "$(keylog_count "$cr")" -eq 5 ] || fail SHIM_KEYLOG_DUPLICATE "連線 $n：伺服器的金鑰紀錄含有重複的行。"
  if ! diff <(printf '%s\n' "${lines[@]}" | sort) <(awk -v cr="$cr" '$2 == cr' "$KEYLOG" | sort) >/dev/null; then
    fail SHIM_KEYLOG_DIFFERS "連線 $n：伺服器的金鑰紀錄與 s_client 的金鑰紀錄不一致。"
  fi
  SCLIENT_RANDOMS+=("$cr")
  note "EDGEFHE_KEYLOG_SHIM_MATCHES_SCLIENT connection=$n client_random=$cr lines=5"
}

# 0. preconditions -----------------------------------------------------------------------------------------------
for tool in openssl tcpdump sha256sum awk sed diff; do
  command -v "$tool" >/dev/null 2>&1 || fail TOOL_MISSING "本演練找不到必要的工具：$tool。"
done
for f in /build/efnet_server /build/efnet_probe /opt/efhe/run_server.sh /opt/efhe/make_demo_pki.sh \
  /opt/efhe/libefhe_keylog.so; do
  [ -e "$f" ] || fail IMAGE_INCOMPLETE "本演練找不到 $f；此程式只能在 edgefhe-demo-cloud 映像內執行。"
done
[ -d "$EVIDENCE" ] && [ -w "$EVIDENCE" ] ||
  fail EVIDENCE_MISSING "本演練需要可寫入的 /evidence；操作人員應以 -v <空的證據資料夾>:/evidence 執行。"
if [ -n "$(ls -A "$EVIDENCE")" ]; then
  fail EVIDENCE_NOT_EMPTY "/evidence 不是空的資料夾。本演練不覆寫既有證據；操作人員應改用新的或空的證據資料夾。"
fi
if [ -e /pki ] || [ -L /pki ]; then
  fail PKI_MOUNTED "/pki 已存在。本演練只使用容器內新建的示範 PKI；操作人員不應掛載 /pki。"
fi
[ ! -e "$PKI" ] || fail PKI_EXISTS "$PKI 已存在。"

# 1. demo PKI -------------------------------------------------------------------------------------------------------
pki_marker=$(bash /opt/efhe/make_demo_pki.sh all-in-one "$PKI" 2>/tmp/pki_messages.txt) ||
  fail PKI "make_demo_pki.sh all-in-one 失敗。"
[[ $pki_marker =~ ^EDGEFHE_DEMO_PKI_READY\ server_pin=([0-9a-f]{64})\ client_pin=([0-9a-f]{64})$ ]] ||
  fail PKI "make_demo_pki.sh 的結果標記不符：$pki_marker"
SERVER_PIN=${BASH_REMATCH[1]} CLIENT_PIN=${BASH_REMATCH[2]}
[ "$(cat "$PKI/board/server.pin")" = "$SERVER_PIN" ] && [ "$(cat "$PKI/cloud/client.pin")" = "$CLIENT_PIN" ] ||
  fail PKI "PKI 目錄內的釘選值與結果標記不一致。"
ln -s "$PKI/cloud" /pki
note "$pki_marker"

# 2. capture first --------------------------------------------------------------------------------------------------
tcpdump -i lo -s 0 -U -B 8192 -w "$EVIDENCE/capture.pcapng" port "$PORT" 2>"$EVIDENCE/capture_stats.txt" &
TCPDUMP_PID=$!
capture_listening() { grep -qs 'listening on lo' "$EVIDENCE/capture_stats.txt"; }
wait_for 10 capture_listening || fail CAPTURE "tcpdump 未在 10 秒內開始擷取 lo。"
note "CAPTURE_STARTED interface=lo filter=port_$PORT file=capture.pcapng format=pcap"

# 3. cloud server with the key-log shim -----------------------------------------------------------------------------
sh /opt/efhe/run_server.sh --loopback --keylog --connections "$CONNECTIONS" >/tmp/run_server.out 2>&1 &
SERVER_PID=$!
server_ready() { server_log_has CPP_E2E_SERVER_READY || ! kill -0 "$SERVER_PID" 2>/dev/null; }
wait_for 20 server_ready || fail SERVER_START "雲端伺服器未在 20 秒內就緒。"
server_log_has CPP_E2E_SERVER_READY || fail SERVER_START "雲端伺服器在就緒之前結束。"
server_log_has WARNING_DEMO_TLS_KEYLOG_ENABLED || fail SERVER_START "server.log 缺少 WARNING_DEMO_TLS_KEYLOG_ENABLED。"
note "SERVER_READY listen=127.0.0.1 port=$PORT connections=$CONNECTIONS keylog=on"

# 4-6. three connections ----------------------------------------------------------------------------------------------
sclient 1 /tmp/sclient.log
compare_keylog 1 /tmp/sclient.log

probe_rc=0
/build/efnet_probe --host 127.0.0.1 --port "$PORT" --ca "$PKI/board/ca.pem" --cert "$PKI/board/client.pem" \
  --key "$PKI/board/client.key" --peer-name edgefhe-cloud --peer-sha256 "$SERVER_PIN" --case key \
  >/tmp/probe.out 2>/tmp/probe.err || probe_rc=$?
[ "$probe_rc" -eq 0 ] || fail PROBE "連線 2：efnet_probe --case key 的結束代碼為 $probe_rc。"
grep -qx 'CPP_E2E_REMOTE_REJECTION_OBSERVED key NOT_CRYPTO_TEST' /tmp/probe.out ||
  fail PROBE "連線 2：efnet_probe 未回報 CPP_E2E_REMOTE_REJECTION_OBSERVED key NOT_CRYPTO_TEST。"
note "CPP_E2E_REMOTE_REJECTION_OBSERVED key NOT_CRYPTO_TEST"
probe_refused() { server_log_has 'CPP_E2E_SERVER_CONNECTION_STOP WIRE_KEY_GENERATION_CHANGE'; }
wait_for 10 probe_refused ||
  fail PROBE "連線 2：server.log 缺少 CPP_E2E_SERVER_CONNECTION_STOP WIRE_KEY_GENERATION_CHANGE。"

sclient 3 /tmp/sclient3.log
compare_keylog 3 /tmp/sclient3.log

# 7. the server stops by itself after three connections; then the capture is stopped -------------------------------
server_stopped() { ! kill -0 "$SERVER_PID" 2>/dev/null; }
wait_for 40 server_stopped || fail SERVER_STOP "雲端伺服器在第三條連線之後 40 秒內仍未結束。"
server_rc=0
wait "$SERVER_PID" || server_rc=$?
SERVER_PID=""
[ "$server_rc" -eq 2 ] || fail SERVER_STOP "雲端伺服器的結束代碼應為 2（一條連線被拒），實際為 $server_rc。"
sleep 1  # the last FIN/ACK segments reach the capture
kill -INT "$TCPDUMP_PID"
tcpdump_rc=0
wait "$TCPDUMP_PID" || tcpdump_rc=$?
TCPDUMP_PID=""
[ "$tcpdump_rc" -eq 0 ] || fail CAPTURE "tcpdump 的結束代碼為 $tcpdump_rc。"
captured=$(sed -n 's/^\([0-9]*\) packets\{0,1\} captured$/\1/p' "$EVIDENCE/capture_stats.txt")
dropped=$(sed -n 's/^\([0-9]*\) packets\{0,1\} dropped by kernel$/\1/p' "$EVIDENCE/capture_stats.txt")
[ -n "$captured" ] && [ "$captured" -gt 0 ] || fail CAPTURE "tcpdump 未擷取到任何封包。"
[ "$dropped" = 0 ] || fail CAPTURE_DROPS "tcpdump 回報核心丟棄了 ${dropped:-未知數量的} 封包。"
note "CAPTURE_STOPPED packets=$captured dropped=$dropped"

# server.log: every line time-stamped; one evaluated all-zero request, one refused key change, the server summary.
if grep -vqE '^[0-9]{9,11}\.[0-9]{9} ' "$SERVER_LOG"; then fail SERVER_LOG "server.log 有缺少時間前綴的行。"; fi
[ "$(grep -cF '"status":"CPP_E2E_RESPONSE_WRITTEN_NOT_OWNER_ACCEPTANCE","sequence":1,' "$SERVER_LOG")" -eq 1 ] ||
  fail SERVER_LOG "server.log 應恰有一行 sequence 1 的 CPP_E2E_RESPONSE_WRITTEN_NOT_OWNER_ACCEPTANCE。"
[ "$(grep -cF 'CPP_E2E_SERVER_CONNECTION_STOP' "$SERVER_LOG")" -eq 1 ] ||
  fail SERVER_LOG "server.log 應恰有一行 CPP_E2E_SERVER_CONNECTION_STOP（金鑰更換被拒）。"
server_log_has '{"status":"CPP_E2E_SERVER_STOPPED","evaluated":1,"responses_written":1,"rejected_connections":1,"owner_secrets_available":false,"automatic_retries":0}' ||
  fail SERVER_LOG "server.log 的 CPP_E2E_SERVER_STOPPED 摘要不符（預期 evaluated 1、responses_written 1、rejected_connections 1）。"
server_log_has 'EDGEFHE_CLOUD_SERVER_EXIT rc=2 stop_signal=none' || fail SERVER_LOG "server.log 缺少 EDGEFHE_CLOUD_SERVER_EXIT rc=2。"

# tls_keys.log: exactly three connections with the five TLS 1.3 labels each; two of them are the s_client connections.
mapfile -t randoms < <(awk 'NF {print $2}' "$KEYLOG" | sort -u)
[ "$(awk 'NF' "$KEYLOG" | wc -l)" -eq $((CONNECTIONS * 5)) ] && [ "${#randoms[@]}" -eq "$CONNECTIONS" ] ||
  fail SHIM_KEYLOG_COUNT "tls_keys.log 應含 $CONNECTIONS 條連線、共 $((CONNECTIONS * 5)) 行。"
probe_cr=""
for cr in "${randoms[@]}"; do
  labels=$(awk -v cr="$cr" '$2 == cr {print $1}' "$KEYLOG" | sort | tr '\n' ' ')
  [ "$labels" = "$LABELS " ] || fail SHIM_KEYLOG_COUNT "tls_keys.log 中 client_random $cr 的標籤不符。"
  case " ${SCLIENT_RANDOMS[*]} " in *" $cr "*) ;; *) probe_cr=$cr ;; esac
done
[ -n "$probe_cr" ] || fail SHIM_KEYLOG_COUNT "tls_keys.log 中找不到 efnet_probe 連線的金鑰。"
note "SHIM_KEYLOG_COMPLETE connections=$CONNECTIONS lines=$((CONNECTIONS * 5)) probe_client_random=$probe_cr"

# 8. public pins and the s_client key logs (the re-checkable side of the comparison) go to /evidence ----------------
cp "$PKI/ca/server.pin" "$PKI/ca/client.pin" "$EVIDENCE/"
cat /tmp/sclient.log /tmp/sclient3.log >"$EVIDENCE/sclient_keys.log"
printf -- '--- server.log\n'
cat "$SERVER_LOG"
note "EVIDENCE_FILES $(cd "$EVIDENCE" && ls | sort | tr '\n' ' ')rehearsal_summary.txt"
note "EDGEFHE_LOOPBACK_REHEARSAL_PASS connections=$CONNECTIONS keylog_lines=$((CONNECTIONS * 5)) packets=$captured dropped=0 server_pin=$SERVER_PIN client_pin=$CLIENT_PIN"
printf '%s\n' "${SUMMARY[@]}" >"$EVIDENCE/rehearsal_summary.txt"
trap - EXIT
