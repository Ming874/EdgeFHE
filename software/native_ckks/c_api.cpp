#include "core.hpp"
#include <algorithm>
#include <cstring>
#include <memory>
#include <string>
using namespace edgefhe;
namespace {
thread_local std::string last_error;
template<class F> int checked(F&& call) { try { call(); last_error.clear(); return 0; }
  catch(const std::exception& e) { last_error=e.what(); return -1; }
  catch(...) { last_error="Unknown native failure"; return -1; } }
Engine& engine(void* context) { require(context!=nullptr,"Null native context"); return *static_cast<Engine*>(context); }
Rns rns(const uint32_t* p) { require(p!=nullptr,"Null RNS pointer"); Rns a;
  for(size_t l=0;l<3;l++) a[l]=Poly(p+l*degree,p+(l+1)*degree);
  validate(a); return a; }
void write(const Rns& a,uint32_t* p) { require(p!=nullptr,"Null RNS output"); validate(a);
  for(size_t l=0;l<3;l++) std::copy(a[l].begin(),a[l].end(),p+l*degree); }
Ternary ternary(const int8_t* p) { require(p!=nullptr,"Null ternary pointer"); return Ternary(p,p+degree); }
Message message(const int64_t* p) { require(p!=nullptr,"Null message pointer"); return Message(p,p+degree); }
Material material(const int8_t* p,const uint32_t* a) {
  Material m; m.secret=ternary(p); m.error=ternary(p+degree); m.a=rns(a);
  for(size_t f=0;f<4;f++) { m.mask[f]=ternary(p+(2+f)*degree); m.e0[f]=ternary(p+(6+f)*degree); m.e1[f]=ternary(p+(10+f)*degree); }
  return m;
}
}
struct WideWords { uint64_t low; int64_t high; };
static_assert(sizeof(WideWords)==16,"Wide ABI mismatch");
extern "C" {
const char* ef_error() { return last_error.c_str(); }
const char* ef_version() { return "edgefhe-native-ckks-v4-s29-candidate-3"; }
void* ef_create(unsigned workers,int fd) { void* result=nullptr;
  checked([&]{ result=new Engine(workers,fd); }); return result; }
void ef_destroy(void* context) { delete static_cast<Engine*>(context); }
int ef_round(double value,int64_t* out) { return checked([&]{ require(out!=nullptr,"Null rounding output"); *out=nearest_even(value); }); }
int ef_encode(const double* interleaved,unsigned count,uint64_t delta,int64_t* out) {
  return checked([&]{ require(count<=slots&&out!=nullptr&&(count==0||interleaved!=nullptr),"Invalid slot pointers/count");
    std::vector<std::complex<double>> values; for(unsigned i=0;i<count;i++) values.emplace_back(interleaved[2*i],interleaved[2*i+1]);
    const auto m=encode(values,delta); std::copy(m.begin(),m.end(),out); });
}
int ef_decode(const WideWords* in,uint64_t delta,double* out) {
  return checked([&]{ require(in&&out,"Null decode pointer"); std::vector<Wide> m(degree);
    for(size_t i=0;i<degree;i++) { const UWide bits=(UWide(uint64_t(in[i].high))<<64)|in[i].low; std::memcpy(&m[i],&bits,sizeof(bits)); }
    const auto result=decode(m,delta); for(size_t i=0;i<slots;i++) { out[2*i]=result[i].real(); out[2*i+1]=result[i].imag(); } });
}
int ef_sample(int8_t* out,uint32_t* a) {
  return checked([&]{ require(out&&a,"Null sampler output"); const auto m=sample_material();
    std::copy(m.secret.begin(),m.secret.end(),out); std::copy(m.error.begin(),m.error.end(),out+degree);
    for(size_t f=0;f<4;f++) { std::copy(m.mask[f].begin(),m.mask[f].end(),out+(2+f)*degree);
      std::copy(m.e0[f].begin(),m.e0[f].end(),out+(6+f)*degree); std::copy(m.e1[f].begin(),m.e1[f].end(),out+(10+f)*degree); } write(m.a,a); });
}
int ef_sample_encryption(int8_t* out) {
  return checked([&]{ require(out!=nullptr,"Null sampler output"); const auto m=sample_encryption();
    for(size_t f=0;f<12;f++) std::copy(m[f].begin(),m[f].end(),out+f*degree); });
}
int ef_multiply(void* c,const uint32_t* a,const uint32_t* b,uint32_t* out) {
  return checked([&]{ write(engine(c).multiply(rns(a),rns(b)),out); });
}
int ef_keygen(void* c,const int8_t* samples,const uint32_t* a,uint32_t* b) {
  return checked([&]{ write(engine(c).keygen(material(samples,a)).b,b); });
}
int ef_encrypt(void* c,const uint32_t* b,const uint32_t* a,const int64_t* messages,const int8_t* samples,uint32_t* out) {
  return checked([&]{ require(messages&&samples&&out,"Null encryption pointer");
    std::array<Message,4> m; std::array<Ternary,4> u,e0,e1;
    for(size_t f=0;f<4;f++) { m[f]=message(messages+f*degree); u[f]=ternary(samples+f*degree);
      e0[f]=ternary(samples+(4+f)*degree); e1[f]=ternary(samples+(8+f)*degree); }
    const auto result=engine(c).encrypt({rns(b),rns(a)},m,u,e0,e1);
    for(size_t f=0;f<4;f++) { write(result[f].c0,out+f*3*degree); write(result[f].c1,out+(4+f)*3*degree); } });
}
int ef_decrypt(void* c,const uint32_t* in,const int8_t* secret,WideWords* out) {
  return checked([&]{ require(in&&secret&&out,"Null decryption pointer");
    const auto result=engine(c).decrypt({rns(in),rns(in+3*degree)},ternary(secret));
    for(size_t i=0;i<degree;i++) { const auto bits=UWide(result[i]); out[i].low=uint64_t(bits);
      const uint64_t high=uint64_t(bits>>64); std::memcpy(&out[i].high,&high,sizeof(high)); } });
}
int ef_affine(void* c,const uint32_t* in,const int64_t* weights,const int64_t* bias,uint32_t* out) {
  return checked([&]{ require(in&&weights&&bias&&out,"Null affine pointer"); std::array<Cipher,4> cipher; std::array<Message,4> w;
    for(size_t f=0;f<4;f++) { cipher[f]={rns(in+f*3*degree),rns(in+(4+f)*3*degree)}; w[f]=message(weights+f*degree); }
    const auto result=engine(c).affine(cipher,w,message(bias)); write(result.c0,out); write(result.c1,out+3*degree); });
}
int ef_no_wrap(const int64_t* messages,const int64_t* weights,const int64_t* bias,WideWords* out) {
  return checked([&]{ require(messages&&weights&&bias&&out,"Null no-wrap pointer");
    std::array<Message,4> m,w;for(size_t f=0;f<4;f++) { m[f]=message(messages+f*degree);w[f]=message(weights+f*degree); }
    const auto b=no_wrap_bound(m,w,message(bias));out->low=uint64_t(b);out->high=int64_t(b>>64); });
}
int ef_error_bound(const double* weights,double input_abs_max,double* out) {
  return checked([&]{ require(weights&&out,"Null bound pointer");std::array<double,4> w;
    std::copy(weights,weights+4,w.begin());*out=affine_error_bound(w,input_abs_max); });
}
int ef_decide(const double* logits,unsigned count,double bound,double* probability,int8_t* decision) {
  return checked([&]{ require(logits&&probability&&decision&&count>=1&&count<=slots,"Invalid decision pointers/count");
    for(unsigned i=0;i<count;i++) { probability[i]=sigmoid(logits[i]);decision[i]=int8_t(classify(logits[i],bound)); } });
}
}
