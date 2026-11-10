#pragma once
#include "core.hpp"
#include <string>
namespace edgefhe {
struct SecureMaterial {
  Ternary secret;
  Message error;
  std::array<Ternary,4> mask;
  std::array<Message,4> e0,e1;
  Rns a;
  ~SecureMaterial();
};
void initialize_secure_sampler(const std::string& os_plugin);
SecureMaterial sample_secure_material();
void sample_secure_encryption(SecureMaterial&);
void sample_secure_encryption_concurrent(SecureMaterial&);  // no process-wide lock; same distribution and draw order
PublicKey keygen_secure(const Engine&,const SecureMaterial&);
std::array<Cipher,4> encrypt_secure(const Engine&,const PublicKey&,const std::array<Message,4>&,const SecureMaterial&);
std::vector<Wide> decrypt_secure(const Engine&,const Cipher&,const Ternary&);
UWide l1(const Message&);
UWide l1(const Ternary&);
UWide encryption_noise_bound(const SecureMaterial&,size_t feature);
UWide affine_coefficient_bound(const std::array<Message,4>&,const std::array<Message,4>&,const Message&,const SecureMaterial&);
double secure_affine_error_bound(const std::array<Message,4>& weight,const SecureMaterial&,double input_abs_max);
}
