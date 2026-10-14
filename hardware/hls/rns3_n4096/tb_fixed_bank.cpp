#define main unused_original_reference_main
#include "hls_source/testbench/tb_rns_ntt_parallel.cpp"
#undef main
#include "rns_ntt_parallel_rns3.hpp"
#include <cstdlib>
#include <stdexcept>
#include <vector>

uint32_t candidate_montgomery(uint32_t, uint32_t, int);
uint32_t candidate_ordinary(uint32_t, uint32_t, int);
uint32_t candidate_add(uint32_t, uint32_t, int);
uint32_t candidate_sub(uint32_t, uint32_t, int);
void candidate_stage(const uint32_t *, uint32_t *, int, int, int);

typedef std::vector<std::vector<uint32_t> > Poly;
static uint64_t compared_coefficients = 0;
static unsigned top_transactions = 0;
static uint64_t random_state = 0x5e1b4a9d73126003ULL;

static void require(bool passed, const char *message) {
    if (!passed) throw std::runtime_error(message);
}
static uint32_t next_value(uint32_t q) {
    random_state ^= random_state << 13;
    random_state ^= random_state >> 7;
    random_state ^= random_state << 17;
    return uint32_t(random_state % q);
}
static Poly poly() { return Poly(3, std::vector<uint32_t>(RNS_N)); }
static void equal(const Poly &actual, const Poly &expected, const char *label) {
    for (int limb = 0; limb < 3; ++limb)
        for (int i = 0; i < RNS_N; ++i) {
            if (actual[limb][i] != expected[limb][i]) {
                std::cerr << label << " limb=" << limb << " coefficient=" << i
                          << " actual=" << actual[limb][i] << " expected=" << expected[limb][i] << '\n';
                throw std::runtime_error("coefficient mismatch");
            }
            ++compared_coefficients;
        }
}
static Poly pattern(int kind) {
    Poly result = poly();
    for (int limb = 0; limb < 3; ++limb) {
        const uint32_t q = RNS_MODULI[limb];
        for (int i = 0; i < RNS_N; ++i) {
            switch (kind) {
            case 0: result[limb][i] = 0; break;
            case 1: result[limb][i] = i == RNS_N - 1 ? q - 1 : 0; break;
            case 2: result[limb][i] = q - 1; break;
            case 3: result[limb][i] = (i & 1) ? q - 1 : 0; break;
            case 4: result[limb][i] = uint64_t(i) * 1000003 % q; break;
            default: result[limb][i] = next_value(q); break;
            }
        }
    }
    return result;
}
static void push(hls::stream<rns3_axis_word_t> &stream, const Poly &p, int index, bool last) {
    rns3_axis_word_t word;
    word.data = 0;
    for (int limb = 0; limb < 3; ++limb)
        word.data.range(32 * limb + 31, 32 * limb) = p[limb][index];
    word.keep = -1;
    word.strb = -1;
    word.last = last ? 1 : 0;
    stream.write(word);
}
static Poly run(operation_t operation, const Poly &a, const Poly &b, const Poly &c) {
    hls::stream<rns3_axis_word_t> input, output;
    const int op = operation.to_uint();
    if (op == 2) {
        for (int i = 0; i < RNS_N; ++i) push(input, a, i, false);
        for (int i = 0; i < RNS_N; ++i) push(input, b, i, i == RNS_N - 1);
    } else if (op >= 3) {
        for (int i = 0; i < RNS_N; ++i) {
            push(input, a, i, false);
            push(input, b, i, op != 4 && i == RNS_N - 1);
            if (op == 4) push(input, c, i, i == RNS_N - 1);
        }
    } else {
        for (int i = 0; i < RNS_N; ++i) push(input, a, i, i == RNS_N - 1);
    }
    rns3_ntt_parallel_accel_n4096(input, output, operation);
    require(input.empty(), "input transaction not completely consumed");
    Poly result = poly();
    for (int i = 0; i < RNS_N; ++i) {
        require(!output.empty(), "short output packet");
        const rns3_axis_word_t word = output.read();
        require(word.last == (i == RNS_N - 1), "TLAST misplaced");
        require(word.keep == 0xffff && word.strb == 0xffff, "invalid TKEEP/TSTRB");
        require(word.data.range(127, 96) == 0, "nonzero padding word");
        for (int limb = 0; limb < 3; ++limb) {
            result[limb][i] = word.data.range(32 * limb + 31, 32 * limb).to_uint();
            require(result[limb][i] < RNS_MODULI[limb], "noncanonical output residue");
        }
    }
    require(output.empty(), "extra output words");
    ++top_transactions;
    return result;
}
static void arithmetic_audit() {
    unsigned pairs = 0;
    for (int limb = 0; limb < 3; ++limb) {
        const uint32_t q = RNS_MODULI[limb];
        const uint32_t r = (uint64_t(1) << 32) % q;
        const uint32_t rinv = ref_mod_pow(r, q - 2, q);
        require(((uint64_t(q) * RNS_MONTGOMERY_NEG_INV[limb].to_uint()) & 0xffffffffULL) == 0xffffffffULL,
                "q negative inverse constant mismatch");
        require(RNS_MONTGOMERY_R[limb] == r, "R constant mismatch");
        require(RNS_MONTGOMERY_R2[limb] == uint64_t(r) * r % q, "R2 constant mismatch");
        require(ref_mod_pow(RNS_PSI[limb], RNS_N, q) == q - 1, "psi is not a negacyclic root");
        require(ref_mod_pow(RNS_PSI[limb], 2 * RNS_N, q) == 1, "psi root order mismatch");
        require(uint64_t(RNS_PSI[limb].to_uint()) * RNS_PSI[limb].to_uint() % q == RNS_OMEGA[limb], "omega mismatch");
        const uint32_t boundary[] = {0, 1, 2, q / 2 - 1, q / 2, q / 2 + 1, q - 3, q - 2, q - 1};
        for (unsigned test = 0; test < 10081; ++test) {
            const uint32_t a = test < 81 ? boundary[test / 9] : next_value(q);
            const uint32_t b = test < 81 ? boundary[test % 9] : next_value(q);
            const uint32_t ordinary = uint64_t(a) * b % q;
            require(candidate_montgomery(a, b, limb) == uint64_t(ordinary) * rinv % q, "Montgomery mismatch");
            require(candidate_ordinary(a, b, limb) == ordinary, "ordinary multiplication mismatch");
            require(candidate_add(a, b, limb) == (uint64_t(a) + b) % q, "modular addition mismatch");
            require(candidate_sub(a, b, limb) == (uint64_t(a) + q - b) % q, "modular subtraction mismatch");
            ++pairs;
        }
    }
    std::cout << "actual arithmetic audit: PASS (" << pairs << " operand pairs, four functions each)\n";
}
static void stage_audit() {
    unsigned cases = 0;
    for (int kind = 2; kind < 5; ++kind) {
        Poly input = pattern(kind == 4 ? 5 : kind);
        for (int limb = 0; limb < 3; ++limb) {
            uint32_t q = RNS_MODULI[limb];
            for (int inverse = 0; inverse < 2; ++inverse)
                for (int stage = 0; stage < 12; ++stage) {
                    const int half = 1 << stage;
                    uint32_t root = ref_mod_pow(inverse ? RNS_OMEGA_INV[limb].to_uint() : RNS_OMEGA[limb].to_uint(), RNS_N / (2 * half), q);
                    std::vector<uint32_t> expected(RNS_N), actual(RNS_N);
                    for (int start = 0; start < RNS_N; start += 2 * half) {
                        uint32_t w = 1;
                        for (int j = 0; j < half; ++j) {
                            uint32_t left = input[limb][start + j];
                            uint32_t right = uint64_t(input[limb][start + j + half]) * w % q;
                            expected[start + j] = (uint64_t(left) + right) % q;
                            expected[start + j + half] = (uint64_t(left) + q - right) % q;
                            w = uint64_t(w) * root % q;
                        }
                    }
                    candidate_stage(input[limb].data(), actual.data(), stage, inverse, limb);
                    require(actual == expected, "individual butterfly stage mismatch");
                    compared_coefficients += RNS_N;
                    ++cases;
                }
        }
    }
    std::cout << "actual stage audit: PASS (" << cases << " cases, all coefficients)\n";
}
static uint32_t direct_forward(const std::vector<uint32_t> &input, int k, int limb) {
    uint32_t q = RNS_MODULI[limb];
    uint32_t x = ref_mod_pow(RNS_PSI[limb], 2 * k + 1, q), power = 1, sum = 0;
    for (int i = 0; i < RNS_N; ++i) {
        sum = (sum + uint64_t(input[i]) * power) % q;
        power = uint64_t(power) * x % q;
    }
    return sum;
}
int main() {
    try {
        const bool smoke = std::getenv("EDGEFHE_COSIM_SMOKE") != 0;
        std::cout << "validation mode: " << (smoke ? "RTL_SMOKE" : "FULL_CSIM") << '\n';
        if (!smoke) { arithmetic_audit(); stage_audit(); }
        Poly empty = poly();
        for (int kind = smoke ? 5 : 0; kind < 6; ++kind) {
            Poly input = pattern(kind), expected = poly();
            for (int limb = 0; limb < 3; ++limb) ref_forward(input[limb].data(), expected[limb].data(), limb);
            Poly transformed = run(RNS_OP_FORWARD, input, empty, empty);
            equal(transformed, expected, "forward reference");
            const int probes[] = {0, 1, RNS_N / 2, RNS_N - 1};
            for (int limb = 0; limb < 3; ++limb)
                for (int k : probes) require(transformed[limb][k] == direct_forward(input[limb], k, limb), "direct negacyclic evaluation mismatch");
            equal(run(RNS_OP_INVERSE, transformed, empty, empty), input, "inverse round trip");
        }
        // An arbitrary spectrum independently checks inverse, beyond round trip.
        Poly spectrum = pattern(5), inverse_expected = poly();
        for (int limb = 0; limb < 3; ++limb) ref_inverse(spectrum[limb].data(), inverse_expected[limb].data(), limb);
        equal(run(RNS_OP_INVERSE, spectrum, empty, empty), inverse_expected, "arbitrary inverse");
        for (int kind = 0; kind < (smoke ? 1 : 3); ++kind) {
            Poly a = poly(), b = poly(), expected = poly();
            for (int limb = 0; limb < 3; ++limb) {
                const uint32_t q = RNS_MODULI[limb];
                if (kind == 0) {
                    a[limb][RNS_N - 1] = q - 1; a[limb][17] = 3;
                    b[limb][1] = 5; b[limb][RNS_N - 3] = q - 2;
                } else {
                    for (int i = 0; i < RNS_N; ++i) {
                        a[limb][i] = kind == 1 ? q - 1 : next_value(q);
                        b[limb][i] = kind == 1 ? q - 1 : next_value(q);
                    }
                }
                // Independent, full O(N^2) signed-wrap convolution; no NTT.
                ref_negacyclic_mul(a[limb].data(), b[limb].data(), expected[limb].data(), q);
            }
            equal(run(RNS_OP_MULTIPLY, a, b, empty), expected, "direct full negacyclic convolution");
        }
        Poly a = pattern(3), b = pattern(2), c = pattern(5);
        for (int op = 3; op <= 5; ++op) {
            Poly expected = poly();
            for (int limb = 0; limb < 3; ++limb) {
                const uint32_t q = RNS_MODULI[limb];
                for (int i = 0; i < RNS_N; ++i) {
                    uint64_t value = op == 5 ? uint64_t(q) - a[limb][i] + b[limb][i]
                                            : uint64_t(a[limb][i]) + b[limb][i] + (op == 4 ? c[limb][i] : 0);
                    expected[limb][i] = value % q;
                }
            }
            equal(run(operation_t(op), a, b, c), expected, "vector ALU");
        }
        std::cout << "fixed-bank validation: PASS (transactions=" << top_transactions
                  << ", coefficient_comparisons=" << compared_coefficients
                  << ", TLAST/TKEEP/TSTRB/padding/packet counts checked)\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "fixed-bank validation: FAIL: " << error.what() << '\n';
        return 1;
    }
}
