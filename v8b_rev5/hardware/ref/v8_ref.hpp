// EdgeFHE V8a reference model (2026-10-15). Plain C++17, no HLS types: the bit-exact specification that the V8a HLS
// kernel is checked against in C simulation. Design: optimization document sections 7.3, 7.4, 7.10, 7.11.
//
// Ring R_q = Z_q[X]/(X^n + 1), n = 4096, q in {998244353, 1004535809, 469762049}; psi = primitive 2n-th root (psi^n = -1).
// Values are canonical residues in [0, q). Montgomery R = 2^32; MontMul(x, y) = x * y * R^-1 mod q.
//   Psi[k]    = psi^brv(k) * R mod q,  PsiInv[k] = psi^-brv(k) * R mod q   (brv = 12-bit bit reversal)
//   ntt_ct    : Cooley-Tukey, natural order in -> bit-reversed order out, twist by psi merged into the twiddles
//   intt_gs   : Gentleman-Sande, bit-reversed in -> natural out, WITHOUT the final n^-1 (absorbed by the cache)
//   cache     : C[i] = NTT(p)[i] * n^-1 * R mod q, so intt_gs(MontMul(U[i], C[i])) = p * u in R_q exactly
// Frame v8 (128-bit beats, little-endian 32-bit words w0..w3):
//   header   : w0 = 0x45463841 ('EF8A'), w1 = command | k << 8, w2/w3 = generation low/high
//   LOAD (6) : header + for poly in (b, a): for limb: 1024 beats, beat j = coefficients 4j..4j+3 of that limb
//   PAIR (7) : header + k features x 64 beats; feature f beat j holds trits 64j..64j+63, trit i in bits 2(i%64)+1:2(i%64)
//              code 00 = 0, 01 = +1, 10 = -1, 11 = invalid (frame error)
//   PAIR out : header + for f: for prod in (b*u, a*u): for j in 0..1023: for limb: one beat (4 coefficients 4j..4j+3)
//              + trailer beat (status; see Kernel)
#pragma once
#include <array>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace v8ref {
constexpr int N = 4096, LOGN = 12, LIMBS = 3;
constexpr std::array<uint32_t, 3> Q = {998244353u, 1004535809u, 469762049u};
constexpr std::array<uint32_t, 3> PSI = {350007156u, 2062645u, 62025685u};
constexpr uint32_t MAGIC = 0x45463841u;
using Poly = std::vector<uint32_t>;

inline uint32_t add(uint32_t a, uint32_t b, uint32_t q) { uint32_t s = a + b; return s >= q ? s - q : s; }
inline uint32_t sub(uint32_t a, uint32_t b, uint32_t q) { return a >= b ? a - b : a + q - b; }
inline uint32_t mulmod(uint32_t a, uint32_t b, uint32_t q) { return uint32_t(uint64_t(a) * b % q); }
inline uint32_t powmod(uint32_t a, uint64_t e, uint32_t q) { uint32_t r = 1; while (e) { if (e & 1) r = mulmod(r, a, q); a = mulmod(a, a, q); e >>= 1; } return r; }
inline uint32_t neg_inv32(uint32_t q) { uint32_t x = 1; for (int i = 0; i < 5; i++) x *= 2 - q * x; return 0u - x; }   // -q^-1 mod 2^32
inline uint32_t mont(uint32_t a, uint32_t b, uint32_t q, uint32_t qp) {
  const uint64_t p = uint64_t(a) * b;
  const uint32_t m = uint32_t(p) * qp;
  const uint64_t t = (p + uint64_t(m) * q) >> 32;          // < 2q
  return uint32_t(t >= q ? t - q : t);
}
inline unsigned brv(unsigned x) { unsigned r = 0; for (int i = 0; i < LOGN; i++) { r = (r << 1) | (x & 1); x >>= 1; } return r; }

struct Tables {
  uint32_t q, qp, r_mod, ninv_r;      // R mod q, n^-1 * R mod q
  std::vector<uint32_t> psi, psi_inv; // Montgomery form, bit-reversed index
  explicit Tables(int limb) : q(Q[limb]), qp(neg_inv32(Q[limb])), psi(N), psi_inv(N) {
    const uint32_t s = PSI[limb], si = powmod(s, q - 2, q);
    if (powmod(s, N, q) != q - 1) throw std::runtime_error("psi^n != -1");
    r_mod = uint32_t((uint64_t(1) << 32) % q);
    ninv_r = mulmod(powmod(N, q - 2, q), r_mod, q);
    for (int k = 0; k < N; k++) {
      psi[k] = mulmod(powmod(s, brv(k), q), r_mod, q);
      psi_inv[k] = mulmod(powmod(si, brv(k), q), r_mod, q);
    }
  }
};

inline void ntt_ct(Poly& a, const Tables& t) {
  int len = N;
  for (int m = 1; m < N; m <<= 1) {
    len >>= 1;
    for (int i = 0; i < m; i++) {
      const uint32_t S = t.psi[m + i];
      const int j1 = 2 * i * len;
      for (int j = j1; j < j1 + len; j++) {
        const uint32_t U = a[j], V = mont(a[j + len], S, t.q, t.qp);
        a[j] = add(U, V, t.q); a[j + len] = sub(U, V, t.q);
      }
    }
  }
}
inline void intt_gs(Poly& a, const Tables& t) {    // no final n^-1
  int len = 1;
  for (int m = N; m > 1; m >>= 1) {
    const int h = m >> 1;
    int j1 = 0;
    for (int i = 0; i < h; i++) {
      const uint32_t S = t.psi_inv[h + i];
      for (int j = j1; j < j1 + len; j++) {
        const uint32_t U = a[j], V = a[j + len];
        a[j] = add(U, V, t.q); a[j + len] = mont(sub(U, V, t.q), S, t.q, t.qp);
      }
      j1 += 2 * len;
    }
    len <<= 1;
  }
}
// Cached spectrum of a public polynomial p (canonical residues): NTT(p) * n^-1 * R.
inline Poly cache_spectrum(Poly p, const Tables& t) {
  ntt_ct(p, t);
  for (auto& x : p) x = mulmod(x, t.ninv_r, t.q);
  return p;
}
// p * u in R_q from the cached spectrum C of p and the residues of u.
inline Poly product(const Poly& C, Poly u, const Tables& t) {
  ntt_ct(u, t);
  for (int i = 0; i < N; i++) u[i] = mont(u[i], C[i], t.q, t.qp);
  intt_gs(u, t);
  return u;
}
// Schoolbook negacyclic product, independent oracle for tests.
inline Poly schoolbook(const Poly& p, const Poly& u, uint32_t q) {
  std::vector<uint64_t> acc(N, 0), neg(N, 0);
  for (int i = 0; i < N; i++) if (p[i]) for (int j = 0; j < N; j++) if (u[j]) {
    const uint64_t v = uint64_t(p[i]) * u[j] % q; const int k = i + j;
    if (k < N) acc[k] = (acc[k] + v) % q; else neg[k - N] = (neg[k - N] + v) % q;
  }
  Poly out(N); for (int k = 0; k < N; k++) out[k] = uint32_t((acc[k] + q - neg[k]) % q); return out;
}

// ---- frame v8 ---------------------------------------------------------------------------------------------------
struct Beat { uint32_t w[4]; };
inline Beat header(uint32_t command, uint32_t k, uint64_t generation) { return {{MAGIC, command | (k << 8), uint32_t(generation), uint32_t(generation >> 32)}}; }
inline std::vector<Beat> load_frame(const std::array<Poly, 3>& b, const std::array<Poly, 3>& a, uint64_t generation) {
  std::vector<Beat> f{header(6, 0, generation)};
  for (const auto* p : {&b, &a}) for (int l = 0; l < LIMBS; l++) for (int j = 0; j < N / 4; j++)
    f.push_back({{(*p)[l][4 * j], (*p)[l][4 * j + 1], (*p)[l][4 * j + 2], (*p)[l][4 * j + 3]}});
  return f;
}
inline std::vector<Beat> pair_frame(const std::vector<std::vector<int8_t>>& u, uint64_t generation) {
  std::vector<Beat> f{header(7, uint32_t(u.size()), generation)};
  for (const auto& v : u) {
    if (v.size() != size_t(N)) throw std::runtime_error("ternary degree");
    for (int j = 0; j < N / 64; j++) {
      Beat b{{0, 0, 0, 0}};
      for (int i = 0; i < 64; i++) {
        const int8_t x = v[64 * j + i];
        if (x < -1 || x > 1) throw std::runtime_error("ternary range");
        const uint32_t code = x == 0 ? 0u : x == 1 ? 1u : 2u;
        b.w[(2 * i) / 32] |= code << ((2 * i) % 32);
      }
      f.push_back(b);
    }
  }
  return f;
}
inline uint32_t trit_residue(uint32_t code, uint32_t q) { return code == 0 ? 0u : code == 1 ? 1u : q - 1; }

// Functional model of the V8a kernel, written the way the hardware behaves (no exceptions on frame content):
//   output = header (echo of command, k, generation) + data + trailer {MAGIC, status, k, generation low}
//   status 0 OK, 1 bad magic/command/k, 2 generation not loaded or mismatched, 3 frame length != expected (TLAST),
//          4 invalid trit code (treated as 0 in the data), 5 non-canonical key coefficient (treated as 0)
//   any nonzero status clears the key (fail-closed; a new LOAD is required), as in V7.
//   header errors (1, 2): no data beats, the input is drained to TLAST. Length errors: missing beats read as zero.
//   INVALIDATE (8): clears the key, header + trailer.
constexpr uint32_t ST_OK = 0, ST_HEADER = 1, ST_GENERATION = 2, ST_LENGTH = 3, ST_TRIT = 4, ST_KEY = 5;
inline Beat trailer(uint32_t status, uint32_t k, uint64_t generation) { return {{MAGIC, status, k, uint32_t(generation)}}; }
struct Kernel {
  std::array<Tables, 3> tables{Tables(0), Tables(1), Tables(2)};
  std::array<Poly, 3> cb, ca; uint64_t epoch = 0; bool valid = false;
  std::vector<Beat> run(const std::vector<Beat>& in) {
    const Beat h = in.empty() ? Beat{{0, 0, 0, 0}} : in[0];
    const uint32_t command = h.w[1] & 0xff, k = (h.w[1] >> 8) & 0xff;
    const uint64_t gen = uint64_t(h.w[2]) | uint64_t(h.w[3]) << 32;
    std::vector<Beat> out{header(command, k, gen)};
    auto beat = [&](size_t i) { return i < in.size() ? in[i] : Beat{{0, 0, 0, 0}}; };
    auto fail = [&](uint32_t st) { valid = false; epoch = 0; out.push_back(trailer(st, k, gen)); return out; };
    if (in.empty() || h.w[0] != MAGIC) return fail(ST_HEADER);
    if (command == 8) { valid = false; epoch = 0; out.push_back(trailer(in.size() == 1 ? ST_OK : ST_LENGTH, k, gen)); return out; }
    if (command == 6) {
      if (gen == 0 || k != 0) return fail(gen == 0 ? ST_GENERATION : ST_HEADER);
      uint32_t st = in.size() == 1 + 2 * 3 * 1024 ? ST_OK : ST_LENGTH;
      for (int pi = 0; pi < 2; pi++) for (int l = 0; l < 3; l++) {
        Poly p(N);
        for (int j = 0; j < 1024; j++) for (int c = 0; c < 4; c++) {
          uint32_t x = beat(1 + (pi * 3 + l) * 1024 + j).w[c];
          if (x >= Q[l]) { x = 0; if (st == ST_OK) st = ST_KEY; }
          p[4 * j + c] = x;
        }
        (pi == 0 ? cb : ca)[l] = cache_spectrum(p, tables[l]);
      }
      if (st != ST_OK) return fail(st);
      epoch = gen; valid = true; out.push_back(trailer(ST_OK, 0, gen)); return out;
    }
    if (command != 7 || k < 1 || k > 8) return fail(ST_HEADER);
    if (!valid || gen != epoch || gen == 0) return fail(ST_GENERATION);
    uint32_t st = in.size() == 1 + k * 64 ? ST_OK : ST_LENGTH;
    for (uint32_t f = 0; f < k; f++) {
      std::array<Poly, 3> pb, pa;
      for (int l = 0; l < 3; l++) {
        Poly u(N);
        for (int j = 0; j < 64; j++) for (int i = 0; i < 64; i++) {
          uint32_t code = (beat(1 + f * 64 + j).w[(2 * i) / 32] >> ((2 * i) % 32)) & 3;
          if (code == 3) { code = 0; if (st == ST_OK) st = ST_TRIT; }
          u[64 * j + i] = trit_residue(code, Q[l]);
        }
        pb[l] = product(cb[l], u, tables[l]);
        pa[l] = product(ca[l], u, tables[l]);
      }
      for (const auto* p : {&pb, &pa}) for (int j = 0; j < 1024; j++) for (int l = 0; l < 3; l++)
        out.push_back({{(*p)[l][4 * j], (*p)[l][4 * j + 1], (*p)[l][4 * j + 2], (*p)[l][4 * j + 3]}});
    }
    if (st != ST_OK) return fail(st);
    out.push_back(trailer(ST_OK, k, gen)); return out;
  }
};
}  // namespace v8ref
