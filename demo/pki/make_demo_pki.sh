#!/usr/bin/env bash
# Demo-only PKI for the EdgeFHE final demo: ECDSA P-256 certificates for TLS 1.3 mutual authentication with two-sided
# pinning between the board client (efnet_client) and the cloud server (efnet_server).
#
# usage: make_demo_pki.sh all-in-one OUT        CA, cloud bundle and board bundle; the board key is generated here
#                                               (PC rehearsals only)
#        make_demo_pki.sh ca-and-server OUT     CA and cloud bundle; the board generates its own key and CSR
#        make_demo_pki.sh sign-client OUT CSR   sign the board CSR with OUT/ca: writes OUT/board/{ca.pem,client.pem,
#                                               server.pin} and OUT/cloud/client.pin
#
# OUT layout (every private key exists in exactly one directory):
#   ca/     ca.key, ca.pem, the openssl ca database (index.txt, newcerts/) and the public record of what was issued:
#           server.csr, server.pem, server.pin, client.csr, client.pem, client.pin
#   cloud/  ca.pem, server.pem, server.key, client.pin       (mounted read-only as /pki in the cloud container)
#   board/  ca.pem, client.pem, server.pin [, client.key]    (the board PKI directory; client.key only in all-in-one)
#
# Every certificate is issued by openssl ca with the fixed validity 2020-01-01T00:00:00Z .. 2027-12-31T23:59:59Z, because
# the board clock restarts in 2023 at every boot (docs/BOARD_GUIDE.md:27). The CA has CA:TRUE, pathlen:0. tls.hpp checks
# names with X509_CHECK_FLAG_NEVER_CHECK_SUBJECT (tls.hpp:101,105,111), so each leaf carries a DNS SAN: the server
# serverAuth + DNS:edgefhe-cloud, the client clientAuth + DNS:edgefhe-kv260. A pin is the lowercase SHA-256 of the leaf
# certificate DER, the value X509_digest compares in tls.hpp:110 (common.hpp:31-35 accepts lowercase hex only).
#
# Safety: umask 077; an existing OUT is refused; nothing is written inside a git work tree (a walk up for .git, then
# git rev-parse); a signed client is never overwritten; a failed creation removes the partial OUT.
# Output: on success stdout carries exactly one marker line (EDGEFHE_DEMO_PKI_READY, EDGEFHE_DEMO_PKI_CA_SERVER_READY or
# EDGEFHE_DEMO_CLIENT_SIGNED); messages go to stderr (UTF-8). Exit status 2 = refused, 1 = an OpenSSL step failed.
# Runs on Linux (cloud image, board) and in Git Bash on Windows: OpenSSL only ever receives relative paths, so the native
# Windows build needs no MSYS path conversion, and conversion is switched off so that -subj /CN=... passes unchanged.
if [ -z "${BASH_VERSION:-}" ]; then
  echo 'EDGEFHE_DEMO_PKI_REFUSED USAGE 本程式必須以 bash 執行。' >&2
  exit 2
fi
set -euo pipefail
umask 077
export LC_ALL=C MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

readonly START=20200101000000Z END=20271231235959Z     # openssl ca -startdate / -enddate
readonly START_EPOCH=1577836800 END_EPOCH=1830297599   # the same two instants as Unix time
DATES_TEXT=$(printf '%s\n' 'notBefore=Jan  1 00:00:00 2020 GMT' 'notAfter=Dec 31 23:59:59 2027 GMT')
readonly DATES_TEXT                                    # openssl x509 -noout -startdate -enddate for that window
readonly CA_SUBJECT='/CN=EdgeFHE demo CA' SERVER_NAME=edgefhe-cloud CLIENT_NAME=edgefhe-kv260
readonly CSR_MAX_BYTES=16384
WORK="" WREL="" CLEAN_ON_FAIL="" SERVER_PIN="" CLIENT_PIN=""
IS_MSYS=0
case "$(uname -s 2>/dev/null || true)" in MINGW* | MSYS*) IS_MSYS=1 ;; esac

usage() {
  cat <<'EOF'
用法：
  bash make_demo_pki.sh all-in-one OUT
      本程式在 OUT 建立示範 CA、雲端憑證與板端憑證。板端私鑰在本機產生，因此本模式僅適用於電腦上的演練。
  bash make_demo_pki.sh ca-and-server OUT
      本程式在 OUT 建立示範 CA 與雲端憑證。板端私鑰與 CSR 由板子自行產生，私鑰不離開板子。
  bash make_demo_pki.sh sign-client OUT CSR
      本程式以 OUT/ca 的 CA 簽發板子產生的 CSR，寫出 OUT/board/{ca.pem,client.pem,server.pin} 與 OUT/cloud/client.pin。

OUT 的內容（每一把私鑰只存在於一個目錄）：
  ca/     CA 私鑰 ca.key、CA 憑證與簽發紀錄；此目錄只留在建立 PKI 的電腦上。
  cloud/  雲端伺服器使用的 ca.pem、server.pem、server.key、client.pin。
  board/  板子使用的 ca.pem、client.pem、server.pin（all-in-one 模式另含 client.key）。

憑證有效期間固定為 2020-01-01 00:00:00 UTC 至 2027-12-31 23:59:59 UTC；板子每次開機時，時鐘回到 2023 年，該時間仍在有效期間內。
本程式拒絕覆寫既有的 OUT，也不在任何 git 工作樹內寫入檔案。成功時，本程式在標準輸出只寫一行結果標記，其餘訊息寫到標準錯誤。
本 PKI 僅供研究展示使用；展示結束後，操作人員應刪除整個 OUT。
EOF
}

say() { printf '%s\n' "$*" >&2; }
refuse() { local code=$1; shift; printf 'EDGEFHE_DEMO_PKI_REFUSED %s %s\n' "$code" "$*" >&2; exit 2; }
fail() { local step=$1; shift; printf 'EDGEFHE_DEMO_PKI_FAILED %s %s\n' "$step" "$*" >&2; exit 1; }
show() {  # show PATH : the path in the form the operator uses on this platform (Windows form under Git Bash)
  if [ "$IS_MSYS" = 1 ]; then cygpath -w -- "$1" 2>/dev/null || printf '%s' "$1"; else printf '%s' "$1"; fi
}
posix_path() {  # posix_path PATH : Git Bash also accepts C:\... arguments; dirname and the .git walk need the /c/... form
  if [ "$IS_MSYS" = 1 ]; then cygpath -u -- "$1"; else printf '%s' "$1"; fi
}

on_exit() {
  local rc=$?
  trap - EXIT
  cd / 2>/dev/null || true  # Windows cannot remove a directory that is still the working directory
  if [ -n "$WORK" ] && [ -d "$WORK" ]; then rm -rf -- "$WORK"; fi
  if [ "$rc" -ne 0 ] && [ -n "$CLEAN_ON_FAIL" ] && [ -d "$CLEAN_ON_FAIL" ]; then
    rm -rf -- "$CLEAN_ON_FAIL"
    say "本程式已移除未完成的輸出：$(show "$CLEAN_ON_FAIL")"
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

inside_git_work_tree() {  # inside_git_work_tree DIR : true when the existing DIR lies in a git work tree or a .git directory
  local dir p out
  dir=$(cd -- "$1" 2>/dev/null && pwd -P) || return 0  # a directory that cannot be resolved counts as unsafe
  p=$dir
  if [ "$IS_MSYS" = 1 ]; then p=$(cygpath -m -- "$dir") || return 0; fi  # walk the Windows ancestors, not MSYS mounts
  while :; do
    if [ -e "$p/.git" ]; then return 0; fi
    case /$p/ in */.git/*) return 0 ;; esac
    case $p in */*) p=${p%/*} ;; *) break ;; esac
    if [ -z "$p" ]; then
      if [ -e /.git ]; then return 0; fi
      break
    fi
  done
  if command -v git >/dev/null 2>&1; then  # git runs inside DIR, so it gets no path argument to convert
    if out=$(cd -- "$dir" && env -u GIT_DIR -u GIT_WORK_TREE -u GIT_CEILING_DIRECTORIES GIT_DISCOVERY_ACROSS_FILESYSTEM=1 \
      git -c safe.directory='*' rev-parse --is-inside-work-tree --is-inside-git-dir 2>&1); then
      case $out in *true*) return 0 ;; esac
    else
      case $out in *"not a git repository"*) ;; *) return 0 ;; esac
    fi
  fi
  return 1
}

need_tools() {
  local t v
  for t in openssl sha256sum cut tr sed grep wc mktemp dirname env; do
    command -v "$t" >/dev/null 2>&1 || refuse TOOL_MISSING "本程式找不到必要的工具：$t。"
  done
  if [ "$IS_MSYS" = 1 ]; then command -v cygpath >/dev/null 2>&1 || refuse TOOL_MISSING "本程式找不到必要的工具：cygpath。"; fi
  v=$(openssl version 2>/dev/null | tr -d '\r') || v=""
  case $v in "OpenSSL 3."*) ;; *) refuse OPENSSL_VERSION "本程式需要 OpenSSL 3.x；目前的版本為：${v:-無法判讀}。" ;; esac
}

write_config() {  # the CA and request configuration; every path is relative to OUT/ca, the working directory of OpenSSL
  cat >"$WORK/ca.cnf" <<'EOF'
# EdgeFHE demo CA (written by make_demo_pki.sh for the duration of one run; demo use only).
[ ca ]
default_ca = demo_ca

[ demo_ca ]
dir             = .
database        = $dir/index.txt
new_certs_dir   = $dir/newcerts
# openssl ca requires this name even with -rand_serial; it then only records the last random serial there.
serial          = $dir/serial
default_md      = sha256
policy          = policy_cn
unique_subject  = no
copy_extensions = none
email_in_dn     = no
preserve        = no

[ policy_cn ]
commonName = supplied

[ req ]
prompt             = no
distinguished_name = req_dn
string_mask        = utf8only

[ req_dn ]
CN = edgefhe-demo-request

[ v3_ca ]
basicConstraints     = critical, CA:TRUE, pathlen:0
keyUsage             = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash

[ v3_server ]
basicConstraints       = critical, CA:FALSE
keyUsage               = critical, digitalSignature
extendedKeyUsage       = serverAuth
subjectAltName         = DNS:edgefhe-cloud
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid:always

[ v3_client ]
basicConstraints       = critical, CA:FALSE
keyUsage               = critical, digitalSignature
extendedKeyUsage       = clientAuth
subjectAltName         = DNS:edgefhe-kv260
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid:always
EOF
}

start_work() {  # start_work OUT : private scratch directory inside OUT (removed on exit) plus the OpenSSL configuration
  WORK=$(mktemp -d "$1/.work.XXXXXX") || fail WORKDIR "本程式無法建立暫存目錄。"
  WREL=../${WORK##*/}
  write_config
  export OPENSSL_CONF="$WREL/ca.cnf"  # relative: every OpenSSL call below runs in OUT/ca
}

ossl() {  # ossl STEP ARGS... : run openssl with its output kept in the work directory; stop with STEP on failure
  local step=$1
  shift
  if ! openssl "$@" >"$WORK/openssl.log" 2>&1; then
    tr -d '\r' <"$WORK/openssl.log" >&2 || true
    fail "$step" "OpenSSL 步驟失敗：openssl $1。"
  fi
}
ossl_text() {  # ossl_text STEP ARGS... : like ossl, but print what openssl wrote to stdout (CR removed)
  local step=$1
  shift
  if ! openssl "$@" >"$WORK/openssl.txt" 2>"$WORK/openssl.log"; then
    tr -d '\r' <"$WORK/openssl.log" >&2 || true
    fail "$step" "OpenSSL 步驟失敗：openssl $1。"
  fi
  tr -d '\r' <"$WORK/openssl.txt"
}
lf() {  # lf FILE : keep every PEM LF-only (the native Windows OpenSSL writes PEM in text mode, that is CRLF)
  local crs  # counted with tr: the Git Bash grep drops CR characters before it matches, so it cannot detect them
  crs=$(tr -cd '\r' <"$1" | wc -c | tr -d ' \t')
  if [ "$crs" != 0 ]; then tr -d '\r' <"$1" >"$1.lf" && mv -f -- "$1.lf" "$1"; fi
}
lf_db() {  # cwd OUT/ca : the openssl ca database stays LF-only, so a CA created in Git Bash also works under Linux
  local f
  for f in index.txt index.txt.attr index.txt.old index.txt.attr.old serial serial.old newcerts/*.pem; do
    if [ -f "$f" ]; then lf "$f"; fi
  done
}
same_text() { [ -f "$1" ] && [ -f "$2" ] && [ "$(tr -d '\r' <"$1")" = "$(tr -d '\r' <"$2")" ]; }
write_pin() { printf '%s\n' "$1" >"$2"; }

gen_key() { ossl KEYGEN genpkey -algorithm EC -pkeyopt ec_paramgen_curve:prime256v1 -pkeyopt ec_param_enc:named_curve -out "$1"; lf "$1"; }
make_csr() { ossl CSR req -new -config "$WREL/ca.cnf" -key "$1" -subj "$2" -out "$3"; lf "$3"; }
ca_sign() {  # ca_sign STEP EXTENSIONS CSR CERT [ARGS...] : openssl ca with the fixed validity window
  local step=$1 ext=$2 csr=$3 cert=$4
  shift 4
  lf_db
  ossl "$step" ca -batch -config "$WREL/ca.cnf" -rand_serial -md sha256 -startdate "$START" -enddate "$END" \
    -extensions "$ext" -notext "$@" -in "$csr" -out "$cert"
  lf "$cert"
  lf_db
}
pub_cert() { ossl_text PUBKEY x509 -in "$1" -noout -pubkey; }
pub_key() { ossl_text PUBKEY pkey -in "$1" -pubout; }
pub_csr() { ossl_text PUBKEY req -in "$1" -noout -pubkey; }

pin_of() {  # pin_of CERT : 64 lowercase hex, the SHA-256 of the certificate DER, cross-checked with the OpenSSL fingerprint
  local a b
  a=$(openssl x509 -in "$1" -outform DER | sha256sum | cut -c1-64) || fail PIN "本程式無法計算釘選值：$1。"
  b=$(openssl x509 -in "$1" -noout -fingerprint -sha256 | tr -d '\r' | sed -e 's/^[^=]*=//' -e 's/://g' | tr 'A-F' 'a-f') ||
    fail PIN "本程式無法讀取憑證指紋：$1。"
  if [[ ! $a =~ ^[0-9a-f]{64}$ ]] || [ "$a" != "$b" ]; then fail PIN "釘選值與 OpenSSL 指紋不一致：$1。"; fi
  printf '%s' "$a"
}

norm_ext() {  # norm_ext CERT : basicConstraints, keyUsage, extendedKeyUsage and subjectAltName, trimmed, one item per line
  ossl_text EXTENSIONS x509 -in "$1" -noout -ext basicConstraints,keyUsage,extendedKeyUsage,subjectAltName |
    sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}
check_window() {  # check_window CERT PURPOSE : the encoded window is exact; chains to ca.pem for PURPOSE inside it only
  local t got
  got=$(ossl_text DATES x509 -in "$1" -noout -startdate -enddate) || exit 1
  [ "$got" = "$DATES_TEXT" ] || fail VALIDITY "憑證的有效期間與預期不符：$1。"
  # OpenSSL treats notAfter itself as expired (X509_cmp_time counts equality as earlier), so the last second it accepts
  # is END_EPOCH - 1; the checks one second outside the window hold under either reading.
  for t in "$START_EPOCH" $((END_EPOCH - 1)); do
    ossl VERIFY verify -x509_strict -attime "$t" -purpose "$2" -CAfile ca.pem "$1"
  done
  for t in $((START_EPOCH - 1)) $((END_EPOCH + 1)); do
    if openssl verify -x509_strict -attime "$t" -purpose "$2" -CAfile ca.pem "$1" >"$WORK/openssl.log" 2>&1; then
      fail VALIDITY "憑證在有效期間之外仍通過驗證：$1。"
    fi
  done
}
check_ca() {  # cwd OUT/ca : the CA certificate has the required extensions and window, and ca.key belongs to it
  local got want a b
  want=$(printf '%s\n' 'X509v3 Basic Constraints: critical' 'CA:TRUE, pathlen:0' 'X509v3 Key Usage: critical' \
    'Certificate Sign, CRL Sign')
  got=$(norm_ext ca.pem) || exit 1
  [ "$got" = "$want" ] || fail EXTENSIONS "CA 憑證的延伸欄位與預期不符。"
  check_window ca.pem any
  a=$(pub_cert ca.pem) || exit 1
  b=$(pub_key ca.key) || exit 1
  [ "$a" = "$b" ] || refuse CA_MISMATCH "ca/ca.key 與 ca/ca.pem 不相符；本程式不使用此 CA。"
}
check_leaf() {  # check_leaf CERT PURPOSE DNS EKU : leaf extensions exactly as required, chain, purpose and window
  local got want
  want=$(printf '%s\n' 'X509v3 Basic Constraints: critical' 'CA:FALSE' 'X509v3 Key Usage: critical' 'Digital Signature' \
    'X509v3 Extended Key Usage:' "$4" 'X509v3 Subject Alternative Name:' "DNS:$3")
  got=$(norm_ext "$1") || exit 1
  [ "$got" = "$want" ] || fail EXTENSIONS "憑證的延伸欄位與預期不符：$1。"
  check_window "$1" "$2"
}
check_cloud_bundle() {  # cwd OUT/ca : the cloud bundle was issued by this CA (sign-client must not mix PKI directories)
  same_text ca.pem ../cloud/ca.pem || refuse CA_MISMATCH "cloud/ca.pem 與 ca/ca.pem 不同；本程式不混用不同的 PKI 目錄。"
  same_text server.pem ../cloud/server.pem || refuse CA_MISMATCH "cloud/server.pem 與 ca/server.pem 不同；本程式不混用不同的 PKI 目錄。"
  check_leaf ../cloud/server.pem sslserver "$SERVER_NAME" 'TLS Web Server Authentication'
}
check_csr() {  # check_csr CSR : a readable request with a valid self-signature, EC P-256, not the CA or the server key
  local csr=$1 pub text other
  openssl req -in "$csr" -noout >"$WORK/openssl.log" 2>&1 ||
    refuse CSR_UNREADABLE "本程式無法讀取 CSR；CSR 必須是 PEM 格式的憑證簽署要求。"
  if ! openssl req -in "$csr" -noout -verify >"$WORK/openssl.log" 2>&1 || ! grep -q 'self-signature verify OK' "$WORK/openssl.log"; then
    refuse CSR_SIGNATURE "CSR 的自簽章驗證失敗；本程式只簽發由持有對應私鑰者產生的 CSR。"
  fi
  pub=$(pub_csr "$csr") || exit 1
  printf '%s\n' "$pub" >"$WORK/request_pub.pem"
  text=$(ossl_text CSR_KEY pkey -pubin -in "$WREL/request_pub.pem" -noout -text_pub) || exit 1
  if ! grep -qx 'Public-Key: (256 bit)' <<<"$text" || ! grep -qx 'ASN1 OID: prime256v1' <<<"$text"; then
    refuse CSR_KEY_TYPE "CSR 的公鑰不是 ECDSA P-256（prime256v1）；本程式只簽發 P-256 金鑰。"
  fi
  for other in ca.pem ../cloud/server.pem; do
    text=$(pub_cert "$other") || exit 1
    [ "$pub" != "$text" ] || refuse CSR_KEY_REUSED "CSR 的公鑰與 $other 相同；板端必須使用自己的私鑰。"
  done
}

init_ca() {  # cwd OUT/ca : CA key and certificate; openssl ca -selfsign applies the same fixed window as every leaf
  mkdir -- newcerts
  : >index.txt
  gen_key ca.key
  make_csr ca.key "$CA_SUBJECT" "$WREL/ca.csr"
  ca_sign SIGN_CA v3_ca "$WREL/ca.csr" ca.pem -selfsign -keyfile ca.key
  check_ca
}
issue_server() {  # cwd OUT/ca : server key (only in ../cloud), certificate, cloud bundle and the server pin record
  local a b
  gen_key ../cloud/server.key
  make_csr ../cloud/server.key "/CN=$SERVER_NAME" server.csr
  ca_sign SIGN_SERVER v3_server server.csr server.pem -cert ca.pem -keyfile ca.key
  check_leaf server.pem sslserver "$SERVER_NAME" 'TLS Web Server Authentication'
  a=$(pub_key ../cloud/server.key) || exit 1
  b=$(pub_cert server.pem) || exit 1
  [ "$a" = "$b" ] || fail SERVER_KEY "伺服器憑證與 cloud/server.key 不相符。"
  SERVER_PIN=$(pin_of server.pem) || exit 1
  write_pin "$SERVER_PIN" server.pin
  cp -f -- ca.pem ../cloud/ca.pem
  cp -f -- server.pem ../cloud/server.pem
}
issue_client() {  # issue_client CSR : cwd OUT/ca, CSR already checked; the certificate subject is set by the CA
  local csr=$1 a b
  ca_sign SIGN_CLIENT v3_client "$csr" "$WREL/client.pem" -cert ca.pem -keyfile ca.key -subj "/CN=$CLIENT_NAME"
  check_leaf "$WREL/client.pem" sslclient "$CLIENT_NAME" 'TLS Web Client Authentication'
  a=$(pub_csr "$csr") || exit 1
  b=$(pub_cert "$WREL/client.pem") || exit 1
  [ "$a" = "$b" ] || fail CLIENT_KEY "簽發的用戶端憑證公鑰與 CSR 不符。"
  CLIENT_PIN=$(pin_of "$WREL/client.pem") || exit 1
  SERVER_PIN=$(pin_of ../cloud/server.pem) || exit 1
  cp -f -- "$csr" client.csr
  cp -f -- "$WREL/client.pem" client.pem
  write_pin "$CLIENT_PIN" client.pin
  cp -f -- ca.pem ../board/ca.pem
  cp -f -- "$WREL/client.pem" ../board/client.pem
  write_pin "$SERVER_PIN" ../board/server.pin
  write_pin "$CLIENT_PIN" ../cloud/.client.pin.tmp
  mv -f -- ../cloud/.client.pin.tmp ../cloud/client.pin  # last step: the cloud trusts this client from here on
}

windows_note() {
  if [ "$IS_MSYS" = 1 ]; then
    say "注意：Git Bash 在 Windows 上不套用 POSIX 權限（umask 077 對 NTFS 無效），私鑰檔案的存取權限取決於上層目錄的 NTFS 權限；操作人員應將 PKI 目錄放在僅限本人存取的位置。"
  fi
}

cmd_create() {  # cmd_create MODE OUT : all-in-one or ca-and-server
  local mode=$1 out parent a b
  out=$(posix_path "$2")
  if [ -e "$out" ] || [ -L "$out" ]; then
    refuse OUT_EXISTS "輸出目錄已存在：$(show "$out")。本程式不覆寫既有的 PKI；操作人員應指定新的目錄。"
  fi
  parent=$(dirname -- "$out")
  [ -d "$parent" ] || refuse OUT_PARENT_MISSING "輸出目錄的上層目錄不存在：$(show "$parent")。"
  if inside_git_work_tree "$parent"; then
    refuse OUT_IN_GIT_WORK_TREE "輸出目錄位於 git 工作樹內：$(show "$out")。本程式不在任何 git 工作樹內寫入金鑰或憑證；操作人員應改用工作樹以外的目錄。"
  fi
  mkdir -- "$out" 2>/dev/null || refuse OUT_EXISTS "本程式無法建立輸出目錄（目錄可能已存在）：$(show "$out")。"
  out=$(cd -- "$out" && pwd -P)
  CLEAN_ON_FAIL=$out
  if inside_git_work_tree "$out"; then
    CLEAN_ON_FAIL=""
    rmdir -- "$out" 2>/dev/null || true
    refuse OUT_IN_GIT_WORK_TREE "輸出目錄位於 git 工作樹內：$(show "$out")。本程式不在任何 git 工作樹內寫入金鑰或憑證；操作人員應改用工作樹以外的目錄。"
  fi
  mkdir -- "$out/ca" "$out/cloud"
  if [ "$mode" = all-in-one ]; then mkdir -- "$out/board"; fi
  start_work "$out"
  cd -- "$out/ca"
  init_ca
  issue_server
  if [ "$mode" = all-in-one ]; then
    gen_key ../board/client.key
    make_csr ../board/client.key "/CN=$CLIENT_NAME" "$WREL/client.csr"
    check_csr "$WREL/client.csr"
    issue_client "$WREL/client.csr"
    a=$(pub_key ../board/client.key) || exit 1
    b=$(pub_cert client.pem) || exit 1
    [ "$a" = "$b" ] || fail CLIENT_KEY "用戶端憑證與 board/client.key 不相符。"
  fi
  CLEAN_ON_FAIL=""
  if [ "$mode" = all-in-one ]; then
    say "EdgeFHE 示範 PKI 已建立：$(show "$out")"
    say "  ca/     CA 私鑰 ca.key 與簽發紀錄；此目錄只留在建立 PKI 的電腦上。"
    say "  cloud/  雲端伺服器使用 ca.pem、server.pem、server.key、client.pin（此目錄在容器內以唯讀方式掛載為 /pki）。"
    say "  board/  板子使用 ca.pem、client.pem、client.key、server.pin。"
  else
    say "EdgeFHE 示範 CA 與雲端憑證已建立：$(show "$out")"
    say "  ca/     CA 私鑰 ca.key 與簽發紀錄；此目錄只留在建立 PKI 的電腦上。"
    say "  cloud/  雲端伺服器使用 ca.pem、server.pem、server.key；client.pin 由 sign-client 寫入。"
    say "下一步：板子以自己的私鑰產生 CSR（主體 CN=$CLIENT_NAME），操作人員再以 sign-client 簽發；板端私鑰不離開板子。"
    say "雲端伺服器須等 sign-client 寫出 cloud/client.pin 之後才能啟動。"
  fi
  say "憑證有效期間：2020-01-01 00:00:00 UTC 至 2027-12-31 23:59:59 UTC；金鑰為 ECDSA P-256；釘選值為憑證 DER 的 SHA-256。"
  if [ "$mode" = all-in-one ]; then
    say "注意：all-in-one 模式在本機產生板端私鑰，僅適用於電腦上的演練。正式展示時，操作人員應改用 ca-and-server 與 sign-client，使板端私鑰在板上產生且不離開板子。"
  fi
  windows_note
  say "本 PKI 僅供研究展示使用；展示結束後，操作人員應刪除整個輸出目錄。"
  if [ "$mode" = all-in-one ]; then
    printf 'EDGEFHE_DEMO_PKI_READY server_pin=%s client_pin=%s\n' "$SERVER_PIN" "$CLIENT_PIN"
  else
    printf 'EDGEFHE_DEMO_PKI_CA_SERVER_READY server_pin=%s\n' "$SERVER_PIN"
  fi
}

cmd_sign_client() {  # cmd_sign_client OUT CSR
  local out csr f size subject
  out=$(posix_path "$1")
  csr=$(posix_path "$2")
  [ -d "$out" ] || refuse OUT_MISSING "本程式找不到 PKI 目錄：$(show "$out")；sign-client 需要 ca-and-server 建立的目錄。"
  out=$(cd -- "$out" && pwd -P)
  if inside_git_work_tree "$out"; then
    refuse OUT_IN_GIT_WORK_TREE "PKI 目錄位於 git 工作樹內：$(show "$out")。本程式不在任何 git 工作樹內寫入金鑰或憑證。"
  fi
  for f in ca/ca.key ca/ca.pem ca/index.txt ca/server.pem cloud/ca.pem cloud/server.pem; do
    [ -f "$out/$f" ] || refuse CA_MISSING "PKI 目錄缺少 $f；操作人員應對 ca-and-server 建立的目錄執行 sign-client。"
  done
  [ -d "$out/ca/newcerts" ] || refuse CA_MISSING "PKI 目錄缺少 ca/newcerts；操作人員應對 ca-and-server 建立的目錄執行 sign-client。"
  if [ -e "$out/board" ] || [ -e "$out/cloud/client.pin" ]; then
    refuse CLIENT_ALREADY_SIGNED "本 PKI 已簽發過板端憑證（board/ 或 cloud/client.pin 已存在）；本程式不覆寫。若板子更換了私鑰，操作人員應先移除 board/ 與 cloud/client.pin，重新簽發後再以新的 client.pin 重新啟動雲端伺服器。"
  fi
  [ -f "$csr" ] || refuse CSR_MISSING "本程式找不到 CSR 檔案：$(show "$csr")。"
  size=$(wc -c <"$csr" | tr -d ' \r\n')
  if [ "$size" -le 0 ] || [ "$size" -gt "$CSR_MAX_BYTES" ]; then
    refuse CSR_SIZE "CSR 檔案大小為 $size 位元組，不在 1 至 $CSR_MAX_BYTES 位元組之間。"
  fi
  start_work "$out"
  cp -- "$csr" "$WORK/client.csr"
  lf "$WORK/client.csr"
  cd -- "$out/ca"
  check_ca
  check_cloud_bundle
  check_csr "$WREL/client.csr"
  subject=$(ossl_text CSR_SUBJECT req -in "$WREL/client.csr" -noout -subject -nameopt RFC2253) || exit 1
  subject=${subject#subject=}
  mkdir -- ../board 2>/dev/null || refuse CLIENT_ALREADY_SIGNED "board/ 已存在；本程式不覆寫已簽發的板端憑證。"
  CLEAN_ON_FAIL=$out/board
  issue_client "$WREL/client.csr"
  CLEAN_ON_FAIL=""
  say "板端憑證已簽發：$(show "$out/board")"
  if [ "$subject" != "CN=$CLIENT_NAME" ]; then
    say "  CSR 的主體為「$subject」；本 CA 將憑證主體設為 CN=$CLIENT_NAME。"
  fi
  say "  憑證主體 CN=$CLIENT_NAME，DNS SAN $CLIENT_NAME，用途為 TLS 用戶端驗證，有效期間 2020-01-01 至 2027-12-31（UTC）。"
  say "  board/  操作人員應將 ca.pem、client.pem、server.pin 傳回板上的 PKI 目錄；板端私鑰 client.key 留在板上。"
  say "  cloud/  client.pin 已寫入；雲端伺服器以此釘選值驗證板子的憑證，因此雲端伺服器須以此檔重新啟動。"
  windows_note
  say "本 PKI 僅供研究展示使用；展示結束後，操作人員應刪除整個輸出目錄與板上的 client.key。"
  printf 'EDGEFHE_DEMO_CLIENT_SIGNED client_pin=%s\n' "$CLIENT_PIN"
}

main() {
  case ${1:-} in
    -h | --help | help)
      usage
      exit 0
      ;;
    all-in-one | ca-and-server)
      if [ $# -ne 2 ] || [ -z "$2" ]; then usage >&2; refuse USAGE "$1 模式需要一個參數 OUT。"; fi
      need_tools
      cmd_create "$1" "$2"
      ;;
    sign-client)
      if [ $# -ne 3 ] || [ -z "$2" ] || [ -z "$3" ]; then usage >&2; refuse USAGE "sign-client 模式需要兩個參數 OUT 與 CSR。"; fi
      need_tools
      cmd_sign_client "$2" "$3"
      ;;
    *)
      usage >&2
      refuse USAGE "未知的模式：${1:-（未指定）}。"
      ;;
  esac
}

main "$@"
