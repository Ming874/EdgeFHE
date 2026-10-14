// Deterministic key material and encryption randomness from a public seed, for QA and
// for comparing runs on different processes or machines bit for bit. Anyone who knows
// the seed can derive the secret key: never use this for real data. Real runs use the
// accepted OS sampler (core.hpp sample_material / sample_encryption, Linux getrandom).
#pragma once
#include "core.hpp"
#include <array>
#include <cstdint>

namespace edgefhe::v5 {
Material qa_material(uint64_t seed);
std::array<Ternary, 12> qa_samples(uint64_t seed, uint64_t batch);
}  // namespace edgefhe::v5
