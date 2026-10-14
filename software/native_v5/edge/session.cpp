#include "edge/session.hpp"
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace edgefhe::v5 {
namespace {
constexpr const char* names[step_count] = {"standardize", "encode", "sample", "encrypt", "cloud", "decrypt", "decode"};
constexpr const char* slides[step_count] = {"1", "2", "3", "4-5", "6-8", "9", "10"};

UWide magnitude(Wide x) { return x < 0 ? UWide(0) - UWide(x) : UWide(x); }
}  // namespace

const char* step_name(size_t step) {
  require(step < step_count, "Unknown step");
  return names[step];
}

const char* step_slide(size_t step) {
  require(step < step_count, "Unknown step");
  return slides[step];
}

std::vector<Batch> plan_batches(size_t records, size_t batch_records, size_t max_batches) {
  require(batch_records >= 1 && batch_records <= slots, "Batch size must be 1..2048 records");
  std::vector<Batch> batches;
  for (size_t first = 0; first < records && (!max_batches || batches.size() < max_batches); first += batch_records) {
    Batch batch;
    batch.index = batches.size();
    batch.first = first;
    batch.count = std::min(batch_records, records - first);
    batches.push_back(std::move(batch));
  }
  return batches;
}

EdgeSession::EdgeSession(PolyBackend& backend, const Model& model, const Preprocess& pre, const CloudAffine& cloud,
                         const Dataset& data, const Material& material, unsigned encode_workers)
    : backend_(backend), client_(backend), pre_(pre), cloud_(cloud), data_(data),
      secret_(material.secret) {
  require(pre.model_id == model.model_id, "Preprocessing belongs to a different model");
  require(encode_workers >= 1 && encode_workers <= 4, "Encode workers must be 1..4 (one per feature)");
  encode_pool_ = std::make_unique<ThreadPool>(encode_workers);
  const auto started = Clock::now();
  key_ = client_.keygen(material);
  keygen_ns_ = elapsed_ns(started);
  bound_ = affine_error_bound(model.weights, model.input_abs_max);
  require(bound_ <= model.max_logit_error, "Native error bound exceeds model target");
}

void EdgeSession::run_step(Step step, Batch& batch) {
  const auto started = Clock::now();
  switch (step) {
    case Step::standardize: standardize(batch); break;
    case Step::encode: encode_batch(batch); break;
    case Step::sample: sample(batch); break;
    case Step::encrypt: encrypt(batch); return;  // times itself, excluding the wait for the backend
    case Step::cloud: cloud(batch); break;
    case Step::decrypt: decrypt(batch); return;
    case Step::decode: decode_batch(batch); break;
    case Step::count: throw std::invalid_argument("Unknown step");
  }
  batch.ns[size_t(step)] = elapsed_ns(started);
}

std::vector<Stage<Batch>> EdgeSession::stages() {
  return {
      {"prepare",
       [this](Batch& b) {
         run_step(Step::standardize, b);
         run_step(Step::encode, b);
         run_step(Step::sample, b);
       }},
      {"encrypt", [this](Batch& b) { run_step(Step::encrypt, b); }},
      {"cloud", [this](Batch& b) { run_step(Step::cloud, b); }},
      {"decrypt", [this](Batch& b) { run_step(Step::decrypt, b); }},
      {"finish", [this](Batch& b) { run_step(Step::decode, b); }},
  };
}

void EdgeSession::standardize(Batch& batch) const {
  require(batch.count >= 1 && batch.count <= slots && batch.first + batch.count <= data_.raw.size(),
          "Batch outside the dataset");
  for (auto& column : batch.z) {
    column.clear();
    column.reserve(batch.count);
  }
  for (size_t i = batch.first; i < batch.first + batch.count; i++) {
    const auto z = v5::standardize(pre_, data_.raw[i]);
    for (size_t f = 0; f < 4; f++) batch.z[f].push_back(z[f]);
  }
}

// The four features are independent and V4 encode() is a pure function, so each task
// writes only its own message. With one worker the caller encodes them in order.
void EdgeSession::encode_batch(Batch& batch) const {
  encode_pool_->run(4, [&batch](size_t f) {
    std::vector<std::complex<double>> values(batch.z[f].begin(), batch.z[f].end());
    batch.messages[f] = encode(values);
  });
  batch.no_wrap = no_wrap_bound(batch.messages, cloud_.encoded().weights, cloud_.encoded().bias);
}

void EdgeSession::sample(Batch& batch) const {
  if (!batch.presampled) batch.samples = sample_encryption();
}

void EdgeSession::encrypt(Batch& batch) {
  std::array<Ternary, 4> u, e0, e1;
  for (size_t f = 0; f < 4; f++) {
    u[f] = batch.samples[f];
    e0[f] = batch.samples[4 + f];
    e1[f] = batch.samples[8 + f];
  }
  std::lock_guard<std::mutex> lock(backend_mutex_);
  const auto started = Clock::now();
  batch.cipher = client_.encrypt(key_, batch.messages, u, e0, e1);
  batch.ns[size_t(Step::encrypt)] = elapsed_ns(started);
}

void EdgeSession::cloud(Batch& batch) const { batch.result = cloud_.evaluate(batch.cipher); }

void EdgeSession::decrypt(Batch& batch) {
  std::lock_guard<std::mutex> lock(backend_mutex_);
  const auto started = Clock::now();
  batch.coefficients = client_.decrypt(batch.result, secret_);
  batch.ns[size_t(Step::decrypt)] = elapsed_ns(started);
}

void EdgeSession::decode_batch(Batch& batch) const {
  require(batch.coefficients.size() == degree, "Missing decrypted coefficients");
  for (const Wide c : batch.coefficients) require(magnitude(c) <= batch.no_wrap, "Returned ciphertext outside local no-wrap bound");
  const auto decoded = decode(batch.coefficients, scale * scale);
  batch.logits.resize(batch.count);
  batch.probability.resize(batch.count);
  batch.decision.resize(batch.count);
  for (size_t i = 0; i < batch.count; i++) {
    require(std::isfinite(decoded[i].real()) && std::isfinite(decoded[i].imag()) && std::abs(decoded[i].imag()) <= bound_,
            "Invalid real output");
    batch.logits[i] = decoded[i].real();
    batch.probability[i] = sigmoid(batch.logits[i]);
    batch.decision[i] = int8_t(classify(batch.logits[i], bound_));
  }
}
}  // namespace edgefhe::v5
