// Native integration with the actual kernel policy; transport is an offline mock.
#include "secure_bridge.hpp"
#include "../driver/edgefhe_policy.h"
#include <iostream>
using namespace edgefhe;
namespace v7=edgefhe_v7;
struct MockKernel {
 ef_policy policy{};Engine math{3};PublicKey key;
 unsigned loads=0,pairs=0,legacy=0;bool early_ack=false;
 v7::Poly input(const edgefhe_v7_request& r,size_t poly) {
  auto *wire=reinterpret_cast<const v7::Beat*>(r.input_ptr);v7::Poly out;
  for(auto& l:out)l.resize(degree);
  for(size_t i=0;i<degree;i++)for(size_t l=0;l<3;l++)out[l][i]=wire[poly*degree+i].limb[l];
  return out;
 }
 int call(edgefhe_v7_request& r) {
  v7::validate_request(r);
  if(!std::equal(v7::active_profile.begin(),v7::active_profile.end(),r.profile_sha256))return -EINVAL;
  auto rc=ef_canonical(reinterpret_cast<const ef_u32*>(r.input_ptr),r.input_words);
  if(rc)return rc;
  rc=ef_begin(&policy,r.command,1,r.generation,r.profile_sha256,r.public_key_sha256);
  if(rc)return rc;
  auto *out=reinterpret_cast<v7::Beat*>(r.output_ptr);
  if(r.command==6) {
   key={input(r,0),input(r,1)};loads++;r.generation=policy.generation;
   out[0]={};
   if(early_ack){r.status=ef_finish(&policy,128,0);return r.status;}
  } else if(r.command==7) {
   pairs++;
   for(size_t f=0;f<4;f++) {
    auto u=input(r,f),bu=math.multiply(key.b,u),au=math.multiply(key.a,u);
    for(size_t i=0;i<degree;i++) {
     out[2*f*degree+i]={};out[(2*f+1)*degree+i]={};
     for(size_t l=0;l<3;l++){out[2*f*degree+i].limb[l]=bu[l][i];out[(2*f+1)*degree+i].limb[l]=au[l][i];}
    }
   }
  } else if(r.command==2) {
   legacy++;auto result=math.multiply(input(r,0),input(r,1));
   for(size_t i=0;i<degree;i++){out[i]={};for(size_t l=0;l<3;l++)out[i].limb[l]=result[l][i];}
  } else return -EINVAL;
  require(!ef_canonical(reinterpret_cast<ef_u32*>(out),r.output_words),"Mock output canonical");
  r.status=ef_finish(&policy,255,0);r.hardware_status=0;return r.status;
 }
};
int main(int argc,char** argv){try{
 require(argc==2,"OS plugin required");initialize_secure_sampler(argv[1]);
 Engine cpu(3);auto material=sample_secure_material();auto key=keygen_secure(cpu,material);
 MockKernel mock;v7::PairedSession session(v7::active_profile,[&](edgefhe_v7_request& r){return mock.call(r);});
 v7::Digest identity{};identity[0]=1;session.load(key.b,key.a,identity);
 uint64_t exact=0;double max_error=0;
 const std::array<double,4> w={1.1,-.7,.9,.4};std::array<Message,4> weights;
 for(size_t f=0;f<4;f++)weights[f]=encode(std::vector<std::complex<double>>(slots,w[f]));
 auto bias=encode(std::vector<std::complex<double>>(slots,-.35),scale*scale);
 for(unsigned trial=0;trial<6;trial++) {
  size_t records=trial%2?2048:1;std::array<std::vector<std::complex<double>>,4> x;
  for(size_t f=0;f<4;f++){x[f].resize(records);for(size_t i=0;i<records;i++)x[f][i]=(int((i+f*19+trial*7)%101)-50)/25.0;}
  auto messages=cpu.encode_four(x);sample_secure_encryption(material);
  require(affine_coefficient_bound(messages,weights,bias,material)<UWide(primes[0])*primes[1]*primes[2]/2,"No-wrap");
  auto expected=encrypt_secure(cpu,key,messages,material);
  auto actual=v7::encrypt_secure_paired(session,messages,material);
  for(size_t f=0;f<4;f++)for(auto member:{&Cipher::c0,&Cipher::c1})for(size_t l=0;l<3;l++)for(size_t i=0;i<degree;i++) {
   require((expected[f].*member)[l][i]==(actual[f].*member)[l][i],"Exact secure bridge ciphertext mismatch");exact++;
  }
  auto response=cpu.affine(actual,weights,bias);
  auto product=session.legacy(2,{response.c1,residues(material.secret)}).at(0);
  auto integer=centered(add(response.c0,product));
  require(integer==decrypt_secure(cpu,response,material.secret),"Exact bridge decryption");exact+=degree;
  auto result=decode(integer,scale*scale);double bound=secure_affine_error_bound(weights,material,2);
  for(size_t i=0;i<records;i++) {
   double expected_score=-.35;for(size_t f=0;f<4;f++)expected_score+=w[f]*x[f][i].real();
   double err=std::abs(result[i]-expected_score);max_error=std::max(max_error,err);
   require(err<=bound&&err<=5e-4,"CKKS secure bridge slot error");
  }
 }
 require(mock.loads==1&&mock.pairs==6&&mock.legacy==6&&session.generation()==1,"Cache and legacy preservation");
 material.e0[0][0]=56;bool rejected=false;
 try{v7::encrypt_secure_paired(session,cpu.encode_four({{{0},{0},{0},{0}}}),material);}catch(const std::exception&){rejected=true;}
 require(rejected&&mock.pairs==6,"Malformed Gaussian rejected before IO");
 MockKernel early;early.early_ack=true;
 v7::PairedSession session2(v7::active_profile,[&](edgefhe_v7_request& r){return early.call(r);});
 rejected=false;try{session2.load(key.b,key.a,identity);}catch(const std::exception&){rejected=true;}
 require(rejected&&!session2.generation()&&!early.policy.valid&&early.policy.quarantined,"Early ACK cannot publish");
 std::cout<<"V7_SECURE_PAIRED_NATIVE_BRIDGE_PASS trials=6 exact_values="<<exact<<" max_slot_error="<<max_error
          <<" load=1 pair=6 legacy_decrypt=6 DMA=0 offline_mock=1\n";
 return 0;
}catch(const std::exception& e){std::cerr<<"V7_SECURE_BRIDGE_FAIL "<<e.what()<<'\n';return 1;}}
