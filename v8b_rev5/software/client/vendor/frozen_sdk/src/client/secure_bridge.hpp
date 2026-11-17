// PS-only integration of the frozen sigma4.5/scale2^30 material with V7 paired IO.
#pragma once
#include "baseline/secure.hpp"
#include "encrypted_session.hpp"
#include "profile.hpp"
#include <openssl/sha.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <cerrno>
#include <memory>
#include <thread>
namespace edgefhe_v7 {
// Capability policy (2026-10-13 candidate, docs/04 X1). Three classes of flag bits:
//   required  : must be set (identity verified, fail-closed);
//   forbidden : RECOVERY_PROVEN stays rejected until whole-path recovery is board accepted;
//   optional  : bits 16..31 are reserved for feature announcements (e.g. a future STATS bit) and are ignored,
// any other bit is unknown and rejected. operation_mask must contain the nine V7 opcodes (extra opcodes are
// allowed); geometry, profile and the reserved words keep their exact V7 values.
constexpr uint32_t caps_required_flags=EDGEFHE_V7_CAP_HW_IDENTITY_VERIFIED|EDGEFHE_V7_CAP_FAIL_CLOSED;
constexpr uint32_t caps_forbidden_flags=EDGEFHE_V7_CAP_RECOVERY_PROVEN;
constexpr uint32_t caps_optional_flags=0xFFFF0000u;
inline bool caps_accepted(const edgefhe_v7_caps& c){
 if(c.abi_version!=7||(c.operation_mask&0x1ffu)!=0x1ffu||c.n!=4096||c.limbs!=3||c.lanes_per_limb!=4||
    c.max_tx_bytes!=262144||c.max_rx_bytes!=524288||c.poisoned)return false;
 if((c.flags&caps_required_flags)!=caps_required_flags||(c.flags&caps_forbidden_flags)||
    (c.flags&~(caps_required_flags|caps_optional_flags)))return false;
 if(!std::equal(active_profile.begin(),active_profile.end(),c.profile_sha256))return false;
 for(auto v:c.reserved)if(v)return false;
 return true;
}
class UnifiedDevice {
 int fd_=-1;
 std::unique_ptr<PairedSession> paired_;
 std::thread::id owner_;
 uint64_t completed_=0,driver_us_=0;
 void check_owner() {
  auto caller=std::this_thread::get_id();
  if(owner_==std::thread::id{})owner_=caller;
  if(owner_!=caller)throw std::runtime_error("Only the DMA I/O owner may use this device");
 }
 int invoke(edgefhe_v7_request& r) {
  check_owner();
  int rc=::ioctl(fd_,EDGEFHE_IOC_V7_RUN,&r);
  if(rc>=0){completed_++;driver_us_+=r.elapsed_us;}
  return rc<0?-errno:rc; // No retry, including EINTR/cancellation.
 }
public:
 explicit UnifiedDevice(const char *device) {
  fd_=::open(device,O_RDWR|O_CLOEXEC);
  if(fd_<0)throw std::runtime_error("Unified device open failed");
  try {
   struct stat st{};
   if(fstat(fd_,&st)||!S_ISCHR(st.st_mode))throw std::runtime_error("Expected character device");
   edgefhe_v7_caps c{};
   if(::ioctl(fd_,EDGEFHE_IOC_V7_CAPS,&c)<0)throw std::runtime_error("Capability ioctl failed");
   if(!caps_accepted(c))throw std::runtime_error("Unaccepted hardware/driver/profile capability");
   paired_=std::make_unique<PairedSession>(active_profile,[this](edgefhe_v7_request& r){return invoke(r);});
  }catch(...){::close(fd_);fd_=-1;throw;}
 }
 UnifiedDevice(const UnifiedDevice&)=delete;
 UnifiedDevice& operator=(const UnifiedDevice&)=delete;
 ~UnifiedDevice(){paired_.reset();if(fd_>=0)::close(fd_);}
 void load_key(const edgefhe::PublicKey& key) {
  check_owner();std::vector<Beat> wire;pack_into(6,{key.b,key.a},wire);
  // Both supported Linux targets are little-endian. Hash the exact padded
  // LOAD wire image b then a; key identity is public and does not authenticate.
  static_assert(__BYTE_ORDER__==__ORDER_LITTLE_ENDIAN__,"Wire endian contract");
  Digest digest{};SHA256(reinterpret_cast<const unsigned char*>(wire.data()),wire.size()*16,digest.data());
  paired_->load(key.b,key.a,digest);
 }
 uint64_t completed_calls()const{return completed_;}
 uint64_t driver_elapsed_total_us()const{return driver_us_;}
 PairedSession& session(){check_owner();return *paired_;}
 Poly multiply(const Poly& a,const Poly& b) {
  check_owner();return paired_->legacy(2,{a,b}).at(0);
 }
};
struct ResidueMaterial {
 std::array<Poly,4> message,mask,e0,e1;
 ~ResidueMaterial() {
  for(auto *a:{&message,&mask,&e0,&e1})for(auto& p:*a)for(auto& l:p) {
   volatile uint32_t *v=l.data();for(size_t i=0;i<l.size();i++)v[i]=0;
  }
 }
};
inline std::array<edgefhe::Cipher,4> encrypt_secure_paired(PairedSession& session,
 const std::array<edgefhe::Message,4>& message,const edgefhe::SecureMaterial& material) {
 ResidueMaterial r;
 for(size_t f=0;f<4;f++) {
  edgefhe::require(material.mask[f].size()==N&&material.e0[f].size()==N&&material.e1[f].size()==N,"Secure material degree");
  for(auto x:material.mask[f])edgefhe::require(x>=-1&&x<=1,"Uniform ternary mask");
  for(const auto *noise:{&material.e0[f],&material.e1[f]})for(auto x:*noise)
   edgefhe::require(x>=-55&&x<=55,"Pinned Gaussian support");
  r.message[f]=edgefhe::residues(message[f]);r.mask[f]=edgefhe::residues(material.mask[f]);
  r.e0[f]=edgefhe::residues(material.e0[f]);r.e1[f]=edgefhe::residues(material.e1[f]);
 }
 auto fused=encrypt_four(session,r.mask,r.message,r.e0,r.e1);
 std::array<edgefhe::Cipher,4> output;
 for(size_t f=0;f<4;f++){output[f].c0=std::move(fused[f].c0);output[f].c1=std::move(fused[f].c1);}
 return output;
}
inline std::vector<edgefhe::Wide> decrypt_secure_unified(UnifiedDevice& device,
 const edgefhe::Cipher& cipher,const edgefhe::Ternary& secret) {
 edgefhe::require(secret.size()==N,"Ternary secret degree");
 for(auto x:secret)edgefhe::require(x>=-1&&x<=1,"Ternary secret coefficient");
 struct SecretResidues {
  Poly p;
  ~SecretResidues(){for(auto& l:p){volatile uint32_t *v=l.data();for(size_t i=0;i<l.size();i++)v[i]=0;}}
 } sr{edgefhe::residues(secret)};
 auto product=device.multiply(cipher.c1,sr.p);
 return edgefhe::centered(edgefhe::add(cipher.c0,product));
}
} // Not a secure product: intermediate/stack zeroization and side channels remain open.
