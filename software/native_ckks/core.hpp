// PS/host C++17 candidate. Never compile this file as an HLS source.
#pragma once
#include <array>
#include <complex>
#include <cstdint>
#include <functional>
#include <vector>

namespace edgefhe {
constexpr size_t degree=4096, limbs=3, features=4, slots=degree/2;
constexpr uint64_t scale=uint64_t{1}<<29; // V4 precision profile; frozen V3 remains 2^24.
constexpr std::array<uint32_t,3> primes={998244353,1004535809,469762049};
constexpr std::array<uint32_t,3> psi={350007156,2062645,62025685};
using Wide=__int128_t;
using UWide=__uint128_t;
using Poly=std::vector<uint32_t>;
using Rns=std::array<Poly,3>;
using Message=std::vector<int64_t>;
using Ternary=std::vector<int8_t>;
struct Material { Ternary secret,error; std::array<Ternary,4> mask,e0,e1; Rns a; };
struct PublicKey { Rns b,a; };
struct Cipher { Rns c0,c1; };

void require(bool ok,const char* message);
void validate(const Rns& value);
void validate_ternary(const Ternary& value,size_t weight);
int64_t nearest_even(double value);
UWide no_wrap_bound(const std::array<Message,4>& message,const std::array<Message,4>& weight,const Message& bias);
double affine_error_bound(const std::array<double,4>& weights,double input_abs_max);
double sigmoid(double value);
int classify(double value,double bound);
Message encode(const std::vector<std::complex<double>>& values,uint64_t delta=scale);
std::vector<std::complex<double>> decode(const std::vector<Wide>& coefficients,uint64_t delta=scale);
Rns residues(const Message& value);
Rns residues(const Ternary& value);
Rns add(const Rns& a,const Rns& b);
std::vector<Wide> centered(const Rns& value);
Material sample_material(); // Linux getrandom, no user-provided seed or fallback PRNG.
std::array<Ternary,12> sample_encryption();

struct Plan {
  uint32_t q,n_inverse; uint64_t reciprocal; Poly twist,untwist,twist_shoup,untwist_shoup;
  std::array<Poly,12> root,inverse_root,shoup,inverse_shoup;
  Plan(uint32_t modulus,uint32_t root_psi);
  uint32_t product(uint32_t a,uint32_t b) const;
  Poly transform(const Poly& value,bool inverse) const;
};
class Engine {
  std::array<Plan,3> plans_;
  unsigned workers_;
  int fd_;
  void parallel(const std::function<void(size_t)>& task) const;
 public:
  explicit Engine(unsigned workers=1,int fd=-1);
  Rns multiply(const Rns& a,const Rns& b) const;
  PublicKey keygen(const Material& material) const;
  std::array<Cipher,4> encrypt(const PublicKey& key,const std::array<Message,4>& message,
      const std::array<Ternary,4>& mask,const std::array<Ternary,4>& e0,
      const std::array<Ternary,4>& e1) const;
  std::vector<Wide> decrypt(const Cipher& cipher,const Ternary& secret) const;
  Cipher affine(const std::array<Cipher,4>& cipher,const std::array<Message,4>& weight,
      const Message& bias) const;
};
} // namespace edgefhe
