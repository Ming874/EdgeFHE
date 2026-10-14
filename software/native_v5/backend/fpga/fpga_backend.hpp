// Hardware backend (group B): frozen PL multiply (op2) through the DMA driver.
#pragma once
#include "backend/backend.hpp"
#include "backend/fpga/device.hpp"
#include "runtime/thread_pool.hpp"
#include <array>
#include <atomic>
#include <vector>

namespace edgefhe::v5 {
class FpgaBackend final : public PolyBackend {
 public:
  // With overlap, a helper thread packs job i+1 and unpacks job i-1 while job i is in
  // the driver (double-buffered). The driver still sees one request at a time.
  explicit FpgaBackend(DmaDevice& device, bool overlap = true, uint32_t timeout_ms = 5000);
  void multiply_batch(const std::vector<MulJob>& jobs) override;
  std::string name() const override;
  // True after a DMA or output failure; every later batch is refused without a request.
  bool stopped() const { return stopped_; }

 private:
  void run_validated(const std::vector<MulJob>& jobs);
  static void pack(const MulJob& job, std::vector<abi::Beat>& input);
  static void unpack(const std::vector<abi::Beat>& output, Rns& out);
  void submit(std::vector<abi::Beat>& input, std::vector<abi::Beat>& output);

  DmaDevice& device_;
  bool overlap_;
  uint32_t timeout_ms_;
  std::array<std::vector<abi::Beat>, 2> input_, output_;
  SerialWorker helper_;
  std::atomic<bool> stopped_{false};
};
}  // namespace edgefhe::v5
