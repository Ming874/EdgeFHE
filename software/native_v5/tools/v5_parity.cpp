// Exact CPU <-> FPGA parity for the V5 client (roadmap phase 2: "V4 native -> PL").
//
// Without --fd this runs against SimulatedDmaDevice (host QA of the packing path only).
// On the board, the operator first runs the live identity gates, opens the device and
// passes the descriptor, for example:
//   exec 3<>/dev/edgefhe_rns3_n4096_dma && ./v5_parity --fd 3 --cases 64
// This tool never opens, resets or retries the device itself.
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

int main(int argc, char** argv) {
  int fd = -1;
  size_t cases = 32;
  bool overlap = true;
  try {
    for (int i = 1; i < argc; i++) {
      const std::string arg = argv[i];
      if (arg == "--fd" && i + 1 < argc) fd = std::stoi(argv[++i]);
      else if (arg == "--cases" && i + 1 < argc) cases = std::stoul(argv[++i]);
      else if (arg == "--no-overlap") overlap = false;
      else throw std::invalid_argument("usage: v5_parity [--fd N] [--cases K] [--no-overlap]");
    }
    require(cases >= 1 && cases <= 100000, "--cases must be 1..100000");
    std::unique_ptr<DmaDevice> device;
    if (fd >= 0) device = std::make_unique<LinuxDmaDevice>(fd);
    else device = std::make_unique<SimulatedDmaDevice>();
    FpgaBackend fpga(*device, overlap);
    CpuBackend cpu(1);

    // 1. Raw products on random canonical operands (public QA seed, not key material),
    //    submitted in batches of eight like one encryption.
    std::mt19937_64 rng(20261013);
    size_t compared = 0, mismatches = 0;
    for (size_t done = 0; done < cases;) {
      const size_t count = std::min<size_t>(8, cases - done);
      std::vector<Rns> a(count), b(count), hw(count), sw(count);
      std::vector<MulJob> hw_jobs, sw_jobs;
      for (size_t j = 0; j < count; j++) {
        for (size_t l = 0; l < limbs; l++) {
          a[j][l].resize(degree);
          b[j][l].resize(degree);
          for (size_t i = 0; i < degree; i++) {
            a[j][l][i] = uint32_t(rng() % primes[l]);
            b[j][l][i] = uint32_t(rng() % primes[l]);
          }
        }
        hw_jobs.push_back({&a[j], &b[j], &hw[j]});
        sw_jobs.push_back({&a[j], &b[j], &sw[j]});
      }
      fpga.multiply_batch(hw_jobs);
      cpu.multiply_batch(sw_jobs);
      for (size_t j = 0; j < count; j++)
        for (size_t l = 0; l < limbs; l++)
          for (size_t i = 0; i < degree; i++) {
            compared++;
            if (hw[j][l][i] != sw[j][l][i] && mismatches++ == 0)
              std::cerr << "first mismatch: case " << done + j << " limb " << l << " coefficient " << i << "\n";
          }
      done += count;
    }

    // 2. One full client round (fresh OS sampling): keygen, four-feature encrypt and
    //    decrypt through the FPGA path must equal the CPU path exactly.
    const auto material = sample_material();
    std::array<Message, 4> messages;
    std::uniform_real_distribution<double> slot(-4, 4);
    for (auto& m : messages) {
      std::vector<std::complex<double>> values(slots);
      for (auto& v : values) v = {slot(rng), 0};
      m = encode(values);
    }
    Client hw_client(fpga), sw_client(cpu);
    const auto hw_key = hw_client.keygen(material), sw_key = sw_client.keygen(material);
    const auto hw_ct = hw_client.encrypt(hw_key, messages, material.mask, material.e0, material.e1);
    const auto sw_ct = sw_client.encrypt(sw_key, messages, material.mask, material.e0, material.e1);
    bool client_equal = hw_key.b == sw_key.b;
    for (size_t f = 0; f < features; f++)
      client_equal = client_equal && hw_ct[f].c0 == sw_ct[f].c0 && hw_ct[f].c1 == sw_ct[f].c1 &&
                     hw_client.decrypt(hw_ct[f], material.secret) == sw_client.decrypt(sw_ct[f], material.secret);

    const auto& s = fpga.stats();
    std::cout << "device=" << device->name() << " backend=" << fpga.name() << " requests=" << s.jobs
              << " pack_ms=" << s.pack_ns / 1e6 << " device_ms=" << s.device_ns / 1e6
              << " unpack_ms=" << s.unpack_ns / 1e6 << " wall_ms=" << s.wall_ns / 1e6
              << " driver_reported_ms=" << s.device_reported_us / 1e3 << "\n";
    if (mismatches || !client_equal) {
      std::cerr << "FAIL: mismatched coefficients=" << mismatches << " client_equal=" << client_equal << "\n";
      return 1;
    }
    std::cout << "V5_PARITY_PASS device=" << device->name() << " cases=" << cases << " coefficients=" << compared
              << " client_round=exact\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "FAIL: " << e.what() << "\n";
    return 1;
  }
}
