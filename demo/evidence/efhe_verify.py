"""EdgeFHE demo evidence verifier: gates G0-G11 over a sealed capture, the TLS key log and the public logs.

A third party can run this on an evidence bundle (capture.pcapng, tls_keys.log, server.log, the board public log
and the fictional disclosures) and check, with tshark and the Python standard library only:

  G0  capture integrity: sealed hash, zero dropped packets, no TCP gaps
  G1  flow inventory and record-size signature without keys (the eavesdropper view)
  G2  TLS 1.3: version, suite, no PSK or early data, SNI, client authentication, leaf pins, no AEAD failure
  G3  every decrypted application byte is an EFHEW001 frame that passes wire.hpp:24-34
  G4  request_id, request_hash, response binding and session_id == TLS exporter (tls.hpp:114-117)
  G5  the response equals the public affine function of the request bit for bit (cloud.cpp:24-43)
  G6  canonical residues, uniform request limbs, no repeated 16 KiB block, fresh key_id and generation
  G7  no encoding of a form value: >= 8-byte encodings everywhere, 4-byte encodings as aligned words
  G8  no client application data after the response (close_notify only)
  G9  capture == server.log == board public log; the salted form commitment opens
  G10 |logit_ckks - logit_plain| <= 2e-3 and a consistent risk level (fictional disclosures only)
  G11 optional observer capture: identical per-connection TCP payload hashes

Gate status: PASS, FAIL, FAIL-PENDING (G7: a 4-byte chance hit not yet confirmed by a resubmission; G0: efhe_seal.py
recorded that the key log was used before the capture was sealed), INCOMPLETE (an input is missing), BLOCKED
(decryption failed, so the gate cannot run), SKIP (optional, not supplied).
The verdict lists the gates of the worst status present, for example FAIL:G6 or PASS.

Run directory (verify_run finds these names; the CLI options override them):
  capture.pcapng      the keyless capture (pcapng from dumpcap, or the classic pcap that tcpdump writes)
  capture.sha256      seal record '<sha256>  capture.pcapng' (efhe_seal.py writes it before the key log is used)
  seal_order.json     written by efhe_seal.py only when capture_dsb.pcapng already existed before the seal record,
                      i.e. the key log was used before the capture was sealed (gate G0 check seal_order: FAIL-PENDING)
  capture_stats.txt   dumpcap or tcpdump statistics text (dropped packets), when the capture has no pcapng
                      interface statistics block
  tls_keys.log        NSS key log; server.pin and client.pin (or pins under ca/, cloud/, board/), lowercase hex
  server.log          efnet_server output with a '<epoch seconds> ' prefix per line (run_server.sh)
  board_public.jsonl  board public log: JSON lines; per run the 'commitment' and the 'request_sha256' and
                      'response_payload_sha256' hashes, flat or nested (for example data.trace[])
  disclosure/         fictional disclosures, one JSON object each: request_sha256, form {GenHlth, HighBP, BMI, Age},
                      salt_hex, canonical_json (the exact string committed), commitment, logit_ckks, logit_plain,
                      level, boundary, fictional; commitment = SHA-256('EDGEFHE_FORM_COMMIT_V1\\n' + salt_hex + '\\n' +
                      canonical_json + '\\n')
  observer.pcapng     optional observer capture (G11)
  rehearsal_summary.txt  written by the PC rehearsals (demo/cloud/rehearse_loopback.sh, demo/sim/rehearse_form.sh)
The report field pc_rehearsal says whether the board side ran on a PC (rehearsal_summary.txt is present, or a board
address is a loopback address or equals the cloud address); report.html then labels the run 'PC 演練（非 KV260 板上
實測）', the same rule as the console's PC 演練 chip. It never changes a gate.
G7 detail: long patterns with four or more zero bytes (int64 or float64 of small values) are searched at every
4-byte-aligned offset of the decrypted streams instead of at every offset, because zero runs at frame boundaries (for
example the all-zero body of the negative-control probe) match them by structure; every other long pattern is searched
at every offset of the raw TCP payload and of the decrypted streams.

usage:
  python -B efhe_verify.py --run RUN_DIR [--out RUN_DIR/verify_report.json]
  python -B efhe_verify.py --capture CAP --keylog KEYLOG [--server-log F] [--board-public F] [--disclosure P]
         [--model demo/model/cdc-diabetes-lr4-v1.json] [--pins DIR|FILE] [--observer CAP] [--out F]
  python -B efhe_verify.py --selftest        (synthetic frames; needs neither tshark nor captures)

Library: verify_run(run_dir, opts) -> report dict; quick_session(capture, keylog, board_port) -> dict.
Python 3.10 or later; standard library only (tls13_native.py adds an optional decryption cross-check when the
'cryptography' package is installed). Research demonstration only; not a medical diagnosis.
"""
import argparse
import base64
import bisect
import hashlib
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import tls13_native as native  # noqa: E402

SCHEMA = "edgefhe.demo.verify-report.v1"
PORT = 18443
N = 4096
CT_WORDS = 6 * N
CT_BYTES = 4 * CT_WORDS
LIMB_BYTES = 4 * N
HDR = 256
Q = (998244353, 1004535809, 469762049)
PROFILE_HEX = "c01b69cf3da9fb260dbd4ac15080bd6c1c425d90bfb9015c8bacc16c061fa05c"
PROFILE = bytes.fromhex(PROFILE_HEX)
EXPORTER_LABEL = b"EXPERIMENTAL-EdgeFHE-Wire-v1"          # tls.hpp:115
SNI = "edgefhe-cloud"
TLS_OVERHEAD = 17                                         # inner content type + 16-byte AEAD tag (no padding)
REC_HEADER = HDR + TLS_OVERHEAD                           # 273: the frame header is always its own record
REC_LIMB = LIMB_BYTES + TLS_OVERHEAD                      # 16,401: one RNS limb per 16,384-byte write (tls.hpp:120)
REC_CLOSE = 2 + TLS_OVERHEAD                              # 19: close_notify
MAX_ABS_ERROR = 2e-3                                      # client.cpp:268, form_cdc.json verification
COMMIT_TAG = "EDGEFHE_FORM_COMMIT_V1"
ALPHA = 1e-7                                              # per-test false-alarm rate for one 4096-word limb
Z_CRIT = 5.33                                             # two-sided normal tail 1e-7
KS_CRIT = math.sqrt(math.log(2.0 / ALPHA) / 2.0)          # asymptotic Kolmogorov bound, about 2.90 / sqrt(n)
SMALL = 1 << 22                                           # |coefficient| bound of an unencrypted slot encoding
GATES = ("G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10", "G11")
REQUIRED = GATES[:11]
STATUS_ORDER = ("FAIL", "FAIL-PENDING", "BLOCKED", "INCOMPLETE", "PASS", "SKIP")
TITLES = {
    "G0": "擷取完整性：封存雜湊與封存順序、零遺失封包、TCP 無缺口",
    "G1": "流量清單與紀錄大小特徵（不使用金鑰）",
    "G2": "TLS 1.3 交握、雙向憑證驗證與釘選",
    "G3": "應用資料全為 EFHEW001 訊框",
    "G4": "請求雜湊與 TLS 通道綁定",
    "G5": "雲端運算可由公開模型逐位元重算",
    "G6": "密文統計：餘數典型、分布均勻、無重複區塊、金鑰與世代皆為新值",
    "G7": "表單明文未出現在任何位元組中",
    "G8": "回應之後用戶端未傳送任何應用資料",
    "G9": "擷取、雲端紀錄與板端公開紀錄三方一致",
    "G10": "CKKS 結果與明文參考值一致，風險等級一致",
    "G11": "獨立觀察者擷取一致（選用）",
}
# Public catalogue (model_catalog.hpp plus the overlay line demo/model/catalog_line.hpp):
# id, wire model_id (source SHA-256), features, weights_int, bias_int. test_verify_unit.py checks it against the files.
CATALOGUE = (
    ("ai4i-failure-logreg-v1", "de0499f25657351b6a1ca2460181c328a05fd43147a0f01aa51f10f89b9dc2a4", 4,
     (-963760162, 1743001042, 2378837386, 987640795), -1625303743450180864),
    ("diabetes-progression-ridge-v1", "727f278c3d58c26466adef1885342c63940a796a970eeae4d6368508e67ef611", 10,
     (-50575981, -155486282, 384645674, 144734787, -413353323, 234384217, 43799762, 94049252, 469988971, 102815026),
     4781),
    ("diabetes-symptom-logreg-v1", "6395cb10c6041f22490e801b341d0eda9ff090dec4ebf2d69e2c665aa111356d", 16,
     (-483683090, 1852163189, 2112091133, 2226339336, 213877902, 222258180, 224306039, 876221047, 469955756,
      -1614097414, 1003255237, -109939664, 381432090, -58474517, 102153859, -261003212), 2369448461902863872),
    ("cdc-diabetes-lr4-v1", "63cee36f9ac68b8e274dc45b4fa2fee0aeae5c4089b3d18fe51d7cf0c588c106", 4,
     (749408897, 478576138, 543091430, 592544664), -666458326697820288),
)
FORM_MODEL_HEX = CATALOGUE[3][1]
FEATURES = ("GenHlth", "HighBP", "BMI", "Age")
HEADER_FIELDS = ((0, 8, "magic"), (8, 1, "version"), (9, 1, "reserved9"), (10, 1, "kind"), (11, 1, "reserved11"),
                 (12, 4, "header_bytes"), (16, 4, "features"), (20, 4, "rows"), (24, 4, "layout"),
                 (28, 4, "reserved28"), (32, 8, "payload_bytes"), (40, 8, "generation"), (48, 8, "sequence"),
                 (56, 8, "reserved56"), (64, 32, "profile_id"), (96, 32, "model_id"), (128, 32, "key_id"),
                 (160, 32, "request_id"), (192, 32, "session_id"), (224, 32, "request_hash"))
UNEXPLAINED_FIELDS = ("generation", "key_id")             # random values that no other gate recomputes
TSHARK_WINDOWS = r"C:\Program Files\Wireshark\tshark.exe"


class VerifyError(Exception):
    """An input cannot be processed (missing tool, unreadable file)."""


# ------------------------------------------------------------------ small helpers
def rnd(x, digits=6):
    if x is None or isinstance(x, (bool, int)) or not isinstance(x, float):
        return x
    if math.isnan(x) or math.isinf(x):
        return str(x)
    return round(x, digits)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def is_hex64(v):
    return isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) is not None


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def worst(statuses):
    present = [s for s in statuses if s]
    for s in STATUS_ORDER:
        if s in present:
            return s
    return "PASS"


def gate(status, checks, **details):
    return dict(status=status, checks=checks, details=details)


def check(name, status, **info):
    d = dict(name=name, status=status)
    d.update(info)
    return d


def combine(checks, empty="PASS"):
    if not checks:
        return empty
    return worst([c["status"] for c in checks])


def model_by_id(hex_id):
    for m in CATALOGUE:
        if m[1] == hex_id:
            return m
    return None


# ------------------------------------------------------------------ wire.hpp
def encode_header(kind, features, rows, nbytes, generation, sequence, digests):
    """wire.hpp:16-22 encode(); digests = (profile, model, key, request_id, session, request_hash)."""
    b = bytearray(HDR)
    b[0:8] = b"EFHEW001"
    b[8] = 1
    b[10] = kind
    struct.pack_into("<5I", b, 12, HDR, features, rows, 1, 0)
    struct.pack_into("<4Q", b, 32, nbytes, generation, sequence, 0)
    for i, d in enumerate(digests):
        b[64 + 32 * i:96 + 32 * i] = d
    return bytes(b)


def parse_header(h):
    """wire.hpp:24-34 decode(): (fields or None, error code or None), checks in the same order."""
    if len(h) != HDR:
        return None, "WIRE_HEADER_LENGTH"
    if h[:8] != b"EFHEW001" or h[8] != 1 or h[9] != 0:
        return None, "WIRE_MAGIC_VERSION"
    if h[10] not in (1, 2) or h[11] != 0 or u32(h, 12) != HDR or u32(h, 24) != 1 or u32(h, 28) != 0 or u64(h, 56):
        return None, "WIRE_KIND_LAYOUT_RESERVED"
    f = dict(kind=h[10], features=u32(h, 16), rows=u32(h, 20), payload_bytes=u64(h, 32), generation=u64(h, 40),
             sequence=u64(h, 48), profile_id=h[64:96].hex(), model_id=h[96:128].hex(), key_id=h[128:160].hex(),
             request_id=h[160:192].hex(), session_id=h[192:224].hex(), request_hash=h[224:256].hex())
    if not (1 <= f["features"] <= 32 and 1 <= f["rows"] <= 2048 and f["generation"] and 1 <= f["sequence"] <= 1000000):
        return f, "WIRE_DIMENSION_GENERATION_SEQUENCE"
    if f["payload_bytes"] != (f["features"] * CT_BYTES if f["kind"] == 1 else CT_BYTES):
        return f, "WIRE_PAYLOAD_LENGTH"
    zero = "0" * 64
    if f["profile_id"] != PROFILE_HEX or zero in (f["key_id"], f["request_id"], f["session_id"], f["request_hash"]):
        return f, "WIRE_PROFILE_OR_EMPTY_ID"
    m = model_by_id(f["model_id"])
    if m is None:
        return f, "MODEL_SOURCE_ID_UNKNOWN"
    if f["features"] != m[2]:
        return f, "WIRE_MODEL_DIMENSIONS"
    return f, None


def request_id_of(h):
    """wire.hpp:35: SHA-256 of the header with request_id and request_hash zeroed, kind 1 and the request size."""
    b = bytearray(h)
    b[160:192] = bytes(32)
    b[224:256] = bytes(32)
    b[10] = 1
    struct.pack_into("<Q", b, 32, u32(h, 16) * CT_BYTES)
    return hashlib.sha256(bytes(b)).hexdigest()


def request_hash_of(h, body):
    """wire.hpp:36 / span_wire.hpp:12: SHA-256 of the header with request_hash zeroed, then the payload."""
    b = bytearray(h)
    b[224:256] = bytes(32)
    return hashlib.sha256(bytes(b) + body).hexdigest()


def split_frames(stream, kind):
    """Split a decrypted application stream into frames. Returns (frames, problems)."""
    frames, problems, off = [], [], 0
    while off < len(stream):
        h = stream[off:off + HDR]
        if len(h) < HDR:
            problems.append(dict(code="WIRE_TRUNCATED_HEADER", offset=off, bytes=len(stream) - off))
            break
        if h[:8] != b"EFHEW001":
            problems.append(dict(code="WIRE_UNACCOUNTED_BYTES", offset=off, bytes=len(stream) - off))
            break
        fields, err = parse_header(h)
        nbytes = u64(h, 32)
        if fields is None or nbytes > 32 * CT_BYTES:
            problems.append(dict(code=err or "WIRE_PAYLOAD_LENGTH", offset=off))
            break
        if fields["kind"] != kind:
            problems.append(dict(code="WIRE_KIND_DIRECTION", offset=off, kind=fields["kind"]))
        body = stream[off + HDR:off + HDR + nbytes]
        fr = dict(offset=off, header=h, body=body, fields=fields, error=err, complete=len(body) == nbytes,
                  end=off + HDR + len(body))
        frames.append(fr)
        if err:
            problems.append(dict(code=err, offset=off))
        if not fr["complete"]:
            problems.append(dict(code="WIRE_TRUNCATED_FRAME", offset=off, missing=nbytes - len(body)))
            break
        off += HDR + nbytes
    return frames, problems


# ------------------------------------------------------------------ ciphertext arithmetic and statistics
def words_of(body):
    return struct.unpack("<%dI" % (len(body) // 4), body[:len(body) // 4 * 4])


def limb_of(word_index):
    return (word_index // N) % 3


def noncanonical(words):
    """Positions of words >= q of their limb (wire.hpp:37-41: limb = (i / N) % 3)."""
    return [i for i, v in enumerate(words) if v >= Q[(i // N) % 3]]


def affine(body, features, weights, bias):
    """cloud.cpp:24-43: per limb l, c_p[i] = sum_f x_f,p[i] * (w_f mod q_l) mod q_l; c0[0] += bias mod q_l."""
    x = words_of(body)
    out = [0] * CT_WORDS
    for l, q in enumerate(Q):
        w = [wi % q for wi in weights]
        for p in range(2):
            base = p * 3 * N + l * N
            acc = [0] * N
            for f in range(features):
                col = x[f * CT_WORDS + base:f * CT_WORDS + base + N]
                wf = w[f]
                acc = [a + v * wf for a, v in zip(acc, col)]
            out[base:base + N] = [a % q for a in acc]
        out[l * N] = (out[l * N] + bias % q) % q
    return struct.pack("<%dI" % CT_WORDS, *out)


def gammaincc(a, x):
    """Regularized upper incomplete gamma Q(a, x) (series or continued fraction)."""
    if x <= 0:
        return 1.0
    lg = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1:
        ap, term, total = a, 1.0 / a, 1.0 / a
        for _ in range(10000):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-16:
                break
        return max(0.0, 1.0 - total * math.exp(lg))
    tiny = 1e-300
    b = x + 1 - a
    c, d = 1 / tiny, 1 / b
    h = d
    for i in range(1, 10000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-16:
            break
    return min(1.0, math.exp(lg) * h)


def limb_stats(vals, q):
    """Uniformity tests of one 4096-word limb against U[0, q): chi-square (64 bins), KS, mean, small residues."""
    n = len(vals)
    bins = [0] * 64
    for v in vals:
        bins[min(v * 64 // q, 63)] += 1
    e = n / 64.0
    chi = sum((b - e) ** 2 for b in bins) / e
    p_chi = gammaincc(31.5, chi / 2.0)
    us = sorted(min(v, q - 1) / q for v in vals)
    ks = max(max((i + 1) / n - u, u - i / n) for i, u in enumerate(us))
    mean_z = (sum(vals) / n / q - 0.5) / math.sqrt(1.0 / 12.0 / n)
    small = sum(1 for v in vals if v < SMALL or q - v <= SMALL) / n
    ps = 2.0 * SMALL / q
    small_bound = ps + 6 * math.sqrt(ps * (1 - ps) / n)
    zeros = sum(1 for v in vals if v == 0)
    canonical = max(vals) < q
    reason = None
    if not canonical:
        reason = "noncanonical"
    elif zeros == n:
        reason = "all_zero"
    elif p_chi <= ALPHA:
        reason = "chi2"
    elif ks * math.sqrt(n) >= KS_CRIT:
        reason = "ks"
    elif abs(mean_z) >= Z_CRIT:
        reason = "mean"
    elif small > small_bound:
        reason = "small_residues"
    return dict(canonical=canonical, chi2=rnd(chi, 3), chi2_p=float("%.6g" % p_chi), ks_sqrt_n=rnd(ks * math.sqrt(n), 4),
                mean_z=rnd(mean_z, 3), small_frac=rnd(small, 5), small_bound=rnd(small_bound, 5), zeros=zeros,
                uniform=reason is None, reason=reason)


def body_stats(body, nct):
    w = words_of(body)
    out = []
    for c in range(nct):
        for poly in range(2):
            for l, q in enumerate(Q):
                base = c * CT_WORDS + poly * 3 * N + l * N
                st = limb_stats(w[base:base + N], q)
                st.update(ct=c, poly=poly, limb=l)
                out.append(st)
    return out


def histogram(body, bins=32):
    w = words_of(body)
    h = [0] * bins
    for i, v in enumerate(w):
        q = Q[(i // N) % 3]
        h[min(v * bins // q, bins - 1)] += 1
    return h


def limb_digests(body):
    return [hashlib.sha256(body[o:o + LIMB_BYTES]).hexdigest() for o in range(0, len(body) - LIMB_BYTES + 1, LIMB_BYTES)]


def locate_word(word_index):
    c, r = divmod(word_index, CT_WORDS)
    poly, r = divmod(r, 3 * N)
    limb, coeff = divmod(r, N)
    return dict(ct=c, poly=poly, limb=limb, coeff=coeff)


# ------------------------------------------------------------------ plaintext patterns (G7)
class Patterns:
    """Long patterns (>= 8 bytes, searched everywhere) and 4-byte word patterns (aligned words in the bodies)."""

    def __init__(self):
        self.long = {}
        self.word = {}

    def add_long(self, label, b):
        if len(b) >= 8 and any(b) and label not in self.long:
            self.long[label] = bytes(b)

    def add_word(self, label, b):
        if len(b) == 4 and any(b) and label not in self.word:
            self.word[label] = bytes(b)

    def prefix_index(self, sparse=None):
        """Index of the long patterns by their first 8 bytes. sparse=None: all; False: dense only; True: sparse only.
        A sparse pattern has four or more zero bytes (int64 or float64 of a small value): it is searched only at
        4-byte-aligned positions, because zero runs at frame boundaries would otherwise match it by structure."""
        idx = {}
        for label, p in self.long.items():
            if sparse is None or is_sparse(p) == sparse:
                idx.setdefault(p[:8], []).append((label, p))
        return idx

    def word_index(self):
        idx = {}
        for label, p in self.word.items():
            idx.setdefault(struct.unpack("<I", p)[0], []).append(label)
        return idx


def _num_texts(name, v):
    texts = {"%g" % v, "%.1f" % v, "%.17g" % v}
    if float(v).is_integer():
        texts.add("%d" % int(v))
    return sorted(texts)


def add_value_patterns(pats, name, v, mean=None, std=None):
    v = float(v)
    integral = v.is_integer()
    for e, tag in (("<", "LE"), (">", "BE")):
        pats.add_long("%s=%s float64 %s" % (name, "%g" % v, tag), struct.pack(e + "d", v))
        pats.add_word("%s=%s float32 %s" % (name, "%g" % v, tag), struct.pack(e + "f", v))
        if integral:
            pats.add_long("%s=%d int64 %s" % (name, int(v), tag), struct.pack(e + "q", int(v)))
            pats.add_word("%s=%d int32 %s" % (name, int(v), tag), struct.pack(e + "i", int(v)))
    for t in _num_texts(name, v):
        for tmpl in ('"%s":%s', '"%s": %s', "%s=%s", "%s: %s", "%s,%s"):
            s = tmpl % (name, t)
            pats.add_long("%s text %r" % (name, s), s.encode("ascii"))
            pats.add_long("%s utf16 %r" % (name, s), s.encode("utf-16-le"))
        if len(t) >= 8:
            pats.add_long("%s text %r" % (name, t), t.encode("ascii"))
        pats.add_long("%s utf16 %r" % (name, t), t.encode("utf-16-le"))
    if mean is not None and std:
        z = (v - mean) / std
        k = int(round(z * (1 << 30)))
        for e, tag in (("<", "LE"), (">", "BE")):
            pats.add_long("%s=%g z float64 %s" % (name, v, tag), struct.pack(e + "d", z))
            pats.add_word("%s=%g z float32 %s" % (name, v, tag), struct.pack(e + "f", z))
            if k:
                pats.add_long("%s=%g round(z*2^30) int64 %s" % (name, v, tag), struct.pack(e + "q", k))
        if k:
            for l, q in enumerate(Q):
                pats.add_word("%s=%g round(z*2^30) mod q%d" % (name, v, l), struct.pack("<I", k % q))


def domain_values(schema):
    """Every value the owner form can send, from form_cdc.json (or the documented defaults)."""
    out = {}
    fields = (schema or {}).get("fields") or []
    for fdef in fields:
        name = fdef.get("name")
        if fdef.get("kind") == "choice":
            out[name] = sorted(float(o["value"]) for o in fdef.get("options", []))
        elif fdef.get("kind") == "number":
            lo, hi, dec = float(fdef["minimum"]), float(fdef["maximum"]), int(fdef.get("decimals", 1))
            step = 10.0 ** -dec
            out[name] = [round(lo + i * step, dec) for i in range(int(round((hi - lo) / step)) + 1)]
    if not out:
        out = {"GenHlth": [1.0, 2.0, 3.0, 4.0, 5.0], "HighBP": [0.0, 1.0],
               "BMI": [round(12.0 + i * 0.1, 1) for i in range(691)], "Age": [float(a) for a in range(1, 14)]}
    return out


def domain_patterns(model, schema):
    pats = Patterns()
    header = ",".join(FEATURES)
    pats.add_long("CSV header text", header.encode("ascii"))
    pats.add_long("CSV header utf16", header.encode("utf-16-le"))
    for name in FEATURES:
        pats.add_long("name quoted %s" % name, ('"%s"' % name).encode("ascii"))
        pats.add_long("name utf16 %s" % name, name.encode("utf-16-le"))
    vals = domain_values(schema)
    for f, name in enumerate(FEATURES):
        for v in vals.get(name, []):
            add_value_patterns(pats, name, v, model["mean"][f] if model else None, model["std"][f] if model else None)
    pats.word = {}                       # 4-byte words of the whole domain would give chance hits; disclosed only
    return pats


def form_patterns(model, form, logits=()):
    """Patterns of one disclosed (fictional) form: field values, records, z, logits and their CKKS residues."""
    pats = Patterns()
    x = [float(form[n]) for n in FEATURES]
    for f, name in enumerate(FEATURES):
        add_value_patterns(pats, name, x[f], model["mean"][f] if model else None, model["std"][f] if model else None)
    row_texts = set()
    for fmt in ("%g", "%.1f"):
        for sep in (",", ", ", ";", "\t", " "):
            row_texts.add(sep.join(("%d" % v) if (v.is_integer() and fmt == "%g") else (fmt % v) for v in x))
    row_texts.add("%d,%d,%s,%d" % (x[0], x[1], format(x[2], ".1f"), x[3]))      # the gateway CSV row
    for t in sorted(row_texts):
        pats.add_long("record text %r" % t, t.encode("ascii"))
        pats.add_long("record utf16 %r" % t, t.encode("utf-16-le"))
    for e, tag in (("<", "LE"), (">", "BE")):
        pats.add_long("record float64 %s" % tag, struct.pack(e + "4d", *x))
        pats.add_long("record float32 %s" % tag, struct.pack(e + "4f", *x))
        if all(v.is_integer() for v in x):
            pats.add_long("record int32 %s" % tag, struct.pack(e + "4i", *[int(v) for v in x]))
    if model:
        z = [(x[f] - model["mean"][f]) / model["std"][f] for f in range(4)]
        pats.add_long("record z float64 LE", struct.pack("<4d", *z))
        pats.add_long("record z float32 LE", struct.pack("<4f", *z))
    for label, v in logits:
        if v is None:
            continue
        v = float(v)
        prob = 1.0 / (1.0 + math.exp(-v)) if v > -700 else 0.0
        for name, val in ((label, v), (label + " sigmoid", prob)):
            for e, tag in (("<", "LE"), (">", "BE")):
                pats.add_long("%s float64 %s" % (name, tag), struct.pack(e + "d", val))
                pats.add_word("%s float32 %s" % (name, tag), struct.pack(e + "f", val))
            for fmt in ("%.6f", "%.4f", "%.17g"):
                t = fmt % val
                pats.add_long("%s text %s" % (name, fmt), t.encode("ascii"))
                pats.add_long("%s utf16 %s" % (name, fmt), t.encode("utf-16-le"))
        k = int(round(v * (1 << 60)))
        for e, tag in (("<", "LE"), (">", "BE")):
            pats.add_long("%s round(x*2^60) int64 %s" % (label, tag), struct.pack(e + "q", k))
        for l, q in enumerate(Q):
            pats.add_word("%s round(x*2^60) mod q%d" % (label, l), struct.pack("<I", k % q))
    return pats


def is_sparse(p):
    return p.count(0) >= 4


def search_long(blob, index, step=1):
    """[(label, offset)] of every long-pattern occurrence (prefix index of the first 8 bytes) at offsets that are
    multiples of `step` (1: every offset; 4: aligned words of the decrypted frames, which start at multiples of 4)."""
    hits = []
    get = index.get
    for i in range(0, len(blob) - 7, step):
        cands = get(blob[i:i + 8])
        if cands:
            for label, p in cands:
                if blob[i:i + len(p)] == p:
                    hits.append((label, i))
    return hits


def search_decrypted(stream, pats_dense, pats_sparse):
    return search_long(stream, pats_dense) + search_long(stream, pats_sparse, 4)


def search_words(body, index):
    """[(label, word_index)] of 4-byte patterns at aligned word positions of a body."""
    hits = []
    get = index.get
    for i, v in enumerate(words_of(body)):
        labels = get(v)
        if labels:
            for label in labels:
                hits.append((label, i))
    return hits


def word_chance(pats, nwords_per_limb):
    """Expected number of chance hits: sum over 4-byte patterns and limbs of words_in_limb / q (value < q only)."""
    total = 0.0
    for p in pats.word.values():
        v = struct.unpack("<I", p)[0]
        for l, q in enumerate(Q):
            if v < q:
                total += nwords_per_limb[l] / q
    return total


def header_field_at(rel):
    for o, n, name in HEADER_FIELDS:
        if o <= rel < o + n:
            return name
    return None


def locate_stream(frames, pos, length, sparse=False):
    """Describe where a hit lies in a decrypted stream. explained=True if every byte lies in the public header; a dense
    pattern inside the random key_id or generation fields stays unexplained (a chance of about 2^-64), while a sparse
    one there is the field's own small value (efnet_probe sends generation 1, probe.cpp:11)."""
    for fr in frames:
        if fr["offset"] <= pos < fr["end"]:
            rel = pos - fr["offset"]
            seq = fr["fields"]["sequence"] if fr["fields"] else None
            if rel < HDR:
                names = sorted(set(header_field_at(r) for r in range(rel, min(rel + length, HDR))))
                inside = rel + length <= HDR
                explained = inside and (sparse or not any(n in UNEXPLAINED_FIELDS for n in names))
                return dict(where="header", fields=names, sequence=seq, explained=explained)
            loc = locate_word((rel - HDR) // 4)
            loc.update(where="body", sequence=seq, byte=(rel - HDR) % 4, explained=False)
            return loc
    return dict(where="outside_frames", explained=False)


# ------------------------------------------------------------------ logs, disclosures, commitment, levels
EPOCH_PREFIX = re.compile(r"^(\d{9,11}(?:\.\d+)?)\s(.*)$")


def parse_server_log(path):
    """server.log lines '<epoch> <text>' (run_server.sh prefixes date +%s.%N); the JSON status lines of efnet_server."""
    entries = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            m = EPOCH_PREFIX.match(line)
            epoch, text = (float(m.group(1)), m.group(2)) if m else (None, line)
            obj = None
            if text.startswith("{"):
                try:
                    obj = json.loads(text)
                except ValueError:
                    obj = None
            entries.append(dict(epoch=epoch, text=text, json=obj))
    out = dict(lines=len(entries), responses=[], ready=False, stopped=None, connection_stops=[], keylog_warning=False,
               prefixed=sum(1 for e in entries if e["epoch"] is not None))
    for e in entries:
        j = e["json"]
        if isinstance(j, dict):
            st = j.get("status")
            if st == "CPP_E2E_SERVER_READY":
                out["ready"] = True
            elif st == "CPP_E2E_RESPONSE_WRITTEN_NOT_OWNER_ACCEPTANCE":
                out["responses"].append(dict(epoch=e["epoch"], sequence=j.get("sequence"),
                                             request_sha256=j.get("request_sha256"),
                                             response_payload_sha256=j.get("response_payload_sha256"),
                                             cloud_ns=j.get("cloud_ns")))
            elif st == "CPP_E2E_SERVER_STOPPED":
                out["stopped"] = {k: j.get(k) for k in ("evaluated", "responses_written", "rejected_connections",
                                                         "owner_secrets_available", "automatic_retries")}
        elif e["text"].startswith("CPP_E2E_SERVER_CONNECTION_STOP"):
            out["connection_stops"].append(dict(epoch=e["epoch"], code=e["text"].split(" ", 1)[-1].strip()))
        elif e["text"].startswith("WARNING_DEMO_TLS_KEYLOG_ENABLED"):
            out["keylog_warning"] = True
    return out


def _walk(obj, found, path=()):
    if isinstance(obj, dict):
        for k in sorted(obj):
            _walk(obj[k], found, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk(v, found, path + (i,))
    else:
        found.append((path, obj))


def parse_board_public(path):
    """Board public JSONL (gateway whitelist): group request/response hashes and commitments by run.
    Accepts flat rows or event envelopes {v, seq, run, type, class, t_mono_ns, data}; hashes may be nested."""
    runs, rows, bad = {}, 0, 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                bad += 1
                continue
            if not isinstance(obj, dict):
                bad += 1
                continue
            rows += 1
            run = obj.get("run")
            if run is None and isinstance(obj.get("data"), dict):
                run = obj["data"].get("run")
            run = str(run) if run is not None else "_row%d" % rows
            r = runs.setdefault(run, dict(run=run, commitments=set(), pairs=set(), requests=set(), responses=set(),
                                          events=[]))
            r["events"].append(dict(type=obj.get("type"), t_mono_ns=obj.get("t_mono_ns")))
            found = []
            _walk(obj, found)
            groups = {}
            for p, v in found:
                if not p:
                    continue
                key = p[-1]
                if key in ("commitment", "commitment_sha256", "form_commitment") and is_hex64(v):
                    r["commitments"].add(v)
                elif key in ("request_sha256", "response_payload_sha256") and is_hex64(v):
                    groups.setdefault(p[:-1], {})[key] = v
            for g in groups.values():
                if "request_sha256" in g:
                    r["requests"].add(g["request_sha256"])
                if "response_payload_sha256" in g:
                    r["responses"].add(g["response_payload_sha256"])
                if len(g) == 2:
                    r["pairs"].add((g["request_sha256"], g["response_payload_sha256"]))
    return dict(rows=rows, unparsed=bad, runs=runs)


def _find(obj, names):
    """First value for any key in `names`, breadth-first through nested dicts."""
    queue = [obj]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            for n in names:
                if n in cur:
                    return cur[n]
            queue.extend(cur[k] for k in sorted(cur) if isinstance(cur[k], (dict, list)))
        elif isinstance(cur, list):
            queue.extend(v for v in cur if isinstance(v, (dict, list)))
    return None


def parse_disclosures(path):
    """Fictional disclosures written by 'edgefhe_gateway.py disclose': a JSON file, a JSONL file or a directory."""
    files = []
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.lower().endswith((".json", ".jsonl")):
                files.append(os.path.join(path, name))
    elif os.path.isfile(path):
        files.append(path)
    items, problems = [], []
    for fp in files:
        with open(fp, encoding="utf-8", errors="replace") as f:
            text = f.read()
        objs = []
        try:
            v = json.loads(text)
            objs = v if isinstance(v, list) else [v]
        except ValueError:
            for line in text.splitlines():
                line = line.strip()
                if line:
                    try:
                        objs.append(json.loads(line))
                    except ValueError:
                        problems.append(dict(file=os.path.basename(fp), problem="unparsed line"))
        for o in objs:
            if not isinstance(o, dict):
                continue
            form = _find(o, ("form", "x", "answers"))
            if isinstance(form, list) and len(form) == 4:
                form = dict(zip(FEATURES, form))
            req = _find(o, ("request_sha256",))
            item = dict(file=os.path.basename(fp), request_sha256=req if is_hex64(req) else None,
                        form=form if isinstance(form, dict) and all(k in form for k in FEATURES) else None,
                        canonical_json=_find(o, ("canonical_json", "form_canonical_json")),
                        salt_hex=_find(o, ("salt_hex", "salt")), commitment=_find(o, ("commitment", "commitment_sha256")),
                        logit_ckks=_find(o, ("logit_ckks",)), logit_plain=_find(o, ("logit_plain",)),
                        level=_find(o, ("level",)), boundary=_find(o, ("boundary",)), run=_find(o, ("run",)),
                        fictional=_find(o, ("fictional",)))
            if isinstance(item["level"], dict):
                item["level"] = item["level"].get("level")
            items.append(item)
    return items, problems


def commitment_of(salt_hex, canonical_json):
    text = COMMIT_TAG + "\n" + salt_hex + "\n" + canonical_json + "\n"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_candidates(form):
    """Canonical JSON spellings of a form (the gateway's spelling is tried first)."""
    out = []
    ordered = dict((k, form[k]) for k in FEATURES)
    for f in (form, ordered):
        for floats in (False, True):
            g = dict(f)
            if floats:
                g["BMI"] = float(g["BMI"])
            for sk in (True, False):
                for ea in (False, True):
                    s = json.dumps(g, sort_keys=sk, separators=(",", ":"), ensure_ascii=ea)
                    if s not in out:
                        out.append(s)
    return out


def classify_level(logit, edges, band):
    """form_cdc.json levels rule: boundary [i+1, i+2] when |logit - e_i| <= band, else 1 + #{e : logit > e}."""
    for i, e in enumerate(edges):
        if abs(logit - e) <= band:
            return None, [i + 1, i + 2]
    return 1 + sum(1 for e in edges if logit > e), None


def reference_logit(model, x):
    """client.cpp:276 order with the model JSON doubles."""
    e = model["bias"]
    for f in range(len(model["features"])):
        e += model["weights"][f] * ((x[f] - model["mean"][f]) / model["std"][f])
    return e


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_pins(path):
    """Pins from a directory (server.pin, client.pin, possibly under ca/, cloud/ or board/), a JSON or a text file."""
    pins = {}
    if not path:
        return pins
    if os.path.isdir(path):
        cands = {"server": ["server.pin", os.path.join("ca", "server.pin"), os.path.join("board", "server.pin"),
                            os.path.join("pki", "server.pin")],
                 "client": ["client.pin", os.path.join("ca", "client.pin"), os.path.join("cloud", "client.pin"),
                            os.path.join("pki", "client.pin")]}
        for role, names in cands.items():
            for n in names:
                p = os.path.join(path, n)
                if os.path.isfile(p):
                    with open(p, encoding="ascii", errors="replace") as f:
                        v = f.read().strip().lower()
                    if is_hex64(v):
                        pins[role] = v
                        break
        return pins
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    try:
        obj = json.loads(text)
        for role in ("server", "client"):
            v = obj.get(role) or obj.get(role + "_pin")
            if is_hex64(v):
                pins[role] = v
    except ValueError:
        for line in text.splitlines():
            p = line.replace("=", " ").replace(":", " ").split()
            if len(p) >= 2 and p[0].lower().startswith(("server", "client")) and is_hex64(p[-1].lower()):
                pins["server" if p[0].lower().startswith("server") else "client"] = p[-1].lower()
    return pins


def drop_counters(cap, stats_path):
    """Dropped-packet evidence: pcapng interface statistics, else the dumpcap or tcpdump statistics text."""
    if cap["isb"]:
        drops = sum(i.get("ifdrop", 0) + i.get("osdrop", 0) for i in cap["isb"])
        recv = sum(i.get("ifrecv", 0) for i in cap["isb"])
        return dict(source="pcapng interface statistics block", dropped=drops, received=recv)
    if stats_path and os.path.isfile(stats_path):
        with open(stats_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        m = re.findall(r"Packets received/dropped on interface[^:]*:\s*(\d+)/(\d+)", text)
        if m:
            return dict(source="dumpcap statistics", dropped=sum(int(b) for _a, b in m),
                        received=sum(int(a) for a, _b in m))
        m = re.findall(r"(\d+) packets dropped by kernel", text)
        if m:
            return dict(source="tcpdump statistics", dropped=sum(int(v) for v in m), received=None)
    return None


# ------------------------------------------------------------------ tshark
def find_tool(name, explicit=None):
    if explicit:
        return explicit
    env = os.environ.get("EFHE_" + name.upper())
    if env:
        return env
    found = shutil.which(name)
    if found:
        return found
    win = os.path.join(r"C:\Program Files\Wireshark", name + ".exe")
    if os.path.isfile(win):
        return win
    return None


_FIELD_SUPPORT = {}                                       # (tshark path, version, field) -> bool, per process


class Tshark:
    """tshark with an empty personal configuration (no user key log, profile or plugins) and no name resolution."""

    def __init__(self, path=None):
        self.path = find_tool("tshark", path)
        if not self.path:
            raise VerifyError("TSHARK_NOT_FOUND")
        self.cfg = tempfile.mkdtemp(prefix="efhe_tshark_")
        self.env = dict(os.environ, WIRESHARK_CONFIG_DIR=self.cfg, LC_ALL="C")
        r = subprocess.run([self.path, "--version"], capture_output=True, env=self.env)
        first = r.stdout.decode("utf-8", "replace").splitlines()[:1]
        m = re.search(r"(\d+\.\d+\.\d+)", first[0] if first else "")
        self.version = m.group(1) if m else "unknown"

    def close(self):
        shutil.rmtree(self.cfg, ignore_errors=True)

    def run(self, args):
        r = subprocess.run([self.path] + args, capture_output=True, env=self.env)
        out = r.stdout.decode("utf-8", "replace")
        if r.returncode and not out.strip():
            raise VerifyError("TSHARK_FAILED " + r.stderr.decode("utf-8", "replace")[-300:])
        return out

    def supports_field(self, capture, field):
        """True when this tshark knows the field (an unknown -e field makes tshark exit before reading any packet).
        The answer is cached per tshark binary and version, so the console pays for the probe once."""
        key = (self.path, self.version, field)
        if key not in _FIELD_SUPPORT:
            r = subprocess.run([self.path, "-n", "-r", capture, "-c", "1", "-T", "fields", "-e", field],
                               capture_output=True, env=self.env)
            if r.returncode == 0:
                _FIELD_SUPPORT[key] = True
            elif field in r.stderr.decode("utf-8", "replace").split():     # "Some fields aren't valid: <field>"
                _FIELD_SUPPORT[key] = False
            else:
                return True             # the capture itself is unreadable; the next tshark call reports that error
        return _FIELD_SUPPORT[key]

    def fields(self, capture, port, keylog, fields, display=None):
        args = ["-r", capture, "-n", "-d", "tcp.port==%d,tls" % port, "-o", "tls.keylog_file:" + (keylog or ""),
                "-T", "fields", "-E", "separator=\t", "-E", "occurrence=a", "-E", "aggregator=,", "-E", "quote=n"]
        if display:
            args += ["-Y", display]
        for f in fields:
            args += ["-e", f]
        rows = []
        for line in self.run(args).splitlines():
            parts = line.split("\t")
            if len(parts) < len(fields):
                parts += [""] * (len(fields) - len(parts))
            rows.append(dict(zip(fields, parts)))
        return rows


def _ints(s):
    out = []
    for x in s.split(","):
        x = x.strip()
        if x:
            try:
                out.append(int(x, 0))
            except ValueError:
                pass
    return out


def _first(s):
    return s.split(",")[0].strip() if s else ""


def parse_follow_yaml(text):
    """'tshark -q -z follow,tls,yaml,N' sections: [{peers {id: (host, port)}, packets [(frame, peer, ts, bytes)]}]."""
    sections, cur, mode, pk = [], None, None, None

    def flush():
        if cur is not None and pk is not None:
            cur["packets"].append((pk[0], pk[1], pk[2], base64.b64decode("".join(pk[3]))))

    pid = None
    for line in text.splitlines():
        s = line.strip()
        if line.startswith("peers:"):
            flush()
            pk = None
            cur = dict(peers={}, packets=[])
            sections.append(cur)
            mode = "peers"
            continue
        if line.startswith("packets:"):
            mode = "packets"
            continue
        if cur is None or not s or s.startswith("="):
            continue
        if mode == "peers":
            m = re.match(r"- peer: (\d+)$", s)
            if m:
                pid = int(m.group(1))
                cur["peers"][pid] = [None, None]
            elif s.startswith("host:") and pid is not None:
                cur["peers"][pid][0] = s.split(":", 1)[1].strip()
            elif s.startswith("port:") and pid is not None:
                cur["peers"][pid][1] = int(s.split(":", 1)[1])
        elif mode == "packets":
            m = re.match(r"- packet: (\d+)$", s)
            if m:
                flush()
                pk = [int(m.group(1)), None, None, []]
            elif pk is not None and s.startswith("peer:"):
                pk[1] = int(s.split(":", 1)[1])
            elif pk is not None and s.startswith("timestamp:"):
                pk[2] = float(s.split(":", 1)[1])
            elif pk is not None and s.startswith("data:"):
                continue
            elif pk is not None:
                pk[3].append(s)
    flush()
    return sections


NOKEY_FIELDS = ("frame.number", "frame.time_epoch", "eth.dst", "ip.src", "ip.dst", "ipv6.src", "ipv6.dst", "ip.proto",
                "ipv6.nxt", "tcp.stream", "tcp.srcport", "tcp.dstport", "tcp.len", "tcp.flags", "udp.srcport",
                "udp.dstport", "arp.opcode", "icmp.type", "icmpv6.type", "tls.record.content_type",
                "tls.record.opaque_type", "tls.record.length")
KEYED_FIELDS = ("frame.number", "frame.time_epoch", "tcp.stream", "tcp.srcport", "tcp.dstport", "tls.record.content_type",
                "tls.record.opaque_type", "tls.record.length", "tls.handshake.type", "tls.handshake.random",
                "tls.handshake.extensions_server_name", "tls.handshake.extension.type",
                "tls.handshake.extensions.supported_version", "tls.handshake.ciphersuite", "tls.handshake.certificate",
                "tls.alert_message.level", "tls.alert_message.desc")
TLS_STREAM_FIELD = "tls.stream"                           # added to KEYED_FIELDS when this tshark knows it


def _multicast(addr):
    if not addr:
        return False
    if ":" in addr:
        return addr.lower().startswith("ff")
    try:
        first = int(addr.split(".")[0])
    except ValueError:
        return False
    return 224 <= first <= 239 or addr == "255.255.255.255"


def _eth_group(mac):
    if not mac:
        return False
    try:
        return int(mac.split(":")[0], 16) & 1 == 1
    except ValueError:
        return False


def parse_conv(text):
    """Rows of 'tshark -q -z conv,tcp|udp' (address, port and frame/byte counts), sorted."""
    rows, proto = [], None
    for line in text.splitlines():
        m = re.match(r"\s*(TCP|UDP) Conversations", line)
        if m:
            proto = m.group(1)
            continue
        m = re.match(r"\s*(\S+):(\d+)\s+<->\s+(\S+):(\d+)\s+(.*)$", line)
        if m and proto:
            rows.append(dict(proto=proto, a="%s:%s" % (m.group(1), m.group(2)), b="%s:%s" % (m.group(3), m.group(4)),
                             a_port=int(m.group(2)), b_port=int(m.group(4)), counts=" ".join(m.group(5).split())))
    rows.sort(key=lambda r: (r["proto"], r["a"], r["b"]))
    return rows


# ------------------------------------------------------------------ capture collection
def collect_capture(capture, keylog, port, tshark, observer=None, quick_port=None):
    """Read the capture with tshark (no-key view and decrypted view) and the native reader.
    Returns dict(cap, connections, frames_nokey, inventory rows, lost, conv)."""
    cap = native.read_capture(capture)
    nconns = native.tcp_connections(cap, port)
    disp = None
    if quick_port is not None:
        disp = "tcp.port==%d && tcp.port==%d" % (quick_port, port)
    nokey = tshark.fields(capture, port, None, NOKEY_FIELDS, disp)
    streams = {}
    for r in nokey:
        sid = _first(r["tcp.stream"])
        sp, dp = _first(r["tcp.srcport"]), _first(r["tcp.dstport"])
        if not sid or not sp or not dp:
            continue
        sp, dp = int(sp), int(dp)
        if port not in (sp, dp):
            continue
        st = streams.setdefault(int(sid), dict(stream=int(sid), frames=[], nokey={"c2s": [], "s2c": []},
                                               keyed={"c2s": [], "s2c": []}, chunks={"c2s": [], "s2c": []},
                                               hs={"c2s": [], "s2c": []}, certs={}, ext_types=set(),
                                               client_random=None, sni=None, version=None, suite=None,
                                               undecrypted={"c2s": 0, "s2c": 0}, hellos={"c2s": 0, "s2c": 0},
                                               tls_streams=set()))
        d = "s2c" if sp == port else "c2s"
        fno, ts = int(r["frame.number"]), float(r["frame.time_epoch"])
        st["frames"].append(fno)
        if "client" not in st:                   # endpoints from either direction (a capture may miss one side)
            src = _first(r["ip.src"]) or _first(r["ipv6.src"])
            dst = _first(r["ip.dst"]) or _first(r["ipv6.dst"])
            if d == "c2s":
                st["client"], st["server"], st["client_port"] = "%s:%d" % (src, sp), "%s:%d" % (dst, dp), sp
            else:
                st["client"], st["server"], st["client_port"] = "%s:%d" % (dst, dp), "%s:%d" % (src, sp), dp
        flags = int(_first(r["tcp.flags"]) or "0", 16)
        if flags & 0x02 and not flags & 0x10 and d == "c2s":
            st["t_syn"] = ts
        ctypes, otypes, lens = _ints(r["tls.record.content_type"]), _ints(r["tls.record.opaque_type"]), _ints(
            r["tls.record.length"])
        nplain = len(ctypes)
        for i, ln in enumerate(lens):
            if i < nplain:
                st["nokey"][d].append((fno, ts, ctypes[i], ln, False))
            else:
                st["nokey"][d].append((fno, ts, otypes[i - nplain] if i - nplain < len(otypes) else 23, ln, True))
    # 'follow,tls,yaml,N' takes the TLS stream index (tls.stream), which since tshark 4.x counts only the TCP
    # conversations that carry TLS: a refused SYN/RST connection on the server port shifts every later TCP index but
    # not the TLS ones. Older tshark has no tls.stream field and follows by the tcp.stream index.
    tls_index = bool(keylog and streams) and tshark.supports_field(capture, TLS_STREAM_FIELD)
    keyed_fields = KEYED_FIELDS + ((TLS_STREAM_FIELD,) if tls_index else ())
    keyed_rows = tshark.fields(capture, port, keylog, keyed_fields,
                               "tls" if disp is None else "tls && " + disp) if keylog else []
    for r in keyed_rows:
        sid = _first(r["tcp.stream"])
        if not sid or int(sid) not in streams:
            continue
        st = streams[int(sid)]
        if tls_index:
            for x in _ints(r.get(TLS_STREAM_FIELD, "")):
                st["tls_streams"].add(x)
        d = "s2c" if int(_first(r["tcp.srcport"]) or 0) == port else "c2s"
        fno, ts = int(r["frame.number"]), float(r["frame.time_epoch"])
        ctypes, lens = _ints(r["tls.record.content_type"]), _ints(r["tls.record.length"])
        levels, descs = _ints(r["tls.alert_message.level"]), _ints(r["tls.alert_message.desc"])
        st["undecrypted"][d] += max(0, len(lens) - len(ctypes))
        ai = 0
        for i, ln in enumerate(lens):
            ct = ctypes[i] if i < len(ctypes) else None
            alert = None
            if ct == 21:
                alert = (levels[ai] if ai < len(levels) else None, descs[ai] if ai < len(descs) else None)
                ai += 1
            st["keyed"][d].append((fno, ts, ct, ln, alert))
        types = _ints(r["tls.handshake.type"])
        st["hs"][d].extend(types)
        if 1 in types and d == "c2s":
            st["hellos"]["c2s"] += types.count(1)
            if st["client_random"] is None:
                st["client_random"] = _first(r["tls.handshake.random"]).replace(":", "").lower() or None
            st["sni"] = _first(r["tls.handshake.extensions_server_name"]) or st["sni"]
        if 2 in types and d == "s2c":
            st["hellos"]["s2c"] += types.count(2)
            sv = _ints(r["tls.handshake.extensions.supported_version"])
            st["version"] = "0x%04x" % sv[0] if sv else None
            cs = _ints(r["tls.handshake.ciphersuite"])
            st["suite"] = "0x%04x" % cs[0] if cs else None
        st["ext_types"].update(_ints(r["tls.handshake.extension.type"]))
        if 11 in types and d not in st["certs"]:
            der = _first(r["tls.handshake.certificate"]).replace(":", "")
            if der:
                try:
                    st["certs"][d] = hashlib.sha256(bytes.fromhex(der)).hexdigest()
                except ValueError:
                    pass
    follow = sorted(set(x for st in streams.values() for x in st["tls_streams"])) if tls_index else sorted(streams)
    if keylog and follow:
        args = ["-r", capture, "-n", "-d", "tcp.port==%d,tls" % port, "-o", "tls.keylog_file:" + keylog, "-q"]
        for idx in follow:
            args += ["-z", "follow,tls,yaml,%d" % idx]
        sections = parse_follow_yaml(tshark.run(args))
        frame_owner = {}
        for sid, st in streams.items():
            for fno in st["frames"]:
                frame_owner[fno] = sid
        for sec in sections:
            owners = set(frame_owner.get(p[0]) for p in sec["packets"])
            owners.discard(None)
            if len(owners) != 1:
                continue
            st = streams[owners.pop()]
            server_peers = [pid for pid, (_h, prt) in sec["peers"].items() if prt == port]
            spid = server_peers[0] if len(server_peers) == 1 else None
            for fno, peer, ts, data in sec["packets"]:
                st["chunks"]["s2c" if peer == spid else "c2s"].append((fno, ts, data))
    # native view: raw streams, gaps, record counts
    by_client = {}
    for c in nconns:
        by_client.setdefault(c["client"], []).append(c)
    conns = []
    for sid in sorted(streams, key=lambda s: (min(streams[s]["frames"]), s)):
        st = streams[sid]
        if "client" not in st:
            continue
        nat = None
        for c in by_client.get(st["client"], []):
            if set(c["frames"]) & set(st["frames"]):
                nat = c
                break
        st["native"] = nat
        conns.append(st)
    for i, st in enumerate(conns, 1):
        st["label"] = "c%02d" % i
    out = dict(cap=cap, connections=conns, nokey_rows=nokey, native_connections=nconns)
    if quick_port is None:
        out["lost"] = [int(r["frame.number"]) for r in tshark.fields(
            capture, port, None, ("frame.number",), "tcp.analysis.lost_segment || tcp.analysis.ack_lost_segment")]
        conv = tshark.run(["-r", capture, "-n", "-q", "-o", "tls.keylog_file:", "-z", "conv,tcp", "-z", "conv,udp"])
        out["conv"] = parse_conv(conv)
    return out


def _time_lookup(chunks):
    ends, times, total = [], [], 0
    for _f, ts, data in chunks:
        total += len(data)
        ends.append(total)
        times.append(ts)

    def at(offset):
        i = bisect.bisect_right(ends, offset)
        return times[i] if i < len(times) else None
    return at


def analyse_connection(st, form_model_hex=FORM_MODEL_HEX):
    """Frames, timing and classification of one connection from its decrypted chunks."""
    up = b"".join(c[2] for c in st["chunks"]["c2s"])
    down = b"".join(c[2] for c in st["chunks"]["s2c"])
    st["app_up"], st["app_down"] = up, down
    at_up, at_down = _time_lookup(st["chunks"]["c2s"]), _time_lookup(st["chunks"]["s2c"])
    reqs, p1 = split_frames(up, 1)
    resps, p2 = split_frames(down, 2)
    for fr in reqs:
        fr["t_first"], fr["t_last"] = at_up(fr["offset"]), at_up(fr["end"] - 1)
    for fr in resps:
        fr["t_first"], fr["t_last"] = at_down(fr["offset"]), at_down(fr["end"] - 1)
    st["requests"], st["responses"], st["frame_problems"] = reqs, resps, p1 + p2
    if st.get("keys_missing") or any((st.get("undecrypted") or {}).values()):
        cls = "undecrypted"
    elif not up and not down:
        cls = "handshake_only"
    elif (len(reqs) == 1 and len(resps) == 1 and not p1 and not p2 and reqs[0]["fields"]
          and reqs[0]["fields"]["model_id"] == form_model_hex and reqs[0]["fields"]["features"] == 4
          and reqs[0]["fields"]["rows"] == 1 and reqs[0]["fields"]["sequence"] == 1):
        cls = "form"
    else:
        cls = "other"
    st["classification"] = cls
    return st


# ------------------------------------------------------------------ record-size signature (no keys)
def record_signature(st):
    """Classify the no-key record sequence of one connection (gate G1)."""
    c2s, s2c = st["nokey"]["c2s"], st["nokey"]["s2c"]
    problems = []
    c_plain = [r for r in c2s if not r[4]]
    c_enc = [r for r in c2s if r[4]]
    s_plain = [r for r in s2c if not r[4]]
    s_enc = [r for r in s2c if r[4]]
    if not c_plain or c_plain[0][2] != 22:
        problems.append("no ClientHello")
    if not s_plain or s_plain[0][2] != 22:
        problems.append("no ServerHello")
    for r in c_plain[1:] + s_plain[1:]:
        if r[2] not in (20, 22):
            problems.append("unexpected cleartext record type %d" % r[2])
    # The client flight (Certificate, CertificateVerify, Finished) ends with the Finished record, whose size is fixed:
    # 4 + 48 + 17 = 69 B (SHA-384 suites) or 4 + 32 + 17 = 53 B (SHA-256 suites).
    k = None
    for i, r in enumerate(c_enc[:4]):
        if r[3] in (REC_HEADER, REC_LIMB):
            break
        if r[3] in (53, 69):
            k = i + 1
            break
    if k is None:
        k = 0
        if c_enc:
            problems.append("no client Finished-sized record in the first encrypted client records")
    c_flight, c_rest = c_enc[:k], c_enc[k:]
    t_cflight = c_flight[0][1] if c_flight else None
    s_flight = [r for r in s_enc if t_cflight is None or r[1] < t_cflight]
    s_rest = [r for r in s_enc if not (t_cflight is None or r[1] < t_cflight)]

    def groups(recs, direction):
        out, i, tail, probs = [], 0, [], []
        while i < len(recs):
            ln = recs[i][3]
            if ln == REC_HEADER:
                j = i + 1
                while j < len(recs) and recs[j][3] == REC_LIMB:
                    j += 1
                m = j - i - 1
                ok = m > 0 and m % 6 == 0 and (direction == "c2s" and m // 6 <= 32 or direction == "s2c" and m == 6)
                out.append(m)
                if not ok:
                    probs.append("%s header record followed by %d limb records" % (direction, m))
                i = j
            else:
                tail.append(ln)
                i += 1
        return out, tail, probs

    s_close = []
    if s_rest and s_rest[-1][3] == REC_CLOSE:          # the server's final close_notify is not a ticket
        s_close = [s_rest.pop()]
    nst = []
    while s_rest and s_rest[0][3] not in (REC_HEADER, REC_LIMB):
        nst.append(s_rest.pop(0)[3])
    cg, ctail, cp = groups(c_rest, "c2s")
    sg, stail, sp = groups(s_rest + s_close, "s2c")
    problems += cp + sp
    if len(nst) > 2 or any(x >= 2048 for x in nst):
        problems.append("server post-handshake records %s (at most 2 tickets below 2048 B expected)" % nst)
    for direction, tail in (("c2s", ctail), ("s2c", stail)):
        if tail and (tail != [REC_CLOSE] or False):
            problems.append("%s records outside frames: %s" % (direction, tail))
    if not cg and not sg:
        cls = "handshake_only"
    elif cg == [24] and sg == [6]:
        cls = "form"
    else:
        cls = "other"
    if len(cg) != len(sg):
        problems.append("%d request groups but %d response groups" % (len(cg), len(sg)))
    sig = dict(classification=cls, client_flight=[r[3] for r in c_flight], server_flight=[r[3] for r in s_flight],
               tickets=nst, c2s_groups=["273+%dx16401" % m for m in cg], s2c_groups=["273+%dx16401" % m for m in sg],
               c2s_tail=ctail, s2c_tail=stail, client_close_notify=ctail[-1:] == [REC_CLOSE],
               server_close_notify=stail[-1:] == [REC_CLOSE], problems=problems,
               c2s_records=len(c2s), s2c_records=len(s2c), request_groups=len(cg))
    return sig


# ------------------------------------------------------------------ gates
SEAL_ORDER_FILE = "seal_order.json"


def read_seal_order(path):
    """The seal-order record that efhe_seal.py writes when capture_dsb.pcapng (made from the key log) already existed
    before capture.sha256 was written. None when there is no record; an unreadable record still counts."""
    if not path or not os.path.isfile(path):
        return None
    try:
        rec = load_json(path)
    except (OSError, ValueError):
        return dict(violation="unreadable seal_order.json")
    return rec if isinstance(rec, dict) else dict(violation="unreadable seal_order.json")


def g0_capture(ctx):
    checks = []
    seal = ctx.get("seal")
    cap_hash = ctx["inputs"]["capture"]["sha256"]
    if seal is None:
        checks.append(check("sealed_hash", "INCOMPLETE", reason="no capture.sha256 seal record"))
    else:
        checks.append(check("sealed_hash", "PASS" if seal == cap_hash else "FAIL", sealed=seal, capture=cap_hash))
    order = ctx.get("seal_order")
    if order is not None:
        # The capture hash was taken after the key log had already been used (capture_dsb.pcapng existed), so the
        # bundle cannot claim that the capture was sealed first. The capture may well be intact: an independent
        # observer capture (G11) or the operator's own record has to settle it.
        checks.append(check("seal_order", "FAIL-PENDING", record=order,
                            reason="capture_dsb.pcapng existed before capture.sha256 was written: the TLS key log was "
                                   "used before the capture was sealed"))
    elif seal is not None:
        checks.append(check("seal_order", "PASS", reason="efhe_seal.py recorded no seal-order violation "
                                                         "(no seal_order.json in the run directory)"))
    cap = ctx["cap"]
    dc = drop_counters(cap, ctx.get("stats_path"))
    if dc is None:
        checks.append(check("dropped_packets", "INCOMPLETE", reason="no drop counters (pcapng statistics or stats file)"))
    else:
        checks.append(check("dropped_packets", "PASS" if dc["dropped"] == 0 else "FAIL", **dc))
    checks.append(check("capture_file", "FAIL" if cap["problems"] else "PASS", format=cap["format"],
                        packets=len(cap["packets"]), problems=cap["problems"]))
    checks.append(check("no_embedded_secrets", "FAIL" if cap["dsb"] else "PASS", decryption_secrets_blocks=cap["dsb"]))
    gaps = []
    for st in ctx["connections"]:
        nat = st.get("native")
        if nat is None:
            gaps.append(dict(connection=st["label"], problem="not reassembled"))
            continue
        for d in ("c2s", "s2c"):
            if nat[d + "_gaps"] or nat[d + "_conflicts"]:
                gaps.append(dict(connection=st["label"], direction=d, gaps=nat[d + "_gaps"][:4],
                                 conflicts=nat[d + "_conflicts"][:4]))
        if nat["truncated_segments"]:
            gaps.append(dict(connection=st["label"], truncated_segments=nat["truncated_segments"]))
    lost = ctx.get("lost", [])
    checks.append(check("tcp_gaps", "FAIL" if gaps or lost else "PASS", gaps=gaps, lost_segment_frames=lost[:20]))
    return gate(combine(checks), checks)


def _ip_of(endpoint):
    return endpoint.rsplit(":", 1)[0]


REHEARSAL_SUMMARY = "rehearsal_summary.txt"      # demo/cloud/rehearse_loopback.sh and demo/sim/rehearse_form.sh


def _loopback_ip(ip):
    ip = (ip or "").strip("[]").lower()
    return ip.startswith("127.") or ip in ("::1", "0:0:0:0:0:0:0:1")


def pc_rehearsal(ctx):
    """Whether the board side of this run ran on a PC (a PC rehearsal or test), so that its times, records and energy
    are PC values and never KV260 figures: the run directory holds rehearsal_summary.txt, a board address is a
    loopback address (as in evidence_console.is_rehearsal), or a board address equals the cloud address (one machine
    on both ends). report.html labels such a run 'PC 演練（非 KV260 板上實測）'; no gate depends on it."""
    conns = ctx.get("connections") or []
    boards = set(_ip_of(c["client"]) for c in conns if c.get("client"))
    clouds = set(_ip_of(c["server"]) for c in conns if c.get("server"))
    if ctx.get("board_ip"):
        boards.add(ctx["board_ip"])
    reasons = []
    if ctx.get("rehearsal_summary"):
        reasons.append("rehearsal_summary_present")
    if any(_loopback_ip(ip) for ip in boards):
        reasons.append("loopback_board_address")
    elif boards & clouds:
        reasons.append("board_address_equals_cloud_address")
    return dict(detected=bool(reasons), reasons=reasons)


def g1_inventory(ctx):
    checks = []
    port = ctx["port"]
    conns = ctx["connections"]
    boards = set(_ip_of(c["client"]) for c in conns)
    pcs = set(_ip_of(c["server"]) for c in conns)
    if ctx.get("board_ip"):
        boards.add(ctx["board_ip"])
    counts = {}
    violations = []
    third = {}
    for r in ctx["nokey_rows"]:
        src = _first(r["ip.src"]) or _first(r["ipv6.src"])
        dst = _first(r["ip.dst"]) or _first(r["ipv6.dst"])
        if r["arp.opcode"]:
            kind = "arp"
        elif not src:
            kind = "non_ip"
        elif _multicast(dst) or _eth_group(_first(r["eth.dst"])):
            kind = "broadcast_multicast"
        else:
            sp = _first(r["tcp.srcport"]) or _first(r["udp.srcport"])
            dp = _first(r["tcp.dstport"]) or _first(r["udp.dstport"])
            is_tcp = bool(_first(r["tcp.srcport"]))
            pair = {src, dst}
            # board <-> cloud in either direction (on a loopback rehearsal both are the same address)
            if (src in boards and dst in pcs) or (src in pcs and dst in boards):
                if is_tcp and str(port) in (sp, dp):
                    kind = "tls_port"
                else:
                    kind = "board_pc_other"
                    if len(violations) < 20:
                        violations.append(dict(frame=int(r["frame.number"]), src=src, dst=dst,
                                               proto="tcp" if is_tcp else ("udp" if _first(r["udp.srcport"]) else
                                                                           "ip%s" % (_first(r["ip.proto"]) or
                                                                                     _first(r["ipv6.nxt"]))),
                                               sport=sp, dport=dp))
            elif pair & boards:
                kind = "board_other_host"
                peer = (pair - boards).pop() if pair - boards else src
                third[peer] = third.get(peer, 0) + 1
            else:
                kind = "other"
        counts[kind] = counts.get(kind, 0) + 1
    checks.append(check("only_unicast_flow_is_tls_port", "FAIL" if counts.get("board_pc_other") else "PASS",
                        frames=counts, violations=violations, board=sorted(boards), cloud=sorted(pcs),
                        third_party_unicast=dict(sorted(third.items()))))
    sigs = []
    for st in conns:
        sig = st["signature"]
        nat = st.get("native")
        if nat is not None:
            nc = len(native.tls_records(nat["c2s"])[0])
            ns = len(native.tls_records(nat["s2c"])[0])
            if (nc, ns) != (sig["c2s_records"], sig["s2c_records"]):
                sig["problems"].append("record counts differ: tshark %d/%d, native %d/%d" % (
                    sig["c2s_records"], sig["s2c_records"], nc, ns))
        ok = not sig["problems"] and (sig["classification"] in ("form", "handshake_only") or
                                      (sig["classification"] == "other" and not ctx.get("forms_only", True)))
        sigs.append(check("record_signature", "PASS" if ok else "FAIL", connection=st["label"], **sig))
    checks.extend(sigs)
    if not conns:
        checks.append(check("connections", "FAIL", reason="no TCP connection to the server port"))
    return gate(combine(checks), checks, conversations=ctx.get("conv", []))


def g2_tls(ctx):
    checks = []
    pins = ctx.get("pins") or {}
    keys = ctx.get("keys") or {}
    if not ctx.get("keylog_supplied", True):
        return gate("INCOMPLETE", [check("tls", "INCOMPLETE", reason="no TLS key log supplied")])
    for st in ctx["connections"]:
        c = dict(connection=st["label"], suite=st["suite"], version=st["version"], sni=st["sni"],
                 tickets=st["hs"]["s2c"].count(4))
        probs, incomplete = [], []
        cr = st["client_random"]
        k = keys.get(cr or "", {})
        missing = [lb for lb in native.KEYLOG_LABELS if lb not in k]
        if not cr:
            probs.append("no ClientHello random")
        elif missing:
            probs.append("key log lacks %s" % ",".join(missing))
        if st["version"] != "0x0304":
            probs.append("ServerHello version %s" % st["version"])
        if st["hellos"]["c2s"] != 1 or st["hellos"]["s2c"] != 1:
            probs.append("hellos %d/%d (HelloRetryRequest or renegotiation)" % (st["hellos"]["c2s"], st["hellos"]["s2c"]))
        if 41 in st["ext_types"] or 42 in st["ext_types"]:
            probs.append("pre_shared_key or early_data extension present")
        if st["sni"] != ctx.get("sni", SNI):
            probs.append("SNI %r" % st["sni"])
        need_s = [t for t in (8, 13, 11, 15, 20) if t not in st["hs"]["s2c"]]
        need_c = [t for t in (11, 15, 20) if t not in st["hs"]["c2s"]]
        if need_s or need_c:
            probs.append("handshake messages not decrypted or absent: server %s client %s" % (need_s, need_c))
        und = st["undecrypted"]
        if und["c2s"] or und["s2c"]:
            probs.append("AEAD failure: %d client and %d server records not decrypted" % (und["c2s"], und["s2c"]))
        c.update(server_leaf_sha256=st["certs"].get("s2c"), client_leaf_sha256=st["certs"].get("c2s"))
        for role, d in (("server", "s2c"), ("client", "c2s")):
            got = st["certs"].get(d)
            if role not in pins:
                incomplete.append("no configured %s pin" % role)
            elif got != pins[role]:
                probs.append("%s leaf SHA-256 %s does not equal the pin" % (role, (got or "absent")[:16]))
        nc = st.get("native_check")
        if nc:
            c["native_crosscheck"] = nc["status"]
            if nc["status"] == "FAIL":
                probs.append("native decoder: " + nc["detail"])
        status = "FAIL" if probs else ("INCOMPLETE" if incomplete else "PASS")
        checks.append(check("tls", status, problems=probs, incomplete=incomplete, **c))
    return gate(combine(checks, "FAIL" if not ctx["connections"] else "PASS"), checks)


def _blocked(st, need=("c2s", "s2c")):
    if st.get("keys_missing"):
        return True
    return any(st["undecrypted"][d] for d in need)


def _nothing_to_check(st):
    """No request in the no-key record sizes and none decrypted: a handshake-only connection."""
    return not st["requests"] and not st["responses"] and not st["signature"].get("request_groups")


def _gate_entry(st, name, need=("c2s", "s2c")):
    """None when the connection can be checked, else the check to report for it (BLOCKED or FAIL)."""
    if _blocked(st, need):
        return check(name, "BLOCKED", connection=st["label"], reason="decryption incomplete")
    if not st["requests"] and not st["responses"]:
        return check(name, "FAIL", connection=st["label"],
                     reason="the record sizes show application data, but none was decrypted")
    return None


def g3_framing(ctx):
    checks = []
    for st in ctx["connections"]:
        if _nothing_to_check(st):
            checks.append(check("framing", "PASS", connection=st["label"], classification="handshake_only"))
            continue
        if _blocked(st):
            checks.append(check("framing", "BLOCKED", connection=st["label"], reason="decryption incomplete"))
            continue
        probs = [p["code"] for p in st["frame_problems"]]
        frames = []
        for fr in st["requests"] + st["responses"]:
            f = fr["fields"] or {}
            frames.append(dict(kind=f.get("kind"), sequence=f.get("sequence"), features=f.get("features"),
                               rows=f.get("rows"), model=(model_by_id(f.get("model_id", "")) or ("unknown",))[0],
                               error=fr["error"], complete=fr["complete"]))
        cls = st["classification"]
        if cls != "form":
            if ctx.get("forms_only", True):
                probs.append("not a form connection (one CDC request with rows 1 and sequence 1 expected)")
        for fr in st["requests"]:
            f = fr["fields"] or {}
            if cls == "form" and f.get("model_id") != ctx["form_model_hex"]:
                probs.append("model_id is not SHA-256 of the model JSON")
        accounted = sum(fr["end"] - fr["offset"] for fr in st["requests"] + st["responses"])
        total = len(st["app_up"]) + len(st["app_down"])
        status = "FAIL" if probs else "PASS"
        checks.append(check("framing", status, connection=st["label"], classification=cls, frames=frames,
                            problems=sorted(set(probs)), app_bytes_up=len(st["app_up"]),
                            app_bytes_down=len(st["app_down"]), accounted_bytes=accounted, total_bytes=total))
    return gate(combine(checks, "FAIL"), checks)


def g4_binding(ctx):
    checks = []
    keys = ctx.get("keys") or {}
    for st in ctx["connections"]:
        if _nothing_to_check(st):
            continue
        entry = _gate_entry(st, "binding")
        if entry:
            checks.append(entry)
            continue
        k = keys.get(st["client_random"] or "", {})
        ems = k.get("EXPORTER_SECRET")
        session = native.tls13_exporter(ems, EXPORTER_LABEL, PROFILE, 32).hex() if ems else None
        probs, rows = [], []
        key0 = gen0 = None
        for i, fr in enumerate(st["requests"]):
            h, f = fr["header"], fr["fields"] or {}
            row = dict(sequence=f.get("sequence"), request_sha256=f.get("request_hash"))
            row["request_id_ok"] = request_id_of(h) == f.get("request_id")
            row["request_hash_ok"] = (request_hash_of(h, fr["body"]) == f.get("request_hash")) if fr["complete"] else None
            row["session_binding_ok"] = session is not None and f.get("session_id") == session
            row["sequence_ok"] = f.get("sequence") == i + 1
            if key0 is None:
                key0, gen0 = f.get("key_id"), f.get("generation")
            row["key_generation_constant"] = f.get("key_id") == key0 and f.get("generation") == gen0
            if i < len(st["responses"]):
                rh = st["responses"][i]["header"]
                exp = bytearray(h)
                exp[10] = 2
                struct.pack_into("<Q", exp, 32, CT_BYTES)
                row["response_binding_ok"] = rh == bytes(exp)
            else:
                row["response_binding_ok"] = None
            for name in ("request_id_ok", "request_hash_ok", "session_binding_ok", "sequence_ok",
                         "key_generation_constant"):
                if row[name] is False:
                    probs.append(name.replace("_ok", "") + " failed (sequence %s)" % row["sequence"])
            if row["response_binding_ok"] is False:
                probs.append("response binding failed (sequence %s)" % row["sequence"])
            rows.append(row)
        if len(st["responses"]) > len(st["requests"]):
            probs.append("more responses than requests")
        if ems is None:
            probs.append("no EXPORTER_SECRET for this connection")
        checks.append(check("binding", "FAIL" if probs else "PASS", connection=st["label"], requests=rows,
                            problems=probs, exporter="TLS-Exporter(EXPORTER_SECRET, %s, profile, 32)" %
                            EXPORTER_LABEL.decode()))
    return gate(combine(checks, "FAIL" if not ctx["connections"] else "PASS"), checks)


def g5_affine(ctx):
    checks = []
    for st in ctx["connections"]:
        if _nothing_to_check(st):
            continue
        entry = _gate_entry(st, "affine")
        if entry:
            checks.append(entry)
            continue
        rows, probs = [], []
        for i, fr in enumerate(st["requests"]):
            f = fr["fields"] or {}
            m = model_by_id(f.get("model_id", ""))
            if i >= len(st["responses"]) or not fr["complete"]:
                rows.append(dict(sequence=f.get("sequence"), bit_exact=None, reason="no complete response"))
                continue
            resp = st["responses"][i]["body"]
            if m is None:
                probs.append("unknown model")
                rows.append(dict(sequence=f.get("sequence"), bit_exact=False, reason="unknown model"))
                continue
            weights = m[3] if m[1] != ctx["form_model_hex"] or not ctx.get("model") else ctx["model"]["weights_int"]
            bias = m[4] if m[1] != ctx["form_model_hex"] or not ctx.get("model") else ctx["model"]["bias_int"]
            exp = affine(fr["body"], f["features"], weights, bias)
            same = exp == resp
            row = dict(sequence=f.get("sequence"), model=m[0], bit_exact=same, words=CT_WORDS)
            if not same:
                a, b = words_of(exp), words_of(resp)
                diff = [j for j in range(min(len(a), len(b))) if a[j] != b[j]]
                row.update(mismatched_words=len(diff) + abs(len(a) - len(b)),
                           first_mismatch=locate_word(diff[0]) if diff else None)
                probs.append("response differs from affine(request) (sequence %s)" % f.get("sequence"))
            rows.append(row)
        checks.append(check("affine", "FAIL" if probs else "PASS", connection=st["label"], pairs=rows, problems=probs))
    return gate(combine(checks, "FAIL" if not ctx["connections"] else "PASS"), checks,
                rule="cloud.cpp:24-43: c_p,l[i] = sum_f x_f,p,l[i] * (W_f mod q_l) mod q_l; c0,l[0] += B mod q_l")


def g6_statistics(ctx):
    checks, negative = [], []
    seen_limbs, keys_seen, gens_seen = {}, {}, {}
    for st in ctx["connections"]:
        if _nothing_to_check(st):
            continue
        entry = _gate_entry(st, "ciphertext")
        if entry:
            checks.append(entry)
            continue
        probs, rows = [], []
        for fr in st["requests"]:
            f = fr["fields"] or {}
            if not fr["complete"]:
                continue
            nct = len(fr["body"]) // CT_BYTES
            stats = body_stats(fr["body"], nct)
            bad = [s for s in stats if not s["uniform"]]
            nonc = noncanonical(words_of(fr["body"]))
            reasons = sorted(set(s["reason"] for s in bad))
            worst_limb = min(stats, key=lambda s: s["chi2_p"]) if stats else None
            rows.append(dict(sequence=f.get("sequence"), limbs=len(stats), uniform_limbs=len(stats) - len(bad),
                             noncanonical_words=len(nonc), reasons=reasons, worst_limb=worst_limb,
                             histogram=histogram(fr["body"])))
            st.setdefault("request_stats", []).append(dict(sequence=f.get("sequence"), stats=stats))
            if bad:
                probs.append("%d of %d request limbs not uniform (%s), sequence %s" % (
                    len(bad), len(stats), ",".join(reasons), f.get("sequence")))
                negative.append(dict(connection=st["label"], sequence=f.get("sequence"), limbs=len(bad),
                                     reasons=reasons))
            for j, dg in enumerate(limb_digests(fr["body"])):
                seen_limbs.setdefault(dg, []).append((st["label"], "request", f.get("sequence"), j))
            if f.get("key_id"):
                keys_seen.setdefault(f["key_id"], set()).add(st["label"])
                gens_seen.setdefault(f["generation"], set()).add(st["label"])
        resp_nonc = 0
        for fr in st["responses"]:
            if fr["complete"]:
                resp_nonc += len(noncanonical(words_of(fr["body"])))
                for j, dg in enumerate(limb_digests(fr["body"])):
                    seen_limbs.setdefault(dg, []).append((st["label"], "response",
                                                          (fr["fields"] or {}).get("sequence"), j))
        if resp_nonc:
            probs.append("%d non-canonical response words" % resp_nonc)
        checks.append(check("ciphertext", "FAIL" if probs else "PASS", connection=st["label"], requests=rows,
                            problems=probs, noncanonical_response_words=resp_nonc))
    repeats = sorted([dict(sha256=dg[:16], places=[list(p) for p in v]) for dg, v in seen_limbs.items() if len(v) > 1],
                     key=lambda r: r["sha256"])
    checks.append(check("no_repeated_16KiB_block", "FAIL" if repeats else "PASS", repeated=repeats[:10],
                        repeated_blocks=len(repeats)))
    reused_keys = sorted(k[:16] for k, v in keys_seen.items() if len(v) > 1)
    reused_gens = sorted("%016x" % g for g, v in gens_seen.items() if len(v) > 1)
    checks.append(check("fresh_key_and_generation", "FAIL" if reused_keys or reused_gens else "PASS",
                        connections_with_keys=len(set(c for v in keys_seen.values() for c in v)),
                        reused_key_ids=reused_keys, reused_generations=reused_gens))
    ctx["negative_control"] = negative
    return gate(combine(checks, "FAIL"), checks,
                thresholds=dict(alpha_per_test=ALPHA, chi2_bins=64, ks_sqrt_n_below=rnd(KS_CRIT, 4),
                                mean_abs_z_below=Z_CRIT, small_residue_bound="2^22 of 0 or q, expected + 6 sigma"))


def _hit_record(direction, label, pos, loc):
    d = dict(direction=direction, pattern=label, offset=pos)
    d.update(loc)
    return d


def _raw_location(stream, pos):
    recs, _trail = native.tls_records(stream)
    for i, (off, typ, ln) in enumerate(recs):
        if off <= pos < off + 5 + ln:
            return dict(where="tls_record", record=i, record_type=typ, in_header=pos < off + 5)
    return dict(where="tcp_stream")


def g7_plaintext(ctx):
    checks = []
    model = ctx.get("model")
    dom = domain_patterns(model, ctx.get("form_schema"))
    dom_dense, dom_sparse = dom.prefix_index(False), dom.prefix_index(True)
    disclosed = [d for d in ctx.get("disclosures", []) if d.get("form")]
    long_hits, explained, blocked = [], [], []
    for st in ctx["connections"]:
        nat = st.get("native")
        if nat is not None:
            for d in ("c2s", "s2c"):
                for label, pos in search_long(nat[d], dom_dense):
                    long_hits.append(dict(connection=st["label"], direction="raw " + d, pattern=label, offset=pos,
                                          **_raw_location(nat[d], pos)))
        if _blocked(st):
            blocked.append(st["label"])
        for d, stream, frames in (("c2s", st["app_up"], st["requests"]), ("s2c", st["app_down"], st["responses"])):
            for label, pos in search_decrypted(stream, dom_dense, dom_sparse):
                loc = locate_stream(frames, pos, len(dom.long[label]), is_sparse(dom.long[label]))
                rec = dict(connection=st["label"], direction=d, pattern=label, offset=pos)
                rec.update(loc)
                (explained if loc["explained"] else long_hits).append(rec)
    n_sparse = sum(1 for p in dom.long.values() if is_sparse(p))
    checks.append(check("domain_long_patterns", "FAIL" if long_hits else ("BLOCKED" if blocked else "PASS"),
                        patterns=len(dom.long), dense_patterns=len(dom.long) - n_sparse, sparse_patterns=n_sparse,
                        unexplained_hits=long_hits[:20], explained_header_hits=len(explained),
                        explained_examples=explained[:6], blocked_connections=blocked,
                        searched="dense patterns at every offset of the raw TCP payload of both directions and of "
                                 "every decrypted byte; sparse patterns (four or more zero bytes) at every 4-byte-"
                                 "aligned offset of the decrypted streams"))
    by_req = {}
    for st in ctx["connections"]:
        for fr in st["requests"]:
            if fr["fields"]:
                by_req[fr["fields"]["request_hash"]] = st
    groups = {}
    for d in disclosed:
        key = tuple(float(d["form"][n]) for n in FEATURES)
        groups.setdefault(key, []).append(d)
    form_checks = []
    expected_chance = 0.0
    for d in disclosed:
        st = by_req.get(d.get("request_sha256"))
        name = (d.get("request_sha256") or "unknown")[:16]
        if st is None:
            form_checks.append(check("disclosed_form", "BLOCKED" if blocked else "FAIL", request=name,
                                     reason="request_sha256 not found among decrypted requests"))
            continue
        pats = form_patterns(model, d["form"], (("logit_ckks", d.get("logit_ckks")), ("logit_plain", d.get("logit_plain"))))
        dense, sparse = pats.prefix_index(False), pats.prefix_index(True)
        hits = []
        for dr, stream, frames in (("c2s", st["app_up"], st["requests"]), ("s2c", st["app_down"], st["responses"])):
            for label, pos in search_decrypted(stream, dense, sparse):
                loc = locate_stream(frames, pos, len(pats.long[label]), is_sparse(pats.long[label]))
                if not loc["explained"]:
                    hits.append(_hit_record(dr, label, pos, loc))
        nat = st.get("native")
        if nat is not None:
            for dr in ("c2s", "s2c"):
                for label, pos in search_long(nat[dr], dense):
                    hits.append(dict(direction="raw " + dr, pattern=label, offset=pos))
        widx = pats.word_index()
        nwords = [0, 0, 0]
        word_hits = []
        for kind, frames in (("request", st["requests"]), ("response", st["responses"])):
            for fr in frames:
                if not fr["complete"]:
                    continue
                for block in range(len(fr["body"]) // LIMB_BYTES):       # block b holds limb b % 3
                    nwords[block % 3] += N
                for label, wi in search_words(fr["body"], widx):
                    word_hits.append(dict(kind=kind, sequence=fr["fields"]["sequence"], word=wi, pattern=label,
                                          **locate_word(wi)))
        chance = word_chance(pats, nwords)
        expected_chance += chance
        resub = [o for o in groups[tuple(float(d["form"][n]) for n in FEATURES)]
                 if o is not d and by_req.get(o.get("request_sha256")) is not None
                 and by_req.get(o.get("request_sha256")) is not st]
        confirmed, dismissed, pending = [], [], []
        for h in word_hits:
            if not resub:
                pending.append(h)
                continue
            again = False
            for o in resub:
                ost = by_req[o["request_sha256"]]
                frames = ost["requests"] if h["kind"] == "request" else ost["responses"]
                for fr in frames:
                    if fr["complete"] and 4 * h["word"] + 4 <= len(fr["body"]):
                        if fr["body"][4 * h["word"]:4 * h["word"] + 4] == pats.word[h["pattern"]]:
                            again = True
            (confirmed if again else dismissed).append(h)
        status = "FAIL" if hits or confirmed else ("FAIL-PENDING" if pending else "PASS")
        form_checks.append(check("disclosed_form", status, request=name, long_patterns=len(pats.long),
                                 word_patterns=len(pats.word), long_hits=hits[:10], word_hits_confirmed=confirmed[:10],
                                 word_hits_pending=pending[:10], word_hits_dismissed_by_resubmission=dismissed[:10],
                                 expected_chance_word_hits=float("%.4g" % chance), resubmissions=len(resub)))
    checks.extend(form_checks)
    if not disclosed:
        checks.append(check("disclosed_forms", "INCOMPLETE",
                            reason="no fictional disclosure: 4-byte encodings of the actual answers not searched"))
    checks.append(positive_control(ctx, dom, disclosed, model))
    return gate(combine(checks), checks, expected_chance_word_hits=float("%.4g" % expected_chance),
                rule=">= 8-byte encodings searched everywhere; 4-byte encodings only as aligned words of the bodies")


def positive_control(ctx, dom, disclosed, model):
    """Plant one long and one 4-byte pattern into a copy of a decrypted request body; the search must find both."""
    body = None
    for st in ctx["connections"]:
        for fr in st["requests"]:
            if fr["complete"] and len(fr["body"]) >= CT_BYTES:
                body = fr["body"]
                break
        if body is not None:
            break
    source = "decrypted request body"
    if body is None:
        rng = random.Random(20261124)
        body = struct.pack("<%dI" % CT_WORDS, *[rng.randrange(Q[(i // N) % 3]) for i in range(CT_WORDS)])
        source = "synthetic uniform body (no decrypted request)"
    pats = form_patterns(model, disclosed[0]["form"]) if disclosed else Patterns()
    if not disclosed:
        add_value_patterns(pats, "BMI", 31.2, model["mean"][2] if model else None, model["std"][2] if model else None)
        add_value_patterns(pats, "Age", 9.0)
    dense = sorted((lb, p) for lb, p in pats.long.items() if not is_sparse(p))
    sparse = sorted((lb, p) for lb, p in pats.long.items() if is_sparse(p))
    label_l, pl = dense[0]
    label_s, ps = sparse[0] if sparse else (None, None)
    label_w, pw = sorted(pats.word.items())[0]
    planted = bytearray(body)
    off_l, off_s, word_w = 1001, 4 * 3001, 2048 + 7          # unaligned dense, aligned sparse, aligned word
    planted[off_l:off_l + len(pl)] = pl
    if ps:
        planted[off_s:off_s + len(ps)] = ps
    planted[4 * word_w:4 * word_w + 4] = pw
    planted = bytes(planted)
    found = search_decrypted(planted, pats.prefix_index(False), pats.prefix_index(True))
    found_l = (label_l, off_l) in found
    found_s = ps is None or (label_s, off_s) in found
    found_w = any(lb == label_w and wi == word_w for lb, wi in search_words(planted, pats.word_index()))
    ok = found_l and found_s and found_w
    return check("positive_control", "PASS" if ok else "FAIL", source=source, dense_pattern=label_l,
                 dense_offset=off_l, dense_found=found_l, sparse_pattern=label_s, sparse_offset=off_s,
                 sparse_found=found_s, word_pattern=label_w, word_index=word_w, word_found=found_w)


def g8_feedback(ctx):
    checks = []
    for st in ctx["connections"]:
        if _nothing_to_check(st):
            continue
        entry = _gate_entry(st, "no_feedback")             # needs the response end time (s2c) and the c2s data
        if entry:
            checks.append(entry)
            continue
        probs = []
        t_end = st["responses"][-1]["t_last"] if st["responses"] else None
        after = []
        if t_end is not None:
            after = [(f, ts, len(data)) for f, ts, data in st["chunks"]["c2s"] if ts is not None and ts > t_end]
            if after:
                probs.append("%d client application bytes after the response" % sum(a[2] for a in after))
            for fno, ts, ct, ln, alert in st["keyed"]["c2s"]:
                if ts > t_end and ct is not None and not (ct == 21 and alert and alert[1] == 0):
                    probs.append("client record type %s (alert %s) after the response" % (ct, alert))
        else:
            probs.append("no response")
        close = any(ct == 21 and alert and alert[1] == 0 for _f, _t, ct, _l, alert in st["keyed"]["c2s"])
        checks.append(check("no_feedback", "FAIL" if probs else "PASS", connection=st["label"], problems=probs,
                            client_close_notify=close, app_chunks_after_response=len(after)))
    return gate(combine(checks, "FAIL" if not ctx["connections"] else "PASS"), checks)


def g9_logs(ctx):
    checks = []
    cap_pairs, blocked_requests = {}, 0
    for st in ctx["connections"]:
        if _blocked(st, ("c2s",)) and not st["responses"]:
            blocked_requests += st["signature"]["request_groups"]
            continue
        frames = st["requests"] if not _blocked(st, ("c2s",)) else []
        down_blocked = _blocked(st, ("s2c",))
        if frames:
            for i, fr in enumerate(frames):
                resp = st["responses"][i] if i < len(st["responses"]) else None
                cap_pairs[fr["fields"]["request_hash"]] = dict(
                    connection=st["label"], sequence=fr["fields"]["sequence"],
                    response=hashlib.sha256(resp["body"]).hexdigest() if resp and resp["complete"] else None,
                    t_response_last=resp["t_last"] if resp else None,
                    response_blocked=down_blocked and resp is None)
        else:
            for fr in st["responses"]:
                if fr["fields"]:
                    cap_pairs[fr["fields"]["request_hash"]] = dict(
                        connection=st["label"], sequence=fr["fields"]["sequence"],
                        response=hashlib.sha256(fr["body"]).hexdigest() if fr["complete"] else None,
                        t_response_last=fr["t_last"], via="response header")
    sl = ctx.get("server_log")
    joined = []
    if sl is None:
        checks.append(check("capture_server_log", "INCOMPLETE", reason="no server.log"))
    else:
        unmatched_log, mism = [], []
        logged = {}
        for r in sl["responses"]:
            logged.setdefault(r["request_sha256"], []).append(r)
        unanswered, unverifiable = [], []
        for rh, cp in sorted(cap_pairs.items(), key=lambda kv: (kv[1]["connection"], kv[1]["sequence"])):
            rows = logged.get(rh, [])
            if cp.get("response_blocked"):
                unverifiable.append(dict(request_sha256=rh[:16], connection=cp["connection"], log_rows=len(rows)))
            elif cp["response"] is None:
                # a request the cloud refused (no response in the capture) must have no response row either
                if rows:
                    mism.append(dict(request_sha256=rh[:16], connection=cp["connection"], log_rows=len(rows),
                                     problem="server.log reports a response that the capture lacks"))
                else:
                    unanswered.append(dict(request_sha256=rh[:16], connection=cp["connection"],
                                           sequence=cp["sequence"]))
            elif len(rows) != 1 or rows[0]["response_payload_sha256"] != cp["response"] or \
                    rows[0]["sequence"] != cp["sequence"]:
                mism.append(dict(request_sha256=rh[:16], connection=cp["connection"], log_rows=len(rows)))
            else:
                joined.append(dict(connection=cp["connection"], sequence=cp["sequence"], request_sha256=rh,
                                   response_payload_sha256=cp["response"], cloud_ns=rows[0]["cloud_ns"],
                                   log_epoch=rnd(rows[0]["epoch"], 6),
                                   t_capture=rnd(cp["t_response_last"], 6)))
        for rh in sorted(logged):
            if rh not in cap_pairs:
                unmatched_log.append(rh[:16])
        status = "PASS"
        if mism:
            status = "FAIL"
        elif unmatched_log:
            status = "BLOCKED" if len(unmatched_log) <= blocked_requests else "FAIL"
        elif unverifiable:
            status = "BLOCKED"
        checks.append(check("capture_server_log", status, matched=len(joined), mismatched=mism,
                            unanswered_requests=unanswered, response_not_decrypted=unverifiable,
                            log_rows_not_in_capture=unmatched_log,
                            server_ready=sl["ready"],
                            keylog_warning_logged=sl["keylog_warning"], connection_stops=sl["connection_stops"],
                            stopped=sl["stopped"], epoch_prefixed_lines=sl["prefixed"], lines=sl["lines"]))
    bp = ctx.get("board_public")
    run_of_request = {}
    if bp is None:
        checks.append(check("capture_board_log", "INCOMPLETE", reason="no board public log"))
    else:
        probs, matched = [], 0
        for run, r in sorted(bp["runs"].items()):
            for rh in sorted(r["requests"]):
                run_of_request[rh] = run
                cp = cap_pairs.get(rh)
                if cp is None:
                    probs.append(dict(run=run, request_sha256=rh[:16], problem="not in the capture"))
                    continue
                pairs = [p for p in r["pairs"] if p[0] == rh]
                if pairs and cp.get("response_blocked"):
                    probs.append(dict(run=run, request_sha256=rh[:16], problem="response not decrypted"))
                    continue
                if pairs and any(p[1] != cp["response"] for p in pairs):
                    probs.append(dict(run=run, request_sha256=rh[:16], problem="response hash differs"))
                    continue
                matched += 1
        board_reqs = set(run_of_request)
        missing = sorted(rh[:16] for rh in cap_pairs if rh not in board_reqs)
        status = "FAIL" if probs or missing else "PASS"
        soft = [p for p in probs if p["problem"] in ("not in the capture", "response not decrypted")]
        if status == "FAIL" and not missing and len(soft) == len(probs) and \
                sum(1 for p in soft if p["problem"] == "not in the capture") <= blocked_requests:
            status = "BLOCKED"
        checks.append(check("capture_board_log", status, matched=matched, problems=probs[:20],
                            capture_requests_missing_in_board_log=missing, board_rows=bp["rows"],
                            unparsed_rows=bp["unparsed"]))
    discl = ctx.get("disclosures") or []
    if not discl:
        checks.append(check("commitment_opens", "INCOMPLETE", reason="no fictional disclosure"))
    else:
        rows, probs = [], []
        for d in discl:
            rh = d.get("request_sha256")
            name = (rh or "unknown")[:16]
            salt = d.get("salt_hex")
            if not d.get("form") or not isinstance(salt, str):
                probs.append(dict(request=name, problem="disclosure lacks the form or the salt"))
                continue
            cands = [d["canonical_json"]] if isinstance(d.get("canonical_json"), str) else canonical_candidates(d["form"])
            if isinstance(d.get("canonical_json"), str):
                try:
                    if json.loads(d["canonical_json"]) != d["form"]:
                        probs.append(dict(request=name, problem="canonical_json differs from the form"))
                except ValueError:
                    probs.append(dict(request=name, problem="canonical_json unparsable"))
            commits = set()
            if bp is not None and rh in run_of_request:
                commits = bp["runs"][run_of_request[rh]]["commitments"]
            want = set(commits)
            if isinstance(d.get("commitment"), str):
                want.add(d["commitment"])
            opened = [c for c in cands if commitment_of(salt, c) in want]
            if bp is not None and not commits:
                probs.append(dict(request=name, problem="no commitment in the board public log for this request"))
            elif len(commits) > 1:
                probs.append(dict(request=name, problem="several commitments for one run"))
            elif not opened:
                probs.append(dict(request=name, problem="commitment does not open"))
            else:
                rows.append(dict(request=name, opened_with=opened[0], public_commitment=sorted(want)[0][:16]))
        checks.append(check("commitment_opens", "FAIL" if probs else "PASS", opened=rows, problems=probs))
    status = combine(checks)
    return gate(status, checks, joined=joined)


def g10_inference(ctx):
    discl = ctx.get("disclosures") or []
    model, schema = ctx.get("model"), ctx.get("form_schema") or {}
    lv = schema.get("levels") or {}
    edges = lv.get("edges_logit")
    band = lv.get("band_abs_logit", MAX_ABS_ERROR)
    tol = (schema.get("verification") or {}).get("max_abs_error", MAX_ABS_ERROR)
    if not discl:
        return gate("INCOMPLETE", [check("disclosed_forms", "INCOMPLETE", reason="no fictional disclosure")])
    if model is None or edges is None:
        return gate("INCOMPLETE", [check("model_and_levels", "INCOMPLETE", reason="model JSON or form_cdc.json missing")])
    checks = []
    for d in discl:
        name = (d.get("request_sha256") or "unknown")[:16]
        probs = []
        if not d.get("form"):
            checks.append(check("inference", "FAIL", request=name, problems=["no form"]))
            continue
        x = [float(d["form"][n]) for n in FEATURES]
        plain = reference_logit(model, x)
        ck = d.get("logit_ckks")
        if not isinstance(ck, (int, float)) or isinstance(ck, bool):
            checks.append(check("inference", "FAIL", request=name, problems=["no logit_ckks in the disclosure"]))
            continue
        err = abs(float(ck) - plain)
        if err > tol:
            probs.append("|logit_ckks - logit_plain| = %.3g > %.3g" % (err, tol))
        if isinstance(d.get("logit_plain"), (int, float)) and abs(float(d["logit_plain"]) - plain) > 1e-12:
            probs.append("disclosed logit_plain differs from the recomputed reference")
        level, boundary = classify_level(float(ck), edges, band)
        disc_level, disc_boundary = d.get("level"), d.get("boundary")
        if disc_level is not None or disc_boundary is not None:
            if disc_level != level or (list(disc_boundary) if disc_boundary else None) != boundary:
                probs.append("disclosed level %s/%s differs from %s/%s" % (disc_level, disc_boundary, level, boundary))
        if d.get("fictional") is False:
            probs.append("disclosure is not marked fictional")
        mz = max(abs((x[f] - model["mean"][f]) / model["std"][f]) for f in range(4))
        if mz > model.get("input_abs_max", 8):
            probs.append("form outside |z| <= 8 (should have been refused locally)")
        checks.append(check("inference", "FAIL" if probs else "PASS", request=name, logit_ckks=rnd(float(ck), 9),
                            logit_plain=rnd(plain, 9), abs_error=float("%.3g" % err), tolerance=tol, level=level,
                            boundary=boundary, problems=probs))
    return gate(combine(checks), checks, edges_logit=edges, band_abs_logit=band)


def g11_observer(ctx):
    obs = ctx.get("observer_path")
    if not obs:
        return gate("SKIP", [check("observer", "SKIP", reason="no observer capture supplied")])
    try:
        ocap = native.read_capture(obs)
    except (OSError, native.CaptureError) as e:
        return gate("FAIL", [check("observer", "FAIL", reason="observer capture unreadable: %s" % e)])
    oconns = native.tcp_connections(ocap, ctx["port"])

    def digest(c):
        return (hashlib.sha256(c["c2s"]).hexdigest(), hashlib.sha256(c["s2c"]).hexdigest())
    seen = {}
    for c in oconns:
        seen.setdefault(digest(c), []).append(c["client"])
    rows, probs = [], []
    mine = set()
    for st in ctx["connections"]:
        nat = st.get("native")
        if nat is None:
            probs.append("%s not reassembled" % st["label"])
            continue
        dg = digest(nat)
        mine.add(dg)
        ok = dg in seen
        rows.append(dict(connection=st["label"], c2s_sha256=dg[0][:16], s2c_sha256=dg[1][:16], in_observer=ok))
        if not ok:
            probs.append("%s payload hashes absent from the observer capture" % st["label"])
    extra = sorted(c["client"] for c in oconns if digest(c) not in mine)
    if extra:
        probs.append("observer saw %d connection(s) the cloud capture lacks" % len(extra))
    return gate("FAIL" if probs else "PASS", [check("observer", "FAIL" if probs else "PASS", connections=rows,
                                                    observer_only=extra, problems=probs,
                                                    observer_connections=len(oconns))])


# ------------------------------------------------------------------ verdict and report
def verdict_of(gates):
    for st in ("FAIL", "FAIL-PENDING", "BLOCKED", "INCOMPLETE"):
        bad = [g for g in GATES if gates.get(g, {}).get("status") == st]
        if bad:
            return "%s:%s" % (st, ",".join(bad))
    return "PASS"


def connection_summary(st):
    out = dict(label=st["label"], client=st["client"], server=st["server"], classification=st["classification"],
               t_syn=rnd(st.get("t_syn"), 6), signature=st["signature"],
               tls=dict(suite=st["suite"], version=st["version"], sni=st["sni"], tickets=st["hs"]["s2c"].count(4),
                        undecrypted_records=st["undecrypted"]), frames=[])
    for fr in st["requests"] + st["responses"]:
        f = fr["fields"] or {}
        item = dict(kind="request" if f.get("kind") == 1 else "response", sequence=f.get("sequence"),
                    features=f.get("features"), rows=f.get("rows"), bytes=fr["end"] - fr["offset"],
                    model=(model_by_id(f.get("model_id", "")) or ("unknown",))[0], model_id=f.get("model_id"),
                    key_id=f.get("key_id"), generation="%016x" % f["generation"] if f.get("generation") else None,
                    request_sha256=f.get("request_hash"), t_first=rnd(fr.get("t_first"), 6),
                    t_last=rnd(fr.get("t_last"), 6))
        if item["kind"] == "response" and fr["complete"]:
            item["response_payload_sha256"] = hashlib.sha256(fr["body"]).hexdigest()
        out["frames"].append(item)
    recs = []
    for d in ("c2s", "s2c"):
        for fno, ts, typ, ln, enc in st["nokey"][d]:
            recs.append([d, rnd(ts, 6), ln, typ, 1 if enc else 0])
    recs.sort(key=lambda r: (r[1], r[0]))
    out["records"] = recs
    out["t_first"] = rnd(min((r[1] for r in recs), default=None), 6) if recs else None
    out["t_last"] = rnd(max((r[1] for r in recs), default=None), 6) if recs else None
    return out


def theta(joined):
    """Clock offset estimate capture - server.log from the matched responses (median), for report.html."""
    diffs = sorted(j["t_capture"] - j["log_epoch"] for j in joined if j.get("t_capture") is not None
                   and j.get("log_epoch") is not None)
    if not diffs:
        return None
    mid = len(diffs) // 2
    med = diffs[mid] if len(diffs) % 2 else (diffs[mid - 1] + diffs[mid]) / 2
    return dict(server_log=rnd(med, 6), spread=rnd(diffs[-1] - diffs[0], 6), samples=len(diffs))


def describe_input(path, base):
    if not path:
        return None
    if not os.path.exists(path):
        return dict(path=_rel(path, base), missing=True)
    if os.path.isdir(path):
        files = sorted(os.listdir(path))
        return dict(path=_rel(path, base), directory=True, files=len(files),
                    sha256=hashlib.sha256("".join("%s %s\n" % (sha256_file(os.path.join(path, f)), f)
                                                  for f in files if os.path.isfile(os.path.join(path, f)))
                                          .encode()).hexdigest())
    return dict(path=_rel(path, base), sha256=sha256_file(path), bytes=os.path.getsize(path))


REPO_ROOT = os.path.dirname(os.path.dirname(HERE))


def plain_path(path):
    """Windows: the path without the extended-length prefixes '\\\\?\\C:\\...' and '\\\\?\\UNC\\host\\share\\...'
    (also '\\\\.\\'), which os.path.realpath keeps and tshark cannot open, so that a string comparison sees the
    drive spelling. Other prefixed forms (GLOBALROOT, Volume{GUID}) stay as they are; _anchor matches them by file
    identity."""
    if os.name != "nt":
        return path
    p = os.fspath(path).replace("/", "\\")
    head = p[:8].upper()
    if head in ("\\\\?\\UNC\\", "\\\\.\\UNC\\"):
        return "\\\\" + p[8:]
    if head[:4] in ("\\\\?\\", "\\\\.\\") and len(p) >= 6 and p[4].isalpha() and p[5] == ":":
        return p[4:]
    return p


def _resolved(path):
    """Absolute path with links resolved and the extended-length prefix removed (on-disk spelling of existing parts)."""
    return plain_path(os.path.realpath(plain_path(path)))


def _canon(path):
    """Comparable form of a path: _resolved and, on Windows, the case folded (NTFS paths are case-insensitive, and
    ntpath.commonpath keeps the case of its first argument)."""
    return os.path.normcase(_resolved(path))


def _anchor(path, root):
    """The components of path below root ([] for root itself), or None when path is neither root nor inside it.
    First a string comparison of the canonical forms; then, for spellings that no string rule folds (an admin share
    such as '\\\\localhost\\C$\\...', a GLOBALROOT or Volume{GUID} path, a substituted drive, a bind mount), each
    existing ancestor of path is compared with root by file identity (os.path.samefile: volume and file index)."""
    try:
        p, r = _resolved(path), _resolved(root)
    except (ValueError, OSError):
        return None
    try:
        if os.path.commonpath([os.path.normcase(p), os.path.normcase(r)]) == os.path.normcase(r):
            rest = os.path.relpath(p, r)
            return [] if rest == os.curdir else rest.split(os.sep)
    except ValueError:                       # different drives or shares: only the identity walk can tell
        pass
    if not os.path.isdir(root):
        return None
    cur, rest = p, []
    while True:
        try:
            if os.path.isdir(cur) and os.path.samefile(cur, root):
                return list(reversed(rest))
        except OSError:
            pass
        parent, name = os.path.split(cur)
        if not name or parent == cur:
            return None
        rest.append(name)
        cur = parent


def _under(path, root):
    return _anchor(path, root) is not None


def inside_repository(path):
    """True when path is the repository root or lies inside it, however it is spelled: drive letter and folder case,
    links, the '\\\\?\\' and '\\\\?\\UNC\\' prefixes, or another name of the same folder (admin share, GLOBALROOT)."""
    return _under(path, REPO_ROOT)


def _rel(path, base):
    """Report path: relative to the run directory, else 'repository:<path in the repository>', else the file name
    (the report never records where the run or the repository lies on the verifying computer). realpath gives the
    on-disk spelling on Windows, so the report does not depend on how the operator typed the path."""
    rest = _anchor(path, base)
    if rest is not None:
        return "/".join(rest) or "."
    rest = _anchor(path, REPO_ROOT)
    if rest is not None:
        return "repository:" + "/".join(rest)
    return os.path.basename(path)


def build_context(opts, tshark):
    port = int(opts.get("port") or PORT)
    capture = opts["capture"]
    base = opts.get("base") or os.path.dirname(os.path.abspath(capture))
    keylog = opts.get("keylog")
    if keylog and not os.path.isfile(keylog):
        keylog = None
    model_path = opts.get("model") or os.path.join(HERE, "..", "model", "cdc-diabetes-lr4-v1.json")
    model = load_json(model_path) if os.path.isfile(model_path) else None
    form_model_hex = sha256_file(model_path) if model is not None else FORM_MODEL_HEX
    schema_path = opts.get("form_schema") or os.path.join(os.path.dirname(model_path), "form_cdc.json")
    schema = load_json(schema_path) if os.path.isfile(schema_path) else None
    ctx = dict(port=port, forms_only=opts.get("forms_only", True), sni=opts.get("sni") or SNI,
               board_ip=opts.get("board_ip"), model=model, form_model_hex=form_model_hex, form_schema=schema,
               observer_path=opts.get("observer"), stats_path=opts.get("stats"))
    ctx["inputs"] = dict(capture=describe_input(capture, base), keylog=describe_input(keylog, base),
                         server_log=describe_input(opts.get("server_log"), base),
                         board_public=describe_input(opts.get("board_public"), base),
                         disclosure=describe_input(opts.get("disclosure"), base),
                         model=describe_input(model_path if model is not None else None, base),
                         form_schema=describe_input(schema_path if schema is not None else None, base),
                         observer=describe_input(opts.get("observer"), base),
                         stats=describe_input(opts.get("stats"), base),
                         seal_order=describe_input(opts.get("seal_order"), base),
                         rehearsal_summary=describe_input(opts.get("rehearsal_summary"), base))
    ctx["rehearsal_summary"] = bool(opts.get("rehearsal_summary") and os.path.isfile(opts["rehearsal_summary"]))
    seal_path = opts.get("seal")
    ctx["seal"] = None
    if seal_path and os.path.isfile(seal_path):
        with open(seal_path, encoding="ascii", errors="replace") as f:
            tok = (f.read().split() or [""])[0].lower()
        ctx["seal"] = tok if is_hex64(tok) else "unreadable"
    ctx["seal_order"] = read_seal_order(opts.get("seal_order"))
    ctx["pins"] = read_pins(opts.get("pins"))
    ctx["keys"] = native.read_keylog(keylog) if keylog else {}
    ctx["keylog_supplied"] = keylog is not None
    ctx["server_log"] = parse_server_log(opts["server_log"]) if opts.get("server_log") and os.path.isfile(
        opts["server_log"]) else None
    ctx["board_public"] = parse_board_public(opts["board_public"]) if opts.get("board_public") and os.path.isfile(
        opts["board_public"]) else None
    ctx["disclosures"], ctx["disclosure_problems"] = parse_disclosures(opts["disclosure"]) if opts.get(
        "disclosure") and os.path.exists(opts["disclosure"]) else ([], [])
    col = collect_capture(capture, keylog, port, tshark, quick_port=opts.get("quick_port"))
    ctx.update(cap=col["cap"], connections=col["connections"], nokey_rows=col["nokey_rows"], lost=col.get("lost", []),
               conv=col.get("conv", []))
    ctx["native_decrypt"] = "available" if native.cryptography_available() else "cryptography package not installed"
    for st in ctx["connections"]:
        st["keys_missing"] = keylog is None or not ctx["keys"].get(st["client_random"] or "")
        analyse_connection(st, form_model_hex)
        st["signature"] = record_signature(st)
        if ctx["native_decrypt"] == "available" and st.get("native") is not None and not st["keys_missing"]:
            st["native_check"] = native_crosscheck(st, ctx["keys"])
    return ctx


def native_crosscheck(st, keys):
    nat = st["native"]
    try:
        r = native.decode_connection(nat["c2s"], nat["s2c"], keys)
    except native.TLSDecodeError as e:
        return dict(status="FAIL", detail="%s %s record %s" % (e.code, e.direction, e.index))
    except Exception as e:                     # an unexpected structure is reported, never hidden
        return dict(status="FAIL", detail="%s: %s" % (type(e).__name__, e))
    same = r["app_c2s"] == st["app_up"] and r["app_s2c"] == st["app_down"]
    certs = r["certs"].get("s2c") == st["certs"].get("s2c") and r["certs"].get("c2s") == st["certs"].get("c2s")
    if not same:
        return dict(status="FAIL", detail="decrypted streams differ from tshark")
    if not certs:
        return dict(status="FAIL", detail="leaf certificate hashes differ from tshark")
    return dict(status="PASS", detail="identical decrypted streams and leaf certificates", tickets=r["new_session_tickets"])


def run_gates(ctx):
    gates = {}
    for name, fn in (("G0", g0_capture), ("G1", g1_inventory), ("G2", g2_tls), ("G3", g3_framing), ("G4", g4_binding),
                     ("G5", g5_affine), ("G6", g6_statistics), ("G7", g7_plaintext), ("G8", g8_feedback),
                     ("G9", g9_logs), ("G10", g10_inference), ("G11", g11_observer)):
        if fn is None:
            continue
        g = fn(ctx)
        g["title"] = TITLES[name]
        gates[name] = g
    return gates


def make_report(ctx, gates, tool):
    conns = [connection_summary(st) for st in ctx["connections"]]
    neg = ctx.get("negative_control", [])
    g9 = gates.get("G9", {}).get("details", {})
    report = dict(schema=SCHEMA, tool=tool, port=ctx["port"], inputs={k: v for k, v in ctx["inputs"].items() if v},
                  pins=ctx.get("pins"), connections=conns, gates=gates, verdict=verdict_of(gates),
                  required_gates=list(REQUIRED),
                  evidence_pass=all(gates.get(g, {}).get("status") == "PASS" for g in REQUIRED),
                  negative_control=dict(detected=gates.get("G6", {}).get("status") == "FAIL" and bool(neg),
                                        gate="G6", requests=neg),
                  summary=dict(connections=len(conns), forms=sum(1 for c in conns if c["classification"] == "form"),
                               requests=sum(1 for c in conns for f in c["frames"] if f["kind"] == "request"),
                               responses=sum(1 for c in conns for f in c["frames"] if f["kind"] == "response")),
                  theta=theta(g9.get("joined", [])), pc_rehearsal=pc_rehearsal(ctx),
                  disclosures=len(ctx.get("disclosures") or []),
                  disclosure_problems=ctx.get("disclosure_problems") or [])
    return report


def dumps_report(report):
    return json.dumps(report, sort_keys=True, indent=1, ensure_ascii=True) + "\n"


def verify_inputs(opts):
    """Run every gate over explicit inputs (see main). Returns the report dict."""
    tshark = Tshark(opts.get("tshark"))
    try:
        ctx = build_context(opts, tshark)
        gates = run_gates(ctx)
        tool = dict(verifier="efhe_verify.py", schema=SCHEMA, tshark=tshark.version, native_decrypt=ctx["native_decrypt"])
        return make_report(ctx, gates, tool)
    finally:
        tshark.close()


def discover(run_dir):
    """Default file names of an evidence run directory."""
    def first(*names):
        for n in names:
            p = os.path.join(run_dir, n)
            if os.path.exists(p):
                return p
        return None
    return dict(capture=first("capture.pcapng", "capture.pcap"), keylog=first("tls_keys.log"),
                server_log=first("server.log"), board_public=first("board_public.jsonl", "public.jsonl"),
                disclosure=first("disclosure", "disclosures", "disclosure.json"), pins=run_dir,
                observer=first("observer.pcapng", "observer.pcap"), seal=first("capture.sha256"),
                seal_order=first(SEAL_ORDER_FILE), rehearsal_summary=first(REHEARSAL_SUMMARY),
                stats=first("capture_stats.txt", "dumpcap_stats.txt", "dumpcap.log", "tcpdump_stats.txt"))


def verify_run(run_dir, opts=None):
    """Verify an evidence run directory. opts overrides the discovered inputs (same keys as verify_inputs)."""
    run_dir = plain_path(run_dir)
    o = discover(run_dir)
    for k, v in (opts or {}).items():
        if v is not None:
            o[k] = v
    if not o.get("capture"):
        raise VerifyError("CAPTURE_NOT_FOUND in %s" % run_dir)
    o["base"] = run_dir
    return verify_inputs(o)


def quick_session(capture, keylog, board_port, port=PORT, tshark_path=None, model=None, pins=None):
    """Decode one session (the TCP connection from board_port) of a possibly still growing capture, for the console.
    Never needs the seal. pins: optional directory or file with the configured pins (gate G2).
    Returns dict(ok, complete, reason, board_port, connection, gates, tshark)."""
    tshark = Tshark(tshark_path)
    try:
        opts = dict(capture=capture, keylog=keylog, port=port, quick_port=int(board_port), model=model, forms_only=True,
                    pins=pins)
        ctx = build_context(opts, tshark)
        if not ctx["connections"]:
            return dict(ok=False, complete=False, reason="session not in the capture yet", board_port=board_port)
        st = ctx["connections"][-1]
        sig = st["signature"]
        complete = bool(st.get("native") and (st["native"]["fin_c2s"] or sig["client_close_notify"]))
        gates = {}
        for name, fn in (("G2", g2_tls), ("G3", g3_framing), ("G4", g4_binding), ("G5", g5_affine),
                         ("G6", g6_statistics), ("G8", g8_feedback)):
            g = fn(ctx)
            g["title"] = TITLES[name]
            gates[name] = dict(status=g["status"], title=g["title"], checks=g["checks"])
        summ = connection_summary(st)
        ok = complete and all(g["status"] in ("PASS", "INCOMPLETE") for g in gates.values())
        return dict(ok=ok, complete=complete, reason=None if complete else "session not closed yet",
                    board_port=board_port, connection=summ, gates=gates, tshark=tshark.version)
    finally:
        tshark.close()


# ------------------------------------------------------------------ selftest (synthetic frames, no tshark)
class _Synth:
    """Synthetic connections in the structure that collect_capture() produces."""

    def __init__(self, seed):
        self.rng = random.Random(seed)

    def uniform_body(self, nct=4):
        r = self.rng
        return struct.pack("<%dI" % (nct * CT_WORDS), *[r.randrange(Q[(i // N) % 3]) for i in range(nct * CT_WORDS)])

    def connection(self, label, body, form_model=FORM_MODEL_HEX, ems=None, key=None, gen=None, t0=100.0,
                   session_override=None, mutate=None, extra_after=None, profile=PROFILE, rows=1):
        r = self.rng
        ems = ems if ems is not None else bytes(r.randrange(256) for _ in range(48))
        session = session_override or native.tls13_exporter(ems, EXPORTER_LABEL, PROFILE, 32)
        key = key or bytes(r.randrange(256) for _ in range(32))
        gen = gen or (r.getrandbits(64) | 1)
        model = bytes.fromhex(form_model)
        nct = len(body) // CT_BYTES
        z = bytes(32)
        h0 = encode_header(1, nct, rows, nct * CT_BYTES, gen, 1, (profile, model, key, z, session, z))
        rid = bytes.fromhex(request_id_of(h0))
        h1 = encode_header(1, nct, rows, nct * CT_BYTES, gen, 1, (profile, model, key, rid, session, z))
        rh = bytes.fromhex(request_hash_of(h1, body))
        req_h = encode_header(1, nct, rows, nct * CT_BYTES, gen, 1, (profile, model, key, rid, session, rh))
        m = model_by_id(form_model)
        resp_b = affine(body, nct, m[3], m[4]) if m else bytes(CT_BYTES)
        resp_h = bytearray(req_h)
        resp_h[10] = 2
        struct.pack_into("<Q", resp_h, 32, CT_BYTES)
        parts = dict(req_h=req_h, body=body, resp_h=bytes(resp_h), resp_b=resp_b)
        if mutate:
            mutate(parts)
        up = parts["req_h"] + parts["body"]
        down = parts["resp_h"] + parts["resp_b"]
        chunks_up = [(1, t0 + 1.0, up[:HDR])] + [(2 + i, t0 + 1.0 + 0.001 * i, up[o:o + 16384])
                                                  for i, o in enumerate(range(HDR, len(up), 16384))]
        chunks_dn = [(100 + i, t0 + 2.0 + 0.001 * i, down[o:o + 16384]) for i, o in enumerate(range(0, len(down), 16384))]
        if extra_after:
            chunks_up.append((500, t0 + 3.0, extra_after))
        keyed_c2s = [(600, t0 + 4.0, 21, REC_CLOSE, (1, 0))]
        st = dict(label=label, client="192.0.2.10:%d" % (40000 + len(label)), server="192.0.2.20:18443",
                  client_port=40000, frames=[], nokey={"c2s": [], "s2c": []}, keyed={"c2s": keyed_c2s, "s2c": []},
                  chunks={"c2s": chunks_up, "s2c": chunks_dn}, hs={"c2s": [11, 15, 20], "s2c": [8, 13, 11, 15, 20, 4, 4]},
                  certs={}, ext_types=set(), client_random="%064x" % r.getrandbits(256), sni=SNI, version="0x0304", suite="0x1302", undecrypted={"c2s": 0, "s2c": 0},
                  hellos={"c2s": 1, "s2c": 1}, native=None, keys_missing=False)
        st["signature"] = dict(request_groups=1, classification="form", problems=[], client_close_notify=True)
        st["_ems"] = ems
        return st


def _selftest_ctx(conns, disclosures=(), server_log=True, board=True):
    model = None
    mp = os.path.join(HERE, "..", "model", "cdc-diabetes-lr4-v1.json")
    if os.path.isfile(mp):
        model = load_json(mp)
    else:
        model = dict(features=list(FEATURES), mean=[2.51113889264315, 0.42801392080010814, 28.384094697481643,
                                                    8.028776411226742],
                     std=[1.0663602589485628, 0.49479086935939603, 6.5980653663061455, 3.0509583054181006],
                     weights=[0.6979414237022599, 0.4457087610493119, 0.5057933085899435, 0.5518502220124103],
                     bias=-0.5780604525414647, weights_int=list(CATALOGUE[3][3]), bias_int=CATALOGUE[3][4],
                     input_abs_max=8)
    sp = os.path.join(HERE, "..", "model", "form_cdc.json")
    schema = load_json(sp) if os.path.isfile(sp) else dict(
        levels=dict(edges_logit=[-0.8472978603872036, 0.0, 0.8472978603872036], band_abs_logit=0.002),
        verification=dict(max_abs_error=0.002))
    keys = {}
    for st in conns:
        keys[st["client_random"]] = dict((lb, b"\x01" * 48) for lb in native.KEYLOG_LABELS)
        keys[st["client_random"]]["EXPORTER_SECRET"] = st["_ems"]
        analyse_connection(st)
    ctx = dict(port=PORT, forms_only=True, sni=SNI, model=model, form_model_hex=FORM_MODEL_HEX, form_schema=schema,
               connections=conns, keys=keys, disclosures=list(disclosures), observer_path=None)
    if server_log:
        resp = []
        for st in conns:
            for i, fr in enumerate(st["requests"]):
                if i < len(st["responses"]):
                    resp.append(dict(epoch=200.0, sequence=fr["fields"]["sequence"],
                                     request_sha256=fr["fields"]["request_hash"],
                                     response_payload_sha256=hashlib.sha256(st["responses"][i]["body"]).hexdigest(),
                                     cloud_ns=1000))
        ctx["server_log"] = dict(lines=len(resp), responses=resp, ready=True, stopped=None, connection_stops=[],
                                 keylog_warning=True, prefixed=len(resp))
    if board:
        runs = {}
        for i, st in enumerate(conns):
            for j, fr in enumerate(st["requests"]):
                rh = fr["fields"]["request_hash"]
                d = [x for x in disclosures if x.get("request_sha256") == rh]
                com = set()
                if d:
                    com.add(commitment_of(d[0]["salt_hex"], d[0]["canonical_json"]))
                resp = hashlib.sha256(st["responses"][j]["body"]).hexdigest() if j < len(st["responses"]) else None
                runs["r%d" % i] = dict(run="r%d" % i, commitments=com, pairs={(rh, resp)} if resp else set(),
                                       requests={rh}, responses={resp} if resp else set(), events=[])
        ctx["board_public"] = dict(rows=len(runs), unparsed=0, runs=runs)
    return ctx


def _disclose(st, form, logit_shift=1e-5, level_override=None, salt=None, model=None):
    fr = st["requests"][0] if st.get("requests") else None
    rh = fr["fields"]["request_hash"] if fr else None
    if rh is None:
        up, _p = split_frames(b"".join(c[2] for c in st["chunks"]["c2s"]), 1)
        rh = up[0]["fields"]["request_hash"]
    canonical = json.dumps(form, sort_keys=True, separators=(",", ":"))
    salt = salt or hashlib.sha256(rh.encode()).hexdigest()
    m = model
    plain = reference_logit(m, [float(form[n]) for n in FEATURES])
    ck = plain + logit_shift
    lv, bd = classify_level(ck, [-0.8472978603872036, 0.0, 0.8472978603872036], 0.002)
    if level_override is not None:
        lv = level_override
    return dict(file="synthetic", request_sha256=rh, form=form, canonical_json=canonical, salt_hex=salt,
                commitment=commitment_of(salt, canonical), logit_ckks=ck, logit_plain=plain, level=lv, boundary=bd,
                run=None, fictional=True)


def selftest(verbose=True):
    """Synthetic negative cases: each must be caught by its gate while the clean baseline passes G3-G10."""
    syn = _Synth(20261124)
    model = _selftest_ctx([], board=False, server_log=False)["model"]
    form_m = {"GenHlth": 3, "HighBP": 1, "BMI": 31.2, "Age": 9}
    form_k = {"GenHlth": 4, "HighBP": 1, "BMI": 26.0, "Age": 4}
    frame_gates = ("G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10")

    def run(conns, disc_forms, expect, label, **kw):
        for st in conns:
            analyse_connection(st)
        discl = [_disclose(st, f, model=model, **kw) for st, f in zip(conns, disc_forms) if f is not None]
        ctx = _selftest_ctx(conns, discl)
        if label == "commitment_does_not_open":
            discl[0]["salt_hex"] = "0" * 64
        if label == "server_log_mismatch":
            ctx["server_log"]["responses"][0]["response_payload_sha256"] = "f" * 64
        ctx["disclosures"] = discl
        results = {}
        for name, fn in (("G3", g3_framing), ("G4", g4_binding), ("G5", g5_affine), ("G6", g6_statistics),
                         ("G7", g7_plaintext), ("G8", g8_feedback), ("G9", g9_logs), ("G10", g10_inference)):
            results[name] = fn(ctx)["status"]
        failing = sorted((g for g in frame_gates if results[g] in ("FAIL", "FAIL-PENDING")), key=GATES.index)
        if expect == "PASS":
            ok = all(results[g] == "PASS" for g in frame_gates)
        elif isinstance(expect, tuple):
            ok = results[expect[0]] == expect[1] and all(results[g] == "PASS" for g in frame_gates if g != expect[0])
        else:
            ok = results[expect] == "FAIL"
        if verbose:
            print("  %-34s expect %-22s got %s %s" % (label, expect if isinstance(expect, str) else "=".join(expect),
                                                      ",".join("%s=%s" % (g, results[g]) for g in failing) or "PASS",
                                                      "ok" if ok else "MISMATCH"))
        return ok

    cases = []
    b1 = syn.uniform_body()
    cases.append(run([syn.connection("base", b1), syn.connection("base2", syn.uniform_body())], [form_m, form_k],
                     "PASS", "uniform_baseline"))
    cases.append(run([syn.connection("zero", bytes(4 * CT_BYTES))], [form_m], "G6", "zero_body"))

    def plain_encoding():
        out = bytearray(4 * CT_BYTES)
        x = [3.0, 1.0, 31.2, 9.0]
        for f in range(4):
            zf = (x[f] - model["mean"][f]) / model["std"][f]
            for i in range(N):
                v = int(round(zf * (1 << 30) * math.cos(math.pi * (2 * i + 1) / (4 * N)) * 2 / N)) if i else int(
                    round(zf * (1 << 30)))
                for l, q in enumerate(Q):
                    struct.pack_into("<I", out, 4 * (f * CT_WORDS + l * N + i), v % q)
        return bytes(out)
    cases.append(run([syn.connection("plain", plain_encoding())], [form_m], "G6", "plain_encoding"))

    def inject_float(parts):
        b = bytearray(parts["body"])
        b[4 * 100 + 2:4 * 100 + 10] = struct.pack("<d", 31.2)
        parts["body"] = bytes(b)
        _refresh(parts)
    cases.append(run([syn.connection("float", syn.uniform_body(), mutate=inject_float)], [form_m], "G7",
                     "injected_float64"))

    def inject_int64(parts):
        b = bytearray(parts["body"])
        b[4 * 2000:4 * 2002] = struct.pack("<q", 9)
        parts["body"] = bytes(b)
        _refresh(parts)
    cases.append(run([syn.connection("int64", syn.uniform_body(), mutate=inject_int64)], [form_m], "G7",
                     "injected_aligned_int64"))

    def word_q0(parts):
        b = bytearray(parts["body"])
        struct.pack_into("<I", b, 0, Q[0])
        parts["body"] = bytes(b)
        _refresh(parts)
    cases.append(run([syn.connection("q0", syn.uniform_body(), mutate=word_q0)], [form_m], "G6", "word_equals_q0"))

    def bad_hash(parts):
        h = bytearray(parts["req_h"])
        h[224] ^= 1
        parts["req_h"] = bytes(h)
        r = bytearray(parts["resp_h"])
        r[224] ^= 1
        parts["resp_h"] = bytes(r)
    cases.append(run([syn.connection("hash", syn.uniform_body(), mutate=bad_hash)], [None], "G4", "bad_request_hash"))

    def flip_resp(parts):
        b = bytearray(parts["resp_b"])
        w = struct.unpack_from("<I", b, 4 * 5)[0]
        struct.pack_into("<I", b, 4 * 5, w ^ 2 if w ^ 2 < Q[0] else w ^ 1)
        parts["resp_b"] = bytes(b)
    cases.append(run([syn.connection("flip", syn.uniform_body(), mutate=flip_resp)], [form_m], "G5",
                     "flipped_response_bit"))
    other = native.tls13_exporter(b"\x02" * 48, EXPORTER_LABEL, PROFILE, 32)
    cases.append(run([syn.connection("exporter", syn.uniform_body(), session_override=other)], [form_m], "G4",
                     "wrong_exporter"))
    cases.append(run([syn.connection("after", syn.uniform_body(), extra_after=b"\x00" * 8)], [form_m], "G8",
                     "data_after_response"))

    def repeated_c1():
        a = syn.uniform_body()
        b = bytearray(syn.uniform_body())
        b[4 * 3 * N:4 * 6 * N] = a[4 * 3 * N:4 * 6 * N]
        return a, bytes(b)
    a, b = repeated_c1()
    cases.append(run([syn.connection("c1a", a), syn.connection("c1b", b)], [form_m, form_k], "G6", "repeated_c1"))
    key = b"\x11" * 32
    cases.append(run([syn.connection("ka", syn.uniform_body(), key=key), syn.connection("kb", syn.uniform_body(),
                                                                                       key=key)],
                     [form_m, form_k], "G6", "key_id_reused"))

    def bad_rid(parts):
        h = bytearray(parts["req_h"])
        h[160] ^= 1
        parts["req_h"] = bytes(h)
        _refresh(parts, keep_rid=True)
    cases.append(run([syn.connection("rid", syn.uniform_body(), mutate=bad_rid)], [None], "G4", "bad_request_id"))
    cases.append(run([syn.connection("profile", syn.uniform_body(), profile=b"\x05" * 32)], [None], "G3",
                     "wrong_profile"))
    cases.append(run([syn.connection("model", syn.uniform_body(), form_model="ab" * 32)], [None], "G3", "unknown_model"))

    def truncate(parts):
        parts["body"] = parts["body"][:-100]
    cases.append(run([syn.connection("trunc", syn.uniform_body(), mutate=truncate)], [None], "G3", "truncated_body"))

    def resp_seq(parts):
        r = bytearray(parts["resp_h"])
        struct.pack_into("<Q", r, 48, 2)
        parts["resp_h"] = bytes(r)
    cases.append(run([syn.connection("rseq", syn.uniform_body(), mutate=resp_seq)], [form_m], "G4",
                     "response_binding"))

    def plant_age(parts):
        b = bytearray(parts["body"])
        struct.pack_into("<i", b, 4 * 777, 9)
        parts["body"] = bytes(b)
        _refresh(parts)
    cases.append(run([syn.connection("word", syn.uniform_body(), mutate=plant_age)], [form_m], ("G7", "FAIL-PENDING"),
                     "word_hit_pending"))
    cases.append(run([syn.connection("word1", syn.uniform_body(), mutate=plant_age),
                      syn.connection("word2", syn.uniform_body())], [form_m, form_m], "PASS",
                     "word_hit_dismissed_by_resubmission"))
    cases.append(run([syn.connection("word3", syn.uniform_body(), mutate=plant_age),
                      syn.connection("word4", syn.uniform_body(), mutate=plant_age)], [form_m, form_m], "G7",
                     "word_hit_confirmed_by_resubmission"))
    cases.append(run([syn.connection("slog", syn.uniform_body())], [form_m], "G9", "server_log_mismatch"))
    cases.append(run([syn.connection("commit", syn.uniform_body())], [form_m], "G9", "commitment_does_not_open"))
    cases.append(run([syn.connection("error", syn.uniform_body())], [form_m], "G10", "logit_error_too_large",
                     logit_shift=3e-3))
    cases.append(run([syn.connection("level", syn.uniform_body())], [form_m], "G10", "level_inconsistent",
                     level_override=1))
    cases.append(run([syn.connection("boundary", syn.uniform_body())], [form_k], "PASS", "boundary_form_K"))
    ok = all(cases)
    return ok, len(cases)


def _refresh(parts, keep_rid=False):
    """Recompute request_id (unless keep_rid), request_hash and the response of mutated synthetic parts."""
    h = bytearray(parts["req_h"])
    if not keep_rid:
        h[160:192] = bytes.fromhex(request_id_of(bytes(h)))
    h[224:256] = bytes.fromhex(request_hash_of(bytes(h), parts["body"]))
    parts["req_h"] = bytes(h)
    m = model_by_id(h[96:128].hex())
    if m and len(parts["body"]) % CT_BYTES == 0:
        parts["resp_b"] = affine(parts["body"], len(parts["body"]) // CT_BYTES, m[3], m[4])
    r = bytearray(h)
    r[10] = 2
    struct.pack_into("<Q", r, 32, CT_BYTES)
    parts["resp_h"] = bytes(r)


def utf8_stdio():
    """Print UTF-8 even when stdout is a pipe on a Windows code page (the console and scripts capture the output)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# ------------------------------------------------------------------ CLI
def print_summary(report, out=sys.stdout):
    for g in GATES:
        d = report["gates"].get(g)
        if d:
            out.write("%-4s %-12s %s\n" % (g, d["status"], d["title"]))
    reh = report.get("pc_rehearsal") or {}
    if reh.get("detected"):
        out.write("EFHE_PC_REHEARSAL reasons=%s（PC 演練，非 KV260 板上實測；報告以此標示）\n" % ",".join(reh["reasons"]))
    out.write("EFHE_VERIFY_VERDICT verdict=%s connections=%d forms=%d\n" % (
        report["verdict"], report["summary"]["connections"], report["summary"]["forms"]))


def main(argv=None):
    utf8_stdio()
    ap = argparse.ArgumentParser(description="EdgeFHE demo evidence verifier (gates G0-G11).")
    ap.add_argument("--run", help="evidence run directory (default file names)")
    ap.add_argument("--capture")
    ap.add_argument("--keylog")
    ap.add_argument("--server-log")
    ap.add_argument("--board-public")
    ap.add_argument("--disclosure")
    ap.add_argument("--model", help="model JSON (default demo/model/cdc-diabetes-lr4-v1.json)")
    ap.add_argument("--form-schema", help="form schema (default: form_cdc.json next to the model)")
    ap.add_argument("--pins", help="directory with server.pin and client.pin, or a pins file")
    ap.add_argument("--observer", help="optional observer capture (gate G11)")
    ap.add_argument("--seal", help="capture.sha256 seal record")
    ap.add_argument("--seal-order", help="seal_order.json (efhe_seal.py writes it when the key log was used first)")
    ap.add_argument("--stats", help="dumpcap or tcpdump statistics text (dropped packets)")
    ap.add_argument("--rehearsal-summary", help="rehearsal_summary.txt of a PC rehearsal (marks the report PC 演練)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--board-ip")
    ap.add_argument("--sni", default=SNI)
    ap.add_argument("--allow-other-connections", action="store_true",
                    help="accept well-formed connections that are not single forms (synthetic gate runs)")
    ap.add_argument("--tshark")
    ap.add_argument("--out", help="write verify_report.json here")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        print("EdgeFHE verifier selftest (synthetic frames, no tshark):")
        ok, n = selftest()
        if ok:
            print("EFHE_VERIFIER_SELFTEST_PASS cases=%d" % n)
            return 0
        print("EFHE_VERIFIER_SELFTEST_FAIL cases=%d" % n)
        return 1
    opts = dict(capture=a.capture, keylog=a.keylog, server_log=a.server_log, board_public=a.board_public,
                disclosure=a.disclosure, model=a.model, form_schema=a.form_schema, pins=a.pins, observer=a.observer,
                seal=a.seal, seal_order=a.seal_order, stats=a.stats, rehearsal_summary=a.rehearsal_summary,
                port=a.port, board_ip=a.board_ip, sni=a.sni,
                tshark=a.tshark, forms_only=not a.allow_other_connections)
    try:
        if a.run:
            report = verify_run(a.run, dict((k, v) for k, v in opts.items() if v is not None))
        else:
            if not a.capture:
                ap.error("--capture or --run is required (or --selftest)")
            opts["base"] = os.path.dirname(os.path.abspath(a.out)) if a.out else os.getcwd()
            report = verify_inputs(opts)
    except (VerifyError, native.CaptureError, OSError) as e:
        print("EFHE_VERIFY_ERROR %s" % e)
        return 2
    text = dumps_report(report)
    out = a.out or (os.path.join(a.run, "verify_report.json") if a.run else None)
    if out:
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    print_summary(report)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
