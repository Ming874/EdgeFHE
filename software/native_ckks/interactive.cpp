// Standalone CPU-only native functional demonstration; private values use stdin.
#include "core.hpp"
#include <chrono>
#include <iomanip>
#include <iostream>
#include <sstream>
int main() {
  try {
    std::array<double,4> x{};
    std::cerr<<"Enter four finite feature values in [-16,16] on stdin:\n";
    std::string line; if(!std::getline(std::cin,line)) throw std::invalid_argument("Input required");
    std::istringstream input(line);
    for(auto& v:x) if(!(input>>v)) throw std::invalid_argument("Four numeric features required");
    std::string extra; if(input>>extra) throw std::invalid_argument("Unexpected fifth input");
    edgefhe::Engine cpu(3); const auto start=std::chrono::steady_clock::now();
    const auto material=edgefhe::sample_material(); const auto key=cpu.keygen(material);
    std::array<edgefhe::Message,4> message,weight;
    const std::array<double,4> w={1.1,-0.7,0.9,0.4}; const double bias=-0.35;
    for(size_t i=0;i<4;i++) { message[i]=edgefhe::encode({x[i]}); weight[i]=edgefhe::encode({w[i]}); }
    const auto bias_message=edgefhe::encode({bias},edgefhe::scale*edgefhe::scale);
    const auto bound=edgefhe::no_wrap_bound(message,weight,bias_message);
    const auto ciphertext=cpu.encrypt(key,message,material.mask,material.e0,material.e1);
    const auto evaluated=cpu.affine(ciphertext,weight,bias_message);
    const auto recovered=cpu.decrypt(evaluated,material.secret);
    for(auto c:recovered) edgefhe::require((c<0?edgefhe::UWide(-c):edgefhe::UWide(c))<=bound,"Decrypted coefficient bound failed");
    const auto output=edgefhe::decode(recovered,edgefhe::scale*edgefhe::scale);
    double expected=bias;for(size_t i=0;i<4;i++)expected+=x[i]*w[i];
    edgefhe::require(std::abs(output[0]-expected)<=edgefhe::affine_error_bound(w,16),"Affine numerical bound failed");
    std::cout<<std::setprecision(12)<<"NATIVE_CPU_DYNAMIC_DEMO_PASS logit="<<output[0].real()
      <<" total_ms="<<std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-start).count()<<"\n";
    return 0;
  } catch(const std::exception& e) { std::cerr<<"FAIL: "<<e.what()<<"\n"; return 1; }
}
