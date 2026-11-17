#include <algorithm>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <vector>
extern "C" {
void* ef_owner_create(unsigned,unsigned,const char*);
void ef_owner_destroy(void*);
int ef_owner_encrypt(void*,const double*,size_t,size_t,uint32_t*,size_t);
int ef_owner_decrypt(void*,const uint32_t*,size_t,size_t,double*);
int ef_cloud_affine(const uint32_t*,size_t,size_t,const int64_t*,int64_t,uint32_t*,size_t);
}
static void check(bool v,const char* text){if(!v)throw std::runtime_error(text);}
int main(int argc,char** argv){try{
  check(argc==2,"OS plugin argument");constexpr size_t F=10,P=12,W=24576;constexpr int64_t D=int64_t{1}<<30;
  size_t checked=0;
  for(unsigned backend:{0u,1u}){
    void* owner=ef_owner_create(3,backend,argv[1]);check(owner,"create owner");
    try{
      for(size_t rows:{size_t{1},size_t{17},size_t{2048}}){
        std::vector<double> x(rows*F),values(rows);std::vector<uint32_t> ciphertext(P*W),result(W);std::vector<int64_t> weights(P);
        for(size_t i=0;i<x.size();i++)x[i]=double(int(i%53)-26)/32.;
        for(size_t f=0;f<F;f++)weights[f]=(int64_t(f%5)-2)*(D/8);
        const int64_t bias=int64_t{1}<<57;
        check(ef_owner_encrypt(owner,x.data(),rows,F,ciphertext.data(),ciphertext.size())==0,"encrypt ten features");
        check(ef_cloud_affine(ciphertext.data(),ciphertext.size(),P,weights.data(),bias,result.data(),result.size())==0,"evaluate ten features");
        check(ef_owner_decrypt(owner,result.data(),result.size(),rows,values.data())==0,"decrypt score");
        for(size_t r=0;r<rows;r++){long double expected=.125L;for(size_t f=0;f<F;f++)expected+=x[r*F+f]*weights[f]/D;check(std::abs(values[r]-expected)<5e-4,"score agreement");checked++;}
        x[0]=std::numeric_limits<double>::quiet_NaN();check(ef_owner_encrypt(owner,x.data(),rows,F,ciphertext.data(),ciphertext.size())!=0,"native NaN rejection");
        check(ef_owner_decrypt(owner,result.data(),1,rows,values.data())!=0,"native wrong length rejection");
      }
    }catch(...){ef_owner_destroy(owner);throw;}
    ef_owner_destroy(owner);
  }
  std::cout<<"APPLICATION_OWNER_API_SELFTEST_PASS values="<<checked<<" ten_features=1 CPU_and_spec=1 hardware=0\n";return 0;
}catch(const std::exception& e){std::cerr<<"APPLICATION_OWNER_API_SELFTEST_FAIL "<<e.what()<<"\n";return 1;}}
