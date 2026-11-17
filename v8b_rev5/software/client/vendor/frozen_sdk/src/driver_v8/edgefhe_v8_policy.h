/* SPDX-License-Identifier: GPL-2.0 */
/* V8b frame shapes, input/output checks and the key-lease policy. Shared by the kernel driver and host tests.
 * Device mutex required. Lease rules are those of the V7 policy (candidate-3) with ENC (9) using the lease like PAIR. */
#ifndef EDGEFHE_V8_POLICY_H
#define EDGEFHE_V8_POLICY_H
#ifdef __KERNEL__
#include <linux/types.h>
#include <linux/string.h>
#include <linux/errno.h>
typedef u8 ef_u8;
typedef u32 ef_u32;
typedef u64 ef_u64;
#else
#include <stdint.h>
#include <string.h>
#include <errno.h>
typedef uint8_t ef_u8;
typedef uint32_t ef_u32;
typedef uint64_t ef_u64;
#endif
#define EF_N 4096U
#define EF_FULL_COMPLETION 0xffU
#define EF_HEADER_BEATS 1U
#define EF_TRAILER_BEATS 1U
/* Bits: RX callback, RX result/cookie, TX callback, TX result/cookie, ap_done, ap_idle, stable identity/epoch,
 * framed canonical output. Every V8 command has a TX frame (at least the header), so all 8 bits are required. */
static const ef_u32 EF_Q[3] = {998244353U, 1004535809U, 469762049U};
/* Payload beats of the input and output frames (header, echo and trailer excluded). */
struct ef_shape { ef_u32 in_beats, out_beats; };
struct ef_policy {
 ef_u64 counter, generation, owner;
 ef_u8 profile[32], key[32];
 ef_u32 command;
 int valid, active, poisoned, quarantined;
};
static inline int ef_nonzero(const ef_u8 *p, ef_u32 n)
{
 ef_u32 i; ef_u8 v=0; for(i=0;i<n;i++)v|=p[i]; return v!=0;
}
static inline int ef_shape_for(ef_u32 cmd, ef_u32 k, struct ef_shape *s)
{
 switch(cmd) {
 case 6:if(k)return -EINVAL;s->in_beats=2*3*1024;s->out_beats=0;break;
 case 7:if(k<1||k>8)return -EINVAL;s->in_beats=64*k;s->out_beats=6144*k;break;
 case 8:if(k)return -EINVAL;s->in_beats=0;s->out_beats=0;break;
 case 9:if(k<1||k>8)return -EINVAL;s->in_beats=2+2048*k;s->out_beats=6144*k;break;
 default:return -EINVAL;
 }
 return 0;
}
/* Input payload checks, so that a frame the hardware would reject never reaches DMA. */
static inline int ef_check_input(ef_u32 cmd, ef_u32 k, const ef_u32 *p)
{
 ef_u32 i;
 if(cmd==6) {
  ef_u32 poly,limb;
  /* LOAD is [poly][limb][4096 words]; hoist the modulus outside the scan. */
  for(poly=0;poly<2;poly++)for(limb=0;limb<3;limb++) {
   const ef_u32 q=EF_Q[limb];
   const ef_u32 *base=p+(poly*3+limb)*4096U;
   for(i=0;i<4096U;i++)if(base[i]>=q)return -EINVAL;
  }
 } else if(cmd==7) {
  for(i=0;i<64*k*4;i++) {
   ef_u32 w=p[i];
   /* code 3 (both bits set) is invalid in every 2-bit field */
   if(w&(w>>1)&0x55555555U)return -EINVAL;
  }
 } else if(cmd==9) {
  ef_u32 seed=0;
  for(i=0;i<8;i++)seed|=p[i];
  if(!seed)return -EINVAL;
  for(i=0;i<2048*k*2;i++) {
   /* little-endian int64 from two words: -2^62 < m < 2^62 <=> bits 63 and 62 are equal and m != -2^62 */
   ef_u32 hi=p[8+2*i+1]>>30;
   if(hi!=0&&hi!=3)return -EINVAL;
   if(p[8+2*i+1]==0xc0000000U&&p[8+2*i]==0)return -EINVAL; /* m = -2^62 */
  }
 }
 return 0;
}
/* Output frame: echo of the sent header, payload with canonical residues (limb = beat % 3, fourth word < q too),
 * trailer {MAGIC, status, k, generation low}. Returns the trailer status, or -EBADMSG for a malformed frame. */
static inline int ef_check_output(const ef_u32 *rx, const ef_u32 *hdr, ef_u32 out_beats, ef_u32 k)
{
 const ef_u32 *t=rx+4*(EF_HEADER_BEATS+out_beats);
 ef_u32 i,c;
 for(c=0;c<4;c++)if(rx[c]!=hdr[c])return -EBADMSG;
 if(t[0]!=0x45463841U||t[2]!=k||t[3]!=hdr[2]||t[1]>255)return -EBADMSG;
 if(t[1])return (int)t[1];
 /* Ciphertext payload is [beat][limb]. Three fixed-q scans avoid per-beat modulo.
  * The tail preserves the helper's legacy behaviour even for non-frame test lengths. */
 for(i=0;out_beats-i>=3U;i+=3U) {
  const ef_u32 *base=rx+4U*(EF_HEADER_BEATS+i);
  for(c=0;c<4;c++)if(base[c]>=EF_Q[0])return -EBADMSG;
  for(c=0;c<4;c++)if(base[4+c]>=EF_Q[1])return -EBADMSG;
  for(c=0;c<4;c++)if(base[8+c]>=EF_Q[2])return -EBADMSG;
 }
 {
  ef_u32 limb=0;
  for(;i<out_beats;i++,limb++)
   for(c=0;c<4;c++)if(rx[4U*(EF_HEADER_BEATS+i)+c]>=EF_Q[limb])return -EBADMSG;
 }
 return 0;
}
/* MMCM fabric clock check against PL0 (supervisor 0x28 cycles per 65536 PL0 cycles, 0x2c windows, 0x30 locked):
 * 2.000x within +/- 2 cycles, at least two completed windows, MMCM locked. Nonzero = reject. */
#define EF_FREQ_WINDOW 131072U
static inline int ef_fabric_bad(ef_u32 cycles, ef_u32 windows, ef_u32 locked)
{
 return cycles<EF_FREQ_WINDOW-2||cycles>EF_FREQ_WINDOW+2||windows<2||locked!=1;
}
static inline void ef_invalidate(struct ef_policy *p)
{
 p->valid=0;p->generation=0;p->owner=0;
 memset(p->profile,0,32);memset(p->key,0,32);
}
static inline int ef_available(const struct ef_policy *p)
{
 if(p->poisoned||p->quarantined)return -EIO;
 return p->active?-EBUSY:0;
}
static inline int ef_valid_for(const struct ef_policy *p,ef_u64 owner)
{
 return p->valid&&!p->active&&!p->poisoned&&!p->quarantined&&p->owner==owner;
}
static inline int ef_begin(struct ef_policy *p,ef_u32 cmd,ef_u64 owner,
 ef_u64 generation,const ef_u8 profile[32],const ef_u8 key[32])
{
 int ret=ef_available(p);if(ret)return ret;
 if(cmd<6||cmd>9||!owner)return -EINVAL;
 if(cmd==6) {
  if(generation||!ef_nonzero(key,32))return -EINVAL;
  ef_invalidate(p);
  if(p->counter==~(ef_u64)0){p->poisoned=1;return -EOVERFLOW;}
  p->generation=++p->counter;p->owner=owner;
  memcpy(p->profile,profile,32);memcpy(p->key,key,32);
 } else if(cmd==7||cmd==9) {
  if(!ef_valid_for(p,owner)||!generation||generation!=p->generation||
     memcmp(profile,p->profile,32)||memcmp(key,p->key,32))return -ESTALE;
 } else {
  /* INVALIDATE: a different open file cannot invalidate another caller's live lease. */
  if(p->valid&&(!ef_valid_for(p,owner)||generation!=p->generation||
     memcmp(profile,p->profile,32)||memcmp(key,p->key,32)))return -ESTALE;
  if(!p->valid&&(generation||ef_nonzero(key,32)))return -ESTALE;
  ef_invalidate(p);
 }
 p->command=cmd;p->active=1;return 0;
}
static inline void ef_fatal(struct ef_policy *p)
{
 p->active=0;p->poisoned=1;p->quarantined=1;ef_invalidate(p);
}
/* The hardware clears its key on any non-zero status and on INVALIDATE; the lease follows it. */
static inline int ef_finish(struct ef_policy *p,ef_u32 mask,ef_u32 hardware_status)
{
 if(!p->active)return -EINVAL;
 if((mask&EF_FULL_COMPLETION)!=EF_FULL_COMPLETION){ef_fatal(p);return -EIO;}
 p->active=0;
 if(hardware_status){ef_invalidate(p);return -EPROTO;}
 if(p->command==6)p->valid=1;
 return 0;
}
static inline void ef_close(struct ef_policy *p,ef_u64 owner)
{
 if(p->owner==owner)ef_invalidate(p);
}
#endif
