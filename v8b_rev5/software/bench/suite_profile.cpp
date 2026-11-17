// EdgeFHE verification suite, S1 profiler (2026-10-24). In-process, no network: the accepted owner library
// (libef_app_owner.so, same binary the client uses) on CPU (backend 0) or PL (backend 2).
//   stages  : for F in {4, 10}, 2048 rows: owner encrypt (encode + sample + encrypt), owner decrypt, and the two
//             byte-stream costs the client pays per request: SHA-256 of the ciphertext payload (request digest) and
//             AES-256-GCM of the payload (TLS 1.3 record protection), with OpenSSL EVP. 3 warm-ups + 30 timed.
//   sustain : encrypt loop for D seconds (power windows; the controller samples the INA260 rail meanwhile).
// The core set is fixed by the controller with taskset-equivalent sched_setaffinity before exec; workers = 1 or 3
// (the owner library accepts exactly these). PL use requires the suite permit (root controller), checked before open.
#include "invoke_permit_suite.hpp"
#include <openssl/evp.h>
#include <algorithm>
#include <chrono>
#include <cinttypes>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
extern "C" {
void* ef_owner_create(unsigned, unsigned, const char*);
void ef_owner_destroy(void*);
const char* ef_owner_error();
int ef_owner_encrypt(void*, const double*, size_t, size_t, uint32_t*, size_t);
int ef_owner_decrypt(void*, const uint32_t*, size_t, size_t, double*);
int ef_owner_device_calls(void*, uint64_t*);
int ef_owner_decrypt_concurrent(void*, const uint32_t*, size_t, size_t, double*);   // O5 owner library
}
namespace {
using Clock = std::chrono::steady_clock;
constexpr size_t WORDS = 6 * 4096, ROWS = 2048;
void need(bool ok, const std::string& m) { if (!ok) throw std::runtime_error(m); }
double ms(Clock::time_point t) { return std::chrono::duration<double, std::milli>(Clock::now() - t).count(); }
double median(std::vector<double> v) { std::sort(v.begin(), v.end()); return v.size() % 2 ? v[v.size() / 2] : (v[v.size() / 2 - 1] + v[v.size() / 2]) / 2; }
void list(const char* name, const std::vector<double>& v) { std::printf(",\"%s\":[", name); for (size_t i = 0; i < v.size(); i++) std::printf("%s%.4f", i ? "," : "", v[i]); std::printf("]"); }

std::vector<double> inputs(size_t F) {   // public synthetic normalized inputs in the profile range
  std::vector<double> v(ROWS * F); for (size_t r = 0; r < ROWS; r++) for (size_t f = 0; f < F; f++) v[r * F + f] = (double((r + f) % 17) - 8.0) / 4.0; return v;
}
double sha256_ms(const std::vector<uint8_t>& b) {
  unsigned char out[32]; unsigned n = 0; auto t = Clock::now(); need(EVP_Digest(b.data(), b.size(), out, &n, EVP_sha256(), nullptr) == 1, "sha256"); return ms(t);
}
double gcm_ms(const std::vector<uint8_t>& b, std::vector<uint8_t>& out) {
  EVP_CIPHER_CTX* c = EVP_CIPHER_CTX_new(); unsigned char key[32] = {1}, iv[12] = {2}; int n = 0, m = 0; out.resize(b.size() + 16);
  auto t = Clock::now();
  need(c && EVP_EncryptInit_ex(c, EVP_aes_256_gcm(), nullptr, key, iv) == 1, "gcm init");
  for (size_t off = 0; off < b.size(); off += 16384) {   // TLS 1.3 records of 16 KiB
    const int len = int(std::min<size_t>(16384, b.size() - off)); need(EVP_EncryptUpdate(c, out.data() + off, &n, b.data() + off, len) == 1, "gcm update");
  }
  need(EVP_EncryptFinal_ex(c, out.data(), &m) == 1, "gcm final"); const double r = ms(t); EVP_CIPHER_CTX_free(c); return r;
}
}  // namespace

int main(int argc, char** argv) {
  try {
    // suite_profile <stages|sustain> <cpu|pl> <workers 1|3> <plugin> <seconds|reps> [--permit-fd n | --local]
    need(argc >= 7, "usage");
    const std::string mode = argv[1], backend = argv[2]; const unsigned workers = unsigned(std::stoul(argv[3])); const std::string plugin = argv[4];
    const int amount = std::stoi(argv[5]); const std::string gate = argv[6];
    need((mode == "stages" || mode == "sustain" || mode == "o5check") && (backend == "cpu" || backend == "pl" || backend == "spec") && (workers == 1 || workers == 3), "arguments");
    need(amount >= 1 && amount <= 600, "amount");
    if (backend == "pl") { need(argc == 8 && gate == "--permit-fd", "PL requires the suite permit"); suite_permit::check(std::stoi(argv[7]), "EDGEFHE_SUITE_PROFILE_V1", mode + "-" + std::to_string(workers)); }
    else need(gate == "--local", "CPU/spec take no permit");
    void* o = ef_owner_create(workers, backend == "cpu" ? 0 : backend == "spec" ? 1 : 2, plugin.c_str()); need(o != nullptr, std::string("owner create: ") + ef_owner_error());
    std::printf("{\"status\":\"SUITE_PROFILE_PASS\",\"mode\":\"%s\",\"backend\":\"%s\",\"workers\":%u,\"cases\":[", mode.c_str(), backend.c_str(), workers);
    bool first = true;
    if (mode == "o5check") {
      // O5 equivalence: decrypt on a dedicated thread (ef_owner_decrypt_concurrent) WHILE the owner thread keeps encrypting,
      // compared bit for bit with ef_owner_decrypt of the same ciphertext on the owner thread.
      uint64_t compared = 0;
      for (size_t F : {size_t(4), size_t(10)}) {
        const size_t P = (F + 3) / 4 * 4; std::vector<double> in(ROWS * F);
        for (int it = 0; it < amount; it++) {
          for (size_t i = 0; i < in.size(); i++) in[i] = (double((i * 7 + size_t(it) * 13) % 33) - 16.0) / 4.0;
          std::vector<uint32_t> ct(P * WORDS), busy(P * WORDS); std::vector<double> a(ROWS), b(ROWS);
          need(ef_owner_encrypt(o, in.data(), ROWS, F, ct.data(), ct.size()) == 0, ef_owner_error());
          int rc = -1; std::string err;
          std::thread d([&] { rc = ef_owner_decrypt_concurrent(o, ct.data(), WORDS, ROWS, b.data()); if (rc) err = ef_owner_error(); });
          need(ef_owner_encrypt(o, in.data(), ROWS, F, busy.data(), busy.size()) == 0, ef_owner_error());   // concurrent owner work
          d.join(); need(rc == 0, "concurrent decrypt: " + err);
          need(ef_owner_decrypt(o, ct.data(), WORDS, ROWS, a.data()) == 0, ef_owner_error());
          need(std::memcmp(a.data(), b.data(), ROWS * sizeof(double)) == 0, "O5 decrypt differs from owner decrypt");
          compared += ROWS;
        }
      }
      std::printf("{\"o5_bit_identical_values\":%" PRIu64 ",\"concurrent_with_encrypt\":true}", compared);
    } else if (mode == "stages") {
      for (size_t F : {size_t(4), size_t(10)}) {
        const auto in = inputs(F); const size_t P = (F + 3) / 4 * 4; std::vector<uint32_t> ct(P * WORDS); std::vector<double> back(ROWS);
        std::vector<double> enc, dec, sha, gcm; std::vector<uint8_t> payload(F * WORDS * 4), sealed;
        for (int i = 0; i < 3 + amount; i++) {
          auto t = Clock::now(); need(ef_owner_encrypt(o, in.data(), ROWS, F, ct.data(), ct.size()) == 0, ef_owner_error()); const double e = ms(t);
          t = Clock::now(); need(ef_owner_decrypt(o, ct.data(), WORDS, ROWS, back.data()) == 0, ef_owner_error()); const double d = ms(t);
          // ciphertext 0 = feature 0 at scale 2^30; the owner decryptor decodes at scale^2 (the affine output), hence * 2^30
          for (size_t r = 0; r < ROWS; r++) need(std::abs(back[r] * 1073741824.0 - in[r * F]) < 1e-3, "decrypt(feature 0) != input");
          std::memcpy(payload.data(), ct.data(), payload.size()); const double s = sha256_ms(payload), g = gcm_ms(payload, sealed);
          if (i >= 3) { enc.push_back(e); dec.push_back(d); sha.push_back(s); gcm.push_back(g); }
        }
        std::printf("%s{\"features\":%zu,\"rows\":%zu,\"payload_bytes\":%zu,\"encrypt_median_ms\":%.4f,\"decrypt_median_ms\":%.4f,\"sha256_median_ms\":%.4f,\"aes256gcm_median_ms\":%.4f",
                    first ? "" : ",", F, ROWS, payload.size(), median(enc), median(dec), median(sha), median(gcm));
        list("encrypt_ms", enc); list("decrypt_ms", dec); std::printf("}"); first = false;
      }
    } else {
      const size_t F = 4; const auto in = inputs(F); std::vector<uint32_t> ct(F * WORDS); uint64_t n = 0;
      const auto end = Clock::now() + std::chrono::seconds(amount); auto t = Clock::now();
      while (Clock::now() < end) { need(ef_owner_encrypt(o, in.data(), ROWS, F, ct.data(), ct.size()) == 0, ef_owner_error()); n++; }
      const double el = ms(t);
      std::printf("{\"features\":%zu,\"rows\":%zu,\"requests\":%" PRIu64 ",\"elapsed_ms\":%.3f,\"records_per_s\":%.3f}", F, ROWS, n, el, double(n) * ROWS * 1000.0 / el);
    }
    uint64_t calls = 0; need(ef_owner_device_calls(o, &calls) == 0, "device calls");
    std::printf("],\"device_calls\":%" PRIu64 "}\n", calls);
    ef_owner_destroy(o); return 0;
  } catch (const std::exception& e) { std::cerr << "STOP_SUITE_PROFILE: " << e.what() << '\n'; return 1; }
}
