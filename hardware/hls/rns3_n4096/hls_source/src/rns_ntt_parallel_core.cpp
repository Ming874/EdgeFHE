#include "rns_ntt_parallel_core.hpp"
#include "rns_ntt_parallel_constants.hpp"

static const int RNS_BANK_DEPTH = RNS_N / RNS_LANES;
static const int RNS_TWIDDLE_BANK_DEPTH = RNS_N / (2 * RNS_LANES);

typedef coeff_t poly_bank_t[RNS_LANES][RNS_BANK_DEPTH];
typedef coeff_t twiddle_bank_t[RNS_LANES][RNS_TWIDDLE_BANK_DEPTH];

static coeff_t mod_add(coeff_t left, coeff_t right, coeff_t modulus) {
#pragma HLS INLINE
    ap_uint<33> sum = (ap_uint<33>)left + (ap_uint<33>)right;
    return sum >= modulus ? (coeff_t)(sum - modulus) : (coeff_t)sum;
}

static coeff_t mod_sub(coeff_t left, coeff_t right, coeff_t modulus) {
#pragma HLS INLINE
    if (left >= right) {
        return left - right;
    }
    return (coeff_t)((ap_uint<33>)left + modulus - right);
}

static coeff_t read_vector_coeff(hls::stream<coeff_t> &stream) {
#pragma HLS INLINE
    return stream.read();
}

static coeff_t read_vector_coeff(hls::stream<axis_word_t> &stream) {
#pragma HLS INLINE
    return stream.read().data;
}

static void write_vector_coeff(
    hls::stream<coeff_t> &stream,
    coeff_t value,
    int) {
#pragma HLS INLINE
    stream.write(value);
}

static void write_vector_coeff(
    hls::stream<axis_word_t> &stream,
    coeff_t value,
    int index) {
#pragma HLS INLINE
    axis_word_t word;
    word.data = value;
    word.keep = -1;
    word.strb = -1;
    word.last = index == RNS_N - 1 ? 1 : 0;
    stream.write(word);
}

template <typename InputStream, typename OutputStream>
static void vector_add_two(
    InputStream &in_stream,
    OutputStream &out_stream,
    coeff_t modulus) {
    for (int index = 0; index < RNS_N; ++index) {
#pragma HLS PIPELINE II=2
#pragma HLS LOOP_TRIPCOUNT min=4096 max=4096
        coeff_t first = read_vector_coeff(in_stream);
        coeff_t second = read_vector_coeff(in_stream);
        write_vector_coeff(
            out_stream, mod_add(first, second, modulus), index);
    }
}

template <typename InputStream, typename OutputStream>
static void vector_add_three(
    InputStream &in_stream,
    OutputStream &out_stream,
    coeff_t modulus) {
    for (int index = 0; index < RNS_N; ++index) {
#pragma HLS PIPELINE II=3
#pragma HLS LOOP_TRIPCOUNT min=4096 max=4096
        coeff_t first = read_vector_coeff(in_stream);
        coeff_t second = read_vector_coeff(in_stream);
        coeff_t third = read_vector_coeff(in_stream);
        coeff_t partial = mod_add(first, second, modulus);
        write_vector_coeff(
            out_stream, mod_add(partial, third, modulus), index);
    }
}

template <typename InputStream, typename OutputStream>
static void vector_negate_add(
    InputStream &in_stream,
    OutputStream &out_stream,
    coeff_t modulus) {
    for (int index = 0; index < RNS_N; ++index) {
#pragma HLS PIPELINE II=2
#pragma HLS LOOP_TRIPCOUNT min=4096 max=4096
        coeff_t first = read_vector_coeff(in_stream);
        coeff_t second = read_vector_coeff(in_stream);
        coeff_t negated = first == 0 ? (coeff_t)0 : (coeff_t)(modulus - first);
        write_vector_coeff(
            out_stream, mod_add(negated, second, modulus), index);
    }
}

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
    coeff_t lane_power[RNS_LANES];
#pragma HLS ARRAY_PARTITION variable=lane_power complete

    lane_power[0] = RNS_MONTGOMERY_R[modulus_index];
    for (int lane = 1; lane < RNS_LANES; ++lane) {
#pragma HLS PIPELINE II=2
        lane_power[lane] = montgomery_mul(
            lane_power[lane - 1],
            base_montgomery,
            modulus,
            RNS_MONTGOMERY_NEG_INV[modulus_index]);
    }
    coeff_t jump = montgomery_mul(
        lane_power[RNS_LANES - 1],
        base_montgomery,
        modulus,
        RNS_MONTGOMERY_NEG_INV[modulus_index]);

    for (int address = 0; address < RNS_BANK_DEPTH; ++address) {
#pragma HLS PIPELINE II=2
#pragma HLS LOOP_TRIPCOUNT min=512 max=2048
        for (int lane = 0; lane < RNS_LANES; ++lane) {
#pragma HLS UNROLL
            cache[lane][address] = lane_power[lane];
            lane_power[lane] = montgomery_mul(
                lane_power[lane],
                jump,
                modulus,
                RNS_MONTGOMERY_NEG_INV[modulus_index]);
        }
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
    coeff_t root = inverse
        ? RNS_STAGE_ROOT_INV_MONT[modulus_index][stage]
        : RNS_STAGE_ROOT_FWD_MONT[modulus_index][stage];
    coeff_t lane_twiddle[RNS_LANES];
#pragma HLS ARRAY_PARTITION variable=lane_twiddle complete

    lane_twiddle[0] = RNS_MONTGOMERY_R[modulus_index];
    for (int lane = 1; lane < RNS_LANES; ++lane) {
#pragma HLS PIPELINE II=2
        lane_twiddle[lane] = montgomery_mul(
            lane_twiddle[lane - 1],
            root,
            modulus,
            RNS_MONTGOMERY_NEG_INV[modulus_index]);
    }
    coeff_t jump = montgomery_mul(
        lane_twiddle[RNS_LANES - 1],
        root,
        modulus,
        RNS_MONTGOMERY_NEG_INV[modulus_index]);
    int addresses = (half + RNS_LANES - 1) / RNS_LANES;

    for (int address = 0; address < addresses; ++address) {
#pragma HLS PIPELINE II=2
#pragma HLS LOOP_TRIPCOUNT min=1 max=1024
        for (int lane = 0; lane < RNS_LANES; ++lane) {
#pragma HLS UNROLL
            int offset = address * RNS_LANES + lane;
            if (offset < half) {
                cache[lane][address] = lane_twiddle[lane];
            }
            lane_twiddle[lane] = montgomery_mul(
                lane_twiddle[lane],
                jump,
                modulus,
                RNS_MONTGOMERY_NEG_INV[modulus_index]);
        }
    }
}

// Four fixed banks, two explicit reads and writes per bank per batch.
// The address/routing audit proves that batches partition every stage's output.
#if RNS_LANES != 4
#error "This isolated fixed-bank candidate requires RNS_LANES=4"
#endif
static void butterfly_stage(
    poly_bank_t src, poly_bank_t dst, twiddle_bank_t twiddles,
    int stage, coeff_t modulus, modulus_index_t modulus_index) {
#pragma HLS INLINE off
#pragma HLS ARRAY_PARTITION variable=src complete dim=1
#pragma HLS ARRAY_PARTITION variable=dst complete dim=1
#pragma HLS ARRAY_PARTITION variable=twiddles complete dim=1
butterfly_batch:
    for (int base = 0; base < RNS_N / 2; base += 4) {
#pragma HLS PIPELINE II=1
#pragma HLS DEPENDENCE variable=dst inter false
#pragma HLS LOOP_TRIPCOUNT min=512 max=512
        int addr0, addr1, twiddle_addr;
        if (stage < 2) {
            addr0 = base >> 1;
            addr1 = addr0 + 1;
            twiddle_addr = 0;
        } else {
            int group = base >> stage;
            int offset = base & ((1 << stage) - 1);
            addr0 = (group << (stage - 1)) + (offset >> 2);
            addr1 = addr0 + (1 << (stage - 2));
            twiddle_addr = offset >> 2;
        }
        coeff_t read0[4], read1[4], twiddle[4], sums[4], differences[4];
#pragma HLS ARRAY_PARTITION variable=read0 complete
#pragma HLS ARRAY_PARTITION variable=read1 complete
#pragma HLS ARRAY_PARTITION variable=twiddle complete
#pragma HLS ARRAY_PARTITION variable=sums complete
#pragma HLS ARRAY_PARTITION variable=differences complete
        for (int bank = 0; bank < 4; ++bank) {
#pragma HLS UNROLL
            read0[bank] = src[bank][addr0];
            read1[bank] = src[bank][addr1];
            // Early stages initialize only one/two twiddle banks: never read
            // uninitialized cache locations, even for unused register lanes.
            twiddle[bank] = 0;
            if (stage >= 2 || bank < (1 << stage))
                twiddle[bank] = twiddles[bank][twiddle_addr];
        }
        for (int lane = 0; lane < 4; ++lane) {
#pragma HLS UNROLL
            coeff_t left, right, factor;
            if (stage == 0) {
                int bank = (lane & 1) << 1;
                left = lane < 2 ? read0[bank] : read1[bank];
                right = lane < 2 ? read0[bank + 1] : read1[bank + 1];
                factor = twiddle[0];
            } else if (stage == 1) {
                int bank = lane & 1;
                left = lane < 2 ? read0[bank] : read1[bank];
                right = lane < 2 ? read0[bank + 2] : read1[bank + 2];
                factor = twiddle[bank];
            } else {
                left = read0[lane];
                right = read1[lane];
                factor = twiddle[lane];
            }
            coeff_t weighted = montgomery_mul(
                right, factor, modulus, RNS_MONTGOMERY_NEG_INV[modulus_index]);
            sums[lane] = mod_add(left, weighted, modulus);
            differences[lane] = mod_sub(left, weighted, modulus);
        }
        for (int bank = 0; bank < 4; ++bank) {
#pragma HLS UNROLL
            coeff_t value0, value1;
            if (stage == 0) {
                int lane = bank >> 1;
                value0 = (bank & 1) ? differences[lane] : sums[lane];
                value1 = (bank & 1) ? differences[lane + 2] : sums[lane + 2];
            } else if (stage == 1) {
                int lane = bank & 1;
                value0 = bank < 2 ? sums[lane] : differences[lane];
                value1 = bank < 2 ? sums[lane + 2] : differences[lane + 2];
            } else {
                value0 = sums[bank];
                value1 = differences[bank];
            }
            dst[bank][addr0] = value0;
            dst[bank][addr1] = value1;
        }
    }
}

static void normalize_inverse(
    poly_bank_t values,
    coeff_t modulus,
    modulus_index_t modulus_index) {
#pragma HLS ARRAY_PARTITION variable=values complete dim=1
    for (int address = 0; address < RNS_BANK_DEPTH; ++address) {
#pragma HLS PIPELINE II=1
        for (int bank = 0; bank < RNS_LANES; ++bank) {
#pragma HLS UNROLL
            values[bank][address] = montgomery_mul(
                values[bank][address],
                RNS_INV_N_MONT[modulus_index],
                modulus,
                RNS_MONTGOMERY_NEG_INV[modulus_index]);
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
        normalize_inverse(stage_a, modulus, modulus_index);
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
            int bank = index & (RNS_LANES - 1);
            int address = index / RNS_LANES;
            value = montgomery_mul(
                value,
                powers[bank][address],
                modulus,
                RNS_MONTGOMERY_NEG_INV[modulus_index]);
        }
        ap_uint<RNS_LOG_N> reversed =
            bit_reverse_index((ap_uint<RNS_LOG_N>)index);
        int reversed_value = reversed.to_uint();
        int bank = reversed_value & (RNS_LANES - 1);
        int address = reversed_value / RNS_LANES;
        values[bank][address] = value;
    }
}

static void load_stream(
    hls::stream<coeff_t> &in_stream,
    poly_bank_t values,
    poly_bank_t powers,
    ap_uint<1> apply_twist,
    coeff_t modulus,
    modulus_index_t modulus_index) {
#pragma HLS ARRAY_PARTITION variable=values complete dim=1
#pragma HLS ARRAY_PARTITION variable=powers complete dim=1
    for (int index = 0; index < RNS_N; ++index) {
#pragma HLS PIPELINE II=1
        coeff_t value = in_stream.read();
        if (value >= modulus) {
            value -= modulus;
        }
        if (apply_twist) {
            int bank = index & (RNS_LANES - 1);
            int address = index / RNS_LANES;
            value = montgomery_mul(
                value,
                powers[bank][address],
                modulus,
                RNS_MONTGOMERY_NEG_INV[modulus_index]);
        }
        ap_uint<RNS_LOG_N> reversed =
            bit_reverse_index((ap_uint<RNS_LOG_N>)index);
        int reversed_value = reversed.to_uint();
        int bank = reversed_value & (RNS_LANES - 1);
        int address = reversed_value / RNS_LANES;
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
        int address = index / RNS_LANES;
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
        word.last = index == RNS_N - 1 ? 1 : 0;
        out_stream.write(word);
    }
}

static void store_stream(
    hls::stream<coeff_t> &out_stream,
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
        int address = index / RNS_LANES;
        coeff_t value = values[bank][address];
        if (apply_untwist) {
            value = montgomery_mul(
                value,
                powers[bank][address],
                modulus,
                RNS_MONTGOMERY_NEG_INV[modulus_index]);
        }
        out_stream.write(value);
    }
}
static void copy_spectrum(poly_bank_t source, poly_bank_t destination) {
#pragma HLS ARRAY_PARTITION variable=source complete dim=1
#pragma HLS ARRAY_PARTITION variable=destination complete dim=1
    for (int address = 0; address < RNS_BANK_DEPTH; ++address) {
#pragma HLS PIPELINE II=1
        for (int bank = 0; bank < RNS_LANES; ++bank) {
#pragma HLS UNROLL
            destination[bank][address] = source[bank][address];
        }
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
    // Bit reversal maps adjacent source lanes to the same destination bank, so
    // this loop stays one coefficient per cycle to avoid a write-port conflict.
    for (int index = 0; index < RNS_N; ++index) {
#pragma HLS PIPELINE II=1
        int source_bank = index & (RNS_LANES - 1);
        int source_address = index / RNS_LANES;
        coeff_t product = multiply_ordinary(
            left[source_bank][source_address],
            right[source_bank][source_address],
            modulus,
            modulus_index);
        ap_uint<RNS_LOG_N> reversed =
            bit_reverse_index((ap_uint<RNS_LOG_N>)index);
        int reversed_value = reversed.to_uint();
        int destination_bank = reversed_value & (RNS_LANES - 1);
        int destination_address = reversed_value / RNS_LANES;
        inverse_input[destination_bank][destination_address] = product;
    }
}

template <typename InputStream, typename OutputStream>
static void rns_ntt_parallel_process_impl_n4096(
    InputStream &in_stream,
    OutputStream &out_stream,
    operation_t operation,
    modulus_index_t modulus_index) {

    if (modulus_index >= RNS_MODULUS_COUNT) {
        modulus_index = 0;
    }
    coeff_t modulus = RNS_MODULI[modulus_index];

    if (operation == RNS_OP_ADD_TWO) {
        vector_add_two(in_stream, out_stream, modulus);
        return;
    }
    if (operation == RNS_OP_ADD_THREE) {
        vector_add_three(in_stream, out_stream, modulus);
        return;
    }
    if (operation == RNS_OP_NEGATE_ADD) {
        vector_negate_add(in_stream, out_stream, modulus);
        return;
    }

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

void rns_ntt_parallel_process_n4096(
    hls::stream<axis_word_t> &in_stream,
    hls::stream<axis_word_t> &out_stream,
    operation_t operation,
    modulus_index_t modulus_index) {
    rns_ntt_parallel_process_impl_n4096(
        in_stream, out_stream, operation, modulus_index);
}

void rns_ntt_parallel_process_coeff_n4096(
    hls::stream<coeff_t> &in_stream,
    hls::stream<coeff_t> &out_stream,
    operation_t operation,
    modulus_index_t modulus_index) {
    rns_ntt_parallel_process_impl_n4096(
        in_stream, out_stream, operation, modulus_index);
}
void rns_ntt_parallel_accel_n4096(
    hls::stream<axis_word_t> &in_stream,
    hls::stream<axis_word_t> &out_stream,
    operation_t operation,
    modulus_index_t modulus_index) {
#pragma HLS INTERFACE axis port=in_stream
#pragma HLS INTERFACE axis port=out_stream
#pragma HLS INTERFACE s_axilite port=operation bundle=control
#pragma HLS INTERFACE s_axilite port=modulus_index bundle=control
#pragma HLS INTERFACE s_axilite port=return bundle=control

    rns_ntt_parallel_process_n4096(
        in_stream, out_stream, operation, modulus_index);
}

// Simulation-only access to the actual arithmetic and stage implementation.
// These functions are excluded from synthesis and do not change the AXI ABI.
#ifndef __SYNTHESIS__
uint32_t candidate_montgomery(uint32_t a, uint32_t b, int limb) {
    return montgomery_mul(a, b, RNS_MODULI[limb], RNS_MONTGOMERY_NEG_INV[limb]).to_uint();
}
uint32_t candidate_ordinary(uint32_t a, uint32_t b, int limb) {
    return multiply_ordinary(a, b, RNS_MODULI[limb], limb).to_uint();
}
uint32_t candidate_add(uint32_t a, uint32_t b, int limb) {
    return mod_add(a, b, RNS_MODULI[limb]).to_uint();
}
uint32_t candidate_sub(uint32_t a, uint32_t b, int limb) {
    return mod_sub(a, b, RNS_MODULI[limb]).to_uint();
}
void candidate_stage(const uint32_t *input, uint32_t *output,
                     int stage, int inverse, int limb) {
    poly_bank_t source, destination;
    twiddle_bank_t cache;
    for (int i = 0; i < RNS_N; ++i) source[i & 3][i >> 2] = input[i];
    generate_stage_twiddles(stage, inverse, cache, RNS_MODULI[limb], limb);
    butterfly_stage(source, destination, cache, stage, RNS_MODULI[limb], limb);
    for (int i = 0; i < RNS_N; ++i) output[i] = destination[i & 3][i >> 2].to_uint();
}
#endif
