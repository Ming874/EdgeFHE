#include "backend/factory.hpp"
#include "backend/cpu/cpu_backend.hpp"
#include "backend/fpga/fpga_backend.hpp"
#include <cctype>
#include <stdexcept>

namespace edgefhe::v5 {
namespace {
bool cpu_workers(const std::string& spec, unsigned& workers) {
  if (spec.rfind("cpu-w", 0) != 0 || spec.size() == 5 || spec.size() > 7) return false;
  workers = 0;
  for (size_t i = 5; i < spec.size(); i++) {
    if (!std::isdigit(static_cast<unsigned char>(spec[i]))) return false;
    workers = workers * 10 + unsigned(spec[i] - '0');
  }
  return workers >= 1 && workers <= 64;
}
}  // namespace

bool backend_needs_device(const std::string& spec) { return spec == "fpga-serial" || spec == "fpga-overlap"; }

std::unique_ptr<PolyBackend> make_backend(const std::string& spec, DmaDevice* device) {
  unsigned workers = 0;
  if (cpu_workers(spec, workers)) return std::make_unique<CpuBackend>(workers);
  if (backend_needs_device(spec)) {
    require(device != nullptr, "FPGA backend needs a DMA device");
    return std::make_unique<FpgaBackend>(*device, spec == "fpga-overlap");
  }
  throw std::invalid_argument("Unknown backend '" + spec + "' (use cpu-w1..cpu-w64, fpga-serial, fpga-overlap)");
}

std::vector<std::string> split_list(const std::string& text) {
  std::vector<std::string> items;
  size_t begin = 0;
  for (;;) {
    const size_t end = text.find(',', begin);
    items.push_back(text.substr(begin, end == std::string::npos ? std::string::npos : end - begin));
    require(!items.back().empty(), "Empty item in comma-separated list");
    if (end == std::string::npos) return items;
    begin = end + 1;
  }
}
}  // namespace edgefhe::v5
