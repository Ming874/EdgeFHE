// V7 perf4 gate (2026-10-15): fair-sink measurement protocol (E01 step 2) + E02 sampler ablation (current vs A2).
// Real proxy device (or, with EDGEFHE_DEVICE_MOCK, an offline mock). Fresh OS-entropy material; nothing persisted.
// Differences from perf3, each removing a measurement asymmetry or adding the sampler factor:
//  - every timed mode hands each ciphertext to the same sink (read every word once, then destroy, inside the timer);
//    perf3 discarded CPU outputs at once but kept all pipeline outputs and their inputs alive past the timer;
//  - the timed pipeline runs as shipped (keep_inputs_for_test off); exactness is checked in separate untimed runs;
//  - 48 jobs per mode, 10 rounds alternating sampler current / A2 (bit-identical algorithms, E02), mode order
//    rotated and printed per round; raw per-round rates are printed, statistics are computed off-board.
//  A. 3 A/B trials per sampler: CPU accepted encrypt_secure == PL fast (every coefficient); decrypt bound and 5e-4.
//  B. Untimed P=3 fast pipeline per sampler, 24 jobs, every ciphertext checked against the CPU accepted encrypt.
//  C. Timed rounds: cpu3_fast_seq, cpu_jobparallel_3 (3 single-worker Engines), pl_fast_seq, pipe_fast_p3.
// Expected device calls: 1 LOAD + 6 + 2*24 + 10*(48 + 48) = 1015 (one MM2S and one S2MM completion each).
// CPU-side secrets: the CPU modes overwrite one reused material per thread; the pipeline wipes each record's
// material and message on destruction. This leaves the PL modes with the extra wiping work (conservative for PL).
#include "fast_encrypt.hpp"
#include "pipeline.hpp"
#include "profile.hpp"
#include "../driver/edgefhe_policy.h"
#include "baseline/fast_sampler.hpp"
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
static void sample(SecureMaterial& m, int sampler) {
 if (sampler) sample_secure_encryption_fast<DirectKernels>(m); else sample_secure_encryption_concurrent(m);
}
// Common sink of every timed mode: read every ciphertext word once (what a serializer / send buffer does), then the
// ciphertext is destroyed inside the timed region. The sum only keeps the work observable.
template<class C> static uint64_t consume(std::array<C, 4>&& c) {
 uint64_t sum = 0;
 for (const auto& x : c) for (const auto* r : {&x.c0, &x.c1}) for (const auto& l : *r) for (auto w : l) sum += w;
 { auto dead = std::move(c); }
 return sum;
}

int main(int argc, char **argv) {
 try {
  require(argc == 3, "usage: device_perf4_selftest <os_prng_plugin> <device>");
  initialize_secure_sampler(argv[1]);
  Engine cpu(3);
  auto material = sample_secure_material();
  auto key = keygen_secure(cpu, material);
  Transport transport(argv[2]);
  transport.load(key);
  auto& session = transport.session();
  const uint64_t generation = session.generation();
  require(generation >= 1 && transport.calls() == 1, "LOAD accepted with a nonzero generation");
  const char* names[2] = {"current", "A2"};
  // A. A/B exactness per sampler (3 trials each): CPU accepted encrypt_secure == PL fast; decryption bound.
  uint64_t exact = 0; double max_error = 0;
  const std::array<double, 4> w = {1.1, -.7, .9, .4}; std::array<Message, 4> weights;
  for (size_t f = 0; f < 4; f++) weights[f] = encode(std::vector<std::complex<double>>(slots, w[f]));
  auto bias = encode(std::vector<std::complex<double>>(slots, -.35), scale * scale);
  for (int s = 0; s < 2; s++) for (unsigned trial = 0; trial < 3; trial++) {
   auto x = job_slots(1000 + 10 * s + trial); auto messages = cpu.encode_four(x); sample(material, s);
   require(affine_coefficient_bound(messages, weights, bias, material) < UWide(primes[0]) * primes[1] * primes[2] / 2, "No-wrap");
   auto ca = encrypt_secure(cpu, key, messages, material);
   auto pf = v7::encrypt_secure_paired_fast(session, messages, material);
   require(same(ca, pf), s ? "A/B: CPU accepted == PL fast, sampler A2" : "A/B: CPU accepted == PL fast, sampler current");
   exact += 4 * 2 * 3 * degree;
   auto result = decode(decrypt_secure(cpu, cpu.affine(pf, weights, bias), material.secret), scale * scale);
   const double bound = secure_affine_error_bound(weights, material, 2);
   for (size_t i = 0; i < slots; i++) {
    double e = -.35; for (size_t f = 0; f < 4; f++) e += w[f] * x[f][i].real();
    const double err = std::abs(result[i] - e); max_error = std::max(max_error, err);
    require(err <= bound && err <= 5e-4, "CKKS secure slot error");
   }
  }
  // B. Untimed pipeline correctness per sampler: P=3 fast pipeline with retained inputs, every ciphertext checked.
  const uint64_t check_jobs = 24;
  uint64_t pipe_checked = 0; size_t max_reorder = 0;
  for (int s = 0; s < 2; s++) {
   v7::EncryptPipeline pipeline(session, {3, 2, true, true, s == 1});
   std::vector<v7::EncryptedRecord> records; uint64_t next = 0;
   auto st = pipeline.run([&]() -> std::optional<v7::PipelineJob> { if (next == check_jobs) return std::nullopt; v7::PipelineJob j{next, job_slots(next)}; next++; return j; },
                          [&](v7::EncryptedRecord&& r) { records.push_back(std::move(r)); });
   max_reorder = std::max(max_reorder, st.max_reorder);
   require(st.delivered == check_jobs && records.size() == check_jobs, "pipeline delivered every job once");
   for (uint64_t id = 0; id < check_jobs; id++) {
    require(records[id].id == id, "pipeline in-order delivery");
    const auto& in = *records[id].inputs;
    require(in.material != nullptr, "fast record material");
    const auto expected = encrypt_secure(cpu, key, in.message_int, *in.material);
    for (size_t f = 0; f < 4; f++) require(expected[f].c0 == records[id].cipher[f].c0 && expected[f].c1 == records[id].cipher[f].c1, "pipeline exact");
    pipe_checked += 2 * 3 * degree * 4;
   }
  }
  // C. Timed rounds. Every mode: encode + sample + encrypt + common sink, for JOBS jobs; nothing retained.
  //    The pipeline runs as shipped (keep_inputs_for_test off: each record's inputs are wiped and freed on the
  //    device-owner thread inside the timed region). Rounds alternate the sampler; the mode order rotates by one
  //    position every two rounds, and the actual order is printed with each round.
  const uint64_t jobs = 48; const unsigned rounds = 10;
  std::vector<std::unique_ptr<Engine>> singles; std::vector<SecureMaterial> mats(3); SecureMaterial scratch;
  for (int i = 0; i < 3; i++) singles.push_back(std::make_unique<Engine>(1));
  for (auto& e : singles) (void)v7::cpu_encrypt_secure_fast(*e, key, cpu.encode_four(job_slots(0)), material);  // warm key caches
  const char* modes[4] = {"cpu3_fast_seq", "cpu_jobparallel_3", "pl_fast_seq", "pipe_fast_p3"};
  uint64_t digest = 0;
  for (unsigned r = 0; r < rounds; r++) {
   const int s = int(r % 2); const unsigned rot = (r / 2) % 4;
   double rate[4]; std::string order;
   for (unsigned k = 0; k < 4; k++) {
    const unsigned mode = (k + rot) % 4; order += (k ? "," : "") + std::string(modes[mode]);
    std::atomic<uint64_t> acc{0};
    auto t0 = Clock::now(); double sec = 0;
    if (mode == 0) {
     for (uint64_t id = 0; id < jobs; id++) { auto m = cpu.encode_four(job_slots(id)); sample(scratch, s); acc += consume(v7::cpu_encrypt_secure_fast(cpu, key, m, scratch)); }
     sec = seconds(t0);
    } else if (mode == 1) {
     sec = parallel_jobs(3, jobs, [&](unsigned t, uint64_t id) {
      const auto x = job_slots(id); std::array<Message, 4> m;
      for (size_t f = 0; f < 4; f++) m[f] = encode(x[f]);
      sample(mats[t], s);
      acc += consume(v7::cpu_encrypt_secure_fast(*singles[t], key, m, mats[t]));
     });
    } else if (mode == 2) {
     for (uint64_t id = 0; id < jobs; id++) { auto m = cpu.encode_four(job_slots(id)); sample(scratch, s); acc += consume(v7::encrypt_secure_paired_fast(session, m, scratch)); }
     sec = seconds(t0);
    } else {
     v7::EncryptPipeline pipeline(session, {3, 2, false, true, s == 1});
     uint64_t next = 0;
     auto st = pipeline.run([&]() -> std::optional<v7::PipelineJob> { if (next == jobs) return std::nullopt; v7::PipelineJob j{next, job_slots(next)}; next++; return j; },
                            [&](v7::EncryptedRecord&& rec) { require(rec.inputs == nullptr, "timed pipeline retains no inputs"); acc += consume(std::move(rec.cipher)); });
     sec = seconds(t0);
     require(st.delivered == jobs, "timed pipeline delivered every job");
     max_reorder = std::max(max_reorder, st.max_reorder);
    }
    rate[mode] = jobs / sec; digest ^= acc.load() + mode;
   }
   std::cout << "V7_PERF4_ROUND sampler=" << names[s] << " round=" << r << " order=" << order;
   for (unsigned mode = 0; mode < 4; mode++) std::cout << " " << modes[mode] << "=" << rate[mode];
   std::cout << " unit=jobs_per_s jobs=" << jobs << "\n";
  }
  const uint64_t expected_calls = 1 + 6 + 2 * check_jobs + rounds * 2 * jobs;
  require(transport.calls() == expected_calls && session.generation() == generation, "device calls on one lease");
  material.e0[0][0] = 56; bool rejected = false;
  try { v7::encrypt_secure_paired_fast(session, cpu.encode_four({{{0}, {0}, {0}, {0}}}), material); } catch (const std::exception&) { rejected = true; }
  require(rejected && transport.calls() == expected_calls, "Out-of-support Gaussian rejected before any I/O");
  std::cout << "V7_PERF4_DEVICE_SELFTEST_PASS transport=" << transport.kind() << " ab_trials=6 exact_values=" << exact
            << " pipeline_exact_values=" << pipe_checked << " max_slot_error=" << max_error << " samplers=current,A2 rounds="
            << rounds << " jobs_per_mode=" << jobs << " max_reorder=" << max_reorder << " sink_digest_nonzero=" << (digest != 0)
            << " generation=" << generation << " calls=" << transport.calls() << "\n";
  return 0;
 } catch (const std::exception& e) {
  std::cerr << "V7_PERF4_DEVICE_SELFTEST_FAIL " << e.what() << '\n';
  return 1;
 }
}
