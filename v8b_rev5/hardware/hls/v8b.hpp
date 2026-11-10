// EdgeFHE V8b HLS kernel interface (2026-10-16): V8a + ENC (command 9, full public-key encryption in the PL).
// Specification: ../ref/v8b_ref.hpp (KernelB).
#pragma once
#include "ap_axi_sdata.h"
#include "ap_int.h"
#include "hls_stream.h"

typedef ap_axiu<128, 0, 0, 0> beat_t;
typedef ap_uint<32> u32;
typedef ap_uint<128> v4_t;       // four 32-bit coefficients of one limb, or (low 8 bits) four 2-bit trit codes

struct ctl_t {
  ap_uint<8> command;   // 6 LOAD, 7 PAIR, 8 INVALIDATE, 9 ENC
  ap_uint<8> k;         // PAIR / ENC: ciphertexts in this frame
  ap_uint<64> gen;
  ap_uint<1> run;       // 1: the engines process data for this frame
};

// rev5 (review H2): only the sampler receives the seed. Followed in its FIFO by two all-zero scrub tokens per frame.
struct sampler_ctl_t {
  ap_uint<8> command;
  ap_uint<8> k;
  ap_uint<1> run;
  ap_uint<256> seed;    // ENC only: ChaCha20 key (sampler B); zero otherwise
};

// rev5: ChaCha20 block 0 for an all-zero key, counter 0 and nonce 0 (RFC 8439 test vector); the post-frame scrub
// batch must reproduce it (the C-sim testbench re-derives it from the frozen PS ChaCha20).
static const unsigned V8_CHACHA_ZERO_BLOCK[16] = {
  0xade0b876u, 0x903df1a0u, 0xe56a5d40u, 0x28bd8653u, 0xb819d2bdu, 0x1aed8da0u, 0xccef36a8u, 0xc70d778bu,
  0x7c5941dau, 0x8d485751u, 0x3fe02477u, 0x374ad8b8u, 0xf4b8436au, 0x1ca11815u, 0x69b687c3u, 0x8665eeb2u};

void edgefhe_v8b(hls::stream<beat_t>& in, hls::stream<beat_t>& out);
