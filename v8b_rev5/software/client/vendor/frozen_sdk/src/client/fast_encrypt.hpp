// Fast PS side of the PL encryption path (2026-10-14 candidate; must be bit-identical to encrypt_secure_paired).
//
// The 2026-10-14 on-board timing breakdown showed ~7.9 ms of user-space work around the PAIR4 ioctl in the secure
// path. The accepted path converts 16 polynomials (m, u, e0, e1 for 4 features) to RNS with a run-time 64-bit '%'
// per coefficient and limb (~197k divisions), validates them, packs u into beats, then fuses and re-validates.
// This path:
//   * packs the ternary mask straight into the DMA beats (x<0 ? q-1 : x), no residue polynomial of u exists;
//   * fuses c0 = b*u + e0 + m and c1 = a*u + e1 directly from the small signed e0/e1 (|e|<=55: x<0 ? q+x : x)
//     and from m with compile-time-constant moduli (the compiler emits multiply-high instead of division);
//   * keeps every validation of the accepted path (material ranges, product canonicality, padding).
// Secret-derived intermediates now live only in the session's wiped beat buffers and in the output ciphertext.
#pragma once
#include "secure_bridge.hpp"

namespace edgefhe_v7 {
namespace fast {
template<uint32_t q> inline uint32_t small_residue(int64_t x){return uint32_t(x<0?int64_t(q)+x:x);}   // |x|<q
template<uint32_t q> inline uint32_t wide_residue(int64_t x){int64_t r=x%int64_t(q);return uint32_t(r<0?r+int64_t(q):r);}
template<uint32_t q> inline uint32_t addq(uint32_t a,uint32_t b){uint32_t s=a+b;return s>=q?s-q:s;}   // a,b<q<2^31

template<size_t L> inline void fuse_limb(const std::vector<Beat>& products,size_t f,const edgefhe::Message& m,
    const edgefhe::Message& e0,const edgefhe::Message& e1,std::vector<uint32_t>& c0,std::vector<uint32_t>& c1){
    constexpr uint32_t q=Q[L];
    c0.resize(N);c1.resize(N);
    const Beat* bu=products.data()+(2*f)*N; const Beat* au=products.data()+(2*f+1)*N;
    for(size_t i=0;i<N;i++){
        c0[i]=addq<q>(addq<q>(bu[i].limb[L],small_residue<q>(e0[i])),wide_residue<q>(m[i]));
        c1[i]=addq<q>(au[i].limb[L],small_residue<q>(e1[i]));
    }
}
}  // namespace fast

namespace fast {
inline edgefhe::Rns small_rns(const std::vector<int8_t>& v){edgefhe::Rns r;for(auto& l:r)l.resize(N);
 for(size_t i=0;i<N;i++){r[0][i]=small_residue<Q[0]>(v[i]);r[1][i]=small_residue<Q[1]>(v[i]);r[2][i]=small_residue<Q[2]>(v[i]);}return r;}
inline edgefhe::Rns small_rns(const edgefhe::Message& v){edgefhe::Rns r;for(auto& l:r)l.resize(N);
 for(size_t i=0;i<N;i++){r[0][i]=small_residue<Q[0]>(v[i]);r[1][i]=small_residue<Q[1]>(v[i]);r[2][i]=small_residue<Q[2]>(v[i]);}return r;}
inline edgefhe::Rns wide_rns(const edgefhe::Message& v){edgefhe::Rns r;for(auto& l:r)l.resize(N);
 for(size_t i=0;i<N;i++){r[0][i]=wide_residue<Q[0]>(v[i]);r[1][i]=wide_residue<Q[1]>(v[i]);r[2][i]=wide_residue<Q[2]>(v[i]);}return r;}
inline void wipe(edgefhe::Rns& r){for(auto& l:r)if(!l.empty())explicit_bzero(l.data(),l.size()*sizeof(uint32_t));}
}  // namespace fast

// CPU baseline with the same residue optimisation (fair A/B): identical validation and Engine::encrypt_residues.
inline std::array<edgefhe::Cipher,4> cpu_encrypt_secure_fast(const edgefhe::Engine& engine,const edgefhe::PublicKey& key,
 const std::array<edgefhe::Message,4>& message,const edgefhe::SecureMaterial& m) {
 std::array<edgefhe::Rns,4> mr,u,e0,e1;
 for(size_t f=0;f<4;f++){
  edgefhe::require(m.mask[f].size()==N,"Ternary degree");for(auto v:m.mask[f])edgefhe::require(v>=-1&&v<=1,"Uniform ternary coefficient");
  for(const auto* e:{&m.e0[f],&m.e1[f]}){edgefhe::require(e->size()==N,"Gaussian degree");for(auto v:*e)edgefhe::require(v>=-55&&v<=55,"Pinned inversion sampler support mismatch");}
  edgefhe::require(message[f].size()==N,"Integer polynomial degree mismatch");
  mr[f]=fast::wide_rns(message[f]);u[f]=fast::small_rns(m.mask[f]);e0[f]=fast::small_rns(m.e0[f]);e1[f]=fast::small_rns(m.e1[f]);
 }
 auto out=engine.encrypt_residues(key,mr,u,e0,e1);
 for(auto* a:{&mr,&u,&e0,&e1})for(auto& p:*a)fast::wipe(p);
 return out;
}

inline std::array<edgefhe::Cipher,4> encrypt_secure_paired_fast(PairedSession& session,
 const std::array<edgefhe::Message,4>& message,const edgefhe::SecureMaterial& material) {
 for(size_t f=0;f<4;f++) {
  edgefhe::require(material.mask[f].size()==N&&material.e0[f].size()==N&&material.e1[f].size()==N,"Secure material degree");
  edgefhe::require(message[f].size()==N,"Integer polynomial degree mismatch");
  for(auto x:material.mask[f])edgefhe::require(x>=-1&&x<=1,"Uniform ternary mask");
  for(const auto *noise:{&material.e0[f],&material.e1[f]})for(auto x:*noise)
   edgefhe::require(x>=-55&&x<=55,"Pinned Gaussian support");
 }
 return session.pair_apply_prepacked(
  [&](std::vector<Beat>& in){
   for(size_t f=0;f<4;f++){const auto& u=material.mask[f];Beat* out=in.data()+f*N;
    for(size_t i=0;i<N;i++){const int64_t x=u[i];
     out[i].limb[0]=fast::small_residue<Q[0]>(x);out[i].limb[1]=fast::small_residue<Q[1]>(x);
     out[i].limb[2]=fast::small_residue<Q[2]>(x);out[i].limb[3]=0;}}
  },
  [&](const std::vector<Beat>& products){
   if(products.size()!=8*N)throw std::invalid_argument("Paired product count");
   for(const auto& b:products){if(b.limb[3])throw std::invalid_argument("Fusion padding");
    if(b.limb[0]>=Q[0]||b.limb[1]>=Q[1]||b.limb[2]>=Q[2])throw std::invalid_argument("Product residue");}
   std::array<edgefhe::Cipher,4> out;
   for(size_t f=0;f<4;f++){
    fast::fuse_limb<0>(products,f,message[f],material.e0[f],material.e1[f],out[f].c0[0],out[f].c1[0]);
    fast::fuse_limb<1>(products,f,message[f],material.e0[f],material.e1[f],out[f].c0[1],out[f].c1[1]);
    fast::fuse_limb<2>(products,f,message[f],material.e0[f],material.e1[f],out[f].c0[2],out[f].c1[2]);
   }
   return out;
  });
}
}  // namespace edgefhe_v7
