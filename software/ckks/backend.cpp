// PS-only checked native CPU oracle and AXI-DMA adapter. No HLS source here.
#define main frozen_public_vector_reference_main
#include "paired_public_vectors.cpp"
#undef main
#include <cerrno>
#include <cstddef>

struct PhaseTimes {
  uint64_t allocation_ns, pack_ns, setup_ns, ioctl_ns, unpack_check_ns, total_ns;
  uint32_t driver_us, reserved;
};
static_assert(sizeof(PhaseTimes)==56 && offsetof(PhaseTimes,driver_us)==48,"Phase ABI mismatch");
static_assert(_IOWR('E',0x04,Request)==0xc0304504UL,"ioctl ABI mismatch");

bool checked_input(const uint32_t* input){
  if(!input)return false;
  for(size_t l=0;l<3;l++)for(size_t i=0;i<N;i++)if(input[l*N+i]>=Q[l])return false;
  return true;
}
Rns as_rns(const uint32_t* input){
  Rns out;for(size_t l=0;l<3;l++)out[l]=Poly(input+l*N,input+(l+1)*N);return out;
}
int checked_output(const std::vector<Beat>& beats,uint32_t* output){
  if(beats.size()!=N || !output)return -EINVAL;
  for(size_t i=0;i<N;i++){
    if(beats[i].limb[3]!=0)return -EBADMSG;
    for(size_t l=0;l<3;l++)if(beats[i].limb[l]>=Q[l])return -EBADMSG;
  }
  for(size_t l=0;l<3;l++)for(size_t i=0;i<N;i++)output[l*N+i]=beats[i].limb[l];
  return 0;
}
uint64_t span_ns(Clock::time_point a,Clock::time_point b){
  return std::chrono::duration_cast<std::chrono::nanoseconds>(b-a).count();
}

#ifdef EDGEFHE_TEST_IOCTL
extern int edgefhe_test_ioctl(int,unsigned long,Request*);
#define edgefhe_ioctl edgefhe_test_ioctl
#else
#define edgefhe_ioctl ioctl
#endif

extern "C" int edgefhe_cpu(uint32_t operation,const uint32_t* a,const uint32_t* b,uint32_t* output){
  if(operation>2 || !output || !checked_input(a) || (operation==2 && !checked_input(b)))return -EINVAL;
  try{
    static const std::array<Plan,3> plans={Plan(Q[0],PSI[0]),Plan(Q[1],PSI[1]),Plan(Q[2],PSI[2])};
    Case c{};c.op=operation;c.a=as_rns(a);if(operation==2)c.b=as_rns(b);
    auto result=cpu(c,plans);
    for(size_t l=0;l<3;l++)for(size_t i=0;i<N;i++)output[l*N+i]=result[l][i];
    return 0;
  }catch(...){return -EIO;}
}

extern "C" int edgefhe_hardware_multiply(int fd,const uint32_t* a,const uint32_t* b,uint32_t* output,PhaseTimes* times){
  if(fd<0 || !output || !times || !checked_input(a) || !checked_input(b))return -EINVAL;
  *times={};
  try{
    auto t0=Clock::now();std::vector<Beat> input(2*N),result(N);auto t1=Clock::now();
    for(size_t i=0;i<N;i++)for(size_t l=0;l<3;l++){input[i].limb[l]=a[l*N+i];input[N+i].limb[l]=b[l*N+i];}
    auto t2=Clock::now();
    Request req{};req.operation=2;req.input_words=2*N;req.output_words=N;
    req.input_ptr=reinterpret_cast<uint64_t>(input.data());req.output_ptr=reinterpret_cast<uint64_t>(result.data());req.timeout_ms=5000;
    auto t3=Clock::now();int ret=edgefhe_ioctl(fd,_IOWR('E',0x04,Request),&req);int saved_errno=errno;auto t4=Clock::now();
    if(ret<0)return -saved_errno;
    if(req.status)return req.status;
    int checked=checked_output(result,output);auto t5=Clock::now();
    if(checked)return checked;
    times->allocation_ns=span_ns(t0,t1);times->pack_ns=span_ns(t1,t2);times->setup_ns=span_ns(t2,t3);
    times->ioctl_ns=span_ns(t3,t4);times->unpack_check_ns=span_ns(t4,t5);times->total_ns=span_ns(t0,t5);times->driver_us=req.elapsed_us;
    return 0;
  }catch(...){return -EIO;}
}
