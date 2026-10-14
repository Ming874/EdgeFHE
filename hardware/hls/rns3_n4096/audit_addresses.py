"""Exhaustively verify the candidate's gather/scatter and Montgomery bounds."""
from pathlib import Path
import json
import math
import re

N, BANKS = 4096, 4
MODULI = (998244353, 1004535809, 469762049)


def main():
    constants_source = (Path(__file__).parent / "hls_source/src/rns_ntt_parallel_constants.hpp").read_text()
    def constants(name):
        match = re.search(r"\b" + name + r"\[[^;=]+?=\s*\{(.*?)\};", constants_source, re.S)
        assert match, name
        return [int(value) for value in re.findall(r"(\d+)U", match.group(1))]
    assert constants("RNS_MODULI") == list(MODULI)
    # Trial division is an exact primality proof for these small, fixed primes.
    for limb, q in enumerate(MODULI):
        assert q % 2 and all(q % d for d in range(3, math.isqrt(q) + 1, 2))
        assert (q - 1) % (2 * N) == 0
        r = 1 << 32
        psi = constants("RNS_PSI")[limb]
        omega = psi * psi % q
        assert pow(psi, N, q) == q - 1 and pow(psi, 2 * N, q) == 1
        # N is a power of two: psi^N=-1 proves exact order 2N.
        expected = {"RNS_MONTGOMERY_NEG_INV": (-pow(q, -1, r)) % r,
                    "RNS_MONTGOMERY_R": r % q, "RNS_MONTGOMERY_R2": r * r % q,
                    "RNS_OMEGA": omega, "RNS_OMEGA_INV": pow(omega, -1, q),
                    "RNS_INV_N": pow(N, -1, q), "RNS_PSI_MONT": psi * r % q,
                    "RNS_PSI_INV_MONT": pow(psi, -1, q) * r % q,
                    "RNS_INV_N_MONT": pow(N, -1, q) * r % q}
        for name, value in expected.items():
            assert constants(name)[limb] == value, name
        for inverse in (False, True):
            name = "RNS_STAGE_ROOT_INV_MONT" if inverse else "RNS_STAGE_ROOT_FWD_MONT"
            roots = constants(name)
            assert len(roots) == 36
            root = pow(omega, -1, q) if inverse else omega
            for stage in range(12):
                assert roots[12 * limb + stage] == pow(root, N >> (stage + 1), q) * r % q
    batch_count = access_count = 0
    for stage in range(12):
        seen = set()
        for base in range(0, N // 2, 4):
            if stage < 2:
                addr0, addr1, tw_addr = base >> 1, (base >> 1) + 1, 0
            else:
                offset = base & ((1 << stage) - 1)
                addr0 = ((base >> stage) << (stage - 1)) + (offset >> 2)
                addr1, tw_addr = addr0 + (1 << (stage - 2)), offset >> 2
            assert 0 <= addr0 < N // BANKS and 0 <= addr1 < N // BANKS and addr0 != addr1
            reads = [[4 * a + bank for a in (addr0, addr1)] for bank in range(4)]
            sums, differences = [None] * 4, [None] * 4
            for lane in range(4):
                if stage == 0:
                    lb, rb, p, tw_bank = (lane & 1) << 1, ((lane & 1) << 1) + 1, lane >> 1, 0
                    actual = reads[lb][p], reads[rb][p]
                elif stage == 1:
                    lb, rb, p, tw_bank = lane & 1, (lane & 1) + 2, lane >> 1, lane & 1
                    actual = reads[lb][p], reads[rb][p]
                else:
                    tw_bank = lane
                    actual = reads[lane][0], reads[lane][1]
                b, half = base + lane, 1 << stage
                left = ((b >> stage) << (stage + 1)) + (b & (half - 1))
                assert actual == (left, left + half)
                assert 4 * tw_addr + tw_bank == b & (half - 1)
                sums[lane], differences[lane] = left, left + half
            for bank in range(4):
                if stage == 0:
                    source = differences if bank & 1 else sums
                    destinations = source[bank >> 1], source[(bank >> 1) + 2]
                elif stage == 1:
                    source = sums if bank < 2 else differences
                    destinations = source[bank & 1], source[(bank & 1) + 2]
                else:
                    destinations = sums[bank], differences[bank]
                assert destinations == tuple(reads[bank])
                for index in destinations:
                    assert index not in seen
                    seen.add(index)
                    access_count += 1
            batch_count += 1
        assert seen == set(range(N))
    bounds = []
    for q in MODULI:
        r = 1 << 32
        maximum_corrected_sum = (q - 1) ** 2 + (r - 1) * q
        assert q < 1 << 30 and maximum_corrected_sum < 1 << 64
        # Canonical operands imply t<q^2 and REDC result <q+q^2/R<2q.
        assert q * q < q * r
        bounds.append({"q": q, "corrected_sum_upper_bound": maximum_corrected_sum,
                       "uint64_no_overflow": True, "single_subtraction_sufficient": True})
    report = {"passed": True, "batches": batch_count, "coefficient_addresses": access_count,
              "checks": ["gather", "scatter", "twiddle_index", "address_bounds", "stage_bijection",
                         "exact_primality", "all_constants", "exact_root_orders", "72_stage_root_constants"],
              "montgomery_bounds": bounds,
              "scope": "exhaustive integer index proof and arithmetic bounds; not RTL timing or board acceptance"}
    path = Path(__file__).with_name("address_math_audit.json")
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Address/math audit: PASS ({batch_count} batches, {access_count} coefficient addresses)")


if __name__ == "__main__":
    main()
