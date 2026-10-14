#!/usr/bin/env python3
"""Minimal CKKS canonical-embedding encoder/decoder reference model.

This module implements the mathematical slot-to-polynomial mapping used by
CKKS. It intentionally stops before encryption, RNS reduction, rescaling, and
security parameter selection. The output coefficients are signed integers and
serve as golden data for the later RNS hardware integration.
"""

from __future__ import annotations

import argparse
import cmath
import json
import math
from pathlib import Path


DEFAULT_N = 1024
DEFAULT_SCALE = 1 << 24


def _require_power_of_two(value: int) -> None:
    if value < 2 or value & (value - 1):
        raise ValueError(f"value must be a power of two >= 2, got {value}")


def fft(values: list[complex], inverse: bool = False) -> list[complex]:
    """Iterative radix-2 FFT using the conventional negative forward sign."""

    n = len(values)
    _require_power_of_two(n)
    out = [complex(value) for value in values]

    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            out[i], out[j] = out[j], out[i]

    length = 2
    sign = 1.0 if inverse else -1.0
    while length <= n:
        root = cmath.exp(sign * 2j * math.pi / length)
        half = length // 2
        for start in range(0, n, length):
            twiddle = 1 + 0j
            for offset in range(half):
                even = out[start + offset]
                odd = out[start + offset + half] * twiddle
                out[start + offset] = even + odd
                out[start + offset + half] = even - odd
                twiddle *= root
        length <<= 1

    if inverse:
        out = [value / n for value in out]
    return out


class CKKSEncoder:
    """Encode up to N/2 complex slots into Z[X]/(X^N + 1)."""

    def __init__(self, degree: int = DEFAULT_N, scale: int = DEFAULT_SCALE):
        _require_power_of_two(degree)
        if scale <= 0:
            raise ValueError("scale must be positive")
        self.degree = degree
        self.slot_count = degree // 2
        self.scale = scale
        self._zeta = cmath.exp(1j * math.pi / degree)

    def encode(self, slots: list[complex | float | int]) -> list[int]:
        if len(slots) > self.slot_count:
            raise ValueError(
                f"received {len(slots)} slots, maximum is {self.slot_count}"
            )

        evaluations = [0j] * self.degree
        for index, value in enumerate(slots):
            slot = complex(value)
            evaluations[index] = slot
            evaluations[self.degree - 1 - index] = slot.conjugate()

        frequency = fft(evaluations)
        coefficients: list[int] = []
        max_imaginary = 0.0
        for index, value in enumerate(frequency):
            coefficient = value / self.degree / (self._zeta**index)
            max_imaginary = max(max_imaginary, abs(coefficient.imag))
            coefficients.append(int(round(coefficient.real * self.scale)))

        if max_imaginary > 1e-8:
            raise ArithmeticError(
                f"canonical interpolation produced imaginary residue "
                f"{max_imaginary:.3e}"
            )
        return coefficients

    def decode(self, coefficients: list[int], count: int | None = None) -> list[complex]:
        if len(coefficients) != self.degree:
            raise ValueError(
                f"expected {self.degree} coefficients, got {len(coefficients)}"
            )
        if count is None:
            count = self.slot_count
        if count < 0 or count > self.slot_count:
            raise ValueError(f"count must be between 0 and {self.slot_count}")

        twisted = [
            (coefficient / self.scale) * (self._zeta**index)
            for index, coefficient in enumerate(coefficients)
        ]
        evaluations = [value * self.degree for value in fft(twisted, inverse=True)]
        return evaluations[:count]


def parse_slots(text: str) -> list[complex]:
    slots: list[complex] = []
    for token in text.split(","):
        stripped = token.strip()
        if stripped:
            slots.append(complex(stripped.replace("i", "j")))
    return slots


def error_metrics(actual: list[complex], expected: list[complex]) -> tuple[float, float]:
    errors = [abs(left - right) for left, right in zip(actual, expected)]
    max_error = max(errors, default=0.0)
    mse = sum(error * error for error in errors) / len(errors) if errors else 0.0
    return max_error, mse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--degree", type=int, default=DEFAULT_N)
    parser.add_argument("--scale", type=int, default=DEFAULT_SCALE)
    parser.add_argument("--values", default="1.5,-0.2,3.14")
    parser.add_argument("--tolerance", type=float, default=1e-5)
    parser.add_argument(
        "--json-out",
        type=Path,
        default=Path("build/reference/ckks_canonical_selftest.json"),
    )
    args = parser.parse_args()

    values = parse_slots(args.values)
    encoder = CKKSEncoder(args.degree, args.scale)
    coefficients = encoder.encode(values)
    recovered = encoder.decode(coefficients, len(values))
    max_error, mse = error_metrics(recovered, values)
    passed = mse < args.tolerance

    report = {
        "scheme_component": "ckks-canonical-embedding",
        "complete_secure_ckks": False,
        "degree": args.degree,
        "slot_capacity": encoder.slot_count,
        "scale": args.scale,
        "input": [{"real": value.real, "imag": value.imag} for value in values],
        "decoded": [{"real": value.real, "imag": value.imag} for value in recovered],
        "max_abs_error": max_error,
        "mse": mse,
        "tolerance": args.tolerance,
        "coefficient_abs_max": max(abs(value) for value in coefficients),
        "passed": passed,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"CKKS canonical embedding: {'pass' if passed else 'fail'}")
    print(f"degree          : {args.degree}")
    print(f"slot capacity   : {encoder.slot_count}")
    print(f"values          : {values}")
    print(f"decoded         : {recovered}")
    print(f"scale           : {args.scale}")
    print(f"max abs error   : {max_error:.12g}")
    print(f"MSE             : {mse:.12g}")
    print(f"coefficient max : {report['coefficient_abs_max']}")
    print(f"wrote {args.json_out}")

    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
