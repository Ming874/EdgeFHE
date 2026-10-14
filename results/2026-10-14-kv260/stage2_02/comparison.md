# A/B comparison: pure software (A) vs hardware/software (B)

- Records: 38,052 (../data/cdc_diabetes/splits/test.csv), model `cdc-diabetes-lr4-v1`
- Machine: aarch64, ARM implementer 0x41 part 0xd03 (Cortex-A53), 4 CPUs, affinity 0-3 (pinned), cpufreq governor userspace
- Randomness: os-getrandom; cloud: in-process CPU (V4 Engine::affine); identical for every backend
- Files: stage2_02/e2e_all.json
- Fairness checks: pass

## 1. Correctness (gate)

| Backend | Group | Pass | Max logit diff | Disagree | Uncertain | Cipher SHA-256 | File |
|---|---|---|---|---|---|---|---|
| cpu-w1 | A | yes | 3.93e-07 | 0 | 1 | 39b881dcd3cc052d | stage2_02/e2e_all.json |
| cpu-w2 | A | yes | 3.93e-07 | 0 | 1 | 39b881dcd3cc052d | stage2_02/e2e_all.json |
| cpu-w3 | A | yes | 3.93e-07 | 0 | 1 | 39b881dcd3cc052d | stage2_02/e2e_all.json |
| cpu-w4 | A | yes | 3.93e-07 | 0 | 1 | 39b881dcd3cc052d | stage2_02/e2e_all.json |
| fpga-serial | B | yes | 3.93e-07 | 0 | 1 | 39b881dcd3cc052d | stage2_02/e2e_all.json |
| fpga-overlap | B | yes | 3.93e-07 | 0 | 1 | 39b881dcd3cc052d | stage2_02/e2e_all.json |

Bit-exact across all backends within each run: **yes** (different runs use fresh OS randomness, so their digests differ by design).

## 2. Latency of one batch (ms, p50 unless noted)

Edge = steps 1-5 and 9-10 on the board; the in-process cloud step is shown separately.

| Backend | Group | Edge p50 | Edge p95 | Encrypt | Decrypt | Cloud | Total | Speedup vs cpu-w1 (same file) |
|---|---|---|---|---|---|---|---|---|
| cpu-w1 | A | 86.76 | 87.09 | 38.34 | 7.86 | 4.56 | 91.37 | 1.00 |
| cpu-w2 | A | 69.08 | 69.28 | 23.37 | 5.17 | 4.57 | 73.71 | 1.26 |
| cpu-w3 | A | 62.90 | 63.09 | 18.48 | 3.83 | 4.58 | 67.55 | 1.38 |
| cpu-w4 | A | 60.45 | 63.04 | 16.17 | 3.75 | 4.58 | 65.08 | 1.44 |
| fpga-serial | B | 94.38 | 94.52 | 47.35 | 6.45 | 4.59 | 99.02 | 0.92 |
| fpga-overlap | B | 93.04 | 93.55 | 46.00 | 6.44 | 4.58 | 97.67 | 0.93 |

### Group A scaling, encrypt step (stage2_02/e2e_all.json)

| Workers | Encrypt p50 ms | Speedup | Efficiency |
|---|---|---|---|
| 1 | 38.34 | 1.00 | 1.00 |
| 2 | 23.37 | 1.64 | 0.82 |
| 3 | 18.48 | 2.07 | 0.69 |
| 4 | 16.17 | 2.37 | 0.59 |

### Group B breakdown per product (ms)

Device call = the whole ioctl seen by the program; driver-reported = the driver's own DMA+PL window.

| Backend | Pack | Device call | Unpack | Driver-reported | KiB moved |
|---|---|---|---|---|---|
| fpga-serial | 0.07 | 4.67 | 0.18 | 0.54 | 192.00 |
| fpga-overlap | 0.07 | 4.72 | 0.18 | 0.55 | 192.00 |

**Latency, best A vs best B:** cpu-w4 60.45 ms vs fpga-overlap 93.04 ms → B/A speed = **0.65×** (B is slower).

## 3. Throughput (stream of batches)

| Backend | Group | Pipeline | Batches | Records/s | Gain vs serial | CPU util | Mean W | Records/J |
|---|---|---|---|---|---|---|---|---|
| cpu-w1 | A | serial | 1 | 22,389.79 | – | 0.25 | 3.35 | 6,693.51 |
| cpu-w1 | A | serial | 2 | 22,381.64 | – | 0.25 | 3.34 | 6,709.12 |
| cpu-w1 | A | serial | 4 | 22,390.84 | – | 0.25 | 3.33 | 6,721.85 |
| cpu-w1 | A | serial | 8 | 22,391.98 | – | 0.25 | 3.34 | 6,710.16 |
| cpu-w1 | A | serial | 19 | 21,906.98 | – | 0.25 | 3.34 | 6,563.09 |
| cpu-w1 | A | pipelined (2) | 1 | 22,311.51 | 1.00 | 0.25 | 3.35 | 6,666.79 |
| cpu-w1 | A | pipelined (2) | 2 | 30,110.52 | 1.35 | 0.35 | 3.45 | 8,734.02 |
| cpu-w1 | A | pipelined (2) | 4 | 35,735.66 | 1.60 | 0.41 | 3.48 | 10,280.23 |
| cpu-w1 | A | pipelined (2) | 8 | 38,976.23 | 1.74 | 0.45 | 3.50 | 11,143.30 |
| cpu-w1 | A | pipelined (2) | 19 | 40,639.22 | 1.86 | 0.48 | 3.53 | 11,515.30 |
| cpu-w2 | A | serial | 1 | 27,760.15 | – | 0.31 | 3.41 | 8,145.58 |
| cpu-w2 | A | serial | 2 | 27,763.34 | – | 0.31 | 3.38 | 8,211.31 |
| cpu-w2 | A | serial | 4 | 27,752.36 | – | 0.31 | 3.38 | 8,221.40 |
| cpu-w2 | A | serial | 8 | 27,769.51 | – | 0.31 | 3.39 | 8,186.77 |
| cpu-w2 | A | serial | 19 | 27,175.81 | – | 0.31 | 3.39 | 8,018.83 |
| cpu-w2 | A | pipelined (2) | 1 | 27,643.47 | 1.00 | 0.31 | 3.38 | 8,188.23 |
| cpu-w2 | A | pipelined (2) | 2 | 37,969.45 | 1.37 | 0.43 | 3.45 | 11,010.20 |
| cpu-w2 | A | pipelined (2) | 4 | 46,808.59 | 1.69 | 0.53 | 3.54 | 13,233.98 |
| cpu-w2 | A | pipelined (2) | 8 | 53,919.82 | 1.94 | 0.62 | 3.66 | 14,749.82 |
| cpu-w2 | A | pipelined (2) | 19 | 57,528.26 | 2.12 | 0.67 | 3.71 | 15,516.11 |
| cpu-w3 | A | serial | 1 | 30,344.19 | – | 0.34 | 3.42 | 8,882.96 |
| cpu-w3 | A | serial | 2 | 30,352.35 | – | 0.34 | 3.41 | 8,907.51 |
| cpu-w3 | A | serial | 4 | 30,330.81 | – | 0.34 | 3.42 | 8,861.75 |
| cpu-w3 | A | serial | 8 | 30,291.74 | – | 0.34 | 3.42 | 8,867.42 |
| cpu-w3 | A | serial | 19 | 29,681.89 | – | 0.34 | 3.42 | 8,684.87 |
| cpu-w3 | A | pipelined (2) | 1 | 30,154.63 | 0.99 | 0.34 | 3.43 | 8,786.31 |
| cpu-w3 | A | pipelined (2) | 2 | 40,930.21 | 1.35 | 0.47 | 3.53 | 11,584.02 |
| cpu-w3 | A | pipelined (2) | 4 | 49,708.66 | 1.64 | 0.57 | 3.66 | 13,598.11 |
| cpu-w3 | A | pipelined (2) | 8 | 55,607.14 | 1.84 | 0.63 | 3.66 | 15,193.21 |
| cpu-w3 | A | pipelined (2) | 19 | 58,333.27 | 1.97 | 0.68 | 3.73 | 15,631.33 |
| cpu-w4 | A | serial | 1 | 31,391.17 | – | 0.36 | 3.41 | 9,216.43 |
| cpu-w4 | A | serial | 2 | 31,334.05 | – | 0.36 | 3.44 | 9,118.68 |
| cpu-w4 | A | serial | 4 | 31,319.58 | – | 0.36 | 3.46 | 9,055.64 |
| cpu-w4 | A | serial | 8 | 31,347.89 | – | 0.36 | 3.41 | 9,186.94 |
| cpu-w4 | A | serial | 19 | 30,634.57 | – | 0.36 | 3.42 | 8,960.01 |
| cpu-w4 | A | pipelined (2) | 1 | 31,023.73 | 0.99 | 0.36 | 3.43 | 9,039.55 |
| cpu-w4 | A | pipelined (2) | 2 | 41,396.74 | 1.32 | 0.48 | 3.51 | 11,788.35 |
| cpu-w4 | A | pipelined (2) | 4 | 50,302.81 | 1.61 | 0.58 | 3.59 | 13,998.93 |
| cpu-w4 | A | pipelined (2) | 8 | 56,067.48 | 1.79 | 0.64 | 3.68 | 15,220.22 |
| cpu-w4 | A | pipelined (2) | 19 | 58,558.18 | 1.91 | 0.69 | 3.72 | 15,742.77 |
| fpga-serial | B | serial | 1 | 20,718.31 | – | 0.24 | 3.38 | 6,138.76 |
| fpga-serial | B | serial | 2 | 20,708.87 | – | 0.24 | 3.38 | 6,126.89 |
| fpga-serial | B | serial | 4 | 20,700.64 | – | 0.24 | 3.38 | 6,124.45 |
| fpga-serial | B | serial | 8 | 20,689.45 | – | 0.24 | 3.37 | 6,133.84 |
| fpga-serial | B | serial | 19 | 20,229.18 | – | 0.24 | 3.37 | 5,998.37 |
| fpga-serial | B | pipelined (2) | 1 | 20,624.79 | 1.00 | 0.24 | 3.36 | 6,132.25 |
| fpga-serial | B | pipelined (2) | 2 | 27,079.17 | 1.31 | 0.31 | 3.45 | 7,859.16 |
| fpga-serial | B | pipelined (2) | 4 | 31,487.24 | 1.52 | 0.37 | 3.48 | 9,051.77 |
| fpga-serial | B | pipelined (2) | 8 | 33,991.21 | 1.64 | 0.39 | 3.54 | 9,597.70 |
| fpga-serial | B | pipelined (2) | 19 | 35,088.37 | 1.73 | 0.42 | 3.59 | 9,777.95 |
| fpga-overlap | B | serial | 1 | 21,017.35 | – | 0.25 | 3.38 | 6,208.96 |
| fpga-overlap | B | serial | 2 | 21,000.16 | – | 0.24 | 3.38 | 6,218.08 |
| fpga-overlap | B | serial | 4 | 21,014.10 | – | 0.24 | 3.39 | 6,204.08 |
| fpga-overlap | B | serial | 8 | 20,999.79 | – | 0.25 | 3.38 | 6,208.82 |
| fpga-overlap | B | serial | 19 | 20,540.39 | – | 0.24 | 3.38 | 6,073.13 |
| fpga-overlap | B | pipelined (2) | 1 | 20,882.76 | 0.99 | 0.24 | 3.39 | 6,151.03 |
| fpga-overlap | B | pipelined (2) | 2 | 27,577.66 | 1.31 | 0.32 | 3.45 | 7,993.53 |
| fpga-overlap | B | pipelined (2) | 4 | 31,863.58 | 1.52 | 0.37 | 3.52 | 9,042.98 |
| fpga-overlap | B | pipelined (2) | 8 | 35,090.12 | 1.67 | 0.41 | 3.57 | 9,825.73 |
| fpga-overlap | B | pipelined (2) | 19 | 36,137.87 | 1.76 | 0.44 | 3.58 | 10,081.09 |

**Throughput, best A vs best B (19 batches):** cpu-w4 pipelined 58,558 vs fpga-overlap pipelined 36,138 records/s → B/A = **0.62×**.

**Energy:** A 15,742.8 vs B 10,081.1 records/J (sensor ina260_u14/power1; hwmon power of that rail or the whole board, not only the CPU or FPGA).

### Stream length (when does pipelining pay off?)

| Backend | Pipeline | 1 batches | 2 batches | 4 batches | 8 batches | 19 batches |
|---|---|---|---|---|---|---|
| cpu-w1 | serial | 22,389.79 | 22,381.64 | 22,390.84 | 22,391.98 | 21,906.98 |
| cpu-w1 | pipelined (2) | 22,311.51 | 30,110.52 | 35,735.66 | 38,976.23 | 40,639.22 |
| cpu-w2 | serial | 27,760.15 | 27,763.34 | 27,752.36 | 27,769.51 | 27,175.81 |
| cpu-w2 | pipelined (2) | 27,643.47 | 37,969.45 | 46,808.59 | 53,919.82 | 57,528.26 |
| cpu-w3 | serial | 30,344.19 | 30,352.35 | 30,330.81 | 30,291.74 | 29,681.89 |
| cpu-w3 | pipelined (2) | 30,154.63 | 40,930.21 | 49,708.66 | 55,607.14 | 58,333.27 |
| cpu-w4 | serial | 31,391.17 | 31,334.05 | 31,319.58 | 31,347.89 | 30,634.57 |
| cpu-w4 | pipelined (2) | 31,023.73 | 41,396.74 | 50,302.81 | 56,067.48 | 58,558.18 |
| fpga-serial | serial | 20,718.31 | 20,708.87 | 20,700.64 | 20,689.45 | 20,229.18 |
| fpga-serial | pipelined (2) | 20,624.79 | 27,079.17 | 31,487.24 | 33,991.21 | 35,088.37 |
| fpga-overlap | serial | 21,017.35 | 21,000.16 | 21,014.10 | 20,999.79 | 20,540.39 |
| fpga-overlap | pipelined (2) | 20,882.76 | 27,577.66 | 31,863.58 | 35,090.12 | 36,137.87 |

