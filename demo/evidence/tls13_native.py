"""Independent capture reader and TLS 1.3 decryptor for the EdgeFHE demo evidence chain.

This module is the cross-check for the tshark path in efhe_verify.py. It reads a capture without Wireshark
(pcapng, or the classic pcap that tcpdump writes), reassembles the TCP connections to one server port, and, given
an NSS key log, decrypts the TLS 1.3 records.

- The capture and TCP functions use the Python standard library only. efhe_verify.py uses them for gate G0
  (drop counters in the pcapng interface statistics, TCP gaps) and gate G11 (observer stream hashes).
- Decryption needs the 'cryptography' package (AES-GCM, ChaCha20-Poly1305). It is imported only when
  decode_connection() runs, so the rest of this module works without it.

usage: python -B tls13_native.py CAPTURE KEYLOG [PORT]
"""
import hashlib
import hmac
import struct
import sys
from collections import defaultdict

DEFAULT_PORT = 18443
TLS_SUITES = {0x1301: ("sha256", 16, "aesgcm"), 0x1302: ("sha384", 32, "aesgcm"), 0x1303: ("sha256", 32, "chacha")}
KEYLOG_LABELS = ("CLIENT_HANDSHAKE_TRAFFIC_SECRET", "SERVER_HANDSHAKE_TRAFFIC_SECRET", "CLIENT_TRAFFIC_SECRET_0",
                 "SERVER_TRAFFIC_SECRET_0", "EXPORTER_SECRET")
HRR_RANDOM = hashlib.sha256(b"HelloRetryRequest").digest()
AF_INET6_VALUES = (10, 23, 24, 28, 30)     # Linux, Windows, BSD variants (DLT_NULL address family)


class CaptureError(Exception):
    """The capture file cannot be read."""


class TLSDecodeError(Exception):
    """A TLS record could not be decrypted or parsed (code, direction, record index)."""

    def __init__(self, code, direction=None, index=None, detail=""):
        Exception.__init__(self, "%s %s %s %s" % (code, direction, index, detail))
        self.code, self.direction, self.index, self.detail = code, direction, index, detail


# ------------------------------------------------------------------ capture files
def read_capture(path):
    """Read a pcapng or pcap file. Returns a dict:
    format, interfaces [{linktype, tsresol}], packets [(ts, if_id, data, orig_len)], isb [...], dsb (count),
    problems [str]. A growing or truncated file is read up to its last complete block (problem CAPTURE_TRUNCATED)."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 24:
        raise CaptureError("CAPTURE_TOO_SHORT")
    if data[:4] == b"\x0a\x0d\x0d\x0a":
        return _read_pcapng(data)
    if data[:4] in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
        return _read_pcap(data)
    raise CaptureError("CAPTURE_FORMAT_UNKNOWN")


def _options(body, off, endian):
    opts = []
    while off + 4 <= len(body):
        code, ln = struct.unpack_from(endian + "HH", body, off)
        if code == 0:
            break
        opts.append((code, body[off + 4:off + 4 + ln]))
        off += 4 + ((ln + 3) & ~3)
    return opts


def _ts_unit(tsresol):
    return 10.0 ** -tsresol if not tsresol & 0x80 else 2.0 ** -(tsresol & 0x7F)


def _read_pcapng(data):
    out = dict(format="pcapng", interfaces=[], packets=[], isb=[], dsb=0, problems=[])
    off, endian, base = 0, "<", 0
    while off < len(data):
        if off + 12 > len(data):
            out["problems"].append("CAPTURE_TRUNCATED")
            break
        if data[off:off + 4] == b"\x0a\x0d\x0d\x0a":
            endian = "<" if data[off + 8:off + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            base = len(out["interfaces"])
        btype, blen = struct.unpack_from(endian + "II", data, off)
        if blen < 12 or blen % 4 or off + blen > len(data):
            out["problems"].append("CAPTURE_TRUNCATED")
            break
        if struct.unpack_from(endian + "I", data, off + blen - 4)[0] != blen:
            out["problems"].append("CAPTURE_BLOCK_LENGTH_MISMATCH")
            break
        body = data[off + 8:off + blen - 4]
        if btype == 1:                                    # Interface Description Block
            link = struct.unpack_from(endian + "H", body, 0)[0]
            tsresol = 6
            for code, val in _options(body, 8, endian):
                if code == 9 and val:
                    tsresol = val[0]
            out["interfaces"].append(dict(linktype=link, tsresol=tsresol))
        elif btype == 6:                                  # Enhanced Packet Block
            iid, th, tl, cap, orig = struct.unpack_from(endian + "IIIII", body, 0)
            iid += base
            if iid >= len(out["interfaces"]):
                out["problems"].append("CAPTURE_PACKET_WITHOUT_INTERFACE")
            else:
                unit = _ts_unit(out["interfaces"][iid]["tsresol"])
                out["packets"].append((((th << 32) | tl) * unit, iid, body[20:20 + cap], orig))
        elif btype == 3:                                  # Simple Packet Block (no timestamp)
            orig = struct.unpack_from(endian + "I", body, 0)[0]
            out["packets"].append((None, base, body[4:4 + orig], orig))
        elif btype == 5:                                  # Interface Statistics Block
            iid = struct.unpack_from(endian + "I", body, 0)[0] + base
            isb = dict(if_id=iid)
            names = {4: "ifrecv", 5: "ifdrop", 6: "filteraccept", 7: "osdrop", 8: "usrdeliv"}
            for code, val in _options(body, 12, endian):
                if code in names and len(val) == 8:
                    isb[names[code]] = struct.unpack(endian + "Q", val)[0]
            out["isb"].append(isb)
        elif btype == 10:                                 # Decryption Secrets Block
            out["dsb"] += 1
        off += blen
    return out


def _read_pcap(data):
    magic = data[:4]
    endian = "<" if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
    nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    link = struct.unpack_from(endian + "I", data, 20)[0] & 0x0FFFFFFF
    out = dict(format="pcap", interfaces=[dict(linktype=link, tsresol=9 if nano else 6)], packets=[], isb=[], dsb=0,
               problems=[])
    off, div = 24, (1e9 if nano else 1e6)
    while off < len(data):
        if off + 16 > len(data):
            out["problems"].append("CAPTURE_TRUNCATED")
            break
        sec, frac, cap, orig = struct.unpack_from(endian + "IIII", data, off)
        if off + 16 + cap > len(data):
            out["problems"].append("CAPTURE_TRUNCATED")
            break
        out["packets"].append((sec + frac / div, 0, data[off + 16:off + 16 + cap], orig))
        off += 16 + cap
    return out


# ------------------------------------------------------------------ IP and TCP
def ip_payload(link, frame):
    """(ip_version, ip_packet) for the supported link types, or (None, b"")."""
    if link == 0:                                         # DLT_NULL: 4-byte address family, writer byte order
        if len(frame) < 4:
            return None, b""
        af = struct.unpack_from("<I", frame, 0)[0]
        if af not in (2,) + AF_INET6_VALUES:
            af = struct.unpack_from(">I", frame, 0)[0]
        return (4 if af == 2 else 6 if af in AF_INET6_VALUES else None), frame[4:]
    if link == 1:                                         # Ethernet, optional 802.1Q / 802.1ad tags
        if len(frame) < 14:
            return None, b""
        et, o = struct.unpack_from(">H", frame, 12)[0], 14
        while et in (0x8100, 0x88A8) and len(frame) >= o + 4:
            et, o = struct.unpack_from(">H", frame, o + 2)[0], o + 4
        return {0x0800: 4, 0x86DD: 6}.get(et), frame[o:]
    if link == 113:                                       # Linux cooked capture v1
        if len(frame) < 16:
            return None, b""
        return {0x0800: 4, 0x86DD: 6}.get(struct.unpack_from(">H", frame, 14)[0]), frame[16:]
    if link == 276:                                       # Linux cooked capture v2
        if len(frame) < 20:
            return None, b""
        return {0x0800: 4, 0x86DD: 6}.get(struct.unpack_from(">H", frame, 0)[0]), frame[20:]
    if link in (12, 14, 101, 228, 229) and frame:         # raw IP
        return frame[0] >> 4, frame
    return None, b""


def ip_text(raw):
    if len(raw) == 4:
        return ".".join(str(b) for b in raw)
    words = ["%x" % w for w in struct.unpack(">8H", raw)]
    return ":".join(words)


def tcp_segments(cap):
    """Yield (frame_number, ts, src, sport, dst, dport, seq, flags, payload, truncated) for every TCP segment."""
    for n, (ts, iid, frame, orig) in enumerate(cap["packets"], 1):
        ver, ip = ip_payload(cap["interfaces"][iid]["linktype"], frame)
        truncated = len(frame) < orig
        if ver == 4 and len(ip) >= 20 and ip[9] == 6:
            ihl = (ip[0] & 15) * 4
            total = struct.unpack_from(">H", ip, 2)[0]
            if struct.unpack_from(">H", ip, 6)[0] & 0x1FFF:      # non-first IPv4 fragment
                continue
            end = total if ihl <= total <= len(ip) else len(ip)  # total 0 (TSO) or truncated: use what was captured
            if total > len(ip):
                truncated = True
            src, dst, seg = ip[12:16], ip[16:20], ip[ihl:end]
        elif ver == 6 and len(ip) >= 40:
            nxt, o = ip[6], 40
            while nxt in (0, 43, 60) and len(ip) >= o + 8:
                nxt, o = ip[o], o + (ip[o + 1] + 1) * 8
            if nxt != 6:
                continue
            plen = struct.unpack_from(">H", ip, 4)[0]
            end = 40 + plen if 40 + plen <= len(ip) else len(ip)
            if 40 + plen > len(ip):
                truncated = True
            src, dst, seg = ip[8:24], ip[24:40], ip[o:end]
        else:
            continue
        if len(seg) < 20:
            continue
        sport, dport, seq, _ack, offflags = struct.unpack_from(">HHIIH", seg, 0)
        hl = (offflags >> 12) * 4
        yield n, ts, ip_text(src), sport, ip_text(dst), dport, seq, offflags & 0x1FF, seg[hl:], truncated


def _stitch(direction):
    """Reassemble one direction. Returns (bytes up to the first gap, gaps, conflicts, [(rel_start, rel_end, ts)])."""
    segs = direction["segs"]
    if not segs:
        return b"", [], [], []
    base = direction["isn"] if direction["isn"] is not None else min(s[0] for s in segs)
    out, gaps, conflicts, spans = bytearray(), [], [], []
    ordered = sorted(((s - base) & 0xFFFFFFFF, i, ts, pl) for i, (s, ts, pl) in enumerate(segs))
    for rel, _i, ts, pl in ordered:
        if rel > 0x7FFFFFFF:          # before the initial sequence number (keep-alive probes, SYN retransmits)
            continue
        end = rel + len(pl)
        spans.append((rel, end, ts))
        if rel > len(out):
            if not gaps or gaps[-1][1] != rel:
                gaps.append((len(out), rel))
            continue
        if gaps:
            continue
        overlap = out[rel:min(end, len(out))]
        if overlap and overlap != pl[:len(overlap)]:
            conflicts.append(rel)
        if end > len(out):
            out += pl[len(out) - rel:]
    return bytes(out), gaps, conflicts, spans


def tcp_connections(cap, port=DEFAULT_PORT):
    """TCP connections whose server port is `port`, ordered by their first packet.
    Each: client, server, client_port, first_ts, last_ts, frames, syn, fin_c2s, fin_s2c, rst, truncated_segments,
    c2s/s2c (bytes), c2s_gaps/s2c_gaps, c2s_conflicts/s2c_conflicts, c2s_spans/s2c_spans."""
    flows = defaultdict(lambda: dict(isn=None, segs=[], fin=False, rst=False, syn=False, first=None, last=None,
                                     frames=[], truncated=0))
    for n, ts, src, sport, dst, dport, seq, flags, pl, trunc in tcp_segments(cap):
        if port not in (sport, dport):
            continue
        f = flows[(src, sport, dst, dport)]
        f["frames"].append(n)
        if f["first"] is None:
            f["first"] = ts
        f["last"] = ts
        if flags & 0x02:
            f["isn"], f["syn"] = (seq + 1) & 0xFFFFFFFF, True
        if flags & 0x01:
            f["fin"] = True
        if flags & 0x04:
            f["rst"] = True
        if trunc:
            f["truncated"] += 1
        if pl:
            f["segs"].append((seq, ts, pl))
    conns = []
    for key, f in flows.items():
        src, sport, dst, dport = key
        if dport != port or sport == port:
            continue
        back = flows.get((dst, dport, src, sport))
        if back is None:
            back = dict(isn=None, segs=[], fin=False, rst=False, syn=False, first=None, last=None, frames=[],
                        truncated=0)
        c2s, cg, cc, cs = _stitch(f)
        s2c, sg, sc, ss = _stitch(back)
        firsts = [x for x in (f["first"], back["first"]) if x is not None]
        lasts = [x for x in (f["last"], back["last"]) if x is not None]
        conns.append(dict(client="%s:%d" % (src, sport), server="%s:%d" % (dst, dport), client_ip=src,
                          server_ip=dst, client_port=sport, first_ts=min(firsts) if firsts else None,
                          last_ts=max(lasts) if lasts else None, frames=sorted(f["frames"] + back["frames"]),
                          syn=f["syn"], fin_c2s=f["fin"], fin_s2c=back["fin"], rst=f["rst"] or back["rst"],
                          truncated_segments=f["truncated"] + back["truncated"], c2s=c2s, s2c=s2c, c2s_gaps=cg,
                          s2c_gaps=sg, c2s_conflicts=cc, s2c_conflicts=sc, c2s_spans=cs, s2c_spans=ss))
    conns.sort(key=lambda c: (c["frames"][0] if c["frames"] else 0, c["client"]))
    return conns


def arrival_time(spans, offset):
    """Capture time at which stream byte `offset` first arrived (spans from tcp_connections), or None."""
    best = None
    for rel, end, ts in spans:
        if rel <= offset < end and ts is not None and (best is None or ts < best):
            best = ts
    return best


# ------------------------------------------------------------------ TLS records
def tls_records(stream):
    """Split a TCP payload stream into TLS records: [(offset, type, length)], trailing_bytes."""
    out, off = [], 0
    while off + 5 <= len(stream):
        typ, _ver, ln = struct.unpack_from(">BHH", stream, off)
        if off + 5 + ln > len(stream):
            break
        out.append((off, typ, ln))
        off += 5 + ln
    return out, len(stream) - off


def hkdf_expand_label(secret, label, context, length, hash_name):
    """HKDF-Expand-Label (RFC 8446, section 7.1)."""
    full = b"tls13 " + label
    info = length.to_bytes(2, "big") + bytes([len(full)]) + full + bytes([len(context)]) + context
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(secret, t + info + bytes([i]), hash_name).digest()
        out += t
        i += 1
    return out[:length]


def tls13_exporter(exporter_master, label, context, length, hash_name=None):
    """TLS-Exporter(label, context, length) (RFC 8446, section 7.5), as SSL_export_keying_material computes it."""
    if hash_name is None:
        hash_name = {32: "sha256", 48: "sha384"}[len(exporter_master)]
    empty = hashlib.new(hash_name, b"").digest()
    derived = hkdf_expand_label(exporter_master, label, empty, len(empty), hash_name)
    return hkdf_expand_label(derived, b"exporter", hashlib.new(hash_name, context).digest(), length, hash_name)


def read_keylog(path):
    """NSS key log -> {client_random_hex: {label: bytes}}."""
    keys = defaultdict(dict)
    with open(path, encoding="ascii", errors="replace") as f:
        for line in f:
            p = line.split()
            if len(p) == 3 and not p[0].startswith("#"):
                try:
                    keys[p[1].lower()][p[0]] = bytes.fromhex(p[2])
                except ValueError:
                    continue
    return dict(keys)


def handshake_messages(buf):
    off, out = 0, []
    while off + 4 <= len(buf):
        ln = int.from_bytes(buf[off + 1:off + 4], "big")
        if off + 4 + ln > len(buf):
            break
        out.append((buf[off], buf[off + 4:off + 4 + ln]))
        off += 4 + ln
    return out, buf[off:]


def parse_hello(body, is_client):
    """(random, cipher_suite or None, {extension_type: data}) of a ClientHello or ServerHello body."""
    rnd, o = body[2:34], 34
    o += 1 + body[o]
    if is_client:
        o += 2 + struct.unpack_from(">H", body, o)[0]
        o += 1 + body[o]
        suite = None
    else:
        suite = struct.unpack_from(">H", body, o)[0]
        o += 3
    exts = {}
    if o + 2 <= len(body):
        end = o + 2 + struct.unpack_from(">H", body, o)[0]
        o += 2
        while o + 4 <= end:
            et, el = struct.unpack_from(">HH", body, o)
            exts[et] = body[o + 4:o + 4 + el]
            o += 4 + el
    return rnd, suite, exts


class _Direction:
    def __init__(self, suite, hs_secret, app_secret):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
        self.hash_name, self.klen, kind = TLS_SUITES[suite]
        self.aead = AESGCM if kind == "aesgcm" else ChaCha20Poly1305
        self.secrets, self.epoch = [hs_secret, app_secret], 0
        self._keys()

    def _keys(self):
        s = self.secrets[self.epoch]
        self.cipher = self.aead(hkdf_expand_label(s, b"key", b"", self.klen, self.hash_name))
        self.iv = hkdf_expand_label(s, b"iv", b"", 12, self.hash_name)
        self.seq = 0

    def switch(self):
        self.epoch = 1
        self._keys()

    def open(self, header, ciphertext):
        nonce = bytes(a ^ b for a, b in zip(self.iv, self.seq.to_bytes(12, "big")))
        plain = self.cipher.decrypt(nonce, ciphertext, header)      # raises InvalidTag on any mismatch
        self.seq += 1
        end = len(plain)
        while end > 0 and plain[end - 1] == 0:
            end -= 1
        if end == 0:
            raise ValueError("TLS_INNER_PLAINTEXT_EMPTY")
        return plain[end - 1], plain[:end - 1]


def decode_connection(c2s, s2c, keylog):
    """Decrypt one TLS 1.3 connection (both TCP payload streams). Needs the 'cryptography' package.
    Returns client_random, suite, version, sni, psk/early-data flags, handshake types per side, leaf certificate
    SHA-256 per side, app_c2s/app_s2c (bytes), app chunks [(stream_end_offset, plaintext)], records
    [(side, index, inner_type, plaintext_length, stream_end_offset)], alerts [(side, level, description)],
    new_session_tickets. Raises TLSDecodeError (for example AEAD_FAILURE) on the first problem."""
    from cryptography.exceptions import InvalidTag
    recs = {"c2s": tls_records(c2s)[0], "s2c": tls_records(s2c)[0]}
    streams = {"c2s": c2s, "s2c": s2c}
    res = dict(client_random=None, suite=None, version=None, sni=None, psk_offered=False, early_data_offered=False,
               psk_selected=False, hello_retry=False, handshake={"c2s": [], "s2c": []}, certs={}, alerts=[],
               records=[], app_chunks={"c2s": [], "s2c": []}, new_session_tickets=0, ccs=0)
    if not recs["c2s"] or recs["c2s"][0][1] != 22:
        raise TLSDecodeError("TLS_NO_CLIENT_HELLO", "c2s", 0)
    off, _t, ln = recs["c2s"][0]
    msgs, _rest = handshake_messages(c2s[off + 5:off + 5 + ln])
    if not msgs or msgs[0][0] != 1:
        raise TLSDecodeError("TLS_NO_CLIENT_HELLO", "c2s", 0)
    crand, _s, cext = parse_hello(msgs[0][1], True)
    res["client_random"] = crand.hex()
    if 0 in cext and len(cext[0]) >= 5:
        sn = cext[0]
        res["sni"] = sn[5:5 + struct.unpack_from(">H", sn, 3)[0]].decode("ascii", "replace")
    res["early_data_offered"], res["psk_offered"] = 42 in cext, 41 in cext
    if not recs["s2c"] or recs["s2c"][0][1] != 22:
        raise TLSDecodeError("TLS_NO_SERVER_HELLO", "s2c", 0)
    off, _t, ln = recs["s2c"][0]
    msgs, _rest = handshake_messages(s2c[off + 5:off + 5 + ln])
    if not msgs or msgs[0][0] != 2:
        raise TLSDecodeError("TLS_NO_SERVER_HELLO", "s2c", 0)
    srand, suite, sext = parse_hello(msgs[0][1], False)
    if srand == HRR_RANDOM:
        res["hello_retry"] = True
        raise TLSDecodeError("TLS_HELLO_RETRY_NOT_SUPPORTED", "s2c", 0)
    if suite not in TLS_SUITES:
        raise TLSDecodeError("TLS_SUITE_UNSUPPORTED", "s2c", 0, "0x%04x" % (suite or 0))
    res["suite"] = "0x%04x" % suite
    res["version"] = "0x%04x" % struct.unpack(">H", sext.get(43, b"\x00\x00")[:2])[0]
    res["psk_selected"] = 41 in sext
    k = keylog.get(crand.hex())
    if not k or any(label not in k for label in KEYLOG_LABELS[:4]):
        raise TLSDecodeError("TLS_KEYS_MISSING", None, None, crand.hex()[:16])
    dirs = {"c2s": _Direction(suite, k["CLIENT_HANDSHAKE_TRAFFIC_SECRET"], k["CLIENT_TRAFFIC_SECRET_0"]),
            "s2c": _Direction(suite, k["SERVER_HANDSHAKE_TRAFFIC_SECRET"], k["SERVER_TRAFFIC_SECRET_0"])}
    app = {"c2s": bytearray(), "s2c": bytearray()}
    for side in ("s2c", "c2s"):
        d, hsbuf, data = dirs[side], b"", streams[side]
        for index, (off, typ, ln) in enumerate(recs[side][1:], 1):
            end = off + 5 + ln
            if typ == 20:
                res["ccs"] += 1
                continue
            if typ != 23:
                raise TLSDecodeError("TLS_UNEXPECTED_CLEARTEXT_RECORD", side, index, str(typ))
            try:
                inner, plain = d.open(data[off:off + 5], data[off + 5:end])
            except InvalidTag:
                raise TLSDecodeError("AEAD_FAILURE", side, index, "epoch %d" % d.epoch)
            except ValueError as e:
                raise TLSDecodeError(str(e), side, index)
            res["records"].append((side, index, inner, len(plain), end))
            if inner == 23:
                if d.epoch != 1:
                    raise TLSDecodeError("TLS_APPLICATION_DATA_UNDER_HANDSHAKE_KEYS", side, index)
                app[side] += plain
                res["app_chunks"][side].append((end, plain))
            elif inner == 21:
                res["alerts"].append((side, plain[0] if plain else None, plain[1] if len(plain) > 1 else None))
            elif inner == 22:
                hsbuf += plain
                hs, hsbuf = handshake_messages(hsbuf)
                for mt, mb in hs:
                    res["handshake"][side].append(mt)
                    if mt == 11 and side not in res["certs"]:        # Certificate: context, list -> leaf DER
                        o = 1 + mb[0] + 3
                        if o + 3 <= len(mb):
                            cl = int.from_bytes(mb[o:o + 3], "big")
                            if cl:
                                res["certs"][side] = hashlib.sha256(mb[o + 3:o + 3 + cl]).hexdigest()
                    if mt == 4:
                        res["new_session_tickets"] += 1
                    if mt == 24:
                        raise TLSDecodeError("TLS_KEY_UPDATE_NOT_SUPPORTED", side, index)
                    if mt == 20 and d.epoch == 0:
                        d.switch()
            else:
                raise TLSDecodeError("TLS_UNEXPECTED_INNER_TYPE", side, index, str(inner))
    res["app_c2s"], res["app_s2c"] = bytes(app["c2s"]), bytes(app["s2c"])
    return res


def cryptography_available():
    try:
        import cryptography.hazmat.primitives.ciphers.aead  # noqa: F401
        return True
    except ImportError:
        return False


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if len(argv) < 3:
        print(__doc__.strip())
        return 2
    cap_path, kl_path = argv[1], argv[2]
    port = int(argv[3]) if len(argv) > 3 else DEFAULT_PORT
    cap = read_capture(cap_path)
    keys = read_keylog(kl_path)
    print("capture format=%s packets=%d interfaces=%d isb=%s dsb=%d problems=%s" % (
        cap["format"], len(cap["packets"]), len(cap["interfaces"]), cap["isb"], cap["dsb"], cap["problems"]))
    rc = 0
    for c in tcp_connections(cap, port):
        print("connection client=%s server=%s c2s=%d s2c=%d gaps=%s/%s" % (
            c["client"], c["server"], len(c["c2s"]), len(c["s2c"]), c["c2s_gaps"], c["s2c_gaps"]))
        try:
            r = decode_connection(c["c2s"], c["s2c"], keys)
        except TLSDecodeError as e:
            print("  decode error: %s direction=%s record=%s %s" % (e.code, e.direction, e.index, e.detail))
            rc = 1
            continue
        print("  suite=%s version=%s sni=%s psk=%s/%s early=%s tickets=%d" % (
            r["suite"], r["version"], r["sni"], r["psk_offered"], r["psk_selected"], r["early_data_offered"],
            r["new_session_tickets"]))
        print("  handshake server=%s client=%s alerts=%s" % (r["handshake"]["s2c"], r["handshake"]["c2s"], r["alerts"]))
        print("  leaf certificate SHA-256: server=%s client=%s" % (r["certs"].get("s2c"), r["certs"].get("c2s")))
        print("  app_c2s=%d sha256=%s" % (len(r["app_c2s"]), hashlib.sha256(r["app_c2s"]).hexdigest()))
        print("  app_s2c=%d sha256=%s" % (len(r["app_s2c"]), hashlib.sha256(r["app_s2c"]).hexdigest()))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
