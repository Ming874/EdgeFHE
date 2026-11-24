"""Builds hls_stage_estimates.json (same folder) from the loop table below and the schedule model (v8b_sched_model.py).
usage: python -I gen_outputs.py   (reads sweep_k8.json from this folder; the MEASURED board numbers are the M_* constants)"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from v8b_sched_model import run, CENTRAL, II_ONLY

# ---- MEASURED board numbers (V8b rev5, 2026-11-23, k = 8 per call) ----
# The driver phases are the last call of rep 1 of the 10-run V8b-vs-OpenFHE comparison (board_enc_check sustain 30 8);
# that call has the lowest dma_hls of the ten runs' last calls. The sustained figures are the means of those ten runs.
M_SRC_CALL = "10-run V8b comparison (2026-11-23), rep 1, last call of board_enc_check sustain 30 8"
M_DMA_HLS = 1075.3                                  # us, dma_hls of that call
M_PHASES = {"copy_in_us": 80.4, "check_in_us": 148.5, "sync_device_us": 63.9, "sync_cpu_us": 63.3,
            "check_out_us": 298.8, "copy_out_us": 217.3, "wipe_us": 272.6}
M_DMA_HLS_RANGE = [1075.3, 1126.8]                  # us, dma_hls of the last call of each of the ten V8b runs
M_PARTITION_DMA_HLS = 1074.4                        # us, partition campaign (2026-11-23), sustain k = 8, last call
M_SUSTAINED_CT = 104982                             # mean ciphertexts of the ten V8b runs (104,982.4)
M_SUSTAINED_S = 30.0                                # each run 30 s (mean 30.0008 s)
M_IOCTL_MS = 2.286                                  # mean of the ten runs' mean ioctl time (2.28625 ms)

MHZ = 200
us = lambda c: round(c / MHZ, 2)
P = CENTRAL
C = P["chacha"]                                   # one chacha8() call (8 blocks)
TB = 511 * 2 + P["d_tern"] + P["ovh"]             # tern_bytes: 512 iterations, II=2
G = 63 + P["d_gauss"] + P["ovh"]                  # gauss_samples: 64 iterations, II=1
UL = 1023 + P["d_uload"] + P["ovh"]
ST = P["stage"]                                   # one ct_stage / gs_stage call
PW = P["pointwise"]
SO = 1023 + P["d_store"] + P["ovh"]

r = run(8, CENTRAL); r0 = run(8, II_ONLY)
period = round(r["steady_period_cycles"]); period0 = round(r0["steady_period_cycles"])
frame = r["s2mm_last_beat"][0]; frame0 = r0["s2mm_last_beat"][0]
u_steady = period - (r["store1_end"][3] - r["u_load_end"][3]) - P["loop_ovh"]
tail = frame - r["store1_end"][-1]
sweep = json.load(open(os.path.join(HERE, "sweep_k8.json")))
lim = [x for x in sweep if x["limiter"] == "limb"]
by_k = {str(k): round(run(k, CENTRAL)["s2mm_last_beat"][0] / MHZ, 1) for k in range(1, 9)}
by_k0 = {str(k): round(run(k, II_ONLY)["s2mm_last_beat"][0] / MHZ, 1) for k in range(1, 9)}
slope = (by_k["8"] - by_k["1"]) / 7
# exposed output tail when the S2MM takes 16 beats and then idles s2mm_gap cycles: 80 %, 67 % and 50 % of 1 beat/cycle
s2mm_tail = {pct: (run(8, dict(CENTRAL, s2mm_gap=gap))["s2mm_last_beat"][0] - frame) / MHZ for pct, gap in ((80, 4), (67, 8), (50, 16))}
# grid points of the sweep that the measurement excludes (PL-only frame above the MEASURED dma_hls)
excl = [x for x in sweep if x["frame_us"] > M_DMA_HLS]
excl_chacha = min(x["chacha"] for x in excl)
assert all(x["frame_us"] > M_DMA_HLS for x in sweep if x["chacha"] >= excl_chacha), "exclusion is not a chacha8 threshold"
assert [x for x in sweep if x["frame_us"] > M_PARTITION_DMA_HLS] == excl, "the partition-campaign value excludes other points"
gap_us = M_DMA_HLS - frame / MHZ
gap_part_us = M_PARTITION_DMA_HLS - frame / MHZ
sus_ct_per_s = M_SUSTAINED_CT / M_SUSTAINED_S

def mod(id_, name, process, cyc, cyc_ii, source, sources, loops, overlapped, critical, extra=None):
    d = {"id": id_, "name": name, "process": process, "cycles_per_ct": cyc, "us_per_ct": us(cyc), "label": "ESTIMATED",
         "source": source, "cycles_per_ct_ii_only": cyc_ii, "us_per_ct_ii_only": us(cyc_ii),
         "cycles_per_frame_k8": 8 * cyc, "us_per_frame_k8": us(8 * cyc), "loops": loops, "overlapped_with": overlapped,
         "on_critical_path": critical, "sources": sources}
    if extra: d.update(extra)
    return d

modules = [
    mod("rns_message", "read message (int64 in) and reduce m to 3 limbs (residue64)", "reader", 4096, 4096,
        "v8b.cpp:253-270", ["v8b.cpp:253-270", "v8b.cpp:52-60", "v8b.cpp:41-51", "v8b.cpp:621-623"],
        [{"loop": "message_coeffs", "lines": "v8b.cpp:253-270", "trip_per_ct": 4096, "ii": 1,
          "note": "one coefficient per iteration, one 128-bit input beat every 2 iterations, one m token per limb every 4"}],
        ["sampler (whole ciphertext)", "limb engines (NTT/INTT of the previous ciphertext)"], False,
        {"pipeline_depth_once_per_frame_cycles": P["d_msg"],
         "comment": "read and reduce are one fused loop; the m0-m2 FIFOs (depth 1024 = one polynomial) let the reader run one ciphertext ahead; in the model it is blocked ~18k cycles per ciphertext"}),
    mod("prng_mask", "ChaCha20 keystream for the ternary mask u (8-block interleaved core)", "sampler", 2 * C + 3, 2 * 88,
        "v8b.cpp:308-312", ["v8b.cpp:308-312", "v8b.cpp:127-159", "v8b.cpp:142-158"],
        [{"loop": "chacha8/cycles", "lines": "v8b.cpp:142-158", "trip_per_call": 88, "ii": 1, "calls_per_ct": 2,
          "note": "80 cycles for 10 double rounds of 8 blocks + 8 output cycles; TRIPCOUNT 2 batches (v8b.cpp:310)"}],
        ["nothing inside the sampler (sequential with the byte decode)"], "partly",
        {"chacha8_call_cycles_assumed": C,
         "comment": "call = ~19-cycle preamble (16 auto-pipelined init sub-loops, v8b.cpp:133-139, assumed concurrent) + 88-iteration core + ~5 control; the second call lies inside the window where u_load waits for the sampler"}),
    mod("sample_ternary", "ternary decode, 5 trits per accepted byte (< 243)", "sampler", 2 * TB, 2 * 1024,
        "v8b.cpp:313-326", ["v8b.cpp:313-326", "v8b.cpp:308-311"],
        [{"loop": "tern_bytes", "lines": "v8b.cpp:313-326", "trip_per_call": 512, "ii": 2, "calls_per_ct": 2,
          "note": "II=2 because one byte can emit two 4-trit tokens to the same FIFO; the loop always runs 512 iterations"}],
        ["limb u_load (lockstep through us0-us2, depth 32)", "32 tokens of head start overlap the previous store p=1"], True,
        {"comment": "1024 tokens need ~864 bytes (820 accepted): batch 2 is needed up to byte ~352; the decoder makes ~0.59 tokens/cycle and the 32-deep u FIFO limits the head start to 32 tokens"}),
    mod("prng_gauss", "ChaCha20 keystreams for e0 and e1 (2 x 512 blocks)", "sampler", 128 * (C + 1), 128 * 88,
        "v8b.cpp:329-334", ["v8b.cpp:329-334", "v8b.cpp:127-159", "sampler_b.hpp:25"],
        [{"loop": "gauss_batches -> chacha8", "lines": "v8b.cpp:331-334", "trip_per_ct": 128, "ii": None,
          "note": "2 polys x 64 batches, LOOP_FLATTEN off; each batch = one 8-block chacha8 call (88-cycle core)"}],
        ["limb NTT / pointwise / INTT of the same ciphertext (e FIFOs depth 1024 hold a whole polynomial)"], False,
        {"chacha8_call_cycles_assumed": C, "comment": "largest single sampler item; not overlapped with CDT63 (sequential in the batch loop)"}),
    mod("sample_cdt63", "constant-time CDT63 Gaussian sampling for e0, e1", "sampler", 128 * G, 128 * 64,
        "v8b.cpp:335-345", ["v8b.cpp:335-345", "v8b.cpp:160-169", "cdt63_table.hpp:5"],
        [{"loop": "gauss_samples", "lines": "v8b.cpp:335-345", "trip_per_call": 64, "ii": 1, "calls_per_ct": 128,
          "note": "one 64-bit keystream word per coefficient, 42 parallel comparisons, one 4-coefficient token every 4 iterations"}],
        ["limb NTT / pointwise / INTT of the same ciphertext"], False),
    mod("rns_small", "residues of u (and e0, e1) into the limbs", "limb x3", UL, 1024,
        "v8b.cpp:506-514", ["v8b.cpp:506-514", "v8b.cpp:61-64", "v8b.cpp:533"],
        [{"loop": "u_load", "lines": "v8b.cpp:506-514", "trip_per_ct": 1024, "ii": 1, "note": "trit code -> {0, 1, q-1} for 4 lanes"},
         {"loop": "store (fused)", "lines": "v8b.cpp:533", "trip_per_ct": 0, "ii": None,
          "note": "residue_small(e) is inside the store loop: 0 extra cycles"}],
        ["ternary sampler (lockstep)", "writer draining c1 of the previous ciphertext"], True,
        {"effective_cycles_per_ct_in_schedule": u_steady, "effective_us_per_ct_in_schedule": us(u_steady),
         "comment": "the loop itself is ~1028 cycles; in steady state it lasts ~%d cycles because it waits for the ternary sampler" % u_steady}),
    mod("ntt_mask", "forward NTT of u (12 CT stages, one engine per limb, 3 in parallel)", "limb x3", 12 * ST, 12 * 512,
        "v8b.cpp:378-413", ["v8b.cpp:374-414", "v8b.cpp:456-458", "v8b.cpp:515"],
        [{"loop": "ct_cycle", "lines": "v8b.cpp:378-413", "trip_per_call": 512, "ii": 1, "calls_per_ct": 12,
          "note": "4 butterflies per cycle; stage latency ~%d = 511 + ~47 depth + ~3 control" % ST}],
        ["sampler e0/e1 generation", "reader intake of the next message", "writer draining c1 of the previous ciphertext (first ~250 cycles)"], True,
        {"pipeline_depth_assumed": 47,
         "comment": "depth dominated by the urem(15-stage) -> sdiv(18-stage) chain of the twiddle index (v8b.cpp:385-387; hierarchical.rpt:316-321)"}),
    mod("pointwise", "pointwise products with the cached public key (b*u, a*u)", "limb x3", 2 * PW, 2 * 1024,
        "v8b.cpp:518-522", ["v8b.cpp:516-522", "v8b.cpp:466-477"],
        [{"loop": "pointwise", "lines": "v8b.cpp:518-522", "trip_per_call": 1024, "ii": 1, "calls_per_ct": 2,
          "note": "4 Montgomery products per cycle against cb / ca in URAM"}],
        ["sampler", "writer (c0 drain during p=1)"], True),
    mod("intt", "two inverse NTTs (b*u, a*u), 24 GS stages", "limb x3", 24 * ST, 24 * 512,
        "v8b.cpp:419-454", ["v8b.cpp:415-455", "v8b.cpp:459-461", "v8b.cpp:523"],
        [{"loop": "gs_cycle", "lines": "v8b.cpp:419-454", "trip_per_call": 512, "ii": 1, "calls_per_ct": 24,
          "note": "stage latency ~%d; depth dominated by udiv_28ns_17ns (32-stage) of the twiddle index (v8b.cpp:426-428; hierarchical.rpt:349-351)" % ST}],
        ["sampler e0/e1 generation", "writer draining c0", "reader intake"], True, {"pipeline_depth_assumed": 47}),
    mod("add", "assembly c0 = b*u + e0 + m, c1 = a*u + e1 (store)", "limb x3", 2 * SO, 2 * 1024,
        "v8b.cpp:524-537", ["v8b.cpp:524-537"],
        [{"loop": "store", "lines": "v8b.cpp:524-537", "trip_per_call": 1024, "ii": 1, "calls_per_ct": 2,
          "note": "reads one e token (4 coefficients) and, for p=0, one m token per cycle; writes one 4-coefficient token to o_L"}],
        ["writer (same-cycle drain of o0-o2)", "sampler"], True),
    mod("write_output", "write the output: 2 polys x 3 limbs x 4096 words as 128-bit beats", "writer", 6144, 6144,
        "v8b.cpp:571-579", ["v8b.cpp:571-579", "v8b.hpp:8", "v8b.cpp:630-632"],
        [{"loop": "data", "lines": "v8b.cpp:571-579", "trip_per_ct": 6144, "ii": 1,
          "note": "beat = ap_axiu<128>: 4 x 32-bit coefficients of one limb; 6144 beats = 98,304 B per ciphertext"}],
        ["limb engines (o0-o2 depth 1024 hold one store's output per limb)"], "only at frame end",
        {"exposed_cycles_per_frame": tail, "exposed_us_per_frame": us(tail),
         "comment": "needs >= 0.35 beat/cycle during the 8.8k cycles between the two stores of a ciphertext; only the final ~2048-beat drain is on the critical path"}),
]

fixed = [
    {"id": "header_seed_control", "what": "header beat, 2 seed beats, control tokens c0-c2/cw/cs and the two zero tokens of cs", "cycles": 6,
     "exposed": True, "label": "ESTIMATED", "source": "v8b.cpp:180-209"},
    {"id": "sampler_key_load", "what": "seed -> key registers", "cycles": P["key_loop"], "exposed": True, "label": "ESTIMATED", "source": "v8b.cpp:296-298"},
    {"id": "first_ciphertext_fill", "what": "first mask has no 32-token head start; first input beat latency (assumed 60 cycles)",
     "cycles": r["u_load_end"][0] - u_steady, "exposed": True, "label": "ESTIMATED",
     "source": "v8b.cpp:308-326, v8b.cpp:506-514"},
    {"id": "output_tail_drain", "what": "writer drains the last c1 (2048 of 3072 beats left when the last store ends) and writes the trailer",
     "cycles": tail, "us": us(tail), "exposed": True, "label": "ESTIMATED", "source": "v8b.cpp:571-588"},
    {"id": "message_scrub", "what": "1024 zero tokens into m0-m2", "cycles": 1024 + P["ovh"] + 1, "exposed": False,
     "label": "ESTIMATED", "source": "v8b.cpp:272-276", "note": "runs while the last ciphertext is processed"},
    {"id": "sampler_rev5_scrub", "what": "zero key, zero-key ChaCha20 batch + RFC 8439 check, noise_scrub (1024 x 6 e FIFOs, 32 x 3 u FIFOs)",
     "cycles": P["key_loop"] + C + P["kat_cmp"] + 1024 + P["ovh"] + 1, "exposed": False, "label": "ESTIMATED",
     "source": "v8b.cpp:349-361", "note": "finishes ~590 cycles after the last store, before the writer reads kat"},
    {"id": "limb_wipe_scrub", "what": "wipe of A/B/C buffers + scrub_enc consuming the zero tokens", "cycles": P["wipe"] + 1024 + 2 + P["ovh"],
     "exposed": False, "label": "ESTIMATED", "source": "v8b.cpp:540-551",
     "note": "parallel with the output tail; sets ap_done at about the same time as the last output beat, not part of the S2MM completion"},
    {"id": "reader_drain", "what": "drain of extra input beats", "cycles": 0, "exposed": False, "label": "ESTIMATED", "source": "v8b.cpp:278-282"},
    {"id": "public_key", "what": "no per-frame key work: cb/ca cached in URAM by LOAD (command 6)", "cycles": 0, "exposed": False,
     "label": "ESTIMATED", "source": "v8b.cpp:466-501",
     "note": "LOAD itself ~2 x (1024 + 12 x %d + 1024) cycles (~%.0f us) once per key lease; not in this measurement" % (ST, 2 * (1024 + 12 * ST + 1039) / MHZ)},
    {"id": "driver_dma_programming_and_irq_wake", "what": "dmaengine issue_pending (RX, TX) before ap_start; S2MM IOC interrupt -> callback -> thread wake",
     "cycles": None, "exposed": True, "label": "NOT DETERMINABLE FROM SOURCE", "source": "ef_v8b_main.c:315-320, 366-368"},
]
fixed_total = r["u_load_end"][0] - u_steady + tail

sampler_ct = 2 * C + 3 + 2 * TB + 128 * (C + 1) + 128 * G + P["loop_ovh"]
limb_ct = UL + 12 * ST + 2 * PW + 24 * ST + 2 * SO + P["loop_ovh"]
out = {
    "schema": "edgefhe.hls_stage_estimates/1", "date": "2026-11-23",
    "design": "EdgeFHE V8b rev5 HLS kernel edgefhe_v8b, ENC (command 9), v8b_rev5/hardware/hls/v8b.cpp",
    "clock_mhz": MHZ,
    "label_legend": {"ESTIMATED": "derived from the HLS source (trip counts x II from the pragmas, plus pipeline depths inferred from the routed hierarchy report) and the schedule model",
                     "MEASURED": "board numbers measured by the team (V8b rev5, 2026-11-23, k = 8): the driver phases of one call (" + M_SRC_CALL
                                 + ") and the means of the ten 30-s V8b runs of that comparison"},
    "method": ("cycles = trip count x II from the pragmas (II-only lower bound) plus pipeline fill/drain and call overhead; depths are inferred from the "
               "core latencies visible in hardware/reports/rev5/hierarchical.rpt (no csynth/cosim report is in the repository); the DATAFLOW "
               "interaction is replayed by a token-level model (energy_probe/kv260/partition/hls_model/v8b_sched_model.py) with the FIFO depths of v8b.cpp:609-632"),
    "frame": {"n": 4096, "limbs": 3, "lanes_per_limb": 4, "k": 8, "beat_bits": 128,
              "input_beats_k8": 1 + 2 + 2048 * 8, "input_bytes_k8": 16 * (1 + 2 + 2048 * 8),
              "output_beats_k8": 1 + 6144 * 8 + 1, "output_bytes_k8": 16 * (1 + 6144 * 8 + 1), "label": "ESTIMATED",
              "check": "agrees with MEASURED tx 262,192 B and rx 786,464 B",
              "source": "v8b.hpp:8, v8b.cpp:180-203, v8b.cpp:254-262, v8b.cpp:565-588, edgefhe_v8_policy.h:23-24"},
    "modules": modules,
    "process_totals_per_ct": {
        "sampler": {"cycles": sampler_ct, "us": us(sampler_ct), "cycles_ii_only": 2 * 88 + 2 * 1024 + 128 * 88 + 128 * 64,
                    "us_ii_only": us(2 * 88 + 2 * 1024 + 128 * 88 + 128 * 64), "label": "ESTIMATED", "source": "v8b.cpp:291-372",
                    "chacha20_share": round((2 * C + 128 * C) / sampler_ct, 3)},
        "limb_engine_own_work": {"cycles": limb_ct, "us": us(limb_ct), "cycles_ii_only": 1024 + 6144 + 2048 + 12288 + 2048,
                                 "us_ii_only": us(1024 + 6144 + 2048 + 12288 + 2048), "label": "ESTIMATED", "source": "v8b.cpp:463-559",
                                 "note": "per engine; the three engines (one per limb) run in parallel and in lockstep"},
        "limb_engine_in_schedule": {"cycles": period, "us": us(period), "label": "ESTIMATED", "note": "own work + waiting for the ternary sampler in u_load"},
        "reader": {"cycles": 4096, "us": us(4096), "label": "ESTIMATED", "source": "v8b.cpp:253-270"},
        "writer": {"cycles": 6144, "us": us(6144), "label": "ESTIMATED", "source": "v8b.cpp:571-579"}},
    "per_frame_fixed": fixed,
    "per_frame_fixed_exposed_total": {"cycles": fixed_total, "us": us(fixed_total), "label": "ESTIMATED",
                                      "note": "PL only; plus driver DMA programming and IRQ/wake, not determinable from source"},
    "dataflow": {
        "processes": "reader | sampler | limb<0> | limb<1> | limb<2> | writer (v8b.cpp:601, 645-650); every channel is an hls::stream FIFO, no ping-pong buffers",
        "fifos": {"c0-c2, cw, cs": "depth 2/2/2/3/2 (control)", "s0-s2": "32 (PAIR only)", "us0-us2": "32 mask tokens (128 coefficients)",
                  "m0-m2": "1024 x 128 bit = one message polynomial per limb (URAM)", "e00-e12": "1024 x 32 bit = one e polynomial per FIFO (URAM)",
                  "o0-o2": "1024 x 128 bit = one store's output per limb (URAM)", "source": "v8b.cpp:602-644"},
        "overlap_across_ciphertexts": [
            "sampler: e0(f), e1(f) are generated while the limb engines run NTT/pointwise/INTT of f (e FIFOs hold a whole polynomial)",
            "sampler: the mask of f+1 can run only 32 tokens ahead (us FIFO depth 32); the rest of ternary(f+1) is in lockstep with u_load(f+1)",
            "reader: m(f+1) enters while store p=0 of f drains m(f) (m FIFO = exactly one polynomial)",
            "writer: c0(f) drains during pointwise/INTT p=1 of f; c1(f) drains during u_load(f+1) and the start of NTT(f+1)",
            "inside a limb engine: u_load, 12 CT stages, then (pointwise, 12 GS stages, store) x 2 run strictly one after another (no DATAFLOW inside limb)"],
        "steady_state_timeline_cycles_rel_end_of_u_load_f": {
            "limb": {"ntt": [0, 12 * ST], "pointwise_p0": [12 * ST, 12 * ST + PW], "intt_p0": [12 * ST + PW, 24 * ST + PW],
                     "store_p0": [r["store0_start"][3] - r["u_load_end"][3], r["store0_start"][3] - r["u_load_end"][3] + SO],
                     "store_p1": [r["store1_start"][3] - r["u_load_end"][3], r["store1_end"][3] - r["u_load_end"][3]],
                     "u_load_next": [r["store1_end"][3] - r["u_load_end"][3] + P["loop_ovh"], period]},
            "sampler": {"e0_done": r["gauss_end"][3 * 128 + 63] - r["u_load_end"][3], "e1_done": r["gauss_end"][3 * 128 + 127] - r["u_load_end"][3],
                        "next_mask_batch_ends": [x - r["u_load_end"][3] for x in r["tern_end"][8:10]]},
            "label": "ESTIMATED (model, ciphertext f = 3, k = 8)"}},
    "bottleneck": {
        "label": "ESTIMATED",
        "stage": "limb engine chain: u_load (waiting for the ternary sampler) -> 12 CT stages -> [pointwise -> 12 GS stages -> store] x 2; three engines in lockstep",
        "process": "limb<0>, limb<1>, limb<2>",
        "cycles_per_ct": period, "us_per_ct": us(period),
        "composition_cycles_per_ct": {"u_load_incl_wait": u_steady, "ntt": 12 * ST, "pointwise_x2": 2 * PW, "intt_x2": 24 * ST, "store_add_x2": 2 * SO,
                                      "loop_control": period - u_steady - 36 * ST - 2 * PW - 2 * SO},
        "butterfly_issue_cycles_per_ct": 36 * 512, "ntt_intt_fill_drain_cycles_per_ct": 36 * (ST - 512),
        "runner_up": {"process": "sampler", "cycles_per_ct": sampler_ct, "us_per_ct": us(sampler_ct),
                      "slack_cycles_per_ct": round(r["stalls"].get("sampler/tern_bytes", 0) / 8),
                      "note": "co-critical: the sampler becomes the bottleneck when one chacha8 call costs more than ~115 cycles (372 slack cycles / 130 calls per ciphertext; model sweep)"},
        "sensitivity": {"limb_bound_region_us_per_ct": [round(min(x["period"] for x in lim) / MHZ, 1), round(max(x["period"] for x in lim) / MHZ, 1)],
                        "limb_bound_region_frame_us": [round(min(x["frame_us"] for x in lim), 1), round(max(x["frame_us"] for x in lim), 1)],
                        "grid": "ct/gs stage call 551-571 cycles, chacha8 call 96-124 cycles; sweep_k8.json",
                        "measurement_constraint": ("parameter sets whose PL-only frame exceeds the MEASURED %.1f us (every grid point with a chacha8 call >= %d cycles) are excluded; "
                                                   "the partition campaign's %.1f us excludes the same points") % (M_DMA_HLS, excl_chacha, M_PARTITION_DMA_HLS)}},
    "predicted_us_per_ct": us(period),
    "predicted_us_per_ct_ii_only_bound": us(period0),
    "predicted_us_per_frame_k8": round(frame / MHZ, 1),
    "predicted_us_per_frame_k8_ii_only_bound": round(frame0 / MHZ, 1),
    "predicted_us_per_frame_k8_scope": "PL only: ap_start to the last output beat accepted by the S2MM; excludes DMA programming before ap_start and the IRQ/wake after completion",
    "predicted_pl_us_by_k": by_k, "predicted_pl_us_by_k_ii_only": by_k0,
    "predicted_pl_linear_fit": {"intercept_us": round(by_k["1"] - slope, 1), "slope_us_per_ct": round(slope, 2), "label": "ESTIMATED"},
    "predicted_mm2s_last_input_beat_us_k8": round(r["message_coeffs_end"][0] / MHZ, 1),
    "measured_dma_hls_us_per_frame_k8": M_DMA_HLS,
    "measured_dma_hls_us_per_ct_k8": round(M_DMA_HLS / 8, 2),
    "measured": {"label": "MEASURED", "k": 8, "dma_hls_us": M_DMA_HLS, "source": M_SRC_CALL, **M_PHASES,
                 "dma_hls_last_calls_of_ten_runs_us": M_DMA_HLS_RANGE,
                 "dma_hls_partition_campaign_us": M_PARTITION_DMA_HLS,
                 "dma_hls_partition_campaign_source": "partition campaign (2026-11-23), board_enc_check sustain 15 8, last call",
                 "comparison_note": ("the model is compared with the lowest single-call values: dma_hls also contains driver work (DMA programming "
                                     "before ap_start, interrupt and thread wake after the S2MM completes) that varies from call to call"),
                 "ioctl_total_ms": M_IOCTL_MS, "ioctl_total_source": "mean of the ten V8b runs' mean ioctl time (2.28625 ms)",
                 "tx_bytes": 262192, "rx_bytes": 786464, "sustained_ct": M_SUSTAINED_CT, "sustained_s": M_SUSTAINED_S,
                 "sustained_source": "mean of the ten V8b runs (104,982.4 ciphertexts in 30.0008 s)",
                 "derived": {"sustained_ct_per_s": round(sus_ct_per_s, 1), "sustained_us_per_ct": round(M_SUSTAINED_S * 1e6 / M_SUSTAINED_CT, 1),
                             "sustained_ms_per_k8_frame": round(8 * M_SUSTAINED_S * 1e3 / M_SUSTAINED_CT, 3),
                             "pl_busy_fraction_of_ioctl": round(M_DMA_HLS / (M_IOCTL_MS * 1000), 3),
                             "label": "MEASURED (derived)"}},
    "gap": {"us_per_frame": round(gap_us, 1), "fraction": round(gap_us / M_DMA_HLS, 3),
            "us_per_ct": round(gap_us / 8, 2), "label": "MEASURED minus ESTIMATED",
            "partition_campaign": {"measured_us": M_PARTITION_DMA_HLS, "us_per_frame": round(gap_part_us, 1),
                                   "fraction": round(gap_part_us / M_PARTITION_DMA_HLS, 3), "label": "MEASURED minus ESTIMATED"},
            "candidates": [
                "driver work inside the dma_hls window: issue_pending of both AXI DMA channels before ap_start and the S2MM IOC interrupt -> tasklet -> callback -> thread wake (ef_v8b_main.c:315-320); not determinable from source; ns_rx_wake / ns_tx_callback (ef_v8b_main.c:366-367) isolate parts of it",
                "S2MM below 1 beat/cycle during the final drain: 80 %% -> +%.1f us, 67 %% -> +%.1f us, 50 %% -> +%.1f us (model)" % (s2mm_tail[80], s2mm_tail[67], s2mm_tail[50]),
                "HLS pipeline depths / call overheads above the central assumption: +5 cycles per NTT/INTT stage -> +7.2 us per frame",
                "not steady-state DMA bandwidth: needs 0.25 GB/s in and 0.74 GB/s out on average versus 3.2 GB/s per direction (128 bit x 200 MHz)"]},
    "dma": {"label": "ESTIMATED (from block design)",
            "config": "AXI DMA 7.1 simple mode, 128-bit MM and stream, MM2S burst 4 beats, S2MM burst 16 beats, all at the 200 MHz fabric clock; both channels -> axi_interconnect (2 SI, 1 MI, SAMD crossbar) -> S_AXI_HP0_FPD 128 bit",
            "source": "build_v8b_system_v3.tcl:20-36, recreate_bd_v8b.tcl:201-220, 837, 890-893, 909; hierarchical.rpt:32",
            "peak_gb_s_per_direction": 3.2,
            "mm2s_needed": "no effect while MM2S >= 0.125 beat/cycle (0.40 GB/s, set by the first message); 0.10 beat/cycle -> +15 us (model, back-pressured)",
            "s2mm_needed": "steady state unaffected while S2MM >= 0.33 beat/cycle (1.07 GB/s); the final drain is 2048 beats + trailer",
            "predicted_mm2s_done_us_k8": round(r["message_coeffs_end"][0] / MHZ, 1)},
    "notes": [
        "No HLS csynth or co-simulation report for V8b rev5 is in the repository (run_v8b_export.tcl writes v8b_ip_prj/, not committed); pipeline depths are inferred, not read.",
        "II values are the pragma targets. The MEASURED %.1f us supports II=1 for the NTT/INTT stages: II=2 would give about 1.8 ms per k=8 frame." % M_DMA_HLS,
        "Core latencies are read from module names in hierarchical.rpt (trailing number = latency + 1, consistent with BIND_OP latency 3/2/3 of mont(), v8b.cpp:44-48): ct_cycle holds urem_11ns_12ns_12_15_1 and sdiv_14ns_14ns_12_18_1 (lines 316-321), gs_cycle holds udiv_28ns_17ns_12_32_1 (lines 349-351). They come from the variable divisions of the address/twiddle index (v8b.cpp:385-387, 426-428) and add ~47 cycles of fill/drain to each of the 36 stage calls per ciphertext (~%d cycles, %.1f us)." % (36 * (ST - 512), 36 * (ST - 512) / MHZ),
        "chacha8: the routed hierarchy shows the key-copy and zero-state loops (v8b.cpp:136, 138) as 16 separate pipelined sub-loops (hierarchical.rpt:731-778). The central value assumes they run concurrently (~19 cycles). Run one after another (~220 cycles) the sampler would need ~52k cycles per ciphertext (~2.1 ms per frame), which the MEASURED %.1f us excludes." % M_DMA_HLS,
        "The ternary acceptance (byte < 243) is random; batch 2 is needed up to byte ~352 +/- 7; the probability of a third batch is negligible (TRIPCOUNT 2, v8b.cpp:310).",
        "The three limb engines are identical and fed in lockstep (the sampler writes u0/u1/u2 and e*0/e*1/e*2 in one statement), so one engine was modelled; their cycle counts are per engine, not summed.",
        "dma_hls (ef_v8b_main.c:315-320, 368) starts before dmaengine issue_pending of RX and TX and ends when the ioctl thread wakes after the S2MM callback; the model covers ap_start to the last output beat only.",
        ("Sustained %s ct in %.1f s (MEASURED, mean of the ten V8b runs) = %.0f ct/s = %.1f us per ciphertext = %.3f ms per k=8 frame, "
         "which equals the measured mean ioctl (%.3f ms), i.e. the serialized ioctl; the PL is busy about %.0f %% of it.")
        % (format(M_SUSTAINED_CT, ","), M_SUSTAINED_S, sus_ct_per_s, M_SUSTAINED_S * 1e6 / M_SUSTAINED_CT, 8 * M_SUSTAINED_S * 1e3 / M_SUSTAINED_CT,
           M_IOCTL_MS, 100 * M_DMA_HLS / (M_IOCTL_MS * 1000)),
        ("The model is compared with the lowest single-call dma_hls values: %.1f us (the lowest of the ten V8b runs' last calls, which range %.1f-%.1f us) "
         "and %.1f us (partition campaign, sustain k = 8, last call; gap %.1f us). dma_hls also contains driver work that varies from call to call, "
         "so the slower calls carry more software time.")
        % (M_DMA_HLS, M_DMA_HLS_RANGE[0], M_DMA_HLS_RANGE[1], M_PARTITION_DMA_HLS, gap_part_us),
        "Module us_per_ct values are each module's own busy time (depth-adjusted). They overlap in time, so they do not add up to the frame time; the per-ciphertext time is set by the bottleneck chain.",
        "For a 'PL does only NTT/pointwise/INTT' partition, ntt_mask + pointwise + intt = %d cycles (%.1f us) per ciphertext per limb engine (ESTIMATED), without the u_load wait or the store." % (12 * ST + 2 * PW + 24 * ST, (12 * ST + 2 * PW + 24 * ST) / MHZ)],
}
dst = os.path.join(HERE, "hls_stage_estimates.json")
with open(dst, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(out, fh, indent=1, ensure_ascii=False)
print("wrote", dst)
print("S2MM tail (us):", {k: round(v, 2) for k, v in s2mm_tail.items()}, "gap", round(gap_us, 2), "partition gap", round(gap_part_us, 2))
print("period", period, us(period), "frame", frame, frame / MHZ, "II-only", period0, us(period0), frame0 / MHZ, "u_steady", u_steady, "tail", tail,
      "fixed", fixed_total, "sampler_ct", sampler_ct, "limb_ct", limb_ct, "by_k", by_k, "fit", round(by_k["1"] - slope, 2), round(slope, 3))
