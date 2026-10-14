#include "edge/qa_random.hpp"
#include <random>

namespace edgefhe::v5 {
namespace {
// Same shapes as the OS sampler (fixed-weight ternary, uniform residues) from a fully
// specified generator, so the output is identical on every platform.
class QaRandom {
 public:
  QaRandom(uint64_t seed, uint64_t stream) : rng_(mix(seed) ^ mix(stream + 0x9E3779B97F4A7C15ull)) {}

  uint64_t below(uint64_t bound) {
    const uint64_t threshold = (0 - bound) % bound;
    for (;;) {
      const uint64_t x = rng_();
      if (x >= threshold) return x % bound;
    }
  }

  Ternary sparse(size_t weight) {
    Ternary p(degree);
    size_t count = 0;
    while (count < weight) {
      const auto i = below(degree);
      if (p[i]) continue;
      p[i] = below(2) ? 1 : -1;
      count++;
    }
    return p;
  }

 private:
  static uint64_t mix(uint64_t x) {  // splitmix64 finalizer
    x += 0x9E3779B97F4A7C15ull;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ull;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBull;
    return x ^ (x >> 31);
  }
  std::mt19937_64 rng_;
};
}  // namespace

Material qa_material(uint64_t seed) {
  QaRandom rng(seed, ~0ull);
  Material m;
  m.secret = rng.sparse(64);
  m.error = rng.sparse(16);
  for (size_t f = 0; f < 4; f++) {
    m.mask[f] = rng.sparse(64);
    m.e0[f] = rng.sparse(16);
    m.e1[f] = rng.sparse(16);
  }
  for (size_t l = 0; l < limbs; l++) {
    m.a[l].resize(degree);
    for (auto& x : m.a[l]) x = uint32_t(rng.below(primes[l]));
  }
  return m;
}

std::array<Ternary, 12> qa_samples(uint64_t seed, uint64_t batch) {
  QaRandom rng(seed, batch);
  std::array<Ternary, 12> out;
  for (size_t i = 0; i < 12; i++) out[i] = rng.sparse(i < 4 ? 64 : 16);
  return out;
}
}  // namespace edgefhe::v5
