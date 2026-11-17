#include "core.hpp"
#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cfenv>
#include <exception>
#include <limits>
#include <stdexcept>
#include <thread>
#include <sys/random.h>
#include <sys/ioctl.h>

namespace edgefhe {
void require(bool ok,const char* message) { if(!ok) throw std::invalid_argument(message); }
static uint32_t mul(uint32_t a,uint32_t b,uint32_t q) { return uint64_t(a)*b%q; }
static uint32_t checked_modulus(uint32_t q) {
  require(std::find(primes.begin(),primes.end(),q)!=primes.end(),"Unsupported fixed-profile modulus");return q;
}
static uint32_t power(uint32_t a,uint32_t n,uint32_t q) {
  uint32_t r=1; for(;n;n>>=1,a=mul(a,a,q)) if(n&1) r=mul(r,a,q); return r;
}
void validate(const Rns& value) {
  for(size_t l=0;l<3;l++) {
    require(value[l].size()==degree,"RNS degree mismatch");
    for(auto x:value[l]) require(x<primes[l],"Noncanonical RNS residue");
  }
}
void validate_ternary(const Ternary& value,size_t weight) {
  require(value.size()==degree,"Ternary degree mismatch"); size_t count=0;
  for(auto x:value) { require(x>=-1&&x<=1,"Nonternary sample"); count+=x!=0; }
  require(count==weight,"Sampler weight mismatch");
}
int64_t nearest_even(double value) {
  require(std::isfinite(value)&&std::abs(value)<0x1p63,"Invalid or oversized encoded coefficient");
  // Independent of the caller's floating-point rounding mode. Python round uses ties to even.
  const double lower=std::floor(value),fraction=value-lower;
  const auto integer=static_cast<int64_t>(lower);
  return integer+((fraction>0.5||(fraction==0.5&&(integer%2)!=0))?1:0);
}
UWide no_wrap_bound(const std::array<Message,4>& message,const std::array<Message,4>& weight,const Message& bias) {
  const UWide limit=UWide(primes[0])*primes[1]*primes[2]/2;
  auto magnitude=[](int64_t x) { return x<0?UWide(-Wide(x)):UWide(x); };
  auto l1=[&](const Message& p) { require(p.size()==degree,"No-wrap degree mismatch"); UWide sum=0;
    for(auto x:p) { sum+=magnitude(x); }
    return sum; };
  require(bias.size()==degree,"No-wrap bias degree mismatch");UWide bound=0;
  for(auto x:bias) bound=std::max(bound,magnitude(x));
  require(bound<=limit,"Bias may wrap centered CRT");
  for(size_t f=0;f<4;f++) {
    const UWide a=l1(message[f])+2064,b=l1(weight[f]);
    // Compare before multiplying, so a malformed input cannot overflow 128 bits.
    require(b==0||a<=(limit-bound)/b,"Affine may wrap centered CRT");bound+=a*b;
  }
  return bound; // Q is odd: 2*bound < Q exactly when bound <= floor(Q/2).
}
double affine_error_bound(const std::array<double,4>& weights,double input_abs_max) {
  require(std::isfinite(input_abs_max)&&input_abs_max>0&&input_abs_max<=16,"Invalid input magnitude bound");
  const double ex=degree/(2.*scale)+2064./scale+1e-8,ew=degree/(2.*scale)+1e-8;
  double out=degree/(2.*double(scale)*scale)+2e-8;
  for(auto w:weights) { require(std::isfinite(w)&&std::abs(w)<=16,"Invalid model weight");
    out+=std::abs(w)*ex+input_abs_max*ew+ex*ew; }
  return out;
}
double sigmoid(double value) {
  require(std::isfinite(value),"Nonfinite sigmoid input");
  if(value>=0) return 1/(1+std::exp(-value));
  const auto e=std::exp(value);return e/(1+e);
}
int classify(double value,double bound) {
  require(std::isfinite(value)&&std::isfinite(bound)&&bound>=0,"Invalid classification interval");
  return value-bound>0?1:value+bound<0?0:-1;
}
static std::vector<std::complex<double>> fft(std::vector<std::complex<double>> value,bool inverse) {
  const size_t n=value.size();
  for(size_t i=1,j=0;i<n;i++) {
    size_t bit=n/2; for(;j&bit;bit>>=1) j^=bit; j^=bit;
    if(i<j) std::swap(value[i],value[j]);
  }
  const double pi=std::acos(-1.0);
  for(size_t length=2;length<=n;length*=2) {
    const auto root=std::polar(1.0,(inverse?1.0:-1.0)*2*pi/double(length));
    for(size_t start=0;start<n;start+=length) {
      std::complex<double> w(1,0);
      for(size_t j=0;j<length/2;j++,w*=root) {
        const auto a=value[start+j],b=value[start+j+length/2]*w;
        value[start+j]=a+b; value[start+j+length/2]=a-b;
      }
    }
  }
  if(inverse) for(auto& x:value) x/=double(n);
  return value;
}
static const std::array<std::complex<double>,degree>& encoding_twist() {
  // Use exactly the original expression, not a recurrence or a new approximation.
  // C++11 function-local initialization is synchronized across caller threads.
  static const auto table=[] {
    std::array<std::complex<double>,degree> out{};
    const auto zeta=std::polar(1.0,std::acos(-1.0)/double(degree));
    for(size_t j=0;j<degree;j++) out[j]=std::pow(zeta,double(j));
    return out;
  }();
  return table;
}
Message encode(const std::vector<std::complex<double>>& values,uint64_t delta) {
  require(std::fegetround()==FE_TONEAREST,"Encoder requires FE_TONEAREST");
  require(values.size()<=slots&&(delta==scale||delta==scale*scale),"Invalid encoder dimensions/scale");
  std::vector<std::complex<double>> evaluation(degree);
  for(size_t k=0;k<values.size();k++) {
    require(std::isfinite(values[k].real())&&std::isfinite(values[k].imag())&&std::abs(values[k])<=16,
        "Slots must be finite with magnitude <=16");
    evaluation[k]=values[k]; evaluation[degree-1-k]=std::conj(values[k]);
  }
  auto frequency=fft(std::move(evaluation),false); Message result(degree);
  const auto& twist=encoding_twist();
  for(size_t j=0;j<degree;j++) {
    const auto c=frequency[j]/double(degree)/twist[j];
    require(std::abs(c.imag())<=1e-8,"Canonical interpolation imaginary residue");
    result[j]=nearest_even(c.real()*double(delta));
  }
  return result;
}
std::array<Message,4> encode_features(const std::array<std::vector<std::complex<double>>,4>& values,uint64_t delta,unsigned workers) {
  require(workers==1||workers==3,"Batch encoder workers must be 1 or 3");
  require(std::fegetround()==FE_TONEAREST,"Batch encoder requires FE_TONEAREST");
  require(delta==scale||delta==scale*scale,"Batch encoder scale mismatch");
  for(const auto& feature:values) {
    require(feature.size()<=slots,"Batch encoder slot count");
    for(const auto& z:feature)require(std::isfinite(z.real())&&std::isfinite(z.imag())&&std::abs(z)<=16,"Invalid batch encoder value");
  }
  std::array<Message,4> result;
  if(workers==1) {
    for(size_t f=0;f<4;f++)result[f]=encode(values[f],delta);
    return result;
  }
  std::array<std::exception_ptr,3> error{};
  auto task=[&](size_t lane) {
    try {
      for(size_t f=lane;f<4;f+=3)result[f]=encode(values[f],delta);
    }catch(...){error[lane]=std::current_exception();}
  };
  // Exactly two helper threads plus this caller. Every started thread is joined.
  std::thread first([&]{task(1);});
  try{std::thread second([&]{task(2);});task(0);second.join();}
  catch(...){first.join();throw;}
  first.join();
  for(auto e:error)if(e)std::rethrow_exception(e);
  return result;
}
std::vector<std::complex<double>> decode(const std::vector<Wide>& coefficient,uint64_t delta) {
  require(std::fegetround()==FE_TONEAREST,"Decoder requires FE_TONEAREST");
  require(coefficient.size()==degree&&(delta==scale||delta==scale*scale),"Invalid decoder dimensions/scale");
  const auto& twist=encoding_twist();
  std::vector<std::complex<double>> value(degree);
  for(size_t j=0;j<degree;j++) value[j]=(double(coefficient[j])/double(delta))*twist[j];
  value=fft(std::move(value),true); value.resize(slots);
  for(auto& x:value) x*=double(degree);
  return value;
}
template<typename T> static Rns residue_impl(const std::vector<T>& value) {
  require(value.size()==degree,"Integer polynomial degree mismatch"); Rns result;
  for(size_t l=0;l<3;l++) {
    result[l].resize(degree);
    for(size_t i=0;i<degree;i++) {
      const int64_t r=int64_t(value[i])%int64_t(primes[l]);
      result[l][i]=uint32_t(r<0?r+primes[l]:r);
    }
  } return result;
}
Rns residues(const Message& value) { return residue_impl(value); }
Rns residues(const Ternary& value) { return residue_impl(value); }
Rns add(const Rns& a,const Rns& b) {
  validate(a); validate(b); Rns out;
  for(size_t l=0;l<3;l++) { out[l].resize(degree); for(size_t i=0;i<degree;i++) {
    auto r=a[l][i]+b[l][i]; out[l][i]=r>=primes[l]?r-primes[l]:r;
  }} return out;
}
std::vector<Wide> centered(const Rns& value) {
  validate(value);
  constexpr uint64_t q0=primes[0],q1=primes[1],q2=primes[2],q01=q0*q1;
  constexpr Wide q=Wide(q01)*q2;
  static const uint32_t inv01=power(uint32_t(q0%q1),uint32_t(q1-2),uint32_t(q1));
  static const uint32_t inv012=power(uint32_t(q01%q2),uint32_t(q2-2),uint32_t(q2));
  // Garner: x01 < q0*q1 < 2^60, d1*inv01 < 2^60,
  // d2*inv012 < 2^58, x < Q < 2^89. Only the final assembly is 128-bit.
  // q0 < q1 is a required property of this fixed profile.
  static_assert(q0<q1 && q01<(uint64_t{1}<<60),"Garner profile bounds");
  std::vector<Wide> result(degree);
  for(size_t i=0;i<degree;i++) {
    const uint64_t d1=(uint64_t(value[1][i])+q1-value[0][i])%q1;
    const uint64_t t1=(d1*inv01)%q1;
    const uint64_t x01=uint64_t(value[0][i])+q0*t1;
    const uint64_t d2=(uint64_t(value[2][i])+q2-x01%q2)%q2;
    const uint64_t t2=(d2*inv012)%q2;
    const Wide x=Wide(x01)+Wide(q01)*t2;
    result[i]=x>q/2?x-q:x;
  } return result;
}
class OsRandom {
  std::array<uint32_t,1024> buffer_{}; size_t cursor_=buffer_.size();
 public:
  ~OsRandom() { volatile uint32_t* p=buffer_.data(); for(size_t i=0;i<buffer_.size();i++) p[i]=0; }
  uint32_t word() {
    if(cursor_==buffer_.size()) {
      auto* p=reinterpret_cast<unsigned char*>(buffer_.data()); size_t remaining=sizeof(buffer_);
      while(remaining) { const auto n=getrandom(p,remaining,0);
        if(n<0&&errno==EINTR) continue;
        if(n<=0) throw std::runtime_error("OS getrandom failed; no PRNG fallback");
        p+=n; remaining-=size_t(n);
      } cursor_=0;
    } return buffer_[cursor_++];
  }
  uint32_t below(uint32_t bound) {
    require(bound>0,"Empty random interval");
    const uint32_t threshold=uint32_t(-bound)%bound;
    for(;;) { auto x=word(); if(x>=threshold) return x%bound; }
  }
  Ternary sparse(size_t weight) {
    Ternary p(degree); size_t count=0;
    while(count<weight) { auto i=below(degree); if(p[i]) continue;
      p[i]=below(2)?1:-1; count++; }
    return p;
  }
};
Material sample_material() {
  OsRandom rng; Material m; m.secret=rng.sparse(64); m.error=rng.sparse(16);
  for(size_t i=0;i<4;i++) { m.mask[i]=rng.sparse(64); m.e0[i]=rng.sparse(16); m.e1[i]=rng.sparse(16); }
  for(size_t l=0;l<3;l++) { m.a[l].resize(degree); for(auto& x:m.a[l]) x=rng.below(primes[l]); }
  return m;
}
std::array<Ternary,12> sample_encryption() {
  OsRandom rng; std::array<Ternary,12> result;
  for(size_t i=0;i<12;i++) result[i]=rng.sparse(i<4?64:16);
  return result;
}
Plan::Plan(uint32_t modulus,uint32_t root_psi):q(checked_modulus(modulus)),n_inverse(power(degree,q-2,q)),reciprocal(uint64_t((UWide(1)<<64)/q)),twist(degree),untwist(degree),twist_shoup(degree),untwist_shoup(degree) {
  require(q>2&&q<(uint32_t(1)<<30),"Unsupported modulus bounds");
  require(power(root_psi,degree,q)==q-1&&power(root_psi,2*degree,q)==1,"Invalid negacyclic root");
  const auto omega=mul(root_psi,root_psi,q),inverse_omega=power(omega,q-2,q),inverse_psi=power(root_psi,q-2,q);
  twist[0]=untwist[0]=1;
  for(size_t i=1;i<degree;i++) { twist[i]=mul(twist[i-1],root_psi,q); untwist[i]=mul(untwist[i-1],inverse_psi,q); }
  for(size_t i=0;i<degree;i++) {
    untwist[i]=mul(untwist[i],n_inverse,q); // Fuse inverse normalization with untwist.
    twist_shoup[i]=(uint64_t(twist[i])<<32)/q;untwist_shoup[i]=(uint64_t(untwist[i])<<32)/q;
  }
  for(size_t s=0,h=1;s<12;s++,h*=2) {
    root[s].resize(h); inverse_root[s].resize(h); shoup[s].resize(h); inverse_shoup[s].resize(h);
    root[s][0]=inverse_root[s][0]=1;
    const auto w=power(omega,degree/(2*h),q),iw=power(inverse_omega,degree/(2*h),q);
    for(size_t j=1;j<h;j++) { root[s][j]=mul(root[s][j-1],w,q); inverse_root[s][j]=mul(inverse_root[s][j-1],iw,q); }
    for(size_t j=0;j<h;j++) { shoup[s][j]=(uint64_t(root[s][j])<<32)/q; inverse_shoup[s][j]=(uint64_t(inverse_root[s][j])<<32)/q; }
  }
}
uint32_t Plan::product(uint32_t a,uint32_t b) const {
  const uint64_t t=uint64_t(a)*b;
  const uint64_t quotient=uint64_t((UWide(t)*reciprocal)>>64);
  const uint64_t r=t-quotient*q;return uint32_t(r>=q?r-q:r);
}
Poly Plan::transform(const Poly& in,bool inverse) const {
  require(in.size()==degree,"NTT degree mismatch"); Poly value=in;
  for(auto x:value) require(x<q,"NTT noncanonical input");
  auto constant_product=[&](uint32_t x,uint32_t w,uint32_t factor) {
    const uint64_t estimate=(uint64_t(x)*factor)>>32;
    const uint64_t r=uint64_t(x)*w-estimate*q;return uint32_t(r>=q?r-q:r);
  };
  if(!inverse) for(size_t i=0;i<degree;i++) value[i]=constant_product(value[i],twist[i],twist_shoup[i]);
  for(size_t i=1,j=0;i<degree;i++) { size_t bit=degree/2; for(;j&bit;bit>>=1) j^=bit; j^=bit;
    if(i<j) std::swap(value[i],value[j]); }
  for(size_t s=0,h=1;s<12;s++,h*=2) {
    const auto& roots=inverse?inverse_root[s]:root[s]; const auto& factors=inverse?inverse_shoup[s]:shoup[s];
    for(size_t b=0;b<degree;b+=2*h) for(size_t j=0;j<h;j++) {
      const uint32_t a=value[b+j],x=value[b+j+h];
      const uint64_t estimate=(uint64_t(x)*factors[j])>>32;
      uint64_t product=uint64_t(x)*roots[j]-estimate*q;
      if(product>=q) product-=q; // Shoup: 0 <= product < 2q, q < 2^30.
      const auto v=uint32_t(product),sum=a+v;
      value[b+j]=sum>=q?sum-q:sum; value[b+j+h]=a>=v?a-v:a+q-v;
    }
  }
  if(inverse) for(size_t i=0;i<degree;i++) value[i]=constant_product(value[i],untwist[i],untwist_shoup[i]);
  return value;
}
Engine::Engine(unsigned workers,int fd):plans_{Plan(primes[0],psi[0]),Plan(primes[1],psi[1]),Plan(primes[2],psi[2])},workers_(workers),pool_(workers),fd_(fd) {
  require(workers==1||workers==3,"CPU workers must be 1 or 3 (RNS limbs)");
  require(fd>=-1,"Invalid hardware descriptor");
}
void Engine::set_model_cache(bool enabled) const {
  std::lock_guard<std::mutex> lock(model_mutex_);
  model_cache_enabled_=enabled;model_ready_=false;
  model_weights_={};model_transforms_={};
}
std::array<uint64_t,2> Engine::model_cache_stats() const {
  std::lock_guard<std::mutex> lock(model_mutex_);
  return {model_hits_,model_builds_};
}
void Engine::parallel(const std::function<void(size_t)>& task) const { pool_.run(task); }
std::array<Message,4> Engine::encode_four(const std::array<std::vector<std::complex<double>>,4>& values,uint64_t delta) const {
  require(std::fegetround()==FE_TONEAREST,"Encoder requires FE_TONEAREST");
  std::array<Message,4> out;
  parallel([&](size_t lane){for(size_t f=lane;f<4;f+=3)out[f]=encode(values[f],delta);});
  return out;
}
std::array<uint64_t,2> Engine::key_cache_stats() const {
  std::lock_guard<std::mutex> guard(key_mutex_); return {key_hits_,key_builds_};
}
void Engine::invalidate_public_key() const {
  std::lock_guard<std::mutex> guard(key_mutex_);
  key_ready_=false;cached_key_={};cached_b_={};cached_a_={};
}
Rns Engine::multiply(const Rns& a,const Rns& b) const {
  validate(a); validate(b); Rns result;
  if(fd_>=0) {
    struct alignas(16) Beat { uint32_t limb[4]; };
    struct Request { uint32_t op,res0,input_words,output_words; uint64_t input_ptr,output_ptr;
      uint32_t timeout_ms,elapsed_us; int32_t status; uint32_t reserved; };
    static_assert(sizeof(Request)==48&&sizeof(Beat)==16,"Frozen DMA ABI mismatch");
    std::vector<Beat> input(2*degree),output(degree);
    for(size_t i=0;i<degree;i++) for(size_t l=0;l<3;l++) { input[i].limb[l]=a[l][i]; input[degree+i].limb[l]=b[l][i]; }
    Request req{}; req.op=2; req.input_words=2*degree; req.output_words=degree;
    req.input_ptr=reinterpret_cast<uint64_t>(input.data()); req.output_ptr=reinterpret_cast<uint64_t>(output.data()); req.timeout_ms=5000;
    static_assert(_IOWR('E',0x04,Request)==0xc0304504UL,"Frozen ioctl mismatch");
    if(ioctl(fd_,_IOWR('E',0x04,Request),&req)<0||req.status) throw std::runtime_error("DMA failure: stop; no retry/reset");
    for(auto& p:result) p.resize(degree);
    for(size_t i=0;i<degree;i++) { require(output[i].limb[3]==0,"DMA nonzero padding");
      for(size_t l=0;l<3;l++) result[l][i]=output[i].limb[l]; }
    validate(result); return result;
  }
  parallel([&](size_t l) {
    auto x=plans_[l].transform(a[l],false),y=plans_[l].transform(b[l],false);
    for(size_t i=0;i<degree;i++) x[i]=plans_[l].product(x[i],y[i]);
    result[l]=plans_[l].transform(x,true);
  }); return result;
}
PublicKey Engine::keygen(const Material& m) const {
  validate_ternary(m.secret,64); validate_ternary(m.error,16); validate(m.a);
  auto b=multiply(m.a,residues(m.secret)),e=residues(m.error);
  for(size_t l=0;l<3;l++) for(size_t i=0;i<degree;i++) {
    auto r=e[l][i]+primes[l]-b[l][i];b[l][i]=r>=primes[l]?r-primes[l]:r;
  }
  return {std::move(b),m.a};
}
std::array<Cipher,4> Engine::encrypt(const PublicKey& key,const std::array<Message,4>& message,
    const std::array<Ternary,4>& mask,const std::array<Ternary,4>& e0,const std::array<Ternary,4>& e1) const {
  validate(key.b); validate(key.a); std::array<Rns,4> mr,ur,r0,r1;
  for(size_t f=0;f<4;f++) { validate_ternary(mask[f],64); validate_ternary(e0[f],16); validate_ternary(e1[f],16);
    mr[f]=residues(message[f]); ur[f]=residues(mask[f]); r0[f]=residues(e0[f]); r1[f]=residues(e1[f]); }
  return encrypt_residues(key,mr,ur,r0,r1);
}
std::array<Cipher,4> Engine::encrypt_residues(const PublicKey& key,const std::array<Rns,4>& mr,
    const std::array<Rns,4>& ur,const std::array<Rns,4>& r0,const std::array<Rns,4>& r1) const {
  validate(key.b);validate(key.a);
  for(size_t f=0;f<4;f++){validate(mr[f]);validate(ur[f]);validate(r0[f]);validate(r1[f]);}
  std::array<Cipher,4> out;
  if(fd_>=0) {
    for(size_t f=0;f<4;f++)out[f]={add(add(multiply(key.b,ur[f]),r0[f]),mr[f]),add(multiply(key.a,ur[f]),r1[f])};
    return out; // Legacy hardware path remains explicit; no paired-ABI claim.
  }
  std::lock_guard<std::mutex> guard(key_mutex_);
  if(!key_ready_ || cached_key_.b!=key.b || cached_key_.a!=key.a) {
    Rns b,a;
    parallel([&](size_t l){b[l]=plans_[l].transform(key.b[l],false);a[l]=plans_[l].transform(key.a[l],false);});
    PublicKey copy=key; // All potentially throwing copies happen before publication.
    cached_key_=std::move(copy);cached_b_=std::move(b);cached_a_=std::move(a);key_ready_=true;key_builds_++;
  }else key_hits_++;
  parallel([&](size_t l) {
    for(size_t f=0;f<4;f++) {
      const auto u=plans_[l].transform(ur[f][l],false);Poly bu(degree),au(degree);
      for(size_t i=0;i<degree;i++){bu[i]=plans_[l].product(cached_b_[l][i],u[i]);au[i]=plans_[l].product(cached_a_[l][i],u[i]);}
      bu=plans_[l].transform(bu,true);au=plans_[l].transform(au,true);
      for(size_t i=0;i<degree;i++) {
        uint32_t sum=bu[i]+r0[f][l][i]+mr[f][l][i];if(sum>=primes[l])sum-=primes[l];if(sum>=primes[l])sum-=primes[l];bu[i]=sum;
        sum=au[i]+r1[f][l][i];au[i]=sum>=primes[l]?sum-primes[l]:sum;
      }
      out[f].c0[l]=std::move(bu);out[f].c1[l]=std::move(au);
    }
  });return out;
}
std::vector<Wide> Engine::decrypt(const Cipher& cipher,const Ternary& secret) const {
  validate_ternary(secret,64); return centered(add(cipher.c0,multiply(cipher.c1,residues(secret))));
}
Cipher Engine::affine(const std::array<Cipher,4>& cipher,const std::array<Message,4>& weight,const Message& bias) const {
  // Serialize cache updates and readers on a shared context; independent contexts
  // remain independent. Internal limb workers only read completed cache entries.
  std::lock_guard<std::mutex> lock(model_mutex_);
  auto bias_rns=residues(bias); std::array<Rns,4> weights;
  for(size_t f=0;f<4;f++) { validate(cipher[f].c0); validate(cipher[f].c1); weights[f]=residues(weight[f]); }
  Cipher out;
  // Cloud CPU only. Caller must create a CPU Engine for this operation.
  require(fd_==-1,"Cloud affine must not use the board descriptor");
  bool constant=true;
  for(const auto& w:weight) constant=constant&&std::all_of(w.begin()+1,w.end(),[](int64_t v){return v==0;});
  if(constant) {
    // Repeated slot weights encode as degree-zero polynomials. Multiplication
    // by W_f[0] is exact coefficientwise scalar multiplication; no NTT needed.
    parallel([&](size_t l) {
      out.c0[l]=bias_rns[l];out.c1[l]=Poly(degree);
      for(size_t f=0;f<4;f++) for(size_t i=0;i<degree;i++) {
        auto sum=out.c0[l][i]+plans_[l].product(cipher[f].c0[l][i],weights[f][l][0]);
        out.c0[l][i]=sum>=primes[l]?sum-primes[l]:sum;
        sum=out.c1[l][i]+plans_[l].product(cipher[f].c1[l][i],weights[f][l][0]);
        out.c1[l][i]=sum>=primes[l]?sum-primes[l]:sum;
      }
    });return out;
  }
  std::array<Rns,4> prepared;
  const std::array<Rns,4>* transforms=nullptr;
  if(model_cache_enabled_&&model_ready_&&model_weights_==weight) {
    transforms=&model_transforms_;model_hits_++;
  } else {
    parallel([&](size_t l) {
      for(size_t f=0;f<4;f++) prepared[f][l]=plans_[l].transform(weights[f][l],false);
    });
    if(model_cache_enabled_) {
      // Publish only after all transforms and the public-model copy succeed.
      auto copied_weights=weight;
      model_weights_=std::move(copied_weights);model_transforms_=std::move(prepared);
      model_ready_=true;model_builds_++;transforms=&model_transforms_;
    } else transforms=&prepared;
  }
  parallel([&](size_t l) {
    Poly sum0(degree),sum1(degree);
    for(size_t f=0;f<4;f++) {
      const auto& w=(*transforms)[f][l];
      const auto a=plans_[l].transform(cipher[f].c0[l],false),b=plans_[l].transform(cipher[f].c1[l],false);
      for(size_t i=0;i<degree;i++) {
        auto sum=sum0[i]+plans_[l].product(a[i],w[i]);sum0[i]=sum>=primes[l]?sum-primes[l]:sum;
        sum=sum1[i]+plans_[l].product(b[i],w[i]);sum1[i]=sum>=primes[l]?sum-primes[l]:sum;
      }
    }
    out.c0[l]=plans_[l].transform(sum0,true); out.c1[l]=plans_[l].transform(sum1,true);
    for(size_t i=0;i<degree;i++) { auto sum=out.c0[l][i]+bias_rns[l][i];out.c0[l][i]=sum>=primes[l]?sum-primes[l]:sum; }
  }); return out;
}
} // namespace edgefhe
