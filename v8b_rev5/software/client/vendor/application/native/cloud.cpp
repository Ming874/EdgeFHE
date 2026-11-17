// Cloud evaluator: public model + ciphertext only. No secret/decrypt/PRNG APIs.
// app v5 (2026-10-22): the three limbs run on three threads (two persistent workers + the caller, each with its own
// accumulator); per-limb arithmetic is the app v3 text unchanged, so output words are identical (ARM diagnostic
// OPT_DIAG_V1: 1.07 -> 0.40 ms for 4 features). Calls are serialized by a mutex (the v4 function was reentrant).
// app v3 (2026-10-19): 30x30-bit products are accumulated exactly in 64 bits (at most 16 products of < 2^60 each stay
// below 2^64) and reduced once per coefficient instead of once per product; the canonical-input check is accumulated
// and decided once per pass. The result is the same residue as v2 (exact modular arithmetic), so the output words are
// bit-identical to v2 (checked by app v3 equivalence selftest). Same API, same validation, same error behaviour.
#include <array>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <mutex>
#include <thread>
#include <cstring>
#include <exception>
#include <stdexcept>
#include <string>
#include <vector>
namespace {
constexpr size_t N=4096, WORDS=6*N;
thread_local std::string error;
void check(bool value,const char* why){if(!value)throw std::invalid_argument(why);}
template<uint32_t Q> void limb(const uint32_t* input,size_t features,const int64_t* weights,int64_t bias,uint32_t* output,size_t l,uint64_t* acc){
  uint64_t w[32];
  for(size_t f=0;f<features;f++){int64_t r=weights[f]%int64_t(Q);if(r<0)r+=Q;w[f]=uint64_t(r);}
  uint32_t bad=0;
  for(size_t p=0;p<2;p++){
    for(size_t i=0;i<N;i++)acc[i]=0;
    for(size_t f0=0;f0<features;f0+=16){
      const size_t f1=f0+16<features?f0+16:features;
      for(size_t f=f0;f<f1;f++){
        const uint32_t* x=input+f*WORDS+p*3*N+l*N;const uint64_t r=w[f];
        for(size_t i=0;i<N;i++){bad|=uint32_t(x[i]>=Q);acc[i]+=uint64_t(x[i])*r;}
      }
      if(f1<features)for(size_t i=0;i<N;i++)acc[i]%=Q;
    }
    check(bad==0,"noncanonical ciphertext");
    uint32_t* dst=output+p*3*N+l*N;
    for(size_t i=0;i<N;i++)dst[i]=uint32_t(acc[i]%Q);
    if(p==0){int64_t r=bias%int64_t(Q);if(r<0)r+=Q;const uint32_t sum=dst[0]+uint32_t(r);dst[0]=sum>=Q?sum-Q:sum;}
  }
}
// Persistent 3-lane runner: lanes 1 and 2 on workers, lane 0 on the caller. Errors are captured per lane and rethrown.
class Lanes {
  std::mutex call_, mu_; std::condition_variable ready_, done_; std::function<void(size_t)> task_; uint64_t epoch_ = 0; unsigned completed_ = 0; bool stop_ = false;
  std::array<std::exception_ptr, 3> errors_{}; std::array<std::thread, 2> threads_;
  void loop(size_t lane) {
    uint64_t seen = 0;
    for (;;) {
      std::unique_lock<std::mutex> lk(mu_); ready_.wait(lk, [&] { return stop_ || epoch_ != seen; }); if (stop_) return; seen = epoch_; lk.unlock();
      try { task_(lane); } catch (...) { errors_[lane] = std::current_exception(); }
      lk.lock(); completed_++; done_.notify_one();
    }
  }
 public:
  Lanes() { for (size_t i = 0; i < 2; i++) threads_[i] = std::thread([this, i] { loop(i + 1); }); }
  ~Lanes() { { std::lock_guard<std::mutex> lk(mu_); stop_ = true; } ready_.notify_all(); for (auto& t : threads_) t.join(); }
  void run(const std::function<void(size_t)>& task) {
    std::lock_guard<std::mutex> call(call_);
    { std::lock_guard<std::mutex> lk(mu_); task_ = task; errors_ = {}; completed_ = 0; epoch_++; }
    ready_.notify_all();
    try { task(0); } catch (...) { errors_[0] = std::current_exception(); }
    std::unique_lock<std::mutex> lk(mu_); done_.wait(lk, [&] { return completed_ == 2; }); task_ = {};
    const auto errors = errors_; lk.unlock(); for (const auto& e : errors) if (e) std::rethrow_exception(e);
  }
};
Lanes& lanes() { static Lanes instance; return instance; }
}
extern "C" {
const char* ef_cloud_error(){return error.c_str();}
int ef_cloud_affine(const uint32_t* input,size_t input_words,size_t features,const int64_t* weights,int64_t bias,uint32_t* output,size_t output_words){
  try{
    check(input&&weights&&output,"null evaluator buffer");
    check(features>=4&&features<=32&&features%4==0&&input_words==features*WORDS&&output_words==WORDS,"evaluator dimensions");
    check(bias>-(int64_t{1}<<62)&&bias<(int64_t{1}<<62),"bias bound");
    for(size_t f=0;f<features;f++)check(weights[f]>=-(int64_t{16}<<30)&&weights[f]<=(int64_t{16}<<30),"weight bound");
    std::array<std::vector<uint64_t>,3> acc;for(auto& a:acc)a.resize(N);
    lanes().run([&](size_t l){
      if(l==0)limb<998244353>(input,features,weights,bias,output,0,acc[0].data());
      else if(l==1)limb<1004535809>(input,features,weights,bias,output,1,acc[1].data());
      else limb<469762049>(input,features,weights,bias,output,2,acc[2].data());
    });
    error.clear();return 0;
  }catch(const std::exception& e){if(output&&output_words==WORDS)std::memset(output,0,WORDS*sizeof(uint32_t));error=e.what();return -1;}
}
}
