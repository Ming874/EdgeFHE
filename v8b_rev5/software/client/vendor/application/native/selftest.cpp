#include "fast_encrypt.hpp"
#include "v8/v8b_device.hpp"
#include <iostream>
using namespace edgefhe;
extern "C" int ef_cloud_affine(const uint32_t*,size_t,size_t,const int64_t*,int64_t,uint32_t*,size_t);
int main(int argc,char** argv){try{
  require(argc==2,"selftest requires OS PRNG plugin");initialize_secure_sampler(argv[1]);Engine engine(3);
  auto material=sample_secure_material();auto key=keygen_secure(engine,material);edgefhe_v8::SpecModel backend;
  edgefhe_v8::Device dev([&](edgefhe_v8_request& r){return backend.run(r);});dev.load(key);
  std::array<Message,4> messages;std::array<Message,4> weights;
  const int64_t wi[4]={3*(int64_t{1}<<27),-5*(int64_t{1}<<26),0,int64_t{1}<<29};
  for(size_t f=0;f<4;f++){
    std::vector<std::complex<double>> values(slots);for(size_t i=0;i<slots;i++)values[i]={double(int((i*17+f*3)%97)-48)/64.,0};messages[f]=encode(values);
    weights[f]=encode(std::vector<std::complex<double>>(slots,double(wi[f])/scale));
    require(weights[f][0]==wi[f],"scalar weight constant coefficient");for(size_t j=1;j<degree;j++)require(weights[f][j]==0,"scalar weight no hidden polynomial terms");
  }
  uint8_t seed[32];sampler_b::os_seed(seed);SecureMaterial scratch;sampler_b::sample_from_seed(scratch,seed);
  auto reference=edgefhe_v7::cpu_encrypt_secure_fast(engine,key,messages,scratch);std::vector<Cipher> actual;
  std::vector<const Message*> ptrs;for(const auto& m:messages)ptrs.push_back(&m);dev.enc(seed,ptrs,actual);explicit_bzero(seed,sizeof seed);
  for(size_t f=0;f<4;f++)require(reference[f].c0==actual[f].c0&&reference[f].c1==actual[f].c1,"CPU/spec exact encryption");
  std::vector<uint32_t> input(4*6*degree),output(6*degree);
  for(size_t f=0;f<4;f++)for(size_t p=0;p<2;p++)for(size_t l=0;l<3;l++){
    const auto& poly=p?actual[f].c1[l]:actual[f].c0[l];std::copy(poly.begin(),poly.end(),input.begin()+f*6*degree+p*3*degree+l*degree);
  }
  auto bias=encode(std::vector<std::complex<double>>(slots,.125),scale*scale);
  require(ef_cloud_affine(input.data(),input.size(),4,wi,bias[0],output.data(),output.size())==0,"scalar cloud evaluator");
  const auto expected=engine.affine(reference,weights,bias);
  for(size_t p=0;p<2;p++)for(size_t l=0;l<3;l++)for(size_t i=0;i<degree;i++)
    require(output[p*3*degree+l*degree+i]==(p?expected.c1[l][i]:expected.c0[l][i]),"direct scalar vs NTT affine exact");
  std::cout<<"APPLICATION_NATIVE_MATH_SELFTEST_PASS encryption_exact=98304 scalar_vs_NTT_exact=24576 simulated_PL=1 hardware_acceptance=0\n";return 0;
}catch(const std::exception& e){std::cerr<<"APPLICATION_NATIVE_MATH_SELFTEST_FAIL "<<e.what()<<"\n";return 1;}}
