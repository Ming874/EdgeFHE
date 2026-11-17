// V8b gate 1 (2026-10-16): first functional + performance run of the V8b image (ENC in the PL) through driver
// candidate-4. Real device, or with EDGEFHE_DEVICE_MOCK the V8b specification model (offline test only).
// Fresh OS-entropy key material and seeds; nothing is persisted. Printed values are measurements of this run only.
//  A. Exactness, k = 1..8 (36 ciphertexts): every returned coefficient equals the bit-exact specification model
//     v8ref::KernelB (the csim/cosim reference) on the same seed and messages; for k >= 4 features 0..3 also equal the
//     board-accepted CPU encryptor (cpu_encrypt_secure_fast) on sampler-B material from the same seed; every ciphertext
//     decrypts (secret key on the PS, test only) to its input slots within 5e-4.
//  B. Fail-closed: a message coefficient 2^62 is rejected by the driver before any DMA (-EINVAL, no device call, lease
//     kept); the next ENC succeeds on the same lease.
//  C. Timed rounds, common sink (read every word once, then destroy, inside the timer), alternating mode order:
//       pl_device     ENC k=8 frames on pre-encoded messages (device + driver + PS seed only)
//       pl_e2e_seq    encode (3 workers) + seed + ENC k=8 + sink, sequential
//       pl_e2e_pipe   encoder thread (3 workers) feeds a 2-slot queue; the device-owner thread runs ENC + sink
//       cpu3_b        encode + sampler B + CPU encrypt (Engine 3 workers) + sink, 4 features per job (R2-style)
//     Rates are ciphertexts per second; statistics are computed off-board from the printed rounds.
//  D. Driver STATS of the last ENC k=8 call (per-phase ns).
#include "fast_encrypt.hpp"
#include "v8/v8b_device.hpp"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <iostream>
#include <mutex>
#include <optional>
#include <thread>
using namespace edgefhe;
namespace v8 = edgefhe_v8;
using Clock = std::chrono::steady_clock;

#ifdef EDGEFHE_DEVICE_MOCK
constexpr unsigned rounds = 2, frames_per_mode = 2;
#else
constexpr unsigned rounds = 10, frames_per_mode = 8;   // 64 ciphertexts per mode and round
#endif

static std::vector<std::complex<double>> slots_for(uint64_t id, size_t f) {
  std::vector<std::complex<double>> x(slots);
  for (size_t i = 0; i < slots; i++) x[i] = {(int((i * 3 + f * 19 + id * 7) % 101) - 50) / 25.0, (int((i * 5 + f * 11 + id) % 61) - 30) / 30.0};
  return x;
}
static std::vector<Message> encode_k(const Engine& e, uint64_t id, size_t k) {
  std::vector<Message> m;
  for (size_t g = 0; g < k; g += 4) {
    std::array<std::vector<std::complex<double>>, 4> x;
    for (size_t f = 0; f < 4; f++) x[f] = slots_for(id, g + f);
    auto four = e.encode_four(x);
    for (size_t f = 0; f < 4 && g + f < k; f++) m.push_back(std::move(four[f]));
  }
  return m;
}
static std::vector<const Message*> ptrs(const std::vector<Message>& m) { std::vector<const Message*> p; for (auto& x : m) p.push_back(&x); return p; }
static double seconds(Clock::time_point a) { return std::chrono::duration<double>(Clock::now() - a).count(); }
template<class C> static uint64_t consume(C&& c) {
  uint64_t sum = 0;
  for (const auto& x : c) for (const auto* r : {&x.c0, &x.c1}) for (const auto& l : *r) for (auto w : l) sum += w;
  { auto dead = std::move(c); }
  return sum;
}
template<class C> static void wipe_messages(C& m) { for (auto& x : m) explicit_bzero(x.data(), x.size() * sizeof(int64_t)); }

int main(int argc, char** argv) {
  try {
    require(argc == 3, "usage: device_v8b_selftest <os_prng_plugin> <device>");
    initialize_secure_sampler(argv[1]);
    Engine cpu(3);
    auto material = sample_secure_material();
    auto key = keygen_secure(cpu, material);
#ifdef EDGEFHE_DEVICE_MOCK
    v8::SpecModel backend;
    v8::Device dev([&](edgefhe_v8_request& r) { return backend.run(r); });
    const char* kind = "offline_spec_model";
    const uint32_t pl_hz = 0;
#else
    v8::Node node(argv[2]);
    v8::Device dev([&](edgefhe_v8_request& r) { return node.run(r); });
    const char* kind = "candidate4_device";
    const uint32_t pl_hz = node.pl_hz;
#endif
    const uint64_t generation = dev.load(key);
    require(dev.calls() == 1, "LOAD accepted");
    // A. Exactness against the specification model and the CPU encryptor; decryption.
    v8ref::KernelB model;
    {
      const auto ack = model.run(v8ref::load_frame(key.b, key.a, generation));
      require(ack.size() == 2 && ack[1].w[1] == 0, "specification model LOAD");
    }
    uint64_t exact = 0, cpu_exact = 0; double max_error = 0;
    for (uint32_t k = 1; k <= v8::max_k; k++) {
      auto msgs = encode_k(cpu, 100 + k, k);
      uint8_t seed[32]; sampler_b::os_seed(seed);
      std::vector<Cipher> hw;
      dev.enc(seed, ptrs(msgs), hw);
      require(hw.size() == k, "ENC returned k ciphertexts");
      const auto spec = model.run(v8ref::enc_frame(seed, msgs, generation));
      require(spec.size() == 2 + size_t(k) * 6144 && spec.back().w[1] == 0, "specification model ENC");
      const auto& out = dev.last_output();
      for (size_t i = 0; i < size_t(k) * 6144; i++) for (int w = 0; w < 4; w++) require(out[4 * i + w] == spec[1 + i].w[w], "V8b ENC == specification model");
      exact += size_t(k) * 6144 * 4;
      if (k >= 4) {
        SecureMaterial sm; sampler_b::sample_from_seed(sm, seed);
        std::array<Message, 4> four; for (size_t f = 0; f < 4; f++) four[f] = msgs[f];
        const auto c = edgefhe_v7::cpu_encrypt_secure_fast(cpu, key, four, sm);
        for (size_t f = 0; f < 4; f++) require(c[f].c0 == hw[f].c0 && c[f].c1 == hw[f].c1, "V8b ENC == CPU encryptor (sampler B, same seed)");
        cpu_exact += 4 * 2 * 3 * degree;
        wipe_messages(four);
      }
      sampler_b::wipe_bytes(seed, sizeof seed);
      for (size_t f = 0; f < k; f++) {
        const auto x = slots_for(100 + k, f);
        const auto y = decode(decrypt_secure(cpu, hw[f], material.secret), scale);
        for (size_t i = 0; i < slots; i++) {
          const double err = std::abs(y[i] - x[i]); max_error = std::max(max_error, err);
          require(err <= 5e-4, "CKKS slot error of a V8b ciphertext");
        }
      }
      wipe_messages(msgs);
    }
    const uint64_t calls_a = dev.calls();
    require(calls_a == 1 + v8::max_k, "exactness calls");
    // B. Fail-closed before DMA.
    {
      auto msgs = encode_k(cpu, 7, 1);
      msgs[0][123] = int64_t(1) << 62;
      uint8_t seed[32]; sampler_b::os_seed(seed);
      std::vector<Cipher> c;
      const int rc = dev.try_enc(seed, ptrs(msgs), c);
      require(rc == -EINVAL && dev.calls() == calls_a, "|m| >= 2^62 rejected before any device call");
      msgs[0][123] = 0;
      dev.enc(seed, ptrs(msgs), c);
      require(dev.calls() == calls_a + 1 && dev.generation() == generation, "lease kept after a pre-DMA rejection");
      sampler_b::wipe_bytes(seed, sizeof seed);
    }
    // C. Timed rounds.
    const size_t k = v8::max_k;
    std::vector<std::vector<Message>> pre;
    for (unsigned j = 0; j < frames_per_mode; j++) pre.push_back(encode_k(cpu, j, k));
    Engine enc_engine(3);
    SecureMaterial scratch;
    for (int i = 0; i < 1; i++) (void)edgefhe_v7::cpu_encrypt_secure_fast(cpu, key, cpu.encode_four({slots_for(0, 0), slots_for(0, 1), slots_for(0, 2), slots_for(0, 3)}), material);
    const char* modes[4] = {"pl_device", "pl_e2e_seq", "pl_e2e_pipe", "cpu3_b"};
    const uint64_t cts = uint64_t(frames_per_mode) * k;
    uint64_t digest = 0;
    for (unsigned r = 0; r < rounds; r++) {
      double rate[4]; std::string order;
      for (unsigned i = 0; i < 4; i++) {
        const unsigned mode = (i + r) % 4; order += (i ? "," : "") + std::string(modes[mode]);
        uint64_t acc = 0; std::vector<Cipher> c;
        const auto t0 = Clock::now();
        if (mode == 0) {
          for (unsigned j = 0; j < frames_per_mode; j++) { uint8_t s[32]; sampler_b::os_seed(s); dev.enc(s, ptrs(pre[j]), c); sampler_b::wipe_bytes(s, 32); acc += consume(std::move(c)); }
        } else if (mode == 1) {
          for (unsigned j = 0; j < frames_per_mode; j++) {
            auto m = encode_k(enc_engine, 1000 + j, k); uint8_t s[32]; sampler_b::os_seed(s);
            dev.enc(s, ptrs(m), c); sampler_b::wipe_bytes(s, 32); wipe_messages(m); acc += consume(std::move(c));
          }
        } else if (mode == 2) {
          std::mutex mu; std::condition_variable cv; std::deque<std::vector<Message>> q; bool done = false; std::exception_ptr err;
          std::thread producer([&] {
            try {
              for (unsigned j = 0; j < frames_per_mode; j++) {
                auto m = encode_k(enc_engine, 1000 + j, k);
                std::unique_lock<std::mutex> g(mu); cv.wait(g, [&] { return q.size() < 2; }); q.push_back(std::move(m)); cv.notify_all();
              }
            } catch (...) { err = std::current_exception(); }
            std::lock_guard<std::mutex> g(mu); done = true; cv.notify_all();
          });
          try {
            for (;;) {
              std::vector<Message> m;
              { std::unique_lock<std::mutex> g(mu); cv.wait(g, [&] { return done || !q.empty(); }); if (q.empty()) break; m = std::move(q.front()); q.pop_front(); cv.notify_all(); }
              uint8_t s[32]; sampler_b::os_seed(s); dev.enc(s, ptrs(m), c); sampler_b::wipe_bytes(s, 32); wipe_messages(m); acc += consume(std::move(c));
            }
          } catch (...) { { std::lock_guard<std::mutex> g(mu); done = true; q.clear(); cv.notify_all(); } producer.join(); throw; }
          producer.join();
          if (err) std::rethrow_exception(err);
        } else {
          for (unsigned j = 0; j < cts / 4; j++) {
            std::array<std::vector<std::complex<double>>, 4> x;
            for (size_t f = 0; f < 4; f++) x[f] = slots_for(2000 + j, f);
            auto m = cpu.encode_four(x);
            sample_secure_encryption_b(scratch);
            acc += consume(edgefhe_v7::cpu_encrypt_secure_fast(cpu, key, m, scratch));
          }
        }
        rate[mode] = cts / seconds(t0); digest ^= acc + mode;
      }
      std::cout << "V8B_GATE1_ROUND round=" << r << " order=" << order;
      for (unsigned m = 0; m < 4; m++) std::cout << " " << modes[m] << "=" << rate[m];
      std::cout << " unit=ciphertexts_per_s ciphertexts=" << cts << " k=" << k << "\n";
    }
    const uint64_t expected = 1 + v8::max_k + 1 + uint64_t(rounds) * 3 * frames_per_mode;
    require(dev.calls() == expected && dev.generation() == generation, "device calls on one lease");
#ifndef EDGEFHE_DEVICE_MOCK
    edgefhe_v8_stats st{};
    require(node.stats(st) && st.command == 9 && st.k == k && st.abi_version == EDGEFHE_V8_ABI && st.own_violations == 0, "driver STATS of the last ENC");
    std::cout << "V8B_GATE1_STATS k=" << st.k << " tx_bytes=" << st.tx_bytes << " rx_bytes=" << st.rx_bytes << " ns_copy_in=" << st.ns_copy_in
              << " ns_check_in=" << st.ns_check_in << " ns_sync_device=" << st.ns_sync_device << " ns_dma_hls=" << st.ns_dma_hls
              << " ns_tx_wait=" << st.ns_tx_wait << " ns_ap_poll=" << st.ns_ap_poll << " ns_identity=" << st.ns_identity
              << " ns_sync_cpu=" << st.ns_sync_cpu << " ns_check_out=" << st.ns_check_out << " ns_copy_out=" << st.ns_copy_out
              << " ns_wipe=" << st.ns_wipe << " ns_rx_wake=" << st.ns_rx_wake << " ns_tx_callback=" << st.ns_tx_callback
              << " ap_polls=" << st.ap_polls << " calls=" << st.calls << "\n";
#endif
    std::cout << "V8B_GATE1_SELFTEST_PASS transport=" << kind << " pl_hz=" << pl_hz << " exact_values=" << exact << " cpu_exact_values=" << cpu_exact
              << " max_slot_error=" << max_error << " fail_closed_checked=1 rounds=" << rounds << " ciphertexts_per_mode=" << cts
              << " sink_digest_nonzero=" << (digest != 0) << " generation=" << generation << " calls=" << dev.calls() << "\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "V8B_GATE1_SELFTEST_FAIL " << e.what() << '\n';
    return 1;
  }
}
