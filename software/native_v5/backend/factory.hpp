// Backend selection by name. Every tool and the C ABI use these spellings, so a result
// file always says which group and configuration produced it.
//   cpu-wN        group A: CpuBackend with N workers (the caller counts), N = 1..64
//   fpga-serial   group B: FpgaBackend, pack -> DMA -> unpack one job after another
//   fpga-overlap  group B: FpgaBackend, packing/unpacking overlapped with the DMA call
// To add a configuration (for example a NEON CPU backend), add one branch here.
#pragma once
#include "backend/backend.hpp"
#include "backend/fpga/device.hpp"
#include <memory>
#include <string>
#include <vector>

namespace edgefhe::v5 {
bool backend_needs_device(const std::string& spec);
// `device` must outlive the backend and is required exactly when backend_needs_device().
std::unique_ptr<PolyBackend> make_backend(const std::string& spec, DmaDevice* device);
// "a,b,c" -> {"a","b","c"}; empty items are rejected.
std::vector<std::string> split_list(const std::string& text);
}  // namespace edgefhe::v5
