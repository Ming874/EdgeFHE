#include "secure.hpp"
#include "openfhe.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <limits>
#include <mutex>
#include <random>
#include <string>
namespace edgefhe {
namespace {
std::mutex sampler_mutex;
std::atomic<bool> sampler_ready{false};
template<class T> void wipe(std::vector<T>& v){volatile T* p=v.data();for(size_t i=0;i<v.size();i++)p[i]=0;}
void ternary(const Ternary& x){require(x.size()==degree,"Ternary degree");for(auto v:x)require(v>=-1&&v<=1,"Uniform ternary coefficient");}
void gaussian(const Message& x){require(x.size()==degree,"Gaussian degree");for(auto v:x)require(v>=-55&&v<=55,"Pinned inversion sampler support mismatch");}
Ternary uniform_ternary(){Ternary p(degree);std::uniform_int_distribution<int> d(-1,1);for(auto& v:p)v=int8_t(d(lbcrypto::PseudoRandomNumberGenerator::GetPRNG()));return p;}
Message noise(){lbcrypto::DiscreteGaussianGeneratorImpl<lbcrypto::NativeVector> dgg(4.5);return dgg.GenerateIntVector(degree);}
template<class T> UWide norm(const std::vector<T>& x){require(x.size()==degree,"Bound degree");UWide total=0;for(auto v:x)total+=v<0?UWide(-(Wide(v))):UWide(v);return total;}
UWide maximum(const Message& x){require(x.size()==degree,"Bound degree");UWide out=0;for(auto v:x)out=std::max(out,v<0?UWide(-Wide(v)):UWide(v));return out;}
}
SecureMaterial::~SecureMaterial(){wipe(secret);wipe(error);for(auto& p:mask)wipe(p);for(auto& p:e0)wipe(p);for(auto& p:e1)wipe(p);}
void initialize_secure_sampler(const std::string& plugin){
  std::lock_guard<std::mutex> lock(sampler_mutex);
  require(!sampler_ready&&!plugin.empty(),"Initialize OS sampler exactly once before worker use");
  lbcrypto::PseudoRandomNumberGenerator::InitPRNGEngine(plugin);sampler_ready.store(true,std::memory_order_release);
}
SecureMaterial sample_secure_material(){
  std::lock_guard<std::mutex> lock(sampler_mutex);require(sampler_ready,"OS sampler not initialized");
  SecureMaterial m;m.secret=uniform_ternary();m.error=noise();
  for(size_t f=0;f<4;f++){m.mask[f]=uniform_ternary();m.e0[f]=noise();m.e1[f]=noise();}
  for(size_t l=0;l<3;l++){m.a[l].resize(degree);std::uniform_int_distribution<uint32_t> d(0,primes[l]-1);
    for(auto& v:m.a[l])v=d(lbcrypto::PseudoRandomNumberGenerator::GetPRNG());}
  return m;
}
void sample_secure_encryption(SecureMaterial& m){
  std::lock_guard<std::mutex> lock(sampler_mutex);require(sampler_ready,"OS sampler not initialized");
  for(size_t f=0;f<4;f++){wipe(m.mask[f]);wipe(m.e0[f]);wipe(m.e1[f]);m.mask[f]=uniform_ternary();m.e0[f]=noise();m.e1[f]=noise();}
}
// Lock-free variant for parallel prepare workers (plan step 09). OpenFHE 1.5.1 keeps one PRNG engine per thread
// (PseudoRandomNumberGenerator::m_prng is omp threadprivate / thread_local; creation is inside omp critical), the
// external OS engine has only per-instance state, and the Gaussian inversion table is a per-thread read-only object
// that consumes no randomness when built. Same draw order and algorithms as sample_secure_encryption, so an
// identical engine stream gives identical material (sampler_selftest checks this with a test-only engine).
void sample_secure_encryption_concurrent(SecureMaterial& m){
  require(sampler_ready.load(std::memory_order_acquire),"OS sampler not initialized");
  thread_local const lbcrypto::DiscreteGaussianGeneratorImpl<lbcrypto::NativeVector> dgg(4.5);
  for(size_t f=0;f<4;f++){wipe(m.mask[f]);wipe(m.e0[f]);wipe(m.e1[f]);
    m.mask[f]=uniform_ternary();m.e0[f]=dgg.GenerateIntVector(degree);m.e1[f]=dgg.GenerateIntVector(degree);}
}
PublicKey keygen_secure(const Engine& engine,const SecureMaterial& m){
  ternary(m.secret);gaussian(m.error);validate(m.a);auto p=engine.multiply(m.a,residues(m.secret)),e=residues(m.error);
  for(size_t l=0;l<3;l++)for(size_t i=0;i<degree;i++){auto r=e[l][i]+primes[l]-p[l][i];p[l][i]=r>=primes[l]?r-primes[l]:r;}
  return {std::move(p),m.a};
}
std::array<Cipher,4> encrypt_secure(const Engine& engine,const PublicKey& key,const std::array<Message,4>& message,const SecureMaterial& m){
  std::array<Rns,4> mr,u,e0,e1;
  for(size_t f=0;f<4;f++){ternary(m.mask[f]);gaussian(m.e0[f]);gaussian(m.e1[f]);mr[f]=residues(message[f]);u[f]=residues(m.mask[f]);e0[f]=residues(m.e0[f]);e1[f]=residues(m.e1[f]);}
  auto out=engine.encrypt_residues(key,mr,u,e0,e1);
  for(auto* a:{&mr,&u,&e0,&e1})for(auto& p:*a)for(auto& limb:p)wipe(limb);
  return out;
}
std::vector<Wide> decrypt_secure(const Engine& engine,const Cipher& cipher,const Ternary& secret){ternary(secret);return centered(add(cipher.c0,engine.multiply(cipher.c1,residues(secret))));}
UWide l1(const Message& x){return norm(x);}UWide l1(const Ternary& x){return norm(x);}
UWide encryption_noise_bound(const SecureMaterial& m,size_t f){
  require(f<4,"Noise feature index");return l1(m.error)*l1(m.mask[f])+l1(m.e0[f])+l1(m.e1[f])*l1(m.secret);
}
UWide affine_coefficient_bound(const std::array<Message,4>& message,const std::array<Message,4>& weight,const Message& bias,const SecureMaterial& m){
  const UWide limit=UWide(primes[0])*primes[1]*primes[2]/2;
  UWide out=maximum(bias);require(out<=limit,"Affine bias wrap");
  for(size_t f=0;f<4;f++){
    const UWide a=maximum(message[f])+encryption_noise_bound(m,f),b=l1(weight[f]);
    require(b==0||a<=(limit-out)/b,"Affine bound overflow or centered CRT wrap");out+=a*b;
  }return out;
}
double secure_affine_error_bound(const std::array<Message,4>& weight,const SecureMaterial& m,double input_abs_max){
  require(std::isfinite(input_abs_max)&&input_abs_max>=0,"Error-bound input range");
  const long double delta=scale,rounding=degree/(2*delta);long double out=degree/(2*delta*delta);
  for(size_t f=0;f<4;f++)out+=(static_cast<long double>(l1(weight[f]))/delta)*rounding+input_abs_max*rounding+rounding*rounding+
    static_cast<long double>(l1(weight[f]))*static_cast<long double>(encryption_noise_bound(m,f))/(delta*delta);
  return double(out);
}
}
