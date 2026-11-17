// Native C++ transport adapter candidate; no implicit board connection.
#pragma once
#include <string.h>
#include "edgefhe_uapi.h"
#include "lease_state.hpp"
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <stdexcept>
#include <vector>
namespace edgefhe_v7 {
constexpr size_t N=4096;
constexpr std::array<uint32_t,3> Q={998244353,1004535809,469762049};
using Poly=std::array<std::vector<uint32_t>,3>;
struct alignas(16) Beat {uint32_t limb[4];};
static_assert(sizeof(Beat)==16&&alignof(Beat)==16,"DMA beat layout");
static_assert(sizeof(edgefhe_legacy_request)==48&&offsetof(edgefhe_legacy_request,input_ptr)==16&&offsetof(edgefhe_legacy_request,status)==40,"Legacy ABI");
static_assert(sizeof(edgefhe_v7_request)==128&&offsetof(edgefhe_v7_request,generation)==40&&offsetof(edgefhe_v7_request,profile_sha256)==48,"V7 request ABI");
static_assert(sizeof(edgefhe_v7_caps)==128&&offsetof(edgefhe_v7_caps,generation)==32,"V7 capability ABI");
inline size_t input_words(unsigned op){if(op>8)throw std::invalid_argument("Unknown opcode");return op<2?N:op==2||op==3||op==5||op==6?2*N:op==4?3*N:op==7?4*N:0;}
inline size_t output_words(unsigned op){if(op>8)throw std::invalid_argument("Unknown opcode");return op<=5?N:op==7?8*N:1;}
inline bool interleaved(unsigned op){return op==3||op==4||op==5;}
inline void pack_into(unsigned op,const std::vector<Poly>& polys,std::vector<Beat>& output){
    const size_t n=input_words(op);if(polys.size()*N!=n)throw std::invalid_argument("Polynomial count");
    for(const auto& p:polys)for(size_t l=0;l<3;l++){if(p[l].size()!=N)throw std::invalid_argument("Coefficient count");for(auto x:p[l])if(x>=Q[l])throw std::invalid_argument("Noncanonical input");}
    output.resize(n);
    for(size_t k=0;k<n;k++){size_t p=interleaved(op)?k%polys.size():k/N,i=interleaved(op)?k/polys.size():k%N;for(size_t l=0;l<3;l++)output[k].limb[l]=polys[p][l][i];output[k].limb[3]=0;}
}
inline std::vector<Poly> unpack(unsigned op,const std::vector<Beat>& input){
    size_t words=output_words(op);if(input.size()!=words)throw std::invalid_argument("Output beat count");
    for(const auto& b:input){if(b.limb[3])throw std::invalid_argument("Output padding");for(size_t l=0;l<3;l++)if(b.limb[l]>=Q[l])throw std::invalid_argument("Output residue");}
    if(op==6||op==8){for(const auto& b:input)for(auto x:b.limb)if(x)throw std::invalid_argument("ACK value");return {};}
    std::vector<Poly> result(words/N);for(auto& p:result)for(auto& limb:p)limb.resize(N);
    for(size_t k=0;k<words;k++)for(size_t l=0;l<3;l++)result[k/N][l][k%N]=input[k].limb[l];return result;
}
inline void validate_request(const edgefhe_v7_request& r){
    if(r.abi_version!=EDGEFHE_V7_ABI||r.flags||r.reserved||r.timeout_ms>30000||r.command>8)throw std::invalid_argument("Request header");
    if(r.input_words!=input_words(r.command)||r.output_words!=output_words(r.command))throw std::invalid_argument("Request shape");
    if(!r.output_ptr||(r.input_words&&!r.input_ptr)||(!r.input_words&&r.input_ptr))throw std::invalid_argument("Request pointers");
    if(r.command==6&&r.generation)throw std::invalid_argument("LOAD generation assigned by driver");
    if(r.command==7&&!r.generation)throw std::invalid_argument("PAIR generation required");
    if(r.command<=5){if(r.generation)throw std::invalid_argument("Unused generation");for(auto x:r.public_key_sha256)if(x)throw std::invalid_argument("Unused key");}
}
// The injected call will be an ioctl only after the new driver and board gates pass.
// Tests use an offline mock. It must return success only after TX/RX/ap_done/status.
class PairedSession {
    std::function<int(edgefhe_v7_request&)> call_;
    Digest profile_,key_{};uint64_t generation_=0;
    std::vector<Beat> in_,out_;
    // Invariant (2026-10-13 candidate): both buffers are all-zero between calls, on every success and failure
    // path, so a call only resizes them. explicit_bzero is a library memset the compiler may not elide.
    void wipe_buffers(){for(auto* v:{&in_,&out_})if(!v->empty())explicit_bzero(v->data(),v->size()*sizeof(Beat));}
    edgefhe_v7_request request(unsigned op){edgefhe_v7_request r{};r.abi_version=EDGEFHE_V7_ABI;r.command=op;r.input_words=input_words(op);r.output_words=output_words(op);r.input_ptr=in_.empty()?0:reinterpret_cast<uintptr_t>(in_.data());r.output_ptr=reinterpret_cast<uintptr_t>(out_.data());r.timeout_ms=5000;std::copy(profile_.begin(),profile_.end(),r.profile_sha256);return r;}
    void invoke(edgefhe_v7_request& r){
        validate_request(r);const auto before=r;
        try{
            if(call_(r)||r.status||r.hardware_status)throw std::runtime_error("Transaction rejected; no retry");
            if(r.abi_version!=before.abi_version||r.command!=before.command||r.input_words!=before.input_words||r.output_words!=before.output_words||
               r.input_ptr!=before.input_ptr||r.output_ptr!=before.output_ptr||r.timeout_ms!=before.timeout_ms||r.flags!=before.flags||r.reserved!=before.reserved||
               !std::equal(r.profile_sha256,r.profile_sha256+32,before.profile_sha256)||
               !std::equal(r.public_key_sha256,r.public_key_sha256+32,before.public_key_sha256))
                throw std::runtime_error("Response descriptor identity mismatch");
        }catch(...){generation_=0;key_.fill(0);wipe_buffers();throw;}
    }
public:
    PairedSession(Digest profile,std::function<int(edgefhe_v7_request&)> call):call_(std::move(call)),profile_(profile){in_.reserve(4*N);out_.reserve(8*N);}
    ~PairedSession(){wipe_buffers();}
    PairedSession(const PairedSession&)=delete;
    PairedSession& operator=(const PairedSession&)=delete;
    std::vector<Poly> legacy(unsigned op,const std::vector<Poly>& input){
        if(op>5)throw std::invalid_argument("Legacy operation");pack_into(op,input,in_);out_.resize(N);auto r=request(op);invoke(r);
        try{if(r.generation)throw std::runtime_error("Legacy generation response");auto result=unpack(op,out_);wipe_buffers();return result;}
        catch(...){generation_=0;key_.fill(0);wipe_buffers();throw;}
    }
    void load(const Poly& b,const Poly& a,const Digest& key){
        generation_=0;key_.fill(0);pack_into(6,{b,a},in_);out_.resize(1);auto r=request(6);
        std::copy(profile_.begin(),profile_.end(),r.profile_sha256);std::copy(key.begin(),key.end(),r.public_key_sha256);invoke(r);
        try{unpack(6,out_);if(!r.generation)throw std::runtime_error("Zero returned generation");}catch(...){generation_=0;wipe_buffers();throw;}
        generation_=r.generation;key_=key;wipe_buffers();
    }
    // A single I/O owner calls this method; consumers cannot retain buffer references.
    template<class Consumer> auto pair_apply(const std::array<Poly,4>& u,Consumer consume){
        if(!generation_)throw std::runtime_error("No cache lease");pack_into(7,{u.begin(),u.end()},in_);out_.resize(8*N);auto r=request(7);r.generation=generation_;
        std::copy(profile_.begin(),profile_.end(),r.profile_sha256);std::copy(key_.begin(),key_.end(),r.public_key_sha256);invoke(r);
        if(r.generation!=generation_){generation_=0;key_.fill(0);wipe_buffers();throw std::runtime_error("Response generation mismatch");}
        try{auto result=consume(static_cast<const std::vector<Beat>&>(out_));wipe_buffers();return result;}catch(...){generation_=0;key_.fill(0);wipe_buffers();throw;}
    }
    std::vector<Poly> pair(const std::array<Poly,4>& u){return pair_apply(u,[](const std::vector<Beat>& data){return unpack(7,data);});}
    // Fast-path entry (2026-10-14 candidate): the caller writes the 4N canonical input beats itself (fill), so no
    // residue polynomials of the secret mask are materialized. Same request, lease, generation and wipe rules as
    // pair_apply; the canonical check that pack_into performed is repeated here on the filled beats.
    template<class Fill,class Consumer> auto pair_apply_prepacked(Fill fill,Consumer consume){
        if(!generation_)throw std::runtime_error("No cache lease");
        in_.resize(4*N);out_.resize(8*N);
        try{
            fill(in_);
            for(const auto& b:in_){if(b.limb[3]||b.limb[0]>=Q[0]||b.limb[1]>=Q[1]||b.limb[2]>=Q[2])throw std::invalid_argument("Noncanonical input");}
        }catch(...){wipe_buffers();throw;}
        auto r=request(7);r.generation=generation_;
        std::copy(profile_.begin(),profile_.end(),r.profile_sha256);std::copy(key_.begin(),key_.end(),r.public_key_sha256);invoke(r);
        if(r.generation!=generation_){generation_=0;key_.fill(0);wipe_buffers();throw std::runtime_error("Response generation mismatch");}
        try{auto result=consume(static_cast<const std::vector<Beat>&>(out_));wipe_buffers();return result;}catch(...){generation_=0;key_.fill(0);wipe_buffers();throw;}
    }
    uint64_t generation()const{return generation_;}
};
}
