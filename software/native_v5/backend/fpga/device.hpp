// DMA device abstraction: the real driver, or a host model of it for x86 tests.
#pragma once
#include "backend/fpga/dma_abi.hpp"
#include <array>
#include <cstdint>
#include <string>

namespace edgefhe::v5 {
class DmaDevice {
 public:
  virtual ~DmaDevice() = default;
  // Runs one request and returns 0 or a negative errno, like the driver. Fills
  // request.status and request.elapsed_us. Device failures are reported, never thrown.
  // The accepted driver allows one request in flight; callers must not overlap submits.
  virtual int submit(abi::Request& request) = 0;
  virtual std::string name() const = 0;
};

// Uses a descriptor the operator opened after the live identity gates. It neither opens
// nor closes the device: board access stays an explicit operator action.
class LinuxDmaDevice final : public DmaDevice {
 public:
  explicit LinuxDmaDevice(int fd);
  int submit(abi::Request& request) override;
  std::string name() const override { return "linux-dma"; }

 private:
  int fd_;
};

// Host model of the driver plus the PL multiply (op2): the same request checks as the
// driver, and products from the accepted V4 CPU NTT. It does not model op0/op1 output
// order or ALU stream interleaving; those return -EOPNOTSUPP. Timings are CPU timings,
// not FPGA timings. Faults can be injected to test the error paths.
class SimulatedDmaDevice final : public DmaDevice {
 public:
  enum class Fault { none, fail_call, nonzero_padding, noncanonical_output };

  SimulatedDmaDevice();
  void inject(Fault fault, uint64_t on_call);  // 1-based index of the submit to corrupt
  int submit(abi::Request& request) override;
  std::string name() const override { return "simulated-dma"; }
  uint64_t calls() const { return calls_; }

 private:
  std::array<Plan, 3> plans_;
  Fault fault_ = Fault::none;
  uint64_t fault_call_ = 0, calls_ = 0;
};
}  // namespace edgefhe::v5
