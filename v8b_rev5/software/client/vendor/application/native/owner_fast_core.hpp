// app v3 (2026-10-19): owner-side fast math built on edgefhe_fast.hpp. Isolated candidate; frozen SDK unchanged.
// cpu_encrypt_limb is the CPU backend of ef_owner_encrypt: bit-identical to Engine::encrypt_residues as called by
// edgefhe_v7::cpu_encrypt_secure_fast (c0 = b*u + e0 + m, c1 = a*u + e1, exact mod each prime), with the public-key
// spectra cached and the Harvey NTT. Validation is the same as cpu_encrypt_secure_fast and runs on the caller first.
#pragma once
#include "edgefhe_fast.hpp"
#include "baseline/secure.hpp"
#include <array>
#include <cstring>
#include <vector>

namespace edgefhe { namespace v3 {

struct KeySpectra {
  Rns b_hat, a_hat;
  void build(const fast::NttSet& set, const PublicKey& key) {
    validate(key.b); validate(key.a);
    for (size_t l = 0; l < 3; l++) {
      b_hat[l] = key.b[l]; a_hat[l] = key.a[l];
      set.ntt[l].transform(b_hat[l].data(), false); set.ntt[l].transform(a_hat[l].data(), false);
    }
  }
};

inline void validate_material(const std::array<Message, 4>& message, const SecureMaterial& m) {
  for (size_t f = 0; f < 4; f++) {
    require(m.mask[f].size() == degree, "Ternary degree");
    for (auto v : m.mask[f]) require(v >= -1 && v <= 1, "Uniform ternary coefficient");
    for (const auto* e : {&m.e0[f], &m.e1[f]}) { require(e->size() == degree, "Gaussian degree"); for (auto v : *e) require(v >= -55 && v <= 55, "Pinned inversion sampler support mismatch"); }
    require(message[f].size() == degree, "Integer polynomial degree mismatch");
  }
}

template <uint32_t q> inline uint32_t small_residue(int64_t x) { return uint32_t(x < 0 ? int64_t(q) + x : x); }   // |x| < q
template <uint32_t q> inline uint32_t wide_residue(int64_t x) { int64_t r = x % int64_t(q); return uint32_t(r < 0 ? r + int64_t(q) : r); }

// One limb, four features. scratch: 3 * degree words (wiped by the caller's owner of the scratch at the end).
template <size_t L> inline void cpu_encrypt_limb(const fast::NttSet& set, const KeySpectra& ks, const std::array<Message, 4>& message,
                                                 const SecureMaterial& m, std::array<Cipher, 4>& out, uint32_t* scratch) {
  constexpr uint32_t q = primes[L];
  const auto& nt = set.ntt[L];
  uint32_t* u = scratch; uint32_t* bu = scratch + degree; uint32_t* au = scratch + 2 * degree;
  for (size_t f = 0; f < 4; f++) {
    for (size_t i = 0; i < degree; i++) u[i] = small_residue<q>(m.mask[f][i]);
    nt.transform(u, false);
    for (size_t i = 0; i < degree; i++) { bu[i] = nt.product(ks.b_hat[L][i], u[i]); au[i] = nt.product(ks.a_hat[L][i], u[i]); }
    nt.transform(bu, true); nt.transform(au, true);
    auto& c0 = out[f].c0[L]; auto& c1 = out[f].c1[L]; c0.resize(degree); c1.resize(degree);
    for (size_t i = 0; i < degree; i++) {
      c0[i] = fast::csub(fast::csub(bu[i] + small_residue<q>(m.e0[f][i]), q) + wide_residue<q>(message[f][i]), q);
      c1[i] = fast::csub(au[i] + small_residue<q>(m.e1[f][i]), q);
    }
  }
  explicit_bzero(scratch, 3 * degree * sizeof(uint32_t));
}

}}  // namespace edgefhe::v3
