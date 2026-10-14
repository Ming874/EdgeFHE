// Host-only V5 tests. Public QA seeds generate operands; key material comes from the
// accepted OS sampler or the public QA sampler. No device is opened. Every product and
// every edge step is compared bit for bit with the accepted V4 Engine, so V5 can only
// change scheduling and measurement, never arithmetic. Usage: v5_tests <tests/data>.
#include "backend/cpu/cpu_backend.hpp"
#include "backend/factory.hpp"
#include "backend/fpga/fpga_backend.hpp"
#include "ckks/client.hpp"
#include "common/json.hpp"
#include "common/platform.hpp"
#include "common/sha256.hpp"
#include "edge/qa_random.hpp"
#include "edge/runner.hpp"
#include "runtime/pipeline.hpp"
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <random>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>

using namespace edgefhe;
using namespace edgefhe::v5;

namespace {
int passed = 0;

void check(bool ok, const std::string& what) {
  if (!ok) throw std::runtime_error("check failed: " + what);
}

template <class F>
void expect_throw(F&& call, const std::string& fragment, const std::string& what) {
  try {
    call();
  } catch (const std::exception& e) {
    check(std::string(e.what()).find(fragment) != std::string::npos,
          what + " (got '" + e.what() + "')");
    return;
  }
  throw std::runtime_error("expected exception: " + what);
}

Rns random_rns(std::mt19937_64& rng) {
  Rns value;
  for (size_t l = 0; l < limbs; l++) {
    value[l].resize(degree);
    for (auto& x : value[l]) x = uint32_t(rng() % primes[l]);
  }
  return value;
}

std::array<Message, 4> random_messages(std::mt19937_64& rng) {
  std::uniform_real_distribution<double> slot(-4, 4);
  std::array<Message, 4> messages;
  for (auto& m : messages) {
    std::vector<std::complex<double>> values(slots);
    for (auto& v : values) v = {slot(rng), 0};
    m = encode(values);
  }
  return messages;
}

template <class F>
void test(const char* name, F&& body) {
  body();
  passed++;
  std::cout << "PASS " << name << "\n";
}

void abi_contract() {
  check(abi::input_words(abi::forward_ntt) == degree && abi::input_words(abi::inverse_ntt) == degree,
        "transform input length");
  check(abi::input_words(abi::multiply) == 2 * degree && abi::input_words(abi::add_two) == 2 * degree &&
            abi::input_words(abi::negate_add) == 2 * degree,
        "two-operand input length");
  check(abi::input_words(abi::add_three) == 3 * degree, "three-operand input length");
  check(abi::output_words == degree, "output length");
}

void thread_pool_fork_join() {
  for (unsigned workers = 1; workers <= 4; workers++) {
    ThreadPool pool(workers);
    for (int round = 0; round < 50; round++) {
      std::vector<uint64_t> squares(1000);
      pool.run(squares.size(), [&](size_t i) { squares[i] = uint64_t(i) * i; });
      uint64_t sum = 0;
      for (auto s : squares) sum += s;
      check(sum == 999ull * 1000 * 1999 / 6, "sum of squares with " + std::to_string(workers) + " workers");
    }
  }
}

void thread_pool_exception() {
  ThreadPool pool(4);
  expect_throw([&] {
    pool.run(100, [](size_t i) {
      if (i == 7) throw std::runtime_error("task seven failed");
    });
  }, "task seven", "first task exception is rethrown");
  std::atomic<int> count{0};
  pool.run(64, [&](size_t) { count++; });
  check(count == 64, "pool reusable after a failed run");
}

void cpu_backend_matches_v4() {
  std::mt19937_64 rng(5001);
  const Engine v4(1);
  for (unsigned workers = 1; workers <= 4; workers++) {
    CpuBackend backend(workers);
    for (int round = 0; round < 3; round++) {
      const auto a = random_rns(rng), b = random_rns(rng);
      Rns out;
      backend.multiply_batch({{&a, &b, &out}});
      check(out == v4.multiply(a, b), "CPU product equals V4 with " + std::to_string(workers) + " workers");
    }
  }
}

void cpu_backend_operand_reuse() {
  std::mt19937_64 rng(5002);
  const Engine v4(1);
  const auto pk_b = random_rns(rng), pk_a = random_rns(rng);
  std::array<Rns, 4> u;
  for (auto& x : u) x = random_rns(rng);
  std::array<Rns, 8> out;
  std::vector<MulJob> jobs;
  for (size_t f = 0; f < 4; f++) {
    jobs.push_back({&pk_b, &u[f], &out[2 * f]});
    jobs.push_back({&pk_a, &u[f], &out[2 * f + 1]});
  }
  CpuBackend backend(3);
  backend.multiply_batch(jobs);
  for (size_t f = 0; f < 4; f++) {
    check(out[2 * f] == v4.multiply(pk_b, u[f]) && out[2 * f + 1] == v4.multiply(pk_a, u[f]),
          "shared-operand batch equals individual products");
  }
  // Six distinct operands and eight products: 3*(6+8) = 42 transforms, as in V4 encrypt.
  check(backend.stats().forward_transforms == 18 && backend.stats().inverse_transforms == 24,
        "public key and masks transformed once per limb");
}

void backend_rejects_aliasing() {
  std::mt19937_64 rng(5003);
  auto a = random_rns(rng);
  const auto b = random_rns(rng);
  Rns out;
  CpuBackend backend(2);
  expect_throw([&] { backend.multiply_batch({{&a, &b, &a}}); }, "aliases", "output aliasing an input");
  expect_throw([&] { backend.multiply_batch({{&a, &b, &out}, {&b, &a, &out}}); }, "same output",
               "two jobs writing one output");
  auto bad = a;
  bad[1][3] = primes[1];
  expect_throw([&] { backend.multiply_batch({{&bad, &b, &out}}); }, "Noncanonical", "noncanonical operand");
}

void simulated_device_validation() {
  SimulatedDmaDevice device;
  std::vector<abi::Beat> input(2 * degree, abi::Beat{{0, 0, 0, 0}}), output(degree);
  const auto request = [&](uint32_t op, uint32_t words) {
    abi::Request r{};
    r.op = op;
    r.input_words = words;
    r.output_words = degree;
    r.input_ptr = reinterpret_cast<uint64_t>(input.data());
    r.output_ptr = reinterpret_cast<uint64_t>(output.data());
    r.timeout_ms = 1000;
    return r;
  };
  auto r = request(abi::multiply, 2 * degree);
  check(device.submit(r) == 0 && r.status == 0, "valid zero multiply accepted");
  r = request(abi::multiply, 2 * degree);
  r.reserved0 = 1;
  check(device.submit(r) == -EINVAL && r.status == -EINVAL, "reserved field rejected");
  r = request(abi::multiply, degree);
  check(device.submit(r) == -EINVAL, "wrong input length rejected");
  r = request(6, degree);
  check(device.submit(r) == -EINVAL, "unknown operation rejected");
  r = request(abi::multiply, 2 * degree);
  r.timeout_ms = abi::max_timeout_ms + 1;
  check(device.submit(r) == -EINVAL, "timeout above driver limit rejected");
  r = request(abi::multiply, 2 * degree);
  r.input_ptr = 0;
  check(device.submit(r) == -EFAULT, "null buffer rejected");
  input[9].limb[2] = primes[2];
  r = request(abi::multiply, 2 * degree);
  check(device.submit(r) == -EINVAL, "noncanonical input residue rejected");
  input[9].limb[2] = 0;
  input[10].limb[3] = 1;
  r = request(abi::multiply, 2 * degree);
  check(device.submit(r) == -EINVAL, "nonzero input padding rejected");
  input[10].limb[3] = 0;
  r = request(abi::forward_ntt, degree);
  check(device.submit(r) == -EOPNOTSUPP, "unmodelled operation reported, not faked");
}

void fpga_backend_matches_cpu() {
  std::mt19937_64 rng(5004);
  for (bool overlap : {false, true})
    for (size_t count : {1, 2, 3, 8}) {
      std::vector<Rns> a(count), b(count), fpga(count), cpu(count);
      std::vector<MulJob> fpga_jobs, cpu_jobs;
      for (size_t j = 0; j < count; j++) {
        a[j] = random_rns(rng);
        b[j] = random_rns(rng);
        fpga_jobs.push_back({&a[j], &b[j], &fpga[j]});
        cpu_jobs.push_back({&a[j], &b[j], &cpu[j]});
      }
      SimulatedDmaDevice device;
      FpgaBackend hardware(device, overlap);
      CpuBackend software(2);
      hardware.multiply_batch(fpga_jobs);
      software.multiply_batch(cpu_jobs);
      check(fpga == cpu, "FPGA path equals CPU, batch " + std::to_string(count));
      check(device.calls() == count && hardware.stats().jobs == count, "one DMA request per job");
      check(hardware.stats().bytes_to_device == count * 2 * degree * 16 &&
                hardware.stats().bytes_from_device == count * degree * 16,
            "transfer byte accounting");
    }
}

void fpga_fault_stops_batch() {
  std::mt19937_64 rng(5005);
  std::vector<Rns> a(8), b(8), out(8);
  std::vector<MulJob> jobs;
  for (size_t j = 0; j < 8; j++) {
    a[j] = random_rns(rng);
    b[j] = random_rns(rng);
    jobs.push_back({&a[j], &b[j], &out[j]});
  }
  for (bool overlap : {false, true}) {
    SimulatedDmaDevice device;
    device.inject(SimulatedDmaDevice::Fault::fail_call, 3);
    FpgaBackend backend(device, overlap);
    expect_throw([&] { backend.multiply_batch(jobs); }, "DMA failure", "device error stops the batch");
    check(device.calls() == 3, "no retry and no request after the failure");
    // The latch also refuses every later batch, e.g. the other pipeline stage sharing it.
    expect_throw([&] { backend.multiply_batch(jobs); }, "stopped", "later batches refused after a failure");
    check(device.calls() == 3 && backend.stopped(), "latched: no further DMA request");
  }
  for (auto fault : {SimulatedDmaDevice::Fault::nonzero_padding, SimulatedDmaDevice::Fault::noncanonical_output}) {
    for (bool overlap : {false, true}) {
      SimulatedDmaDevice device;
      device.inject(fault, 2);
      FpgaBackend backend(device, overlap);
      expect_throw([&] { backend.multiply_batch(jobs); },
                   fault == SimulatedDmaDevice::Fault::nonzero_padding ? "nonzero padding" : "Noncanonical",
                   "corrupt device output detected");
      const uint64_t calls = device.calls();
      expect_throw([&] { backend.multiply_batch(jobs); }, "stopped", "no batch after corrupt output");
      check(device.calls() == calls, "corrupt output also latches the backend");
    }
  }
}

void client_matches_v4_engine() {
  std::mt19937_64 rng(5006);
  const auto material = sample_material();
  const auto messages = random_messages(rng);
  const Engine v4(1);
  const auto key = v4.keygen(material);
  const auto cipher = v4.encrypt(key, messages, material.mask, material.e0, material.e1);
  SimulatedDmaDevice device;
  CpuBackend cpu(4);
  FpgaBackend fpga(device, true);
  for (PolyBackend* backend : std::initializer_list<PolyBackend*>{&cpu, &fpga}) {
    Client client(*backend);
    const auto v5_key = client.keygen(material);
    check(v5_key.b == key.b && v5_key.a == key.a, backend->name() + " keygen equals V4");
    const auto v5_cipher = client.encrypt(v5_key, messages, material.mask, material.e0, material.e1);
    for (size_t f = 0; f < 4; f++) {
      check(v5_cipher[f].c0 == cipher[f].c0 && v5_cipher[f].c1 == cipher[f].c1,
            backend->name() + " ciphertext equals V4");
      check(client.decrypt(v5_cipher[f], material.secret) == v4.decrypt(cipher[f], material.secret),
            backend->name() + " decryption equals V4");
    }
  }
  check(fpga.stats().batches == 6 && fpga.stats().jobs == 1 + 8 + 4, "encrypt is one batch of eight");
}

void client_round_trip_accuracy() {
  std::mt19937_64 rng(5007);
  std::uniform_real_distribution<double> slot(-4, 4);
  std::vector<std::complex<double>> values(slots);
  for (auto& v : values) v = {slot(rng), 0};
  std::array<Message, 4> messages;
  for (auto& m : messages) m = encode(values);
  const auto material = sample_material();
  CpuBackend backend(2);
  Client client(backend);
  const auto key = client.keygen(material);
  const auto cipher = client.encrypt(key, messages, material.mask, material.e0, material.e1);
  const auto restored = decode(client.decrypt(cipher[0], material.secret));
  double worst = 0;
  for (size_t i = 0; i < slots; i++) worst = std::max(worst, std::abs(restored[i] - values[i]));
  // Noise per coefficient is at most 16+16+1 = 33 for this sparse profile.
  check(worst < degree * 34.0 / scale, "decrypted slots within the noise bound, got " + std::to_string(worst));
}


void pipeline_order_and_backpressure() {
  std::vector<int> items(50);
  for (int i = 0; i < 50; i++) items[i] = i;
  const std::vector<Stage<int>> stages = {
      {"triple", [](int& x) { x *= 3; }},
      {"slow",
       [](int& x) {
         std::this_thread::sleep_for(std::chrono::microseconds(200));
         x += 1;
       }},
      {"last", [](int&) {}},
  };
  PipelineStats stats;
  const auto out = run_stages(items, stages, 2, &stats);
  check(out.size() == 50, "every item delivered");
  for (int i = 0; i < 50; i++) check(out[i] == i * 3 + 1, "output order preserved");
  check(stats.queue_high_water.size() == 2 && stats.queue_high_water[0] <= 2 && stats.queue_high_water[1] <= 2,
        "queues never exceed capacity");
  check(stats.names.size() == 3 && stats.busy_ns.size() == 3 && stats.items == 50, "per-stage statistics");
  check(run_stages(items, stages, 0) == out, "serial reference gives the same items");
}

void pipeline_exception() {
  std::vector<int> items(20, 1);
  std::atomic<int> started{0};
  const std::vector<Stage<int>> middle = {
      {"a", [](int&) {}},
      {"b",
       [&](int&) {
         if (++started == 5) throw std::runtime_error("stage two failed");
       }},
      {"c", [](int&) {}},
      {"d", [](int&) {}},
  };
  expect_throw([&] { run_stages(items, middle, 2); }, "stage two", "stage exception cancels and is rethrown");
  const std::vector<Stage<int>> last = {{"a", [](int&) {}},
                                        {"z", [](int&) { throw std::runtime_error("last stage failed"); }}};
  expect_throw([&] { run_stages(items, last, 1); }, "last stage", "exception in the caller's stage");
  expect_throw([&] { run_stages(items, last, 0); }, "last stage", "exception in serial mode");
}

void pipeline_overlaps_stages() {
  std::vector<int> items(10);
  const std::function<void(int&)> sleep = [](int&) { std::this_thread::sleep_for(std::chrono::milliseconds(15)); };
  PipelineStats stats;
  run_stages(items, std::vector<Stage<int>>{{"a", sleep}, {"b", sleep}, {"c", sleep}}, 2, &stats);
  uint64_t serial = 0;
  for (const auto busy : stats.busy_ns) serial += busy;
  check(stats.wall_ns < serial * 7 / 10, "three stages overlap in time");
}

std::string sample_dir;
std::string sample(const char* name) { return sample_dir + "/" + name; }

void write_file(const std::string& path, const std::string& text) {
  std::ofstream file(path, std::ios::binary);
  file << text;
  check(bool(file), "write " + path);
}

std::string temp_file(const std::string& text) {
  char path[] = "/tmp/edgefhe_v5_test_XXXXXX";
  const int fd = ::mkstemp(path);
  check(fd >= 0, "temporary file");
  ::close(fd);
  write_file(path, text);
  return path;
}

void json_strict_round_trip() {
  const auto v = parse_json(R"({"a": [1, -2.5e3, true, null], "b": "xé\n", "c": {}})");
  check(v.at("a").array("a").size() == 4 && v.at("a").array("a")[1].number("n") == -2500, "numbers and arrays");
  check(v.at("b").string("b") == "x\xc3\xa9\n", "string escapes and UTF-8");
  check(v.at("c").keys("c").empty() && v.find("missing") == nullptr, "empty object, absent key");
  for (const char* bad : {R"({"a":1,"a":2})", "[1,]", R"({"a":01})", "NaN", "[1e999]", "\"\x01\"", "{} x",
                          R"(["\ud800"])", ""})
    expect_throw([&] { parse_json(bad); }, "Invalid JSON", std::string("rejects ") + bad);
  JsonWriter w;
  w.begin_object()
      .key("pi").value(3.141592653589793)
      .key("tenth").value(0.1)
      .key("big").value(uint64_t(18446744073709551615ull))
      .key("list").begin_array().value(int64_t(-1)).value(true).null().value("q\"\n").end_array()
      .key("empty").begin_object().end_object()
      .end_object();
  const auto back = parse_json(w.str());
  check(back.at("pi").number("pi") == 3.141592653589793 && back.at("tenth").number("t") == 0.1, "doubles round-trip");
  check(back.at("list").array("l")[3].string("s") == "q\"\n", "escaped string round-trip");
  check(back.at("empty").keys("e").empty(), "empty object written");
  expect_throw([&] {
    JsonWriter x;
    x.begin_object();
    x.value(1);
  }, "without key", "value without key rejected");
  expect_throw([&] {
    JsonWriter x;
    x.begin_array();
    (void)x.str();
  }, "not complete", "unfinished document rejected");
}

void sha256_vectors() {
  check(sha256_hex("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "empty input");
  check(sha256_hex("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "abc");
  check(sha256_hex("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq") ==
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
        "two-block message");
  check(sha256_hex(std::string(1000000, 'a')) == "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0",
        "one million a");
  Sha256 split;
  split.update("ab", 2);
  split.update("c", 1);
  check(split.hex() == sha256_hex("abc"), "incremental update");
}

void platform_facts() {
  check(parse_cpu_list("0-3,5") == std::vector<unsigned>{0, 1, 2, 3, 5}, "ranges and single CPUs");
  check(format_cpu_list({0, 1, 2, 3, 5, 7, 8}) == "0-3,5,7-8", "compact CPU list");
  for (const char* bad : {"", "3-1", "1,1", "a", "0-", "1,,2"})
    expect_throw([&] { parse_cpu_list(bad); }, "CPU", std::string("rejects '") + bad + "'");
  check(!current_affinity().empty() && online_cpus() >= 1, "affinity readable");
  const uint64_t before = process_cpu_ns();
  volatile double sink = 0;
  for (int i = 0; i < 3000000; i++) sink = sink + std::sqrt(double(i));
  check(process_cpu_ns() >= before, "process CPU time is monotonic");
  check(!machine_info().machine.empty(), "machine name");
}

void power_sampler_fake_hwmon() {
  char root[] = "/tmp/edgefhe_v5_hwmon_XXXXXX";
  check(::mkdtemp(root) != nullptr, "temporary hwmon root");
  const std::string dir = std::string(root) + "/hwmon3";
  check(::mkdir(dir.c_str(), 0700) == 0, "hwmon directory");
  write_file(dir + "/name", "ina260\n");
  write_file(dir + "/power1_input", "1500000\n");
  write_file(dir + "/in1_input", "12000\n");
  const auto sensors = find_power_sensors(root);
  check(sensors.size() == 1 && sensors[0].label == "ina260/power1", "power sensor discovered, voltage ignored");
  PowerSampler sampler(sensors, 2);
  sampler.start();
  std::this_thread::sleep_for(std::chrono::milliseconds(20));
  const auto readings = sampler.stop();
  check(readings.size() == 1 && readings[0].samples >= 2 && std::abs(readings[0].mean_w - 1.5) < 1e-12 &&
            readings[0].min_w == 1.5 && readings[0].max_w == 1.5,
        "mean power in watts");
  check(find_power_sensors(std::string(root) + "/missing").empty(), "no hwmon directory, no sensors");
  PowerSampler none({}, 10);
  none.start();
  check(none.stop().empty(), "no sensors, no readings");
  for (const char* file : {"/name", "/power1_input", "/in1_input"}) std::remove((dir + file).c_str());
  ::rmdir(dir.c_str());
  ::rmdir(root);
}

void model_and_preprocess() {
  const auto model = load_model(sample("sample_model.json"));
  check(model.model_id == "v5-sample-lr4" && model.weights[2] == 0.5 && model.bias == -0.58, "sample model");
  const auto pre = load_preprocess(sample("sample_preprocess.json"), model);
  check(pre.features[3] == "x3" && pre.label == "y" && pre.clip == 16, "sample preprocessing");
  const auto z = standardize(pre, {2.5 + 1.07, 0.43, 28.4 + 1000, 8.0});
  check(std::abs(z[0] - 1) < 1e-12 && z[1] == 0 && z[2] == 16 && z[3] == 0, "standardize and clip");
  check(std::abs(plain_logit(model, z) - (0.7 + 0.5 * 16 - 0.58)) < 1e-12, "plaintext logit");
  const auto bad_model = [&](const std::string& text, const char* fragment) {
    const auto path = temp_file(text);
    expect_throw([&] { load_model(path); }, fragment, std::string("model rejected: ") + fragment);
    std::remove(path.c_str());
  };
  bad_model(R"({"model_id":"m","weights":[1,2,3],"bias":0,"input_abs_max":4,"max_logit_error":0.001})",
            "Four model weights");
  bad_model(R"({"model_id":"m","weights":[1,2,3,4],"bias":0,"input_abs_max":4,"max_logit_error":0.001,"x":1})",
            "unknown field");
  bad_model(R"({"model_id":"m","weights":[1,2,3,99],"bias":0,"input_abs_max":4,"max_logit_error":0.001})",
            "|value| <= 16");
  bad_model(R"({"model_id":"bad id","weights":[1,2,3,4],"bias":0,"input_abs_max":4,"max_logit_error":0.001})",
            "model ID");
  bad_model(R"({"model_id":"m","weights":[16,16,16,16],"bias":0,"input_abs_max":16,"max_logit_error":0.00001})",
            "error target");
  bad_model(R"({"model_id":"m","weights":[1,2,3,true],"bias":0,"input_abs_max":4,"max_logit_error":0.001})",
            "must be a number");
  const auto other = temp_file(
      R"({"model_id":"other","features":["x0","x1","x2","x3"],"mean":[0,0,0,0],"std":[1,1,1,1],"clip":4,"label":"y"})");
  expect_throw([&] { load_preprocess(other, model); }, "different model", "preprocessing of another model rejected");
  std::remove(other.c_str());
}

void dataset_csv() {
  const auto model = load_model(sample("sample_model.json"));
  const auto pre = load_preprocess(sample("sample_preprocess.json"), model);
  const auto data = load_csv(sample("sample_records.csv"), pre);
  check(data.raw.size() == 300 && data.labels.size() == 300, "all synthetic records and labels");
  check(data.raw[0] == std::array<double, 4>{5, 1, 23.7, 12} && data.labels[0] == 1, "first row, model column order");
  check(load_csv(sample("sample_records.csv"), pre, 17).raw.size() == 17, "record limit");
  const auto bad = [&](const std::string& text, const char* fragment) {
    const auto path = temp_file(text);
    expect_throw([&] { load_csv(path, pre); }, fragment, std::string("CSV rejected: ") + fragment);
    std::remove(path.c_str());
  };
  bad("x0,x1,x2\n1,2,3\n", "no column x3");
  bad("x0,x1,x2,x3,y\n1,2,3\n", "wrong number of fields");
  bad("x0,x1,x2,x3,y\n1,2,3,nan,0\n", "Invalid number");
  bad("x0,x1,x2,x3,y\n1,2,3,4,2\n", "Label must be 0 or 1");
  bad("x0,x1,x2,x3,y\n\"1\",2,3,4,0\n", "Quoted");
  bad("x0,x1,x2,x3,y\n", "no records");
  const auto unlabelled = temp_file("x3,x2,x1,x0\r\n4,3,2,1\r\n");
  const auto d = load_csv(unlabelled, pre);
  check(d.labels.empty() && d.raw[0] == std::array<double, 4>{1, 2, 3, 4}, "column order, CRLF, no label column");
  std::remove(unlabelled.c_str());
}

void qa_random_deterministic() {
  const auto a = qa_material(11), b = qa_material(11), c = qa_material(12);
  check(a.secret == b.secret && a.a == b.a && a.mask == b.mask && a.e1 == b.e1, "same seed, same material");
  check(a.secret != c.secret && a.a != c.a, "different seed, different material");
  validate_ternary(a.secret, 64);
  validate_ternary(a.error, 16);
  for (size_t f = 0; f < 4; f++) {
    validate_ternary(a.mask[f], 64);
    validate_ternary(a.e0[f], 16);
    validate_ternary(a.e1[f], 16);
  }
  validate(a.a);
  const auto s = qa_samples(11, 3);
  check(s == qa_samples(11, 3) && s != qa_samples(11, 4), "independent per-batch streams");
  for (size_t i = 0; i < 12; i++) validate_ternary(s[i], i < 4 ? 64 : 16);
}

void session_matches_v4_engine() {
  const auto model = load_model(sample("sample_model.json"));
  const auto pre = load_preprocess(sample("sample_preprocess.json"), model);
  const auto data = load_csv(sample("sample_records.csv"), pre);
  const CloudAffine cloud(model, 1);
  const auto material = qa_material(21);
  CpuBackend cpu(2);
  EdgeSession session(cpu, model, pre, cloud, data, material);
  auto batches = plan_batches(data.raw.size(), 128);
  check(batches.size() == 3 && batches[2].first == 256 && batches[2].count == 44, "batch plan");
  check(plan_batches(300, 128, 2).size() == 2, "batch limit");
  expect_throw([] { plan_batches(10, 2049); }, "1..2048", "oversized batch rejected");
  auto& batch = batches[1];
  batch.samples = qa_samples(21, 1);
  batch.presampled = true;
  for (size_t s = 0; s < step_count; s++) session.run_step(Step(s), batch);

  // Independent recomputation of every step with the accepted V4 Engine.
  const Engine v4(1);
  const auto key = v4.keygen(material);
  check(key.b == session.public_key().b && key.a == session.public_key().a, "session key equals V4 keygen");
  std::array<Message, 4> messages;
  for (size_t f = 0; f < 4; f++) {
    std::vector<std::complex<double>> values;
    for (size_t i = batch.first; i < batch.first + batch.count; i++) values.push_back(standardize(pre, data.raw[i])[f]);
    messages[f] = encode(values);
    check(messages[f] == batch.messages[f], "step 2 equals encode of standardized records");
  }
  std::array<Ternary, 4> u, e0, e1;
  for (size_t f = 0; f < 4; f++) {
    u[f] = batch.samples[f];
    e0[f] = batch.samples[4 + f];
    e1[f] = batch.samples[8 + f];
  }
  const auto cipher = v4.encrypt(key, messages, u, e0, e1);
  for (size_t f = 0; f < 4; f++)
    check(cipher[f].c0 == batch.cipher[f].c0 && cipher[f].c1 == batch.cipher[f].c1, "steps 4-5 equal V4 encrypt");
  const auto affine = v4.affine(cipher, cloud.encoded().weights, cloud.encoded().bias);
  check(affine.c0 == batch.result.c0 && affine.c1 == batch.result.c1, "steps 6-8 equal V4 affine");
  check(v4.decrypt(affine, material.secret) == batch.coefficients, "step 9 equals V4 decrypt");
  check(batch.logits.size() == batch.count && batch.decision.size() == batch.count, "step 10 outputs");
  for (size_t i = 0; i < batch.count; i++) {
    const double reference = plain_logit(model, standardize(pre, data.raw[batch.first + i]));
    check(std::abs(batch.logits[i] - reference) <= session.error_bound(), "logit within the error bound");
  }
  for (size_t s = 0; s < step_count; s++) check(batch.ns[s] > 0 || Step(s) == Step::sample, "every step timed");
}

// Step 2 on 1, 2 or 4 threads: same messages, and whole runs (serial and pipelined)
// with the same ciphertext and decryption digests.
void session_parallel_encode_bit_exact() {
  const auto model = load_model(sample("sample_model.json"));
  const auto pre = load_preprocess(sample("sample_preprocess.json"), model);
  const auto data = load_csv(sample("sample_records.csv"), pre);
  const CloudAffine cloud(model, 1);
  const auto material = qa_material(41);
  CpuBackend cpu(2);
  for (const unsigned bad : {0u, 5u})
    expect_throw([&] { EdgeSession session(cpu, model, pre, cloud, data, material, bad); }, "1..4",
                 "encode workers " + std::to_string(bad) + " rejected");
  auto plan = plan_batches(data.raw.size(), 128);
  for (auto& batch : plan) {
    batch.samples = qa_samples(41, batch.index);
    batch.presampled = true;
  }
  std::array<Message, 4> messages0;
  std::string cipher0, plain0;
  for (const unsigned workers : {1u, 2u, 4u}) {
    EdgeSession session(cpu, model, pre, cloud, data, material, workers);
    check(session.encode_workers() == workers, "encode workers kept");
    auto batch = plan[2];  // the short last batch: 44 records
    session.run_step(Step::standardize, batch);
    session.run_step(Step::encode, batch);
    if (workers == 1) messages0 = batch.messages;
    check(batch.messages == messages0, "step 2 bit-identical on " + std::to_string(workers) + " threads");
    for (const size_t capacity : {0, 2}) {
      const auto run = run_batches(session, plan, capacity);
      const auto c = cipher_digest(run.batches), p = decrypted_digest(run.batches);
      if (cipher0.empty()) {
        cipher0 = c;
        plain0 = p;
      }
      check(c == cipher0 && p == plain0,
            "encode workers " + std::to_string(workers) + " capacity " + std::to_string(capacity) + " bit-exact");
    }
  }
}

void runner_backends_and_pipeline_agree() {
  const auto model = load_model(sample("sample_model.json"));
  const auto pre = load_preprocess(sample("sample_preprocess.json"), model);
  const auto data = load_csv(sample("sample_records.csv"), pre);
  const CloudAffine cloud(model, 1);
  const auto material = qa_material(31);
  auto plan = plan_batches(data.raw.size(), 100);
  for (auto& batch : plan) {
    batch.samples = qa_samples(31, batch.index);
    batch.presampled = true;
  }
  SimulatedDmaDevice device;
  CpuBackend cpu1(1), cpu4(4);
  FpgaBackend serial(device, false), overlap(device, true);
  std::string cipher0, plain0;
  for (PolyBackend* backend : std::initializer_list<PolyBackend*>{&cpu1, &cpu4, &serial, &overlap})
    for (size_t capacity : {0, 1, 3}) {
      EdgeSession session(*backend, model, pre, cloud, data, material);
      const auto run = run_batches(session, plan, capacity);
      check(run.batches.size() == 3 && run.pipeline.names.size() == 5, "five stages, every batch");
      for (size_t i = 0; i < 3; i++) check(run.batches[i].index == i, "batch order kept");
      const auto c = cipher_digest(run.batches), p = decrypted_digest(run.batches);
      if (cipher0.empty()) {
        cipher0 = c;
        plain0 = p;
      }
      check(c == cipher0 && p == plain0, backend->name() + " capacity " + std::to_string(capacity) + " bit-exact");
      const auto accuracy = check_accuracy(run.batches, data, model, pre, session.error_bound());
      check(accuracy.records == 300 && accuracy.within_bound && accuracy.disagree == 0, "accuracy vs plaintext");
      check(accuracy.labelled && accuracy.auc > 0.5 && accuracy.plain_auc > 0.5, "labels evaluated");
      check(run.wall_ns > 0 && run.cpu_ns > 0, "run measured");
    }
  SimulatedDmaDevice broken;
  broken.inject(SimulatedDmaDevice::Fault::fail_call, 4);  // keygen is call 1
  FpgaBackend failing(broken, true);
  EdgeSession session(failing, model, pre, cloud, data, material);
  expect_throw([&] { run_batches(session, plan, 2); }, "DMA failure", "device failure stops the pipeline");
  check(broken.calls() == 4, "no request after the failure");
}

void backend_factory() {
  SimulatedDmaDevice device;
  check(make_backend("cpu-w3", nullptr)->name() == "cpu-w3", "cpu spec");
  check(make_backend("fpga-overlap", &device)->name() == "fpga-simulated-dma-overlap", "overlap spec");
  check(make_backend("fpga-serial", &device)->name() == "fpga-simulated-dma-serial", "serial spec");
  check(backend_needs_device("fpga-serial") && !backend_needs_device("cpu-w1"), "device requirement");
  for (const char* bad : {"cpu-w0", "cpu-w65", "cpu-w", "cpu-wx", "gpu", "fpga"})
    expect_throw([&] { make_backend(bad, &device); }, "Unknown backend", std::string("rejects ") + bad);
  expect_throw([&] { make_backend("fpga-serial", nullptr); }, "needs a DMA device", "FPGA spec without device");
  check(split_list("a,b,c") == std::vector<std::string>{"a", "b", "c"}, "list split");
  expect_throw([] { split_list("a,,b"); }, "Empty item", "empty list item");
}
}  // namespace

int main(int argc, char** argv) {
  if (argc != 2) {
    std::cerr << "usage: v5_tests <software/native_v5/tests/data>\n";
    return 2;
  }
  sample_dir = argv[1];
  try {
    test("abi_contract", abi_contract);
    test("thread_pool_fork_join", thread_pool_fork_join);
    test("thread_pool_exception", thread_pool_exception);
    test("cpu_backend_matches_v4", cpu_backend_matches_v4);
    test("cpu_backend_operand_reuse", cpu_backend_operand_reuse);
    test("backend_rejects_aliasing", backend_rejects_aliasing);
    test("simulated_device_validation", simulated_device_validation);
    test("fpga_backend_matches_cpu", fpga_backend_matches_cpu);
    test("fpga_fault_stops_batch", fpga_fault_stops_batch);
    test("client_matches_v4_engine", client_matches_v4_engine);
    test("client_round_trip_accuracy", client_round_trip_accuracy);
    test("pipeline_order_and_backpressure", pipeline_order_and_backpressure);
    test("pipeline_exception", pipeline_exception);
    test("pipeline_overlaps_stages", pipeline_overlaps_stages);
    test("json_strict_round_trip", json_strict_round_trip);
    test("sha256_vectors", sha256_vectors);
    test("platform_facts", platform_facts);
    test("power_sampler_fake_hwmon", power_sampler_fake_hwmon);
    test("model_and_preprocess", model_and_preprocess);
    test("dataset_csv", dataset_csv);
    test("qa_random_deterministic", qa_random_deterministic);
    test("session_matches_v4_engine", session_matches_v4_engine);
    test("session_parallel_encode_bit_exact", session_parallel_encode_bit_exact);
    test("runner_backends_and_pipeline_agree", runner_backends_and_pipeline_agree);
    test("backend_factory", backend_factory);
    std::cout << "V5_TESTS_PASS tests=" << passed << "\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "FAIL: " << e.what() << "\n";
    return 1;
  }
}
