// TEST ONLY: never use for encryption. Deterministic OpenFHE external PRNG engine for sampler_selftest:
// every engine instance replays the same splitmix64 stream, so two fresh threads draw identical sequences and
// any cross-thread sharing of an engine shows up as a mismatch. Counts engine construction and destruction.
#include "utils/prng/prng.h"
#include <atomic>
#include <cstdint>
namespace {
std::atomic<long> created{0}, destroyed{0};
class DeterministicTestEngine final : public PRNG {
  uint64_t state_ = 0x243f6a8885a308d3ull;
 public:
  DeterministicTestEngine() { created++; }
  ~DeterministicTestEngine() override { destroyed++; }
  result_type operator()() override {
    uint64_t z = (state_ += 0x9e3779b97f4a7c15ull);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ull;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebull;
    return result_type((z ^ (z >> 31)) >> 32);
  }
};
}  // namespace
extern "C" PRNG* createEngineInstance() { return new DeterministicTestEngine; }
extern "C" long edgefhe_test_engines_created() { return created.load(); }
extern "C" long edgefhe_test_engines_destroyed() { return destroyed.load(); }
