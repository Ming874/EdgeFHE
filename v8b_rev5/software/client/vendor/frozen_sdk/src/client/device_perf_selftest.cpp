// V7 performance + parallelism gate through the real proxy device (or, with EDGEFHE_DEVICE_MOCK, an offline mock).
// Fresh OS-entropy secret/noise; nothing persisted. Current (accepted) driver; public timings are observations.
//  A. 6 A/B trials: CPU accepted == CPU fast == PL accepted == PL fast (every coefficient), CPU-decrypted affine
//     result within the analytic bound and 5e-4.
//  B. 20 timed reps of each path (fresh sampling outside the timer): median / P10 / P90 in microseconds.
//  C. Throughput over 24 jobs (4 features x 2048 slots each): CPU-only (fast, 3 workers) sequential; PL fast
//     sequential; two-slot PS/PL pipeline with P=2 and P=3 prepare threads; every pipelined ciphertext is
//     afterwards checked exactly against Engine::encrypt_residues on the same residues.
// Expected device calls: 1 LOAD + 2*6 + 2*20 + 24 + 2*24 = 125 (one MM2S and one S2MM completion each).
#include "fast_encrypt.hpp"
#include "pipeline.hpp"
#include "profile.hpp"
#include "../driver/edgefhe_policy.h"
#include <algorithm>
#include <chrono>
#include <iostream>
#include <openssl/sha.h>
#include <random>
using namespace edgefhe;
namespace v7 = edgefhe_v7;
using Clock = std::chrono::steady_clock;

#ifdef EDGEFHE_DEVICE_MOCK
struct MockKernel {
 ef_policy policy{}; Engine math{3}; PublicKey key; unsigned calls = 0;
 std::thread::id owner{};  // same single-DMA-owner rule as UnifiedDevice::check_owner
 v7::Poly input(const edgefhe_v7_request& r, size_t poly) {
  auto *wire = reinterpret_cast<const v7::Beat*>(r.input_ptr); v7::Poly out;
  for (auto& l : out) l.resize(degree);
  for (size_t i = 0; i < degree; i++) for (size_t l = 0; l < 3; l++) out[l][i] = wire[poly * degree + i].limb[l];
  return out;
 }
 int call(edgefhe_v7_request& r) {
  if (owner == std::thread::id{}) owner = std::this_thread::get_id();
  if (owner != std::this_thread::get_id()) return -EPERM;
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
 uint64_t calls() const { return mock.calls; }
 const char *kind() const { return "offline_mock"; }
};
#else
struct Transport {
 v7::UnifiedDevice device;
 explicit Transport(const char *path) : device(path) {}
 void load(const PublicKey& key) { device.load_key(key); }
 v7::PairedSession& session() { return device.session(); }
 uint64_t calls() const { return device.completed_calls(); }
 const char *kind() const { return "proxy_device"; }
};
#endif

static std::array<std::vector<std::complex<double>>, 4> job_slots(uint64_t id) {
 std::array<std::vector<std::complex<double>>, 4> x;
 for (size_t f = 0; f < 4; f++) { x[f].resize(slots); for (size_t i = 0; i < slots; i++) x[f][i] = (int((i * 3 + f * 19 + id * 7) % 101) - 50) / 25.0; }
 return x;
}
static void stats(const char* name, std::vector<double> v) {
 std::sort(v.begin(), v.end());
 auto q = [&](double p) { return v[std::min(v.size() - 1, size_t(p * (v.size() - 1) + 0.5))]; };
 std::cout << "V7_PERF_TIMING path=" << name << " n=" << v.size() << " median_us=" << q(0.5) << " p10_us=" << q(0.1)
           << " p90_us=" << q(0.9) << "\n";
}
static bool same(const std::array<Cipher, 4>& a, const std::array<Cipher, 4>& b) {
 for (size_t f = 0; f < 4; f++) if (a[f].c0 != b[f].c0 || a[f].c1 != b[f].c1) return false;
 return true;
}

int main(int argc, char **argv) {
 try {
  require(argc == 3, "usage: device_perf_selftest <os_prng_plugin> <device>");
  initialize_secure_sampler(argv[1]);
  Engine cpu(3);
  auto material = sample_secure_material();
  auto key = keygen_secure(cpu, material);
  Transport transport(argv[2]);
  transport.load(key);
  auto& session = transport.session();
  const uint64_t generation = session.generation();
  require(generation >= 1 && transport.calls() == 1, "LOAD accepted with a nonzero generation");
  // A. A/B exactness and decryption.
  uint64_t exact = 0; double max_error = 0;
  const std::array<double, 4> w = {1.1, -.7, .9, .4}; std::array<Message, 4> weights;
  for (size_t f = 0; f < 4; f++) weights[f] = encode(std::vector<std::complex<double>>(slots, w[f]));
  auto bias = encode(std::vector<std::complex<double>>(slots, -.35), scale * scale);
  for (unsigned trial = 0; trial < 6; trial++) {
   auto x = job_slots(1000 + trial); auto messages = cpu.encode_four(x); sample_secure_encryption(material);
   require(affine_coefficient_bound(messages, weights, bias, material) < UWide(primes[0]) * primes[1] * primes[2] / 2, "No-wrap");
   auto ca = encrypt_secure(cpu, key, messages, material);
   auto cf = v7::cpu_encrypt_secure_fast(cpu, key, messages, material);
   auto pa = v7::encrypt_secure_paired(session, messages, material);
   auto pf = v7::encrypt_secure_paired_fast(session, messages, material);
   require(same(ca, cf) && same(ca, pa) && same(ca, pf), "A/B: all four encryption paths bit-identical");
   exact += 4 * 2 * 3 * degree;
   auto result = decode(decrypt_secure(cpu, cpu.affine(pf, weights, bias), material.secret), scale * scale);
   const double bound = secure_affine_error_bound(weights, material, 2);
   for (size_t i = 0; i < slots; i++) {
    double e = -.35; for (size_t f = 0; f < 4; f++) e += w[f] * x[f][i].real();
    const double err = std::abs(result[i] - e); max_error = std::max(max_error, err);
    require(err <= bound && err <= 5e-4, "CKKS secure slot error");
   }
  }
  // B. Timed repetitions (sampling and encoding outside the timer; path order rotates every repetition).
  std::vector<double> t_ca, t_cf, t_pa, t_pf;
  for (unsigned rep = 0; rep < 20; rep++) {
   auto messages = cpu.encode_four(job_slots(2000 + rep)); sample_secure_encryption(material);
   for (unsigned k = 0; k < 4; k++) {
    const unsigned which = (k + rep) % 4; auto t0 = Clock::now();
    if (which == 0) (void)encrypt_secure(cpu, key, messages, material);
    if (which == 1) (void)v7::cpu_encrypt_secure_fast(cpu, key, messages, material);
    if (which == 2) (void)v7::encrypt_secure_paired(session, messages, material);
    if (which == 3) (void)v7::encrypt_secure_paired_fast(session, messages, material);
    const double us = std::chrono::duration<double, std::micro>(Clock::now() - t0).count();
    (which == 0 ? t_ca : which == 1 ? t_cf : which == 2 ? t_pa : t_pf).push_back(us);
   }
  }
  stats("cpu3_accepted", t_ca); stats("cpu3_fast", t_cf); stats("pl_accepted", t_pa); stats("pl_fast", t_pf);
  // C. Throughput: encode + sample + encrypt for 24 jobs.
  const uint64_t jobs = 24;
  auto seconds = [](Clock::time_point a) { return std::chrono::duration<double>(Clock::now() - a).count(); };
  SecureMaterial scratch;
  auto t0 = Clock::now();
  for (uint64_t id = 0; id < jobs; id++) { auto m = cpu.encode_four(job_slots(id)); sample_secure_encryption(scratch); (void)v7::cpu_encrypt_secure_fast(cpu, key, m, scratch); }
  const double cpu_s = seconds(t0);
  t0 = Clock::now();
  for (uint64_t id = 0; id < jobs; id++) { auto m = cpu.encode_four(job_slots(id)); sample_secure_encryption(scratch); (void)v7::encrypt_secure_paired_fast(session, m, scratch); }
  const double pl_seq_s = seconds(t0);
  double pipe_s[2] = {0, 0}; uint64_t pipe_checked = 0; size_t max_reorder = 0;
  for (unsigned p = 2; p <= 3; p++) {
   v7::EncryptPipeline pipeline(session, {p, 2, true});
   std::vector<v7::EncryptedRecord> records; uint64_t next = 0;
   t0 = Clock::now();
   auto st = pipeline.run([&]() -> std::optional<v7::PipelineJob> { if (next == jobs) return std::nullopt; v7::PipelineJob j{next, job_slots(next)}; next++; return j; },
                          [&](v7::EncryptedRecord&& r) { records.push_back(std::move(r)); });
   pipe_s[p - 2] = seconds(t0); max_reorder = std::max(max_reorder, st.max_reorder);
   require(st.delivered == jobs && records.size() == jobs, "pipeline delivered every job once");
   for (uint64_t id = 0; id < jobs; id++) {
    require(records[id].id == id, "pipeline in-order delivery");
    const auto& in = *records[id].inputs;
    auto expected = cpu.encrypt_residues(key, in.message, in.mask, in.e0, in.e1);
    for (size_t f = 0; f < 4; f++) require(expected[f].c0 == records[id].cipher[f].c0 && expected[f].c1 == records[id].cipher[f].c1, "pipeline exact");
    pipe_checked += 2 * 3 * degree * 4;
   }
  }
  std::cout << "V7_PERF_THROUGHPUT jobs=" << jobs << " slots_per_job=" << 4 * slots << " cpu3_fast_jobs_per_s=" << jobs / cpu_s
            << " pl_fast_sequential_jobs_per_s=" << jobs / pl_seq_s << " pipeline_p2_jobs_per_s=" << jobs / pipe_s[0]
            << " pipeline_p3_jobs_per_s=" << jobs / pipe_s[1] << " max_reorder=" << max_reorder << "\n";
  require(transport.calls() == 125 && session.generation() == generation, "125 device calls on one lease");
  material.e0[0][0] = 56; bool rejected = false;
  try { v7::encrypt_secure_paired_fast(session, cpu.encode_four({{{0}, {0}, {0}, {0}}}), material); } catch (const std::exception&) { rejected = true; }
  require(rejected && transport.calls() == 125, "Out-of-support Gaussian rejected before any I/O");
  std::cout << "V7_PERF_DEVICE_SELFTEST_PASS transport=" << transport.kind() << " trials=6 exact_values=" << exact
            << " pipeline_exact_values=" << pipe_checked << " max_slot_error=" << max_error << " reps=20 jobs=24 generation="
            << generation << " calls=" << transport.calls() << "\n";
  return 0;
 } catch (const std::exception& e) {
  std::cerr << "V7_PERF_DEVICE_SELFTEST_FAIL " << e.what() << '\n';
  return 1;
 }
}
