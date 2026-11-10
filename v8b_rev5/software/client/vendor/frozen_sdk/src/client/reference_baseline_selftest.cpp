// Reference baseline R1: the stock OpenFHE 1.5.1 library on the same CPU (no device, no driver).
// Standard-library numbers for CKKS / BFV / BGV public-key encryption and decryption at 128-bit classic security,
// so EdgeFHE's own CPU and PL paths can be placed against an independent, reproducible baseline (references:
// OpenFHE, WAHC 2022; fair-baseline argument of HEAAN Demystified, IEEE Access 2021).
//   CKKS: N=4096, FIXEDMANUAL, BV key switching (no P), 3 towers of 30 bits (scale 2^30), 2048 slots -> EdgeFHE class
//   BFV/BGV: plaintext modulus 65537, multiplicative depth 1, ring dimension chosen by OpenFHE for 128-bit
// One "job" = 4 ciphertexts (CKKS: 4 features x 2048 records; BFV/BGV: 4 x N packed integer slots).
// Encryption uses OpenFHE's own sampling with the same OS-entropy PRNG plugin as EdgeFHE. Every decryption is checked.
// Timings are single-process observations on the machine that runs this binary.
#include "openfhe.h"
#include <omp.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <iostream>
#include <random>
#include <string>
#include <vector>
using namespace lbcrypto;
using Clock = std::chrono::steady_clock;

namespace {
void need(bool ok, const std::string& m) { if (!ok) throw std::runtime_error(m); }
double us_since(Clock::time_point t) { return std::chrono::duration<double, std::micro>(Clock::now() - t).count(); }
struct Stat { double median, p10, p90; };
Stat stat(std::vector<double> v) {
  std::sort(v.begin(), v.end());
  auto q = [&](double p) { return v[std::min(v.size() - 1, size_t(p * (v.size() - 1) + 0.5))]; };
  return {q(0.5), q(0.1), q(0.9)};
}
template <class CC> void describe(const std::string& name, const CC& cc, const std::string& extra) {
  auto ep = cc->GetCryptoParameters()->GetElementParams();
  std::cout << "V7_REF_PARAMS scheme=" << name << " N=" << cc->GetRingDimension() << " towers=" << ep->GetParams().size()
            << " logQ=" << ep->GetModulus().GetMSB() << " security=HEStd_128_classic " << extra << "\n";
}
void report(const std::string& scheme, const std::string& what, unsigned threads, const std::vector<double>& v) {
  auto s = stat(v);
  std::cout << "V7_REF_TIMING scheme=" << scheme << " op=" << what << " threads=" << threads << " n=" << v.size()
            << " median_us=" << s.median << " p10_us=" << s.p10 << " p90_us=" << s.p90 << "\n";
}

// CKKS, same parameter class as EdgeFHE.
uint64_t run_ckks(unsigned reps) {
  CCParams<CryptoContextCKKSRNS> p;
  p.SetSecurityLevel(HEStd_128_classic);
  p.SetRingDim(4096);
  p.SetMultiplicativeDepth(2);
  p.SetScalingModSize(30);
  p.SetFirstModSize(30);
  p.SetScalingTechnique(FIXEDMANUAL);
  p.SetBatchSize(2048);
  p.SetKeySwitchTechnique(BV);   // public-key encryption only: no auxiliary P modulus, so log(QP) = log Q = 90
  auto cc = GenCryptoContext(p);
  cc->Enable(PKE);
  describe("CKKS", cc, "scale_bits=30 slots=2048");
  auto kp = cc->KeyGen();
  std::mt19937_64 g(7);
  std::uniform_real_distribution<double> d(-2.0, 2.0);
  uint64_t checked = 0;
  for (unsigned threads : {1u, 3u}) {
    omp_set_num_threads(int(threads));
    std::vector<double> enc, dec;
    for (unsigned r = 0; r < reps; r++) {
      std::array<std::vector<double>, 4> x;
      for (auto& f : x) { f.resize(2048); for (auto& v : f) v = d(g); }
      std::vector<Ciphertext<DCRTPoly>> cts;
      auto t0 = Clock::now();
      for (auto& f : x) cts.push_back(cc->Encrypt(kp.publicKey, cc->MakeCKKSPackedPlaintext(f)));
      enc.push_back(us_since(t0));
      Plaintext out;
      t0 = Clock::now();
      cc->Decrypt(kp.secretKey, cts[0], &out);
      out->SetLength(2048);
      dec.push_back(us_since(t0));
      auto vals = out->GetRealPackedValue();
      for (size_t i = 0; i < 2048; i++) need(std::abs(vals[i] - x[0][i]) < 1e-3, "CKKS decryption error bound");
      checked += 2048;
    }
    report("CKKS", "encode_encrypt_4ct", threads, enc);
    report("CKKS", "decrypt_decode_1ct", threads, dec);
  }
  return checked;
}

// BFV or BGV with batching (t = 65537), depth 1, OpenFHE-chosen ring dimension for 128-bit classic security.
template <class Params> uint64_t run_int(const std::string& name, unsigned reps) {
  CCParams<Params> p;
  p.SetSecurityLevel(HEStd_128_classic);
  p.SetPlaintextModulus(65537);
  p.SetMultiplicativeDepth(1);
  auto cc = GenCryptoContext(p);
  cc->Enable(PKE);
  const size_t slots = cc->GetRingDimension();
  describe(name, cc, "plaintext_modulus=65537 slots=" + std::to_string(slots));
  auto kp = cc->KeyGen();
  std::mt19937_64 g(11);
  std::uniform_int_distribution<int64_t> d(-30000, 30000);
  uint64_t checked = 0;
  for (unsigned threads : {1u, 3u}) {
    omp_set_num_threads(int(threads));
    std::vector<double> enc, dec;
    for (unsigned r = 0; r < reps; r++) {
      std::array<std::vector<int64_t>, 4> x;
      for (auto& f : x) { f.resize(slots); for (auto& v : f) v = d(g); }
      std::vector<Ciphertext<DCRTPoly>> cts;
      auto t0 = Clock::now();
      for (auto& f : x) cts.push_back(cc->Encrypt(kp.publicKey, cc->MakePackedPlaintext(f)));
      enc.push_back(us_since(t0));
      Plaintext out;
      t0 = Clock::now();
      cc->Decrypt(kp.secretKey, cts[0], &out);
      out->SetLength(slots);
      dec.push_back(us_since(t0));
      need(out->GetPackedValue() == x[0], name + " exact decryption");
      checked += slots;
    }
    report(name, "encode_encrypt_4ct", threads, enc);
    report(name, "decrypt_decode_1ct", threads, dec);
  }
  return checked;
}
}  // namespace

int main(int argc, char** argv) {
  try {
    need(argc == 3, "usage: reference_baseline_selftest <os_prng_plugin> <reps>");
    PseudoRandomNumberGenerator::InitPRNGEngine(argv[1]);
    const unsigned reps = unsigned(std::stoul(argv[2]));
    need(reps >= 5 && reps <= 200, "reps 5..200");
    const uint64_t a = run_ckks(reps);
    const uint64_t b = run_int<CryptoContextBFVRNS>("BFV", reps);
    const uint64_t c = run_int<CryptoContextBGVRNS>("BGV", reps);
    std::cout << "V7_REFERENCE_BASELINE_PASS library=OpenFHE-1.5.1 schemes=CKKS,BFV,BGV reps=" << reps
              << " checked_values=" << a + b + c << " device=0\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "V7_REFERENCE_BASELINE_FAIL " << e.what() << '\n';
    return 1;
  }
}
