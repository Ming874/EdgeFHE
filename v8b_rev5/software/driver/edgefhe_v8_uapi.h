/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
/* EdgeFHE V8b research driver ABI 8 (candidate-4, 2026-10-16). Frame format: v8a_hls_20261015/ref/v8_ref.hpp and
 * v8b_ref.hpp. The driver writes the frame header itself (MAGIC, command, k, lease generation) and returns only the
 * payload between the hardware's header echo and its trailer, and only when the trailer status is 0.
 * Commands: 6 LOAD (payload: b then a, limb-major, 3 x 1024 beats each; no output payload), 7 PAIR (k x 64 beats of 2-bit
 * trit codes; output k x 6144 beats), 8 INVALIDATE (no payloads), 9 ENC (2 seed beats + k x 2048 beats of int64 pairs;
 * output k x 6144 beats). Output beat i of a feature carries limb i % 3. Byte counts are payload bytes. */
#ifndef EDGEFHE_V8_UAPI_H
#define EDGEFHE_V8_UAPI_H
#include <linux/types.h>
#include <linux/ioctl.h>
#define EDGEFHE_V8_ABI 8U
#define EDGEFHE_V8_MAGIC 0x45463841U
#define EDGEFHE_V8_MAX_K 8U
#define EDGEFHE_V8_MAX_TX_BYTES 262192U /* header + ENC k = 8 payload */
#define EDGEFHE_V8_MAX_RX_BYTES 786464U /* header echo + PAIR/ENC k = 8 payload + trailer */
struct edgefhe_v8_request {
    __u32 abi_version, command, k, flags;
    __u64 input_ptr, output_ptr;
    __u32 input_bytes, output_bytes;
    __u64 generation;              /* LOAD: in 0, out the new lease; PAIR/ENC/INVALIDATE: the caller's lease */
    __u8 profile_sha256[32], public_key_sha256[32];
    __u32 timeout_ms, elapsed_us, hardware_status;
    __s32 status;
};
struct edgefhe_v8_caps {
    __u32 abi_version, flags, command_mask, n;
    __u32 limbs, lanes_per_limb, max_tx_bytes, max_rx_bytes;
    __u64 generation;
    __u32 cache_valid_for_caller, poisoned;
    __u8 profile_sha256[32], public_key_sha256[32];
    __u32 max_k, pl_hz, reserved[2];
};
/* Per-phase timing of the caller's last successful RUN (ns); only the open file that ran it may read it. */
struct edgefhe_v8_stats {
    __u32 abi_version, command, tx_bytes, rx_bytes;
    __u64 calls;
    __u64 ns_copy_in, ns_check_in, ns_sync_device, ns_dma_hls, ns_tx_wait, ns_ap_poll;
    __u64 ns_identity, ns_sync_cpu, ns_check_out, ns_copy_out, ns_wipe;
    __u64 ns_rx_wake, ns_tx_callback, own_violations;
    __u32 ap_polls, k;
};
#define EDGEFHE_IOC_V8_CAPS _IOR('E',0x30,struct edgefhe_v8_caps)
#define EDGEFHE_IOC_V8_RUN _IOWR('E',0x31,struct edgefhe_v8_request)
#define EDGEFHE_IOC_V8_STATS _IOR('E',0x32,struct edgefhe_v8_stats)
#define EDGEFHE_V8_CAP_HW_IDENTITY_VERIFIED 1U
#define EDGEFHE_V8_CAP_FAIL_CLOSED 2U
/* Forbidden until whole-path reset/quiescence is board accepted. */
#define EDGEFHE_V8_CAP_RECOVERY_PROVEN 4U
#endif
