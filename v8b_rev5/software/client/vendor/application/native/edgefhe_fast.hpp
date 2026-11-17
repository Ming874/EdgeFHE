// EdgeFHE PS-side performance redesign candidate (2026-10-19). Isolated: not linked into owner/cloud/SDK yet.
//
// Every routine is either bit-identical to the frozen SDK routine it replaces (NTT, multiply, decrypt polynomial,
// cloud affine, device-output transpose) or numerically equivalent within a stated float bound (encode/decode FFT).
// All secret-dependent arithmetic is branch-free (mask corrections behind an optimisation barrier).
//
//  * Harvey lazy-reduction NTT (Intel HEXL, EF37 Alg. 1): butterfly values stay in [0,4q) (4q < 2^32 for q < 2^30),
//    one correction per butterfly instead of three; same transform/order as core.cpp Plan::transform, so the reduced
//    output is identical.
//  * Decrypt: NTT(s) is cached per key (secret-derived; caller wipes it), so c1*s costs 2 transforms instead of 3.
//  * Encode/decode: the real polynomial p(x) = A(x) + x^{N/2} B(x) evaluated at the slot roots w_k = zeta^(2k+1)
//    (k < N/2, core.cpp order) equals C(x) = A(x) + i B(x) evaluated on the coset zeta * <zeta^4>:
//      k even (k = 2m):   p(w_k) = C(zeta^(4m+1))
//      k odd  (k = 2m+1): p(w_k) = conj(C(zeta^(4(N/2-m-1)+1)))
//    so one N/2-point complex FFT with precomputed twiddles replaces the N-point FFT with recurrence twiddles.
//  * Cloud affine: 30x30-bit products accumulate exactly in 64 bits (16 products < 2^64), one reduction per coefficient.
#pragma once
#include "core.hpp"
#include <array>
#include <cfenv>
#include <cmath>
#include <complex>
#include <cstdint>
#include <cstring>
#include <vector>

namespace edgefhe { namespace fast {

template <class T> inline T barrier(T x) { __asm__ volatile("" : "+r"(x)); return x; }
// r in [0, 2m) -> r mod m, branch-free (m < 2^31, r < 2^32)
inline uint32_t csub(uint32_t r, uint32_t m) {
  const uint32_t t = r - m;
  const uint32_t mask = barrier(uint32_t(0) - (t >> 31));     // all ones iff r < m (t wrapped, top bit set)
  return t + (m & mask);
}
inline uint32_t shoup(uint32_t x, uint32_t w, uint32_t w_shoup, uint32_t q) {   // x < 2^32, w < q -> [0, 2q)
  const uint32_t est = uint32_t((uint64_t(x) * w_shoup) >> 32);
  return uint32_t(x) * w - est * q;                                          // exact mod 2^32, value in [0, 2q)
}

// ---------------------------------------------------------------- NTT (bit-identical to Plan::transform)
struct Ntt {
  const Plan* p;
  std::vector<uint32_t> bitrev_pairs;   // (i, j) with i < j, flattened
  explicit Ntt(const Plan& plan) : p(&plan) {
    for (size_t i = 1, j = 0; i < degree; i++) {
      size_t bit = degree / 2; for (; j & bit; bit >>= 1) j ^= bit; j ^= bit;
      if (i < j) { bitrev_pairs.push_back(uint32_t(i)); bitrev_pairs.push_back(uint32_t(j)); }
    }
  }
  // in-place; input canonical [0,q); output canonical
  void transform(uint32_t* v, bool inverse) const {
    const uint32_t q = p->q, q2 = 2 * q;
    if (!inverse) for (size_t i = 0; i < degree; i++) v[i] = shoup(v[i], p->twist[i], p->twist_shoup[i], q);   // [0,2q)
    for (size_t k = 0; k < bitrev_pairs.size(); k += 2) std::swap(v[bitrev_pairs[k]], v[bitrev_pairs[k + 1]]);
    for (size_t s = 0, h = 1; s < 12; s++, h *= 2) {
      const uint32_t* roots = (inverse ? p->inverse_root[s] : p->root[s]).data();
      const uint32_t* factors = (inverse ? p->inverse_shoup[s] : p->shoup[s]).data();
      for (size_t b = 0; b < degree; b += 2 * h) {
        uint32_t* lo = v + b; uint32_t* hi = v + b + h;
        for (size_t j = 0; j < h; j++) {
          const uint32_t a = csub(lo[j], q2);                         // [0,2q)
          const uint32_t t = shoup(hi[j], roots[j], factors[j], q);   // [0,2q)
          lo[j] = a + t;                                              // [0,4q)
          hi[j] = a - t + q2;                                         // (0,4q)
        }
      }
    }
    if (inverse) for (size_t i = 0; i < degree; i++) v[i] = csub(shoup(v[i], p->untwist[i], p->untwist_shoup[i], q), q);
    else for (size_t i = 0; i < degree; i++) v[i] = csub(csub(v[i], q2), q);
  }
  // Barrett product exactly as Plan::product, branch-free correction
  uint32_t product(uint32_t a, uint32_t b) const {
    const uint64_t t = uint64_t(a) * b;
    const uint64_t quotient = uint64_t((UWide(t) * p->reciprocal) >> 64);
    return csub(uint32_t(t - quotient * p->q), p->q);
  }
};

struct NttSet {
  std::array<Plan, 3> plans{Plan(primes[0], psi[0]), Plan(primes[1], psi[1]), Plan(primes[2], psi[2])};
  std::array<Ntt, 3> ntt{Ntt(plans[0]), Ntt(plans[1]), Ntt(plans[2])};
};

// Secret transform cache: NTT(s) per limb. Secret-derived: wipe() before release.
struct SecretTransform {
  Rns hat;
  void build(const NttSet& set, const Ternary& s) {
    require(s.size() == degree, "secret degree");
    uint32_t bad = 0;
    for (auto x : s) bad |= uint32_t(x < -1 || x > 1);
    require(barrier(bad) == 0, "secret not ternary");
    for (size_t l = 0; l < 3; l++) {
      hat[l].resize(degree);
      const uint32_t q = primes[l];
      for (size_t i = 0; i < degree; i++) {          // branch-free residue of {-1,0,1}
        const uint32_t neg = barrier(uint32_t(0) - uint32_t(s[i] < 0));
        hat[l][i] = (uint32_t(s[i] > 0)) | ((q - 1) & neg);
      }
      set.ntt[l].transform(hat[l].data(), false);
    }
  }
  void wipe() { for (auto& p : hat) if (!p.empty()) { volatile uint32_t* v = p.data(); for (size_t i = 0; i < p.size(); i++) v[i] = 0; } }
  ~SecretTransform() { wipe(); }
};

// c0 + c1*s (mod Q) per limb, identical to add(c0, Engine::multiply(c1, residues(s))). Limb l only (callers may run
// limbs on separate workers). scratch must hold degree words; it ends up holding secret-dependent data -> caller wipes.
inline void decrypt_limb(const NttSet& set, const SecretTransform& sk, const Cipher& c, size_t l, uint32_t* out) {
  const auto& nt = set.ntt[l]; const uint32_t q = primes[l];
  std::memcpy(out, c.c1[l].data(), degree * 4);
  nt.transform(out, false);
  for (size_t i = 0; i < degree; i++) out[i] = nt.product(out[i], sk.hat[l][i]);
  nt.transform(out, true);
  for (size_t i = 0; i < degree; i++) out[i] = csub(out[i] + c.c0[l][i], q);
}

// ---------------------------------------------------------------- canonical-embedding FFT (N/2 points)
struct Fft {
  static constexpr size_t H = degree / 2;            // 2048-point complex FFT
  std::vector<std::complex<double>> zeta_pow;        // zeta^j, j < H        (zeta = exp(i pi / N))
  std::vector<std::complex<double>> roots;           // exp(2 pi i t / H), t < H/2
  std::vector<uint32_t> rev;
  Fft() : zeta_pow(H), roots(H / 2), rev(H) {
    const long double pi = 3.141592653589793238462643383279502884L;
    for (size_t j = 0; j < H; j++) zeta_pow[j] = {double(cosl(pi * j / degree)), double(sinl(pi * j / degree))};
    for (size_t t = 0; t < H / 2; t++) roots[t] = {double(cosl(2 * pi * t / H)), double(sinl(2 * pi * t / H))};
    for (size_t i = 0, j = 0; i < H; i++) { rev[i] = uint32_t(j); size_t bit = H / 2; for (; j & bit; bit >>= 1) j ^= bit; j ^= bit; }
  }
  // forward: y[m] = sum_j x[j] exp(+2 pi i j m / H)   (sign = +1) or exp(-...) (sign = -1); in place
  void dft(std::complex<double>* x, int sign) const {
    for (size_t i = 0; i < H; i++) if (i < rev[i]) std::swap(x[i], x[rev[i]]);
    for (size_t len = 2; len <= H; len *= 2) {
      const size_t half = len / 2, stride = H / len;
      for (size_t start = 0; start < H; start += len)
        for (size_t j = 0; j < half; j++) {
          std::complex<double> w = roots[j * stride];
          if (sign < 0) w = std::conj(w);
          const std::complex<double> b = std::complex<double>(x[start + j + half].real() * w.real() - x[start + j + half].imag() * w.imag(),
                                                              x[start + j + half].real() * w.imag() + x[start + j + half].imag() * w.real());
          const std::complex<double> a = x[start + j];
          x[start + j] = {a.real() + b.real(), a.imag() + b.imag()};
          x[start + j + half] = {a.real() - b.real(), a.imag() - b.imag()};
        }
    }
  }
  static inline std::complex<double> mul(std::complex<double> a, std::complex<double> b) {   // no __muldc3 NaN path
    return {a.real() * b.real() - a.imag() * b.imag(), a.real() * b.imag() + a.imag() * b.real()};
  }
};

// Branch-free rounding (bit-identical to core.cpp nearest_even for finite |v| < 2^63 under FE_TONEAREST)
inline int64_t nearest_even(double value) {
  const double lower = std::floor(value), fraction = value - lower;
  const auto integer = static_cast<int64_t>(lower);
  const uint64_t gt = uint64_t(fraction > 0.5), eq = uint64_t(fraction == 0.5), odd = uint64_t(integer) & 1u;
  return integer + int64_t(barrier(gt | (eq & odd)));
}

// Round half to even for |v| < 2^51 under FE_TONEAREST: (v + 1.5*2^52) - 1.5*2^52 is exact-integer and ties-to-even,
// identical to nearest_even; no floor/branch. volatile-free: barrier() keeps the compiler from folding the pair.
inline int64_t round_magic(double v) {
  const double magic = 0x1.8p52;
  double t = v + magic;
  __asm__ volatile("" : "+m"(t));   // portable: forces the sum to be materialised before subtracting
  return int64_t(t - magic);
}

// encode: same contract as core.cpp encode (slots k < values.size(), zero elsewhere), N/2-point FFT.
inline void encode_into(const Fft& f, const std::vector<std::complex<double>>& values, uint64_t delta, int64_t* out,
                        std::vector<std::complex<double>>& scratch) {
  require(std::fegetround() == FE_TONEAREST, "Encoder requires FE_TONEAREST");
  require(values.size() <= slots && (delta == scale || delta == scale * scale), "Invalid encoder dimensions/scale");
  constexpr size_t H = Fft::H;
  scratch.assign(H, {0.0, 0.0});
  uint32_t bad = 0;
  for (size_t k = 0; k < values.size(); k++) {
    const double re = values[k].real(), im = values[k].imag();
    bad |= uint32_t(!(std::isfinite(re) && std::isfinite(im))) | uint32_t(!(re * re + im * im <= 256.0));
    const size_t m = k >> 1;
    if (k & 1) scratch[H - m - 1] = {re, -im};          // D[H-m-1] = conj(z_k)
    else scratch[m] = {re, im};                         // D[m] = z_k
  }
  require(barrier(bad) == 0, "Slots must be finite with magnitude <=16");
  f.dft(scratch.data(), -1);                            // sum_m D[m] w^{-jm}
  const double scale_d = double(delta) / double(H);
  uint32_t big = 0;
  for (size_t j = 0; j < H; j++) {
    const std::complex<double> c = Fft::mul(scratch[j], std::conj(f.zeta_pow[j]));   // C_j = (1/H) * ... * zeta^{-j}
    const double a = c.real() * scale_d, b = c.imag() * scale_d;
    big |= uint32_t(!(std::fabs(a) < 0x1p63)) | uint32_t(!(std::fabs(b) < 0x1p63));
    big |= uint32_t(delta == scale) & (uint32_t(!(std::fabs(a) < 0x1p51)) | uint32_t(!(std::fabs(b) < 0x1p51)));   // unreachable for |z| <= 16 at 2^30
    if (delta == scale) {      // |a|,|b| <= 16 * 2^30 * ... < 2^51: magic-constant round-half-even (pure FP add/sub)
      out[j] = round_magic(a); out[j + H] = round_magic(b);
    } else {
      out[j] = nearest_even(a); out[j + H] = nearest_even(b);
    }
  }
  require(barrier(big) == 0, "Invalid or oversized encoded coefficient");
}

// int128 -> double without the libgcc magnitude-dependent path. |x| < 2^88: x = h*2^32 + l with h = x >> 32 (signed,
// |h| < 2^56) and l the low 32 bits (exact in double); one rounding in double(h) and one in the fma: <= ~1 ulp.
// (A hi/lo split at 2^64 is wrong for negative x: double(lo) of a ~2^64 unsigned word loses ~2^11.)
inline double wide_to_double(Wide x) {
  const int64_t h = int64_t(x >> 32);
  const uint32_t l = uint32_t(uint64_t(x) & 0xffffffffu);
  return std::fma(double(h), 0x1p32, double(l));
}

// decode: values[k] = p(w_k) / delta for k < 2048 (core.cpp order).
inline void decode_into(const Fft& f, const Wide* coefficient, uint64_t delta, std::complex<double>* out,
                        std::vector<std::complex<double>>& scratch) {
  require(std::fegetround() == FE_TONEAREST, "Decoder requires FE_TONEAREST");
  require(delta == scale || delta == scale * scale, "Invalid decoder scale");
  constexpr size_t H = Fft::H;
  scratch.resize(H);
  const double inv = 1.0 / double(delta);   // power of two: exact
  for (size_t j = 0; j < H; j++)
    scratch[j] = Fft::mul({wide_to_double(coefficient[j]) * inv, wide_to_double(coefficient[j + H]) * inv}, f.zeta_pow[j]);
  f.dft(scratch.data(), +1);                // D[m] = C(zeta^{4m+1})
  for (size_t k = 0; k < slots; k++) {
    const size_t m = k >> 1;
    out[k] = (k & 1) ? std::conj(scratch[H - m - 1]) : scratch[m];
  }
}

// ---------------------------------------------------------------- cloud scalar affine (bit-identical to cloud.cpp)
// input: features x [poly][limb][N] words; weights int64 (|w| <= 16*2^30); bias; output [poly][limb][N].
// Returns false (and leaves output unspecified) on a noncanonical input word.
template <uint32_t Q> inline bool affine_limb(const uint32_t* input, size_t features, const int64_t* weights, int64_t bias,
                                              uint32_t* output, size_t l, uint64_t* acc /* N words scratch */) {
  constexpr size_t N = degree, WORDS = 6 * degree;
  uint64_t w[32];
  for (size_t f = 0; f < features; f++) { int64_t r = weights[f] % int64_t(Q); if (r < 0) r += Q; w[f] = uint64_t(r); }
  uint32_t bad = 0;
  for (size_t p = 0; p < 2; p++) {
    for (size_t i = 0; i < N; i++) acc[i] = 0;
    uint32_t* dst = output + p * 3 * N + l * N;
    for (size_t f0 = 0; f0 < features; f0 += 16) {          // 16 products of < 2^60 each stay below 2^64
      const size_t f1 = f0 + 16 < features ? f0 + 16 : features;
      for (size_t f = f0; f < f1; f++) {
        const uint32_t* x = input + f * WORDS + p * 3 * N + l * N; const uint64_t r = w[f];
        for (size_t i = 0; i < N; i++) { bad |= uint32_t(x[i] >= Q); acc[i] += uint64_t(x[i]) * r; }
      }
      if (f1 < features) for (size_t i = 0; i < N; i++) acc[i] %= Q;
    }
    for (size_t i = 0; i < N; i++) dst[i] = uint32_t(acc[i] % Q);
    if (p == 0) { int64_t r = bias % int64_t(Q); if (r < 0) r += Q; const uint32_t s = dst[0] + uint32_t(r); dst[0] = s >= Q ? s - Q : s; }
  }
  return bad == 0;
}

// ---------------------------------------------------------------- device output -> application layout
// Device payload (per feature f, poly p): beat j holds limbs l = 0,1,2 of coefficients 4j..4j+3 as words
// src[(3j+l)*4 + c]. Application layout: out[f*6N + p*3N + l*N + 4j + c]. Fused canonical check (branch-free).
inline bool transpose_device_output(const uint32_t* dev, size_t k, uint32_t* app) {
  uint32_t bad = 0;
  for (size_t f = 0; f < k; f++) for (size_t p = 0; p < 2; p++) {
    const uint32_t* src = dev + (f * 2 + p) * 1024 * 12;
    uint32_t* dst = app + f * 6 * degree + p * 3 * degree;
    for (size_t j = 0; j < 1024; j++) {
      const uint32_t* b = src + 12 * j;
      for (size_t l = 0; l < 3; l++) {
        const uint32_t q = primes[l];
        uint32_t* d = dst + l * degree + 4 * j;
        for (size_t c = 0; c < 4; c++) { const uint32_t v = b[4 * l + c]; bad |= uint32_t(v >= q); d[c] = v; }
      }
    }
  }
  return bad == 0;
}

}}  // namespace edgefhe::fast
