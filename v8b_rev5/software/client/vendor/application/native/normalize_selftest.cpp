#include <array>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <vector>
extern "C" int ef_normalize_checked(const double*,std::size_t,std::size_t,const double*,const double*,int,int,double*,std::size_t) noexcept;
int main(){try{
  auto need=[](bool ok){if(!ok)throw std::runtime_error("normalization oracle/wipe failed");};
  std::array<double,4> mean={0,-0.0,2,-3},sd={1,2,.5,4};
  std::vector<double> raw(1024),output(1024,77),expected(1024);
  for(std::size_t i=0;i<raw.size();i++){raw[i]=mean[i%4]+sd[i%4]*(double(int(i%129)-64)/8);volatile double delta=raw[i]-mean[i%4];expected[i]=delta/sd[i%4];}
  need(ef_normalize_checked(raw.data(),256,4,mean.data(),sd.data(),0,1,output.data(),output.size())==0);
  need(std::memcmp(output.data(),expected.data(),output.size()*sizeof(double))==0);
  std::size_t rejected=0;
  auto rejection=[&](int code,int uci=0,int dictionary=1){
    std::fill(output.begin(),output.end(),77);
    need(ef_normalize_checked(raw.data(),256,4,mean.data(),sd.data(),uci,dictionary,output.data(),output.size())==code);
    for(double v:output){need(v==0);}rejected++;
  };
  const double saved=raw[999];raw[999]=std::numeric_limits<double>::quiet_NaN();rejection(2);raw[999]=saved;
  raw[999]=std::numeric_limits<double>::infinity();rejection(2);raw[999]=saved;
  raw[999]=mean[3]+9*sd[3];rejection(6);raw[999]=saved;
  rejection(3,1,0);rejection(4,1,1);
  raw[0]=20;raw[1]=2;rejection(5,1,1);
  sd[0]=0;rejection(7);
  // Nulls and invalid dimensions do not read the input, and erase a bounded output.
  std::fill(output.begin(),output.end(),77);
  need(ef_normalize_checked(nullptr,256,4,mean.data(),sd.data(),0,1,output.data(),output.size())==1);
  for(double v:output){need(v==0);}rejected++;
  std::cout<<"P1_NATIVE_NORMALIZATION_BIT_ORACLE_WIPE_PASS values=1024 rejections="<<rejected<<" FPGA=0 DMA=0\n";
  return 0;
}catch(const std::exception& e){std::cerr<<"P1_NATIVE_NORMALIZATION_FAIL "<<e.what()<<'\n';return 1;}}
