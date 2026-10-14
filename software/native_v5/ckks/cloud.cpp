#include "ckks/cloud.hpp"

namespace edgefhe::v5 {
CloudAffine::CloudAffine(const Model& model, unsigned workers) : encoded_(encode_model(model)), engine_(workers, -1) {}

Cipher CloudAffine::evaluate(const std::array<Cipher, 4>& cipher) const {
  return engine_.affine(cipher, encoded_.weights, encoded_.bias);
}
}  // namespace edgefhe::v5
