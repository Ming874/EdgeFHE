// Cloud evaluator: public model + ciphertext only. No secret/decrypt/PRNG APIs.
#include <cstdint>
#include <cstring>
#include <exception>
#include <stdexcept>
#include <string>
namespace {
constexpr size_t N=4096, WORDS=6*N;
thread_local std::string error;
void check(bool value,const char* why){if(!value)throw std::invalid_argument(why);}
// Card E (2026-10-17): the weight residues are computed once per limb (they were recomputed for every coefficient) and
// the feature loop is outside the coefficient loop, so each pass streams one contiguous polynomial. The arithmetic and
// the order of the modular additions per coefficient are unchanged, so the output is bit-identical to the previous version.
template<uint32_t Q> void limb(const uint32_t* input,size_t features,const int64_t* weights,int64_t bias,uint32_t* output,size_t l){
  uint64_t w[32];
  for(size_t f=0;f<features;f++){int64_t r=weights[f]%int64_t(Q);if(r<0)r+=Q;w[f]=uint64_t(r);}
  for(size_t p=0;p<2;p++){
    uint32_t* acc=output+p*3*N+l*N;
    for(size_t i=0;i<N;i++)acc[i]=0;
    for(size_t f=0;f<features;f++){
      const uint32_t* x=input+f*WORDS+p*3*N+l*N;const uint64_t r=w[f];
      for(size_t i=0;i<N;i++){
        check(x[i]<Q,"noncanonical ciphertext");
        const uint32_t product=uint32_t(uint64_t(x[i])*r%Q);
        const uint32_t sum=acc[i]+product;acc[i]=sum>=Q?sum-Q:sum;
      }
    }
    if(p==0){int64_t r=bias%int64_t(Q);if(r<0)r+=Q;const uint32_t sum=acc[0]+uint32_t(r);acc[0]=sum>=Q?sum-Q:sum;}
  }
}
}
extern "C" {
const char* ef_cloud_error(){return error.c_str();}
int ef_cloud_affine(const uint32_t* input,size_t input_words,size_t features,const int64_t* weights,int64_t bias,uint32_t* output,size_t output_words){
  try{
    check(input&&weights&&output,"null evaluator buffer");
    check(features>=4&&features<=32&&features%4==0&&input_words==features*WORDS&&output_words==WORDS,"evaluator dimensions");
    check(bias>-(int64_t{1}<<62)&&bias<(int64_t{1}<<62),"bias bound");
    for(size_t f=0;f<features;f++)check(weights[f]>=-(int64_t{16}<<30)&&weights[f]<=(int64_t{16}<<30),"weight bound");
    limb<998244353>(input,features,weights,bias,output,0);
    limb<1004535809>(input,features,weights,bias,output,1);
    limb<469762049>(input,features,weights,bias,output,2);
    error.clear();return 0;
  }catch(const std::exception& e){if(output&&output_words==WORDS)std::memset(output,0,WORDS*sizeof(uint32_t));error=e.what();return -1;}
}
}
