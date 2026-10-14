#pragma once

#include <stdint.h>

#include "ap_axi_sdata.h"
#include "ap_int.h"
#include "hls_stream.h"

static const int RNS_N = 4096;
static const int RNS_LOG_N = 12;
static const int RNS_TWIDDLE_COUNT = RNS_N - 1;
static const int RNS_MODULUS_COUNT = 3;

typedef ap_axiu<32, 0, 0, 0> axis_word_t;
typedef ap_uint<32> coeff_t;
typedef ap_uint<2> modulus_index_t;
typedef ap_uint<2> operation_t;

static const operation_t RNS_OP_FORWARD = 0;
static const operation_t RNS_OP_INVERSE = 1;
static const operation_t RNS_OP_MULTIPLY = 2;

void rns_ntt_onthefly_accel_n4096(
    hls::stream<axis_word_t> &in_stream,
    hls::stream<axis_word_t> &out_stream,
    operation_t operation,
    modulus_index_t modulus_index);
