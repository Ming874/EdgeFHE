// The single seam between the CKKS client and whatever computes polynomial products.
// The CPU and FPGA backends implement the same interface, so one harness can compare
// them bit for bit (the two-group plan: software-parallel vs. hardware/software).
#pragma once
#include "common/stats.hpp"
#include "core.hpp"
#include <string>
#include <vector>

namespace edgefhe::v5 {
// out = a * b in Z_Q[X]/(X^4096 + 1): canonical RNS residues, coefficient domain,
// natural order. Outputs must not alias any input or each other.
struct MulJob {
  const Rns* a;
  const Rns* b;
  Rns* out;
};

class PolyBackend {
 public:
  virtual ~PolyBackend() = default;
  // Computes every job. On failure throws; outputs of that batch are then unspecified.
  // One batch at a time per backend object.
  virtual void multiply_batch(const std::vector<MulJob>& jobs) = 0;
  virtual std::string name() const = 0;

  const PhaseStats& stats() const { return stats_; }
  void reset_stats() { stats_ = {}; }

 protected:
  static void validate_jobs(const std::vector<MulJob>& jobs);
  PhaseStats stats_;
};
}  // namespace edgefhe::v5
