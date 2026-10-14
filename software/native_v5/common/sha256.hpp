// SHA-256 (FIPS 180-4) for input identities and result digests. Two runs, two groups or
// two machines produced bit-identical outputs exactly when their digests match.
#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

namespace edgefhe::v5 {
class Sha256 {
 public:
  Sha256();
  void update(const void* data, size_t bytes);
  void update_u32(uint32_t value);  // little-endian, fixed width
  void update_u64(uint64_t value);  // little-endian, fixed width
  std::string hex();                // finishes the hash; the object is then spent

 private:
  void block(const uint8_t* data);
  std::array<uint32_t, 8> state_;
  std::array<uint8_t, 64> buffer_{};
  size_t buffered_ = 0;
  uint64_t total_ = 0;
  bool finished_ = false;
};

std::string sha256_hex(const std::string& bytes);
std::string sha256_file(const std::string& path);  // streams the file
}  // namespace edgefhe::v5
