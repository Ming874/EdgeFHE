// app v5 (2026-10-22): owner-side request engine shared by owner.cpp (OS seeds) and v5_equivalence_selftest (fixed seeds,
// specification model). Selected from the ARM diagnostic OPT_DIAG_V1 (1021new1); every routine is bit-identical to app_v4
// for the same seed, messages and key:
//  * decrypt: buffers allocated once; c1*s per limb on the math pool, then Garner CRT + decode pre-twist split over the
//    3 lanes by coefficient range, serial 2048-point FFT (ARM 2.095 -> 1.528 ms).
//  * CPU backend: sampler B streams of one group spread over the 3 lanes (ARM 6.13 -> 2.32 ms per 4 features), then the
//    same 3-limb encrypt. Padding features (f >= F) are not encrypted: their ciphertext words are zero (their weight is 0).
//  * PL backend: frames of up to 4 real features (tail frame k = F mod 4, new OS seed per frame, frame-local f as in v4);
//    a persistent helper thread encodes frame i+1 and transposes frame i-1 while the owner thread runs ENC i into one of
//    two caller-owned buffers. Device calls per request stay ceil(F/4).
//  * Encode buffers, sampler material and device buffers live in the context and are wiped after each use.
#pragma once
#include "edgefhe_fast.hpp"
#include "owner_fast_core.hpp"
#include "baseline/sampler_b.hpp"
#include "baseline/worker_pool.hpp"
#include "v8b_device_v5.hpp"
#include <array>
#include <complex>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <exception>
#include <functional>
#include <map>
#include <mutex>
#include <thread>
#include <vector>

namespace edgefhe { namespace v5 {
namespace v8 = edgefhe_v8_v5;
constexpr size_t WORDS = 6 * degree;

// Persistent helper thread, FIFO. wait(t) rethrows the exception of job t; quiesce() waits for every submitted job.
class Helper {
  std::mutex mu_; std::condition_variable ready_, done_;
  std::deque<std::pair<uint64_t, std::function<void()>>> queue_;
  std::map<uint64_t, std::exception_ptr> errors_;
  uint64_t submitted_ = 0, finished_ = 0; bool stop_ = false; std::thread thread_;
  void loop() {
    for (;;) {
      std::pair<uint64_t, std::function<void()>> job;
      { std::unique_lock<std::mutex> lk(mu_); ready_.wait(lk, [&] { return stop_ || !queue_.empty(); });
        if (queue_.empty()) { return; }
        job = std::move(queue_.front()); queue_.pop_front(); }
      std::exception_ptr e; try { job.second(); } catch (...) { e = std::current_exception(); }
      { std::lock_guard<std::mutex> lk(mu_); if (e) errors_[job.first] = e; finished_ = job.first; }
      done_.notify_all();
    }
  }
 public:
  Helper() : thread_([this] { loop(); }) {}
  ~Helper() { quiesce(); { std::lock_guard<std::mutex> lk(mu_); stop_ = true; } ready_.notify_all(); thread_.join(); }
  Helper(const Helper&) = delete; Helper& operator=(const Helper&) = delete;
  uint64_t submit(std::function<void()> f) { std::lock_guard<std::mutex> lk(mu_); queue_.emplace_back(++submitted_, std::move(f)); ready_.notify_all(); return submitted_; }
  void wait(uint64_t ticket) {
    std::unique_lock<std::mutex> lk(mu_); done_.wait(lk, [&] { return finished_ >= ticket; });
    const auto it = errors_.find(ticket); if (it != errors_.end()) { auto e = it->second; errors_.erase(it); std::rethrow_exception(e); }
  }
  void quiesce() noexcept { std::unique_lock<std::mutex> lk(mu_); done_.wait(lk, [&] { return finished_ >= submitted_; }); errors_.clear(); }
};
struct Quiesce { Helper& h; ~Quiesce() { h.quiesce(); } };   // never unwind while a helper job may touch our buffers

inline uint32_t modpow(uint32_t a, uint32_t n, uint32_t q) { uint64_t r = 1, b = a % q; for (; n; n >>= 1) { if (n & 1) r = r * b % q; b = b * b % q; } return uint32_t(r); }
// Garner lift exactly as core.cpp centered() for one coefficient.
struct Garner {
  static constexpr uint64_t q0 = primes[0], q1 = primes[1], q2 = primes[2], q01 = q0 * q1;
  uint32_t inv01, inv012; Wide q, half;
  Garner() : inv01(modpow(uint32_t(q0 % q1), uint32_t(q1 - 2), uint32_t(q1))), inv012(modpow(uint32_t(q01 % q2), uint32_t(q2 - 2), uint32_t(q2))),
             q(Wide(q01) * q2), half(q / 2) {}
  Wide lift(uint32_t r0, uint32_t r1, uint32_t r2) const {
    const uint64_t d1 = (uint64_t(r1) + q1 - r0) % q1, t1 = (d1 * inv01) % q1, x01 = uint64_t(r0) + q0 * t1;
    const uint64_t d2 = (uint64_t(r2) + q2 - x01 % q2) % q2, t2 = (d2 * inv012) % q2;
    const Wide x = Wide(x01) + Wide(q01) * t2; return x > half ? x - q : x;
  }
};

struct Decryptor {
  Rns sum; std::vector<std::complex<double>> scratch; std::array<uint32_t, 3> bad{}; Garner garner;
  Decryptor() { for (auto& p : sum) { p.assign(degree, 0); } scratch.assign(fast::Fft::H, {}); }
  ~Decryptor() { wipe(); }
  void wipe() { for (auto& p : sum) { explicit_bzero(p.data(), p.size() * 4); } explicit_bzero(scratch.data(), scratch.size() * sizeof(scratch[0])); }
  // in: canonical-checked [poly][limb][N]; values: rows real scores (same checks as v4).
  void run(WorkerPool& pool, const fast::NttSet& set, const fast::SecretTransform& sk, const fast::Fft& fft, const uint32_t* in, size_t rows, double* values) {
    constexpr size_t H = fast::Fft::H;
    struct WipeOnExit { Decryptor& d; ~WipeOnExit() { d.wipe(); } } wipe_on_exit{*this};
    pool.run([&](size_t l) {
      const auto& nt = set.ntt[l]; const uint32_t q = primes[l]; uint32_t* o = sum[l].data();
      std::memcpy(o, in + 3 * degree + l * degree, degree * 4); nt.transform(o, false);
      for (size_t i = 0; i < degree; i++) o[i] = nt.product(o[i], sk.hat[l][i]);
      nt.transform(o, true);
      const uint32_t* c0 = in + l * degree; for (size_t i = 0; i < degree; i++) o[i] = fast::csub(o[i] + c0[i], q);
    });
    const double inv = 1.0 / double(scale * scale);
    pool.run([&](size_t lane) {
      uint32_t b = 0;
      for (size_t j = lane * H / 3; j < (lane + 1) * H / 3; j++) {
        for (size_t l = 0; l < 3; l++) b |= uint32_t(sum[l][j] >= primes[l]) | uint32_t(sum[l][j + H] >= primes[l]);
        const Wide lo = garner.lift(sum[0][j], sum[1][j], sum[2][j]), hi = garner.lift(sum[0][j + H], sum[1][j + H], sum[2][j + H]);
        scratch[j] = fast::Fft::mul({fast::wide_to_double(lo) * inv, fast::wide_to_double(hi) * inv}, fft.zeta_pow[j]);
      }
      bad[lane] = b;
    });
    require((bad[0] | bad[1] | bad[2]) == 0, "Noncanonical RNS residue");
    fft.dft(scratch.data(), +1);
    for (size_t k = 0; k < rows; k++) {
      const size_t m = k >> 1; const std::complex<double> d = (k & 1) ? std::conj(scratch[H - m - 1]) : scratch[m];
      require(std::isfinite(d.real()) && std::isfinite(d.imag()) && std::abs(d.imag()) < .05, "invalid real-score output");
      values[k] = d.real();
    }
  }
};

// Sampler B material of features 0..nf-1 of one seed (identical to sample_from_seed for those features), 3 lanes.
struct Sampler {
  std::array<std::vector<uint64_t>, 3> words;
  Sampler() { for (auto& w : words) w.assign(degree, 0); }
  ~Sampler() { for (auto& w : words) explicit_bzero(w.data(), w.size() * 8); }
  void run(WorkerPool& pool, SecureMaterial& m, const uint8_t seed[32], size_t nf) {
    require(nf >= 1 && nf <= 4, "sampler feature count");
    for (size_t f = 0; f < nf; f++) { m.e0[f].resize(degree); m.e1[f].resize(degree); m.mask[f].resize(degree); }
    uint32_t key[8]; sampler_b::key_words(seed, key);
    std::array<bool, 3> ok{true, true, true}; const size_t jobs = 3 * nf;   // [Gaussian 0..2nf-1][ternary 0..nf-1], job i -> lane i%3
    pool.run([&](size_t lane) {
      for (size_t j = lane; j < jobs; j += 3) {
        if (j < 2 * nf) {
          const size_t f = j / 2; const uint32_t nonce[3] = {uint32_t(2 + j % 2), uint32_t(f), 0};
          Message& dst = (j % 2) ? m.e1[f] : m.e0[f];
          ok[lane] = ok[lane] && chacha::keystream(key, 0, nonce, reinterpret_cast<uint32_t*>(words[lane].data()), sampler_b::gauss_blocks);
          sampler_b::cdt63(words[lane].data(), dst.data(), degree);
        } else {
          const size_t f = j - 2 * nf; const uint32_t nonce[3] = {1, uint32_t(f), 0};
          sampler_b::ternary(key, nonce, m.mask[f].data(), degree);
        }
      }
    });
    explicit_bzero(key, sizeof key);
    require(ok[0] && ok[1] && ok[2], "ChaCha20 counter range");
  }
};
inline void wipe_material(SecureMaterial& m) {
  for (size_t f = 0; f < 4; f++) {   // vectors of features never sampled are empty: data() may be null
    if (!m.mask[f].empty()) explicit_bzero(m.mask[f].data(), m.mask[f].size());
    if (!m.e0[f].empty()) explicit_bzero(m.e0[f].data(), m.e0[f].size() * 8);
    if (!m.e1[f].empty()) explicit_bzero(m.e1[f].data(), m.e1[f].size() * 8);
  }
}
inline void validate_material_nf(const std::array<Message, 4>& message, const SecureMaterial& m, size_t nf) {
  for (size_t f = 0; f < nf; f++) {
    require(m.mask[f].size() == degree, "Ternary degree");
    for (auto v : m.mask[f]) require(v >= -1 && v <= 1, "Uniform ternary coefficient");
    for (const auto* e : {&m.e0[f], &m.e1[f]}) { require(e->size() == degree, "Gaussian degree"); for (auto v : *e) require(v >= -55 && v <= 55, "Pinned inversion sampler support mismatch"); }
    require(message[f].size() == degree, "Integer polynomial degree mismatch");
  }
}
// v3::cpu_encrypt_limb for the first nf features, written straight into the application layout at out (feature-major).
template <size_t L> inline void cpu_encrypt_limb_nf(const fast::NttSet& set, const v3::KeySpectra& ks, const std::array<Message, 4>& message,
                                                    const SecureMaterial& m, size_t nf, uint32_t* out, uint32_t* scratch) {
  constexpr uint32_t q = primes[L];
  const auto& nt = set.ntt[L];
  uint32_t* u = scratch; uint32_t* bu = scratch + degree; uint32_t* au = scratch + 2 * degree;
  for (size_t f = 0; f < nf; f++) {
    for (size_t i = 0; i < degree; i++) u[i] = v3::small_residue<q>(m.mask[f][i]);
    nt.transform(u, false);
    for (size_t i = 0; i < degree; i++) { bu[i] = nt.product(ks.b_hat[L][i], u[i]); au[i] = nt.product(ks.a_hat[L][i], u[i]); }
    nt.transform(bu, true); nt.transform(au, true);
    uint32_t* c0 = out + f * WORDS + L * degree; uint32_t* c1 = out + f * WORDS + 3 * degree + L * degree;
    for (size_t i = 0; i < degree; i++) {
      c0[i] = fast::csub(fast::csub(bu[i] + v3::small_residue<q>(m.e0[f][i]), q) + v3::wide_residue<q>(message[f][i]), q);
      c1[i] = fast::csub(au[i] + v3::small_residue<q>(m.e1[f][i]), q);
    }
  }
  explicit_bzero(scratch, 3 * degree * sizeof(uint32_t));
}
// device payload of k features -> application layout (feature-major), 2k (feature, poly) blocks over the 3 lanes.
inline bool transpose_par(WorkerPool& pool, const uint32_t* dev, size_t k, uint32_t* app) {
  std::array<uint32_t, 3> bad{};
  pool.run([&](size_t lane) {
    uint32_t b = 0;
    for (size_t blk = lane; blk < 2 * k; blk += 3) {
      const size_t f = blk / 2, p = blk % 2; const uint32_t* src = dev + (f * 2 + p) * 1024 * 12; uint32_t* dst = app + f * WORDS + p * 3 * degree;
      for (size_t j = 0; j < 1024; j++) { const uint32_t* s = src + 12 * j;
        for (size_t l = 0; l < 3; l++) { const uint32_t q = primes[l]; uint32_t* d = dst + l * degree + 4 * j;
          for (size_t c = 0; c < 4; c++) { const uint32_t v = s[4 * l + c]; b |= uint32_t(v >= q); d[c] = v; } } }
    }
    bad[lane] = b;
  });
  return (bad[0] | bad[1] | bad[2]) == 0;
}

struct MessageSet {
  std::array<std::vector<std::complex<double>>, 4> x; std::array<Message, 4> m;
  MessageSet() { for (auto& v : x) { v.assign(slots, {}); } for (auto& v : m) { v.assign(degree, 0); } }
  ~MessageSet() { wipe(); }
  void wipe() { for (auto& v : x) { explicit_bzero(v.data(), v.size() * sizeof(v[0])); } for (auto& v : m) { explicit_bzero(v.data(), v.size() * 8); } }
};
using SeedSource = std::function<void(uint8_t*)>;

// Everything the v5 request engine needs besides the key material (owned by the caller's context).
struct Engine {
  const fast::NttSet& set; const fast::Fft& fft; const v3::KeySpectra& spectra; WorkerPool& math; WorkerPool& encode_pool;
  Helper helper; std::array<MessageSet, 2> sets; SecureMaterial material; Sampler sampler; Decryptor decryptor;
  std::array<std::vector<uint32_t>, 3> limb_scratch; std::array<std::vector<std::complex<double>>, 3> enc_scratch; std::array<std::vector<uint32_t>, 2> device_out;
  Engine(const fast::NttSet& s, const fast::Fft& f, const v3::KeySpectra& k, WorkerPool& m, WorkerPool& e) : set(s), fft(f), spectra(k), math(m), encode_pool(e) {
    for (auto& v : limb_scratch) v.assign(3 * degree, 0);
    for (auto& v : device_out) v.assign(4 * 6144 * 4, 0);
  }
  // encode features base..base+nf-1 of the row-major values into set (lanes as v4: lane l encodes f = l, l + 3)
  void encode(MessageSet& s, const double* values, size_t rows, size_t features, size_t base, size_t nf) {
    encode_pool.run([&](size_t lane) {
      for (size_t f = lane; f < nf; f += 3) {
        auto& x = s.x[f]; std::fill(x.begin(), x.end(), std::complex<double>{});
        for (size_t r = 0; r < rows; r++) x[r] = {values[r * features + base + f], 0};
        fast::encode_into(fft, x, scale, s.m[f].data(), enc_scratch[lane]);
      }
    });
  }
  void encrypt_cpu(const double* values, size_t rows, size_t features, uint32_t* out, const SeedSource& seed_source) {
    const size_t padded = (features + 3) / 4 * 4;
    std::memset(out + features * WORDS, 0, (padded - features) * WORDS * 4);
    Quiesce q{helper};
    size_t nf0 = std::min<size_t>(4, features);
    uint64_t ticket = helper.submit([&, nf0] { encode(sets[0], values, rows, features, 0, nf0); });
    for (size_t base = 0, i = 0; base < features; base += 4, i++) {
      const size_t nf = std::min<size_t>(4, features - base); MessageSet& cur = sets[i % 2];
      helper.wait(ticket);
      if (base + 4 < features) { const size_t nb = base + 4, nn = std::min<size_t>(4, features - nb); MessageSet& nxt = sets[(i + 1) % 2];
        ticket = helper.submit([&, nb, nn] { encode(nxt, values, rows, features, nb, nn); }); }
      uint8_t seed[32]; seed_source(seed);
      sampler.run(math, material, seed, nf); explicit_bzero(seed, sizeof seed);
      validate_material_nf(cur.m, material, nf);
      uint32_t* dst = out + base * WORDS;
      math.run([&](size_t l) {
        if (l == 0) cpu_encrypt_limb_nf<0>(set, spectra, cur.m, material, nf, dst, limb_scratch[0].data());
        else if (l == 1) cpu_encrypt_limb_nf<1>(set, spectra, cur.m, material, nf, dst, limb_scratch[1].data());
        else cpu_encrypt_limb_nf<2>(set, spectra, cur.m, material, nf, dst, limb_scratch[2].data());
      });
      wipe_material(material); cur.wipe();
    }
  }
  // ENC frames of up to 4 real features; frame i+1 encoded and frame i-1 transposed by the helper while ENC i runs.
  void encrypt_device(v8::Device& device, const double* values, size_t rows, size_t features, uint32_t* out, const SeedSource& seed_source) {
    const size_t padded = (features + 3) / 4 * 4, frames = (features + 3) / 4;
    std::memset(out + features * WORDS, 0, (padded - features) * WORDS * 4);
    Quiesce q{helper};
    std::vector<uint64_t> transposed(frames, 0);
    uint64_t encoded = helper.submit([&] { encode(sets[0], values, rows, features, 0, std::min<size_t>(4, features)); });
    for (size_t i = 0; i < frames; i++) {
      const size_t base = 4 * i, nf = std::min<size_t>(4, features - base); MessageSet& cur = sets[i % 2];
      helper.wait(encoded);
      if (i + 1 < frames) { const size_t nb = base + 4, nn = std::min<size_t>(4, features - nb); MessageSet& nxt = sets[(i + 1) % 2];
        encoded = helper.submit([&, nb, nn] { encode(nxt, values, rows, features, nb, nn); }); }
      if (i >= 2) helper.wait(transposed[i - 2]);                // device_out[i % 2] free again
      std::vector<const Message*> m; for (size_t f = 0; f < nf; f++) m.push_back(&cur.m[f]);
      uint8_t seed[32]; seed_source(seed); uint32_t hw = ~0u;
      const int rc = device.try_enc_into(seed, m, device_out[i % 2].data(), &hw); explicit_bzero(seed, sizeof seed); cur.wipe();
      if (rc) throw v8::DeviceError("V8b ENC failed hw=" + std::to_string(hw), rc);
      uint32_t* src = device_out[i % 2].data(); uint32_t* dst = out + base * WORDS;
      transposed[i] = helper.submit([this, src, nf, dst] { require(transpose_par(math, src, nf, dst), "noncanonical device output"); });
    }
    for (size_t i = 0; i < frames; i++) helper.wait(transposed[i]);
  }
};

}}  // namespace edgefhe::v5
