// Sampler B (2026-10-15; user-approved entropy architecture: OS seed + ChaCha20). Candidate profile, not yet adopted.
//
// Per job: seed = 32 bytes from getrandom (no fallback). For feature f = 0..3 three independent ChaCha20 keystreams
// (RFC 8439) with key = seed, counter from 0 and the domain-separating nonce (purpose, f, 0):
//   purpose 1 -> mask u_f : uniform ternary, 5 trits per accepted byte (byte < 243 = 3^5), exactly uniform
//   purpose 2 -> e0_f, purpose 3 -> e1_f : constant-time CDT, one 64-bit little-endian word per coefficient,
//                low 63 bits against the 42-entry table of cdt63_table.hpp, bit 63 = sign (sigma 4.5, |x| <= 42,
//                statistical distance from the truncated target about 2^-60; see tools/cdt63_table.json)
// The same seed always gives the same material, which makes this the bit-exact specification of a later PL sampler.
// Production entry draws a fresh seed for every call and wipes seed, key and keystream buffers afterwards.
// Not interchangeable with the current or A/A2 samplers: different algorithms and draws (new profile identity).
#pragma once
#include "cdt63_table.hpp"
#include "chacha20.hpp"
#include "secure.hpp"
#include <array>
#include <cerrno>
#include <cstring>
#include <string.h>
#include <sys/random.h>
#include <vector>
namespace edgefhe {
namespace sampler_b {

constexpr size_t gauss_blocks = degree * 8 / 64;   // 512 keystream blocks = 4096 words of 64 bits

inline void wipe_bytes(void* p, size_t n) { explicit_bzero(p, n); }

inline void key_words(const uint8_t seed[32], uint32_t key[8]) {
  for (int i = 0; i < 8; i++) key[i] = uint32_t(seed[4 * i]) | uint32_t(seed[4 * i + 1]) << 8 | uint32_t(seed[4 * i + 2]) << 16 | uint32_t(seed[4 * i + 3]) << 24;
}

// Constant-time CDT on n words: out[i] = (+/-) #{k : (w & (2^63-1)) >= T[k]}, sign = bit 63.
// cdt63_scalar is the reference; cdt63 evaluates four words at once with vector compares (NEON on aarch64) and
// must be equal on every input (sampler_b_selftest golden checks random words and every table boundary).
inline int64_t cdt63_one(uint64_t w) {
  const uint64_t r = w & 0x7fffffffffffffffull;
  uint64_t v = 0;
  for (unsigned k = 0; k < cdt63::K; k++) v += uint64_t(r >= cdt63::T[k]);
  const uint64_t s = w >> 63;                          // 0 or 1
  return int64_t((v ^ (0 - s)) + s);                   // s ? -v : v without a branch
}
inline void cdt63_scalar(const uint64_t* w, int64_t* out, size_t n) { for (size_t i = 0; i < n; i++) out[i] = cdt63_one(w[i]); }
typedef uint64_t u64x4 __attribute__((vector_size(32)));
inline void cdt63(const uint64_t* w, int64_t* out, size_t n) {
  const u64x4 low = {0x7fffffffffffffffull, 0x7fffffffffffffffull, 0x7fffffffffffffffull, 0x7fffffffffffffffull};
  const size_t groups = n / 4;          // count-based loops: GCC 11 -Werror=aggressive-loop-optimizations (board build)
  for (size_t g = 0; g < groups; g++) {
    const size_t i = 4 * g;
    u64x4 x; std::memcpy(&x, w + i, sizeof x);
    const u64x4 r = x & low;
    u64x4 v = {0, 0, 0, 0};
    for (unsigned k = 0; k < cdt63::K; k++) {
      const u64x4 t = {cdt63::T[k], cdt63::T[k], cdt63::T[k], cdt63::T[k]};
      v -= (u64x4)(r >= t);                              // compare gives all-ones (-1) or 0 per lane
    }
    const u64x4 s = x >> 63;
    const u64x4 y = (v ^ (u64x4{0, 0, 0, 0} - s)) + s;
    std::memcpy(out + i, &y, sizeof y);
  }
  for (size_t t = 0; t < n % 4; t++) { const size_t i = 4 * groups + t; out[i] = cdt63_one(w[i]); }
}

// Uniform ternary from the byte stream of one keystream; returns the number of keystream blocks consumed.
inline uint32_t ternary(const uint32_t key[8], const uint32_t nonce[3], int8_t* out, size_t n) {
  uint32_t words[64];
  uint32_t counter = 0; size_t i = 0;
  while (i < n) {
    require(chacha::keystream(key, counter, nonce, words, 4), "ChaCha20 counter range");
    counter += 4;
    const uint8_t* bytes = reinterpret_cast<const uint8_t*>(words);   // little-endian serialisation
    for (size_t j = 0; j < sizeof words && i < n; j++) {
      unsigned x = bytes[j];
      if (x >= 243) continue;                                      // rejection, independent of the output
      for (int d = 0; d < 5 && i < n; d++) { out[i++] = int8_t(int(x % 3) - 1); x /= 3; }
    }
  }
  wipe_bytes(words, sizeof words);
  return counter;
}

// Bit-exact specification: material for one job from a 32-byte seed. Caller owns and wipes the seed.
inline void sample_from_seed(SecureMaterial& m, const uint8_t seed[32]) {
  uint32_t key[8]; key_words(seed, key);
  std::vector<uint64_t> words(degree);
  for (size_t f = 0; f < 4; f++) {
    for (auto* p : {&m.e0[f], &m.e1[f]}) if (p->size() != degree) { for (auto& v : *p) v = 0; p->assign(degree, 0); }
    if (m.mask[f].size() != degree) { for (auto& v : m.mask[f]) v = 0; m.mask[f].assign(degree, 0); }
    const uint32_t n_mask[3] = {1, uint32_t(f), 0}, n_e0[3] = {2, uint32_t(f), 0}, n_e1[3] = {3, uint32_t(f), 0};
    ternary(key, n_mask, m.mask[f].data(), degree);
    const uint32_t* nonces[2] = {n_e0, n_e1};
    Message* dsts[2] = {&m.e0[f], &m.e1[f]};
    for (int t = 0; t < 2; t++) {
      require(chacha::keystream(key, 0, nonces[t], reinterpret_cast<uint32_t*>(words.data()), gauss_blocks), "ChaCha20 counter range");
      cdt63(words.data(), dsts[t]->data(), degree);
    }
  }
  wipe_bytes(words.data(), words.size() * sizeof(uint64_t));
  wipe_bytes(key, sizeof key);
}

inline void os_seed(uint8_t seed[32]) {
  size_t got = 0;
  while (got < 32) {
    const auto n = getrandom(seed + got, 32 - got, 0);
    if (n < 0 && errno == EINTR) continue;
    require(n > 0, "OS entropy unavailable; no fallback");
    got += size_t(n);
  }
}
}  // namespace sampler_b

// Production entry: fresh OS seed per job, no shared state between threads.
inline void sample_secure_encryption_b(SecureMaterial& m) {
  uint8_t seed[32];
  sampler_b::os_seed(seed);
  sampler_b::sample_from_seed(m, seed);
  sampler_b::wipe_bytes(seed, sizeof seed);
}
}  // namespace edgefhe
