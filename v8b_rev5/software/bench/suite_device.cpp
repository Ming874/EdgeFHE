// EdgeFHE verification suite, device part (2026-10-24). One binary, two modes, both against the real V8b node (board)
// or the bit-exact V8b specification model standing in for it (local QA, --local-spec):
//
//  m1  : M1 exactness on the current hardware.
//        PAIR: 50 LOADs of arbitrary dense (b, a) (40 uniform, 10 boundary patterns), each followed by 5 PAIR frames of
//        k = 4 ternary u (random and boundary patterns) = 1000 products, each (b*u, a*u) in all 3 RNS limbs, compared
//        word for word with (1) an independent schoolbook negacyclic product (no NTT) and (2) the specification model.
//        ENC : 1 LOAD + 200 ENC frames (k = 1..8, random/boundary messages |m| < 2^62, random seeds) compared word for
//        word with the specification model (same PL sampler B from the same seed).
//  c01 : fault matrix at the device boundary. Malformed or out-of-lease frames must be refused BEFORE any DMA
//        (the controller checks the interrupt counters), the lease must survive the refusals, INVALIDATE must make the
//        old generation unusable, and a fresh LOAD must restore exact operation.
// All randomness is from public seeds printed in the report (reproducible). Device calls are counted per success.
#include "v8b_device_v5.hpp"
#include "invoke_permit_suite.hpp"
#include <atomic>
#include <cinttypes>
#include <cstdio>
#include <iostream>
#include <random>
#include <thread>

// The frozen SDK declares edgefhe::require in core.hpp and defines it in core.cpp (an OpenFHE-linked library);
// this tool only needs the header-only device/specification code, so it carries the identical one-line definition.
namespace edgefhe { void require(bool ok, const char* what) { if (!ok) throw std::runtime_error(what); } }

namespace {
using v8ref::N;
using v8ref::Q;
constexpr uint32_t W = 6144 * 4;     // output words per feature (2 polys x 3 limbs x 4096)

struct Link {
  std::function<int(edgefhe_v8_request&)> run;
  uint64_t generation = 0, dma = 0;
  std::array<uint8_t, 32> key{};
  int call(uint32_t command, uint32_t k, const void* in, size_t in_bytes, void* out, size_t out_bytes, uint64_t gen_override = UINT64_MAX,
           const uint8_t* profile = nullptr, const uint8_t* key_override = nullptr, uint32_t* hw = nullptr) {
    edgefhe_v8_request r{};
    r.abi_version = EDGEFHE_V8_ABI; r.command = command; r.k = k;
    r.input_ptr = in_bytes ? uint64_t(uintptr_t(in)) : 0; r.output_ptr = out_bytes ? uint64_t(uintptr_t(out)) : 0;
    r.input_bytes = uint32_t(in_bytes); r.output_bytes = uint32_t(out_bytes);
    r.generation = gen_override != UINT64_MAX ? gen_override : (command == 6 ? 0 : generation); r.timeout_ms = 2000;
    std::memcpy(r.profile_sha256, profile ? profile : edgefhe_v8_v5::profile.data(), 32);
    std::memcpy(r.public_key_sha256, key_override ? key_override : key.data(), 32);
    const int rc = run(r);
    if (hw) *hw = r.hardware_status;
    if (rc == 0) { dma++; if (command == 6) generation = r.generation; }
    return rc;
  }
  int load(const std::vector<uint32_t>& payload) {
    SHA256(reinterpret_cast<const unsigned char*>(payload.data()), payload.size() * 4, key.data());
    return call(6, 0, payload.data(), payload.size() * 4, nullptr, 0);
  }
};

// ---- independent oracle: schoolbook negacyclic product of a canonical polynomial and a ternary polynomial ----
void schoolbook_ternary(const uint32_t* p, const int8_t* u, uint32_t q, uint32_t* out) {
  std::vector<int64_t> acc(N, 0);
  for (int j = 0; j < N; j++) {
    if (!u[j]) continue;
    const int64_t s = u[j];
    for (int i = 0; i < N - j; i++) acc[i + j] += s * int64_t(p[i]);          // x^(i+j), no wrap
    for (int i = N - j; i < N; i++) acc[i + j - N] -= s * int64_t(p[i]);      // x^(i+j) = -x^(i+j-N)
  }
  for (int k = 0; k < N; k++) { int64_t v = acc[k] % int64_t(q); if (v < 0) v += q; out[k] = uint32_t(v); }
}

std::vector<uint32_t> dense_payload(std::mt19937_64& g, int pattern) {
  std::vector<uint32_t> p(2 * 3 * N);
  for (int poly = 0; poly < 2; poly++) for (int l = 0; l < 3; l++) {
    uint32_t* x = p.data() + (poly * 3 + l) * N; const uint32_t q = Q[l];
    for (int i = 0; i < N; i++) {
      uint32_t v;
      switch (pattern) {
        case 0: v = q - 1; break;                                         // all maximal
        case 1: v = (i == 0) ? 1 : 0; break;                              // identity element
        case 2: v = (i % 2) ? q - 1 : 0; break;                           // alternating
        case 3: v = (i == N - 1) ? q - 1 : 0; break;                      // top coefficient only (wrap)
        case 4: v = uint32_t(g() % 4); break;                             // small
        case 5: v = (q - 1) / 2 + uint32_t(g() % 2); break;               // around q/2
        case 6: v = uint32_t(g() % Q[2]); break;                          // same integer range in every limb
        case 7: v = q - 1 - uint32_t(g() % 16); break;                    // near q
        default: v = uint32_t(g() % q);                                   // uniform dense
      }
      x[i] = v;
    }
  }
  return p;
}

std::vector<int8_t> ternary(std::mt19937_64& g, int pattern) {
  std::vector<int8_t> u(N, 0);
  switch (pattern) {
    case 0: std::fill(u.begin(), u.end(), 1); break;
    case 1: std::fill(u.begin(), u.end(), -1); break;
    case 2: u[0] = 1; break;
    case 3: u[N - 1] = -1; break;
    case 4: for (int i = 0; i < N; i++) u[i] = (i % 2) ? -1 : 1; break;
    case 5: for (int t = 0; t < 64; t++) u[g() % N] = (g() & 1) ? 1 : -1; break;
    default: for (auto& v : u) v = int8_t(int(g() % 3) - 1);
  }
  return u;
}

std::vector<uint32_t> trit_payload(const std::vector<std::vector<int8_t>>& us) {
  std::vector<uint32_t> w(us.size() * 64 * 4, 0);
  for (size_t f = 0; f < us.size(); f++) for (int j = 0; j < 64; j++) for (int i = 0; i < 64; i++) {
    const int8_t x = us[f][64 * j + i]; const uint32_t code = x == 0 ? 0u : x == 1 ? 1u : 2u;
    w[(f * 64 + j) * 4 + (2 * i) / 32] |= code << ((2 * i) % 32);
  }
  return w;
}

// coefficient c of limb l of product p (0 = b*u, 1 = a*u) of feature f in the device output layout
inline uint32_t out_coeff(const std::vector<uint32_t>& o, size_t f, int p, int l, int c) {
  const int j = c / 4; return o[(f * 2 + p) * 1024 * 3 * 4 + (size_t(j) * 3 + l) * 4 + c % 4];
}

// The oracle must not be vacuous: on a public case the specification product must equal the schoolbook product,
// and a single changed coefficient must be detected (run before any device call).
void oracle_selfcheck() {
  std::mt19937_64 g(7); v8ref::Tables t(0); v8ref::Poly p(N); for (auto& x : p) x = uint32_t(g() % Q[0]);
  auto u = ternary(g, 99); v8ref::Poly ur(N); for (int i = 0; i < N; i++) ur[i] = u[i] == 0 ? 0 : u[i] == 1 ? 1 : Q[0] - 1;
  const auto spec = v8ref::product(v8ref::cache_spectrum(p, t), ur, t); std::vector<uint32_t> ref(N); schoolbook_ternary(p.data(), u.data(), Q[0], ref.data());
  edgefhe::require(std::equal(ref.begin(), ref.end(), spec.begin()), "oracle self-check: schoolbook != NTT specification");
  auto bad = spec; bad[1234] = bad[1234] == 0 ? 1 : bad[1234] - 1;
  edgefhe::require(!std::equal(ref.begin(), ref.end(), bad.begin()), "oracle self-check: mutation not detected");
}

struct Report { uint64_t pair_products = 0, pair_limb_polys = 0, coeffs_vs_schoolbook = 0, coeffs_vs_spec = 0, enc_frames = 0, enc_ciphertexts = 0, enc_words = 0; };

void m1(Link& dev, Link& spec, uint64_t seed, Report& rep) {
  std::mt19937_64 g(seed);
  for (int L = 0; L < 50; L++) {
    const int pattern = L < 40 ? 99 : L - 40;                               // 40 uniform + patterns 0..7 + 2 more uniform
    const auto payload = dense_payload(g, pattern);
    edgefhe::require(dev.load(payload) == 0 && spec.load(payload) == 0, "M1 LOAD");
    for (int frame = 0; frame < 5; frame++) {
      std::vector<std::vector<int8_t>> us;
      for (int t = 0; t < 4; t++) us.push_back(ternary(g, (frame * 4 + t) % 10 < 6 ? (frame * 4 + t) % 10 : 99));
      const auto in = trit_payload(us);
      std::vector<uint32_t> a(4 * W), b(4 * W);
      edgefhe::require(dev.call(7, 4, in.data(), in.size() * 4, a.data(), a.size() * 4) == 0, "M1 PAIR on device");
      edgefhe::require(spec.call(7, 4, in.data(), in.size() * 4, b.data(), b.size() * 4) == 0, "M1 PAIR on spec");
      edgefhe::require(a == b, "M1 PAIR device != specification");
      rep.coeffs_vs_spec += a.size();
      // independent schoolbook for every (feature, product, limb): 24 polynomials per frame, 3 threads
      std::atomic<int> next{0}; std::atomic<bool> bad{false};
      auto worker = [&] {
        std::vector<uint32_t> p(N), ref(N), got(N);
        for (int task; (task = next++) < 24;) {
          const int f = task / 6, prod = (task / 3) % 2, l = task % 3;
          const uint32_t* src = payload.data() + (prod * 3 + l) * N;     // LOAD layout: b limbs then a limbs
          schoolbook_ternary(src, us[f].data(), Q[l], ref.data());
          for (int c = 0; c < N; c++) if (out_coeff(a, f, prod, l, c) != ref[c]) bad = true;
        }
      };
      std::thread t1(worker), t2(worker); worker(); t1.join(); t2.join();
      edgefhe::require(!bad, "M1 PAIR device != independent schoolbook");
      rep.pair_products += 4; rep.pair_limb_polys += 24; rep.coeffs_vs_schoolbook += 24 * uint64_t(N);
    }
  }
  // ENC: one dense LOAD, 200 frames
  const auto payload = dense_payload(g, 99);
  edgefhe::require(dev.load(payload) == 0 && spec.load(payload) == 0, "M1 ENC LOAD");
  for (int i = 0; i < 200; i++) {
    const uint32_t k = 1 + i % 8; uint8_t s[32]; for (auto& x : s) x = uint8_t(g()); s[0] |= 1;
    std::vector<uint32_t> in(8 + k * 2048 * 4);
    std::memcpy(in.data(), s, 32);
    for (uint32_t f = 0; f < k; f++) for (int c = 0; c < N; c++) {
      int64_t m;
      switch ((i + int(f)) % 5) {
        case 0: m = int64_t(g() % (uint64_t(1) << 41)) - (int64_t(1) << 40); break;      // encoder range
        case 1: m = (c % 2) ? (int64_t(1) << 62) - 1 : -(int64_t(1) << 62) + 1; break; // contract boundary
        case 2: m = 0; break;
        case 3: m = int64_t(g() >> 2) * ((g() & 1) ? 1 : -1); break;                   // |m| < 2^62
        default: m = int64_t(g() % 2000001) - 1000000;
      }
      std::memcpy(in.data() + 8 + (size_t(f) * N + c) * 2, &m, 8);
    }
    std::vector<uint32_t> a(k * W), b(k * W);
    edgefhe::require(dev.call(9, k, in.data(), in.size() * 4, a.data(), a.size() * 4) == 0, "M1 ENC on device");
    edgefhe::require(spec.call(9, k, in.data(), in.size() * 4, b.data(), b.size() * 4) == 0, "M1 ENC on spec");
    edgefhe::require(a == b, "M1 ENC device != specification");
    rep.enc_frames++; rep.enc_ciphertexts += k; rep.enc_words += a.size();
  }
}

struct Case { std::string name; int rc; bool refused; };

void c01(Link& dev, Link& spec, uint64_t seed, std::vector<Case>& cases, uint64_t& expected_dma) {
  std::mt19937_64 g(seed);
  const auto payload = dense_payload(g, 99);
  edgefhe::require(dev.load(payload) == 0 && spec.load(payload) == 0, "C01 LOAD");
  expected_dma = 1;
  std::vector<uint32_t> out(8 * W);
  auto enc_input = [&](uint32_t k) { std::vector<uint32_t> in(8 + k * 2048 * 4, 0); for (int i = 0; i < 8; i++) in[i] = uint32_t(g()) | 1u; return in; };
  auto refused = [&](const std::string& name, int rc) { cases.push_back({name, rc, rc != 0}); };
  // malformed frames (input checks) - refused before DMA
  { auto in = trit_payload({ternary(g, 99)}); in[5] |= 3u << 6; refused("pair_invalid_trit_code_3", dev.call(7, 1, in.data(), in.size() * 4, out.data(), W * 4)); }
  { std::vector<uint32_t> in(9 * 64 * 4, 0); refused("pair_k_9", dev.call(7, 9, in.data(), in.size() * 4, out.data(), out.size() * 4)); }
  { refused("pair_k_0", dev.call(7, 0, nullptr, 0, nullptr, 0)); }
  { auto bad = payload; bad[17] = Q[0]; refused("load_noncanonical_coefficient_q", dev.call(6, 0, bad.data(), bad.size() * 4, nullptr, 0)); }
  { auto in = enc_input(1); std::fill(in.begin(), in.begin() + 8, 0u); refused("enc_zero_seed", dev.call(9, 1, in.data(), in.size() * 4, out.data(), W * 4)); }
  { auto in = enc_input(1); const int64_t m = int64_t(1) << 62; std::memcpy(in.data() + 8, &m, 8); refused("enc_message_2_pow_62", dev.call(9, 1, in.data(), in.size() * 4, out.data(), W * 4)); }
  { auto in = enc_input(1); const int64_t m = -(int64_t(1) << 62); std::memcpy(in.data() + 8, &m, 8); refused("enc_message_minus_2_pow_62", dev.call(9, 1, in.data(), in.size() * 4, out.data(), W * 4)); }
  { auto in = enc_input(1); refused("enc_input_bytes_short", dev.call(9, 1, in.data(), in.size() * 4 - 16, out.data(), W * 4)); }
  { auto in = enc_input(1); refused("enc_output_bytes_wrong", dev.call(9, 1, in.data(), in.size() * 4, out.data(), W * 4 - 16)); }
  { auto in = enc_input(1); refused("unknown_command_5", dev.call(5, 1, in.data(), in.size() * 4, out.data(), W * 4)); }
  // lease violations - refused before DMA
  { auto in = enc_input(1); refused("enc_stale_generation", dev.call(9, 1, in.data(), in.size() * 4, out.data(), W * 4, dev.generation + 1)); }
  { auto in = enc_input(1); refused("enc_generation_0", dev.call(9, 1, in.data(), in.size() * 4, out.data(), W * 4, 0)); }
  { auto in = enc_input(1); uint8_t p[32]{}; p[0] = 1; refused("enc_wrong_profile", dev.call(9, 1, in.data(), in.size() * 4, out.data(), W * 4, UINT64_MAX, p)); }
  { auto in = enc_input(1); uint8_t k[32]; std::memcpy(k, dev.key.data(), 32); k[0] ^= 1; refused("enc_wrong_key_digest", dev.call(9, 1, in.data(), in.size() * 4, out.data(), W * 4, UINT64_MAX, nullptr, k)); }
  { refused("load_nonzero_generation", dev.call(6, 0, payload.data(), payload.size() * 4, nullptr, 0, 7)); }
  { uint8_t z[32]{}; refused("load_zero_key_digest", dev.call(6, 0, payload.data(), payload.size() * 4, nullptr, 0, UINT64_MAX, nullptr, z)); }
  // the lease survived every refusal: a valid ENC still equals the specification
  {
    auto in = enc_input(2); std::vector<uint32_t> a(2 * W), b(2 * W);
    const int rc = dev.call(9, 2, in.data(), in.size() * 4, a.data(), a.size() * 4); const int rs = spec.call(9, 2, in.data(), in.size() * 4, b.data(), b.size() * 4);
    edgefhe::require(rc == 0 && rs == 0 && a == b, "C01 lease must survive refused frames"); expected_dma++;
    cases.push_back({"valid_enc_after_refusals_equals_spec", 0, false});
  }
  // INVALIDATE is a real frame; afterwards the old generation is unusable; a new LOAD restores exact operation
  {
    const uint64_t old = dev.generation;
    edgefhe::require(dev.call(8, 0, nullptr, 0, nullptr, 0, old) == 0 && spec.call(8, 0, nullptr, 0, nullptr, 0, spec.generation) == 0, "C01 INVALIDATE"); expected_dma++;
    auto in = enc_input(1); refused("enc_after_invalidate_old_generation", dev.call(9, 1, in.data(), in.size() * 4, out.data(), W * 4, old));
    edgefhe::require(dev.load(payload) == 0 && spec.load(payload) == 0, "C01 reload"); expected_dma++;
    auto in2 = enc_input(3); std::vector<uint32_t> a(3 * W), b(3 * W);
    edgefhe::require(dev.call(9, 3, in2.data(), in2.size() * 4, a.data(), a.size() * 4) == 0 && spec.call(9, 3, in2.data(), in2.size() * 4, b.data(), b.size() * 4) == 0 && a == b,
                     "C01 exact after reload"); expected_dma++;
    cases.push_back({"reload_restores_exact_enc", 0, false});
  }
}
}  // namespace

int main(int argc, char** argv) {
  try {
    // board: <m1|c01> --permit-fd <n>; local QA: <m1|c01> --local-spec
    edgefhe::require(argc >= 3, "usage: suite_device <m1|c01> --permit-fd <n> | --local-spec");
    const std::string mode = argv[1]; const bool local = std::string(argv[2]) == "--local-spec";
    edgefhe::require(mode == "m1" || mode == "c01", "mode");
    if (!local) { edgefhe::require(argc == 4 && std::string(argv[2]) == "--permit-fd", "permit"); suite_permit::check(std::stoi(argv[3]), "EDGEFHE_SUITE_DEVICE_V1", mode); }
    std::unique_ptr<edgefhe_v8_v5::Node> node; if (!local) node.reset(new edgefhe_v8_v5::Node("/dev/edgefhe_v8b_dma"));
    edgefhe_v8_v5::SpecModel stand_in, reference;
    Link dev{[&](edgefhe_v8_request& r) { return local ? stand_in.run(r) : node->run(r); }};
    Link spec{[&](edgefhe_v8_request& r) { return reference.run(r); }};
    const uint64_t seed = mode == "m1" ? 20261024001ull : 20261024002ull;
    if (mode == "m1") {
      oracle_selfcheck();
      Report rep; m1(dev, spec, seed, rep);
      edgefhe::require(dev.dma == 50 + 250 + 1 + 200, "M1 device call accounting");
      std::printf("{\"status\":\"SUITE_M1_EXACT_PASS\",\"real_PL\":%s,\"PL_hz\":%u,\"seed\":%" PRIu64 ",\"loads\":51,\"pair_frames\":250,\"pair_products\":%" PRIu64
                  ",\"pair_limb_polynomials\":%" PRIu64 ",\"coefficients_vs_schoolbook\":%" PRIu64 ",\"pair_words_vs_spec\":%" PRIu64 ",\"enc_frames\":%" PRIu64
                  ",\"enc_ciphertexts\":%" PRIu64 ",\"enc_words_vs_spec\":%" PRIu64 ",\"device_calls\":%" PRIu64 "}\n",
                  local ? "false" : "true", local ? 0u : node->pl_hz, seed, rep.pair_products, rep.pair_limb_polys, rep.coeffs_vs_schoolbook, rep.coeffs_vs_spec,
                  rep.enc_frames, rep.enc_ciphertexts, rep.enc_words, dev.dma);
    } else {
      std::vector<Case> cases; uint64_t expected = 0; c01(dev, spec, seed, cases, expected);
      bool ok = dev.dma == expected;
      std::printf("{\"status\":\"%s\",\"real_PL\":%s,\"seed\":%" PRIu64 ",\"device_calls\":%" PRIu64 ",\"expected_device_calls\":%" PRIu64 ",\"cases\":[",
                  "SUITE_C01_DEVICE_CASES_RETURNED", local ? "false" : "true", seed, dev.dma, expected);
      for (size_t i = 0; i < cases.size(); i++) {
        std::printf("%s{\"case\":\"%s\",\"rc\":%d,\"refused\":%s}", i ? "," : "", cases[i].name.c_str(), cases[i].rc, cases[i].refused ? "true" : "false");
        if (cases[i].name.rfind("valid", 0) != 0 && cases[i].name.rfind("reload", 0) != 0) ok &= cases[i].refused;
      }
      std::printf("],\"all_malformed_refused\":%s}\n", ok ? "true" : "false");
      edgefhe::require(ok, "C01 a malformed frame was accepted or accounting differs");
    }
    return 0;
  } catch (const std::exception& e) { std::cerr << "STOP_SUITE_DEVICE: " << e.what() << '\n'; return 1; }
}
