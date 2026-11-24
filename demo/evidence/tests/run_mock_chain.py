"""PC-only rehearsal of the EdgeFHE evidence chain: dumpcap on the loopback adapter, the Python TLS 1.3 mock,
then efhe_seal.py (seal, capinfos, editcap --inject-secrets, verifier, report.html, MANIFEST.sha256).

Three sealed runs (plus refused_first below), each in its own directory under a work directory outside the
repository:
  uniform           forms M, M, K with uniform residues; a second dumpcap is the observer (gate G11)  -> PASS
  negative_control  form M with small residues (an unencrypted-looking body)                     -> FAIL:G6
  tampered_keylog   the uniform run with one hex digit of a traffic secret changed in tls_keys.log  -> FAIL:G2
Then the verifier runs twice more on the uniform run; both verify_report.json files must be byte-identical to the
one the seal step wrote. Last, refused_first captures refused SYN/RST connections to the server port followed by one
form session, and efhe_verify.quick_session (the console's per-session decode) must still decode that session as a
form (MOCK_CHAIN_QUICK_SESSION ok).

usage: python -B demo/evidence/tests/run_mock_chain.py [--work DIR] [--keep] [--iface NAME] [--pki-python]
Success: MOCK_CHAIN_PASS uniform=PASS negative_control=FAIL:G6 tampered_keylog=FAIL:G2 (exit 0).
Needs Wireshark (dumpcap, tshark, editcap, capinfos) and loopback capture (Npcap on Windows: \\Device\\NPF_Loopback;
Linux: lo with capture rights). The PKI comes from demo/pki/make_demo_pki.sh (Git Bash and OpenSSL 3), or with
--pki-python, or when bash is missing, from the 'cryptography' package. The work directory holds throwaway private
keys and TLS key logs: it is deleted at the end unless --keep is given.
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EVIDENCE = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(EVIDENCE))
sys.path.insert(0, EVIDENCE)
sys.path.insert(0, HERE)
import efhe_verify  # noqa: E402
import mock_efnet  # noqa: E402

SEAL = os.path.join(EVIDENCE, "efhe_seal.py")
VERIFY = os.path.join(EVIDENCE, "efhe_verify.py")
PKI_SCRIPT = os.path.join(REPO, "demo", "pki", "make_demo_pki.sh")
MODEL = os.path.join(REPO, "demo", "model", "cdc-diabetes-lr4-v1.json")
SCHEMA = os.path.join(REPO, "demo", "model", "form_cdc.json")
WINDOWS = os.name == "nt"
EXPECT = dict(uniform="PASS", negative_control="FAIL:G6", tampered_keylog="FAIL:G2")


def say(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def inside(path, root):
    """Case-folded and link-resolved containment (efhe_verify._under): on Windows 'C:\\Work\\Repo' and 'c:\\work\\repo'
    name the same folder, so a plain commonpath comparison would let a work directory inside the repository pass."""
    return efhe_verify._under(path, root)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ PKI
def find_bash():
    cands = [os.environ.get("EFHE_BASH")]
    if WINDOWS:
        cands += [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"]
    w = shutil.which("bash")
    if w and not (WINDOWS and "system32" in w.lower()):         # never the WSL launcher
        cands.append(w)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def make_pki(out, force_python=False):
    bash = None if force_python else find_bash()
    if bash:
        env = dict(os.environ, MSYS_NO_PATHCONV="1")
        r = subprocess.run([bash, PKI_SCRIPT.replace("\\", "/"), "all-in-one", out.replace("\\", "/")],
                           capture_output=True, env=env)
        text = r.stdout.decode("utf-8", "replace")
        if r.returncode == 0 and "EDGEFHE_DEMO_PKI_READY" in text:
            return "make_demo_pki.sh all-in-one"
        raise SystemExit("MOCK_CHAIN_PKI_FAILED %s %s" % (text.strip(), r.stderr.decode("utf-8", "replace")[-400:]))
    return make_pki_python(out)


def make_pki_python(out):
    """The make_demo_pki.sh all-in-one layout with the 'cryptography' package (fallback without bash)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    start = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    end = datetime.datetime(2027, 12, 31, 23, 59, 59, tzinfo=datetime.timezone.utc)

    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    def pem_key(k):
        return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())

    def pin(cert):
        return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    for d in ("ca", "cloud", "board"):
        os.makedirs(os.path.join(out, d))
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(name("EdgeFHE demo CA")).issuer_name(name("EdgeFHE demo CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(start)
          .not_valid_after(end).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .sign(ca_key, hashes.SHA256()))

    def leaf(cn, eku):
        k = ec.generate_private_key(ec.SECP256R1())
        c = (x509.CertificateBuilder().subject_name(name(cn)).issuer_name(ca.subject).public_key(k.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(start).not_valid_after(end)
             .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
             .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
             .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
             .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False)
             .sign(ca_key, hashes.SHA256()))
        return k, c
    sk, sc = leaf("edgefhe-cloud", ExtendedKeyUsageOID.SERVER_AUTH)
    ck, cc = leaf("edgefhe-kv260", ExtendedKeyUsageOID.CLIENT_AUTH)
    files = {"ca/ca.pem": ca.public_bytes(serialization.Encoding.PEM), "ca/ca.key": pem_key(ca_key),
             "cloud/ca.pem": ca.public_bytes(serialization.Encoding.PEM),
             "cloud/server.pem": sc.public_bytes(serialization.Encoding.PEM), "cloud/server.key": pem_key(sk),
             "board/ca.pem": ca.public_bytes(serialization.Encoding.PEM),
             "board/client.pem": cc.public_bytes(serialization.Encoding.PEM), "board/client.key": pem_key(ck)}
    for role, cert, dirs in (("server", sc, ("ca", "board")), ("client", cc, ("ca", "cloud"))):
        for d in dirs:
            files["%s/%s.pin" % (d, role)] = (pin(cert) + "\n").encode()
    for rel, data in files.items():
        with open(os.path.join(out, rel), "wb") as f:
            f.write(data)
    return "cryptography package (fallback)"


# ------------------------------------------------------------------ capture
class Dumpcap:
    def __init__(self, iface, port, path, stats):
        exe = efhe_verify.find_tool("dumpcap")
        if not exe:
            raise SystemExit("MOCK_CHAIN_TOOL_MISSING dumpcap")
        self.stats_path = stats
        self.err = open(stats, "wb")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0
        self.p = subprocess.Popen([exe, "-i", iface, "-f", "tcp port %d" % port, "-s", "0", "-w", path],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.err,
                                  creationflags=flags)
        for _ in range(150):
            time.sleep(0.1)
            if self.p.poll() is not None:
                break
            with open(stats, "rb") as f:
                if b"Capturing on" in f.read():
                    return
        self.err.close()
        with open(stats, "rb") as f:
            raise SystemExit("MOCK_CHAIN_DUMPCAP_NOT_STARTED %s" % f.read().decode("utf-8", "replace")[-300:])

    def stop(self):
        time.sleep(1.0)                                  # let dumpcap flush the last packets
        if WINDOWS:
            os.kill(self.p.pid, signal.CTRL_BREAK_EVENT)
        else:
            self.p.send_signal(signal.SIGINT)
        try:
            self.p.wait(20)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        self.err.close()
        with open(self.stats_path, "rb") as f:
            text = f.read().decode("utf-8", "replace")
        if "Packets received/dropped" not in text:
            raise SystemExit("MOCK_CHAIN_DUMPCAP_NOT_STOPPED_CLEANLY %s" % text[-300:])
        with open(self.stats_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text.replace("\r\n", "\n"))


def capture_run(run, iface, pki, forms, small=None, observer=False):
    os.makedirs(run)
    port = free_port()
    caps = [Dumpcap(iface, port, os.path.join(run, "capture.pcapng"), os.path.join(run, "capture_stats.txt"))]
    if observer:
        caps.append(Dumpcap(iface, port, os.path.join(run, "observer.pcapng"), os.path.join(run, "observer_stats.txt")))
    try:
        res, rc = mock_efnet.run_both(pki, port, run, forms, MODEL, SCHEMA, small)
    finally:
        for c in caps:
            c.stop()
    if rc != 0:
        raise SystemExit("MOCK_CHAIN_SERVER_REJECTED rc=%s" % rc)
    for role in ("server", "client"):
        shutil.copyfile(os.path.join(pki, "ca", role + ".pin"), os.path.join(run, role + ".pin"))
    return port, res


def refused_connect(port, host="127.0.0.1"):
    """One connection attempt to a port where nothing listens yet (SYN answered by RST; Windows retries the SYN)."""
    s = socket.socket()
    s.settimeout(10)
    try:
        s.connect((host, port))
    except OSError:
        return True
    finally:
        s.close()
    return False


def capture_refused_first(run, iface, pki):
    """A board that connects before the server is ready: refused SYN/RST connections, then one form session, all
    on the server port in one capture. Their tcp.stream indices come before the form's, but they carry no TLS."""
    os.makedirs(run)
    port = free_port()
    cap = Dumpcap(iface, port, os.path.join(run, "capture.pcapng"), os.path.join(run, "capture_stats.txt"))
    try:
        refused = refused_connect(port)
        res, rc = mock_efnet.run_both(pki, port, run, ["M"], MODEL, SCHEMA)
    finally:
        cap.stop()
    if rc != 0 or not refused:
        raise SystemExit("MOCK_CHAIN_REFUSED_FIRST_SETUP rc=%s refused=%s" % (rc, refused))
    for role in ("server", "client"):
        shutil.copyfile(os.path.join(pki, "ca", role + ".pin"), os.path.join(run, role + ".pin"))
    return port, res


def quick_session_after_refused(run, port):
    """efhe_verify.quick_session must decode the form session itself (regression: it used to pass the tcp.stream
    index to 'follow,tls', which tshark 4.6 numbers by TLS stream, and so reported the session as handshake_only)."""
    capture = os.path.join(run, "capture.pcapng")
    conns = efhe_verify.native.tcp_connections(efhe_verify.native.read_capture(capture), port)
    forms = [c for c in conns if c["c2s"]]
    empty = [c for c in conns if not c["c2s"]]
    if len(forms) != 1 or not empty:
        say("MOCK_CHAIN_QUICK_SESSION MISMATCH connections=%d with_data=%d" % (len(conns), len(forms)))
        return False
    board_port = forms[0]["client_port"]
    r = efhe_verify.quick_session(capture, os.path.join(run, "tls_keys.log"), board_port, port=port, model=MODEL,
                                  pins=run)
    conn = r.get("connection") or {}
    gates = r.get("gates") or {}
    good = (bool(r.get("ok")) and conn.get("classification") == "form" and
            conn.get("client", "").endswith(":%d" % board_port) and
            all(gates.get(g, {}).get("status") == "PASS" for g in ("G3", "G4", "G5", "G6", "G8")))
    say("MOCK_CHAIN_QUICK_SESSION %s refused_connections=%d board_port=%d classification=%s gates=%s" % (
        "ok" if good else "MISMATCH", len(empty), board_port, conn.get("classification"),
        ",".join("%s=%s" % (g, gates[g].get("status")) for g in sorted(gates, key=efhe_verify.GATES.index))))
    return good


def tamper_keylog(src, dst):
    """Copy the inputs of a run and change one hex digit of the first CLIENT_TRAFFIC_SECRET_0 in tls_keys.log."""
    os.makedirs(dst)
    for name in ("capture.pcapng", "capture_stats.txt", "server.log", "board_public.jsonl", "server.pin", "client.pin"):
        shutil.copyfile(os.path.join(src, name), os.path.join(dst, name))
    shutil.copytree(os.path.join(src, "disclosure"), os.path.join(dst, "disclosure"))
    with open(os.path.join(src, "tls_keys.log"), encoding="ascii") as f:
        lines = f.read().splitlines()
    done = False
    for i, line in enumerate(lines):
        p = line.split()
        if not done and len(p) == 3 and p[0] == "CLIENT_TRAFFIC_SECRET_0":
            s = p[2]
            p[2] = s[:-1] + ("0" if s[-1] != "0" else "1")
            lines[i] = " ".join(p)
            done = True
    with open(os.path.join(dst, "tls_keys.log"), "w", encoding="ascii", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return done


def seal(run, extra=()):
    r = subprocess.run([sys.executable, "-B", SEAL, "--run", run, "--verify"] + list(extra), capture_output=True)
    out = r.stdout.decode("utf-8", "replace")
    for line in out.splitlines():
        if line.startswith("EFHE_"):
            say("  " + line)
    path = os.path.join(run, "verify_report.json")
    if not os.path.isfile(path):
        raise SystemExit("MOCK_CHAIN_NO_REPORT %s rc=%d %s" % (run, r.returncode, r.stderr.decode("utf-8", "replace")[-500:]))
    with open(path, encoding="utf-8") as f:
        return json.load(f), out, r.returncode


def main(argv=None):
    efhe_verify.utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--work", help="work directory (new or empty, outside the repository)")
    ap.add_argument("--keep", action="store_true", help="keep the work directory (captures, logs, throwaway keys)")
    ap.add_argument("--iface", default=r"\Device\NPF_Loopback" if WINDOWS else "lo")
    ap.add_argument("--pki-python", action="store_true", help="create the PKI with the cryptography package")
    a = ap.parse_args(argv)
    work = os.path.abspath(a.work) if a.work else tempfile.mkdtemp(prefix="edgefhe_mock_chain_")
    if inside(work, REPO):
        say("MOCK_CHAIN_REFUSED 工作目錄位於 repository 之內；本測試只在 repository 以外寫入檔案。")
        return 2
    if os.path.exists(work) and os.listdir(work):
        say("MOCK_CHAIN_REFUSED 工作目錄不是空的：%s" % work)
        return 2
    os.makedirs(work, exist_ok=True)
    verdicts, ok = {}, True
    try:
        pki = os.path.join(work, "pki")
        say("MOCK_CHAIN_PKI %s" % make_pki(pki, a.pki_python))
        uniform = os.path.join(work, "uniform")
        negative = os.path.join(work, "negative_control")
        tampered = os.path.join(work, "tampered_keylog")
        port_u, res = capture_run(uniform, a.iface, pki, ["M", "M", "K"], observer=True)
        say("MOCK_CHAIN_CAPTURED uniform forms=%d port=%d" % (len(res), port_u))
        port_n, res = capture_run(negative, a.iface, pki, ["M"], small=1)
        say("MOCK_CHAIN_CAPTURED negative_control forms=%d port=%d" % (len(res), port_n))
        if not tamper_keylog(uniform, tampered):
            raise SystemExit("MOCK_CHAIN_TAMPER_FAILED")
        for name, run, port, extra in (("uniform", uniform, port_u, ()),
                                       ("negative_control", negative, port_n, ("--expect-negative-control",)),
                                       ("tampered_keylog", tampered, port_u, ())):
            say("MOCK_CHAIN_SEAL %s" % name)
            report, _out, rc = seal(run, ("--port", str(port)) + extra)
            verdicts[name] = report["verdict"]
            want = EXPECT[name]
            good = report["verdict"] == want
            if name == "uniform":
                good = good and report["evidence_pass"] and report["gates"]["G11"]["status"] == "PASS" and rc == 0
            if name == "negative_control":
                good = good and report["negative_control"]["detected"] and rc == 0
            if name == "tampered_keylog":
                g2 = report["gates"]["G2"]["checks"]
                good = good and any("AEAD" in p for c in g2 for p in c.get("problems", []))
            say("MOCK_CHAIN_RESULT %s verdict=%s expected=%s %s" % (name, report["verdict"], want,
                                                                   "ok" if good else "MISMATCH"))
            ok = ok and good
        with open(os.path.join(uniform, "verify_report.json"), "rb") as f:
            first = f.read()
        same = True
        for i in (1, 2):
            out = os.path.join(work, "determinism_%d.json" % i)
            subprocess.run([sys.executable, "-B", VERIFY, "--run", uniform, "--port", str(port_u), "--out", out],
                           capture_output=True)
            with open(out, "rb") as f:
                same = same and f.read() == first
        say("MOCK_CHAIN_DETERMINISTIC_REPORT %s bytes=%d" % ("identical" if same else "DIFFERENT", len(first)))
        ok = ok and same
        refused_first = os.path.join(work, "refused_first")
        port_r, res = capture_refused_first(refused_first, a.iface, pki)
        say("MOCK_CHAIN_CAPTURED refused_first forms=%d port=%d" % (len(res), port_r))
        ok = quick_session_after_refused(refused_first, port_r) and ok
    finally:
        if a.keep:
            say("MOCK_CHAIN_WORK_KEPT %s（內含一次性的私鑰與 TLS 金鑰紀錄，用畢請刪除）" % work)
        else:
            shutil.rmtree(work, ignore_errors=True)
    line = "uniform=%s negative_control=%s tampered_keylog=%s" % (
        verdicts.get("uniform"), verdicts.get("negative_control"), verdicts.get("tampered_keylog"))
    if ok:
        say("MOCK_CHAIN_PASS " + line)
        return 0
    say("MOCK_CHAIN_FAIL " + line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
