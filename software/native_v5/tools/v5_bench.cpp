// Same-input timing of the client hot path (four-feature encrypt + one decrypt) for
// every backend configuration: CPU with 1..4 workers (group A) and the DMA backend with
// and without overlap (group B). Configurations are interleaved in a rotating order each
// round so drift affects all of them alike. Key generation and sampling are outside the
// timed region. Output is one JSON object on stdout.
//
// With --fd the DMA backend uses the operator's gated descriptor (see v5_parity.cpp).
// Without it the DMA rows use SimulatedDmaDevice, whose times are CPU times, not FPGA.
#include "ckks/client.hpp"
#include "backend/cpu/cpu_backend.hpp"
#include "backend/fpga/fpga_backend.hpp"
#include <algorithm>
#include <iostream>
#include <memory>
#include <random>
#include <string>

using namespace edgefhe;
using namespace edgefhe::v5;

namespace {
double percentile(std::vector<uint64_t> values, double p) {
  std::sort(values.begin(), values.end());
  const size_t index = std::min(values.size() - 1, size_t(p * double(values.size() - 1) + 0.5));
  return double(values[index]) / 1e3;  // microseconds
}
}  // namespace

int main(int argc, char** argv) {
  int fd = -1;
  size_t rounds = 30, warmup = 3;
  try {
    for (int i = 1; i < argc; i++) {
      const std::string arg = argv[i];
      if (arg == "--fd" && i + 1 < argc) fd = std::stoi(argv[++i]);
      else if (arg == "--rounds" && i + 1 < argc) rounds = std::stoul(argv[++i]);
      else if (arg == "--warmup" && i + 1 < argc) warmup = std::stoul(argv[++i]);
      else throw std::invalid_argument("usage: v5_bench [--fd N] [--rounds R] [--warmup W]");
    }
    require(rounds >= 1 && rounds <= 10000 && warmup <= 1000, "Invalid --rounds/--warmup");

    std::unique_ptr<DmaDevice> device;
    if (fd >= 0) device = std::make_unique<LinuxDmaDevice>(fd);
    else device = std::make_unique<SimulatedDmaDevice>();
    std::vector<std::unique_ptr<PolyBackend>> backends;
    for (unsigned workers = 1; workers <= 4; workers++) backends.push_back(std::make_unique<CpuBackend>(workers));
    backends.push_back(std::make_unique<FpgaBackend>(*device, false));
    backends.push_back(std::make_unique<FpgaBackend>(*device, true));

    // Fixed public inputs for every configuration; full 2048-slot batches.
    std::mt19937_64 rng(20261013);
    std::uniform_real_distribution<double> slot(-4, 4);
    std::array<Message, 4> messages;
    for (auto& m : messages) {
      std::vector<std::complex<double>> values(slots);
      for (auto& v : values) v = {slot(rng), 0};
      m = encode(values);
    }
    const auto material = sample_material();
    CpuBackend setup(1);
    const auto key = Client(setup).keygen(material);

    std::vector<std::vector<uint64_t>> samples(backends.size());
    for (size_t round = 0; round < warmup + rounds; round++) {
      for (size_t k = 0; k < backends.size(); k++) {
        const size_t b = (round + k) % backends.size();
        Client client(*backends[b]);
        if (round == warmup) backends[b]->reset_stats();
        const auto started = Clock::now();
        const auto cipher = client.encrypt(key, messages, material.mask, material.e0, material.e1);
        const auto plain = client.decrypt(cipher[0], material.secret);
        const auto spent = elapsed_ns(started);
        require(plain.size() == degree, "Unexpected decryption length");
        if (round >= warmup) samples[b].push_back(spent);
      }
    }

    std::cout << "{\n  \"device\": \"" << device->name() << "\",\n"
              << "  \"device_times_are_fpga\": " << (fd >= 0 ? "true" : "false") << ",\n"
              << "  \"rounds\": " << rounds << ", \"warmup\": " << warmup << ",\n"
              << "  \"timed_region\": \"encrypt four full-slot features + decrypt one; keygen and sampling excluded\",\n"
              << "  \"results\": [\n";
    for (size_t b = 0; b < backends.size(); b++) {
      const auto& s = backends[b]->stats();
      std::cout << "    {\"backend\": \"" << backends[b]->name() << "\", \"p50_us\": " << percentile(samples[b], 0.5)
                << ", \"p95_us\": " << percentile(samples[b], 0.95) << ", \"multiply_jobs\": " << s.jobs
                << ", \"pack_ms\": " << s.pack_ns / 1e6 << ", \"device_ms\": " << s.device_ns / 1e6
                << ", \"unpack_ms\": " << s.unpack_ns / 1e6 << ", \"multiply_wall_ms\": " << s.wall_ns / 1e6
                << ", \"driver_reported_ms\": " << s.device_reported_us / 1e3 << "}"
                << (b + 1 < backends.size() ? "," : "") << "\n";
    }
    std::cout << "  ]\n}\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "FAIL: " << e.what() << "\n";
    return 1;
  }
}
