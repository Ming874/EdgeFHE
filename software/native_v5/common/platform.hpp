// Read-only facts about the machine and process, so measurements from the two groups
// are comparable: CPU affinity (the "fixed cores" fairness rule), process CPU time
// (utilization), cpufreq settings and hwmon power sensors (records per joule).
// Nothing here writes system settings, needs root or touches the DMA device.
#pragma once
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace edgefhe::v5 {
std::vector<unsigned> parse_cpu_list(const std::string& text);  // "0-3,5" -> {0,1,2,3,5}
std::string format_cpu_list(const std::vector<unsigned>& cpus);
std::vector<unsigned> current_affinity();
// Pins the calling thread; threads it creates afterwards inherit the set. Call before
// building thread pools to pin the whole measurement.
void pin_calling_thread(const std::vector<unsigned>& cpus);
unsigned online_cpus();
uint64_t process_cpu_ns();  // user + system time of every thread so far

struct CpuFreq {
  unsigned cpu = 0;
  std::string governor;  // empty when cpufreq is not exposed (for example WSL)
  int64_t cur_khz = -1, min_khz = -1, max_khz = -1;
};
std::vector<CpuFreq> read_cpufreq();

struct MachineInfo {
  std::string system, release, version, machine, cpu_model, compiler;
  unsigned online_cpus = 0;
};
MachineInfo machine_info();

// hwmon power*_input files (microwatts). On a board without them the list is empty and
// results report power as unavailable instead of guessing.
struct PowerSensor {
  std::string label;  // "<hwmon name>/powerN"
  std::string path;
};
std::vector<PowerSensor> find_power_sensors(const std::string& root = "/sys/class/hwmon");

struct PowerReading {
  std::string label;
  size_t samples = 0;
  double mean_w = 0, min_w = 0, max_w = 0;
};

// Samples every sensor at a fixed interval on one background thread between start()
// and stop(). Unreadable samples are skipped and counted.
class PowerSampler {
 public:
  PowerSampler(std::vector<PowerSensor> sensors, unsigned interval_ms);
  ~PowerSampler();
  PowerSampler(const PowerSampler&) = delete;
  PowerSampler& operator=(const PowerSampler&) = delete;

  bool empty() const { return sensors_.empty(); }
  void start();
  std::vector<PowerReading> stop();
  size_t failed_reads() const { return failed_; }

 private:
  void sample_once();
  std::vector<PowerSensor> sensors_;
  unsigned interval_ms_;
  std::vector<PowerReading> readings_;
  std::vector<double> sums_;
  std::mutex mutex_;
  std::condition_variable wake_;
  bool running_ = false;
  size_t failed_ = 0;
  std::thread thread_;
};
}  // namespace edgefhe::v5
