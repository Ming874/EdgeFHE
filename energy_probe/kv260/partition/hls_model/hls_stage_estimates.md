# V8b rev5 PL stage estimates: one ENC frame (command 9)

Date: 2026-11-23. Scope: the programmable-logic (PL) side of a k-ciphertext ENC frame of the V8b rev5 HLS kernel
`edgefhe_v8b` (`v8b_rev5/hardware/hls/v8b.cpp`), at the 200 MHz fabric clock. This is a read-only analysis of the
repository. No board access.

**Labels.** **(E) ESTIMATED** means derived from the source: trip count × II from the pragmas, plus pipeline depth and
call overhead, combined in a schedule model. **(M) MEASURED** means a board number measured by the team (V8b rev5,
2026-11-23, k = 8 per call): the driver phases of one call, namely the last call of rep 1 of the 10-run V8b-vs-OpenFHE
comparison (`board_enc_check sustain 30 8`), and the means of the ten 30-s V8b runs of that comparison. 1 cycle = 5 ns.

**Method in one paragraph.** For every loop the model takes the trip count and the pragma II. That product is the
"II×trip" lower bound. The model then adds pipeline fill/drain and call overhead. No csynth or co-simulation report
for V8b is in the repository (`run_v8b_export.tcl` writes `v8b_ip_prj/`, which is not committed), so the depths are
inferred rather than read. They come from the core latencies that the routed hierarchy report shows in the HLS module names
(`hardware/reports/rev5/hierarchical.rpt`). A token-level schedule model then replays the six DATAFLOW processes with
the real FIFO depths (v8b.cpp:609-632) to get the overlap and the bottleneck. Model files are listed in section 8.

---

## 1. Result

| Quantity | Value | Label |
|---|---:|---|
| Steady-state bottleneck | limb-engine chain: u_load (waiting for the ternary sampler) → 12 CT stages → [pointwise → 12 GS stages → store] × 2; the 3 engines run in lockstep | (E) |
| Bottleneck time per ciphertext | 26,136 cycles = **130.7 µs** (II×trip bound 24,287 = 121.4 µs) | (E) |
| Runner-up | sampler, 25,713 cycles = 128.6 µs per ciphertext, 372 cycles of slack (co-critical) | (E) |
| PL time per k = 8 frame (ap_start → last output beat into the S2MM) | 211,387 cycles = **1,056.9 µs** (II×trip bound 982.5 µs; sensitivity range 1,041.8–1,071.5 µs) | (E) |
| Same, averaged per ciphertext | 132.1 µs | (E) |
| dma_hls, one k = 8 call (10-run V8b rep 1, last call) | **1,075.3 µs** = **134.4 µs per ciphertext**; the last calls of the ten V8b runs range 1,075.3–1,126.8 µs | (M) |
| Gap, measured minus estimated | 18.4 µs per frame (1.7 %) = 2.3 µs per ciphertext | (M) − (E) |
| Same, partition campaign (sustain k = 8, last call) | dma_hls 1,074.4 µs: gap 17.5 µs per frame (1.6 %) | (M) − (E) |

The estimate and the measurement agree to within 1.7 %. The model is compared with the lowest single-call values,
because dma_hls also contains driver work that varies from call to call; the slower calls carry more of it. The
18.4 µs left over is consistent with that driver work inside the dma_hls window, which the model does not cover (DMA
programming before `ap_start`, plus interrupt and wake after the S2MM completes). Two other candidates are a final
output drain slower than 1 beat/cycle and small HLS-depth error. The source cannot separate these; section 6 has the
details.

## 2. Frame shape (from source, consistent with the measurement)

| Item | Value | Label | Source |
|---|---|---|---|
| Input beats, k = 8 | 1 header + 2 seed + 8 × 2048 message = 16,387 beats = 262,192 B | (E), matches (M) tx | v8b.cpp:180-203, 254-262 |
| Output beats, k = 8 | 1 header + 8 × 6,144 data + 1 trailer = 49,154 beats = 786,464 B | (E), matches (M) rx | v8b.cpp:565-588, edgefhe_v8_policy.h:23-24 |
| Output beat width | 128 bit (`ap_axiu<128,0,0,0>`): 4 × 32-bit coefficients of one limb per beat; per ciphertext 2 polys × 3 limbs × 1024 beats = 6,144 beats = 98,304 B | (E) | v8b.hpp:8, v8b.cpp:571-579 |
| DMA / port width | AXI DMA 128-bit MM and stream, MM2S burst 4 beats, S2MM burst 16 beats, all at 200 MHz → 3.2 GB/s peak per direction; both channels share one interconnect (2 SI → 1 MI, SAMD crossbar) into S_AXI_HP0_FPD (128 bit) | (E) | build_v8b_system_v3.tcl:20-36; recreate_bd_v8b.tcl:201-220, 837, 890-893, 909; hierarchical.rpt:32 |

## 3. Per-module estimates (one ciphertext; k = 8 = 8 × per ciphertext)

All values are **(E) ESTIMATED**. "II×trip → estimated" gives the lower bound and then the depth-adjusted value. µs
values use the estimated cycles. The three limb engines (one per RNS limb) work in parallel, so their numbers are per
engine and are not summed. The modules overlap in time, so the column does not add up to the frame time. The frame time
is set by the bottleneck chain in section 5.

| # | Module (`id`) | Process | Cycles per ct: II×trip → estimated | µs per ct | µs per k = 8 frame (busy) | Overlapped with | Source lines |
|---:|---|---|---:|---:|---:|---|---|
| 1 | read message + reduce m to 3 limbs (`rns_message`) | reader | 4,096 → 4,096 (+26 depth once per frame) | 20.48 | 163.8 | sampler; limb engines. m0–m2 (depth 1024 = one polynomial) let the reader run one ciphertext ahead; it is blocked about 18k cycles per ct | v8b.cpp:253-270 (II=1 at 255); residue64 52-60 |
| 2 | ChaCha20 for mask u (`prng_mask`) | sampler | 176 → 227 (2 calls × 112 + loop control) | 1.14 | 9.1 | nothing inside the sampler (runs before each decode batch) | calls v8b.cpp:308-312; core 127-159, `cycles` loop 142-158 (88 iterations, II=1) |
| 3 | ternary decode, 5 trits per accepted byte (`sample_ternary`) | sampler | 2,048 → 2,060 (2 batches × 512 × II 2) | 10.30 | 82.4 | limb u_load, in lockstep through us0–us2 (depth 32) | v8b.cpp:313-326 (II=2 at 315); TRIPCOUNT 2 at 310 |
| 4 | ChaCha20 for e0, e1 (`prng_gauss`) | sampler | 11,264 → 14,464 (128 calls × 113) | 72.32 | 578.6 | limb NTT/pointwise/INTT of the same ct (e FIFOs hold a whole polynomial) | v8b.cpp:329-334 (64 batches per poly, 332; LOOP_FLATTEN off, 333) |
| 5 | CDT63 Gaussian sampling (`sample_cdt63`) | sampler | 8,192 → 8,960 (128 × 70) | 44.80 | 358.4 | same as 4; sequential with the ChaCha calls | v8b.cpp:335-345 (II=1 at 337); cdt63 160-169 (K = 42) |
| 6 | residues of u, e0, e1 (`rns_small`) | limb ×3 | 1,024 → 1,028; **1,798 in the schedule** (waits for the ternary sampler) | 5.14 (8.99 in schedule) | 41.1 (71.9) | ternary sampler (lockstep); writer drain of the previous c1 | u: u_load v8b.cpp:506-514; e: residue_small 61-64, fused into the store at 533 (0 extra cycles) |
| 7 | forward NTT of u (`ntt_mask`) | limb ×3 | 6,144 → 6,732 (12 × 561) | 33.66 | 269.3 | sampler e0/e1; reader intake | ct_cycle v8b.cpp:378-413 (II=1 at 380) × 12 (456-458, call 515) |
| 8 | pointwise products with cached key (`pointwise`) | limb ×3 | 2,048 → 2,078 (2 × 1,039) | 10.39 | 83.1 | sampler; writer | v8b.cpp:518-522 (II=1 at 520) |
| 9 | two inverse NTTs (`intt`) | limb ×3 | 12,288 → 13,464 (24 × 561) | 67.32 | 538.6 | sampler; writer (c0); reader | gs_cycle v8b.cpp:419-454 (II=1 at 421) × 24 (459-461, call 523) |
| 10 | assembly c0 = b·u + e0 + m, c1 = a·u + e1 (`add`) | limb ×3 | 2,048 → 2,062 (2 × 1,031) | 10.31 | 82.5 | writer drains o0–o2 in the same cycles | store v8b.cpp:524-537 (II=1 at 526) |
| 11 | write output (`write_output`) | writer | 6,144 → 6,144 (1 beat per cycle) | 30.72 | 245.8 (exposed only 10.3 per frame) | limb engines (o0–o2 depth 1024 = one store per limb) | data v8b.cpp:571-579 (II=1 at 573) |

Process totals per ciphertext **(E)**:

| Process | II×trip | Estimated | Note |
|---|---:|---:|---|
| sampler (2–5 + loop control) | 21,680 cyc = 108.4 µs | 25,713 cyc = 128.6 µs | 56.6 % of it is ChaCha20 (130 calls) |
| one limb engine, own work (6–10) | 23,552 cyc = 117.8 µs | 25,366 cyc = 126.8 µs | ×3 engines in parallel |
| one limb engine in the schedule | 24,287 cyc = 121.4 µs | **26,136 cyc = 130.7 µs** | own work + 770 cycles waiting in u_load |
| reader | 4,096 cyc = 20.5 µs | 4,096 cyc = 20.5 µs | |
| writer | 6,144 cyc = 30.7 µs | 6,144 cyc = 30.7 µs | at 1 beat per cycle |

Where the depth assumptions come from **(E)**:

- **NTT/INTT stage call ≈ 561 cycles** (511 + ≈47 depth + ≈3 control). The ct_cycle pipeline contains
  `urem_11ns_12ns_12_15_1` and `sdiv_14ns_14ns_12_18_1` (hierarchical.rpt:316-321). The gs_cycle pipeline contains
  `udiv_28ns_17ns_12_32_1` (hierarchical.rpt:349-351). These dividers come from the variable divisions in the
  address/twiddle index (v8b.cpp:385-387, 426-428), and they sit in front of the twiddle ROM read and the 3/2/3-cycle
  Montgomery chain. The trailing number in the core names is latency + 1, which is consistent with the
  `mul_32ns_32ns_64_4_1` / `mul_32s_31ns_32_3_1` / `mul_32ns_31ns_62_4_1` cores that implement BIND_OP latency 3/2/3
  (v8b.cpp:44-48). Fill/drain over the 36 stage calls per ciphertext: 1,764 cycles = 8.8 µs.
- **chacha8 call ≈ 112 cycles** = ≈19-cycle preamble + 88-iteration core + ≈5 control. The routed hierarchy shows the
  key-copy and state-zero loops (v8b.cpp:136, 138) as 16 separate pipelined sub-loops (hierarchical.rpt:731-778). The
  model assumes that they run concurrently. If they ran one after another (≈220 cycles), the sampler alone would need ≈52k cycles
  per ciphertext (≈2.1 ms per frame), which the measured 1,075 µs rules out.
- The other loops use small depths (pointwise ≈14, store ≈6, CDT ≈5, ternary ≈6, u_load ≈3) plus ≈2 cycles of loop
  control each.

## 4. Per-frame fixed work (k = 8)

| Item | Cycles | µs | On the critical path? | Label | Source |
|---|---:|---:|---|---|---|
| Header beat, 2 seed beats, control tokens, 2 zero tokens to cs | ≈6 | 0.03 | yes (included in the next row) | (E) | v8b.cpp:180-209 |
| Sampler key load from the seed | ≈10 | 0.05 | yes (included in the next row) | (E) | v8b.cpp:296-298 |
| First-ciphertext fill: the first mask has no 32-token head start; first input beat assumed 60 cycles after ap_start | 251 | 1.3 | yes | (E) | v8b.cpp:308-326, 506-514 |
| Final output drain: when the last store ends, 2,048 of its 3,072 beats are still queued; then the trailer | 2,052 | 10.3 | **yes** | (E) | v8b.cpp:571-588 |
| `message_scrub` (1024 zero tokens to m0–m2) | ≈1,027 | 5.1 | no (runs during the last ciphertext) | (E) | v8b.cpp:272-276 |
| Sampler rev5 scrub: zero key, zero-key ChaCha20 + RFC 8439 check, `noise_scrub` | ≈1,167 | 5.8 | no (ends ≈590 cycles after the last store, before the writer reads kat) | (E) | v8b.cpp:349-361 |
| Limb `wipe` of A/B/C + `scrub_enc` | ≈2,055 | 10.3 | no for dma_hls: it runs in parallel with the output drain and sets ap_done at about the same time as the last beat | (E) | v8b.cpp:540-551 |
| Reader `drain` | 0 | 0 | — | (E) | v8b.cpp:278-282 |
| Public key | 0 per frame: cb/ca are cached in URAM by LOAD. LOAD itself ≈17.6k cycles ≈ 88 µs once per key lease (not in this measurement) | — | no | (E) | v8b.cpp:466-501 |
| Driver DMA programming before ap_start; S2MM interrupt → callback → thread wake | not determinable from source | — | yes, inside dma_hls | — | ef_v8b_main.c:315-320, 366-368 |
| **Exposed PL fixed total** | **≈2,303** | **≈11.5** | | (E) | model intercept |

## 5. DATAFLOW: what overlaps and what does not

The six processes `reader | sampler | limb<0> | limb<1> | limb<2> | writer` run concurrently (v8b.cpp:601, 645-650).
Every channel is an `hls::stream` FIFO; there are **no ping-pong buffers**. Each process loops over the k ciphertexts
internally (v8b.cpp:254, 303, 504, 572). The FIFOs that allow overlap across ciphertexts:

| FIFO | Depth | What it allows | Source |
|---|---|---|---|
| m0–m2 (URAM) | 1024 × 128 bit = one message per limb | the reader runs one ciphertext ahead | v8b.cpp:621-623, 633-635 |
| e00–e12 (URAM) | 1024 × 32 bit = one e polynomial each | the sampler makes e0(f), e1(f) while the limbs run NTT/INTT of f | v8b.cpp:624-629, 636-641 |
| us0–us2 | **32 tokens = 128 coefficients** | the mask of f+1 can run only 32 tokens ahead; the rest is in lockstep with u_load(f+1) | v8b.cpp:618-620 |
| o0–o2 (URAM) | 1024 × 128 bit = one store per limb | the writer drains c0(f) and c1(f) while the limbs continue | v8b.cpp:630-632, 642-644 |

Inside a limb engine nothing overlaps: u_load, 12 CT stages, then (pointwise, 12 GS stages, store) × 2 run one after
another. `limb` has no DATAFLOW, and the routed hierarchy shows one shared ct_stage and one gs_stage instance per engine
(hierarchical.rpt:291, 322). Inside the sampler, ChaCha20 and the decode/CDT loops are also sequential.

**Steady-state timeline of one ciphertext** **(E)**, from the model (k = 8, f = 3), in cycles after u_load(f) ends:

| Cycles | Limb engines (×3) | Sampler | Reader | Writer |
|---|---|---|---|---|
| 0 – 6,732 | NTT(u) | finishes mask(f) batch 2 (to ≈325), then e0(f) | blocked (m FIFO full) | drains c1(f−1) for the first ≈250 cycles |
| 6,732 – 14,503 | pointwise b·u, INTT | e0(f) done at 12,021, then e1(f) | blocked | idle |
| 14,503 – 15,534 | store c0 = b·u + e0 + m | e1(f) | takes in m(f+1), 14,503 – ≈18,630 | drains c0(f), 14,503 – ≈17,580 |
| 15,534 – 23,305 | pointwise a·u, INTT | e1(f) done at 23,733 | blocked after ≈18,630 | idle |
| 23,305 – 24,336 | store c1 = a·u + e1 (the last e1 batch is ready ≈600 cycles before the store needs it) | ChaCha for mask(f+1), 32 tokens, then blocked on the full u FIFO | blocked | drains c1(f), 23,305 – ≈26,380 |
| 24,338 – 26,136 | u_load(f+1), waiting for tokens | mask(f+1): batch 1 to 25,315, ChaCha, batch 2 to the 1024th token | blocked | drains c1(f) |

**Bottleneck** **(E)**: the limb-engine chain, at 26,136 cycles = 130.7 µs per ciphertext. It breaks down as INTT
13,464 (51.5 %), NTT 6,732 (25.8 %), pointwise 2,078 (8.0 %), store 2,062 (7.9 %) and u_load including waiting 1,798
(6.9 %). Within NTT + INTT, 18,432 cycles issue butterflies and 1,764 cycles are pipeline fill/drain. About 770 cycles
per ciphertext go to u_load waiting for mask tokens. The ternary decoder produces ≈0.59 tokens per cycle while u_load
takes 1 per cycle, and the 32-deep us FIFO limits the head start to 32 tokens.

The sampler is **co-critical**: 25,713 cycles per ciphertext and 372 cycles of slack. It becomes the bottleneck if one
chacha8 call costs more than ≈115 cycles. The sensitivity sweep covers stage calls of 551–571 and chacha8 calls of
96–124 cycles. Over the limb-bound region it gives 128.8–132.5 µs per ciphertext and 1,041.8–1,071.5 µs per frame. Every
grid point with a chacha8 call ≥ 120 cycles gives a PL-only frame above the measured 1,075.3 µs, so the measurement
excludes it (the 1,074.4 µs of the partition campaign excludes the same points). The reader (20.5 µs per ciphertext)
and the writer (30.7 µs per ciphertext) are not limiting.

## 6. Predicted vs measured, and the gap

| | Per ciphertext | Per k = 8 frame | Label |
|---|---:|---:|---|
| II×trip only (no depth, no call overhead) | 121.4 µs | 982.5 µs | (E) |
| Depth-adjusted, central | 130.7 µs steady state (132.1 frame average) | **1,056.9 µs** | (E) |
| Sensitivity range (limb-bound region) | 128.8 – 132.5 µs | 1,041.8 – 1,071.5 µs | (E) |
| dma_hls, one call (10-run V8b rep 1, last call) | **134.4 µs** | **1,075.3 µs** | (M) |
| dma_hls, one call (partition campaign, sustain k = 8, last call) | 134.3 µs | 1,074.4 µs | (M) |
| dma_hls, last calls of the ten V8b runs | 134.4 – 140.9 µs | 1,075.3 – 1,126.8 µs | (M) |

Predicted PL time per frame for other k **(E)**, as a check if dma_hls is measured at other k:

| k | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| µs | 142.2 | 272.9 | 403.6 | 534.3 | 665.0 | 795.6 | 926.3 | 1,056.9 |

This fits 11.5 µs + 130.67 µs × k. Driver latency per call would add to the intercept, not to the slope. The model also
predicts that the MM2S hands over the last input beat about 887.5 µs after ap_start for k = 8. The driver's
`ns_tx_callback` should therefore read about 888 µs plus software latency (ef_v8b_main.c:367).

The model is compared with the lowest single-call values (the first two rows of (M)): dma_hls also contains driver
work that varies from call to call, so the slower calls carry more software time.

**Explaining the 18.4 µs gap (1.7 %; 17.5 µs, 1.6 %, against the partition campaign):**

1. **Driver work inside the dma_hls window (not modelled, not determinable from source).** `start` is taken before
   `dma_async_issue_pending` of RX and TX and the `ap_start` write. `t_rx` is taken after the ioctl thread wakes from
   the S2MM completion callback (ef_v8b_main.c:315-320, 368). The driver's `ns_rx_wake` and `ns_tx_callback` stats
   (ef_v8b_main.c:366-367) would isolate part of this.
2. **The final output drain, if the S2MM sustains less than 1 beat/cycle.** In the model the steady state is
   unaffected down to 0.33 beat/cycle (1.07 GB/s), but the exposed 2,048-beat tail grows: 80 % → +3.8 µs, 67 % →
   +7.7 µs, 50 % → +15.3 µs (E). The source does not fix the sustained S2MM → HP0 → DDR write rate.
3. **HLS depth or overhead above the central assumption.** For example, +5 cycles per NTT/INTT stage call adds
   ≈7.2 µs per frame (E).
4. **Not steady-state DMA bandwidth.** On average the frame needs 0.25 GB/s in and 0.74 GB/s out, against 3.2 GB/s per
   direction (128 bit × 200 MHz) (E). Reads and writes use separate channels of a SAMD crossbar. With a back-pressured
   MM2S model, the input has no effect down to 0.125 beat/cycle (0.40 GB/s), a limit set by the first message. At
   0.10 beat/cycle the frame grows by 15 µs (E). The 4-beat MM2S burst (build_v8b_system_v3.tcl:27) matters only if
   it pushed the MM2S below that rate. The source cannot show whether it does.
5. **Non-overlapped stages** are already in the estimate: the sequential limb chain, the u_load wait (≈770 cycles per
   ciphertext) and the 10.3 µs output tail.

**Sustained throughput** **(M, derived)**: 104,982 ciphertexts in 30.0 s (mean of the ten V8b runs) is 3,499 ct/s =
285.8 µs per ciphertext = 2.286 ms per k = 8 frame. That equals the measured mean ioctl (2.286 ms), so the sustained
rate is set by the serialized ioctl, and the PL is busy for 1,075.3 / 2,286 ≈ 47 % of it. With dma_hls alone the rate
would be 8 / 1.0753 ms ≈ 7,440 ct/s (M, derived); the PL-only model gives ≈7,569 ct/s (E).

## 7. Assumptions and limits

- **II values are the pragma targets, not reported results.** The measurement supports II = 1 for the NTT/INTT stages:
  with II = 2, a k = 8 frame would take about 1.8 ms.
- **Pipeline depths and call overheads are inferred** from the core latencies in the routed hierarchy report.
  Confirming them needs `csynth.rpt` or co-simulation from a local `vitis_hls -f run_v8b_export.tcl` run.
- **Ternary acceptance is random.** About 864 bytes per mask (batch 2 up to byte ≈352 ± 7); the model draws it with
  p = 243/256. A third batch is negligible (TRIPCOUNT 2, v8b.cpp:310).
- **One limb engine is modelled.** The three engines are identical and fed in lockstep: the sampler writes u0/u1/u2 and
  e*0/e*1/e*2 in one statement (v8b.cpp:322, 342-343).
- **Assumed DMA behaviour.** The first input beat is assumed 60 cycles after ap_start. The S2MM accepts 1 beat per cycle
  unless stated otherwise. Driver, interrupt and DDR latencies are not modelled.
- **For the partition analysis.** If only NTT, pointwise and INTT were placed in the PL, the PL time would be
  ntt_mask + pointwise + intt = 22,274 cycles = 111.4 µs per ciphertext per engine (E). That excludes the u_load wait
  and the store.

## 8. Files

All files are in this folder (`energy_probe/kv260/partition/hls_model/`).

- `hls_stage_estimates.json`: the machine-readable version of this report (read by `../analyze_partition.py`).
- `v8b_sched_model.py`: the token-level schedule model (`python -I v8b_sched_model.py 8`). It only prints; give an
  output path as a second argument (outside this folder) to save the full JSON dump.
- `sweep.py` and `sweep_k8.json`: the sensitivity sweep over chacha8 and stage latency (`python -I sweep.py` rewrites
  `sweep_k8.json` in this folder, the input of `gen_outputs.py`).
- `timeline.py`: prints the steady-state timeline of one ciphertext (writes no file).
- `gen_outputs.py`: regenerates `hls_stage_estimates.json` from the same parameters and the MEASURED values listed at
  its top (`python -I gen_outputs.py`).

All scripts can be run from any folder.
