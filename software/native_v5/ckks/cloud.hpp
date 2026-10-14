// Everything the cloud does (step 7): sum of public weight x ciphertext plus bias,
// without keys or plaintext. CPU-only, using the accepted V4 Engine::affine. In
// single-board measurements it runs in-process and is identical for both groups; the
// two-machine demo runs the same arithmetic in software/native_ckks/pc_server.py.
#pragma once
#include "ckks/model.hpp"
#include "core.hpp"
#include <array>

namespace edgefhe::v5 {
class CloudAffine {
 public:
  // workers 1 or 3 (the V4 Engine rule).
  CloudAffine(const Model& model, unsigned workers);
  Cipher evaluate(const std::array<Cipher, 4>& cipher) const;
  const EncodedModel& encoded() const { return encoded_; }

 private:
  EncodedModel encoded_;
  Engine engine_;
};
}  // namespace edgefhe::v5
