// Off-board check of encrypt_secure_paired_fast (fast_encrypt.hpp): bit-identical ciphertexts to the accepted
// encrypt_secure_paired AND to the CPU encrypt_secure, over random OS-sampled materials and edge cases (mask all
// -1/+1, noise at +-55, large/negative/multiple-of-q message coefficients). An offline mock computes exact PAIR4
// products. A second, latency-free mock (zero products) times only the user-space work around the ioctl.
// x86 timings are NOT board timings.
#include "fast_encrypt.hpp"
#include "profile.hpp"
#include "../driver/edgefhe_policy.h"
#include <chrono>
#include <iostream>
#include <openssl/sha.h>
#include <random>
using namespace edgefhe;
namespace v7 = edgefhe_v7;
using Clock = std::chrono::steady_clock;

struct Mock {
  ef_policy policy{}; Engine math{1}; PublicKey key; bool compute = true;
  v7::Poly input(const edgefhe_v7_request& r, size_t poly) {
    auto *wire = reinterpret_cast<const v7::Beat*>(r.input_ptr); v7::Poly out;
    for (auto& l : out) l.resize(degree);
    for (size_t i = 0; i < degree; i++) for (size_t l = 0; l < 3; l++) out[l][i] = wire[poly * degree + i].limb[l];
    return out;
  }
  int call(edgefhe_v7_request& r) {
    v7::validate_request(r);
    if (r.input_words && ef_canonical(reinterpret_cast<const ef_u32*>(r.input_ptr), r.input_words)) return -EINVAL;
    int rc = ef_begin(&policy, r.command, 1, r.generation, r.profile_sha256, r.public_key_sha256);
    if (rc) return rc;
    auto *out = reinterpret_cast<v7::Beat*>(r.output_ptr);
    if (r.command == 6) { key = {input(r, 0), input(r, 1)}; r.generation = policy.generation; out[0] = {}; }
    else if (r.command == 7) {
      for (size_t f = 0; f < 4; f++) {
        if (!compute) { for (size_t i = 0; i < 2 * degree; i++) out[2 * f * degree + i] = {}; continue; }
        auto u = input(r, f), bu = math.multiply(key.b, u), au = math.multiply(key.a, u);
        for (size_t i = 0; i < degree; i++) {
          out[2 * f * degree + i] = {}; out[(2 * f + 1) * degree + i] = {};
          for (size_t l = 0; l < 3; l++) { out[2 * f * degree + i].limb[l] = bu[l][i]; out[(2 * f + 1) * degree + i].limb[l] = au[l][i]; }
        }
      }
    } else return -EINVAL;
    r.status = ef_finish(&policy, 255, 0); r.hardware_status = 0; return r.status;
  }
};

int main(int argc, char **argv) {
  try {
    require(argc == 2, "usage: fast_path_selftest OS_PLUGIN");
    initialize_secure_sampler(argv[1]);
    Engine cpu(3);
    auto material = sample_secure_material();
    auto key = keygen_secure(cpu, material);
    auto session = [&](Mock& mock) {
      auto s = std::make_unique<v7::PairedSession>(v7::active_profile, [&mock](edgefhe_v7_request& r) { return mock.call(r); });
      std::vector<v7::Beat> wire; v7::pack_into(6, {key.b, key.a}, wire); v7::Digest d{};
      SHA256(reinterpret_cast<const unsigned char*>(wire.data()), wire.size() * 16, d.data());
      s->load(key.b, key.a, d); return s;
    };
    Mock m_old, m_fast; auto s_old = session(m_old), s_fast = session(m_fast);
    std::mt19937_64 rng(2026101403);
    uint64_t exact = 0; const int trials = 24;
    for (int t = 0; t < trials; t++) {
      sample_secure_encryption(material);
      std::array<Message, 4> msg;
      for (size_t f = 0; f < 4; f++) {
        std::vector<std::complex<double>> z(slots);
        for (auto& x : z) x = {std::uniform_real_distribution<double>(-16, 16)(rng), 0};
        msg[f] = encode(z);
      }
      if (t == 1) for (size_t f = 0; f < 4; f++) for (auto& x : material.mask[f]) x = -1;
      if (t == 2) for (size_t f = 0; f < 4; f++) for (auto& x : material.mask[f]) x = 1;
      if (t == 3) for (size_t f = 0; f < 4; f++) for (size_t i = 0; i < degree; i++) { material.e0[f][i] = (i & 1) ? 55 : -55; material.e1[f][i] = (i & 1) ? -55 : 55; }
      if (t == 4) for (size_t f = 0; f < 4; f++) for (size_t i = 0; i < degree; i++) {
        const int64_t q = primes[i % 3], k = int64_t(i % 7) - 3;      // multiples of q, q-1, -q, large +- values
        msg[f][i] = (i % 4 == 0) ? k * q : (i % 4 == 1) ? k * q - 1 : (i % 4 == 2) ? (int64_t(1) << 40) * (k ? k : 1) : -(int64_t(1) << 38) - int64_t(i);
      }
      auto a = v7::encrypt_secure_paired(*s_old, msg, material);
      auto b = v7::encrypt_secure_paired_fast(*s_fast, msg, material);
      auto c = encrypt_secure(cpu, key, msg, material);
      for (size_t f = 0; f < 4; f++) {
        require(a[f].c0 == b[f].c0 && a[f].c1 == b[f].c1, "fast == accepted paired path");
        require(c[f].c0 == b[f].c0 && c[f].c1 == b[f].c1, "fast == CPU encrypt_secure");
        exact += 2 * 3 * degree;
      }
    }
    // Rejections stay identical: an out-of-support noise value must be refused before any device call.
    sample_secure_encryption(material); material.e0[0][7] = 56; bool refused = false;
    std::array<Message, 4> zero; for (auto& z : zero) z.assign(degree, 0);
    try { v7::encrypt_secure_paired_fast(*s_fast, zero, material); } catch (const std::exception&) { refused = true; }
    require(refused, "fast path keeps the Gaussian support check");
    // User-space timing only (latency-free mock, zero products).
    Mock t_old, t_fast; t_old.compute = t_fast.compute = false; auto ts_old = session(t_old), ts_fast = session(t_fast);
    sample_secure_encryption(material);
    std::array<Message, 4> msg; for (size_t f = 0; f < 4; f++) msg[f] = encode(std::vector<std::complex<double>>(slots, {1.25, 0}));
    auto time = [&](auto fn) { for (int i = 0; i < 3; i++) fn(); auto t0 = Clock::now(); const int n = 40; for (int i = 0; i < n; i++) fn();
                               return std::chrono::duration<double, std::micro>(Clock::now() - t0).count() / n; };
    const double us_old = time([&] { (void)v7::encrypt_secure_paired(*ts_old, msg, material); });
    const double us_fast = time([&] { (void)v7::encrypt_secure_paired_fast(*ts_fast, msg, material); });
    std::cout << "V7_FAST_PATH_SELFTEST_PASS trials=" << trials << " exact_values=" << exact
              << " equal_to_accepted_paired=1 equal_to_cpu=1 support_refusal=1 x86_user_space_us_accepted=" << us_old
              << " x86_user_space_us_fast=" << us_fast << " x86_ratio=" << us_old / us_fast << " x86_not_board=1\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "V7_FAST_PATH_SELFTEST_FAIL " << e.what() << '\n';
    return 1;
  }
}
