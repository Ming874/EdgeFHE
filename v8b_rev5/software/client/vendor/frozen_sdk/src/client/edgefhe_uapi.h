/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef EDGEFHE_UNIFIED_V7_UAPI_H
#define EDGEFHE_UNIFIED_V7_UAPI_H
#include <linux/types.h>
#include <linux/ioctl.h>
#define EDGEFHE_V7_ABI 7U
#define EDGEFHE_V7_OPMASK 0x1ffU
#define EDGEFHE_V7_MAX_TX_BYTES 262144U
#define EDGEFHE_V7_MAX_RX_BYTES 524288U
/* Legacy byte layout and command are retained, including the reserved fields. */
struct edgefhe_legacy_request {
    __u32 operation, reserved0, input_words, output_words;
    __u64 input_ptr, output_ptr;
    __u32 timeout_ms, elapsed_us;
    __s32 status;
    __u32 reserved;
};
#define EDGEFHE_IOC_LEGACY _IOWR('E',0x04,struct edgefhe_legacy_request)
/* Counts denote 16-byte AXIS beats, not scalar coefficients. */
struct edgefhe_v7_request {
    __u32 abi_version, command, input_words, output_words;
    __u64 input_ptr, output_ptr;
    __u32 timeout_ms, flags;
    __u64 generation;
    __u8 profile_sha256[32], public_key_sha256[32];
    __u32 elapsed_us, hardware_status;
    __s32 status;
    __u32 reserved;
};
struct edgefhe_v7_caps {
    __u32 abi_version, flags, operation_mask, n;
    __u32 limbs, lanes_per_limb, max_tx_bytes, max_rx_bytes;
    __u64 generation;
    __u32 cache_valid_for_caller, poisoned;
    __u8 profile_sha256[32], public_key_sha256[32];
    __u32 reserved[4];
};
#define EDGEFHE_IOC_V7_CAPS _IOR('E',0x20,struct edgefhe_v7_caps)
#define EDGEFHE_IOC_V7_RUN _IOWR('E',0x21,struct edgefhe_v7_request)
#define EDGEFHE_V7_CAP_HW_IDENTITY_VERIFIED 1U
#define EDGEFHE_V7_CAP_FAIL_CLOSED 2U
/* This flag is forbidden until whole-path reset/quiescence is board accepted. */
#define EDGEFHE_V7_CAP_RECOVERY_PROVEN 4U
#endif
