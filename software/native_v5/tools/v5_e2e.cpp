// End-to-end comparison harness for the two groups (docs/COMPARISON.md). Same
// records, model, keys and timed steps for every configuration; only the backend
// differs: cpu-wN (group A) or fpga-serial / fpga-overlap (group B).
//   correctness  every record through every backend: CKKS logits vs plaintext, decisions,
//                and SHA-256 over all ciphertexts and decrypted coefficients, which must
//                be identical across backends (each group is the other's oracle).
//   latency      one batch (2048 records by default), steps 1..10 timed, the backends in
//                a rotating order each round so drift affects all of them alike.
//   throughput   the dataset as a stream of batches, serial (capacity 0) and pipelined,
//                with wall time, CPU time and hwmon power when the board exposes it.
// With --fd the FPGA rows use the operator's gated descriptor; this tool never opens,
// resets or retries the device. Without --fd they use SimulatedDmaDevice, whose times
// are CPU times, not FPGA times, and the result says so.
#include "backend/factory.hpp"
#include "common/json.hpp"
#include "common/platform.hpp"
#include "common/sha256.hpp"
#include "edge/qa_random.hpp"
#include "edge/runner.hpp"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <memory>
#include <sys/stat.h>

using namespace edgefhe;
using namespace edgefhe::v5;

namespace {
const char* usage =
    "usage: v5_e2e --data CSV --preprocess JSON --model JSON --output NEW.json|-\n"
    "  [--backends cpu-w1,cpu-w2,cpu-w3,cpu-w4,fpga-serial,fpga-overlap] [--fd N]\n"
    "  [--modes correctness,latency,throughput] [--limit N] [--batch-records 2048]\n"
    "  [--rounds 30] [--warmup 3] [--repeats 3] [--capacity 0,2] [--stream-batches 1,2,4,all]\n"
    "  [--cloud-workers 1|3] [--encode-workers 1..4] [--cpus 0-3] [--power auto|off]\n"
    "  [--power-interval-ms 20] [--qa-seed S] [--label TEXT]\n";

struct Options {
  std::string data, preprocess, model, output, label;
  std::vector<std::string> backends = {"cpu-w1", "cpu-w2", "cpu-w3", "cpu-w4", "fpga-serial", "fpga-overlap"};
  std::vector<std::string> modes = {"correctness", "latency", "throughput"};
  int fd = -1;
  size_t limit = 0, batch_records = slots, rounds = 30, warmup = 3, repeats = 3;
  std::vector<size_t> capacities = {0, 2}, stream_batches;  // empty stream_batches = all
  unsigned cloud_workers = 1;
  unsigned encode_workers = 1;  // step 2 threads, the same for every backend in a run
  std::string cpus;
  bool power = true;
  unsigned power_interval_ms = 20;
  bool qa = false;
  uint64_t qa_seed = 0;
};

size_t to_size(const std::string& text, const char* what) {
  require(!text.empty() && text.size() <= 18 &&
              std::all_of(text.begin(), text.end(), [](unsigned char c) { return c >= '0' && c <= '9'; }),
          (std::string(what) + " must be a non-negative integer").c_str());
  return size_t(std::stoull(text));
}

// Range-checked before any narrowing cast, so an oversized value is an error, not a wrap.
size_t to_bounded(const std::string& text, const char* what, size_t low, size_t high) {
  const size_t value = to_size(text, what);
  require(value >= low && value <= high,
          (std::string(what) + " must be " + std::to_string(low) + ".." + std::to_string(high)).c_str());
  return value;
}

std::vector<size_t> to_sizes(const std::string& text, const char* what) {
  std::vector<size_t> out;
  for (const auto& item : split_list(text)) out.push_back(to_size(item, what));
  return out;
}

Options parse(int argc, char** argv) {
  Options o;
  for (int i = 1; i < argc; i++) {
    const std::string arg = argv[i];
    const auto next = [&]() -> std::string {
      require(i + 1 < argc, ("missing value after " + arg).c_str());
      return argv[++i];
    };
    if (arg == "--data") o.data = next();
    else if (arg == "--preprocess") o.preprocess = next();
    else if (arg == "--model") o.model = next();
    else if (arg == "--output") o.output = next();
    else if (arg == "--label") o.label = next();
    else if (arg == "--backends") o.backends = split_list(next());
    else if (arg == "--modes") o.modes = split_list(next());
    else if (arg == "--fd") o.fd = int(to_bounded(next(), "--fd", 0, 1 << 20));
    else if (arg == "--limit") o.limit = to_size(next(), "--limit");
    else if (arg == "--batch-records") o.batch_records = to_bounded(next(), "--batch-records", 1, slots);
    else if (arg == "--rounds") o.rounds = to_size(next(), "--rounds");
    else if (arg == "--warmup") o.warmup = to_size(next(), "--warmup");
    else if (arg == "--repeats") o.repeats = to_size(next(), "--repeats");
    else if (arg == "--capacity") o.capacities = to_sizes(next(), "--capacity");
    else if (arg == "--stream-batches") {
      o.stream_batches.clear();  // "all" = every batch of the dataset
      for (const auto& item : split_list(next()))
        o.stream_batches.push_back(item == "all" ? SIZE_MAX : to_size(item, "--stream-batches"));
    } else if (arg == "--cloud-workers") o.cloud_workers = unsigned(to_bounded(next(), "--cloud-workers", 1, 3));
    else if (arg == "--encode-workers") o.encode_workers = unsigned(to_bounded(next(), "--encode-workers", 1, 4));
    else if (arg == "--cpus") o.cpus = next();
    else if (arg == "--power") {
      const auto value = next();
      require(value == "auto" || value == "off", "--power must be auto or off");
      o.power = value == "auto";
    } else if (arg == "--power-interval-ms") o.power_interval_ms = unsigned(to_bounded(next(), "--power-interval-ms", 1, 10000));
    else if (arg == "--qa-seed") {
      o.qa = true;
      o.qa_seed = to_size(next(), "--qa-seed");
    } else throw std::invalid_argument("unknown argument " + arg + "\n" + usage);
  }
  require(!o.data.empty() && !o.preprocess.empty() && !o.model.empty() && !o.output.empty(),
          "--data, --preprocess, --model and --output are required");
  for (const auto& mode : o.modes)
    require(mode == "correctness" || mode == "latency" || mode == "throughput",
            ("unknown mode " + mode).c_str());
  require(o.rounds >= 1 && o.rounds <= 10000 && o.warmup <= 1000 && o.repeats >= 1 && o.repeats <= 100,
          "Invalid --rounds/--warmup/--repeats");
  for (const size_t c : o.capacities) require(c <= 64, "--capacity must be 0..64 (0 = serial)");
  for (const size_t k : o.stream_batches) require(k >= 1, "--stream-batches items must be >= 1");
  require(o.cloud_workers == 1 || o.cloud_workers == 3, "--cloud-workers must be 1 or 3");
  return o;
}

double percentile_us(std::vector<uint64_t> values, double p) {
  if (values.empty()) return 0;
  std::sort(values.begin(), values.end());
  const size_t index = std::min(values.size() - 1, size_t(p * double(values.size() - 1) + 0.5));
  return double(values[index]) / 1e3;
}

double mean_us(const std::vector<uint64_t>& values) {
  if (values.empty()) return 0;
  double sum = 0;
  for (const auto v : values) sum += double(v);
  return sum / double(values.size()) / 1e3;
}

void write_latency(JsonWriter& w, const std::vector<uint64_t>& values) {
  w.begin_object()
      .key("p50_us").value(percentile_us(values, 0.5))
      .key("p95_us").value(percentile_us(values, 0.95))
      .key("mean_us").value(mean_us(values))
      .end_object();
}

void write_stats(JsonWriter& w, const PhaseStats& s) {
  w.begin_object()
      .key("batches").value(s.batches)
      .key("jobs").value(s.jobs)
      .key("wall_ms").value(double(s.wall_ns) / 1e6)
      .key("pack_ms").value(double(s.pack_ns) / 1e6)
      .key("device_ms").value(double(s.device_ns) / 1e6)
      .key("unpack_ms").value(double(s.unpack_ns) / 1e6)
      .key("driver_reported_ms").value(double(s.device_reported_us) / 1e3)
      .key("bytes_to_device").value(s.bytes_to_device)
      .key("bytes_from_device").value(s.bytes_from_device)
      .key("forward_transforms").value(s.forward_transforms)
      .key("inverse_transforms").value(s.inverse_transforms)
      .end_object();
}

void write_accuracy(JsonWriter& w, const Accuracy& a) {
  w.key("records").value(a.records)
      .key("max_abs_logit_diff").value(a.max_abs_diff)
      .key("mean_abs_logit_diff").value(a.mean_abs_diff)
      .key("all_within_bound").value(a.within_bound)
      .key("decisions").begin_object()
      .key("agree").value(a.agree)
      .key("disagree").value(a.disagree)
      .key("uncertain").value(a.uncertain)
      .end_object();
  if (a.labelled) {
    const double certain = double(a.tp + a.tn + a.fp + a.fn);
    w.key("labels").begin_object()
        .key("auc_ckks").value(a.auc)
        .key("auc_plain").value(a.plain_auc)
        .key("accuracy_certain").value(certain ? double(a.tp + a.tn) / certain : 0.0)
        .key("confusion").begin_object()
        .key("tn").value(a.tn).key("fp").value(a.fp).key("fn").value(a.fn).key("tp").value(a.tp)
        .end_object()
        .end_object();
  }
}

std::string join(const std::vector<std::string>& items) {
  std::string out;
  for (const auto& item : items) out += (out.empty() ? "" : ",") + item;
  return out;
}

bool file_exists(const std::string& path) {
  struct stat info{};
  return ::stat(path.c_str(), &info) == 0;
}
}  // namespace

int main(int argc, char** argv) {
  Options o;
  try {
    o = parse(argc, argv);
    require(o.output == "-" || !file_exists(o.output), "--output exists: use a new file and keep earlier results");
  } catch (const std::exception& e) {
    std::cerr << "FAIL: " << e.what() << "\n";
    return 2;
  }

  // With --output - the JSON goes to stdout and progress lines to stderr.
  std::ostream& log = o.output == "-" ? std::cerr : std::cout;
  JsonWriter w;
  std::string failure;
  bool pass = true;
  std::vector<std::string> notes;
  try {
    if (!o.cpus.empty()) pin_calling_thread(parse_cpu_list(o.cpus));  // before any thread exists
    const auto affinity = current_affinity();
    const auto machine = machine_info();
    const auto cpufreq = read_cpufreq();
    const auto sensors = o.power ? find_power_sensors() : std::vector<PowerSensor>{};

    const Model model = load_model(o.model);
    const Preprocess pre = load_preprocess(o.preprocess, model);
    const Dataset data = load_csv(o.data, pre, o.limit);
    const CloudAffine cloud(model, o.cloud_workers);

    bool any_fpga = false;
    for (const auto& spec : o.backends) any_fpga = any_fpga || backend_needs_device(spec);
    std::unique_ptr<DmaDevice> device;
    if (any_fpga) {
      if (o.fd >= 0) device = std::make_unique<LinuxDmaDevice>(o.fd);
      else device = std::make_unique<SimulatedDmaDevice>();
    }
    std::vector<std::unique_ptr<PolyBackend>> backends;
    for (const auto& spec : o.backends) backends.push_back(make_backend(spec, device.get()));

    const Material material = o.qa ? qa_material(o.qa_seed) : sample_material();
    std::vector<std::unique_ptr<EdgeSession>> sessions;
    for (auto& backend : backends)
      sessions.push_back(std::make_unique<EdgeSession>(*backend, model, pre, cloud, data, material, o.encode_workers));
    const double bound = sessions.front()->error_bound();

    w.begin_object()
        .key("schema").value("edgefhe-v5-e2e-1")
        .key("tool").value("v5_e2e")
        .key("label").value(o.label);
    w.key("environment").begin_object()
        .key("system").value(machine.system)
        .key("release").value(machine.release)
        .key("machine").value(machine.machine)
        .key("cpu_model").value(machine.cpu_model)
        .key("online_cpus").value(machine.online_cpus)
        .key("affinity").value(format_cpu_list(affinity))
        .key("pinned").value(!o.cpus.empty())
        .key("compiler").value(machine.compiler);
    w.key("cpufreq").begin_array();
    for (const auto& f : cpufreq)
      w.begin_object()
          .key("cpu").value(f.cpu)
          .key("governor").value(f.governor)
          .key("cur_khz").value(f.cur_khz)
          .key("min_khz").value(f.min_khz)
          .key("max_khz").value(f.max_khz)
          .end_object();
    w.end_array();
    w.key("power_sensors").begin_array();
    for (const auto& s : sensors) w.value(s.label);
    w.end_array().end_object();

    w.key("inputs").begin_object()
        .key("data").value(o.data)
        .key("data_sha256").value(sha256_file(o.data))
        .key("model").value(o.model)
        .key("model_sha256").value(sha256_file(o.model))
        .key("preprocess").value(o.preprocess)
        .key("preprocess_sha256").value(sha256_file(o.preprocess))
        .key("model_id").value(model.model_id)
        .key("records").value(data.raw.size())
        .key("limit").value(o.limit)
        .key("labelled").value(!data.labels.empty());
    w.key("features").begin_array();
    for (const auto& f : pre.features) w.value(f);
    w.end_array().end_object();

    w.key("profile").begin_object()
        .key("degree").value(degree)
        .key("slots").value(slots)
        .key("primes").begin_array().value(primes[0]).value(primes[1]).value(primes[2]).end_array()
        .key("input_scale_bits").value(29)
        .key("output_scale_bits").value(58)
        .key("sampler").value("fixed-weight-ternary-s64-u64-e16")
        .key("security_bits").null()
        .key("error_bound").value(bound)
        .end_object();

    w.key("device").begin_object()
        .key("name").value(device ? device->name() : std::string("none"))
        .key("device_times_are_fpga").value(o.fd >= 0)
        .key("opened_by_tool").value(false)
        .end_object();
    w.key("randomness").value(o.qa ? "qa-seed:" + std::to_string(o.qa_seed) + " (public seed; QA only)"
                                   : std::string("os-getrandom"));
    w.key("settings").begin_object()
        .key("backends").value(join(o.backends))
        .key("modes").value(join(o.modes))
        .key("batch_records").value(o.batch_records)
        .key("rounds").value(o.rounds)
        .key("warmup").value(o.warmup)
        .key("repeats").value(o.repeats)
        .key("cloud_workers").value(o.cloud_workers)
        .key("encode_workers").value(o.encode_workers)
        .key("cloud").value("in-process CPU (V4 Engine::affine); identical for every backend")
        .key("power_interval_ms").value(o.power_interval_ms)
        .end_object();

    w.key("keygen").begin_array();
    for (size_t b = 0; b < sessions.size(); b++)
      w.begin_object()
          .key("backend").value(o.backends[b])
          .key("keygen_ms").value(double(sessions[b]->keygen_ns()) / 1e6)
          .key("public_key_sha256").value(key_digest(sessions[b]->public_key()))
          .end_object();
    w.end_array();
    const std::string key0 = key_digest(sessions.front()->public_key());
    for (const auto& s : sessions)
      if (key_digest(s->public_key()) != key0) {
        pass = false;
        notes.push_back("public keys differ across backends");
      }

    const auto mode_on = [&](const char* mode) {
      return std::find(o.modes.begin(), o.modes.end(), mode) != o.modes.end();
    };

    if (mode_on("correctness")) {
      log << "correctness: " << data.raw.size() << " records x " << o.backends.size() << " backends\n"
                << std::flush;
      auto plan = plan_batches(data.raw.size(), o.batch_records);
      for (auto& batch : plan) {  // one set of randomness, shared by every backend
        batch.samples = o.qa ? qa_samples(o.qa_seed, batch.index) : sample_encryption();
        batch.presampled = true;
      }
      std::string cipher0, plain0;
      bool exact = true;
      w.key("correctness").begin_object().key("backends").begin_array();
      for (size_t b = 0; b < sessions.size(); b++) {
        auto run = run_batches(*sessions[b], plan, 0);
        const auto accuracy = check_accuracy(run.batches, data, model, pre, bound);
        const auto cipher = cipher_digest(run.batches), plain = decrypted_digest(run.batches);
        if (b == 0) {
          cipher0 = cipher;
          plain0 = plain;
        }
        exact = exact && cipher == cipher0 && plain == plain0;
        const bool ok = accuracy.within_bound && accuracy.disagree == 0;
        pass = pass && ok;
        w.begin_object().key("backend").value(o.backends[b]).key("name").value(backends[b]->name());
        write_accuracy(w, accuracy);
        w.key("cipher_sha256").value(cipher)
            .key("decrypted_sha256").value(plain)
            .key("pass").value(ok)
            .end_object();
        log << "  " << o.backends[b] << ": max|diff| " << accuracy.max_abs_diff << " disagree "
                  << accuracy.disagree << " uncertain " << accuracy.uncertain << " cipher " << cipher.substr(0, 12)
                  << "\n" << std::flush;
      }
      w.end_array()
          .key("batches").value(plan.size())
          .key("bit_exact_across_backends").value(exact)
          .end_object();
      pass = pass && exact;
      if (!exact) notes.push_back("backends disagree bit for bit");
    }

    if (mode_on("latency")) {
      log << "latency: " << o.warmup << "+" << o.rounds << " rounds\n" << std::flush;
      const size_t count = std::min(o.batch_records, data.raw.size());
      std::vector<std::array<std::vector<uint64_t>, step_count>> steps(sessions.size());
      std::vector<std::vector<uint64_t>> total(sessions.size()), edge(sessions.size());
      for (size_t round = 0; round < o.warmup + o.rounds; round++) {
        for (size_t k = 0; k < sessions.size(); k++) {
          const size_t b = (round + k) % sessions.size();
          if (round == o.warmup) backends[b]->reset_stats();
          auto plan = plan_batches(count, o.batch_records, 1);
          if (o.qa) {
            plan[0].samples = qa_samples(o.qa_seed, 1000000 + round);
            plan[0].presampled = true;
          }
          const auto run = run_batches(*sessions[b], std::move(plan), 0);
          if (round < o.warmup) continue;
          const auto& batch = run.batches.front();
          uint64_t sum = 0;
          for (size_t s = 0; s < step_count; s++) {
            steps[b][s].push_back(batch.ns[s]);
            sum += batch.ns[s];
          }
          total[b].push_back(run.wall_ns);
          edge[b].push_back(sum - batch.ns[size_t(Step::cloud)]);
          const auto accuracy = check_accuracy(run.batches, data, model, pre, bound);
          if (!accuracy.within_bound) {
            pass = false;
            notes.push_back("latency run outside the error bound: " + o.backends[b]);
          }
        }
      }
      w.key("latency").begin_object()
          .key("records_per_batch").value(count)
          .key("timed_region").value("steps 1-10 for one batch; keygen excluded; cloud in-process");
      w.key("results").begin_array();
      for (size_t b = 0; b < sessions.size(); b++) {
        w.begin_object().key("backend").value(o.backends[b]).key("name").value(backends[b]->name());
        w.key("steps").begin_array();
        for (size_t s = 0; s < step_count; s++) {
          w.begin_object().key("step").value(step_name(s)).key("slide").value(step_slide(s)).key("time");
          write_latency(w, steps[b][s]);
          w.end_object();
        }
        w.end_array();
        w.key("total");
        write_latency(w, total[b]);
        w.key("edge_only");
        write_latency(w, edge[b]);
        w.key("backend_stats");
        write_stats(w, backends[b]->stats());
        w.end_object();
        log << "  " << o.backends[b] << ": total p50 " << percentile_us(total[b], 0.5) / 1e3
                  << " ms, encrypt p50 " << percentile_us(steps[b][size_t(Step::encrypt)], 0.5) / 1e3 << " ms\n"
                  << std::flush;
      }
      w.end_array().end_object();
    }

    if (mode_on("throughput")) {
      const auto all = plan_batches(data.raw.size(), o.batch_records);
      auto lengths = o.stream_batches;
      if (lengths.empty()) lengths.push_back(all.size());
      for (auto& k : lengths) k = std::min(k, all.size());
      std::sort(lengths.begin(), lengths.end());
      lengths.erase(std::unique(lengths.begin(), lengths.end()), lengths.end());
      PowerSampler sampler(sensors, o.power_interval_ms);
      const size_t cpus = affinity.size();
      log << "throughput: " << all.size() << " batches available, " << o.repeats << " repeats\n" << std::flush;
      w.key("throughput").begin_object()
          .key("batch_records").value(o.batch_records)
          .key("cpus_for_utilization").value(cpus)
          .key("power_available").value(!sensors.empty());
      w.key("results").begin_array();
      for (size_t b = 0; b < sessions.size(); b++)
        for (const size_t capacity : o.capacities)
          for (const size_t length : lengths) {
            // One entry per repeat; the median-wall repeat is reported, with its own backend
            // statistics (not the sum over repeats) and the stages' own work time.
            struct Repeat {
              RunResult run;
              PhaseStats stats;
              std::array<uint64_t, 5> work_ns{};
            };
            std::vector<Repeat> runs;
            for (size_t r = 0; r < o.repeats; r++) {
              auto plan = plan_batches(data.raw.size(), o.batch_records, length);
              if (o.qa)
                for (auto& batch : plan) {
                  batch.samples = qa_samples(o.qa_seed, 2000000 + batch.index);
                  batch.presampled = true;
                }
              backends[b]->reset_stats();
              Repeat repeat;
              repeat.run = run_batches(*sessions[b], std::move(plan), capacity, sampler.empty() ? nullptr : &sampler);
              repeat.stats = backends[b]->stats();
              const auto accuracy = check_accuracy(repeat.run.batches, data, model, pre, bound);
              if (!accuracy.within_bound) {
                pass = false;
                notes.push_back("throughput run outside the error bound: " + o.backends[b]);
              }
              for (const auto& batch : repeat.run.batches) {
                const auto& ns = batch.ns;
                repeat.work_ns[0] += ns[size_t(Step::standardize)] + ns[size_t(Step::encode)] + ns[size_t(Step::sample)];
                repeat.work_ns[1] += ns[size_t(Step::encrypt)];
                repeat.work_ns[2] += ns[size_t(Step::cloud)];
                repeat.work_ns[3] += ns[size_t(Step::decrypt)];
                repeat.work_ns[4] += ns[size_t(Step::decode)];
              }
              repeat.run.batches.clear();  // results checked; keep memory flat
              runs.push_back(std::move(repeat));
            }
            std::sort(runs.begin(), runs.end(),
                      [](const Repeat& x, const Repeat& y) { return x.run.wall_ns < y.run.wall_ns; });
            const auto& median = runs[runs.size() / 2];
            size_t records = 0;
            for (size_t i = 0; i < length; i++) records += all[i].count;
            const double seconds = double(median.run.wall_ns) / 1e9;
            w.begin_object()
                .key("backend").value(o.backends[b])
                .key("name").value(backends[b]->name())
                .key("pipeline").value(capacity == 0 ? "serial" : "pipelined")
                .key("capacity").value(capacity)
                .key("batches").value(length)
                .key("records").value(records);
            w.key("wall_ms_repeats").begin_array();
            for (const auto& repeat : runs) w.value(double(repeat.run.wall_ns) / 1e6);
            w.end_array();
            w.key("wall_ms").value(seconds * 1e3)
                .key("records_per_s").value(double(records) / seconds)
                .key("batches_per_s").value(double(length) / seconds)
                .key("cpu_utilization").value(double(median.run.cpu_ns) / (double(median.run.wall_ns) * double(cpus)));
            // busy_ms: time the stage's thread spent inside its stage function, including
            // waiting for the backend shared by encrypt and decrypt. work_ms: the steps' own
            // time with that wait excluded.
            w.key("stages").begin_array();
            for (size_t s = 0; s < median.run.pipeline.names.size(); s++) {
              w.begin_object()
                  .key("stage").value(median.run.pipeline.names[s])
                  .key("busy_ms").value(double(median.run.pipeline.busy_ns[s]) / 1e6);
              if (s < median.work_ns.size()) w.key("work_ms").value(double(median.work_ns[s]) / 1e6);
              if (s < median.run.pipeline.queue_high_water.size())
                w.key("queue_high_water_after").value(median.run.pipeline.queue_high_water[s]);
              w.end_object();
            }
            w.end_array();
            // Energy only from enough real samples; otherwise null, never a made-up 0.
            w.key("power").begin_array();
            for (const auto& p : median.run.power) {
              const bool usable = p.samples >= 5 && p.mean_w > 0;
              const double joules = p.mean_w * seconds;
              w.begin_object().key("sensor").value(p.label).key("samples").value(p.samples);
              if (usable) {
                w.key("mean_w").value(p.mean_w)
                    .key("min_w").value(p.min_w)
                    .key("max_w").value(p.max_w)
                    .key("energy_j").value(joules)
                    .key("records_per_j").value(double(records) / joules);
              } else {
                w.key("mean_w").null().key("energy_j").null().key("records_per_j").null()
                    .key("note").value("fewer than 5 readings or zero power: use a longer stream");
              }
              w.end_object();
            }
            w.end_array();
            w.key("backend_stats");
            write_stats(w, median.stats);
            w.end_object();
            log << "  " << o.backends[b] << (capacity ? " pipelined" : " serial") << " x" << length << ": "
                      << double(records) / seconds << " records/s\n" << std::flush;
          }
      w.end_array().end_object();
    }
  } catch (const std::exception& e) {
    pass = false;
    failure = e.what();
  }

  // A finished run gets its notes and final status; a failed run is replaced by a short
  // failure record (partial measurements are not kept, so they cannot be mistaken).
  std::string document;
  try {
    if (failure.empty()) {
      w.key("notes").begin_array();
      for (const auto& note : notes) w.value(note);
      w.end_array();
      w.key("final_status").value(pass ? "V5_E2E_PASS" : "V5_E2E_FAIL").end_object();
      document = w.str();
    }
  } catch (const std::exception& e) {
    failure = std::string("result writer: ") + e.what();
  }
  if (!failure.empty()) {
    JsonWriter f;
    f.begin_object()
        .key("schema").value("edgefhe-v5-e2e-1")
        .key("final_status").value("V5_E2E_FAIL")
        .key("failure").value(failure)
        .end_object();
    document = f.str();
  }
  if (o.output == "-") {
    std::cout << document << std::flush;
  } else {
    std::ofstream out(o.output, std::ios::out | std::ios::trunc);
    out << document;
    out.close();
    if (!out) {
      std::cerr << "FAIL: cannot write " << o.output << "\n";
      return 1;
    }
  }
  if (!failure.empty()) std::cerr << "FAIL: " << failure << "\n";
  log << (pass && failure.empty() ? "V5_E2E_PASS" : "V5_E2E_FAIL") << " output=" << o.output << "\n";
  return pass && failure.empty() ? 0 : 1;
}
