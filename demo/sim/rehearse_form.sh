#!/usr/bin/env bash
# PC rehearsal of one EdgeFHE demo session inside one edgefhe-demo-sim container (PC only: no board, no LAN, no
# internet). The PC plays every role: the owner (the gateway and the owner screen; encryption with the PL bit-exact
# software model, efnet_client --backend spec), the wire (loopback) and the cloud (the unchanged efnet_server with the
# demo-only key-log shim). Research demonstration only; not a medical diagnosis.
#
# usage (PowerShell; the evidence folder must be new or empty):
#   docker run --rm --init -p 127.0.0.1:8080:8080 -v C:/EdgeFHE_evidence/rehearsal_sim:/evidence `
#     edgefhe-demo-sim:<tag> bash /repo/demo/sim/rehearse_form.sh [--auto]
#   --auto  drive the presets, the two permit checks and the disclosures, then stop (the acceptance run); the owner
#           screen at http://127.0.0.1:8080/ shows the forms live meanwhile
#   (none)  manual rehearsal: the owner screen is served at http://127.0.0.1:8080/ (the PC acts as the owner; rehearsal
#           only, fictional answers only) until Ctrl+C in this window or docker stop -t 120 <container>
#
# Steps:
#   1. all-in-one demo PKI in /tmp/pki (make_demo_pki.sh); /pki -> /tmp/pki/cloud for run_server.sh
#   2. /etc/edgefhe-demo/gateway.json = demo/sim/gateway.sim.json with this run's server pin (drive_presets.py
#      --write-config, 0600), then the gateway preflight for the spec backend (GATEWAY_PREFLIGHT_PASS)
#   3. tcpdump -i lo -s 0 -w /evidence/capture.pcapng port 18443, started before anything connects
#   4. sh /opt/efhe/run_server.sh --loopback --keylog (the launcher of demo/cloud: server.log, tls_keys.log)
#   5. EDGEFHE_REHEARSAL=1 edgefhe_gateway.py serve --rehearsal-listen 0.0.0.0 (GATEWAY_SERVING 0.0.0.0:8080)
#   6. --auto: drive_presets.py (SIM_FORM_PRESETS_PASS n=19), --permit-check (SIM_PERMIT_ACCEPTED_OWNER_CREATE) and
#      --permit-negative (SIM_PERMIT_CORRUPTED_REFUSED); manual: wait for Ctrl+C or docker stop
#   7. stop the gateway (SIGTERM; it lets the form in flight finish first), then the cloud server (SIGTERM) and tcpdump
#      (SIGINT); require 0 dropped packets and print the capture SHA-256 (CAPTURE_STOPPED)
#   8. only after the capture has stopped: the fictional disclosures (edgefhe_gateway.py disclose for every accepted
#      form of the public log) into /evidence/disclosure/, the gateway public log as /evidence/board_public.jsonl, the
#      pins, the gateway journal and the summary
# /evidence must be empty. It receives capture.pcapng (tcpdump writes the classic pcap format under this name),
# capture_stats.txt, server.log, tls_keys.log, cloud_binaries.sha256, server.pin, client.pin, board_public.jsonl,
# disclosure/<request_sha256>.json, gateway_serve.log and rehearsal_summary.txt. Then, on the PC (seal before use):
#   python -B demo\evidence\efhe_seal.py --run C:\EdgeFHE_evidence\rehearsal_sim --verify   -> EFHE_EVIDENCE_PASS
# The private keys stay in /tmp/pki and disappear with the container. Only the gateway, the cloud server and tcpdump
# are ever signalled. No client is ever signalled or killed: the gateway starts each client in its own session and,
# on SIGTERM, waits for the client in flight (an interrupted PL ENC would quarantine the board driver). A Ctrl+C takes
# effect after the step in progress.
# Output: marker lines on stdout; the last one is SIM_REHEARSAL_PASS, or SIM_REHEARSAL_FAIL <CODE> <reason> with exit
# status 1 (64 for a usage error).
set -euo pipefail
umask 077
export LC_ALL=C.UTF-8

readonly EVIDENCE=/evidence PKI=/tmp/pki PORT=18443 HTTP_PORT=8080 WORK=/run/edgefhe-sim
readonly GATEWAY=/repo/demo/edge/edgefhe_gateway.py DRIVER=/repo/demo/sim/drive_presets.py
readonly TEMPLATE=/repo/demo/sim/gateway.sim.json CONFIG=/etc/edgefhe-demo/gateway.json DMA_NODE=/dev/edgefhe_v8b_dma
readonly CAPTURE=/evidence/capture.pcapng SERVER_LOG=/evidence/server.log KEYLOG=/evidence/tls_keys.log
readonly LABELS='CLIENT_HANDSHAKE_TRAFFIC_SECRET CLIENT_TRAFFIC_SECRET_0 EXPORTER_SECRET SERVER_HANDSHAKE_TRAFFIC_SECRET SERVER_TRAFFIC_SECRET_0'
MODE=manual STOP_SIGNAL="" LATE_SIGNAL=""
TCPDUMP_PID="" SERVER_PID="" GATEWAY_PID="" WAIT_RC=0
GATEWAY_RC="" SERVER_RC="" TCPDUMP_RC=""
SUMMARY=()

usage() {
  cat <<'EOF'
用法：docker run --rm --init -p 127.0.0.1:8080:8080 -v C:/EdgeFHE_evidence/<新資料夾>:/evidence edgefhe-demo-sim:<標籤> bash /repo/demo/sim/rehearse_form.sh [--auto]
  --auto  本演練依序送出預設的虛構表單、執行兩項 FPGA 呼叫許可檢查並寫出揭露檔，完成後自行結束（驗收用）。
  未指定  本演練在 http://127.0.0.1:8080/ 提供擁有者畫面供手動演練（PC 扮演擁有者，僅限演練，只能填寫虛構答案）；
          操作人員按 Ctrl+C 或執行 docker stop -t 120 <容器名稱> 後，本演練等待執行中的表單完成，再停止並整理證據。
本演練使用 PL 位元精確軟體模型（spec 後端）取代 FPGA；本展示為研究用途，並非醫療診斷。
EOF
}

note() { printf '%s\n' "$*"; SUMMARY+=("$*"); }
say() { printf '%s\n' "$*" >&2; }

wait_exit() {  # wait_exit PID : WAIT_RC = exit status of PID once it has exited on its own (a trapped signal interrupts wait)
  local pid=$1
  while :; do
    WAIT_RC=0
    wait "$pid" 2>/dev/null || WAIT_RC=$?
    kill -0 "$pid" 2>/dev/null || return 0
  done
}

stop_all() {  # the gateway first (it waits for the form in flight), then the cloud server, then the capture
  if [ -n "$GATEWAY_PID" ]; then
    if kill -0 "$GATEWAY_PID" 2>/dev/null; then kill -TERM "$GATEWAY_PID" 2>/dev/null || :; fi
    wait_exit "$GATEWAY_PID"
    GATEWAY_RC=$WAIT_RC GATEWAY_PID=""
  fi
  if [ -n "$SERVER_PID" ]; then
    if kill -0 "$SERVER_PID" 2>/dev/null; then kill -TERM "$SERVER_PID" 2>/dev/null || :; fi
    wait_exit "$SERVER_PID"
    SERVER_RC=$WAIT_RC SERVER_PID=""
  fi
  if [ -n "$TCPDUMP_PID" ]; then
    sleep 1  # the last FIN/ACK segments reach the capture
    if kill -0 "$TCPDUMP_PID" 2>/dev/null; then kill -INT "$TCPDUMP_PID" 2>/dev/null || :; fi
    wait_exit "$TCPDUMP_PID"
    TCPDUMP_RC=$WAIT_RC TCPDUMP_PID=""
  fi
}

fail() {
  local code=$1 f
  shift
  trap - EXIT
  trap 'LATE_SIGNAL=1' INT TERM HUP  # an orderly stop is in progress; a second Ctrl+C does not cut it short
  stop_all
  for f in /tmp/pki_messages.txt /tmp/preflight.txt /tmp/run_server.out /tmp/gateway_serve.err /tmp/drive_presets.txt \
    /tmp/drive_permit_check.txt /tmp/drive_permit_negative.txt /tmp/drive_disclose.txt; do
    if [ -s "$f" ]; then printf -- '--- %s (last lines)\n' "$f" >&2; tail -n 15 "$f" >&2; fi
  done
  printf 'SIM_REHEARSAL_FAIL %s %s\n' "$code" "$*"
  exit 1
}
trap 'fail UNEXPECTED_EXIT "本演練意外結束（指令失敗，結束代碼 $?）；/evidence 內的檔案不完整。"' EXIT

on_signal() {  # auto: abort (after the step in progress); manual: the operator ends the session
  STOP_SIGNAL=$1
  if [ "$MODE" = auto ]; then
    fail INTERRUPTED "本演練收到 $1 而中止；閘道已等待執行中的表單完成後停止，/evidence 內的檔案不完整。"
  fi
}
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP

wait_for() {  # wait_for SECONDS COMMAND... : poll every 0.1 s until COMMAND succeeds
  local tenths=$(($1 * 10)) i
  shift
  for ((i = 0; i < tenths; i++)); do
    if "$@"; then return 0; fi
    sleep 0.1
  done
  return 1
}

step() {  # step NAME COMMAND... : run a driver step in the foreground; its output goes to stdout and /tmp/drive_NAME.txt
  local name=$1 rc=0 last
  shift
  "$@" 2>&1 | tee "/tmp/drive_$name.txt" || rc=$?
  mapfile -t STEP_LINES < <(grep -E '^SIM_[A-Z_]+ ' "/tmp/drive_$name.txt" || :)
  SUMMARY+=("${STEP_LINES[@]}")
  if [ "$rc" -ne 0 ]; then
    last=$(grep -E '^SIM_[A-Z_]+_FAIL ' "/tmp/drive_$name.txt" | tail -n 1 || :)
    fail "STEP_${name^^}" "步驟 $name 未通過（結束代碼 $rc）：${last:-沒有失敗標記}"
  fi
}

LINE=""
require_line() {  # require_line FILE PREFIX CODE : LINE = the first line of FILE that starts with PREFIX, else fail CODE
  # (called directly, never inside $(...): fail must run in this shell, the parent of every background process)
  LINE=$(awk -v p="$2" 'index($0, p) == 1 {print; exit}' "$1" 2>/dev/null || :)
  [ -n "$LINE" ] || fail "$3" "$1 缺少「$2」開頭的標記行。"
}

server_log_has() { [ -f "$SERVER_LOG" ] && grep -qF -- "$1" "$SERVER_LOG"; }

# 0. arguments and preconditions ------------------------------------------------------------------------------------
case $# in
  0) MODE=manual ;;
  1)
    case $1 in
      --auto) MODE=auto ;;
      -h | --help) trap - EXIT; usage; exit 0 ;;
      *) trap - EXIT; usage >&2; printf 'SIM_REHEARSAL_FAIL USAGE 本演練不接受此參數：%s\n' "$1"; exit 64 ;;
    esac
    ;;
  *) trap - EXIT; usage >&2; printf 'SIM_REHEARSAL_FAIL USAGE 本演練最多接受一個參數（--auto）。\n'; exit 64 ;;
esac
[ "$$" -ne 1 ] || fail NOT_UNDER_INIT "本演練以 PID 1 執行；操作人員必須以 docker run --init 啟動（閘道與呼叫許可檢查的父程序不得為 PID 1，invoke_permit.hpp:30）。"
for tool in openssl tcpdump python3 sha256sum awk sed sort tee cut; do
  command -v "$tool" >/dev/null 2>&1 || fail TOOL_MISSING "本演練找不到必要的工具：$tool。"
done
for f in /out/net/efnet_client /build/efnet_server /opt/efhe/run_server.sh /opt/efhe/make_demo_pki.sh \
  /opt/efhe/libefhe_keylog.so /opt/efhe/OVERLAY_RECORD.json /opt/efhe/BUILD_SHA256 "$GATEWAY" "$DRIVER" "$TEMPLATE"; do
  [ -e "$f" ] || fail IMAGE_INCOMPLETE "本演練找不到 $f；此程式只能在 edgefhe-demo-sim 映像內執行。"
done
if ! (cd / && sha256sum --check --strict --quiet /opt/efhe/BUILD_SHA256) >/dev/null 2>&1; then
  fail BUILD_INTEGRITY "映像內的檔案與建置時記錄的 SHA-256（/opt/efhe/BUILD_SHA256）不一致；操作人員應重新建置映像。"
fi
[ ! -e "$DMA_NODE" ] || fail ON_BOARD "偵測到 FPGA 裝置 $DMA_NODE；PC 演練不得在開發板上執行。"
[ -d "$EVIDENCE" ] && [ -w "$EVIDENCE" ] ||
  fail EVIDENCE_MISSING "本演練需要可寫入的 /evidence；操作人員應以 -v <空的證據資料夾>:/evidence 執行。"
if [ -n "$(ls -A "$EVIDENCE")" ]; then
  fail EVIDENCE_NOT_EMPTY "/evidence 不是空的資料夾。本演練不覆寫既有證據；操作人員應改用新的或空的證據資料夾。"
fi
if [ -e /pki ] || [ -L /pki ]; then
  fail PKI_MOUNTED "/pki 已存在。本演練只使用容器內新建的示範 PKI；操作人員不應掛載 /pki。"
fi
[ ! -e "$PKI" ] || fail PKI_EXISTS "$PKI 已存在。"
[ ! -e "$CONFIG" ] || fail CONFIG_EXISTS "$CONFIG 已存在；本演練每次都以新的示範 PKI 寫出新的閘道設定。"
TREE_ID=$(sed -n 's/^# staged_tree_id=//p' /opt/efhe/BUILD_SHA256)
COMMIT=$(sed -n 's/^# edgefhe_commit=//p' /opt/efhe/BUILD_SHA256)
OPENFHE=$(sed -n 's/^# openfhe=//p' /opt/efhe/BUILD_SHA256)
note "SIM_IMAGE mode=$MODE staged_tree_id=$TREE_ID commit=$COMMIT openfhe=${OPENFHE// /_} backend=spec"

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

# 2. gateway configuration and preflight ------------------------------------------------------------------------------
config_marker=$(python3 -I -B "$DRIVER" --write-config --template "$TEMPLATE" --server-pin "$SERVER_PIN" \
  --out "$CONFIG" 2>&1) || fail CONFIG "閘道設定檔無法寫出：$config_marker"
note "$config_marker"
PUBLIC_LOG=$(python3 -I -B -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["public_log"])' \
  "$CONFIG")
[ ! -e "$PUBLIC_LOG" ] || fail PUBLIC_LOG_EXISTS "閘道的公開紀錄 $PUBLIC_LOG 已存在。"
python3 -I -B "$GATEWAY" preflight --config "$CONFIG" --backend spec >/tmp/preflight.txt 2>&1 ||
  fail PREFLIGHT "閘道預檢未通過：$(grep -E '^(PREFLIGHT_FAIL|GATEWAY_PREFLIGHT_FAIL) ' /tmp/preflight.txt | tr '\n' ' ')"
require_line /tmp/preflight.txt 'GATEWAY_PREFLIGHT_PASS ' PREFLIGHT
note "$LINE"

# 3. capture first ----------------------------------------------------------------------------------------------------
tcpdump -i lo -s 0 -U -B 32768 -w "$CAPTURE" port "$PORT" 2>"$EVIDENCE/capture_stats.txt" &
TCPDUMP_PID=$!
capture_listening() { grep -qs 'listening on lo' "$EVIDENCE/capture_stats.txt"; }
wait_for 10 capture_listening || fail CAPTURE "tcpdump 未在 10 秒內開始擷取 lo。"
note "CAPTURE_STARTED interface=lo filter=port_$PORT file=capture.pcapng format=pcap"

# 4. cloud server with the key-log shim (demo/cloud/run_server.sh) -----------------------------------------------------
sh /opt/efhe/run_server.sh --loopback --keylog >/tmp/run_server.out 2>&1 &
SERVER_PID=$!
server_ready() { server_log_has CPP_E2E_SERVER_READY || ! kill -0 "$SERVER_PID" 2>/dev/null; }
wait_for 20 server_ready || fail SERVER_START "雲端伺服器未在 20 秒內就緒。"
server_log_has CPP_E2E_SERVER_READY || fail SERVER_START "雲端伺服器在就緒之前結束。"
server_log_has WARNING_DEMO_TLS_KEYLOG_ENABLED || fail SERVER_START "server.log 缺少 WARNING_DEMO_TLS_KEYLOG_ENABLED。"
note "SERVER_READY listen=127.0.0.1 port=$PORT connections=10000 keylog=on server_pin=$SERVER_PIN client_pin=$CLIENT_PIN"

# 5. gateway with the owner screen (PC rehearsal listen) -----------------------------------------------------------------
EDGEFHE_REHEARSAL=1 python3 -I -B "$GATEWAY" serve --config "$CONFIG" --rehearsal-listen 0.0.0.0 \
  >/tmp/gateway_serve.out 2>/tmp/gateway_serve.err &
GATEWAY_PID=$!
gateway_serving() { grep -qx "GATEWAY_SERVING 0.0.0.0:$HTTP_PORT" /tmp/gateway_serve.out || ! kill -0 "$GATEWAY_PID" 2>/dev/null; }
wait_for 20 gateway_serving || fail GATEWAY_START "閘道未在 20 秒內開始提供服務。"
grep -qx "GATEWAY_SERVING 0.0.0.0:$HTTP_PORT" /tmp/gateway_serve.out ||
  fail GATEWAY_START "閘道在提供服務之前結束：$(tail -n 3 /tmp/gateway_serve.err | tr '\n' ' ')"
note "GATEWAY_SERVING 0.0.0.0:$HTTP_PORT pid=$GATEWAY_PID config=$CONFIG owner_screen=http://127.0.0.1:$HTTP_PORT/"

# 6. the session -----------------------------------------------------------------------------------------------------
if [ "$MODE" = auto ]; then
  say "PC 演練（自動）：本演練依序送出預設的虛構表單；操作人員可在 PC 的瀏覽器開啟 http://127.0.0.1:$HTTP_PORT/ 觀看。"
  step presets python3 -I -B "$DRIVER" --config "$CONFIG" --capture "$CAPTURE" --server-log "$SERVER_LOG" \
    --requests "$WORK/requests.txt"
  require_line /tmp/drive_presets.txt 'SIM_FORM_PRESETS_PASS ' STEP_PRESETS
  step permit_check python3 -I -B "$DRIVER" --permit-check --config "$CONFIG" --capture "$CAPTURE"
  require_line /tmp/drive_permit_check.txt 'SIM_PERMIT_ACCEPTED_OWNER_CREATE ' STEP_PERMIT_CHECK
  step permit_negative python3 -I -B "$DRIVER" --permit-negative --config "$CONFIG" --capture "$CAPTURE"
  require_line /tmp/drive_permit_negative.txt 'SIM_PERMIT_CORRUPTED_REFUSED ' STEP_PERMIT_NEGATIVE
  kill -0 "$GATEWAY_PID" 2>/dev/null || fail GATEWAY_EXIT "閘道在演練期間自行結束。"
else
  note "SIM_MANUAL_READY owner_screen=http://127.0.0.1:$HTTP_PORT/ backend=spec"
  say "PC 演練已就緒：操作人員可在 PC 的瀏覽器開啟 http://127.0.0.1:$HTTP_PORT/ 填寫虛構表單（PC 扮演擁有者，僅限演練）。"
  say "結束時請在此視窗按 Ctrl+C，或執行 docker stop -t 120 <容器名稱>；本演練會等待執行中的表單完成後，再停止並整理證據。"
  gateway_rc=0
  while [ -z "$STOP_SIGNAL" ] && kill -0 "$GATEWAY_PID" 2>/dev/null; do
    wait "$GATEWAY_PID" 2>/dev/null || gateway_rc=$?
  done
  [ -n "$STOP_SIGNAL" ] || fail GATEWAY_EXIT "閘道在演練期間自行結束（結束代碼 $gateway_rc）：$(tail -n 3 /tmp/gateway_serve.err | tr '\n' ' ')"
  note "SIM_MANUAL_STOP signal=$STOP_SIGNAL"
fi

# 7. orderly stop: gateway, cloud server, capture -----------------------------------------------------------------------
trap 'LATE_SIGNAL=1' INT TERM HUP
stop_all
[ "$GATEWAY_RC" = 0 ] || fail GATEWAY_STOP "閘道停止時的結束代碼為 $GATEWAY_RC（預期 0）。"
grep -qx 'GATEWAY_STOP_REQUESTED signal=SIGTERM' /tmp/gateway_serve.out ||
  fail GATEWAY_STOP "閘道的輸出缺少 GATEWAY_STOP_REQUESTED signal=SIGTERM。"
[ "$SERVER_RC" = 143 ] || fail SERVER_STOP "雲端伺服器停止時的結束代碼為 $SERVER_RC（預期 143，即本演練送出的 SIGTERM）。"
[ "$TCPDUMP_RC" = 0 ] || fail CAPTURE "tcpdump 的結束代碼為 $TCPDUMP_RC。"
captured=$(sed -n 's/^\([0-9]*\) packets\{0,1\} captured$/\1/p' "$EVIDENCE/capture_stats.txt")
dropped=$(sed -n 's/^\([0-9]*\) packets\{0,1\} dropped by kernel$/\1/p' "$EVIDENCE/capture_stats.txt")
[ -n "$captured" ] || fail CAPTURE "capture_stats.txt 缺少擷取封包數。"
[ "$dropped" = 0 ] || fail CAPTURE_DROPS "tcpdump 回報核心丟棄了 ${dropped:-未知數量的} 封包。"
if [ "$MODE" = auto ] && [ "$captured" -eq 0 ]; then fail CAPTURE "tcpdump 未擷取到任何封包。"; fi
CAPTURE_SHA256=$(sha256sum "$CAPTURE" | cut -c1-64)
note "CAPTURE_STOPPED packets=$captured dropped=$dropped sha256=$CAPTURE_SHA256"

# server.log: every line time-stamped, the stop by this rehearsal, one response line per accepted form, and no
# connection stop other than the idle accept deadline (one per 30 s without a client).
if grep -vqE '^[0-9]{9,11}\.[0-9]{9} ' "$SERVER_LOG"; then fail SERVER_LOG "server.log 有缺少時間前綴的行。"; fi
server_log_has 'EDGEFHE_CLOUD_SERVER_EXIT rc=143 stop_signal=TERM' ||
  fail SERVER_LOG "server.log 缺少 EDGEFHE_CLOUD_SERVER_EXIT rc=143 stop_signal=TERM。"
responses=$(grep -cF '"status":"CPP_E2E_RESPONSE_WRITTEN_NOT_OWNER_ACCEPTANCE","sequence":1,' "$SERVER_LOG" || :)
other_stops=$(grep -F 'CPP_E2E_SERVER_CONNECTION_STOP' "$SERVER_LOG" | grep -cvF 'CPP_E2E_SERVER_CONNECTION_STOP TLS_DEADLINE' || :)
idle_stops=$(grep -cF 'CPP_E2E_SERVER_CONNECTION_STOP TLS_DEADLINE' "$SERVER_LOG" || :)
if [ "$MODE" = auto ]; then
  forms=$(wc -l <"$WORK/requests.txt")
  [ "$responses" -eq "$forms" ] ||
    fail SERVER_LOG "server.log 應恰有 $forms 行回應（每筆被接受的表單一行），實際為 $responses 行。"
  [ "$other_stops" -eq 0 ] || fail SERVER_LOG "server.log 有 $other_stops 次非閒置逾時的連線中止。"
fi
note "SERVER_LOG_CHECKED responses=$responses connection_stops=$other_stops idle_deadlines=$idle_stops exit=rc_143_stop_signal_TERM"

# tls_keys.log: the five TLS 1.3 labels for every connection; one connection per response.
mapfile -t randoms < <(awk 'NF {print $2}' "$KEYLOG" | sort -u)
for cr in "${randoms[@]}"; do
  labels=$(awk -v cr="$cr" '$2 == cr {print $1}' "$KEYLOG" | sort | tr '\n' ' ')
  [ "$labels" = "$LABELS " ] || fail KEYLOG "tls_keys.log 中 client_random $cr 的標籤不符：$labels"
done
if [ "$MODE" = auto ] && [ "${#randoms[@]}" -ne "$responses" ]; then
  fail KEYLOG "tls_keys.log 應含 $responses 條連線的金鑰，實際為 ${#randoms[@]} 條。"
fi
note "KEYLOG_CHECKED connections=${#randoms[@]} lines=$(awk 'NF' "$KEYLOG" | wc -l)"

# 8. only now, with the capture stopped: disclosures, public log, pins, journal, summary -----------------------------------
if [ "$MODE" = auto ]; then
  step disclose python3 -I -B "$DRIVER" --disclose --config "$CONFIG" --out "$EVIDENCE/disclosure" \
    --requests "$WORK/requests.txt"
else
  say "本演練為 PC 演練，所有答案均應為虛構；本演練將為每一筆被接受的表單寫出揭露檔，以便第三方驗證承諾值與推論結果。"
  step disclose python3 -I -B "$DRIVER" --disclose --config "$CONFIG" --out "$EVIDENCE/disclosure"
fi
require_line /tmp/drive_disclose.txt 'SIM_DISCLOSURES_WRITTEN ' STEP_DISCLOSE
disclosures=${LINE#SIM_DISCLOSURES_WRITTEN n=}
disclosures=${disclosures%% *}
if [ -f "$PUBLIC_LOG" ]; then cp "$PUBLIC_LOG" "$EVIDENCE/board_public.jsonl"; else : >"$EVIDENCE/board_public.jsonl"; fi
cp "$PKI/ca/server.pin" "$PKI/ca/client.pin" "$EVIDENCE/"
cp /tmp/gateway_serve.out "$EVIDENCE/gateway_serve.log"
public_rows=$(awk 'NF' "$EVIDENCE/board_public.jsonl" | wc -l)
refused=0
if [ "$MODE" = auto ]; then
  refused=$(grep -c '^SIM_FORM_REFUSED ' /tmp/drive_presets.txt || :)
  [ "$disclosures" -eq "$forms" ] || fail DISCLOSE "揭露檔數（$disclosures）應等於被接受的表單數（$forms）。"
else
  forms=$disclosures
fi
note "EVIDENCE_FILES $(cd "$EVIDENCE" && ls | sort | tr '\n' ' ')rehearsal_summary.txt"
note "SIM_REHEARSAL_PASS mode=$MODE forms=$forms refused=$refused connections=${#randoms[@]} disclosures=$disclosures public_rows=$public_rows packets=$captured dropped=0 capture_sha256=$CAPTURE_SHA256 staged_tree_id=$TREE_ID late_signal_ignored=${LATE_SIGNAL:-0}"
printf '%s\n' "${SUMMARY[@]}" >"$EVIDENCE/rehearsal_summary.txt"
say "下一步：操作人員應在 PC 上執行 python -B demo\\evidence\\efhe_seal.py --run <證據資料夾> --verify，先封存擷取檔，再以金鑰紀錄驗證 G0–G10。"
trap - EXIT
