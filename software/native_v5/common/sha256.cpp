#include "common/sha256.hpp"
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <stdexcept>

namespace edgefhe::v5 {
namespace {
constexpr std::array<uint32_t, 64> k = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
inline uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }
}  // namespace

Sha256::Sha256()
    : state_{0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19} {}

void Sha256::block(const uint8_t* data) {
  std::array<uint32_t, 64> w;
  for (int i = 0; i < 16; i++)
    w[i] = uint32_t(data[4 * i]) << 24 | uint32_t(data[4 * i + 1]) << 16 | uint32_t(data[4 * i + 2]) << 8 |
           uint32_t(data[4 * i + 3]);
  for (int i = 16; i < 64; i++) {
    const uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
    const uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
    w[i] = w[i - 16] + s0 + w[i - 7] + s1;
  }
  uint32_t a = state_[0], b = state_[1], c = state_[2], d = state_[3];
  uint32_t e = state_[4], f = state_[5], g = state_[6], h = state_[7];
  for (int i = 0; i < 64; i++) {
    const uint32_t t1 = h + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + k[i] + w[i];
    const uint32_t t2 = (rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
    h = g;
    g = f;
    f = e;
    e = d + t1;
    d = c;
    c = b;
    b = a;
    a = t1 + t2;
  }
  state_[0] += a;
  state_[1] += b;
  state_[2] += c;
  state_[3] += d;
  state_[4] += e;
  state_[5] += f;
  state_[6] += g;
  state_[7] += h;
}

void Sha256::update(const void* data, size_t bytes) {
  if (finished_) throw std::logic_error("Sha256 already finished");
  const auto* p = static_cast<const uint8_t*>(data);
  total_ += bytes;
  while (bytes) {
    const size_t take = std::min(bytes, buffer_.size() - buffered_);
    std::memcpy(buffer_.data() + buffered_, p, take);
    buffered_ += take;
    p += take;
    bytes -= take;
    if (buffered_ == buffer_.size()) {
      block(buffer_.data());
      buffered_ = 0;
    }
  }
}

void Sha256::update_u32(uint32_t value) {
  const uint8_t bytes[4] = {uint8_t(value), uint8_t(value >> 8), uint8_t(value >> 16), uint8_t(value >> 24)};
  update(bytes, 4);
}

void Sha256::update_u64(uint64_t value) {
  update_u32(uint32_t(value));
  update_u32(uint32_t(value >> 32));
}

std::string Sha256::hex() {
  if (finished_) throw std::logic_error("Sha256 already finished");
  const uint64_t bits = total_ * 8;
  const uint8_t one = 0x80, zero = 0;
  update(&one, 1);
  while (buffered_ != 56) update(&zero, 1);
  uint8_t length[8];
  for (int i = 0; i < 8; i++) length[i] = uint8_t(bits >> (56 - 8 * i));
  update(length, 8);
  finished_ = true;
  std::string out;
  char digit[3];
  for (const uint32_t word : state_)
    for (int shift = 24; shift >= 0; shift -= 8) {
      std::snprintf(digit, sizeof(digit), "%02x", unsigned((word >> shift) & 0xff));
      out += digit;
    }
  return out;
}

std::string sha256_hex(const std::string& bytes) {
  Sha256 hash;
  hash.update(bytes.data(), bytes.size());
  return hash.hex();
}

std::string sha256_file(const std::string& path) {
  std::ifstream file(path, std::ios::binary);
  if (!file) throw std::invalid_argument("Cannot read " + path);
  Sha256 hash;
  char buffer[65536];
  while (file.read(buffer, sizeof(buffer)) || file.gcount() > 0) hash.update(buffer, size_t(file.gcount()));
  if (file.bad()) throw std::invalid_argument("Read error on " + path);
  return hash.hex();
}
}  // namespace edgefhe::v5
