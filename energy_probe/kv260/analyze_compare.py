import json, os, re, sys, statistics as st
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
d = sys.argv[1]
log = json.load(open(os.path.join(d, "runs.json"), encoding="utf-8"))
rows = [list(map(float, l.split(","))) for l in open(os.path.join(d, "probe.csv")).read().splitlines()[1:]]
def mean_p(a, b):
    ps = [r[4] / 1000 for r in rows if a <= r[0] <= b]
    return st.mean(ps), len(ps)
idle = {}
for w in log["idle"]:
    p, n = mean_p(w["start"] + 2, w["end"] - 1)
    idle[w["version"]] = p
    print(f"idle {w['version']:12s} {p:.3f} W  (n={n}, CPU {w['cpu']*100:.1f}%)")
esp = [re.findall(r"result (\S+) records=(\d+) E_J=([\d.]+) rec_per_J=([\d.]+)", e["line"])[0] for e in log["esp_results"]]
out = []
print(f"\n{'version':9s} rep  sec    records   rec/s    P_run  P_idle  rec/J(gross) rec/J(net) ESP32 rec/J  CPU")
for i, r in enumerate(log["runs"]):
    sec = float(r["parsed"]["seconds"]); a = r["t_begin"]; b = a + sec
    p, n = mean_p(a + 0.3, b - 0.3)
    e = p * sec; pi = idle[r.get("baseline", r["version"])]
    g = r["records"] / e; net = r["records"] / ((p - pi) * sec)
    k = sum(1 for q in log["runs"][:i] if q["version"] == r["version"])      # k-th run of this version
    ej = [x for x in esp if x[0] == r["version"]]
    espv = float(ej[k][3]) if k < len(ej) and int(ej[k][1]) == r["records"] else float("nan")
    out.append({"version": r["version"], "rep": r["rep"], "sec": sec, "records": r["records"], "rec_s": r["records"] / sec,
                "P_run": p, "P_idle": pi, "E_J": e, "rpj_gross": g, "rpj_net": net, "esp_rpj": espv, "cpu": r["cpu"],
                "ms_frame": float(r["parsed"].get("mean_ioctl_ms") or r["parsed"].get("mean_frame_ms")
                                  or float(r["parsed"].get("mean_ct_ms", "nan")) * float(r["parsed"].get("k", 8)))})
    print(f"{r['version']:9s} {r['rep']:3d} {sec:6.2f} {r['records']:9d} {r['records']/sec:8.0f} {p:7.3f} {pi:7.3f} {g:12.0f} {net:10.0f} {espv:11.0f} {r['cpu']*100:5.1f}%")
print()
summ = {}
for v in dict.fromkeys(o["version"] for o in out):
    xs = [o for o in out if o["version"] == v]
    f = lambda k: (st.mean(o[k] for o in xs), st.stdev(o[k] for o in xs) if len(xs) > 1 else 0)
    summ[v] = {k: f(k) for k in ("rec_s", "P_run", "rpj_gross", "rpj_net", "esp_rpj", "cpu", "ms_frame")}
    s = summ[v]
    print(f"{v:9s} n={len(xs)}  rec/s {s['rec_s'][0]:.0f}±{s['rec_s'][1]:.0f}  P_run {s['P_run'][0]:.3f}±{s['P_run'][1]:.3f} W  "
          f"rec/J gross {s['rpj_gross'][0]:.0f}±{s['rpj_gross'][1]:.0f}  net {s['rpj_net'][0]:.0f}±{s['rpj_net'][1]:.0f}  "
          f"ESP32 {s['esp_rpj'][0]:.0f}  CPU {s['cpu'][0]*100:.1f}%  frame {s['ms_frame'][0]:.3f} ms")
vs = list(summ)
a = vs[0]
for b in vs[1:]:
    print(f"\nratio {b}/{a}: gross {summ[b]['rpj_gross'][0]/summ[a]['rpj_gross'][0]:.2f}x  net {summ[b]['rpj_net'][0]/summ[a]['rpj_net'][0]:.2f}x  "
          f"throughput {summ[b]['rec_s'][0]/summ[a]['rec_s'][0]:.2f}x  power {summ[b]['P_run'][0]/summ[a]['P_run'][0]:.3f}x")
json.dump({"runs": out, "idle_W": idle}, open(os.path.join(d, "summary.json"), "w"), indent=1)
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(11, 3.8)); t0 = rows[0][0]
ax.plot([r[0] - t0 for r in rows], [r[4] / 1000 for r in rows], lw=0.7, color="#1f5fbf")
col = {"V8b": "#e67e22", "edgefhe2": "#2e7d32", "OpenFHE1": "#7b1fa2", "OpenFHE4": "#c2185b"}
for w in log["idle"]:
    ax.axvspan(w["start"] - t0, w["end"] - t0, color="#9e9e9e", alpha=0.15)
for r in log["runs"]:
    ax.axvspan(r["t_begin"] - t0, r["t_begin"] + float(r["parsed"]["seconds"]) - t0, color=col.get(r["version"], "#888"), alpha=0.2)
ax.set_xlabel("time (s)"); ax.set_ylabel("12 V input power (W)"); ax.grid(alpha=0.3)
ax.set_title("grey = idle, orange = V8b, green = edgefhe2, purple = OpenFHE x1, pink = OpenFHE x4")
fig.tight_layout(); fig.savefig(os.path.join(d, "power_trace.png"), dpi=110); print("plot saved")
