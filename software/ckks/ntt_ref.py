#!/usr/bin/env python3
"""Reference negacyclic NTT model for the EdgeFHE KV260 project."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


DEFAULT_N = 1024
DEFAULT_Q = 12289


def factorize(value: int) -> list[int]:
    factors: list[int] = []
    divisor = 2
    while divisor * divisor <= value:
        if value % divisor == 0:
            factors.append(divisor)
            while value % divisor == 0:
                value //= divisor
        divisor += 1
    if value > 1:
        factors.append(value)
    return factors


def primitive_root_prime(q: int) -> int:
    factors = factorize(q - 1)
    for candidate in range(2, q):
        if all(pow(candidate, (q - 1) // factor, q) != 1 for factor in factors):
            return candidate
    raise ValueError(f"no primitive root found for q={q}")


def bit_reverse_copy(values: list[int]) -> list[int]:
    n = len(values)
    width = n.bit_length() - 1
    out = [0] * n
    for index, value in enumerate(values):
        rev = int(f"{index:0{width}b}"[::-1], 2)
        out[rev] = value
    return out


def mod_inverse(value: int, q: int) -> int:
    return pow(value, q - 2, q)


def roots_for_negacyclic_ntt(n: int, q: int) -> tuple[int, int, int]:
    if (q - 1) % (2 * n) != 0:
        raise ValueError(f"q={q} must satisfy q = 1 mod 2N for N={n}")
    generator = primitive_root_prime(q)
    psi = pow(generator, (q - 1) // (2 * n), q)
    omega = pow(psi, 2, q)
    if pow(psi, n, q) != q - 1 or pow(omega, n, q) != 1:
        raise ValueError("invalid root selection")
    return generator, psi, omega


def ntt(values: list[int], root: int, q: int) -> list[int]:
    n = len(values)
    out = bit_reverse_copy([value % q for value in values])
    length = 2
    while length <= n:
        step_root = pow(root, n // length, q)
        half = length // 2
        for start in range(0, n, length):
            twiddle = 1
            for offset in range(half):
                left = out[start + offset]
                right = (out[start + offset + half] * twiddle) % q
                out[start + offset] = (left + right) % q
                out[start + offset + half] = (left - right) % q
                twiddle = (twiddle * step_root) % q
        length *= 2
    return out


def intt(values: list[int], root: int, q: int) -> list[int]:
    n = len(values)
    inv_root = mod_inverse(root, q)
    inv_n = mod_inverse(n, q)
    return [(value * inv_n) % q for value in ntt(values, inv_root, q)]


def negacyclic_forward(values: list[int], q: int) -> list[int]:
    n = len(values)
    _, psi, omega = roots_for_negacyclic_ntt(n, q)
    twisted = [(value * pow(psi, index, q)) % q for index, value in enumerate(values)]
    return ntt(twisted, omega, q)


def negacyclic_inverse(values: list[int], q: int) -> list[int]:
    n = len(values)
    _, psi, omega = roots_for_negacyclic_ntt(n, q)
    psi_inv = mod_inverse(psi, q)
    untwisted = intt(values, omega, q)
    return [
        (value * pow(psi_inv, index, q)) % q
        for index, value in enumerate(untwisted)
    ]


def schoolbook_negacyclic_mul(a: list[int], b: list[int], q: int) -> list[int]:
    n = len(a)
    out = [0] * n
    for i, aval in enumerate(a):
        for j, bval in enumerate(b):
            product = (aval * bval) % q
            index = i + j
            if index >= n:
                out[index - n] = (out[index - n] - product) % q
            else:
                out[index] = (out[index] + product) % q
    return out


def ntt_negacyclic_mul(a: list[int], b: list[int], q: int) -> list[int]:
    a_ntt = negacyclic_forward(a, q)
    b_ntt = negacyclic_forward(b, q)
    product_ntt = [(left * right) % q for left, right in zip(a_ntt, b_ntt)]
    return negacyclic_inverse(product_ntt, q)


def random_poly(n: int, q: int, rng: random.Random) -> list[int]:
    return [rng.randrange(q) for _ in range(n)]


def self_test(n: int, q: int, seed: int, rounds: int) -> None:
    generator, psi, omega = roots_for_negacyclic_ntt(n, q)
    rng = random.Random(seed)
    print(
        json.dumps(
            {
                "n": n,
                "q": q,
                "primitive_root": generator,
                "psi": psi,
                "omega": omega,
                "rounds": rounds,
            },
            indent=2,
        )
    )

    for round_index in range(rounds):
        a = random_poly(n, q, rng)
        b = random_poly(n, q, rng)
        restored = negacyclic_inverse(negacyclic_forward(a, q), q)
        if restored != [value % q for value in a]:
            raise AssertionError(f"forward/inverse mismatch at round {round_index}")
        expected = schoolbook_negacyclic_mul(a, b, q)
        actual = ntt_negacyclic_mul(a, b, q)
        if actual != expected:
            raise AssertionError(f"multiplication mismatch at round {round_index}")
    print("self-test: pass")


def emit_vectors(output_dir: Path, n: int, q: int, seed: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    a = random_poly(n, q, rng)
    b = random_poly(n, q, rng)
    payload = {
        "n": n,
        "q": q,
        "a": a,
        "b": b,
        "a_ntt": negacyclic_forward(a, q),
        "b_ntt": negacyclic_forward(b, q),
        "mul": ntt_negacyclic_mul(a, b, q),
    }
    (output_dir / "ntt_vectors.json").write_text(json.dumps(payload, indent=2))
    print(f"wrote {output_dir / 'ntt_vectors.json'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    parser.add_argument("--q", type=int, default=DEFAULT_Q)
    parser.add_argument("--seed", type=int, default=260)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--emit-vectors", type=Path)
    args = parser.parse_args()

    if args.emit_vectors:
        emit_vectors(args.emit_vectors, args.n, args.q, args.seed)
    if args.self_test or not args.emit_vectors:
        self_test(args.n, args.q, args.seed, args.rounds)


if __name__ == "__main__":
    main()
