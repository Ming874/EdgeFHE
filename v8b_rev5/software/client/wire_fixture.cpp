#include "wire.hpp"
#include <iostream>
int main(int argc,char** argv){
  using namespace efnet;
  try{
    if(argc==3&&std::string(argv[1])=="--decode"){
      const std::string h=argv[2];need(h.size()==512,"FIXTURE_HEX_SIZE");HeaderBytes b{};
      for(size_t i=0;i<8;i++){const auto d=unhex(h.substr(i*64,64));std::copy(d.begin(),d.end(),b.begin()+32*i);}
      decode(b.data(),b.size());std::cout<<"HEADER_ACCEPTED\n";return 0;
    }
    need(argc==1,"FIXTURE_OPTIONS");Header h;h.features=10;h.rows=2048;h.bytes=10*ct_bytes;h.generation=7;h.sequence=1;
    h.profile_id=profile();h.model_id=unhex(catalogue()[1].source_sha256);h.key_id=digest(std::string("test public-key identity fixture"));h.session_id=digest(std::string("test TLS exporter fixture"));
    h.request_id=request_id(h);std::vector<uint8_t> body(h.bytes,0);h.request_hash=request_hash(h,body);const auto b=encode(h);
    constexpr char symbols[]="0123456789abcdef";std::string s;for(auto v:b){s+=symbols[v>>4];s+=symbols[v&15];}
    std::cout<<"{\"header_hex\":\""<<s<<"\",\"request_id\":\""<<hex(h.request_id)<<"\",\"request_hash\":\""<<hex(h.request_hash)<<"\"}\n";return 0;
  }catch(const std::exception& e){std::cerr<<"FIXTURE_REJECT "<<e.what()<<'\n';return 1;}
}
