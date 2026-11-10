// EdgeFHE V8b HLS kernel (2026-10-16): V8a (LOAD / PAIR / INVALIDATE) + ENC, the full public-key encryption of the
// proposal's dataflow in the PL. Specification: ../ref/v8b_ref.hpp (KernelB); C-sim compares beat for beat.
//
// ENC (command 9): the PS sends a 32-byte seed and the encoded message m (int64 coefficients); the PL
//   - expands the seed with ChaCha20 exactly as the PS sampler B (key = seed, nonce (purpose, f, 0)):
//       mask u_f: 5 trits per accepted keystream byte; e0_f, e1_f: constant-time CDT63, one 64-bit word each
//   - reduces m to the three limbs (|m| < 2^62, else status 6)
//   - computes c0 = b*u + e0 + m and c1 = a*u + e1 with the V8a NTT datapath and outputs both
// Dataflow: reader (frame, seed, message) | sampler (ChaCha20 + CDT63 + ternary) | 3 limb engines | writer.
// Resource revision (csynth v1: LUT 120%): one interleaved ChaCha20 core and one CDT unit, one message coefficient
// per cycle in the reader, aligned 4-bank twiddle reads, signed-byte e tokens.
// The limb engines zero their working buffers at the end of every PAIR / ENC frame (the mask spectrum and the
// products are secret-derived).
// rev5 (2026-10-29, review H2/H3/H4): the seed travels only to the sampler, followed by two zero tokens that overwrite
// its control FIFO; after every ENC frame the sampler runs one zero-key ChaCha batch (overwrites the shared core, key
// and block registers; block 0 must equal the RFC 8439 zero vector, else trailer status 7) and refills the noise and
// mask FIFOs with zeros; the reader refills the message FIFOs (ENC) and the trit FIFOs (PAIR) with zeros. Every input
// beat must carry TKEEP = all ones, otherwise the frame ends with ST_LENGTH. Single last-value pipeline registers are
// not scrubbed at source level (residency inventory, not a physical zeroization proof).
#include "v8b.hpp"
#include "v8_tables.hpp"
#include "../frozen_math/cdt63_table.hpp"

static const int N = 4096, LOGN = 12, BANK = 1024;
static const u32 MAGIC = 0x45463841u;
static const unsigned ST_OK = 0, ST_HEADER = 1, ST_GENERATION = 2, ST_LENGTH = 3, ST_TRIT = 4, ST_KEY = 5, ST_MESSAGE = 6, ST_SELFTEST = 7;
static const unsigned KEEP_ALL = 0xFFFF;
typedef u32 poly_t[4][BANK];   // coefficient i at bank i % 4, address i / 4

static u32 mod_add(u32 a, u32 b, u32 q) {
#pragma HLS INLINE
  ap_uint<33> s = ap_uint<33>(a) + b;
  return s >= q ? u32(s - q) : u32(s);
}
static u32 mod_sub(u32 a, u32 b, u32 q) {
#pragma HLS INLINE
  return a >= b ? u32(a - b) : u32(a + q - b);
}
// Revision 4: each multiplication is a pipelined DSP (latency 3/2/3). System build v1 (rev3, 200 MHz) failed route by
// -0.212 ns: latency-1 multipliers of mont() in the NTT butterflies were chained DSP-to-DSP (396 failing endpoints).
static u32 mont(u32 a, u32 b, u32 q, u32 qp) {
#pragma HLS INLINE
  const ap_uint<64> p = ap_uint<64>(a) * b;
#pragma HLS BIND_OP variable=p op=mul impl=dsp latency=3
  const u32 m = u32(p.range(31, 0)) * qp;
#pragma HLS BIND_OP variable=m op=mul impl=dsp latency=2
  const ap_uint<64> mq = ap_uint<64>(m) * q;
#pragma HLS BIND_OP variable=mq op=mul impl=dsp latency=3
  const ap_uint<65> t = (ap_uint<65>(p) + mq) >> 32;
  return t >= q ? u32(t - q) : u32(t);
}
// x in (-2^62, 2^62) as two's complement -> x mod q (canonical). x + 2^62 = H * 2^32 + L, H < 2^31.
static u32 residue64(ap_uint<64> x, int l) {
#pragma HLS INLINE
  const u32 q = V8_Q[l], qp = V8_QP[l], r2 = V8_R2MOD[l];
  const ap_uint<64> y = x + (ap_uint<64>(1) << 62);
  const u32 H = y.range(62, 32), L = y.range(31, 0);
  const u32 r = mod_add(mont(H, r2, q, qp), mont(mont(L, r2, q, qp), 1, q, qp), q);   // H*R + L (mod q)
  return mod_sub(r, V8_OFF62[l], q);
}
static u32 residue_small(ap_int<8> e, int l) {
#pragma HLS INLINE
  return e < 0 ? u32(u32(V8_Q[l]) - u32(ap_uint<8>(-e))) : u32(ap_uint<8>(e));
}

// One twiddle ROM per limb and direction.
template<int L> static u32 rom_psi(int i);
template<int L> static u32 rom_psi_inv(int i);
template<> u32 rom_psi<0>(int i) {
#pragma HLS INLINE
  return V8_PSI_M_0[i];
}
template<> u32 rom_psi<1>(int i) {
#pragma HLS INLINE
  return V8_PSI_M_1[i];
}
template<> u32 rom_psi<2>(int i) {
#pragma HLS INLINE
  return V8_PSI_M_2[i];
}
template<> u32 rom_psi_inv<0>(int i) {
#pragma HLS INLINE
  return V8_PSI_INV_M_0[i];
}
template<> u32 rom_psi_inv<1>(int i) {
#pragma HLS INLINE
  return V8_PSI_INV_M_1[i];
}
template<> u32 rom_psi_inv<2>(int i) {
#pragma HLS INLINE
  return V8_PSI_INV_M_2[i];
}

// ---- ChaCha20 (RFC 8439) and CDT63 --------------------------------------------------------------------------------
// One double-round datapath shared by 8 blocks in flight (round-robin, dependence distance 8): 8 blocks per ~80 cycles.
// csynth v1 unrolled two complete ChaCha20 instances and eight CDT units (sampler 63.7k LUT, design 120% LUT).
static u32 rotl(u32 v, int n) {
#pragma HLS INLINE
  return (v << n) | (v >> (32 - n));
}
#define V8_QR(a, b, c, d) \
  x[a] += x[b]; x[d] ^= x[a]; x[d] = rotl(x[d], 16); x[c] += x[d]; x[b] ^= x[c]; x[b] = rotl(x[b], 12); \
  x[a] += x[b]; x[d] ^= x[a]; x[d] = rotl(x[d], 8);  x[c] += x[d]; x[b] ^= x[c]; x[b] = rotl(x[b], 7);
static const int CC_SLOTS = 8;
// Revision 3: an explicit 8-stage systolic double round. Stage k applies one quarter-round step to the four
// independent quarter rounds of the column (k < 4) or diagonal (k >= 4) round: one 32-bit add, xor and rotation per
// word, i.e. one register-to-register step per cycle. The 8 blocks circulate (stage 7 feeds stage 0), so each block
// completes 10 double rounds in 80 cycles. Revision 2 (FIFO ring) was scheduled at depth 2, slack -10.64 ns at 5 ns.
static const int CC_COL[4][4] = {{0, 4, 8, 12}, {1, 5, 9, 13}, {2, 6, 10, 14}, {3, 7, 11, 15}};
static const int CC_DIAG[4][4] = {{0, 5, 10, 15}, {1, 6, 11, 12}, {2, 7, 8, 13}, {3, 4, 9, 14}};
static void cc_step(int k, const u32 x[16], u32 y[16]) {
#pragma HLS INLINE
  for (int i = 0; i < 16; i++) y[i] = x[i];
  for (int q = 0; q < 4; q++) {
    const int a = k < 4 ? CC_COL[q][0] : CC_DIAG[q][0];
    const int b = k < 4 ? CC_COL[q][1] : CC_DIAG[q][1];
    const int c = k < 4 ? CC_COL[q][2] : CC_DIAG[q][2];
    const int d = k < 4 ? CC_COL[q][3] : CC_DIAG[q][3];
    switch (k % 4) {
      case 0: y[a] = x[a] + x[b]; y[d] = rotl(x[d] ^ y[a], 16); break;
      case 1: y[c] = x[c] + x[d]; y[b] = rotl(x[b] ^ y[c], 12); break;
      case 2: y[a] = x[a] + x[b]; y[d] = rotl(x[d] ^ y[a], 8); break;
      default: y[c] = x[c] + x[d]; y[b] = rotl(x[b] ^ y[c], 7); break;
    }
  }
}
static void chacha8(const u32 key[8], u32 counter0, u32 n0, u32 n1, u32 out[CC_SLOTS][16]) {
#pragma HLS INLINE off
  u32 st[CC_SLOTS][16];
#pragma HLS ARRAY_PARTITION variable=st complete dim=0
  u32 init[CC_SLOTS][16];
#pragma HLS ARRAY_PARTITION variable=init complete dim=0
  for (int b = 0; b < CC_SLOTS; b++) {
#pragma HLS UNROLL
    init[b][0] = 0x61707865u; init[b][1] = 0x3320646eu; init[b][2] = 0x79622d32u; init[b][3] = 0x6b206574u;
    for (int i = 0; i < 8; i++) init[b][4 + i] = key[i];
    init[b][12] = counter0 + b; init[b][13] = n0; init[b][14] = n1; init[b][15] = 0;
    for (int i = 0; i < 16; i++) st[b][i] = 0;
  }
  // cycle t: block t enters stage 0 (t < 8); block b leaves stage 7 after its 10th double round at the end of cycle
  // b + 79 and is emitted in cycle b + 80.
cycles:
  for (int t = 0; t < 10 * CC_SLOTS + CC_SLOTS; t++) {
#pragma HLS PIPELINE II=1
    if (t >= 10 * CC_SLOTS) {
      const int b = t - 10 * CC_SLOTS;
      for (int i = 0; i < 16; i++) out[b][i] = st[CC_SLOTS - 1][i] + init[b][i];
    }
    u32 nx[CC_SLOTS][16];
#pragma HLS ARRAY_PARTITION variable=nx complete dim=0
    u32 in[16];
#pragma HLS ARRAY_PARTITION variable=in complete
    for (int i = 0; i < 16; i++) in[i] = t < CC_SLOTS ? init[t % CC_SLOTS][i] : st[CC_SLOTS - 1][i];
    cc_step(0, in, nx[0]);
    for (int k = 1; k < CC_SLOTS; k++) cc_step(k, st[k - 1], nx[k]);
    for (int k = 0; k < CC_SLOTS; k++)
      for (int i = 0; i < 16; i++) st[k][i] = nx[k][i];
  }
}
static ap_int<8> cdt63(ap_uint<64> w) {
#pragma HLS INLINE
  const ap_uint<63> r = w.range(62, 0);
  ap_uint<7> v = 0;
  for (unsigned k = 0; k < edgefhe::cdt63::K; k++) {
#pragma HLS UNROLL
    v += ap_uint<1>(r >= ap_uint<63>(edgefhe::cdt63::T[k]));
  }
  return w[63] ? ap_int<8>(-ap_int<8>(v)) : ap_int<8>(v);
}

// ---- reader ------------------------------------------------------------------------------------------------------
static void reader(hls::stream<beat_t>& in, hls::stream<ctl_t>& c0, hls::stream<ctl_t>& c1, hls::stream<ctl_t>& c2,
                   hls::stream<ctl_t>& cw, hls::stream<sampler_ctl_t>& cs, hls::stream<v4_t>& s0, hls::stream<v4_t>& s1,
                   hls::stream<v4_t>& s2, hls::stream<v4_t>& m0, hls::stream<v4_t>& m1, hls::stream<v4_t>& m2,
                   hls::stream<ap_uint<8> >& status_out) {
  static ap_uint<1> valid = 0;
  static ap_uint<64> epoch = 0;
#pragma HLS RESET variable=valid
#pragma HLS RESET variable=epoch
  beat_t h = in.read();
  bool ended = h.last;
  const ap_uint<128> hd = h.data;
  const ap_uint<8> command = hd.range(39, 32), k = hd.range(47, 40);
  const ap_uint<64> gen = (ap_uint<64>(hd.range(127, 96)) << 32) | ap_uint<64>(hd.range(95, 64));
  unsigned head = ST_OK, content = ST_OK;
  bool len_err = h.keep != KEEP_ALL, run = false;
  if (hd.range(31, 0) != MAGIC) head = ST_HEADER;
  else if (command == 8) { /* header + trailer only */ }
  else if (command == 6) { if (gen == 0) head = ST_GENERATION; else if (k != 0) head = ST_HEADER; else run = true; }
  else if (command == 7 || command == 9) {
    if (k < 1 || k > 8) head = ST_HEADER;
    else if (valid == 0 || gen != epoch || gen == 0) head = ST_GENERATION;
    else run = true;
  } else head = ST_HEADER;
  ap_uint<256> seed = 0;
  if (run && command == 9) {
  seed_beats:
    for (int b = 0; b < 2; b++) {
      ap_uint<128> d = 0;
      if (!ended) { beat_t x = in.read(); d = x.data; ended = x.last; if (ended || x.keep != KEEP_ALL) len_err = true; }   // message beats must follow
      else len_err = true;
      seed.range(128 * b + 127, 128 * b) = d;
    }
  }
  ctl_t c; c.command = command; c.k = k; c.gen = gen; c.run = run;
  sampler_ctl_t sc; sc.command = command; sc.k = k; sc.run = run; sc.seed = seed;
  c0.write(c); c1.write(c); c2.write(c); cw.write(c); cs.write(sc);
  sampler_ctl_t zero; zero.command = 0; zero.k = 0; zero.run = 0; zero.seed = 0;
  cs.write(zero); cs.write(zero);   // depth-2 FIFO: the seed slot is overwritten once the sampler has read it
  seed = 0;
  if (run && command == 6) {
  load_beats:
    for (int i = 0; i < 2 * 3 * BANK; i++) {
#pragma HLS PIPELINE II=1
      ap_uint<128> d = 0;
      if (!ended) { beat_t b = in.read(); d = b.data; ended = b.last; if ((ended && i != 2 * 3 * BANK - 1) || b.keep != KEEP_ALL) len_err = true; }
      else len_err = true;
      const int limb = (i / BANK) % 3;
      ap_uint<128> v = 0;
      for (int c4 = 0; c4 < 4; c4++) {
        u32 x = d.range(32 * c4 + 31, 32 * c4);
        if (x >= V8_Q[limb]) { x = 0; content = ST_KEY; }
        v.range(32 * c4 + 31, 32 * c4) = x;
      }
      if (limb == 0) s0.write(v); else if (limb == 1) s1.write(v); else s2.write(v);
    }
  } else if (run && command == 7) {
    ap_uint<128> d = 0;
  pair_tokens:
    for (int i = 0; i < int(k) * 64 * 16; i++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1024 max=8192
      if ((i & 15) == 0) {
        d = 0;
        if (!ended) { beat_t b = in.read(); d = b.data; ended = b.last; if ((ended && i != int(k) * 1024 - 16) || b.keep != KEEP_ALL) len_err = true; }
        else len_err = true;
      }
      ap_uint<8> t = d.range(8 * (i & 15) + 7, 8 * (i & 15));
      for (int c4 = 0; c4 < 4; c4++) {
        if (t.range(2 * c4 + 1, 2 * c4) == 3) { t.range(2 * c4 + 1, 2 * c4) = 0; content = ST_TRIT; }
      }
      v4_t v = t;
      s0.write(v); s1.write(v); s2.write(v);
    }
  pair_scrub:
    for (int i = 0; i < 32; i++) {   // s0-s2 depth 32: the trit FIFOs end holding zeros
#pragma HLS PIPELINE II=1
      s0.write(0); s1.write(0); s2.write(0);
    }
  } else if (run && command == 9) {
    v4_t a0 = 0, a1 = 0, a2 = 0;
    ap_uint<128> d = 0;
  message_coeffs:
    for (int i = 0; i < int(k) * 4096; i++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=4096 max=32768
      if ((i & 1) == 0) {
        d = 0;
        if (!ended) { beat_t b = in.read(); d = b.data; ended = b.last; if ((ended && i != int(k) * 4096 - 2) || b.keep != KEEP_ALL) len_err = true; }
        else len_err = true;
      }
      ap_uint<64> x = (i & 1) ? ap_uint<64>(d.range(127, 64)) : ap_uint<64>(d.range(63, 0));
      const ap_int<64> sx = x;
      if (sx >= (ap_int<64>(1) << 62) || sx <= -(ap_int<64>(1) << 62)) { x = 0; content = ST_MESSAGE; }
      const int lane = i & 3;
      a0.range(32 * lane + 31, 32 * lane) = residue64(x, 0);
      a1.range(32 * lane + 31, 32 * lane) = residue64(x, 1);
      a2.range(32 * lane + 31, 32 * lane) = residue64(x, 2);
      if (lane == 3) { m0.write(a0); m1.write(a1); m2.write(a2); }
    }
    a0 = 0; a1 = 0; a2 = 0;
  message_scrub:
    for (int i = 0; i < 1024; i++) {   // m0-m2 depth 1024: the message FIFOs end holding zeros
#pragma HLS PIPELINE II=1
      m0.write(a0); m1.write(a1); m2.write(a2);
    }
  }
drain:
  while (!ended) {
#pragma HLS PIPELINE II=1
    beat_t b = in.read(); ended = b.last; len_err = true;
  }
  const unsigned st = head != ST_OK ? head : len_err ? ST_LENGTH : content;
  if (command == 6 && run && st == ST_OK) { valid = 1; epoch = gen; }
  else if (st != ST_OK || command == 8) { valid = 0; epoch = 0; }
  status_out.write(st);
}

// ---- sampler (ENC) -------------------------------------------------------------------------------------------------
// e values travel as four signed bytes per 32-bit token (limb independent); each limb engine forms its residues.
static void sampler_enc(const sampler_ctl_t& c, hls::stream<v4_t>& u0, hls::stream<v4_t>& u1, hls::stream<v4_t>& u2,
                        hls::stream<ap_uint<32> >& e00, hls::stream<ap_uint<32> >& e01, hls::stream<ap_uint<32> >& e02,
                        hls::stream<ap_uint<32> >& e10, hls::stream<ap_uint<32> >& e11, hls::stream<ap_uint<32> >& e12,
                        hls::stream<ap_uint<1> >& kat) {
#pragma HLS INLINE
  u32 key[8];
#pragma HLS ARRAY_PARTITION variable=key complete
  for (int i = 0; i < 8; i++) key[i] = c.seed.range(32 * i + 31, 32 * i);
  u32 blk[CC_SLOTS][16];
#pragma HLS ARRAY_PARTITION variable=blk complete dim=0
  ap_uint<32> tok = 0;
feature:
  for (int f = 0; f < int(c.k); f++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=8
    // mask: keystream (1, f, 0) bytes in order; byte < 243 gives five trits (least significant digit first)
    ap_uint<16> acc = 0;
    int nacc = 0, tokens = 0;
  tern_batches:
    for (int batch = 0; batch < 8; batch++) {
#pragma HLS LOOP_TRIPCOUNT min=2 max=2
      if (tokens >= 1024) break;
      chacha8(key, CC_SLOTS * batch, 1, f, blk);
    tern_bytes:
      for (int j = 0; j < CC_SLOTS * 64; j++) {
#pragma HLS PIPELINE II=2
        const u32 word = blk[j / 64][(j % 64) / 4];
        const ap_uint<8> x = word.range(8 * (j % 4) + 7, 8 * (j % 4));
        if (tokens < 1024 && x < 243) { acc |= ap_uint<16>(V8_TRIT5[x]) << (2 * nacc); nacc += 5; }
        for (int e = 0; e < 2; e++) {
          if (nacc >= 4 && tokens < 1024) {
            const v4_t t = v4_t(acc.range(7, 0));
            u0.write(t); u1.write(t); u2.write(t);
            acc >>= 8; nacc -= 4; tokens++;
          }
        }
      }
    }
    // e0 / e1: keystreams (2, f, 0) and (3, f, 0), one little-endian 64-bit word per coefficient
  gauss_poly:
    for (int t = 0; t < 2; t++) {
    gauss_batches:
      for (int batch = 0; batch < 512 / CC_SLOTS; batch++) {
#pragma HLS LOOP_FLATTEN off
        chacha8(key, CC_SLOTS * batch, 2 + t, f, blk);
      gauss_samples:
        for (int i = 0; i < CC_SLOTS * 8; i++) {
#pragma HLS PIPELINE II=1
          const ap_uint<64> w = (ap_uint<64>(blk[i / 8][2 * (i % 8) + 1]) << 32) | ap_uint<64>(blk[i / 8][2 * (i % 8)]);
          const ap_int<8> e = cdt63(w);
          tok.range(8 * (i % 4) + 7, 8 * (i % 4)) = ap_uint<8>(e);
          if ((i % 4) == 3) {
            if (t == 0) { e00.write(tok); e01.write(tok); e02.write(tok); }
            else { e10.write(tok); e11.write(tok); e12.write(tok); }
          }
        }
      }
    }
  }
  // rev5 scrub: zero key through the shared ChaCha core (state, init and blk registers), checked against RFC 8439.
  for (int i = 0; i < 8; i++) key[i] = 0;
  chacha8(key, 0, 0, 0, blk);
  ap_uint<1> ok = 1;
  for (int i = 0; i < 16; i++) if (blk[0][i] != V8_CHACHA_ZERO_BLOCK[i]) ok = 0;
  tok = 0;
noise_scrub:
  for (int i = 0; i < 1024; i++) {   // e FIFOs depth 1024, mask FIFOs depth 32: end holding zeros
#pragma HLS PIPELINE II=1
    e00.write(tok); e01.write(tok); e02.write(tok); e10.write(tok); e11.write(tok); e12.write(tok);
    if (i < 32) { u0.write(0); u1.write(0); u2.write(0); }
  }
  kat.write(ok);
}

static void sampler(hls::stream<sampler_ctl_t>& ctl, hls::stream<v4_t>& u0, hls::stream<v4_t>& u1, hls::stream<v4_t>& u2,
                    hls::stream<ap_uint<32> >& e00, hls::stream<ap_uint<32> >& e01, hls::stream<ap_uint<32> >& e02,
                    hls::stream<ap_uint<32> >& e10, hls::stream<ap_uint<32> >& e11, hls::stream<ap_uint<32> >& e12,
                    hls::stream<ap_uint<1> >& kat) {
  const sampler_ctl_t c = ctl.read();
  if (c.run && c.command == 9) sampler_enc(c, u0, u1, u2, e00, e01, e02, e10, e11, e12, kat);
  const sampler_ctl_t z0 = ctl.read(), z1 = ctl.read();   // the two scrub tokens of every frame
  (void)z0; (void)z1;
}
// ---- limb engine -------------------------------------------------------------------------------------------------
template<int L> static void ct_stage(poly_t src, poly_t dst, int s) {
#pragma HLS INLINE off
  const u32 q = V8_Q[L], qp = V8_QP[L];
  const int len = N >> (s + 1), m = 1 << s;
ct_cycle:
  for (int c = 0; c < N / 8; c++) {
#pragma HLS PIPELINE II=1
#pragma HLS DEPENDENCE variable=dst inter false
    u32 x[8], w[4];
    int a0, a1;
    if (len >= 4) {
      const int j0 = ((4 * c) / len) * 2 * len + (4 * c) % len;
      a0 = j0 / 4; a1 = (j0 + len) / 4;
      const int ti = m + j0 / (2 * len);
      u32 g4[4];
      for (int b = 0; b < 4; b++) g4[b] = rom_psi<L>(4 * (ti >> 2) + b);
      const u32 S = g4[ti & 3];
      for (int b = 0; b < 4; b++) { x[b] = src[b][a0]; x[4 + b] = src[b][a1]; w[b] = S; }
    } else {
      a0 = 2 * c; a1 = 2 * c + 1;
      for (int b = 0; b < 4; b++) { x[b] = src[b][a0]; x[4 + b] = src[b][a1]; }
      const int ti = len == 2 ? m + 2 * c : m + 4 * c;
      u32 g4[4];
      for (int b = 0; b < 4; b++) g4[b] = rom_psi<L>(4 * (ti >> 2) + b);
      if (len == 2) { w[0] = w[1] = g4[ti & 3]; w[2] = w[3] = g4[(ti & 3) + 1]; }
      else { for (int b = 0; b < 4; b++) w[b] = g4[b]; }
    }
    int ui[4], vi[4];
    for (int b = 0; b < 4; b++) {
      if (len >= 4) { ui[b] = b; vi[b] = 4 + b; }
      else if (len == 2) { const int base = (b < 2) ? 0 : 4; ui[b] = base + (b & 1); vi[b] = base + (b & 1) + 2; }
      else { ui[b] = 2 * b; vi[b] = 2 * b + 1; }
    }
    u32 y[8];
    for (int b = 0; b < 4; b++) {
      const u32 U = x[ui[b]], V = mont(x[vi[b]], w[b], q, qp);
      y[ui[b]] = mod_add(U, V, q); y[vi[b]] = mod_sub(U, V, q);
    }
    for (int b = 0; b < 4; b++) { dst[b][a0] = y[b]; dst[b][a1] = y[4 + b]; }
  }
}
template<int L> static void gs_stage(poly_t src, poly_t dst, int s) {
#pragma HLS INLINE off
  const u32 q = V8_Q[L], qp = V8_QP[L];
  const int len = 1 << s, h = N >> (s + 1);
gs_cycle:
  for (int c = 0; c < N / 8; c++) {
#pragma HLS PIPELINE II=1
#pragma HLS DEPENDENCE variable=dst inter false
    u32 x[8], w[4];
    int a0, a1;
    if (len >= 4) {
      const int j0 = ((4 * c) / len) * 2 * len + (4 * c) % len;
      a0 = j0 / 4; a1 = (j0 + len) / 4;
      const int ti = h + j0 / (2 * len);
      u32 g4[4];
      for (int b = 0; b < 4; b++) g4[b] = rom_psi_inv<L>(4 * (ti >> 2) + b);
      const u32 S = g4[ti & 3];
      for (int b = 0; b < 4; b++) { x[b] = src[b][a0]; x[4 + b] = src[b][a1]; w[b] = S; }
    } else {
      a0 = 2 * c; a1 = 2 * c + 1;
      for (int b = 0; b < 4; b++) { x[b] = src[b][a0]; x[4 + b] = src[b][a1]; }
      const int ti = len == 2 ? h + 2 * c : h + 4 * c;
      u32 g4[4];
      for (int b = 0; b < 4; b++) g4[b] = rom_psi_inv<L>(4 * (ti >> 2) + b);
      if (len == 2) { w[0] = w[1] = g4[ti & 3]; w[2] = w[3] = g4[(ti & 3) + 1]; }
      else { for (int b = 0; b < 4; b++) w[b] = g4[b]; }
    }
    int ui[4], vi[4];
    for (int b = 0; b < 4; b++) {
      if (len >= 4) { ui[b] = b; vi[b] = 4 + b; }
      else if (len == 2) { const int base = (b < 2) ? 0 : 4; ui[b] = base + (b & 1); vi[b] = base + (b & 1) + 2; }
      else { ui[b] = 2 * b; vi[b] = 2 * b + 1; }
    }
    u32 y[8];
    for (int b = 0; b < 4; b++) {
      const u32 U = x[ui[b]], V = x[vi[b]];
      y[ui[b]] = mod_add(U, V, q); y[vi[b]] = mont(mod_sub(U, V, q), w[b], q, qp);
    }
    for (int b = 0; b < 4; b++) { dst[b][a0] = y[b]; dst[b][a1] = y[4 + b]; }
  }
}
template<int L> static void ntt(poly_t a, poly_t b) {
  for (int s = 0; s < LOGN; s += 2) { ct_stage<L>(a, b, s); ct_stage<L>(b, a, s + 1); }
}
template<int L> static void intt(poly_t a, poly_t b) {
  for (int s = 0; s < LOGN; s += 2) { gs_stage<L>(a, b, s); gs_stage<L>(b, a, s + 1); }
}

template<int L> static void limb(hls::stream<ctl_t>& ctl, hls::stream<v4_t>& in, hls::stream<v4_t>& us,
                                 hls::stream<v4_t>& m_in, hls::stream<ap_uint<32> >& e0_in, hls::stream<ap_uint<32> >& e1_in,
                                 hls::stream<v4_t>& out) {
  static u32 cb[4][BANK], ca[4][BANK];
#pragma HLS ARRAY_PARTITION variable=cb complete dim=1
#pragma HLS ARRAY_PARTITION variable=ca complete dim=1
  u32 A[4][BANK], B[4][BANK], C[4][BANK];
#pragma HLS ARRAY_PARTITION variable=A complete dim=1
#pragma HLS ARRAY_PARTITION variable=B complete dim=1
#pragma HLS ARRAY_PARTITION variable=C complete dim=1
#pragma HLS BIND_STORAGE variable=A type=ram_t2p impl=bram
#pragma HLS BIND_STORAGE variable=B type=ram_t2p impl=bram
#pragma HLS BIND_STORAGE variable=C type=ram_t2p impl=bram
#pragma HLS BIND_STORAGE variable=cb type=ram_t2p impl=uram
#pragma HLS BIND_STORAGE variable=ca type=ram_t2p impl=uram
  const u32 q = V8_Q[L], qp = V8_QP[L], ninv = V8_NINV_R2[L];
  const ctl_t c = ctl.read();
  if (!c.run) return;
  if (c.command == 6) {
  key_poly:
    for (int pi = 0; pi < 2; pi++) {
    key_load:
      for (int j = 0; j < BANK; j++) {
#pragma HLS PIPELINE II=1
        const v4_t v = in.read();
        for (int b = 0; b < 4; b++) A[b][j] = v.range(32 * b + 31, 32 * b);
      }
      ntt<L>(A, B);
    key_cache:
      for (int j = 0; j < BANK; j++) {
#pragma HLS PIPELINE II=1
        for (int b = 0; b < 4; b++) {
          const u32 x = mont(A[b][j], ninv, q, qp);
          if (pi == 0) cb[b][j] = x; else ca[b][j] = x;
        }
      }
    }
    return;
  }
  const bool enc = c.command == 9;
feature:
  for (int f = 0; f < int(c.k); f++) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=8
  u_load:
    for (int j = 0; j < BANK; j++) {
#pragma HLS PIPELINE II=1
      const ap_uint<8> t = enc ? ap_uint<8>(us.read().range(7, 0)) : ap_uint<8>(in.read().range(7, 0));
      for (int b = 0; b < 4; b++) {
        const ap_uint<2> code = t.range(2 * b + 1, 2 * b);
        A[b][j] = code == 1 ? u32(1) : code == 2 ? u32(q - 1) : u32(0);
      }
    }
    ntt<L>(A, B);                                       // A = U^
  product:
    for (int p = 0; p < 2; p++) {
    pointwise:
      for (int j = 0; j < BANK; j++) {
#pragma HLS PIPELINE II=1
        for (int b = 0; b < 4; b++) B[b][j] = mont(A[b][j], p == 0 ? cb[b][j] : ca[b][j], q, qp);
      }
      intt<L>(B, C);                                    // B = p * u
    store:
      for (int j = 0; j < BANK; j++) {
#pragma HLS PIPELINE II=1
        ap_uint<32> ev = 0;
        v4_t mv = 0;
        if (enc) { ev = p == 0 ? e0_in.read() : e1_in.read(); if (p == 0) mv = m_in.read(); }
        v4_t v;
        for (int b = 0; b < 4; b++) {
          u32 x = B[b][j];
          if (enc) { x = mod_add(x, residue_small(ap_int<8>(ev.range(8 * b + 7, 8 * b)), L), q); if (p == 0) x = mod_add(x, mv.range(32 * b + 31, 32 * b), q); }
          v.range(32 * b + 31, 32 * b) = x;
        }
        out.write(v);
      }
    }
  }
wipe:
  for (int j = 0; j < BANK; j++) {
#pragma HLS PIPELINE II=1
    for (int b = 0; b < 4; b++) { A[b][j] = 0; B[b][j] = 0; C[b][j] = 0; }
  }
  if (enc) {
  scrub_enc:   // consume the zero tokens that overwrite the message, noise and mask FIFOs
    for (int j = 0; j < 1024; j++) {
#pragma HLS PIPELINE II=1
      (void)m_in.read(); (void)e0_in.read(); (void)e1_in.read();
      if (j < 32) (void)us.read();
    }
  } else {
  scrub_pair:
    for (int j = 0; j < 32; j++) {
#pragma HLS PIPELINE II=1
      (void)in.read();
    }
  }
}

// ---- writer ------------------------------------------------------------------------------------------------------
static void writer(hls::stream<ctl_t>& ctl, hls::stream<v4_t>& o0, hls::stream<v4_t>& o1, hls::stream<v4_t>& o2,
                   hls::stream<ap_uint<8> >& status_in, hls::stream<ap_uint<1> >& kat, hls::stream<beat_t>& out) {
  const ctl_t c = ctl.read();
  beat_t h;
  h.data = 0; h.data.range(31, 0) = MAGIC; h.data.range(39, 32) = c.command; h.data.range(47, 40) = c.k;
  h.data.range(95, 64) = c.gen.range(31, 0); h.data.range(127, 96) = c.gen.range(63, 32);
  h.keep = -1; h.strb = -1; h.last = 0;
  out.write(h);
  if (c.run && (c.command == 7 || c.command == 9)) {
  data:
    for (int i = 0; i < int(c.k) * 2 * BANK * 3; i++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=6144 max=49152
      const int l = i % 3;
      const v4_t v = l == 0 ? o0.read() : l == 1 ? o1.read() : o2.read();
      beat_t b; b.data = v; b.keep = -1; b.strb = -1; b.last = 0;
      out.write(b);
    }
  }
  ap_uint<1> kat_ok = 1;
  if (c.run && c.command == 9) kat_ok = kat.read();
  ap_uint<8> st = status_in.read();
  if (st == ST_OK && kat_ok == 0) st = ST_SELFTEST;
  beat_t t;
  t.data = 0; t.data.range(31, 0) = MAGIC; t.data.range(63, 32) = st; t.data.range(95, 64) = c.k; t.data.range(127, 96) = c.gen.range(31, 0);
  t.keep = -1; t.strb = -1; t.last = 1;
  out.write(t);
}

void edgefhe_v8b(hls::stream<beat_t>& in, hls::stream<beat_t>& out) {
#pragma HLS INTERFACE axis port=in
#pragma HLS INTERFACE axis port=out
#pragma HLS INTERFACE s_axilite port=return bundle=control
#pragma HLS ARRAY_PARTITION variable=V8_PSI_M_0 cyclic factor=4
#pragma HLS ARRAY_PARTITION variable=V8_PSI_M_1 cyclic factor=4
#pragma HLS ARRAY_PARTITION variable=V8_PSI_M_2 cyclic factor=4
#pragma HLS ARRAY_PARTITION variable=V8_PSI_INV_M_0 cyclic factor=4
#pragma HLS ARRAY_PARTITION variable=V8_PSI_INV_M_1 cyclic factor=4
#pragma HLS ARRAY_PARTITION variable=V8_PSI_INV_M_2 cyclic factor=4
#pragma HLS DATAFLOW
  hls::stream<ctl_t> c0, c1, c2, cw;
  hls::stream<sampler_ctl_t> cs;
  hls::stream<ap_uint<1> > kat;
#pragma HLS STREAM variable=kat depth=2
  hls::stream<v4_t> s0, s1, s2, us0, us1, us2, m0, m1, m2, o0, o1, o2;
  hls::stream<ap_uint<32> > e00, e01, e02, e10, e11, e12;
  hls::stream<ap_uint<8> > status;
#pragma HLS STREAM variable=c0 depth=2
#pragma HLS STREAM variable=c1 depth=2
#pragma HLS STREAM variable=c2 depth=2
#pragma HLS STREAM variable=cw depth=3
#pragma HLS STREAM variable=cs depth=2
#pragma HLS STREAM variable=status depth=3
#pragma HLS STREAM variable=s0 depth=32
#pragma HLS STREAM variable=s1 depth=32
#pragma HLS STREAM variable=s2 depth=32
#pragma HLS STREAM variable=us0 depth=32
#pragma HLS STREAM variable=us1 depth=32
#pragma HLS STREAM variable=us2 depth=32
#pragma HLS STREAM variable=m0 depth=1024
#pragma HLS STREAM variable=m1 depth=1024
#pragma HLS STREAM variable=m2 depth=1024
#pragma HLS STREAM variable=e00 depth=1024
#pragma HLS STREAM variable=e01 depth=1024
#pragma HLS STREAM variable=e02 depth=1024
#pragma HLS STREAM variable=e10 depth=1024
#pragma HLS STREAM variable=e11 depth=1024
#pragma HLS STREAM variable=e12 depth=1024
#pragma HLS STREAM variable=o0 depth=1024
#pragma HLS STREAM variable=o1 depth=1024
#pragma HLS STREAM variable=o2 depth=1024
#pragma HLS BIND_STORAGE variable=m0 type=fifo impl=uram
#pragma HLS BIND_STORAGE variable=m1 type=fifo impl=uram
#pragma HLS BIND_STORAGE variable=m2 type=fifo impl=uram
#pragma HLS BIND_STORAGE variable=e00 type=fifo impl=uram
#pragma HLS BIND_STORAGE variable=e01 type=fifo impl=uram
#pragma HLS BIND_STORAGE variable=e02 type=fifo impl=uram
#pragma HLS BIND_STORAGE variable=e10 type=fifo impl=uram
#pragma HLS BIND_STORAGE variable=e11 type=fifo impl=uram
#pragma HLS BIND_STORAGE variable=e12 type=fifo impl=uram
#pragma HLS BIND_STORAGE variable=o0 type=fifo impl=uram
#pragma HLS BIND_STORAGE variable=o1 type=fifo impl=uram
#pragma HLS BIND_STORAGE variable=o2 type=fifo impl=uram
  reader(in, c0, c1, c2, cw, cs, s0, s1, s2, m0, m1, m2, status);
  sampler(cs, us0, us1, us2, e00, e01, e02, e10, e11, e12, kat);
  limb<0>(c0, s0, us0, m0, e00, e10, o0);
  limb<1>(c1, s1, us1, m1, e01, e11, o1);
  limb<2>(c2, s2, us2, m2, e02, e12, o2);
  writer(cw, o0, o1, o2, status, kat, out);
}
