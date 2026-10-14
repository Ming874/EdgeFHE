// Phase timing. FPGA calls are split into pack / device / unpack so the transfer and
// layout-conversion cost can be measured separately from the PL compute.
#pragma once
#include <chrono>
#include <cstdint>

namespace edgefhe::v5 {
using Clock = std::chrono::steady_clock;

inline uint64_t elapsed_ns(Clock::time_point since) {
  return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - since).count());
}

struct PhaseStats {
  uint64_t batches = 0, jobs = 0;
  uint64_t wall_ns = 0;          // whole multiply_batch calls
  // FPGA backend. With overlap, pack/unpack run beside the device call, so
  // pack_ns + device_ns + unpack_ns can exceed wall_ns; the difference is the overlap.
  uint64_t pack_ns = 0, device_ns = 0, unpack_ns = 0;
  uint64_t device_reported_us = 0;  // driver's own elapsed_us (DMA + PL)
  uint64_t bytes_to_device = 0, bytes_from_device = 0;
  // CPU backend.
  uint64_t forward_transforms = 0, inverse_transforms = 0;
};
}  // namespace edgefhe::v5
