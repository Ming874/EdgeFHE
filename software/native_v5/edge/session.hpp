// The edge side of the system, step by step as on the architecture slide:
//   1 standardize   2 encode   3 sample   4-5 encrypt (backend)   6-8 cloud
//   9 decrypt (backend)   10 decode, sigmoid, decision
// Group A and group B run exactly this code; only the PolyBackend differs. Each step
// records its own time in the batch, so results can be split by step for both groups.
// Step 2 can encode the four features on up to four threads (encode_workers); every
// feature still goes through the accepted V4 encode(), so the result is bit-identical.
#pragma once
#include "backend/backend.hpp"
#include "ckks/client.hpp"
#include "ckks/cloud.hpp"
#include "ckks/model.hpp"
#include "edge/dataset.hpp"
#include "runtime/pipeline.hpp"
#include "runtime/thread_pool.hpp"
#include <array>
#include <memory>
#include <mutex>
#include <vector>

namespace edgefhe::v5 {
enum class Step : size_t { standardize, encode, sample, encrypt, cloud, decrypt, decode, count };
constexpr size_t step_count = size_t(Step::count);
const char* step_name(size_t step);   // "standardize", "encode", ...
const char* step_slide(size_t step);  // slide numbers: "1", "2", "3", "4-5", "6-8", "9", "10"

struct Batch {
  size_t index = 0, first = 0, count = 0;  // records [first, first + count)
  bool presampled = false;                 // samples already filled (QA seed or shared parity samples)
  std::array<std::vector<double>, 4> z;    // 1
  std::array<Message, 4> messages;         // 2
  UWide no_wrap = 0;                       // 2: largest admissible decrypted coefficient
  std::array<Ternary, 12> samples;         // 3: mask x4, e0 x4, e1 x4
  std::array<Cipher, 4> cipher;            // 4-5
  Cipher result;                           // 6-8
  std::vector<Wide> coefficients;          // 9
  std::vector<double> logits, probability; // 10
  std::vector<int8_t> decision;            // 10: 1, 0, or -1 when within the error bound of 0
  std::array<uint64_t, step_count> ns{};
};

// Consecutive batches of at most batch_records (1..2048) records; max_batches 0 = all.
std::vector<Batch> plan_batches(size_t records, size_t batch_records, size_t max_batches = 0);

class EdgeSession {
 public:
  // Generates the public key with the backend (one product). Only the secret part of
  // `material` is kept. `data`, `pre` and `cloud` must outlive the session.
  // encode_workers 1..4: threads for step 2 (1 = one feature after another, as before).
  EdgeSession(PolyBackend& backend, const Model& model, const Preprocess& pre, const CloudAffine& cloud,
              const Dataset& data, const Material& material, unsigned encode_workers = 1);

  void run_step(Step step, Batch& batch);
  // Pipeline stages: prepare (1-3), encrypt (4-5), cloud (6-8), decrypt (9), finish (10).
  // Encrypt and decrypt share the backend, so they never run at the same time.
  std::vector<Stage<Batch>> stages();

  const PublicKey& public_key() const { return key_; }
  double error_bound() const { return bound_; }
  uint64_t keygen_ns() const { return keygen_ns_; }
  unsigned encode_workers() const { return encode_pool_->workers(); }
  PolyBackend& backend() { return backend_; }

 private:
  void standardize(Batch& batch) const;
  void encode_batch(Batch& batch) const;
  void sample(Batch& batch) const;
  void encrypt(Batch& batch);
  void cloud(Batch& batch) const;
  void decrypt(Batch& batch);
  void decode_batch(Batch& batch) const;

  PolyBackend& backend_;
  Client client_;
  const Preprocess& pre_;
  const CloudAffine& cloud_;
  const Dataset& data_;
  Ternary secret_;
  PublicKey key_;
  double bound_ = 0;
  uint64_t keygen_ns_ = 0;
  std::unique_ptr<ThreadPool> encode_pool_;  // step 2; used only by the thread running "prepare"
  std::mutex backend_mutex_;  // one product batch in the backend at a time (one DMA in flight)
};
}  // namespace edgefhe::v5
