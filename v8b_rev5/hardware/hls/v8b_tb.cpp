// C-simulation testbench of the V8b kernel against the reference KernelB (../ref/v8b_ref.hpp), beat for beat.
#include "v8b.hpp"
#include "v8_tables.hpp"
#include "../ref/v8b_ref.hpp"
#include <cstdio>
#include <random>
using namespace v8ref;

static int fails = 0;
static void check(bool ok, const char* what) { if (!ok) { std::printf("V8B_TB_FAIL %s\n", what); fails++; } }

static std::vector<Beat> run_hw(const std::vector<Beat>& frame, int bad_keep = -1) {
  hls::stream<beat_t> in, out;
  for (size_t i = 0; i < frame.size(); i++) {
    beat_t b; b.data = 0;
    for (int w = 0; w < 4; w++) b.data.range(32 * w + 31, 32 * w) = frame[i].w[w];
    b.keep = int(i) == bad_keep ? 0x0fff : 0xffff; b.strb = -1; b.last = i + 1 == frame.size();
    in.write(b);
  }
  edgefhe_v8b(in, out);
  std::vector<Beat> r;
  bool last = false;
  while (!out.empty()) {
    beat_t b = out.read();
    check(!last, "beat after TLAST");
    Beat x; for (int w = 0; w < 4; w++) x.w[w] = b.data.range(32 * w + 31, 32 * w);
    r.push_back(x); last = b.last;
  }
  check(last, "TLAST on the final beat");
  check(in.empty(), "input fully consumed");
  return r;
}
static bool same(const std::vector<Beat>& a, const std::vector<Beat>& b) {
  if (a.size() != b.size()) return false;
  for (size_t i = 0; i < a.size(); i++) for (int w = 0; w < 4; w++) if (a[i].w[w] != b[i].w[w]) return false;
  return true;
}

int main() {
  std::mt19937_64 g(20261016);
  KernelB ref;
  std::array<Poly, 3> pb, pa;
  for (int l = 0; l < 3; l++) { pb[l].resize(N); pa[l].resize(N); for (auto& x : pb[l]) x = uint32_t(g() % Q[l]); for (auto& x : pa[l]) x = uint32_t(g() % Q[l]); }
  auto tern = [&](int k) { std::vector<std::vector<int8_t>> u(k, std::vector<int8_t>(N)); for (auto& v : u) for (auto& x : v) x = int8_t(int(g() % 3) - 1); return u; };
  auto msgs = [&](int k) {
    std::vector<std::vector<int64_t>> m(k, std::vector<int64_t>(N));
    for (auto& v : m) for (auto& x : v) x = int64_t(g() % (uint64_t(1) << 50)) - (int64_t(1) << 49);
    m[0][1] = (int64_t(1) << 62) - 1; m[0][2] = -((int64_t(1) << 62) - 1); m[0][3] = 0; m[0][4] = -1;
    return m;
  };
  uint8_t seed[32]; for (auto& x : seed) x = uint8_t(g());
  int frames = 0, beats = 0;
  auto step = [&](const std::vector<Beat>& f, const char* name, int bad_keep = -1) {
    const auto want = run_keep(ref, f, bad_keep < 0), got = run_hw(f, bad_keep);
    frames++; beats += int(got.size());
    if (!same(want, got)) {
      std::printf("V8B_TB_MISMATCH %s want=%zu got=%zu\n", name, want.size(), got.size()); fails++;
      if (!got.empty()) std::printf("  got trailer %08x %08x %08x %08x\n", got.back().w[0], got.back().w[1], got.back().w[2], got.back().w[3]);
      if (!want.empty()) std::printf("  want trailer %08x %08x %08x %08x\n", want.back().w[0], want.back().w[1], want.back().w[2], want.back().w[3]);
    } else std::printf("V8B_TB_FRAME_OK %s beats=%zu status=%u\n", name, got.size(), got.back().w[1]);
  };
  {  // rev5: the post-frame scrub batch must reproduce the RFC 8439 zero block of the frozen PS ChaCha20
    const uint32_t zk[8] = {0, 0, 0, 0, 0, 0, 0, 0}, zn[3] = {0, 0, 0}; uint32_t w[16];
    check(edgefhe::chacha::keystream(zk, 0, zn, w, 1), "zero-key keystream");
    for (int i = 0; i < 16; i++) check(w[i] == V8_CHACHA_ZERO_BLOCK[i], "RFC 8439 zero block constant");
  }
  step(load_frame(pb, pa, 31), "LOAD");
  step(enc_frame(seed, msgs(1), 31), "ENC k=1");
#ifdef V8B_SHORT   // RTL co-simulation subset
  step(enc_frame(seed, msgs(1), 31), "ENC partial TKEEP (message beat)", 3 + 100);
  step(enc_frame(seed, msgs(1), 31), "ENC after keep failure (generation)");
  step(load_frame(pb, pa, 31), "reLOAD (generation 31 again)");
  { auto bad = enc_frame(seed, msgs(1), 31); bad[3].w[1] = 0x40000000u; step(bad, "ENC message out of range"); }
  step(enc_frame(seed, msgs(1), 31), "ENC after failure (generation)");
#ifdef V8B_COSIM_R2   // rev5: scrub token paths in RTL (PAIR scrub, back-to-back ENC, PAIR partial keep)
  step(load_frame(pb, pa, 32), "reLOAD");
  step(pair_frame(tern(1), 32), "PAIR k=1");
  for (int r = 0; r < 2; r++) { for (auto& x : seed) x = uint8_t(g()); step(enc_frame(seed, msgs(2), 32), "ENC k=2 back-to-back after scrub"); }
  step(pair_frame(tern(1), 32), "PAIR partial TKEEP", 10);
  step(load_frame(pb, pa, 33), "reLOAD");
  step(enc_frame(seed, msgs(1), 33), "ENC after PAIR keep failure and reLOAD");
#endif
  if (fails == 0) std::printf("V8B_COSIM_SUBSET_PASS frames=%d output_beats=%d\n", frames, beats);
  return fails ? 1 : 0;
#endif
  for (auto& x : seed) x = uint8_t(g());
  step(enc_frame(seed, msgs(4), 31), "ENC k=4");
  step(pair_frame(tern(2), 31), "PAIR k=2 (V8a path kept)");
  { auto bad = enc_frame(seed, msgs(2), 31); bad[3 + 2048 + 5].w[3] = 0x80000000u; step(bad, "ENC message out of range (feature 1)"); }
  step(enc_frame(seed, msgs(1), 31), "ENC after failure (generation)");
  step(load_frame(pb, pa, 32), "reLOAD");
  { auto s = enc_frame(seed, msgs(2), 32); s.pop_back(); step(s, "ENC short frame"); }
  step(load_frame(pb, pa, 33), "reLOAD");
  { auto s = enc_frame(seed, msgs(1), 33); s.resize(2); step(s, "ENC truncated inside the seed"); }
  step(load_frame(pb, pa, 34), "reLOAD");
  step({header(9, 0, 34)}, "ENC k=0");
  step(load_frame(pb, pa, 35), "reLOAD");
  for (auto& x : seed) x = 0xff;
  step(enc_frame(seed, msgs(8), 35), "ENC k=8, all-ones seed");
  step({header(8, 0, 0)}, "INVALIDATE");
  step(enc_frame(seed, msgs(1), 35), "ENC after INVALIDATE");
  // rev5 TKEEP rule (review H4) and repeated frames after the scrub passes
  step(load_frame(pb, pa, 36), "LOAD partial TKEEP (header)", 0);
  step(enc_frame(seed, msgs(1), 36), "ENC after bad-keep LOAD (generation)");
  step(load_frame(pb, pa, 37), "reLOAD");
  step(enc_frame(seed, msgs(2), 37), "ENC partial TKEEP (seed beat)", 1);
  step(load_frame(pb, pa, 38), "reLOAD");
  step(pair_frame(tern(1), 38), "PAIR partial TKEEP", 10);
  step(load_frame(pb, pa, 39), "reLOAD");
  { auto bad = enc_frame(seed, msgs(1), 39); bad[3].w[1] = 0x40000000u; step(bad, "ENC message error + partial TKEEP (length wins)", 3 + 7); }
  step(load_frame(pb, pa, 40), "reLOAD");
  step({header(9, 9, 40)}, "ENC k=9 header error + partial TKEEP (header wins)", 0);
  step(load_frame(pb, pa, 41), "reLOAD");
  for (int r = 0; r < 3; r++) { for (auto& x : seed) x = uint8_t(g()); step(enc_frame(seed, msgs(3), 41), "ENC k=3 back-to-back after scrub"); }
  step(pair_frame(tern(3), 41), "PAIR k=3 after ENC scrub");
  step(enc_frame(seed, msgs(1), 41), "ENC after PAIR scrub");
  if (fails == 0) std::printf("V8B_CSIM_PASS frames=%d output_beats=%d reference=v8b_ref_kernel_b\n", frames, beats);
  return fails ? 1 : 0;
}
