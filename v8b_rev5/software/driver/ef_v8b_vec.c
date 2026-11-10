// SPDX-License-Identifier: GPL-2.0
/* candidate-5b vector payload scans with GCC vector extensions (128-bit; NEON on arm64). Built without
 * -mgeneral-regs-only, so every function here may only run between kernel_neon_begin() and kernel_neon_end().
 * No branches on data; the result is an OR-accumulated verdict, the same accept set as the scalar opt1 checks:
 *  - |m| < 2^62  <=>  u = m + (2^62 - 1) (mod 2^64) <= 2^63 - 2 (unsigned);
 *  - x canonical <=> x < q, i.e. not (x >= q), lane-wise with q of the beat's limb. */
#ifdef EF_USERSPACE
#include <stdint.h>
typedef uint32_t u32;
typedef uint64_t u64;
#else
#include <linux/types.h>
#endif
#include "ef_v8b_vec.h"
typedef u32 ef_v4u32 __attribute__((vector_size(16)));
typedef u64 ef_v2u64 __attribute__((vector_size(16)));

u32 ef5_vec_messages_bad(const u32 *words, u32 n)
{
 const ef_v2u64 off = {0x3fffffffffffffffULL, 0x3fffffffffffffffULL}, lim = {0x7ffffffffffffffeULL, 0x7ffffffffffffffeULL};
 ef_v2u64 bad = {0, 0}, x;
 u32 i;
 for (i = 0; i + 2 <= n; i += 2) {
  __builtin_memcpy(&x, words + 2 * i, 16);
  bad |= (ef_v2u64)((x + off) > lim);
 }
 if (i < n) {
  const u64 m = (u64)words[2 * i] | ((u64)words[2 * i + 1] << 32);
  bad[0] |= (u64)(m + 0x3fffffffffffffffULL > 0x7ffffffffffffffeULL);
 }
 return (bad[0] | bad[1]) != 0;
}

u32 ef5_vec_payload_bad(const u32 *payload, u32 beats, u32 q0, u32 q1, u32 q2)
{
 const ef_v4u32 Q0 = {q0, q0, q0, q0}, Q1 = {q1, q1, q1, q1}, Q2 = {q2, q2, q2, q2};
 ef_v4u32 bad = {0, 0, 0, 0}, a, b, c;
 u32 i;
 for (i = 0; i + 3 <= beats; i += 3) {
  __builtin_memcpy(&a, payload + 4 * i, 16);
  __builtin_memcpy(&b, payload + 4 * i + 4, 16);
  __builtin_memcpy(&c, payload + 4 * i + 8, 16);
  bad |= (ef_v4u32)(a >= Q0) | (ef_v4u32)(b >= Q1) | (ef_v4u32)(c >= Q2);
 }
 return (bad[0] | bad[1] | bad[2] | bad[3]) != 0;
}
