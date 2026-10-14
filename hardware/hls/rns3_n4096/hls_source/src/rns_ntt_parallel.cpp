#include "rns_ntt_onthefly.hpp"
#include "rns_ntt_onthefly_constants.hpp"

static const int RNS_LANES = 2;
static const int RNS_BANK_DEPTH = RNS_N / RNS_LANES;
static const int RNS_TWIDDLE_BANK_DEPTH = RNS_N / (2 * RNS_LANES);

typedef coeff_t poly_bank_t[RNS_LANES][RNS_BANK_DEPTH];
typedef coeff_t twiddle_bank_t[RNS_LANES][RNS_TWIDDLE_BANK_DEPTH];

static coeff_t mod_add(coeff_t left, coeff_t right, coeff_t modulus) {
#pragma HLS INLINE
    ap_uint<33> sum = (ap_uint<33>)left + (ap_uint<33>)right;
    return (sum >= modulus) ? (coeff_t)(sum - modulus) : (coeff_t)sum;
}

static coeff_t mod_sub(coeff_t left, coeff_t right, coeff_t modulus) {
#pragma HLS INLINE
    if (left >= right) {
        return left - right;
    }
    return (coeff_t)((ap_uint<33>)left + modulus - right);
}

// Montgomery inputs are tagged by convention:
//   ordinary * montgomery -> ordinary
//   montgomery * montgomery -> montgomery
static coeff_t montgomery_mul(
    coeff_t left,
    coeff_t right,
    coeff_t modulus,
    coeff_t modulus_neg_inv) {
#pragma HLS INLINE
    ap_uint<64> product = (ap_uint<64>)left * (ap_uint<64>)right;
    ap_uint<64> correction_product =
        (ap_uint<64>)(coeff_t)product * (ap_uint<64>)modulus_neg_inv;
    coeff_t correction = correction_product.range(31, 0);
    ap_uint<64> corrected =
        product + (ap_uint<64>)correction * (ap_uint<64>)modulus;
    ap_uint<33> reduced = corrected >> 32;
    if (reduced >= modulus) {
        reduced -= modulus;
    }
    return (coeff_t)reduced;
}

static coeff_t multiply_ordinary(
    coeff_t left,
    coeff_t right,
    coeff_t modulus,
    modulus_index_t modulus_index) {
#pragma HLS INLINE
    coeff_t product_montgomery = montgomery_mul(
        left,
        right,
        modulus,
        RNS_MONTGOMERY_NEG_INV[modulus_index]);
    return montgomery_mul(
        product_montgomery,
        RNS_MONTGOMERY_R2[modulus_index],
        modulus,
        RNS_MONTGOMERY_NEG_INV[modulus_index]);
}

static ap_uint<RNS_LOG_N> bit_reverse_index(ap_uint<RNS_LOG_N> index) {
#pragma HLS INLINE
    ap_uint<RNS_LOG_N> reversed;
    for (int bit = 0; bit < RNS_LOG_N; ++bit) {
#pragma HLS UNROLL
        reversed[bit] = index[RNS_LOG_N - 1 - bit];
    }
    return reversed;
}

static void generate_power_cache(
    coeff_t base_montgomery,
    poly_bank_t cache,
    coeff_t modulus,
    modulus_index_t modulus_index) {
#pragma HLS ARRAY_PARTITION variable=cache complete dim=1
    coeff_t power_montgomery = RNS_MONTGOMERY_R[modulus_index];
    for (int index = 0; index < RNS_N; ++index) {
#pragma HLS LOOP_TRIPCOUNT min=4096 max=4096
        int bank = index & (RNS_LANES - 1);
        int address = index >> 1;
        cache[bank][address] = power_montgomery;
        power_montgomery = montgomery_mul(
            power_montgomery,
            base_montgomery,
            modulus,
            RNS_MONTGOMERY_NEG_INV[modulus_index]);
    }
}

static void generate_stage_twiddles(
    int stage,
    ap_uint<1> inverse,
    twiddle_bank_t cache,
    coeff_t modulus,
    modulus_index_t modulus_index) {
#pragma HLS ARRAY_PARTITION variable=cache complete dim=1
    int half = 1 << stage;
    coeff_t stage_root_montgomery = inverse
        ? RNS_STAGE_ROOT_INV_MONT[modulus_index][stage]
        : RNS_STAGE_ROOT_FWD_MONT[modulus_index][stage];
    coeff_t twiddle_montgomery = RNS_MONTGOMERY_R[modulus_index];
    for (int offset = 0; offset < half; ++offset) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=2048
        int bank = offset & (RNS_LANES - 1);
        int address = offset >> 1;
        cache[bank][address] = twiddle_montgomery;
        twiddle_montgomery = montgomery_mul(
            twiddle_montgomery,
            stage_root_montgomery,
            modulus,
            RNS_MONTGOMERY_NEG_INV[modulus_index]);
    }
}

template <int LEFT_BANK, int RIGHT_BANK>
static void butterfly_banked(
    poly_bank_t src,
    poly_bank_t dst,
    twiddle_bank_t twiddles,
    int butterfly,
    int stage,
    coeff_t modulus,
    modulus_index_t modulus_index) {
#pragma HLS INLINE
    int half = 1 << stage;
    int length = half << 1;
    int offset = butterfly & (half - 1);
    int group = butterfly >> stage;
    int left_index = group * length + offset;
    int right_index = left_index + half;
    int left_address = left_index >> 1;
    int right_address = right_index >> 1;
    int twiddle_bank = offset & (RNS_LANES - 1);
    int twiddle_address = offset >> 1;

    coeff_t left = src[LEFT_BANK][left_address];
    coeff_t right = montgomery_mul(
        src[RIGHT_BANK][right_address],
        twiddles[twiddle_bank][twiddle_address],
        modulus,
        RNS_MONTGOMERY_NEG_INV[modulus_index]);
    dst[LEFT_BANK][left_address] = mod_add(left, right, modulus);
    dst[RIGHT_BANK][right_address] = mod_sub(left, right, modulus);
}

static void butterfly_stage(
    poly_bank_t src,
    poly_bank_t dst,
    twiddle_bank_t twiddles,
    int stage,
    coeff_t modulus,
    modulus_index_t modulus_index) {
#pragma HLS ARRAY_PARTITION variable=src complete dim=1
#pragma HLS ARRAY_PARTITION variable=dst complete dim=1
#pragma HLS ARRAY_PARTITION variable=twiddles complete dim=1
    for (int butterfly_base = 0; butterfly_base < RNS_N / 2;
         butterfly_base += RNS_LANES) {
#pragma HLS PIPELINE II=1
#pragma HLS DEPENDENCE variable=src inter false
#pragma HLS DEPENDENCE variable=dst inter false
#pragma HLS LOOP_TRIPCOUNT min=1024 max=1024
        if (stage == 0) {
            butterfly_banked<0, 1>(
                src,
                dst,
                twiddles,
                butterfly_base,
                stage,
                modulus,
                modulus_index);
            butterfly_banked<0, 1>(
                src,
                dst,
                twiddles,
                butterfly_base + 1,
                stage,
                modulus,
                modulus_index);
        } else {
            butterfly_banked<0, 0>(
                src,
                dst,
                twiddles,
                butterfly_base,
                stage,
                modulus,
                modulus_index);
            butterfly_banked<1, 1>(
                src,
                dst,
                twiddles,
                butterfly_base + 1,
                stage,
                modulus,
                modulus_index);
        }
    }
}

static void cyclic_ntt(
    poly_bank_t stage_a,
    poly_bank_t stage_b,
    twiddle_bank_t twiddles,
    ap_uint<1> inverse,
    coeff_t modulus,
    modulus_index_t modulus_index) {
#pragma HLS ARRAY_PARTITION variable=stage_a complete dim=1
#pragma HLS ARRAY_PARTITION variable=stage_b complete dim=1
#pragma HLS ARRAY_PARTITION variable=twiddles complete dim=1
    for (int stage = 0; stage < RNS_LOG_N; ++stage) {
#pragma HLS LOOP_TRIPCOUNT min=12 max=12
        generate_stage_twiddles(
            stage, inverse, twiddles, modulus, modulus_index);
        if ((stage & 1) == 0) {
            butterfly_stage(
                stage_a, stage_b, twiddles, stage, modulus, modulus_index);
        } else {
            butterfly_stage(
                stage_b, stage_a, twiddles, stage, modulus, modulus_index);
        }
    }

    if (inverse) {
        for (int index = 0; index < RNS_N; ++index) {
#pragma HLS PIPELINE II=1
#pragma HLS DEPENDENCE variable=stage_a inter false
            int bank = index & (RNS_LANES - 1);
            int address = index >> 1;
            stage_a[bank][address] = montgomery_mul(
                stage_a[bank][address],
                RNS_INV_N_MONT[modulus_index],
                modulus,
                RNS_MONTGOMERY_NEG_INV[modulus_index]);
        }
    }
}

static void load_stream(
    hls::stream<axis_word_t> &in_stream,
    poly_bank_t values,
    poly_bank_t powers,
    ap_uint<1> apply_twist,
    coeff_t modulus,
    modulus_index_t modulus_index) {
#pragma HLS ARRAY_PARTITION variable=values complete dim=1
#pragma HLS ARRAY_PARTITION variable=powers complete dim=1
    for (int index = 0; index < RNS_N; ++index) {
#pragma HLS PIPELINE II=1
        axis_word_t word = in_stream.read();
        coeff_t value = word.data;
        if (value >= modulus) {
            value -= modulus;
        }
        if (apply_twist) {
            int power_bank = index & (RNS_LANES - 1);
            int power_address = index >> 1;
            value = montgomery_mul(
                value,
                powers[power_bank][power_address],
                modulus,
                RNS_MONTGOMERY_NEG_INV[modulus_index]);
        }
        ap_uint<RNS_LOG_N> reversed =
            bit_reverse_index((ap_uint<RNS_LOG_N>)index);
        int bank = reversed[0] ? 1 : 0;
        int address = reversed.to_uint() >> 1;
        values[bank][address] = value;
    }
}

static void store_stream(
    hls::stream<axis_word_t> &out_stream,
    poly_bank_t values,
    poly_bank_t powers,
    ap_uint<1> apply_untwist,
    coeff_t modulus,
    modulus_index_t modulus_index) {
#pragma HLS ARRAY_PARTITION variable=values complete dim=1
#pragma HLS ARRAY_PARTITION variable=powers complete dim=1
    for (int index = 0; index < RNS_N; ++index) {
#pragma HLS PIPELINE II=1
        int bank = index & (RNS_LANES - 1);
        int address = index >> 1;
        coeff_t value = values[bank][address];
        if (apply_untwist) {
            value = montgomery_mul(
                value,
                powers[bank][address],
                modulus,
                RNS_MONTGOMERY_NEG_INV[modulus_index]);
        }
        axis_word_t word;
        word.data = value;
        word.keep = -1;
        word.strb = -1;
        word.last = (index == RNS_N - 1) ? 1 : 0;
        out_stream.write(word);
    }
}

static void copy_spectrum(poly_bank_t source, poly_bank_t destination) {
#pragma HLS ARRAY_PARTITION variable=source complete dim=1
#pragma HLS ARRAY_PARTITION variable=destination complete dim=1
    for (int index = 0; index < RNS_N; ++index) {
#pragma HLS PIPELINE II=1
        int bank = index & (RNS_LANES - 1);
        int address = index >> 1;
        destination[bank][address] = source[bank][address];
    }
}

static void pointwise_multiply_and_bit_reverse(
    poly_bank_t left,
    poly_bank_t right,
    poly_bank_t inverse_input,
    coeff_t modulus,
    modulus_index_t modulus_index) {
#pragma HLS ARRAY_PARTITION variable=left complete dim=1
#pragma HLS ARRAY_PARTITION variable=right complete dim=1
#pragma HLS ARRAY_PARTITION variable=inverse_input complete dim=1
    for (int index = 0; index < RNS_N; ++index) {
#pragma HLS PIPELINE II=1
        int source_bank = index & (RNS_LANES - 1);
        int source_address = index >> 1;
        coeff_t product = multiply_ordinary(
            left[source_bank][source_address],
            right[source_bank][source_address],
            modulus,
            modulus_index);
        ap_uint<RNS_LOG_N> reversed =
            bit_reverse_index((ap_uint<RNS_LOG_N>)index);
        int destination_bank = reversed[0] ? 1 : 0;
        int destination_address = reversed.to_uint() >> 1;
        inverse_input[destination_bank][destination_address] = product;
    }
}

void rns_ntt_onthefly_accel_n4096(
    hls::stream<axis_word_t> &in_stream,
    hls::stream<axis_word_t> &out_stream,
    operation_t operation,
    modulus_index_t modulus_index) {
#pragma HLS INTERFACE axis port=in_stream
#pragma HLS INTERFACE axis port=out_stream
#pragma HLS INTERFACE s_axilite port=operation bundle=control
#pragma HLS INTERFACE s_axilite port=modulus_index bundle=control
#pragma HLS INTERFACE s_axilite port=return bundle=control

    if (modulus_index >= RNS_MODULUS_COUNT) {
        modulus_index = 0;
    }
    coeff_t modulus = RNS_MODULI[modulus_index];

    poly_bank_t stage_a;
    poly_bank_t stage_b;
    poly_bank_t left_spectrum;
    poly_bank_t power_cache;
    twiddle_bank_t twiddle_cache;
#pragma HLS ARRAY_PARTITION variable=stage_a complete dim=1
#pragma HLS ARRAY_PARTITION variable=stage_b complete dim=1
#pragma HLS ARRAY_PARTITION variable=left_spectrum complete dim=1
#pragma HLS ARRAY_PARTITION variable=power_cache complete dim=1
#pragma HLS ARRAY_PARTITION variable=twiddle_cache complete dim=1
#pragma HLS BIND_STORAGE variable=stage_a type=ram_t2p impl=bram
#pragma HLS BIND_STORAGE variable=stage_b type=ram_t2p impl=bram
#pragma HLS BIND_STORAGE variable=left_spectrum type=ram_t2p impl=bram
#pragma HLS BIND_STORAGE variable=power_cache type=ram_t2p impl=bram
#pragma HLS BIND_STORAGE variable=twiddle_cache type=ram_t2p impl=bram

    if (operation == RNS_OP_MULTIPLY) {
        generate_power_cache(
            RNS_PSI_MONT[modulus_index],
            power_cache,
            modulus,
            modulus_index);
        load_stream(
            in_stream, stage_a, power_cache, 1, modulus, modulus_index);
        cyclic_ntt(
            stage_a, stage_b, twiddle_cache, 0, modulus, modulus_index);
        copy_spectrum(stage_a, left_spectrum);

        load_stream(
            in_stream, stage_a, power_cache, 1, modulus, modulus_index);
        cyclic_ntt(
            stage_a, stage_b, twiddle_cache, 0, modulus, modulus_index);
        pointwise_multiply_and_bit_reverse(
            left_spectrum, stage_a, stage_b, modulus, modulus_index);
        cyclic_ntt(
            stage_b, stage_a, twiddle_cache, 1, modulus, modulus_index);

        generate_power_cache(
            RNS_PSI_INV_MONT[modulus_index],
            power_cache,
            modulus,
            modulus_index);
        store_stream(
            out_stream, stage_b, power_cache, 1, modulus, modulus_index);
        return;
    }

    ap_uint<1> inverse = operation == RNS_OP_INVERSE ? 1 : 0;
    if (!inverse) {
        generate_power_cache(
            RNS_PSI_MONT[modulus_index],
            power_cache,
            modulus,
            modulus_index);
    }
    load_stream(
        in_stream, stage_a, power_cache, !inverse, modulus, modulus_index);
    cyclic_ntt(
        stage_a, stage_b, twiddle_cache, inverse, modulus, modulus_index);
    if (inverse) {
        generate_power_cache(
            RNS_PSI_INV_MONT[modulus_index],
            power_cache,
            modulus,
            modulus_index);
    }
    store_stream(
        out_stream, stage_a, power_cache, inverse, modulus, modulus_index);
}
