// Off-board test of the lock-free encryption sampler (plan step 09).
//
//   sampler_selftest det TEST_ENGINE.so
//     With the test-only deterministic engine (every engine instance replays one stream): the locked and the
//     concurrent sampler give bit-identical material on fresh threads, also while three threads sample at the
//     same time (a shared engine would interleave the stream and fail), and engine creation/destruction counts.
//   sampler_selftest os OS_ENGINE.so THREADS JOBS
//     With the production OS-entropy engine: THREADS workers sample concurrently; pre-registered chi-square tests
//     (p = 1e-4 per test) of the uniform ternary mask and of the sigma = 4.5 Gaussian against the inversion
//     sampler's pmf, a max-CDF-deviation test, per thread and pooled; distinct per-thread streams; locked vs
//     concurrent wall time. A distribution check is not a cryptographic proof; timings are x86 observations.
#include "baseline/secure.hpp"
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <dlfcn.h>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <mutex>
#include <thread>
#include <vector>
using namespace edgefhe;
using Clock = std::chrono::steady_clock;

namespace {
void req(bool ok, const std::string& message) { if (!ok) throw std::runtime_error(message); }
struct Snapshot { std::array<Ternary, 4> mask; std::array<Message, 4> e0, e1; };
bool operator==(const Snapshot& a, const Snapshot& b) { return a.mask == b.mask && a.e0 == b.e0 && a.e1 == b.e1; }
Snapshot take(const SecureMaterial& m) { Snapshot s; s.mask = m.mask; s.e0 = m.e0; s.e1 = m.e1; return s; }

std::vector<Snapshot> draw(bool concurrent, int k) {
  SecureMaterial m; std::vector<Snapshot> out;
  for (int i = 0; i < k; i++) { concurrent ? sample_secure_encryption_concurrent(m) : sample_secure_encryption(m); out.push_back(take(m)); }
  return out;
}
// Start `n` threads together (a start barrier makes them overlap) and collect their results.
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

int det_mode(const std::string& plugin) {
  initialize_secure_sampler(plugin);
  void* h = dlopen(plugin.c_str(), RTLD_NOW | RTLD_NOLOAD);
  req(h != nullptr, "test engine handle");
  auto created = reinterpret_cast<long (*)()>(dlsym(h, "edgefhe_test_engines_created"));
  auto destroyed = reinterpret_cast<long (*)()>(dlsym(h, "edgefhe_test_engines_destroyed"));
  req(created && destroyed, "test engine counters (refusing to run with a non-test engine)");
  const int K = 6;
  auto ref = together(1, [&](int) { return draw(false, K); })[0];
  auto lockfree = together(1, [&](int) { return draw(true, K); })[0];
  req(lockfree == ref, "concurrent sampler differs from the locked sampler on an identical stream");
  for (int i = 1; i < K; i++) req(!(ref[i] == ref[0]), "consecutive samples must differ");
  auto three = together(3, [&](int) { return draw(true, K); });
  for (auto& r : three) req(r == ref, "concurrent threads interfered (shared engine or shared state)");
  auto mixed = together(3, [&](int i) { return draw(i != 0, K); });
  for (auto& r : mixed) req(r == ref, "locked and concurrent samplers interfered");
  const long c = created(), d = destroyed();
  req(c == 8, "one engine per sampling thread (expected 8, got " + std::to_string(c) + ")");
  std::cout << "V7_SAMPLER_DETERMINISTIC_EQUIVALENCE_PASS samples_per_thread=" << K << " threads=8 identical=1"
            << " engines_created=" << c << " engines_destroyed_at_thread_exit=" << d
            << (d == c ? " engine_lifecycle=closed" : " engine_lifecycle=NOT_DESTROYED_AT_THREAD_EXIT")
            << " test_engine_only=1\n";
  return 0;
}

double chi2_crit(double k) {  // Wilson-Hilferty upper quantile, p = 1e-4 (z = 3.7190)
  const double z = 3.7190, a = 2.0 / (9.0 * k);
  return k * std::pow(1.0 - a + z * std::sqrt(a), 3.0);
}
struct Hist { std::array<uint64_t, 3> tern{}; std::array<uint64_t, 111> gauss{}; uint64_t nt = 0, ng = 0; std::array<int8_t, 64> head{}; };
std::string check(const Hist& h, const char* name, std::vector<double>& pmf) {
  // Ternary: uniform over {-1,0,1}.
  double x2 = 0; for (auto c : h.tern) { const double e = h.nt / 3.0; x2 += (c - e) * (c - e) / e; }
  if (x2 > chi2_crit(2)) return std::string(name) + " ternary chi2 " + std::to_string(x2);
  // Gaussian: bins merged at the tails until the expected count is >= 5.
  std::vector<double> obs, exp; double eo = 0, ee = 0;
  for (int x = -55; x <= 55; x++) {
    eo += double(h.gauss[x + 55]); ee += pmf[x + 55] * double(h.ng);
    if (ee >= 5) { obs.push_back(eo); exp.push_back(ee); eo = ee = 0; }
  }
  if (ee > 0) { obs.back() += eo; exp.back() += ee; }
  double g2 = 0; for (size_t i = 0; i < obs.size(); i++) g2 += (obs[i] - exp[i]) * (obs[i] - exp[i]) / exp[i];
  if (g2 > chi2_crit(double(obs.size() - 1))) return std::string(name) + " gaussian chi2 " + std::to_string(g2) + " bins " + std::to_string(obs.size());
  double cdf = 0, emp = 0, dmax = 0;
  for (int x = -55; x <= 55; x++) { cdf += pmf[x + 55]; emp += double(h.gauss[x + 55]) / double(h.ng); dmax = std::max(dmax, std::abs(cdf - emp)); }
  if (dmax > 2.2 / std::sqrt(double(h.ng))) return std::string(name) + " gaussian max CDF deviation " + std::to_string(dmax);
  return "";
}

int os_mode(const std::string& plugin, int threads, int jobs) {
  req(threads >= 1 && threads <= 4 && jobs >= 10, "threads 1..4, jobs >= 10");
  initialize_secure_sampler(plugin);
  req(dlsym(dlopen(plugin.c_str(), RTLD_NOW | RTLD_NOLOAD), "edgefhe_test_engines_created") == nullptr, "os mode needs the OS engine");
  std::vector<double> pmf(111); double z = 0;
  for (int x = -55; x <= 55; x++) z += pmf[x + 55] = std::exp(-double(x) * x / (2 * 4.5 * 4.5));
  for (auto& p : pmf) p /= z;
  std::vector<Hist> hist(threads);
  auto t0 = Clock::now();
  together(threads, [&](int i) {
    SecureMaterial m; Hist& h = hist[i];
    for (int j = 0; j < jobs; j++) {
      sample_secure_encryption_concurrent(m);
      if (j == 0) for (int k = 0; k < 64; k++) h.head[k] = m.mask[0][k];
      for (size_t f = 0; f < 4; f++) {
        for (auto v : m.mask[f]) { req(v >= -1 && v <= 1, "ternary range"); h.tern[v + 1]++; h.nt++; }
        for (const auto* e : {&m.e0[f], &m.e1[f]}) for (auto v : *e) { req(v >= -55 && v <= 55, "gaussian support"); h.gauss[v + 55]++; h.ng++; }
      }
    }
    return std::vector<Snapshot>{};
  });
  const double concurrent_s = std::chrono::duration<double>(Clock::now() - t0).count();
  auto t1 = Clock::now();
  together(threads, [&](int) { SecureMaterial m; for (int j = 0; j < jobs; j++) sample_secure_encryption(m); return std::vector<Snapshot>{}; });
  const double locked_s = std::chrono::duration<double>(Clock::now() - t1).count();
  Hist pooled;
  for (int i = 0; i < threads; i++) {
    const auto err = check(hist[i], ("thread" + std::to_string(i)).c_str(), pmf);
    req(err.empty(), "distribution test failed: " + err);
    for (int k = 0; k < 3; k++) pooled.tern[k] += hist[i].tern[k];
    for (int k = 0; k < 111; k++) pooled.gauss[k] += hist[i].gauss[k];
    pooled.nt += hist[i].nt; pooled.ng += hist[i].ng;
    for (int j = 0; j < i; j++) req(hist[i].head != hist[j].head, "two threads produced the same mask prefix");
  }
  const auto err = check(pooled, "pooled", pmf);
  req(err.empty(), "distribution test failed: " + err);
  std::cout << "V7_SAMPLER_OS_CONCURRENT_PASS threads=" << threads << " jobs_per_thread=" << jobs
            << " ternary=" << pooled.nt << " gaussian=" << pooled.ng << " tests_per_group=3 p_per_test=1e-4"
            << " concurrent_ms_per_job=" << 1000.0 * concurrent_s / jobs << " locked_ms_per_job=" << 1000.0 * locked_s / jobs
            << " (wall, " << threads << " threads, x86 observation) distribution_check_not_crypto_proof=1\n";
  return 0;
}
}  // namespace

int main(int argc, char** argv) {
  try {
    const std::string mode = argc > 1 ? argv[1] : "";
    if (mode == "det" && argc == 3) return det_mode(argv[2]);
    if (mode == "os" && argc == 5) return os_mode(argv[2], std::stoi(argv[3]), std::stoi(argv[4]));
    std::cerr << "usage: sampler_selftest det TEST_ENGINE.so | os OS_ENGINE.so THREADS JOBS\n";
    return 2;
  } catch (const std::exception& e) {
    std::cerr << "V7_SAMPLER_SELFTEST_FAIL " << e.what() << '\n';
    return 1;
  }
}
