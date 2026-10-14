// The public model and the edge preprocessing, loaded with the same rules as the Python
// side (software/native_ckks/model.py), and their CKKS encodings. Only public values:
// no records, keys or results.
#pragma once
#include "core.hpp"
#include <array>
#include <string>

namespace edgefhe::v5 {
// training/outputs/model.json
struct Model {
  std::string model_id;
  std::array<double, 4> weights{};
  double bias = 0, input_abs_max = 0, max_logit_error = 0;
};

// training/outputs/edge_preprocess.json: which CSV columns feed the model, and
// z = clip((x - mean) / std) (step 1 on the board).
struct Preprocess {
  std::string model_id, label;
  std::array<std::string, 4> features;
  std::array<double, 4> mean{}, std{};
  double clip = 0;
};

Model load_model(const std::string& path);
// Also checks that it belongs to `model` and clips inside the model's input bound.
Preprocess load_preprocess(const std::string& path, const Model& model);

// The same public weight in every slot encodes as a constant polynomial at scale 2^29;
// the bias is encoded at 2^58 so it adds directly to the sum of weight x ciphertext.
struct EncodedModel {
  std::array<Message, 4> weights;
  Message bias;
};
EncodedModel encode_model(const Model& model);

std::array<double, 4> standardize(const Preprocess& pre, const std::array<double, 4>& raw);
// Plaintext reference, summed in the same order as the Python training check.
double plain_logit(const Model& model, const std::array<double, 4>& z);
}  // namespace edgefhe::v5
