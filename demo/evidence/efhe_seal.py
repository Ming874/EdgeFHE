"""Seal and verify an EdgeFHE demo evidence run directory.

Order of the evidence chain (the seal comes before any use of the key log):
  1. SHA-256 of capture.pcapng -> capture.sha256 (prints EFHE_CAPTURE_SEALED sha256=...). An existing seal record is
     checked instead; a mismatch stops with EFHE_CAPTURE_SEAL_MISMATCH. When capture_dsb.pcapng already exists before
     the seal record (the key log was used first), the tool still seals but writes seal_order.json, and the verifier
     reports gate G0 check seal_order as FAIL-PENDING (EFHE_SEAL_ORDER_WARNING).
  2. capinfos -A capture.pcapng -> capinfos.txt
  3. editcap --inject-secrets tls,tls_keys.log -> capture_dsb.pcapng (a copy that Wireshark decrypts on its own;
     the sealed capture.pcapng is never modified)
  4. with --verify or --expect-negative-control: efhe_verify.verify_run -> verify_report.json, then report.html
  5. MANIFEST.sha256 over every file of the run directory (LF, sorted by path)
  6. with --sign-key KEY: ssh-keygen -Y sign -n edgefhe-evidence -> MANIFEST.sha256.sig

usage: python -B efhe_seal.py --run DIR [--verify] [--expect-negative-control] [--sign-key KEY]
                              [--board-public FILE] [--disclosure PATH] [--observer FILE] [--model FILE] [--port N]
DIR must lie outside the repository (it holds the capture, the TLS key log and the decryptable copy); the check
resolves links, folds case on Windows, where NTFS paths are case-insensitive, and also recognises the '\\\\?\\' and
admin-share ('\\\\localhost\\C$\\...') spellings of the repository by file identity (EFHE_SEAL_REFUSED reason=...).
Markers: EFHE_CAPTURE_SEALED, EFHE_SEAL_ORDER_WARNING / EFHE_SEAL_ORDER_RECORDED, EFHE_SECRETS_INJECTED,
EFHE_MANIFEST_WRITTEN, EFHE_EVIDENCE_PASS / EFHE_EVIDENCE_FAIL, EFHE_NEGATIVE_CONTROL_DETECTED /
EFHE_NEGATIVE_CONTROL_MISSED. Exit status: 0 success, 1 verification outcome not as required (a recorded late seal
gives FAIL-PENDING:G0), 2 refused (run directory inside the repository, missing capture, seal mismatch, tool failure).
The Wireshark tools are called with argument lists (no shell quoting); on Windows they are found under
C:\\Program Files\\Wireshark when they are not on PATH. Python 3.10 or later, standard library only.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import efhe_verify  # noqa: E402
import report_html  # noqa: E402

CAPTURE, SEAL, KEYLOG = "capture.pcapng", "capture.sha256", "tls_keys.log"
DSB, CAPINFOS, REPORT, HTML = "capture_dsb.pcapng", "capinfos.txt", "verify_report.json", "report.html"
MANIFEST, SIGNATURE = "MANIFEST.sha256", "MANIFEST.sha256.sig"
ORDER = efhe_verify.SEAL_ORDER_FILE
NAMESPACE = "edgefhe-evidence"


def say(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def refuse(code, text):
    say("%s %s" % (code, text))
    return 2


def write_lf(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def seal(run):
    cap = os.path.join(run, CAPTURE)
    digest = efhe_verify.sha256_file(cap)
    rec = os.path.join(run, SEAL)
    if os.path.exists(rec):
        with open(rec, encoding="ascii", errors="replace") as f:
            sealed = (f.read().split() or [""])[0].lower()
        if sealed != digest:
            return None, "EFHE_CAPTURE_SEAL_MISMATCH sealed=%s capture=%s：本程式偵測到擷取檔與封存紀錄不一致，已停止。" % (
                sealed[:16], digest[:16])
        say("EFHE_CAPTURE_SEALED sha256=%s file=%s state=already_sealed" % (digest, CAPTURE))
        if os.path.exists(os.path.join(run, ORDER)):
            say("EFHE_SEAL_ORDER_RECORDED file=%s：本次封存晚於金鑰紀錄的使用，驗證項目 G0 標示為待確認。" % ORDER)
        return digest, None
    early = [n for n in (DSB, DSB + ".tmp") if os.path.exists(os.path.join(run, n))]
    if early:
        # The record goes into the run directory (and so into MANIFEST.sha256 and the report): a seal taken after the
        # key log was used must never read as 'sealed first'.
        order = dict(violation="decrypted_copy_before_seal", files=dict(
            (n, efhe_verify.sha256_file(os.path.join(run, n))) for n in early if os.path.isfile(os.path.join(run, n))),
            capture_sha256=digest, detail="%s existed before %s was written" % (" and ".join(early), SEAL))
        write_lf(os.path.join(run, ORDER), json.dumps(order, sort_keys=True, indent=1) + "\n")
        say("EFHE_SEAL_ORDER_WARNING file=%s 注意：%s 在封存之前已存在，表示 TLS 金鑰紀錄在封存之前已被使用。"
            "本程式仍封存目前的擷取檔，並將此情形記錄於 %s；驗證報告的驗證項目 G0 因此標示為待確認。" % (
                ORDER, "、".join(early), ORDER))
    write_lf(rec, "%s  %s\n" % (digest, CAPTURE))
    say("EFHE_CAPTURE_SEALED sha256=%s file=%s" % (digest, CAPTURE))
    return digest, None


def run_tool(name, args, cwd):
    exe = efhe_verify.find_tool(name)
    if not exe:
        raise RuntimeError("TOOL_NOT_FOUND %s" % name)
    r = subprocess.run([exe] + args, cwd=cwd, capture_output=True)
    if r.returncode:
        raise RuntimeError("%s failed (%d): %s" % (name, r.returncode, r.stderr.decode("utf-8", "replace")[-300:]))
    return r.stdout.decode("utf-8", "replace")


def capinfos(run):
    text = run_tool("capinfos", ["-A", CAPTURE], run)
    write_lf(os.path.join(run, CAPINFOS), text.replace("\r\n", "\n"))


def inject_secrets(run):
    if not os.path.isfile(os.path.join(run, KEYLOG)):
        say("EFHE_SECRETS_NOT_INJECTED 本程式在執行目錄中找不到 %s，因此不產生可自行解密的副本。" % KEYLOG)
        return False
    tmp = DSB + ".tmp"
    run_tool("editcap", ["-F", "pcapng", "--inject-secrets", "tls," + KEYLOG, CAPTURE, tmp], run)
    os.replace(os.path.join(run, tmp), os.path.join(run, DSB))
    say("EFHE_SECRETS_INJECTED file=%s（此副本內含 TLS 金鑰，僅供展示時以 Wireshark 解密檢視）" % DSB)
    return True


def manifest(run):
    rows = []
    for root, dirs, files in os.walk(run):
        dirs.sort()
        for name in files:
            p = os.path.join(root, name)
            rel = os.path.relpath(p, run).replace("\\", "/")
            if rel in (MANIFEST, SIGNATURE) or rel.endswith(".tmp"):
                continue
            rows.append((rel, efhe_verify.sha256_file(p)))
    rows.sort()
    text = "".join("%s  %s\n" % (h, rel) for rel, h in rows)
    write_lf(os.path.join(run, MANIFEST), text)
    say("EFHE_MANIFEST_WRITTEN files=%d sha256=%s" % (len(rows), hashlib.sha256(text.encode()).hexdigest()))


def sign(run, key):
    exe = shutil.which("ssh-keygen")
    if not exe:
        raise RuntimeError("TOOL_NOT_FOUND ssh-keygen")
    sig = os.path.join(run, SIGNATURE)
    if os.path.exists(sig):
        os.remove(sig)
    r = subprocess.run([exe, "-Y", "sign", "-f", key, "-n", NAMESPACE, MANIFEST], cwd=run, capture_output=True)
    if r.returncode or not os.path.isfile(sig):
        raise RuntimeError("ssh-keygen -Y sign failed: %s" % r.stderr.decode("utf-8", "replace")[-300:])
    say("EFHE_MANIFEST_SIGNED file=%s namespace=%s" % (SIGNATURE, NAMESPACE))


def main(argv=None):
    efhe_verify.utf8_stdio()
    ap = argparse.ArgumentParser(description="Seal, decrypt-copy and verify an EdgeFHE demo evidence run directory.")
    ap.add_argument("--run", required=True)
    ap.add_argument("--verify", action="store_true", help="run gates G0-G11 and write verify_report.json, report.html")
    ap.add_argument("--expect-negative-control", action="store_true",
                    help="success means that gate G6 flags the negative-control request (for example the probe)")
    ap.add_argument("--sign-key", help="ssh private key for ssh-keygen -Y sign")
    ap.add_argument("--board-public")
    ap.add_argument("--disclosure")
    ap.add_argument("--observer")
    ap.add_argument("--model")
    ap.add_argument("--port", type=int)
    ap.add_argument("--tshark")
    a = ap.parse_args(argv)
    run = efhe_verify.plain_path(os.path.abspath(a.run))           # tshark and editcap cannot open '\\?\' paths
    if efhe_verify.inside_repository(run):
        return refuse("EFHE_SEAL_REFUSED", "reason=RUN_INSIDE_REPOSITORY 本程式不在 repository 之內封存證據：證據資料夾"
                      "含有擷取檔、TLS 金鑰紀錄與可自行解密的副本，必須位於 repository 以外（例如 "
                      "C:\\EdgeFHE_evidence\\<場次>）。")
    if not os.path.isfile(os.path.join(run, CAPTURE)):
        return refuse("EFHE_SEAL_REFUSED", "本程式在 %s 找不到 %s。" % (run, CAPTURE))
    digest, err = seal(run)
    if err:
        return refuse("EFHE_SEAL_REFUSED", err)
    try:
        capinfos(run)
        inject_secrets(run)
    except RuntimeError as e:
        return refuse("EFHE_SEAL_TOOL_FAILED", str(e))
    report = None
    if a.verify or a.expect_negative_control:
        opts = dict(board_public=a.board_public, disclosure=a.disclosure, observer=a.observer, model=a.model,
                    port=a.port, tshark=a.tshark)
        try:
            report = efhe_verify.verify_run(run, opts)
        except (efhe_verify.VerifyError, OSError) as e:
            return refuse("EFHE_VERIFY_ERROR", str(e))
        write_lf(os.path.join(run, REPORT), efhe_verify.dumps_report(report))
        report_html.write_report(report, run, os.path.join(run, HTML))
        efhe_verify.print_summary(report)
    manifest(run)
    if a.sign_key:
        try:
            sign(run, a.sign_key)
        except RuntimeError as e:
            return refuse("EFHE_SIGN_FAILED", str(e))
    if report is None:
        say("本程式已封存擷取檔；取得板端公開紀錄與虛構揭露檔之後，請以 --verify 執行驗證。")
        return 0
    if a.expect_negative_control:
        neg = report["negative_control"]
        if neg["detected"]:
            first = neg["requests"][0]
            say("EFHE_NEGATIVE_CONTROL_DETECTED gate=G6 connection=%s sequence=%s reasons=%s limbs=%d" % (
                first["connection"], first["sequence"], ",".join(first["reasons"]), first["limbs"]))
            say("負對照已被偵測：驗證項目 G6 判定該請求的餘數不是均勻分布的密文。整體判定：%s" % report["verdict"])
            return 0
        say("EFHE_NEGATIVE_CONTROL_MISSED verdict=%s：驗證項目 G6 未偵測到負對照請求。" % report["verdict"])
        return 1
    if report["evidence_pass"]:
        say("EFHE_EVIDENCE_PASS gates=G0..G10 G11=%s verdict=%s" % (report["gates"]["G11"]["status"], report["verdict"]))
        return 0
    say("EFHE_EVIDENCE_FAIL verdict=%s" % report["verdict"])
    return 1


if __name__ == "__main__":
    sys.exit(main())
