// app v3 equivalence selftest (2026-10-19): v3 hot paths vs the frozen v2 paths on the same key, material, seed and
// messages. Usage: ef_app_v3_equivalence <os_prng_plugin.so> <v2 libef_app_cloud.so>
#include "fast_encrypt.hpp"
#include "v8/v8b_device.hpp"
#include "v8b_device_v3.hpp"
#include "owner_fast_core.hpp"
#include <dlfcn.h>
#include <cstdio>
#include <iostream>
#include <random>

using namespace edgefhe;
extern "C" int ef_cloud_affine(const uint32_t*, size_t, size_t, const int64_t*, int64_t, uint32_t*, size_t);  // v3 (linked)
static int failures = 0;
static void check(bool ok, const char* what) { if (!ok) { failures++; std::printf("FAIL %s\n", what); } }
static void put(const Cipher& c, uint32_t* out) { for (size_t p = 0; p < 2; p++) { const auto& r = p ? c.c1 : c.c0; for (size_t l = 0; l < 3; l++) std::copy(r[l].begin(), r[l].end(), out + p * 3 * degree + l * degree); } }

int main(int argc, char** argv) {
  if (argc != 3) { std::cerr << "usage: plugin v2_cloud_so\n"; return 2; }
  initialize_secure_sampler(argv[1]);
  Engine engine(3); auto material = sample_secure_material(); auto key = keygen_secure(engine, material);
  fast::NttSet set; fast::Fft fft; v3::KeySpectra spectra; spectra.build(set, key); fast::SecretTransform sk; sk.build(set, material.secret);
  std::mt19937_64 rng(20261019); std::uniform_real_distribution<double> u(-8, 8);
  size_t cpu_ct = 0, dev_ct = 0, enc_poly = 0, dec_poly = 0; double dec_max = 0;
  // devices: frozen v2 Device and v3 Device, both on the specification model of the V8b hardware
  edgefhe_v8::SpecModel spec2; edgefhe_v8::Device dev2([&](edgefhe_v8_request& r) { return spec2.run(r); }); dev2.load(key);
  edgefhe_v8_v3::SpecModel spec3; edgefhe_v8_v3::Device dev3([&](edgefhe_v8_request& r) { return spec3.run(r); }); dev3.load(key);
  std::vector<uint32_t> scratch(3 * degree), a(4 * 6 * degree), b(4 * 6 * degree);
  for (int t = 0; t < 6; t++) {
    std::array<std::vector<std::complex<double>>, 4> x; std::array<Message, 4> m2, m3;
    for (size_t f = 0; f < 4; f++) {
      x[f].assign(slots, {}); const size_t rows = t == 0 ? 1 : t == 1 ? 7 : slots;
      for (size_t r = 0; r < rows; r++) x[f][r] = {t == 2 && f == 3 ? 0.0 : u(rng), 0};
      m2[f] = encode(x[f]); m3[f].resize(degree); std::vector<std::complex<double>> sc; fast::encode_into(fft, x[f], scale, m3[f].data(), sc);
      check(m2[f] == m3[f], "encode v3 == v2 (scale 2^30)"); enc_poly++;
    }
    uint8_t seed[32]; for (auto& s : seed) s = uint8_t(rng());
    // CPU backend: v2 cpu_encrypt_secure_fast vs v3 cpu_encrypt_limb
    SecureMaterial sm; sampler_b::sample_from_seed(sm, seed);
    const auto c2 = edgefhe_v7::cpu_encrypt_secure_fast(engine, key, m2, sm);
    v3::validate_material(m3, sm); std::array<Cipher, 4> c3;
    v3::cpu_encrypt_limb<0>(set, spectra, m3, sm, c3, scratch.data()); v3::cpu_encrypt_limb<1>(set, spectra, m3, sm, c3, scratch.data());
    v3::cpu_encrypt_limb<2>(set, spectra, m3, sm, c3, scratch.data());
    for (size_t f = 0; f < 4; f++) { check(c2[f].c0 == c3[f].c0 && c2[f].c1 == c3[f].c1, "CPU encrypt v3 == v2"); cpu_ct++; }
    // device path: v2 enc + unpack + put vs v3 try_enc_raw + transpose, same seed and messages
    std::vector<const Message*> p2, p3; for (size_t f = 0; f < 4; f++) { p2.push_back(&m2[f]); p3.push_back(&m3[f]); }
    std::vector<Cipher> d2; dev2.enc(seed, p2, d2); for (size_t f = 0; f < 4; f++) put(d2[f], a.data() + f * 6 * degree);
    check(dev3.try_enc_raw(seed, p3) == 0, "v3 raw ENC");
    check(fast::transpose_device_output(dev3.last_output().data(), 4, b.data()), "v3 transpose canonical");
    check(a == b, "device path v3 == v2 (application words)"); dev_ct += 4;
    // the CPU and device ciphertexts of one seed are identical in v2; v3 keeps that
    std::vector<uint32_t> cpu_words(4 * 6 * degree); for (size_t f = 0; f < 4; f++) put(c3[f], cpu_words.data() + f * 6 * degree);
    check(cpu_words == b, "v3 CPU == v3 device for one seed");
    // decrypt polynomial and decode: v2 decrypt_secure + decode vs v3 fast path
    for (size_t f = 0; f < 4; f++) {
      const auto poly2 = decrypt_secure(engine, c2[f], material.secret);
      Rns sum; for (size_t l = 0; l < 3; l++) { sum[l].resize(degree); fast::decrypt_limb(set, sk, c3[f], l, sum[l].data()); }
      const auto poly3 = centered(sum); check(poly2 == poly3, "decrypt polynomial v3 == v2"); dec_poly++;
      const auto v2 = decode(poly2, scale); std::vector<std::complex<double>> v3d(slots), sc; fast::decode_into(fft, poly3.data(), scale, v3d.data(), sc);
      for (size_t k = 0; k < slots; k++) dec_max = std::max(dec_max, std::abs(v2[k] - v3d[k]));
    }
  }
  check(dec_max < 1e-9, "decode v3 vs v2");
  // cloud: v3 (linked) vs v2 (dlopen) on identical inputs
  void* h = dlopen(argv[2], RTLD_NOW | RTLD_LOCAL); check(h != nullptr, "dlopen v2 cloud");
  size_t cloud_cases = 0;
  if (h) {
    using F = int (*)(const uint32_t*, size_t, size_t, const int64_t*, int64_t, uint32_t*, size_t);
    F v2cloud = reinterpret_cast<F>(dlsym(h, "ef_cloud_affine")); check(v2cloud != nullptr, "dlsym v2 cloud");
    for (size_t features : {4, 12, 16, 20, 32}) {
      std::vector<uint32_t> in(features * 6 * degree);
      for (size_t f = 0; f < features; f++) for (size_t p = 0; p < 2; p++) for (size_t l = 0; l < 3; l++) for (size_t i = 0; i < degree; i++)
        in[f * 6 * degree + p * 3 * degree + l * degree + i] = uint32_t(rng() % primes[l]);
      std::vector<int64_t> w(features); for (auto& x : w) x = int64_t(rng() % (int64_t{32} << 30)) - (int64_t{16} << 30);
      const int64_t bias = int64_t(rng() >> 3) - (int64_t{1} << 60);
      std::vector<uint32_t> o2(6 * degree), o3(6 * degree);
      check(v2cloud(in.data(), in.size(), features, w.data(), bias, o2.data(), o2.size()) == 0 && ef_cloud_affine(in.data(), in.size(), features, w.data(), bias, o3.data(), o3.size()) == 0, "cloud rc");
      check(o2 == o3, "cloud v3 == v2"); cloud_cases++;
      in[3] = primes[0]; check(ef_cloud_affine(in.data(), in.size(), features, w.data(), bias, o3.data(), o3.size()) != 0, "cloud v3 rejects noncanonical");
    }
  }
  std::printf("%s encode_polys=%zu cpu_ciphertexts=%zu device_ciphertexts=%zu decrypt_polys=%zu decode_max_diff=%.3e cloud_cases=%zu failures=%d\n",
              failures ? "APP_V3_EQUIVALENCE_FAIL" : "APP_V3_EQUIVALENCE_PASS", enc_poly, cpu_ct, dev_ct, dec_poly, dec_max, cloud_cases, failures);
  return failures ? 1 : 0;
}
