"""Offline replay test of the EdgeFHE demo cloud console (demo/console): no server, no board, no serial port.

Part 1 (standard library only, no Wireshark):
  - the keyless field feed: the tshark arguments of the live feed, TCP flags, and the size labels of a synthetic form
    session in the record pattern of the real server (two 74 B NewSessionTickets, 69 B Finished, 273 B frame header,
    24 + 6 limb records of 16,401 B, 19 B close_notify), marked '≈'
  - the session tracker: SYN opens, a retransmitted SYN does not, FIN closes, close_notify closes after 2 s
  - server.log in the format of run_server.sh and efnet_server; CPP_E2E_SERVER_CONNECTION_STOP lines are attached to
    the connection that was alive at the stop, and an accept deadline of an idle server (TLS_DEADLINE with no SYN
    behind it) counts as idle, also when the next connection starts 0.1 s later
  - the probe reader: DTR and RTS are False before the port opens, nothing is ever written to the port, E_J steps
    across an accumulator reset, and Delta E over [SYN - 2 s, FIN + 0.5 s] minus the idle median
  - private folders: the PKI folder (after explicit grants to Everyone, Authenticated Users and BUILTIN\\Users) and a
    new run folder below an evidence root that grants Authenticated Users and Users access end up accessible to the
    current user and SYSTEM only (icacls, read back with icacls /save on Windows; chmod elsewhere); a missing PKI
    folder is refused; the wide evidence root is reported (EVIDENCE_ROOT_ACL_WARNING, read-only)
  - the repository guard: a run folder inside the repository is refused in any letter case and in the extended-length
    and administrative-share spellings of the same folder
Part 2 (tshark): a recorded run replayed twice, without decoding (the eavesdropper view) and with the per-session
decode (efhe_verify.quick_session and the demo-only TLS key log): every form session shows 24 upload and 6 download
limb records, labelled ct k//6, poly (k%6)//3, limb k%3 and poly k//3, limb k%3; the decoded frames give the same
request_sha256 and response_payload_sha256 as server.log; gates G2-G8 pass; the HTTP guards (Host, Origin,
X-EdgeFHE-Console, JSON body), the Content-Security-Policy and the event stream work; and the console state never
carries owner data (disclosed answers, salts, logits). The command line --replay --once prints the same sessions.

usage: python -B demo/console/tests/test_console_replay.py [--run DIR [--port N]] [--probe-csv FILE] [--keep]
  --run DIR        a recorded run directory (capture.pcapng or capture.pcap, tls_keys.log, server.log, server.pin,
                   client.pin), for example the 'uniform' directory that demo/evidence/tests/run_mock_chain.py --keep
                   leaves; without it the test records one: dumpcap on the loopback adapter while the Python TLS 1.3
                   mock (demo/evidence/tests/mock_efnet.py) serves the fictional presets M, M and K, 3 s apart, after
                   one connection refused before the server listens (a TCP stream without TLS: tshark's TLS stream
                   numbering then differs from tcp.stream for every later session, and the decode must still find
                   each session; the refused connection itself is shown as having no TLS record)
                   (Wireshark with Npcap on Windows; capture rights on lo elsewhere)
  --probe-csv F    probe samples host_s,t_ms,V,I_mA,P_mW,E_J recorded at another time; the test moves them onto the
                   time line of the capture. Without it the test writes synthetic samples with a known energy bump per
                   session and one accumulator reset, and checks Delta E against the bump.
  --keep           keep the work directory (it holds throwaway private keys and TLS key logs)
The server.log of the run is wrapped in the launcher lines of run_server.sh (EDGEFHE_CLOUD_SERVER_START, the
WARNING_DEMO_TLS_KEYLOG_ENABLED line of the key-log shim, EDGEFHE_CLOUD_SERVER_EXIT) and two idle TLS_DEADLINE lines.
Success: CONSOLE_REPLAY_PASS sessions=N ... up_limbs=24 down_limbs=6 per session (exit 0); a failed check prints
CONSOLE_REPLAY_FAIL (exit 1); a fixture that cannot be recorded prints CONSOLE_REPLAY_FIXTURE_UNAVAILABLE (exit 2).
Everything is written outside the repository. Python 3.10 or later; research demonstration only.
"""
import argparse
import contextlib
import hashlib
import http.client
import io
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE_DIR = os.path.dirname(HERE)
DEMO = os.path.dirname(CONSOLE_DIR)
REPO = os.path.dirname(DEMO)
EVIDENCE = os.path.join(DEMO, "evidence")
EVIDENCE_TESTS = os.path.join(EVIDENCE, "tests")
for _p in (EVIDENCE_TESTS, EVIDENCE, CONSOLE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import efhe_verify  # noqa: E402
import evidence_console as ec  # noqa: E402
import live_feed as lf  # noqa: E402
import probe_reader as pr  # noqa: E402

CONSOLE_PY = os.path.join(CONSOLE_DIR, "evidence_console.py")
MODEL = os.path.join(DEMO, "model", "cdc-diabetes-lr4-v1.json")
SCHEMA = os.path.join(DEMO, "model", "form_cdc.json")
WINDOWS = os.name == "nt"
FORMS = ("M", "M", "K")
GAP_S = 3.0
PLAN_FIELDS = ("frame.number", "frame.time_epoch", "ip.src", "tcp.srcport", "tcp.dstport", "tcp.len", "tcp.flags.str",
               "tls.record.opaque_type", "tls.record.content_type", "tls.record.length", "tls.handshake.type",
               "tls.handshake.extensions_server_name", "tls.handshake.extensions.supported_version",
               "tls.handshake.ciphersuite")
OWNER_ONLY = ("salt_hex", "canonical_json", "logit_plain", "logit_ckks", "logit_ckks_source", "boundary")
IDLE_MW, BUMP_MW = 8000.0, 1500.0
BUMP_PRE_S, BUMP_POST_S = 1.0, 0.2          # synthetic board activity [SYN - 1 s, FIN + 0.2 s] inside the window
T0 = 1795400000.0


def say(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def inside(path, root):
    """True when path lies in root: the console's own guard (links resolved, case-insensitive where the file system
    is, any spelling of the same folder)."""
    return ec.inside(path, root)


class FixtureUnavailable(Exception):
    pass


class Checks:
    def __init__(self):
        self.n = 0
        self.failed = []

    def ok(self, cond, what):
        self.n += 1
        if not cond:
            self.failed.append(what)
            say("  FAIL " + what)
        return bool(cond)

    def eq(self, got, want, what):
        return self.ok(got == want, "%s: got %r, expected %r" % (what, got, want))


# ------------------------------------------------------------------ synthetic packets (parse_line() dicts)
def pkt(frame, t, up, board_port, flags="", recs=(), tcp_len=0, hs=(), sni=None, versions=(), suites=(), port=18443):
    fl = dict(ack="A" in flags, psh="P" in flags, rst="R" in flags, syn="S" in flags, fin="F" in flags)
    return dict(frame=frame, t=t, src="192.0.2.20" if up else "192.0.2.10", sport=board_port if up else port,
                dport=port if up else board_port, tcp_len=tcp_len, flags=fl, records=list(recs), hs=list(hs),
                sni=sni, versions=list(versions), suites=list(suites))


def form_packets(t0, board_port, frame0=1, fin=True, close_notify=True):
    """A form session in the record pattern of efnet_server (OpenSSL 3, TLS_AES_256_GCM_SHA384)."""
    up, down = True, False
    enc = lambda n: (23, n, True)  # noqa: E731
    p = [pkt(0, t0, up, board_port, "S"), pkt(0, t0 + 0.001, down, board_port, "SA"),
         pkt(0, t0 + 0.002, up, board_port, "A"),
         pkt(0, t0 + 0.003, up, board_port, "AP", [(22, 238, False)], 243, hs=[1], sni="edgefhe-cloud",
             versions=[0x0304]),
         pkt(0, t0 + 0.006, down, board_port, "AP", [(22, 122, False), (20, 1, False), enc(23), enc(62), enc(875),
                                                     enc(96), enc(69)], 1278, hs=[2], versions=[0x0304],
             suites=[0x1302]),
         pkt(0, t0 + 0.010, up, board_port, "AP", [(20, 1, False), enc(874), enc(97), enc(69)], 1061),
         pkt(0, t0 + 0.012, down, board_port, "AP", [enc(74), enc(74)], 158),
         pkt(0, t0 + 0.300, up, board_port, "AP", [enc(273)], 278)]
    t = t0 + 0.301
    for _i in range(6):                                  # 24 limb records, 4 per segment
        p.append(pkt(0, t, up, board_port, "AP", [enc(16401)] * 4, 4 * 16406))
        t += 0.004
    p.append(pkt(0, t0 + 0.360, down, board_port, "AP", [enc(273)] + [enc(16401)] * 2, 278 + 2 * 16406))
    p.append(pkt(0, t0 + 0.362, down, board_port, "AP", [enc(16401)] * 4, 4 * 16406))
    tt = t0 + 0.370
    if close_notify:
        p.append(pkt(0, tt, up, board_port, "AP", [enc(19)], 24))
        p.append(pkt(0, tt + 0.001, down, board_port, "AP", [enc(19)], 24))
    if fin:
        p.append(pkt(0, tt + 0.002, up, board_port, "FA"))
        p.append(pkt(0, tt + 0.003, down, board_port, "FA"))
        p.append(pkt(0, tt + 0.004, up, board_port, "A"))
    for i, x in enumerate(p):
        x["frame"] = frame0 + i
    return p


def check_form_labels(ch, recs, where, approx):
    """24 upload and 6 download limb records with the ct/poly/limb mapping; '≈' exactly when approx."""
    ups = [r for r in recs if r["dir"] == "up" and r["group"] == "limb"]
    downs = [r for r in recs if r["dir"] == "down" and r["group"] == "limb"]
    ch.eq(len(ups), 24, where + " upload limb records")
    ch.eq(len(downs), 6, where + " download limb records")
    ch.ok(all(r["len"] == lf.REC_LIMB for r in ups + downs), where + " limb records are 16,401 B")
    for k, r in enumerate(ups):
        want = "ct%d c%d L%d" % (k // 6, (k % 6) // 3, k % 3)
        if not ch.ok(r["k"] == k and r["short"] == want, "%s upload limb %d labelled %s (got k=%s %s)" % (
                where, k, want, r["k"], r["short"])):
            break
    for k, r in enumerate(downs):
        want = "resp c%d L%d" % (k // 3, k % 3)
        if not ch.ok(r["k"] == k and r["short"] == want, "%s download limb %d labelled %s (got k=%s %s)" % (
                where, k, want, r["k"], r["short"])):
            break
    hdrs = [r for r in recs if r["group"] == "hdr"]
    ch.eq(sorted((r["dir"], r["len"]) for r in hdrs), [("down", 273), ("up", 273)], where + " frame header records")
    encrypted = [r for r in recs if r["enc"]]
    if approx:
        ch.ok(all(r["label"].startswith(lf.APPROX) and r["approx"] for r in encrypted),
              where + " every encrypted record label starts with '≈' before the decode")
    else:
        ch.ok(not any("≈" in r["label"] or r["approx"] for r in recs), where + " no '≈' after the decode")
    ch.ok(any(r["group"] == "close" and r["dir"] == "up" for r in recs), where + " client close_notify labelled")
    return len(ups), len(downs)


# ------------------------------------------------------------------ part 1: units
def unit_feed(ch):
    args = lf.tshark_live_args("tshark", r"\Device\NPF_Loopback", "host 192.0.2.20")
    fields = tuple(args[i + 1] for i, a in enumerate(args) if a == "-e")
    ch.eq(fields, PLAN_FIELDS, "live feed fields")
    joined = " ".join(args)
    for part in ("-i \\Device\\NPF_Loopback", "-f host 192.0.2.20", "-n -l", "-d tcp.port==18443,tls",
                 "-o tls.keylog_file: ", "-T fields", "-E separator=/t", "-E occurrence=a", "-E aggregator=,"):
        ch.ok(part in joined, "live feed argument %r" % part)
    ch.eq(sum(1 for a in args if "keylog" in a), 1, "live feed sets only an empty key log")
    flags = {"··········S·": "syn", "·······A··S·": "ack syn", "·······AP···": "ack psh", "·······A···F": "ack fin",
             "·······A·R··": "ack rst", "": ""}
    for text, want in flags.items():
        got = " ".join(k for k, v in lf.parse_flags(text).items() if v)
        ch.eq(got, want, "tcp.flags.str %r" % text)
    line = "\t".join(["7", "1795400000.123456", "192.0.2.20", "50000", "18443", "16406", "·······AP···", "23",
                      "", "16401", "", "", "", ""])
    p = lf.parse_line(line)
    ch.ok(p is not None and p["records"] == [(23, 16401, True)] and p["flags"]["psh"] and p["dport"] == 18443,
          "parse_line of one encrypted record")
    ch.eq(lf.parse_line("Capturing on 'Adapter for loopback traffic capture'"), None, "parse_line skips messages")

    events = []
    tr = lf.SessionTracker(18443, "192.0.2.20", on_event=lambda kind, s, extra: events.append((kind, s.id)))
    pk = form_packets(T0, 50001)
    pk.insert(1, dict(pk[0], frame=0))                    # a retransmitted SYN
    for x in pk:
        tr.feed(x)
    ch.eq(len(tr.sessions), 1, "retransmitted SYN opens no second session")
    s = tr.sessions[0]
    ch.eq((s.state, s.close_reason), ("closed", "FIN"), "FIN closes the session")
    ch.eq(s.suite, 0x1302, "ServerHello cipher suite")
    ch.eq(s.version, 0x0304, "ServerHello supported_version")
    ch.eq(s.sni, "edgefhe-cloud", "ClientHello server name")
    recs = s.records
    check_form_labels(ch, recs, "synthetic keyless", approx=True)
    nst = [r for r in recs if r["group"] == "nst"]
    ch.ok(len(nst) == 2 and all(r["len"] == 74 and r["label"].startswith("≈ NewSessionTicket") for r in nst),
          "two 74 B server records after the client Finished are labelled ≈ NewSessionTicket")
    fins = [(r["dir"], r["len"]) for r in recs if r["group"] == "fin"]
    ch.eq(fins, [("down", 69), ("up", 69)], "69 B Finished records")
    sig = s.signature()
    ch.eq((sig["classification"], sig["up_text"], sig["down_text"]), ("form", ["273+24x16401"], ["273+6x16401"]),
          "keyless record-size signature")
    ch.eq(s.end_time(), s.fin["up"], "energy window ends at the first FIN")
    ph = s.phases()
    ch.ok(ph["syn"] < ph["client_finished"] < ph["request_first"] <= ph["request_last"] < ph["response_first"]
          <= ph["response_last"] < ph["fin"], "wire phases in order")
    ch.eq([k for k, _sid in events].count("open"), 1, "one open event")

    tr2 = lf.SessionTracker(18443, "192.0.2.20")
    for x in form_packets(T0 + 100.0, 50002, fin=False):
        tr2.feed(x)
    s2 = tr2.sessions[0]
    tr2.tick(T0 + 100.0 + 0.371 + 1.5)
    ch.eq(s2.state, "open", "close_notify without FIN: still open after 1.5 s")
    tr2.tick(T0 + 100.0 + 0.371 + 2.1)
    ch.eq((s2.state, s2.close_reason), ("closed", "close_notify"), "close_notify without FIN: closed after 2 s")
    ch.eq(s2.end_time(), s2.close_notify_t, "without FIN the window ends at the close_notify")
    for x in form_packets(T0 + 200.0, 50002, frame0=100):  # the board reuses the port much later
        tr2.feed(x)
    ch.eq(len(tr2.sessions), 2, "a new SYN on a reused port opens a new session")
    for i, (dt, up, fl) in enumerate(((0.0, True, "S"), (0.0002, False, "RA"), (0.5, True, "S"), (0.5002, False, "RA"))):
        tr2.feed(pkt(200 + i, T0 + 300.0 + dt, up, 50003, fl))      # a refused attempt, repeated once by the stack
    s3 = tr2.sessions[-1]
    ch.ok(len(tr2.sessions) == 3 and s3.board_port == 50003 and s3.state == "closed" and s3.close_reason == "RST"
          and s3.packets == 4 and not s3.records, "a refused attempt and its repeated SYN form one session without TLS")


def launcher_lines(t0, port, connections, server_pin, client_pin):
    """The launcher lines of run_server.sh around efnet_server (the hashes are computed, never literals)."""
    sha = hashlib.sha256(b"fixture efnet_server").hexdigest()
    tree = hashlib.sha256(b"fixture staged tree").hexdigest()
    return ["%.9f EDGEFHE_CLOUD_SERVER_START listen=0.0.0.0 port=%d connections=%d timeout_ms=30000 keylog=on "
            "peer_name=edgefhe-kv260 server_pin=%s client_pin=%s efnet_server_sha256=%s staged_tree_id=%s commit=dev"
            % (t0, port, connections, server_pin, client_pin, sha, tree),
            "%.9f WARNING_DEMO_TLS_KEYLOG_ENABLED path=/evidence/tls_keys.log 示範專用：本伺服器將每一條 TLS 連線的"
            "工作階段金鑰寫入此檔；持有此檔者能解開本次擷取的 TLS 流量。" % (t0 + 0.004)]


def response_line(t, request_sha256, response_sha256, cloud_ns=673026):
    return "%.9f %s" % (t, json.dumps({"status": "CPP_E2E_RESPONSE_WRITTEN_NOT_OWNER_ACCEPTANCE", "sequence": 1,
                                        "request_sha256": request_sha256, "response_payload_sha256": response_sha256,
                                        "cloud_ns": cloud_ns}, separators=(",", ":")))


def replay_cfg():
    return ec.validate_config(dict(ec.DEFAULTS, listen="127.0.0.1:0"), live=False)


def unit_server_log(ch, work):
    pin_s, pin_c = hashlib.sha256(b"server leaf").hexdigest(), hashlib.sha256(b"client leaf").hexdigest()
    lines = launcher_lines(T0, 18443, 10000, pin_s, pin_c)
    lines.append("%.9f %s" % (T0 + 0.008, '{"status":"CPP_E2E_SERVER_READY","port":18443,"test_injection":false}'))
    kinds = [ec.parse_server_line(x)["kind"] for x in lines]
    ch.eq(kinds, ["launcher_start", "keylog_warning", "ready"], "launcher, shim and server start lines")
    start = ec.parse_server_line(lines[0])
    ch.eq((start["epoch"], start["data"].get("timeout_ms"), start["data"].get("client_pin")), (T0, "30000", pin_c),
          "launcher start fields")
    ch.eq(ec.parse_server_line(lines[1])["data"].get("path"), "/evidence/tls_keys.log", "key-log warning path")
    rq = hashlib.sha256(b"request A").hexdigest()
    rs = hashlib.sha256(b"response A").hexdigest()
    e = ec.parse_server_line(response_line(T0 + 10.365, rq, rs))
    ch.eq((e["kind"], e["data"]["request_sha256"], e["data"]["cloud_ns"]), ("response", rq, 673026),
          "response line")
    ch.eq(ec.parse_server_line("%.9f CPP_E2E_SERVER_CONNECTION_STOP TLS_DEADLINE" % T0)["data"], {"code": "TLS_DEADLINE"},
          "connection stop line")
    ch.eq(ec.parse_server_line("%.9f EDGEFHE_CLOUD_SERVER_EXIT rc=143 stop_signal=TERM" % T0)["data"],
          {"rc": "143", "stop_signal": "TERM"}, "launcher exit line")

    # Stops and sessions on one time line (seconds after T0):
    #   A 10.0-10.374 a form; idle deadline at 40.30; B starts 0.1 s later (40.40); C 50.0-50.378 refused by the
    #   server (stop at 50.380); D starts at 50.390, 12 ms after C; E opens at 60.0 and stalls, the server's
    #   TLS_DEADLINE comes at 90.0 with its FIN; another idle deadline at 120.0.
    run = os.path.join(work, "unit_stops")
    os.makedirs(run)
    c = ec.Console(replay_cfg(), "replay", run, 18443)
    try:
        packets = []
        packets += form_packets(T0 + 10.0, 50010, frame0=1)
        packets += form_packets(T0 + 40.40, 50011, frame0=100)
        refused = form_packets(T0 + 50.0, 50012, frame0=200)
        packets += refused
        packets += form_packets(T0 + 50.39, 50013, frame0=300)
        stall = [pkt(400, T0 + 60.0, True, 50014, "S"), pkt(401, T0 + 60.001, False, 50014, "SA"),
                 pkt(402, T0 + 60.002, True, 50014, "A"),
                 pkt(403, T0 + 60.003, True, 50014, "AP", [(22, 238, False)], 243, hs=[1], sni="edgefhe-cloud"),
                 pkt(404, T0 + 90.001, False, 50014, "FA"), pkt(405, T0 + 90.002, True, 50014, "FA")]
        packets += stall
        packets.sort(key=lambda x: x["t"])
        for x in packets:
            c.feed_packet(x)
        log = lines + [response_line(T0 + 10.365, rq, rs),
                       "%.9f CPP_E2E_SERVER_CONNECTION_STOP TLS_DEADLINE" % (T0 + 40.30),
                       "%.9f CPP_E2E_SERVER_CONNECTION_STOP WIRE_KEY_GENERATION_CHANGE" % (T0 + 50.380),
                       "%.9f CPP_E2E_SERVER_CONNECTION_STOP TLS_DEADLINE" % (T0 + 90.0),
                       "%.9f CPP_E2E_SERVER_CONNECTION_STOP TLS_DEADLINE" % (T0 + 120.0)]
        for x in log:
            c.server_line(x)
        c.rematch()
        st = c.state()
        got = dict((s["id"], (s["connection_stop"] or {}).get("code")) for s in st["sessions"])
        ch.eq(got, {"s01": None, "s02": None, "s03": "WIRE_KEY_GENERATION_CHANGE", "s04": None, "s05": "TLS_DEADLINE"},
              "connection stops attached to the connection alive at the stop")
        ch.eq(st["status"]["server"]["idle_deadlines"], 2, "idle accept deadlines counted as idle")
        idle = [x["idle"] for x in st["status"]["server"]["stops"]]
        ch.eq(idle, [True, False, False, True], "idle flags of the stop lines")
        s01 = st["sessions"][0]
        ch.eq((s01["server_log"] or {}).get("match"), "time", "response joined by time before any decode")
        ch.eq(s01["hashes"]["server_log"]["request"], rq, "server.log request hash shown for the session")
        ch.eq(st["status"]["server"]["launcher"].get("connections"), "10000", "launcher fields in the state")
    finally:
        c.shutdown()


def unit_guards(ch):
    """A run directory inside the repository is refused before anything is created, whatever the letter case and
    whatever the spelling of the same folder (extended-length \\\\?\\C:\\..., \\\\?\\UNC\\localhost\\C$\\..., and the
    administrative share \\\\localhost\\C$\\... where this PC offers it)."""
    target = os.path.join(REPO, "demo", "console_run_dir_must_not_exist")
    variants = [("as given", target)]
    if WINDOWS:
        full = os.path.abspath(target)
        drive, rest = os.path.splitdrive(full)
        variants.append(("swapped case", target.swapcase()))
        variants.append(("extended-length", "\\\\?\\" + full.swapcase()))
        if len(drive) == 2 and drive[1] == ":":
            share = "\\\\localhost\\%s$" % drive[0]
            if os.path.isdir(share + "\\"):              # the administrative share is optional (and may be disabled)
                variants.append(("administrative share", share + rest))
                variants.append(("extended-length share", "\\\\?\\UNC\\localhost\\%s$%s" % (drive[0], rest)))
    for how, v in variants:
        if not ch.ok(ec.inside(v, REPO), "inside() places the %s path in the repository (%s)" % (how, v)):
            continue                                     # never let resolve_run_dir create a folder in the repository
        try:
            ec.resolve_run_dir(dict(ec.DEFAULTS), v)
            code = None
        except ec.Refused as e:
            code = e.code
        ch.ok(code == "RUN_IN_REPO" and not os.path.exists(target),
              "a run directory inside the repository is refused, %s (%s)" % (how, code))
    ch.ok(not ec.inside(REPO + "_other", REPO), "a sibling directory with the same prefix is outside the repository")
    ch.ok(not ec.inside(os.path.join(tempfile.gettempdir(), "edgefhe_console_x"), REPO),
          "a folder outside the repository stays outside")


def unit_acl(ch, work):
    """The PKI folder and a new run folder end up private to the current user and SYSTEM. On Windows the test PKI
    folder first gets explicit grants for Everyone and Authenticated Users, and a file in it stops inheriting and
    grants BUILTIN\\Users; afterwards icacls /save shows the user and SYSTEM only, for the folder, its sub-folder and
    every file. A missing folder is refused."""
    base = os.path.join(work, "acl")
    pki = os.path.join(base, "pki")
    os.makedirs(os.path.join(pki, "cloud"))
    for rel in (("cloud", "server.key"), ("cloud", "server.pem"), ("ca.key",)):
        with open(os.path.join(pki, *rel), "w", encoding="ascii") as f:
            f.write("placeholder for the access test, not a key\n")
    user = None
    if WINDOWS:
        icacls = ec._system_tool("icacls.exe")
        for args in ([pki, "/grant", "*S-1-1-0:(OI)(CI)R", "*S-1-5-11:(OI)(CI)M"],
                     [os.path.join(pki, "ca.key"), "/inheritance:d"],
                     [os.path.join(pki, "ca.key"), "/grant", "*S-1-5-32-545:R"]):
            r = subprocess.run([icacls] + args + ["/Q"], capture_output=True, stdin=subprocess.DEVNULL)
            ch.eq(r.returncode, 0, "icacls %s widens the test folder" % args[1])
        user = ec.current_user_sid()
        wide = [ec.acl_problems(s, user, i == 0) for i, (_n, s) in enumerate(ec.acl_table(pki))]
        ch.ok(all(wide) and any("S-1-5-11" in p or "AU" in p for p in wide),
              "before the restriction every entry grants other accounts (%s)" % wide[0])
        # the read-only warning for folders the console does not restrict (evidence root, replayed folder)
        grants = ec.wide_grants(pki, tree=True)
        ch.ok(grants is not None and {"S-1-1-0", "S-1-5-11", "S-1-5-32-545"} <= set(grants),
              "wide_grants lists Everyone, Authenticated Users and Users before the restriction (%s)" % grants)
        ch.ok(set(ec.wide_grants(pki) or ()) >= {"S-1-1-0", "S-1-5-11"},
              "wide_grants of the folder alone lists its own grants")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ec.warn_wide("EVIDENCE_REPLAY_ACL_WARNING", pki, True, "")
        ch.ok(out.getvalue().startswith("EVIDENCE_REPLAY_ACL_WARNING path=") and "6.3.1" in out.getvalue(),
              "warn_wide prints its marker and points to README section 6.3.1")
    else:
        os.chmod(pki, 0o755)
        os.chmod(os.path.join(pki, "ca.key"), 0o644)
    cfg = dict(ec.DEFAULTS, server="mock", pki_dir=pki)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ec.private_pki(cfg)
    ch.ok("EVIDENCE_PKI_PRIVATE" in out.getvalue(), "private_pki prints EVIDENCE_PKI_PRIVATE")

    def private(path):
        if WINDOWS:
            return [(n, p) for i, (n, s) in enumerate(ec.acl_table(path)) for p in ec.acl_problems(s, user, i == 0)]
        open_entries = []
        for root, _dirs, files in os.walk(path):
            for p in [root] + [os.path.join(root, x) for x in files]:
                if os.stat(p).st_mode & 0o077:
                    open_entries.append((p, oct(os.stat(p).st_mode & 0o777)))
        return open_entries
    ch.eq(private(pki), [], "the PKI folder, its sub-folder and every file are private to the user and SYSTEM")
    if WINDOWS:
        ch.eq(ec.wide_grants(pki, tree=True), [], "wide_grants finds no wide group after the restriction")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ec.warn_wide("EVIDENCE_REPLAY_ACL_WARNING", pki, True, "")
        ch.eq(out.getvalue(), "", "warn_wide prints nothing for a private folder")
    with open(os.path.join(pki, "cloud", "server.key"), encoding="ascii") as f:
        ch.ok(f.read().startswith("placeholder"), "the files stay readable by the current user")
    for target, call, want in ((os.path.join(base, "missing"), lambda p: ec.make_private(p, "PKI_ACL"), "PKI_ACL"),
                               (os.path.join(base, "missing"), lambda p: ec.private_pki(
                                   dict(ec.DEFAULTS, server="docker", pki_dir=p)), "PKI_MISSING")):
        try:
            call(target)
            code = None
        except ec.Refused as e:
            code = e.code
        ch.eq(code, want, "a missing PKI folder is refused (%s)" % want)
    root = os.path.join(base, "evidence")
    os.makedirs(root)
    if WINDOWS:                                          # as C:\EdgeFHE_evidence before README section 6.3.1
        r = subprocess.run([ec._system_tool("icacls.exe"), root, "/grant", "*S-1-5-11:(OI)(CI)M",
                            "*S-1-5-32-545:(OI)(CI)RX", "/Q"], capture_output=True, stdin=subprocess.DEVNULL)
        ch.eq(r.returncode, 0, "icacls widens the test evidence root")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        run = ec.resolve_run_dir(dict(ec.DEFAULTS, evidence_root=root), None)
    ch.ok(os.path.isdir(run) and not os.listdir(run) and "EVIDENCE_RUN_PRIVATE" in out.getvalue(),
          "resolve_run_dir creates an empty run folder and prints EVIDENCE_RUN_PRIVATE")
    ch.eq(private(run), [], "the new run folder is private to the user and SYSTEM, also below a wide evidence root")
    if WINDOWS:
        ch.ok("EVIDENCE_ROOT_ACL_WARNING path=%s grants=" % root in out.getvalue() and "S-1-5-11" in out.getvalue(),
              "a wide evidence root is reported with EVIDENCE_ROOT_ACL_WARNING (%s)" % out.getvalue().strip()[-160:])
    if WINDOWS:                                          # NTFS: new files inherit the folder's access list
        with open(os.path.join(run, "capture.pcapng"), "wb") as f:
            f.write(b"\0")
        ch.eq(private(run), [], "a file created in the run folder inherits the private access list")
        # icacls unavailable: refused, and nothing is left behind
        target = os.path.join(root, "no_icacls")
        saved = ec._system_tool
        ec._system_tool = lambda name: os.path.join(base, "missing", name)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ec.resolve_run_dir(dict(ec.DEFAULTS), target)
            code = None
        except ec.Refused as e:
            code = e.code
        finally:
            ec._system_tool = saved
        ch.ok(code == "RUN_ACL" and not os.path.exists(target),
              "a run folder whose access cannot be restricted is refused and removed (%s)" % code)


class FakeSerial:
    """Stands in for serial.Serial: records attribute assignments, open() and any write."""

    def __init__(self, lines=()):
        self.events = []
        self.lines = list(lines)
        self.writes = []
        self.is_open = False
        self._dtr = self._rts = None
        self.port = self.baudrate = self.timeout = None

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, v):
        self.events.append(("dtr", v))
        self._dtr = v

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, v):
        self.events.append(("rts", v))
        self._rts = v

    def open(self):
        self.events.append(("open", None))
        self.is_open = True

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        time.sleep(0.05)
        return b""

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def reset_input_buffer(self):
        self.events.append(("reset_input_buffer", None))

    def close(self):
        self.is_open = False


def unit_probe(ch, work):
    ch.eq(pr.parse_sample("4990873,12.1414,660.70,8021.9,12582.310"), (4990873, 12.1414, 660.70, 8021.9, 12582.310),
          "probe sample line")
    ch.eq(pr.parse_sample("# run begin"), None, "probe message line")
    ch.eq(pr.parse_sample("1,2,3"), None, "broken probe line")

    # DTR and RTS False before open; pyserial is imported only here (a stand-in module)
    fake = FakeSerial()
    saved = sys.modules.get("serial")
    sys.modules["serial"] = types.SimpleNamespace(Serial=lambda: fake)
    try:
        port = pr.open_serial("COM5")
    finally:
        if saved is None:
            sys.modules.pop("serial", None)
        else:
            sys.modules["serial"] = saved
    order = [e[0] for e in fake.events]
    ch.ok(port is fake and order.index("dtr") < order.index("open") and order.index("rts") < order.index("open")
          and fake.events[order.index("dtr")][1] is False and fake.events[order.index("rts")][1] is False,
          "DTR and RTS set to False before the port opens (%s)" % order)

    # the reader never writes: no B, E, r or S command, no reset of the input buffer
    lines = [b"# EdgeFHE probe\n", b"4990873,12.1414,660.70,8021.9,12582.310\n", b"garbage\n",
             b"4990973,12.1424,665.97,8086.4,12583.119\n", b"# energy reset\n", b"4991073,12.1426,664.14,8064.4,0.020\n"]
    fake2 = FakeSerial(lines)
    got, states = [], []
    csv_path = os.path.join(work, "unit_probe.csv")
    rd = pr.ProbeReader("COM5", csv_path, on_sample=lambda t, s: got.append((t, s)),
                        on_status=lambda st, d: states.append(st), opener=lambda p, b: fake2)
    t_before = time.time()
    rd.start()
    t_end = time.time() + 5
    while len(got) < 3 and time.time() < t_end:
        time.sleep(0.05)
    rd.stop()
    ch.eq(len(got), 3, "probe samples read")
    ch.eq(fake2.writes, [], "the probe reader never writes to the port")
    ch.ok(not any(e[0] == "reset_input_buffer" for e in fake2.events), "the probe reader never resets the port")
    ch.ok(all(t_before <= t <= time.time() for t, _s in got), "samples stamped with the host clock on arrival")
    ch.eq((rd.messages, rd.resets_seen), (2, 1), "probe messages and accumulator resets counted")
    ch.ok("streaming" in states, "probe status streaming")
    rows = pr.load_csv(csv_path)
    with open(csv_path, encoding="utf-8") as f:
        head = f.readline().strip()
    ch.eq(head, pr.HEADER, "probe.csv header (report_html.py reads host_s and P_mW)")
    ch.eq([r[1] for r in rows], [4990873, 4990973, 4991073], "probe.csv rows")

    # E_J differences; a reset step is bridged with the trapezoid of P; Delta E against the idle median
    samples, e = [], 100.0
    for k in range(200):                                 # 1000.0 s ... 1019.9 s at 10 Hz; bump over 1010-1012 s
        t = 1000.0 + k / 10.0
        p = IDLE_MW + (BUMP_MW if 100 <= k < 120 else 0.0)
        if k == 150:
            e = 0.010                                    # accumulator reset (a '# run begin' on the probe)
        samples.append((t, k * 100, 12.1, p / 12.1, p, e))
        e += p / 1000.0 * 0.1
    r = pr.energy_between(samples, 1000.0, 1019.9)
    ch.ok(r["resets"] == 1 and abs(r["joules"] - (IDLE_MW * 19.9 + BUMP_MW * 2.0) / 1000.0) < 0.01,
          "energy across one accumulator reset (%s)" % r)
    se = pr.session_energy(samples, 1011.0, 1011.5, IDLE_MW)
    ch.ok(se["approx"] is True and se["window"] == [1009.0, 1012.0] and abs(se["delta_j"] - BUMP_MW * 2.0 / 1000.0)
          < 0.01, "session Delta E over [SYN - 2 s, FIN + 0.5 s] minus idle (%s)" % se)
    ch.eq(pr.idle_power_mw(samples, [(1009.0, 1012.0)]), IDLE_MW, "idle median outside the session windows")


# ------------------------------------------------------------------ part 2: fixture and replay
def record_fixture(work, iface):
    """dumpcap on the loopback adapter while the mock serves FORMS, GAP_S apart. Returns (run_dir, port)."""
    try:
        import mock_efnet
        import run_mock_chain as rmc
    except ImportError as e:
        raise FixtureUnavailable("demo/evidence/tests: %s" % e)
    if not efhe_verify.find_tool("dumpcap"):
        raise FixtureUnavailable("dumpcap not found (Wireshark)")
    pki = os.path.join(work, "pki")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rmc.make_pki(pki)
    except (SystemExit, OSError, ImportError) as e:
        raise FixtureUnavailable("PKI: %s" % e)
    run = os.path.join(work, "recorded")
    os.makedirs(run)
    port = rmc.free_port()
    try:
        cap = rmc.Dumpcap(iface, port, os.path.join(run, "capture.pcapng"), os.path.join(run, "capture_stats.txt"))
    except SystemExit as e:
        raise FixtureUnavailable("dumpcap on %s: %s" % (iface, e))
    result = {}
    try:
        # a refused connection before the server listens (as when the board connects before the cloud is ready):
        # a TCP stream without TLS, so tshark's TLS stream numbering differs from tcp.stream for every later session
        probe = socket.socket()
        probe.settimeout(1.0)
        try:
            probe.connect(("127.0.0.1", port))
        except OSError:
            pass
        finally:
            probe.close()
        time.sleep(0.5)
        ready = threading.Event()

        def serve():
            result["rc"] = mock_efnet.server_main(pki, port, os.path.join(run, "tls_keys.log"),
                                                  os.path.join(run, "server.log"), len(FORMS), MODEL, "127.0.0.1",
                                                  ready)
        t = threading.Thread(target=serve, name="mock-server", daemon=True)
        t.start()
        if not ready.wait(15):
            raise FixtureUnavailable("the mock server did not start")
        for i, form in enumerate(FORMS):
            time.sleep(GAP_S)
            with contextlib.redirect_stdout(io.StringIO()):
                mock_efnet.client_main(pki, "127.0.0.1", port, os.path.join(work, "owner_%d" % (i + 1)), [form],
                                       MODEL, SCHEMA)
        t.join(60)
        time.sleep(GAP_S)
    finally:
        try:
            cap.stop()
        except SystemExit as e:
            raise FixtureUnavailable("dumpcap stop: %s" % e)
    if result.get("rc") != 0:
        raise FixtureUnavailable("the mock server rejected a connection (rc=%s)" % result.get("rc"))
    for role in ("server", "client"):
        shutil.copyfile(os.path.join(pki, "ca", role + ".pin"), os.path.join(run, role + ".pin"))
    # the owner records of the separate client runs, with distinct run names (one client process per form)
    os.makedirs(os.path.join(run, "disclosure"))
    with open(os.path.join(run, "board_public.jsonl"), "w", encoding="utf-8", newline="\n") as out:
        for i in range(len(FORMS)):
            src = os.path.join(work, "owner_%d" % (i + 1))
            with open(os.path.join(src, "board_public.jsonl"), encoding="utf-8") as f:
                for line in f:
                    row = json.loads(line)
                    row["run"] = "mock-%02d-%s" % (i + 1, FORMS[i])
                    out.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            for name in os.listdir(os.path.join(src, "disclosure")):
                shutil.copyfile(os.path.join(src, "disclosure", name), os.path.join(run, "disclosure", name))
    return run, port


def prepare_replay(src, dst):
    """Copy the evidence inputs the console reads, and wrap server.log in the launcher lines (see the docstring)."""
    os.makedirs(dst)
    cap = "capture.pcapng" if os.path.isfile(os.path.join(src, "capture.pcapng")) else "capture.pcap"
    for name in (cap, "tls_keys.log", "server.pin", "client.pin", "board_public.jsonl"):
        if os.path.isfile(os.path.join(src, name)):
            shutil.copyfile(os.path.join(src, name), os.path.join(dst, name))
    if os.path.isdir(os.path.join(src, "disclosure")):
        shutil.copytree(os.path.join(src, "disclosure"), os.path.join(dst, "disclosure"))
    for need in (cap, "tls_keys.log", "server.pin", "client.pin"):
        if not os.path.isfile(os.path.join(dst, need)):
            raise FixtureUnavailable("the run directory lacks %s" % need)
    if not os.path.isfile(os.path.join(src, "server.log")):
        raise FixtureUnavailable("the run directory lacks server.log")
    with open(os.path.join(src, "server.log"), encoding="utf-8") as f:
        log = [x.rstrip("\r\n") for x in f if efhe_verify.EPOCH_PREFIX.match(x.rstrip("\r\n"))]
    if not log:
        raise FixtureUnavailable("server.log has no line with the epoch prefix of run_server.sh")
    first = efhe_verify.EPOCH_PREFIX.match(log[0])
    last = efhe_verify.EPOCH_PREFIX.match(log[-1])
    with open(os.path.join(dst, "server.pin"), encoding="ascii") as f:
        spin = f.read().strip()
    with open(os.path.join(dst, "client.pin"), encoding="ascii") as f:
        cpin = f.read().strip()
    port = ec.detect_port(src, 18443)
    return dst, cap, port, log, float(first.group(1)), float(last.group(1)), spin, cpin


def write_server_log(dst, log, t_first, port, spin, cpin, idle_times, t_exit):
    """The recorded server.log plus the launcher lines of run_server.sh (when the recording has none: the mock writes
    efnet_server's lines only) and the idle accept deadlines, merged in time order."""
    texts = [efhe_verify.EPOCH_PREFIX.match(x).group(2) for x in log]
    extra = []
    if not any(t.startswith("EDGEFHE_CLOUD_SERVER_START") for t in texts):
        extra += launcher_lines(t_first - 0.010, port, 10000, spin, cpin)
        log = [x for x, t in zip(log, texts) if not t.startswith("WARNING_DEMO_TLS_KEYLOG_ENABLED")]
    extra += ["%.9f CPP_E2E_SERVER_CONNECTION_STOP TLS_DEADLINE" % t for t in idle_times]
    if not any(t.startswith("EDGEFHE_CLOUD_SERVER_EXIT") for t in texts):
        extra.append("%.9f EDGEFHE_CLOUD_SERVER_EXIT rc=143 stop_signal=TERM" % t_exit)
    lines = sorted(log + extra, key=lambda x: float(efhe_verify.EPOCH_PREFIX.match(x).group(1)))
    with open(os.path.join(dst, "server.log"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def synthetic_probe(path, windows, t_lo, t_hi, reset_at=None, seed=20261124):
    """10 Hz samples: idle power with small noise and a bump of BUMP_MW over each [SYN - 1 s, FIN + 0.2 s]; E_J is the
    exact integral (like the INA228 accumulator), reset once at reset_at. Returns the expected joules per window."""
    rng = random.Random(seed)
    bumps = [(a - BUMP_PRE_S, b + BUMP_POST_S) for a, b in windows]

    def bump_overlap(x0, x1):
        return sum(max(0.0, min(x1, b1) - max(x0, b0)) for b0, b1 in bumps)
    rows, e, t, t_ms, done_reset = [], 12345.678, t_lo, 4990873, False
    while t <= t_hi:
        noise = rng.uniform(-10.0, 10.0)
        in_bump = any(b0 <= t < b1 for b0, b1 in bumps)
        p = IDLE_MW + noise + (BUMP_MW if in_bump else 0.0)
        if reset_at is not None and not done_reset and t >= reset_at:
            e, done_reset = 0.012, True
        rows.append(pr.format_row(t, (t_ms, 12.14, p / 12.14, p, e)))
        dt = 0.1 + rng.uniform(-0.004, 0.004)
        e += ((IDLE_MW + noise) * dt + BUMP_MW * bump_overlap(t, t + dt)) / 1000.0
        t += dt
        t_ms += 100
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(pr.HEADER + "\n" + "\n".join(rows) + "\n")
    return [BUMP_MW * (b1 - b0) / 1000.0 for b0, b1 in bumps]


def http_checks(ch, console):
    httpd = ec.make_server(console, 0)
    t = threading.Thread(target=httpd.serve_forever, kwargs=dict(poll_interval=0.1), daemon=True)
    t.start()
    port = httpd.server_port

    def req(method, path, headers=None, body=None, host="default"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not None:
            c.putheader("Host", "127.0.0.1:%d" % port if host == "default" else host)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        if body is not None:
            c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), data
    try:
        st, hd, body = req("GET", "/")
        ch.ok(st == 200 and hd.get("content-security-policy") == ec.CSP and hd.get("x-content-type-options") ==
              "nosniff" and hd.get("cache-control") == "no-store" and "雲端證據主控台" in body.decode("utf-8"),
              "GET / with CSP, nosniff and no-store (%s)" % st)
        ch.ok(b"http://" not in body and b"https://" not in body, "the page loads no external resource")
        st, hd, _b = req("GET", "/console.js")
        ch.ok(st == 200 and hd.get("content-type", "").startswith("text/javascript"), "GET /console.js")
        st, hd, _b = req("GET", "/console.css")
        ch.ok(st == 200 and hd.get("content-type", "").startswith("text/css"), "GET /console.css")
        for path in ("/web/../evidence_console.py", "/..%2fevidence_console.py", "//etc/passwd", "/web/console.py",
                     "/api/v1/../../evidence_console.py"):
            ch.eq(req("GET", path)[0], 404, "static path %s" % path)
        st, _hd, body = req("GET", "/api/v1/state")
        state = json.loads(body.decode("utf-8")) if st == 200 else {}
        ch.ok(st == 200 and state.get("mode") == "replay" and len(state.get("sessions", [])) >= 2,
              "GET /api/v1/state")
        ch.eq(req("GET", "/api/v1/state", host="evil.example:%d" % port)[0], 403, "foreign Host refused")
        ch.eq(req("GET", "/api/v1/state", host=None)[0], 403, "missing Host refused")
        ch.eq(req("GET", "/api/v1/state", headers={"Origin": "http://evil.example"})[0], 403, "cross Origin refused")
        ch.eq(req("GET", "/api/v1/state", headers={"Origin": "http://127.0.0.1:%d" % port})[0], 200,
              "same Origin accepted")
        jhdr = {"Content-Type": "application/json", "X-EdgeFHE-Console": "1"}
        ch.eq(req("POST", "/api/v1/seal", headers={"Content-Type": "application/json"}, body=b"{}")[0], 403,
              "POST without X-EdgeFHE-Console refused")
        ch.eq(req("POST", "/api/v1/seal", headers={"Content-Type": "text/plain", "X-EdgeFHE-Console": "1"},
                  body=b"{}")[0], 403, "POST without a JSON content type refused")
        ch.eq(req("POST", "/api/v1/seal", headers=jhdr, body=b"{" + b" " * 2000 + b"}")[0], 403,
              "POST body over 1 KiB refused")
        ch.eq(req("POST", "/api/v1/seal", headers=dict(jhdr, Origin="http://evil.example"), body=b"{}")[0], 403,
              "cross-origin POST refused")
        ch.eq(req("POST", "/api/v1/seal", headers=jhdr, body=b"[]")[0], 400, "POST body must be a JSON object")
        st, _hd, body = req("POST", "/api/v1/seal", headers=jhdr, body=b"{}")
        ch.ok(st == 409 and b"REPLAY" in body, "replay mode refuses to seal (%s)" % st)
        ch.eq(req("POST", "/api/v1/verify", headers=jhdr, body=b"{}")[0], 409, "replay mode refuses to verify")
        ch.eq(req("POST", "/api/v1/other", headers=jhdr, body=b"{}")[0], 404, "unknown POST path")
        ch.eq(req("GET", "/api/v1/events?since=abc")[0], 400, "event stream with a bad since")
        ch.eq(req("GET", "/evidence/report.html")[0], 404, "no report.html before the verification")
        # the event stream: headers, retry hint, then an event or a heartbeat within the 2 s heartbeat period
        s = socket.create_connection(("127.0.0.1", port), timeout=6)
        s.sendall(("GET /api/v1/events?since=0 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % port).encode())
        buf, t_end = b"", time.time() + 5
        while time.time() < t_end and not (b": heartbeat" in buf or b"\ndata: " in buf):
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        s.close()
        head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        ch.ok(" 200 " in head.split("\r\n")[0] and "text/event-stream" in head and "Content-Security-Policy" in head
              and b"retry: 2000" in buf and (b": heartbeat" in buf or b"\ndata: " in buf),
              "event stream answers with events or a heartbeat")
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.httpd = None


def disclosed_secrets(run):
    """Salts and canonical answers of the fictional disclosures in the run (owner data the console must not show)."""
    out = []
    d = os.path.join(run, "disclosure")
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            try:
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    obj = json.load(f)
            except (OSError, ValueError):
                continue
            for k in ("salt_hex", "canonical_json"):
                if isinstance(obj.get(k), str) and obj[k]:
                    out.append(obj[k])
    return out


def replay_part(ch, work, run_src, port_arg, probe_csv):
    dst, _cap, port, log, t_log0, _t_log1, spin, cpin = prepare_replay(run_src, os.path.join(work, "replay"))
    port = port_arg or port
    cfg = replay_cfg()
    # first pass: the session times from the keyless feed (no server.log, probe or decode yet)
    first = ec.build_replay(dst, cfg, port=port, decode=False)
    sess = [(s.t_syn or s.t_first, s.end_time()) for s in first.tracker.sessions]
    first.shutdown()
    if len(sess) < 2:
        ch.ok(False, "the recorded run has at least 2 sessions (found %d)" % len(sess))
        return None
    t_lo, t_hi = min(a for a, _b in sess), max(b for _a, b in sess)
    # idle deadlines: 0.1 s before the first SYN (the next connection starts right after) and 30 s after the last
    idle_times = [t_lo - 0.1, t_hi + 30.0]
    write_server_log(dst, log, t_log0, port, spin, cpin, idle_times, t_hi + 31.0)
    expected = None
    shift = 0.0
    if probe_csv:
        rows = pr.load_csv(probe_csv)
        shift = (t_lo - 60.0) - rows[0][0]
        if rows[-1][0] + shift < t_hi + 10.0:
            say("  NOTE the probe recording is shorter than the session span; some windows may lack samples")
        probe_path, probe_kind = probe_csv, "recorded probe.csv moved by %+.3f s" % shift
    else:
        probe_path = os.path.join(dst, "probe.csv")
        reset_at = sess[1][0] - 1.6                     # inside the second window, before its bump
        expected = synthetic_probe(probe_path, sess, t_lo - 30.0, t_hi + 30.0, reset_at)
        probe_kind = "synthetic (bump %.1f W per session, one accumulator reset)" % (BUMP_MW / 1000.0)
    say("CONSOLE_REPLAY_FIXTURE sessions=%d port=%d probe=%s" % (len(sess), port, probe_kind))

    # the eavesdropper view: keyless labels only
    nokey = ec.build_replay(dst, cfg, port=port, probe_csv=probe_path, probe_shift=shift, decode=False)
    keyless = {}
    try:
        for s in nokey.tracker.sessions:
            d = s.to_dict()
            if d["signature"]["classification"] == "form":
                keyless[s.id] = check_form_labels(ch, d["records"], "%s keyless" % s.id, approx=True)
            ch.eq(d["records_source"], "live", "%s labels come from the keyless feed" % s.id)
    finally:
        nokey.shutdown()

    # the decoded view
    console = ec.build_replay(dst, cfg, port=port, probe_csv=probe_path, probe_shift=shift, decode=True)
    summary = dict(sessions=0, forms=0, refused=0, decoded=0, joined=0, energy=0)
    try:
        st = console.state()
        # the footer shows the port that the replay decodes on (from server.log), not the configured default
        ch.eq((st["port"], st["status"]["server"]["port"]), (port, port), "the state carries the replay port")
        if not port_arg:
            ch.eq(ec.detect_port(dst), port, "the replay port is detected from server.log")
        sessions = st["sessions"]
        forms = [s for s in sessions if s["records"]]
        summary["sessions"] = len(sessions)
        ch.ok(len(sessions) >= 2 and len(forms) >= 2, "sessions >= 2 (found %d, %d with TLS records)" % (
            len(sessions), len(forms)))
        responses = dict((e["data"].get("request_sha256"), e["data"]) for e in console.responses)
        for i, s in enumerate(sessions):
            if not s["records"]:                         # a refused connection (SYN, RST): no TLS at all
                ch.eq(((s["decode"] or {}).get("state"), s["signature"]["classification"]), ("notls", "handshake_only"),
                      "%s refused connection without TLS is not decoded" % s["id"])
                summary["refused"] += 1
                say("CONSOLE_REPLAY_SESSION id=%s board=%s refused connection (no TLS record) decode=notls" % (
                    s["id"], s["board"]))
                continue
            sig = s["signature"]["classification"]
            dec = s["decode"] or {}
            res = dec.get("result") or {}
            ch.eq(sig, "form", "%s keyless signature" % s["id"])
            if sig != "form":
                continue
            summary["forms"] += 1
            ch.eq(keyless.get(s["id"]), (24, 6), "%s keyless limb records 24 up / 6 down" % s["id"])
            ups, downs = check_form_labels(ch, s["records"], "%s decoded" % s["id"], approx=False)
            ch.eq(s["records_source"], "capture", "%s labels from the decrypted record types" % s["id"])
            ok = ch.ok(dec.get("state") == "done" and dec.get("ok") and res.get("classification") == "form",
                       "%s decoded as one CDC form (%s)" % (s["id"], dec.get("state")))
            summary["decoded"] += 1 if ok else 0
            gates = dict((g, v.get("status")) for g, v in (res.get("gates") or {}).items())
            ch.eq(gates, dict((g, "PASS") for g in ec.QUICK_GATES), "%s gates G2-G8" % s["id"])
            req = next((f for f in res.get("frames", []) if f.get("kind") == "request"), {})
            ch.ok(req.get("features") == 4 and req.get("rows") == 1 and req.get("model") == "cdc-diabetes-lr4-v1"
                  and req.get("bytes") == 256 + 4 * 98304, "%s request frame: CDC model, 4 x 98,304 B" % s["id"])
            hs = s["hashes"]
            logged = responses.get(res.get("request_sha256")) or {}
            joined = (hs["capture_server"] == {"request": True, "response": True}
                      and (s["server_log"] or {}).get("match") == "hash"
                      and logged.get("response_payload_sha256") == res.get("response_payload_sha256"))
            ch.ok(joined, "%s capture = server.log for request_sha256 and response_payload_sha256" % s["id"])
            summary["joined"] += 1 if joined else 0
            ch.ok(isinstance((s["server_log"] or {}).get("cloud_ns"), int), "%s cloud_ns from server.log" % s["id"])
            ch.eq(s["connection_stop"], None, "%s no connection stop attached (idle deadlines stay idle)" % s["id"])
            g5 = (res.get("gates") or {}).get("G5", {}).get("facts", {})
            ch.ok(g5.get("bit_exact") is True and g5.get("words") == 6 * 4096, "%s G5 bit-exact affine" % s["id"])
            en = s["energy"] or {}
            a, b = (s["t_syn"] or s["t_first"]) - pr.PRE_S, s["t_end"] + pr.POST_S
            good = (en.get("approx") is True and isinstance(en.get("delta_j"), float) and en.get("covered")
                    and abs(en["window"][0] - round(a, 3)) < 1e-6 and abs(en["window"][1] - round(b, 3)) < 1e-6)
            if expected is not None:
                tol = 0.05 + 0.02 * expected[i]
                good = good and abs(en["delta_j"] - expected[i]) <= tol
                ch.ok(good, "%s Delta E ≈ %.3f J, expected %.3f J ± %.3f (window %s, resets %s)" % (
                    s["id"], en.get("delta_j") or float("nan"), expected[i], tol, en.get("window"), en.get("resets")))
            else:
                ch.ok(good, "%s Delta E over [SYN - 2 s, FIN + 0.5 s] (%s)" % (s["id"], en))
            summary["energy"] += 1 if good else 0
            say("CONSOLE_REPLAY_SESSION id=%s board=%s keyless_up_limbs=%d keyless_down_limbs=%d decoded_up_limbs=%d "
                "decoded_down_limbs=%d decode=%s gates=%s server_log=%s energy_delta_j=≈%s" % (
                    s["id"], s["board"], (keyless.get(s["id"]) or (0, 0))[0], (keyless.get(s["id"]) or (0, 0))[1],
                    ups, downs, dec.get("state"), ",".join("%s:%s" % (g, gates.get(g)) for g in ec.QUICK_GATES),
                    (s["server_log"] or {}).get("match"),
                    "%.3f" % en["delta_j"] if isinstance(en.get("delta_j"), float) else None))
        if expected is not None:
            ch.ok(any((x["energy"] or {}).get("resets") == 1 for x in sessions),
                  "the accumulator reset inside a window is bridged")
        srv = st["status"]["server"]
        idle = dict((round(x["epoch"], 3), x["idle"]) for x in srv["stops"])
        ch.eq([idle.get(round(t, 3)) for t in idle_times], [True, True],
              "idle TLS_DEADLINE lines (0.1 s before a SYN, 30 s after the last close) shown as idle, not as failures")
        ch.ok(srv["idle_deadlines"] >= 2, "idle accept deadlines counted (%s)" % srv["idle_deadlines"])
        ch.ok(srv.get("keylog_warning") and srv.get("ready") and srv.get("launcher", {}).get("keylog") == "on"
              and srv.get("rc") is not None, "launcher, key-log warning, READY and EXIT lines")
        ch.ok(st["probe"]["samples"] and st["status"]["probe"]["state"] == "file", "probe samples in the state")
        with open(MODEL, encoding="utf-8") as f:
            model = json.load(f)
        ch.ok(st["model"]["bias_int_text"] == str(model["bias_int"]) and st["model"]["weights_int"] ==
              model["weights_int"], "public W and the exact B text (B lies beyond the 2^53 of a JavaScript number)")
        text = ec.dumps(st)
        secrets = disclosed_secrets(dst)
        leaked = [k for k in OWNER_ONLY if '"%s"' % k in text]
        leaked += [v[:12] for v in secrets if v in text or json.dumps(v, ensure_ascii=False)[1:-1] in text]
        ch.ok(secrets or not os.path.isdir(os.path.join(dst, "disclosure")), "disclosures read for the owner-data check")
        ch.eq(leaked, [], "the console state carries no owner data")
        if os.path.isfile(os.path.join(dst, "board_public.jsonl")):
            ch.ok(st["board"]["state"] == "joined" and all(x["hashes"]["capture_board"]["request"] for x in forms),
                  "board public log joined by request_sha256")
        http_checks(ch, console)
    finally:
        console.shutdown()

    # the command line: --replay --once
    args = [sys.executable, "-B", CONSOLE_PY, "--replay", dst, "--port", str(port), "--once"]
    if probe_csv:
        args += ["--probe-csv", probe_csv, "--probe-shift", "%.6f" % shift]
    r = subprocess.run(args, capture_output=True, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    out = r.stdout.decode("utf-8", "replace")
    rows = [x for x in out.splitlines() if x.startswith("EVIDENCE_REPLAY_SESSION")]
    good = [x for x in rows if "up_limbs=24 down_limbs=6 decode=done" in x and "server_log=hash" in x]
    notls = [x for x in rows if "up_limbs=0 down_limbs=0 decode=notls" in x]
    ch.ok(r.returncode == 0 and len(rows) == summary["sessions"] and len(good) == summary["forms"]
          and len(notls) == summary["refused"],
          "evidence_console.py --replay --once (rc=%d, %d session lines)" % (r.returncode, len(rows)))
    return summary


def main(argv=None):
    efhe_verify.utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", help="recorded run directory (default: record one with the mock)")
    ap.add_argument("--port", type=int, help="server port of --run (default: from its server.log)")
    ap.add_argument("--probe-csv", help="recorded probe samples (default: synthetic)")
    ap.add_argument("--iface", default=r"\Device\NPF_Loopback" if WINDOWS else "lo")
    ap.add_argument("--work", help="work directory outside the repository (default: a new temporary directory)")
    ap.add_argument("--keep", action="store_true", help="keep the work directory")
    a = ap.parse_args(argv)
    work = os.path.abspath(a.work) if a.work else tempfile.mkdtemp(prefix="edgefhe_console_test_")
    if inside(work, REPO):
        say("CONSOLE_REPLAY_REFUSED 工作目錄位於 repository 之內；本測試只在 repository 以外寫入檔案。")
        return 2
    if a.work:
        if os.path.exists(work) and os.listdir(work):
            say("CONSOLE_REPLAY_REFUSED 工作目錄不是空的：%s" % work)
            return 2
        os.makedirs(work, exist_ok=True)
    ch = Checks()
    t0 = time.time()
    try:
        unit_feed(ch)
        unit_server_log(ch, work)
        unit_guards(ch)
        unit_acl(ch, work)
        unit_probe(ch, work)
        say("CONSOLE_REPLAY_UNITS checks=%d failed=%d" % (ch.n, len(ch.failed)))
        try:
            if not efhe_verify.find_tool("tshark"):
                raise FixtureUnavailable("tshark not found (Wireshark)")
            if a.run:
                run, port, how = os.path.abspath(a.run), a.port, "given"
            else:
                run, port = record_fixture(work, a.iface)
                how = "recorded (mock_efnet.py, forms %s, %.0f s apart)" % (",".join(FORMS), GAP_S)
            say("CONSOLE_REPLAY_RUN %s" % how)
            summary = replay_part(ch, work, run, port, a.probe_csv)
        except FixtureUnavailable as e:
            say("CONSOLE_REPLAY_FIXTURE_UNAVAILABLE %s" % e)
            return 2
    finally:
        if a.keep:
            say("CONSOLE_REPLAY_WORK_KEPT %s（內含一次性的私鑰與 TLS 金鑰紀錄，用畢請刪除）" % work)
        else:
            shutil.rmtree(work, ignore_errors=True)
    if ch.failed or not summary:
        say("CONSOLE_REPLAY_FAIL checks=%d failed=%d first=%s" % (ch.n, len(ch.failed),
                                                                 (ch.failed or ["no replay summary"])[0]))
        return 1
    say("CONSOLE_REPLAY_PASS sessions=%d forms=%d refused=%d up_limbs=24 down_limbs=6 per form session decoded=%d "
        "hash_join=%d energy=%d checks=%d seconds=%.1f" % (
            summary["sessions"], summary["forms"], summary["refused"], summary["decoded"], summary["joined"],
            summary["energy"], ch.n, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
