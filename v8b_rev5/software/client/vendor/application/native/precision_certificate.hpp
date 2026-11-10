// B04 candidate (2026-10-19): owner-side per-key precision certificate for public scalar affine models.
// Isolated research candidate; not linked into owner.cpp, the frozen SDK or any board package.
//
// For the fixed profile (N=4096, 2048 slots, scale 2^30, output scale 2^60) the decoded error of slot k is
//   quantisation + encoder rounding + decode float + N_k,
//   N_k = sum_i (W_i/2^60) [e(w_k) u_i(w_k) + e0_i(w_k) + s(w_k) e1_i(w_k)],  w_k = exp(i*pi*(2k+1)/N).
// Given the key, Re N_k is sub-Gaussian with proxy V_k = sum_i (W_i/2^60)^2 (N/2) (su2 |e(w_k)|^2 + se2 (1+|s(w_k)|^2)),
// su2 = 2/3 (uniform ternary u, exact certificate), se2 = 20.4525 (CDT63 table law, exact even-moment certificate).
// Union over 2048 slots: P(max_k |Re N_k| > t) <= 4096 exp(-t^2/(2 V_max)) per decryption.
// Derivation, assumptions and the Python oracle: ../B04_PRECISION_CONTRACT_zh-TW.md, ../b04_bound.py.
//
// The evaluations e(w_k), s(w_k) are secret-derived: they are wiped before return and the certificate itself
// (two doubles) must stay owner-only. It is a function of the key, so compute it once per key, not per request.
#pragma once
#include <algorithm>
#include <cmath>
#include <complex>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace edgefhe { namespace b04 {

constexpr size_t N = 4096, SLOTS = 2048;
constexpr double DELTA = 1073741824.0;               // 2^30
constexpr double DELTA2 = 1152921504606846976.0;     // 2^60
constexpr double SIGMA_U2 = 2.0 / 3.0;
constexpr double SIGMA_E2 = 20.4525;                 // certified proxy for the CDT63 table (b04_bound.py)
constexpr double ROUNDING_L1 = 2064.0;               // core.cpp budget; b04_bound.py proves <= 2060.008
constexpr double U53 = 1.1102230246251565e-16;       // 2^-53

inline void require(bool ok, const char* what) { if (!ok) throw std::invalid_argument(what); }

struct KeyCertificate {
  double Ae = 0;  // >= max_k |e(w_k)|
  double As = 0;  // >= max_k |s(w_k)|
};

namespace detail {
inline void wipe(std::vector<double>& v) {
  volatile double* p = v.data();
  for (size_t i = 0; i < v.size(); i++) p[i] = 0.0;
}
inline void wipe(std::vector<std::complex<double>>& v) {
  // std::complex<double> is layout-compatible with double[2] ([complex.numbers.general]).
  volatile double* p = reinterpret_cast<double*>(v.data());
  for (size_t i = 0; i < 2 * v.size(); i++) p[i] = 0.0;
}
// max_k |p(w_k)| over all N odd roots (= max over 2048 slots for real p). Radix-2 FFT with directly computed
// twiddles (std::polar per index, no recurrence). Returns the float maximum plus a rigorous-margin inflation:
// |error| <= eta * sqrt(N) * ||p||_2 with eta <= 1e-12 (Higham Thm 24.2 with mu ~ 2u gives ~1e-14).
inline double max_evaluation(const std::vector<double>& p) {
  require(p.size() == N, "certificate degree");
  std::vector<std::complex<double>> a(N);
  const double pi = std::acos(-1.0);
  double l2 = 0;
  for (size_t j = 0; j < N; j++) { a[j] = p[j] * std::polar(1.0, pi * double(j) / double(N)); l2 += p[j] * p[j]; }
  for (size_t i = 1, j = 0; i < N; i++) {
    size_t bit = N >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) std::swap(a[i], a[j]);
  }
  for (size_t len = 2; len <= N; len <<= 1)
    for (size_t start = 0; start < N; start += len)
      for (size_t k = 0; k < len / 2; k++) {
        const auto w = std::polar(1.0, 2 * pi * double(k) / double(len));
        const auto x = a[start + k], y = a[start + k + len / 2] * w;
        a[start + k] = x + y; a[start + k + len / 2] = x - y;
      }
  double out = 0;
  for (auto& z : a) out = std::max(out, std::abs(z));
  wipe(a);
  require(std::isfinite(out), "certificate evaluation");
  return out + 1e-12 * std::sqrt(double(N)) * std::sqrt(l2) + 1e-9;
}
}  // namespace detail

inline KeyCertificate key_certificate(const std::vector<int64_t>& error, const std::vector<int8_t>& secret) {
  require(error.size() == N && secret.size() == N, "certificate key degree");
  std::vector<double> e(N), s(N);
  for (size_t j = 0; j < N; j++) {
    require(error[j] >= -55 && error[j] <= 55, "certificate keygen error outside [-55,55]");
    require(secret[j] >= -1 && secret[j] <= 1, "certificate secret not ternary");
    e[j] = double(error[j]); s[j] = double(secret[j]);
  }
  KeyCertificate c;
  c.Ae = detail::max_evaluation(e);
  c.As = detail::max_evaluation(s);
  detail::wipe(e); detail::wipe(s);
  return c;
}

struct AffineBound {
  double noise_std_proxy, noise_tail, quantisation, rounding, decode_float, total;
};

// weights_int/bias_int: model integers (W = round_even(w*2^30), B = round_even(b*2^60));
// weights/bias: the real model the result is compared with; failure_log2: per-decryption failure 2^-failure_log2.
inline AffineBound affine_bound(const KeyCertificate& c, const std::vector<int64_t>& weights_int, int64_t bias_int,
                                const std::vector<double>& weights, double bias, double input_abs_max, double failure_log2) {
  require(!weights_int.empty() && weights_int.size() == weights.size() && weights.size() <= 32, "bound model size");
  require(std::isfinite(c.Ae) && std::isfinite(c.As) && c.Ae >= 0 && c.As >= 0, "bound certificate");
  require(std::isfinite(input_abs_max) && input_abs_max > 0 && input_abs_max <= 8, "bound input range");
  require(std::isfinite(failure_log2) && failure_log2 >= 1 && failure_log2 <= 128, "bound failure exponent");
  require(std::isfinite(bias), "bound bias");
  double sumW2 = 0, l1W = 0, quant = 0, ymax = std::abs(bias);
  for (size_t i = 0; i < weights.size(); i++) {
    require(std::isfinite(weights[i]) && std::abs(weights[i]) <= 16, "bound weight");
    require(weights_int[i] > -(int64_t(1) << 40) && weights_int[i] < (int64_t(1) << 40), "bound weight integer");
    const double wi = double(weights_int[i]) / DELTA2;  // exact: |W| < 2^53
    sumW2 += wi * wi; l1W += std::abs(wi);
    quant += std::abs(double(weights_int[i]) / DELTA - weights[i]) * input_abs_max;
    ymax += std::abs(weights[i]) * input_abs_max;
  }
  quant += std::abs(double(bias_int) / DELTA2 - bias);
  sumW2 *= 1 + 64 * U53; l1W *= 1 + 64 * U53; quant = quant * (1 + 64 * U53) + 64 * U53 * (ymax + 1);
  const double V = sumW2 * (double(N) / 2) * (SIGMA_U2 * c.Ae * c.Ae + SIGMA_E2 + SIGMA_E2 * c.As * c.As);
  const double log_union = std::log(2.0 * double(SLOTS)) + failure_log2 * std::log(2.0);
  AffineBound b{};
  b.noise_std_proxy = std::sqrt(V);
  b.noise_tail = std::sqrt(2 * V * log_union) * (1 + 1e-12);
  b.rounding = l1W * ROUNDING_L1;
  // decode float: (Higham FFT rel + twist/convert) * sqrt(N) * (ymax + 1); constants as in b04_bound.py float_bounds
  const double mu = 2048 * 8 * U53, g4 = 4 * U53 / (1 - 4 * U53), eta = mu + g4 * (std::sqrt(2.0) + mu);
  const double rel = 12 * eta / (1 - 12 * eta) + 64 * U53 + 2 * U53;
  b.decode_float = rel * std::sqrt(double(N)) * (ymax + 1);
  b.quantisation = quant;
  b.total = b.noise_tail + b.rounding + b.decode_float + b.quantisation;
  require(std::isfinite(b.total), "bound overflow");
  return b;
}

}}  // namespace edgefhe::b04
