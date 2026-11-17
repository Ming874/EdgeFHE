/* candidate-5b user-space selftest + micro-benchmark of the vector payload scans (ef_v8b_vec.c compiled with
 * -DEF_USERSPACE: NEON on arm64, SSE on x86). Verdicts are compared with the accepted opt1 scalar policy checks
 * (edgefhe_v8_policy.h) on boundary values at every lane/limb position class and on random inputs; the benchmark
 * reports scalar opt1 vs vector scan time on warm and cold (cache-evicted) 384 KiB / 128 KiB buffers.
 * Not a kernel test: the kernel build of the same source runs inside kernel_neon_begin/end.
 * usage: vec_selftest [bench_reps] */
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
typedef uint32_t u32;
typedef uint64_t u64;
#include "edgefhe_v8_uapi.h"
#include "edgefhe_v8_policy.h"
#include "ef_v8b_vec.h"

static u64 rng = 0x2545f4914f6cdd1dull;
static u32 rnd(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (u32)rng; }
static unsigned long checks, failures;
static void must(int ok, const char *what, long a, long b)
{
  checks++;
  if (!ok) { if (failures++ < 10) fprintf(stderr, "VEC_SELFTEST_FAIL %s (%ld, %ld)\n", what, a, b); }
}
static double now_ns(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1e9 + t.tv_nsec; }

/* frame helpers: header beat + payload + trailer, as the driver sees RX */
static u32 *frame(u32 k, u32 **payload)
{
  const u32 beats = 6144 * k;
  u32 *rx = calloc((2 + beats) * 4, 4), i, c;
  rx[0] = 0x45463841U; rx[1] = 9 | (k << 8); rx[2] = 77; rx[3] = 0;
  for (i = 0; i < beats; i++) for (c = 0; c < 4; c++) rx[4 * (1 + i) + c] = rnd() % EF_Q[i % 3];
  rx[4 * (1 + beats)] = 0x45463841U; rx[4 * (1 + beats) + 1] = 0; rx[4 * (1 + beats) + 2] = k; rx[4 * (1 + beats) + 3] = 77;
  *payload = rx + 4;
  return rx;
}
static int scalar_output_bad(const u32 *rx, u32 k) { const u32 hdr[4] = {rx[0], rx[1], rx[2], rx[3]}; return ef_check_output(rx, hdr, 6144 * k, k) == -EBADMSG; }
static int scalar_input_bad(const u32 *p, u32 k) { return ef_check_input(9, k, p) != 0; }

int main(int argc, char **argv)
{
  const int reps = argc > 1 ? atoi(argv[1]) : 50;
  u32 k, i, c;
  /* output scans: every limb x lane x boundary value at the first, a middle and the last beat of the limb class */
  for (k = 1; k <= 8; k++) {
    u32 *pl, *rx = frame(k, &pl); const u32 beats = 6144 * k;
    must(!ef5_vec_payload_bad(pl, beats, EF_Q[0], EF_Q[1], EF_Q[2]) && !scalar_output_bad(rx, k), "valid frame", k, 0);
    const u32 pos[] = {0, 1, 2, beats / 2, beats / 2 + 1, beats / 2 + 2, beats - 3, beats - 2, beats - 1};
    for (i = 0; i < sizeof pos / sizeof pos[0]; i++) for (c = 0; c < 4; c++) {
      const u32 q = EF_Q[pos[i] % 3], vals[] = {0, q - 1, q, q + 1, 0x7fffffffU, 0x80000000U, 0xffffffffU};
      u32 v;
      for (v = 0; v < sizeof vals / sizeof vals[0]; v++) {
        const u32 old = pl[4 * pos[i] + c]; pl[4 * pos[i] + c] = vals[v];
        const int want = vals[v] >= q, got = ef5_vec_payload_bad(pl, beats, EF_Q[0], EF_Q[1], EF_Q[2]) != 0;
        must(got == want && scalar_output_bad(rx, k) == want, "output boundary", (long)pos[i] * 4 + c, (long)vals[v]);
        pl[4 * pos[i] + c] = old;
      }
    }
    for (i = 0; i < 2000; i++) {   /* random single violations */
      const u32 b = rnd() % beats, cc = rnd() % 4, q = EF_Q[b % 3], old = pl[4 * b + cc];
      pl[4 * b + cc] = q + rnd() % (0xffffffffU - q);
      must(ef5_vec_payload_bad(pl, beats, EF_Q[0], EF_Q[1], EF_Q[2]) && scalar_output_bad(rx, k), "output random violation", b, cc);
      pl[4 * b + cc] = old;
    }
    free(rx);
  }
  /* input scans: seed words then 4096k int64; boundary values at the first, a middle and the last coefficient */
  for (k = 1; k <= 8; k++) {
    const u32 n = 4096 * k;
    u32 *p = calloc(8 + 2 * n, 4);
    for (i = 0; i < 8; i++) p[i] = rnd() | 1;
    for (i = 0; i < n; i++) { const int64_t m = (int64_t)(((u64)rnd() << 32) | rnd()) >> 3; p[8 + 2 * i] = (u32)m; p[9 + 2 * i] = (u32)((u64)m >> 32); }
    must(!ef5_vec_messages_bad(p + 8, n) && !scalar_input_bad(p, k), "valid messages", k, 0);
    const int64_t vals[] = {0, 1, -1, (int64_t)1 << 61, -((int64_t)1 << 61), ((int64_t)1 << 62) - 1, -((int64_t)1 << 62) + 1, (int64_t)1 << 62,
                            -((int64_t)1 << 62), ((int64_t)1 << 62) + 1, -((int64_t)1 << 62) - 1, INT64_MAX, INT64_MIN, (int64_t)3 << 61, -((int64_t)3 << 61)};
    const u32 pos[] = {0, 1, n / 2, n / 2 + 1, n - 2, n - 1};
    for (i = 0; i < sizeof pos / sizeof pos[0]; i++) for (c = 0; c < sizeof vals / sizeof vals[0]; c++) {
      const u32 lo = p[8 + 2 * pos[i]], hi = p[9 + 2 * pos[i]];
      const int64_t m = vals[c]; const int want = !(m > -((int64_t)1 << 62) && m < ((int64_t)1 << 62));
      p[8 + 2 * pos[i]] = (u32)m; p[9 + 2 * pos[i]] = (u32)((u64)m >> 32);
      must((ef5_vec_messages_bad(p + 8, n) != 0) == want && scalar_input_bad(p, k) == want, "input boundary", (long)pos[i], (long)c);
      p[8 + 2 * pos[i]] = lo; p[9 + 2 * pos[i]] = hi;
    }
    for (i = 0; i < 2000; i++) {
      const u32 j = rnd() % n, lo = p[8 + 2 * j], hi = p[9 + 2 * j];
      p[8 + 2 * j] = rnd(); p[9 + 2 * j] = rnd();
      must((ef5_vec_messages_bad(p + 8, n) != 0) == scalar_input_bad(p, k), "input random", j, 0);
      p[8 + 2 * j] = lo; p[9 + 2 * j] = hi;
    }
    free(p);
  }
  /* odd count tail of the message scan */
  { u32 w[6] = {1, 0, 2, 0, 0, 0x40000000U}; must(ef5_vec_messages_bad(w, 3) == 1 && ef5_vec_messages_bad(w, 2) == 0, "odd tail", 0, 0); }
  printf("VEC_EQUIVALENCE %s checks=%lu failures=%lu\n", failures ? "FAIL" : "PASS", checks, failures);
  if (failures) return 1;

  /* micro-benchmark: k = 4 frame (384 KiB output, 128 KiB input); cold = rotate through 12 copies (>> 1 MiB L2) */
  { enum { COPIES = 12 }; const u32 kk = 4, beats = 6144 * kk, n = 4096 * kk; u32 *rx[COPIES], *in[COPIES], *pl; int r, j;
    double t_so = 0, t_vo = 0, t_si = 0, t_vi = 0, w_so = 1e30, w_vo = 1e30, w_si = 1e30, w_vi = 1e30, t_ms = 0; volatile u32 sink = 0;
    for (j = 0; j < COPIES; j++) { rx[j] = frame(kk, &pl); in[j] = calloc(8 + 2 * n, 4); for (i = 0; i < 8; i++) in[j][i] = 1; }
    for (r = 0; r < reps; r++) {   /* cold: each buffer was last touched COPIES-1 buffers (>4 MiB) ago */
      double a, b;
      for (j = 0; j < COPIES; j++) { a = now_ns(); sink += scalar_output_bad(rx[j], kk); b = now_ns(); t_so += b - a; }
      for (j = 0; j < COPIES; j++) { a = now_ns(); sink += ef5_vec_payload_bad(rx[j] + 4, beats, EF_Q[0], EF_Q[1], EF_Q[2]); b = now_ns(); t_vo += b - a; }
      for (j = 0; j < COPIES; j++) { a = now_ns(); sink += scalar_input_bad(in[j], kk); b = now_ns(); t_si += b - a; }
      for (j = 0; j < COPIES; j++) { a = now_ns(); sink += ef5_vec_messages_bad(in[j] + 8, n); b = now_ns(); t_vi += b - a; }
      for (j = 0; j < COPIES; j++) { a = now_ns(); memset(rx[j] + 4, 0, beats * 16); b = now_ns(); t_ms += b - a; }
      for (j = 0; j < COPIES; j++) { u32 t; for (t = 0; t < beats * 4; t++) rx[j][4 + t] = t % 469762049U; }
    }
    for (r = 0; r < reps; r++) {   /* warm: same buffer twice, best of the second pass */
      double a, b; sink += scalar_output_bad(rx[0], kk); a = now_ns(); sink += scalar_output_bad(rx[0], kk); b = now_ns(); if (b - a < w_so) w_so = b - a;
      sink += ef5_vec_payload_bad(rx[0] + 4, beats, EF_Q[0], EF_Q[1], EF_Q[2]); a = now_ns(); sink += ef5_vec_payload_bad(rx[0] + 4, beats, EF_Q[0], EF_Q[1], EF_Q[2]); b = now_ns(); if (b - a < w_vo) w_vo = b - a;
      sink += scalar_input_bad(in[0], kk); a = now_ns(); sink += scalar_input_bad(in[0], kk); b = now_ns(); if (b - a < w_si) w_si = b - a;
      sink += ef5_vec_messages_bad(in[0] + 8, n); a = now_ns(); sink += ef5_vec_messages_bad(in[0] + 8, n); b = now_ns(); if (b - a < w_vi) w_vi = b - a;
    }
    const double cold = reps * (double)COPIES;
    printf("{\"status\":\"VEC_BENCH_DIAGNOSTIC\",\"k\":4,\"reps\":%d,\"cold_mean_ms\":{\"scalar_output\":%.4f,\"vector_output\":%.4f,\"scalar_input\":%.4f,\"vector_input\":%.4f,\"memset_384KiB\":%.4f},"
           "\"warm_best_ms\":{\"scalar_output\":%.4f,\"vector_output\":%.4f,\"scalar_input\":%.4f,\"vector_input\":%.4f},\"sink\":%u}\n",
           reps, t_so / cold / 1e6, t_vo / cold / 1e6, t_si / cold / 1e6, t_vi / cold / 1e6, t_ms / cold / 1e6,
           w_so / 1e6, w_vo / 1e6, w_si / 1e6, w_vi / 1e6, (unsigned)sink);
    for (j = 0; j < COPIES; j++) { free(rx[j]); free(in[j]); }
  }
  return 0;
}
