// E02 sampler profile and candidate A / A2 check (2026-10-15). CPU only: no device, no sudo.
//
//   sampler_profile_selftest det TEST_ENGINE.so
//     TEST-ONLY deterministic engine. (1) The candidates' inversion table equals OpenFHE's private m_vals / m_a bit
//     for bit. (2) On fresh threads (each replays the same engine stream) candidates A and A2 fed from the OpenFHE
//     engine, and through the production BufferedWords code with buffer sizes 1, 7 and 4096, gives material
//     bit-identical to sample_secure_encryption_concurrent, also with three threads sampling at once. (3) A2's
//     double-only unit() and scan index() equal the library versions on ~2M crafted words and every table boundary.
//   sampler_profile_selftest profile OS_ENGINE.so REPS JOBS
//     Production OS engine. Single-thread medians over REPS of: getrandom for one job's words, the same words
//     through the OpenFHE engine (virtual call per word), current ternary 4x4096, current Gaussian 8x4096, the
//     current job, the A and A2 jobs, and their conversion alone (words prefilled from getrandom outside
//     the timer), for candidates A and A2. Then jobs/s for current, A and A2 with P = 1, 2, 3 threads (JOBS per
//     thread), and pre-registered chi-square / CDF checks (p = 1e-4 per test) of A2's output (separate untimed run)
//     per thread and pooled.
//     Timings are observations on the machine that runs it; a distribution check is not a cryptographic proof.
#include "baseline/fast_sampler.hpp"
#include "openfhe.h"
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <dlfcn.h>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
using namespace edgefhe;
using Clock = std::chrono::steady_clock;
using Dgg = lbcrypto::DiscreteGaussianGeneratorImpl<lbcrypto::NativeVector>;

// Standard-conforming access to OpenFHE's private table for the bitwise comparison (explicit instantiation
// ignores access checks). Test code only.
struct ValsTag { using type = std::vector<double> Dgg::*; friend type get(ValsTag); };
struct ATag { using type = double Dgg::*; friend type get(ATag); };
template<class Tag, typename Tag::type M> struct Rob { friend typename Tag::type get(Tag) { return M; } };
template struct Rob<ValsTag, &Dgg::m_vals>;
template struct Rob<ATag, &Dgg::m_a>;

namespace {
void req(bool ok, const std::string& message) { if (!ok) throw std::runtime_error(message); }
struct Snapshot { std::array<Ternary, 4> mask; std::array<Message, 4> e0, e1; };
bool operator==(const Snapshot& a, const Snapshot& b) { return a.mask == b.mask && a.e0 == b.e0 && a.e1 == b.e1; }
Snapshot take(const SecureMaterial& m) { Snapshot s; s.mask = m.mask; s.e0 = m.e0; s.e1 = m.e1; return s; }

struct EngineSource { uint32_t next() { return lbcrypto::PseudoRandomNumberGenerator::GetPRNG()(); } };
struct EngineFill { void operator()(uint32_t* w, size_t n) const { for (size_t i = 0; i < n; i++) w[i] = lbcrypto::PseudoRandomNumberGenerator::GetPRNG()(); } };

template<class F> std::vector<std::vector<Snapshot>> together(int n, F f) {
  std::mutex mu; std::condition_variable cv; int ready = 0; bool go = false;
  std::vector<std::vector<Snapshot>> out(n); std::vector<std::thread> t; std::vector<std::exception_ptr> err(n);
  for (int i = 0; i < n; i++) t.emplace_back([&, i] {
    { std::unique_lock<std::mutex> g(mu); ready++; cv.notify_all(); cv.wait(g, [&] { return go; }); }
    try { out[i] = f(i); } catch (...) { err[i] = std::current_exception(); }
  });
  { std::unique_lock<std::mutex> g(mu); cv.wait(g, [&] { return ready == n; }); go = true; cv.notify_all(); }
  for (auto& x : t) x.join();
  for (auto& e : err) if (e) std::rethrow_exception(e);
  return out;
}

uint64_t unit_edge_checks();
const int K = 6;
std::vector<Snapshot> draw_current() { SecureMaterial m; std::vector<Snapshot> o; for (int i = 0; i < K; i++) { sample_secure_encryption_concurrent(m); o.push_back(take(m)); } return o; }
template<class Kn, class S> std::vector<Snapshot> draw_with(S& s) { SecureMaterial m; std::vector<Snapshot> o; for (int i = 0; i < K; i++) { sample_encryption_from<Kn>(m, s); o.push_back(take(m)); } return o; }
template<class Kn> std::vector<Snapshot> draw_engine() { EngineSource s; return draw_with<Kn>(s); }
template<class Kn, size_t N> std::vector<Snapshot> draw_buffered() { BufferedWords<N, EngineFill> s; return draw_with<Kn>(s); }

int det_mode(const std::string& plugin) {
  initialize_secure_sampler(plugin);
  void* h = dlopen(plugin.c_str(), RTLD_NOW | RTLD_NOLOAD);
  req(h != nullptr && dlsym(h, "edgefhe_test_engines_created") != nullptr, "refusing to run det mode with a non-test engine");
  const Dgg dgg(4.5);
  const auto& vals = dgg.*get(ValsTag());
  const double a = dgg.*get(ATag());
  const auto& mine = sigma45_table();
  req(vals.size() == 55 && mine.vals.size() == vals.size(), "inversion table length 55");
  req(std::memcmp(vals.data(), mine.vals.data(), vals.size() * sizeof(double)) == 0 && std::memcmp(&a, &mine.a, sizeof a) == 0,
      "inversion table differs from OpenFHE bit for bit");
  const auto ref = together(1, [](int) { return draw_current(); })[0];
  for (int i = 1; i < K; i++) req(!(ref[i] == ref[0]), "consecutive samples must differ");
  req(together(1, [](int) { return draw_engine<StdKernels>(); })[0] == ref, "A (engine words) differs from current sampler");
  req(together(1, [](int) { return draw_engine<DirectKernels>(); })[0] == ref, "A2 (engine words) differs from current sampler");
  req(together(1, [](int) { return draw_buffered<StdKernels, 1>(); })[0] == ref, "A (buffer 1) differs");
  req(together(1, [](int) { return draw_buffered<DirectKernels, 7>(); })[0] == ref, "A2 (buffer 7) differs");
  req(together(1, [](int) { return draw_buffered<StdKernels, 4096>(); })[0] == ref, "A (buffer 4096) differs");
  req(together(1, [](int) { return draw_buffered<DirectKernels, 4096>(); })[0] == ref, "A2 (buffer 4096) differs");
  for (auto& r : together(3, [](int i) { return i == 0 ? draw_buffered<DirectKernels, 4096>() : i == 1 ? draw_engine<StdKernels>() : draw_current(); }))
    req(r == ref, "concurrent threads interfered");
  const uint64_t edge = unit_edge_checks();
  std::cout << "V7_FAST_SAMPLER_DETERMINISTIC_EQUIVALENCE_PASS table=55_bitwise samples_per_thread=" << K
            << " kernels=A,A2 sources=engine,buffer1,buffer7,buffer4096 concurrent_threads=3 identical=1 unit_edge_words=" << edge
            << " test_engine_only=1\n";
  return 0;
}

// A2's unit() against the library on crafted words: zero, all-ones (rounds to 1 -> nextafter), carries and the
// halves around 0.5, plus 2^20 pairs from a local counter hash (test input only). The library is the reference;
// any difference fails. index() is compared at, just below and just above every table entry.
struct Script { const uint32_t* w; size_t i; uint32_t next() { return w[i++]; } };
uint64_t unit_edge_checks() {
  std::vector<std::array<uint32_t, 2>> pairs = {{0, 0}, {0xffffffffu, 0xffffffffu}, {0xffffffffu, 0}, {0, 0xffffffffu},
      {0, 0x80000000u}, {0xffffffffu, 0x7fffffffu}, {1, 0x80000000u}, {0xfffffbffu, 0xffffffffu}, {0xfffff7ffu, 0xffffffffu},
      {0x00000400u, 0x80000000u}, {0x000007ffu, 0x7fffffffu}, {0x00000800u, 0}, {0x000003ffu, 0}};
  uint64_t z = 0x6a09e667f3bcc909ull;
  for (int k = 0; k < (1 << 20); k++) {
    z += 0x9e3779b97f4a7c15ull; uint64_t x = z; x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ull; x = (x ^ (x >> 27)) * 0x94d049bb133111ebull; x ^= x >> 31;
    pairs.push_back({uint32_t(x), uint32_t(x >> 32)});
  }
  for (const auto& p : pairs) {
    Script a{p.data(), 0}, b{p.data(), 0};
    WordUrng<Script> ga{a}, gb{b};
    const double x = StdKernels::unit(ga), y = DirectKernels::unit(gb);
    req(std::memcmp(&x, &y, sizeof x) == 0 && a.i == 2 && b.i == 2, "A2 unit() differs from the library on a crafted word pair");
  }
  const auto& t = sigma45_table();
  for (size_t i = 0; i < t.vals.size(); i++)
    for (double x : {t.vals[i], std::nextafter(t.vals[i], 0.0), std::nextafter(t.vals[i], 1.0)})
      if (x > 0 && x <= t.vals.back()) req(StdKernels::index(t, x) == DirectKernels::index(t, x), "A2 index() differs at a table boundary");
  return 2 * pairs.size();
}

double us_since(Clock::time_point t) { return std::chrono::duration<double, std::micro>(Clock::now() - t).count(); }
void report(const char* step, std::vector<double> v) {
  std::sort(v.begin(), v.end());
  auto q = [&](double p) { return v[std::min(v.size() - 1, size_t(p * (v.size() - 1) + 0.5))]; };
  std::cout << "V7_SAMPLER_PROFILE step=" << step << " n=" << v.size() << " median_us=" << q(0.5) << " p10_us=" << q(0.1)
            << " p90_us=" << q(0.9) << "\n";
}
struct CountingOs { OsWords w; uint64_t n = 0; uint32_t next() { n++; return w.next(); } };
struct Prefilled {   // words drawn from getrandom before the timer; refuses to run past the end
  std::vector<uint32_t> w; size_t i = 0;
  explicit Prefilled(size_t n) : w(n) { GetrandomFill()(w.data(), n); }
  ~Prefilled() { volatile uint32_t* p = w.data(); for (size_t k = 0; k < w.size(); k++) p[k] = 0; }
  uint32_t next() { if (i >= w.size()) throw std::runtime_error("prefilled words exhausted"); const uint32_t r = w[i]; w[i++] = 0; return r; }
};

double chi2_crit(double k) { const double z = 3.7190, a = 2.0 / (9.0 * k); return k * std::pow(1.0 - a + z * std::sqrt(a), 3.0); }
struct Hist { std::array<uint64_t, 3> tern{}; std::array<uint64_t, 111> gauss{}; uint64_t nt = 0, ng = 0; std::array<int8_t, 64> head{}; };
void add(Hist& h, const SecureMaterial& m) {
  for (size_t f = 0; f < 4; f++) {
    for (auto v : m.mask[f]) { req(v >= -1 && v <= 1, "ternary range"); h.tern[v + 1]++; h.nt++; }
    for (const auto* e : {&m.e0[f], &m.e1[f]}) for (auto v : *e) { req(v >= -55 && v <= 55, "gaussian support"); h.gauss[v + 55]++; h.ng++; }
  }
}
std::string check(const Hist& h, const std::string& name, const std::vector<double>& pmf) {
  double x2 = 0; for (auto c : h.tern) { const double e = h.nt / 3.0; x2 += (c - e) * (c - e) / e; }
  if (x2 > chi2_crit(2)) return name + " ternary chi2 " + std::to_string(x2);
  std::vector<double> obs, exp; double eo = 0, ee = 0;
  for (int x = -55; x <= 55; x++) {
    eo += double(h.gauss[x + 55]); ee += pmf[x + 55] * double(h.ng);
    if (ee >= 5) { obs.push_back(eo); exp.push_back(ee); eo = ee = 0; }
  }
  if (ee > 0) { obs.back() += eo; exp.back() += ee; }
  double g2 = 0; for (size_t i = 0; i < obs.size(); i++) g2 += (obs[i] - exp[i]) * (obs[i] - exp[i]) / exp[i];
  if (g2 > chi2_crit(double(obs.size() - 1))) return name + " gaussian chi2 " + std::to_string(g2);
  double cdf = 0, emp = 0, dmax = 0;
  for (int x = -55; x <= 55; x++) { cdf += pmf[x + 55]; emp += double(h.gauss[x + 55]) / double(h.ng); dmax = std::max(dmax, std::abs(cdf - emp)); }
  if (dmax > 2.2 / std::sqrt(double(h.ng))) return name + " gaussian max CDF deviation " + std::to_string(dmax);
  return "";
}

int profile_mode(const std::string& plugin, int reps, int jobs) {
  req(reps >= 5 && reps <= 500 && jobs >= 10 && jobs <= 2000, "reps 5..500, jobs 10..2000");
  initialize_secure_sampler(plugin);
  req(dlsym(dlopen(plugin.c_str(), RTLD_NOW | RTLD_NOLOAD), "edgefhe_test_engines_created") == nullptr, "profile mode needs the OS engine");
  uint64_t words_per_job = 0;
  { SecureMaterial m; CountingOs c; sample_encryption_from<StdKernels>(m, c); words_per_job = c.n; }
  req(words_per_job >= 81920 && words_per_job < 82000, "words per job " + std::to_string(words_per_job));
  std::vector<double> t_getrandom, t_engine, t_tern, t_gauss, t_cur, t_fast, t_conv, t_direct, t_dconv;
  {
    SecureMaterial cur, fast, conv, direct;
    thread_local const Dgg dgg(4.5);
    std::vector<uint32_t> sink(words_per_job);
    for (int r = 0; r < reps + 2; r++) {
      const bool keep = r >= 2;   // two warm-up rounds
      auto t = Clock::now(); GetrandomFill()(sink.data(), sink.size()); if (keep) t_getrandom.push_back(us_since(t));
      t = Clock::now(); for (auto& w : sink) w = lbcrypto::PseudoRandomNumberGenerator::GetPRNG()(); if (keep) t_engine.push_back(us_since(t));
      t = Clock::now();
      for (size_t f = 0; f < 4; f++) { Ternary p(degree); std::uniform_int_distribution<int> d(-1, 1); for (auto& v : p) v = int8_t(d(lbcrypto::PseudoRandomNumberGenerator::GetPRNG())); cur.mask[f] = std::move(p); }
      if (keep) t_tern.push_back(us_since(t));
      t = Clock::now(); for (size_t f = 0; f < 4; f++) { cur.e0[f] = dgg.GenerateIntVector(degree); cur.e1[f] = dgg.GenerateIntVector(degree); }
      if (keep) t_gauss.push_back(us_since(t));
      t = Clock::now(); sample_secure_encryption_concurrent(cur); if (keep) t_cur.push_back(us_since(t));
      t = Clock::now(); sample_secure_encryption_fast<StdKernels>(fast); if (keep) t_fast.push_back(us_since(t));
      t = Clock::now(); sample_secure_encryption_fast<DirectKernels>(direct); if (keep) t_direct.push_back(us_since(t));
      { Prefilled pre(words_per_job + 64);
        t = Clock::now(); sample_encryption_from<StdKernels>(conv, pre); if (keep) t_conv.push_back(us_since(t)); }
      { Prefilled pre(words_per_job + 64);
        t = Clock::now(); sample_encryption_from<DirectKernels>(conv, pre); if (keep) t_dconv.push_back(us_since(t)); }
    }
    volatile uint32_t* p = sink.data(); for (size_t i = 0; i < sink.size(); i++) p[i] = 0;
  }
  std::cout << "V7_SAMPLER_WORDS words_per_job=" << words_per_job << " bytes_per_job=" << 4 * words_per_job << "\n";
  report("getrandom_job_words", t_getrandom); report("engine_job_words", t_engine); report("current_ternary_4x4096", t_tern);
  report("current_gaussian_8x4096", t_gauss); report("current_job", t_cur); report("A_fast_job", t_fast); report("A_convert_only", t_conv);
  report("A2_direct_job", t_direct); report("A2_convert_only", t_dconv);
  std::vector<double> pmf(111); double z = 0;
  for (int x = -55; x <= 55; x++) z += pmf[x + 55] = std::exp(-double(x) * x / (2 * 4.5 * 4.5));
  for (auto& p : pmf) p /= z;
  for (int threads = 1; threads <= 3; threads++) {
    double rate[3];
    std::vector<Hist> hist(threads);
    for (int kind = 0; kind < 3; kind++) {
      const auto t = Clock::now();
      together(threads, [&](int) {
        SecureMaterial m;
        for (int j = 0; j < jobs; j++) {
          if (kind == 0) sample_secure_encryption_concurrent(m);
          else if (kind == 1) sample_secure_encryption_fast<StdKernels>(m);
          else sample_secure_encryption_fast<DirectKernels>(m);
        }
        return std::vector<Snapshot>{};
      });
      rate[kind] = threads * jobs / std::chrono::duration<double>(Clock::now() - t).count();
    }
    std::cout << "V7_SAMPLER_RATE threads=" << threads << " jobs_per_thread=" << jobs << " current_jobs_per_s=" << rate[0]
              << " A_jobs_per_s=" << rate[1] << " A2_jobs_per_s=" << rate[2] << "\n";
    together(threads, [&](int i) {   // untimed: A2 output of P concurrent threads for the distribution checks
      SecureMaterial m;
      for (int j = 0; j < jobs; j++) {
        sample_secure_encryption_fast<DirectKernels>(m);
        add(hist[i], m);
        if (j == 0) for (int k = 0; k < 64; k++) hist[i].head[k] = m.mask[0][k];
      }
      return std::vector<Snapshot>{};
    });
    Hist pooled;
    for (int i = 0; i < threads; i++) {
      const auto err = check(hist[i], "P" + std::to_string(threads) + "_thread" + std::to_string(i), pmf);
      req(err.empty(), "distribution test failed: " + err);
      for (int k = 0; k < 3; k++) pooled.tern[k] += hist[i].tern[k];
      for (int k = 0; k < 111; k++) pooled.gauss[k] += hist[i].gauss[k];
      pooled.nt += hist[i].nt; pooled.ng += hist[i].ng;
      for (int j = 0; j < i; j++) req(hist[i].head != hist[j].head, "two threads produced the same mask prefix");
    }
    const auto err = check(pooled, "P" + std::to_string(threads) + "_pooled", pmf);
    req(err.empty(), "distribution test failed: " + err);
  }
  std::cout << "V7_SAMPLER_PROFILE_PASS reps=" << reps << " jobs_per_thread=" << jobs << " words_per_job=" << words_per_job
            << " distribution_tests=pass p_per_test=1e-4 distribution_check_not_crypto_proof=1 device=0\n";
  return 0;
}
}  // namespace

int main(int argc, char** argv) {
  try {
    const std::string mode = argc > 1 ? argv[1] : "";
    if (mode == "det" && argc == 3) return det_mode(argv[2]);
    if (mode == "profile" && argc == 5) return profile_mode(argv[2], std::stoi(argv[3]), std::stoi(argv[4]));
    throw std::runtime_error("usage: sampler_profile_selftest det TEST_ENGINE.so | profile OS_ENGINE.so REPS JOBS");
  } catch (const std::exception& e) {
    std::cerr << "V7_SAMPLER_PROFILE_FAIL " << e.what() << "\n";
    return 1;
  }
}
