"""Unit tests of the evidence toolchain (efhe_verify.py, tls13_native.py, report_html.py). No tshark, no capture.

Covered: the embedded public catalogue against model_catalog.hpp and demo/model/catalog_line.hpp; the TLS 1.3 key
schedule against RFC 8448 and the exporter against OpenSSL (Linux); the wire.hpp header checks in their order;
request_id and request_hash; the cloud affine (cloud.cpp:24-43); the limb statistics; the plaintext search rules
(dense, sparse aligned, 4-byte aligned words, explained header fields, chance rate); the risk levels of the
form_cdc.json presets; the log, disclosure, commitment and pin parsers; pcapng and pcap reading with interface
statistics and TCP reassembly; the no-key record signature; a deterministic report.html with text of 18 px or more
and the cloud-key wording; the case-insensitive repository guards (efhe_verify, efhe_seal, run_mock_chain); the
seal-order record of efhe_seal.py and gate G0 check seal_order.

usage: python -B demo/evidence/tests/test_verify_unit.py      (prints EVIDENCE_UNIT_TESTS_PASS)
Python 3.10 or later, standard library only.
"""
import hashlib
import hmac
import json
import math
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EVIDENCE = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(EVIDENCE))
sys.path.insert(0, EVIDENCE)
sys.path.insert(0, HERE)
import efhe_seal as es  # noqa: E402
import efhe_verify as ev  # noqa: E402
import report_html as rh  # noqa: E402
import run_mock_chain as rmc  # noqa: E402
import tls13_native as tn  # noqa: E402

MODEL = os.path.join(REPO, "demo", "model", "cdc-diabetes-lr4-v1.json")
SCHEMA = os.path.join(REPO, "demo", "model", "form_cdc.json")
CATALOG_HPP = os.path.join(REPO, "v8b_rev5", "software", "client", "models", "model_catalog.hpp")
CATALOG_LINE = os.path.join(REPO, "demo", "model", "catalog_line.hpp")
PROFILE_HPP = os.path.join(REPO, "v8b_rev5", "software", "client", "profile_id.hpp")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def uniform_body(rng, nct=4):
    return struct.pack("<%dI" % (nct * ev.CT_WORDS), *[rng.randrange(ev.Q[(i // ev.N) % 3])
                                                         for i in range(nct * ev.CT_WORDS)])


class Catalogue(unittest.TestCase):
    def parse(self, text):
        out = []
        for m in re.finditer(r'Model\{"([^"]+)","([0-9a-f]{64})",\{([^}]*)\},(?:\{[^}]*\},){3}\{([^}]*)\},'
                             r'([^,]+),(-?\d+)LL,', text):
            feats = [x.strip().strip('"') for x in m.group(3).split(",") if x.strip()]
            weights = tuple(int(x.strip().rstrip("L")) for x in m.group(4).split(","))
            out.append((m.group(1), m.group(2), len(feats), weights, int(m.group(6))))
        return out

    def test_embedded_catalogue_matches_repository(self):
        with open(CATALOG_HPP, encoding="utf-8") as f:
            base = self.parse(f.read())
        with open(CATALOG_LINE, encoding="utf-8") as f:
            line = self.parse(f.read())
        self.assertEqual(len(base), 3)
        self.assertEqual(len(line), 1)
        self.assertEqual(tuple(base + line), ev.CATALOGUE)

    def test_model_json_is_the_wire_model_id(self):
        with open(MODEL, "rb") as f:
            raw = f.read()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), ev.FORM_MODEL_HEX)
        m = json.loads(raw.decode("utf-8"))
        self.assertEqual(tuple(m["weights_int"]), ev.CATALOGUE[3][3])
        self.assertEqual(m["bias_int"], ev.CATALOGUE[3][4])
        self.assertEqual(tuple(m["features"]), ev.FEATURES)

    def test_profile_and_lua_constants(self):
        with open(PROFILE_HPP, encoding="utf-8") as f:
            self.assertIn(ev.PROFILE_HEX, f.read())
        with open(os.path.join(EVIDENCE, "efhe.lua"), encoding="utf-8") as f:
            lua = f.read()
        for _name, hexid, _f, _w, _b in ev.CATALOGUE:
            self.assertIn(hexid, lua)
        self.assertIn(ev.PROFILE_HEX, lua)
        self.assertIn("math.tointeger", lua)
        self.assertIn("DEFAULT_PORT = 18443", lua)


class KeySchedule(unittest.TestCase):
    def test_rfc8448_early_and_derived(self):
        # RFC 8448 section 3: early secret = HKDF-Extract(0, 0^32); "tls13 derived" with the empty transcript hash
        early = hmac.new(b"", bytes(32), "sha256").digest()
        self.assertEqual(early.hex(), "33ad0a1c607ec03b09e6cd9893680ce210adf300aa1f2660e1b22e10f170f92a")
        derived = tn.hkdf_expand_label(early, b"derived", hashlib.sha256(b"").digest(), 32, "sha256")
        self.assertEqual(derived.hex(), "6f2615a108c702c5678f54fc9dbab69716c076189c48250cebeac3576c3611ba")

    def test_exporter_composition(self):
        ems = bytes(range(48))
        inner = tn.hkdf_expand_label(ems, ev.EXPORTER_LABEL, hashlib.sha384(b"").digest(), 48, "sha384")
        want = tn.hkdf_expand_label(inner, b"exporter", hashlib.sha384(ev.PROFILE).digest(), 32, "sha384")
        self.assertEqual(tn.tls13_exporter(ems, ev.EXPORTER_LABEL, ev.PROFILE, 32), want)

    @unittest.skipIf(os.name == "nt", "OpenSSL s_client on Windows does not read piped stdin")
    def test_exporter_against_openssl(self):
        exe = shutil.which("openssl")
        if not exe:
            self.skipTest("openssl not found")
        d = tempfile.mkdtemp(prefix="efhe_unit_")
        try:
            r = subprocess.run([exe, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                                "-nodes", "-keyout", "k.pem", "-out", "c.pem", "-subj", "/CN=t", "-days", "1"],
                               cwd=d, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            label = ev.EXPORTER_LABEL.decode()
            srv = subprocess.Popen([exe, "s_server", "-accept", "127.0.0.1:%d" % port, "-cert", "c.pem", "-key", "k.pem",
                                    "-tls1_3", "-naccept", "1", "-keylogfile", "kl.txt"], cwd=d,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            lines = []
            t = threading.Thread(target=lambda: lines.extend(srv.stdout), daemon=True)
            t.start()
            for _ in range(250):
                if any(b"ACCEPT" in x for x in lines):
                    break
                time.sleep(0.02)
            cl = subprocess.run([exe, "s_client", "-connect", "127.0.0.1:%d" % port, "-tls1_3", "-keymatexport", label,
                                 "-keymatexportlen", "32"], cwd=d, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=30)
            srv.stdin.close()
            srv.wait(30)
            m = re.search(r"Keying material:\s*([0-9A-Fa-f]+)", cl.stdout.decode("utf-8", "replace"))
            self.assertIsNotNone(m, cl.stdout[-500:])
            keys = tn.read_keylog(os.path.join(d, "kl.txt"))
            self.assertEqual(len(keys), 1)
            ems = list(keys.values())[0]["EXPORTER_SECRET"]
            # without a context OpenSSL hashes the empty string (RFC 8446 section 7.5)
            self.assertEqual(tn.tls13_exporter(ems, ev.EXPORTER_LABEL, b"", 32).hex(), m.group(1).lower())
        finally:
            shutil.rmtree(d, ignore_errors=True)


class Wire(unittest.TestCase):
    def setUp(self):
        self.digests = (ev.PROFILE, bytes.fromhex(ev.FORM_MODEL_HEX), b"\x01" * 32, b"\x02" * 32, b"\x03" * 32,
                        b"\x04" * 32)
        self.h = ev.encode_header(1, 4, 1, 4 * ev.CT_BYTES, 0x1234, 1, self.digests)

    def test_valid_header(self):
        f, err = ev.parse_header(self.h)
        self.assertIsNone(err)
        self.assertEqual((f["kind"], f["features"], f["rows"], f["sequence"]), (1, 4, 1, 1))
        self.assertEqual(f["model_id"], ev.FORM_MODEL_HEX)

    def test_errors_in_wire_hpp_order(self):
        def mut(off, data):
            b = bytearray(self.h)
            b[off:off + len(data)] = data
            return bytes(b)
        cases = [(self.h[:255], "WIRE_HEADER_LENGTH"), (mut(0, b"X"), "WIRE_MAGIC_VERSION"),
                 (mut(8, b"\x02"), "WIRE_MAGIC_VERSION"), (mut(10, b"\x03"), "WIRE_KIND_LAYOUT_RESERVED"),
                 (mut(24, struct.pack("<I", 2)), "WIRE_KIND_LAYOUT_RESERVED"),
                 (mut(56, b"\x01"), "WIRE_KIND_LAYOUT_RESERVED"),
                 (mut(16, struct.pack("<I", 33)), "WIRE_DIMENSION_GENERATION_SEQUENCE"),
                 (mut(20, struct.pack("<I", 2049)), "WIRE_DIMENSION_GENERATION_SEQUENCE"),
                 (mut(40, bytes(8)), "WIRE_DIMENSION_GENERATION_SEQUENCE"),
                 (mut(48, bytes(8)), "WIRE_DIMENSION_GENERATION_SEQUENCE"),
                 (mut(32, struct.pack("<Q", ev.CT_BYTES)), "WIRE_PAYLOAD_LENGTH"),
                 (mut(64, b"\x00"), "WIRE_PROFILE_OR_EMPTY_ID"), (mut(128, bytes(32)), "WIRE_PROFILE_OR_EMPTY_ID"),
                 (mut(96, b"\xab" * 32), "MODEL_SOURCE_ID_UNKNOWN")]
        for h, code in cases:
            self.assertEqual(ev.parse_header(h)[1], code)
        h = ev.encode_header(1, 10, 1, 10 * ev.CT_BYTES, 1, 1, (ev.PROFILE, bytes.fromhex(ev.FORM_MODEL_HEX)) +
                             self.digests[2:])
        self.assertEqual(ev.parse_header(h)[1], "WIRE_MODEL_DIMENSIONS")

    def test_request_id_and_hash(self):
        body = b"\x05" * (4 * ev.CT_BYTES)
        z = bytearray(self.h)
        z[160:192] = bytes(32)
        z[224:256] = bytes(32)
        self.assertEqual(ev.request_id_of(self.h), hashlib.sha256(bytes(z)).hexdigest())
        resp = bytearray(self.h)
        resp[10] = 2
        struct.pack_into("<Q", resp, 32, ev.CT_BYTES)
        self.assertEqual(ev.request_id_of(bytes(resp)), ev.request_id_of(self.h))      # wire.hpp:35 kind and size
        z = bytearray(self.h)
        z[224:256] = bytes(32)
        self.assertEqual(ev.request_hash_of(self.h, body), hashlib.sha256(bytes(z) + body).hexdigest())

    def test_split_frames(self):
        body = bytes(4 * ev.CT_BYTES)
        frames, probs = ev.split_frames(self.h + body + b"tail", 1)
        self.assertEqual(len(frames), 1)
        self.assertEqual(probs[0]["code"], "WIRE_TRUNCATED_HEADER")
        frames, probs = ev.split_frames(self.h + body[:-1], 1)
        self.assertFalse(frames[0]["complete"])
        self.assertEqual(probs[-1]["code"], "WIRE_TRUNCATED_FRAME")
        frames, probs = ev.split_frames(b"GARBAGE!" + self.h, 1)
        self.assertEqual(probs[0]["code"], "WIRE_UNACCOUNTED_BYTES")


class Affine(unittest.TestCase):
    def test_zero_body_gives_bias_only(self):
        w, b = ev.CATALOGUE[0][3], ev.CATALOGUE[0][4]
        out = ev.words_of(ev.affine(bytes(4 * ev.CT_BYTES), 4, w, b))
        for l, q in enumerate(ev.Q):
            self.assertEqual(out[l * ev.N], b % q)
        self.assertEqual(sum(1 for v in out if v), 3)

    def test_against_reference_formula(self):
        rng = random.Random(7)
        body = uniform_body(rng)
        w, b = ev.CATALOGUE[3][3], ev.CATALOGUE[3][4]
        got = ev.words_of(ev.affine(body, 4, w, b))
        x = ev.words_of(body)
        for _ in range(300):
            i = rng.randrange(ev.CT_WORDS)
            q = ev.Q[(i // ev.N) % 3]
            want = sum(x[f * ev.CT_WORDS + i] * w[f] for f in range(4)) % q      # Python % is the mod-q residue
            if i % (3 * ev.N) % ev.N == 0 and i < 3 * ev.N:
                want = (want + b) % q
            self.assertEqual(got[i], want)
        for l, q in enumerate(ev.Q):
            want = (sum(x[f * ev.CT_WORDS + l * ev.N] * w[f] for f in range(4)) + b) % q
            self.assertEqual(got[l * ev.N], want)


class Statistics(unittest.TestCase):
    def test_gammaincc_known_values(self):
        for x in (0.1, 1.0, 5.0, 40.0):
            self.assertAlmostEqual(ev.gammaincc(1.0, x), math.exp(-x), places=12)
            self.assertAlmostEqual(ev.gammaincc(0.5, x), math.erfc(math.sqrt(x)), places=10)
        self.assertEqual(ev.gammaincc(31.5, 1e6), 0.0)

    def test_limb_verdicts(self):
        rng = random.Random(11)
        q = ev.Q[0]
        self.assertTrue(ev.limb_stats([rng.randrange(q) for _ in range(ev.N)], q)["uniform"])
        self.assertEqual(ev.limb_stats([0] * ev.N, q)["reason"], "all_zero")
        small = [rng.randint(-(1 << 21), 1 << 21) % q for _ in range(ev.N)]
        self.assertFalse(ev.limb_stats(small, q)["uniform"])
        nonc = [rng.randrange(q) for _ in range(ev.N)]
        nonc[5] = q
        self.assertEqual(ev.limb_stats(nonc, q)["reason"], "noncanonical")
        self.assertEqual(ev.noncanonical([ev.Q[0]] + [0] * (ev.N - 1) + [ev.Q[1]]), [0, ev.N])


class Plaintext(unittest.TestCase):
    def test_dense_anywhere_sparse_aligned_words_aligned(self):
        p = ev.Patterns()
        p.add_long("dense", struct.pack("<d", 31.2))
        p.add_long("sparse", struct.pack("<q", 9))
        p.add_word("word", struct.pack("<i", 9))
        blob = bytearray(4096)
        blob[1001:1009] = struct.pack("<d", 31.2)
        blob[2001:2009] = struct.pack("<q", 9)          # unaligned: structure, not searched
        blob[3000:3008] = struct.pack("<q", 9)          # aligned
        blob = bytes(blob)
        hits = ev.search_decrypted(blob, p.prefix_index(False), p.prefix_index(True))
        self.assertIn(("dense", 1001), hits)
        self.assertIn(("sparse", 3000), hits)
        self.assertNotIn(("sparse", 2001), hits)
        words = ev.search_words(blob, p.word_index())
        self.assertIn(("word", 750), words)
        self.assertNotIn(("word", 500), [(lb, i) for lb, i in words])
        self.assertTrue(all(lb == "word" for lb, _i in words))

    def test_zero_patterns_are_not_searchable(self):
        p = ev.Patterns()
        p.add_long("zero", bytes(8))
        p.add_word("zero", bytes(4))
        self.assertEqual((p.long, p.word), ({}, {}))

    def test_chance_rate(self):
        p = ev.Patterns()
        p.add_word("small", struct.pack("<I", 9))
        p.add_word("between", struct.pack("<I", 600000000))     # below q0 and q1, above q2
        p.add_word("large", struct.pack("<I", 0xFFFFFFF0))      # above every q: impossible in canonical limbs
        n = [32768, 32768, 32768]
        want = sum(n[l] / q for l, q in enumerate(ev.Q)) + n[0] / ev.Q[0] + n[1] / ev.Q[1]
        self.assertAlmostEqual(ev.word_chance(p, n), want, places=15)

    def test_header_hits(self):
        h = ev.encode_header(1, 4, 1, 4 * ev.CT_BYTES, 1, 1, (ev.PROFILE, bytes.fromhex(ev.FORM_MODEL_HEX),
                                                              b"\x01" * 32, b"\x02" * 32, b"\x03" * 32, b"\x04" * 32))
        frames = [dict(offset=0, end=ev.HDR, fields=ev.parse_header(h)[0])]
        self.assertTrue(ev.locate_stream(frames, 48, 8, True)["explained"])           # sequence field
        self.assertTrue(ev.locate_stream(frames, 40, 8, True)["explained"])           # sparse in generation
        self.assertFalse(ev.locate_stream(frames, 40, 8, False)["explained"])         # dense in generation
        self.assertFalse(ev.locate_stream(frames, 130, 8, False)["explained"])        # dense in key_id

    def test_domain_covers_every_answer(self):
        vals = ev.domain_values(load(SCHEMA))
        self.assertEqual(vals["GenHlth"], [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(len(vals["BMI"]), 691)
        self.assertEqual((vals["BMI"][0], vals["BMI"][-1]), (12.0, 81.0))
        self.assertIn(31.2, vals["BMI"])


class Levels(unittest.TestCase):
    def test_presets(self):
        model, schema = load(MODEL), load(SCHEMA)
        lv = schema["levels"]
        for p in schema["presets"]:
            if p["expected"]["refused"]:
                continue
            x = [float(p["form"][n]) for n in ev.FEATURES]
            plain = ev.reference_logit(model, x)
            self.assertAlmostEqual(plain, p["expected"]["logit_plain"], delta=1e-9)
            level, boundary = ev.classify_level(plain, lv["edges_logit"], lv["band_abs_logit"])
            self.assertEqual((level, boundary), (p["expected"]["level"], p["expected"]["boundary"]), p["id"])


class Logs(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="efhe_unit_logs_")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, name, text):
        p = os.path.join(self.d, name)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return p

    def test_server_log(self):
        rq, rs = "a" * 64, "b" * 64
        p = self.write("server.log", "1795491230.058350934 WARNING_DEMO_TLS_KEYLOG_ENABLED /evidence/tls_keys.log\n"
                       '1795491230.061 {"status":"CPP_E2E_SERVER_READY","port":18443,"test_injection":false}\n'
                       '1795491230.195 {"status":"CPP_E2E_RESPONSE_WRITTEN_NOT_OWNER_ACCEPTANCE","sequence":1,'
                       '"request_sha256":"%s","response_payload_sha256":"%s","cloud_ns":583191}\n'
                       "1795491230.198 CPP_E2E_SERVER_CONNECTION_STOP WIRE_KEY_GENERATION_CHANGE\n" % (rq, rs))
        s = ev.parse_server_log(p)
        self.assertTrue(s["ready"] and s["keylog_warning"])
        self.assertEqual(s["responses"][0]["request_sha256"], rq)
        self.assertAlmostEqual(s["responses"][0]["epoch"], 1795491230.195)
        self.assertEqual(s["connection_stops"][0]["code"], "WIRE_KEY_GENERATION_CHANGE")

    def test_board_public_and_disclosure(self):
        rq, rs = "c" * 64, "d" * 64
        form = {"GenHlth": 3, "HighBP": 1, "BMI": 31.2, "Age": 9}
        salt = "e" * 64
        canonical = json.dumps(form, sort_keys=True, separators=(",", ":"))
        commit = ev.commitment_of(salt, canonical)
        self.assertEqual(commit, hashlib.sha256(("EDGEFHE_FORM_COMMIT_V1\n%s\n%s\n" % (salt, canonical)).encode())
                         .hexdigest())
        pub = self.write("board_public.jsonl", "\n".join(json.dumps(x) for x in (
            {"v": 1, "seq": 1, "run": "r1", "type": "form.accepted", "class": "public", "data": {"commitment": commit}},
            {"v": 1, "seq": 2, "run": "r1", "type": "client.exited", "class": "public",
             "data": {"trace": [{"request_sha256": rq, "response_payload_sha256": rs}]}})) + "\n")
        b = ev.parse_board_public(pub)
        self.assertEqual(b["runs"]["r1"]["pairs"], {(rq, rs)})
        self.assertEqual(b["runs"]["r1"]["commitments"], {commit})
        os.makedirs(os.path.join(self.d, "disclosure"))
        self.write(os.path.join("disclosure", rq + ".json"), json.dumps(
            {"request_sha256": rq, "fictional": True, "owner": {"form": form, "salt_hex": salt, "logit_ckks": 0.65}}))
        items, probs = ev.parse_disclosures(os.path.join(self.d, "disclosure"))
        self.assertEqual(probs, [])
        self.assertEqual((items[0]["form"], items[0]["salt_hex"], items[0]["logit_ckks"]), (form, salt, 0.65))
        self.assertIn(canonical, ev.canonical_candidates(form))

    def test_pins(self):
        os.makedirs(os.path.join(self.d, "ca"))
        self.write(os.path.join("ca", "server.pin"), "1" * 64 + "\n")
        self.write(os.path.join("ca", "client.pin"), "2" * 64 + "\n")
        self.assertEqual(ev.read_pins(self.d), {"server": "1" * 64, "client": "2" * 64})
        p = self.write("pins.json", json.dumps({"server": "3" * 64, "client_pin": "4" * 64}))
        self.assertEqual(ev.read_pins(p), {"server": "3" * 64, "client": "4" * 64})


def eth_ip_tcp(src, dst, sport, dport, seq, flags, payload):
    ip_len = 20 + 20 + len(payload)
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, ip_len, 0, 0, 64, 6, 0, bytes(src), bytes(dst))
    tcp = struct.pack(">HHIIBBHHH", sport, dport, seq, 0, 5 << 4, flags, 65535, 0, 0)
    return b"\x02" * 6 + b"\x04" * 6 + b"\x08\x00" + ip + tcp + payload


def pcapng(packets, isb=None):
    out = bytearray()

    def block(btype, body):
        body += b"\x00" * ((4 - len(body) % 4) % 4)
        n = 12 + len(body)
        return struct.pack("<II", btype, n) + body + struct.pack("<I", n)
    out += block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    out += block(1, struct.pack("<HHI", 1, 0, 0))
    for i, data in enumerate(packets):
        ts = 1795491230000000 + i * 1000
        out += block(6, struct.pack("<IIIII", 0, ts >> 32, ts & 0xFFFFFFFF, len(data), len(data)) + data)
    if isb is not None:
        opts = struct.pack("<HHQ", 4, 8, isb[0]) + struct.pack("<HHQ", 5, 8, isb[1]) + struct.pack("<HH", 0, 0)
        out += block(5, struct.pack("<III", 0, 0, 0) + opts)
    return bytes(out)


class Capture(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="efhe_unit_cap_")
        c, s = (192, 0, 2, 10), (192, 0, 2, 20)
        self.payload = [bytes([0x17, 3, 3, 0, 4]) + b"abcd", bytes([0x17, 3, 3, 0, 4]) + b"efgh"]
        self.pk = [eth_ip_tcp(c, s, 50000, 18443, 999, 0x02, b""), eth_ip_tcp(s, c, 18443, 50000, 4999, 0x12, b""),
                   eth_ip_tcp(c, s, 50000, 18443, 1000, 0x18, self.payload[0]),
                   eth_ip_tcp(c, s, 50000, 18443, 1009, 0x18, self.payload[1]),
                   eth_ip_tcp(c, s, 50000, 18443, 1009, 0x18, self.payload[1]),          # retransmission
                   eth_ip_tcp(s, c, 18443, 50000, 5000, 0x18, b"\x15\x03\x03\x00\x02\x01\x00")]

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def path(self, name, data):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_pcapng_isb_and_reassembly(self):
        cap = tn.read_capture(self.path("a.pcapng", pcapng(self.pk, isb=(6, 0))))
        self.assertEqual((cap["format"], len(cap["packets"]), cap["problems"]), ("pcapng", 6, []))
        self.assertEqual(cap["isb"], [{"if_id": 0, "ifrecv": 6, "ifdrop": 0}])
        self.assertEqual(ev.drop_counters(cap, None)["dropped"], 0)
        (conn,) = tn.tcp_connections(cap, 18443)
        self.assertEqual(conn["c2s"], self.payload[0] + self.payload[1])
        self.assertEqual((conn["c2s_gaps"], conn["c2s_conflicts"]), ([], []))
        self.assertEqual(tn.tls_records(conn["c2s"]), ([(0, 23, 4), (9, 23, 4)], 0))
        self.assertEqual(conn["client"], "192.0.2.10:50000")

    def test_gap_and_truncation(self):
        cap = tn.read_capture(self.path("b.pcapng", pcapng(self.pk[:2] + self.pk[3:])))
        (conn,) = tn.tcp_connections(cap, 18443)
        self.assertEqual(conn["c2s_gaps"], [(0, 9)])
        data = pcapng(self.pk)
        cap = tn.read_capture(self.path("c.pcapng", data[:-7]))
        self.assertIn("CAPTURE_TRUNCATED", cap["problems"])

    def test_classic_pcap(self):
        hdr = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 1)
        recs = b"".join(struct.pack("<IIII", 1795491230, i, len(p), len(p)) + p for i, p in enumerate(self.pk))
        cap = tn.read_capture(self.path("d.pcap", hdr + recs))
        self.assertEqual((cap["format"], len(cap["packets"])), ("pcap", 6))
        stats = self.path("tcpdump.txt", b"66 packets captured\n132 packets received by filter\n0 packets dropped by kernel\n")
        self.assertEqual(ev.drop_counters(cap, stats)["dropped"], 0)
        stats = self.path("dumpcap.txt", "Packets received/dropped on interface 'x': 9/2 (pcap:0)\n".encode())
        self.assertEqual(ev.drop_counters(cap, stats)["dropped"], 2)

    def test_record_signature(self):
        def recs(sizes, t0):
            out = [(1, t0, 22, 240, False), (2, t0 + 0.001, 20, 1, False)]
            for i, ln in enumerate(sizes):
                out.append((3 + i, t0 + 0.002 + i * 1e-4, 23, ln, True))
            return out
        form = dict(nokey={"c2s": recs([877, 95, 69, 273] + [16401] * 24 + [19], 10.0),
                           "s2c": [(1, 10.0005, 22, 122, False), (1, 10.0005, 20, 1, False)] +
                           [(1, 10.0006, 23, ln, True) for ln in (23, 62, 877, 96, 69)] +
                           [(9, 10.01, 23, 74, True), (9, 10.01, 23, 74, True), (40, 10.2, 23, 273, True)] +
                           [(41, 10.2, 23, 16401, True)] * 6 + [(50, 10.3, 23, 19, True)]})
        sig = ev.record_signature(form)
        self.assertEqual((sig["classification"], sig["problems"], sig["tickets"]), ("form", [], [74, 74]))
        self.assertTrue(sig["client_close_notify"] and sig["server_close_notify"])
        probe = dict(nokey={"c2s": recs([877, 97, 69, 273] + [16401] * 24 + [273], 10.0), "s2c": form["nokey"]["s2c"][:-1]})
        sig = ev.record_signature(probe)
        self.assertEqual(sig["classification"], "other")
        self.assertTrue(any("followed by 0 limb records" in p for p in sig["problems"]))
        hs = dict(nokey={"c2s": recs([877, 95, 69, 19], 10.0), "s2c": form["nokey"]["s2c"][:9] + [(51, 10.4, 23, 19, True)]})
        sig = ev.record_signature(hs)
        self.assertEqual((sig["classification"], sig["problems"]), ("handshake_only", []))


class Report(unittest.TestCase):
    def test_render_is_deterministic_and_local(self):
        gates = dict((g, dict(status="PASS", title=ev.TITLES[g], checks=[], details={})) for g in ev.GATES)
        gates["G11"]["status"] = "SKIP"
        report = dict(schema=ev.SCHEMA, verdict="PASS", evidence_pass=True, gates=gates, connections=[],
                      inputs={"capture": {"path": "capture.pcapng", "sha256": "0" * 64, "bytes": 10}},
                      tool={"tshark": "4.6.4", "native_decrypt": "available"}, negative_control={"detected": False})
        a, b = rh.render(report), rh.render(report)
        self.assertEqual(a, b)
        self.assertIn("封包證據驗證報告", a)
        self.assertIn("不構成醫療診斷", a)
        self.assertNotRegex(a, r"https?://")
        self.assertNotIn("機率", a)

    def test_dumps_report_sorted(self):
        text = ev.dumps_report({"b": 1, "a": {"d": 2.5, "c": [3]}})
        self.assertEqual(text, '{\n "a": {\n  "c": [\n   3\n  ],\n  "d": 2.5\n },\n "b": 1\n}\n')

    def base_report(self, **inputs):
        gates = dict((g, dict(status="PASS", title=ev.TITLES[g], checks=[], details={})) for g in ev.GATES)
        gates["G11"]["status"] = "SKIP"
        inp = {"capture": {"path": "capture.pcapng", "sha256": "0" * 64, "bytes": 10}}
        inp.update(inputs)
        return dict(schema=ev.SCHEMA, verdict="PASS", evidence_pass=True, gates=gates, inputs=inp, tool={},
                    negative_control={"detected": False},
                    connections=[dict(label="c01", client="198.51.100.123:54321", classification="form", t_syn=10.0,
                                      t_first=10.0, t_last=10.3, records=[["c2s", 10.0, 273, 23, 1],
                                                                          ["s2c", 10.2, 16401, 23, 1]])])

    def test_cloud_keys_are_named_exactly(self):
        # The cloud holds the TLS server key (and, in evidence sessions, the demo key log) but no CKKS key at all.
        a = rh.render(self.base_report(keylog={"path": "tls_keys.log", "sha256": "1" * 64, "bytes": 5}))
        b = rh.render(self.base_report())
        for text in (a, b):
            self.assertIn("雲端電腦全程沒有任何 CKKS 金鑰，無法解密 CKKS 密文", text)
            self.assertIn("TLS 伺服器憑證與私鑰", text)
            self.assertNotIn("沒有任何解密金鑰", text)
            self.assertNotIn("沒有任何金鑰", text)
            self.assertNotIn("板子", text)
            self.assertNotIn("閘門", text)
        self.assertIn("示範專用的 TLS 金鑰紀錄", a)
        self.assertNotIn("示範專用的 TLS 金鑰紀錄", b)

    def test_text_is_at_least_18px(self):
        samples = [(10.0 + i * 0.1, 3000.0 + (i % 7)) for i in range(80)]
        text = rh.render(self.base_report(), samples)
        sizes = [int(x) for x in re.findall(r"font-size:(\d+)px", text)]
        sizes += [int(x) for x in re.findall(r'font-size="(\d+)"', text)]
        self.assertTrue(sizes)
        self.assertGreaterEqual(min(sizes), 18, sorted(set(sizes)))
        self.assertNotRegex(text, r"font-size:[\d.]+(em|rem|%)")
        # scaled figures never shrink below their viewBox width, so 18 user units stay 18 px or more
        for m in re.finditer(r'<svg ([^>]*)viewBox="0 0 (\d+) \d+" width="100%"', text):
            self.assertIn('class="wide"', m.group(1))
            self.assertEqual(m.group(2), "1000")
        self.assertIn("svg.wide{display:block;min-width:1000px}", text)

    def test_seal_order_violation_is_reported(self):
        rep = self.base_report()
        rep["gates"]["G0"] = dict(status="FAIL-PENDING", title=ev.TITLES["G0"], details={}, checks=[
            dict(name="sealed_hash", status="PASS"),
            dict(name="seal_order", status="FAIL-PENDING", reason="capture_dsb.pcapng existed before capture.sha256")])
        rep.update(verdict="FAIL-PENDING:G0", evidence_pass=False)
        text = rh.render(rep)
        self.assertIn("封存順序：efhe_seal.py 記錄到 capture_dsb.pcapng 在封存紀錄 capture.sha256", text)
        self.assertIn("封存晚於金鑰紀錄的使用（待確認，見 seal_order.json）", text)
        self.assertIn("整體判定：待確認（G0）", text)
        clean = rh.render(self.base_report())
        self.assertNotIn("封存晚於金鑰紀錄的使用", clean)

    def with_g1(self, rep, port):
        rep["port"] = port
        rep["gates"]["G1"]["checks"] = [
            dict(name="only_unicast_flow_is_tls_port", status="PASS", frames={"tls_port": 80, "board_pc_other": 0}),
            dict(name="record_signature", status="PASS", connection="c01", classification="form")]
        return rep

    def test_g1_summary_names_the_verified_port(self):
        text = rh.render(self.with_g1(self.base_report(), 59628))
        self.assertIn("KV260 與雲端電腦之間，TCP 連接埠 59628 以外的單播封包 0 個。", text)
        self.assertNotIn("18443", text)
        self.assertIn("TCP 連接埠 18443 以外", rh.render(self.with_g1(self.base_report(), 18443)))

    def test_negative_control_wording_is_formal(self):
        rep = self.base_report()
        rep["negative_control"] = dict(detected=True, gate="G6", requests=[
            dict(connection="c02", sequence=1, reasons=["all_zero"], limbs=24)])
        text = rh.render(rep)
        self.assertIn("因此此結果符合預期", text)
        self.assertNotIn("這個", text)

    def test_pc_rehearsal_is_labelled(self):
        samples = [(9.0 + i * 0.1, 3000.0 + (i % 7)) for i in range(60)]
        rep = self.with_g1(self.base_report(), 18443)
        rep["pc_rehearsal"] = dict(detected=True, reasons=["rehearsal_summary_present", "loopback_board_address"])
        text = rh.render(rep, samples)
        self.assertIn("<h1>EdgeFHE 期末展示：封包證據驗證報告（PC 演練）</h1>", text)
        self.assertIn("<title>EdgeFHE 證據驗證報告（PC 演練）</title>", text)
        self.assertIn("資料來源：PC 演練（非 KV260 板上實測）。本次的板端程式在 PC 上執行（判定依據：執行目錄含有 PC 演練摘要 "
                      "rehearsal_summary.txt；板端位址為本機迴路位址）", text)
        self.assertIn("<h2>連線時序（PC 演練）</h2><p class=\"warn\">PC 演練（非 KV260 板上實測）：時間軸上的時間", text)
        self.assertIn("PC 上的板端程式與雲端電腦之間只有 TLS 1.3 加密連線", text)            # claim C1
        self.assertIn("雲端送出回應之後，PC 上的板端程式未再傳送任何應用資料。", text)       # claim C6
        self.assertIn("PC 上的板端程式與雲端電腦之間，TCP 連接埠 18443 以外的單播封包 0 個。", text)
        self.assertIn("上行（PC 上的板端程式→雲端）", text)
        self.assertIn("上方的刻度是 PC 上的板端程式送出的紀錄", text)
        self.assertIn('aria-label="PC 上的板端程式功率與各連線時間窗"', text)
        self.assertIn("不是 KV260 的能耗結果", text)
        # outside the PC rehearsal label itself, no sentence presents the run as a KV260 measurement
        rest = text.replace("PC 演練（非 KV260 板上實測）", "").replace("不代表 KV260 的表現", "").replace(
            "不是 KV260 的能耗結果", "")
        self.assertNotIn("KV260", rest)
        board = rh.render(self.with_g1(self.base_report(), 18443), samples)
        self.assertNotIn("PC 演練", board)
        self.assertNotIn("PC 上的板端程式", board)
        self.assertIn("KV260 與雲端電腦之間只有 TLS 1.3 加密連線", board)
        rep["pc_rehearsal"] = dict(detected=False, reasons=[])
        self.assertEqual(rh.render(rep, samples), board)


class Rehearsal(unittest.TestCase):
    def ctx(self, client, server, **kw):
        conns = [dict(client=client, server=server)] if client else []
        return dict(connections=conns, **kw)

    def test_pc_rehearsal_rule(self):
        cases = [(self.ctx("127.0.0.1:59629", "127.0.0.1:18443"), ["loopback_board_address"]),
                 (self.ctx("::1:59629", "::1:18443"), ["loopback_board_address"]),
                 (self.ctx("192.0.2.7:40000", "192.0.2.7:18443"), ["board_address_equals_cloud_address"]),
                 (self.ctx("198.51.100.123:54321", "198.51.100.10:18443"), []),
                 (self.ctx("198.51.100.123:54321", "198.51.100.10:18443", rehearsal_summary=True),
                  ["rehearsal_summary_present"]),
                 (self.ctx(None, None, board_ip="127.0.0.1"), ["loopback_board_address"]),
                 (self.ctx(None, None), [])]
        for ctx, reasons in cases:
            self.assertEqual(ev.pc_rehearsal(ctx), dict(detected=bool(reasons), reasons=reasons), ctx)

    def test_discover_finds_the_rehearsal_summary(self):
        d = tempfile.mkdtemp(prefix="efhe_unit_reh_")
        try:
            self.assertIsNone(ev.discover(d)["rehearsal_summary"])
            with open(os.path.join(d, ev.REHEARSAL_SUMMARY), "w", encoding="utf-8") as f:
                f.write("SIM_REHEARSAL_PASS\n")
            self.assertEqual(ev.discover(d)["rehearsal_summary"], os.path.join(d, "rehearsal_summary.txt"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


def quietly(fn, *args):
    """Run an efhe_seal function with its marker lines collected instead of printed: (lines, result)."""
    out, old = [], es.say
    es.say = out.append
    try:
        return out, fn(*args)
    finally:
        es.say = old


class SealOrder(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="efhe_unit_seal_")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def put(self, name, data):
        with open(os.path.join(self.d, name), "wb") as f:
            f.write(data)

    def g0(self, seal, order):
        cap = dict(problems=[], format="pcapng", packets=[], isb=[{"if_id": 0, "ifrecv": 1, "ifdrop": 0}], dsb=0)
        ctx = dict(seal=seal, seal_order=order, inputs={"capture": {"sha256": "a" * 64}}, cap=cap, connections=[],
                   lost=[])
        g = ev.g0_capture(ctx)
        return g["status"], dict((c["name"], c["status"]) for c in g["checks"])

    def test_g0_seal_order_check(self):
        self.assertEqual(self.g0("a" * 64, None), ("PASS", {"sealed_hash": "PASS", "seal_order": "PASS",
                                                            "dropped_packets": "PASS", "capture_file": "PASS",
                                                            "no_embedded_secrets": "PASS", "tcp_gaps": "PASS"}))
        st, checks = self.g0("a" * 64, {"violation": "decrypted_copy_before_seal"})
        self.assertEqual((st, checks["seal_order"]), ("FAIL-PENDING", "FAIL-PENDING"))
        self.assertNotIn("seal_order", self.g0(None, None)[1])
        self.assertEqual(ev.verdict_of({"G0": {"status": st}}), "FAIL-PENDING:G0")

    def test_late_seal_writes_the_record_and_verify_reads_it(self):
        self.put(es.CAPTURE, b"capture bytes")
        self.put(es.DSB, b"decrypted copy made from the key log")
        out, (digest, err) = quietly(es.seal, self.d)
        self.assertTrue(any(x.startswith("EFHE_SEAL_ORDER_WARNING file=seal_order.json") for x in out), out)
        self.assertIsNone(err)
        self.assertEqual(digest, hashlib.sha256(b"capture bytes").hexdigest())
        rec = ev.read_seal_order(ev.discover(self.d)["seal_order"])
        self.assertEqual(rec["violation"], "decrypted_copy_before_seal")
        self.assertEqual(rec["capture_sha256"], digest)
        self.assertEqual(rec["files"], {es.DSB: hashlib.sha256(b"decrypted copy made from the key log").hexdigest()})

    def test_normal_seal_writes_no_record(self):
        self.put(es.CAPTURE, b"capture bytes")
        out, _r = quietly(es.seal, self.d)
        self.assertFalse(any("SEAL_ORDER" in x for x in out), out)
        self.assertTrue(os.path.isfile(os.path.join(self.d, es.SEAL)))
        self.assertIsNone(ev.discover(self.d)["seal_order"])
        self.assertIsNone(ev.read_seal_order(os.path.join(self.d, es.ORDER)))


def respell(path):
    """The same folder spelled with a different case (Windows) or unchanged (case-sensitive systems)."""
    return path.swapcase() if os.name == "nt" else path


class RepositoryGuards(unittest.TestCase):
    def test_under_ignores_case_on_windows(self):
        inside = SCHEMA                          # an existing file: realpath gives its on-disk spelling
        for p in (inside, respell(inside), inside[0].swapcase() + inside[1:]):
            self.assertTrue(ev._under(p, REPO), p)
            self.assertTrue(ev.inside_repository(p), p)
            self.assertTrue(rmc.inside(p, REPO), p)
            self.assertEqual(ev._rel(p, tempfile.gettempdir()), "repository:demo/model/form_cdc.json")
        self.assertFalse(ev.inside_repository(tempfile.gettempdir()))
        self.assertFalse(rmc.inside(tempfile.gettempdir(), REPO))

    def test_rel_inside_base_ignores_spelling(self):
        d = tempfile.mkdtemp(prefix="efhe_unit_rel_")
        try:
            f = os.path.join(d, "sub", "capture.pcapng")
            os.makedirs(os.path.dirname(f))
            with open(f, "wb"):
                pass
            self.assertEqual(ev._rel(respell(f), d), "sub/capture.pcapng")
            self.assertEqual(ev._rel(f, respell(d)), "sub/capture.pcapng")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(os.name == "nt", "Windows path spellings")
    def test_extended_length_and_share_spellings(self):
        real = os.path.realpath(SCHEMA)
        drive, tail = os.path.splitdrive(real)
        missing = os.path.join(REPO, "demo", "evidence", "tests", "no_such_run_dir", "x")
        spellings = ["\\\\?\\" + real, "\\\\?\\" + respell(real), "\\\\?\\" + missing, "\\\\.\\" + real]
        share = "\\\\localhost\\%s$%s" % (drive[0], tail)              # the admin share, when this account can use it
        if os.path.isfile(share):
            spellings += [share, respell(share), "\\\\?\\UNC\\localhost\\%s$%s" % (drive[0], tail),
                          "\\\\localhost\\%s$%s" % (drive[0], os.path.splitdrive(missing)[1])]
        for p in spellings:
            self.assertTrue(ev.inside_repository(p), p)
            self.assertTrue(rmc.inside(p, REPO), p)
        for p in spellings[:2] + spellings[4:6]:
            self.assertEqual(ev._rel(p, tempfile.gettempdir()), "repository:demo/model/form_cdc.json", p)
        outside = tempfile.gettempdir()
        for p in ("\\\\?\\" + outside, "\\\\?\\" + os.path.join(outside, "efhe_no_such_dir", "x")):
            self.assertFalse(ev.inside_repository(p), p)

    def test_identity_walk_catches_another_name_of_the_folder(self):
        # A spelling that no string rule folds (admin share, GLOBALROOT, a substituted drive, a bind mount) still
        # names the same folder: _anchor then compares the existing ancestors by file identity. Simulated with a
        # symbolic link to a scratch folder (never to the repository) that _resolved is not allowed to resolve.
        d = tempfile.mkdtemp(prefix="efhe_unit_walk_")
        target, alias, other = os.path.join(d, "target"), os.path.join(d, "alias"), os.path.join(d, "other")
        os.makedirs(os.path.join(target, "sub"))
        os.makedirs(other)
        real_resolved = ev._resolved
        try:
            try:
                os.symlink(target, alias, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("this account cannot create symbolic links")
            ev._resolved = lambda p: os.path.abspath(p) if str(p).startswith(alias) else real_resolved(p)
            self.assertEqual(ev._anchor(os.path.join(alias, "sub", "new_run", "x"), target), ["sub", "new_run", "x"])
            self.assertEqual(ev._anchor(alias, target), [])
            self.assertTrue(ev._under(os.path.join(alias, "sub"), target))
            self.assertEqual(ev._rel(os.path.join(alias, "sub", "f.json"), target), "sub/f.json")
            self.assertIsNone(ev._anchor(os.path.join(alias, "sub"), other))
            self.assertIsNone(ev._anchor(os.path.join(other, "x"), target))
        finally:
            ev._resolved = real_resolved
            if os.path.islink(alias):
                (os.rmdir if os.name == "nt" else os.unlink)(alias)     # removes the link only
            shutil.rmtree(d, ignore_errors=True)

    def test_seal_refuses_a_run_inside_the_repository(self):
        run = os.path.join(REPO, "demo", "evidence", "tests", "no_such_run_dir")
        spellings = [run, respell(run)] + (["\\\\?\\" + run, "\\\\?\\" + respell(run)] if os.name == "nt" else [])
        for p in spellings:
            out, rc = quietly(es.main, ["--run", p])
            self.assertEqual(rc, 2)
            self.assertTrue(out and out[0].startswith("EFHE_SEAL_REFUSED reason=RUN_INSIDE_REPOSITORY"), out)
            self.assertFalse(os.path.exists(run))


if __name__ == "__main__":
    ev.utf8_stdio()
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    print("EVIDENCE_UNIT_TESTS_PASS" if ok else "EVIDENCE_UNIT_TESTS_FAIL")
    sys.exit(0 if ok else 1)
