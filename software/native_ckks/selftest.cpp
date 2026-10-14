// Public QA seed only. Deployment sampler has no deterministic-seed interface.
#include "core.hpp"
#include <algorithm>
#include <cmath>
#include <iostream>
#include <random>
using namespace edgefhe;
static std::vector<Wide> sparse_product(const Ternary& a,const Ternary& b) {
  std::vector<Wide> out(degree);
  for(size_t j=0;j<degree;j++) if(b[j]) for(size_t i=0;i<degree;i++) if(a[i]) {
    const size_t index=i+j; out[index%degree]+=Wide(a[i])*b[j]*(index>=degree?-1:1);
  } return out;
}
int main() {
  try {
    Engine one(1),three(3); const auto material=sample_material(); const auto key=one.keygen(material);
    const Wide q=Wide(primes[0])*primes[1]*primes[2];
    std::vector<Wide> wanted(degree); Rns crt;
    for(auto& p:crt) p.resize(degree);
    for(size_t i=0;i<degree;i++) { wanted[i]=(i%2?1:-1)*(q/2-Wide(i));
      for(size_t l=0;l<3;l++) { const Wide r=wanted[i]%primes[l]; crt[l][i]=uint32_t(r<0?r+primes[l]:r); } }
    require(centered(crt)==wanted,"Wide CRT centered boundary mismatch");
    std::array<Message,4> messages;
    std::mt19937 public_qa(20261007); std::uniform_real_distribution<double> distribution(-4,4);
    for(size_t l=0;l<3;l++) {
      const Plan plan(primes[l],psi[l]);
      for(size_t i=0;i<10000;i++) { const auto a=public_qa()%primes[l],b=public_qa()%primes[l];
        require(plan.product(a,b)==uint64_t(a)*b%primes[l],"Barrett point-product mismatch"); }
      require(plan.product(primes[l]-1,primes[l]-1)==1,"Barrett boundary failed");
    }
    for(size_t f=0;f<4;f++) {
      std::vector<std::complex<double>> values(slots);
      for(auto& v:values) v={distribution(public_qa),distribution(public_qa)};
      messages[f]=encode(values); const auto restored=decode(std::vector<Wide>(messages[f].begin(),messages[f].end()));
      for(size_t i=0;i<slots;i++) require(std::abs(restored[i]-values[i])<=degree/(2.*scale)+1e-8,"Full complex slots rounding bound");
    }
    const auto cipher=one.encrypt(key,messages,material.mask,material.e0,material.e1);
    const auto parallel_cipher=three.encrypt(key,messages,material.mask,material.e0,material.e1);
    for(size_t f=0;f<4;f++) {
      require(cipher[f].c0==parallel_cipher[f].c0&&cipher[f].c1==parallel_cipher[f].c1,"Parallel encryption differs");
      const auto eu=sparse_product(material.error,material.mask[f]),e1s=sparse_product(material.e1[f],material.secret);
      const auto recovered=one.decrypt(cipher[f],material.secret);
      for(size_t i=0;i<degree;i++) require(recovered[i]==Wide(messages[f][i])+eu[i]+material.e0[f][i]+e1s[i],"Integer RLWE cancellation differs");
    }
    std::array<Message,4> weights;for(auto& w:weights)w=encode(std::vector<std::complex<double>>(slots,1.1));
    const auto bias=encode(std::vector<std::complex<double>>(slots,0.35),scale*scale);
    const auto scalar_result=one.affine(cipher,weights,bias);
    const auto br=residues(bias);std::array<Rns,4> wr;
    for(size_t f=0;f<4;f++)wr[f]=residues(weights[f]);
    for(size_t l=0;l<3;l++)for(size_t i=0;i<degree;i++) {
      uint64_t a=br[l][i],b=0;
      for(size_t f=0;f<4;f++) { a+=uint64_t(cipher[f].c0[l][i])*wr[f][l][0];b+=uint64_t(cipher[f].c1[l][i])*wr[f][l][0]; }
      require(scalar_result.c0[l][i]==a%primes[l]&&scalar_result.c1[l][i]==b%primes[l],"Scalar affine independent coefficient oracle mismatch");
    }
    require(no_wrap_bound(messages,weights,bias)<UWide(q)/2,"Admissible no-wrap bound failed");
    auto oversized=messages;std::fill(oversized[0].begin(),oversized[0].end(),INT64_MAX);
    bool refused=false;try{no_wrap_bound(oversized,weights,bias);}catch(const std::invalid_argument&){refused=true;}
    require(refused,"Oversized no-wrap operands accepted");
    require(sigmoid(1000)==1&&sigmoid(-1000)==0&&classify(0,0.001)==-1,"Stable sigmoid/classification boundary failed");
    std::cout<<"NATIVE_CPP_SELFTEST_PASS full_complex_slots=8192 exact_noise_coefficients=16384 wide_CRT=4096 scalar_affine_exact=24576 workers=1,3\n";
    return 0;
  } catch(const std::exception& e) { std::cerr<<"FAIL: "<<e.what()<<"\n"; return 1; }
}
