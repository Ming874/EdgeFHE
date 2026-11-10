// app v5 equivalence selftest (2026-10-22). With the same key, messages and seeds, v5 must reproduce app_v4 exactly:
//  * CPU groups: v5 Engine::encrypt_cpu == v4 path (sample_from_seed + v3::cpu_encrypt_limb + put) on every real feature
//    word; padding features are zero words in v5.
//  * PL frames (specification model behind the driver checks): v5 Engine::encrypt_device (frames of <= 4 real features)
//    == v4 frames of 4 (padding encrypted) through try_enc_raw + transpose, on every real feature word; calls = ceil(F/4).
//  * decrypt: v5 Decryptor == v4 ef_owner_decrypt arithmetic, bitwise on every returned double.
//  * cloud: v5 ef_cloud_affine (3 lanes) == fast::affine_limb (v4 cloud arithmetic) on every output word.
//  * API: owner/cloud/decrypt round trips on CPU and spec within the owner's B04 bound; failure paths keep v4 behaviour.
#include "owner_v5_core.hpp"
#include "v8b_device_v3.hpp"
#include "precision_certificate.hpp"
#include <cstdio>
#include <iostream>
#include <openssl/sha.h>
#include <random>
using namespace edgefhe;
extern "C" {
int ef_cloud_affine(const uint32_t*, size_t, size_t, const int64_t*, int64_t, uint32_t*, size_t);
void* ef_owner_create(unsigned, unsigned, const char*);
void ef_owner_destroy(void*);
const char* ef_owner_error();
int ef_owner_encrypt(void*, const double*, size_t, size_t, uint32_t*, size_t);
int ef_owner_decrypt(void*, const uint32_t*, size_t, size_t, double*);
int ef_owner_device_calls(void*, uint64_t*);
int ef_owner_error_bound(void*, const int64_t*, const double*, size_t, int64_t, double, double, double, double*);
}
namespace {
constexpr size_t W = 6 * degree;
struct Seeds {   // deterministic, public test seeds: SHA-256("edgefhe-v5-eq" || stream || counter)
  uint32_t stream; uint32_t counter = 0;
  void operator()(uint8_t* out) { uint8_t in[21] = "edgefhe-v5-eq"; std::memcpy(in + 13, &stream, 4); std::memcpy(in + 17, &counter, 4); counter++; SHA256(in, sizeof in, out); }
};
struct Keys {
  Engine engine{3}; SecureMaterial material; PublicKey key; fast::NttSet set; fast::Fft fft; fast::SecretTransform sk; v3::KeySpectra spectra;
  Keys() : material(sample_secure_material()), key(keygen_secure(engine, material)) { sk.build(set, material.secret); spectra.build(set, key); }
};
size_t checks = 0;
void must(bool ok, const std::string& what) { if (!ok) { std::cerr << "V5_EQUIVALENCE_FAIL " << what << '\n'; std::exit(1); } checks++; }
std::vector<double> random_values(std::mt19937_64& rng, size_t rows, size_t F) {
  std::normal_distribution<double> nd(0, 1.5); std::vector<double> v(rows * F); for (auto& x : v) x = std::max(-8.0, std::min(8.0, nd(rng))); return v;
}
// v4 encode of one 4-feature group (padding features encoded as zero values), exactly as app_v4 encode_group
std::array<Message, 4> v4_group(const fast::Fft& fft, const std::vector<double>& v, size_t rows, size_t F, size_t base) {
  std::array<Message, 4> m; std::vector<std::complex<double>> scratch;
  for (size_t f = 0; f < 4; f++) { std::vector<std::complex<double>> x(slots); if (base + f < F) for (size_t r = 0; r < rows; r++) x[r] = {v[r * F + base + f], 0};
    m[f].resize(degree); fast::encode_into(fft, x, scale, m[f].data(), scratch); }
  return m;
}
void v4_put(const Cipher& c, uint32_t* out) { for (size_t p = 0; p < 2; p++) { const auto& r = p ? c.c1 : c.c0; for (size_t l = 0; l < 3; l++) std::copy(r[l].begin(), r[l].end(), out + p * 3 * degree + l * degree); } }
// v4 ef_owner_decrypt arithmetic
void v4_decrypt(WorkerPool& math, const Keys& k, const uint32_t* in, size_t rows, double* values) {
  Rns sum; for (auto& s : sum) s.resize(degree);
  math.run([&](size_t l) { const auto& nt = k.set.ntt[l]; const uint32_t q = primes[l]; uint32_t* o = sum[l].data();
    std::memcpy(o, in + 3 * degree + l * degree, degree * 4); nt.transform(o, false);
    for (size_t i = 0; i < degree; i++) o[i] = nt.product(o[i], k.sk.hat[l][i]);
    nt.transform(o, true); const uint32_t* c0 = in + l * degree; for (size_t i = 0; i < degree; i++) o[i] = fast::csub(o[i] + c0[i], q); });
  auto poly = centered(sum); std::vector<std::complex<double>> d(slots), sc; fast::decode_into(k.fft, poly.data(), scale * scale, d.data(), sc);
  for (size_t i = 0; i < rows; i++) values[i] = d[i].real();
}
}

int main(int argc, char** argv) {
  if (argc != 2) { std::cerr << "usage: ef_app_v5_equivalence <os_prng_plugin>\n"; return 2; }
  // The owner library carries the frozen sampler state too: create an owner first, then initialize ours only if the
  // state is not shared with it (the frozen SDK allows exactly one initialization per state).
  void* first = ef_owner_create(3, 0, argv[1]); must(first != nullptr, "first owner create");
  try { initialize_secure_sampler(argv[1]); } catch (const std::exception& e) { must(std::string(e.what()).find("exactly once") != std::string::npos, e.what()); }
  Keys k; WorkerPool math(3), enc(3); std::mt19937_64 rng(20261022);
  v5::Engine eng(k.set, k.fft, k.spectra, math, enc);
  size_t cpu_groups = 0, pl_frames = 0;
  // CPU and PL encrypt equivalence for F = 1..16 and rows 1 / 2048
  for (size_t F = 1; F <= 16; F++) for (size_t rows : {size_t(1), size_t(2048)}) {
    const auto v = random_values(rng, rows, F); const size_t P = (F + 3) / 4 * 4;
    std::vector<uint32_t> a(P * W, 0xdeadbeef), b(P * W, 0xdeadbeef);
    Seeds s5{uint32_t(F * 10 + (rows > 1))}, s4{uint32_t(F * 10 + (rows > 1))};
    eng.encrypt_cpu(v.data(), rows, F, a.data(), s5);
    for (size_t base = 0; base < F; base += 4) {
      const auto m = v4_group(k.fft, v, rows, F, base); uint8_t seed[32]; s4(seed); SecureMaterial mat; sampler_b::sample_from_seed(mat, seed);
      v3::validate_material(m, mat); std::array<Cipher, 4> ct; std::vector<uint32_t> scr(3 * degree);
      v3::cpu_encrypt_limb<0>(k.set, k.spectra, m, mat, ct, scr.data()); v3::cpu_encrypt_limb<1>(k.set, k.spectra, m, mat, ct, scr.data()); v3::cpu_encrypt_limb<2>(k.set, k.spectra, m, mat, ct, scr.data());
      for (size_t f = 0; f < 4; f++) v4_put(ct[f], b.data() + (base + f) * W);
      cpu_groups++;
    }
    must(std::memcmp(a.data(), b.data(), F * W * 4) == 0, "cpu ciphertexts F=" + std::to_string(F));
    must(std::all_of(a.begin() + F * W, a.end(), [](uint32_t x) { return x == 0; }), "cpu padding zero F=" + std::to_string(F));
    // PL: v5 frames (spec) vs v4 frames of 4 through the v3 device header (spec)
    edgefhe_v8_v5::SpecModel spec5; edgefhe_v8_v5::Device d5([&](edgefhe_v8_request& r) { return spec5.run(r); }); d5.load(k.key);
    edgefhe_v8_v3::SpecModel spec4; edgefhe_v8_v3::Device d4([&](edgefhe_v8_request& r) { return spec4.run(r); }); d4.load(k.key);
    std::fill(a.begin(), a.end(), 0xdeadbeef); std::fill(b.begin(), b.end(), 0xdeadbeef);
    Seeds p5{uint32_t(1000 + F * 10 + (rows > 1))}, p4{uint32_t(1000 + F * 10 + (rows > 1))};
    eng.encrypt_device(d5, v.data(), rows, F, a.data(), p5);
    for (size_t base = 0; base < F; base += 4) {
      const auto m = v4_group(k.fft, v, rows, F, base); std::vector<const Message*> mp; for (auto& x : m) mp.push_back(&x);
      uint8_t seed[32]; p4(seed); uint32_t hw = ~0u; must(d4.try_enc_raw(seed, mp, &hw) == 0, "v4 spec ENC");
      must(fast::transpose_device_output(d4.last_output().data(), 4, b.data() + base * W), "v4 transpose"); pl_frames++;
    }
    must(std::memcmp(a.data(), b.data(), F * W * 4) == 0, "pl ciphertexts F=" + std::to_string(F));
    must(std::all_of(a.begin() + F * W, a.end(), [](uint32_t x) { return x == 0; }), "pl padding zero F=" + std::to_string(F));
    must(d5.calls() == 1 + (F + 3) / 4 && spec5.frames == 1 + (F + 3) / 4, "pl calls F=" + std::to_string(F));
    // cloud v5 vs v4 arithmetic, decrypt v5 vs v4 (on the v4 ciphertexts with random public weights)
    std::vector<int64_t> wts(P, 0); std::uniform_int_distribution<int64_t> wd(-(int64_t{16} << 30), int64_t{16} << 30); for (size_t f = 0; f < F; f++) wts[f] = wd(rng);
    std::vector<uint32_t> r5(W), r4(W); std::vector<uint64_t> acc(degree);
    must(ef_cloud_affine(b.data(), P * W, P, wts.data(), 4781, r5.data(), W) == 0, "cloud v5 call");
    must(fast::affine_limb<primes[0]>(b.data(), P, wts.data(), 4781, r4.data(), 0, acc.data()) && fast::affine_limb<primes[1]>(b.data(), P, wts.data(), 4781, r4.data(), 1, acc.data())
         && fast::affine_limb<primes[2]>(b.data(), P, wts.data(), 4781, r4.data(), 2, acc.data()), "cloud v4 reference");
    must(r5 == r4, "cloud words F=" + std::to_string(F));
    std::vector<double> x5(rows), x4(rows); eng.decryptor.run(math, k.set, k.sk, k.fft, r5.data(), rows, x5.data()); v4_decrypt(math, k, r5.data(), rows, x4.data());
    must(std::memcmp(x5.data(), x4.data(), rows * 8) == 0, "decrypt bitwise F=" + std::to_string(F));
  }
  // cloud failure path: noncanonical word -> error and zeroed output (v4 behaviour)
  { std::vector<uint32_t> in(4 * W, 1), out(W, 7); in[5 * degree + 3] = primes[2]; std::vector<int64_t> w(4, 3);
    must(ef_cloud_affine(in.data(), in.size(), 4, w.data(), 0, out.data(), W) == -1 && std::all_of(out.begin(), out.end(), [](uint32_t x) { return x == 0; }), "cloud noncanonical refusal"); }
  // API round trips (CPU and spec) within the owner's certified B04 bound; device calls per request = ceil(F/4)
  size_t api = 0;
  for (unsigned backend : {0u, 1u}) {
    void* o = backend ? ef_owner_create(3, backend, argv[1]) : first; must(o != nullptr, "owner create");
    uint64_t calls0 = 0; ef_owner_device_calls(o, &calls0);
    for (size_t F : {size_t(4), size_t(10), size_t(16)}) for (size_t rows : {size_t(1), size_t(2048)}) {
      const auto v = random_values(rng, rows, F); const size_t P = (F + 3) / 4 * 4;
      std::vector<double> w(F); std::vector<int64_t> wi(P, 0); std::uniform_real_distribution<double> ud(-1, 1);
      for (size_t f = 0; f < F; f++) { w[f] = ud(rng); wi[f] = int64_t(std::llround(w[f] * double(scale))); }
      std::vector<uint32_t> c(P * W), r(W); std::vector<double> y(rows);
      must(ef_owner_encrypt(o, v.data(), rows, F, c.data(), c.size()) == 0, ef_owner_error());
      must(ef_cloud_affine(c.data(), c.size(), P, wi.data(), 0, r.data(), W) == 0, "api cloud");
      must(ef_owner_decrypt(o, r.data(), W, rows, y.data()) == 0, ef_owner_error());
      double bound = 0; must(ef_owner_error_bound(o, wi.data(), w.data(), F, 0, 0.0, 8, 40, &bound) == 0, "bound");
      double err = 0; for (size_t i = 0; i < rows; i++) { double e = 0; for (size_t f = 0; f < F; f++) e += double(wi[f]) / double(scale) * v[i * F + f]; err = std::max(err, std::abs(y[i] - e)); }
      must(err <= bound && err < 5e-3, "api oracle F=" + std::to_string(F)); api++;
      uint64_t calls = 0; ef_owner_device_calls(o, &calls); calls0 += backend ? (F + 3) / 4 : 0; must(calls == calls0, "api device calls");
    }
    std::vector<double> bad(4, 9.0); std::vector<uint32_t> c(4 * W);
    must(ef_owner_encrypt(o, bad.data(), 1, 4, c.data(), c.size()) == -1, "out-of-profile refusal");
    std::vector<uint32_t> nc(W, 0); nc[100] = primes[0]; std::vector<double> y(1);
    must(ef_owner_decrypt(o, nc.data(), W, 1, y.data()) == -1, "noncanonical decrypt refusal");
    ef_owner_destroy(o);
  }
  std::printf("APP_V5_EQUIVALENCE_PASS checks=%zu cpu_groups=%zu pl_frames=%zu api_round_trips=%zu NOT_DEVICE_TEST\n", checks, cpu_groups, pl_frames, api);
  return 0;
}
