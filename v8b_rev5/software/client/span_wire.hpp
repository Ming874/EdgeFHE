#pragma once
#include "tls.hpp"
namespace efnet {
struct PayloadView {const uint8_t* data;size_t bytes;};
inline void little_endian_host(){const uint32_t one=1;need(*reinterpret_cast<const uint8_t*>(&one)==1,"SPAN_REQUIRES_LITTLE_ENDIAN");}
inline PayloadView payload_view(const std::vector<uint32_t>& words,size_t features){
  little_endian_host();need(features>0&&features<=32&&words.size()==((features+3)/4*4)*ct_words,"SPAN_PADDED_LENGTH");
  canonical(words.data(),words.size());
  need(std::all_of(words.begin()+features*ct_words,words.end(),[](uint32_t x){return x==0;}),"SPAN_NONZERO_PADDING");
  return {reinterpret_cast<const uint8_t*>(words.data()),features*ct_bytes};
}
inline Digest span_request_hash(Header h,PayloadView body){h.request_hash={};const auto b=encode(h);return digest(b.data(),b.size(),body.data,body.bytes);}
inline void write_span_frame(Channel& channel,const Header& h,PayloadView body,Deadline end){
  need(body.data&&body.bytes==h.bytes,"SPAN_FRAME_LENGTH");const auto bytes=encode(h);
  channel.write(bytes.data(),bytes.size(),end);channel.write(body.data,body.bytes,end);
}
}
