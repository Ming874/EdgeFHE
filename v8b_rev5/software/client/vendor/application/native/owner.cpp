// O5 (2026-10-24): adds ef_owner_decrypt_concurrent - decryption from ONE dedicated thread (bound on first call) with its
// own worker pool and Decryptor scratch, so the owner thread keeps encrypting meanwhile. It reads only the immutable
// key-derived tables (NttSet, SecretTransform, Fft); the arithmetic is the same Decryptor::run, hence bit-identical to
// ef_owner_decrypt. Every other function is unchanged.
// app v5 (2026-10-22): same exported API and arithmetic as app_v4; encrypt/decrypt run on the v5 engine of
// owner_v5_core.hpp (decrypt fused/parallel, CPU sampler on 3 lanes, PL frames pipelined with a helper thread, padding
// features not encrypted -> zero words). Everything below that is not the v5 engine is the app_v4 text.
// Isolated application adapter, app v3 candidate (2026-10-19). Frozen SDK arithmetic/driver/HLS sources unchanged;
// the app-local fast math (edgefhe_fast.hpp, owner_fast_core.hpp) replaces the PS hot paths:
//  * encode: N/2-point special FFT, 3 lanes on the owner's encode pool (overlaps the previous group's ENC as before);
//  * CPU backend encrypt: Harvey NTT with cached public-key spectra, limbs on the owner's math pool (bit-identical);
//  * PL / spec backend: ENC without output zero-fill/unpack; one fused transpose+canonical-check into the caller buffer;
//  * decrypt: NTT(s) cached per Context (secret-derived, wiped on destroy), limbs on the math pool, fast decode;
//  * ef_owner_error_bound: B04 per-key precision certificate (owner-only; never sent to the cloud).
// Arithmetic results: ciphertext words and the decrypted polynomial are bit-identical to v2 for the same seed and
// material; decoded values differ from v2 only by float rounding (< 1e-12, more accurate than v2).
#include "fast_encrypt.hpp"
#include "owner_v5_core.hpp"
#include "owner_fast_core.hpp"
#include "precision_certificate.hpp"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <memory>
#include <mutex>
#include <openssl/sha.h>
#include <string>
#include <future>
#include <thread>
using namespace edgefhe;
namespace v8=edgefhe_v8_v5;
namespace {
constexpr size_t WORDS=6*degree;
thread_local std::string error;
std::mutex initialization;
std::string initialized_plugin;
constexpr const char* PL_NODE="/dev/edgefhe_v8b_dma";
struct Context {
  std::thread::id owner=std::this_thread::get_id();
  Engine engine;                       // keygen only (frozen keygen_secure)
  WorkerPool math;                     // CPU encrypt limbs and decrypt limbs (owner thread only)
  WorkerPool encode_pool;              // encode lanes (encoding thread only)
  SecureMaterial material;PublicKey key;
  fast::NttSet set;fast::Fft fft;fast::SecretTransform sk;v3::KeySpectra spectra;b04::KeyCertificate certificate;
  std::array<std::vector<uint32_t>,3> scratch;std::array<std::vector<std::complex<double>>,3> enc_scratch;
  std::unique_ptr<v8::SpecModel> spec;std::unique_ptr<v8::Node> node;std::unique_ptr<v8::Device> device;
  std::unique_ptr<v5::Engine> v5engine;
  unsigned workers_;std::mutex dlock;std::thread::id dthread{};std::unique_ptr<WorkerPool> dmath;std::unique_ptr<v5::Decryptor> ddec;   // O5
  Context(unsigned workers,unsigned backend):engine(workers),math(workers),encode_pool(workers),material(sample_secure_material()),key(keygen_secure(engine,material)),workers_(workers){
    sk.build(set,material.secret);spectra.build(set,key);v5engine.reset(new v5::Engine(set,fft,spectra,math,encode_pool));
    certificate=b04::key_certificate(material.error,material.secret);
    for(auto& s:scratch)s.assign(3*degree,0);
    if(backend==1){spec.reset(new v8::SpecModel);device.reset(new v8::Device([this](edgefhe_v8_request& r){return spec->run(r);}));device->load(key);}
    if(backend==2){node.reset(new v8::Node(PL_NODE));device.reset(new v8::Device([this](edgefhe_v8_request& r){return node->run(r);}));device->load(key);}
  }
  ~Context(){ddec.reset();dmath.reset();sk.wipe();for(auto& s:scratch)explicit_bzero(s.data(),s.size()*4);certificate={};}
  void check_owner()const{require(owner==std::this_thread::get_id(),"owner context used from another thread");}
};
}
extern "C" {
const char* ef_owner_error(){return error.c_str();}
void* ef_owner_create(unsigned workers,unsigned backend,const char* os_plugin){try{
  require((workers==1||workers==3)&&backend<=2&&os_plugin,"owner configuration: backend 0 CPU, 1 spec, 2 PL");
  {std::lock_guard<std::mutex> lock(initialization);if(initialized_plugin.empty()){initialize_secure_sampler(os_plugin);initialized_plugin=os_plugin;}else require(initialized_plugin==os_plugin,"one pinned OS plugin per process");}
  auto* c=new Context(workers,backend);error.clear();return c;
}catch(const std::exception& e){error=e.what();return nullptr;}}
void ef_owner_destroy(void* p){delete static_cast<Context*>(p);}
int ef_owner_device_calls(void* ptr,uint64_t* calls){try{require(ptr&&calls,"null device calls");auto& c=*static_cast<Context*>(ptr);c.check_owner();
  *calls=c.device?c.device->calls():0;return 0;}catch(const std::exception& e){error=e.what();return -1;}}
int ef_owner_pl_hz(void* ptr,uint32_t* hz){try{require(ptr&&hz,"null pl hz");auto& c=*static_cast<Context*>(ptr);c.check_owner();
  *hz=c.node?c.node->pl_hz:0;return 0;}catch(const std::exception& e){error=e.what();return -1;}}
int ef_owner_key_id(void* ptr,uint8_t* digest){try{require(ptr&&digest,"null key id");auto& c=*static_cast<Context*>(ptr);c.check_owner();const auto pk=v8::load_payload(c.key);SHA256(reinterpret_cast<const uint8_t*>(pk.data()),pk.size()*4,digest);return 0;}catch(const std::exception& e){error=e.what();return -1;}}
int ef_owner_encrypt(void* ptr,const double* values,size_t rows,size_t features,uint32_t* out,size_t out_words){try{
  require(ptr&&values&&out,"null owner encrypt buffer");require(rows>0&&rows<=slots&&features>0&&features<=32,"owner input dimensions");
  const size_t padded=(features+3)/4*4;require(out_words==padded*WORDS,"owner output dimensions");
  auto& c=*static_cast<Context*>(ptr);c.check_owner();
  for(size_t i=0;i<rows*features;i++)require(std::isfinite(values[i])&&std::abs(values[i])<=8,"normalized feature outside profile");
  const v5::SeedSource os=[](uint8_t* seed){sampler_b::os_seed(seed);};
  if(c.device)c.v5engine->encrypt_device(*c.device,values,rows,features,out,os);
  else c.v5engine->encrypt_cpu(values,rows,features,out,os);
  error.clear();return 0;
}catch(const std::exception& e){error=e.what();return -1;}}
int ef_owner_decrypt(void* ptr,const uint32_t* in,size_t in_words,size_t rows,double* values){try{
  require(ptr&&in&&values&&in_words==WORDS&&rows>0&&rows<=slots,"owner decrypt dimensions");auto& c=*static_cast<Context*>(ptr);c.check_owner();
  uint32_t bad=0;
  for(size_t p=0;p<2;p++)for(size_t l=0;l<3;l++){const uint32_t q=primes[l];const uint32_t* x=in+p*3*degree+l*degree;for(size_t i=0;i<degree;i++)bad|=uint32_t(x[i]>=q);}
  require(bad==0,"Noncanonical RNS residue");
  c.v5engine->decryptor.run(c.math,c.set,c.sk,c.fft,in,rows,values);
  error.clear();return 0;
}catch(const std::exception& e){error=e.what();return -1;}}
int ef_owner_decrypt_concurrent(void* ptr,const uint32_t* in,size_t in_words,size_t rows,double* values){try{
  require(ptr&&in&&values&&in_words==WORDS&&rows>0&&rows<=slots,"owner decrypt dimensions");auto& c=*static_cast<Context*>(ptr);
  {std::lock_guard<std::mutex> g(c.dlock);
   if(!c.ddec){c.dthread=std::this_thread::get_id();c.dmath.reset(new WorkerPool(c.workers_));c.ddec.reset(new v5::Decryptor);}
   require(c.dthread==std::this_thread::get_id(),"concurrent decrypt is bound to one thread");}
  uint32_t bad=0;
  for(size_t p=0;p<2;p++)for(size_t l=0;l<3;l++){const uint32_t q=primes[l];const uint32_t* x=in+p*3*degree+l*degree;for(size_t i=0;i<degree;i++)bad|=uint32_t(x[i]>=q);}
  require(bad==0,"Noncanonical RNS residue");
  c.ddec->run(*c.dmath,c.set,c.sk,c.fft,in,rows,values);
  error.clear();return 0;
}catch(const std::exception& e){error=e.what();return -1;}}
// B04 per-key precision certificate for a public scalar affine model: total decoded-error bound per decryption with
// failure probability 2^-failure_log2 (owner-only value).
int ef_owner_error_bound(void* ptr,const int64_t* weights_int,const double* weights,size_t features,int64_t bias_int,double bias,
                         double input_abs_max,double failure_log2,double* bound){try{
  require(ptr&&weights_int&&weights&&bound&&features>0&&features<=32,"error bound arguments");auto& c=*static_cast<Context*>(ptr);c.check_owner();
  const auto b=b04::affine_bound(c.certificate,std::vector<int64_t>(weights_int,weights_int+features),bias_int,
                                 std::vector<double>(weights,weights+features),bias,input_abs_max,failure_log2);
  *bound=b.total;error.clear();return 0;
}catch(const std::exception& e){error=e.what();return -1;}}
}
