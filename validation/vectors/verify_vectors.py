"""Independently verify all public OpenFHE vectors and freeze their provenance."""
import hashlib
import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parent
N = 4096
Q = [998244353, 1004535809, 469762049]
PSI = [350007156, 2062645, 62025685]
PIN = "1306d14f8c26bb6150d3e6ad54f28dfe1007689e"


def transform(a, q, psi, inverse=False):
    values = list(a)
    if not inverse:
        w = 1
        for i in range(N):
            values[i] = values[i] * w % q
            w = w * psi % q
    j = 0
    for i in range(1, N):
        bit = N >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            values[i], values[j] = values[j], values[i]
    omega = psi * psi % q
    if inverse:
        omega = pow(omega, -1, q)
    length = 2
    while length <= N:
        half = length // 2
        root = pow(omega, N // length, q)
        for start in range(0, N, length):
            w = 1
            for offset in range(half):
                l = start + offset
                r = l + half
                left = values[l]
                right = values[r] * w % q
                values[l], values[r] = (left + right) % q, (left - right) % q
                w = w * root % q
        length <<= 1
    if inverse:
        w = pow(N, -1, q)
        inverse_psi = pow(psi, -1, q)
        for i in range(N):
            values[i] = values[i] * w % q
            w = w * inverse_psi % q
    return values


def main():
    assert (ROOT / "openfhe_revision.txt").read_text().strip() == PIN
    data = (ROOT / "vectors.bin").read_bytes()
    assert data[:8] == b"EFNTTV1\0"
    words = struct.unpack("<" + "I" * ((len(data) - 8) // 4), data[8:])
    assert words[:4] == (1, N, 3, 16)
    assert list(words[4:7]) == Q and list(words[7:10]) == PSI
    pos, comparisons, operations = 10, 0, []
    for case in range(16):
        op = words[pos]
        pos += 1
        polynomials = []
        for _ in range(3):
            polynomials.append([list(words[pos + limb * N:pos + (limb + 1) * N]) for limb in range(3)])
            pos += 3 * N
        a, b, expected = polynomials
        assert op in (0, 1, 2)
        for limb, (q, psi) in enumerate(zip(Q, PSI)):
            assert all(0 <= v < q for p in polynomials for v in p[limb])
            if op == 2:
                fa, fb = transform(a[limb], q, psi), transform(b[limb], q, psi)
                result = transform([x * y % q for x, y in zip(fa, fb)], q, psi, True)
            else:
                result = transform(a[limb], q, psi, bool(op))
            assert result == expected[limb], f"Independent oracle mismatch: case={case}, limb={limb}"
            comparisons += N
        operations.append(op)
    assert pos == len(words)
    report = {"status": "PASS", "scope": "V1 low-level polynomial interoperability; not OpenFHE CKKS ciphertext interoperability",
              "openfhe_revision": PIN, "n": N, "primes": Q, "psi": PSI,
              "library_output_order": "bit-reversed; exact permutation independently measured from transform of x",
              "hls_output_order": "natural; explicit adapter", "cases": 16,
              "operations": operations, "independent_coefficient_comparisons": comparisons,
              "secret_keys_generated": False,
              "vector_sha256": hashlib.sha256(data).hexdigest(),
              "generator_sha256": hashlib.sha256((ROOT / "export_vectors.cpp").read_bytes()).hexdigest(),
              "library_binary_sha256": (ROOT / "library_binary_sha256.txt").read_text().strip().splitlines()}
    (ROOT / "vector_manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Independent OpenFHE V1 vector verification: PASS ({comparisons} coefficients, 16 cases)")


if __name__ == "__main__":
    main()
