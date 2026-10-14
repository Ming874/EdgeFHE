// Frozen V3 DMA ABI, copied from the accepted driver
// (software/driver/edgefhe_rns3_n4096_dma_proxy.c). The accepted bitstream and kernel
// module implement exactly this layout; changing it requires a new board acceptance.
#pragma once
#include "core.hpp"
#include <cstddef>
#include <cstdint>
#include <sys/ioctl.h>

namespace edgefhe::v5::abi {
enum Operation : uint32_t {
  forward_ntt = 0,
  inverse_ntt = 1,
  multiply = 2,
  add_two = 3,
  add_three = 4,
  negate_add = 5,
};

// One AXI-Stream beat: the three RNS residues of one coefficient plus zero padding.
struct alignas(16) Beat { uint32_t limb[4]; };

struct Request {
  uint32_t op, reserved0, input_words, output_words;
  uint64_t input_ptr, output_ptr;
  uint32_t timeout_ms, elapsed_us;
  int32_t status;
  uint32_t reserved;
};

static_assert(sizeof(Beat) == 16, "Frozen beat width");
static_assert(sizeof(Request) == 48 && offsetof(Request, status) == 40, "Frozen request layout");
constexpr unsigned long run_request = _IOWR('E', 0x04, Request);
static_assert(run_request == 0xc0304504UL, "Frozen ioctl number");

constexpr uint32_t max_timeout_ms = 30000;
constexpr uint32_t output_words = degree;

// Input length in beats that the driver accepts for each operation.
constexpr uint32_t input_words(uint32_t op) {
  return op == multiply || op == add_two || op == negate_add ? 2 * degree
       : op == add_three ? 3 * degree
       : degree;
}
}  // namespace edgefhe::v5::abi
