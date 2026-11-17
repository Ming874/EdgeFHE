// app v5 real-PL exactness check (2026-10-22). With a throwaway key and fixed public test seeds, the v5 PL engine
// (frames of <= 4 real features, helper-thread pipeline, two device buffers) must return, for F = 1..16 features and
// 2048 rows, ciphertext words identical to the V8b specification model behind the same driver checks. Every frame
// also decrypts to its message within the noise range. Device calls: 1 LOAD + sum_F ceil(F/4) = 41.
// Requires a root-controller inherited, invocation-bound permit (EDGEFHE_APP_V5_PL_CHECK_V1), checked before open.
#include "owner_v5_core.hpp"
#include "pl_permit.hpp"
#include <charconv>
#include <cstdio>
#include <iostream>
#include <openssl/sha.h>
#include <memory>
#include <random>
using namespace edgefhe;
namespace {
constexpr size_t W = 6 * degree;
std::string canonical(const PermitIdentity& i) {
  return "EDGEFHE_APP_V5_PL_CHECK_V1\n" + i.boot + '\n' + std::to_string(i.parent) + '\n' + std::to_string(i.device) + '\n' + std::to_string(i.inode) + '\n';
}
void check_permit(int fd) {
  permit_need(fd >= 3 && ::geteuid() == 0 && ::getuid() == 0);
  struct stat st{}, exe{}; permit_need(::fstat(fd, &st) == 0 && ::stat("/proc/self/exe", &exe) == 0 && st.st_size > 0 && st.st_size < 1024);
  char bytes[1024]{}; const auto count = ::pread(fd, bytes, size_t(st.st_size), 0); permit_need(count == st.st_size);
  std::ifstream file("/proc/sys/kernel/random/boot_id"); std::string boot; std::getline(file, boot); permit_need(bool(file) && boot.size() == 36);
  const int flags = ::fcntl(fd, F_GETFL); permit_need(flags >= 0);
  PermitIdentity i; i.root = true; i.regular = S_ISREG(st.st_mode); i.readonly = (flags & O_ACCMODE) == O_RDONLY; i.uid = st.st_uid; i.mode = st.st_mode & 07777;
  i.links = st.st_nlink; i.parent = ::getppid(); i.device = exe.st_dev; i.inode = exe.st_ino; i.boot = boot;
  permit_need(i.regular && i.readonly && i.uid == 0 && i.mode == 0600 && i.links == 1 && std::string(bytes, size_t(count)) == canonical(i));
  permit_need(::close(fd) == 0);
}
struct Seeds { uint32_t stream; uint32_t counter = 0;
  void operator()(uint8_t* out) { uint8_t in[21] = "edgefhe-v5-pl"; std::memcpy(in + 13, &stream, 4); std::memcpy(in + 17, &counter, 4); counter++; SHA256(in, sizeof in, out); } };
int64_t noise(const fast::NttSet& set, const fast::SecretTransform& sk, const uint32_t* ct, const Message& m) {
  Rns sum; for (auto& s : sum) s.resize(degree);
  for (size_t l = 0; l < 3; l++) { const auto& nt = set.ntt[l]; uint32_t* o = sum[l].data(); std::memcpy(o, ct + 3 * degree + l * degree, degree * 4); nt.transform(o, false);
    for (size_t i = 0; i < degree; i++) o[i] = nt.product(o[i], sk.hat[l][i]);
    nt.transform(o, true); for (size_t i = 0; i < degree; i++) o[i] = fast::csub(o[i] + ct[l * degree + i], primes[l]); }
  const auto poly = centered(sum); Wide worst = 0;
  for (size_t i = 0; i < degree; i++) { Wide d = poly[i] - Wide(m[i]); if (d < 0) d = -d; worst = std::max(worst, d); }
  require(worst < (Wide(1) << 20), "decrypted noise out of range"); return int64_t(worst);
}
}
int main(int argc, char** argv) {
  try {
    // board: --plugin <so> --permit-fd <n>; local QA only: --local-spec <so> (the specification model stands in for the node)
    const bool local = argc == 3 && std::string(argv[1]) == "--local-spec";
    require(local || (argc == 5 && std::string(argv[1]) == "--plugin" && std::string(argv[3]) == "--permit-fd"), "usage: ef_app_v5_pl_check --plugin <so> --permit-fd <n>");
    if (!local) { int fd = -1; const std::string v = argv[4]; const auto r = std::from_chars(v.data(), v.data() + v.size(), fd);
      require(r.ec == std::errc{} && fd >= 3 && fd <= 1024, "permit fd"); check_permit(fd); }
    initialize_secure_sampler(argv[2]);
    Engine eng(3); SecureMaterial material = sample_secure_material(); PublicKey key = keygen_secure(eng, material);
    fast::NttSet set; fast::Fft fft; fast::SecretTransform sk; sk.build(set, material.secret); v3::KeySpectra spectra; spectra.build(set, key);
    WorkerPool math(3), enc(3); v5::Engine e5(set, fft, spectra, math, enc);
    std::unique_ptr<edgefhe_v8_v5::Node> node; if (!local) node.reset(new edgefhe_v8_v5::Node("/dev/edgefhe_v8b_dma"));
    edgefhe_v8_v5::SpecModel stand_in; edgefhe_v8_v5::Device dev([&](edgefhe_v8_request& r) { return local ? stand_in.run(r) : node->run(r); }); dev.load(key);
    edgefhe_v8_v5::SpecModel spec; edgefhe_v8_v5::Device sdev([&](edgefhe_v8_request& r) { return spec.run(r); }); sdev.load(key);
    std::mt19937_64 rng(20261023); std::normal_distribution<double> nd(0, 1.5); size_t compared = 0; int64_t worst = 0;
    for (size_t F = 1; F <= 16; F++) {
      const size_t rows = 2048, P = (F + 3) / 4 * 4; std::vector<double> v(rows * F); for (auto& x : v) x = std::max(-8.0, std::min(8.0, nd(rng)));
      std::vector<uint32_t> a(P * W, 1), b(P * W, 2); Seeds s1{uint32_t(F)}, s2{uint32_t(F)};
      e5.encrypt_device(dev, v.data(), rows, F, a.data(), s1); e5.encrypt_device(sdev, v.data(), rows, F, b.data(), s2);
      require(a == b, "real PL v5 output differs from the V8b specification");
      for (size_t f = 0; f < F; f++) {   // re-encode to recover the exact message of feature f
        std::vector<std::complex<double>> x(slots), sc; for (size_t r = 0; r < rows; r++) x[r] = {v[r * F + f], 0};
        Message m(degree); fast::encode_into(fft, x, scale, m.data(), sc); worst = std::max(worst, noise(set, sk, a.data() + f * W, m)); compared++;
      }
    }
    require(dev.calls() == 41 && spec.frames == 41, "device accounting");
    std::printf("{\"status\":\"APP_V5_PL_CHECK_PASS\",\"real_PL\":%s,\"PL_hz\":%u,\"features\":\"1..16\",\"rows\":2048,\"ciphertexts_spec_equal\":%zu,\"max_abs_noise\":%lld,\"device_protocol_calls\":%llu}\n",
                local ? "false" : "true", local ? 0u : node->pl_hz, compared, (long long)worst, (unsigned long long)dev.calls());
    return 0;
  } catch (const std::exception& e) { std::cerr << "STOP_APP_V5_PL_CHECK: " << e.what() << '\n'; return 1; }
}
