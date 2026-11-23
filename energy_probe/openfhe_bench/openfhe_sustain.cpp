// openfhe_sustain.cpp : OpenFHE 1.5.1 CKKS public-key encryption loop for power measurement.
// Same definition as V8b `board_enc_check sustain` and edgefhe2 `--sustain`:
//   a frame = K ciphertexts; the K plaintexts are encoded once (encoding is not in the loop);
//   the loop does nothing but Encrypt(pk, pt); the first frame and the last ciphertext are decrypted and checked.
// Parameters follow docs/NATIVE_V4.md section 4.4 (the repo's standard OpenFHE baseline):
//   N 4096, batch 2048, depth 2, first mod 30, scaling mod 29, UNIFORM_TERNARY, HEStd_128_classic, BV, FIXEDMANUAL.
// usage: openfhe_sustain <seconds> [k=8] [threads=1]
//   threads=1 : one encrypting thread (OpenFHE's own OpenMP loops use OMP_NUM_THREADS)
//   threads=T : T OpenMP threads encrypt independent ciphertexts (nested regions inside OpenFHE run single-threaded)
#include "openfhe.h"
#include <omp.h>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <vector>

using namespace lbcrypto;
using Clock = std::chrono::steady_clock;

int main(int argc, char** argv) {
    const int secs = argc > 1 ? std::atoi(argv[1]) : 30;
    const int K = argc > 2 ? std::atoi(argv[2]) : 8;
    const int T = argc > 3 ? std::atoi(argv[3]) : 1;
    if (secs < 1 || secs > 600 || K < 1 || K > 8 || T < 1 || T > 4) {
        std::printf("usage: openfhe_sustain <seconds 1..600> [k 1..8] [threads 1..4]\n");
        return 2;
    }
    const uint32_t slots = 2048;

    CCParams<CryptoContextCKKSRNS> p;
    p.SetMultiplicativeDepth(2);
    p.SetFirstModSize(30);
    p.SetScalingModSize(29);
    p.SetRingDim(4096);
    p.SetBatchSize(slots);
    p.SetSecretKeyDist(UNIFORM_TERNARY);
    p.SetSecurityLevel(HEStd_128_classic);
    p.SetKeySwitchTechnique(BV);
    p.SetScalingTechnique(FIXEDMANUAL);
    CryptoContext<DCRTPoly> cc = GenCryptoContext(p);
    cc->Enable(PKE);
    cc->Enable(KEYSWITCH);
    cc->Enable(LEVELEDSHE);
    auto kp = cc->KeyGen();

    const auto& towers = cc->GetCryptoParameters()->GetElementParams()->GetParams();
    std::printf("OpenFHE %s CKKS N=%u slots=%u towers=%zu q=", GetOPENFHEVersion().c_str(), cc->GetRingDimension(), slots,
                towers.size());
    for (const auto& t : towers) std::printf("%s ", t->GetModulus().ToString().c_str());
    std::printf("omp_max_threads=%d\n", omp_get_max_threads());

    // K feature vectors, normalized like the edgefhe2 test data
    std::vector<std::vector<double>> x(K, std::vector<double>(slots));
    std::vector<Plaintext> pt(K);
    for (int c = 0; c < K; c++) {
        for (uint32_t i = 0; i < slots; i++)
            x[c][i] = (1.2 * std::sin(2 * M_PI * (i + 37.0 * c) / 256.0) + 0.3 * std::cos(2 * M_PI * i / 19.0)) / 2.0;
        pt[c] = cc->MakeCKKSPackedPlaintext(x[c]);
    }
    auto max_err = [&](const Ciphertext<DCRTPoly>& ct, int c) {
        Plaintext out;
        cc->Decrypt(kp.secretKey, ct, &out);
        out->SetLength(slots);
        const auto v = out->GetRealPackedValue();
        double e = 0;
        for (uint32_t i = 0; i < slots; i++) e = std::max(e, std::fabs(v[i] - x[c][i]));
        return e;
    };
    double first_err = 0;
    for (int c = 0; c < K; c++) first_err = std::max(first_err, max_err(cc->Encrypt(kp.publicKey, pt[c]), c));
    const double bound = 1e-3;
    if (!(first_err < bound)) {
        std::printf("STOP first frame decrypt error %.3e above %.0e\n", first_err, bound);
        return 1;
    }
    std::printf("SUSTAIN_BEGIN k=%d seconds=%d threads=%d (first frame decrypt error %.3e)\n", K, secs, T, first_err);
    std::fflush(stdout);

    std::atomic<long> cts{0};
    Ciphertext<DCRTPoly> last;
    const auto t0 = Clock::now();
    const auto end = t0 + std::chrono::seconds(secs);
    if (T == 1) {
        while (Clock::now() < end) {
            for (int c = 0; c < K; c++) last = cc->Encrypt(kp.publicKey, pt[c]);
            cts += K;
        }
    } else {
#pragma omp parallel num_threads(T)
        {
            Ciphertext<DCRTPoly> mine;
            long n = 0;
            while (Clock::now() < end) {
                for (int c = 0; c < K; c++) mine = cc->Encrypt(kp.publicKey, pt[c]);
                n += K;
            }
            cts += n;
#pragma omp critical
            if (omp_get_thread_num() == 0) last = mine;
        }
    }
    const double sec = std::chrono::duration<double>(Clock::now() - t0).count();
    const double last_err = max_err(last, K - 1);
    const long n = cts.load();
    const bool ok = last_err < bound;
    std::printf("%s seconds=%.3f k=%d threads=%d frames=%ld ciphertexts=%ld ciphertexts_per_s=%.1f "
                "records_per_s_4_features=%.0f mean_ct_ms=%.4f last_decrypt_error=%.3e\n",
                ok ? "OPENFHE_SUSTAIN_PASS" : "OPENFHE_SUSTAIN_FAIL", sec, K, T, n / K, n, n / sec,
                n * double(slots) / 4.0 / sec, sec * 1000.0 * T / n, last_err);
    return ok ? 0 : 1;
}
