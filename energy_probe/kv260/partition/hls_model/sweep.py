"""Sensitivity sweep of the schedule model (k = 8) over the chacha8 call and the NTT/INTT stage call latency.
usage: python -I sweep.py   (from any folder; rewrites sweep_k8.json in this folder, the input of gen_outputs.py)"""
import os, sys, json
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from v8b_sched_model import run, CENTRAL, II_ONLY
k = 8
print("chacha stage | period cyc  us/ct | frame us | limiter (stall cycles/ct: limb u_load, limb store_p1, sampler tern_bytes)")
rows = []
for stage in (551, 556, 561, 566, 571):
    for chacha in (96, 104, 108, 112, 116, 120, 124):
        r = run(k, dict(CENTRAL, chacha=chacha, stage=stage))
        st = r["stalls"]
        su, sp1, stb = st.get("limb/u_load", 0) / k, st.get("limb/store_p1", 0) / k, st.get("sampler/tern_bytes", 0) / k
        lim = "sampler" if sp1 > 0 and stb == 0 else ("limb" if stb > 0 else "both")
        end = r["s2mm_last_beat"][0]
        rows.append(dict(stage=stage, chacha=chacha, period=r["steady_period_cycles"], frame_us=end / 200, limiter=lim))
        print(f"{chacha:6d} {stage:5d} | {r['steady_period_cycles']:9.0f} {r['steady_period_cycles']/200:7.2f} | {end/200:8.1f} | {lim:7s} ({su:6.0f}, {sp1:6.0f}, {stb:6.0f})")
with open(os.path.join(HERE, "sweep_k8.json"), "w", newline="\n") as fh:
    json.dump(rows, fh, indent=1)
r = run(k, CENTRAL)
print("CENTRAL phase cycles per ct:")
for proc, ph in r["phase_cycles"].items():
    print(" ", proc, {a: round(b / k, 1) for a, b in sorted(ph.items(), key=lambda x: -x[1])})
print("CENTRAL stalls per ct:", {a: round(b / k, 1) for a, b in r["stalls"].items()})
r1 = run(k, II_ONLY)
print("II_ONLY phase cycles per ct:")
for proc, ph in r1["phase_cycles"].items():
    print(" ", proc, {a: round(b / k, 1) for a, b in sorted(ph.items(), key=lambda x: -x[1])})
