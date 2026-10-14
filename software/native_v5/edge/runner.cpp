#include "edge/runner.hpp"
#include "common/sha256.hpp"
#include <algorithm>
#include <cmath>
#include <numeric>

namespace edgefhe::v5 {
namespace {
void hash_rns(Sha256& hash, const Rns& value) {
  for (const auto& limb : value)
    for (const uint32_t x : limb) hash.update_u32(x);
}
}  // namespace

RunResult run_batches(EdgeSession& session, std::vector<Batch> batches, size_t capacity, PowerSampler* power) {
  RunResult result;
  if (power) power->start();
  const uint64_t cpu_before = process_cpu_ns();
  const auto started = Clock::now();
  try {
    result.batches = run_stages(std::move(batches), session.stages(), capacity, &result.pipeline);
  } catch (...) {
    if (power) power->stop();
    throw;
  }
  result.wall_ns = elapsed_ns(started);
  result.cpu_ns = process_cpu_ns() - cpu_before;
  if (power) result.power = power->stop();
  return result;
}

std::string cipher_digest(const std::vector<Batch>& batches) {
  Sha256 hash;
  for (const auto& batch : batches)
    for (const auto& cipher : batch.cipher) {
      hash_rns(hash, cipher.c0);
      hash_rns(hash, cipher.c1);
    }
  return hash.hex();
}

std::string decrypted_digest(const std::vector<Batch>& batches) {
  Sha256 hash;
  for (const auto& batch : batches)
    for (const Wide c : batch.coefficients) {
      const UWide bits = UWide(c);
      hash.update_u64(uint64_t(bits));
      hash.update_u64(uint64_t(bits >> 64));
    }
  return hash.hex();
}

std::string key_digest(const PublicKey& key) {
  Sha256 hash;
  hash_rns(hash, key.b);
  hash_rns(hash, key.a);
  return hash.hex();
}

double roc_auc(const std::vector<int8_t>& labels, const std::vector<double>& scores) {
  require(labels.size() == scores.size() && !labels.empty(), "AUC needs one score per label");
  std::vector<size_t> order(scores.size());
  std::iota(order.begin(), order.end(), 0);
  std::sort(order.begin(), order.end(), [&](size_t a, size_t b) { return scores[a] < scores[b]; });
  std::vector<double> rank(scores.size());
  for (size_t i = 0; i < order.size();) {
    size_t j = i;
    while (j + 1 < order.size() && scores[order[j + 1]] == scores[order[i]]) j++;
    for (size_t k = i; k <= j; k++) rank[order[k]] = double(i + j) / 2 + 1;
    i = j + 1;
  }
  double positives = 0, rank_sum = 0;
  for (size_t i = 0; i < labels.size(); i++)
    if (labels[i] == 1) {
      positives++;
      rank_sum += rank[i];
    }
  const double negatives = double(labels.size()) - positives;
  require(positives > 0 && negatives > 0, "AUC needs both classes");
  return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives);
}

Accuracy check_accuracy(const std::vector<Batch>& batches, const Dataset& data, const Model& model,
                        const Preprocess& pre, double bound) {
  Accuracy a;
  std::vector<double> ckks, plain;
  std::vector<int8_t> labels;
  double total = 0;
  for (const auto& batch : batches) {
    require(batch.logits.size() == batch.count, "Batch without decoded results");
    for (size_t i = 0; i < batch.count; i++) {
      const size_t record = batch.first + i;
      const double reference = plain_logit(model, standardize(pre, data.raw[record]));
      const double diff = std::abs(batch.logits[i] - reference);
      a.max_abs_diff = std::max(a.max_abs_diff, diff);
      total += diff;
      a.within_bound = a.within_bound && diff <= bound;
      const int8_t decision = batch.decision[i];
      if (decision < 0) a.uncertain++;
      else if (decision == (reference > 0 ? 1 : 0)) a.agree++;
      else a.disagree++;
      ckks.push_back(batch.logits[i]);
      plain.push_back(reference);
      if (!data.labels.empty()) {
        const int8_t label = data.labels[record];
        labels.push_back(label);
        if (decision == 1 && label == 1) a.tp++;
        if (decision == 0 && label == 0) a.tn++;
        if (decision == 1 && label == 0) a.fp++;
        if (decision == 0 && label == 1) a.fn++;
      }
    }
  }
  a.records = ckks.size();
  a.mean_abs_diff = a.records ? total / double(a.records) : 0;
  if (!labels.empty() && std::count(labels.begin(), labels.end(), 1) > 0 &&
      std::count(labels.begin(), labels.end(), 0) > 0) {
    a.labelled = true;
    a.auc = roc_auc(labels, ckks);
    a.plain_auc = roc_auc(labels, plain);
  }
  return a;
}
}  // namespace edgefhe::v5
