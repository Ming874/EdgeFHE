#include "common/platform.hpp"
#include "core.hpp"
#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <limits>
#include <sched.h>
#include <sys/resource.h>
#include <sys/utsname.h>
#include <unistd.h>

namespace edgefhe::v5 {
namespace {
std::string first_line(const std::string& path) {
  std::ifstream file(path);
  std::string line;
  if (file) std::getline(file, line);
  return line;
}

int64_t number_file(const std::string& path) {
  const std::string text = first_line(path);
  if (text.empty()) return -1;
  try {
    size_t used = 0;
    const long long value = std::stoll(text, &used);
    return used == text.size() ? int64_t(value) : -1;
  } catch (...) {
    return -1;
  }
}

unsigned parse_unsigned(const std::string& text) {
  require(!text.empty() && text.size() <= 4 &&
              std::all_of(text.begin(), text.end(), [](unsigned char c) { return c >= '0' && c <= '9'; }),
          "CPU list items must be small non-negative integers");
  return unsigned(std::stoul(text));
}
}  // namespace

std::vector<unsigned> parse_cpu_list(const std::string& text) {
  std::vector<unsigned> cpus;
  size_t begin = 0;
  while (begin <= text.size()) {
    const size_t end = std::min(text.find(',', begin), text.size());
    const std::string item = text.substr(begin, end - begin);
    const size_t dash = item.find('-');
    if (dash == std::string::npos) {
      cpus.push_back(parse_unsigned(item));
    } else {
      const unsigned low = parse_unsigned(item.substr(0, dash)), high = parse_unsigned(item.substr(dash + 1));
      require(low <= high, "CPU range must be ascending");
      for (unsigned cpu = low; cpu <= high; cpu++) cpus.push_back(cpu);
    }
    begin = end + 1;
  }
  std::sort(cpus.begin(), cpus.end());
  require(std::adjacent_find(cpus.begin(), cpus.end()) == cpus.end(), "CPU list repeats a CPU");
  require(cpus.back() < CPU_SETSIZE, "CPU number too large");
  return cpus;
}

std::string format_cpu_list(const std::vector<unsigned>& cpus) {
  std::string out;
  for (size_t i = 0; i < cpus.size();) {
    size_t j = i;
    while (j + 1 < cpus.size() && cpus[j + 1] == cpus[j] + 1) j++;
    if (!out.empty()) out += ',';
    out += std::to_string(cpus[i]);
    if (j > i) out += '-' + std::to_string(cpus[j]);
    i = j + 1;
  }
  return out;
}

std::vector<unsigned> current_affinity() {
  cpu_set_t set;
  CPU_ZERO(&set);
  require(sched_getaffinity(0, sizeof(set), &set) == 0, "sched_getaffinity failed");
  std::vector<unsigned> cpus;
  for (unsigned cpu = 0; cpu < CPU_SETSIZE; cpu++)
    if (CPU_ISSET(cpu, &set)) cpus.push_back(cpu);
  return cpus;
}

void pin_calling_thread(const std::vector<unsigned>& cpus) {
  require(!cpus.empty(), "Empty CPU set");
  cpu_set_t set;
  CPU_ZERO(&set);
  for (const unsigned cpu : cpus) CPU_SET(cpu, &set);
  require(sched_setaffinity(0, sizeof(set), &set) == 0,
          "sched_setaffinity failed: are these CPUs online and allowed?");
}

unsigned online_cpus() {
  const long count = sysconf(_SC_NPROCESSORS_ONLN);
  return count > 0 ? unsigned(count) : 1;
}

uint64_t process_cpu_ns() {
  rusage usage{};
  require(getrusage(RUSAGE_SELF, &usage) == 0, "getrusage failed");
  const auto ns = [](const timeval& t) { return uint64_t(t.tv_sec) * 1000000000ull + uint64_t(t.tv_usec) * 1000ull; };
  return ns(usage.ru_utime) + ns(usage.ru_stime);
}

std::vector<CpuFreq> read_cpufreq() {
  std::vector<CpuFreq> out;
  for (unsigned cpu = 0; cpu < online_cpus(); cpu++) {
    const std::string base = "/sys/devices/system/cpu/cpu" + std::to_string(cpu) + "/cpufreq/";
    CpuFreq f;
    f.cpu = cpu;
    f.governor = first_line(base + "scaling_governor");
    f.cur_khz = number_file(base + "scaling_cur_freq");
    f.min_khz = number_file(base + "scaling_min_freq");
    f.max_khz = number_file(base + "scaling_max_freq");
    out.push_back(f);
  }
  return out;
}

MachineInfo machine_info() {
  MachineInfo info;
  utsname name{};
  if (uname(&name) == 0) {
    info.system = name.sysname;
    info.release = name.release;
    info.version = name.version;
    info.machine = name.machine;
  }
  std::ifstream cpuinfo("/proc/cpuinfo");
  std::string line, implementer, part;
  while (std::getline(cpuinfo, line)) {
    const auto colon = line.find(':');
    if (colon == std::string::npos) continue;
    std::string key = line.substr(0, colon), value = line.substr(colon + 1);
    key.erase(key.find_last_not_of(" \t") + 1);
    value.erase(0, value.find_first_not_of(" \t"));
    if (key == "model name" && info.cpu_model.empty()) info.cpu_model = value;
    if (key == "CPU implementer" && implementer.empty()) implementer = value;
    if (key == "CPU part" && part.empty()) part = value;
  }
  if (info.cpu_model.empty() && !part.empty()) {
    info.cpu_model = "ARM implementer " + implementer + " part " + part;
    if (implementer == "0x41" && part == "0xd03") info.cpu_model += " (Cortex-A53)";
  }
#ifdef __VERSION__
  info.compiler = __VERSION__;
#endif
  info.online_cpus = online_cpus();
  return info;
}

std::vector<PowerSensor> find_power_sensors(const std::string& root) {
  namespace fs = std::filesystem;
  std::vector<PowerSensor> sensors;
  std::error_code error;
  if (!fs::is_directory(root, error)) return sensors;
  for (const auto& hwmon : fs::directory_iterator(root, error)) {
    const std::string name = first_line(hwmon.path().string() + "/name");
    std::vector<std::string> files;
    for (const auto& entry : fs::directory_iterator(hwmon.path(), error)) {
      const std::string file = entry.path().filename().string();
      if (file.rfind("power", 0) == 0 && file.size() > 11 && file.compare(file.size() - 6, 6, "_input") == 0)
        files.push_back(file);
    }
    std::sort(files.begin(), files.end());
    for (const auto& file : files)
      sensors.push_back({(name.empty() ? hwmon.path().filename().string() : name) + "/" + file.substr(0, file.size() - 6),
                         (hwmon.path() / file).string()});
  }
  std::sort(sensors.begin(), sensors.end(), [](const PowerSensor& a, const PowerSensor& b) { return a.label < b.label; });
  return sensors;
}

PowerSampler::PowerSampler(std::vector<PowerSensor> sensors, unsigned interval_ms)
    : sensors_(std::move(sensors)), interval_ms_(interval_ms) {
  require(interval_ms >= 1 && interval_ms <= 10000, "Power sampling interval must be 1..10000 ms");
}

PowerSampler::~PowerSampler() {
  if (thread_.joinable()) stop();
}

void PowerSampler::sample_once() {
  for (size_t i = 0; i < sensors_.size(); i++) {
    const int64_t microwatts = number_file(sensors_[i].path);
    if (microwatts < 0) {
      failed_++;
      continue;
    }
    const double watts = double(microwatts) / 1e6;
    auto& r = readings_[i];
    r.min_w = r.samples ? std::min(r.min_w, watts) : watts;
    r.max_w = r.samples ? std::max(r.max_w, watts) : watts;
    r.samples++;
    sums_[i] += watts;
  }
}

void PowerSampler::start() {
  require(!thread_.joinable(), "Power sampler already running");
  readings_.assign(sensors_.size(), {});
  sums_.assign(sensors_.size(), 0);
  failed_ = 0;
  for (size_t i = 0; i < sensors_.size(); i++) readings_[i].label = sensors_[i].label;
  if (sensors_.empty()) return;
  running_ = true;
  thread_ = std::thread([this] {
    std::unique_lock<std::mutex> lock(mutex_);
    while (running_) {
      sample_once();
      wake_.wait_for(lock, std::chrono::milliseconds(interval_ms_), [&] { return !running_; });
    }
  });
}

std::vector<PowerReading> PowerSampler::stop() {
  if (thread_.joinable()) {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      running_ = false;
    }
    wake_.notify_all();
    thread_.join();
    std::lock_guard<std::mutex> lock(mutex_);
    sample_once();  // closing sample, so very short runs still have two readings
  }
  for (size_t i = 0; i < readings_.size(); i++)
    if (readings_[i].samples) readings_[i].mean_w = sums_[i] / double(readings_[i].samples);
  return readings_;
}
}  // namespace edgefhe::v5
