#include "backend/fpga/device.hpp"
#include "common/stats.hpp"
#include <cerrno>
#include <sys/ioctl.h>

namespace edgefhe::v5 {
LinuxDmaDevice::LinuxDmaDevice(int fd) : fd_(fd) {
  require(fd >= 0, "LinuxDmaDevice needs an operator-opened, gated descriptor");
}

int LinuxDmaDevice::submit(abi::Request& request) {
  // No EINTR retry: resubmitting could run the same job twice (V4 policy: stop, no retry).
  if (::ioctl(fd_, abi::run_request, &request) < 0) return -errno;
  return request.status;
}

SimulatedDmaDevice::SimulatedDmaDevice()
    : plans_{Plan(primes[0], psi[0]), Plan(primes[1], psi[1]), Plan(primes[2], psi[2])} {}

void SimulatedDmaDevice::inject(Fault fault, uint64_t on_call) {
  fault_ = fault;
  fault_call_ = on_call;
}

int SimulatedDmaDevice::submit(abi::Request& request) {
  const auto started = Clock::now();
  calls_++;
  request.elapsed_us = 0;
  request.status = 0;
  const auto finish = [&](int result) {
    request.status = result;
    return result;
  };
  // Same order of checks as the accepted driver's ioctl handler.
  if (request.reserved0 || request.reserved) return finish(-EINVAL);
  if (request.timeout_ms > abi::max_timeout_ms) return finish(-EINVAL);
  if (request.op > abi::negate_add) return finish(-EINVAL);
  if (request.input_words != abi::input_words(request.op) || request.output_words != abi::output_words)
    return finish(-EINVAL);
  if (!request.input_ptr || !request.output_ptr) return finish(-EFAULT);
  const auto* in = reinterpret_cast<const abi::Beat*>(request.input_ptr);
  auto* out = reinterpret_cast<abi::Beat*>(request.output_ptr);
  for (uint32_t beat = 0; beat < request.input_words; beat++) {
    for (size_t l = 0; l < limbs; l++)
      if (in[beat].limb[l] >= primes[l]) return finish(-EINVAL);
    if (in[beat].limb[3] != 0) return finish(-EINVAL);
  }
  if (fault_ == Fault::fail_call && calls_ == fault_call_) return finish(-EIO);
  if (request.op != abi::multiply) return finish(-EOPNOTSUPP);

  for (size_t l = 0; l < limbs; l++) {
    Poly a(degree), b(degree);
    for (size_t i = 0; i < degree; i++) {
      a[i] = in[i].limb[l];
      b[i] = in[degree + i].limb[l];
    }
    auto x = plans_[l].transform(a, false);
    const auto y = plans_[l].transform(b, false);
    for (size_t i = 0; i < degree; i++) x[i] = plans_[l].product(x[i], y[i]);
    const auto product = plans_[l].transform(x, true);
    for (size_t i = 0; i < degree; i++) out[i].limb[l] = product[i];
  }
  for (size_t i = 0; i < degree; i++) out[i].limb[3] = 0;

  if (calls_ == fault_call_) {
    if (fault_ == Fault::nonzero_padding) out[7].limb[3] = 1;
    if (fault_ == Fault::noncanonical_output) out[5].limb[0] = primes[0];
  }
  request.elapsed_us = uint32_t(elapsed_ns(started) / 1000);
  return finish(0);
}
}  // namespace edgefhe::v5
