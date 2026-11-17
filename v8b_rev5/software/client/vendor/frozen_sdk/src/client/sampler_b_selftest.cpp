// Sampler B self-test and profile (2026-10-15). CPU only: no device, no sudo.
//
//   sampler_b_selftest golden
//     ChaCha20 RFC 8439 2.3.2 block vector; blocks4 (vector extensions: NEON on aarch64) equal to the scalar block
//     on 4096 pseudo-random (key, counter, nonce) cases; sampler B material for three public TEST seeds equal to
//     the independent Python reference (tools/sampler_b_reference.py, FNV-1a-64 over the serialised material).
//   sampler_b_selftest profile OS_ENGINE.so REPS JOBS
//     Production OS seeds. Single-thread medians: B job, its ChaCha20 keystream part, CDT part (vector and scalar
//     reference), A2 job and the
//     current job; jobs/s for current / A2 / B with P = 1, 2, 3; then pre-registered chi-square checks
//     (p = 1e-4 per test) of B's ternary (uniform) and Gaussian (against the realised CDT pmf) per thread and pooled,
//     support |x| <= 42, and distinct per-thread output. Distribution checks are not a cryptographic proof.
#include "baseline/fast_sampler.hpp"
#include "baseline/sampler_b.hpp"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstring>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
using namespace edgefhe;
using Clock = std::chrono::steady_clock;

namespace {
void req(bool ok, const std::string& message) { if (!ok) throw std::runtime_error(message); }

uint64_t fnv(const void* p, size_t n, uint64_t h) {
  auto* b = static_cast<const uint8_t*>(p);
  for (size_t i = 0; i < n; i++) h = (h ^ b[i]) * 0x100000001b3ull;
  return h;
}
uint64_t digest(const SecureMaterial& m) {
  uint64_t h = 0xcbf29ce484222325ull;
  for (size_t f = 0; f < 4; f++) {
    h = fnv(m.mask[f].data(), m.mask[f].size(), h);
    for (const auto* e : {&m.e0[f], &m.e1[f]}) for (auto v : *e) { uint8_t le[8]; for (int k = 0; k < 8; k++) le[k] = uint8_t(uint64_t(v) >> (8 * k)); h = fnv(le, 8, h); }
  }
  return h;
}

int golden_mode() {
  uint32_t key[8], nonce[3] = {0x09000000u, 0x4a000000u, 0}, out[16];
  for (int i = 0; i < 8; i++) key[i] = uint32_t(4 * i) | uint32_t(4 * i + 1) << 8 | uint32_t(4 * i + 2) << 16 | uint32_t(4 * i + 3) << 24;
  chacha::block(key, 1, nonce, out);
  const uint32_t want[16] = {0xe4e7f110u, 0x15593bd1u, 0x1fdd0f50u, 0xc47120a3u, 0xc7f4d1c7u, 0x0368c033u, 0x9aaa2204u, 0x4e6cd4c3u,
                             0x466482d2u, 0x09aa9f07u, 0x05d7c214u, 0xa2028bd9u, 0xd19c12b5u, 0xb94e16deu, 0xe883d0cbu, 0x4e3c50a2u};
  req(std::memcmp(out, want, sizeof out) == 0, "RFC 8439 2.3.2 block vector");
  uint64_t z = 0x3c6ef372fe94f82bull;
  auto next = [&] { z += 0x9e3779b97f4a7c15ull; uint64_t x = z; x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ull; x = (x ^ (x >> 27)) * 0x94d049bb133111ebull; return x ^ (x >> 31); };
  for (int c = 0; c < 4096; c++) {
    for (auto& k : key) k = uint32_t(next());
    for (auto& n : nonce) n = uint32_t(next());
    const uint32_t ctr = uint32_t(next()) % 0xfffffff0u;
    uint32_t v[64], s[64];
    chacha::blocks4(key, ctr, nonce, v);
    for (int b = 0; b < 4; b++) chacha::block(key, ctr + b, nonce, s + 16 * b);
    req(std::memcmp(v, s, sizeof v) == 0, "blocks4 == 4 x block");
  }
  {  // vector CDT == scalar CDT on random words and on every table boundary (both signs)
    std::vector<uint64_t> w;
    for (int i = 0; i < 1 << 16; i++) w.push_back(next());
    for (unsigned k = 0; k < cdt63::K; k++) for (uint64_t d : {0ull, 1ull}) for (uint64_t t : {cdt63::T[k] - 1, cdt63::T[k], cdt63::T[k] + 1}) w.push_back(t | (d << 63));
    for (uint64_t t : {0ull, 0x7fffffffffffffffull, 0x8000000000000000ull, ~0ull}) w.push_back(t);
    std::vector<int64_t> a(w.size()), b(w.size());
    sampler_b::cdt63(w.data(), a.data(), w.size()); sampler_b::cdt63_scalar(w.data(), b.data(), w.size());
    req(a == b, "vector CDT == scalar CDT");
  }
  struct G { const char* name; uint8_t seed[32]; uint64_t want; } g[3] = {{"seed_00_1f", {}, 0x8fd70b30cad96208ull},
                                                                          {"seed_ff", {}, 0xadf2eb3a6aa50755ull},
                                                                          {"seed_a5", {}, 0x9ca00ab10812fc34ull}};
  for (int i = 0; i < 32; i++) { g[0].seed[i] = uint8_t(i); g[1].seed[i] = 0xff; g[2].seed[i] = (i & 1) ? 0x5a : 0xa5; }
  for (auto& x : g) {
    SecureMaterial m;
    sampler_b::sample_from_seed(m, x.seed);
    const uint64_t d = digest(m);
    req(d == x.want, std::string("sampler B differs from the Python reference for ") + x.name);
    SecureMaterial again; sampler_b::sample_from_seed(again, x.seed);
    req(digest(again) == d, "deterministic for a fixed seed");
  }
  std::cout << "V7_SAMPLER_B_GOLDEN_PASS rfc8439_block=1 blocks4_vs_block=4096 cdt_vector_vs_scalar=65804 python_reference_seeds=3 test_seeds_only=1\n";
  return 0;
}

template<class F> void together(int n, F f) {
  std::mutex mu; std::condition_variable cv; int ready = 0; bool go = false;
  std::vector<std::thread> t; std::vector<std::exception_ptr> err(n);
  for (int i = 0; i < n; i++) t.emplace_back([&, i] {
    { std::unique_lock<std::mutex> g(mu); ready++; cv.notify_all(); cv.wait(g, [&] { return go; }); }
    try { f(i); } catch (...) { err[i] = std::current_exception(); }
  });
  { std::unique_lock<std::mutex> g(mu); cv.wait(g, [&] { return ready == n; }); go = true; cv.notify_all(); }
  for (auto& x : t) x.join();
  for (auto& e : err) if (e) std::rethrow_exception(e);
}
double us_since(Clock::time_point t) { return std::chrono::duration<double, std::micro>(Clock::now() - t).count(); }
void report(const char* step, std::vector<double> v) {
  std::sort(v.begin(), v.end());
  auto q = [&](double p) { return v[std::min(v.size() - 1, size_t(p * (v.size() - 1) + 0.5))]; };
  std::cout << "V7_SAMPLER_B_PROFILE step=" << step << " n=" << v.size() << " median_us=" << q(0.5) << " p10_us=" << q(0.1) << " p90_us=" << q(0.9) << "\n";
}
double chi2_crit(double k) { const double z = 3.7190, a = 2.0 / (9.0 * k); return k * std::pow(1.0 - a + z * std::sqrt(a), 3.0); }
struct Hist { std::array<uint64_t, 3> tern{}; std::array<uint64_t, 85> gauss{}; uint64_t nt = 0, ng = 0; std::array<int8_t, 64> head{}; };
void add(Hist& h, const SecureMaterial& m) {
  for (size_t f = 0; f < 4; f++) {
    for (auto v : m.mask[f]) { req(v >= -1 && v <= 1, "ternary range"); h.tern[v + 1]++; h.nt++; }
    for (const auto* e : {&m.e0[f], &m.e1[f]}) for (auto v : *e) { req(v >= -42 && v <= 42, "CDT63 support"); h.gauss[v + 42]++; h.ng++; }
  }
}
std::string check(const Hist& h, const std::string& name, const std::vector<double>& pmf) {
  double x2 = 0; for (auto c : h.tern) { const double e = h.nt / 3.0; x2 += (c - e) * (c - e) / e; }
  if (x2 > chi2_crit(2)) return name + " ternary chi2 " + std::to_string(x2);
  std::vector<double> obs, exp; double eo = 0, ee = 0;
  for (int x = -42; x <= 42; x++) { eo += double(h.gauss[x + 42]); ee += pmf[x + 42] * double(h.ng); if (ee >= 5) { obs.push_back(eo); exp.push_back(ee); eo = ee = 0; } }
  if (ee > 0) { obs.back() += eo; exp.back() += ee; }
  double g2 = 0; for (size_t i = 0; i < obs.size(); i++) g2 += (obs[i] - exp[i]) * (obs[i] - exp[i]) / exp[i];
  if (g2 > chi2_crit(double(obs.size() - 1))) return name + " gaussian chi2 " + std::to_string(g2);
  return "";
}

int profile_mode(const std::string& plugin, int reps, int jobs) {
  req(reps >= 5 && reps <= 500 && jobs >= 10 && jobs <= 2000, "reps 5..500, jobs 10..2000");
  initialize_secure_sampler(plugin);
  std::vector<double> t_b, t_ks, t_cdt, t_cdts, t_a2, t_cur;
  {
    SecureMaterial b, a2, cur;
    std::vector<uint64_t> words(degree); std::vector<int64_t> vals(degree);
    uint32_t key[8]; uint8_t seed[32];
    for (int r = 0; r < reps + 2; r++) {
      const bool keep = r >= 2;
      auto t = Clock::now(); sample_secure_encryption_b(b); if (keep) t_b.push_back(us_since(t));
      sampler_b::os_seed(seed); sampler_b::key_words(seed, key);
      t = Clock::now();
      for (int k = 0; k < 8; k++) { const uint32_t n[3] = {2, uint32_t(k), 0}; req(chacha::keystream(key, 0, n, reinterpret_cast<uint32_t*>(words.data()), sampler_b::gauss_blocks), "ks"); }
      if (keep) t_ks.push_back(us_since(t));
      t = Clock::now(); for (int k = 0; k < 8; k++) sampler_b::cdt63(words.data(), vals.data(), degree); if (keep) t_cdt.push_back(us_since(t));
      t = Clock::now(); for (int k = 0; k < 8; k++) sampler_b::cdt63_scalar(words.data(), vals.data(), degree); if (keep) t_cdts.push_back(us_since(t));
      t = Clock::now(); sample_secure_encryption_fast<DirectKernels>(a2); if (keep) t_a2.push_back(us_since(t));
      t = Clock::now(); sample_secure_encryption_concurrent(cur); if (keep) t_cur.push_back(us_since(t));
    }
    explicit_bzero(words.data(), words.size() * 8); explicit_bzero(vals.data(), vals.size() * 8); explicit_bzero(key, sizeof key); explicit_bzero(seed, sizeof seed);
  }
  report("B_job", t_b); report("B_chacha_gauss_8x4096", t_ks); report("B_cdt_8x4096", t_cdt); report("B_cdt_scalar_8x4096", t_cdts); report("A2_job", t_a2); report("current_job", t_cur);
  std::vector<double> pmf(85, 0.0);
  pmf[42] = double(cdt63::T[0]) / 9223372036854775808.0;
  for (unsigned k = 1; k <= cdt63::K; k++) {
    const double p = double((k < cdt63::K ? cdt63::T[k] : 0x8000000000000000ull) - cdt63::T[k - 1]) / 9223372036854775808.0;
    pmf[42 + k] = pmf[42 - k] = p / 2;
  }
  for (int threads = 1; threads <= 3; threads++) {
    double rate[3];
    for (int kind = 0; kind < 3; kind++) {
      const auto t = Clock::now();
      together(threads, [&](int) {
        SecureMaterial m;
        for (int j = 0; j < jobs; j++) {
          if (kind == 0) sample_secure_encryption_concurrent(m);
          else if (kind == 1) sample_secure_encryption_fast<DirectKernels>(m);
          else sample_secure_encryption_b(m);
        }
      });
      rate[kind] = threads * jobs / std::chrono::duration<double>(Clock::now() - t).count();
    }
    std::cout << "V7_SAMPLER_B_RATE threads=" << threads << " jobs_per_thread=" << jobs << " current_jobs_per_s=" << rate[0]
              << " A2_jobs_per_s=" << rate[1] << " B_jobs_per_s=" << rate[2] << "\n";
    std::vector<Hist> hist(threads);
    together(threads, [&](int i) {
      SecureMaterial m;
      for (int j = 0; j < jobs; j++) { sample_secure_encryption_b(m); add(hist[i], m); if (j == 0) for (int k = 0; k < 64; k++) hist[i].head[k] = m.mask[0][k]; }
    });
    Hist pooled;
    for (int i = 0; i < threads; i++) {
      const auto err = check(hist[i], "P" + std::to_string(threads) + "_thread" + std::to_string(i), pmf);
      req(err.empty(), "distribution test failed: " + err);
      for (int k = 0; k < 3; k++) pooled.tern[k] += hist[i].tern[k];
      for (int k = 0; k < 85; k++) pooled.gauss[k] += hist[i].gauss[k];
      pooled.nt += hist[i].nt; pooled.ng += hist[i].ng;
      for (int j = 0; j < i; j++) req(hist[i].head != hist[j].head, "two threads produced the same mask prefix");
    }
    const auto err = check(pooled, "P" + std::to_string(threads) + "_pooled", pmf);
    req(err.empty(), "distribution test failed: " + err);
  }
  std::cout << "V7_SAMPLER_B_PROFILE_PASS reps=" << reps << " jobs_per_thread=" << jobs
            << " distribution_tests=pass p_per_test=1e-4 support=42 distribution_check_not_crypto_proof=1 device=0\n";
  return 0;
}
}  // namespace

int main(int argc, char** argv) {
  try {
    const std::string mode = argc > 1 ? argv[1] : "";
    if (mode == "golden" && argc == 2) return golden_mode();
    if (mode == "profile" && argc == 5) return profile_mode(argv[2], std::stoi(argv[3]), std::stoi(argv[4]));
    throw std::runtime_error("usage: sampler_b_selftest golden | profile OS_ENGINE.so REPS JOBS");
  } catch (const std::exception& e) {
    std::cerr << "V7_SAMPLER_B_FAIL " << e.what() << "\n";
    return 1;
  }
}
