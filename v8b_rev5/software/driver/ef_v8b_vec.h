/* candidate-5b vector payload scans (ef_v8b_vec.c). Kernel callers must hold kernel_neon_begin(); the object is the
 * only one built without -mgeneral-regs-only. Both return nonzero exactly when the scalar opt1 scan would reject. */
#ifndef EF_V8B_VEC_H
#define EF_V8B_VEC_H
/* ENC messages: n little-endian int64 (2n words); nonzero iff some m is outside (-2^62, 2^62). */
u32 ef5_vec_messages_bad(const u32 *words, u32 n);
/* ENC/PAIR output payload [beat][limb]: beats multiple of 3, limb = beat % 3; nonzero iff some word >= q of its limb. */
u32 ef5_vec_payload_bad(const u32 *payload, u32 beats, u32 q0, u32 q1, u32 q2);
#endif
