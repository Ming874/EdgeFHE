"""Token-level schedule model of the V8b rev5 ENC frame (v8b_rev5/hardware/hls/v8b.cpp), cycle-stepped.

ESTIMATE ONLY. It replays the loop structure of the six DATAFLOW processes (reader, sampler, limb x3, writer) with the
FIFO depths of v8b.cpp:609-632 and the trip counts / II of the pragmas. Pipeline depths and call overheads are
parameters (the csynth report is not in the repository); the central set is derived from the core latencies that
appear in the routed hierarchy report (hardware/reports/rev5/hierarchical.rpt).
The three limb engines are identical and fed in lockstep (the sampler writes u0/u1/u2 and e*0/e*1/e*2 in the same
statement), so one limb is simulated and its store feeds o0, o1, o2 together.
usage: python v8b_sched_model.py [k] [dump.json]
  prints the summary; it writes the full run dump (about 70 kB) only when an output path is given, so a run never
  leaves an untracked file in this folder
"""
import collections, json, random, sys

class Fifo:
    def __init__(s, name, cap):
        s.name, s.cap, s.q, s.reads, s.writes = name, cap, collections.deque(), 0, 0
    def avail(s, t, n):
        if len(s.q) < n: return False
        return s.q[n - 1] <= t           # visibility times are non-decreasing
    def space(s, n): return s.cap - len(s.q) >= n
    def pop(s, n):
        for _ in range(n): s.q.popleft()
        s.reads += n
    def push(s, tv, n):
        for j in range(n): s.q.append(tv + j)
        s.writes += n

INF = 1 << 40

def run(k=8, P=None, seed=1):
    P = dict(P)
    rnd = random.Random(seed)
    F = {n: Fifo(n, c) for n, c in [("IN", INF), ("CS", 2), ("CL", 2), ("CW", 3), ("U", 32), ("E0", 1024), ("E1", 1024),
                                     ("M", 1024), ("O0", 1024), ("O1", 1024), ("O2", 1024), ("KAT", 2), ("ST", 3),
                                     ("OUT", P["out_fifo"])]}
    # MM2S: beat n of the input frame visible at in_latency + n (DMA ahead of the reader; reader takes <= 1 beat / 2 cycles)
    n_in = 1 + 2 + 2048 * k
    if P.get("mm2s_gap") is None:
        for n in range(n_in): F["IN"].q.append(P["in_latency"] + n)
    else:
        F["IN"].cap = P.get("mm2s_fifo", 64)   # bounded: the MM2S is back-pressured by the reader
    ev = collections.defaultdict(list)   # event log: name -> list of times
    stall = collections.Counter()

    def L(name, trip, ii, depth, ovh, plan):
        return ("loop", name, trip, ii, depth, ovh, plan)

    def reader():
        yield L("rd_header_seed", 3, 1, 1, 0, lambda i: ([("IN", 1)], [], None))
        yield L("rd_ctl", 1, 1, 1, 0, lambda i: ([], [("CL", 1), ("CW", 1), ("CS", 1)], None))
        yield L("rd_cs_zero", 2, 1, 1, 0, lambda i: ([], [("CS", 1)], None))
        yield L("message_coeffs", 4096 * k, 1, P["d_msg"], P["ovh"],
                lambda i: ([("IN", 1)] if i % 2 == 0 else [], [("M", 1)] if i % 4 == 3 else [], None))
        yield L("message_scrub", 1024, 1, 1, P["ovh"], lambda i: ([], [("M", 1)], None))
        yield L("rd_status", 1, 1, 1, 0, lambda i: ([], [("ST", 1)], None))

    def sampler():
        yield L("sm_ctl", 1, 1, 1, 0, lambda i: ([("CS", 1)], [], None))
        yield ("wait", "key_from_seed", P["key_loop"])
        for f in range(k):
            yield ("wait", "feature_ovh", P["loop_ovh"])
            st = {"acc": 0, "nacc": 0, "tok": 0}
            for batch in range(8):
                yield ("wait", "batch_ovh", P["batch_ovh"])
                if st["tok"] >= 1024: break
                yield ("wait", "chacha_mask", P["chacha"])
                acc_bytes = [rnd.random() < 243 / 256 for _ in range(512)]
                def plan(j, st=st, acc_bytes=acc_bytes):
                    nacc, tok = st["nacc"], st["tok"]
                    if tok < 1024 and acc_bytes[j]: nacc += 5
                    emit = 0
                    for _ in range(2):
                        if nacc >= 4 and tok < 1024: nacc -= 4; tok += 1; emit += 1
                    def commit(nacc=nacc, tok=tok): st["nacc"], st["tok"] = nacc, tok
                    return ([], [("U", emit)] if emit else [], commit)
                yield L("tern_bytes", 512, 2, P["d_tern"], P["ovh"], plan)
            for t in range(2):
                for b in range(64):
                    yield ("wait", "batch_ovh", P["batch_ovh"])
                    yield ("wait", "chacha_gauss", P["chacha"])
                    E = "E0" if t == 0 else "E1"
                    yield L("gauss_samples", 64, 1, P["d_gauss"], P["ovh"],
                            lambda i, E=E: ([], [(E, 1)] if i % 4 == 3 else [], None))
        yield ("wait", "zero_key", P["key_loop"])
        yield ("wait", "chacha_kat", P["chacha"])
        yield ("wait", "kat_compare", P["kat_cmp"])
        yield L("noise_scrub", 1024, 1, 1, P["ovh"], lambda i: ([], [("E0", 1), ("E1", 1)] + ([("U", 1)] if i < 32 else []), None))
        yield L("kat_write", 1, 1, 1, 0, lambda i: ([], [("KAT", 1)], None))
        yield L("sm_ctl_zero", 2, 1, 1, 0, lambda i: ([("CS", 1)], [], None))

    def limb():
        yield L("lb_ctl", 1, 1, 1, 0, lambda i: ([("CL", 1)], [], None))
        for f in range(k):
            yield ("wait", "feature_ovh", P["loop_ovh"])
            yield L("u_load", 1024, 1, P["d_uload"], P["ovh"], lambda j: ([("U", 1)], [], None))
            yield ("mark", "mark_u_load_end")
            yield ("wait", "ntt", 12 * P["stage"])
            for p in range(2):
                yield ("wait", "pointwise", P["pointwise"])
                yield ("wait", "intt", 12 * P["stage"])
                E = "E0" if p == 0 else "E1"
                yield ("mark", "store_start_p%d" % p)
                yield L("store_p%d" % p, 1024, 1, P["d_store"], P["ovh"],
                        lambda j, E=E, p=p: ([(E, 1)] + ([("M", 1)] if p == 0 else []), [("O0", 1), ("O1", 1), ("O2", 1)], None))
                yield ("mark", "store_end_p%d" % p)
        yield ("wait", "wipe", P["wipe"])
        yield L("scrub_enc", 1024, 1, 2, P["ovh"],
                lambda j: ([("M", 1), ("E0", 1), ("E1", 1)] + ([("U", 1)] if j < 32 else []), [], None))
        yield ("mark", "limb_done")

    def writer():
        yield L("wr_ctl", 1, 1, 1, 0, lambda i: ([("CW", 1)], [], None))
        yield L("wr_header", 1, 1, 1, 0, lambda i: ([], [("OUT", 1)], None))
        yield L("wr_data", 6144 * k, 1, P["d_writer"], P["ovh"],
                lambda i: ([("O%d" % (i % 3), 1)], [("OUT", 1)], None))
        yield L("wr_kat_status", 1, 1, 1, 0, lambda i: ([("KAT", 1), ("ST", 1)], [], None))
        yield L("wr_trailer", 1, 1, 1, 0, lambda i: ([], [("OUT", 1)], None))
        yield ("mark", "writer_done")

    def s2mm():   # AXI DMA S2MM: accepts one beat per cycle, with an optional gap after every burst
        nb = 1 + 6144 * k + 1
        for b in range(nb):
            yield L("s2mm_beat", 1, 1, 1, 0, lambda i: ([("OUT", 1)], [], None))
            if P["s2mm_gap"] and (b + 1) % 16 == 0: yield ("wait", "s2mm_gap", P["s2mm_gap"])
        yield ("mark", "s2mm_last_beat")

    def mm2s():   # AXI DMA MM2S: one beat, then mm2s_gap idle cycles (rate = 1 / (1 + gap) beats per cycle)
        yield ("wait", "mm2s_latency", P["in_latency"])
        for b in range(n_in):
            yield L("mm2s_beat", 1, 1, 1, 0, lambda i: ([], [("IN", 1)], None))
            if P["mm2s_gap"]: yield ("wait", "mm2s_gap", P["mm2s_gap"])
        yield ("mark", "mm2s_done")

    procs = {"reader": reader(), "sampler": sampler(), "limb": limb(), "writer": writer(), "s2mm": s2mm()}
    if P.get("mm2s_gap") is not None: procs["mm2s"] = mm2s()
    state = {n: {"ready": 0, "cur": None, "done": False} for n in procs}
    phase_time = collections.defaultdict(lambda: collections.Counter())   # proc -> phase -> cycles
    t = 0
    while not all(s["done"] for s in state.values()):
        for n, g in procs.items():
            s = state[n]
            while not s["done"] and s["cur"] is None and t >= s["ready"]:
                try: a = next(g)
                except StopIteration: s["done"] = True; ev[n + "_end"].append(t); break
                if a[0] == "wait":
                    phase_time[n][a[1]] += a[2]; s["ready"] = t + a[2]
                elif a[0] == "mark":
                    ev[a[1]].append(t)
                else:
                    _, name, trip, ii, depth, ovh, plan = a
                    s["cur"] = {"name": name, "trip": trip, "ii": ii, "depth": depth, "ovh": ovh, "plan": plan, "i": 0,
                                "next": t, "start": t}
            c = s["cur"]
            if c is None or t < c["next"]: continue
            reads, writes, commit = c["plan"](c["i"])
            ok = all(F[f].avail(t, m) for f, m in reads) and all(F[f].space(m) for f, m in writes)
            if not ok:
                stall[(n, c["name"])] += 1; c["next"] = t + 1; continue
            for f, m in reads: F[f].pop(m)
            dw = max(c["depth"] - 1, 0)
            for f, m in writes: F[f].push(t + dw if dw else t + 1, m)
            if commit: commit()
            c["i"] += 1
            if c["i"] == c["trip"]:
                end = t + c["depth"] + c["ovh"]
                phase_time[n][c["name"]] += end - c["start"]
                ev[c["name"] + "_end"].append(end)
                s["cur"] = None; s["ready"] = end
            else:
                c["next"] = t + c["ii"]
        t += 1
        if t > 5_000_000: raise RuntimeError("deadlock?")
    U = ev["mark_u_load_end"]
    per = [U[i + 1] - U[i] for i in range(len(U) - 1)]
    res = {
        "k": k, "params": P,
        "u_load_end": U, "period_cycles": per,
        "steady_period_cycles": sum(per[1:]) / max(len(per) - 1, 1) if len(per) > 1 else None,
        "store1_start": ev["store_start_p1"], "store1_end": ev["store_end_p1"],
        "message_coeffs_end": ev["message_coeffs_end"], "mask_done": None,
        "writer_done": ev["writer_done"], "s2mm_last_beat": ev["s2mm_last_beat"], "limb_done": ev["limb_done"],
        "sampler_end": ev["sampler_end"], "reader_end": ev["reader_end"],
        "gauss_end": ev["gauss_samples_end"], "tern_end": ev["tern_bytes_end"], "store0_start": ev["store_start_p0"],
        "stalls": {f"{a}/{b}": v for (a, b), v in sorted(stall.items(), key=lambda x: -x[1])},
        "phase_cycles": {n: dict(v) for n, v in phase_time.items()},
    }
    return res

CENTRAL = dict(chacha=112, d_tern=6, d_gauss=5, d_uload=3, stage=561, pointwise=1039, d_store=6, wipe=1027,
               d_writer=2, d_msg=26, ovh=2, loop_ovh=2, batch_ovh=1, key_loop=10, kat_cmp=18,
               in_latency=60, out_fifo=64, s2mm_gap=0)
II_ONLY = dict(chacha=88, d_tern=2, d_gauss=1, d_uload=1, stage=512, pointwise=1024, d_store=1, wipe=1024,
               d_writer=1, d_msg=1, ovh=0, loop_ovh=0, batch_ovh=0, key_loop=0, kat_cmp=0,
               in_latency=0, out_fifo=64, s2mm_gap=0)
LOW = dict(CENTRAL, chacha=104, stage=555, pointwise=1035, d_tern=4, d_gauss=4, d_store=4)
HIGH = dict(CENTRAL, chacha=120, stage=567, pointwise=1043, d_tern=8, d_gauss=7, d_store=8)
S2MM80 = dict(CENTRAL, s2mm_gap=4)          # S2MM sustaining 16 of every 20 cycles (80 %)

def timeline(r, f):
    """steady-state timeline of ciphertext f relative to the end of u_load(f)"""
    u = r["u_load_end"][f]
    return {"store_p1_start": r["store1_start"][f] - u, "store_p1_end": r["store1_end"][f] - u,
            "next_u_load_end": (r["u_load_end"][f + 1] - u) if f + 1 < len(r["u_load_end"]) else None}

if __name__ == "__main__":
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    out = {}
    for name, p in [("II_ONLY", II_ONLY), ("CENTRAL", CENTRAL), ("LOW", LOW), ("HIGH", HIGH), ("CENTRAL_S2MM80", S2MM80)]:
        r = run(k, p)
        end = r["s2mm_last_beat"][0]
        out[name] = r
        print(f"{name:15s} k={k} steady period {r['steady_period_cycles']:.0f} cyc = {r['steady_period_cycles']/200:.2f} us/ct | "
              f"last output beat {end} cyc = {end/200:.1f} us | limb done {r['limb_done'][0]/200:.1f} us | "
              f"MM2S last beat ~{r['message_coeffs_end'][0]/200:.1f} us")
        top = list(r["stalls"].items())[:6]
        print("   stalls:", ", ".join(f"{a}={b}" for a, b in top))
        print("   periods:", r["period_cycles"])
        print("   store1 start-to-end:", [e - s for s, e in zip(r["store1_start"], r["store1_end"])])
        print("   timeline f=3:", timeline(r, 3) if k > 4 else None)
    if len(sys.argv) > 2:   # optional full dump, e.g. python -I v8b_sched_model.py 8 <scratch dir>/v8b_sched_model_k8.json
        with open(sys.argv[2], "w", encoding="utf-8", newline="\n") as fh:
            json.dump(out, fh, indent=1, default=str)
        print("wrote", sys.argv[2])
    print("k sweep (CENTRAL / II_ONLY), last output beat in us:")
    for kk in range(1, 9):
        a, b = run(kk, CENTRAL), run(kk, II_ONLY)
        print(f"  k={kk}: central {a['s2mm_last_beat'][0]/200:8.1f} us   ii-only {b['s2mm_last_beat'][0]/200:8.1f} us   limb_done {a['limb_done'][0]/200:8.1f} us")
