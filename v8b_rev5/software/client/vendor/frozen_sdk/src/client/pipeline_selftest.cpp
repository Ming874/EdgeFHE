// Off-board test of the two-slot PS/PL pipeline with an offline mock accelerator.
// The mock computes exact PAIR4 products and then waits until a configurable "PL latency"
// has elapsed, emulating a device that works while the CPU is free. Correctness checks:
//   * every pipelined ciphertext equals Engine::encrypt_residues on the same residues (exact),
//   * every ciphertext decrypts to the job's slots within 5e-4,
//   * results arrive in order, nothing is lost or duplicated, a mid-stream device failure
//     stops all stages with one exception and no retry.
// Throughput numbers are x86 mock observations, NOT board measurements.
#include "pipeline.hpp"
#include "profile.hpp"
#include "../driver/edgefhe_policy.h"
#include <chrono>
#include <iostream>
#include <openssl/sha.h>
using namespace edgefhe;
namespace v7 = edgefhe_v7;
using Clock = std::chrono::steady_clock;

struct MockAccelerator {
  ef_policy policy{}; Engine math{1}; PublicKey key; unsigned calls = 0, fail_at = 0, overruns = 0;
  std::chrono::microseconds latency{0};
  // compute=false: latency-only device (canonical zero products) for throughput runs, so the x86 CPU cost of the
  // mock's own exact multiplication cannot masquerade as "PL time". Exactness runs keep compute=true.
  bool compute = true;
  v7::Poly input(const edgefhe_v7_request& r, size_t poly) {
    auto *wire = reinterpret_cast<const v7::Beat*>(r.input_ptr); v7::Poly out;
    for (auto& l : out) l.resize(degree);
    for (size_t i = 0; i < degree; i++) for (size_t l = 0; l < 3; l++) out[l][i] = wire[poly * degree + i].limb[l];
    return out;
  }
  std::thread::id owner{};  // same single-DMA-owner rule as UnifiedDevice::check_owner (2026-10-14 board finding)
  int call(edgefhe_v7_request& r) {
    const auto start = Clock::now();
    if (owner == std::thread::id{}) owner = std::this_thread::get_id();
    if (owner != std::this_thread::get_id()) return -EPERM;
    v7::validate_request(r);
    if (fail_at && calls + 1 == fail_at) { calls++; return -EIO; }
    if (r.input_words && ef_canonical(reinterpret_cast<const ef_u32*>(r.input_ptr), r.input_words)) return -EINVAL;
    int rc = ef_begin(&policy, r.command, 1, r.generation, r.profile_sha256, r.public_key_sha256);
    if (rc) return rc;
    auto *out = reinterpret_cast<v7::Beat*>(r.output_ptr);
    if (r.command == 6) { key = {input(r, 0), input(r, 1)}; r.generation = policy.generation; out[0] = {}; }
    else if (r.command == 7 && !compute) {
      for (size_t i = 0; i < 8 * degree; i++) out[i] = {};
      if (Clock::now() > start + latency) overruns++;
      std::this_thread::sleep_until(start + latency);
    } else if (r.command == 7) {
      for (size_t f = 0; f < 4; f++) {
        auto u = input(r, f), bu = math.multiply(key.b, u), au = math.multiply(key.a, u);
        for (size_t i = 0; i < degree; i++) {
          out[2 * f * degree + i] = {}; out[(2 * f + 1) * degree + i] = {};
          for (size_t l = 0; l < 3; l++) { out[2 * f * degree + i].limb[l] = bu[l][i]; out[(2 * f + 1) * degree + i].limb[l] = au[l][i]; }
        }
      }
      if (Clock::now() > start + latency) overruns++;  // mock math slower than the emulated PL: report it
      std::this_thread::sleep_until(start + latency);   // the "PL" is busy; the CPU thread just waits
    } else return -EINVAL;
    calls++; r.status = ef_finish(&policy, 255, 0); r.hardware_status = 0; return r.status;
  }
};

static std::array<std::vector<std::complex<double>>, 4> slots_for(uint64_t id) {
  std::array<std::vector<std::complex<double>>, 4> x;
  for (size_t f = 0; f < 4; f++) { x[f].resize(id % 3 == 0 ? slots : 1);
    for (size_t i = 0; i < x[f].size(); i++) x[f][i] = {(int((i + f * 19 + id * 7) % 101) - 50) / 25.0, 0}; }
  return x;
}

int main(int argc, char **argv) {
  try {
    require(argc == 3, "usage: pipeline_selftest OS_PLUGIN PL_LATENCY_US");
    initialize_secure_sampler(argv[1]);
    const auto latency = std::chrono::microseconds(std::stoul(argv[2]));
    Engine cpu(3);
    auto material = sample_secure_material();
    auto key = keygen_secure(cpu, material);
    auto make_session = [&](MockAccelerator& mock) {
      auto s = std::make_unique<v7::PairedSession>(v7::active_profile, [&mock](edgefhe_v7_request& r) { return mock.call(r); });
      std::vector<v7::Beat> wire; v7::pack_into(6, {key.b, key.a}, wire); v7::Digest d{};
      SHA256(reinterpret_cast<const unsigned char*>(wire.data()), wire.size() * 16, d.data());
      s->load(key.b, key.a, d); return s;
    };
    const uint64_t jobs = 24;
    // Sequential reference: one record at a time on one thread (prepare -> device -> next).
    MockAccelerator seq_mock; seq_mock.latency = latency; seq_mock.compute = false; auto seq_session = make_session(seq_mock);
    SecureMaterial scratch; auto t0 = Clock::now();
    for (uint64_t id = 0; id < jobs; id++) {
      auto x = slots_for(id); std::array<v7::Poly, 4> m, u, e0, e1;
      for (size_t f = 0; f < 4; f++) m[f] = residues(encode(x[f]));
      sample_secure_encryption(scratch);
      for (size_t f = 0; f < 4; f++) { u[f] = residues(scratch.mask[f]); e0[f] = residues(scratch.e0[f]); e1[f] = residues(scratch.e1[f]); }
      (void)v7::encrypt_four(*seq_session, u, m, e0, e1);
    }
    const double seq_s = std::chrono::duration<double>(Clock::now() - t0).count();
    // Fair throughput run: same work as the sequential loop (no verification inside the sink).
    MockAccelerator fast_mock; fast_mock.latency = latency; fast_mock.compute = false; auto fast_session = make_session(fast_mock);
    v7::EncryptPipeline fast(*fast_session, {2, 2, false});
    uint64_t fast_next = 0, fast_sunk = 0;
    auto tf = Clock::now();
    auto fast_stats = fast.run(
      [&]() -> std::optional<v7::PipelineJob> { if (fast_next == jobs) return std::nullopt; v7::PipelineJob j{fast_next, slots_for(fast_next)}; fast_next++; return j; },
      [&](v7::EncryptedRecord&& r) { require(r.id == fast_sunk++, "in-order delivery (throughput run)"); });
    const double fast_s = std::chrono::duration<double>(Clock::now() - tf).count();
    require(fast_stats.delivered == jobs && fast_sunk == jobs, "throughput run delivered all records");
    require(fast_stats.max_reorder <= fast.inflight_limit(), "reorder map bounded by the admission window");
    // Pipelined run with full exactness and decryption checks (slower: the sink re-encrypts and decrypts).
    MockAccelerator pipe_mock; pipe_mock.latency = latency; auto pipe_session = make_session(pipe_mock);
    v7::EncryptPipeline pipeline(*pipe_session, {2, 2, true});
    uint64_t next_job = 0, delivered = 0, exact = 0; double worst = 0;
    auto t1 = Clock::now();
    auto stats = pipeline.run(
      [&]() -> std::optional<v7::PipelineJob> { if (next_job == jobs) return std::nullopt; v7::PipelineJob j{next_job, slots_for(next_job)}; next_job++; return j; },
      [&](v7::EncryptedRecord&& r) {
        require(r.id == delivered, "in-order delivery");
        const auto& in = *r.inputs;
        auto expected = cpu.encrypt_residues(key, in.message, in.mask, in.e0, in.e1);
        for (size_t f = 0; f < 4; f++) {
          require(expected[f].c0 == r.cipher[f].c0 && expected[f].c1 == r.cipher[f].c1, "pipelined ciphertext exact vs CPU");
          exact += 2 * 3 * degree;
          Cipher c{r.cipher[f].c0, r.cipher[f].c1};
          auto plain = decode(decrypt_secure(cpu, c, material.secret));
          auto x = slots_for(r.id);
          for (size_t i = 0; i < x[f].size(); i++) { worst = std::max(worst, std::abs(plain[i] - x[f][i])); }
        }
        delivered++;
      });
    const double checked_s = std::chrono::duration<double>(Clock::now() - t1).count();
    require(stats.delivered == jobs && stats.encrypted == jobs && stats.prepared == jobs && delivered == jobs, "all records once");
    require(stats.max_reorder <= pipeline.inflight_limit(), "reorder map bounded (checked run)");
    require(worst <= 5e-4, "slot error gate");
    // Fast path (lock-free sampler + direct pack/fuse): same throughput conditions as the accepted pipeline run,
    // then a checked run against the accepted CPU path encrypt_secure on the same signed material.
    MockAccelerator ffast_mock; ffast_mock.latency = latency; ffast_mock.compute = false; auto ffast_session = make_session(ffast_mock);
    v7::EncryptPipeline ffast(*ffast_session, {2, 2, false, true});
    uint64_t ff_next = 0, ff_sunk = 0;
    auto tff = Clock::now();
    auto ff_stats = ffast.run(
      [&]() -> std::optional<v7::PipelineJob> { if (ff_next == jobs) return std::nullopt; v7::PipelineJob j{ff_next, slots_for(ff_next)}; ff_next++; return j; },
      [&](v7::EncryptedRecord&& r) { require(r.id == ff_sunk++, "in-order delivery (fast throughput run)"); });
    const double ffast_s = std::chrono::duration<double>(Clock::now() - tff).count();
    require(ff_stats.delivered == jobs && ff_sunk == jobs, "fast throughput run delivered all records");
    MockAccelerator fcheck_mock; fcheck_mock.latency = latency; auto fcheck_session = make_session(fcheck_mock);
    v7::EncryptPipeline fcheck(*fcheck_session, {3, 2, true, true});
    uint64_t fc_next = 0, fc_delivered = 0, fc_exact = 0; double fc_worst = 0;
    auto fc_stats = fcheck.run(
      [&]() -> std::optional<v7::PipelineJob> { if (fc_next == jobs) return std::nullopt; v7::PipelineJob j{fc_next, slots_for(fc_next)}; fc_next++; return j; },
      [&](v7::EncryptedRecord&& r) {
        require(r.id == fc_delivered, "in-order delivery (fast checked run)");
        const auto& in = *r.inputs;
        require(in.material != nullptr, "fast path keeps signed material for the test");
        auto expected = encrypt_secure(cpu, key, in.message_int, *in.material);   // accepted CPU path
        auto x = slots_for(r.id);
        for (size_t f = 0; f < 4; f++) {
          require(expected[f].c0 == r.cipher[f].c0 && expected[f].c1 == r.cipher[f].c1, "fast pipelined ciphertext exact vs accepted CPU");
          fc_exact += 2 * 3 * degree;
          auto plain = decode(decrypt_secure(cpu, Cipher{r.cipher[f].c0, r.cipher[f].c1}, material.secret));
          for (size_t i = 0; i < x[f].size(); i++) fc_worst = std::max(fc_worst, std::abs(plain[i] - x[f][i]));
        }
        fc_delivered++;
      });
    require(fc_stats.delivered == jobs && fc_stats.prepared == jobs && fc_delivered == jobs, "fast path all records once");
    require(fc_worst <= 5e-4, "fast path slot error gate");
    // A sink (application/network) exception stops every stage once; the same object is reusable afterwards
    // because the device itself did not fail (the stale error of the failed run must not leak).
    uint64_t sink_fed = 0, sink_seen = 0; bool sink_stopped = false;
    try {
      fast.run([&]() -> std::optional<v7::PipelineJob> { if (sink_fed == jobs) return std::nullopt; v7::PipelineJob j{sink_fed, slots_for(sink_fed)}; sink_fed++; return j; },
               [&](v7::EncryptedRecord&&) { if (++sink_seen == 5) throw std::runtime_error("sink failure"); });
    } catch (const std::runtime_error& e) { sink_stopped = std::string(e.what()) == "sink failure"; }
    require(sink_stopped && sink_seen == 5 && sink_fed <= 5 + fast.inflight_limit(), "sink exception stops the feeder within the window");
    uint64_t again_next = 0, again_sunk = 0;
    fast.run([&]() -> std::optional<v7::PipelineJob> { if (again_next == 3) return std::nullopt; v7::PipelineJob j{again_next, slots_for(again_next)}; again_next++; return j; },
             [&](v7::EncryptedRecord&&) { again_sunk++; });
    require(again_sunk == 3, "pipeline object reusable after a sink exception");
    // A source that skips an id is a caller bug: rejected, nothing hangs.
    uint64_t gap_next = 0; bool gap_rejected = false;
    try {
      fast.run([&]() -> std::optional<v7::PipelineJob> { if (gap_next > 3) return std::nullopt; v7::PipelineJob j{gap_next == 2 ? 7 : gap_next, slots_for(gap_next)}; gap_next++; return j; },
               [&](v7::EncryptedRecord&&) {});
    } catch (const std::invalid_argument&) { gap_rejected = true; }
    require(gap_rejected, "non-consecutive job id rejected");
    // Failure injection: the device fails on its 6th call; the pipeline must stop with one exception.
    MockAccelerator bad; bad.latency = latency; auto bad_session = make_session(bad); bad.fail_at = 6;
    v7::EncryptPipeline failing(*bad_session, {2, 2, false});
    uint64_t fed = 0, sunk = 0; bool stopped = false;
    try {
      failing.run([&]() -> std::optional<v7::PipelineJob> { if (fed == jobs) return std::nullopt; v7::PipelineJob j{fed, slots_for(fed)}; fed++; return j; },
                  [&](v7::EncryptedRecord&&) { sunk++; });
    } catch (const std::exception&) { stopped = true; }
    require(stopped && sunk < jobs && bad.calls == 6, "device failure stops the pipeline without retry");
    std::cout << "V7_PIPELINE_SELFTEST_PASS records=" << jobs << " exact_values=" << exact << " max_slot_error=" << worst
              << " mock_pl_latency_us=" << latency.count() << " mock_overruns=" << seq_mock.overruns + fast_mock.overruns
              << " sequential_records_per_s=" << jobs / seq_s << " pipelined_records_per_s=" << jobs / fast_s
              << " speedup=" << seq_s / fast_s << " checked_run_records_per_s=" << jobs / checked_s
              << " fastpath_exact_values=" << fc_exact << " fastpath_max_slot_error=" << fc_worst
              << " fastpath_pipelined_records_per_s=" << jobs / ffast_s
              << " max_reorder=" << fast_stats.max_reorder << "/" << fast.inflight_limit()
              << " failure_stop=1 sunk_before_stop=" << sunk << " sink_exception_stop=1 reuse_after_sink_exception=1"
              << " id_gap_rejected=1 x86_mock_not_board=1\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "V7_PIPELINE_SELFTEST_FAIL " << e.what() << '\n';
    return 1;
  }
}
