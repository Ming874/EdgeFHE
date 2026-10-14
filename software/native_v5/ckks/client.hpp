// CKKS client arithmetic on top of any PolyBackend. Encoding, sampling, RNS mapping,
// CRT and every bound check are the accepted V4 functions; V5 only changes how the
// polynomial products are scheduled. Results are bit-identical to V4 Engine.
#pragma once
#include "backend/backend.hpp"
#include <array>
#include <vector>

namespace edgefhe::v5 {
class Client {
 public:
  explicit Client(PolyBackend& backend) : backend_(backend) {}

  // b = e - a*s: one product.
  PublicKey keygen(const Material& material);
  // c0 = b*u + e0 + m, c1 = a*u + e1 for four features: all eight products in one batch.
  std::array<Cipher, 4> encrypt(const PublicKey& key, const std::array<Message, 4>& message,
                                const std::array<Ternary, 4>& mask, const std::array<Ternary, 4>& e0,
                                const std::array<Ternary, 4>& e1);
  // centered CRT of c0 + c1*s: one product.
  std::vector<Wide> decrypt(const Cipher& cipher, const Ternary& secret);

  PolyBackend& backend() { return backend_; }

 private:
  PolyBackend& backend_;
};
}  // namespace edgefhe::v5
