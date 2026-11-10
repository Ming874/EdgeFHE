#include "span_wire.hpp"
#include <iostream>
int main(){using namespace efnet;size_t checked=0,rejected=0;
  for(size_t features=1;features<=32;features++){
    std::vector<uint32_t> words(((features+3)/4*4)*ct_words);
    for(size_t i=0;i<features*ct_words;i++)words[i]=uint32_t((i*7919+features)%q[(i/N)%3]);
    const auto legacy=pack(words,features);const auto view=payload_view(words,features);
    need(view.bytes==legacy.size()&&std::equal(legacy.begin(),legacy.end(),view.data),"SPAN_BYTE_EQUIVALENCE");checked+=legacy.size();
    Header h;h.bytes=view.bytes;h.features=uint32_t(features);need(request_hash(h,legacy)==span_request_hash(h,view),"SPAN_HASH_EQUIVALENCE");
    words[0]=q[0];try{payload_view(words,features);throw std::logic_error("SPAN_BAD_CANON_ACCEPTED");}catch(const std::runtime_error&){++rejected;}
  }
  std::vector<uint32_t> words(4*ct_words);words[ct_words]=1;
  try{payload_view(words,1);throw std::logic_error("SPAN_BAD_PAD_ACCEPTED");}catch(const std::runtime_error&){++rejected;}
  need(rejected==33,"SPAN_REFUSALS");std::cout<<"CPP_MTLS_SPAN_BYTE_HASH_EQUIVALENCE_PASS bytes="<<checked<<" refused="<<rejected<<" NO_DEVICE\n";
}
