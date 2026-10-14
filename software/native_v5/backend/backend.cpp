#include "backend/backend.hpp"
#include <unordered_set>

namespace edgefhe::v5 {
void PolyBackend::validate_jobs(const std::vector<MulJob>& jobs) {
  std::unordered_set<const Rns*> inputs, outputs;
  for (const auto& job : jobs) {
    require(job.a && job.b && job.out, "Null multiply job pointer");
    validate(*job.a);
    validate(*job.b);
    inputs.insert(job.a);
    inputs.insert(job.b);
    require(outputs.insert(job.out).second, "Two jobs write the same output");
  }
  for (const auto* out : outputs) require(!inputs.count(out), "Job output aliases an input");
}
}  // namespace edgefhe::v5
