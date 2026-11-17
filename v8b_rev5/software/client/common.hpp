#pragma once
#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>
#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/rand.h>
namespace efnet {
inline void need(bool ok,const char* error){if(!ok)throw std::runtime_error(error);}
using Clock=std::chrono::steady_clock;
inline uint64_t now(){return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count());}
using Digest=std::array<uint8_t,32>;
inline bool equal(const Digest& a,const Digest& b){return CRYPTO_memcmp(a.data(),b.data(),a.size())==0;}
inline bool nonzero(const Digest& a){uint8_t x=0;for(auto b:a)x|=b;return x!=0;}
inline Digest digest(const uint8_t* a,size_t n,const uint8_t* b=nullptr,size_t m=0){
  Digest out{};unsigned length=0;EVP_MD_CTX* p=EVP_MD_CTX_new();need(p!=nullptr,"HASH_ALLOC");
  const bool ok=EVP_DigestInit_ex(p,EVP_sha256(),nullptr)==1&&EVP_DigestUpdate(p,a,n)==1&&(!m||EVP_DigestUpdate(p,b,m)==1)&&EVP_DigestFinal_ex(p,out.data(),&length)==1;
  EVP_MD_CTX_free(p);need(ok&&length==out.size(),"HASH_FAILED");return out;
}
inline Digest digest(const std::string& text){return digest(reinterpret_cast<const uint8_t*>(text.data()),text.size());}
inline Digest unhex(const std::string& text){
  need(text.size()==64,"DIGEST_HEX_LENGTH");Digest d{};
  auto nibble=[](char c)->unsigned{if(c>='0'&&c<='9')return unsigned(c-'0');if(c>='a'&&c<='f')return unsigned(c-'a')+10;throw std::runtime_error("DIGEST_HEX_LOWERCASE");};
  for(size_t i=0;i<32;i++){d[i]=uint8_t(nibble(text[2*i])*16+nibble(text[2*i+1]));}
  return d;
}
inline std::string hex(const Digest& d){constexpr char h[]="0123456789abcdef";std::string s;for(auto b:d){s+=h[b>>4];s+=h[b&15];}return s;}
inline Digest random_digest(){Digest d{};need(RAND_bytes(d.data(),int(d.size()))==1&&nonzero(d),"OS_RANDOM_FAILED");return d;}
inline uint64_t number(const std::string& s,uint64_t lo,uint64_t hi){uint64_t n=0;const auto r=std::from_chars(s.data(),s.data()+s.size(),n);need(r.ec==std::errc{}&&r.ptr==s.data()+s.size()&&n>=lo&&n<=hi,"OPTION_NUMBER_RANGE");return n;}
class Options {
  std::map<std::string,std::string> values_;
 public:
  Options(int argc,char** argv){for(int i=1;i<argc;i+=2){need(i+1<argc,"OPTION_VALUE_MISSING");std::string k=argv[i];need(k.rfind("--",0)==0&&values_.emplace(k,argv[i+1]).second,"OPTION_DUPLICATE");}}
  std::string take(const std::string& key,const std::string& fallback=""){auto p=values_.find(key);if(p==values_.end())return fallback;std::string v=p->second;values_.erase(p);return v;}
  std::string required(const std::string& key){auto v=take(key);need(!v.empty(),"OPTION_REQUIRED");return v;}
  void finish(){need(values_.empty(),"OPTION_UNKNOWN");}
};
template<class T>struct Secret {
  std::vector<T> v;
  explicit Secret(size_t n=0):v(n){}
  ~Secret(){wipe();}
  Secret(const Secret&)=delete;Secret& operator=(const Secret&)=delete;
  void wipe()noexcept{if(!v.empty())OPENSSL_cleanse(v.data(),v.size()*sizeof(T));}
};
}
