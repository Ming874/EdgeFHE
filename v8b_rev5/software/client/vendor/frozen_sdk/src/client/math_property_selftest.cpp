// Off-board mathematical property tests for the V7 PS core (CPU only, no device).
// Complements v7_selftest (OpenFHE dense products) with boundary, identity and bound checks:
//  1. NTT round trip and negacyclic product vs schoolbook at boundary values (0, 1, q-1, sparse, dense)
//  2. Garner centered CRT vs direct 128-bit reconstruction, including +-(Q-1)/2 and 0
//  3. nearest_even ties-to-even contract
//  4. encode/decode round trip error at scale 2^30 and 2^60 for |z| <= 16
//  5. Encryption identity: c0 + c1*s == m + e*u + e0 + e1*s exactly (integer, centered), and the
//     coefficient noise never exceeds encryption_noise_bound (which is an L1 bound)
//  6. CPU worker counts 1 and 3 give identical ciphertext products
#include "secure.hpp"
#include <cmath>
#include <iostream>
#include <random>
using namespace edgefhe;

static std::vector<Wide> schoolbook(const std::vector<int64_t>& a, const std::vector<int64_t>& b) {
  std::vector<Wide> out(degree);
  for (size_t j = 0; j < degree; j++) if (b[j]) for (size_t i = 0; i < degree; i++) {
    size_t k = i + j; out[k & (degree - 1)] += Wide(a[i]) * b[j] * (k >= degree ? -1 : 1);
  }
  return out;
}
static Wide mod_q(Wide x, uint32_t q) { Wide r = x % q; return r < 0 ? r + q : r; }

int main(int argc, char **argv) {
  try {
    require(argc == 2, "usage: math_property_selftest OS_PLUGIN");
    initialize_secure_sampler(argv[1]);
    Engine one(1), three(3);
    std::mt19937_64 rng(20261013);
    uint64_t checks = 0;
    // 1. Products at boundary patterns, checked against an exact signed schoolbook product mod q.
    for (int pattern = 0; pattern < 6; pattern++) {
      std::vector<int64_t> a(degree), b(degree);
      for (size_t i = 0; i < degree; i++) {
        switch (pattern) {
          case 0: a[i] = 0; b[i] = int64_t(rng() % 1000) - 500; break;
          case 1: a[i] = (i == 0); b[i] = int64_t(rng() % 1000) - 500; break;            // identity
          case 2: a[i] = (i == degree - 1); b[i] = (i == 1); break;                       // X^(N-1)*X = -1
          case 3: a[i] = -1; b[i] = 1; break;                                             // all q-1 times all 1
          case 4: a[i] = (rng() % 64 == 0) ? int64_t(rng() % 3) - 1 : 0; b[i] = int64_t(rng() % 2001) - 1000; break;
          default: a[i] = int64_t(rng() % 2000001) - 1000000; b[i] = int64_t(rng() % 2000001) - 1000000; break;
        }
      }
      auto exact = schoolbook(a, b);
      auto ar = residues(Message(a.begin(), a.end())), br = residues(Message(b.begin(), b.end()));
      auto p1 = one.multiply(ar, br), p3 = three.multiply(ar, br);
      require(p1 == p3, "worker count changed product");
      for (size_t l = 0; l < 3; l++) for (size_t i = 0; i < degree; i++) {
        require(Wide(p1[l][i]) == mod_q(exact[i], primes[l]), "product vs schoolbook"); checks++;
      }
    }
    // 2. Garner centered CRT at boundaries and random values.
    const Wide Q = Wide(primes[0]) * primes[1] * primes[2];
    std::vector<Wide> targets = {0, 1, -1, (Q - 1) / 2, -(Q - 1) / 2, (Q - 1) / 2 - 1, -(Q - 1) / 2 + 1};
    while (targets.size() < degree) {
      Wide x = (Wide(rng()) << 32 | Wide(rng() & 0xffffffffULL)) % Q; targets.push_back(x > Q / 2 ? x - Q : x);
    }
    Rns packed; for (size_t l = 0; l < 3; l++) { packed[l].resize(degree); for (size_t i = 0; i < degree; i++) packed[l][i] = uint32_t(mod_q(targets[i], primes[l])); }
    auto back = centered(packed);
    for (size_t i = 0; i < degree; i++) { require(back[i] == targets[i], "Garner centered CRT"); checks++; }
    // 3. Rounding contract.
    require(nearest_even(0.5) == 0 && nearest_even(1.5) == 2 && nearest_even(-0.5) == 0 && nearest_even(-1.5) == -2 &&
            nearest_even(2.4999) == 2 && nearest_even(-2.5001) == -3, "ties-to-even");
    checks += 6;
    // 4. Encode/decode round trip.
    double worst30 = 0, worst60 = 0;
    for (int t = 0; t < 8; t++) {
      std::vector<std::complex<double>> z(slots);
      for (auto& v : z) v = {std::ldexp(double(int64_t(rng() % 32001) - 16000), -10), 0};
      z[0] = {16, 0}; z[1] = {-16, 0};
      auto e30 = encode(z);
      auto d30 = decode(std::vector<Wide>(e30.begin(), e30.end()), scale);
      auto e60 = encode(z, scale * scale);
      auto d60 = decode(std::vector<Wide>(e60.begin(), e60.end()), scale * scale);
      for (size_t k = 0; k < slots; k++) { worst30 = std::max(worst30, std::abs(d30[k] - z[k])); worst60 = std::max(worst60, std::abs(d60[k] - z[k])); }
    }
    require(worst30 < 1e-5 && worst60 < 1e-9, "encode/decode round trip");
    checks += 2;
    // 5. Encryption identity and noise bound.
    uint64_t identity = 0; double tightest = 1e300;
    for (int t = 0; t < 6; t++) {
      auto m = sample_secure_material(); auto key = keygen_secure(three, m);
      std::array<std::vector<std::complex<double>>, 4> x;
      for (auto& f : x) { f.resize(slots); for (auto& v : f) v = {double(int64_t(rng() % 2001) - 1000) / 125.0, 0}; }
      auto msg = three.encode_four(x);
      sample_secure_encryption(m);
      auto ct = encrypt_secure(three, key, msg, m);
      std::vector<int64_t> s(m.secret.begin(), m.secret.end()), e(m.error.begin(), m.error.end());
      for (size_t f = 0; f < 4; f++) {
        auto dec = decrypt_secure(three, ct[f], m.secret);
        std::vector<int64_t> u(m.mask[f].begin(), m.mask[f].end());
        auto eu = schoolbook(e, u), e1s = schoolbook(std::vector<int64_t>(m.e1[f].begin(), m.e1[f].end()), s);
        Wide worst = 0;
        for (size_t i = 0; i < degree; i++) {
          const Wide noise = eu[i] + m.e0[f][i] + e1s[i];
          require(dec[i] == Wide(msg[f][i]) + noise, "exact decryption identity"); identity++;
          worst = std::max(worst, noise < 0 ? -noise : noise);
        }
        const auto bound = encryption_noise_bound(m, f);
        require(UWide(worst) <= bound, "noise bound violated");
        tightest = std::min(tightest, double(bound) / double(worst > 0 ? worst : 1));
      }
    }
    checks += identity;
    std::cout << "V7_MATH_PROPERTY_SELFTEST_PASS checks=" << checks << " encode_err_2^30=" << worst30
              << " encode_err_2^60=" << worst60 << " identity_coefficients=" << identity
              << " min_bound_over_observed_noise=" << tightest << "\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "V7_MATH_PROPERTY_SELFTEST_FAIL " << e.what() << '\n';
    return 1;
  }
}
