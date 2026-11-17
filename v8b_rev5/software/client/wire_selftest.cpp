#include "wire.hpp"
#include <iostream>
extern "C" int ef_cloud_affine(const uint32_t*,size_t,size_t,const int64_t*,int64_t,uint32_t*,size_t);
int main(){
  using namespace efnet;
  try{
    size_t checks=0,refused=0;uint32_t rng=17082026;
    auto next=[&]{rng=rng*1664525+1013904223;return rng;};
    auto reject=[&](const auto& fn){bool failed=false;try{fn();}catch(const std::exception&){failed=true;}need(failed,"SELFTEST_EXPECTED_REFUSAL");++refused;};
    for(size_t f=1;f<=32;f++){
      const size_t p=(f+3)/4*4;std::vector<uint32_t> full(p*ct_words,0);
      for(size_t i=0;i<f*ct_words;i++)full[i]=next()%q[(i/N)%3];
      const auto body=pack(full,f);need(body.size()==f*ct_bytes&&unpack(body,f,true)==full,"PACK_ROUNDTRIP");++checks;
      std::vector<int64_t> weights(p,0);for(size_t k=0;k<f;k++)weights[k]=int64_t(next()%100000)-50000;
      auto padded_noise=full;for(size_t i=f*ct_words;i<full.size();i++)padded_noise[i]=next()%q[(i/N)%3];
      std::vector<uint32_t> a(ct_words),b(ct_words);const int64_t bias=-192;
      need(ef_cloud_affine(padded_noise.data(),padded_noise.size(),p,weights.data(),bias,a.data(),a.size())==0,"REFERENCE_AFFINE");
      need(ef_cloud_affine(full.data(),full.size(),p,weights.data(),bias,b.data(),b.size())==0&&a==b,"ZERO_WEIGHT_TAIL_EQUIVALENCE");checks+=ct_words;
      // Separate scalar oracle: reduce each product before adding, avoiding the
      // cloud implementation's grouped accumulation and threading strategy.
      for(size_t c=0;c<2;c++)for(size_t l=0;l<3;l++)for(size_t i=0;i<N;i++){
        uint64_t acc=0;for(size_t k=0;k<f;k++){int64_t w=weights[k]%int64_t(q[l]);if(w<0)w+=q[l];acc=(acc+(uint64_t(full[k*ct_words+c*3*N+l*N+i])*uint64_t(w))%q[l])%q[l];}
        if(c==0&&i==0){int64_t r=bias%int64_t(q[l]);if(r<0)r+=q[l];acc=(acc+uint64_t(r))%q[l];}
        need(a[c*3*N+l*N+i]==acc,"INDEPENDENT_SCALAR_AFFINE");++checks;
      }
      auto bad=body;put32(bad.data(),q[0]);reject([&]{unpack(bad,f,true);});
      if(p>f){full[f*ct_words]=1;reject([&]{pack(full,f);});}
    }
    const auto& m=catalogue()[1];Header h;h.features=10;h.rows=2048;h.bytes=10*ct_bytes;h.generation=7;h.sequence=1;
    h.profile_id=profile();h.model_id=unhex(m.source_sha256);h.key_id=random_digest();h.session_id=random_digest();h.request_id=request_id(h);
    std::vector<uint8_t> body(10*ct_bytes,0);h.request_hash=request_hash(h,body);auto bytes=encode(h);need(encode(decode(bytes.data(),bytes.size()))==bytes,"HEADER_ROUNDTRIP");++checks;
    ConnectionSequence sequence(h.session_id);sequence.consume(h,body);reject([&]{sequence.consume(h,body);});
    auto next_header=h;next_header.sequence=2;next_header.request_id=request_id(next_header);next_header.request_hash=request_hash(next_header,body);sequence.header(next_header);++checks;
    for(size_t offset:{size_t(0),size_t(8),size_t(9),size_t(10),size_t(11),size_t(12),size_t(24),size_t(28),size_t(56),size_t(64),size_t(96)}){
      auto bad=bytes;bad[offset]^=0x80;reject([&]{decode(bad.data(),bad.size());});
    }
    for(size_t n:{size_t(0),size_t(1),size_t(255)})reject([&]{decode(bytes.data(),n);});
    for(uint32_t n:{0u,33u,UINT32_MAX}){auto bad=bytes;put32(bad.data()+16,n);reject([&]{decode(bad.data(),bad.size());});}
    auto bad=bytes;put64(bad.data()+32,UINT64_MAX);reject([&]{decode(bad.data(),bad.size());});
    auto response=h;response.kind=2;response.bytes=ct_bytes;validate_response(response,h);++checks;
    response.key_id=random_digest();reject([&]{validate_response(response,h);});
    next_header.key_id=random_digest();next_header.request_id=request_id(next_header);reject([&]{sequence.header(next_header);});
    next_header=h;next_header.sequence=2;next_header.session_id=random_digest();next_header.request_id=request_id(next_header);reject([&]{sequence.header(next_header);});
    std::cout<<"CPP_E2E_WIRE_AND_PADDED_AFFINE_PASS exact_checks="<<checks<<" refusals="<<refused<<" NO_DEVICE\n";return 0;
  }catch(const std::exception& e){std::cerr<<"STOP_WIRE_SELFTEST "<<e.what()<<'\n';return 1;}
}
