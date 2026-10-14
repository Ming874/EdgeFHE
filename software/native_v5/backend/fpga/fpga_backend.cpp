#include "backend/fpga/fpga_backend.hpp"
#include <stdexcept>

namespace edgefhe::v5 {
FpgaBackend::FpgaBackend(DmaDevice& device, bool overlap, uint32_t timeout_ms)
    : device_(device), overlap_(overlap), timeout_ms_(timeout_ms) {
  require(timeout_ms > 0 && timeout_ms <= abi::max_timeout_ms, "DMA timeout must be 1..30000 ms");
  for (auto& buffer : input_) buffer.resize(abi::input_words(abi::multiply));
  for (auto& buffer : output_) buffer.resize(abi::output_words);
}

std::string FpgaBackend::name() const {
  return "fpga-" + device_.name() + (overlap_ ? "-overlap" : "-serial");
}

// Software keeps polynomials limb-major ([3][4096]); the stream is coefficient-major
// ([4096][limb0, limb1, limb2, 0]). This transpose runs on every call.
void FpgaBackend::pack(const MulJob& job, std::vector<abi::Beat>& input) {
  for (size_t i = 0; i < degree; i++) {
    input[i] = {{(*job.a)[0][i], (*job.a)[1][i], (*job.a)[2][i], 0}};
    input[degree + i] = {{(*job.b)[0][i], (*job.b)[1][i], (*job.b)[2][i], 0}};
  }
}

void FpgaBackend::unpack(const std::vector<abi::Beat>& output, Rns& out) {
  for (auto& poly : out) poly.resize(degree);
  for (size_t i = 0; i < degree; i++) {
    require(output[i].limb[3] == 0, "DMA nonzero padding");
    for (size_t l = 0; l < limbs; l++) out[l][i] = output[i].limb[l];
  }
  validate(out);
}

void FpgaBackend::submit(std::vector<abi::Beat>& input, std::vector<abi::Beat>& output) {
  abi::Request request{};
  request.op = abi::multiply;
  request.input_words = abi::input_words(abi::multiply);
  request.output_words = abi::output_words;
  request.input_ptr = reinterpret_cast<uint64_t>(input.data());
  request.output_ptr = reinterpret_cast<uint64_t>(output.data());
  request.timeout_ms = timeout_ms_;
  const int result = device_.submit(request);
  if (result < 0 || request.status != 0)
    throw std::runtime_error("DMA failure (" + std::to_string(result < 0 ? result : request.status) +
                             "): stop; no retry/reset");
  stats_.device_reported_us += request.elapsed_us;
}

void FpgaBackend::multiply_batch(const std::vector<MulJob>& jobs) {
  validate_jobs(jobs);
  if (jobs.empty()) return;
  // After any DMA or output failure this backend never submits again (V4 policy: stop,
  // no retry, no reset). The latch also stops the other pipeline stage sharing it.
  if (stopped_) throw std::runtime_error("FPGA backend stopped after an earlier DMA/output failure: no further requests");
  try {
    run_validated(jobs);
  } catch (...) {
    stopped_ = true;
    throw;
  }
}

void FpgaBackend::run_validated(const std::vector<MulJob>& jobs) {
  const auto started = Clock::now();
  uint64_t pack_ns = 0, device_ns = 0, unpack_ns = 0;
  const auto timed = [](uint64_t& sink, const auto& step) {
    const auto begin = Clock::now();
    step();
    sink += elapsed_ns(begin);
  };
  const size_t count = jobs.size();

  if (!overlap_ || count == 1) {
    for (const auto& job : jobs) {
      timed(pack_ns, [&] { pack(job, input_[0]); });
      timed(device_ns, [&] { submit(input_[0], output_[0]); });
      timed(unpack_ns, [&] { unpack(output_[0], *job.out); });
    }
  } else {
    // Job j uses buffer pair j % 2. While job j is in the driver, the helper fills the
    // other input buffer (job j+1) and drains the other output buffer (job j-1).
    timed(pack_ns, [&] { pack(jobs[0], input_[0]); });
    for (size_t j = 0; j < count; j++) {
      uint64_t helper_pack = 0, helper_unpack = 0;
      auto prepared = helper_.submit([&, j] {
        if (j + 1 < count) timed(helper_pack, [&] { pack(jobs[j + 1], input_[(j + 1) % 2]); });
        if (j > 0) timed(helper_unpack, [&] { unpack(output_[(j - 1) % 2], *jobs[j - 1].out); });
      });
      try {
        timed(device_ns, [&] { submit(input_[j % 2], output_[j % 2]); });
      } catch (...) {
        prepared.wait();  // never return while the helper still touches our buffers
        throw;
      }
      prepared.get();  // rethrows a padding/canonical failure of job j-1
      pack_ns += helper_pack;
      unpack_ns += helper_unpack;
    }
    timed(unpack_ns, [&] { unpack(output_[(count - 1) % 2], *jobs[count - 1].out); });
  }

  stats_.batches++;
  stats_.jobs += count;
  stats_.pack_ns += pack_ns;
  stats_.device_ns += device_ns;
  stats_.unpack_ns += unpack_ns;
  stats_.bytes_to_device += count * abi::input_words(abi::multiply) * sizeof(abi::Beat);
  stats_.bytes_from_device += count * abi::output_words * sizeof(abi::Beat);
  stats_.wall_ns += elapsed_ns(started);
}
}  // namespace edgefhe::v5
