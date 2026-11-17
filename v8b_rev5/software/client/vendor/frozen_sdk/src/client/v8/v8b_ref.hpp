// EdgeFHE V8b reference (2026-10-16): full public-key encryption in the PL, as in the proposal's dataflow
// ("PRNG generates noise polynomials e0, e1; NTT core; result c0 and c1"). Extends the V8a kernel model.
//
// New command ENC (9). Frame in:
//   header {MAGIC, 9 | k << 8, gen low, gen high}
//   2 beats: 32-byte seed (w0..w3 of beat 1 = seed bytes 0..15 little-endian words, beat 2 = bytes 16..31)
//   for f in 0..k-1: 2048 beats, beat j = m[2j], m[2j+1] as little-endian two's-complement int64 (w0,w1 | w2,w3)
// Material = sampler B of the PS (src/client/baseline/sampler_b.hpp), bit for bit: ChaCha20 (RFC 8439) keystreams with
// key = seed and nonce (purpose, f, 0): purpose 1 = mask u_f (5 trits per accepted byte), 2 = e0_f, 3 = e1_f
// (constant-time CDT63, |x| <= 42). Ciphertext f: c0 = b*u + e0 + m, c1 = a*u + e1 in each limb (canonical residues).
// Frame out: header + for f: for poly in (c0, c1): for j in 0..1023: for limb: beat(4 coefficients) + trailer.
// Extra status: 6 = message coefficient outside |m| < 2^62 (encoder contract); the coefficient is treated as 0.
// The PS keeps the seed only for the frame and wipes it; the PL wipes seed and material when the frame ends.
#pragma once
#include "v8_ref.hpp"
#include "../baseline/chacha20.hpp"
#include "../baseline/cdt63_table.hpp"

namespace v8ref {
constexpr uint32_t ST_MESSAGE = 6;

struct Material { std::vector<int8_t> u; std::vector<int64_t> e0, e1; };

inline void key_from_seed(const uint8_t seed[32], uint32_t key[8]) {
  for (int i = 0; i < 8; i++) key[i] = uint32_t(seed[4 * i]) | uint32_t(seed[4 * i + 1]) << 8 | uint32_t(seed[4 * i + 2]) << 16 | uint32_t(seed[4 * i + 3]) << 24;
}
inline int64_t cdt63_value(uint64_t w) {
  const uint64_t r = w & 0x7fffffffffffffffull;
  uint64_t v = 0;
  for (unsigned k = 0; k < edgefhe::cdt63::K; k++) v += uint64_t(r >= edgefhe::cdt63::T[k]);
  return (w >> 63) ? -int64_t(v) : int64_t(v);
}
// Sampler B material for feature f (same algorithm and draws as edgefhe::sampler_b::sample_from_seed).
inline Material sample_b(const uint8_t seed[32], uint32_t f) {
  uint32_t key[8]; key_from_seed(seed, key);
  Material m; m.u.reserve(N);
  const uint32_t nu[3] = {1, f, 0}, n0[3] = {2, f, 0}, n1[3] = {3, f, 0};
  uint32_t words[64]; uint32_t counter = 0;
  while (m.u.size() < size_t(N)) {
    edgefhe::chacha::keystream(key, counter, nu, words, 4); counter += 4;
    const uint8_t* bytes = reinterpret_cast<const uint8_t*>(words);
    for (int j = 0; j < 256 && m.u.size() < size_t(N); j++) {
      unsigned x = bytes[j];
      if (x >= 243) continue;
      for (int d = 0; d < 5 && m.u.size() < size_t(N); d++) { m.u.push_back(int8_t(int(x % 3) - 1)); x /= 3; }
    }
  }
  std::vector<uint32_t> g(2 * N);
  for (int t = 0; t < 2; t++) {
    edgefhe::chacha::keystream(key, 0, t == 0 ? n0 : n1, g.data(), 512);
    auto& e = t == 0 ? m.e0 : m.e1;
    for (int i = 0; i < N; i++) e.push_back(cdt63_value(uint64_t(g[2 * i]) | uint64_t(g[2 * i + 1]) << 32));
  }
  return m;
}
inline uint32_t residue(int64_t x, uint32_t q) { int64_t r = x % int64_t(q); return uint32_t(r < 0 ? r + q : r); }

inline std::vector<Beat> enc_frame(const uint8_t seed[32], const std::vector<std::vector<int64_t>>& msg, uint64_t generation) {
  std::vector<Beat> f{header(9, uint32_t(msg.size()), generation)};
  for (int b = 0; b < 2; b++) {
    Beat x{{0, 0, 0, 0}};
    for (int w = 0; w < 4; w++) for (int i = 0; i < 4; i++) x.w[w] |= uint32_t(seed[16 * b + 4 * w + i]) << (8 * i);
    f.push_back(x);
  }
  for (const auto& m : msg) {
    if (m.size() != size_t(N)) throw std::runtime_error("message degree");
    for (int j = 0; j < N / 2; j++) {
      const uint64_t a = uint64_t(m[2 * j]), b = uint64_t(m[2 * j + 1]);
      f.push_back({{uint32_t(a), uint32_t(a >> 32), uint32_t(b), uint32_t(b >> 32)}});
    }
  }
  return f;
}

// Kernel model with ENC: wraps the V8a model (shared key state) and adds command 9.
struct KernelB : Kernel {
  std::vector<Beat> run(const std::vector<Beat>& in) {
    const uint32_t command = in.empty() ? 0 : in[0].w[1] & 0xff;
    if (in.empty() || in[0].w[0] != MAGIC || command != 9) return Kernel::run(in);
    const uint32_t k = (in[0].w[1] >> 8) & 0xff;
    const uint64_t gen = uint64_t(in[0].w[2]) | uint64_t(in[0].w[3]) << 32;
    std::vector<Beat> out{header(9, k, gen)};
    auto fail = [&](uint32_t st) { valid = false; epoch = 0; out.push_back(trailer(st, k, gen)); return out; };
    if (k < 1 || k > 8) return fail(ST_HEADER);
    if (!valid || gen != epoch || gen == 0) return fail(ST_GENERATION);
    auto beat = [&](size_t i) { return i < in.size() ? in[i] : Beat{{0, 0, 0, 0}}; };
    uint32_t st = in.size() == 1 + 2 + size_t(k) * 2048 ? ST_OK : ST_LENGTH;
    uint8_t seed[32];
    for (int b = 0; b < 2; b++) for (int w = 0; w < 4; w++) for (int i = 0; i < 4; i++) seed[16 * b + 4 * w + i] = uint8_t(beat(1 + b).w[w] >> (8 * i));
    for (uint32_t f = 0; f < k; f++) {
      const Material mat = sample_b(seed, f);
      std::vector<int64_t> msg(N);
      for (int j = 0; j < N / 2; j++) {
        const Beat x = beat(3 + f * 2048 + j);
        int64_t a = int64_t(uint64_t(x.w[0]) | uint64_t(x.w[1]) << 32), b = int64_t(uint64_t(x.w[2]) | uint64_t(x.w[3]) << 32);
        for (int64_t* v : {&a, &b}) if (*v >= (int64_t(1) << 62) || *v <= -(int64_t(1) << 62)) { *v = 0; if (st == ST_OK) st = ST_MESSAGE; }
        msg[2 * j] = a; msg[2 * j + 1] = b;
      }
      std::array<Poly, 3> c0, c1;
      for (int l = 0; l < 3; l++) {
        Poly u(N);
        for (int i = 0; i < N; i++) u[i] = residue(mat.u[i], Q[l]);
        c0[l] = product(cb[l], u, tables[l]);
        c1[l] = product(ca[l], u, tables[l]);
        for (int i = 0; i < N; i++) {
          c0[l][i] = add(add(c0[l][i], residue(mat.e0[i], Q[l]), Q[l]), residue(msg[i], Q[l]), Q[l]);
          c1[l][i] = add(c1[l][i], residue(mat.e1[i], Q[l]), Q[l]);
        }
      }
      for (const auto* p : {&c0, &c1}) for (int j = 0; j < 1024; j++) for (int l = 0; l < 3; l++)
        out.push_back({{(*p)[l][4 * j], (*p)[l][4 * j + 1], (*p)[l][4 * j + 2], (*p)[l][4 * j + 3]}});
    }
    for (auto& s : seed) s = 0;
    if (st != ST_OK) return fail(st);
    out.push_back(trailer(ST_OK, k, gen));
    return out;
  }
};
}  // namespace v8ref
