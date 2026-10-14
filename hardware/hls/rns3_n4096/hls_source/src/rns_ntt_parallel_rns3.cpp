#include "rns_ntt_parallel_rns3.hpp"

static void unpack_rns3(
    hls::stream<rns3_axis_word_t> &wide_stream,
    hls::stream<coeff_t> &limb0,
    hls::stream<coeff_t> &limb1,
    hls::stream<coeff_t> &limb2,
    operation_t operation) {
    int input_words = RNS_N;
    if (operation == RNS_OP_MULTIPLY ||
        operation == RNS_OP_ADD_TWO ||
        operation == RNS_OP_NEGATE_ADD) {
        input_words = 2 * RNS_N;
    } else if (operation == RNS_OP_ADD_THREE) {
        input_words = 3 * RNS_N;
    }
    for (int index = 0; index < input_words; ++index) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=4096 max=12288
        rns3_axis_word_t wide = wide_stream.read();
        coeff_t words[3];
#pragma HLS ARRAY_PARTITION variable=words complete
        for (int limb = 0; limb < 3; ++limb) {
#pragma HLS UNROLL
            words[limb] = wide.data.range(32 * limb + 31, 32 * limb);
        }
        limb0.write(words[0]);
        limb1.write(words[1]);
        limb2.write(words[2]);
    }
}

template <int LIMB>
static void process_limb(
    hls::stream<coeff_t> &input,
    hls::stream<coeff_t> &output,
    operation_t operation) {
#pragma HLS INLINE off
    rns_ntt_parallel_process_coeff_n4096(
        input, output, operation, (modulus_index_t)LIMB);
}

static void pack_rns3(
    hls::stream<coeff_t> &limb0,
    hls::stream<coeff_t> &limb1,
    hls::stream<coeff_t> &limb2,
    hls::stream<rns3_axis_word_t> &wide_stream) {
    for (int index = 0; index < RNS_N; ++index) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=4096 max=4096
        coeff_t words[3];
#pragma HLS ARRAY_PARTITION variable=words complete
        words[0] = limb0.read();
        words[1] = limb1.read();
        words[2] = limb2.read();

        rns3_axis_word_t wide;
        wide.data = 0;
        for (int limb = 0; limb < 3; ++limb) {
#pragma HLS UNROLL
            wide.data.range(32 * limb + 31, 32 * limb) = words[limb];
        }
        wide.keep = -1;
        wide.strb = -1;
        wide.last = index == RNS_N - 1 ? 1 : 0;
        wide_stream.write(wide);
    }
}

void rns3_ntt_parallel_accel_n4096(
    hls::stream<rns3_axis_word_t> &in_stream,
    hls::stream<rns3_axis_word_t> &out_stream,
    operation_t operation) {
#pragma HLS INTERFACE axis port=in_stream
#pragma HLS INTERFACE axis port=out_stream
#pragma HLS INTERFACE s_axilite port=operation bundle=control
#pragma HLS INTERFACE s_axilite port=return bundle=control
#pragma HLS DATAFLOW

    hls::stream<coeff_t> input0("input0");
    hls::stream<coeff_t> input1("input1");
    hls::stream<coeff_t> input2("input2");
    hls::stream<coeff_t> output0("output0");
    hls::stream<coeff_t> output1("output1");
    hls::stream<coeff_t> output2("output2");
#pragma HLS STREAM variable=input0 depth=64
#pragma HLS STREAM variable=input1 depth=64
#pragma HLS STREAM variable=input2 depth=64
#pragma HLS STREAM variable=output0 depth=64
#pragma HLS STREAM variable=output1 depth=64
#pragma HLS STREAM variable=output2 depth=64

    unpack_rns3(in_stream, input0, input1, input2, operation);
    process_limb<0>(input0, output0, operation);
    process_limb<1>(input1, output1, operation);
    process_limb<2>(input2, output2, operation);
    pack_rns3(output0, output1, output2, out_stream);
}
