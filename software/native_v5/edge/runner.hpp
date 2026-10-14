// Runs batches through an EdgeSession, serially (capacity 0) or pipelined, and measures
// the run as a whole: wall time, process CPU time and, when sensors exist, power. Also
// the digests and accuracy checks that let two runs or two groups be compared.
#pragma once
#include "common/platform.hpp"
#include "edge/session.hpp"
#include <string>
#include <vector>

namespace edgefhe::v5 {
struct RunResult {
  std::vector<Batch> batches;
  PipelineStats pipeline;
  uint64_t wall_ns = 0, cpu_ns = 0;
  std::vector<PowerReading> power;
};
RunResult run_batches(EdgeSession& session, std::vector<Batch> batches, size_t capacity,
                      PowerSampler* power = nullptr);

// SHA-256 over every batch in order: ciphertexts (c0 then c1 of each feature, residues as
// little-endian u32) and decrypted coefficients (two's complement, 16 bytes little-endian).
std::string cipher_digest(const std::vector<Batch>& batches);
std::string decrypted_digest(const std::vector<Batch>& batches);
std::string key_digest(const PublicKey& key);

struct Accuracy {
  size_t records = 0;
  double max_abs_diff = 0, mean_abs_diff = 0;  // CKKS logit vs plaintext logit
  bool within_bound = true;
  size_t agree = 0, disagree = 0, uncertain = 0;  // CKKS decision vs plaintext decision
  bool labelled = false;
  size_t tp = 0, tn = 0, fp = 0, fn = 0;  // certain CKKS decisions vs labels
  double auc = 0, plain_auc = 0;
};
Accuracy check_accuracy(const std::vector<Batch>& batches, const Dataset& data, const Model& model,
                        const Preprocess& pre, double bound);
// Rank-based ROC AUC with ties averaged (the same definition as training/ckks_check.py).
double roc_auc(const std::vector<int8_t>& labels, const std::vector<double>& scores);
}  // namespace edgefhe::v5
