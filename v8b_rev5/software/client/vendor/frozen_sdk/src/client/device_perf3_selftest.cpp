// V7 perf3 gate: single-change ablation of the PS prepare path on the CURRENT driver (plan steps 08-11).
// Real proxy device (or, with EDGEFHE_DEVICE_MOCK, an offline mock). Fresh OS-entropy material; nothing persisted.
//  A. 3 A/B trials with the lock-free sampler: CPU accepted encrypt_secure == PL fast (every coefficient); CPU
//     decrypt of the cloud affine within the analytic bound and 5e-4.
//  B. Prepare profile (no device), 20 reps, order rotated: 4x encode (pipeline worker), encode_four (3 workers),
//     locked sampler, lock-free sampler, 16 RNS conversions of the accepted Stage A.
//  C. Prepare-only rates (no device), 24 jobs, P = 1/2/3 threads: accepted (encode + locked sample + RNS) vs fast
//     (encode + lock-free sample).
//  D. Throughput, 24 jobs each (encode + sample + encrypt): CPU3 fast sequential; CPU job-parallel (3 threads, one
//     single-worker Engine each); PL fast sequential; accepted pipeline P=2; fast pipeline P=1/2/3. Every pipelined
//     ciphertext is checked exactly afterwards (accepted: Engine::encrypt_residues on the same residues; fast: the
//     accepted CPU encrypt_secure on the same signed material).
// Expected device calls: 1 LOAD + 3 + 24 + 4*24 = 124 (one MM2S and one S2MM completion each).
#include "fast_encrypt.hpp"
#include "pipeline.hpp"
#include "profile.hpp"
#include "../driver/edgefhe_policy.h"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <iostream>
#include <openssl/sha.h>
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
 std::cout << "V7_PERF3_PREPARE step=" << name << " n=" << v.size() << " median_us=" << q(0.5) << " p10_us=" << q(0.1)
           << " p90_us=" << q(0.9) << "\n";
}
static bool same(const std::array<Cipher, 4>& a, const std::array<Cipher, 4>& b) {
 for (size_t f = 0; f < 4; f++) if (a[f].c0 != b[f].c0 || a[f].c1 != b[f].c1) return false;
 return true;
}
static double seconds(Clock::time_point a) { return std::chrono::duration<double>(Clock::now() - a).count(); }
// Run `jobs` work items on `threads` threads pulling ids from a shared counter; returns wall seconds.
template<class F> static double parallel_jobs(unsigned threads, uint64_t jobs, F work) {
 std::atomic<uint64_t> next{0}; std::vector<std::thread> t; std::vector<std::exception_ptr> err(threads);
 auto t0 = Clock::now();
 for (unsigned i = 0; i < threads; i++) t.emplace_back([&, i] {
  try { for (uint64_t id = next++; id < jobs; id = next++) work(i, id); } catch (...) { err[i] = std::current_exception(); }
 });
 for (auto& x : t) x.join();
 const double s = seconds(t0);
 for (auto& e : err) if (e) std::rethrow_exception(e);
 return s;
}

int main(int argc, char **argv) {
 try {
  require(argc == 3, "usage: device_perf3_selftest <os_prng_plugin> <device>");
  initialize_secure_sampler(argv[1]);
  Engine cpu(3);
  auto material = sample_secure_material();
  auto key = keygen_secure(cpu, material);
  Transport transport(argv[2]);
  transport.load(key);
  auto& session = transport.session();
  const uint64_t generation = session.generation();
  require(generation >= 1 && transport.calls() == 1, "LOAD accepted with a nonzero generation");
  // A. A/B exactness with the lock-free sampler, and decryption.
  uint64_t exact = 0; double max_error = 0;
  const std::array<double, 4> w = {1.1, -.7, .9, .4}; std::array<Message, 4> weights;
  for (size_t f = 0; f < 4; f++) weights[f] = encode(std::vector<std::complex<double>>(slots, w[f]));
  auto bias = encode(std::vector<std::complex<double>>(slots, -.35), scale * scale);
  for (unsigned trial = 0; trial < 3; trial++) {
   auto x = job_slots(1000 + trial); auto messages = cpu.encode_four(x); sample_secure_encryption_concurrent(material);
   require(affine_coefficient_bound(messages, weights, bias, material) < UWide(primes[0]) * primes[1] * primes[2] / 2, "No-wrap");
   auto ca = encrypt_secure(cpu, key, messages, material);
   auto pf = v7::encrypt_secure_paired_fast(session, messages, material);
   require(same(ca, pf), "A/B: CPU accepted == PL fast with lock-free material");
   exact += 4 * 2 * 3 * degree;
   auto result = decode(decrypt_secure(cpu, cpu.affine(pf, weights, bias), material.secret), scale * scale);
   const double bound = secure_affine_error_bound(weights, material, 2);
   for (size_t i = 0; i < slots; i++) {
    double e = -.35; for (size_t f = 0; f < 4; f++) e += w[f] * x[f][i].real();
    const double err = std::abs(result[i] - e); max_error = std::max(max_error, err);
    require(err <= bound && err <= 5e-4, "CKKS secure slot error");
   }
  }
  // B. Prepare profile on this thread, no device.
  std::vector<double> t_enc, t_enc3, t_lock, t_free, t_rns;
  SecureMaterial scratch;
  for (unsigned rep = 0; rep < 20; rep++) {
   const auto x = job_slots(3000 + rep);
   std::array<Message, 4> m;
   for (unsigned k = 0; k < 5; k++) {
    const unsigned which = (k + rep) % 5; auto t0 = Clock::now();
    if (which == 0) for (size_t f = 0; f < 4; f++) m[f] = encode(x[f]);
    if (which == 1) (void)cpu.encode_four(x);
    if (which == 2) sample_secure_encryption(scratch);
    if (which == 3) sample_secure_encryption_concurrent(scratch);
    if (which == 4) {
     if (m[0].empty()) for (size_t f = 0; f < 4; f++) m[f] = encode(x[f]);
     t0 = Clock::now();
     std::array<v7::Poly, 4> r[4];
     for (size_t f = 0; f < 4; f++) { r[0][f] = residues(m[f]); r[1][f] = residues(scratch.mask[f]); r[2][f] = residues(scratch.e0[f]); r[3][f] = residues(scratch.e1[f]); }
    }
    const double us = std::chrono::duration<double, std::micro>(Clock::now() - t0).count();
    (which == 0 ? t_enc : which == 1 ? t_enc3 : which == 2 ? t_lock : which == 3 ? t_free : t_rns).push_back(us);
   }
  }
  stats("encode_4x_single", t_enc); stats("encode_four_3workers", t_enc3); stats("sample_locked", t_lock);
  stats("sample_lockfree", t_free); stats("rns_16_accepted_stage_a", t_rns);
  // C. Prepare-only rates, no device.
  const uint64_t jobs = 24;
  for (int fast = 0; fast <= 1; fast++) for (unsigned p = 1; p <= 3; p++) {
   std::vector<SecureMaterial> mats(p);
   const double s = parallel_jobs(p, jobs, [&](unsigned t, uint64_t id) {
    const auto x = job_slots(id); std::array<Message, 4> m;
    for (size_t f = 0; f < 4; f++) m[f] = encode(x[f]);
    if (fast) { sample_secure_encryption_concurrent(mats[t]); return; }
    sample_secure_encryption(mats[t]);
    std::array<v7::Poly, 4> r[4];
    for (size_t f = 0; f < 4; f++) { r[0][f] = residues(m[f]); r[1][f] = residues(mats[t].mask[f]); r[2][f] = residues(mats[t].e0[f]); r[3][f] = residues(mats[t].e1[f]); }
   });
   std::cout << "V7_PERF3_PREPARE_RATE kind=" << (fast ? "fast" : "accepted") << " threads=" << p << " jobs=" << jobs
             << " jobs_per_s=" << jobs / s << "\n";
  }
  // D. Throughput.
  auto t0 = Clock::now();
  for (uint64_t id = 0; id < jobs; id++) { auto m = cpu.encode_four(job_slots(id)); sample_secure_encryption_concurrent(scratch); (void)v7::cpu_encrypt_secure_fast(cpu, key, m, scratch); }
  const double cpu_seq_s = seconds(t0);
  std::vector<std::unique_ptr<Engine>> singles; std::vector<SecureMaterial> mats(3);
  for (int i = 0; i < 3; i++) singles.push_back(std::make_unique<Engine>(1));
  for (auto& e : singles) (void)v7::cpu_encrypt_secure_fast(*e, key, cpu.encode_four(job_slots(0)), material);  // warm key caches
  const double cpu_par_s = parallel_jobs(3, jobs, [&](unsigned t, uint64_t id) {
   const auto x = job_slots(id); std::array<Message, 4> m;
   for (size_t f = 0; f < 4; f++) m[f] = encode(x[f]);
   sample_secure_encryption_concurrent(mats[t]);
   (void)v7::cpu_encrypt_secure_fast(*singles[t], key, m, mats[t]);
  });
  t0 = Clock::now();
  for (uint64_t id = 0; id < jobs; id++) { auto m = cpu.encode_four(job_slots(id)); sample_secure_encryption_concurrent(scratch); (void)v7::encrypt_secure_paired_fast(session, m, scratch); }
  const double pl_seq_s = seconds(t0);
  struct Run { const char *name; unsigned p; bool fast; double s = 0; };
  std::array<Run, 4> runs = {{{"pipe_accepted_p2", 2, false}, {"pipe_fast_p1", 1, true}, {"pipe_fast_p2", 2, true}, {"pipe_fast_p3", 3, true}}};
  uint64_t pipe_checked = 0; size_t max_reorder = 0;
  for (auto& run : runs) {
   v7::EncryptPipeline pipeline(session, {run.p, 2, true, run.fast});
   std::vector<v7::EncryptedRecord> records; uint64_t next = 0;
   t0 = Clock::now();
   auto st = pipeline.run([&]() -> std::optional<v7::PipelineJob> { if (next == jobs) return std::nullopt; v7::PipelineJob j{next, job_slots(next)}; next++; return j; },
                          [&](v7::EncryptedRecord&& r) { records.push_back(std::move(r)); });
   run.s = seconds(t0); max_reorder = std::max(max_reorder, st.max_reorder);
   require(st.delivered == jobs && records.size() == jobs, "pipeline delivered every job once");
   for (uint64_t id = 0; id < jobs; id++) {
    require(records[id].id == id, "pipeline in-order delivery");
    const auto& in = *records[id].inputs;
    std::array<Cipher, 4> expected;
    if (run.fast) { require(in.material != nullptr, "fast record material"); expected = encrypt_secure(cpu, key, in.message_int, *in.material); }
    else expected = cpu.encrypt_residues(key, in.message, in.mask, in.e0, in.e1);
    for (size_t f = 0; f < 4; f++) require(expected[f].c0 == records[id].cipher[f].c0 && expected[f].c1 == records[id].cipher[f].c1, "pipeline exact");
    pipe_checked += 2 * 3 * degree * 4;
   }
  }
  std::cout << "V7_PERF3_THROUGHPUT jobs=" << jobs << " slots_per_job=" << 4 * slots << " cpu3_fast_seq=" << jobs / cpu_seq_s
            << " cpu_jobparallel_3=" << jobs / cpu_par_s << " pl_fast_seq=" << jobs / pl_seq_s;
  for (const auto& run : runs) std::cout << " " << run.name << "=" << jobs / run.s;
  std::cout << " max_reorder=" << max_reorder << "\n";
  require(transport.calls() == 124 && session.generation() == generation, "124 device calls on one lease");
  material.e0[0][0] = 56; bool rejected = false;
  try { v7::encrypt_secure_paired_fast(session, cpu.encode_four({{{0}, {0}, {0}, {0}}}), material); } catch (const std::exception&) { rejected = true; }
  require(rejected && transport.calls() == 124, "Out-of-support Gaussian rejected before any I/O");
  std::cout << "V7_PERF3_DEVICE_SELFTEST_PASS transport=" << transport.kind() << " trials=3 exact_values=" << exact
            << " pipeline_exact_values=" << pipe_checked << " max_slot_error=" << max_error << " jobs=24 generation="
            << generation << " calls=" << transport.calls() << "\n";
  return 0;
 } catch (const std::exception& e) {
  std::cerr << "V7_PERF3_DEVICE_SELFTEST_FAIL " << e.what() << '\n';
  return 1;
 }
}
