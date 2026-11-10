// API-level timing of one W2-like request (10 features x 2048 records): owner encrypt -> cloud affine -> owner decrypt.
// The same source is built against the v2 and the v3 application libraries. Usage: ef_app_owner_timing <plugin> <backend> [reps]
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <random>
#include <string>
#include <vector>
extern "C" {
void* ef_owner_create(unsigned, unsigned, const char*);
void ef_owner_destroy(void*);
const char* ef_owner_error();
int ef_owner_encrypt(void*, const double*, size_t, size_t, uint32_t*, size_t);
int ef_owner_decrypt(void*, const uint32_t*, size_t, size_t, double*);
int ef_cloud_affine(const uint32_t*, size_t, size_t, const int64_t*, int64_t, uint32_t*, size_t);
#ifdef EF_V3
int ef_owner_error_bound(void*, const int64_t*, const double*, size_t, int64_t, double, double, double, double*);
#endif
}
int main(int argc, char** argv) {
  if (argc < 3) return 2;
  const unsigned backend = unsigned(std::stoul(argv[2])); const int reps = argc > 3 ? std::stoi(argv[3]) : 10;
  void* owner = ef_owner_create(3, backend, argv[1]);
  if (!owner) { std::printf("create failed: %s\n", ef_owner_error()); return 1; }
  const size_t rows = 2048, F = 10, P = 12, W = 6 * 4096;
  // diabetes-progression-ridge-v1 weights (public model)
  const double w[10] = {-0.04710255273010207, -0.14480788439881626, 0.35822919922441115, 0.1347947746537797, -0.3849652805990932,
                        0.2182873124479204, 0.04079170692598594, 0.08759019183329089, 0.43771133842934856, 0.09575395514088171};
  const int64_t wi[10] = {-50575981, -155486282, 384645674, 144734787, -413353323, 234384217, 43799762, 94049252, 469988971, 102815026};
  std::vector<int64_t> weights(P, 0); for (size_t f = 0; f < F; f++) weights[f] = wi[f];
  std::mt19937_64 rng(7); std::normal_distribution<double> nd(0, 1);
  std::vector<double> x(rows * F); for (auto& v : x) v = std::max(-8.0, std::min(8.0, nd(rng)));
  std::vector<uint32_t> ct(P * W), res(W); std::vector<double> out(rows);
  std::vector<double> te, tc, td; double maxerr = 0;
  using clk = std::chrono::steady_clock;
  auto ms = [](clk::time_point a, clk::time_point b) { return std::chrono::duration<double, std::milli>(b - a).count(); };
  for (int r = 0; r < reps + 2; r++) {
    auto a = clk::now();
    if (ef_owner_encrypt(owner, x.data(), rows, F, ct.data(), ct.size())) { std::printf("encrypt failed: %s\n", ef_owner_error()); return 1; }
    auto b = clk::now();
    if (ef_cloud_affine(ct.data(), ct.size(), P, weights.data(), 4781, res.data(), res.size())) { std::printf("cloud failed\n"); return 1; }
    auto c = clk::now();
    if (ef_owner_decrypt(owner, res.data(), res.size(), rows, out.data())) { std::printf("decrypt failed: %s\n", ef_owner_error()); return 1; }
    auto d = clk::now();
    if (r >= 2) { te.push_back(ms(a, b)); tc.push_back(ms(b, c)); td.push_back(ms(c, d)); }
    for (size_t i = 0; i < rows; i++) { double y = 4.146895757484905e-15; for (size_t f = 0; f < F; f++) y += w[f] * x[i * F + f]; maxerr = std::max(maxerr, std::fabs(out[i] - y)); }
  }
  auto med = [](std::vector<double> v) { std::sort(v.begin(), v.end()); return v[v.size() / 2]; };
  std::printf("OWNER_TIMING backend=%u reps=%d encrypt_ms=%.3f cloud_ms=%.3f decrypt_ms=%.3f total_ms=%.3f max_error=%.3e\n", backend, reps,
              med(te), med(tc), med(td), med(te) + med(tc) + med(td), maxerr);
#ifdef EF_V3
  double bound = 0;
  if (ef_owner_error_bound(owner, wi, w, F, 4781, 4.146895757484905e-15, 8, 40, &bound)) { std::printf("bound failed: %s\n", ef_owner_error()); return 1; }
  std::printf("OWNER_BOUND certified_2^-40=%.3e observed_max=%.3e within=%d\n", bound, maxerr, int(maxerr <= bound));
  if (maxerr > bound) return 1;
#endif
  ef_owner_destroy(owner);
  return 0;
}
