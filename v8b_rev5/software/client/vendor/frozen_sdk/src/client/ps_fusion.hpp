// PS-only reconstruction of c0=b*u+e0+m and c1=a*u+e1.
// Input/output residues are ordinary coefficient-domain canonical residues.
#pragma once
#include "protocol.hpp"
namespace edgefhe_v7 {
struct Ciphertext { Poly c0,c1; };
inline uint32_t add_residue(uint32_t a,uint32_t b,uint32_t q){uint32_t s=a+b;return s>=q?s-q:s;}
inline std::array<Ciphertext,4> unpack_and_fuse(const std::vector<Beat>& products,
    const std::array<Poly,4>& message,const std::array<Poly,4>& error0,const std::array<Poly,4>& error1){
    if(products.size()!=8*N)throw std::invalid_argument("Paired product count");
    for(const auto* components:{&message,&error0,&error1})for(const auto& p:*components)for(size_t l=0;l<3;l++){
        if(p[l].size()!=N)throw std::invalid_argument("Fusion shape");for(auto x:p[l])if(x>=Q[l])throw std::invalid_argument("Fusion residue");
    }
    std::array<Ciphertext,4> result;
    for(size_t f=0;f<4;f++){
        for(size_t l=0;l<3;l++){result[f].c0[l].resize(N);result[f].c1[l].resize(N);}
        for(size_t i=0;i<N;i++){
            const auto& bu=products[(2*f)*N+i];const auto& au=products[(2*f+1)*N+i];
            if(bu.limb[3]||au.limb[3])throw std::invalid_argument("Fusion padding");
            for(size_t l=0;l<3;l++){
                if(bu.limb[l]>=Q[l]||au.limb[l]>=Q[l])throw std::invalid_argument("Product residue");
                result[f].c0[l][i]=add_residue(add_residue(bu.limb[l],error0[f][l][i],Q[l]),message[f][l][i],Q[l]);
                result[f].c1[l][i]=add_residue(au.limb[l],error1[f][l][i],Q[l]);
            }
        }
    }
    return result; // No partially validated ciphertext is returned.
}
}
