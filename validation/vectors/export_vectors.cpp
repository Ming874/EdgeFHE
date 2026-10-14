// Public arithmetic vectors only. No CKKS context, keys, or ciphertexts.
#include "math/hal/intnat/transformnat.h"
#include <cstdint>
#include <fstream>
#include <iostream>
#include <map>
#include <stdexcept>
#include <vector>
using namespace lbcrypto;
using intnat::NativeInteger;
using intnat::NativeVector;
using intnat::ChineseRemainderTransformFTTNat;
constexpr uint32_t N=4096;
const uint32_t Q[3]={998244353,1004535809,469762049};
const uint32_t PSI[3]={350007156,2062645,62025685};
using Poly=std::vector<std::vector<uint32_t>>;
struct Case { uint32_t op; Poly a,b,expected; };
static Poly zeros(){return Poly(3,std::vector<uint32_t>(N));}
static uint64_t rng=0x20261003ed9eULL;
static uint32_t random_value(uint32_t q){rng^=rng<<13;rng^=rng>>7;rng^=rng<<17;return rng%q;}
static void require(bool ok,const char* message){if(!ok)throw std::runtime_error(message);}
static uint32_t power(uint32_t x,uint32_t exponent,uint32_t q){uint64_t result=1;while(exponent){if(exponent&1)result=result*x%q;x=uint64_t(x)*x%q;exponent>>=1;}return result;}
static std::vector<uint32_t> mapping(int limb){
    // Discover output convention from the transform of x, then verify it is
    // exactly bit reversal. Do not silently assume library ordering.
    NativeInteger q(Q[limb]),root(PSI[limb]);
    NativeVector impulse(N,q);impulse[1]=NativeInteger(1);
    ChineseRemainderTransformFTTNat<NativeVector> transform;
    transform.ForwardTransformToBitReverseInPlace(root,2*N,&impulse);
    std::map<uint64_t,uint32_t> odd_powers;
    uint64_t value=PSI[limb],omega=uint64_t(value)*value%Q[limb];
    for(uint32_t k=0;k<N;++k){odd_powers.emplace(value,k);value=value*omega%Q[limb];}
    require(odd_powers.size()==N,"root powers not distinct");
    std::vector<uint32_t> result(N);
    for(uint32_t i=0;i<N;++i){
        const auto found=odd_powers.find(impulse[i].ConvertToInt());
        require(found!=odd_powers.end(),"library convention does not match odd root evaluations");
        result[i]=found->second;
        uint32_t reversed=0,index=i;for(int bit=0;bit<12;++bit){reversed=(reversed<<1)|(index&1);index>>=1;}
        require(result[i]==reversed,"unexpected OpenFHE output permutation");
    }
    return result;
}
static Poly transform_poly(const Poly& input,bool inverse,const std::vector<std::vector<uint32_t>>& maps){
    Poly result=zeros();
    for(int limb=0;limb<3;++limb){
        NativeInteger q(Q[limb]),root(PSI[limb]);NativeVector values(N,q);
        for(uint32_t i=0;i<N;++i)values[i]=NativeInteger(input[limb][inverse?maps[limb][i]:i]);
        ChineseRemainderTransformFTTNat<NativeVector> transform;
        if(inverse)transform.InverseTransformFromBitReverseInPlace(root,2*N,&values);
        else transform.ForwardTransformToBitReverseInPlace(root,2*N,&values);
        for(uint32_t i=0;i<N;++i)result[limb][inverse?i:maps[limb][i]]=values[i].ConvertToInt();
    }
    return result;
}
static void word(std::ofstream& out,uint32_t value){for(int b=0;b<4;++b)out.put(char(value>>(8*b)));}
int main(int argc,char** argv){try{
    require(argc==2,"usage: export_vectors OUTPUT.bin");
    {std::ifstream existing(argv[1],std::ios::binary);require(!existing.good(),"refusing output overwrite");}
    std::vector<std::vector<uint32_t>> maps;for(int limb=0;limb<3;++limb)maps.push_back(mapping(limb));
    std::vector<Case> cases;
    for(int kind=0;kind<6;++kind){
        Poly input=zeros();
        for(int limb=0;limb<3;++limb)for(uint32_t i=0;i<N;++i){
            uint32_t q=Q[limb];
            input[limb][i]=kind==0?0:kind==1?(i==N-1?q-1:0):kind==2?q-1:kind==3?(i&1?q-1:0):kind==4?uint64_t(i)*1000003%q:random_value(q);
        }
        Poly forward=transform_poly(input,false,maps);
        for(int limb=0;limb<3;++limb)for(uint32_t k:{0U,1U,N/2,N-1}){
            uint64_t x=power(PSI[limb],2*k+1,Q[limb]),w=1,sum=0;
            for(uint32_t i=0;i<N;++i){sum=(sum+uint64_t(input[limb][i])*w)%Q[limb];w=w*x%Q[limb];}
            require(forward[limb][k]==sum,"OpenFHE direct evaluation mismatch");
        }
        Poly inverse=transform_poly(forward,true,maps);require(inverse==input,"OpenFHE roundtrip mismatch");
        cases.push_back({0,input,zeros(),forward});cases.push_back({1,forward,zeros(),inverse});
    }
    Poly arbitrary=zeros();for(int limb=0;limb<3;++limb)for(auto& value:arbitrary[limb])value=random_value(Q[limb]);
    cases.push_back({1,arbitrary,zeros(),transform_poly(arbitrary,true,maps)});
    for(int kind=0;kind<3;++kind){
        Poly a=zeros(),b=zeros();
        for(int limb=0;limb<3;++limb){
            if(kind==0){a[limb][N-1]=Q[limb]-1;a[limb][17]=3;b[limb][1]=5;b[limb][N-3]=Q[limb]-2;}
            else for(uint32_t i=0;i<N;++i){a[limb][i]=kind==1?Q[limb]-1:random_value(Q[limb]);b[limb][i]=kind==1?Q[limb]-1:random_value(Q[limb]);}
        }
        Poly fa=transform_poly(a,false,maps),fb=transform_poly(b,false,maps);
        for(int limb=0;limb<3;++limb)for(uint32_t i=0;i<N;++i)fa[limb][i]=uint64_t(fa[limb][i])*fb[limb][i]%Q[limb];
        cases.push_back({2,a,b,transform_poly(fa,true,maps)});
    }
    std::ofstream out(argv[1],std::ios::binary);require(out.good(),"cannot create output");
    out.write("EFNTTV1\0",8);word(out,1);word(out,N);word(out,3);word(out,cases.size());
    for(auto q:Q) { word(out,q); }
    for(auto root:PSI) { word(out,root); }
    for(const auto& test:cases){word(out,test.op);for(const auto* p:{&test.a,&test.b,&test.expected})for(const auto& limb:*p)for(auto value:limb)word(out,value);}
    out.close();require(out.good(),"vector output write failed");
    std::cout<<"OpenFHE V1 public NTT vectors: PASS (16 cases; 3 primes; exact bit-reversal mapping; no keys)\n";
    return 0;
}catch(const std::exception& error){std::cerr<<"vector export FAIL: "<<error.what()<<'\n';return 1;}}
