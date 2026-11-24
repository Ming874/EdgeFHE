// module_bench.cpp : per-module ARM cost of the client CKKS path, using the V8b CPU implementation
// (baseline/core.cpp, sampler_b.hpp: the bit-exact specification of the V8b PL kernel), N = 4096, 3 limbs, scale 2^30.
// One "unit" = one ciphertext (one feature of a 2048-record batch).
//   module_bench bench [reps]                 -> JSON lines: median ms per unit for every module (1 thread)
//   module_bench loop <module> <seconds> [threads] -> run one module continuously (for the energy probe)
#include "core.hpp"
#include "sampler_b.hpp"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <complex>
#include <cstdio>
#include <cstring>
#include <functional>
#include <map>
#include <string>
#include <thread>
#include <vector>

using namespace edgefhe;
using Clock = std::chrono::steady_clock;

struct Ctx {
  std::vector<double> raw;
  std::vector<std::complex<double>> z;
  Message m;
  Rns m_rns, u_rns, e0_rns, e1_rns, u_hat, bu_hat, au_hat, bu, au, c0, c1;
  Ternary u; Message e0, e1;
  std::vector<uint64_t> words;
  std::array<Plan, 3> plans{Plan(primes[0], psi[0]), Plan(primes[1], psi[1]), Plan(primes[2], psi[2])};
  PublicKey pk; Ternary secret; Rns b_hat, a_hat;
  Cipher ct; std::vector<Wide> dec; std::vector<std::complex<double>> slots_out;
  uint32_t key[8];
  volatile double sink = 0;
  Engine engine{1};                     // built once: its NTT tables are set-up cost, not per-ciphertext decryption work
  Ctx() {
    raw.resize(slots); z.resize(slots);
    for (size_t i = 0; i < slots; i++) raw[i] = 36.5 + 1.2 * std::sin(2 * M_PI * i / 256.0) + 0.3 * std::cos(2 * M_PI * i / 19.0);
    for (int i = 0; i < 8; i++) key[i] = 0x9e3779b9u * (i + 1);
    words.resize(degree);
    // key material (timing does not depend on the distribution of the key)
    Material mat = sample_material();
    pk = engine.keygen(mat); secret = mat.secret;
    for (size_t l = 0; l < 3; l++) { b_hat[l] = plans[l].transform(pk.b[l], false); a_hat[l] = plans[l].transform(pk.a[l], false); }
    // one full pass so every buffer holds valid data
    normalize(); encode_(); rns_m(); sample(); rns_small(); ntt_u(); pointwise(); intt(); add_(); decrypt(); decode_();
  }
  void normalize() { for (size_t i = 0; i < slots; i++) z[i] = {(raw[i] - 36.5) / 2.0, 0.0}; }
  void encode_() { m = encode(z); }
  void rns_m() { m_rns = residues(m); }
  void prng() {                       // keystream only: mask (~16 blocks) + e0, e1 (2 x 512 blocks)
    const uint32_t n0[3] = {2, 0, 0}, n1[3] = {3, 0, 0}, nm[3] = {1, 0, 0};
    uint32_t tmp[64];
    chacha::keystream(key, 0, nm, tmp, 4); chacha::keystream(key, 4, nm, tmp, 4);
    chacha::keystream(key, 8, nm, tmp, 4); chacha::keystream(key, 12, nm, tmp, 4);
    chacha::keystream(key, 0, n0, reinterpret_cast<uint32_t*>(words.data()), sampler_b::gauss_blocks);
    chacha::keystream(key, 0, n1, reinterpret_cast<uint32_t*>(words.data()), sampler_b::gauss_blocks);
  }
  void cdt() { e0.resize(degree); e1.resize(degree); sampler_b::cdt63(words.data(), e0.data(), degree); sampler_b::cdt63(words.data(), e1.data(), degree); }
  void tern() { const uint32_t nm[3] = {1, 0, 0}; u.resize(degree); sampler_b::ternary(key, nm, u.data(), degree); }
  void sample() {                     // full sampler for one ciphertext: mask + e0 + e1 (keystream included)
    tern();
    const uint32_t n0[3] = {2, 0, 0}, n1[3] = {3, 0, 0};
    chacha::keystream(key, 0, n0, reinterpret_cast<uint32_t*>(words.data()), sampler_b::gauss_blocks);
    e0.resize(degree); sampler_b::cdt63(words.data(), e0.data(), degree);
    chacha::keystream(key, 0, n1, reinterpret_cast<uint32_t*>(words.data()), sampler_b::gauss_blocks);
    e1.resize(degree); sampler_b::cdt63(words.data(), e1.data(), degree);
  }
  void rns_small() { u_rns = residues(u); e0_rns = residues(e0); e1_rns = residues(e1); }
  void ntt_u() { for (size_t l = 0; l < 3; l++) u_hat[l] = plans[l].transform(u_rns[l], false); }
  void pointwise() {
    for (size_t l = 0; l < 3; l++) {
      bu_hat[l].resize(degree); au_hat[l].resize(degree);
      for (size_t i = 0; i < degree; i++) { bu_hat[l][i] = plans[l].product(b_hat[l][i], u_hat[l][i]); au_hat[l][i] = plans[l].product(a_hat[l][i], u_hat[l][i]); }
    }
  }
  void intt() { for (size_t l = 0; l < 3; l++) { bu[l] = plans[l].transform(bu_hat[l], true); au[l] = plans[l].transform(au_hat[l], true); } }
  void add_() { c0 = add(add(bu, e0_rns), m_rns); c1 = add(au, e1_rns); ct = {c0, c1}; }
  void encrypt_all() { rns_m(); sample(); rns_small(); ntt_u(); pointwise(); intt(); add_(); }
  void decrypt() { dec = engine.decrypt(ct, secret); }
  void decode_() { slots_out = decode(dec); }
  void sigmoid_() { double s = 0; for (size_t i = 0; i < slots; i++) s += sigmoid(slots_out[i].real()); sink = s; }
};

static std::vector<std::pair<std::string, std::function<void(Ctx&)>>> modules() {
  return {
    {"normalize", [](Ctx& c) { c.normalize(); }},
    {"encode", [](Ctx& c) { c.encode_(); }},
    {"front", [](Ctx& c) { c.normalize(); c.encode_(); }},                            // ARM work per ciphertext in the V8b partition (before the PL)
    {"rns_message", [](Ctx& c) { c.rns_m(); }},
    {"prng_chacha20", [](Ctx& c) { c.prng(); }},
    {"sample_ternary", [](Ctx& c) { c.tern(); }},
    {"sample_cdt63", [](Ctx& c) { c.cdt(); }},
    {"sample_all", [](Ctx& c) { c.sample(); }},
    {"rns_small", [](Ctx& c) { c.rns_small(); }},
    {"ntt_mask", [](Ctx& c) { c.ntt_u(); }},
    {"pointwise", [](Ctx& c) { c.pointwise(); }},
    {"intt", [](Ctx& c) { c.intt(); }},
    {"add", [](Ctx& c) { c.add_(); }},
    {"encrypt_all", [](Ctx& c) { c.encrypt_all(); }},
    {"client_ct", [](Ctx& c) { c.normalize(); c.encode_(); c.encrypt_all(); }},   // all-ARM client work per ciphertext
    {"decrypt", [](Ctx& c) { c.decrypt(); }},
    {"decode", [](Ctx& c) { c.decode_(); }},
    {"sigmoid", [](Ctx& c) { c.sigmoid_(); }},
  };
}

static double median(std::vector<double> v) { std::sort(v.begin(), v.end()); return v[v.size() / 2]; }

int main(int argc, char** argv) {
  const std::string mode = argc > 1 ? argv[1] : "bench";
  auto mods = modules();
  if (mode == "bench") {
    const int reps = argc > 2 ? std::atoi(argv[2]) : 50;
    Ctx ctx;
    for (auto& [name, fn] : mods) {
      for (int w = 0; w < 3; w++) fn(ctx);                     // warm-up
      std::vector<double> t;
      for (int r = 0; r < reps; r++) {
        const auto a = Clock::now(); fn(ctx); const auto b = Clock::now();
        t.push_back(std::chrono::duration<double, std::milli>(b - a).count());
      }
      std::printf("{\"module\":\"%s\",\"median_ms\":%.5f,\"min_ms\":%.5f,\"reps\":%d}\n", name.c_str(), median(t),
                  *std::min_element(t.begin(), t.end()), reps);
      std::fflush(stdout);
    }
    std::printf("MODULE_BENCH_PASS\n");
    return 0;
  }
  if (mode == "loop" && argc >= 4) {
    const std::string which = argv[2]; const double secs = std::atof(argv[3]); const int threads = argc > 4 ? std::atoi(argv[4]) : 1;
    std::function<void(Ctx&)> fn;
    for (auto& [name, f] : mods) if (name == which) fn = f;
    if (!fn) { std::printf("STOP unknown module %s\n", which.c_str()); return 2; }
    std::vector<Ctx*> ctxs; for (int t = 0; t < threads; t++) ctxs.push_back(new Ctx());
    std::atomic<long> count{0};
    std::printf("SUSTAIN_BEGIN module=%s seconds=%.1f threads=%d\n", which.c_str(), secs, threads); std::fflush(stdout);
    const auto t0 = Clock::now(); const auto end = t0 + std::chrono::duration<double>(secs);
    std::vector<std::thread> pool;
    for (int t = 0; t < threads; t++) pool.emplace_back([&, t] { long n = 0; while (Clock::now() < end) { fn(*ctxs[t]); n++; } count += n; });
    for (auto& th : pool) th.join();
    const double sec = std::chrono::duration<double>(Clock::now() - t0).count();
    std::printf("MODULE_SUSTAIN_PASS module=%s seconds=%.3f threads=%d units=%ld ms_per_unit_per_thread=%.5f\n",
                which.c_str(), sec, threads, count.load(), sec * 1000.0 * threads / count.load());
    return 0;
  }
  std::printf("usage: module_bench bench [reps] | module_bench loop <module> <seconds> [threads]\n");
  return 2;
}
