// Public deterministic inputs only. Independent OpenFHE arithmetic oracle.
#include "core.hpp"
#include "math/hal/intnat/transformnat.h"
#include <fstream>
#include <iostream>
#include <random>
using namespace edgefhe;
using intnat::NativeInteger;using intnat::NativeVector;
using intnat::ChineseRemainderTransformFTTNat;
static Rns product(const Rns& a,const Rns& b){
  Rns out;for(size_t l=0;l<3;l++){NativeInteger q(primes[l]),root(psi[l]);NativeVector x(degree,q),y(degree,q);
    for(size_t i=0;i<degree;i++){x[i]=NativeInteger(a[l][i]);y[i]=NativeInteger(b[l][i]);}
    ChineseRemainderTransformFTTNat<NativeVector> t;t.ForwardTransformToBitReverseInPlace(root,2*degree,&x);t.ForwardTransformToBitReverseInPlace(root,2*degree,&y);
    x=x.ModMul(y);t.InverseTransformFromBitReverseInPlace(root,2*degree,&x);out[l].resize(degree);for(size_t i=0;i<degree;i++)out[l][i]=x[i].ConvertToInt();
  }return out;
}
static void write(std::ofstream& out,uint32_t x){for(int b=0;b<4;b++)out.put(char(x>>(8*b)));}
static void poly(std::ofstream& out,const Rns& p){for(size_t l=0;l<3;l++)for(auto x:p[l])write(out,x);}
int main(int argc,char** argv){try{
  require(argc==2,"usage: export_hls_vectors OUTPUT");std::ifstream old(argv[1]);require(!old.good(),"Refuse overwrite");std::ofstream out(argv[1],std::ios::binary);
  out.write("EFPAIR7\0",8);write(out,3);std::mt19937_64 rng(2026100972);uint64_t compared=0;
  for(int c=0;c<3;c++){
    Rns a,b;std::array<Rns,4> u;
    for(auto* p:{&a,&b,&u[0],&u[1],&u[2],&u[3]})for(size_t l=0;l<3;l++){
      (*p)[l].resize(degree);for(size_t i=0;i<degree;i++)(*p)[l][i]=c==0?(i&1?primes[l]-1:0):rng()%primes[l];}
    poly(out,b);poly(out,a);for(auto& p:u)poly(out,p);
    for(size_t f=0;f<4;f++){poly(out,product(b,u[f]));poly(out,product(a,u[f]));compared+=6*degree;}
  }out.close();require(out.good(),"Vector write failed");
  std::cout<<"V7_PUBLIC_PAIRED_VECTORS_PASS cases=3 product_coefficients="<<compared<<" private_material=0\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
