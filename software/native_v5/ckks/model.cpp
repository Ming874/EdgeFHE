#include "ckks/model.hpp"
#include "common/json.hpp"
#include <algorithm>
#include <cctype>
#include <cmath>
#include <set>

namespace edgefhe::v5 {
namespace {
void exact_keys(const JsonValue& object, const std::set<std::string>& required, const std::set<std::string>& optional,
                const std::string& what) {
  const auto& keys = object.keys(what);
  for (const auto& key : required) require(object.find(key) != nullptr, (what + ": missing field " + key).c_str());
  for (const auto& key : keys)
    require(required.count(key) || optional.count(key), (what + ": unknown field " + key).c_str());
}

double bounded(const JsonValue& value, const std::string& what, double limit) {
  const double x = value.number(what);
  require(std::isfinite(x) && std::abs(x) <= limit, (what + " must be finite with |value| <= 16").c_str());
  return x;
}

bool valid_model_id(const std::string& id) {
  return !id.empty() && id.size() <= 64 && std::all_of(id.begin(), id.end(), [](unsigned char c) {
    return std::isalnum(c) || c == '_' || c == '.' || c == '-';
  });
}
}  // namespace

Model load_model(const std::string& path) {
  const JsonValue json = read_json_file(path);
  exact_keys(json, {"model_id", "weights", "bias", "input_abs_max", "max_logit_error"}, {}, "model");
  Model m;
  m.model_id = json.at("model_id").string("model_id");
  require(valid_model_id(m.model_id), "Wrong model ID");
  const auto& weights = json.at("weights").array("weights");
  require(weights.size() == 4, "Four model weights required");
  for (size_t f = 0; f < 4; f++) m.weights[f] = bounded(weights[f], "weight", 16);
  m.bias = bounded(json.at("bias"), "bias", 16);
  m.input_abs_max = bounded(json.at("input_abs_max"), "input_abs_max", 16);
  require(m.input_abs_max > 0, "Invalid private input bound");
  m.max_logit_error = json.at("max_logit_error").number("max_logit_error");
  require(std::isfinite(m.max_logit_error) && m.max_logit_error > 0 && m.max_logit_error <= 0.1,
          "Invalid public numerical error target");
  require(affine_error_bound(m.weights, m.input_abs_max) <= m.max_logit_error,
          "Model/range exceeds conservative CKKS error target");
  return m;
}

Preprocess load_preprocess(const std::string& path, const Model& model) {
  const JsonValue json = read_json_file(path);
  exact_keys(json, {"model_id", "features", "mean", "std", "clip", "label"}, {"decision", "dataset_manifest_sha256"},
             "preprocess");
  Preprocess p;
  p.model_id = json.at("model_id").string("model_id");
  require(p.model_id == model.model_id, "Preprocessing belongs to a different model");
  p.label = json.at("label").string("label");
  require(!p.label.empty(), "Empty label column name");
  const auto& features = json.at("features").array("features");
  const auto& mean = json.at("mean").array("mean");
  const auto& std = json.at("std").array("std");
  require(features.size() == 4 && mean.size() == 4 && std.size() == 4, "Preprocessing needs four features");
  for (size_t f = 0; f < 4; f++) {
    p.features[f] = features[f].string("feature name");
    require(!p.features[f].empty() && p.features[f] != p.label, "Bad feature column name");
    for (size_t g = 0; g < f; g++) require(p.features[g] != p.features[f], "Repeated feature column");
    p.mean[f] = mean[f].number("mean");
    p.std[f] = std[f].number("std");
    require(std::isfinite(p.mean[f]) && std::isfinite(p.std[f]) && p.std[f] > 0, "Invalid standardization");
  }
  p.clip = json.at("clip").number("clip");
  require(std::isfinite(p.clip) && p.clip > 0 && p.clip <= model.input_abs_max,
          "Clip must be positive and within the model input bound");
  return p;
}

EncodedModel encode_model(const Model& model) {
  EncodedModel out;
  for (size_t f = 0; f < 4; f++) out.weights[f] = encode(std::vector<std::complex<double>>(slots, model.weights[f]));
  out.bias = encode(std::vector<std::complex<double>>(slots, model.bias), scale * scale);
  return out;
}

std::array<double, 4> standardize(const Preprocess& pre, const std::array<double, 4>& raw) {
  std::array<double, 4> z{};
  for (size_t f = 0; f < 4; f++) z[f] = std::min(pre.clip, std::max(-pre.clip, (raw[f] - pre.mean[f]) / pre.std[f]));
  return z;
}

double plain_logit(const Model& model, const std::array<double, 4>& z) {
  double sum = 0;
  for (size_t f = 0; f < 4; f++) sum += model.weights[f] * z[f];
  return sum + model.bias;
}
}  // namespace edgefhe::v5
