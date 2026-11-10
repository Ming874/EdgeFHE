// Off-board check of the optional OpenFHE-compatible slot order (slot_order.hpp).
//  1. the 5^k slot map is a bijection onto the legacy slots;
//  2. encode/decode round trip in both orders;
//  3. rotation semantics: in the 5^k order X -> X^(5^r) is the cyclic rotation by r (OpenFHE EvalRotate
//     convention) and X -> X^(2N-1) is conjugation; in the legacy order X -> X^5 is NOT a cyclic shift;
//  4. slot-wise product (negacyclic product, CRT, decode at scale^2) in both orders;
//  5. the 5^k-order coefficients equal OpenFHE 1.5.1 MakeCKKSPackedPlaintext coefficients (|diff| <= 1).
// Mathematical/software evidence only (x86); no board, no DMA, no change to the accepted encoder.
#include "slot_order.hpp"
#include "openfhe.h"
#include <iostream>
#include <random>
using namespace edgefhe;

static double max_err(const std::vector<std::complex<double>>& a, const std::vector<std::complex<double>>& b, size_t n) {
  double e = 0; for (size_t i = 0; i < n; i++) e = std::max(e, std::abs(a[i] - b[i])); return e;
}
static std::vector<Wide> wide(const Message& m) { return std::vector<Wide>(m.begin(), m.end()); }

int main() {
  try {
    std::mt19937_64 rng(2026101302);
    std::uniform_real_distribution<double> u(-4.0, 4.0);
    const auto& map = openfhe_slot_map();
    std::array<bool, slots> hit{};
    for (size_t k = 0; k < slots; k++) { require(!hit[map.legacy[k]], "bijection"); hit[map.legacy[k]] = true; }
    std::vector<std::complex<double>> z(slots), w(slots);
    for (size_t i = 0; i < slots; i++) { z[i] = {u(rng), u(rng)}; w[i] = {u(rng), u(rng)}; }
    double round_trip = 0, rot = 0, conj_err = 0, product = 0;
    for (auto order : {SlotOrder::legacy_2k_plus_1, SlotOrder::openfhe_5k}) {
      round_trip = std::max(round_trip, max_err(decode_ordered(wide(encode_ordered(z, scale, order)), scale, order), z, slots));
      Engine cpu(1);
      auto p = centered(cpu.multiply(residues(encode_ordered(z, scale, order)), residues(encode_ordered(w, scale, order))));
      auto zw = decode_ordered(p, scale * scale, order);
      for (size_t i = 0; i < slots; i++) product = std::max(product, std::abs(zw[i] - z[i] * w[i]));
    }
    require(round_trip < 1e-6 && product < 1e-5, "round trip / slot-wise product");
    const auto enc5 = encode_ordered(z, scale, SlotOrder::openfhe_5k);
    for (uint64_t r : {1u, 3u, 100u, unsigned(slots - 1)}) {
      uint64_t g = 1; for (uint64_t i = 0; i < r; i++) g = g * 5 % (2 * degree);
      auto out = decode_ordered(wide(automorphism(enc5, g)), scale, SlotOrder::openfhe_5k);
      for (size_t k = 0; k < slots; k++) rot = std::max(rot, std::abs(out[k] - z[(k + r) % slots]));
    }
    auto conj_out = decode_ordered(wide(automorphism(enc5, 2 * degree - 1)), scale, SlotOrder::openfhe_5k);
    for (size_t k = 0; k < slots; k++) conj_err = std::max(conj_err, std::abs(conj_out[k] - std::conj(z[k])));
    require(rot < 1e-6 && conj_err < 1e-6, "5^k order: automorphisms are rotations / conjugation");
    // Legacy order: X -> X^5 must not equal any cyclic shift (documents why the option exists).
    auto legacy_rot = decode_ordered(wide(automorphism(encode(z, scale), 5)), scale, SlotOrder::legacy_2k_plus_1);
    double best_shift = 1e300;
    for (size_t s = 0; s < slots; s++) {
      double e = 0; for (size_t k = 0; k < slots && e < best_shift; k++) e = std::max(e, std::abs(legacy_rot[k] - z[(k + s) % slots]));
      best_shift = std::min(best_shift, e);
    }
    require(best_shift > 1e-2, "legacy order rotation unexpectedly cyclic");
    // OpenFHE 1.5.1 reference encoder at the same N, slots and scale 2^30. Slot-order v2 showed that the default
    // CKKSDataType REAL keeps only the real part (complex inputs gave a fitted ratio ~0.5), so real vectors are
    // compared in a REAL context and complex vectors (which also fix the conjugation direction) in a COMPLEX one.
    auto context = [](lbcrypto::CKKSDataType type) {
      lbcrypto::CCParams<lbcrypto::CryptoContextCKKSRNS> params;
      params.SetMultiplicativeDepth(1); params.SetScalingModSize(30); params.SetFirstModSize(60);
      params.SetRingDim(degree); params.SetBatchSize(slots); params.SetCKKSDataType(type);
      params.SetScalingTechnique(lbcrypto::FIXEDMANUAL); params.SetSecurityLevel(lbcrypto::HEStd_NotSet);
      auto cc = lbcrypto::GenCryptoContext(params); cc->Enable(lbcrypto::PKE); return cc;
    };
    auto real_cc = context(lbcrypto::REAL), complex_cc = context(lbcrypto::COMPLEX);
    size_t compared = 0, exact = 0; int64_t worst = 0;
    for (int trial = 0; trial < 4; trial++) {
      const bool real = trial < 2;
      auto& cc = real ? real_cc : complex_cc;
      std::vector<std::complex<double>> v(slots);
      for (auto& x : v) x = real ? std::complex<double>(u(rng), 0) : std::complex<double>(u(rng), u(rng));
      auto pt = cc->MakeCKKSPackedPlaintext(v, 1, 0, nullptr, slots);
      auto element = pt->GetElement<lbcrypto::DCRTPoly>(); element.SetFormat(Format::COEFFICIENT);
      auto big = element.CRTInterpolate(); const auto modulus = big.GetModulus(), half = modulus >> 1;
      const auto ours = encode_ordered(v, scale, SlotOrder::openfhe_5k);
      std::vector<int64_t> ref(degree);
      for (size_t i = 0; i < degree; i++) {
        const auto x = big[i];
        ref[i] = x > half ? -int64_t((modulus - x).ConvertToInt<uint64_t>()) : int64_t(x.ConvertToInt<uint64_t>());
        const int64_t d = std::llabs(ref[i] - ours[i]);
        worst = std::max(worst, d); exact += d == 0; compared++;
      }
      // Diagnostics (stderr): fitted ratio and residual against candidate conventions.
      std::vector<std::complex<double>> vc(v.size()); for (size_t i = 0; i < v.size(); i++) vc[i] = std::conj(v[i]);
      const std::pair<const char*, Message> cands[] = {{"5k", ours}, {"5k_conj", encode_ordered(vc, scale, SlotOrder::openfhe_5k)},
                                                       {"legacy", encode(v, scale)}};
      for (const auto& [name, c] : cands) {
        long double num = 0, den = 0; for (size_t i = 0; i < degree; i++) { num += (long double)ref[i] * c[i]; den += (long double)c[i] * c[i]; }
        const long double ratio = num / den; long double res = 0;
        for (size_t i = 0; i < degree; i++) res = std::max(res, std::fabs((long double)ref[i] - ratio * c[i]));
        std::cerr << "DIAG trial=" << trial << " cand=" << name << " ratio=" << (double)ratio << " max_residual=" << (double)res
                  << " ref0=" << ref[0] << " ours0=" << c[0] << " modulus_bits=" << modulus.GetMSB() << '\n';
      }
    }
    require(worst <= 1, "5^k-order coefficients match OpenFHE MakeCKKSPackedPlaintext within 1");
    std::cout << "V7_SLOT_ORDER_SELFTEST_PASS bijection=" << slots << " round_trip_err=" << round_trip
              << " slotwise_product_err=" << product << " rotation_err=" << rot << " conjugation_err=" << conj_err
              << " legacy_best_cyclic_shift_err=" << best_shift << " openfhe_coefficients=" << compared
              << " openfhe_exact=" << exact << " openfhe_max_abs_diff=" << worst << " accepted_encoder_unchanged=1\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "V7_SLOT_ORDER_SELFTEST_FAIL " << e.what() << '\n';
    return 1;
  }
}
