#pragma once
#include "models.hpp"
#include "profile_id.hpp"
namespace efnet {
constexpr size_t N=4096,ct_words=6*N,ct_bytes=ct_words*4,header_bytes=256;
constexpr std::array<uint32_t,3> q={998244353,1004535809,469762049};
using HeaderBytes=std::array<uint8_t,header_bytes>;
inline Digest profile(){return unhex(profile_hex);}
inline void put32(uint8_t* p,uint32_t v){for(unsigned i=0;i<4;i++)p[i]=uint8_t(v>>(8*i));}
inline void put64(uint8_t* p,uint64_t v){for(unsigned i=0;i<8;i++)p[i]=uint8_t(v>>(8*i));}
inline uint32_t get32(const uint8_t* p){uint32_t n=0;for(unsigned i=0;i<4;i++)n|=uint32_t(p[i])<<(8*i);return n;}
inline uint64_t get64(const uint8_t* p){uint64_t n=0;for(unsigned i=0;i<8;i++)n|=uint64_t(p[i])<<(8*i);return n;}
struct Header {
  uint16_t kind=1;uint32_t features=0,rows=0;uint64_t bytes=0,generation=0,sequence=0;
  Digest profile_id{},model_id{},key_id{},request_id{},session_id{},request_hash{};
};
inline HeaderBytes encode(const Header& h){
  HeaderBytes b{};std::memcpy(b.data(),"EFHEW001",8);b[8]=1;b[10]=uint8_t(h.kind);put32(&b[12],header_bytes);
  put32(&b[16],h.features);put32(&b[20],h.rows);put32(&b[24],1);put64(&b[32],h.bytes);put64(&b[40],h.generation);put64(&b[48],h.sequence);
  const Digest* ds[]={&h.profile_id,&h.model_id,&h.key_id,&h.request_id,&h.session_id,&h.request_hash};
  for(size_t i=0;i<6;i++){std::copy(ds[i]->begin(),ds[i]->end(),b.begin()+64+32*i);}
  return b;
}
inline Header decode(const uint8_t* p,size_t size){
  need(p&&size==header_bytes,"WIRE_HEADER_LENGTH");need(std::memcmp(p,"EFHEW001",8)==0&&p[8]==1&&p[9]==0,"WIRE_MAGIC_VERSION");
  need((p[10]==1||p[10]==2)&&p[11]==0&&get32(p+12)==header_bytes&&get32(p+24)==1&&get32(p+28)==0&&get64(p+56)==0,"WIRE_KIND_LAYOUT_RESERVED");
  Header h;h.kind=p[10];h.features=get32(p+16);h.rows=get32(p+20);h.bytes=get64(p+32);h.generation=get64(p+40);h.sequence=get64(p+48);
  need(h.features>=1&&h.features<=32&&h.rows>=1&&h.rows<=2048&&h.generation!=0&&h.sequence>=1&&h.sequence<=1000000,"WIRE_DIMENSION_GENERATION_SEQUENCE");
  need(h.bytes==(h.kind==1?uint64_t(h.features)*ct_bytes:ct_bytes),"WIRE_PAYLOAD_LENGTH");
  Digest* ds[]={&h.profile_id,&h.model_id,&h.key_id,&h.request_id,&h.session_id,&h.request_hash};
  for(size_t i=0;i<6;i++)std::copy(p+64+32*i,p+96+32*i,ds[i]->begin());
  need(equal(h.profile_id,profile())&&nonzero(h.key_id)&&nonzero(h.request_id)&&nonzero(h.session_id)&&nonzero(h.request_hash),"WIRE_PROFILE_OR_EMPTY_ID");
  const auto& m=model_digest(h.model_id);need(h.features==m.features.size(),"WIRE_MODEL_DIMENSIONS");return h;
}
inline Digest request_id(Header h){h.request_id={};h.request_hash={};h.kind=1;h.bytes=uint64_t(h.features)*ct_bytes;const auto b=encode(h);return digest(b.data(),b.size());}
inline Digest request_hash(Header h,const std::vector<uint8_t>& payload){h.request_hash={};const auto b=encode(h);return digest(b.data(),b.size(),payload.data(),payload.size());}
inline void canonical(const uint32_t* words,size_t count){
  need(words&&count>0&&count%ct_words==0&&count<=32*ct_words,"WIRE_WORD_COUNT");uint32_t bad=0;
  for(size_t i=0;i<count;i++){bad|=uint32_t(words[i]>=q[(i/N)%3]);}
  need(bad==0,"WIRE_NONCANONICAL");
}
inline std::vector<uint8_t> pack(const std::vector<uint32_t>& padded,size_t features){
  need(features>0&&features<=32&&padded.size()==((features+3)/4*4)*ct_words,"WIRE_PADDED_LENGTH");canonical(padded.data(),padded.size());
  need(std::all_of(padded.begin()+features*ct_words,padded.end(),[](uint32_t v){return v==0;}),"WIRE_NONZERO_PADDING");
  std::vector<uint8_t> bytes(features*ct_bytes);for(size_t i=0;i<features*ct_words;i++)put32(bytes.data()+4*i,padded[i]);return bytes;
}
inline std::vector<uint32_t> unpack(const std::vector<uint8_t>& bytes,size_t features,bool pad){
  need(features>0&&features<=32&&bytes.size()==features*ct_bytes,"WIRE_BODY_LENGTH");
  std::vector<uint32_t> words((pad?(features+3)/4*4:features)*ct_words,0);
  for(size_t i=0;i<features*ct_words;i++){words[i]=get32(bytes.data()+4*i);}
  canonical(words.data(),words.size());return words;
}
inline std::vector<uint8_t> pack_response(const std::vector<uint32_t>& words){
  need(words.size()==ct_words,"WIRE_RESPONSE_LENGTH");canonical(words.data(),words.size());
  std::vector<uint8_t> b(ct_bytes);for(size_t i=0;i<ct_words;i++)put32(b.data()+4*i,words[i]);return b;
}
inline void validate_response(const Header& response,const Header& request){
  Header expected=request;expected.kind=2;expected.bytes=ct_bytes;
  need(encode(response)==encode(expected),"WIRE_RESPONSE_BINDING");
}
class ConnectionSequence {
  Digest session_;bool bound_=false;Digest key_{};uint64_t generation_=0,next_=1;
 public:
  explicit ConnectionSequence(Digest channel):session_(channel){need(nonzero(channel),"CHANNEL_BINDING_EMPTY");}
  void header(const Header& h)const{
    need(h.kind==1&&h.sequence==next_&&equal(h.session_id,session_),"WIRE_REPLAY_OR_CHANNEL");
    need(equal(h.request_id,request_id(h)),"WIRE_REQUEST_ID");
    if(bound_)need(equal(h.key_id,key_)&&h.generation==generation_,"WIRE_KEY_GENERATION_CHANGE");
  }
  void consume(const Header& h,const std::vector<uint8_t>& body){
    header(h);need(body.size()==h.bytes&&equal(h.request_hash,request_hash(h,body)),"WIRE_REQUEST_DIGEST");
    key_=h.key_id;generation_=h.generation;bound_=true;++next_;
  }
};
}
