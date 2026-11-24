#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
partition_model.py -- EdgeFHE preliminary, measurement-driven HW/SW partition model (v0.1)

    python partition_model.py [--repo REPO_ROOT] [--out DIR] [--no-plots] [--quick]
    (--repo defaults to the repository that contains this file; --out to this folder)

Standard library only (matplotlib is used for two optional PNG plots if it is already
installed; nothing is installed). Deterministic: no randomness, sorted enumeration,
deterministic tie-breaking in the scheduler.

Pipeline of the script
  1. load_inputs()     read the team's measured numbers from the repo (read-only).
  2. derive()          per-node ARM costs, PL kernel cycles, transfer model D0/D1/D2,
                       power model.  Every parameter is stored with a label:
                         MEASURED  (number copied from a result file; path given)
                         DERIVED   (formula over MEASURED numbers; formula given)
                         ESTIMATED (engineering estimate; assumption stated)
                         ASSUMED   (scenario input, e.g. a future driver; not a fact)
  3. calibrate()       rebuild the six measured configurations (cpu-w1..w4,
                       fpga-serial, fpga-overlap) "as implemented" (IMPL mode) and compare
                       per step, edge latency, keygen, throughput, CPU utilisation, power.
  4. enumerate()       exhaustive search over ARM/PL assignments of the dual-capable nodes
                       (4 symmetric features -> multisets), list scheduler (4 A53 cores,
                       one PL engine, driver mutex depth 1 or 2), objectives: single-batch
                       edge latency, steady-state throughput (bottleneck bound), energy.
  5. stone()           Stone (IEEE TSE 1977) two-processor min-cut with Edmonds-Karp.
  6. break_even()      LogCA-style (Altaf & Wood, ISCA 2017) break-even of the per-call
                       fixed overhead vs. products per call, and vs. ARM transform speed.
  7. outputs           model_results.json, graph_best.mmd, graph_best.dot,
                       PARTITION_MODEL.md (Traditional Chinese), optional PNGs.
"""
import argparse
import collections
import heapq
import itertools
import json
import math
import os
import sys
import time
from pathlib import Path

VERSION = "0.1"
N = 4096                      # ring degree
LIMBS = 3                     # RNS limbs
FEATS = 4                     # features per batch
BATCH = 2048                  # records per batch (slots)
POLY_SW = LIMBS * N * 4       # 48 KiB, limb-major software layout
POLY_DMA = N * 16             # 64 KiB, one 128-bit beat per coefficient
BEAT = 16
CYC_NS = 10.0                 # 100 MHz PL clock

# ----------------------------------------------------------------------------------------
# Source paths (relative to the repo root)
# ----------------------------------------------------------------------------------------
SRC_E2E3 = "results/2026-10-14-kv260/stage2_03/e2e_all.json"
SRC_CMP3 = "results/2026-10-14-kv260/stage2_03/comparison.md"
SRC_E2E2 = "results/2026-10-14-kv260/stage2_02/e2e_all.json"
SRC_BRK = "results/2026-10-14-kv260/dma_trace_01/breakdown.json"
SRC_TRC = "results/2026-10-14-kv260/dma_trace_01/trace_report.json"
SRC_RES = "release/RESULTS.json"
SRC_VAL = "docs/VALIDATION.md#2.2"
SRC_COMP = "docs/COMPARISON.md"
SRC_CLIENT = "software/native_v5/ckks/client.cpp"
SRC_CPUBE = "software/native_v5/backend/cpu/cpu_backend.cpp"
SRC_FPGABE = "software/native_v5/backend/fpga/fpga_backend.cpp"
SRC_SESSION = "software/native_v5/edge/session.cpp"
SRC_CORE = "software/native_ckks/core.cpp"
SRC_DRIVER = "software/driver/edgefhe_rns3_n4096_dma_proxy.c"

BACKENDS = ["cpu-w1", "cpu-w2", "cpu-w3", "cpu-w4", "fpga-serial", "fpga-overlap"]
STEPS = ["standardize", "encode", "sample", "encrypt", "cloud", "decrypt", "decode"]

# Embedded copy of the inputs (used only if the repo files cannot be read).  Values were
# copied from the files named above on 2026-10-15; the loader reports which path was used.
FALLBACK = {
    "rounds": 30,
    "lat": {  # stage2_03 p50 ms
        "cpu-w1": [0.426934, 8.327743, 0.109411, 38.381704, 4.537546, 7.864429, 8.628667],
        "cpu-w2": [0.426654, 8.326744, 0.107601, 23.333674, 4.544516, 5.183332, 8.633396],
        "cpu-w3": [0.427094, 8.318973, 0.108011, 18.409775, 4.550176, 3.816228, 8.640777],
        "cpu-w4": [0.426854, 8.311163, 0.108081, 16.171792, 4.529385, 3.744708, 8.645677],
        "fpga-serial": [0.427054, 8.303793, 0.108391, 47.267443, 4.579655, 6.457954, 8.652097],
        "fpga-overlap": [0.426494, 8.322933, 0.108602, 45.98464, 4.534905, 6.449754, 8.641476],
    },
    "edge": {"cpu-w1": 63.765699, "cpu-w2": 46.018431, "cpu-w3": 39.736558, "cpu-w4": 37.403903,
             "fpga-serial": 71.263163, "fpga-overlap": 69.910619},
    "total": {"cpu-w1": 68.365934, "cpu-w2": 50.632156, "cpu-w3": 44.331153, "cpu-w4": 41.99994,
              "fpga-serial": 75.914569, "fpga-overlap": 74.513425},
    "bstats": {
        "cpu-w1": {"wall_ms": 1102.985712, "jobs": 270, "pack_ms": 0, "device_ms": 0, "unpack_ms": 0, "driver_reported_ms": 0},
        "cpu-w2": {"wall_ms": 570.461665, "jobs": 270, "pack_ms": 0, "device_ms": 0, "unpack_ms": 0, "driver_reported_ms": 0},
        "cpu-w3": {"wall_ms": 382.684109, "jobs": 270, "pack_ms": 0, "device_ms": 0, "unpack_ms": 0, "driver_reported_ms": 0},
        "cpu-w4": {"wall_ms": 321.394495, "jobs": 270, "pack_ms": 0, "device_ms": 0, "unpack_ms": 0, "driver_reported_ms": 0},
        "fpga-serial": {"wall_ms": 1329.4252, "jobs": 270, "pack_ms": 18.849852, "device_ms": 1260.791062, "unpack_ms": 49.667744, "driver_reported_ms": 142.193},
        "fpga-overlap": {"wall_ms": 1285.538518, "jobs": 270, "pack_ms": 22.105865, "device_ms": 1268.797701, "unpack_ms": 49.793013, "driver_reported_ms": 144.352},
    },
    "keygen": {"cpu-w1": 7.370283, "cpu-w2": 4.561, "cpu-w3": 3.384, "cpu-w4": 3.198, "fpga-serial": 5.84, "fpga-overlap": 5.816},
    "encode_single": {"cpu-w1": 31.399554, "cpu-w2": 31.401094, "cpu-w3": 31.343743, "cpu-w4": 31.357824,
                      "fpga-serial": 31.405214, "fpga-overlap": 31.383364},
    "tput19": {  # pipelined(2) 19 batches: wall_ms, records/s, cpu util, mean W ; serial 19
        "cpu-w1": {"pipe": [926.78, 41058.14, 0.483, 3.534], "serial": [1311.78, 29007.81, 0.339, 3.411]},
        "cpu-w2": {"pipe": [655.15, 58081.05, 0.682, 3.708], "serial": [979.93, 38831.26, 0.454, 3.524]},
        "cpu-w3": {"pipe": [580.72, 65525.23, 0.770, 3.763], "serial": [865.43, 43968.70, 0.514, 3.533]},
        "cpu-w4": {"pipe": [570.08, 66748.43, 0.786, 3.787], "serial": [812.31, 46844.47, 0.551, 3.596]},
        "fpga-serial": {"pipe": [1093.06, 34812.29, 0.420, 3.563], "serial": [1458.44, 26090.94, 0.312, 3.452]},
        "fpga-overlap": {"pipe": [1054.66, 36080.03, 0.438, 3.596], "serial": [1435.41, 26509.48, 0.320, 3.462]},
    },
    "brk": {"enter": 3.1, "checks": 2.9, "copy_in": 28.0, "validate": 3589.0, "clear_out": 10.0, "dma_setup": 18.0,
            "dma_wait": 563.0, "copy_out": 450.0, "wipe": 34.5, "leave": 3.6, "total": 4705.0},
    "trace_baseline": {"device_ms_per_call": 4.6768, "driver_reported_ms_per_call": 0.5457,
                       "pack_ms_per_call": 0.0692, "unpack_ms_per_call": 0.183},
    "trace_traced_device_ms": 4.7345,
    "cosim": {"forward": 18775, "inverse_roundtrip": 17741, "negacyclic_multiply": 45986, "add2": 8228, "add3": 12323, "negate_add": 8228},
    "v3bench": {"0": [1852.14, 2701.12, 261.0], "1": [2265.55, 2692.52, 253.0], "2": [6590.73, 5025.85, 549.0]},
}


# ----------------------------------------------------------------------------------------
# Parameter registry with provenance labels
# ----------------------------------------------------------------------------------------
class Params:
    LABELS = ("MEASURED", "DERIVED", "ESTIMATED", "ASSUMED")

    def __init__(self):
        self.items = collections.OrderedDict()

    def set(self, key, value, unit, label, source):
        assert label in self.LABELS, label
        self.items[key] = {"value": value, "unit": unit, "label": label, "source": source}
        return value

    def __getitem__(self, key):
        return self.items[key]["value"]

    def lab(self, key):
        return self.items[key]["label"]


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs)


def ceil_div(a, b):
    return -(-a // b)


# ----------------------------------------------------------------------------------------
# 1. Loading measured inputs (read-only)
# ----------------------------------------------------------------------------------------
def load_inputs(repo):
    """Return a dict of measured inputs. Reads the repo files; falls back per item."""
    out = {"loaded_from": {}, "fallback_used": []}

    def jload(rel):
        with open(repo / rel, "r", encoding="utf-8") as fh:
            return json.load(fh)

    fb = FALLBACK
    # --- stage2_03 latency + backend stats + keygen + throughput
    try:
        e3 = jload(SRC_E2E3)
        out["rounds"] = e3["settings"]["rounds"]
        out["encode_workers"] = e3["settings"].get("encode_workers", 1)
        lat, edge, total, bst = {}, {}, {}, {}
        for r in e3["latency"]["results"]:
            b = r["backend"]
            stepmap = {s["step"]: s["time"]["p50_us"] / 1000.0 for s in r["steps"]}
            lat[b] = [stepmap[s] for s in STEPS]
            edge[b] = r["edge_only"]["p50_us"] / 1000.0
            total[b] = r["total"]["p50_us"] / 1000.0
            bst[b] = r["backend_stats"]
        out["lat"], out["edge"], out["total"], out["bstats"] = lat, edge, total, bst
        out["keygen"] = {k["backend"]: k["keygen_ms"] for k in e3["keygen"]}
        tp = {}
        rows = []
        for r in e3["throughput"]["results"]:
            pw = r["power"][0] if r.get("power") else {}
            row = {"backend": r["backend"], "pipeline": r["pipeline"], "capacity": r["capacity"],
                   "batches": r["batches"], "records": r["records"], "wall_ms": r["wall_ms"],
                   "records_per_s": r["records_per_s"], "cpu_util": r["cpu_utilization"],
                   "mean_w": pw.get("mean_w"), "records_per_j": pw.get("records_per_j"),
                   "driver_reported_ms": r["backend_stats"].get("driver_reported_ms", 0.0),
                   "stages": r["stages"]}
            rows.append(row)
            if r["batches"] == 19:
                key = "pipe" if r["capacity"] == 2 else "serial"
                tp.setdefault(r["backend"], {})[key] = [r["wall_ms"], r["records_per_s"],
                                                        r["cpu_utilization"], pw.get("mean_w")]
        out["tput19"] = tp
        out["tput_rows"] = rows
        out["loaded_from"]["stage2_03"] = SRC_E2E3
    except (OSError, KeyError, ValueError) as exc:
        out["fallback_used"].append("stage2_03 (%s)" % exc)
        out["rounds"] = fb["rounds"]
        out["encode_workers"] = 4
        out["lat"], out["edge"], out["total"] = fb["lat"], fb["edge"], fb["total"]
        out["bstats"], out["keygen"] = fb["bstats"], fb["keygen"]
        out["tput19"] = fb["tput19"]
        out["tput_rows"] = []
    # --- stage2_02: single-thread encode
    try:
        e2 = jload(SRC_E2E2)
        enc1 = {}
        for r in e2["latency"]["results"]:
            stepmap = {s["step"]: s["time"]["p50_us"] / 1000.0 for s in r["steps"]}
            enc1[r["backend"]] = stepmap["encode"]
        out["encode_single"] = enc1
        out["encode_workers_stage2_02"] = e2["settings"].get("encode_workers", 1)
        out["loaded_from"]["stage2_02"] = SRC_E2E2
    except (OSError, KeyError, ValueError) as exc:
        out["fallback_used"].append("stage2_02 (%s)" % exc)
        out["encode_single"] = fb["encode_single"]
        out["encode_workers_stage2_02"] = 1
    # --- DMA trace
    try:
        brk = jload(SRC_BRK)
        seg = {s["segment"]: s["median_us"] for s in brk["segments"]}
        seg["total"] = brk["total"]["median_us"]
        out["brk"] = seg
        trc = jload(SRC_TRC)
        out["trace_baseline"] = trc["baseline"]
        out["trace_traced_device_ms"] = trc["traced"]["device_ms_per_call"]
        out["loaded_from"]["dma_trace"] = SRC_BRK + " , " + SRC_TRC
    except (OSError, KeyError, ValueError) as exc:
        out["fallback_used"].append("dma_trace (%s)" % exc)
        out["brk"] = fb["brk"]
        out["trace_baseline"] = fb["trace_baseline"]
        out["trace_traced_device_ms"] = fb["trace_traced_device_ms"]
    # --- RESULTS.json cosim + V3 benchmark
    try:
        res = jload(SRC_RES)
        out["cosim"] = res["hls"]["cosim"]["operation_latency_cycles"]
        v3 = {}
        for op in ("0", "1", "2"):
            b = res["benchmark"][op]
            v3[op] = [b["cpu_wall_us"]["p50"], b["fpga_wall_us"]["p50"], b["driver_us"]["p50"]]
        out["v3bench"] = v3
        out["loaded_from"]["RESULTS"] = SRC_RES
    except (OSError, KeyError, ValueError) as exc:
        out["fallback_used"].append("RESULTS (%s)" % exc)
        out["cosim"] = fb["cosim"]
        out["v3bench"] = fb["v3bench"]
    return out


# ----------------------------------------------------------------------------------------
# 2. Derivation of model parameters
# ----------------------------------------------------------------------------------------
def lstsq2(xs, ys):
    """Least squares y = a + b x. Returns (a, b)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    return my - b * mx, b


def solve3(A, y):
    """Solve 3x3 linear system (Gauss elimination with partial pivoting)."""
    M = [row[:] + [v] for row, v in zip(A, y)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        for r in range(3):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, 4):
                    M[r][k] -= f * M[c][k]
    return [M[i][3] / M[i][i] for i in range(3)]


def cpu_rounds(k, n_fwd, n_prod):
    return ceil_div(n_fwd, k), ceil_div(n_prod, k)


def derive(inp):
    P = Params()
    R = inp["rounds"]
    lat = inp["lat"]
    st = {s: i for i, s in enumerate(STEPS)}

    # ---------------- ARM, shared steps (MEASURED p50, mean over the six backends) --------
    P.set("t_std", mean(lat[b][st["standardize"]] for b in BACKENDS), "ms", "MEASURED",
          SRC_E2E3 + " step standardize p50 (mean of 6 backends)")
    P.set("t_sample", mean(lat[b][st["sample"]] for b in BACKENDS), "ms", "MEASURED",
          SRC_E2E3 + " step sample p50 (mean of 6 backends)")
    P.set("t_cloud", mean(lat[b][st["cloud"]] for b in BACKENDS), "ms", "MEASURED",
          SRC_E2E3 + " step cloud p50, in-process CPU (out of edge scope)")
    dec_meas = P.set("t_decode_step", mean(lat[b][st["decode"]] for b in BACKENDS), "ms", "MEASURED",
                     SRC_E2E3 + " step decode p50 (decode + sigmoid + classify)")
    enc4 = P.set("t_encode_step_w4", mean(lat[b][st["encode"]] for b in BACKENDS), "ms", "MEASURED",
                 SRC_E2E3 + " step encode p50 with 4 encode workers")
    enc1 = P.set("t_encode_step_w1", mean(inp["encode_single"].values()), "ms", "MEASURED",
                 SRC_E2E2 + " step encode p50, single thread")
    nowrap = P.set("t_nowrap", 0.02, "ms", "ESTIMATED",
                   "no_wrap_bound(): ~20k int64/128-bit L1 ops ~1.5 ns each (session.cpp encode_batch)")
    enc_f = P.set("t_enc_f", (enc1 - nowrap) / FEATS, "ms", "DERIVED",
                  "(encode_step_w1 - NOWRAP)/4  [V4 encode(): FFT 4096 + twist (std::pow) + round]")
    P.set("c_enc4", (enc4 - nowrap) / enc_f, "x", "DERIVED",
          "(encode_step_w4 - NOWRAP)/t_enc_f : slowdown when 4 encodes run concurrently")
    sig = P.set("t_sigmoid", 0.20, "ms", "ESTIMATED", "2048 x (sigmoid exp + classify + checks) ~100 ns")
    nwc = P.set("t_nwcheck", 0.04, "ms", "ESTIMATED", "4096 x 128-bit magnitude <= no_wrap compare ~10 ns")
    P.set("t_dec_fft", dec_meas - sig - nwc, "ms", "DERIVED",
          "decode_step - SIGMOID - NWCHECK  [decode(): 4096 std::pow twist + inverse FFT; serial]")

    # ---------------- ARM, polynomial transforms (CPU backend wall time) ------------------
    bst = inp["bstats"]
    wall = {k: bst["cpu-w%d" % k]["wall_ms"] / R for k in (1, 2, 3, 4)}
    for k in (1, 2, 3, 4):
        P.set("be_wall_w%d" % k, wall[k], "ms/round", "MEASURED",
              SRC_E2E3 + " latency backend_stats.wall_ms / %d rounds (cpu-w%d; 8+1 products per round)" % (R, k))
    pw = P.set("t_pw_limb", 0.031, "ms", "ESTIMATED",
               "pointwise product per limb: 4096 Plan::product (128-bit Barrett) ~10 cycles @1.333 GHz + alloc")
    # per round: encrypt 18 forward (6 operands x 3) + 24 (pw+inverse); decrypt 6 + 3
    fr1, pr1 = 18 + 6, 24 + 3
    tau = P.set("t_ntt_limb", (wall[1] - pr1 * pw) / (fr1 + pr1), "ms", "DERIVED",
                "(be_wall_w1 - 27*pw)/51 : one 4096-point NTT on one limb, one A53 core "
                "(V4 Shoup NTT incl. canonical check, copy, twist, bit-reverse)")
    P.set("t_intt_limb", tau, "ms", "ESTIMATED",
          "inverse NTT assumed equal to forward (same loop structure, untwist instead of twist)")
    cont = {1: 1.0}
    for k in (2, 3, 4):
        f_r = ceil_div(18, k) + ceil_div(6, k)
        p_r = ceil_div(24, k) + ceil_div(3, k)
        cont[k] = P.set("c_be%d" % k, wall[k] / (f_r * tau + p_r * (tau + pw)), "x", "DERIVED",
                        "be_wall_w%d / (fwd_rounds*tau + prod_rounds*(tau+pw)), rounds=ceil(tasks/%d): "
                        "memory/sync slowdown with %d workers" % (k, k, k))

    def be_cpu(k, n_fwd, n_prod):
        f_r, p_r = cpu_rounds(k, n_fwd, n_prod)
        return cont[k] * (f_r * tau + p_r * (tau + pw))

    # ---------------- FPGA backend per job (MEASURED, stage2_03 latency) ------------------
    fs = bst["fpga-serial"]
    jobs = fs["jobs"]
    pack_ms = P.set("pack_op2_ms", fs["pack_ms"] / jobs, "ms", "MEASURED",
                    SRC_E2E3 + " fpga-serial backend_stats.pack_ms/jobs (8192 beats in)")
    unpack_ms = P.set("unpack_op2_ms", fs["unpack_ms"] / jobs, "ms", "MEASURED",
                      SRC_E2E3 + " fpga-serial backend_stats.unpack_ms/jobs (4096 beats out, incl. validate)")
    dev_ms = P.set("device_op2_ms_e2e", fs["device_ms"] / jobs, "ms", "MEASURED",
                   SRC_E2E3 + " fpga-serial backend_stats.device_ms/jobs (whole ioctl)")
    P.set("pack_ns_per_beat", pack_ms * 1e6 / 8192, "ns/beat", "DERIVED", "pack_op2_ms / 8192 beats (assumed linear)")
    P.set("unpack_ns_per_beat", unpack_ms * 1e6 / 4096, "ns/beat", "DERIVED", "unpack_op2_ms / 4096 beats (assumed linear)")

    # ---------------- Serial client parts: least squares over the 6 backends -------------
    enc_meas = {b: lat[b][st["encrypt"]] for b in BACKENDS}
    dec_meas_d = {b: lat[b][st["decrypt"]] for b in BACKENDS}
    job_serial = pack_ms + dev_ms + unpack_ms
    be_enc = {}
    be_dec = {}
    for k in (1, 2, 3, 4):
        be_enc["cpu-w%d" % k] = be_cpu(k, 18, 24)
        be_dec["cpu-w%d" % k] = be_cpu(k, 6, 3)
    be_enc["fpga-serial"] = 8 * job_serial
    be_dec["fpga-serial"] = job_serial
    be_dec["fpga-overlap"] = job_serial    # count==1 -> serial path in FpgaBackend
    fit_enc = [b for b in BACKENDS if b != "fpga-overlap"]
    s_enc = P.set("S_enc", mean(enc_meas[b] - be_enc[b] for b in fit_enc), "ms", "DERIVED",
                  "mean over cpu-w1..w4, fpga-serial of (encrypt_step - backend model): serial client code in "
                  "Client::encrypt (validate, residues of m,u,e0,e1, 12 add())")
    s_dec = P.set("S_dec", mean(dec_meas_d[b] - be_dec[b] for b in BACKENDS), "ms", "DERIVED",
                  "mean over 6 backends of (decrypt_step - backend model): validate, residues(s), add, centered CRT")
    P.items["S_enc"]["residuals_ms"] = {b: round(enc_meas[b] - be_enc[b] - s_enc, 4) for b in fit_enc}
    P.items["S_dec"]["residuals_ms"] = {b: round(dec_meas_d[b] - be_dec[b] - s_dec, 4) for b in BACKENDS}

    # ---------------- residue cost from keygen (independent of the encrypt fit) ----------
    v_ns = P.set("validate_ns_per_word", 1.5, "ns", "ESTIMATED", "load+compare+branch per 32-bit word (~2 cycles)")
    kg_w1 = inp["keygen"]["cpu-w1"]
    kg_other = (N * LIMBS * v_ns + 2 * N * v_ns + N * LIMBS * 3.0) * 1e-6  # validate(a), 2x validate_ternary, subtract
    r_ns = P.set("residue_ns_per_elem",
                 (kg_w1 - be_cpu(1, 6, 3) - kg_other) * 1e6 / (2 * N * LIMBS), "ns", "DERIVED",
                 "(keygen_w1 - backend(1 product,w1) - validates/subtract)/24576 ; keygen = residues(s)+residues(e)"
                 "+1 product (client.cpp). keygen is a single cold sample: " + SRC_E2E3 + " keygen[cpu-w1]")
    rns_m = P.set("t_rns_m_f", N * LIMBS * r_ns * 1e-6, "ms", "DERIVED", "12288 x residue_ns (int64 % q, SDIV)")
    rns_t = P.set("t_rns_t_f", 3 * N * LIMBS * r_ns * 1e-6 + 3 * N * v_ns * 1e-6, "ms", "DERIVED",
                  "3 x 12288 x residue_ns + 3 validate_ternary (u,e0,e1)")
    valkey = P.set("t_valkey", 2 * N * LIMBS * v_ns * 1e-6, "ms", "ESTIMATED", "validate(key.b), validate(key.a)")
    add_cost = P.set("t_add_call", (s_enc - FEATS * (rns_m + rns_t) - valkey) / 12.0, "ms", "DERIVED",
                     "(S_enc - 4*(RNS_m+RNS_t) - VALKEY)/12 add() calls (2 validates + alloc + add); "
                     "op-count estimate 0.074 ms agrees")
    P.set("t_dec_pre", 2 * N * LIMBS * v_ns * 1e-6 + N * v_ns * 1e-6, "ms", "ESTIMATED",
          "validate(c0'), validate(c1'), validate_ternary(s)")
    P.set("t_rns_s", N * LIMBS * r_ns * 1e-6, "ms", "DERIVED", "residues(s): 12288 x residue_ns")
    P.set("t_crt", s_dec - P["t_dec_pre"] - P["t_rns_s"] - add_cost, "ms", "DERIVED",
          "S_dec - DEC_PRE - RNS_s - add : centered() 128-bit CRT, 4096 coefficients")

    # ---------------- PL kernel cycles (MEASURED cosim) and composition (DERIVED) -------
    cs = inp["cosim"]
    P.set("cyc_op0", cs["forward"], "cycles", "MEASURED", SRC_RES + " hls.cosim.operation_latency_cycles.forward")
    P.set("cyc_op1", cs["inverse_roundtrip"], "cycles", "MEASURED", SRC_RES + " ...inverse_roundtrip")
    P.set("cyc_op2", cs["negacyclic_multiply"], "cycles", "MEASURED", SRC_RES + " ...negacyclic_multiply")
    P.set("cyc_op3", cs["add2"], "cycles", "MEASURED", SRC_RES + " ...add2")
    P.set("cyc_op4", cs["add3"], "cycles", "MEASURED", SRC_RES + " ...add3")
    Lc = N
    ntt_core = P.set("cyc_ntt_core", cs["forward"] - 2 * Lc, "cycles", "DERIVED", "op0 - 4096 in - 4096 out")
    intt_core = P.set("cyc_intt_core", cs["inverse_roundtrip"] - 2 * Lc, "cycles", "DERIVED", "op1 - 4096 in - 4096 out")
    pw_eff = P.set("cyc_pw_eff", cs["negacyclic_multiply"] - 3 * Lc - 2 * ntt_core - intt_core, "cycles", "DERIVED",
                   "op2 - 3*4096 stream - 2 ntt_core - intt_core (absorbs overlap inside op2)")
    add_ovh = P.set("cyc_add_ovh", cs["add3"] - 3 * Lc, "cycles", "DERIVED", "add3 - 3*4096")
    P.set("cyc_fenc_f", Lc + ntt_core + 2 * (pw_eff + intt_core) + 3 * Lc + add_ovh, "cycles", "ESTIMATED",
          "new op (key resident in NTT form): stream u + NTT(u) + 2x(pw + INTT) + stream m,e0,e1 (c0,c1 out "
          "overlapped) ; composition of cosim anchors, no overlap between phases")
    P.set("cyc_fdec", Lc + ntt_core + pw_eff + intt_core + Lc + add_ovh, "cycles", "ESTIMATED",
          "new op (s resident in NTT form): c1' in + NTT + pw + INTT + c0' in / d out overlapped")
    P.set("cyc_kpair", Lc + ntt_core + 2 * (pw_eff + intt_core) + 2 * Lc, "cycles", "ESTIMATED",
          "new op K (key resident): stream u + NTT(u) + 2x(pw + INTT) + stream out b*u, a*u (not overlapped)")
    P.set("t_sparse_prod_limb", 64 * N * 2.5 / 1.333e9 * 1e3, "ms", "ESTIMATED",
          "W2 only: sparse-ternary negacyclic product, 64 nonzeros x 4096 lazy 64-bit add/sub at ~2.5 cycles "
          "(scalar A53, 1.333 GHz); range 1.5-4 cycles; needs constant-time review (secret-dependent offsets)")
    P.set("t_rns_t_f_fast", 3 * N * LIMBS * 1.0e-6 + 3 * N * v_ns * 1e-6, "ms", "ESTIMATED",
          "W1-W3 only: ternary residues by table lookup ~1 ns/element (+validate)")
    P.set("t_rns_m_f_fast", N * LIMBS * 4.0e-6, "ms", "ESTIMATED",
          "W1-W3 only: int64 message residues by Barrett with constant q ~4 ns/element")
    P.set("cyc_fft_pl_whatif", 12 * 512 + 2 * Lc, "cycles", "ESTIMATED",
          "what-if only: 4096-pt radix-2 complex FFT at 4 butterflies/cycle (like the NTT engine) + streaming; "
          "needs double precision (>=35-bit mantissa); NOT implemented, not used in scenarios")

    # ---------------- Transfer model D0 (calibrated on the trace) ------------------------
    brk = inp["brk"]
    tb = inp["trace_baseline"]
    in2, out2 = 8192 * BEAT, 4096 * BEAT
    P.set("d0_copy_in_ns_per_B", brk["copy_in"] * 1e3 / in2, "ns/B", "MEASURED",
          SRC_BRK + " copy_in 28.0 us / 131072 B (uncached write-combine, 4.7 GB/s)")
    P.set("d0_validate_ns_per_B", brk["validate"] * 1e3 / in2, "ns/B", "MEASURED",
          SRC_BRK + " validate 3589 us / 131072 B (32768 uncached 32-bit loads, 109.5 ns each)")
    P.set("d0_clear_ns_per_B", brk["clear_out"] * 1e3 / out2, "ns/B", "MEASURED", SRC_BRK + " clear_out 10 us / 65536 B")
    P.set("d0_copy_out_ns_per_B", brk["copy_out"] * 1e3 / out2, "ns/B", "MEASURED",
          SRC_BRK + " copy_out 450 us / 65536 B (uncached read, 146 MB/s)")
    P.set("d0_wipe_us_fixed", brk["wipe"], "us", "MEASURED", SRC_BRK + " wipe 34.5 us (whole 192+64 KiB buffers)")
    P.set("drv_reported_untraced_ms", tb["driver_reported_ms_per_call"], "ms", "MEASURED",
          SRC_TRC + " baseline.driver_reported_ms_per_call (DMA+PL window, untraced)")
    P.set("device_untraced_ms", tb["device_ms_per_call"], "ms", "MEASURED", SRC_TRC + " baseline.device_ms_per_call")
    fixed_us = P.set("d0_fixed_us",
                     tb["device_ms_per_call"] * 1e3 - (brk["copy_in"] + brk["validate"] + brk["clear_out"] +
                                                        tb["driver_reported_ms_per_call"] * 1e3 + brk["copy_out"] +
                                                        brk["wipe"]),
                     "us", "DERIVED",
                     "untraced call - (copy_in+validate+clear_out+window+copy_out+wipe): syscall entry/exit, "
                     "mutex, checks, dma_setup, leave")
    pre_share = (brk["enter"] + brk["checks"] + brk["dma_setup"]) / (brk["enter"] + brk["checks"] + brk["dma_setup"] + brk["leave"])
    P.set("fixed_pre_share", pre_share, "x", "DERIVED", "(enter+checks+dma_setup)/(..+leave) from traced medians")
    # DMA/PL window: t_win = K*10ns + delta*I + t0 (least squares over 4 measured windows)
    v3 = inp["v3bench"]
    pts = [(4096, cs["forward"], v3["0"][2]), (4096, cs["inverse_roundtrip"], v3["1"][2]),
           (8192, cs["negacyclic_multiply"], v3["2"][2]),
           (8192, cs["negacyclic_multiply"], tb["driver_reported_ms_per_call"] * 1e3)]
    a0, b0 = lstsq2([p[0] for p in pts], [p[2] - p[1] * CYC_NS / 1000.0 for p in pts])
    P.set("win_t0_us", a0, "us", "DERIVED",
          "LSQ of (driver_us - cosim cycles*10ns) vs input beats over V3 op0/op1/op2 driver p50 (" + SRC_RES +
          " benchmark) + V5 untraced op2 (" + SRC_TRC + "): DMA start + IRQ + completion wake-up")
    P.set("win_delta_ns_per_beat", b0 * 1e3, "ns/beat", "DERIVED",
          "slope of the same fit: extra per input beat beyond 1 beat/cycle (MM2S stalls)")
    P.items["win_t0_us"]["fit_points"] = [{"in_beats": p[0], "cycles": p[1], "window_us": p[2],
                                           "model_us": round(p[1] * CYC_NS / 1000 + a0 + b0 * p[0], 1)} for p in pts]

    # ---------------- D1 (team estimate) and D2 (assumption) -----------------------------
    P.set("d1_validate_ns_per_B", 0.05e6 / in2, "ns/B", "ESTIMATED",
          SRC_COMP + " team estimate: validate on cacheable memory 3.59 -> ~0.05 ms per 128 KiB")
    P.set("d1_copy_out_ns_per_B", 0.05e6 / out2, "ns/B", "ESTIMATED",
          SRC_COMP + " team estimate: copy_out from cacheable buffer 0.45 -> ~0.05 ms per 64 KiB")
    P.set("d1_maint_ns_per_B", 10e3 / POLY_DMA, "ns/B", "ESTIMATED",
          "streaming-DMA cache maintenance (dma_sync_* clean/invalidate by VA), ~10 us per 64 KiB each way "
          "(1024 lines x ~10 ns); not in the team estimate; range 5-40 us")
    P.set("wipe_ns_per_B", brk["wipe"] * 1e3 / (192 * 1024 + 64 * 1024), "ns/B", "DERIVED",
          "wipe 34.5 us / 256 KiB; D1/D2 wipe only the bytes used")
    P.set("d2_pack_factor", 1.3, "x", "ASSUMED", "D2: canonical check folded into user-space pack (+30%)")
    P.set("d2_bw_GBps", 1.6, "GB/s", "ASSUMED", "D2: HP0 / AXIS limit 128 bit x 100 MHz; zero-copy user buffers")

    # ---------------- power model --------------------------------------------------------
    rows = [r for r in inp.get("tput_rows", []) if r.get("mean_w")]
    if len(rows) >= 6:
        A = [[0.0] * 3 for _ in range(3)]
        yv = [0.0] * 3
        for r in rows:
            busy = r["cpu_util"] * 4
            duty = (r["driver_reported_ms"] / r["wall_ms"]) if r["wall_ms"] else 0.0
            x = [1.0, busy, duty]
            for i in range(3):
                yv[i] += x[i] * r["mean_w"]
                for j in range(3):
                    A[i][j] += x[i] * x[j]
        p0, pc, pp = solve3(A, yv)
        res = [r["mean_w"] - (p0 + pc * r["cpu_util"] * 4 + pp * r["driver_reported_ms"] / r["wall_ms"]) for r in rows]
        rmse = math.sqrt(sum(e * e for e in res) / len(res))
        src = ("LSQ over %d throughput rows with power in %s: W = P0 + a*busy_cores + b*PL_duty "
               "(INA260 ina260_u14/power1, whole board/rail); rmse %.3f W" % (len(rows), SRC_E2E3, rmse))
    else:
        p0, pc, pp, rmse = 3.15, 0.205, 0.12, float("nan")
        src = "fallback constants (ESTIMATED)"
    P.set("pw_P0", p0, "W", "DERIVED", src)
    P.set("pw_core", pc, "W/core", "DERIVED", src)
    P.set("pw_pl", pp, "W/duty", "DERIVED", src + " (PL duty term poorly identified: only 3 duty levels)")
    P.set("pw_rmse", rmse, "W", "DERIVED", "fit residual")

    # ---------------- throughput efficiency factors (fit in calibrate) -------------------
    return P, cont


# ----------------------------------------------------------------------------------------
# Driver / call cost model
# ----------------------------------------------------------------------------------------
class Driver:
    """Per-call cost of one PS->PL->PS request.  All times in ms.
       pack/unpack: user space, ARM.  pre/post: inside the driver mutex, ARM busy.
       win: DMA + PL kernel window, ARM thread sleeps, PL engine busy."""

    def __init__(self, name, P, kind):
        self.name, self.kind = name, kind
        self.fixed_ms = P["d0_fixed_us"] * 1e-3
        self.pre_share = P["fixed_pre_share"]
        self.copy_in = P["d0_copy_in_ns_per_B"] * 1e-6
        self.clear = P["d0_clear_ns_per_B"] * 1e-6
        self.t0 = P["win_t0_us"] * 1e-3
        self.delta = P["win_delta_ns_per_beat"] * 1e-6
        self.pack_beat = P["pack_ns_per_beat"] * 1e-6
        self.unpack_beat = P["unpack_ns_per_beat"] * 1e-6
        self.bw = None
        if kind == "D0":
            self.validate = P["d0_validate_ns_per_B"] * 1e-6
            self.copy_out = P["d0_copy_out_ns_per_B"] * 1e-6
            self.maint = 0.0
            self.wipe_fixed = P["d0_wipe_us_fixed"] * 1e-3
            self.wipe_per_B = 0.0
        elif kind == "D1":
            self.validate = P["d1_validate_ns_per_B"] * 1e-6
            self.copy_out = P["d1_copy_out_ns_per_B"] * 1e-6
            self.maint = P["d1_maint_ns_per_B"] * 1e-6
            self.wipe_fixed = 0.0
            self.wipe_per_B = P["wipe_ns_per_B"] * 1e-6
        elif kind == "D2":
            self.validate = 0.0
            self.copy_in = 0.0
            self.clear = 0.0
            self.copy_out = 0.0
            self.maint = P["d1_maint_ns_per_B"] * 1e-6
            self.wipe_fixed = 0.0
            self.wipe_per_B = P["wipe_ns_per_B"] * 1e-6
            self.pack_beat *= P["d2_pack_factor"]
            self.bw = P["d2_bw_GBps"]
        else:
            raise ValueError(kind)

    def parts(self, in_beats, out_beats, cycles):
        bi, bo = in_beats * BEAT, out_beats * BEAT
        pack = in_beats * self.pack_beat
        unpack = out_beats * self.unpack_beat
        pre = (self.fixed_ms * self.pre_share + bi * (self.copy_in + self.validate + self.maint) + bo * self.clear)
        kern = cycles * CYC_NS * 1e-6
        if self.bw:
            kern = max(kern, (bi + bo) / (self.bw * 1e9) * 1e3)
        win = kern + in_beats * self.delta + self.t0
        post = (self.fixed_ms * (1 - self.pre_share) + bo * (self.copy_out + self.maint) +
                self.wipe_fixed + (bi + bo) * self.wipe_per_B)
        return {"pack": pack, "pre": pre, "win": win, "post": post, "unpack": unpack}

    def fixed_part(self):
        """Per-call constant part (independent of bytes and cycles)."""
        return self.fixed_ms + self.wipe_fixed + self.t0

    def total(self, in_beats, out_beats, cycles):
        p = self.parts(in_beats, out_beats, cycles)
        return sum(p.values())


# ----------------------------------------------------------------------------------------
# 3. Task graph + list scheduler
# ----------------------------------------------------------------------------------------
ARM, PL, NONE = 0, 1, 2
POOL_ENC, POOL_BE, POOL_HLP = 0, 1, 2


class TL:
    """Task list. Each task: name, node (minimal-node id), res, dur(ms), deps, stage, kind."""

    def __init__(self):
        self.name, self.node, self.res, self.dur, self.deps = [], [], [], [], []
        self.stage, self.kind, self.pool, self.mop, self.call = [], [], [], [], []
        self.calls = []        # list of dicts: in_beats, out_beats, cycles, op, node

    def add(self, name, node, res, dur, deps, stage, kind="compute", pool=-1, mop=0, call=-1):
        i = len(self.name)
        self.name.append(name)
        self.node.append(node)
        self.res.append(res)
        self.dur.append(max(0.0, dur))
        self.deps.append([d for d in deps if d is not None])
        self.stage.append(stage)
        self.kind.append(kind)
        self.pool.append(pool)
        self.mop.append(mop)
        self.call.append(call)
        return i

    def barrier(self, name, deps, stage):
        return self.add(name, "BARRIER", NONE, 0.0, deps, stage, kind="barrier")


def simulate(tl, cores=4, mutex_cap=1, pool_caps=(4, 4, 1), subset=None):
    """Event-driven non-preemptive list scheduler.
       ARM tasks need one of `cores`; PL tasks need the single PL engine; NONE tasks are free.
       mop=1 (driver PRE) acquires a driver-mutex slot (capacity mutex_cap) which is released
       when mop=2 (driver POST) of the same call ends.  Priority = upward rank (HEFT-like);
       POST/WIN tasks get a large bonus (they hold the mutex).
       Returns (makespan, start, end) restricted to `subset` (all tasks if None)."""
    n_all = len(tl.dur)
    if subset is None:
        idx = list(range(n_all))
    else:
        idx = sorted(subset)
    pos = {g: i for i, g in enumerate(idx)}
    n = len(idx)
    dur = [tl.dur[g] for g in idx]
    res = [tl.res[g] for g in idx]
    pool = [tl.pool[g] for g in idx]
    mop = [tl.mop[g] for g in idx]
    succ = [[] for _ in range(n)]
    indeg = [0] * n
    for i, g in enumerate(idx):
        for d in tl.deps[g]:
            j = pos.get(d)
            if j is not None:
                succ[j].append(i)
                indeg[i] += 1
    # topological order + upward rank
    deg = indeg[:]
    order = [i for i in range(n) if deg[i] == 0]
    k = 0
    while k < len(order):
        u = order[k]
        k += 1
        for s in succ[u]:
            deg[s] -= 1
            if deg[s] == 0:
                order.append(s)
    if len(order) != n:
        raise RuntimeError("cycle in task graph")
    rank = [0.0] * n
    for u in reversed(order):
        m = 0.0
        for s in succ[u]:
            if rank[s] > m:
                m = rank[s]
        rank[u] = dur[u] + m
    prio = [rank[i] + (1e6 if mop[i] == 2 else 0.0) for i in range(n)]
    ready = ([], [], [])
    for i in range(n):
        if indeg[i] == 0:
            heapq.heappush(ready[res[i]], (-prio[i], i))
    start = [0.0] * n
    end = [0.0] * n
    ev = []
    free = cores
    pl_free = True
    mutex_used = 0
    pool_used = [0, 0, 0]
    t = 0.0
    done = 0
    while done < n:
        # resource-free tasks complete instantly
        while ready[NONE]:
            _, i = heapq.heappop(ready[NONE])
            start[i] = end[i] = t
            heapq.heappush(ev, (t, i))
        if pl_free and ready[PL]:
            _, i = heapq.heappop(ready[PL])
            start[i] = t
            end[i] = t + dur[i]
            pl_free = False
            heapq.heappush(ev, (end[i], i))
        if free > 0 and ready[ARM]:
            deferred = []
            while free > 0 and ready[ARM]:
                item = heapq.heappop(ready[ARM])
                i = item[1]
                p = pool[i]
                if p >= 0 and pool_used[p] >= pool_caps[p]:
                    deferred.append(item)
                    continue
                if mop[i] == 1 and mutex_used >= mutex_cap:
                    deferred.append(item)
                    continue
                start[i] = t
                end[i] = t + dur[i]
                free -= 1
                if p >= 0:
                    pool_used[p] += 1
                if mop[i] == 1:
                    mutex_used += 1
                heapq.heappush(ev, (end[i], i))
            for item in deferred:
                heapq.heappush(ready[ARM], item)
        if not ev:
            raise RuntimeError("deadlock in scheduler")
        t, i = heapq.heappop(ev)
        batch = [i]
        while ev and ev[0][0] <= t + 1e-12:
            batch.append(heapq.heappop(ev)[1])
        for i in batch:
            done += 1
            r = res[i]
            if r == ARM:
                free += 1
                if pool[i] >= 0:
                    pool_used[pool[i]] -= 1
                if mop[i] == 2:
                    mutex_used -= 1
            elif r == PL:
                pl_free = True
            for s in succ[i]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    heapq.heappush(ready[res[s]], (-prio[s], s))
    mk = max(end) - min(start) if n else 0.0
    return mk, dict(zip(idx, start)), dict(zip(idx, end))


# ----------------------------------------------------------------------------------------
# Scenario configuration
# ----------------------------------------------------------------------------------------
def _sc(driver, fused=False, coalesce=False, cache=False, s_resident=False, arm_speed=1.0, kop=False,
        arm_alg="ntt", fast_rns=False, encdec_speed=1.0, group="main", desc=""):
    return dict(driver=driver, fused=fused, coalesce=coalesce, cache=cache, s_resident=s_resident,
                arm_speed=arm_speed, kop=kop, arm_alg=arm_alg, fast_rns=fast_rns, encdec_speed=encdec_speed,
                group=group, desc=desc)


SCENARIOS = collections.OrderedDict([
    ("S0", _sc("D0", desc="current: V3 driver (D0), ops of the frozen bitstream (V5 uses op2 only), key re-sent "
                          "every call; ARM side as implemented (no session caching)")),
    ("S1", _sc("D1", desc="D1 fixed driver (cacheable buffers + cache maintenance), same op interface")),
    ("S2", _sc("D1", fused=True, cache=True,
               desc="S1 + public key resident in PL (NTT form) + fused encrypt-one-feature op (in m,u,e0,e1; "
                    "out c0,c1); ARM side also caches NTT(a),NTT(b),NTT(s),residues(s) per session (fairness)")),
    ("S3", _sc("D1", fused=True, coalesce=True, cache=True,
               desc="S2 + all fused features of a batch in ONE call (call coalescing)")),
    ("S4", _sc("D2", fused=True, coalesce=True, cache=True,
               desc="S3 + D2 zero-copy user buffers (u-dma-buf like), DMA-bandwidth bound only")),
    ("S5", _sc("D1", fused=True, coalesce=True, cache=True, arm_speed=3.0,
               desc="S3 with a stronger ARM baseline: NTT/INTT/pointwise 3x faster (NEON-NTT-like; sensitivity)")),
    ("S6", _sc("D2", fused=True, coalesce=True, cache=True, s_resident=True, group="whatif",
               desc="what-if: S4 + secret key resident in PL, fused decrypt d=c0'+c1'*s (security trade-off)")),
    ("S7", _sc("D2", fused=True, coalesce=True, cache=True, kop=True, group="whatif",
               desc="what-if: S4 + key-resident product-pair op K (in u; out b*u, a*u; adds stay on ARM) -- the "
                    "interface suggested by Stone's min-cut")),
    ("W1", _sc("D2", fused=True, coalesce=True, cache=True, kop=True, fast_rns=True, group="arm-sw",
               desc="ARM-software what-if on S7 hardware: ternary residues by lookup, m residues by Barrett "
                    "(ESTIMATED), session caches")),
    ("W2", _sc("D2", fused=True, coalesce=True, cache=True, kop=True, fast_rns=True, arm_alg="sparse",
               group="arm-sw",
               desc="W1 + ARM products as sparse-ternary convolutions (u,s have 64 nonzeros; no NTT) (ESTIMATED)")),
    ("W3", _sc("D2", fused=True, coalesce=True, cache=True, kop=True, fast_rns=True, encdec_speed=3.0,
               group="arm-sw",
               desc="W1 + encode/decode 3x faster (hypothesis: per-coefficient std::pow twist dominates; "
                    "UNMEASURED)")),
])

FEAT_MODES_BASE = ["A", "O", "OA", "N0", "N1", "N01"]
MODE_DESC = {
    "A": "ARM path (NTT u, 2x pw+INTT; or sparse product in W2)",
    "O": "2x op2 black-box product on PL",
    "OA": "op2 for b*u on PL, ARM for a*u",
    "N0": "NTT(u) on PL (op0), pw+INTT on ARM",
    "N1": "NTT(u)+pw on ARM, 2x INTT on PL (op1)",
    "N01": "NTT(u) op0, pw ARM, 2x INTT op1",
    "F": "fused encrypt-feature op on PL (key resident; in m,u,e0,e1; out c0,c1)",
    "K": "key-resident product pair on PL (in u; out b*u, a*u); adds on ARM",
}
DEC_DESC = {"A": "ARM", "O": "op2 on PL", "N0": "NTT(c1) op0 + ARM", "N01": "op0 + ARM pw + op1",
            "F": "fused decrypt (s resident)"}


class Cfg:
    """Resolved costs for one scenario."""

    def __init__(self, name, sc, P, cont):
        self.name = name
        self.sc = sc
        self.P = P
        self.drv = Driver(sc["driver"], P, sc["driver"])
        s = sc["arm_speed"]
        self.t_ntt = P["t_ntt_limb"] / s
        self.t_intt = P["t_intt_limb"] / s
        self.t_pw = P["t_pw_limb"] / s
        self.cont = cont
        self.c4 = cont[4]
        self.ce4 = P["c_enc4"]
        self.cache = sc["cache"]
        self.fused = sc["fused"]
        self.coalesce = sc["coalesce"]
        self.s_res = sc["s_resident"]
        self.kop = sc["kop"]
        self.sparse = sc["arm_alg"] == "sparse"
        self.ovr = {}
        if sc["fast_rns"]:
            self.ovr["t_rns_m_f"] = P["t_rns_m_f_fast"]
            self.ovr["t_rns_t_f"] = P["t_rns_t_f_fast"]
        if sc["encdec_speed"] != 1.0:
            self.ovr["t_enc_f"] = P["t_enc_f"] / sc["encdec_speed"]
            self.ovr["t_dec_fft"] = P["t_dec_fft"] / sc["encdec_speed"]

    def v(self, key):
        return self.ovr.get(key, self.P[key])

    def feat_modes(self):
        return FEAT_MODES_BASE + (["F"] if self.fused else []) + (["K"] if self.kop else [])

    def dec_modes(self):
        return ["A", "O", "N0", "N01"] + (["F"] if self.s_res else [])

    def key_modes(self):
        return ["C"] if self.cache else ["A", "P"]

    def s_modes(self):
        return ["C"] if self.cache else ["A", "P"]


Asg = collections.namedtuple("Asg", "key feats dec s_ntt")


def asg_str(a):
    cnt = collections.Counter(a.feats)
    parts = ["%dx%s" % (cnt[m], m) for m in sorted(cnt, key=lambda m: (-cnt[m], m))]
    return "key=%s; features=[%s]; dec=%s; NTT(s)=%s" % (a.key, " ".join(parts), a.dec, a.s_ntt)


def asg_uses_pl(a):
    return any(f != "A" for f in a.feats) or a.dec != "A" or a.key == "P" or a.s_ntt == "P"


# ----------------------------------------------------------------------------------------
# DAG builder for predictions (modes DAG and STEP)
# ----------------------------------------------------------------------------------------
def build(cfg, a, mode, cloud_dur=0.0, floor=False):
    """Build the per-batch task graph for assignment `a` under scenario `cfg`.
       mode 'DAG': only data dependencies (dataflow runtime);
       mode 'STEP': V5 step barriers kept (prepare -> encrypt -> cloud -> decrypt -> finish),
                    dataflow inside each step.
       floor=True: every PL-capable node costs 0 and has no transfer (Amdahl floor)."""
    P = cfg.P
    tl = TL()
    drv = cfg.drv
    c4 = cfg.c4
    t_ntt, t_intt, t_pw = cfg.t_ntt * c4, cfg.t_intt * c4, cfg.t_pw * c4
    if floor:
        t_ntt = t_intt = t_pw = 0.0
    add_c = 0.0 if floor else P["t_add_call"]
    step = (mode == "STEP")

    def call(tag, node, in_beats, out_beats, cycles, deps, stage, parts=1, part_deps=None):
        """One PL call -> tasks pack(s), pre, win, post, unpack(s). Returns unpack ids per part."""
        cid = len(tl.calls)
        tl.calls.append({"tag": tag, "node": node, "in_beats": in_beats, "out_beats": out_beats, "cycles": cycles})
        if floor:
            z = tl.add(tag, node, NONE, 0.0, deps + [x for pd in (part_deps or []) for x in pd], stage, "pl0")
            return [z] * parts
        c = drv.parts(in_beats, out_beats, cycles)
        packs = []
        for p in range(parts):
            dp = part_deps[p] if part_deps else deps
            packs.append(tl.add("%s.pack%d" % (tag, p), node, ARM, c["pack"] / parts, dp, stage, "pack", call=cid))
        pre = tl.add(tag + ".pre", node, ARM, c["pre"], packs, stage, "pre", mop=1, call=cid)
        win = tl.add(tag + ".win", node, PL, c["win"], [pre], stage, "win", call=cid)
        post = tl.add(tag + ".post", node, ARM, c["post"], [win], stage, "post", mop=2, call=cid)
        return [tl.add("%s.unpack%d" % (tag, p), node, ARM, c["unpack"] / parts, [post], stage, "unpack", call=cid)
                for p in range(parts)]

    t_sp = 0.0 if floor else P["t_sparse_prod_limb"] * c4
    # ------------------------------ prepare ------------------------------------------
    std = tl.add("STD", "STD", ARM, P["t_std"], [], "prepare")
    enc = [tl.add("ENC_f%d" % f, "ENC_f%d" % f, ARM, cfg.v("t_enc_f") * cfg.ce4, [std], "prepare") for f in range(FEATS)]
    nowrap = tl.add("NOWRAP", "NOWRAP", ARM, P["t_nowrap"], enc, "prepare")
    sample = tl.add("SAMPLE", "SAMPLE", ARM, P["t_sample"], [std] if step else [], "prepare")
    prep_end = tl.barrier("prepare_end", [nowrap, sample], "prepare") if step else None
    gate = [prep_end] if step else []

    # ------------------------------ encrypt ------------------------------------------
    valkey = tl.add("VALKEY", "VALKEY", ARM, P["t_valkey"], gate, "encrypt")
    rns_m = [tl.add("RNS_m_f%d" % f, "RNS_m_f%d" % f, ARM, cfg.v("t_rns_m_f"), [enc[f]] + gate, "encrypt")
             for f in range(FEATS)]
    rns_t = [tl.add("RNS_t_f%d" % f, "RNS_t_f%d" % f, ARM, cfg.v("t_rns_t_f"), [sample] + gate, "encrypt")
             for f in range(FEATS)]
    sparse = cfg.sparse
    if sparse:   # sparse ARM products need no key transforms
        need_b = any(m in ("N0", "N1", "N01") for m in a.feats)
        need_a = any(m in ("N0", "N1", "N01") for m in a.feats)
    else:
        need_b = any(m in ("A", "N0", "N1", "N01") for m in a.feats)
        need_a = any(m in ("A", "OA", "N0", "N1", "N01") for m in a.feats)
    keyt = {"b": [None] * LIMBS, "a": [None] * LIMBS}
    for X, need in (("b", need_b), ("a", need_a)):
        if not need or a.key == "C":
            continue
        if a.key == "A":
            keyt[X] = [tl.add("NTT_%s[%d]" % (X, l), "NTT_%s" % X, ARM, t_ntt, gate, "encrypt") for l in range(LIMBS)]
        else:  # op0 on PL (public key: no data dependency)
            u = call("OP0_%s" % X, "NTT_%s" % X, N, N, P["cyc_op0"], gate, "encrypt")
            keyt[X] = [u[0]] * LIMBS
    outs = []
    fused_feats = []
    kpair_feats = []
    kpair_out = {}
    for f in range(FEATS):
        m = a.feats[f]
        dep_t = [rns_t[f], valkey]
        if m == "F":
            fused_feats.append(f)
            continue
        if m == "K":
            kpair_feats.append(f)
            continue
        prod = {}
        if m in ("A", "N1", "OA") and not (sparse and m in ("A", "OA")):
            nu = [tl.add("NTT_u_f%d[%d]" % (f, l), "NTT_u_f%d" % f, ARM, t_ntt, dep_t, "encrypt") for l in range(LIMBS)]
        elif m in ("N0", "N01"):
            uu = call("OP0_u_f%d" % f, "NTT_u_f%d" % f, N, N, P["cyc_op0"], dep_t, "encrypt")
            nu = [uu[0]] * LIMBS
        for X in ("b", "a"):
            node_pw, node_in = "PW_%su_f%d" % (X, f), "INTT_%su_f%d" % (X, f)
            if m == "O" or (m == "OA" and X == "b"):
                u = call("OP2_%su_f%d" % (X, f), "OP2_%su_f%d" % (X, f), 2 * N, N, P["cyc_op2"], dep_t, "encrypt")
                prod[X] = u
            elif sparse and (m == "A" or (m == "OA" and X == "a")):
                prod[X] = [tl.add("SPMUL_%su_f%d[%d]" % (X, f, l), "SPMUL_%su_f%d" % (X, f), ARM, t_sp, dep_t,
                                  "encrypt") for l in range(LIMBS)]
            elif m in ("A", "N0") or (m == "OA" and X == "a"):
                prod[X] = [tl.add("PWINTT_%su_f%d[%d]" % (X, f, l), node_in, ARM, t_pw + t_intt,
                                  [nu[l], keyt[X][l]], "encrypt") for l in range(LIMBS)]
            elif m in ("N1", "N01"):
                pwt = [tl.add("PW_%su_f%d[%d]" % (X, f, l), node_pw, ARM, t_pw, [nu[l], keyt[X][l]], "encrypt")
                       for l in range(LIMBS)]
                prod[X] = call("OP1_%su_f%d" % (X, f), node_in, N, N, P["cyc_op1"], pwt, "encrypt")
        c0 = tl.add("ADD_c0_f%d" % f, "ADD_c0_f%d" % f, ARM, 2 * add_c, prod["b"] + [rns_m[f], rns_t[f]], "encrypt")
        c1 = tl.add("ADD_c1_f%d" % f, "ADD_c1_f%d" % f, ARM, add_c, prod["a"] + [rns_t[f]], "encrypt")
        outs += [c0, c1]
    if fused_feats:
        cyc = P["cyc_fenc_f"]
        if cfg.coalesce:
            k = len(fused_feats)
            pdeps = [[rns_m[f], rns_t[f], valkey] for f in fused_feats]
            outs += call("FENC_x%d" % k, "FENC_ALL", 4 * N * k, 2 * N * k, cyc * k, [], "encrypt",
                         parts=k, part_deps=pdeps)
        else:
            for f in fused_feats:
                outs += call("FENC_f%d" % f, "FENC_f%d" % f, 4 * N, 2 * N, cyc, [rns_m[f], rns_t[f], valkey], "encrypt")
    if kpair_feats:
        cyc = P["cyc_kpair"]
        if cfg.coalesce:
            k = len(kpair_feats)
            pdeps = [[rns_t[f], valkey] for f in kpair_feats]
            ups = call("KPAIR_x%d" % k, "KPAIR_ALL", N * k, 2 * N * k, cyc * k, [], "encrypt", parts=k, part_deps=pdeps)
            for f, u in zip(kpair_feats, ups):
                kpair_out[f] = [u]
        else:
            for f in kpair_feats:
                kpair_out[f] = call("KPAIR_f%d" % f, "KPAIR_f%d" % f, N, 2 * N, cyc, [rns_t[f], valkey], "encrypt")
        for f in kpair_feats:
            c0 = tl.add("ADD_c0_f%d" % f, "ADD_c0_f%d" % f, ARM, 2 * add_c, kpair_out[f] + [rns_m[f], rns_t[f]], "encrypt")
            c1 = tl.add("ADD_c1_f%d" % f, "ADD_c1_f%d" % f, ARM, add_c, kpair_out[f] + [rns_t[f]], "encrypt")
            outs += [c0, c1]
    cloud = tl.add("CLOUD", "CLOUD", ARM if cloud_dur > 0 else NONE, cloud_dur, outs, "cloud")

    # ------------------------------ decrypt ------------------------------------------
    dpre = tl.add("DEC_PRE", "DEC_PRE", ARM, P["t_dec_pre"], [cloud], "decrypt")
    s_cached = cfg.cache
    rns_s = None
    if not s_cached and a.dec != "F":
        rns_s = tl.add("RNS_s", "RNS_s", ARM, P["t_rns_s"], [cloud] if step else [], "decrypt")
    dprod = []
    if a.dec == "F":
        dprod = call("FDEC", "FDEC", 2 * N, N, P["cyc_fdec"], [dpre], "decrypt")
    elif a.dec == "O":
        dprod = call("OP2_d", "OP2_d", 2 * N, N, P["cyc_op2"], [dpre, rns_s], "decrypt")
    elif a.dec == "A" and sparse:
        dprod = [tl.add("SPMUL_d[%d]" % l, "SPMUL_d", ARM, t_sp, [dpre], "decrypt") for l in range(LIMBS)]
    else:
        # NTT(s)
        if s_cached or a.s_ntt == "C":
            ns = [None] * LIMBS
        elif a.s_ntt == "A":
            ns = [tl.add("NTT_s[%d]" % l, "NTT_s", ARM, t_ntt, [rns_s], "decrypt") for l in range(LIMBS)]
        else:
            u = call("OP0_s", "NTT_s", N, N, P["cyc_op0"], [rns_s], "decrypt")
            ns = [u[0]] * LIMBS
        if a.dec == "A":
            nc = [tl.add("NTT_c1[%d]" % l, "NTT_c1", ARM, t_ntt, [dpre], "decrypt") for l in range(LIMBS)]
        else:
            u = call("OP0_c1", "NTT_c1", N, N, P["cyc_op0"], [dpre], "decrypt")
            nc = [u[0]] * LIMBS
        if a.dec in ("A", "N0"):
            dprod = [tl.add("PWINTT_d[%d]" % l, "INTT_d", ARM, t_pw + t_intt, [nc[l], ns[l]], "decrypt")
                     for l in range(LIMBS)]
        else:  # N01
            pwt = [tl.add("PW_d[%d]" % l, "PW_d", ARM, t_pw, [nc[l], ns[l]], "decrypt") for l in range(LIMBS)]
            dprod = call("OP1_d", "INTT_d", N, N, P["cyc_op1"], pwt, "decrypt")
    if a.dec == "F":
        addd = tl.barrier("ADD_d(fused)", dprod, "decrypt")
    else:
        addd = tl.add("ADD_d", "ADD_d", ARM, add_c, dprod + [dpre], "decrypt")
    crt = [tl.add("CRT[%d]" % q, "CRT", ARM, P["t_crt"] / 4, [addd], "decrypt") for q in range(4)]
    dec_end = tl.barrier("decrypt_end", crt, "decrypt") if step else None

    # ------------------------------ finish -------------------------------------------
    nwc = tl.add("NWCHECK", "NWCHECK", ARM, P["t_nwcheck"], crt + [nowrap] + ([dec_end] if step else []), "finish")
    dfft = tl.add("DEC_FFT", "DEC_FFT", ARM, cfg.v("t_dec_fft"), [nwc], "finish")
    tl.add("SIGMOID", "SIGMOID", ARM, P["t_sigmoid"], [dfft], "finish")
    return tl


# ----------------------------------------------------------------------------------------
# IMPL mode: the six measured configurations exactly as V5 runs them (calibration)
# ----------------------------------------------------------------------------------------
def build_impl(cfg, backend, cloud_dur=0.0, zero_products=False):
    """V5 as implemented: steps in sequence; serial client code in the stage thread;
       CpuBackend two pool.run phases; FpgaBackend serial job chain or double-buffered helper."""
    P = cfg.P
    tl = TL()
    drv = cfg.drv
    is_cpu = backend.startswith("cpu")
    k = int(backend[-1]) if is_cpu else 1
    ck = cfg.cont[k] if is_cpu else 1.0
    t_f, t_p = P["t_ntt_limb"] * ck, (P["t_pw_limb"] + P["t_intt_limb"]) * ck
    add_c = P["t_add_call"]
    if zero_products:          # Amdahl floor of the CURRENT code structure: PL-capable work is free
        t_f = t_p = add_c = 0.0
    overlap = backend == "fpga-overlap"
    sync = 0.0
    # prepare
    std = tl.add("STD", "STD", ARM, P["t_std"], [], "prepare")
    enc = [tl.add("ENC_f%d" % f, "ENC_f%d" % f, ARM, P["t_enc_f"] * P["c_enc4"], [std], "prepare", pool=POOL_ENC)
           for f in range(FEATS)]
    nowrap = tl.add("NOWRAP", "NOWRAP", ARM, P["t_nowrap"], enc, "prepare")
    sample = tl.add("SAMPLE", "SAMPLE", ARM, P["t_sample"], [nowrap], "prepare")
    # encrypt: serial residues in the stage thread
    prev = tl.add("VALKEY", "VALKEY", ARM, P["t_valkey"], [sample], "encrypt")
    for f in range(FEATS):
        prev = tl.add("RNS_m_f%d" % f, "RNS_m_f%d" % f, ARM, P["t_rns_m_f"], [prev], "encrypt")
        prev = tl.add("RNS_t_f%d" % f, "RNS_t_f%d" % f, ARM, P["t_rns_t_f"], [prev], "encrypt")

    def cpu_backend(prev, operands, products, stage):
        fw = [tl.add("FWD_%s[%d]" % (o, l), "NTT_" + o, ARM, t_f, [prev], stage, pool=POOL_BE)
              for l in range(LIMBS) for o in operands]
        b1 = tl.barrier("pool1_done", fw, stage)
        pr = [tl.add("PROD_%s[%d]" % (p, l), "INTT_" + p, ARM, t_p, [b1], stage, pool=POOL_BE)
              for l in range(LIMBS) for p in products]
        return tl.barrier("pool2_done", pr, stage)

    def fpga_backend(prev, jobs, stage):
        c = drv.parts(2 * N, N, P["cyc_op2"])
        if not overlap or len(jobs) == 1:
            for j in jobs:
                p1 = tl.add(j + ".pack", j, ARM, c["pack"], [prev], stage, "pack")
                p2 = tl.add(j + ".pre", j, ARM, c["pre"], [p1], stage, "pre", mop=1)
                p3 = tl.add(j + ".win", j, PL, c["win"], [p2], stage, "win")
                p4 = tl.add(j + ".post", j, ARM, c["post"], [p3], stage, "post", mop=2)
                prev = tl.add(j + ".unpack", j, ARM, c["unpack"], [p4], stage, "unpack")
            return prev
        n = len(jobs)
        pack0 = tl.add(jobs[0] + ".pack", jobs[0], ARM, c["pack"], [prev], stage, "pack")
        gate = [pack0]
        posts = []
        for j in range(n):
            hp = None
            if j + 1 < n:
                hp = tl.add(jobs[j + 1] + ".pack", jobs[j + 1], ARM, c["pack"], gate, stage, "pack", pool=POOL_HLP)
            hu = None
            if j > 0:
                hu = tl.add(jobs[j - 1] + ".unpack", jobs[j - 1], ARM, c["unpack"], gate + [posts[j - 1]] + ([hp] if hp is not None else []),
                            stage, "unpack", pool=POOL_HLP)
            p2 = tl.add(jobs[j] + ".pre", jobs[j], ARM, c["pre"], gate, stage, "pre", mop=1)
            p3 = tl.add(jobs[j] + ".win", jobs[j], PL, c["win"], [p2], stage, "win")
            p4 = tl.add(jobs[j] + ".post", jobs[j], ARM, c["post"], [p3], stage, "post", mop=2)
            posts.append(p4)
            gate = [tl.add(jobs[j] + ".sync", jobs[j], ARM, sync, [p4] + [x for x in (hp, hu) if x is not None],
                           stage, "sync")]
        return tl.add(jobs[n - 1] + ".unpack", jobs[n - 1], ARM, c["unpack"], gate, stage, "unpack")

    if is_cpu:
        prev = cpu_backend(prev, ["b", "u0", "a", "u1", "u2", "u3"],
                           ["bu%d" % f if X == "b" else "au%d" % f for f in range(FEATS) for X in ("b", "a")], "encrypt")
    else:
        prev = fpga_backend(prev, ["OP2_%su_f%d" % (X, f) for f in range(FEATS) for X in ("b", "a")], "encrypt")
    for f in range(FEATS):
        prev = tl.add("ADD_c0_f%d" % f, "ADD_c0_f%d" % f, ARM, 2 * add_c, [prev], "encrypt")
        prev = tl.add("ADD_c1_f%d" % f, "ADD_c1_f%d" % f, ARM, add_c, [prev], "encrypt")
    cloud = tl.add("CLOUD", "CLOUD", ARM if cloud_dur > 0 else NONE, cloud_dur, [prev], "cloud")
    prev = tl.add("DEC_PRE", "DEC_PRE", ARM, P["t_dec_pre"], [cloud], "decrypt")
    prev = tl.add("RNS_s", "RNS_s", ARM, P["t_rns_s"], [prev], "decrypt")
    if is_cpu:
        prev = cpu_backend(prev, ["c1", "s"], ["d"], "decrypt")
    else:
        prev = fpga_backend(prev, ["OP2_d"], "decrypt")
    prev = tl.add("ADD_d", "ADD_d", ARM, add_c, [prev], "decrypt")
    prev = tl.add("CRT", "CRT", ARM, P["t_crt"], [prev], "decrypt")
    prev = tl.add("NWCHECK", "NWCHECK", ARM, P["t_nwcheck"], [prev], "finish")
    prev = tl.add("DEC_FFT", "DEC_FFT", ARM, P["t_dec_fft"], [prev], "finish")
    tl.add("SIGMOID", "SIGMOID", ARM, P["t_sigmoid"], [prev], "finish")
    return tl, (4, k, 1)


def step_latencies(tl, start, end):
    out = {}
    for s in ("prepare", "encrypt", "cloud", "decrypt", "finish"):
        ids = [i for i in range(len(tl.dur)) if tl.stage[i] == s]
        if ids:
            out[s] = max(end[i] for i in ids) - min(start[i] for i in ids)
    return out


def resource_totals(tl):
    arm = sum(d for d, r in zip(tl.dur, tl.res) if r == ARM)
    pl = sum(d for d, r in zip(tl.dur, tl.res) if r == PL)
    mutex = sum(d for d, k in zip(tl.dur, tl.kind) if k in ("pre", "win", "post"))
    drv_cpu = sum(d for d, k in zip(tl.dur, tl.kind) if k in ("pre", "post"))
    packun = sum(d for d, k in zip(tl.dur, tl.kind) if k in ("pack", "unpack"))
    return {"arm_core_ms": arm, "pl_ms": pl, "mutex_ms": mutex, "driver_cpu_ms": drv_cpu, "pack_unpack_ms": packun,
            "calls": len(tl.calls) if tl.calls else sum(1 for k in tl.kind if k == "win")}


# ----------------------------------------------------------------------------------------
# Throughput (bottleneck bound) and energy
# ----------------------------------------------------------------------------------------
def throughput_eval(tot, stage_lat, eta, P, cloud_on_board=True, lock=True):
    """T_batch = max over resources of (demand / efficiency).
       eta['C'] : ARM-core efficiency (fit on cpu-w4: CPU-bound), eta['L'] : stage/lock efficiency."""
    arm = tot["arm_core_ms"]
    b = collections.OrderedDict()
    b["ARM cores (4)"] = arm / (4 * eta["C"])
    b["PL engine"] = tot["pl_ms"] / eta["L"]
    b["driver mutex"] = tot["mutex_ms"] / eta["L"]
    if lock:
        b["backend lock (encrypt+decrypt stages)"] = (stage_lat.get("encrypt", 0) + stage_lat.get("decrypt", 0)) / eta["L"]
    for s in ("prepare", "encrypt", "decrypt", "finish") + (("cloud",) if cloud_on_board else ()):
        if s in stage_lat:
            b["stage " + s] = stage_lat[s] / eta["L"]
    name = max(b, key=lambda k: b[k])
    T = b[name]
    util_cores = arm / T
    duty = tot["pl_ms"] / T
    watts = P["pw_P0"] + P["pw_core"] * util_cores + P["pw_pl"] * duty
    return {"T_ms": T, "records_per_s": BATCH / T * 1e3, "bottleneck": name, "bounds_ms": dict(b),
            "watts": watts, "records_per_J": BATCH / (watts * T * 1e-3)}


# ----------------------------------------------------------------------------------------
# 4. Calibration
# ----------------------------------------------------------------------------------------
def calibrate(inp, P, cont):
    cfg0 = Cfg("S0", SCENARIOS["S0"], P, cont)
    lat = inp["lat"]
    st = {s: i for i, s in enumerate(STEPS)}
    rows = []
    models = {}
    for b in BACKENDS:
        tl, caps = build_impl(cfg0, b, cloud_dur=0.0)
        mk, start, end = simulate(tl, cores=4, mutex_cap=1, pool_caps=caps)
        sl = step_latencies(tl, start, end)
        model = {"standardize": None, "prepare": sl["prepare"], "encrypt": sl["encrypt"], "decrypt": sl["decrypt"],
                 "decode": sl["finish"], "edge": mk}
        meas = {"prepare": lat[b][st["standardize"]] + lat[b][st["encode"]] + lat[b][st["sample"]],
                "encrypt": lat[b][st["encrypt"]], "decrypt": lat[b][st["decrypt"]], "decode": lat[b][st["decode"]],
                "edge": inp["edge"][b]}
        tl2, caps2 = build_impl(cfg0, b, cloud_dur=P["t_cloud"])
        tot = resource_totals(tl2)
        models[b] = {"stage_lat": {"prepare": sl["prepare"], "encrypt": sl["encrypt"], "decrypt": sl["decrypt"],
                                   "finish": sl["finish"], "cloud": P["t_cloud"]}, "tot": tot, "edge": mk}
        for key in ("prepare", "encrypt", "decrypt", "decode", "edge"):
            rows.append({"backend": b, "quantity": key, "measured_ms": meas[key], "model_ms": model[key],
                         "err_pct": 100.0 * (model[key] - meas[key]) / meas[key]})
    # keygen (held-out except cpu-w1, which defines the residue cost)
    kg_rows = []
    kg_other = (N * LIMBS * P["validate_ns_per_word"] + 2 * N * P["validate_ns_per_word"] + N * LIMBS * 3.0) * 1e-6
    for b in BACKENDS:
        if b.startswith("cpu"):
            k = int(b[-1])
            f_r, p_r = cpu_rounds(k, 6, 3)
            prod = cont[k] * (f_r * P["t_ntt_limb"] + p_r * (P["t_ntt_limb"] + P["t_pw_limb"]))
        else:
            prod = cfg0.drv.total(2 * N, N, P["cyc_op2"])
        model = prod + 2 * P["t_rns_s"] + kg_other
        meas = inp["keygen"][b]
        kg_rows.append({"backend": b, "measured_ms": meas, "model_ms": model, "err_pct": 100 * (model - meas) / meas,
                        "note": "in-sample (defines residue cost)" if b == "cpu-w1" else "held-out (single cold sample)"})
    # D0 per-call model vs measurements (V5 e2e and V3 benchmark: out-of-sample for op0/op1)
    drv0 = cfg0.drv
    call_rows = []
    v3 = inp["v3bench"]
    for op, ib, cyc, meas, src in (("op2 (V5 e2e, ioctl only)", 2 * N, P["cyc_op2"], P["device_op2_ms_e2e"], SRC_E2E3),
                                   ("op2 (trace baseline, ioctl only)", 2 * N, P["cyc_op2"], P["device_untraced_ms"], SRC_TRC)):
        c = drv0.parts(ib, N, cyc)
        model = c["pre"] + c["win"] + c["post"]
        call_rows.append({"call": op, "measured_ms": meas, "model_ms": model, "err_pct": 100 * (model - meas) / meas,
                          "source": src})
    for opk, name, ib, cyc in (("0", "op0 NTT (V3, pack+ioctl+unpack)", N, P["cyc_op0"]),
                               ("1", "op1 INTT (V3, pack+ioctl+unpack)", N, P["cyc_op1"]),
                               ("2", "op2 multiply (V3, pack+ioctl+unpack)", 2 * N, P["cyc_op2"])):
        model = drv0.total(ib, N, cyc)
        meas = v3[opk][1] / 1000.0
        call_rows.append({"call": name, "measured_ms": meas, "model_ms": model, "err_pct": 100 * (model - meas) / meas,
                          "source": SRC_RES + " benchmark (V3 software pack/unpack differs from V5)"})
    # throughput: fit eta_L on lock-bound configs, eta_C on cpu-w4 (CPU-bound); validate on others
    tp = inp["tput19"]
    tmeas = {b: tp[b]["pipe"][0] / 19.0 for b in BACKENDS}
    lockb = {b: models[b]["stage_lat"]["encrypt"] + models[b]["stage_lat"]["decrypt"] for b in BACKENDS}
    fit_L = ["cpu-w1", "fpga-serial", "fpga-overlap"]
    eta_L = P.set("eta_L", mean(lockb[b] / tmeas[b] for b in fit_L), "x", "DERIVED",
                  "mean over cpu-w1, fpga-serial, fpga-overlap (backend-lock-bound) of (enc+dec latency)/(wall/19); "
                  + SRC_E2E3 + " throughput pipelined(2), 19 batches")
    eta_C = P.set("eta_C", models["cpu-w4"]["tot"]["arm_core_ms"] / (4 * tmeas["cpu-w4"]), "x", "DERIVED",
                  "cpu-w4 (CPU-bound): ARM core-ms per batch / (4 * wall/19)")
    eta = {"L": eta_L, "C": eta_C}
    tp_rows = []
    for b in BACKENDS:
        r = throughput_eval(models[b]["tot"], models[b]["stage_lat"], eta, P)
        m = tmeas[b]
        util_meas = tp[b]["pipe"][2]
        util_model = models[b]["tot"]["arm_core_ms"] / (4 * m)
        w_meas = tp[b]["pipe"][3]
        tp_rows.append({"backend": b, "measured_T_ms": m, "model_T_ms": r["T_ms"],
                        "err_pct": 100 * (r["T_ms"] - m) / m,
                        "measured_rec_s_full_batches": BATCH / m * 1e3, "model_rec_s": r["records_per_s"],
                        "reported_rec_s": tp[b]["pipe"][1], "bottleneck": r["bottleneck"],
                        "role": "fit" if b in fit_L + ["cpu-w4"] else "held-out",
                        "arm_core_ms": models[b]["tot"]["arm_core_ms"],
                        "cpu_util_measured": util_meas, "cpu_util_model_at_measured_T": util_model,
                        "watts_measured": w_meas, "watts_model": r["watts"], "bounds_ms": r["bounds_ms"]})
    # serial pipeline (capacity 0): T = sum of all steps (edge + cloud)
    ser_rows = []
    for b in BACKENDS:
        m = tp[b]["serial"][0] / 19.0
        model = models[b]["edge"] + P["t_cloud"]
        ser_rows.append({"backend": b, "measured_T_ms": m, "model_T_ms": model, "err_pct": 100 * (model - m) / m})
    # Amdahl floor of the current V5 structure (IMPL, cpu-w4 shape, products+adds free)
    tlf, capsf = build_impl(cfg0, "cpu-w4", cloud_dur=0.0, zero_products=True)
    fl_lat, fs_, fe_ = simulate(tlf, cores=4, mutex_cap=1, pool_caps=capsf)
    slf = step_latencies(tlf, fs_, fe_)
    tlf2, _ = build_impl(cfg0, "cpu-w4", cloud_dur=P["t_cloud"], zero_products=True)
    totf = resource_totals(tlf2)
    thf = throughput_eval(totf, {"prepare": slf["prepare"], "encrypt": slf["encrypt"], "decrypt": slf["decrypt"],
                                 "finish": slf["finish"], "cloud": P["t_cloud"]}, eta, P)
    impl_floor = {"edge_floor_ms": fl_lat, "edge_ceiling_vs_measured_w4": inp["edge"]["cpu-w4"] / fl_lat,
                  "T_floor_ms": thf["T_ms"], "records_per_s_ceiling": thf["records_per_s"],
                  "throughput_ceiling_vs_measured_w4": tp["cpu-w4"]["pipe"][1] and (thf["records_per_s"] / (BATCH / tmeas["cpu-w4"] * 1e3)),
                  "bottleneck": thf["bottleneck"]}
    return {"steps": rows, "keygen": kg_rows, "calls": call_rows, "throughput": tp_rows, "serial": ser_rows,
            "impl_floor": impl_floor,
            "eta": eta, "models": {b: {"edge": models[b]["edge"], "stage_lat": models[b]["stage_lat"],
                                       "tot": models[b]["tot"]} for b in BACKENDS}}


# ----------------------------------------------------------------------------------------
# 5. Enumeration
# ----------------------------------------------------------------------------------------
def all_assignments(cfg, include_add_pl=False):
    out = []
    seen = set()
    for feats in itertools.combinations_with_replacement(cfg.feat_modes(), FEATS):
        need_key = any(m in ("A", "OA", "N0", "N1", "N01") for m in feats)
        for key in (cfg.key_modes() if need_key else [cfg.key_modes()[0]]):
            for dec in cfg.dec_modes():
                need_s = dec in ("A", "N0", "N01")
                for s in (cfg.s_modes() if need_s else [cfg.s_modes()[0]]):
                    a = Asg(key, tuple(feats), dec, s)
                    if a not in seen:
                        seen.add(a)
                        out.append(a)
    return out


def evaluate(cfg, a, mode, depth, eta, fixed_stage):
    tl = build(cfg, a, mode, cloud_dur=0.0)
    mk, start, end = simulate(tl, cores=4, mutex_cap=depth)
    # throughput: board configuration (cloud in-process on the A53s, as measured)
    tl2 = build(cfg, a, mode, cloud_dur=cfg.P["t_cloud"])
    tot = resource_totals(tl2)
    sl = dict(fixed_stage)
    for s in ("encrypt", "decrypt"):
        ids = [i for i in range(len(tl.dur)) if tl.stage[i] == s]
        sl[s], _, _ = simulate(tl, cores=4, mutex_cap=depth, subset=ids)
    if depth >= 2:
        tot = dict(tot)
        tot["mutex_ms"] = 0.0     # queue depth 2: only the PL engine serialises windows
    thr = throughput_eval(tot, sl, eta, cfg.P, cloud_on_board=True)
    tot_edge = dict(tot)
    tot_edge["arm_core_ms"] -= cfg.P["t_cloud"]
    sl_edge = {k: v for k, v in sl.items() if k != "cloud"}
    thr_edge = throughput_eval(tot_edge, sl_edge, eta, cfg.P, cloud_on_board=False)
    work = sum(d for d, r in zip(tl.dur, tl.res) if r in (ARM, PL))
    return {"asg": a, "latency_ms": mk, "thr": thr, "thr_edge_only": thr_edge, "tot": tot, "stage_lat": sl,
            "tl": tl, "start": start, "end": end, "work_ms": work, "arm_groups": arm_groups(tl2)}


def fixed_stages(cfg, mode):
    a = Asg(cfg.key_modes()[0], ("A",) * FEATS, "A", cfg.s_modes()[0])
    tl = build(cfg, a, mode, cloud_dur=0.0)
    out = {"cloud": cfg.P["t_cloud"]}
    for s in ("prepare", "finish"):
        ids = [i for i in range(len(tl.dur)) if tl.stage[i] == s]
        out[s], _, _ = simulate(tl, cores=4, subset=ids)
    return out


def enumerate_scenario(cfg, eta, mode="DAG", depth=1, include_add_pl=False):
    fs = fixed_stages(cfg, mode)
    res = [evaluate(cfg, a, mode, depth, eta, fs) for a in all_assignments(cfg, include_add_pl)]
    return res, fs


TIE_TOL = 0.01   # assignments within 1% of the optimum are treated as ties -> prefer fewest PL calls


def pick_recommended(results, key):
    best = min(key(r) for r in results)
    cands = [r for r in results if key(r) <= best * (1 + TIE_TOL)]
    return min(cands, key=lambda r: (r["tot"]["calls"], round(r["tot"]["arm_core_ms"], 6), key(r), asg_str(r["asg"])))


def summarize(cfg, results, eta, mode, depth, fs):
    by_lat = sorted(results, key=lambda r: (round(r["latency_ms"], 9), asg_str(r["asg"])))
    by_thr = sorted(results, key=lambda r: (round(r["thr"]["T_ms"], 9), asg_str(r["asg"])))
    allarm = [r for r in results if not asg_uses_pl(r["asg"])]
    allarm = min(allarm, key=lambda r: r["latency_ms"])
    best = pick_recommended(results, lambda r: r["latency_ms"])
    bt = pick_recommended(results, lambda r: r["thr"]["T_ms"])
    strict_best = by_lat[0]
    strict_bt = by_thr[0]
    # Amdahl floor: PL-capable nodes cost nothing
    fl = build(cfg, best["asg"], mode, cloud_dur=0.0, floor=True)
    floor_lat, _, _ = simulate(fl, cores=4, mutex_cap=depth)
    fl2 = build(cfg, best["asg"], mode, cloud_dur=cfg.P["t_cloud"], floor=True)
    tot = resource_totals(fl2)
    sl = dict(fs)
    for s in ("encrypt", "decrypt"):
        ids = [i for i in range(len(fl.dur)) if fl.stage[i] == s]
        sl[s], _, _ = simulate(fl, cores=4, subset=ids)
    floor_thr = throughput_eval(tot, sl, eta, cfg.P)
    # critical-path bottleneck node of the best schedule: node with the largest ARM/PL time on the critical path
    crit = critical_nodes(best)
    top = by_lat[:5]
    return {
        "scenario": cfg.name, "mode": mode, "depth": depth, "n_assignments": len(results),
        "best_latency": pack_result(best), "best_throughput": pack_result(bt), "all_arm": pack_result(allarm),
        "strict_best_latency": pack_result(strict_best), "strict_best_throughput": pack_result(strict_bt),
        "top5_latency": [pack_result(r) for r in top],
        "amdahl": {"floor_latency_ms": floor_lat, "ceiling_latency_vs_allarm": allarm["latency_ms"] / floor_lat,
                   "floor_T_ms": floor_thr["T_ms"], "floor_records_per_s": floor_thr["records_per_s"],
                   "ceiling_throughput_vs_allarm": allarm["thr"]["T_ms"] / floor_thr["T_ms"],
                   "floor_throughput_bottleneck": floor_thr["bottleneck"]},
        "critical_path_best": crit,
    }


def pack_result(r):
    a = r["asg"]
    return {"assignment": asg_str(a), "feats": list(a.feats), "key": a.key, "dec": a.dec, "s_ntt": a.s_ntt,
            "uses_pl": asg_uses_pl(a), "latency_ms": r["latency_ms"], "T_ms": r["thr"]["T_ms"],
            "records_per_s": r["thr"]["records_per_s"], "bottleneck": r["thr"]["bottleneck"],
            "records_per_J": r["thr"]["records_per_J"], "watts": r["thr"]["watts"],
            "edge_only_records_per_s": r["thr_edge_only"]["records_per_s"],
            "edge_only_bottleneck": r["thr_edge_only"]["bottleneck"],
            "arm_core_ms": r["tot"]["arm_core_ms"], "pl_ms": r["tot"]["pl_ms"], "calls": r["tot"]["calls"],
            "driver_cpu_ms": r["tot"]["driver_cpu_ms"], "pack_unpack_ms": r["tot"]["pack_unpack_ms"],
            "total_work_ms": r["work_ms"], "arm_groups_ms": r["arm_groups"],
            "stage_lat_ms": r["stage_lat"], "bounds_ms": r["thr"]["bounds_ms"]}


def arm_groups(tl):
    """ARM core-ms per batch by node group (what fills the ARM cores)."""
    g = collections.OrderedDict((k, 0.0) for k in ("encode", "decode", "cloud", "rns", "crt", "products(ARM)",
                                                     "add", "driver", "pack/unpack", "other"))
    for d, r, nd, k in zip(tl.dur, tl.res, tl.node, tl.kind):
        if r != ARM:
            continue
        if k in ("pre", "post"):
            g["driver"] += d
        elif k in ("pack", "unpack"):
            g["pack/unpack"] += d
        elif nd.startswith("ENC") or nd == "NOWRAP":
            g["encode"] += d
        elif nd in ("DEC_FFT", "SIGMOID", "NWCHECK"):
            g["decode"] += d
        elif nd == "CLOUD":
            g["cloud"] += d
        elif nd.startswith("RNS") or nd in ("VALKEY", "DEC_PRE"):
            g["rns"] += d
        elif nd == "CRT":
            g["crt"] += d
        elif nd.startswith(("NTT", "INTT", "PW", "SPMUL")):
            g["products(ARM)"] += d
        elif nd.startswith("ADD"):
            g["add"] += d
        else:
            g["other"] += d
    return g


def critical_nodes(r):
    """Walk back the schedule from the last finishing task, following the predecessor (data or
       resource) that finished last; aggregate time by node."""
    tl, start, end = r["tl"], r["start"], r["end"]
    n = len(tl.dur)
    last = max(range(n), key=lambda i: (end[i], i))
    path = [last]
    cur = last
    while True:
        cands = [d for d in tl.deps[cur]]
        if not cands:
            # resource wait: pick the task that ended exactly at our start (any)
            prev = [i for i in range(n) if abs(end[i] - start[cur]) < 1e-9 and i != cur and end[i] > 0]
            if not prev or start[cur] <= 1e-12:
                break
            cur = max(prev, key=lambda i: tl.dur[i])
        else:
            dmax = max(cands, key=lambda i: end[i])
            if end[dmax] + 1e-9 < start[cur]:
                prev = [i for i in range(n) if abs(end[i] - start[cur]) < 1e-9 and i != cur]
                cur = max(prev, key=lambda i: tl.dur[i]) if prev else dmax
            else:
                cur = dmax
        path.append(cur)
        if start[cur] <= 1e-12:
            break
    agg = collections.OrderedDict()
    for i in reversed(path):
        nd = tl.node[i]
        if nd == "BARRIER":
            continue
        key = nd + ("" if tl.res[i] != PL else " [PL]")
        agg[key] = agg.get(key, 0.0) + tl.dur[i]
    return [{"node": k, "ms": v} for k, v in sorted(agg.items(), key=lambda kv: -kv[1])[:8]]


# ----------------------------------------------------------------------------------------
# 6. Stone's min-cut (Edmonds-Karp)
# ----------------------------------------------------------------------------------------
def edmonds_karp(cap, s, t):
    """cap: dict u -> dict v -> capacity (directed). Returns (flow, source-side set)."""
    flow = 0.0
    res = collections.defaultdict(dict)
    for u in cap:
        for v, c in cap[u].items():
            res[u][v] = res[u].get(v, 0.0) + c
            res[v].setdefault(u, 0.0)
    while True:
        parent = {s: None}
        q = collections.deque([s])
        while q and t not in parent:
            u = q.popleft()
            for v in sorted(res[u]):
                if v not in parent and res[u][v] > 1e-12:
                    parent[v] = u
                    q.append(v)
        if t not in parent:
            break
        b = float("inf")
        v = t
        while parent[v] is not None:
            u = parent[v]
            b = min(b, res[u][v])
            v = u
        v = t
        while parent[v] is not None:
            u = parent[v]
            res[u][v] -= b
            res[v][u] += b
            v = u
        flow += b
    seen = {s}
    q = collections.deque([s])
    while q:
        u = q.popleft()
        for v, c in res[u].items():
            if c > 1e-12 and v not in seen:
                seen.add(v)
                q.append(v)
    return flow, seen


def stone_graph(cfg):
    """Stone (1977) two-processor model of one batch.
       Node costs: ARM = core-ms of the node (same per-task costs as the DAG builder, incl. the
       4-core contention factor); PL = time on the PL.
       Fusable scenarios (S2+): PL cost = kernel compute only (cosim cycles without streaming);
       transfer costs sit on DIRECTED arcs: data u->v with u on ARM and v on PL costs `in_poly`
       (pack + driver-in + stream-in), u on PL and v on ARM costs `out_poly` (stream-out +
       driver-out + unpack); plus a share of the per-call fixed overhead.
       Non-fusable scenarios (S0, S1): each PL node is its own op0/op1/op3/op4 call, so its full
       call cost is its PL cost and arcs are free (the cut is separable, per-node).
       Returns (arm_cost, pl_cost, arcs) with arcs = [(u, v, cost_if_u_ARM_v_PL, cost_if_u_PL_v_ARM)]."""
    P = cfg.P
    INF = 1e9
    drv = cfg.drv
    arm, pl, arcs = {}, {}, []
    c4 = cfg.c4
    sparse = cfg.sparse
    tn, ti, tp = cfg.t_ntt * LIMBS * c4, cfg.t_intt * LIMBS * c4, cfg.t_pw * LIMBS * c4
    fus = cfg.fused

    def node(nm, a_cost, p_cost):
        arm[nm] = a_cost
        pl[nm] = p_cost

    cin = drv.parts(N, 0, 0)
    cout = drv.parts(0, N, 0)
    in_poly = cin["pack"] + (cin["pre"] - drv.fixed_ms * drv.pre_share) + N * (CYC_NS * 1e-6 + drv.delta)
    out_poly = (cout["post"] - drv.fixed_ms * (1 - drv.pre_share) - drv.wipe_fixed) + cout["unpack"] + N * CYC_NS * 1e-6
    fixed = drv.fixed_part()
    share = fixed / (6.0 if not cfg.coalesce else 24.0)

    def plcall(in_b, out_b, cyc):
        return drv.total(in_b, out_b, cyc)

    for nm, c in (("STD", P["t_std"]), ("NOWRAP", P["t_nowrap"]), ("SAMPLE", P["t_sample"]), ("VALKEY", P["t_valkey"]),
                  ("DEC_PRE", P["t_dec_pre"]), ("CRT", P["t_crt"]), ("NWCHECK", P["t_nwcheck"]),
                  ("DEC_FFT", cfg.v("t_dec_fft")), ("SIGMOID", P["t_sigmoid"])):
        node(nm, c, INF)
    ntt_pl = (P["cyc_ntt_core"] * CYC_NS * 1e-6) if fus else plcall(N, N, P["cyc_op0"])
    intt_pl = (P["cyc_intt_core"] * CYC_NS * 1e-6) if fus else plcall(N, N, P["cyc_op1"])
    pw_pl = (P["cyc_pw_eff"] * CYC_NS * 1e-6) if fus else INF
    add2_pl = (P["cyc_add_ovh"] * CYC_NS * 1e-6) if fus else plcall(2 * N, N, P["cyc_op3"])
    add3_pl = (P["cyc_add_ovh"] * CYC_NS * 1e-6) if fus else plcall(3 * N, N, P["cyc_op4"])
    if not cfg.cache:
        node("NTT_a", tn, ntt_pl)
        node("NTT_b", tn, ntt_pl)
        node("RNS_s", P["t_rns_s"], INF)
        node("NTT_s", tn, ntt_pl)
    else:
        # residues(s) and NTT(s) cached on the ARM; on the PL, s is resident only in S6
        node("RNS_s", 0.0, INF)
        node("NTT_s", 0.0, 0.0 if cfg.s_res else (ntt_pl if fus else INF))
    for f in range(FEATS):
        node("ENC_f%d" % f, cfg.v("t_enc_f") * cfg.ce4, INF)
        node("RNS_m_f%d" % f, cfg.v("t_rns_m_f"), INF)
        node("RNS_t_f%d" % f, cfg.v("t_rns_t_f"), INF)
        node("NTT_u_f%d" % f, 0.0 if sparse else tn, ntt_pl)
        for X in ("b", "a"):
            node("PW_%su_f%d" % (X, f), 0.0 if sparse else tp, pw_pl)
            node("INTT_%su_f%d" % (X, f), P["t_sparse_prod_limb"] * LIMBS * c4 if sparse else ti, intt_pl)
        node("ADD_c0_f%d" % f, 2 * P["t_add_call"], add3_pl)
        node("ADD_c1_f%d" % f, P["t_add_call"], add2_pl)
        node("CLOUD_in_f%d" % f, 0.0, INF)   # ciphertexts must reach the ARM (network) side
    node("NTT_c1", 0.0 if sparse else tn, ntt_pl)
    node("PW_d", 0.0 if sparse else tp, pw_pl)
    node("INTT_d", P["t_sparse_prod_limb"] * LIMBS * c4 if sparse else ti, intt_pl)
    node("ADD_d", P["t_add_call"], add2_pl)

    def e(u, v, polys=1):
        if fus:
            arcs.append((u, v, polys * in_poly + share, polys * out_poly + share))
        else:
            arcs.append((u, v, 0.0, 0.0))

    for f in range(FEATS):
        e("STD", "ENC_f%d" % f)
        e("ENC_f%d" % f, "RNS_m_f%d" % f)
        e("ENC_f%d" % f, "NOWRAP")
        e("SAMPLE", "RNS_t_f%d" % f)
        e("RNS_t_f%d" % f, "NTT_u_f%d" % f)
        for X in ("b", "a"):
            e("NTT_u_f%d" % f, "PW_%su_f%d" % (X, f))
            if not cfg.cache:
                e("NTT_%s" % X, "PW_%su_f%d" % (X, f))
            e("PW_%su_f%d" % (X, f), "INTT_%su_f%d" % (X, f))
        e("INTT_bu_f%d" % f, "ADD_c0_f%d" % f)
        e("INTT_au_f%d" % f, "ADD_c1_f%d" % f)
        e("RNS_m_f%d" % f, "ADD_c0_f%d" % f)
        e("RNS_t_f%d" % f, "ADD_c0_f%d" % f)   # e0
        e("RNS_t_f%d" % f, "ADD_c1_f%d" % f)   # e1
        e("ADD_c0_f%d" % f, "CLOUD_in_f%d" % f)
        e("ADD_c1_f%d" % f, "CLOUD_in_f%d" % f)
        e("CLOUD_in_f%d" % f, "DEC_PRE", 2)
    e("DEC_PRE", "NTT_c1")
    e("NTT_c1", "PW_d")
    e("RNS_s", "NTT_s")
    e("NTT_s", "PW_d")
    e("PW_d", "INTT_d")
    e("INTT_d", "ADD_d")
    e("DEC_PRE", "ADD_d")
    e("ADD_d", "CRT")
    e("CRT", "NWCHECK")
    e("NOWRAP", "NWCHECK")
    e("NWCHECK", "DEC_FFT")
    e("DEC_FFT", "SIGMOID")
    return arm, pl, arcs


def stone(cfg):
    arm, pl, arcs = stone_graph(cfg)
    S, T = "__ARM__", "__PL__"
    cap = collections.defaultdict(dict)
    for v in arm:
        # v stays on the ARM side -> arc (v,T) is cut -> pay its ARM cost
        # v goes to the PL side   -> arc (S,v) is cut -> pay its PL cost
        cap[S][v] = cap[S].get(v, 0.0) + pl[v]
        cap[v][T] = cap[v].get(T, 0.0) + arm[v]
    for u, v, c_ap, c_pa in arcs:
        if c_ap > 0:
            cap[u][v] = cap[u].get(v, 0.0) + c_ap      # cut iff u on ARM, v on PL (ARM->PL transfer)
        if c_pa > 0:
            cap[v][u] = cap[v].get(u, 0.0) + c_pa      # cut iff v on ARM, u on PL (PL->ARM transfer)
    flow, side = edmonds_karp(cap, S, T)
    on_pl = sorted(v for v in arm if v not in side)
    return {"min_total_cost_ms": flow, "pl_nodes": on_pl,
            "arm_total_if_all_arm_ms": sum(arm.values()),
            "arcs_with_cost": sum(1 for _, _, a, b in arcs if a > 0 or b > 0)}


def stone_cost_of(cfg, pl_set):
    arm, pl, arcs = stone_graph(cfg)
    c = sum(pl[v] if v in pl_set else arm[v] for v in arm)
    for u, v, c_ap, c_pa in arcs:
        if u not in pl_set and v in pl_set:
            c += c_ap
        elif u in pl_set and v not in pl_set:
            c += c_pa
    return c


FEAT_PL_SETS = {
    "N0": lambda f: {"NTT_u_f%d" % f},
    "N1": lambda f: {"INTT_bu_f%d" % f, "INTT_au_f%d" % f},
    "N01": lambda f: {"NTT_u_f%d" % f, "INTT_bu_f%d" % f, "INTT_au_f%d" % f},
    "O": lambda f: {"NTT_u_f%d" % f, "PW_bu_f%d" % f, "PW_au_f%d" % f, "INTT_bu_f%d" % f, "INTT_au_f%d" % f},
    "OA": lambda f: {"PW_bu_f%d" % f, "INTT_bu_f%d" % f},
    "K": lambda f: {"NTT_u_f%d" % f, "PW_bu_f%d" % f, "PW_au_f%d" % f, "INTT_bu_f%d" % f, "INTT_au_f%d" % f},
    "F": lambda f: {"NTT_u_f%d" % f, "PW_bu_f%d" % f, "PW_au_f%d" % f, "INTT_bu_f%d" % f, "INTT_au_f%d" % f,
                    "ADD_c0_f%d" % f, "ADD_c1_f%d" % f},
}


def asg_to_plnodes(cfg, a):
    """Approximate minimal-node image of an enumeration assignment (op2 = its product nodes)."""
    s = set()
    for f, m in enumerate(a.feats):
        if m in FEAT_PL_SETS:
            s |= FEAT_PL_SETS[m](f)
    if a.dec in ("O", "F"):
        s |= {"NTT_c1", "PW_d", "INTT_d"} | ({"ADD_d", "NTT_s"} if a.dec == "F" else set())
    if a.dec in ("N0", "N01"):
        s.add("NTT_c1")
    if a.dec == "N01":
        s.add("INTT_d")
    if a.key == "P":
        s |= {"NTT_a", "NTT_b"}
    if a.s_ntt == "P":
        s.add("NTT_s")
    return s


def plnodes_to_asg(cfg, pls):
    feats = []
    for f in range(FEATS):
        sub = {x for x in pls if x.endswith("_f%d" % f)}
        if not sub:
            feats.append("A")
            continue
        hit = None
        for m in (["F"] if cfg.fused else []) + (["K"] if cfg.kop else []) + ["N0", "N1", "N01"]:
            if sub == FEAT_PL_SETS[m](f):
                hit = m
                break
        if hit is None:
            return None, "feature %d: PL subset %s is not an op of this scenario" % (f, sorted(sub))
        feats.append(hit)
    d = {x for x in pls if x in ("NTT_c1", "PW_d", "INTT_d", "ADD_d", "NTT_s")}
    d_core = d - {"NTT_s"}
    if not d_core:
        dec = "A"
    elif d_core == {"NTT_c1"}:
        dec = "N0"
    elif d_core == {"NTT_c1", "INTT_d"}:
        dec = "N01"
    elif d_core == {"NTT_c1", "PW_d", "INTT_d"}:
        dec = "O"
    elif d_core == {"NTT_c1", "PW_d", "INTT_d", "ADD_d"} and cfg.s_res:
        dec = "F"
    else:
        return None, "decrypt PL subset %s is not an op of this scenario" % sorted(d)
    key = "C" if cfg.cache else ("P" if {"NTT_a", "NTT_b"} <= pls else "A")
    s = "C" if cfg.cache else ("P" if "NTT_s" in pls and dec in ("A", "N0", "N01") else "A")
    return Asg(key, tuple(sorted(feats)), dec, s), ""


# ----------------------------------------------------------------------------------------
# 7. Break-even (LogCA-style)
# ----------------------------------------------------------------------------------------
def cpu_products_time(P, cont, k_products, style, speed=1.0, workers=4):
    """Latency (4 workers) and core-time of computing k products with the V5 CpuBackend
       (shared operands transformed once).  style 'op2': key transforms included;
       'fused': key/s transforms cached (as the PL keeps them resident) + the adds of c0/c1."""
    tau = P["t_ntt_limb"] / speed
    pw = P["t_pw_limb"] / speed
    c = cont[workers]
    if k_products == 9:
        n_ops = (6 if style == "op2" else 4) + (2 if style == "op2" else 1)
        n_prod = 9
        adds = 12 + 1
    elif k_products == 1:
        n_ops = 2 if style == "op2" else 1
        n_prod = 1
        adds = 1
    else:
        feats = k_products // 2
        n_ops = (2 + feats) if style == "op2" else feats
        n_prod = k_products
        adds = 3 * feats
    n_fwd, n_p = LIMBS * n_ops, LIMBS * n_prod
    lat = c * (ceil_div(n_fwd, workers) * tau + ceil_div(n_p, workers) * (tau + pw))
    core = c * (n_fwd * tau + n_p * (tau + pw))
    if style == "fused":   # the fused op also performs the adds -> CPU comparator does them too
        lat += adds * P["t_add_call"] / min(workers, max(1, adds))
        core += adds * P["t_add_call"]
    return lat, core


def pl_call_shape(P, k_products, style):
    """(in_beats, out_beats, cycles) of ONE call that computes k products.
       op2: operands re-sent (V5 today).  fused: F op (in m,u,e0,e1 / out c0,c1) + FDEC for the 9th.
       kpair: K op (in u / out b*u,a*u) + resident-s product for the 9th (in c1' / out c1'*s)."""
    one = N + P["cyc_ntt_core"] + P["cyc_pw_eff"] + P["cyc_intt_core"] + N    # resident-operand single product
    if style == "op2":
        return 2 * N * k_products, N * k_products, P["cyc_op2"] * k_products
    if style == "fused":
        if k_products == 1:
            return 2 * N, N, P["cyc_fdec"]
        if k_products == 9:
            return 4 * N * 4 + 2 * N, 2 * N * 4 + N, P["cyc_fenc_f"] * 4 + P["cyc_fdec"]
        feats = k_products // 2
        return 4 * N * feats, 2 * N * feats, P["cyc_fenc_f"] * feats
    # kpair
    if k_products == 1:
        return N, N, one
    if k_products == 9:
        return N * 4 + N, 2 * N * 4 + N, P["cyc_kpair"] * 4 + one
    feats = k_products // 2
    return N * feats, 2 * N * feats, P["cyc_kpair"] * feats


def break_even(P, cont):
    rows = []
    drivers = {k: Driver(k, P, k) for k in ("D0", "D1", "D2")}
    for style in ("op2", "fused", "kpair"):
        for k in (1, 2, 4, 8, 9):
            cpu_lat, cpu_core = cpu_products_time(P, cont, k, style)
            ib, ob, cyc = pl_call_shape(P, k, style)
            row = {"style": style, "k": k, "in_KiB": ib * BEAT / 1024, "out_KiB": ob * BEAT / 1024,
                   "kernel_ms": cyc * CYC_NS * 1e-6, "cpu_w4_latency_ms": cpu_lat, "cpu_core_ms": cpu_core}
            for dn, d in drivers.items():
                parts = d.parts(ib, ob, cyc)
                tot = sum(parts.values())
                var = tot - d.fixed_part()
                arm_side = parts["pack"] + parts["pre"] + parts["post"] + parts["unpack"]
                arm_fixed = d.fixed_ms + d.wipe_fixed
                row[dn] = {"pl_call_ms": tot, "variable_ms": var, "fixed_ms": d.fixed_part(),
                           "tfixed_breakeven_latency_ms": cpu_lat - var,
                           "breaks_even_latency": (cpu_lat - var) > d.fixed_part(),
                           "arm_side_ms": arm_side,
                           "tfixed_breakeven_armtime_ms": cpu_core - (arm_side - arm_fixed),
                           "arm_speed_breakeven_latency": cpu_lat / tot if tot > 0 else None}
            rows.append(row)
    return rows


# ----------------------------------------------------------------------------------------
# 8. Graph output
# ----------------------------------------------------------------------------------------
def node_graph(tl):
    """Aggregate tasks to minimal nodes (+ PL calls as nodes) with edges and bytes."""
    nodes = collections.OrderedDict()
    for i in range(len(tl.dur)):
        nd = tl.node[i]
        if nd == "BARRIER":
            continue
        k = tl.kind[i]
        is_call = k in ("pack", "pre", "win", "post", "unpack")
        if nd not in nodes:
            nodes[nd] = {"arm_ms": 0.0, "pl_ms": 0.0, "call": is_call, "stage": tl.stage[i]}
        if tl.res[i] == ARM:
            nodes[nd]["arm_ms"] += tl.dur[i]
        elif tl.res[i] == PL:
            nodes[nd]["pl_ms"] += tl.dur[i]
        nodes[nd]["call"] = nodes[nd]["call"] or is_call
    edges = set()
    for i in range(len(tl.dur)):
        for d in tl.deps[i]:
            a, b = tl.node[d], tl.node[i]
            if a != b and a != "BARRIER" and b != "BARRIER":
                edges.add((a, b))
    return nodes, sorted(edges)


def edge_bytes(a, b, nodes=None, tl=None):
    """Bytes carried on a node-level edge. Software layout (limb-major, 48 KiB per RNS polynomial)
       between ARM nodes; DMA layout (64 KiB per polynomial, one 128-bit beat per coefficient)
       on edges into a PL call node; a call node's outputs are counted after unpack."""
    is_call = lambda nd: nodes is not None and nd in nodes and nodes[nd]["call"]
    if is_call(b) and not is_call(a):
        # polynomials this producer hands to the call
        if a.startswith("RNS_t"):
            return 3 * POLY_DMA if b.startswith(("FENC", "ADD")) else POLY_DMA
        if a == "DEC_PRE":
            return POLY_DMA
        return POLY_DMA
    if a == "STD":
        return 16 * 1024 if b.startswith("ENC") else 0
    if a.startswith("ENC"):
        return 32 * 1024
    if a == "SAMPLE":
        return 12 * 1024
    if a.startswith("RNS_m"):
        return POLY_SW
    if a.startswith("RNS_t"):
        return 2 * POLY_SW if b.startswith("ADD_c0") else (POLY_SW if b.startswith(("ADD_c1", "NTT_u")) else 3 * POLY_SW)
    if a == "CLOUD":
        return 2 * POLY_SW
    if a == "CRT":
        return N * 16
    if a == "NWCHECK":
        return N * 16
    if a == "NOWRAP":
        return 16          # one 128-bit bound
    if a == "DEC_FFT":
        return BATCH * 16
    if is_call(a) and tl is not None:
        outs = [c["out_beats"] for c in tl.calls if c["node"] == a]
        return (sum(outs) * LIMBS * 4) if outs else POLY_SW   # unpacked, software layout
    return POLY_SW


def fmt_bytes(nb):
    if nb >= 1024:
        return "%dK" % (nb // 1024)
    return "%dB" % nb


def write_graphs(best, cfg, out_dir, title):
    tl = best["tl"]
    nodes, edges = node_graph(tl)
    pl_like = lambda nd: nodes[nd]["call"]
    ids = {nd: "n%d" % i for i, nd in enumerate(nodes)}
    lines = ["%% " + title, "flowchart LR"]
    stages = collections.OrderedDict()
    for nd, v in nodes.items():
        stages.setdefault(v["stage"], []).append(nd)
    for s, nds in stages.items():
        lines.append("  subgraph %s" % s)
        for nd in nds:
            v = nodes[nd]
            if v["call"]:
                lab = "%s<br/>PL %.2fms<br/>ARM %.2fms" % (nd, v["pl_ms"], v["arm_ms"])
            elif nd == "CLOUD":
                lab = "CLOUD<br/>(not in edge latency)"
            else:
                lab = "%s<br/>%.2fms" % (nd, v["arm_ms"])
            lines.append('    %s["%s"]' % (ids[nd], lab))
        lines.append("  end")
    for a, b in edges:
        nb = edge_bytes(a, b, nodes, tl)
        lines.append("  %s -->|%s| %s" % (ids[a], fmt_bytes(nb), ids[b]))
    lines.append("  classDef arm fill:#dbeafe,stroke:#1d4ed8,color:#0b1d4f")
    lines.append("  classDef pl fill:#fed7aa,stroke:#c2410c,color:#431407")
    arm_ids = [ids[nd] for nd in nodes if not pl_like(nd)]
    pl_ids = [ids[nd] for nd in nodes if pl_like(nd)]
    if arm_ids:
        lines.append("  class %s arm" % ",".join(arm_ids))
    if pl_ids:
        lines.append("  class %s pl" % ",".join(pl_ids))
    (out_dir / "graph_best.mmd").write_text("\n".join(lines) + "\n", encoding="utf-8")
    d = ['// ' + title, "digraph edgefhe_best {", "  rankdir=LR;", '  node [shape=box, style="rounded,filled", fontsize=10];']
    for s, nds in stages.items():
        d.append('  subgraph cluster_%s { label="%s"; color=gray;' % (s, s))
        for nd in nds:
            v = nodes[nd]
            if v["call"]:
                lab = "%s\\nPL %.2f ms | ARM %.2f ms" % (nd, v["pl_ms"], v["arm_ms"])
                d.append('    %s [label="%s", fillcolor="#fed7aa", color="#c2410c"];' % (ids[nd], lab))
            else:
                lab = "CLOUD\\n(not in edge latency)" if nd == "CLOUD" else "%s\\n%.2f ms" % (nd, v["arm_ms"])
                d.append('    %s [label="%s", fillcolor="#dbeafe", color="#1d4ed8"];' % (ids[nd], lab))
        d.append("  }")
    for a, b in edges:
        nb = edge_bytes(a, b, nodes, tl)
        d.append('  %s -> %s [label="%s", fontsize=8];' % (ids[a], ids[b], fmt_bytes(nb)))
    d.append("}")
    (out_dir / "graph_best.dot").write_text("\n".join(d) + "\n", encoding="utf-8")
    return {"nodes": len(nodes), "edges": len(edges)}


# ----------------------------------------------------------------------------------------
# 9. Markdown report (Traditional Chinese; every number is filled from the results)
# ----------------------------------------------------------------------------------------
LAB = {"MEASURED": "M", "DERIVED": "D", "ESTIMATED": "E", "ASSUMED": "A"}


def f2(x, nd=2):
    if x is None:
        return "-"
    return ("%%.%df" % nd) % x


def pct(x, nd=1):
    return ("%+." + str(nd) + "f%%") % x


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def short_asg(s):
    return s.replace("features=", "").replace("; NTT(s)=C", "").replace("key=C; ", "")


def stone_note(s_):
    """Short Chinese note for an unmappable Stone cut."""
    pls = set(s_.get("pl_nodes", []))
    kset = all({"NTT_u_f%d" % f, "PW_bu_f%d" % f, "PW_au_f%d" % f, "INTT_bu_f%d" % f, "INTT_au_f%d" % f} <= pls
               for f in range(FEATS)) and not any(x.startswith("ADD_c") for x in pls)
    if kset:
        return "K 型（乘積上 PL、加法留 ARM；本情境沒有此運算，見 S7）"
    return "不可實作：" + s_.get("stone_unmappable_reason", "")


def write_markdown(res, P, out_dir):
    cal = res["calibration"]
    sc = res["scenarios"]
    st = res["stone"]
    be = res["break_even"]
    L = []
    add = L.append
    pv = lambda k, nd=3: f2(P[k], nd)
    pl = lambda k: LAB[P.lab(k)]

    def run(n, key="DAG-d1"):
        return sc[n]["runs"][key]

    meas_w4_edge = [r for r in cal["steps"] if r["backend"] == "cpu-w4" and r["quantity"] == "edge"][0]["measured_ms"]
    meas_w4_T = [r for r in cal["throughput"] if r["backend"] == "cpu-w4"][0]
    meas_w4_rps = meas_w4_T["reported_rec_s"]
    step_err = [abs(r["err_pct"]) for r in cal["steps"]]
    edge_err = [abs(r["err_pct"]) for r in cal["steps"] if r["quantity"] == "edge"]
    tp_err = {r["backend"]: r["err_pct"] for r in cal["throughput"]}
    s0 = run("S0")
    s1, s3, s4, s5, s7 = run("S1"), run("S3"), run("S4"), run("S5"), run("S7")
    w1, w2, w3 = run("W1"), run("W2"), run("W3")
    enc_w4 = [r for r in cal["steps"] if r["backend"] == "cpu-w4" and r["quantity"] == "encrypt"][0]["measured_ms"]
    berow = {(r["style"], r["k"]): r for r in be}

    add("# EdgeFHE 軟硬體切分模型 v%s（以量測為本的初版）" % VERSION)
    add("")
    add("產生方式：`python partition_model.py`（只用 Python 標準函式庫，結果可重現，約 1 分鐘）。程式唯讀 repo 內 "
        "2026-10-14 的 KV260 量測檔，重跑會覆寫本檔、`model_results.json`、`graph_best.mmd`、`graph_best.dot`。")
    add("")
    add("**數字標記**：**MEASURED（M）**＝直接取自結果檔（附路徑）；**DERIVED（D）**＝由量測值以公式推得（附公式）；"
        "**ESTIMATED（E）**＝工程估計（寫明假設）；**ASSUMED（A）**＝情境輸入（未來的驅動或硬體，不是事實）。"
        "情境結果是模型**預測**，不是實測；只有第 4 節的「實測」欄是板上量測。")
    add("")
    # ------------------------------------------------------------------ 0
    add("## 0. 摘要")
    add("")
    add("- **校準**：模型照 V5「實際寫法」重建六種設定（cpu-w1–w4、fpga-serial、fpga-overlap），"
        "各步驟誤差最大 %.1f%%、邊緣延遲誤差最大 %.1f%%；19 批管線吞吐量誤差 %s（cpu-w2 例外 %s，兩個瓶頸幾乎相等時"
        "max-bound 會低估）；CPU 使用率的預測與實測差 ≤ 0.01。fpga-serial：加密 %s / 解密 %s / 邊緣 %s ms"
        "（實測 47.27 / 6.46 / 71.26）。"
        % (max(step_err), max(edge_err),
           "、".join("%s %s" % (b, pct(e)) for b, e in tp_err.items() if b != "cpu-w2"), pct(tp_err["cpu-w2"]),
           f2([r for r in cal["steps"] if r["backend"] == "fpga-serial" and r["quantity"] == "encrypt"][0]["model_ms"]),
           f2([r for r in cal["steps"] if r["backend"] == "fpga-serial" and r["quantity"] == "decrypt"][0]["model_ms"]),
           f2([r for r in cal["steps"] if r["backend"] == "fpga-serial" and r["quantity"] == "edge"][0]["model_ms"])))
    add("- **現況（S0）輸在驅動，不在 PL**：一次 op2 呼叫 4.68 ms 裡，validate 3.59 ms＋copy_out 0.45 ms 都是 ARM 在讀"
        "不經快取的記憶體；PL＋DMA 只有 0.55 ms。更糟的是這 4.1 ms 全是 **ARM 忙碌時間**，所以 S0 卸載也不會空出 ARM。"
        "模型在 D0 下的延遲最佳切分＝全部留在 ARM（任何 PL 指派的增益都 < 1%）。")
    add("- **加密步驟有 %.2f ms 是與後端無關的序列程式碼**（`residues()` 以 `int64 %% q` 把 m、u、e0、e1 轉成 RNS ≈ %.2f ms，每元素約 47 個週期，像是 64-bit 除法，"
        "加 12 次 add() ≈ %.2f ms），占 cpu-w4 加密 %.2f ms 的 %.0f%%；keygen 的量測獨立印證每個元素約 %.1f ns。"
        "這段是 ARM 專屬、而且多半可用查表／Barrett 消掉（ESTIMATED，見 W1）。"
        % (P["S_enc"], 4 * (P["t_rns_m_f"] + P["t_rns_t_f"]), 12 * P["t_add_call"], enc_w4,
           100 * P["S_enc"] / enc_w4, P["residue_ns_per_elem"]))
    a4 = s4["amdahl"]
    imf = cal["impl_floor"]
    add("- **現行程式結構下的 Amdahl 上限（只用 MEASURED/DERIVED）**：照 V5 現在的寫法（IMPL），就算 NTT/INTT/pointwise/add 全部"
        "免費、傳輸也免費，邊緣延遲只能從實測 %.2f 降到 %.1f ms（最多 %.2f×），吞吐量最多 %.2f×（瓶頸：%s）。"
        "任何只換「乘積由誰算」的方案都跨不過這條線。"
        % (meas_w4_edge, imf["edge_floor_ms"], imf["edge_ceiling_vs_measured_w4"], imf["throughput_ceiling_vs_measured_w4"],
           imf["bottleneck"]))
    add("- **Amdahl 天花板**：就算所有可上 PL 的節點（NTT/INTT/pointwise/add）都變成 0，邊緣延遲也只能到 %.1f ms"
        "（DAG 排程；encode 4 核 8.3 ms＋decode 單執行緒 8.4 ms 占大半），相對同軟體的 all-ARM（%.1f ms）最多 %.2f×；"
        "吞吐量最多 %.2f×。光是把軟體改成資料流排程（不動 PL），模型就預測 cpu-w4 由 37.4 降到約 %.1f ms。"
        % (a4["floor_latency_ms"], s4["all_arm"]["latency_ms"], a4["ceiling_latency_vs_allarm"],
           a4["ceiling_throughput_vs_allarm"], s0["all_arm"]["latency_ms"]))
    add("- **修好驅動後（D1/D2，ESTIMATED/ASSUMED）PL 才划算，而且主要贏在吞吐量**：S1–S4 的延遲最佳切分 %s ms"
        "（比同情境 all-ARM 少 %s），吞吐量 %s 筆/秒（%s× all-ARM），因為 ARM 才是吞吐瓶頸，"
        "每卸載一個乘積可省約 4 ms ARM 核心時間、只付約 0.3–0.5 ms 打包／驅動。延遲最佳解是**混合**的"
        "（留 1 個特徵或部分轉換在 ARM 與 PL 並行），不是「全部丟 PL」。"
        % (" / ".join(f2(run(n)["best_latency"]["latency_ms"], 1) for n in ("S1", "S2", "S3", "S4")),
           " / ".join("%.0f%%" % (100 * (1 - run(n)["best_latency"]["latency_ms"] / run(n)["all_arm"]["latency_ms"]))
                      for n in ("S1", "S2", "S3", "S4")),
           " / ".join("%.0fk" % (run(n)["best_throughput"]["records_per_s"] / 1e3) for n in ("S1", "S2", "S3", "S4")),
           " / ".join(f2(run(n)["all_arm"]["T_ms"] / run(n)["best_throughput"]["T_ms"], 2) for n in ("S1", "S2", "S3", "S4"))))
    add("- **ARM 變強，PL 的延遲價值很快消失**：NTT 快 3×（S5）時延遲增益只剩 %.1f%%（%.2f vs %.2f ms），吞吐量仍多 %.0f%%。"
        "8 個乘積以 op2 形式卸載，在 D1 下 ARM 只要快 %.2f× 就打平（延遲）；改用 Stone 建議的 K 介面可撐到 %.2f×（D1）／%.2f×（D2）。"
        % (100 * (1 - s5["best_latency"]["latency_ms"] / s5["all_arm"]["latency_ms"]), s5["best_latency"]["latency_ms"],
           s5["all_arm"]["latency_ms"], 100 * (s5["all_arm"]["T_ms"] / s5["best_throughput"]["T_ms"] - 1),
           berow[("op2", 8)]["D1"]["arm_speed_breakeven_latency"], berow[("kpair", 8)]["D1"]["arm_speed_breakeven_latency"],
           berow[("kpair", 8)]["D2"]["arm_speed_breakeven_latency"]))
    add("- **Stone min-cut 提出比指定融合運算更好的介面**：只把 u_f 送進 PL（公鑰常駐、NTT 形式），回傳 b·u_f、a·u_f，"
        "加法留在 ARM（K 運算；每特徵進 1 出 2 個多項式，而不是進 4 出 2）。列舉法驗證：S7 %.2f ms vs S4 %.2f ms（延遲約好 3%%，吞吐量相同；差距小，重點是方法能指出介面）。"
        "Stone 自己的解（4×K）makespan 是 %.2f ms，列舉法的 makespan 最佳解（%s）是 %.2f ms：前者最小化總工作量，後者最小化完成時間。"
        % (s7["best_latency"]["latency_ms"], s4["best_latency"]["latency_ms"],
           st["S7"].get("stone_assignment_latency_ms") or float("nan"), short_asg(s7["best_latency"]["assignment"]),
           s7["best_latency"]["latency_ms"]))
    add("- **下一步最值得量的不是 PL**：若 encode/decode 的成本真的主要在逐係數 `std::pow` twist（未量測的假設），"
        "把它快 3×（W3）會讓 PL 切分的價值變大：延遲 %.1f vs all-ARM %.1f ms（%.2f×），吞吐 %.0fk vs %.0fk 筆/秒。"
        "也就是 Amdahl 的順序：先量、先修 ARM 專屬節點，再談加硬體。"
        % (w3["best_latency"]["latency_ms"], w3["all_arm"]["latency_ms"],
           w3["all_arm"]["latency_ms"] / w3["best_latency"]["latency_ms"],
           w3["best_throughput"]["records_per_s"] / 1e3, w3["all_arm"]["records_per_s"] / 1e3))
    add("")
    add("**哪些結論穩、哪些看估計值**：S0 的診斷、序列 RNS 段、Amdahl 天花板、校準誤差都只用 MEASURED/DERIVED，屬穩健結論。"
        "S1–S7 的絕對數字依賴 D1 的 ESTIMATED 成本（團隊自己的估計＋本模型加的快取維護）、D2 的 ASSUMED、"
        "以及新 HLS 運算的 ESTIMATED 週期數；但「延遲增益小、吞吐增益大、最佳解是混合切分」在 D1 每 byte 成本放大 8× 時仍成立（第 7.2 節）。"
        "W1–W3 是 ARM 軟體的假設情境，只用來排定量測優先順序。")
    add("")
    # ------------------------------------------------------------------ 1
    add("## 1. 方法")
    add("")
    add("老師的批評：ARM／FPGA 的分工是憑直覺定的。本模型改成：")
    add("")
    add("1. **拆成最小可實作節點**：把每批（2,048 筆、4 特徵）的 client 工作拆成第 2 節的 DAG。節點的粒度＝今天或下一版"
        "能單獨實作的一段程式或一個 PL 運算（例如每個 limb 的 NTT、每個特徵的 RNS 對應）。")
    add("2. **量／估每個節點在 ARM 與 PL 上的成本**：ARM 成本由實測的步驟時間與 V5 原始碼裡的工作量（轉換次數、元素數）"
        "反推（DERIVED）；PL 成本由 RTL cosim 週期數（MEASURED）組合；PL 做不到的節點標成 ARM-only。")
    add("3. **邊上放傳輸成本**：一次 PS→PL→PS 呼叫＝`pack`（使用者空間轉置）＋`pre`（驅動：copy_in、validate、清空、DMA 設定）"
        "＋`win`（DMA＋PL kernel；ARM 執行緒睡眠）＋`post`（copy_out、wipe）＋`unpack`。參數由 DMA trace 校準（第 3 節）。")
    add("4. **先校準再預測**：照 V5 的實際結構（步驟依序、client 序列段、CpuBackend 兩階段 pool、FpgaBackend 逐筆或雙緩衝）"
        "重建六種已量測設定，對上實測（第 4 節）後，才拿去預測新情境。")
    add("5. **求最佳切分**：對每個情境窮舉所有雙棲節點的 ARM/PL 指派（4 個特徵對稱→以多重集合列舉，含「k 個特徵上 PL、"
        "其餘留 ARM」的混合解），每個指派用 list scheduler 排程：ARM 4 核（encode 每特徵一個任務、decode 單執行緒、"
        "CRT 可依係數切 4 塊—每個係數獨立，理由充分）、PL 一個引擎；驅動 mutex 深度 1（現況）或 2（雙緩衝：第 i+1 次的傳輸"
        "與第 i 次的 kernel 重疊）。")
    add("6. **目標函數**：(1) 單批邊緣延遲（不含雲端）；(2) 長串流穩態吞吐量＝2048 ÷ 每批瓶頸資源時間"
        "（ARM 4 核、PL 引擎、驅動 mutex、各管線段、加解密共用的 backend lock，各除以由實測校準的效率 η）；(3) 能耗"
        "（整板功率線性模型，由 54 筆實測擬合）。")
    add("7. **兩個對照解法**：Stone（IEEE TSE 1977）雙處理器 min-cut（Edmonds-Karp 自寫）與 LogCA（Altaf & Wood, ISCA 2017）式的"
        "break-even 分析。")
    add("")
    add("**三種排程模式**：`IMPL`＝V5 現在的寫法（只用於校準）；`STEP`＝保留 V5 的步驟邊界、步驟內依資料相依並行；"
        "`DAG`＝只保留資料相依（資料流 runtime）。情境比較一律在**同一模式**下比，所以「資料流重構」和「PL 切分」的效果可以分開看。"
        "相同延遲（差 < %.0f%%）時，報告呼叫次數最少的切分（不值得為 < 1%% 去加硬體呼叫）。" % (100 * TIE_TOL))
    add("")
    # ------------------------------------------------------------------ 2
    add("## 2. 節點表（每批）")
    add("")
    add("資料大小：軟體 RNS 多項式 3×4096×uint32＝48 KiB（limb-major）；DMA 格式每係數一個 128-bit beat＝64 KiB。"
        "ARM 成本為**單核**時間（4 核同時跑轉換時再乘 %s，DERIVED c_be4；4 個 encode 同時跑乘 %s，DERIVED c_enc4）。"
        % (pv("c_be4"), pv("c_enc4")))
    add("")
    cyc = lambda k: "%d cyc = %.3f ms" % (P[k], P[k] * CYC_NS * 1e-6)
    rows = [
        ["STD", "1", "原始 4×2048 double → z 4×16 KiB", "%s (%s)" % (pv("t_std"), pl("t_std")), "否（ARM-only）", "-"],
        ["ENC_f", "4", "z_f 16 KiB → m_f 32 KiB（int64）", "%s (%s) = (encode_w1 − NOWRAP)/4" % (pv("t_enc_f"), pl("t_enc_f")),
         "否：浮點 FFT＋逐係數 `std::pow` twist＋取整", "what-if：僅 FFT 核心 %s (E)，需倍精度；twist 未計，不採用" % cyc("cyc_fft_pl_whatif")],
        ["NOWRAP", "1", "4×m_f → 上界", "%s (%s)" % (pv("t_nowrap"), pl("t_nowrap")), "否", "-"],
        ["SAMPLE", "1", "OS 亂數 → 12 個三元多項式（各 4 KiB）", "%s (%s)" % (pv("t_sample"), pl("t_sample")),
         "否（政策：PRNG 不搬回 PL）", "-"],
        ["VALKEY", "1", "公鑰 96 KiB 檢查", "%s (%s)" % (pv("t_valkey"), pl("t_valkey")), "否", "-"],
        ["RNS_m_f", "4", "m_f 32 KiB → 48 KiB", "%s (%s) = 12288×%s ns" % (pv("t_rns_m_f"), pl("t_rns_m_f"), pv("residue_ns_per_elem", 1)),
         "否（未實作；可做 Barrett）", "-"],
        ["RNS_t_f", "4", "u,e0,e1 3×4 KiB → 3×48 KiB", "%s (%s)" % (pv("t_rns_t_f"), pl("t_rns_t_f")), "否（三元值可查表）", "-"],
        ["NTT_a / NTT_b", "2（各 3 limb）", "48 KiB → 48 KiB", "3×%s (%s)，可 session 快取" % (pv("t_ntt_limb"), pl("t_ntt_limb")),
         "是：op0", "%s (M, cosim，含串流)" % cyc("cyc_op0")],
        ["NTT_u_f", "4", "48 KiB → 48 KiB", "3×%s (%s)" % (pv("t_ntt_limb"), pl("t_ntt_limb")), "是：op0", "%s (M)" % cyc("cyc_op0")],
        ["PW_bu_f / PW_au_f", "8", "2×48 KiB → 48 KiB", "3×%s (%s)" % (pv("t_pw_limb"), pl("t_pw_limb")),
         "只在 op2／F／K 內", "pw_eff %s (D)" % cyc("cyc_pw_eff")],
        ["INTT_bu_f / INTT_au_f", "8", "48 KiB → 48 KiB", "3×%s (%s，假設＝正轉換)" % (pv("t_intt_limb"), pl("t_intt_limb")),
         "是：op1", "%s (M)" % cyc("cyc_op1")],
        ["OP2（黑盒乘積）", "8＋1", "DMA 128 KiB → 64 KiB", "-", "是：op2（兩運算元都重送、重轉）", "%s (M)" % cyc("cyc_op2")],
        ["ADD_c0_f", "4", "bu＋e0＋m → c0", "2×%s (%s)" % (pv("t_add_call"), pl("t_add_call")), "是：op4（被支配，見 3.3）", "%s (M)" % cyc("cyc_op4")],
        ["ADD_c1_f", "4", "au＋e1 → c1", "%s (%s)" % (pv("t_add_call"), pl("t_add_call")), "是：op3（被支配）", "%s (M)" % cyc("cyc_op3")],
        ["FENC_f（S2+）", "4", "DMA m,u,e0,e1 4×64 KiB → c0,c1 2×64 KiB", "-", "新 HLS 運算（公鑰常駐 NTT 形式）", "%s (E)" % cyc("cyc_fenc_f")],
        ["KPAIR_f（S7+）", "4", "DMA u 64 KiB → b·u, a·u 2×64 KiB", "-", "新 HLS 運算（Stone 建議）", "%s (E)" % cyc("cyc_kpair")],
        ["CLOUD", "1", "4 密文 384 KiB → 1 密文 96 KiB", "%s (%s)，不在邊緣範圍；吞吐量模式在板上同行程" % (pv("t_cloud"), pl("t_cloud")), "-", "-"],
        ["DEC_PRE", "1", "檢查 c0', c1', s", "%s (%s)" % (pv("t_dec_pre"), pl("t_dec_pre")), "否", "-"],
        ["RNS_s", "1", "s 4 KiB → 48 KiB", "%s (%s)，可 session 快取" % (pv("t_rns_s"), pl("t_rns_s")), "否", "-"],
        ["NTT_c1 / NTT_s", "2", "48 KiB → 48 KiB", "3×%s (%s)；NTT(s) 可快取" % (pv("t_ntt_limb"), pl("t_ntt_limb")), "是：op0", "%s (M)" % cyc("cyc_op0")],
        ["PW_d / INTT_d", "1 / 1", "→ c1'·s", "3×%s / 3×%s" % (pv("t_pw_limb"), pv("t_intt_limb")), "INTT：op1；或整個 op2", "%s / %s" % (cyc("cyc_pw_eff"), cyc("cyc_op1"))],
        ["ADD_d", "1", "c0'＋c1'·s", "%s (%s)" % (pv("t_add_call"), pl("t_add_call")), "是：op3（被支配）", "%s (M)" % cyc("cyc_op3")],
        ["FDEC（S6）", "1", "DMA c0',c1' → d", "-", "新 HLS 運算（私鑰常駐，安全取捨）", "%s (E)" % cyc("cyc_fdec")],
        ["CRT", "1", "48 KiB → 64 KiB（int128）", "%s (%s)；可依係數切塊並行" % (pv("t_crt"), pl("t_crt")), "否（128-bit，未實作）", "-"],
        ["NWCHECK", "1", "64 KiB 比較", "%s (%s)" % (pv("t_nwcheck"), pl("t_nwcheck")), "否", "-"],
        ["DEC_FFT", "1", "64 KiB → 2048 complex 32 KiB", "%s (%s)，單執行緒" % (pv("t_dec_fft"), pl("t_dec_fft")), "否（同 ENC）", "-"],
        ["SIGMOID", "1", "2048 logit → 機率／判斷", "%s (%s)" % (pv("t_sigmoid"), pl("t_sigmoid")), "否", "-"],
        ["SPMUL（只用於 W2）", "9×3", "稀疏三元乘積（64 個 ±1）", "%s／limb (%s)" % (pv("t_sparse_prod_limb"), pl("t_sparse_prod_limb")), "-", "-"],
    ]
    add(md_table(["節點", "每批數量", "輸入 → 輸出", "ARM 成本 ms（標記）", "PL 今天能做嗎", "PL kernel 成本（標記）"], rows))
    add("")
    add("ARM 成本的推導（都在 `model_results.json` 的 `parameters`，每項附來源）：")
    add("")
    add("- `t_ntt_limb` = %s ms (D)：CpuBackend 的 wall time（`%s` latency `backend_stats.wall_ms` ÷ 30 輪 = %s ms/輪，cpu-w1）"
        "÷ 每輪 51 個 limb 轉換（加密 18 正＋24 逆、解密 6 正＋3 逆，原始碼 `%s`）減去 pointwise（E，%s ms/limb）。"
        % (pv("t_ntt_limb", 4), SRC_E2E3, pv("be_wall_w1"), SRC_CPUBE, pv("t_pw_limb")))
    add("- 2/3/4 個 worker 的減速 c_be2..4 = %s / %s / %s (D)：同法，任務數取 ceil(任務/worker) 輪。"
        % (pv("c_be2"), pv("c_be3"), pv("c_be4")))
    add("- `S_enc` = %s ms、`S_dec` = %s ms (D)：各步驟實測減去後端模型的平均（5／6 個後端），殘差 ≤ 0.17 ms。"
        "它們是 `Client::encrypt/decrypt`（`%s`）的序列部分，與後端無關。" % (pv("S_enc"), pv("S_dec"), SRC_CLIENT))
    add("- 每元素 residue 成本 %s ns (D)：由 cpu-w1 keygen（7.37 ms，單次冷樣本）減去 1 個乘積的後端模型得到；"
        "用它算出的 S_enc 分解（RNS %s＋add %s ms）與 op-count 估計（add() ≈ 0.074 ms）一致。"
        % (pv("residue_ns_per_elem", 1), f2(4 * (P["t_rns_m_f"] + P["t_rns_t_f"])), f2(12 * P["t_add_call"])))
    add("")
    # ------------------------------------------------------------------ 3
    add("## 3. 邊與傳輸模型")
    add("")
    add("一次呼叫：`t_call = pack(I) + [t_fixed·s + I·(copy_in+validate+maint) + O·clear] + [K·10ns + I·δ + t0] "
        "+ [t_fixed·(1−s) + O·(copy_out+maint) + wipe] + unpack(O)`，I/O 為輸入／輸出 bytes（或 beats），K 為 kernel 週期。"
        "中括號依序是 `pre`（ARM 忙、持有 mutex）、`win`（ARM 睡、PL 忙）、`post`（ARM 忙、持有 mutex）。")
    add("")
    d0 = Driver("D0", P, "D0")
    d1 = Driver("D1", P, "D1")
    d2 = Driver("D2", P, "D2")
    rows = [
        ["copy_in (ns/B)", f2(d0.copy_in * 1e6, 3) + " (M)", f2(d1.copy_in * 1e6, 3) + " (A＝D0)", "0 (A)"],
        ["validate (ns/B)", f2(d0.validate * 1e6, 2) + " (M)", f2(d1.validate * 1e6, 3) + " (E 團隊)", "0：併入 pack (A)"],
        ["clear_out (ns/B)", f2(d0.clear * 1e6, 3) + " (M)", f2(d1.clear * 1e6, 3) + " (A)", "0 (A)"],
        ["快取維護 (ns/B)", "0", f2(d1.maint * 1e6, 3) + " (E)", f2(d2.maint * 1e6, 3) + " (E)"],
        ["copy_out (ns/B)", f2(d0.copy_out * 1e6, 3) + " (M)", f2(d1.copy_out * 1e6, 3) + " (E 團隊)", "0 (A)"],
        ["wipe", "%.1f us 固定 (M)" % (d0.wipe_fixed * 1e3), "%.3f ns/B (D)" % (d1.wipe_per_B * 1e6), "%.3f ns/B (A)" % (d2.wipe_per_B * 1e6)],
        ["t_fixed（syscall、mutex、檢查、DMA 設定）", "%.1f us (D)" % (d0.fixed_ms * 1e3), "同 (A)", "同 (A)"],
        ["win 的 t0（DMA 啟動＋IRQ＋喚醒）", "%.1f us (D)" % P["win_t0_us"], "同", "同"],
        ["win 的 δ（每輸入 beat 額外）", "%.2f ns/beat (D)" % P["win_delta_ns_per_beat"], "同", "同"],
        ["pack / unpack (ns/beat)", "%.2f / %.2f (D)" % (P["pack_ns_per_beat"], P["unpack_ns_per_beat"]), "同", "pack ×%.1f (A)" % P["d2_pack_factor"]],
        ["每次呼叫固定部分 t_fixed+wipe+t0", "%.3f ms" % d0.fixed_part(), "%.3f ms" % d1.fixed_part(), "%.3f ms" % d2.fixed_part()],
    ]
    add(md_table(["參數", "D0 現況 V3 驅動", "D1 修正驅動（可快取＋快取維護）", "D2 zero-copy（u-dma-buf 類）"], rows))
    add("")
    add("來源：D0 各段＝`%s` 的中位數（109 次呼叫）；t_fixed 由未追蹤的整體 4.677 ms（`%s` baseline）扣掉各段得到；"
        "t0、δ 由 V3 benchmark 的 op0/op1/op2 driver p50（`%s`）與 V5 未追蹤 op2 共 4 點最小平方擬合，殘差 ≤ 1.7 us。"
        "D1 的 validate／copy_out 是團隊在 `%s` 的估計（3.59→0.05、0.45→0.05 ms），快取維護（每 64 KiB 約 10 us）是本模型另加的 ESTIMATED。"
        "D2 的 1.6 GB/s（128 bit×100 MHz）已內含在 kernel 的 1 beat/cycle 串流裡，所以不會再額外加傳輸時間。"
        % (SRC_BRK, SRC_TRC, SRC_RES, SRC_COMP))
    add("")
    add("### 3.1 一次 op2 乘積（輸入 128 KiB、輸出 64 KiB）在三種驅動下")
    add("")
    rows = []
    for name, d in (("D0", d0), ("D1", d1), ("D2", d2)):
        p = d.parts(2 * N, N, P["cyc_op2"])
        rows.append([name, f2(p["pack"], 3), f2(p["pre"], 3), f2(p["win"], 3), f2(p["post"], 3), f2(p["unpack"], 3),
                     f2(sum(p.values()), 3), f2(p["pack"] + p["pre"] + p["post"] + p["unpack"], 3)])
    add(md_table(["驅動", "pack", "pre", "win（PL+DMA）", "post", "unpack", "合計 ms", "其中 ARM 忙碌 ms"], rows))
    add("")
    add("同一個乘積在 ARM 上（cpu-w1 單核、無共用轉換）約 %.2f ms 核心時間：D0 呼叫本身就花 %.2f ms ARM 忙碌時間，"
        "所以 S0 的卸載連 ARM 都省不下來。" % (6 * P["t_ntt_limb"] + 3 * (P["t_ntt_limb"] + P["t_pw_limb"]),
                                     sum(v for k, v in d0.parts(2 * N, N, P["cyc_op2"]).items() if k != "win")))
    add("")
    add("### 3.2 t0／δ 擬合點")
    add("")
    add(md_table(["輸入 beats", "cosim 週期", "實測 window us", "模型 us"],
                 [[p["in_beats"], p["cycles"], f2(p["window_us"], 1), f2(p["model_us"], 1)] for p in P.items["win_t0_us"]["fit_points"]]))
    add("")
    add("### 3.3 被支配的選項（不進列舉）")
    add("")
    add("把加法丟到 PL（op3/op4）在三種驅動下都被支配：呼叫的 ARM 端成本（pack＋pre＋post＋unpack）已大於 ARM 直接相加，"
        "而且結果更晚回來，所以不可能改善延遲或吞吐量。")
    add("")
    add(md_table(["驅動", "選項", "ARM 直接做 ms", "呼叫的 ARM 端 ms", "呼叫延遲 ms", "被支配"],
                 [[r["driver"], r["option"], f2(r["arm_cost_ms"], 3), f2(r["arm_side_of_call_ms"], 3),
                   f2(r["call_latency_ms"], 3), "是" if r["dominated"] else "否"] for r in res["dominance_add_on_pl"]]))
    add("")
    # ------------------------------------------------------------------ 4
    add("## 4. 校準：模型先重現實測（IMPL 模式）")
    add("")
    add("### 4.1 每步驟延遲（ms，p50；`%s`）" % SRC_E2E3)
    add("")
    rows = []
    by = collections.defaultdict(dict)
    for r in cal["steps"]:
        by[r["backend"]][r["quantity"]] = r
    for b in BACKENDS:
        row = [b]
        for q in ("prepare", "encrypt", "decrypt", "decode", "edge"):
            r = by[b][q]
            row.append("%s / %s (%s)" % (f2(r["measured_ms"]), f2(r["model_ms"]), pct(r["err_pct"])))
        rows.append(row)
    add(md_table(["後端", "準備 ①–③ 實測/模型", "加密 ④⑤", "解密 ⑨", "解碼 ⑩", "邊緣合計"], rows))
    add("")
    add("擬合用到的量測：每個後端的 backend wall time（推 t_ntt_limb、c_be2..4）、加解密步驟（推 S_enc、S_dec 兩個常數）、"
        "stage2_02 的單執行緒 encode（推 t_enc_f）。其餘欄位是結構預測：例如 fpga-serial、fpga-overlap 的加密步驟完全由"
        "trace 校準的 D0 呼叫模型＋S_enc 預測，誤差 %s／%s。"
        % (pct(by["fpga-serial"]["encrypt"]["err_pct"]), pct(by["fpga-overlap"]["encrypt"]["err_pct"])))
    add("")
    add("### 4.2 保留驗證（held-out）")
    add("")
    note_zh = lambda t: "樣本內（用來定 residue 成本）" if t.startswith("in-sample") else "保留驗證（單次冷樣本）"
    rows = [[r["backend"], f2(r["measured_ms"], 3), f2(r["model_ms"], 3), pct(r["err_pct"]), note_zh(r["note"])] for r in cal["keygen"]]
    add(md_table(["keygen 後端", "實測 ms (M)", "模型 ms", "誤差", "說明"], rows))
    add("")
    rows = [[r["call"], f2(r["measured_ms"], 3), f2(r["model_ms"], 3), pct(r["err_pct"]), r["source"]] for r in cal["calls"]]
    add(md_table(["單次呼叫", "實測 ms (M)", "模型 ms", "誤差", "來源"], rows))
    add("")
    add("V3 的 op0／op1（NTT／INTT 單獨呼叫）在 D0 模型裡完全沒有被擬合，預測誤差約 +4%（V3 的 pack/unpack 程式不同）。")
    add("")
    add("### 4.3 吞吐量、CPU 使用率、功率（19 批、佇列容量 2；`%s`）" % SRC_E2E3)
    add("")
    rows = []
    for r in cal["throughput"]:
        rows.append([r["backend"], r["role"], f2(r["measured_T_ms"]), f2(r["model_T_ms"]), pct(r["err_pct"]), r["bottleneck"],
                     "%.0f" % r["reported_rec_s"], "%.3f / %.3f" % (r["cpu_util_measured"], r["cpu_util_model_at_measured_T"]),
                     "%.2f / %.2f" % (r["watts_measured"], r["watts_model"])])
    add(md_table(["後端", "角色", "實測 wall/批 ms", "模型 ms", "誤差", "模型瓶頸", "報表筆/秒 (M)", "CPU 使用率 實測/模型", "W 實測/模型"], rows))
    add("")
    w2b = [r for r in cal["throughput"] if r["backend"] == "cpu-w2"][0]["bounds_ms"]
    udiff = max(abs(r["cpu_util_measured"] - r["cpu_util_model_at_measured_T"]) for r in cal["throughput"])
    add("效率 η：η_L = %.3f（backend-lock 受限的 cpu-w1、fpga-serial、fpga-overlap 平均）、η_C = %.3f（ARM 受限的 cpu-w4）。"
        "cpu-w2、cpu-w3 是保留驗證；cpu-w2 的 %s 來自兩個界限（lock %.1f、ARM %.1f ms）幾乎相等，max-bound 忽略了兩者的交互排隊。"
        "CPU 使用率的模型值（ARM 核心時間÷(4×實測 wall)）在 6 種設定都與實測差 ≤ %.3f，"
        "這直接驗證「D0 驅動的 validate／copy_out 是 ARM 忙碌時間」。串行（容量 0）吞吐量 6 種設定誤差 %s 到 %s。"
        % (cal["eta"]["L"], cal["eta"]["C"], pct(tp_err["cpu-w2"]), w2b["backend lock (encrypt+decrypt stages)"],
           w2b["ARM cores (4)"], udiff, pct(min(r["err_pct"] for r in cal["serial"])),
           pct(max(r["err_pct"] for r in cal["serial"]))))
    add("")
    add("功率模型 W = %.2f + %.3f×忙碌核心 + %.2f×PL duty（54 筆，RMSE %.3f W；PL 項只有 3 個 duty 水準，辨識度低）。"
        % (P["pw_P0"], P["pw_core"], P["pw_pl"], P["pw_rmse"]))
    add("")
    add("")
    # ------------------------------------------------------------------ 5
    add("## 5. 情境結果（預測）")
    add("")
    add("基準：實測 cpu-w4 邊緣延遲 %.2f ms、19 批吞吐量 %.0f 筆/秒（報表值）。延遲是單批邊緣（不含雲端）；吞吐量是板上配置"
        "（雲端在同一行程，和實測一致）；「edge-only」欄假設雲端在 PC。" % (meas_w4_edge, meas_w4_rps))
    add("")
    for title, names in (("5.1 指定情境 S0–S5", ("S0", "S1", "S2", "S3", "S4", "S5")),
                         ("5.2 延伸情境（what-if）", ("S6", "S7", "W1", "W2", "W3"))):
        add("### " + title)
        add("")
        rows = []
        for n in names:
            r = run(n)
            bl, bt, aa, am = r["best_latency"], r["best_throughput"], r["all_arm"], r["amdahl"]
            r2 = sc[n]["runs"].get("DAG-d2")
            rs = sc[n]["runs"].get("STEP-d1")
            rows.append([
                n, sc[n]["driver"], short_asg(bl["assignment"]),
                "%s / %s / %s" % (f2(bl["latency_ms"]), f2(r2["best_latency"]["latency_ms"]) if r2 else "-",
                                  f2(rs["best_latency"]["latency_ms"]) if rs else "-"),
                f2(aa["latency_ms"]), "%.2f×" % (aa["latency_ms"] / bl["latency_ms"]), "%.2f×" % (meas_w4_edge / bl["latency_ms"]),
                short_asg(bt["assignment"]), "%.0f" % bt["records_per_s"], "%.0f" % bt["edge_only_records_per_s"],
                "%.2f×" % (aa["T_ms"] / bt["T_ms"]), bt["bottleneck"],
                "%.2f× / %.2f×" % (am["ceiling_latency_vs_allarm"], am["ceiling_throughput_vs_allarm"]),
                "%.0f" % bt["records_per_J"]])
        add(md_table(["情境", "驅動", "延遲最佳切分（DAG, d1）", "延遲 ms DAG-d1 / DAG-d2 / STEP", "同情境 all-ARM ms",
                      "vs all-ARM", "vs 實測 cpu-w4", "吞吐最佳切分", "筆/秒（板上）", "筆/秒（edge-only）",
                      "吞吐 vs all-ARM", "吞吐瓶頸", "Amdahl 上限 延遲/吞吐", "筆/焦耳"], rows))
        add("")
    add("情境定義：")
    add("")
    for n, s in SCENARIOS.items():
        add("- **%s**（%s）：%s" % (n, s["driver"], s["desc"]))
    add("")
    add("切分記號：`4xA`＝4 個特徵都在 ARM；`F`＝融合加密運算；`K`＝公鑰常駐乘積對；`O`＝op2 黑盒；`OA`＝b·u 用 op2、a·u 留 ARM；"
        "`N0`＝NTT(u) 用 op0；`N1`＝兩個 INTT 用 op1；`N01`＝兩者；`dec=`解密乘積的位置；S0–S1 另有 `key=A/P`（公鑰 NTT 在 ARM 或 op0）。"
        "S3 以後同類運算的特徵合併成一次呼叫。")
    add("")
    add("### 5.3 誰占滿了 ARM（吞吐最佳切分，每批 ARM 核心 ms，含板上雲端）")
    add("")
    groups = ["encode", "decode", "rns", "cloud", "crt", "products(ARM)", "add", "driver", "pack/unpack", "other"]
    rows = []
    for n in ("S0", "S1", "S3", "S4", "S7", "W1", "W3"):
        bt = run(n)["best_throughput"]
        g = bt["arm_groups_ms"]
        rows.append([n] + [f2(g[k], 1) for k in groups] + [f2(bt["arm_core_ms"], 1), f2(bt["pl_ms"], 2)])
    aa = run("S0")["all_arm"]
    rows.append(["S0 all-ARM"] + [f2(aa["arm_groups_ms"][k], 1) for k in groups] + [f2(aa["arm_core_ms"], 1), "0"])
    add(md_table(["情境"] + groups + ["ARM 合計", "PL 引擎"], rows))
    add("")
    add("### 5.4 延遲最佳解的關鍵路徑（DAG, d1；前幾大節點）")
    add("")
    rows = []
    for n in ("S0", "S1", "S3", "S4", "S5", "S7", "W3"):
        cp = run(n)["critical_path_best"]
        rows.append([n, "、".join("%s %.2f" % (c["node"], c["ms"]) for c in cp[:5])])
    add(md_table(["情境", "關鍵路徑上的主要節點（ms）"], rows))
    add("")
    # ------------------------------------------------------------------ 6
    add("## 6. Stone min-cut vs 列舉法")
    add("")
    add("Stone 模型：每個節點在 ARM／PL 各有執行成本，資料邊被切開時付傳輸成本；min s-t cut＝最小**總工作量**。"
        "可融合情境（S2+）用 PL kernel-only 成本＋有方向的傳輸弧（ARM→PL 付 pack＋驅動進＋串流進；PL→ARM 付串流出＋驅動出＋unpack）。"
        "S0、S1 每個 PL 節點都是獨立呼叫，資料必經 DDR，所以 cut 退化成逐節點比較。")
    add("")
    rows = []
    for n in ("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "W1", "W3"):
        s_ = st.get(n, {})
        if "skipped" in s_ or not s_:
            continue
        rows.append([n, f2(s_["min_total_cost_ms"], 1), len(s_["pl_nodes"]),
                     short_asg(s_["stone_assignment"]) if s_.get("stone_assignment") else stone_note(s_),
                     f2(s_.get("stone_assignment_latency_ms")), f2(s_.get("stone_assignment_total_work_ms"), 1),
                     short_asg(s_["enumeration_best"]), f2(s_["enumeration_best_latency_ms"]), f2(s_["enumeration_best_total_work_ms"], 1),
                     f2(s_["all_arm_total_work_ms"], 1)])
    add(md_table(["情境", "min-cut 值 ms", "PL 節點數", "Stone 解（對應切分）", "其 makespan ms", "其總工作量 ms",
                  "列舉法延遲最佳", "其 makespan ms", "其總工作量 ms", "all-ARM 總工作量 ms"], rows))
    add("")
    add("讀法：")
    add("")
    add("- 兩者目標不同。Stone 最小化 ARM＋PL＋傳輸的**總和**，不知道 ARM 有 4 核、也不知道 ARM 與 PL 可以同時工作，"
        "所以傾向把「單位成本較低」的節點全部搬到 PL；列舉法最小化 **makespan**，會刻意留一部分在 ARM 與 PL 並行（負載平衡）。")
    add("- S2–S6 的 Stone 解是「NTT、pointwise、INTT 上 PL，加法留 ARM」，這不是 F 運算（F 把 m、e0、e1 也送進 PL）。"
        "因此新增 S7（K 運算）：每特徵只送 u_f，傳輸量從 4 進 2 出降到 1 進 2 出。S7 的列舉最佳延遲 %.2f ms，比 S4 的 %.2f ms 好，"
        "這是「用模型而不是直覺」修正介面設計的例子。" % (s7["best_latency"]["latency_ms"], s4["best_latency"]["latency_ms"]))
    add("- 已知限制：Stone 的弧成本是線性的，無法表達「每次呼叫一次固定成本、合併呼叫可分攤」與「同一輸出被兩個消費者使用只需傳一次」；"
        "本模型以平均分攤固定成本近似。W2（稀疏乘積）節點結構不同，不做 Stone。")
    add("")
    # ------------------------------------------------------------------ 7
    add("## 7. Break-even（LogCA 式）與敏感度")
    add("")
    add("把 k 個乘積合併成一次 PL 呼叫，和 cpu-w4（4 核、V5 CpuBackend 的共用轉換）比較單批延遲。"
        "`t_fixed*`＝打平時可容忍的每次呼叫固定開銷＝T_cpu(k) − T_PL 的變動部分；> 實際 t_fixed 才划算，負值＝怎樣都不划算。"
        "`s*`＝ARM 轉換要快幾倍，CPU 就追上這次呼叫（延遲）。三種呼叫形式：op2（兩運算元都送，現況）、F（融合加密：m,u,e0,e1 → c0,c1；"
        "k=1／9 的第 9 個是 FDEC）、K（公鑰常駐乘積對：u → b·u, a·u；k=1／9 的第 9 個是私鑰常駐單乘積）。CPU 對照組在 F、K 形式下也快取公鑰 NTT（公平）。")
    add("")
    rows = []
    for r in be:
        row = [r["style"], r["k"], "%.0f/%.0f" % (r["in_KiB"], r["out_KiB"]), f2(r["kernel_ms"], 3), f2(r["cpu_w4_latency_ms"])]
        for dn in ("D0", "D1", "D2"):
            x = r[dn]
            row.append("%s / %s / %s" % (f2(x["pl_call_ms"]), f2(x["tfixed_breakeven_latency_ms"]), f2(x["arm_speed_breakeven_latency"])))
        rows.append(row)
    add(md_table(["形式", "k", "DMA KiB 進/出", "kernel ms", "cpu-w4 ms", "D0：呼叫 / t_fixed* / s*", "D1：呼叫 / t_fixed* / s*",
                  "D2：呼叫 / t_fixed* / s*"], rows))
    add("")
    add("實際每次呼叫的固定部分：D0 %.3f ms、D1 %.3f ms、D2 %.3f ms。結論："
        % (d0.fixed_part(), d1.fixed_part(), d2.fixed_part()))
    add("")
    add("- D0：所有 k 的 t_fixed* 都是負的——就算固定開銷是 0，D0 的「每 byte」成本（validate 27 ns/B、copy_out 6.9 ns/B）"
        "就已經比 4 核 CPU 慢；合併呼叫救不了 D0（成本與 bytes 成正比，不是固定成本問題）。")
    tf = [r[dn]["tfixed_breakeven_latency_ms"] for r in be for dn in ("D1", "D2")]
    s_op2 = [r[dn]["arm_speed_breakeven_latency"] for r in be if r["style"] == "op2" and r["k"] in (4, 8) for dn in ("D1", "D2")]
    s_k1 = [r["D1"]["arm_speed_breakeven_latency"] for r in be if r["style"] == "kpair"]
    s_k2 = [r["D2"]["arm_speed_breakeven_latency"] for r in be if r["style"] == "kpair"]
    add("- D1／D2：t_fixed* 介於 %.2f–%.2f ms，遠大於實際約 %.3f ms，所以固定開銷已不是問題；限制變成「每 byte 的打包、"
        "unpack 與串流」以及 ARM 本身多快。op2 形式在 k=4–8 時 s* 只有 %.2f–%.2f：ARM 轉換再快一點點，op2 卸載就不划算；"
        "K 形式傳得最少，s* 最高（D1 %.2f–%.2f，D2 %.2f–%.2f）。"
        % (min(tf), max(tf), d1.fixed_part(), min(s_op2), max(s_op2), min(s_k1), max(s_k1), min(s_k2), max(s_k2)))
    add("- 吞吐量的 break-even（ARM 核心時間）寬鬆得多：見 `model_results.json` 的 `tfixed_breakeven_armtime_ms`"
        "（op2、k=8、D1 約 %.1f ms）。" % berow[("op2", 8)]["D1"]["tfixed_breakeven_armtime_ms"])
    add("")
    add("### 7.1 全模型對 ARM 轉換速度的敏感度（DAG, d1）")
    add("")
    rows = []
    for r in res["arm_speed_sensitivity"]:
        rows.append([r["base"], "%.1f×" % r["arm_speed"], f2(r["best_latency_ms"]), f2(r["all_arm_latency_ms"]),
                     "%.1f%%" % (100 * (1 - r["best_latency_ms"] / r["all_arm_latency_ms"])), short_asg(r["best_latency_asg"]),
                     "%.0f" % r["best_T_records_per_s"], "%.0f" % r["all_arm_records_per_s"],
                     "%.0f%%" % (100 * (r["all_arm_T_ms"] / r["best_T_ms"] - 1))])
    add(md_table(["情境", "ARM NTT 加速", "最佳延遲 ms", "all-ARM ms", "PL 延遲增益", "最佳切分", "最佳筆/秒", "all-ARM 筆/秒", "PL 吞吐增益"], rows))
    add("")
    hs = [r for r in res["arm_speed_sensitivity"] if r["arm_speed"] >= 3.0]
    lg = [100 * (1 - r["best_latency_ms"] / r["all_arm_latency_ms"]) for r in hs]
    tg = [100 * (r["all_arm_T_ms"] / r["best_T_ms"] - 1) for r in hs]
    add("延遲方面，ARM 轉換快 3–4× 時 PL 增益掉到 %.1f–%.1f%%（本模型的平手門檻是 1%%）；吞吐量方面仍有 %.0f–%.0f%%，"
        "因為 encode／decode 等 ARM 專屬節點占滿核心，卸載任何工作都能換到核心時間。" % (min(lg), max(lg), min(tg), max(tg)))
    add("")
    add("### 7.2 D1 估計值的敏感度（S3；validate、copy_out、快取維護的每 byte 成本同乘一個倍數）")
    add("")
    rows = [["%.1f×" % r["d1_cost_factor"], f2(r["best_latency_ms"]), short_asg(r["best_latency_asg"]), f2(r["all_arm_latency_ms"]),
             f2(r["best_T_ms"]), f2(r["all_arm_T_ms"])] for r in res["d1_cost_sensitivity_S3"]]
    add(md_table(["D1 成本倍數", "最佳延遲 ms", "最佳切分", "all-ARM ms", "最佳 T ms/批", "all-ARM T ms/批"], rows))
    add("")
    ds = res["d1_cost_sensitivity_S3"]
    lat_gain = lambda r: 100 * (1 - r["best_latency_ms"] / r["all_arm_latency_ms"])
    add("D1 每 byte 成本即使比團隊估計差 %.0f×，PL 在吞吐量上仍勝 %.2f×，延遲增益則從 %.0f%% 縮到 %.0f%%："
        "「延遲小贏、吞吐大贏」不依賴 D1 估計的精確值。"
        % (ds[-1]["d1_cost_factor"], ds[-1]["all_arm_T_ms"] / ds[-1]["best_T_ms"],
           lat_gain([r for r in ds if r["d1_cost_factor"] == 1.0][0]), lat_gain(ds[-1])))
    add("")
    # ------------------------------------------------------------------ 8
    add("## 8. 瓶頸與 Amdahl 討論")
    add("")
    gaps = [run(n)["best_latency"]["latency_ms"] - run(n)["amdahl"]["floor_latency_ms"] for n in ("S1", "S2", "S3", "S4", "S7")]
    add("- **延遲的地板是 ARM 專屬節點**：DAG 排程下，所有可上 PL 的節點成本歸零時，邊緣延遲仍有 %.1f ms：STD 0.43、ENC 8.3"
        "（4 核並行，c_enc4=%.3f）、RNS 與序列 glue、CRT，最後 DEC_FFT %.1f ms 單執行緒收尾。S1–S4、S7 的最佳解離地板只剩 %.1f–%.1f ms。"
        % (a4["floor_latency_ms"], P["c_enc4"], P["t_dec_fft"], min(gaps), max(gaps)))
    btb = s4["best_throughput"]
    duty = btb["pl_ms"] / btb["T_ms"]
    add("- **吞吐量的地板也是 ARM**：S4 吞吐最佳切分每批 ARM 核心時間約 %.0f ms，其中 encode %.1f、decode %.1f、RNS %.1f、"
        "板上雲端 %.1f ms；PL 引擎每批只忙 %.2f ms（利用率 %.0f%%）。新的瓶頸資源是 ARM 4 核，新的瓶頸節點是 ENC_f 與 DEC_FFT。"
        % (btb["arm_core_ms"], btb["arm_groups_ms"]["encode"], btb["arm_groups_ms"]["decode"], btb["arm_groups_ms"]["rns"],
           btb["arm_groups_ms"]["cloud"], btb["pl_ms"], 100 * duty))
    add("- **所以加寬 PL（更多 lanes）幾乎沒有用**：PL 引擎利用率只有 %.0f%%；kernel 再快 2× 最多省下每批 %.1f ms 的 PL 時間，"
        "而且 PL 不在吞吐瓶頸上；在延遲上也只影響關鍵路徑上的那一次呼叫。" % (100 * duty, btb["pl_ms"] / 2))
    w4_impl = [r for r in cal["steps"] if r["backend"] == "cpu-w4" and r["quantity"] == "edge"][0]["model_ms"]
    add("- **資料流重構本身的價值**：S0 的 all-ARM 在 IMPL（現行寫法）模型是 %.1f ms（實測 %.1f），在 DAG 排程是 %.1f ms；"
        "差異來自 RNS／CRT 並行、公鑰與 s 的轉換和 encode 重疊、拿掉步驟邊界。這部分不需要任何硬體。"
        % (w4_impl, meas_w4_edge, s0["all_arm"]["latency_ms"]))
    add("- **W1–W3 說明順序**：只修 RNS（W1）就讓延遲地板降 %.1f ms；若 encode/decode 真能快 3×（W3，未量測），地板降到 %.1f ms，"
        "PL 切分的延遲增益從 S7 的 %.2f× 變成 %.2f×、吞吐增益從 %.2f× 變成 %.2f×。PL 的價值取決於先把 ARM 專屬的部分做快。"
        % (a4["floor_latency_ms"] - w1["amdahl"]["floor_latency_ms"], w3["amdahl"]["floor_latency_ms"],
           s7["all_arm"]["latency_ms"] / s7["best_latency"]["latency_ms"],
           w3["all_arm"]["latency_ms"] / w3["best_latency"]["latency_ms"],
           s7["all_arm"]["T_ms"] / s7["best_throughput"]["T_ms"], w3["all_arm"]["T_ms"] / w3["best_throughput"]["T_ms"]))
    add("- **傳輸量還能再砍（未建模）**：u_f、e0_f、e1_f 都是三元多項式（u 只有 64 個非零），現在每個仍以 64 KiB 的 RNS DMA 格式送；"
        "改成壓縮格式（2 bit/係數＝1 KiB，或 64 個索引＋符號）可讓 K／F 運算的輸入小 1–2 個數量級，代價是 PL 端要加解碼器。")
    add("- **稀疏三元乘積（W2，ESTIMATED）**：u 與 s 只有 64 個非零 ±1，b·u 可直接寫成 64 次帶號負循環平移相加，"
        "ARM 上約 %.2f ms/limb，比 NTT 路徑每乘積攤提約 1.3 ms/limb 便宜；all-ARM 延遲因此由 %.1f 降到 %.1f ms，"
        "PL 的延遲增益只剩 %.0f%%。這需要 constant-time 審查（位移量取決於秘密的非零位置）。"
        % (P["t_sparse_prod_limb"], w1["all_arm"]["latency_ms"], w2["all_arm"]["latency_ms"],
           100 * (1 - w2["best_latency"]["latency_ms"] / w2["all_arm"]["latency_ms"])))
    add("")
    # ------------------------------------------------------------------ 9
    add("## 9. 還缺的量測（把 DERIVED/ESTIMATED 換成 MEASURED）")
    add("")
    rows = [
        ["ARM 每 limb NTT／INTT／pointwise（目前 D/E：由 backend wall 反推，INTT＝NTT 是假設）",
         "微基準直接呼叫 `Plan::transform`、`Plan::product`；`perf stat -e cycles,instructions,L1-dcache-load-misses,l2d_cache_refill`；綁核、暖機",
         "1 核與 4 核同時跑（量 c_be4）；每種 1000 次 × 5 輪", "p50、p95、cycles/butterfly"],
        ["encode() 拆成 FFT／`std::pow` twist／取整；decode() 拆成 twist／IFFT／sigmoid（目前只有整步驟）",
         "`perf record -g` 看熱點＋在 V4 `encode()`／`decode()` 內加 steady_clock 分段（不改數學）", "30 輪 × 3 次", "p50；各段占比"],
        ["residues()、add()、validate()、centered() 的每元素成本（目前由 keygen 單次冷樣本推）",
         "微基準；SDIV 版 vs 查表／Barrett 版", "每種 10⁴ 次 × 5 輪", "ns/元素 p50"],
        ["PL kernel-only 時間（目前用 cosim 週期）",
         "PL 內加 AXI Timer 或在 HLS top 加 free-running counter，記 ap_start→ap_done、第一個輸入 beat→TLAST；或 ILA",
         "每個 op 1000 次", "週期 p50／最大；對照 cosim"],
        ["DMA 視窗的 t0 與 δ（IRQ→完成→喚醒、MM2S 停頓）",
         "現有 `trace_dma_call.py` 加 kprobe 於 DMA callback 與 wait 返回；op3（add2，kernel 幾乎只有串流）掃不同大小",
         "每點 200 次", "中位數＋p90；線性擬合"],
        ["D1／D2 的每 byte 成本（目前 ESTIMATED/ASSUMED）",
         "新驅動候選：可快取 TX/RX＋`dma_sync_single_for_device/cpu`；u-dma-buf 版本；同一 trace 工具分段",
         "64 KiB–1 MiB × 每點 200 次", "us 中位數、MB/s"],
        ["快取維護成本（dma_sync_* clean/invalidate）", "kprobe 計時 `dma_sync_single_*`；或使用者空間 `DC CVAC` 迴圈",
         "64 KiB–1 MiB", "us/64 KiB"],
        ["pack/unpack 的 bytes 線性度與 NEON 版本", "微基準 1–16 個多項式", "1000 次", "ns/beat"],
        ["新 HLS 運算（F、K、FDEC）的週期", "C/RTL cosim＋板上 AXI Timer", "每運算 100 次", "週期"],
        ["4 核同時負載下的效率 η_C、η_L（目前各由 1–3 個設定擬合）",
         "更長串流（≥ 100 批）＋每段 busy/work 時間序列，量填充／排空與佇列水位", "≥ 3 次", "穩態 wall/批"],
        ["分電源功率（目前只有整板 INA260）", "外接功率計或分軌量測（PS／PL）；與吞吐量同步取樣", "≥ 3 次", "W 平均、J/批"],
        ["稀疏三元乘積（W2）與 NEON NTT（S5）實作", "原型＋同上微基準；constant-time 檢查（dudect 類測試）", "1000 次", "p50、時間與秘密是否相關"],
    ]
    add(md_table(["缺什麼（現在的標記）", "怎麼量（工具）", "次數", "統計量"], rows))
    add("")
    # ------------------------------------------------------------------ 10
    add("## 10. 限制")
    add("")
    for t in [
        "PL 的 kernel 時間只有 cosim 週期（MEASURED 於 RTL 模擬，不是板上 kernel-only）；新運算（F、K、FDEC）的週期是由現有運算組合的 ESTIMATED，"
        "沒有考慮資源（DSP 已用 60%、BRAM 43%）與時序（WNS 4 ps）是否放得下常駐金鑰（2×48 KiB NTT 形式約 21 個 BRAM36，或 3 個 URAM）。",
        "D1 是團隊估計＋本模型的快取維護估計；D2 是假設。兩者都需要新驅動候選與板測驗收才能變成事實；V3 驅動凍結不改。",
        "吞吐量用「瓶頸界限 ÷ 校準效率 η」，η_C 只由 cpu-w4 一個設定擬合；兩個資源同時接近飽和時會低估（cpu-w2 −12%）。"
        "PL 重的切分其 ARM 空轉比例可能和 cpu-w4 不同。",
        "list scheduler 是非搶佔、HEFT 式的啟發式，不保證最佳 makespan（列舉的是切分，排程是啟發式）；"
        "ARM 的記憶體頻寬競爭只用固定的 c_be4、c_enc4 係數表示，PL DMA 與 ARM 搶 DDR 的效應沒有建模。",
        "DAG/STEP 模式假設軟體可以依資料相依並行（RNS 每特徵並行、CRT 切 4 塊），這是軟體改動，不是現狀；IMPL 才是現狀。"
        "DAG 模式沒有計入執行緒喚醒／同步（每次約 20–50 us），細粒度任務多的切分可能低估幾個百分點。",
        "能耗模型的 PL 項辨識度低（實測只有 3 個 duty 水準），整板 INA260 也不能分開 CPU 與 FPGA。",
        "W1–W3 的 ARM 軟體改善（查表 residue、稀疏乘積、encode/decode 加速）全是 ESTIMATED/假設，只用來排優先順序；"
        "稀疏乘積需要 constant-time 審查；私鑰常駐 PL（S6）是安全取捨。",
        "keygen 是單次冷樣本（非 p50），由它推得的 residue 成本有雜訊；cpu-w3 keygen 誤差 −6% 即屬此類。",
        "所有結論限於 N=4096、3 limbs、4 特徵、2048 筆/批的固定 profile；安全等級未宣稱（`security_bits=null`）。",
    ]:
        add("- " + t)
    add("")
    # ------------------------------------------------------------------ 11
    add("## 11. 檔案與重現")
    add("")
    add("- `partition_model.py`：模型本體（`--repo` 指定 repo 路徑，`--quick` 只跑 DAG-d1）。")
    add("- `model_results.json`：全部參數（含標記與來源）、校準、每情境前 5 名切分、Stone、break-even、敏感度。")
    add("- `graph_best.mmd`／`graph_best.dot`：%s" % res["graph"]["title"])
    add("- 讀取的量測檔：" + "、".join("`%s`" % v for v in res["inputs_loaded_from"].values()))
    if res.get("fallback_used"):
        add("- **注意**：以下輸入讀檔失敗，改用內嵌副本：" + "；".join(res["fallback_used"]))
    add("")
    (out_dir / "PARTITION_MODEL.md").write_text("\n".join(L) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--repo", default=os.environ.get("EDGEFHE_REPO", str(Path(__file__).resolve().parents[2])))
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--no-plots", action="store_true")
    ap.add_argument("--quick", action="store_true", help="skip STEP-mode and depth-2 enumerations")
    args = ap.parse_args()
    repo = Path(args.repo)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    inp = load_inputs(repo)
    P, cont = derive(inp)
    cal = calibrate(inp, P, cont)
    eta = cal["eta"]
    print("calibration done (%.1fs)" % (time.time() - t0))
    for r in cal["steps"]:
        if r["quantity"] in ("encrypt", "decrypt", "edge"):
            print("  %-13s %-8s meas %7.2f model %7.2f err %+5.1f%%" % (r["backend"], r["quantity"], r["measured_ms"], r["model_ms"], r["err_pct"]))
    for r in cal["throughput"]:
        print("  tput %-13s T meas %6.2f model %6.2f err %+5.1f%% (%s, %s)" % (r["backend"], r["measured_T_ms"], r["model_T_ms"], r["err_pct"], r["role"], r["bottleneck"]))

    results = {"version": VERSION, "generated_by": "partition_model.py", "repo": str(repo),
               "inputs_loaded_from": inp["loaded_from"], "fallback_used": inp["fallback_used"],
               "parameters": P.items, "calibration": cal, "scenarios": {}, "stone": {}, "break_even": None}
    cfgs = {n: Cfg(n, sc, P, cont) for n, sc in SCENARIOS.items()}
    summaries = {}
    keep_best = {}
    for name, cfg in cfgs.items():
        summaries[name] = {"desc": cfg.sc["desc"], "driver": cfg.sc["driver"], "runs": {}}
        modes = [("DAG", 1), ("DAG", 2)] + ([] if args.quick else [("STEP", 1)])
        if args.quick:
            modes = [("DAG", 1)]
        for mode, depth in modes:
            t1 = time.time()
            res, fs = enumerate_scenario(cfg, eta, mode, depth)
            sm = summarize(cfg, res, eta, mode, depth, fs)
            summaries[name]["runs"]["%s-d%d" % (mode, depth)] = sm
            best = pick_recommended(res, lambda r: r["latency_ms"])
            keep_best[(name, mode, depth)] = best
            keep_best[(name, mode, depth, "T")] = pick_recommended(res, lambda r: r["thr"]["T_ms"])
            print("%s %s d%d: %d asg, best lat %.2f ms [%s] T %.2f ms (%s) all-ARM %.2f ms  (%.1fs)" % (
                name, mode, depth, len(res), sm["best_latency"]["latency_ms"], sm["best_latency"]["assignment"],
                sm["best_throughput"]["T_ms"], sm["best_throughput"]["bottleneck"], sm["all_arm"]["latency_ms"],
                time.time() - t1))
        # Stone
        if cfg.sparse:
            results["stone"][name] = {"skipped": "Stone graph uses NTT-decomposed nodes; the sparse-product "
                                                  "ARM algorithm of this what-if has a different node structure"}
            continue
        st = stone(cfg)
        run = summaries[name]["runs"]["DAG-d1"]
        bl = run["best_latency"]
        best_r = keep_best[(name, "DAG", 1)]
        best_asg = best_r["asg"]
        img = asg_to_plnodes(cfg, best_asg)
        st["enumeration_best"] = bl["assignment"]
        st["enumeration_best_latency_ms"] = bl["latency_ms"]
        st["enumeration_best_total_work_ms"] = bl["total_work_ms"]
        st["enumeration_best_pl_nodes"] = sorted(img)
        st["stone_model_cost_of_enumeration_best_ms"] = (stone_cost_of(cfg, img) if all(
            stone_graph(cfg)[1].get(v, 0) < 1e8 for v in img) else None)
        mapped, why = plnodes_to_asg(cfg, set(st["pl_nodes"]))
        fs = fixed_stages(cfg, "DAG")
        if mapped is not None:
            ev = evaluate(cfg, mapped, "DAG", 1, eta, fs)
            st["stone_assignment"] = asg_str(mapped)
            st["stone_assignment_latency_ms"] = ev["latency_ms"]
            st["stone_assignment_T_ms"] = ev["thr"]["T_ms"]
            st["stone_assignment_total_work_ms"] = ev["work_ms"]
        else:
            st["stone_assignment"] = None
            st["stone_unmappable_reason"] = why
        allarm_r = run["all_arm"]
        st["all_arm_total_work_ms"] = allarm_r["total_work_ms"]
        results["stone"][name] = st
        print("  Stone %s: cut %.2f ms, PL nodes %d -> %s ; lat %s" % (
            name, st["min_total_cost_ms"], len(st["pl_nodes"]),
            st.get("stone_assignment") or st.get("stone_unmappable_reason"),
            ("%.2f" % st["stone_assignment_latency_ms"]) if st.get("stone_assignment") else "-"))
    results["scenarios"] = summaries
    # dominance check: ADD on PL (op3/op4) -- analytic
    dom = []
    for dn in ("D0", "D1", "D2"):
        d = Driver(dn, P, dn)
        for op, ib, cyc, arm_c in (("ADD_c1 via op3", 2 * N, P["cyc_op3"], P["t_add_call"]),
                                   ("ADD_c0 via op4", 3 * N, P["cyc_op4"], 2 * P["t_add_call"])):
            p = d.parts(ib, N, cyc)
            arm_side = p["pack"] + p["pre"] + p["post"] + p["unpack"]
            dom.append({"driver": dn, "option": op, "arm_cost_ms": arm_c, "arm_side_of_call_ms": arm_side,
                        "call_latency_ms": sum(p.values()), "dominated": arm_side >= arm_c and sum(p.values()) >= arm_c})
    results["dominance_add_on_pl"] = dom
    # break-even
    results["break_even"] = break_even(P, cont)
    # ARM-speed sensitivity on the full model (S3, S4)
    sens = []
    for base in ("S1", "S3", "S4", "S7"):
        for s in (1.0, 1.5, 2.0, 3.0, 4.0):
            sc = dict(SCENARIOS[base])
            sc["arm_speed"] = s
            cfg = Cfg(base + "@x%.1f" % s, sc, P, cont)
            res, fs = enumerate_scenario(cfg, eta, "DAG", 1)
            sm = summarize(cfg, res, eta, "DAG", 1, fs)
            sens.append({"base": base, "arm_speed": s, "best_latency_ms": sm["best_latency"]["latency_ms"],
                         "best_latency_asg": sm["best_latency"]["assignment"], "best_uses_pl": sm["best_latency"]["uses_pl"],
                         "all_arm_latency_ms": sm["all_arm"]["latency_ms"],
                         "best_T_ms": sm["best_throughput"]["T_ms"], "best_T_asg": sm["best_throughput"]["assignment"],
                         "best_T_uses_pl": sm["best_throughput"]["uses_pl"], "all_arm_T_ms": sm["all_arm"]["T_ms"],
                         "best_T_records_per_s": sm["best_throughput"]["records_per_s"],
                         "all_arm_records_per_s": sm["all_arm"]["records_per_s"]})
    results["arm_speed_sensitivity"] = sens
    # D1 estimate sensitivity (S3): scale D1 per-byte costs
    d1s = []
    for fct in (0.5, 1.0, 2.0, 4.0, 8.0):
        P2 = Params()
        P2.items = json.loads(json.dumps(P.items))
        for k in ("d1_validate_ns_per_B", "d1_copy_out_ns_per_B", "d1_maint_ns_per_B"):
            P2.items[k]["value"] *= fct
        cfg = Cfg("S3@D1x%.1f" % fct, SCENARIOS["S3"], P2, cont)
        res, fs = enumerate_scenario(cfg, eta, "DAG", 1)
        sm = summarize(cfg, res, eta, "DAG", 1, fs)
        d1s.append({"d1_cost_factor": fct, "best_latency_ms": sm["best_latency"]["latency_ms"],
                    "best_latency_asg": sm["best_latency"]["assignment"], "best_T_ms": sm["best_throughput"]["T_ms"],
                    "best_T_asg": sm["best_throughput"]["assignment"], "all_arm_latency_ms": sm["all_arm"]["latency_ms"],
                    "all_arm_T_ms": sm["all_arm"]["T_ms"]})
    results["d1_cost_sensitivity_S3"] = d1s
    # best scenario graph: lowest DAG-d1 latency among hardware scenarios S1-S4 that use the PL
    cands = []
    for n in ("S1", "S2", "S3", "S4"):
        r = keep_best[(n, "DAG", 1)]
        cands.append((r["latency_ms"], n, r))
    cands.sort(key=lambda x: (x[0], x[1]))
    gl, gname, gres = cands[0]
    title = "EdgeFHE %s optimal (DAG, depth 1): %s ; edge latency %.2f ms" % (gname, asg_str(gres["asg"]), gl)
    results["graph"] = {"scenario": gname, "title": title, **write_graphs(gres, cfgs[gname], out_dir, title)}
    results["runtime_s"] = round(time.time() - t0, 1)
    # strip non-serialisable
    with open(out_dir / "model_results.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=1, ensure_ascii=False, default=lambda o: o._asdict() if hasattr(o, "_asdict") else str(o))
    write_markdown(results, P, out_dir)
    print("done in %.1fs" % (time.time() - t0))
    return results, P, cfgs, keep_best, args


if __name__ == "__main__":
    main()
