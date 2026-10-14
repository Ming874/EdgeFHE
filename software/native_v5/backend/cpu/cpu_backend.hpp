// Software backend (group A): accepted V4 Shoup/Barrett NTT on a persistent pool.
#pragma once
#include "backend/backend.hpp"
#include "runtime/thread_pool.hpp"
#include <array>

namespace edgefhe::v5 {
class CpuBackend final : public PolyBackend {
 public:
  // workers counts the calling thread; the KV260 has four Cortex-A53 cores.
  explicit CpuBackend(unsigned workers = 1);
  void multiply_batch(const std::vector<MulJob>& jobs) override;
  std::string name() const override;
  unsigned workers() const { return pool_.workers(); }

 private:
  std::array<Plan, 3> plans_;
  ThreadPool pool_;
};
}  // namespace edgefhe::v5
