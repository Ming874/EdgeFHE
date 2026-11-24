"""Prints the steady-state timeline of one ciphertext (k = 8, f = 3) from the schedule model; writes no file.
usage: python -I timeline.py   (from any folder)"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from v8b_sched_model import run, CENTRAL, II_ONLY
for name, P in (("CENTRAL", CENTRAL), ("II_ONLY", II_ONLY)):
    r = run(8, P)
    U = r["u_load_end"]; f = 3; u = U[f]
    ge = r["gauss_end"]            # 128 per ct
    te = r["tern_end"]             # 2 per ct
    e0_done = ge[f * 128 + 63] - u; e1_done = ge[f * 128 + 127] - u
    print(name, "U_0 =", U[0], "cyc =", U[0] / 200, "us; steady P =", r["steady_period_cycles"])
    print("  f=3 rel. to end of u_load(f): store_p0 start", r["store0_start"][f] - u, "| store_p1 start", r["store1_start"][f] - u,
          "end", r["store1_end"][f] - u, "| e0 done", e0_done, "| e1 done", e1_done,
          "| tern batch ends of f+1:", [x - u for x in te[2 * (f + 1): 2 * (f + 1) + 2]], "| next u_load end", U[f + 1] - u)
    last = r["s2mm_last_beat"][0]; st1 = r["store1_end"][-1]
    print("  last store_p1 end", st1, "| last output beat", last, "| tail", last - st1, "cyc =", (last - st1) / 200, "us",
          "| writer done", r["writer_done"][0], "| limb done", r["limb_done"][0], "| sampler end", r["sampler_end"][0], "| reader end", r["reader_end"][0])
    print("  frame = U0 + 7P + (P - uload_part) + tail  ->", U[0] + 7 * r["steady_period_cycles"], "+", st1 - U[-1], "+", last - st1, "=", last)
    print("  fixed per frame vs 8*P:", last - 8 * r["steady_period_cycles"], "cyc =", (last - 8 * r["steady_period_cycles"]) / 200, "us")
