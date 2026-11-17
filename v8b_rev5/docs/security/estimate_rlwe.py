"""Simplified, independent RLWE security estimate for the EdgeFHE profile (B02; local, no board).

Primary evidence is the HomomorphicEncryption.org standard table (Albrecht et al., HE Standard v1.1, 2018: ternary
secret, sigma ~3.2): for n=4096 the largest log2(q) is 109 bits (128-bit classical) and 101 bits (128-bit quantum).
This script adds a second, simplified view: the primal uSVP attack in the 2016 "core-SVP" model (Alkim-Ducas-Pöppelmann-
Schwabe), with secret/error rescaling for a small secret; cost 2^(0.292 b) classical and 2^(0.265 b) quantum for
the smallest BKZ block size b that succeeds, minimised over the number of samples m <= 2n. It is NOT the lattice
estimator (no hybrid/dual/BDD refinements, no rounding of costs) and is reported as DERIVED.
usage: python -X utf8 -B estimate_rlwe.py"""
import json, math
from pathlib import Path

PRIMES = [998244353, 1004535809, 469762049]   # vendor/frozen_sdk/src/client/baseline/core.hpp
N, SIGMA_E = 4096, 4.5                         # degree; CDT63 / OpenFHE discrete Gaussian sigma (secure.cpp, cdt63_table.hpp)
SIGMA_S = math.sqrt(2 / 3)                     # uniform ternary secret {-1,0,1}
HE_STANDARD = {4096: dict(classical_128=109, quantum_128=101)}


def delta(b):
    return ((math.pi * b) ** (1 / b) * b / (2 * math.pi * math.e)) ** (1 / (2 * (b - 1)))


def primal_block_size(n, logq, sigma_e, sigma_s, m):
    """Smallest b with sigma_e*sqrt(b) <= delta^(2b-d) * (q^m * nu^n)^(1/d), d = m+n+1, nu = sigma_e/sigma_s."""
    nu = sigma_e / sigma_s; d = m + n + 1
    log_vol = (m * logq * math.log(2) + n * math.log(nu)) / d
    for b in range(50, d):
        if math.log(sigma_e * math.sqrt(b)) <= (2 * b - d) * math.log(delta(b)) + log_vol:
            return b
    return None


def main():
    logq = sum(math.log2(p) for p in PRIMES)
    best = None
    for m in range(N // 2, 2 * N + 1, 64):
        b = primal_block_size(N, logq, SIGMA_E, SIGMA_S, m)
        if b and (best is None or b < best[0]):
            best = (b, m)
    b, m = best
    out = dict(status='B02_SIMPLIFIED_ESTIMATE_DERIVED_NOT_LATTICE_ESTIMATOR', n=N, primes=PRIMES, log2_q=round(logq, 3), sigma_e=SIGMA_E, secret='uniform ternary (dense)',
               he_standard_limits=HE_STANDARD[N], within_he_standard_classical_128=logq <= HE_STANDARD[N]['classical_128'], within_he_standard_quantum_128=logq <= HE_STANDARD[N]['quantum_128'],
               primal_usvp=dict(block_size=b, samples_m=m, core_svp_classical_bits=round(0.292 * b, 1), core_svp_quantum_bits=round(0.265 * b, 1)),
               note='simplified primal uSVP only; hybrid, dual and refined BKZ cost models are not covered; the HE Standard table is the primary reference')
    (Path(__file__).resolve().parent / 'ESTIMATE.json').write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    main()
