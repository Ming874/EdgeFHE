#include <stdint.h>

#include <iostream>

#include "rns_ntt_onthefly.hpp"
#include "rns_ntt_onthefly_constants.hpp"

static uint32_t ref_mod_pow(uint32_t base, uint32_t exponent, uint32_t modulus) {
    uint64_t result = 1;
    uint64_t power = base;
    while (exponent != 0) {
        if ((exponent & 1U) != 0U) {
            result = (result * power) % modulus;
        }
        power = (power * power) % modulus;
        exponent >>= 1;
    }
    return (uint32_t)result;
}

static int ref_bit_reverse(int value) {
    int reversed = 0;
    for (int bit = 0; bit < RNS_LOG_N; ++bit) {
        reversed = (reversed << 1) | ((value >> bit) & 1);
    }
    return reversed;
}

static void ref_cyclic_ntt(
    uint32_t input[RNS_N],
    uint32_t output[RNS_N],
    uint32_t root,
    uint32_t modulus) {
    for (int index = 0; index < RNS_N; ++index) {
        output[ref_bit_reverse(index)] = input[index] % modulus;
    }
    for (int length = 2; length <= RNS_N; length <<= 1) {
        int half = length >> 1;
        uint32_t stage_root =
            ref_mod_pow(root, RNS_N / length, modulus);
        for (int start = 0; start < RNS_N; start += length) {
            uint32_t twiddle = 1;
            for (int offset = 0; offset < half; ++offset) {
                int left_index = start + offset;
                int right_index = left_index + half;
                uint32_t left = output[left_index];
                uint32_t right =
                    (uint64_t)output[right_index] * twiddle % modulus;
                output[left_index] = (left + right) % modulus;
                output[right_index] =
                    (left + modulus - right) % modulus;
                twiddle = (uint64_t)twiddle * stage_root % modulus;
            }
        }
    }
}

static void ref_forward(
    uint32_t input[RNS_N],
    uint32_t output[RNS_N],
    int limb) {
    uint32_t modulus = RNS_MODULI[limb];
    uint32_t twisted[RNS_N];
    uint32_t power = 1;
    for (int index = 0; index < RNS_N; ++index) {
        twisted[index] = (uint64_t)input[index] * power % modulus;
        power = (uint64_t)power * RNS_PSI[limb] % modulus;
    }
    ref_cyclic_ntt(twisted, output, RNS_OMEGA[limb], modulus);
}

static void ref_inverse(
    uint32_t input[RNS_N],
    uint32_t output[RNS_N],
    int limb) {
    uint32_t modulus = RNS_MODULI[limb];
    ref_cyclic_ntt(input, output, RNS_OMEGA_INV[limb], modulus);
    uint32_t psi_inv =
        ref_mod_pow(RNS_PSI[limb], modulus - 2, modulus);
    uint32_t power = 1;
    for (int index = 0; index < RNS_N; ++index) {
        output[index] =
            (uint64_t)output[index] * RNS_INV_N[limb] % modulus;
        output[index] = (uint64_t)output[index] * power % modulus;
        power = (uint64_t)power * psi_inv % modulus;
    }
}

static void ref_negacyclic_mul(
    uint32_t left[RNS_N],
    uint32_t right[RNS_N],
    uint32_t output[RNS_N],
    uint32_t modulus) {
    for (int index = 0; index < RNS_N; ++index) {
        output[index] = 0;
    }
    for (int left_index = 0; left_index < RNS_N; ++left_index) {
        for (int right_index = 0; right_index < RNS_N; ++right_index) {
            int output_index = left_index + right_index;
            bool negate = output_index >= RNS_N;
            if (negate) {
                output_index -= RNS_N;
            }
            uint32_t product =
                (uint64_t)left[left_index] * right[right_index] % modulus;
            output[output_index] = negate
                ? (output[output_index] + modulus - product) % modulus
                : (output[output_index] + product) % modulus;
        }
    }
}

static void push_poly(
    hls::stream<axis_word_t> &stream,
    uint32_t values[RNS_N]) {
    for (int index = 0; index < RNS_N; ++index) {
        axis_word_t word;
        word.data = values[index];
        word.keep = -1;
        word.strb = -1;
        word.last = (index == RNS_N - 1) ? 1 : 0;
        stream.write(word);
    }
}

static void pop_poly(
    hls::stream<axis_word_t> &stream,
    uint32_t values[RNS_N]) {
    for (int index = 0; index < RNS_N; ++index) {
        axis_word_t word = stream.read();
        values[index] = (uint32_t)word.data;
        if (index == RNS_N - 1 && word.last != 1) {
            std::cerr << "missing TLAST on final output word\n";
        }
    }
}

static int compare_poly(
    const char *label,
    uint32_t actual[RNS_N],
    uint32_t expected[RNS_N],
    int limb) {
    int errors = 0;
    for (int index = 0; index < RNS_N; ++index) {
        if (actual[index] != expected[index]) {
            if (errors < 8) {
                std::cerr << label << " limb " << limb << " mismatch at "
                          << index << ": got " << actual[index]
                          << ", expected " << expected[index] << "\n";
            }
            ++errors;
        }
    }
    return errors;
}

int main() {
    int errors = 0;
    for (int limb = 0; limb < RNS_MODULUS_COUNT; ++limb) {
        uint32_t input[RNS_N];
        uint32_t transformed[RNS_N];
        uint32_t restored[RNS_N];
        uint32_t expected_forward[RNS_N];
        uint32_t expected_inverse[RNS_N];
        uint32_t modulus = RNS_MODULI[limb];

        for (int index = 0; index < RNS_N; ++index) {
            input[index] =
                ((uint64_t)(limb + 1) * 1000003U * index + 17U) % modulus;
        }
        ref_forward(input, expected_forward, limb);

        hls::stream<axis_word_t> in_forward;
        hls::stream<axis_word_t> out_forward;
        push_poly(in_forward, input);
        rns_ntt_onthefly_accel_n4096(in_forward, out_forward, RNS_OP_FORWARD, limb);
        pop_poly(out_forward, transformed);
        errors +=
            compare_poly("forward", transformed, expected_forward, limb);

        ref_inverse(transformed, expected_inverse, limb);
        hls::stream<axis_word_t> in_inverse;
        hls::stream<axis_word_t> out_inverse;
        push_poly(in_inverse, transformed);
        rns_ntt_onthefly_accel_n4096(in_inverse, out_inverse, RNS_OP_INVERSE, limb);
        pop_poly(out_inverse, restored);
        errors +=
            compare_poly("inverse", restored, expected_inverse, limb);
        errors += compare_poly("round-trip", restored, input, limb);

        uint32_t left[RNS_N];
        uint32_t right[RNS_N];
        uint32_t product[RNS_N];
        uint32_t expected_product[RNS_N];
        for (int index = 0; index < RNS_N; ++index) {
            left[index] = ((uint64_t)17U * index + 3U) % modulus;
            right[index] = ((uint64_t)29U * index + 5U) % modulus;
        }
        ref_negacyclic_mul(left, right, expected_product, modulus);

        hls::stream<axis_word_t> in_multiply;
        hls::stream<axis_word_t> out_multiply;
        push_poly(in_multiply, left);
        push_poly(in_multiply, right);
        rns_ntt_onthefly_accel_n4096(
            in_multiply,
            out_multiply,
            RNS_OP_MULTIPLY,
            limb);
        pop_poly(out_multiply, product);
        errors +=
            compare_poly("multiply", product, expected_product, limb);
    }

    if (errors != 0) {
        std::cerr << "test failed with " << errors << " mismatches\n";
        return 1;
    }
    std::cout << "RNS NTT forward/inverse/multiply tests passed for "
              << RNS_MODULUS_COUNT << " moduli\n";
    return 0;
}
