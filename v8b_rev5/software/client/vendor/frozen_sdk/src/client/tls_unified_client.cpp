// Native C++ dynamic-input client. Ciphertext-only wire, pinned CA + mTLS.
#include "secure.hpp"
#include "secure_bridge.hpp"
#include <memory>
#include <openssl/ssl.h>
#include <openssl/sha.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <array>
#include <algorithm>
#include <numeric>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
using namespace edgefhe;
using Bytes=std::vector<unsigned char>;
using Hash=std::array<unsigned char,32>;
using Clock=std::chrono::steady_clock;
static const std::string profile_text="edgefhe-v7-n4096-q998244353,1004535809,469762049-s30-uniformternary-dgg4.5-openfhe1.5.1";
static const std::string model_text="affine:1.1,-0.7,0.9,0.4;-0.35;scale=1073741824";
static Hash hash(const Bytes& x){Hash out{};SHA256(x.data(),x.size(),out.data());return out;}
static Hash hash(const std::string& x){return hash(Bytes(x.begin(),x.end()));}
static void put(Bytes& b,uint64_t x,unsigned n){for(unsigned i=0;i<n;i++)b.push_back((x>>(8*i))&255);}
static uint64_t get(const Bytes& b,size_t offset,unsigned n){require(offset+n<=b.size(),"Truncated packet");uint64_t x=0;for(unsigned i=0;i<n;i++)x|=uint64_t(b[offset+i])<<(8*i);return x;}
template<size_t N> static void append(Bytes& b,const std::array<unsigned char,N>& x){b.insert(b.end(),x.begin(),x.end());}
static Bytes payload(const std::array<Cipher,4>& cipher){Bytes out;out.reserve(8*3*degree*4);for(const auto& c:cipher)for(const Rns* p:{&c.c0,&c.c1})for(size_t l=0;l<3;l++)for(auto v:(*p)[l])put(out,v,4);return out;}
static std::array<unsigned char,16> request_id(){std::array<unsigned char,16> id{};size_t n=0;while(n<id.size()){auto r=getrandom(id.data()+n,id.size()-n,0);if(r<0&&errno==EINTR)continue;require(r>0,"Request entropy failed");n+=size_t(r);}return id;}
static Bytes packet(const std::array<Cipher,4>& cipher,size_t records,const std::array<unsigned char,16>& id){
  auto p=payload(cipher);Bytes out={'E','F','C','7','P','K','T',0};put(out,7,4);put(out,0,4);put(out,records,4);put(out,p.size()/4,4);put(out,0,8);
  append(out,hash(profile_text));append(out,hash(model_text));append(out,id);append(out,Hash{});append(out,hash(p));
  require(out.size()==176,"Header ABI");out.insert(out.end(),p.begin(),p.end());append(out,hash(out));return out;
}
static Cipher response(const Bytes& raw,size_t records,const std::array<unsigned char,16>& id,const Hash& request_digest,uint64_t& cloud_ns){
  require(raw.size()==176+2*3*degree*4+32,"Response size");Bytes content(raw.begin(),raw.end()-32);auto digest=hash(content);
  require(std::equal(digest.begin(),digest.end(),raw.end()-32),"Response digest");
  require(std::memcmp(raw.data(),"EFC7PKT\0",8)==0&&get(raw,8,4)==7&&get(raw,12,4)==1&&get(raw,16,4)==records&&get(raw,20,4)==2*3*degree,"Response profile dimensions");
  auto profile=hash(profile_text),model=hash(model_text);
  require(std::equal(profile.begin(),profile.end(),raw.begin()+32)&&std::equal(model.begin(),model.end(),raw.begin()+64),"Response profile/model binding");
  require(std::equal(id.begin(),id.end(),raw.begin()+96)&&std::equal(request_digest.begin(),request_digest.end(),raw.begin()+112),"Response request binding");
  Bytes p(raw.begin()+176,raw.end()-32);auto ph=hash(p);require(std::equal(ph.begin(),ph.end(),raw.begin()+144),"Response payload digest");
  cloud_ns=get(raw,24,8);Cipher out;size_t offset=0;
  for(Rns* c:{&out.c0,&out.c1})for(size_t l=0;l<3;l++){(*c)[l].resize(degree);for(auto& x:(*c)[l]){x=uint32_t(get(p,offset,4));offset+=4;require(x<primes[l],"Noncanonical response");}}
  return out;
}
class TLS {
  SSL_CTX* context_=nullptr;SSL* ssl_=nullptr;int socket_=-1;
 public:
  TLS(const char* ip,int port,const char* ca,const char* cert,const char* key){
    try{
      require(port>0&&port<=65535,"Invalid port");sockaddr_in address{};address.sin_family=AF_INET;address.sin_port=htons(uint16_t(port));require(inet_pton(AF_INET,ip,&address.sin_addr)==1,"IPv4 endpoint required");
      context_=SSL_CTX_new(TLS_client_method());require(context_,"TLS context");require(SSL_CTX_set_min_proto_version(context_,TLS1_2_VERSION)==1,"TLS minimum");
      SSL_CTX_set_verify(context_,SSL_VERIFY_PEER,nullptr);
      require(SSL_CTX_load_verify_locations(context_,ca,nullptr)==1,"CA load");require(SSL_CTX_use_certificate_chain_file(context_,cert)==1,"Client certificate");
      require(SSL_CTX_use_PrivateKey_file(context_,key,SSL_FILETYPE_PEM)==1&&SSL_CTX_check_private_key(context_)==1,"Client private key");
      socket_=socket(AF_INET,SOCK_STREAM,0);require(socket_>=0,"Socket create");timeval timeout{20,0};
      require(setsockopt(socket_,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout))==0&&setsockopt(socket_,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout))==0,"Socket timeout");
      if(connect(socket_,reinterpret_cast<sockaddr*>(&address),sizeof(address))!=0)
        throw std::runtime_error(std::string("Peer connect errno=")+std::to_string(errno)+" "+std::strerror(errno));
      ssl_=SSL_new(context_);require(ssl_,"TLS session");require(SSL_set_fd(ssl_,socket_)==1,"TLS socket");
      require(X509_VERIFY_PARAM_set1_ip_asc(SSL_get0_param(ssl_),ip)==1,"TLS peer IP");require(SSL_connect(ssl_)==1&&SSL_get_verify_result(ssl_)==X509_V_OK,"mTLS peer verification");
    }catch(...){clear();throw;}
  }
  void clear(){if(ssl_){SSL_free(ssl_);ssl_=nullptr;}if(socket_>=0){close(socket_);socket_=-1;}if(context_){SSL_CTX_free(context_);context_=nullptr;}}
  ~TLS(){clear();}
  void write(const Bytes& x){size_t done=0;while(done<x.size()){int n=SSL_write(ssl_,x.data()+done,int(x.size()-done));require(n>0,"TLS send failed; no retry");done+=size_t(n);}}
  Bytes read(size_t size){Bytes x(size);size_t done=0;while(done<size){int n=SSL_read(ssl_,x.data()+done,int(size-done));require(n>0,"TLS receive failed; no retry");done+=size_t(n);}return x;}
  Bytes call(const Bytes& x){Bytes length;put(length,x.size(),4);write(length);write(x);auto header=read(4);auto n=get(header,0,4);require(n>208&&n<=512*1024,"Peer frame length");return read(size_t(n));}
};
static uint64_t ns(Clock::time_point a,Clock::time_point b){return std::chrono::duration_cast<std::chrono::nanoseconds>(b-a).count();}
int main(int argc,char** argv){try{
  require(argc==13,"usage: tls_client PLUGIN WORKERS INPUT.csv ROUNDS OUTPUT.tsv PRIVATE_RESULT IP PORT CA CERT KEY CPU_OR_DEVICE");
  const unsigned workers=std::stoul(argv[2]);const size_t rounds=std::stoul(argv[4]);require(rounds>0,"Round count");
  std::ifstream input(argv[3]);require(input.good(),"Private input file");std::array<std::vector<std::complex<double>>,4> x;std::string line;
  while(std::getline(input,line)){std::replace(line.begin(),line.end(),',',' ');std::istringstream row(line);double v[4];for(auto& value:v)require(bool(row>>value)&&std::isfinite(value)&&std::abs(value)<=16,"Input requires four finite standardized features <=16");std::string extra;require(!(row>>extra),"Extra feature");for(size_t f=0;f<4;f++)x[f].push_back(v[f]);}
  const size_t records=x[0].size();require(records>0&&records<=slots,"Input records 1..2048");
  auto cold_math_start=Clock::now();initialize_secure_sampler(argv[1]);Engine client(workers);auto material=sample_secure_material();auto key=keygen_secure(client,material);
  auto cold_math_ns=ns(cold_math_start,Clock::now());
  auto cache_start=Clock::now();std::unique_ptr<edgefhe_v7::UnifiedDevice> pl;
  if(std::string(argv[12])!="CPU"){pl=std::make_unique<edgefhe_v7::UnifiedDevice>(argv[12]);pl->load_key(key);}
  auto cache_ns=ns(cache_start,Clock::now());
  std::array<Message,4> weight;const std::array<double,4> w={1.1,-.7,.9,.4};for(size_t f=0;f<4;f++)weight[f]=encode(std::vector<std::complex<double>>(slots,w[f]));auto bias=encode(std::vector<std::complex<double>>(slots,-.35),scale*scale);
  std::ifstream old(argv[5]);require(!old.good(),"Timing output already exists");std::ofstream out(argv[5]);require(out.good(),"Timing create");
  int private_fd=open(argv[6],O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);require(private_fd>=0,"Private output exclusive create");close(private_fd);std::ofstream private_out(argv[6]);
  auto setup_start=Clock::now();TLS transport(argv[7],std::stoi(argv[8]),argv[9],argv[10],argv[11]);auto setup_ns=ns(setup_start,Clock::now());
  out<<"sample\twarmup\tworkers\trecords\tstandardize_ns\tencode_ns\tsample_ns\tencrypt_ns\tpack_ns\tnetwork_TLS_ns\tcloud_ns\tunpack_ns\tdecrypt_ns\tdecode_classify_ns\tE2E_wall_ns\tdriver_dma_us\tDMA_calls\n";
  double max_error=0;
  for(size_t round=0;round<rounds+3;round++){
    uint64_t before_driver=pl?pl->driver_elapsed_total_us():0,before_calls=pl?pl->completed_calls():0;
    std::array<uint64_t,10> stage{};auto start=Clock::now(),last=start;auto values=x; // Frozen identity normalization: mean=0, stddev=1.
    auto now=Clock::now();stage[0]=ns(last,now);last=now;auto message=client.encode_four(values);now=Clock::now();stage[1]=ns(last,now);last=now;
    sample_secure_encryption(material);now=Clock::now();stage[2]=ns(last,now);last=now;require(affine_coefficient_bound(message,weight,bias,material)<UWide(primes[0])*primes[1]*primes[2]/2,"No-wrap guard");
    auto encrypted=pl?edgefhe_v7::encrypt_secure_paired(pl->session(),message,material):encrypt_secure(client,key,message,material);now=Clock::now();stage[3]=ns(last,now);last=now;auto id=request_id();auto request=packet(encrypted,records,id);auto req_hash=hash(request);now=Clock::now();stage[4]=ns(last,now);last=now;
    auto raw=transport.call(request);now=Clock::now();auto roundtrip=ns(last,now);last=now;uint64_t cloud=0;auto evaluated=response(raw,records,id,req_hash,cloud);
    require(cloud<=roundtrip,"Impossible peer duration");stage[5]=roundtrip-cloud;stage[6]=cloud;now=Clock::now();stage[7]=ns(last,now);last=now;
    auto integer=(pl?edgefhe_v7::decrypt_secure_unified(*pl,evaluated,material.secret):decrypt_secure(client,evaluated,material.secret));now=Clock::now();stage[8]=ns(last,now);last=now;auto decoded=decode(integer,scale*scale);
    const double bound=secure_affine_error_bound(weight,material,16.0);
    for(size_t i=0;i<records;i++){auto probability=sigmoid(decoded[i].real());auto decision=classify(decoded[i].real(),bound);(void)probability;(void)decision;}
    now=Clock::now();stage[9]=ns(last,now);auto wall=ns(start,now);
    for(size_t i=0;i<records;i++){double expected=-.35;for(size_t f=0;f<4;f++)expected+=w[f]*x[f][i].real();double error=std::abs(decoded[i]-expected);max_error=std::max(max_error,error);require(error<=bound&&error<=5e-4,"E2E precision gate");if(round==rounds+2)private_out<<i<<'\t'<<decoded[i].real()<<'\t'<<sigmoid(decoded[i].real())<<'\t'<<classify(decoded[i].real(),bound)<<'\n';}
    require(std::accumulate(stage.begin(),stage.end(),uint64_t(0))==wall,"E2E accounting");
    out<<round<<'\t'<<(round<3)<<'\t'<<workers<<'\t'<<records;for(auto v:stage)out<<'\t'<<v;out<<'\t'<<wall<<'\t'<<(pl?pl->driver_elapsed_total_us()-before_driver:0)<<'\t'<<(pl?pl->completed_calls()-before_calls:0)<<'\n';
  }
  out.close();private_out.close();require(out.good()&&private_out.good(),"Results write");
  std::cout<<"V7_NATIVE_CPP_MTLS_DYNAMIC_PASS records="<<records<<" measured="<<rounds<<" max_slot_error="<<max_error<<" TLS_setup_ns="<<setup_ns<<" cold_math_ns="<<cold_math_ns<<" cold_cache_ns="<<cache_ns<<" actual_DMA="<<(pl?pl->completed_calls():0)<<" two_machine_acceptance_pending=1\n";return 0;
}catch(const std::exception& e){std::cerr<<"V7_TLS_CLIENT_FAIL "<<e.what()<<'\n';return 1;}}
