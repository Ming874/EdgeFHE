// V7 secure-profile full CKKS math gate through the real proxy device (or, with EDGEFHE_DEVICE_MOCK, the
// offline mock kernel used by bridge_selftest). Fresh OS-entropy secret/noise material per process; nothing persisted.
// Per trial: CPU encrypt_secure vs PL PAIR4 + PS fusion must be equal coefficient by coefficient; cloud affine on
// CPU; decryption via PL negacyclic multiply must equal CPU decrypt_secure exactly; slot error <= analytic bound
// and <= 5e-4. Timing lines are single-process observations, not a benchmark.
#include "secure_bridge.hpp"
#include "../driver/edgefhe_policy.h"
#include <chrono>
#include <iostream>
#include <openssl/sha.h>
using namespace edgefhe;
namespace v7 = edgefhe_v7;
using Clock = std::chrono::steady_clock;

#ifdef EDGEFHE_DEVICE_MOCK
struct MockKernel {
 ef_policy policy{}; Engine math{3}; PublicKey key; unsigned calls = 0;
 v7::Poly input(const edgefhe_v7_request& r, size_t poly) {
  auto *wire = reinterpret_cast<const v7::Beat*>(r.input_ptr); v7::Poly out;
  for (auto& l : out) l.resize(degree);
  for (size_t i = 0; i < degree; i++) for (size_t l = 0; l < 3; l++) out[l][i] = wire[poly * degree + i].limb[l];
  return out;
 }
 int call(edgefhe_v7_request& r) {
  v7::validate_request(r);
  if (!std::equal(v7::active_profile.begin(), v7::active_profile.end(), r.profile_sha256)) return -EINVAL;
  if (r.input_words && ef_canonical(reinterpret_cast<const ef_u32*>(r.input_ptr), r.input_words)) return -EINVAL;
  int rc = ef_begin(&policy, r.command, 1, r.generation, r.profile_sha256, r.public_key_sha256);
  if (rc) return rc;
  auto *out = reinterpret_cast<v7::Beat*>(r.output_ptr);
  if (r.command == 6) { key = {input(r, 0), input(r, 1)}; r.generation = policy.generation; out[0] = {}; }
  else if (r.command == 7) {
   for (size_t f = 0; f < 4; f++) {
    auto u = input(r, f), bu = math.multiply(key.b, u), au = math.multiply(key.a, u);
    for (size_t i = 0; i < degree; i++) {
     out[2 * f * degree + i] = {}; out[(2 * f + 1) * degree + i] = {};
     for (size_t l = 0; l < 3; l++) { out[2 * f * degree + i].limb[l] = bu[l][i]; out[(2 * f + 1) * degree + i].limb[l] = au[l][i]; }
    }
   }
  } else if (r.command == 2) {
   auto result = math.multiply(input(r, 0), input(r, 1));
   for (size_t i = 0; i < degree; i++) { out[i] = {}; for (size_t l = 0; l < 3; l++) out[i].limb[l] = result[l][i]; }
  } else return -EINVAL;
  calls++; r.status = ef_finish(&policy, 255, 0); r.hardware_status = 0; return r.status;
 }
};
struct Transport {
 MockKernel mock; v7::PairedSession paired{v7::active_profile, [this](edgefhe_v7_request& r) { return mock.call(r); }};
 explicit Transport(const char*) {}
 void load(const PublicKey& key) {
  std::vector<v7::Beat> wire; v7::pack_into(6, {key.b, key.a}, wire); v7::Digest d{};
  SHA256(reinterpret_cast<const unsigned char*>(wire.data()), wire.size() * 16, d.data()); paired.load(key.b, key.a, d);
 }
 v7::PairedSession& session() { return paired; }
 v7::Poly multiply(const v7::Poly& a, const v7::Poly& b) { return paired.legacy(2, {a, b}).at(0); }
 uint64_t calls() const { return mock.calls; }
 const char *kind() const { return "offline_mock"; }
};
#else
struct Transport {
 v7::UnifiedDevice device;
 explicit Transport(const char *path) : device(path) {}
 void load(const PublicKey& key) { device.load_key(key); }
 v7::PairedSession& session() { return device.session(); }
 v7::Poly multiply(const v7::Poly& a, const v7::Poly& b) { return device.multiply(a, b); }
 uint64_t calls() const { return device.completed_calls(); }
 const char *kind() const { return "proxy_device"; }
};
#endif

int main(int argc, char **argv) {
 try {
  require(argc == 3, "usage: device_secure_selftest <os_prng_plugin> <device>");
  initialize_secure_sampler(argv[1]);
  Engine cpu(3);
  auto material = sample_secure_material();
  auto key = keygen_secure(cpu, material);
  Transport transport(argv[2]);
  transport.load(key);
  auto& session = transport.session();
  const uint64_t generation = session.generation();
  require(generation >= 1 && transport.calls() == 1, "LOAD accepted with a nonzero generation");
  uint64_t exact = 0; double max_error = 0; uint64_t slots_checked = 0;
  const std::array<double, 4> w = {1.1, -.7, .9, .4}; std::array<Message, 4> weights;
  for (size_t f = 0; f < 4; f++) weights[f] = encode(std::vector<std::complex<double>>(slots, w[f]));
  auto bias = encode(std::vector<std::complex<double>>(slots, -.35), scale * scale);
  for (unsigned trial = 0; trial < 6; trial++) {
   size_t records = trial % 2 ? 2048 : 1; std::array<std::vector<std::complex<double>>, 4> x;
   for (size_t f = 0; f < 4; f++) { x[f].resize(records); for (size_t i = 0; i < records; i++) x[f][i] = (int((i + f * 19 + trial * 7) % 101) - 50) / 25.0; }
   auto messages = cpu.encode_four(x); sample_secure_encryption(material);
   require(affine_coefficient_bound(messages, weights, bias, material) < UWide(primes[0]) * primes[1] * primes[2] / 2, "No-wrap");
   auto t0 = Clock::now();
   auto expected = encrypt_secure(cpu, key, messages, material);
   auto t1 = Clock::now();
   auto actual = v7::encrypt_secure_paired(session, messages, material);
   auto t2 = Clock::now();
   for (size_t f = 0; f < 4; f++) for (auto member : {&Cipher::c0, &Cipher::c1}) for (size_t l = 0; l < 3; l++) for (size_t i = 0; i < degree; i++) {
    require((expected[f].*member)[l][i] == (actual[f].*member)[l][i], "Exact CPU-vs-PL secure ciphertext mismatch"); exact++;
   }
   auto response = cpu.affine(actual, weights, bias);
   auto product = transport.multiply(response.c1, residues(material.secret));
   auto integer = centered(add(response.c0, product));
   require(integer == decrypt_secure(cpu, response, material.secret), "Exact PL-vs-CPU decryption"); exact += degree;
   auto result = decode(integer, scale * scale); double bound = secure_affine_error_bound(weights, material, 2);
   for (size_t i = 0; i < records; i++) {
    double expected_score = -.35; for (size_t f = 0; f < 4; f++) expected_score += w[f] * x[f][i].real();
    double err = std::abs(result[i] - expected_score); max_error = std::max(max_error, err); slots_checked++;
    require(err <= bound && err <= 5e-4, "CKKS secure slot error");
   }
   std::cout << "V7_SECURE_TRIAL " << trial << " records=" << records << " cpu_encrypt_us="
             << std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count() << " pl_encrypt_us="
             << std::chrono::duration_cast<std::chrono::microseconds>(t2 - t1).count() << " bound=" << bound << " EXACT\n";
  }
  require(transport.calls() == 13 && session.generation() == generation, "One LOAD, six PAIR4, six PL decrypt multiplies");
  material.e0[0][0] = 56; bool rejected = false;
  try { v7::encrypt_secure_paired(session, cpu.encode_four({{{0}, {0}, {0}, {0}}}), material); } catch (const std::exception&) { rejected = true; }
  require(rejected && transport.calls() == 13, "Out-of-support Gaussian rejected before any I/O");
  std::cout << "V7_SECURE_DEVICE_SELFTEST_PASS transport=" << transport.kind() << " trials=6 exact_values=" << exact
            << " slots=" << slots_checked << " max_slot_error=" << max_error << " generation=" << generation
            << " calls=" << transport.calls() << "\n";
  return 0;
 } catch (const std::exception& e) {
  std::cerr << "V7_SECURE_DEVICE_SELFTEST_FAIL " << e.what() << '\n';
  return 1;
 }
}
