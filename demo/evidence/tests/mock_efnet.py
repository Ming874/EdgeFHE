"""Python TLS 1.3 mock of the EdgeFHE efnet exchange, for PC-only tests of the evidence chain.

This is a test peer, not the application: the "ciphertexts" are random residues, never CKKS encryptions of the form.
It speaks the real wire format (wire.hpp) over TLS 1.3 with mutual authentication and two-sided pinning, so the
capture, key log and logs it produces have the same structure as a demo run:

- server: like efnet_server (server.cpp): checks the client pin and DNS SAN, validates each request (header layout,
  sequence, session_id == TLS exporter, request_id, request_hash, canonical residues), answers with the public affine
  function (cloud.cpp:24-43), and writes the NSS key log (DEMO ONLY) and server.log lines '<epoch> <json>' as
  run_server.sh does.
- client: like one efnet_client run per form: a fresh connection, a fresh key_id and generation, session_id from the
  TLS exporter (computed from the client's own temporary key log, because Python's ssl has no exporter API), one
  request of 4 ciphertexts (header write, then 16 KiB writes), the response check, close_notify. As the owner side it
  writes board_public.jsonl (public hashes and the salted form commitment) and fictional disclosures. The disclosed
  'logit_ckks' is the plaintext reference plus 1e-5 (marked "mock": there is no CKKS here).
- --small N makes the N-th form (1-based) a negative control: small residues, as an unencrypted encoding would look.

The wire helpers below are written independently of efhe_verify.py on purpose, so the mock chain cross-checks it.

usage:
  python -B mock_efnet.py server --pki DIR --port P --keylog FILE --log FILE [--connections N] [--model JSON]
  python -B mock_efnet.py client --pki DIR --port P --out DIR [--host 127.0.0.1] [--forms M,M,K] [--small N]
  python -B mock_efnet.py run    --pki DIR --port P --out DIR [--forms M,M,K] [--small N]     (both, in one process)
PKI: the make_demo_pki.sh layout (cloud/, board/, ca/) or one flat directory. Python 3.10 or later.
"""
import argparse
import hashlib
import hmac
import json
import os
import random
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time

N = 4096
CT_WORDS = 6 * N
CT_BYTES = 4 * CT_WORDS
HDR = 256
Q = (998244353, 1004535809, 469762049)
PROFILE = bytes.fromhex("c01b69cf3da9fb260dbd4ac15080bd6c1c425d90bfb9015c8bacc16c061fa05c")
LABEL = b"EXPERIMENTAL-EdgeFHE-Wire-v1"
SERVER_NAME, CLIENT_NAME = "edgefhe-cloud", "edgefhe-kv260"
COMMIT_TAG = "EDGEFHE_FORM_COMMIT_V1"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = os.path.join(HERE, "..", "..", "model", "cdc-diabetes-lr4-v1.json")
SYS = random.SystemRandom()


# ------------------------------------------------------------------ wire (independent of efhe_verify.py)
def expand_label(secret, label, context, length, name):
    full = b"tls13 " + label
    info = struct.pack(">H", length) + bytes([len(full)]) + full + bytes([len(context)]) + context
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(secret, block + info + bytes([counter]), name).digest()
        out += block
        counter += 1
    return out[:length]


def exporter(master, label, context, length):
    name = "sha384" if len(master) == 48 else "sha256"
    derived = expand_label(master, label, hashlib.new(name).digest(), len(master), name)
    return expand_label(derived, b"exporter", hashlib.new(name, context).digest(), length, name)


def header(kind, features, rows, nbytes, generation, sequence, model, key, rid, session, rhash):
    h = bytearray(HDR)
    h[0:8] = b"EFHEW001"
    h[8], h[10] = 1, kind
    struct.pack_into("<IIIII", h, 12, HDR, features, rows, 1, 0)
    struct.pack_into("<QQQQ", h, 32, nbytes, generation, sequence, 0)
    for i, d in enumerate((PROFILE, model, key, rid, session, rhash)):
        h[64 + 32 * i:96 + 32 * i] = d
    return bytes(h)


def build_request(features, generation, sequence, model, key, session, body):
    zero = bytes(32)
    rid = hashlib.sha256(header(1, features, 1, features * CT_BYTES, generation, sequence, model, key, zero, session,
                                zero)).digest()
    partial = header(1, features, 1, features * CT_BYTES, generation, sequence, model, key, rid, session, zero)
    rhash = hashlib.sha256(partial + body).digest()
    return header(1, features, 1, features * CT_BYTES, generation, sequence, model, key, rid, session, rhash)


def random_body(features, small=False, rng=SYS):
    out = bytearray()
    for _f in range(features):
        for _p in range(2):
            for q in Q:
                if small:
                    vals = [rng.randint(-(1 << 21), 1 << 21) % q for _ in range(N)]
                else:
                    vals = [rng.randrange(q) for _ in range(N)]
                out += struct.pack("<%dI" % N, *vals)
    return bytes(out)


def cloud_affine(body, features, weights, bias):
    """cloud.cpp: per limb l and polynomial p, sum_f x * (w_f mod q_l) mod q_l, plus bias mod q_l on c0[0]."""
    x = struct.unpack("<%dI" % (features * CT_WORDS), body)
    out = [0] * CT_WORDS
    for p in range(2):
        for l, q in enumerate(Q):
            w = [wi % q for wi in weights]
            base = p * 3 * N + l * N
            for i in range(N):
                acc = 0
                for f in range(features):
                    acc += x[f * CT_WORDS + base + i] * w[f]
                out[base + i] = acc % q
            if p == 0:
                out[base] = (out[base] + bias % q) % q
    return struct.pack("<%dI" % CT_WORDS, *out)


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if not buf:
                return None
            raise EOFError("TLS_TRUNCATED_MESSAGE")
        buf += chunk
    return bytes(buf)


def send_chunked(sock, data):
    for i in range(0, len(data), 16384):          # efnet writes at most 16,384 bytes per SSL_write_ex (tls.hpp:120)
        sock.sendall(data[i:i + 16384])


def pin_of(sock):
    return hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()


def last_exporter_master(path):
    secret = None
    with open(path, encoding="ascii", errors="replace") as f:
        for line in f:
            p = line.split()
            if len(p) == 3 and p[0] == "EXPORTER_SECRET":
                secret = bytes.fromhex(p[2])
    return secret


# ------------------------------------------------------------------ PKI layout
class Pki:
    def __init__(self, root):
        def pick(sub, name):
            for d in (os.path.join(root, sub), root):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    return p
            raise SystemExit("MOCK_PKI_MISSING %s/%s" % (sub, name))
        self.server_ca, self.server_cert, self.server_key = pick("cloud", "ca.pem"), pick("cloud", "server.pem"), pick(
            "cloud", "server.key")
        self.client_pin_file = pick("cloud", "client.pin")
        self.client_ca, self.client_cert, self.client_key = pick("board", "ca.pem"), pick("board", "client.pem"), pick(
            "board", "client.key")
        self.server_pin_file = pick("board", "server.pin")

    @staticmethod
    def read(path):
        with open(path, encoding="ascii") as f:
            return f.read().strip()


def load_model(path):
    with open(path, "rb") as f:
        raw = f.read()
    m = json.loads(raw.decode("utf-8"))
    m["_sha256"] = hashlib.sha256(raw).digest()
    return m


# ------------------------------------------------------------------ server
class Log:
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8", newline="\n")
        self.lock = threading.Lock()

    def line(self, text):
        with self.lock:
            self.f.write("%.9f %s\n" % (time.time(), text))
            self.f.flush()

    def close(self):
        self.f.close()


def server_main(pki_dir, port, keylog, log_path, connections, model_path, host="127.0.0.1", ready=None):
    pki, model = Pki(pki_dir), load_model(model_path)
    log = Log(log_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(pki.server_cert, pki.server_key)
    ctx.load_verify_locations(pki.server_ca)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.options |= ssl.OP_NO_TICKET
    ctx.keylog_filename = keylog                  # DEMO ONLY: the cloud side writes the NSS key log
    log.line("WARNING_DEMO_TLS_KEYLOG_ENABLED %s" % os.path.basename(keylog))
    client_pin = pki.read(pki.client_pin_file)
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind((host, port))
    ls.listen(4)
    log.line(json.dumps({"status": "CPP_E2E_SERVER_READY", "port": port, "test_injection": False},
                        separators=(",", ":")))
    if ready is not None:
        ready.set()
    evaluated = written = rejected = 0
    for _ in range(connections):
        raw, _addr = ls.accept()
        raw.settimeout(30)
        try:
            with ctx.wrap_socket(raw, server_side=True) as s:
                if pin_of(s) != client_pin:
                    raise ValueError("TLS_PEER_PIN")
                sans = [v for k, v in s.getpeercert().get("subjectAltName", ()) if k == "DNS"]
                if CLIENT_NAME not in sans:
                    raise ValueError("TLS_CLIENT_SAN")
                session = exporter(last_exporter_master(keylog), LABEL, PROFILE, 32)
                expect, key0, gen0 = 1, None, None
                while True:
                    h = recv_exact(s, HDR)
                    if h is None:
                        try:
                            s.unwrap()                    # answer the client's close_notify (tls.hpp:180)
                        except (ssl.SSLError, OSError):
                            pass
                        break
                    feats, _rows = struct.unpack_from("<II", h, 16)
                    gen, seq = struct.unpack_from("<QQ", h, 40)
                    if h[:8] != b"EFHEW001" or h[10] != 1 or h[64:96] != PROFILE or h[96:128] != model["_sha256"] \
                            or feats != len(model["features"]):
                        raise ValueError("WIRE_HEADER")
                    if seq != expect or h[192:224] != session:
                        raise ValueError("WIRE_REPLAY_OR_CHANNEL")
                    if key0 is not None and (h[128:160] != key0 or gen != gen0):
                        raise ValueError("WIRE_KEY_GENERATION_CHANGE")
                    zero = bytearray(h)
                    zero[160:192], zero[224:256] = bytes(32), bytes(32)
                    if hashlib.sha256(bytes(zero)).digest() != h[160:192]:
                        raise ValueError("WIRE_REQUEST_ID")
                    body = recv_exact(s, feats * CT_BYTES)
                    zero = bytearray(h)
                    zero[224:256] = bytes(32)
                    if body is None or hashlib.sha256(bytes(zero) + body).digest() != h[224:256]:
                        raise ValueError("WIRE_REQUEST_DIGEST")
                    words = struct.unpack("<%dI" % (feats * CT_WORDS), body)
                    if any(v >= Q[(i // N) % 3] for i, v in enumerate(words)):
                        raise ValueError("WIRE_NONCANONICAL")
                    key0, gen0, expect = h[128:160], gen, expect + 1
                    t0 = time.perf_counter_ns()
                    out = cloud_affine(body, feats, model["weights_int"], model["bias_int"])
                    cloud_ns = time.perf_counter_ns() - t0
                    evaluated += 1
                    resp = bytearray(h)
                    resp[10] = 2
                    struct.pack_into("<Q", resp, 32, CT_BYTES)
                    send_chunked(s, bytes(resp))
                    send_chunked(s, out)
                    written += 1
                    log.line(json.dumps({"status": "CPP_E2E_RESPONSE_WRITTEN_NOT_OWNER_ACCEPTANCE", "sequence": seq,
                                         "request_sha256": h[224:256].hex(),
                                         "response_payload_sha256": hashlib.sha256(out).hexdigest(),
                                         "cloud_ns": cloud_ns}, separators=(",", ":")))
        except (ValueError, EOFError, ssl.SSLError, OSError) as e:
            rejected += 1
            log.line("CPP_E2E_SERVER_CONNECTION_STOP %s" % e)
    ls.close()
    log.line(json.dumps({"status": "CPP_E2E_SERVER_STOPPED", "evaluated": evaluated, "responses_written": written,
                         "rejected_connections": rejected, "owner_secrets_available": False, "automatic_retries": 0},
                        separators=(",", ":")))
    log.close()
    return 0 if not rejected else 2


# ------------------------------------------------------------------ client (one session per form) and owner records
def reference(model, x):
    e = model["bias"]
    for f in range(4):
        e += model["weights"][f] * ((x[f] - model["mean"][f]) / model["std"][f])
    return e


def level_of(logit, schema):
    lv = schema["levels"]
    for i, e in enumerate(lv["edges_logit"]):
        if abs(logit - e) <= lv["band_abs_logit"]:
            return None, [i + 1, i + 2]
    return 1 + sum(1 for e in lv["edges_logit"] if logit > e), None


def client_session(pki, host, port, model, small=False):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(pki.client_cert, pki.client_key)
    ctx.load_verify_locations(pki.client_ca)
    fd, own_log = tempfile.mkstemp(prefix="mock_client_keys_", suffix=".txt")
    os.close(fd)
    os.unlink(own_log)
    ctx.keylog_filename = own_log                 # private to this process: only to compute the TLS exporter
    server_pin = pki.read(pki.server_pin_file)
    features = len(model["features"])
    key = hashlib.sha256(os.urandom(32)).digest()  # stands in for SHA-256 of a fresh public key
    generation = SYS.getrandbits(64) | 1
    body = random_body(features, small)
    raw = socket.create_connection((host, port), timeout=30)
    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        with ctx.wrap_socket(raw, server_hostname=SERVER_NAME) as s:
            if pin_of(s) != server_pin:
                raise SystemExit("MOCK_SERVER_PIN_MISMATCH")
            session = exporter(last_exporter_master(own_log), LABEL, PROFILE, 32)
            h = build_request(features, generation, 1, model["_sha256"], key, session, body)
            send_chunked(s, h)
            send_chunked(s, body)
            rh = recv_exact(s, HDR)
            rb = recv_exact(s, CT_BYTES)
            if rh is None or rb is None:
                raise SystemExit("MOCK_NO_RESPONSE")
            expect = bytearray(h)
            expect[10] = 2
            struct.pack_into("<Q", expect, 32, CT_BYTES)
            if rh != bytes(expect):
                raise SystemExit("MOCK_RESPONSE_BINDING")
            s.unwrap()
    finally:
        try:
            os.unlink(own_log)
        except OSError:
            pass
    return dict(request_sha256=h[224:256].hex(), response_payload_sha256=hashlib.sha256(rb).hexdigest())


def client_main(pki_dir, host, port, out, forms, model_path, schema_path, small_index=None):
    pki, model = Pki(pki_dir), load_model(model_path)
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)
    presets = dict((p["id"], p) for p in schema["presets"])
    os.makedirs(os.path.join(out, "disclosure"), exist_ok=True)
    pub = open(os.path.join(out, "board_public.jsonl"), "a", encoding="utf-8", newline="\n")
    seq = [0]

    def emit(run, typ, data):
        seq[0] += 1
        pub.write(json.dumps({"v": 1, "seq": seq[0], "run": run, "type": typ, "class": "public",
                              "t_mono_ns": time.monotonic_ns(), "data": data}, sort_keys=True,
                             separators=(",", ":")) + "\n")
        pub.flush()
    results = []
    for n, pid in enumerate(forms, 1):
        form = dict(presets[pid]["form"])
        run = "mock-%02d-%s" % (n, pid)
        if presets[pid]["expected"]["refused"]:
            emit(run, "form.refused", {"code": presets[pid]["expected"]["refused"]})
            results.append(dict(run=run, preset=pid, refused=True))
            continue
        salt = os.urandom(32).hex()
        canonical = json.dumps(form, sort_keys=True, separators=(",", ":"))
        commitment = hashlib.sha256((COMMIT_TAG + "\n" + salt + "\n" + canonical + "\n").encode()).hexdigest()
        emit(run, "form.accepted", {"commitment": commitment})
        r = client_session(pki, host, port, model, small=(small_index == n))
        emit(run, "client.exited", {"status": "CPP_E2E_CPU_SPEC_NUMERIC_AND_DRAIN_PASS", "device_calls": 0,
                                    "trace": [r]})
        emit(run, "wiped", {})
        x = [float(form[k]) for k in ("GenHlth", "HighBP", "BMI", "Age")]
        plain = reference(model, x)
        ckks = plain + 1e-5                       # mock stand-in: there is no CKKS decryption in this test peer
        level, boundary = level_of(ckks, schema)
        disclosure = {"schema": "edgefhe.demo.disclosure.v1", "fictional": True, "mock": True, "run": run,
                      "preset": pid, "request_sha256": r["request_sha256"],
                      "response_payload_sha256": r["response_payload_sha256"], "form": form,
                      "canonical_json": canonical, "salt_hex": salt, "commitment": commitment, "logit_plain": plain,
                      "logit_ckks": ckks, "logit_ckks_source": "mock: plaintext reference plus 1e-5", "level": level,
                      "boundary": boundary}
        with open(os.path.join(out, "disclosure", r["request_sha256"] + ".json"), "w", encoding="utf-8",
                  newline="\n") as f:
            json.dump(disclosure, f, sort_keys=True, indent=1)
            f.write("\n")
        results.append(dict(run=run, preset=pid, small=small_index == n, **r))
        print("MOCK_CLIENT_FORM preset=%s request_sha256=%s response_payload_sha256=%s small=%s" % (
            pid, r["request_sha256"], r["response_payload_sha256"], small_index == n), flush=True)
    pub.close()
    return results


def run_both(pki_dir, port, out, forms, model_path, schema_path, small_index=None, host="127.0.0.1"):
    """Server thread and client sessions in one process; writes tls_keys.log and server.log into `out` too."""
    with open(schema_path, encoding="utf-8") as f:
        refused = set(p["id"] for p in json.load(f)["presets"] if p["expected"]["refused"])
    connections = sum(1 for p in forms if p not in refused)
    ready = threading.Event()
    result = {}

    def serve():
        result["server"] = server_main(pki_dir, port, os.path.join(out, "tls_keys.log"),
                                       os.path.join(out, "server.log"), connections, model_path, host, ready)
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    if not ready.wait(15):
        raise SystemExit("MOCK_SERVER_NOT_READY")
    res = client_main(pki_dir, host, port, out, forms, model_path, schema_path, small_index)
    t.join(60)
    return res, result.get("server")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):        # UTF-8 output even through a pipe on a Windows code page
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=("server", "client", "run"))
    ap.add_argument("--pki", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18443)
    ap.add_argument("--keylog")
    ap.add_argument("--log")
    ap.add_argument("--connections", type=int, default=1)
    ap.add_argument("--out")
    ap.add_argument("--forms", default="M,M,K")
    ap.add_argument("--small", type=int, help="1-based index of the form sent as a negative control")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--form-schema")
    a = ap.parse_args(argv)
    schema = a.form_schema or os.path.join(os.path.dirname(os.path.abspath(a.model)), "form_cdc.json")
    forms = [x.strip() for x in a.forms.split(",") if x.strip()]
    if a.mode == "server":
        if not (a.keylog and a.log):
            ap.error("server needs --keylog and --log")
        return server_main(a.pki, a.port, a.keylog, a.log, a.connections, a.model, a.host)
    if not a.out:
        ap.error("--out is required")
    os.makedirs(a.out, exist_ok=True)
    if a.mode == "client":
        client_main(a.pki, a.host, a.port, a.out, forms, a.model, schema, a.small)
        print("MOCK_DONE")
        return 0
    _res, rc = run_both(a.pki, a.port, a.out, forms, a.model, schema, a.small, a.host)
    print("MOCK_DONE server_rc=%s" % rc)
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
