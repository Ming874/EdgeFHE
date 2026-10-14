// Independent standard OpenFHE CPU client baseline; no DMA or board device access.
#include "openfhe.h"
#include <omp.h>
#include <array>
#include <chrono>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <random>
using namespace lbcrypto;
using Clock=std::chrono::steady_clock;
static double micros(Clock::time_point a,Clock::time_point b) { return std::chrono::duration<double,std::micro>(b-a).count(); }
int main(int argc,char** argv) {
  try {
    if(argc!=4) throw std::invalid_argument("Usage: baseline threads rounds new-output.tsv");
    const int workers=std::stoi(argv[1]),rounds=std::stoi(argv[2]);
    if((workers!=1&&workers!=3&&workers!=4)||rounds<10) throw std::invalid_argument("Need workers=1,3,4 and >=10 rounds");
    omp_set_dynamic(0);omp_set_num_threads(workers);
    std::ifstream exists(argv[3]); if(exists.good()) throw std::invalid_argument("Refusing existing evidence path");
    std::ofstream output(argv[3]); if(!output) throw std::runtime_error("Cannot open new evidence file");
    CCParams<CryptoContextCKKSRNS> parameters;
    parameters.SetMultiplicativeDepth(2); parameters.SetScalingModSize(29); parameters.SetFirstModSize(30);
    parameters.SetRingDim(4096);parameters.SetBatchSize(2048);parameters.SetSecurityLevel(HEStd_128_classic);
    parameters.SetSecretKeyDist(UNIFORM_TERNARY);parameters.SetKeySwitchTechnique(BV);parameters.SetScalingTechnique(FIXEDMANUAL);
    auto cc=GenCryptoContext(parameters); cc->Enable(PKE); cc->Enable(KEYSWITCH); cc->Enable(LEVELEDSHE);
    const auto params=cc->GetCryptoParameters()->GetElementParams();
    std::cout<<"OPENFHE_STANDARD_PROFILE N="<<cc->GetRingDimension()<<" batch=2048 configured_security=HEStd_128_classic secret=UNIFORM_TERNARY scale_bits=29 moduli=";
    for(const auto& p:params->GetParams()) std::cout<<p->GetModulus()<<',';
    std::cout<<" threads="<<workers<<" omp_max="<<omp_get_max_threads()<<"\n";
    auto keys=cc->KeyGen(); if(!keys.good()) throw std::runtime_error("KeyGen failed");
    std::array<std::vector<double>,4> data;
    std::array<Plaintext,4> weight;const std::array<double,4> w={1.1,-0.7,0.9,0.4};const double bias=-0.35;
    std::mt19937 public_qa(20261007);std::uniform_real_distribution<double> distribution(-4,4);
    for(size_t f=0;f<4;f++) { data[f].resize(2048);for(auto& x:data[f]) x=distribution(public_qa);
      weight[f]=cc->MakeCKKSPackedPlaintext(std::vector<double>(2048,w[f])); }
    auto bias_plain=cc->MakeCKKSPackedPlaintext(std::vector<double>(2048,bias),2,0);
    output<<"round\tencode_us\tencrypt_us\tcloud_affine_us\tdecrypt_decode_us\thot_path_us\tkeygen_us\tmax_slot_error\n";
    double maximum=0;
    for(int r=-3;r<rounds;r++) {
      auto kt=Clock::now();auto generated=cc->KeyGen();auto ke=Clock::now();if(!generated.good()) throw std::runtime_error("Timed KeyGen failed");
      auto t0=Clock::now();std::array<Plaintext,4> plain;
      for(size_t f=0;f<4;f++) plain[f]=cc->MakeCKKSPackedPlaintext(data[f]);
      auto t1=Clock::now();std::array<Ciphertext<DCRTPoly>,4> cipher;
      for(size_t f=0;f<4;f++) cipher[f]=cc->Encrypt(keys.publicKey,plain[f]);
      auto t2=Clock::now();auto evaluated=cc->EvalMult(cipher[0],weight[0]);
      for(size_t f=1;f<4;f++) evaluated=cc->EvalAdd(evaluated,cc->EvalMult(cipher[f],weight[f]));
      evaluated=cc->EvalAdd(evaluated,bias_plain);
      auto t3=Clock::now();Plaintext decoded;cc->Decrypt(keys.secretKey,evaluated,&decoded);decoded->SetLength(2048);
      const auto values=decoded->GetRealPackedValue();auto t4=Clock::now();double error=0;
      for(size_t i=0;i<2048;i++) { double expected=bias;for(size_t f=0;f<4;f++) expected+=w[f]*data[f][i];
        error=std::max(error,std::abs(values.at(i)-expected)); }
      if(!std::isfinite(error)||error>5e-4) throw std::runtime_error("OpenFHE 2048-slot affine tolerance failed error="+std::to_string(error));
      maximum=std::max(maximum,error);
      if(r>=0) output<<std::setprecision(12)<<r<<'\t'<<micros(t0,t1)<<'\t'<<micros(t1,t2)<<'\t'<<micros(t2,t3)<<'\t'<<micros(t3,t4)<<'\t'<<micros(t0,t4)<<'\t'<<micros(kt,ke)<<'\t'<<error<<'\n';
    }
    output.flush();if(!output) throw std::runtime_error("Evidence write failed");
    std::cout<<"OPENFHE_STANDARD_CLIENT_BENCHMARK_PASS rounds="<<rounds<<" threads="<<workers<<" slots=2048 max_error="<<maximum<<" board=0\n";
    return 0;
  } catch(const std::exception& e) { std::cerr<<"FAIL: "<<e.what()<<"\n";return 1; }
}
