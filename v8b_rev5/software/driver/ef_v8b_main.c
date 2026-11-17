// SPDX-License-Identifier: GPL-2.0
/* candidate-5b-safe (2026-10-22), derived from the accepted candidate-4-opt1 source (board SDK identity 7c5a1b49...).
 * Two changes, both selectable at run time by the root-only module parameter `fastpath` (bit 0, bit 1; default 3) so
 * that the old and new paths can be A/B-measured in one boot; neither weakens a check, ownership rule or wipe:
 *  bit 0 - payload scans of the ENC input (|m| < 2^62) and of the ENC/PAIR output (canonical residues) run as 128-bit
 *          vector loops (ef_v8b_vec.c) inside kernel_neon_begin/end; header, seed, trailer, status and LOAD/PAIR input
 *          checks are the unchanged scalar code, and the verdict equals the scalar verdict for every input;
 *  bit 1 - the RX pre-DMA memset is skipped only when the buffer is proven all-zero: set at probe after the full wipe
 *          and after every successful RX wipe, cleared as soon as the RX buffer is handed to the device. RX is still
 *          wiped after every call exactly as before, so the device always receives an all-zero RX buffer.
 * Unchanged: ABI 8 (request/caps/stats layout), frames, leases, DMA ownership/sync, quarantine and wipe policy.
 *
 * Research candidate-4 (V8b, ABI 8). No automatic reset/retry or proven recovery capability.
 * Derived from candidate-3 (v7_dma_ownership_20261014/driver_candidate3): the same cacheable non-coherent buffers with
 * explicit CPU/DEVICE/QUARANTINED DMA ownership, owner-bound STATS, in-flight failures quarantined until a supervised
 * fresh boot. Changes for the V8b image (edgefhe_v8b HLS IP, supervisor identity EFG8 revision 0x00080003; the PS PL0
 * clock stays at the boot default 100 MHz and an MMCM in the PL makes the 200 MHz fabric clock):
 *  - clock: the DT clock (PL0) must be 99.9..100.0 MHz and is held with clk_rate_exclusive_get; the supervisor
 *    measures the fabric clock against PL0 in hardware (0x28 = fabric cycles per 65536 PL0 cycles, 131072 +/- 2 for
 *    2.000x), reports completed windows (0x2c >= 2) and the synchronised MMCM locked bit (0x30 = 1);
 *  - frames instead of command registers: the driver writes the header {MAGIC, command | k << 8, lease generation}
 *    in front of the caller's payload, so the generation the hardware sees always comes from the lease policy;
 *  - every payload is checked before DMA (LOAD residues, PAIR trit codes, ENC seed and |m| < 2^62);
 *  - the output must echo the header, end with a trailer {MAGIC, status, k, generation low} and, for status 0,
 *    carry canonical residues; only then is the payload copied to the caller. A non-zero status is a hardware
 *    rejection (-EPROTO, lease cleared like the hardware key); a malformed frame is a fault (quarantine);
 *  - an RX transfer shorter than the full frame (the hardware ends header/generation rejections after two beats)
 *    is a fault: the driver never sends such a frame, so it means lost hardware state.
 */
#include <linux/clk.h>
#include <linux/completion.h>
#include <linux/delay.h>
#include <linux/dma-mapping.h>
#include <linux/dmaengine.h>
#include <linux/fs.h>
#include <linux/io.h>
#include <linux/jiffies.h>
#include <linux/kref.h>
#include <linux/math64.h>
#include <linux/ktime.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/sched/signal.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/moduleparam.h>
#include <asm/neon.h>
#include <asm/simd.h>
#include "edgefhe_v8_uapi.h"
#include "edgefhe_v8_policy.h"
#include "profile_identity_v8.h"
#include "ef_v8b_vec.h"
static unsigned int fastpath = 3;
module_param(fastpath, uint, 0644);
MODULE_PARM_DESC(fastpath, "bit0: vector payload checks; bit1: RX pre-DMA memset skipped when proven zero (A/B only)");

#define EF_TIMEOUT_MAX 30000U
#define EF_AP 0x00
#define EF_DONE 2U
#define EF_IDLE 4U
#define EF_AUTORESTART 128U
#define EF_EXPECTED_REVISION 0x00080003U
#define EF_TX_BYTES EDGEFHE_V8_MAX_TX_BYTES
#define EF_RX_BYTES EDGEFHE_V8_MAX_RX_BYTES
struct ef_done {
 struct completion completion;
 enum dmaengine_tx_result result;
 u32 residue;
 bool result_present;
 u64 t_ns; /* callback time, 0 = no callback */
};
struct ef_proxy {
 struct device *dev, *dma_dev;
 struct clk *clk;
 struct kref refs;
 struct mutex lock;
 struct miscdevice misc;
 struct dma_chan *tx, *rx;
 void __iomem *ntt, *supervisor;
 void *tx_buf, *rx_buf;
 dma_addr_t tx_addr, rx_addr;
 struct ef_done tx_done, rx_done;
 struct ef_policy policy;
 u64 owner_counter;
 u32 epoch;
 u32 dirty_tx,dirty_rx; /* bytes written since the last wipe */
 u32 own_tx,own_rx,devlen_tx,devlen_rx; /* EF_OWN_* and the synced length while device-owned */
 u64 own_violations,stats_owner;
 struct edgefhe_v8_stats stats;
 bool online, retained, pending;
 bool rx_zero; /* candidate-5b: RX buffer proven all-zero in DRAM and CPU-owned since the last wipe */
};
struct ef_file { struct ef_proxy *proxy; u64 owner; };

static void ef_free(struct kref *ref)
{
 struct ef_proxy *p=container_of(ref,struct ef_proxy,refs);
 kfree(p);
}
/* Explicit DMA ownership of the two cacheable streaming buffers (Linux DMA API):
 * EF_OWN_CPU: only the CPU may access; EF_OWN_DEVICE: only the device, between ef_give and ef_take;
 * EF_OWN_QUARANTINED: completion uncertain, never synced, wiped, reused or freed again. */
#define EF_OWN_CPU 0U
#define EF_OWN_DEVICE 1U
#define EF_OWN_QUARANTINED 2U
static int ef_give(struct ef_proxy *p,bool rx,u32 len)
{
 u32 *own=rx?&p->own_rx:&p->own_tx;
 if(*own!=EF_OWN_CPU||!len||len>(rx?EF_RX_BYTES:EF_TX_BYTES)){p->own_violations++;return -EIO;}
 dma_sync_single_for_device(p->dma_dev,rx?p->rx_addr:p->tx_addr,len,DMA_BIDIRECTIONAL);
 *own=EF_OWN_DEVICE;
 if(rx)p->devlen_rx=len;else p->devlen_tx=len;
 return 0;
}
static int ef_take(struct ef_proxy *p,bool rx)
{
 u32 *own=rx?&p->own_rx:&p->own_tx,*len=rx?&p->devlen_rx:&p->devlen_tx;
 if(*own!=EF_OWN_DEVICE||!*len){p->own_violations++;return -EIO;}
 dma_sync_single_for_cpu(p->dma_dev,rx?p->rx_addr:p->tx_addr,*len,DMA_BIDIRECTIONAL);
 *own=EF_OWN_CPU;*len=0;
 return 0;
}
/* Zero only what was used while CPU-owned, push the zeros to DRAM (device sync) and take the lines
 * back (CPU sync): both buffers are CPU-owned whenever no transaction is in flight. */
static int ef_wipe(struct ef_proxy *p)
{
 if(p->own_tx!=EF_OWN_CPU||p->own_rx!=EF_OWN_CPU){p->own_violations++;return -EIO;}
 if(p->dirty_tx){memzero_explicit(p->tx_buf,p->dirty_tx);
  if(ef_give(p,false,p->dirty_tx)||ef_take(p,false))return -EIO;
  p->dirty_tx=0;}
 if(p->dirty_rx){memzero_explicit(p->rx_buf,p->dirty_rx);
  if(ef_give(p,true,p->dirty_rx)||ef_take(p,true))return -EIO;
  p->dirty_rx=0;p->rx_zero=true;}
 return 0;
}
static void ef_callback(void *arg,const struct dmaengine_result *r)
{
 struct ef_done *d=arg;
 d->result_present=r!=NULL;
 d->result=r?r->result:DMA_TRANS_ABORTED;
 d->residue=r?r->residue:~0U;
 d->t_ns=ktime_get_ns();
 complete(&d->completion);
 /* No MMIO, allocation, termination or access to the platform devm resources. */
}
static void ef_reset_done(struct ef_done *d)
{
 reinit_completion(&d->completion);
 d->result_present=false;d->result=DMA_TRANS_ABORTED;d->residue=~0U;d->t_ns=0;
}
static int ef_identity(struct ef_proxy *p,bool first)
{
 static const u32 expected[]={0x45464738,EF_EXPECTED_REVISION,0x3c0,4096,
                             0x00040003,EF_TX_BYTES,EF_RX_BYTES,200000000};
 u32 i,epoch,status;
 unsigned long hz=clk_get_rate(p->clk);
 if(hz<99900000||hz>100000000)return -ERANGE;
 for(i=0;i<ARRAY_SIZE(expected);i++)
  if(ioread32(p->supervisor+4*i)!=expected[i])return -ENODEV;
 for(i=0;i<8;i++) {
  u32 word=(u32)ef_profile[4*i]|((u32)ef_profile[4*i+1]<<8)|
   ((u32)ef_profile[4*i+2]<<16)|((u32)ef_profile[4*i+3]<<24);
  if(ioread32(p->supervisor+0x80+4*i)!=word)return -ENODEV;
 }
 if(ef_fabric_bad(ioread32(p->supervisor+0x28),ioread32(p->supervisor+0x2c),ioread32(p->supervisor+0x30)))return -ERANGE;
 status=ioread32(p->supervisor+0x24);
 epoch=ioread32(p->supervisor+0x20);
 if(status!=1||!epoch)return -EIO;
 if(first)p->epoch=epoch;
 else if(epoch!=p->epoch)return -ESTALE;
 /* Re-read status/epoch to reject a reset transition during identity capture. */
 if(ioread32(p->supervisor+0x24)!=1||
    ioread32(p->supervisor+0x20)!=p->epoch)return -ESTALE;
 return 0;
}
/* A DMA terminate return value does not certify whole AXI/HLS quiescence.
 * Retain callbacks, buffers, provider channels, clock and module after any
 * in-flight failure. Normal file close or platform unbind cannot free them.
 */
static void ef_retain(struct ef_proxy *p,const char *reason)
{
 ef_fatal(&p->policy);
 p->own_tx=p->own_rx=EF_OWN_QUARANTINED;
 if(p->retained)return;
 p->retained=true;
 kref_get(&p->refs);get_device(p->dev);__module_get(THIS_MODULE);
 dev_crit(p->dev,"V8b quarantine (%s): no retry/reset/unload/overlay reload; "
          "buffers and callbacks retained; supervised fresh boot required\n",reason);
}
static void ef_terminal(struct ef_proxy *p,const char *reason)
{
 int rx,tx;
 ef_retain(p,reason);
 rx=dmaengine_terminate_sync(p->rx);
 tx=dmaengine_terminate_sync(p->tx);
 dev_err(p->dev,"DMA terminate RX=%d TX=%d; whole-path recovery remains unproven\n",rx,tx);
}
static int ef_wait(struct ef_done *d,unsigned long deadline)
{
 unsigned long now;
 long ret;
 if(completion_done(&d->completion))return 0;
 now=jiffies;
 if(time_after_eq(now,deadline))return -ETIMEDOUT;
 ret=wait_for_completion_interruptible_timeout(&d->completion,deadline-now);
 if(ret<0)return (int)ret;
 return ret?0:-ETIMEDOUT;
}
static bool ef_dma_ok(struct dma_chan *chan,dma_cookie_t cookie,struct ef_done *d)
{
 return d->result_present&&d->result==DMA_TRANS_NOERROR&&!d->residue&&
        dma_async_is_tx_complete(chan,cookie,NULL,NULL)==DMA_COMPLETE;
}
static int ef_header(const struct edgefhe_v8_request *r,struct ef_shape *s)
{
 int ret=ef_shape_for(r->command,r->k,s);
 if(ret)return ret;
 if(r->abi_version!=EDGEFHE_V8_ABI||r->flags||r->timeout_ms>EF_TIMEOUT_MAX||
    r->input_bytes!=s->in_beats*16||r->output_bytes!=s->out_beats*16)return -EINVAL;
 if((s->in_beats&&!r->input_ptr)||(s->out_beats&&!r->output_ptr))return -EFAULT;
 if((!s->in_beats&&r->input_ptr)||(!s->out_beats&&r->output_ptr))return -EINVAL;
 if(memcmp(r->profile_sha256,ef_profile,32))return -EINVAL;
 return 0;
}
/* candidate-5b: same verdicts as ef_check_input/ef_check_output (opt1); only the long payload scans are vectorized,
 * between kernel_neon_begin/end (process context; scalar opt1 code when SIMD is not usable or bit 0 is clear). */
static int ef5_check_input(u32 cmd,u32 k,const u32 *p,u32 vec)
{
 u32 i,seed=0,bad;
 if(cmd!=9||!vec||!may_use_simd())return ef_check_input(cmd,k,p);
 if(k<1||k>8)return ef_check_input(cmd,k,p);
 for(i=0;i<8;i++)seed|=p[i];
 if(!seed)return -EINVAL;
 kernel_neon_begin();bad=ef5_vec_messages_bad(p+8,4096U*k);kernel_neon_end(); /* 2048k beats = 4096k int64 */
 return bad?-EINVAL:0;
}
static int ef5_check_output(const u32 *rx,const u32 *hdr,u32 out_beats,u32 k,u32 vec)
{
 const u32 *t=rx+4*(EF_HEADER_BEATS+out_beats);
 u32 c,bad;
 if(!vec||!out_beats||out_beats%3U||!may_use_simd())return ef_check_output(rx,hdr,out_beats,k);
 for(c=0;c<4;c++)if(rx[c]!=hdr[c])return -EBADMSG;
 if(t[0]!=0x45463841U||t[2]!=k||t[3]!=hdr[2]||t[1]>255)return -EBADMSG;
 if(t[1])return (int)t[1];
 kernel_neon_begin();bad=ef5_vec_payload_bad(rx+4U*EF_HEADER_BEATS,out_beats,EF_Q[0],EF_Q[1],EF_Q[2]);kernel_neon_end();
 return bad?-EBADMSG:0;
}
static int ef_transfer(struct ef_file *f,struct edgefhe_v8_request *r)
{
 struct ef_proxy *p=f->proxy;
 struct ef_shape s;
 struct dma_async_tx_descriptor *desc;
 dma_cookie_t rx_cookie=0,tx_cookie=0;
 unsigned long deadline;
 ktime_t start;
 u64 t0=ktime_get_ns(),t_copy=0,t_in=0,t_sync=0,t_rx=0,t_tx=0,t_ap=0,t_id=0,t_cpu=0,t_chk=0;
 u32 ctrl,mask=0,tx_bytes,rx_bytes,polls=0,hdr[4];
 int ret,frame;
 bool ap_done=false,issued=false,submitted=false;
 const u32 fast=READ_ONCE(fastpath); /* one value per call (A/B by the root-only parameter between calls) */
 r->elapsed_us=0;r->hardware_status=~0U;r->status=0;
 if(!p->online)return -ENODEV;
 ret=ef_available(&p->policy);if(ret)return ret;
 ret=ef_header(r,&s);if(ret)return ret;
 if(p->own_tx!=EF_OWN_CPU||p->own_rx!=EF_OWN_CPU) {
  p->own_violations++;ef_retain(p,"DMA buffer not CPU-owned at entry");return -EIO;
 }
 /* Check idle/identity BEFORE touching the buffers. Unexpected activity
  * is not safe to wipe, overwrite or return to another caller. */
 ret=ef_identity(p,false);
 ctrl=ioread32(p->ntt+EF_AP);
 if(ret||!(ctrl&EF_IDLE)||(ctrl&EF_AUTORESTART)) {
  ef_terminal(p,"unexpected identity/reset/HLS activity");return ret?ret:-EBUSY;
 }
 tx_bytes=(EF_HEADER_BEATS+s.in_beats)*16;
 rx_bytes=(EF_HEADER_BEATS+s.out_beats+EF_TRAILER_BEATS)*16;
 p->dirty_tx=max(p->dirty_tx,tx_bytes);
 if(s.in_beats&&copy_from_user((u8 *)p->tx_buf+16,u64_to_user_ptr(r->input_ptr),s.in_beats*16)) {
  ret=-EFAULT;goto wipe;
 }
 t_copy=ktime_get_ns();
 ret=ef5_check_input(r->command,r->k,(const u32 *)((u8 *)p->tx_buf+16),fast&1U);if(ret)goto wipe;
 t_in=ktime_get_ns();
 ret=ef_begin(&p->policy,r->command,f->owner,r->generation,
              r->profile_sha256,r->public_key_sha256);
 if(ret)goto wipe;
 if(r->command==6)r->generation=p->policy.generation;
 /* The header carries the lease generation (INVALIDATE: none is checked by the hardware). */
 hdr[0]=EDGEFHE_V8_MAGIC;hdr[1]=r->command|(r->k<<8);
 hdr[2]=(u32)r->generation;hdr[3]=(u32)(r->generation>>32);
 memcpy(p->tx_buf,hdr,16);
 /* candidate-5b bit1: skip only when proven zero (probe/after an RX wipe, never handed to the device since). */
 if(!((fast&2U)&&p->rx_zero&&!p->dirty_rx))memset(p->rx_buf,0,rx_bytes);
 p->rx_zero=false;p->dirty_rx=max(p->dirty_rx,rx_bytes);
 /* Hand both buffers to the device; no CPU access until ef_take after proven completion. */
 if(ef_give(p,false,tx_bytes)||ef_give(p,true,rx_bytes)){ret=-EIO;goto before_issue;}
 t_sync=ktime_get_ns();
 ef_reset_done(&p->rx_done);ef_reset_done(&p->tx_done);
 desc=dmaengine_prep_slave_single(p->rx,p->rx_addr,rx_bytes,DMA_DEV_TO_MEM,
                                DMA_PREP_INTERRUPT|DMA_CTRL_ACK);
 if(!desc){ret=-EIO;goto before_issue;}
 desc->callback_result=ef_callback;desc->callback_param=&p->rx_done;
 rx_cookie=dmaengine_submit(desc);submitted=true;
 ret=dma_submit_error(rx_cookie);if(ret)goto before_issue;
 desc=dmaengine_prep_slave_single(p->tx,p->tx_addr,tx_bytes,DMA_MEM_TO_DEV,
                                 DMA_PREP_INTERRUPT|DMA_CTRL_ACK);
 if(!desc){ret=-EIO;goto before_issue;}
 desc->callback_result=ef_callback;desc->callback_param=&p->tx_done;
 tx_cookie=dmaengine_submit(desc);
 ret=dma_submit_error(tx_cookie);if(ret)goto before_issue;
 deadline=jiffies+msecs_to_jiffies(r->timeout_ms?r->timeout_ms:5000);
 dma_wmb();start=ktime_get();p->pending=true;issued=true;
 dma_async_issue_pending(p->rx);
 dma_async_issue_pending(p->tx);
 iowrite32(1,p->ntt+EF_AP); /* AUTORESTART remains disabled. */
 ret=ef_wait(&p->rx_done,deadline);if(ret)goto fault;
 t_rx=ktime_get_ns();
 mask|=1;
 if(ef_dma_ok(p->rx,rx_cookie,&p->rx_done))mask|=2;else {ret=-EIO;goto fault;}
 ret=ef_wait(&p->tx_done,deadline);if(ret)goto fault;
 mask|=4;
 if(ef_dma_ok(p->tx,tx_cookie,&p->tx_done))mask|=8;else {ret=-EIO;goto fault;}
 t_tx=ktime_get_ns();
 /* ap_done is clear-on-read. Latch it across reads and require ap_idle too.
  * A DMA callback alone must never publish the lease. */
 for(;;) {
  ctrl=ioread32(p->ntt+EF_AP);polls++;
  ap_done|=!!(ctrl&EF_DONE);
  if(ctrl&EF_AUTORESTART){ret=-EIO;goto fault;}
  if(ap_done&&(ctrl&EF_IDLE))break;
  if(signal_pending(current)){ret=-ERESTARTSYS;goto fault;}
  if(time_after_eq(jiffies,deadline)){ret=-ETIMEDOUT;goto fault;}
  usleep_range(10,30);
 }
 mask|=16|32;
 t_ap=ktime_get_ns();
 ret=ef_identity(p,false);if(ret)goto fault;
 mask|=64;
 t_id=ktime_get_ns();
 dma_rmb();
 /* Callbacks, AP done+idle and a stable identity prove the device finished with both buffers. */
 if(ef_take(p,false)||ef_take(p,true)){ret=-EIO;goto fault;}
 t_cpu=ktime_get_ns();
 frame=ef5_check_output((const u32 *)p->rx_buf,hdr,s.out_beats,r->k,fast&1U);
 if(frame<0){ret=-EIO;goto fault;}
 r->hardware_status=(u32)frame;
 mask|=128;
 /* All transactions stopped normally, including a hardware-status rejection. */
 p->pending=false;
 r->elapsed_us=(u32)ktime_to_us(ktime_sub(ktime_get(),start));
 t_chk=ktime_get_ns();
 ret=ef_finish(&p->policy,mask,r->hardware_status);
 if(ret)goto wipe;
 if(s.out_beats&&copy_to_user(u64_to_user_ptr(r->output_ptr),(u8 *)p->rx_buf+16,s.out_beats*16)) {
  ef_invalidate(&p->policy);ret=-EFAULT;
 }
 {
  u64 t_out=ktime_get_ns();
  struct edgefhe_v8_stats *st=&p->stats;
  st->abi_version=EDGEFHE_V8_ABI;st->command=r->command;st->k=r->k;st->tx_bytes=tx_bytes;st->rx_bytes=rx_bytes;
  st->ap_polls=polls;st->own_violations=p->own_violations;p->stats_owner=f->owner;
  st->ns_copy_in=t_copy-t0;st->ns_check_in=t_in-t_copy;
  st->ns_rx_wake=p->rx_done.t_ns&&t_rx>p->rx_done.t_ns?t_rx-p->rx_done.t_ns:0;
  st->ns_tx_callback=p->tx_done.t_ns?p->tx_done.t_ns-ktime_to_ns(start):0;
  st->ns_sync_device=t_sync-t_in;st->ns_dma_hls=t_rx-ktime_to_ns(start);
  st->ns_tx_wait=t_tx-t_rx;st->ns_ap_poll=t_ap-t_tx;st->ns_identity=t_id-t_ap;st->ns_sync_cpu=t_cpu-t_id;
  st->ns_check_out=t_chk-t_cpu;st->ns_copy_out=t_out-t_chk;
  if(!p->retained&&ef_wipe(p))ef_retain(p,"DMA buffer ownership violation in wipe");
  st->ns_wipe=ktime_get_ns()-t_out;if(!ret)st->calls++;
  return ret;
 }
before_issue:
 /* No issue_pending or ap_start yet. Terminate queued descriptors before reuse. */
 p->policy.active=0;ef_invalidate(&p->policy);
 if(submitted) {
  int rx=dmaengine_terminate_sync(p->rx);
  int tx=dmaengine_terminate_sync(p->tx);
  if(rx||tx)ef_retain(p,"unissued descriptor termination failed");
 }
 /* Never issued: the device did not access the buffers, so take them back before the wipe. */
 if(!p->retained&&((p->own_tx==EF_OWN_DEVICE&&ef_take(p,false))||
                   (p->own_rx==EF_OWN_DEVICE&&ef_take(p,true))))
  ef_retain(p,"DMA buffer ownership violation before issue");
 goto wipe;
fault:
 if(issued)ef_terminal(p,"in-flight completion/cancel/output failure");
wipe:
 if(!p->retained&&ef_wipe(p))ef_retain(p,"DMA buffer ownership violation in wipe");
 return ret;
}
static void ef_caps(struct ef_file *f,struct edgefhe_v8_caps *c)
{
 struct ef_proxy *p=f->proxy;
 memset(c,0,sizeof(*c));
 c->abi_version=EDGEFHE_V8_ABI;c->flags=EDGEFHE_V8_CAP_HW_IDENTITY_VERIFIED|EDGEFHE_V8_CAP_FAIL_CLOSED;
 c->command_mask=0x3c0;c->n=4096;c->limbs=3;c->lanes_per_limb=4;
 c->max_tx_bytes=EF_TX_BYTES;c->max_rx_bytes=EF_RX_BYTES;c->max_k=EDGEFHE_V8_MAX_K;
 c->pl_hz=(u32)div_u64((u64)clk_get_rate(p->clk)*ioread32(p->supervisor+0x28),65536); /* measured fabric clock */
 c->poisoned=p->policy.poisoned||p->retained;
 memcpy(c->profile_sha256,ef_profile,32);
 c->cache_valid_for_caller=ef_valid_for(&p->policy,f->owner);
 if(c->cache_valid_for_caller) {
  c->generation=p->policy.generation;
  memcpy(c->public_key_sha256,p->policy.key,32);
 }
 /* No RECOVERY_PROVEN flag. Key digest is a caller binding, not authentication. */
}
static long ef_ioctl(struct file *file,unsigned int cmd,unsigned long arg)
{
 struct ef_file *f=file->private_data;
 struct ef_proxy *p=f->proxy;
 struct edgefhe_v8_request r;
 struct edgefhe_v8_caps c;
 void __user *user=(void __user *)arg;
 int ret;
 if(cmd!=EDGEFHE_IOC_V8_RUN&&cmd!=EDGEFHE_IOC_V8_CAPS&&cmd!=EDGEFHE_IOC_V8_STATS)return -ENOTTY;
 memset(&r,0,sizeof(r));
 if(cmd==EDGEFHE_IOC_V8_RUN&&copy_from_user(&r,user,sizeof(r)))return -EFAULT;
 ret=mutex_lock_interruptible(&p->lock);if(ret)return ret;
 if(!p->online){ret=-ENODEV;goto unlock;}
 if(cmd==EDGEFHE_IOC_V8_STATS) { /* read-only timing of the caller's last successful call; no MMIO */
  struct edgefhe_v8_stats st=p->stats;
  if(!st.calls||p->stats_owner!=f->owner){ret=-ENODATA;goto unlock;}
  ret=copy_to_user(user,&st,sizeof(st))?-EFAULT:0;goto unlock;
 }
 if(cmd==EDGEFHE_IOC_V8_CAPS) {
  if(!p->retained&&ef_identity(p,false))ef_terminal(p,"capability epoch mismatch");
  ef_caps(f,&c);ret=copy_to_user(user,&c,sizeof(c))?-EFAULT:0;
  goto unlock;
 }
 ret=ef_transfer(f,&r);r.status=ret;
 if(copy_to_user(user,&r,sizeof(r))) {
  ef_close(&p->policy,f->owner);if(!ret)ret=-EFAULT;
 }
unlock:
 mutex_unlock(&p->lock);return ret;
}
static int ef_open(struct inode *inode,struct file *file)
{
 struct miscdevice *misc=file->private_data;
 struct ef_proxy *p=container_of(misc,struct ef_proxy,misc);
 struct ef_file *f=kzalloc(sizeof(*f),GFP_KERNEL);
 int ret=0;
 if(!f)return -ENOMEM;
 mutex_lock(&p->lock);
 if(!p->online)ret=-ENODEV;
 else if(p->policy.poisoned)ret=-EIO;
 else if(p->owner_counter==~0ULL)ret=-EOVERFLOW;
 else {f->proxy=p;f->owner=++p->owner_counter;kref_get(&p->refs);file->private_data=f;}
 mutex_unlock(&p->lock);
 if(ret)kfree(f);
 return ret;
}
static int ef_release(struct inode *inode,struct file *file)
{
 struct ef_file *f=file->private_data;
 struct ef_proxy *p=f->proxy;
 mutex_lock(&p->lock);ef_close(&p->policy,f->owner);mutex_unlock(&p->lock);
 kref_put(&p->refs,ef_free);kfree(f);return 0;
}
static const struct file_operations ef_fops={
 .owner=THIS_MODULE,.open=ef_open,.release=ef_release,
 .unlocked_ioctl=ef_ioctl,.compat_ioctl=ef_ioctl,.llseek=no_llseek,
};
static void ef_release_resources(struct ef_proxy *p)
{
 if(p->rx_buf)dma_free_noncoherent(p->dma_dev,EF_RX_BYTES,p->rx_buf,p->rx_addr,DMA_BIDIRECTIONAL);
 if(p->tx_buf)dma_free_noncoherent(p->dma_dev,EF_TX_BYTES,p->tx_buf,p->tx_addr,DMA_BIDIRECTIONAL);
 if(p->dma_dev)put_device(p->dma_dev);
 if(p->rx)dma_release_channel(p->rx);
 if(p->tx)dma_release_channel(p->tx);
}
static int ef_probe(struct platform_device *pdev)
{
 struct ef_proxy *p;
 struct resource *res;
 u32 i;
 int ret;
 BUILD_BUG_ON(sizeof(struct edgefhe_v8_request)!=128);
 BUILD_BUG_ON(sizeof(struct edgefhe_v8_caps)!=128);
 BUILD_BUG_ON(offsetof(struct edgefhe_v8_request,generation)!=40);
 BUILD_BUG_ON(sizeof(struct edgefhe_v8_stats)!=144);
 BUILD_BUG_ON(EF_TX_BYTES!=(1+2+2048*8)*16||EF_RX_BYTES!=(2+6144*8)*16);
 p=kzalloc(sizeof(*p),GFP_KERNEL);if(!p)return -ENOMEM;
 p->dev=&pdev->dev;kref_init(&p->refs);mutex_init(&p->lock);
 init_completion(&p->tx_done.completion);init_completion(&p->rx_done.completion);
 if(of_find_property(pdev->dev.of_node,"dma-control",NULL)){ret=-EINVAL;goto free;}
 for(i=0;i<2;i++) {
  void __iomem *regs;
  res=platform_get_resource(pdev,IORESOURCE_MEM,i);
  if(!res||res->start!=(i?0xa0030000:0xa0010000)||resource_size(res)!=0x10000) {
   ret=-EINVAL;goto free;
  }
  regs=devm_ioremap_resource(&pdev->dev,res);
  if(IS_ERR(regs)){ret=PTR_ERR(regs);goto free;}
  if(i)p->supervisor=regs;else p->ntt=regs;
 }
 p->clk=clk_get(&pdev->dev,"ap_clk");
 if(IS_ERR(p->clk)){ret=PTR_ERR(p->clk);goto free;}
 ret=clk_prepare_enable(p->clk);if(ret)goto put_clk;
 ret=clk_rate_exclusive_get(p->clk);if(ret)goto disable_clk;
 ret=ef_identity(p,true);if(ret)goto release_clk;
 if(!(ioread32(p->ntt+EF_AP)&EF_IDLE)){ret=-EBUSY;goto release_clk;}
 p->tx=dma_request_chan(&pdev->dev,"tx");
 if(IS_ERR(p->tx)){ret=PTR_ERR(p->tx);p->tx=NULL;goto release_clk;}
 p->rx=dma_request_chan(&pdev->dev,"rx");
 if(IS_ERR(p->rx)){ret=PTR_ERR(p->rx);p->rx=NULL;goto resources;}
 if(dmaengine_get_dma_device(p->tx)!=dmaengine_get_dma_device(p->rx)) {
  ret=-EXDEV;goto resources;
 }
 p->dma_dev=get_device(dmaengine_get_dma_device(p->tx));
 ret=dma_set_mask_and_coherent(p->dma_dev,DMA_BIT_MASK(32));if(ret)goto resources;
 /* Cacheable buffers + explicit dma_sync: the PL AXI DMA is not a coherent master here. */
 p->tx_buf=dma_alloc_noncoherent(p->dma_dev,EF_TX_BYTES,&p->tx_addr,DMA_BIDIRECTIONAL,GFP_KERNEL);
 if(!p->tx_buf){ret=-ENOMEM;goto resources;}
 p->rx_buf=dma_alloc_noncoherent(p->dma_dev,EF_RX_BYTES,&p->rx_addr,DMA_BIDIRECTIONAL,GFP_KERNEL);
 if(!p->rx_buf){ret=-ENOMEM;goto resources;}
 if((p->tx_addr&15)||(p->rx_addr&15)||(u64)p->tx_addr+EF_TX_BYTES>0x80000000ULL||
    (u64)p->rx_addr+EF_RX_BYTES>0x80000000ULL){ret=-ERANGE;goto resources;}
 p->dirty_tx=EF_TX_BYTES;p->dirty_rx=EF_RX_BYTES;
 if(ef_wipe(p)){ret=-EIO;goto resources;}
 p->online=true;
 p->misc.minor=MISC_DYNAMIC_MINOR;p->misc.name="edgefhe_v8b_dma";
 p->misc.fops=&ef_fops;p->misc.parent=&pdev->dev;
 platform_set_drvdata(pdev,p);
 ret=misc_register(&p->misc);if(ret){p->online=false;goto resources;}
 dev_info(&pdev->dev,"V8b candidate-5b-safe ABI=8 fastpath=%u ",READ_ONCE(fastpath));
 dev_info(&pdev->dev,"V8b candidate-4 ABI=8 epoch=%u tx=%pad rx=%pad buffers=cacheable+owned-sync "
          "DMA provider=%s recovery_proven=0\n",p->epoch,&p->tx_addr,&p->rx_addr,
          dev_name(p->dma_dev));
 return 0;
resources:
 ef_release_resources(p);
release_clk:
 clk_rate_exclusive_put(p->clk);
disable_clk:
 clk_disable_unprepare(p->clk);
put_clk:
 clk_put(p->clk);
free:
 kref_put(&p->refs,ef_free);
 return dev_err_probe(&pdev->dev,ret,"V8b candidate probe rejected\n");
}
static int ef_remove(struct platform_device *pdev)
{
 struct ef_proxy *p=platform_get_drvdata(pdev);
 int rx,tx;
 mutex_lock(&p->lock);p->online=false;ef_invalidate(&p->policy);
 mutex_unlock(&p->lock);
 /* Deregister outside our lock to avoid misc open/deregister lock inversion. */
 misc_deregister(&p->misc);
 mutex_lock(&p->lock);
 if(!p->retained) {
  /* Successful ioctl waited for callbacks AND HLS idle. A reset/identity
   * change or a busy engine observed at remove requires retention too. */
  if(p->pending||ef_identity(p,false)||!(ioread32(p->ntt+EF_AP)&EF_IDLE))
   ef_terminal(p,"remove without stable idle identity");
  else {
   rx=dmaengine_terminate_sync(p->rx);tx=dmaengine_terminate_sync(p->tx);
   if(rx||tx)ef_retain(p,"remove termination failed");
  }
 }
 if(!p->retained) {
  p->dirty_tx=EF_TX_BYTES;p->dirty_rx=EF_RX_BYTES;
  if(ef_wipe(p))ef_retain(p,"DMA buffer ownership violation at remove");
 }
 if(!p->retained) {
  ef_release_resources(p);
  clk_rate_exclusive_put(p->clk);clk_disable_unprepare(p->clk);clk_put(p->clk);
 }
 mutex_unlock(&p->lock);kref_put(&p->refs,ef_free);return 0;
}
static const struct of_device_id ef_of_match[]={
 {.compatible="ncue,edgefhe-v8b-dma-proxy"},{},
};
MODULE_DEVICE_TABLE(of,ef_of_match);
static struct platform_driver ef_driver={
 .probe=ef_probe,.remove=ef_remove,
 .driver={.name="edgefhe-v8b-dma-proxy",.of_match_table=ef_of_match},
};
module_platform_driver(ef_driver);
MODULE_LICENSE("GPL");
MODULE_AUTHOR("EdgeFHE Project");
MODULE_DESCRIPTION("V8b research DMAengine proxy: driver-framed ENC/PAIR, paired leases and terminal quarantine");
MODULE_VERSION("v8b-candidate-5b-safe");
