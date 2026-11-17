// EdgeFHE V8b PS-side device access (2026-10-16): ABI 8 frames for driver candidate-4 (/dev/edgefhe_v8b_dma).
// The PS encodes and draws a 32-byte seed per frame (OS entropy); the PL samples u/e0/e1 with sampler B, computes
// c0 = b*u + e0 + m and c1 = a*u + e1 for k <= 8 features and returns canonical residues. The driver writes the frame
// header (lease generation) itself and returns only the payload, after its header-echo, trailer and canonical checks.
// Runner: the real ioctl, or (offline tests) the bit-exact V8b specification model v8ref::KernelB behind the same
// driver-side checks (edgefhe_v8_policy.h), so frame packing is verified against the specification.
// Single DMA owner: every call must come from the thread that created the Device.
#pragma once
#include "../../driver_v8/edgefhe_v8_uapi.h"
#include "../../driver_v8/edgefhe_v8_policy.h"
#include "../baseline/core.hpp"
#include "../baseline/sampler_b.hpp"
#include "v8b_ref.hpp"
#include <array>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <functional>
#include <memory>
#include <openssl/sha.h>
#include <stdexcept>
#include <string>
#include <sys/ioctl.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace edgefhe_v8 {
using edgefhe::Cipher;
using edgefhe::Message;
using edgefhe::PublicKey;
using edgefhe::degree;
// SHA-256 of the V8b profile string (v8b_system_20261016/profile/EDGEFHE_V8B_PROFILE.json).
constexpr std::array<uint8_t, 32> profile = {0xe5, 0xee, 0x65, 0x3a, 0xc4, 0x40, 0x7f, 0x3c, 0x9d, 0x76, 0x89, 0x80, 0x18, 0x6f, 0xbb, 0x85,
                                             0xae, 0x91, 0xa4, 0x97, 0xd5, 0x8f, 0x91, 0xd8, 0x25, 0x32, 0xa9, 0x35, 0x39, 0xf7, 0x39, 0xc9};
constexpr uint32_t max_k = EDGEFHE_V8_MAX_K;

struct DeviceError : std::runtime_error {
  int code;
  DeviceError(const std::string& what, int c) : std::runtime_error(what + " (" + std::to_string(c) + ")"), code(c) {}
};

// LOAD payload: b then a, limb-major, beat j of a limb = coefficients 4j..4j+3.
inline std::vector<uint32_t> load_payload(const PublicKey& key) {
  std::vector<uint32_t> p; p.reserve(2 * 3 * degree);
  for (const auto* r : {&key.b, &key.a}) for (size_t l = 0; l < 3; l++) {
    edgefhe::require((*r)[l].size() == degree, "LOAD key degree");
    p.insert(p.end(), (*r)[l].begin(), (*r)[l].end());
  }
  return p;
}
// ENC payload: 32-byte seed, then k messages as little-endian int64 (two coefficients per beat).
inline void enc_payload(const uint8_t seed[32], const std::vector<const Message*>& m, std::vector<uint8_t>& out) {
  out.resize(32 + m.size() * degree * 8);
  std::memcpy(out.data(), seed, 32);
  for (size_t f = 0; f < m.size(); f++) {
    edgefhe::require(m[f]->size() == degree, "ENC message degree");
    std::memcpy(out.data() + 32 + f * degree * 8, m[f]->data(), degree * 8);   // aarch64 / x86-64: little-endian
  }
}
// Output payload -> ciphertexts: feature f, poly p (c0, c1), beat j, limb l = coefficients 4j..4j+3.
inline void unpack(const uint32_t* out, size_t k, std::vector<Cipher>& c) {
  c.resize(k);
  for (size_t f = 0; f < k; f++) for (int p = 0; p < 2; p++) {
    auto& r = p == 0 ? c[f].c0 : c[f].c1;
    for (size_t l = 0; l < 3; l++) r[l].resize(degree);
    const uint32_t* src = out + size_t(f * 2 + p) * 1024 * 3 * 4;
    for (size_t j = 0; j < 1024; j++) for (size_t l = 0; l < 3; l++) std::memcpy(&r[l][4 * j], src + (3 * j + l) * 4, 16);
  }
}

using Runner = std::function<int(edgefhe_v8_request&)>;   // returns 0 or -errno; fills generation/status fields

class Device {
  Runner run_;
  std::thread::id owner_ = std::this_thread::get_id();
  uint64_t generation_ = 0, calls_ = 0;
  std::array<uint8_t, 32> key_digest_{};
  std::vector<uint8_t> in_;
  std::vector<uint32_t> out_;
  void check_owner() const { if (std::this_thread::get_id() != owner_) throw DeviceError("V8b device used from a non-owner thread", -EPERM); }
  int call(uint32_t command, uint32_t k, const void* in, size_t in_bytes, void* out, size_t out_bytes, uint32_t* hw) {
    check_owner();
    edgefhe_v8_request r{};
    r.abi_version = EDGEFHE_V8_ABI; r.command = command; r.k = k;
    r.input_ptr = in_bytes ? uint64_t(uintptr_t(in)) : 0; r.output_ptr = out_bytes ? uint64_t(uintptr_t(out)) : 0;
    r.input_bytes = uint32_t(in_bytes); r.output_bytes = uint32_t(out_bytes);
    r.generation = command == 6 ? 0 : generation_; r.timeout_ms = 2000;
    std::memcpy(r.profile_sha256, profile.data(), 32);
    std::memcpy(r.public_key_sha256, key_digest_.data(), 32);
    const int rc = run_(r);
    if (hw) *hw = r.hardware_status;
    if (rc == 0) { calls_++; if (command == 6) generation_ = r.generation; }
    return rc;
  }
 public:
  explicit Device(Runner run) : run_(std::move(run)) {}
  uint64_t generation() const { return generation_; }
  uint64_t calls() const { return calls_; }
  uint64_t load(const PublicKey& key) {
    const auto p = load_payload(key);
    SHA256(reinterpret_cast<const unsigned char*>(p.data()), p.size() * 4, key_digest_.data());
    uint32_t hw = ~0u;
    const int rc = call(6, 0, p.data(), p.size() * 4, nullptr, 0, &hw);
    if (rc) throw DeviceError("V8b LOAD failed hw=" + std::to_string(hw), rc);
    edgefhe::require(generation_ != 0, "LOAD returned a lease generation");
    return generation_;
  }
  // ENC of k = m.size() features with one seed. Returns 0 or -errno (no exception), output in `c` only on 0.
  int try_enc(const uint8_t seed[32], const std::vector<const Message*>& m, std::vector<Cipher>& c, uint32_t* hw = nullptr) {
    edgefhe::require(!m.empty() && m.size() <= max_k, "ENC k in 1..8");
    enc_payload(seed, m, in_);
    out_.assign(m.size() * 6144 * 4, 0);
    const int rc = call(9, uint32_t(m.size()), in_.data(), in_.size(), out_.data(), out_.size() * 4, hw);
    edgefhe::sampler_b::wipe_bytes(in_.data(), in_.size());     // seed and messages leave the PS buffer
    if (rc == 0) unpack(out_.data(), m.size(), c);
    return rc;
  }
  void enc(const uint8_t seed[32], const std::vector<const Message*>& m, std::vector<Cipher>& c) {
    uint32_t hw = ~0u;
    const int rc = try_enc(seed, m, c, &hw);
    if (rc) throw DeviceError("V8b ENC failed hw=" + std::to_string(hw), rc);
  }
  const std::vector<uint32_t>& last_output() const { return out_; }
};

// Real device: ioctl on the candidate-4 node, with CAPS checks at open.
class Node {
  int fd_ = -1;
 public:
  explicit Node(const char* path) {
    fd_ = ::open(path, O_RDWR | O_CLOEXEC);
    if (fd_ < 0) throw DeviceError(std::string("open ") + path, -errno);
    edgefhe_v8_caps c{};
    if (ioctl(fd_, EDGEFHE_IOC_V8_CAPS, &c)) { const int e = -errno; ::close(fd_); throw DeviceError("V8b CAPS", e); }
    if (c.abi_version != EDGEFHE_V8_ABI || c.command_mask != 0x3c0 || c.n != degree || c.limbs != 3 || c.lanes_per_limb != 4 ||
        c.max_k != max_k || c.max_tx_bytes != EDGEFHE_V8_MAX_TX_BYTES || c.max_rx_bytes != EDGEFHE_V8_MAX_RX_BYTES || c.poisoned ||
        !(c.flags & EDGEFHE_V8_CAP_HW_IDENTITY_VERIFIED) || (c.flags & EDGEFHE_V8_CAP_RECOVERY_PROVEN) ||
        c.pl_hz < 199800000u || c.pl_hz > 200000000u || std::memcmp(c.profile_sha256, profile.data(), 32)) {
      ::close(fd_); throw DeviceError("V8b CAPS mismatch", -EPROTO);
    }
    pl_hz = c.pl_hz;
  }
  ~Node() { if (fd_ >= 0) ::close(fd_); }
  Node(const Node&) = delete;
  Node& operator=(const Node&) = delete;
  uint32_t pl_hz = 0;
  int run(edgefhe_v8_request& r) { return ioctl(fd_, EDGEFHE_IOC_V8_RUN, &r) ? -errno : 0; }
  bool stats(edgefhe_v8_stats& s) { return ioctl(fd_, EDGEFHE_IOC_V8_STATS, &s) == 0; }
};

// Offline stand-in: driver-side checks of candidate-4 + the KernelB specification model of the V8b hardware.
class SpecModel {
  v8ref::KernelB model_;
  ef_policy policy_{};
 public:
  uint64_t frames = 0;
  int run(edgefhe_v8_request& r) {
    ef_shape s{};
    if (r.abi_version != EDGEFHE_V8_ABI || ef_shape_for(r.command, r.k, &s) || r.input_bytes != s.in_beats * 16 ||
        r.output_bytes != s.out_beats * 16 || std::memcmp(r.profile_sha256, profile.data(), 32)) return -EINVAL;
    const auto* in = reinterpret_cast<const uint32_t*>(uintptr_t(r.input_ptr));
    if (s.in_beats && ef_check_input(r.command, r.k, in)) return -EINVAL;
    if (int rc = ef_begin(&policy_, r.command, 1, r.generation, r.profile_sha256, r.public_key_sha256)) return rc;
    if (r.command == 6) r.generation = policy_.generation;
    std::vector<v8ref::Beat> frame{v8ref::header(r.command, r.k, r.generation)};
    for (uint32_t i = 0; i < s.in_beats; i++) frame.push_back({{in[4 * i], in[4 * i + 1], in[4 * i + 2], in[4 * i + 3]}});
    const auto out = model_.run(frame);
    frames++;
    std::vector<uint32_t> rx; rx.reserve(out.size() * 4);
    for (const auto& b : out) rx.insert(rx.end(), b.w, b.w + 4);
    const uint32_t hdr[4] = {EDGEFHE_V8_MAGIC, r.command | (r.k << 8), uint32_t(r.generation), uint32_t(r.generation >> 32)};
    if (rx.size() != (2 + s.out_beats) * 4) { ef_fatal(&policy_); return -EIO; }
    const int st = ef_check_output(rx.data(), hdr, s.out_beats, r.k);
    if (st < 0) { ef_fatal(&policy_); return -EIO; }
    r.hardware_status = uint32_t(st);
    if (int rc = ef_finish(&policy_, EF_FULL_COMPLETION, r.hardware_status)) return rc;
    if (s.out_beats) std::memcpy(reinterpret_cast<void*>(uintptr_t(r.output_ptr)), rx.data() + 4, s.out_beats * 16);
    return 0;
  }
};
}  // namespace edgefhe_v8
