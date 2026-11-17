// Off-board test of the forward-compatible capability policy (secure_bridge.hpp caps_accepted, docs/04 X1).
// The accepted V7 driver's exact capability block must still pass; RECOVERY_PROVEN, unknown low bits, missing
// required bits, wrong geometry/profile, poisoned state and reserved words must still fail; optional feature
// bits (16..31) and extra opcodes are accepted. No device is opened.
#include "secure_bridge.hpp"
#include <iostream>
using namespace edgefhe_v7;

int main() {
  edgefhe_v7_caps base{};
  base.abi_version = 7; base.flags = EDGEFHE_V7_CAP_HW_IDENTITY_VERIFIED | EDGEFHE_V7_CAP_FAIL_CLOSED;
  base.operation_mask = 0x1ff; base.n = 4096; base.limbs = 3; base.lanes_per_limb = 4;
  base.max_tx_bytes = 262144; base.max_rx_bytes = 524288;
  std::copy(active_profile.begin(), active_profile.end(), base.profile_sha256);
  struct Case { const char* name; bool expect; void (*edit)(edgefhe_v7_caps&); };
  const Case cases[] = {
    {"accepted_v7_driver_exact", true, [](edgefhe_v7_caps&) {}},
    {"optional_feature_bit16", true, [](edgefhe_v7_caps& c) { c.flags |= 1u << 16; }},
    {"optional_feature_bit31", true, [](edgefhe_v7_caps& c) { c.flags |= 1u << 31; }},
    {"extra_opcode_9", true, [](edgefhe_v7_caps& c) { c.operation_mask |= 1u << 9; }},
    {"recovery_proven_forbidden", false, [](edgefhe_v7_caps& c) { c.flags |= EDGEFHE_V7_CAP_RECOVERY_PROVEN; }},
    {"unknown_low_bit3", false, [](edgefhe_v7_caps& c) { c.flags |= 1u << 3; }},
    {"unknown_low_bit15", false, [](edgefhe_v7_caps& c) { c.flags |= 1u << 15; }},
    {"missing_identity_verified", false, [](edgefhe_v7_caps& c) { c.flags &= ~EDGEFHE_V7_CAP_HW_IDENTITY_VERIFIED; }},
    {"missing_fail_closed", false, [](edgefhe_v7_caps& c) { c.flags &= ~EDGEFHE_V7_CAP_FAIL_CLOSED; }},
    {"missing_opcode_7", false, [](edgefhe_v7_caps& c) { c.operation_mask &= ~(1u << 7); }},
    {"abi_8", false, [](edgefhe_v7_caps& c) { c.abi_version = 8; }},
    {"n_8192", false, [](edgefhe_v7_caps& c) { c.n = 8192; }},
    {"limbs_4", false, [](edgefhe_v7_caps& c) { c.limbs = 4; }},
    {"lanes_8", false, [](edgefhe_v7_caps& c) { c.lanes_per_limb = 8; }},
    {"tx_size", false, [](edgefhe_v7_caps& c) { c.max_tx_bytes = 1; }},
    {"rx_size", false, [](edgefhe_v7_caps& c) { c.max_rx_bytes = 1; }},
    {"poisoned", false, [](edgefhe_v7_caps& c) { c.poisoned = 1; }},
    {"profile_mismatch", false, [](edgefhe_v7_caps& c) { c.profile_sha256[31] ^= 1; }},
    {"reserved_word", false, [](edgefhe_v7_caps& c) { c.reserved[3] = 1; }},
  };
  unsigned passed = 0, total = 0;
  for (const auto& tc : cases) {
    auto c = base; tc.edit(c); total++;
    if (caps_accepted(c) == tc.expect) passed++;
    else std::cerr << "V7_CAPS_POLICY_CASE_FAIL " << tc.name << '\n';
  }
  if (passed != total) { std::cerr << "V7_CAPS_POLICY_SELFTEST_FAIL " << passed << '/' << total << '\n'; return 1; }
  std::cout << "V7_CAPS_POLICY_SELFTEST_PASS cases=" << total << " accepted_driver_still_accepted=1"
               " recovery_proven_rejected=1 optional_bits_ignored=1 no_device=1\n";
  return 0;
}
