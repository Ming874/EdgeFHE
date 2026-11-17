// Native phase timing. Local mode is explicitly not a two-machine E2E benchmark.
#include "secure.hpp"
#include <chrono>
#include <fstream>
#include <iostream>
#include <numeric>
using namespace edgefhe;
using Clock=std::chrono::steady_clock;
static uint64_t ns(Clock::time_point a,Clock::time_point b){return std::chrono::duration_cast<std::chrono::nanoseconds>(b-a).count();}
int main(int argc,char** argv){try{
  require(argc==6,"usage: native_client OS_PLUGIN WORKERS RECORDS ROUNDS OUTPUT.tsv");
  unsigned workers=std::stoul(argv[2]);size_t count=std::stoul(argv[3]),rounds=std::stoul(argv[4]);
  require(count>=1&&count<=slots&&rounds>=1,"Invalid benchmark dimensions");
  initialize_secure_sampler(argv[1]);Engine client(workers),cloud(3);auto material=sample_secure_material();auto key=keygen_secure(client,material);
  const std::array<double,4> w={1.1,-.7,.9,.4};std::array<Message,4> weights;
  for(size_t f=0;f<4;f++)weights[f]=encode(std::vector<std::complex<double>>(slots,w[f]));
  auto bias=encode(std::vector<std::complex<double>>(slots,-.35),scale*scale);
  std::ifstream existing(argv[5]);require(!existing.good(),"Refuse output overwrite");
  std::ofstream output(argv[5]);require(output.good(),"Output create failed");
  output<<"sample\twarmup\tworkers\trecords\tstandardize_ns\tencode_ns\tsample_ns\tencrypt_ns\tlocal_cloud_ns\tdecrypt_ns\tdecode_ns\tlocal_math_wall_ns\n";
  double checksum=0;
  for(size_t r=0;r<rounds+3;r++){
    auto start=Clock::now(),last=start;std::array<uint64_t,7> phases{};
    std::array<std::vector<std::complex<double>>,4> x;
    for(size_t f=0;f<4;f++){x[f].resize(count);for(size_t i=0;i<count;i++)x[f][i]=(double((i+f*17)%101)-50)/25.0;}
    auto now=Clock::now();phases[0]=ns(last,now);last=now;
    auto message=client.encode_four(x);now=Clock::now();phases[1]=ns(last,now);last=now;
    sample_secure_encryption(material);now=Clock::now();phases[2]=ns(last,now);last=now;
    require(affine_coefficient_bound(message,weights,bias,material)<UWide(primes[0])*primes[1]*primes[2]/2,"Timed no-wrap gate");
    auto ciphertext=encrypt_secure(client,key,message,material);now=Clock::now();phases[3]=ns(last,now);last=now;
    auto response=cloud.affine(ciphertext,weights,bias);now=Clock::now();phases[4]=ns(last,now);last=now;
    auto integer=decrypt_secure(client,response,material.secret);now=Clock::now();phases[5]=ns(last,now);last=now;
    auto decoded=decode(integer,scale*scale);now=Clock::now();phases[6]=ns(last,now);
    double bound=secure_affine_error_bound(weights,material,2.0);
    for(size_t i=0;i<count;i++){double expected=-.35;for(size_t f=0;f<4;f++)expected+=w[f]*x[f][i].real();require(std::abs(decoded[i]-expected)<=bound&&std::abs(decoded[i]-expected)<=5e-4,"Timed result incorrect");checksum+=decoded[i].real();}
    output<<r<<'\t'<<(r<3)<<'\t'<<workers<<'\t'<<count;for(auto t:phases)output<<'\t'<<t;output<<'\t'<<ns(start,now)<<'\n';
    require(std::accumulate(phases.begin(),phases.end(),uint64_t(0))==ns(start,now),"Phase accounting mismatch");
  }
  output.close();require(output.good(),"Timing write failed");
  std::cout<<"V7_NATIVE_LOCAL_PHASE_TIMING_PASS workers="<<workers<<" records="<<count<<" measured="<<rounds<<" checksum="<<checksum<<" DMA=0 network=0 E2E_acceptance=0\n";
  return 0;
}catch(const std::exception& e){std::cerr<<"V7_NATIVE_FAIL "<<e.what()<<'\n';return 1;}}
