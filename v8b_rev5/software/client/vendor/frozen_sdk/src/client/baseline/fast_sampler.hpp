// E02 candidate A (2026-10-15): implementation-equivalent encryption sampler. Not used by any accepted path yet.
//
// Same algorithms and draw order as sample_secure_encryption_concurrent (per feature f = 0..3: mask[f] = 4096
// uniform ternary, e0[f] and e1[f] = 4096 sigma-4.5 inversion Gaussians each):
//   ternary  : std::uniform_int_distribution<int>(-1, 1)
//   Gaussian : OpenFHE 1.5.1 DiscreteGaussianGeneratorImpl::GenerateInt (inversion, table of Initialize())
// Both are fed by a uint32 word source with the OpenFHE PRNG min()/max(), so the libstdc++ distribution templates
// run the same code and an identical word stream gives bit-identical material. sampler_profile_selftest det checks
// this against OpenFHE itself and compares the inversion table bit for bit.
// Two kernels (StdKernels = A, DirectKernels = A2, below). What changes: words come from a non-virtual buffered source (no virtual call per word), and the polynomials are
// overwritten in place (no per-polynomial allocation). Production source: getrandom only, EINTR retry, no fallback,
// every word wiped when consumed and the buffer wiped at thread exit.
#pragma once
#include "secure.hpp"
#include <algorithm>
#include <array>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <limits>
#include <random>
#include <sys/random.h>
#include <vector>
namespace edgefhe {

template<class Source> struct WordUrng {
  using result_type = uint32_t;
  static constexpr result_type min() { return std::numeric_limits<result_type>::min(); }
  static constexpr result_type max() { return std::numeric_limits<result_type>::max(); }
  Source& source;
  result_type operator()() { return source.next(); }
};

// Verbatim arithmetic of DiscreteGaussianGeneratorImpl<>::Initialize() (OpenFHE 1.5.1) for a given sigma.
struct InversionTable {
  std::vector<double> vals;
  double a = 0.0;
  explicit InversionTable(double std_dev) {
    double M{12.00610553538285};
    int64_t fin{static_cast<int64_t>(std::ceil(std_dev * M))};
    vals.resize(fin);
    double variance{2 * std_dev * std_dev};
    double cusum{0.0};
    for (int64_t x = 1; x <= fin; ++x)
      vals[x - 1] = (cusum += std::exp(-(static_cast<double>(x * x) / variance)));
    a = 1.0 / (2 * cusum + 1.0);
    for (int64_t x = 0; x < fin; ++x)
      vals[x] *= a;
  }
};

// Verbatim arithmetic of GenerateInt() + FindInVector(), parameterised by how the two primitives are computed:
//   unit(g)      : std::uniform_real_distribution<double>(0, 1)(g)
//   index(t, x)  : std::lower_bound(t.vals, x) - begin + 1 (throws if past the end)
template<class K, class Urng> inline int64_t inversion_gaussian(const InversionTable& t, Urng& g) {
  double seed = K::unit(g) - 0.5;
  double tmp = std::abs(seed) - t.a / 2;
  if (tmp <= 0.0) return 0;
  return K::index(t, tmp) * (seed > 0.0 ? 1 : -1);
}

// A: the library templates themselves.
struct StdKernels {
  template<class G> static double unit(G& g) { return std::uniform_real_distribution<double>(0.0, 1.0)(g); }
  static int64_t index(const InversionTable& t, double tmp) {
    auto lower = std::lower_bound(t.vals.begin(), t.vals.end(), tmp);
    require(lower != t.vals.end(), "DGG inversion sampling: value not found");
    return int64_t(lower - t.vals.begin() + 1);
  }
};

// A2: the same arithmetic written out in double. libstdc++ generate_canonical<double, 53> with a 32-bit generator
// takes m = 2 words: sum = w0 * 1 + w1 * 2^32 (each product exact, one rounding in the addition, also under FMA
// contraction), divides by 2^64 (exact), and maps a result >= 1 to nextafter(1, 0); uniform_real_distribution(0, 1)
// then computes r * (1 - 0) + 0 = r. The library computes the scale factor in long double, which aarch64 emulates
// in software; this version does not. lower_bound on an ascending table is the first index with vals[i] >= x,
// found here by a forward scan (the Gaussian mass sits at the first few entries). Equivalence is a claim to be
// tested, not assumed: sampler_profile_selftest det compares A2 with OpenFHE on the target machine.
struct DirectKernels {
  template<class G> static double unit(G& g) {
    double sum = double(g());
    sum += double(g()) * 4294967296.0;
    double r = sum / 18446744073709551616.0;
    if (__builtin_expect(r >= 1.0, 0)) r = std::nextafter(1.0, 0.0);
    return r;
  }
  static int64_t index(const InversionTable& t, double tmp) {
    const double* v = t.vals.data();
    const size_t n = t.vals.size();
    size_t i = 0;
    while (i < n && v[i] < tmp) ++i;
    require(i != n, "DGG inversion sampling: value not found");
    return int64_t(i + 1);
  }
};

inline const InversionTable& sigma45_table() { static const InversionTable table(4.5); return table; }

template<class K, class Source> void sample_encryption_from(SecureMaterial& m, Source& source) {
  const InversionTable& table = sigma45_table();
  WordUrng<Source> g{source};
  for (size_t f = 0; f < 4; f++) {
    for (auto* p : {&m.e0[f], &m.e1[f]}) if (p->size() != degree) { for (auto& v : *p) v = 0; p->assign(degree, 0); }
    if (m.mask[f].size() != degree) { for (auto& v : m.mask[f]) v = 0; m.mask[f].assign(degree, 0); }
    std::uniform_int_distribution<int> d(-1, 1);
    for (auto& v : m.mask[f]) v = int8_t(d(g));
    for (auto& v : m.e0[f]) v = inversion_gaussian<K>(table, g);
    for (auto& v : m.e1[f]) v = inversion_gaussian<K>(table, g);
  }
}

// Buffered word source. Fill(uint32_t* words, size_t count) must fill all words or throw.
template<size_t N, class Fill> class BufferedWords {
  std::array<uint32_t, N> data_{};
  size_t cursor_ = N;
  Fill fill_;
 public:
  explicit BufferedWords(Fill fill = Fill()) : fill_(fill) {}
  BufferedWords(const BufferedWords&) = delete;
  BufferedWords& operator=(const BufferedWords&) = delete;
  ~BufferedWords() { volatile uint32_t* p = data_.data(); for (size_t i = 0; i < N; i++) p[i] = 0; }
  uint32_t next() {
    if (cursor_ == N) { fill_(data_.data(), N); cursor_ = 0; }
    const uint32_t r = data_[cursor_];
    data_[cursor_++] = 0;
    return r;
  }
};

struct GetrandomFill {
  void operator()(uint32_t* words, size_t count) const {
    auto* p = reinterpret_cast<unsigned char*>(words);
    size_t remaining = count * sizeof(uint32_t);
    while (remaining) {
      const auto n = getrandom(p, remaining, 0);
      if (n < 0 && errno == EINTR) continue;
      require(n > 0, "OS entropy unavailable; no fallback");
      p += n; remaining -= size_t(n);
    }
  }
};
using OsWords = BufferedWords<4096, GetrandomFill>;   // 16 KiB of getrandom per refill

// Production entries: one OS word buffer per thread and kernel, no process-wide lock, no OpenFHE engine involved.
template<class K> inline void sample_secure_encryption_fast(SecureMaterial& m) {
  thread_local OsWords words;
  sample_encryption_from<K>(m, words);
}
}  // namespace edgefhe
