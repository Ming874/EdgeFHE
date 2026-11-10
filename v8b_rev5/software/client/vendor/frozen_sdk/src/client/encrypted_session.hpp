// PS-only paired products -> four CKKS ciphertexts. No sampling occurs here.
#pragma once
#include "ps_fusion.hpp"
namespace edgefhe_v7 {
inline std::array<Ciphertext,4> encrypt_four(PairedSession& session,
    const std::array<Poly,4>& u,const std::array<Poly,4>& message,
    const std::array<Poly,4>& error0,const std::array<Poly,4>& error1){
    return session.pair_apply(u,[&](const std::vector<Beat>& products){
        return unpack_and_fuse(products,message,error0,error1);
    });
}
} // No eight-polynomial intermediate allocation/unpack pass.
