#include "backend/cpu/cpu_backend.hpp"
#include <unordered_map>

namespace edgefhe::v5 {
CpuBackend::CpuBackend(unsigned workers)
    : plans_{Plan(primes[0], psi[0]), Plan(primes[1], psi[1]), Plan(primes[2], psi[2])}, pool_(workers) {}

std::string CpuBackend::name() const { return "cpu-w" + std::to_string(pool_.workers()); }

void CpuBackend::multiply_batch(const std::vector<MulJob>& jobs) {
  validate_jobs(jobs);
  if (jobs.empty()) return;
  const auto started = Clock::now();

  // An operand shared by several jobs (the public key, a mask) is transformed once per
  // limb. For the four-feature encrypt batch this gives V4's 14 transforms per limb
  // instead of 24, without the client having to know about NTT reuse.
  std::vector<const Rns*> operands;
  std::unordered_map<const Rns*, size_t> index;
  std::vector<std::array<size_t, 2>> uses(jobs.size());
  for (size_t j = 0; j < jobs.size(); j++)
    for (size_t side = 0; side < 2; side++) {
      const Rns* operand = side ? jobs[j].b : jobs[j].a;
      const auto [it, added] = index.emplace(operand, operands.size());
      if (added) operands.push_back(operand);
      uses[j][side] = it->second;
    }

  // Tasks are (limb, operand) and then (limb, job): more independent tasks than limbs,
  // so four workers stay busy instead of capping at three.
  const size_t count = operands.size();
  std::vector<Poly> transformed(limbs * count);
  pool_.run(limbs * count, [&](size_t task) {
    const size_t l = task / count, u = task % count;
    transformed[task] = plans_[l].transform((*operands[u])[l], false);
  });
  std::vector<Rns> results(jobs.size());
  pool_.run(limbs * jobs.size(), [&](size_t task) {
    const size_t l = task / jobs.size(), j = task % jobs.size();
    const auto& x = transformed[l * count + uses[j][0]];
    const auto& y = transformed[l * count + uses[j][1]];
    Poly product(degree);
    for (size_t i = 0; i < degree; i++) product[i] = plans_[l].product(x[i], y[i]);
    results[j][l] = plans_[l].transform(product, true);
  });
  for (size_t j = 0; j < jobs.size(); j++) *jobs[j].out = std::move(results[j]);

  stats_.batches++;
  stats_.jobs += jobs.size();
  stats_.forward_transforms += limbs * count;
  stats_.inverse_transforms += limbs * jobs.size();
  stats_.wall_ns += elapsed_ns(started);
}
}  // namespace edgefhe::v5
