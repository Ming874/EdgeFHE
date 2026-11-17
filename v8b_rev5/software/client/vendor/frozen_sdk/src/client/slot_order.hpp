// Optional OpenFHE-compatible CKKS slot order (extension candidate, 2026-10-13; not used by accepted gates).
//
// The accepted encoder maps slot k to the root zeta^(2k+1) (zeta = exp(i*pi/N)) and slot N-1-k to its
// conjugate. Edge and cloud both use that encoder, so slot-wise affine maps and per-slot weights are already
// consistent. Rotations are not: the Galois automorphism X -> X^(5^r) that OpenFHE's EvalRotate(r) uses is a
// cyclic shift only when slot k is the root zeta^(5^k mod 2N). This wrapper re-indexes slots into that order
// around the unchanged encoder/decoder, so it inherits their exact rounding and validation:
//   5^k mod 2N =  2j+1  -> legacy slot j holds z_k
//   5^k mod 2N = -(2j+1)-> legacy slot j holds conj(z_k)
// {+-5^k : k < N/2} covers every odd residue mod 2N exactly once, so this is a bijection.
#pragma once
#include "baseline/core.hpp"
#include <stdexcept>

namespace edgefhe {

struct SlotMap { std::array<uint32_t, slots> legacy; std::array<bool, slots> conjugated; };

inline const SlotMap& openfhe_slot_map() {
  static const SlotMap map = [] {
    SlotMap m{}; std::array<bool, slots> used{};
    uint64_t e = 1;                                   // 5^k mod 2N
    for (size_t k = 0; k < slots; k++, e = e * 5 % (2 * degree)) {
      const bool upper = e > degree;                  // exponent in (N, 2N): conjugate of 2N - e
      const uint64_t odd = upper ? 2 * degree - e : e;
      const size_t j = (odd - 1) / 2;
      if (j >= slots || used[j]) throw std::logic_error("slot map is not a bijection");
      used[j] = true; m.legacy[k] = uint32_t(j); m.conjugated[k] = upper;
    }
    return m;
  }();
  return map;
}

enum class SlotOrder { legacy_2k_plus_1, openfhe_5k };

inline Message encode_ordered(const std::vector<std::complex<double>>& z, uint64_t delta, SlotOrder order) {
  if (order == SlotOrder::legacy_2k_plus_1) return encode(z, delta);
  if (z.size() > slots) throw std::invalid_argument("too many slots");
  const auto& m = openfhe_slot_map();
  std::vector<std::complex<double>> legacy(slots);    // zero padding, like OpenFHE's packed encoding
  for (size_t k = 0; k < z.size(); k++) legacy[m.legacy[k]] = m.conjugated[k] ? std::conj(z[k]) : z[k];
  return encode(legacy, delta);
}

inline std::vector<std::complex<double>> decode_ordered(const std::vector<Wide>& c, uint64_t delta, SlotOrder order) {
  auto legacy = decode(c, delta);
  if (order == SlotOrder::legacy_2k_plus_1) return legacy;
  const auto& m = openfhe_slot_map();
  std::vector<std::complex<double>> z(slots);
  for (size_t k = 0; k < slots; k++) z[k] = m.conjugated[k] ? std::conj(legacy[m.legacy[k]]) : legacy[m.legacy[k]];
  return z;
}

// X -> X^g on a coefficient vector (g odd): coefficient i moves to i*g mod 2N, negated when it lands >= N.
template<class T> std::vector<T> automorphism(const std::vector<T>& c, uint64_t g) {
  if (c.size() != degree || g % 2 == 0) throw std::invalid_argument("automorphism shape");
  std::vector<T> out(degree);
  for (size_t i = 0; i < degree; i++) {
    const uint64_t t = uint64_t(i) * g % (2 * degree);
    if (t < degree) out[t] = c[i]; else out[t - degree] = -c[i];
  }
  return out;
}
}  // namespace edgefhe
