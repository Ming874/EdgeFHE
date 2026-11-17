// Application PL gate, native part (2026-10-16). Usage: ef_app_pl_selftest <os_plugin> <device|spec>
// With a device path the real V8b PL is used through driver candidate-4 (CAPS checked by v8::Node); with "spec" the
// V8b specification model behind the same driver-side checks is used (offline QA only, never PL evidence).
// For three explicit OS seeds, one ENC k=4 frame each (the owner's group size), every PL ciphertext coefficient must equal
// the CPU encryptor on sampler-B material from the same seed; a ten-feature scalar affine of the PL ciphertexts must
// equal the same affine of the CPU ciphertexts; |m| >= 2^62 must be refused before any device call.
#include "fast_encrypt.hpp"
#include "v8/v8b_device.hpp"
#include <cstring>
#include <iostream>
#include <memory>
using namespace edgefhe;
namespace v8=edgefhe_v8;
extern "C" int ef_cloud_affine(const uint32_t*,size_t,size_t,const int64_t*,int64_t,uint32_t*,size_t);
int main(int argc,char** argv){try{
  require(argc==3,"usage: ef_app_pl_selftest <os_plugin> <device|spec>");initialize_secure_sampler(argv[1]);
  const bool spec=std::strcmp(argv[2],"spec")==0;
  Engine engine(3);auto material=sample_secure_material();auto key=keygen_secure(engine,material);
  std::unique_ptr<v8::SpecModel> model;std::unique_ptr<v8::Node> node;std::unique_ptr<v8::Device> dev;
  if(spec){model.reset(new v8::SpecModel);dev.reset(new v8::Device([&](edgefhe_v8_request& r){return model->run(r);}));}
  else{node.reset(new v8::Node(argv[2]));dev.reset(new v8::Device([&](edgefhe_v8_request& r){return node->run(r);}));}
  const uint64_t generation=dev->load(key);
  constexpr size_t groups=3,F=4;
  std::vector<Cipher> pl,cpu;uint64_t exact=0;
  for(size_t g=0;g<groups;g++){
    std::array<std::vector<std::complex<double>>,4> x;
    for(size_t f=0;f<F;f++){x[f].resize(slots);for(size_t i=0;i<slots;i++)x[f][i]={double(int((i*13+(4*g+f)*7)%89)-44)/48.,0};}
    if(g==2)for(size_t f=2;f<F;f++)std::fill(x[f].begin(),x[f].end(),std::complex<double>{});   // owner padding 10 -> 12
    auto m=engine.encode_four(x);uint8_t seed[32];sampler_b::os_seed(seed);
    std::vector<const Message*> ptrs;for(const auto& p:m)ptrs.push_back(&p);
    std::vector<Cipher> c;dev->enc(seed,ptrs,c);
    SecureMaterial scratch;sampler_b::sample_from_seed(scratch,seed);explicit_bzero(seed,sizeof seed);
    const auto ref=edgefhe_v7::cpu_encrypt_secure_fast(engine,key,m,scratch);
    for(size_t f=0;f<F;f++){require(c[f].c0==ref[f].c0&&c[f].c1==ref[f].c1,"PL ciphertext == CPU ciphertext (same seed)");exact+=2*3*degree;pl.push_back(c[f]);cpu.push_back(ref[f]);}
    for(auto& v:m)explicit_bzero(v.data(),v.size()*sizeof(int64_t));
  }
  // Ten-feature affine (12 ciphertexts incl. padding) over the PL and the CPU ciphertexts must be identical.
  const size_t P=groups*F,W=6*degree;std::vector<int64_t> w(P,0);for(size_t f=0;f<10;f++)w[f]=(int64_t(f%5)-2)*(int64_t{1}<<27);
  std::vector<uint32_t> in_pl(P*W),in_cpu(P*W),out_pl(W),out_cpu(W);
  auto flatten=[&](const std::vector<Cipher>& v,std::vector<uint32_t>& o){for(size_t f=0;f<P;f++)for(size_t p=0;p<2;p++)for(size_t l=0;l<3;l++){
    const auto& poly=p?v[f].c1[l]:v[f].c0[l];std::copy(poly.begin(),poly.end(),o.begin()+f*W+p*3*degree+l*degree);}};
  flatten(pl,in_pl);flatten(cpu,in_cpu);
  require(ef_cloud_affine(in_pl.data(),in_pl.size(),P,w.data(),int64_t{1}<<57,out_pl.data(),out_pl.size())==0,"affine PL");
  require(ef_cloud_affine(in_cpu.data(),in_cpu.size(),P,w.data(),int64_t{1}<<57,out_cpu.data(),out_cpu.size())==0,"affine CPU");
  require(out_pl==out_cpu,"affine of PL ciphertexts == affine of CPU ciphertexts");
  // Fail-closed before DMA: out-of-range message coefficient.
  const uint64_t calls=dev->calls();
  {std::array<std::vector<std::complex<double>>,4> x;for(auto& v:x)v.assign(slots,{});auto m=engine.encode_four(x);m[0][7]=int64_t{1}<<62;
   uint8_t seed[32];sampler_b::os_seed(seed);std::vector<const Message*> ptrs;for(const auto& p:m)ptrs.push_back(&p);std::vector<Cipher> c;
   require(dev->try_enc(seed,ptrs,c)==-EINVAL&&dev->calls()==calls,"|m| >= 2^62 refused before any device call");explicit_bzero(seed,sizeof seed);}
  require(dev->generation()==generation,"one lease");
  std::cout<<"APPLICATION_PL_NATIVE_SELFTEST_PASS backend="<<(spec?"spec_model_not_PL":"V8B_PL_DEVICE")<<" pl_hz="<<(node?node->pl_hz:0)
           <<" exact_values="<<exact<<" affine_exact_values="<<W<<" fail_closed=1 device_calls="<<dev->calls()<<"\n";
  return 0;
}catch(const std::exception& e){std::cerr<<"APPLICATION_PL_NATIVE_SELFTEST_FAIL "<<e.what()<<"\n";return 1;}}
