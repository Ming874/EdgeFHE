// ChaCha20 block function and keystream (RFC 8439), PS reference for sampler B (2026-10-15).
// block()  : scalar, one 64-byte block, exactly RFC 8439 section 2.3.
// blocks() : four consecutive counters at once in a lane-per-block layout using GCC vector extensions, which
//            compile to NEON on aarch64 and SSE on x86; results are checked against block() and the RFC vectors.
// The keystream is the little-endian serialisation of the 16 state words of each block, in counter order.
#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>
namespace edgefhe { namespace chacha {

constexpr uint32_t C0 = 0x61707865u, C1 = 0x3320646eu, C2 = 0x79622d32u, C3 = 0x6b206574u;
inline uint32_t rotl(uint32_t x, int n) { return (x << n) | (x >> (32 - n)); }
#define EF_QR(a, b, c, d) \
  a += b; d ^= a; d = rotl(d, 16); c += d; b ^= c; b = rotl(b, 12); \
  a += b; d ^= a; d = rotl(d, 8);  c += d; b ^= c; b = rotl(b, 7);

inline void block(const uint32_t key[8], uint32_t counter, const uint32_t nonce[3], uint32_t out[16]) {
  uint32_t s[16] = {C0, C1, C2, C3, key[0], key[1], key[2], key[3], key[4], key[5], key[6], key[7], counter, nonce[0], nonce[1], nonce[2]};
  uint32_t x[16];
  std::memcpy(x, s, sizeof x);
  for (int i = 0; i < 10; i++) {
    EF_QR(x[0], x[4], x[8], x[12]) EF_QR(x[1], x[5], x[9], x[13]) EF_QR(x[2], x[6], x[10], x[14]) EF_QR(x[3], x[7], x[11], x[15])
    EF_QR(x[0], x[5], x[10], x[15]) EF_QR(x[1], x[6], x[11], x[12]) EF_QR(x[2], x[7], x[8], x[13]) EF_QR(x[3], x[4], x[9], x[14])
  }
  for (int i = 0; i < 16; i++) out[i] = x[i] + s[i];
}

typedef uint32_t v4 __attribute__((vector_size(16)));
inline v4 vrotl(v4 x, int n) { return (x << n) | (x >> (32 - n)); }
#define EF_VQR(a, b, c, d) \
  a += b; d ^= a; d = vrotl(d, 16); c += d; b ^= c; b = vrotl(b, 12); \
  a += b; d ^= a; d = vrotl(d, 8);  c += d; b ^= c; b = vrotl(b, 7);

// Four blocks: counters counter .. counter+3 (32-bit wrap is refused by the caller), written as 64 words in order.
inline void blocks4(const uint32_t key[8], uint32_t counter, const uint32_t nonce[3], uint32_t out[64]) {
  v4 s[16];
  const uint32_t init[16] = {C0, C1, C2, C3, key[0], key[1], key[2], key[3], key[4], key[5], key[6], key[7], 0, nonce[0], nonce[1], nonce[2]};
  for (int i = 0; i < 16; i++) s[i] = v4{init[i], init[i], init[i], init[i]};
  s[12] = v4{counter, counter + 1, counter + 2, counter + 3};
  v4 x[16];
  for (int i = 0; i < 16; i++) x[i] = s[i];
  for (int i = 0; i < 10; i++) {
    EF_VQR(x[0], x[4], x[8], x[12]) EF_VQR(x[1], x[5], x[9], x[13]) EF_VQR(x[2], x[6], x[10], x[14]) EF_VQR(x[3], x[7], x[11], x[15])
    EF_VQR(x[0], x[5], x[10], x[15]) EF_VQR(x[1], x[6], x[11], x[12]) EF_VQR(x[2], x[7], x[8], x[13]) EF_VQR(x[3], x[4], x[9], x[14])
  }
  for (int i = 0; i < 16; i++) {
    const v4 y = x[i] + s[i];
    for (int b = 0; b < 4; b++) out[16 * b + i] = y[b];
  }
}
#undef EF_QR
#undef EF_VQR

// nblocks keystream blocks starting at counter into words[16 * nblocks]. Refuses a 32-bit counter wrap.
inline bool keystream(const uint32_t key[8], uint32_t counter, const uint32_t nonce[3], uint32_t* words, size_t nblocks) {
  if (nblocks > (uint64_t(1) << 32) - counter) return false;
  const size_t groups = nblocks / 4;   // count-based loops: GCC 11 -Werror=aggressive-loop-optimizations (board build)
  for (size_t g = 0; g < groups; g++) blocks4(key, counter + uint32_t(4 * g), nonce, words + 64 * g);
  for (size_t t = 0; t < nblocks % 4; t++) { const size_t b = 4 * groups + t; block(key, counter + uint32_t(b), nonce, words + 16 * b); }
  return true;
}
}}  // namespace edgefhe::chacha
