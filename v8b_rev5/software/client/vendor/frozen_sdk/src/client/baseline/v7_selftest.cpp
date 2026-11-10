#include "secure.hpp"
#include "openfhe.h"
#include "math/hal/intnat/transformnat.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <fstream>
#include <iostream>
#include <random>
#include <thread>
using namespace edgefhe;
using intnat::NativeInteger;
using intnat::NativeVector;
using intnat::ChineseRemainderTransformFTTNat;
static Rns independent_product(const Rns& a,const Rns& b){
  Rns out;for(size_t l=0;l<3;l++){
    NativeInteger q(primes[l]),root(psi[l]);NativeVector x(degree,q),y(degree,q);
    for(size_t i=0;i<degree;i++){x[i]=NativeInteger(a[l][i]);y[i]=NativeInteger(b[l][i]);}
    ChineseRemainderTransformFTTNat<NativeVector> t;
    t.ForwardTransformToBitReverseInPlace(root,2*degree,&x);t.ForwardTransformToBitReverseInPlace(root,2*degree,&y);
    x=x.ModMul(y);t.InverseTransformFromBitReverseInPlace(root,2*degree,&x);
    out[l].resize(degree);for(size_t i=0;i<degree;i++)out[l][i]=x[i].ConvertToInt();
  }return out;
}
template<class A,class B> static std::vector<Wide> schoolbook(const A& a,const B& b){
  std::vector<Wide> out(degree);for(size_t j=0;j<degree;j++)if(b[j])for(size_t i=0;i<degree;i++){
    size_t k=i+j;out[k&(degree-1)]+=Wide(a[i])*b[j]*(k>=degree?-1:1);}
  return out;
}
int main(int argc,char** argv){try{
  require(argc==3,"usage: v7_selftest OS_PLUGIN DENSE_CASES");
  const size_t dense=std::stoul(argv[2]);require(dense>=1,"Dense cases must be positive");
  initialize_secure_sampler(argv[1]);
  WorkerPool pool(3);std::array<unsigned,3> calls{};std::array<std::thread::id,3> ids{};
  for(size_t i=0;i<100;i++)pool.run([&](size_t l){calls[l]++;if(i==0)ids[l]=std::this_thread::get_id();else require(ids[l]==std::this_thread::get_id(),"Workers were recreated");});
  bool refused=false;try{pool.run([](size_t l){if(l==1)throw std::runtime_error("injected worker error");});}catch(const std::runtime_error&){refused=true;}
  require(refused,"Worker exception lost");pool.run([&](size_t l){calls[l]++;});
  for(auto n:calls)require(n==101,"Worker liveness after exception");
  std::atomic<unsigned> concurrency{0};std::array<std::thread,4> callers;
  for(auto& t:callers)t=std::thread([&]{for(int i=0;i<20;i++)pool.run([&](size_t){concurrency++;});});
  for(auto& t:callers){t.join();}
  require(concurrency==240,"Concurrent pool dispatch lost");
  std::cout<<"V7_PERSISTENT_WORKERS_PASS dispatch=181 concurrent_callers=4 exception_recovery=1\n";
  Engine one(1),three(3);std::mt19937_64 public_rng(2026100971);
  for(size_t c=0;c<dense;c++){
    Rns a,b;for(size_t l=0;l<3;l++){a[l].resize(degree);b[l].resize(degree);for(size_t i=0;i<degree;i++){a[l][i]=public_rng()%primes[l];b[l][i]=public_rng()%primes[l];}}
    auto exact=independent_product(a,b);require(one.multiply(a,b)==exact&&three.multiply(a,b)==exact,"OpenFHE dense product mismatch");
    if((c+1)%100==0)std::cout<<"V7_DENSE_PROGRESS cases="<<c+1<<std::endl;
  }
  std::cout<<"V7_OPENFHE_DENSE_EXACT_PASS distinct_cases="<<dense<<" coefficients="<<dense*3*degree<<" seed=2026100971\n";
  auto m=sample_secure_material();unsigned zeros=0;for(auto s:m.secret)zeros+=s==0;
  require(zeros>degree/4&&zeros<degree/2,"Sampler gross distribution diagnostic");
  auto mapped=residues(m.error);
  for(size_t l=0;l<3;l++)for(size_t i=0;i<degree;i++){
    const auto v=m.error[i];const auto expected=v<0?primes[l]-uint32_t(-v):uint32_t(v);
    require(mapped[l][i]==expected,"RNS error must map the same signed integer");
  }
  require(classify(0,.001)==-1&&classify(.001,.001)==-1&&classify(-.001,.001)==-1&&classify(.002,.001)==1&&classify(-.002,.001)==0,"Classification boundary contract");
  const unsigned maxq=lbcrypto::StdLatticeParm::FindMaxQ(lbcrypto::HEStd_ternary,lbcrypto::HEStd_128_classic,degree);
  const UWide q=UWide(primes[0])*primes[1]*primes[2];unsigned bits=0;for(UWide t=q;t;t>>=1)bits++;
  require(bits<=maxq,"OpenFHE classical-128 parameter table rejection");
  std::cout<<"V7_STANDARD_TABLE_PASS Q_bits="<<bits<<" max_Q_bits="<<maxq<<" independent_estimator_pending=1\n";
  const auto key=keygen_secure(one,m);require(key.b==keygen_secure(three,m).b,"Secure keygen workers differ");
  double largest_error=0;
  for(size_t count:{size_t(1),size_t(32),size_t(256),slots}){
    std::array<std::vector<std::complex<double>>,4> values;
    for(size_t f=0;f<4;f++){values[f].resize(count);for(auto& v:values[f])v=(int(public_rng()%16001)-8000)/1000.0;}
    const auto messages=three.encode_four(values);std::array<Message,4> weights;
    const std::array<double,4> w={1.1,-.7,.9,.4};for(size_t f=0;f<4;f++)weights[f]=encode(std::vector<std::complex<double>>(slots,w[f]));
    auto bias=encode(std::vector<std::complex<double>>(slots,-.35),scale*scale);
    require(affine_coefficient_bound(messages,weights,bias,m)<q/2,"Secure affine coefficient wrap bound");
    auto a=encrypt_secure(one,key,messages,m),b=encrypt_secure(three,key,messages,m);
    for(size_t f=0;f<4;f++){
      require(a[f].c0==b[f].c0&&a[f].c1==b[f].c1,"Secure encrypt workers differ");
      auto recovered=decrypt_secure(three,a[f],m.secret);
      if(count==1){auto eu=schoolbook(m.error,m.mask[f]),e1s=schoolbook(m.e1[f],m.secret);
        for(size_t i=0;i<degree;i++)require(recovered[i]==Wide(messages[f][i])+eu[i]+m.e0[f][i]+e1s[i],"Independent exact RLWE cancellation");}
    }
    auto result=three.affine(a,weights,bias);auto decoded=decode(decrypt_secure(three,result,m.secret),scale*scale);
    double bound=secure_affine_error_bound(weights,m,8.0);
    for(size_t i=0;i<count;i++){double expected=-.35;for(size_t f=0;f<4;f++)expected+=w[f]*values[f][i].real();
      double error=std::abs(decoded[i]-expected);largest_error=std::max(largest_error,error);require(error<=bound&&error<=5e-4,"Secure CKKS exceeds precision gate");}
    std::cout<<"V7_SECURE_CKKS_CASE_PASS records="<<count<<" bound="<<bound<<" max_error_so_far="<<largest_error<<"\n";
    sample_secure_encryption(m);
  }
  auto stats=three.key_cache_stats();require(stats[1]==1&&stats[0]>=3,"Warm public-key cache not reused");
  auto changed=key;changed.a[0][0]=(changed.a[0][0]+1)%primes[0];
  std::array<Message,4> empty;for(auto& x:empty)x=Message(degree);
  encrypt_secure(three,changed,empty,m);require(three.key_cache_stats()[1]==2,"Public key change not invalidated");
  three.invalidate_public_key();encrypt_secure(three,key,empty,m);require(three.key_cache_stats()[1]==3,"Explicit invalidate ignored");
  auto oversized=empty;for(auto& p:oversized)p.assign(degree,INT64_MAX);
  bool overflow_rejected=false;try{(void)affine_coefficient_bound(oversized,oversized,Message(degree),m);}catch(const std::invalid_argument&){overflow_rejected=true;}
  require(overflow_rejected,"Overflowing no-wrap bound was accepted");
  std::cout<<"V7_SIGNED_RNS_BOUNDARY_AND_OVERFLOW_PASS coefficients=12288 classification_cases=5 malformed_bound_rejected=1\n";
  std::cout<<"V7_PUBLIC_CACHE_LIFECYCLE_PASS builds=3 source_identity_checked=1\n";
  std::cout<<"V7_CPU_SECURE_MATH_PASS independent_noise_coefficients=16384 max_slot_error="<<largest_error<<" actual_DMA=0 full_security_acceptance=0\n";
  return 0;
}catch(const std::exception& e){std::cerr<<"V7_FAIL "<<e.what()<<'\n';return 1;}}
