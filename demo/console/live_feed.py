"""EdgeFHE demo cloud console: the keyless tshark field feed, its parser, the session tracker and the record labels.

The live feed is the eavesdropper view of the network. It never decrypts:
  tshark -i IFACE -f 'host BOARD' -n -l -d tcp.port==18443,tls -o tls.keylog_file: -T fields -E separator=/t
         -E occurrence=a -E aggregator=, -e frame.number -e frame.time_epoch -e ip.src -e tcp.srcport -e tcp.dstport
         -e tcp.len -e tcp.flags.str -e tls.record.opaque_type -e tls.record.content_type -e tls.record.length
         -e tls.handshake.type -e tls.handshake.extensions_server_name -e tls.handshake.extensions.supported_version
         -e tls.handshake.ciphersuite
evidence_console.py runs it with an empty Wireshark configuration directory, so no personal key log or profile
applies; a replay reads a capture file with the same fields (-r).

Session tracker: a SYN from the board to the server port opens a session; a FIN or RST closes it, and so does the
passing of 2 s after a close_notify-sized record without a FIN. Packets that arrive within 5 s after the close (the
other FIN, the last ACKs, and a repeated SYN of a refused attempt that carried no TLS record) stay with that session.

Record labels without keys come from the record sizes and their order and start with '≈': 273 B is the EFHEW001 frame
header (256 B plus 17 B of TLS 1.3 overhead); 16,401 B is one RNS limb of 4096 uint32 residues. Upload limb k carries
ciphertext k//6, polynomial (k%6)//3 and limb k%3; download limb k carries polynomial k//3 and limb k%3 of the result
(tls.hpp:120 writes at most 16,384 B per record, span_wire.hpp:13-16). 69 B (53 B with a SHA-256 suite) is a
Finished message, 19 B is close_notify, and small server records after the client's Finished are NewSessionTickets.
After the per-session decode, records_from_keyed() relabels every record from the decrypted content types, without
'≈'. The wording follows the Wireshark postdissector in demo/evidence/efhe.lua.

Python 3.10 or later, standard library only.
"""
import re

PORT = 18443
FIELDS = ("frame.number", "frame.time_epoch", "ip.src", "tcp.srcport", "tcp.dstport", "tcp.len", "tcp.flags.str",
          "tls.record.opaque_type", "tls.record.content_type", "tls.record.length", "tls.handshake.type",
          "tls.handshake.extensions_server_name", "tls.handshake.extensions.supported_version",
          "tls.handshake.ciphersuite")
KEYED_FIELDS = ("frame.number", "frame.time_epoch", "tcp.srcport", "tcp.dstport", "tls.record.content_type",
                "tls.record.opaque_type", "tls.record.length", "tls.handshake.type", "tls.alert_message.desc")
N = 4096
HDR = 256
TLS_OVERHEAD = 17                     # inner content type + 16-byte AEAD tag
REC_HEADER = HDR + TLS_OVERHEAD       # 273
REC_LIMB = 4 * N + TLS_OVERHEAD       # 16,401
REC_CLOSE = 2 + TLS_OVERHEAD          # 19
FINISHED_SIZES = (69, 53)             # 4 + 48 + 17 (SHA-384 suites), 4 + 32 + 17 (SHA-256 suites)
TICKET_MAX = 2048
Q = (998244353, 1004535809, 469762049)
FORM_UP_LIMBS, FORM_DOWN_LIMBS = 24, 6
CLOSE_GRACE_S = 2.0
LATE_PACKET_S = 5.0
INACTIVE_CLOSE_S = 75.0               # a session without any packet for this long is closed (capture missed the FIN)
APPROX = "≈ "
HS_NAMES = {1: "ClientHello", 2: "ServerHello", 4: "NewSessionTicket", 5: "EndOfEarlyData", 8: "EncryptedExtensions",
            11: "Certificate", 13: "CertificateRequest", 15: "CertificateVerify", 20: "Finished", 24: "KeyUpdate"}
HS_ZH = {1: "用戶端問候", 2: "伺服器問候", 4: "工作階段票證", 5: "早期資料結束", 8: "加密延伸欄位", 11: "憑證",
         13: "憑證請求", 15: "憑證驗證", 20: "交握完成", 24: "金鑰更新"}
SUITES = {0x1301: "TLS_AES_128_GCM_SHA256", 0x1302: "TLS_AES_256_GCM_SHA384", 0x1303: "TLS_CHACHA20_POLY1305_SHA256"}
DIRECTIONS = ("up", "down")           # up: board -> cloud (client to server), down: cloud -> board


# ------------------------------------------------------------------ tshark arguments
def tshark_live_args(tshark, iface, capture_filter, port=PORT):
    """The live, keyless field feed (one line per captured packet, flushed at once)."""
    args = [tshark, "-i", iface]
    if capture_filter:
        args += ["-f", capture_filter]
    args += ["-n", "-l", "-d", "tcp.port==%d,tls" % port, "-o", "tls.keylog_file:", "-T", "fields",
             "-E", "separator=/t", "-E", "occurrence=a", "-E", "aggregator=,"]
    for f in FIELDS:
        args += ["-e", f]
    return args


def tshark_file_args(tshark, capture, port=PORT, display=None):
    """The same keyless fields from a capture file (replay)."""
    args = [tshark, "-r", capture, "-n", "-d", "tcp.port==%d,tls" % port, "-o", "tls.keylog_file:", "-T", "fields",
            "-E", "separator=/t", "-E", "occurrence=a", "-E", "aggregator=,"]
    if display:
        args += ["-Y", display]
    for f in FIELDS:
        args += ["-e", f]
    return args


# ------------------------------------------------------------------ parsing
def _ints(text):
    out = []
    for x in (text or "").split(","):
        x = x.strip()
        if x:
            try:
                out.append(int(x, 0))
            except ValueError:
                pass
    return out


def _first(text):
    return (text or "").split(",")[0].strip()


def _first_int(text):
    v = _ints(_first(text))
    return v[0] if v else None


def parse_flags(text):
    """tcp.flags.str, for example '··········S·' (12 positions ending in A P R S F; '·' when clear)."""
    s = (text or "").strip()
    flags = dict(ack=False, psh=False, rst=False, syn=False, fin=False)
    if len(s) >= 5:
        tail = s[-5:]
        for name, ch in zip(("ack", "psh", "rst", "syn", "fin"), tail):
            flags[name] = ch.isalpha()
    return flags


def parse_records(content_types, opaque_types, lengths):
    """[(type, length, encrypted)] of one frame: the cleartext records (content_type) come first, then the encrypted
    ones (opaque_type 23 without keys)."""
    ctypes, otypes, lens = _ints(content_types), _ints(opaque_types), _ints(lengths)
    out, nplain = [], len(ctypes)
    for i, ln in enumerate(lens):
        if i < nplain:
            out.append((ctypes[i], ln, False))
        else:
            j = i - nplain
            out.append((otypes[j] if j < len(otypes) else 23, ln, True))
    return out


def parse_line(line):
    """One tab-separated line of the field feed -> packet dict, or None for a line that is not a packet."""
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) < 2:
        return None
    if len(parts) < len(FIELDS):
        parts += [""] * (len(FIELDS) - len(parts))
    try:
        frame = int(parts[0])
        t = float(parts[1])
    except ValueError:
        return None
    return dict(frame=frame, t=t, src=_first(parts[2]) or None, sport=_first_int(parts[3]),
                dport=_first_int(parts[4]), tcp_len=_first_int(parts[5]) or 0, flags=parse_flags(parts[6]),
                records=parse_records(parts[8], parts[7], parts[9]), hs=_ints(parts[10]),
                sni=_first(parts[11]) or None, versions=_ints(parts[12]), suites=_ints(parts[13]))


def parse_keyed_line(line):
    """A line of KEYED_FIELDS (decode pass with the key log) -> dict, or None."""
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) < len(KEYED_FIELDS):
        parts += [""] * (len(KEYED_FIELDS) - len(parts))
    try:
        frame, t = int(parts[0]), float(parts[1])
    except ValueError:
        return None
    return dict(frame=frame, t=t, sport=_first_int(parts[2]), dport=_first_int(parts[3]), ctypes=_ints(parts[4]),
                otypes=_ints(parts[5]), lens=_ints(parts[6]), hs=_ints(parts[7]), alerts=_ints(parts[8]))


def keyed_rows_from_dicts(rows):
    """Rows as efhe_verify.Tshark.fields() returns them (dict per field name) -> parse_keyed_line() dicts."""
    out = []
    for r in rows:
        line = "\t".join(r.get(f, "") for f in KEYED_FIELDS)
        p = parse_keyed_line(line)
        if p is not None:
            out.append(p)
    return out


# ------------------------------------------------------------------ labels
def limb_label(direction, k, approx=True):
    """(text, short) of limb record k (0-based) of a frame in that direction."""
    mark = APPROX if approx else ""
    limb = k % 3
    if direction == "up":
        ct, poly = k // 6, (k % 6) // 3
        return ("%s密文 #%d 的 c%d 第 %d 個 RNS 分量（q=%d）" % (mark, ct, poly, limb, Q[limb]),
                "ct%d c%d L%d" % (ct, poly, limb))
    poly = k // 3
    return ("%s結果密文的 c%d 第 %d 個 RNS 分量（q=%d）" % (mark, poly, limb, Q[limb]), "resp c%d L%d" % (poly, limb))


def header_label(direction, approx=True):
    mark = APPROX if approx else ""
    if direction == "up":
        return mark + "EFHE 標頭（請求，公開中繼資料）", "req hdr"
    return mark + "EFHE 標頭（回應，公開中繼資料）", "resp hdr"


def handshake_label(types, approx=False):
    mark = APPROX if approx else ""
    names = [HS_NAMES.get(t, "type %d" % t) for t in types]
    zh = [HS_ZH.get(t, "交握訊息") for t in types]
    return mark + "%s（%s）" % ("、".join(names), "、".join(zh)), "/".join(names)


def suite_name(code):
    if code is None:
        return None
    return SUITES.get(code, "0x%04x" % code)


class _DirState:
    """Labelling state of one direction of one session."""

    def __init__(self):
        self.group = None             # limb counter inside the current EFHEW001 frame, or None
        self.headers = 0
        self.limbs = 0                # limb records in all frames
        self.groups = []              # limb records per frame
        self.finished = False         # this side's Finished-sized record seen (encrypted handshake done)
        self.records = 0


def label_nokey(st_dir, other_dir, direction, ctype, length, encrypted):
    """Label one record from its size and order (no keys). Returns (group, text, short, k, approx)."""
    if not encrypted:
        if ctype == 22:
            name = "ClientHello" if direction == "up" else "ServerHello"
            return "hello", handshake_label([1 if direction == "up" else 2])[0], name, None, False
        if ctype == 20:
            return "ccs", "ChangeCipherSpec（相容性紀錄，無內容）", "CCS", None, False
        if ctype == 21:
            return "alert", "明文警示紀錄", "alert", None, False
        return "other", "明文紀錄（類型 %d，%d 位元組）" % (ctype, length), "type %d" % ctype, None, False
    if length == REC_HEADER:
        st_dir.group = 0
        st_dir.headers += 1
        st_dir.groups.append(0)
        text, short = header_label(direction)
        return "hdr", text, short, None, True
    if length == REC_LIMB and st_dir.group is not None:
        k = st_dir.group
        st_dir.group += 1
        st_dir.limbs += 1
        st_dir.groups[-1] += 1
        text, short = limb_label(direction, k)
        return "limb", text, short, k, True
    if length == REC_CLOSE:
        st_dir.group = None
        return "close", APPROX + "close_notify（關閉通知）", "close_notify", None, True
    if st_dir.headers == 0 and not st_dir.finished:
        if length in FINISHED_SIZES:
            st_dir.finished = True
            who = "用戶端" if direction == "up" else "伺服器"
            return "fin", APPROX + "%s Finished（交握完成）" % who, "Finished", None, True
        if direction == "up":
            return "hs", APPROX + "用戶端交握訊息（憑證或憑證驗證，%d 位元組）" % length, "hs %d" % length, None, True
        return "hs", APPROX + "伺服器交握訊息（%d 位元組）" % length, "hs %d" % length, None, True
    if direction == "down" and st_dir.headers == 0 and length < TICKET_MAX and other_dir.finished:
        return "nst", APPROX + "NewSessionTicket（工作階段票證，%d 位元組）" % length, "ticket", None, True
    return "other", APPROX + "其他紀錄（%d 位元組）" % length, "rec %d" % length, None, True


# ------------------------------------------------------------------ sessions
class Session:
    def __init__(self, sid, n, board_ip, board_port, server, t, partial):
        self.id = sid
        self.n = n
        self.board_ip = board_ip
        self.board_port = board_port
        self.server = server
        self.t_syn = None if partial else t
        self.t_first = t
        self.t_last = t
        self.t_close = None
        self.close_reason = None
        self.close_notify_t = None
        self.partial = partial
        self.state = "open"
        self.records = []
        self.records_source = "live"
        self.packets = 0
        self.tcp_bytes = {"up": 0, "down": 0}
        self.fin = {"up": None, "down": None}
        self.rst = None
        self.sni = None
        self.offered_versions = []
        self.version = None
        self.suite = None
        self.dirs = {"up": _DirState(), "down": _DirState()}

    def counts(self):
        out = {}
        for d in DIRECTIONS:
            recs = [r for r in self.records if r["dir"] == d]
            out[d] = dict(records=len(recs), headers=sum(1 for r in recs if r["group"] == "hdr"),
                          limbs=sum(1 for r in recs if r["group"] == "limb"),
                          record_bytes=sum(r["len"] for r in recs), tcp_bytes=self.tcp_bytes[d])
        return out

    def signature(self):
        """Keyless classification from the record sizes: form (273+24x16,401 up and 273+6x16,401 down), handshake
        only, or other."""
        groups = {}
        for d in DIRECTIONS:
            gs, cur = [], None
            for r in self.records:
                if r["dir"] != d or not r["enc"]:
                    continue
                if r["len"] == REC_HEADER:
                    cur = 0
                    gs.append(0)
                elif r["len"] == REC_LIMB and cur is not None:
                    gs[-1] += 1
                else:
                    cur = None
            groups[d] = gs
        if not groups["up"] and not groups["down"]:
            cls = "handshake_only"
        elif groups["up"] == [FORM_UP_LIMBS] and groups["down"] == [FORM_DOWN_LIMBS]:
            cls = "form"
        else:
            cls = "other"
        return dict(classification=cls, up_groups=groups["up"], down_groups=groups["down"],
                    up_text=["273+%dx16401" % m for m in groups["up"]],
                    down_text=["273+%dx16401" % m for m in groups["down"]])

    def end_time(self):
        """End of the connection on the wire: the first FIN, else the RST, else the close_notify, else the last
        packet (the end of the energy window [SYN - 2 s, FIN + 0.5 s])."""
        fins = [t for t in self.fin.values() if t is not None]
        if fins:
            return min(fins)
        if self.rst is not None:
            return self.rst
        if self.close_notify_t is not None:
            return self.close_notify_t
        return self.t_last

    def phases(self):
        """Wire times of the phases (capture clock, epoch seconds), from the records."""
        def first(pred):
            for r in self.records:
                if pred(r):
                    return r["t"]
            return None

        def last(pred):
            t = None
            for r in self.records:
                if pred(r):
                    t = r["t"]
            return t
        up_fin = first(lambda r: r["dir"] == "up" and r["group"] == "fin")
        if up_fin is None:
            up_fin = last(lambda r: r["dir"] == "up" and r["group"] in ("fin", "hs"))
        return dict(syn=self.t_syn, client_hello=first(lambda r: r["dir"] == "up" and r["group"] == "hello"),
                    server_hello=first(lambda r: r["dir"] == "down" and r["group"] == "hello"),
                    client_finished=up_fin,
                    request_first=first(lambda r: r["dir"] == "up" and r["group"] == "hdr"),
                    request_last=last(lambda r: r["dir"] == "up" and r["group"] in ("hdr", "limb")),
                    response_first=first(lambda r: r["dir"] == "down" and r["group"] == "hdr"),
                    response_last=last(lambda r: r["dir"] == "down" and r["group"] in ("hdr", "limb")),
                    close_notify=first(lambda r: r["group"] == "close"),
                    fin=min([t for t in self.fin.values() if t is not None], default=None),
                    close=self.t_close)

    def to_dict(self, records=True):
        d = dict(id=self.id, n=self.n, board="%s:%s" % (self.board_ip or "?", self.board_port),
                 board_port=self.board_port, server=self.server, t_syn=self.t_syn, t_first=self.t_first,
                 t_last=self.t_last, t_close=self.t_close, t_end=None if self.state == "open" else self.end_time(),
                 close_reason=self.close_reason, partial=self.partial,
                 state=self.state, packets=self.packets, records_source=self.records_source,
                 tls=dict(sni=self.sni, version=None if self.version is None else "0x%04x" % self.version,
                          offered_versions=["0x%04x" % v for v in self.offered_versions],
                          suite=None if self.suite is None else "0x%04x" % self.suite,
                          suite_name=suite_name(self.suite)),
                 counts=self.counts(), signature=self.signature(), phases=self.phases())
        if records:
            d["records"] = list(self.records)
        return d


class SessionTracker:
    """Sessions of the board's TCP connections to the server port, from parse_line() packets in capture order."""

    def __init__(self, port=PORT, board_ip=None, on_event=None, close_grace=CLOSE_GRACE_S):
        self.port = port
        self.board_ip = board_ip
        self.on_event = on_event
        self.close_grace = close_grace
        self.sessions = []
        self.by_port = {}
        self.other_packets = 0
        self.packets = 0
        self.foreign = 0              # server-port packets whose board side is not the configured board address
        self.last_t = None

    def _emit(self, kind, sess, **extra):
        if self.on_event is not None:
            self.on_event(kind, sess, extra)

    def _new(self, board_ip, board_port, server, t, partial):
        n = len(self.sessions) + 1
        s = Session("s%02d" % n, n, board_ip, board_port, server, t, partial)
        self.sessions.append(s)
        self.by_port[board_port] = s
        self._emit("open", s)
        return s

    def close(self, sess, t, reason):
        if sess.state != "open":
            return
        sess.state = "closed"
        sess.t_close = t
        sess.close_reason = reason
        self._emit("close", sess)

    def feed(self, pkt):
        """Add one packet; returns the session it belongs to, or None (another flow)."""
        self.packets += 1
        self.last_t = pkt["t"]
        sp, dp = pkt["sport"], pkt["dport"]
        if sp is None or dp is None or self.port not in (sp, dp) or sp == dp:
            self.other_packets += 1
            return None
        direction = "up" if dp == self.port else "down"
        board_port = sp if direction == "up" else dp
        if direction == "up" and self.board_ip and pkt["src"] and pkt["src"] != self.board_ip:
            self.foreign += 1
        fl = pkt["flags"]
        sess = self.by_port.get(board_port)
        syn_open = direction == "up" and fl["syn"] and not fl["ack"]
        if syn_open and sess is not None and sess.state == "open" and not sess.records and sess.packets <= 2 \
                and not sess.partial:
            syn_open = False              # a retransmitted SYN of the same connection
        elif syn_open and sess is not None and sess.state == "closed" and sess.close_reason == "RST" \
                and not sess.records and pkt["t"] - sess.t_close <= LATE_PACKET_S:
            syn_open = False              # the same refused attempt again (Windows repeats a SYN answered by RST)
        elif syn_open:
            if sess is not None and sess.state == "open":
                self.close(sess, pkt["t"], "port_reused")
            sess = None
        elif sess is not None and sess.state == "closed" and pkt["t"] - (sess.t_close or pkt["t"]) > LATE_PACKET_S:
            sess = None
        if sess is None:
            server = None
            src = pkt["src"] or "?"
            if direction == "up":
                board_ip = src
            else:
                board_ip, server = None, "%s:%d" % (src, sp)
            sess = self._new(board_ip, board_port, server, pkt["t"], partial=not syn_open)
        if sess.board_ip is None and direction == "up":
            sess.board_ip = pkt["src"]
        if sess.server is None and direction == "down" and pkt["src"]:
            sess.server = "%s:%d" % (pkt["src"], sp)
        sess.packets += 1
        sess.t_last = pkt["t"]
        sess.tcp_bytes[direction] += pkt["tcp_len"] or 0
        if pkt["sni"] and direction == "up":
            sess.sni = pkt["sni"]
        if direction == "up" and 1 in pkt["hs"] and pkt["versions"]:
            sess.offered_versions = pkt["versions"]
        if direction == "down" and 2 in pkt["hs"]:
            if pkt["versions"]:
                sess.version = pkt["versions"][0]
            if pkt["suites"]:
                sess.suite = pkt["suites"][0]
        new = []
        for i, (ctype, ln, enc) in enumerate(pkt["records"]):
            other = "down" if direction == "up" else "up"
            group, text, short, k, approx = label_nokey(sess.dirs[direction], sess.dirs[other], direction, ctype, ln,
                                                        enc)
            sess.dirs[direction].records += 1
            rec = dict(i=len(sess.records), dir=direction, t=pkt["t"], frame=pkt["frame"], len=ln, enc=enc,
                       type=ctype, group=group, k=k, label=text, short=short, approx=approx)
            sess.records.append(rec)
            new.append(rec)
            if group == "close" and sess.close_notify_t is None:
                sess.close_notify_t = pkt["t"]
        self._emit("packet", sess, packet=pkt, records=new)
        if fl["rst"]:
            sess.rst = pkt["t"]
            self.close(sess, pkt["t"], "RST")
        elif fl["fin"]:
            if sess.fin[direction] is None:
                sess.fin[direction] = pkt["t"]
            self.close(sess, pkt["t"], "FIN")
        return sess

    def tick(self, now):
        """Close sessions 2 s after a close_notify without FIN, and sessions silent for INACTIVE_CLOSE_S."""
        for s in self.sessions:
            if s.state != "open":
                continue
            if s.close_notify_t is not None and now - s.close_notify_t >= self.close_grace:
                self.close(s, s.close_notify_t + self.close_grace, "close_notify")
            elif now - s.t_last >= INACTIVE_CLOSE_S:
                self.close(s, s.t_last, "inactive")

    def finish(self, t_end=None):
        """End of a capture file (replay): close what is still open."""
        t = t_end if t_end is not None else self.last_t
        for s in self.sessions:
            if s.state == "open":
                self.close(s, s.close_notify_t if s.close_notify_t is not None else (t or s.t_last),
                           "close_notify" if s.close_notify_t is not None else "capture_end")

    def open_sessions(self):
        return [s for s in self.sessions if s.state == "open"]


# ------------------------------------------------------------------ exact labels after the decode
def records_from_keyed(rows, port, board_port=None):
    """Records of one session from the decode pass with the key log (parse_keyed_line dicts in capture order),
    labelled from the decrypted content types. A record that was not decrypted keeps a '≈' size label."""
    st = {"up": _DirState(), "down": _DirState()}
    out = []
    for r in rows:
        sp, dp = r["sport"], r["dport"]
        if sp is None or dp is None or port not in (sp, dp):
            continue
        direction = "up" if dp == port else "down"
        if board_port is not None and (sp if direction == "up" else dp) != board_port:
            continue
        other = "down" if direction == "up" else "up"
        lens, ctypes, otypes = r["lens"], r["ctypes"], r["otypes"]
        n_enc = min(len(otypes), len(lens))
        hs = list(r["hs"])
        alerts = list(r["alerts"])
        hs_records = [i for i, _ in enumerate(lens) if i < len(ctypes) and ctypes[i] == 22]
        per_record = {}
        if len(hs) == len(hs_records):
            for i, t in zip(hs_records, hs):
                per_record[i] = [t]
        elif hs_records:
            for i in hs_records:
                per_record[i] = hs
        ai = 0
        for i, ln in enumerate(lens):
            enc = i >= len(lens) - n_enc
            inner = ctypes[i] if i < len(ctypes) else None       # None: this record was not decrypted
            if not enc and inner is not None:
                group, text, short, k, approx = label_nokey(st[direction], st[other], direction, inner, ln, False)
                if inner == 22 and per_record.get(i):
                    text, short = handshake_label(per_record[i])
            elif inner is None:
                group, text, short, k, approx = label_nokey(st[direction], st[other], direction, 23, ln, True)
            elif inner == 23:
                if ln == REC_HEADER:
                    st[direction].group = 0
                    st[direction].headers += 1
                    st[direction].groups.append(0)
                    text, short = header_label(direction, approx=False)
                    group, k = "hdr", None
                elif ln == REC_LIMB and st[direction].group is not None:
                    k = st[direction].group
                    st[direction].group += 1
                    st[direction].limbs += 1
                    st[direction].groups[-1] += 1
                    text, short = limb_label(direction, k, approx=False)
                    group = "limb"
                else:
                    group, k = "app", None
                    text, short = "應用資料（%d 位元組）" % (ln - TLS_OVERHEAD), "app %d" % ln
                approx = False
            elif inner == 22:
                types = per_record.get(i) or []
                text, short = handshake_label(types) if types else ("交握訊息（%d 位元組）" % ln, "hs")
                group = "fin" if types == [20] else ("nst" if types and set(types) == {4} else "hs")
                if types == [20]:
                    st[direction].finished = True
                k, approx = None, False
            elif inner == 21:
                desc = alerts[ai] if ai < len(alerts) else None
                ai += 1
                st[direction].group = None
                if desc == 0:
                    group, text, short = "close", "close_notify（關閉通知）", "close_notify"
                else:
                    group, text, short = "alert", "警示紀錄（代碼 %s）" % desc, "alert %s" % desc
                k, approx = None, False
            elif inner == 20:
                group, text, short, k, approx = "ccs", "ChangeCipherSpec（相容性紀錄，無內容）", "CCS", None, False
            else:
                group, text, short, k, approx = "other", "其他紀錄（類型 %d，%d 位元組）" % (inner, ln), \
                    "type %d" % inner, None, False
            out.append(dict(i=len(out), dir=direction, t=r["t"], frame=r["frame"], len=ln, enc=enc,
                            type=inner if inner is not None else 23, group=group, k=k, label=text, short=short,
                            approx=approx))
    return out


def limb_counts(records):
    return dict(up=sum(1 for r in records if r["dir"] == "up" and r["group"] == "limb"),
                down=sum(1 for r in records if r["dir"] == "down" and r["group"] == "limb"))


_HEX64 = re.compile(r"[0-9a-f]{64}")


def is_hex64(v):
    return isinstance(v, str) and _HEX64.fullmatch(v) is not None
