#pragma once

#include "rns_ntt_parallel_core.hpp"

typedef ap_axiu<128, 0, 0, 0> rns3_axis_word_t;

void rns3_ntt_parallel_accel_n4096(
    hls::stream<rns3_axis_word_t> &in_stream,
    hls::stream<rns3_axis_word_t> &out_stream,
    operation_t operation);
