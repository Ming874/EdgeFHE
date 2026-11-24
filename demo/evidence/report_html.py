"""Self-contained report.html for an EdgeFHE demo evidence run (inline SVG and CSS, no external resources).

Sections: overall verdict, gates G0-G11, claims and the gates behind them, per-connection timeline (capture clock;
server.log events placed with the estimated clock offset theta), record-size signature, hash join, residue
histograms, cloud computation, plaintext search, energy (when probe.csv overlaps the capture), inputs, limitations.
A PC rehearsal (verify_report.json field pc_rehearsal) is labelled 'PC 演練（非 KV260 板上實測）' in the title, the
verdict panel, the timeline and the energy section, and its claims name 'PC 上的板端程式' instead of KV260.
The page is deterministic: the same verify_report.json (and probe.csv) gives the same bytes; no wall clock.

usage: python -B report_html.py --report RUN/verify_report.json [--out RUN/report.html] [--probe RUN/probe.csv]
Library: write_report(report, run_dir, out_path, probe_csv=None). Python 3.10 or later, standard library only.
Research demonstration only; not a medical diagnosis.
"""
import argparse
import html
import json
import os
import sys

STATUS = {"PASS": ("✓", "通過", "ok"), "FAIL": ("✗", "未通過", "bad"), "FAIL-PENDING": ("!", "待確認", "warn"),
          "INCOMPLETE": ("…", "資料不足", "warn"), "BLOCKED": ("⊘", "無法檢查", "warn"),
          "SKIP": ("—", "未執行（選用）", "muted")}
CLAIMS = (
    ("C0", "擷取檔在 TLS 金鑰紀錄被使用之前已封存，且沒有遺失封包或 TCP 缺口。", ("G0",)),
    ("C1", "KV260 與雲端電腦之間只有 TLS 1.3 加密連線，且每筆表單的紀錄大小固定。", ("G1", "G2")),
    ("C2", "雲端收到的應用資料全部是 EFHEW001 訊框，即公開標頭與 CKKS 密文。", ("G3", "G6")),
    ("C3", "每一個請求都綁定於該次 TLS 連線，請求與回應的雜湊值一致。", ("G4",)),
    ("C4", "雲端的運算可由第三方以公開模型逐位元重算。", ("G5",)),
    ("C5", "表單答案的任何編碼都未出現在擷取的位元組中。", ("G7",)),
    ("C6", "雲端送出回應之後，KV260 未再傳送任何應用資料。", ("G8",)),
    ("C7", "擷取檔、雲端紀錄與板端公開紀錄三方一致，且表單承諾可以開啟。", ("G9",)),
    ("C8", "解密結果與明文參考值一致，畫面顯示的風險等級一致。", ("G10",)),
    ("C9", "獨立觀察者的擷取與雲端電腦的擷取一致。", ("G11",)),
)
CLASS_TEXT = {"form": "表單", "handshake_only": "僅交握", "other": "其他", "undecrypted": "無法解密"}
SOURCE_TEXT = {"pcapng interface statistics block": "pcapng 介面統計區塊", "dumpcap statistics": "dumpcap 統計輸出",
               "tcpdump statistics": "tcpdump 統計輸出"}
CROSS_TEXT = {"PASS": "一致", "FAIL": "不一致", "available": "可用",
              "cryptography package not installed": "未安裝 cryptography 套件，未執行"}
VERDICT_TEXT = {"FAIL": "未通過", "FAIL-PENDING": "待確認", "BLOCKED": "無法檢查", "INCOMPLETE": "資料不足"}
SEAL_ORDER_TEXT = {"PASS": "efhe_seal.py 未記錄異常（先封存，後使用金鑰紀錄）",
                   "FAIL-PENDING": "封存晚於金鑰紀錄的使用（待確認，見 seal_order.json）"}
SVG_FONT = 18                                   # px at the SVG's natural size (no figure is drawn smaller)
DEFAULT_PORT = 18443                            # only when a report carries no port (efhe_verify always records it)
REHEARSAL_LABEL = "PC 演練（非 KV260 板上實測）"
REHEARSAL_BOARD = "PC 上的板端程式"               # replaces 'KV260' in claims and figures of a PC rehearsal
REHEARSAL_REASON_TEXT = {"rehearsal_summary_present": "執行目錄含有 PC 演練摘要 rehearsal_summary.txt",
                         "loopback_board_address": "板端位址為本機迴路位址",
                         "board_address_equals_cloud_address": "板端位址與雲端位址相同"}
CSS = """
:root{--ink:#18392f;--text:#243e34;--muted:#5d6d62;--line:#d8e1d5;--canvas:#f4f6f2;--panel:#ffffff;--green:#178365;
--amber:#8a6220;--red:#a2392b;--blue:#2d5f8d;--up:#2d5f8d;--down:#178365;--hs:#9aa79e;--grid:#e6ece3}
@media (prefers-color-scheme:dark){:root{--ink:#e4eee7;--text:#d3e0d7;--muted:#9bb0a3;--line:#2e4038;--canvas:#0f1814;
--panel:#16221d;--green:#4fbf98;--amber:#d9a85b;--red:#e2806f;--blue:#7fb0e0;--up:#7fb0e0;--down:#4fbf98;--hs:#6c7d72;
--grid:#22322b}}
*{box-sizing:border-box}
body{margin:0;background:var(--canvas);color:var(--text);font-family:'Segoe UI','Microsoft JhengHei','Noto Sans TC',
sans-serif;font-size:18px;line-height:1.65}
main{max-width:1180px;margin:0 auto;padding:28px 18px 60px}
h1{font-size:30px;color:var(--ink);margin:0 0 6px;line-height:1.3}
h2{font-size:22px;color:var(--ink);margin:34px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:18px;color:var(--ink);margin:18px 0 8px}
p{margin:6px 0}
.notice{background:var(--panel);border:1px solid var(--line);border-left:5px solid var(--amber);padding:10px 14px;
border-radius:8px;margin:14px 0}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
.verdict{font-size:22px;font-weight:600}
.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}.muted{color:var(--muted)}
table{border-collapse:collapse;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:10px;
overflow:hidden}
th,td{text-align:left;vertical-align:top;padding:8px 10px;border-bottom:1px solid var(--line)}
th{font-weight:600;color:var(--ink);background:var(--grid);white-space:nowrap}
td.st{white-space:nowrap;font-weight:600}
code,.mono{font-family:Consolas,'Cascadia Mono',monospace;font-size:18px;overflow-wrap:anywhere}
.scroll{overflow-x:auto}
.small{font-size:18px;color:var(--muted)}
ul{margin:6px 0 6px 22px;padding:0}
svg text{font-family:'Segoe UI','Microsoft JhengHei','Noto Sans TC',sans-serif}
svg.wide{display:block;min-width:1000px}
.legend span{display:inline-block;margin-right:18px}
.sw{display:inline-block;width:14px;height:10px;margin-right:6px;vertical-align:middle;border-radius:2px}
details{margin:8px 0}summary{cursor:pointer;color:var(--ink)}
"""


def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


def num(n):
    return "{:,}".format(n) if isinstance(n, int) else esc(n)


def chip(status):
    sym, text, cls = STATUS.get(status, ("?", status, "muted"))
    return '<span class="%s">%s %s</span>' % (cls, sym, esc(text))


def short(h, n=16):
    return esc(h[:n]) if isinstance(h, str) else "—"


def checks_of(report, gate, name=None):
    g = report["gates"].get(gate, {})
    return [c for c in g.get("checks", []) if name is None or c.get("name") == name]


def is_rehearsal(report):
    """True when efhe_verify found that the board side ran on a PC (field pc_rehearsal of verify_report.json)."""
    return bool((report.get("pc_rehearsal") or {}).get("detected"))


def board_text(report, text):
    """Text that names the board side: unchanged for a KV260 run; in a PC rehearsal every 'KV260' (with the space that
    separates it from the following Chinese text) becomes 'PC 上的板端程式', so no claim or figure of a PC run
    reads as a KV260 measurement. Callers never pass the PC rehearsal label itself."""
    if not is_rehearsal(report):
        return text
    return text.replace("KV260 ", REHEARSAL_BOARD).replace("KV260", REHEARSAL_BOARD)


def seal_order_status(report):
    """Status of the G0 seal_order check (FAIL-PENDING when efhe_seal.py recorded a late seal), or None."""
    found = checks_of(report, "G0", "seal_order") if report.get("gates") else []
    return found[0].get("status") if found else None


# ------------------------------------------------------------------ per-gate one-line summaries
def gate_line(report, g):
    gd = report["gates"].get(g, {})
    st = gd.get("status")
    cs = gd.get("checks", [])
    try:
        if g == "G0":
            d = {c["name"]: c for c in cs}
            drop = d.get("dropped_packets", {})
            src = SOURCE_TEXT.get(drop.get("source"), drop.get("source") or "無來源")
            order = SEAL_ORDER_TEXT.get(d.get("seal_order", {}).get("status"), "未檢查（沒有封存紀錄）")
            return "封存雜湊：%s；封存順序：%s；遺失封包 %s（來源：%s）；TCP 缺口：%s。" % (
                "一致" if d.get("sealed_hash", {}).get("status") == "PASS" else "未確認", order,
                drop.get("dropped", "未知"), esc(src), "無" if d.get("tcp_gaps", {}).get("status") == "PASS" else "有")
        if g == "G1":
            sig = [c for c in cs if c["name"] == "record_signature"]
            forms = sum(1 for c in sig if c.get("classification") == "form")
            hs = sum(1 for c in sig if c.get("classification") == "handshake_only")
            inv = next((c for c in cs if c["name"] == "only_unicast_flow_is_tls_port"), {})
            other = (inv.get("frames") or {}).get("board_pc_other", 0)
            port = report.get("port") or DEFAULT_PORT
            return board_text(report, "連線 %d 條（表單 %d 條、僅交握 %d 條）；KV260 與雲端電腦之間，TCP 連接埠 %d 以外"
                                      "的單播封包 %d 個。" % (len(sig), forms, hs, int(port), other))
        if g == "G2":
            suites = sorted(set(c.get("suite") or "?" for c in cs))
            tickets = sorted(set(c.get("tickets") for c in cs if c.get("tickets") is not None))
            native = sorted(set(CROSS_TEXT.get(c.get("native_crosscheck"), "未執行") for c in cs))
            return "TLS 1.3，加密套件 %s；工作階段票證每條 %s 張；兩側葉憑證與釘選值比對；獨立解密交叉檢查：%s。" % (
                esc(",".join(suites)), esc(",".join(str(t) for t in tickets) or "0"), esc(",".join(native)))
        if g == "G3":
            ok = sum(1 for c in cs if c["status"] == "PASS")
            return "%d 條連線中 %d 條的應用資料位元組全部屬於 EFHEW001 訊框。" % (len(cs), ok)
        if g == "G4":
            rows = [r for c in cs for r in c.get("requests", [])]
            good = sum(1 for r in rows if r.get("request_id_ok") and r.get("request_hash_ok") and r.get(
                "session_binding_ok") and r.get("response_binding_ok"))
            return "%d 個請求中 %d 個的 request_id、request_hash、回應綁定與 TLS 匯出值全部一致。" % (len(rows), good)
        if g == "G5":
            pairs = [p for c in cs for p in c.get("pairs", [])]
            exact = sum(1 for p in pairs if p.get("bit_exact"))
            return "%d 個回應中 %d 個逐位元等於公開仿射運算的結果（每個 24,576 個 32 位元字）。" % (len(pairs), exact)
        if g == "G6":
            rows = [r for c in cs if c["name"] == "ciphertext" for r in c.get("requests", [])]
            limbs = sum(r.get("limbs", 0) for r in rows)
            uni = sum(r.get("uniform_limbs", 0) for r in rows)
            rep = next((c for c in cs if c["name"] == "no_repeated_16KiB_block"), {})
            fresh = next((c for c in cs if c["name"] == "fresh_key_and_generation"), {})
            return "請求 RNS 分量 %s 個中 %s 個通過均勻性檢定；重複 16 KiB 區塊 %s 個；金鑰識別碼或世代值重複：%s。" % (
                num(limbs), num(uni), num(rep.get("repeated_blocks", 0)),
                "無" if fresh.get("status") == "PASS" else "有")
        if g == "G7":
            dom = next((c for c in cs if c["name"] == "domain_long_patterns"), {})
            pc = next((c for c in cs if c["name"] == "positive_control"), {})
            forms = [c for c in cs if c["name"] == "disclosed_form"]
            return "表單值域樣式 %s 個（密集 %s、稀疏 %s）；揭露表單 %d 筆；未解釋命中 %d 個；預期隨機 4 位元組命中 %s；正對照：%s。" % (
                num(dom.get("patterns", 0)), num(dom.get("dense_patterns", 0)), num(dom.get("sparse_patterns", 0)),
                len(forms), len(dom.get("unexplained_hits", [])) + sum(len(c.get("long_hits", [])) for c in forms),
                esc(gd.get("details", {}).get("expected_chance_word_hits")), "成功" if pc.get("status") == "PASS" else "失敗")
        if g == "G8":
            ok = sum(1 for c in cs if c["status"] == "PASS")
            return "%d 條連線中 %d 條在回應之後沒有用戶端應用資料（僅 close_notify）。" % (len(cs), ok)
        if g == "G9":
            d = {c["name"]: c for c in cs}
            return "擷取與雲端紀錄一致 %s 筆；擷取與板端公開紀錄一致 %s 筆；表單承諾開啟 %s 筆。" % (
                d.get("capture_server_log", {}).get("matched", "—"), d.get("capture_board_log", {}).get("matched", "—"),
                len(d.get("commitment_opens", {}).get("opened", []) or []))
        if g == "G10":
            ok = sum(1 for c in cs if c["status"] == "PASS" and c["name"] == "inference")
            n = sum(1 for c in cs if c["name"] == "inference")
            return "揭露的虛構表單 %d 筆中 %d 筆的 |logit_ckks − logit_plain| ≤ 2×10⁻³ 且風險等級一致。" % (n, ok)
        if g == "G11":
            if st == "SKIP":
                return "本次未提供獨立觀察者的擷取。"
            c = cs[0] if cs else {}
            return "觀察者擷取連線 %s 條；與雲端擷取的每連線 TCP 酬載雜湊比對結果：%s。" % (
                c.get("observer_connections", "—"), "一致" if st == "PASS" else "不一致")
    except (KeyError, TypeError, ValueError):
        return ""
    return ""


def problems_of(report, g):
    out = []
    for c in report["gates"].get(g, {}).get("checks", []):
        if c.get("status") not in ("PASS", "SKIP"):
            text = c.get("problems") or c.get("reason") or c.get("unexplained_hits") or c.get("mismatched") or ""
            out.append("%s %s: %s" % (c.get("connection", ""), c.get("name"), json.dumps(text, ensure_ascii=False,
                                                                                          sort_keys=True)[:400]))
    return out


# ------------------------------------------------------------------ SVG pieces
def svg_timeline(report):
    conns = [c for c in report.get("connections", []) if c.get("records")]
    if not conns:
        return '<p class="small">本次擷取沒有可繪製的連線。</p>'
    theta = (report.get("theta") or {}).get("server_log")
    joined = (report["gates"].get("G9", {}).get("details") or {}).get("joined", [])
    marks = {}
    for j in joined:
        if theta is not None and j.get("log_epoch") is not None:
            marks.setdefault(j["connection"], []).append(j["log_epoch"] + theta)
    spans = []
    for c in conns:
        t0 = min([r[1] for r in c["records"]] + ([c["t_syn"]] if c.get("t_syn") else []))
        t1 = max(r[1] for r in c["records"])
        spans.append((t0, t1))
    # Text is SVG_FONT user units and the SVG never shrinks below its viewBox width (class wide), so every label
    # renders at 18 px or more; a narrow window scrolls the figure instead.
    width, left, right, lane = 1000, 240, 50, 76
    span = max(max(t1 - t0 for t0, t1 in spans), 1e-6)
    scale = (width - left - right) / span
    height = 44 + lane * len(conns)
    out = ['<svg class="wide" viewBox="0 0 %d %d" width="100%%" role="img" aria-label="每條連線的 TLS 紀錄時序">' % (
        width, height)]
    step = nice_step(span * 1000.0)
    t = 0.0
    while t <= span * 1000.0 + 1e-9:
        x = left + t / 1000.0 * scale
        out.append('<line x1="%.1f" y1="26" x2="%.1f" y2="%d" stroke="var(--grid)"/>' % (x, x, height - 6))
        out.append('<text x="%.1f" y="18" font-size="%d" fill="var(--muted)" text-anchor="middle">%s ms</text>' % (
            x, SVG_FONT, fmt_ms(t)))
        t += step
    for i, (c, (t0, _t1)) in enumerate(zip(conns, spans)):
        y = 44 + lane * i + lane / 2 - 8
        out.append('<text x="8" y="%.1f" font-size="%d" fill="var(--ink)">%s %s</text>' % (
            y - 10, SVG_FONT, esc(c["label"]), esc(CLASS_TEXT.get(c.get("classification"), ""))))
        out.append('<text x="8" y="%.1f" font-size="%d" fill="var(--muted)">%s</text>' % (y + 16, SVG_FONT,
                                                                                         esc(c["client"])))
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--line)"/>' % (left, y, width - right, y))
        for d, ts, ln, typ, enc in c["records"]:
            x = left + (ts - t0) * scale
            h = 6 if ln < 300 else (12 if ln < 16000 else 18)
            color = "var(--hs)"
            if ln in (273, 16401):
                color = "var(--up)" if d == "c2s" else "var(--down)"
            if d == "c2s":
                out.append('<rect x="%.1f" y="%.1f" width="2" height="%d" fill="%s"/>' % (x, y - h, h, color))
            else:
                out.append('<rect x="%.1f" y="%.1f" width="2" height="%d" fill="%s"/>' % (x, y + 1, h, color))
        for tm in marks.get(c["label"], []):
            x = left + (tm - t0) * scale
            out.append('<path d="M%.1f %.1f l5 6 l-5 6 l-5 -6 z" fill="var(--amber)"/>' % (x, y - 26))
        if c.get("t_syn"):
            x = left + (c["t_syn"] - t0) * scale
            out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="var(--ink)" stroke-dasharray="3 2"/>' % (
                x, y - 20, x, y + 20))
    out.append("</svg>")
    legend = board_text(report, '<p class="legend small"><span><i class="sw" style="background:var(--up)"></i>上行（KV260→雲端）'
                                '標頭與密文紀錄</span><span><i class="sw" style="background:var(--down)"></i>下行（雲端→KV260）'
                                '</span><span><i class="sw" style="background:var(--hs)"></i>交握、票證與 close_notify</span>'
                                '<span><i class="sw" style="background:var(--amber)"></i>雲端紀錄寫出回應的時間（已校正時鐘'
                                '偏移 θ）</span><span>虛線：TCP SYN</span></p>')
    return '<div class="scroll">%s</div>%s' % ("".join(out), legend)


def nice_step(total_ms):
    for s in (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 60000):
        if total_ms / s <= 8:
            return float(s)
    return 120000.0


def fmt_ms(t):
    return ("%.1f" % t) if t < 10 and t != int(t) else "{:,}".format(int(round(t)))


def svg_histogram(hist, label):
    if not hist:
        return ""
    total = float(sum(hist)) or 1.0
    exp = total / len(hist)
    w, h, pad = 300, 90, 4
    bw = (w - 2 * pad) / len(hist)
    peak = max(max(hist) / exp, 2.0)
    out = ['<svg viewBox="0 0 %d %d" width="%d" height="%d" role="img" aria-label="%s">' % (w, h + 26, w, h + 26,
                                                                                          esc(label))]
    for i, v in enumerate(hist):
        bh = (v / exp) / peak * h
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="var(--up)"/>' % (
            pad + i * bw + 0.5, h - bh, max(bw - 1, 0.5), bh))
    ye = h - (1.0 / peak) * h
    out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--amber)" stroke-dasharray="4 3"/>' % (
        pad, ye, w - pad, ye))
    out.append('<text x="%d" y="%d" font-size="%d" fill="var(--muted)">0</text>' % (pad, h + 21, SVG_FONT))
    out.append('<text x="%d" y="%d" font-size="%d" fill="var(--muted)" text-anchor="end">q</text>' % (
        w - pad, h + 21, SVG_FONT))
    out.append("</svg>")
    return "".join(out)


def load_probe(path):
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.readline().strip().split(",")
            ih, ip = head.index("host_s"), head.index("P_mW")
            for line in f:
                p = line.strip().split(",")
                if len(p) > max(ih, ip):
                    try:
                        rows.append((float(p[ih]), float(p[ip])))
                    except ValueError:
                        continue
    except (OSError, ValueError):
        return []
    rows.sort()
    return rows


def energy_section(report, samples):
    conns = [c for c in report.get("connections", []) if c.get("t_first") is not None]
    if not samples or not conns:
        return '<p class="small">本次執行目錄沒有能耗探針紀錄（probe.csv），因此本節從缺。</p>'
    windows = []
    for c in conns:
        start = (c.get("t_syn") or c["t_first"]) - 2.0
        windows.append((c["label"], start, c["t_last"] + 0.5))
    lo, hi = min(w[1] for w in windows) - 3.0, max(w[2] for w in windows) + 3.0
    sel = [s for s in samples if lo <= s[0] <= hi]
    if len(sel) < 5:
        return '<p class="small">probe.csv 與本次擷取的時間範圍沒有足夠的重疊，因此本節從缺。</p>'
    inside = lambda t: any(a <= t <= b for _l, a, b in windows)  # noqa: E731
    idle = sorted(p for t, p in sel if not inside(t))
    idle_mw = idle[len(idle) // 2] if idle else min(p for _t, p in sel)
    rows = []
    for label, a, b in windows:
        pts = [s for s in sel if a <= s[0] <= b]
        e = 0.0
        for (t1, p1), (t2, _p2) in zip(pts, pts[1:]):
            e += (p1 - idle_mw) * (t2 - t1) / 1000.0
        rows.append((label, b - a, e, len(pts)))
    w, h, left = 1000, 220, 100
    pmin, pmax = min(p for _t, p in sel), max(p for _t, p in sel)
    pr = max(pmax - pmin, 1.0)
    sx = (w - left - 10) / (hi - lo)
    rows_y = (h + 22, h + 44, h + 66)              # window labels: first row with room, else left to the table below
    out = ['<svg class="wide" viewBox="0 0 %d %d" width="100%%" role="img" aria-label="%s">' % (
        w, rows_y[-1] + 8, esc(board_text(report, "KV260 功率與各連線時間窗")))]
    ends = [None] * len(rows_y)
    for label, a, b in windows:
        x = left + (a - lo) * sx
        out.append('<rect x="%.1f" y="10" width="%.1f" height="%d" fill="var(--grid)"/>' % (
            x, max((b - a) * sx, 1), h - 10))
        for k, yk in enumerate(rows_y):
            if ends[k] is None or x + 2 >= ends[k]:
                out.append('<text x="%.1f" y="%d" font-size="%d" fill="var(--muted)">%s</text>' % (
                    x + 2, yk, SVG_FONT, esc(label)))
                ends[k] = x + 2 + SVG_FONT * 0.6 * len(label) + 8
                break
    step = max(1, len(sel) // 1200)
    pts = " ".join("%.1f,%.1f" % (left + (t - lo) * sx, h - (p - pmin) / pr * (h - 20)) for t, p in sel[::step])
    out.append('<polyline points="%s" fill="none" stroke="var(--blue)" stroke-width="1.5"/>' % pts)
    yi = h - (idle_mw - pmin) / pr * (h - 20)
    out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--amber)" stroke-dasharray="5 3"/>' % (
        left, yi, w - 10, yi))
    out.append('<text x="4" y="22" font-size="%d" fill="var(--muted)">%.0f mW</text>' % (SVG_FONT, pmax))
    out.append('<text x="4" y="%d" font-size="%d" fill="var(--muted)">%.0f mW</text>' % (h, SVG_FONT, pmin))
    out.append("</svg>")
    table = ["<table><tr><th>連線</th><th>時間窗</th><th>樣本數</th><th>ΔE（≈，扣除閒置中位數 %.0f mW）</th></tr>" % idle_mw]
    for label, dur, e, n in rows:
        table.append("<tr><td>%s</td><td>%.1f s</td><td>%d</td><td>≈ %.2f J</td></tr>" % (esc(label), dur, n, e))
    table.append("</table>")
    note = ('<p class="small">本節以探針的主機時間戳記對齊擷取時間；時間窗為 [SYN − 2 s, 最後一筆紀錄 + 0.5 s]。'
            '探針取樣率約 10 Hz，且閒置功率遠大於單筆表單的功率變化，因此單筆表單的能量差只能估計（標示 ≈）。</p>')
    if is_rehearsal(report):
        note += ('<p class="warn">%s：本次的連線來自 PC 上的板端程式，探針紀錄與本次連線不一定來自同一台裝置；'
                 '上表的 ΔE 只用於檢查本節的計算流程，不是 KV260 的能耗結果。</p>' % REHEARSAL_LABEL)
    return '<div class="scroll">%s</div>%s%s' % ("".join(out), "".join(table), note)


# ------------------------------------------------------------------ page
def render(report, probe_samples=None):
    gates = report.get("gates", {})
    verdict = report.get("verdict", "")
    ok = report.get("evidence_pass")
    inputs = report.get("inputs", {})
    cap = inputs.get("capture") or {}
    reh = is_rehearsal(report)
    tag = "（PC 演練）" if reh else ""
    parts = ['<!doctype html>\n<html lang="zh-Hant-TW">\n<head>\n<meta charset="utf-8">\n'
             '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
             '<title>EdgeFHE 證據驗證報告%s</title>\n<style>%s</style>\n</head>\n<body>\n<main>\n' % (tag, CSS)]
    parts.append("<h1>EdgeFHE 期末展示：封包證據驗證報告%s</h1>" % tag)
    keylog_text = "，以及本次證據場次示範專用的 TLS 金鑰紀錄" if inputs.get("keylog") else ""
    parts.append('<p class="notice">本報告是研究展示的驗證紀錄，不構成醫療診斷；報告中的表單答案均為虛構資料。'
                 '雲端電腦全程沒有任何 CKKS 金鑰，無法解密 CKKS 密文：雲端電腦只持有公開模型、收到的密文、'
                 'TLS 伺服器憑證與私鑰%s。TLS 金鑰只保護傳輸過程，不能解開 CKKS 密文。</p>' % keylog_text)
    cls = "ok" if ok else ("bad" if verdict.startswith("FAIL:") else "warn")
    if ok:
        vtext = "G0–G10 全部通過"
    elif ":" in verdict:
        kind, gl = verdict.split(":", 1)
        vtext = "%s（%s）" % (VERDICT_TEXT.get(kind, kind), "、".join(gl.split(",")))
    else:
        vtext = verdict
    parts.append('<div class="panel"><p class="verdict %s">整體判定：%s</p>' % (cls, esc(vtext)))
    if reh:
        why = "；".join(REHEARSAL_REASON_TEXT.get(r, r) for r in report["pc_rehearsal"].get("reasons", []))
        parts.append('<p class="warn">資料來源：%s。本次的板端程式在 PC 上執行（判定依據：%s），因此報告中的時間、'
                     '紀錄與能耗都是 PC 演練值，不代表 KV260 的表現；各驗證項目的判定方式與正式場次相同。'
                     '主張與圖表中的「%s」即指此程式。</p>' % (REHEARSAL_LABEL, esc(why), REHEARSAL_BOARD))
    parts.append("<p>擷取檔 <code>%s</code>，SHA-256 <code>%s</code>，大小 %s 位元組。驗證工具：tshark %s；"
                 "獨立 TLS 1.3 解密交叉檢查：%s。</p>" % (
                     esc(cap.get("path")), esc(cap.get("sha256")), num(cap.get("bytes")),
                     esc(report.get("tool", {}).get("tshark")),
                     esc(CROSS_TEXT.get(report.get("tool", {}).get("native_decrypt"), report.get("tool", {}).get(
                         "native_decrypt")))))
    if seal_order_status(report) == "FAIL-PENDING":
        parts.append('<p class="warn">封存順序：efhe_seal.py 記錄到 capture_dsb.pcapng 在封存紀錄 capture.sha256 '
                     '寫入之前已經存在，表示 TLS 金鑰紀錄在擷取檔封存之前已被使用。因此本報告不能主張「擷取檔先封存、'
                     '金鑰紀錄後使用」；驗證項目 G0 標示為待確認，須以獨立觀察者的擷取（G11）或現場紀錄確認擷取檔'
                     '未被更動。</p>')
    neg = report.get("negative_control") or {}
    if neg.get("detected"):
        first = neg["requests"][0]
        parts.append('<p class="warn">負對照：驗證項目 G6 判定連線 %s 第 %s 個請求的餘數不是均勻分布的密文（%s）。'
                     '負對照的目的是證明檢查有效，因此此結果符合預期。</p>' % (
                         esc(first.get("connection")), esc(first.get("sequence")), esc(",".join(first.get("reasons", [])))))
    parts.append("</div>")
    parts.append("<h2>驗證項目判定</h2><div class=\"scroll\"><table><tr><th>驗證項目</th><th>結果</th><th>檢查內容</th>"
                 "<th>本次結果摘要</th></tr>")
    for g in ("G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10", "G11"):
        d = gates.get(g)
        if not d:
            continue
        probs = problems_of(report, g)
        extra = ""
        if probs:
            extra = "<details><summary>技術細節（%d 項）</summary><ul>%s</ul></details>" % (
                len(probs), "".join("<li><code>%s</code></li>" % esc(p) for p in probs[:12]))
        parts.append('<tr><td class="st">%s</td><td class="st">%s</td><td>%s</td><td>%s%s</td></tr>' % (
            g, chip(d.get("status")), esc(d.get("title")), gate_line(report, g), extra))
    parts.append("</table></div>")
    parts.append("<h2>主張與依據</h2><div class=\"scroll\"><table><tr><th>主張</th><th>內容</th><th>依據</th>"
                 "<th>結果</th></tr>")
    for cid, text, gs in CLAIMS:
        sts = [gates.get(g, {}).get("status", "SKIP") for g in gs]
        worst = next((s for s in ("FAIL", "FAIL-PENDING", "BLOCKED", "INCOMPLETE", "SKIP") if s in sts), "PASS")
        parts.append("<tr><td>%s</td><td>%s</td><td>%s</td><td class=\"st\">%s</td></tr>" % (
            cid, esc(board_text(report, text)), "、".join(gs), chip(worst)))
    parts.append("</table></div>")
    parts.append("<h2>連線時序%s</h2>" % tag)
    if reh:
        parts.append('<p class="warn">%s：時間軸上的時間與紀錄都來自 PC 上的板端程式，是 PC 演練值。</p>' % REHEARSAL_LABEL)
    parts.append(board_text(report, "<p>每一列是一條 TCP 連線，時間軸從該連線的 SYN 起算（擷取時鐘）。"
                                    "上方的刻度是 KV260 送出的紀錄，下方是雲端送出的紀錄。</p>"))
    parts.append(svg_timeline(report))
    th = report.get("theta")
    if th:
        parts.append('<p class="small">雲端紀錄與擷取時鐘的偏移 θ = %.3f ms（%d 筆回應的中位數，範圍 %.3f ms）；'
                     '此估計只用於本報告的時間軸，不影響任何驗證項目的判定。</p>' % (
                         th["server_log"] * 1000, th["samples"], th["spread"] * 1000))
    parts.append("<h2>紀錄大小特徵（不使用金鑰）</h2><div class=\"scroll\"><table><tr><th>連線</th><th>類別</th>"
                 "<th>用戶端交握</th><th>上行訊框</th><th>下行訊框</th><th>票證</th><th>結尾警示紀錄（上／下）</th></tr>")
    for c in report.get("connections", []):
        s = c.get("signature", {})
        parts.append("<tr><td>%s</td><td>%s</td><td class=\"mono\">%s</td><td class=\"mono\">%s</td>"
                     "<td class=\"mono\">%s</td><td class=\"mono\">%s</td><td>%s／%s</td></tr>" % (
                         esc(c["label"]), esc(CLASS_TEXT.get(c.get("classification"), "")),
                         esc("+".join(str(x) for x in s.get("client_flight", []))),
                         esc(", ".join(s.get("c2s_groups", [])) or "—"), esc(", ".join(s.get("s2c_groups", [])) or "—"),
                         esc(", ".join(str(x) for x in s.get("tickets", [])) or "—"),
                         "有" if s.get("client_close_notify") else "無", "有" if s.get("server_close_notify") else "無"))
    parts.append("</table></div><p class=\"small\">每筆表單的上行為 1 筆 273 位元組的標頭紀錄加 24 筆 16,401 位元組的"
                 "密文紀錄（4 個密文 × 2 個多項式 × 3 個 RNS 分量），下行為 1 筆標頭加 6 筆密文紀錄。</p>")
    parts.append("<h2>雜湊比對</h2><div class=\"scroll\"><table><tr><th>連線</th><th>序號</th><th>request_sha256</th>"
                 "<th>response_payload_sha256</th><th>雲端 cloud_ns</th></tr>")
    for j in (gates.get("G9", {}).get("details") or {}).get("joined", []):
        parts.append("<tr><td>%s</td><td>%s</td><td class=\"mono\">%s</td><td class=\"mono\">%s</td><td>%s</td></tr>" % (
            esc(j["connection"]), esc(j["sequence"]), short(j.get("request_sha256")),
            short(j.get("response_payload_sha256")), num(j.get("cloud_ns"))))
    parts.append("</table></div><p class=\"small\">表中只列出與雲端紀錄逐筆一致的請求；與板端公開紀錄及表單承諾的比對結果"
                 "見驗證項目 G9。雜湊值只顯示前 16 個十六進位字元，完整值見 verify_report.json。</p>")
    parts.append("<h2>密文統計</h2>")
    hist_rows = [(c.get("connection"), r) for c in checks_of(report, "G6", "ciphertext") for r in c.get("requests", [])]
    if not hist_rows:
        parts.append('<p class="small">本次沒有可統計的請求。</p>')
    for conn, r in hist_rows[:24]:
        wl = r.get("worst_limb") or {}
        parts.append('<div class="panel"><h3>%s 第 %s 個請求：%s／%s 個 RNS 分量通過均勻性檢定</h3>%s'
                     '<p class="small">最差分量：χ² = %s（p = %s），KS·√n = %s，平均值 z = %s，小餘數比例 %s（上限 %s）。'
                     '虛線為均勻分布的期望高度。</p></div>' % (
                         esc(conn), esc(r.get("sequence")), num(r.get("uniform_limbs")), num(r.get("limbs")),
                         svg_histogram(r.get("histogram"), "餘數分布直方圖"), esc(wl.get("chi2")), esc(wl.get("chi2_p")),
                         esc(wl.get("ks_sqrt_n")), esc(wl.get("mean_z")), esc(wl.get("small_frac")),
                         esc(wl.get("small_bound"))))
    parts.append("<h2>能耗</h2>")
    parts.append(energy_section(report, probe_samples or []))
    parts.append("<h2>輸入檔案</h2><div class=\"scroll\"><table><tr><th>項目</th><th>路徑</th><th>SHA-256</th>"
                 "<th>大小</th></tr>")
    for k in sorted(inputs):
        v = inputs[k] or {}
        parts.append("<tr><td>%s</td><td class=\"mono\">%s</td><td class=\"mono\">%s</td><td>%s</td></tr>" % (
            esc(k), esc(v.get("path")), esc(v.get("sha256")), num(v.get("bytes")) if v.get("bytes") is not None else (
                "%s 個檔案" % v.get("files") if v.get("directory") else "")))
    parts.append("</table></div>")
    pins = report.get("pins") or {}
    if pins:
        parts.append('<p class="small">設定的釘選值：伺服器 <code>%s</code>，用戶端 <code>%s</code>。</p>' % (
            esc(pins.get("server")), esc(pins.get("client"))))
    parts.append("<h2>限制</h2><ul>")
    if seal_order_status(report) == "FAIL-PENDING":
        parts.append("<li>本次的封存晚於 TLS 金鑰紀錄的使用（seal_order.json）：封存雜湊只能證明擷取檔自封存以後未被更動，"
                     "不能證明封存之前的狀態。</li>")
    if gates.get("G11", {}).get("status") != "PASS":
        parts.append("<li>本次沒有獨立觀察者的擷取：擷取檔與 TLS 金鑰紀錄都來自雲端電腦。第三方可以檢查兩者一致"
                     "（AEAD 驗證與雜湊比對），但不能排除擷取者本身的造假。</li>")
    parts.append("<li>TLS 與 CKKS 都不隱藏中繼資料：連線時間、紀錄大小（等於特徵數 × 96 KiB）與模型身分是公開的。</li>"
                 "<li>驗證項目 G10 依據板端揭露的虛構表單；第三方沒有 CKKS 私鑰，無法自行解密 CKKS 密文，只能重算明文參考值並比對。</li>"
                 "<li>TLS 金鑰紀錄只為展示而開啟；金鑰紀錄是否保留在封存的證據包中，由團隊決定。</li>"
                 "</ul>")
    parts.append('<p class="small">本報告由 efhe_verify.py 與 report_html.py 依 verify_report.json 產生；'
                 "同一組輸入檔每次都產生相同的內容。報告結構版本 %s。</p>" % esc(report.get("schema")))
    parts.append("</main>\n</body>\n</html>\n")
    return "".join(parts)


def write_report(report, run_dir, out_path, probe_csv=None):
    probe = probe_csv or (os.path.join(run_dir, "probe.csv") if run_dir else None)
    samples = load_probe(probe) if probe and os.path.isfile(probe) else []
    text = render(report, samples)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, out_path)
    return out_path


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Write report.html from verify_report.json.")
    ap.add_argument("--report", required=True)
    ap.add_argument("--out")
    ap.add_argument("--probe")
    a = ap.parse_args(argv)
    with open(a.report, encoding="utf-8") as f:
        report = json.load(f)
    run_dir = os.path.dirname(os.path.abspath(a.report))
    out = a.out or os.path.join(run_dir, "report.html")
    write_report(report, run_dir, out, a.probe)
    print("EFHE_REPORT_HTML_WRITTEN file=%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
