// PS/host-only independent O(N log N) reference and paired DMA benchmark.
// % and / here execute on the CPU; this file is never an HLS/PL source.
#include <array>
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/utsname.h>
#include <unistd.h>
using Poly=std::vector<uint32_t>;
using Rns=std::array<Poly,3>;
constexpr size_t N=4096;
constexpr std::array<uint32_t,3> Q={998244353,1004535809,469762049};
constexpr std::array<uint32_t,3> PSI={350007156,2062645,62025685};
using Clock=std::chrono::steady_clock;
uint32_t mul(uint32_t a,uint32_t b,uint32_t q){return uint64_t(a)*b%q;}
uint32_t power(uint32_t a,uint32_t b,uint32_t q){uint32_t r=1;while(b){if(b&1)r=mul(r,a,q);a=mul(a,a,q);b>>=1;}return r;}
struct Plan{
  uint32_t q,inv_n; Poly twist,untwist; std::array<Poly,12> roots,inv_roots;
  Plan(uint32_t modulus,uint32_t psi):q(modulus),inv_n(power(N,q-2,q)),twist(N),untwist(N){
    const auto ipsi=power(psi,q-2,q),omega=mul(psi,psi,q),iomega=power(omega,q-2,q);
    if(power(psi,N,q)!=q-1 || power(psi,2*N,q)!=1)throw std::runtime_error("Bad negacyclic root");
    twist[0]=untwist[0]=1;
    for(size_t i=1;i<N;i++){twist[i]=mul(twist[i-1],psi,q);untwist[i]=mul(untwist[i-1],ipsi,q);}
    for(size_t s=0,half=1;s<12;s++,half*=2){
      roots[s].resize(half); inv_roots[s].resize(half);roots[s][0]=inv_roots[s][0]=1;
      auto w=power(omega,N/(2*half),q),iw=power(iomega,N/(2*half),q);
      for(size_t j=1;j<half;j++){roots[s][j]=mul(roots[s][j-1],w,q);inv_roots[s][j]=mul(inv_roots[s][j-1],iw,q);}
    }
  }
  void cyclic(Poly& a,bool inverse)const{
    for(size_t i=1,j=0;i<N;i++){size_t bit=N/2;for(;j&bit;bit>>=1)j^=bit;j^=bit;if(i<j)std::swap(a[i],a[j]);}
    for(size_t s=0,half=1;s<12;s++,half*=2){
      const auto& w=inverse?inv_roots[s]:roots[s];
      for(size_t base=0;base<N;base+=2*half)for(size_t j=0;j<half;j++){
        uint32_t u=a[base+j],v=mul(a[base+j+half],w[j],q);
        uint32_t sum=u+v;a[base+j]=sum>=q?sum-q:sum;
        a[base+j+half]=u>=v?u-v:u+q-v;
      }
    }
    if(inverse)for(auto& x:a)x=mul(x,inv_n,q);
  }
  Poly transform(const Poly& in,bool inverse)const{
    Poly a=in;
    if(!inverse)for(size_t i=0;i<N;i++)a[i]=mul(a[i],twist[i],q);
    cyclic(a,inverse);
    if(inverse)for(size_t i=0;i<N;i++)a[i]=mul(a[i],untwist[i],q);
    return a;
  }
};
struct Case{uint32_t op; Rns a,b,gold;};
uint32_t read32(std::istream& f){unsigned char b[4];f.read(reinterpret_cast<char*>(b),4);if(!f)throw std::runtime_error("Truncated vectors");return b[0]|uint32_t(b[1])<<8|uint32_t(b[2])<<16|uint32_t(b[3])<<24;}
std::vector<Case> load(const std::string& path){
  std::ifstream f(path,std::ios::binary);char magic[8]{};f.read(magic,8);
  if(!f)throw std::runtime_error("Cannot read vector header");
  if(std::string(magic,8)!=std::string("EFNTTV1\0",8))throw std::runtime_error("Bad magic");
  if(read32(f)!=1||read32(f)!=N||read32(f)!=3||read32(f)!=16)throw std::runtime_error("Bad header");
  for(auto q:Q)if(read32(f)!=q)throw std::runtime_error("Wrong modulus");
  for(auto p:PSI)if(read32(f)!=p)throw std::runtime_error("Wrong psi");
  std::vector<Case> cases(16);
  for(size_t c=0;c<cases.size();c++){
    auto& t=cases[c];t.op=read32(f);auto wanted=c<12?c%2:c==12?1:2;
    if(t.op!=wanted)throw std::runtime_error("Unexpected operation");
    for(auto* p:{&t.a,&t.b,&t.gold})for(size_t l=0;l<3;l++){
      (*p)[l].resize(N);for(auto& x:(*p)[l]){x=read32(f);if(x>=Q[l])throw std::runtime_error("Noncanonical coefficient");}
    }
  }
  if(f.peek()!=std::char_traits<char>::eof())throw std::runtime_error("Trailing vector data");
  return cases;
}
Rns cpu(const Case& t,const std::array<Plan,3>& plans){
  Rns out;
  for(size_t l=0;l<3;l++){
    if(t.op<2)out[l]=plans[l].transform(t.a[l],t.op==1);
    else{auto a=plans[l].transform(t.a[l],false),b=plans[l].transform(t.b[l],false);
      for(size_t i=0;i<N;i++)a[i]=mul(a[i],b[i],Q[l]);
      out[l]=plans[l].transform(a,true);}
  }return out;
}
struct alignas(16) Beat{uint32_t limb[4];};
struct Request{uint32_t operation,reserved0,input_words,output_words;uint64_t input_ptr,output_ptr;uint32_t timeout_ms,elapsed_us;int32_t status;uint32_t reserved;};
static_assert(sizeof(Beat)==16 && sizeof(Request)==48,"ABI mismatch");
struct HardwareResult{Rns output;double wall_us;uint32_t driver_us;};
HardwareResult hardware(int fd,const Case& t){
  auto start=Clock::now();std::vector<Beat> in(t.op==2?2*N:N),out(N);
  for(size_t i=0;i<N;i++)for(size_t l=0;l<3;l++){
    in[i].limb[l]=t.a[l][i];if(t.op==2)in[N+i].limb[l]=t.b[l][i];
  }
  Request req{};req.operation=t.op;req.input_words=in.size();req.output_words=N;
  req.input_ptr=reinterpret_cast<uint64_t>(in.data());req.output_ptr=reinterpret_cast<uint64_t>(out.data());req.timeout_ms=5000;
  if(ioctl(fd,_IOWR('E',0x04,Request),&req)<0 || req.status)throw std::runtime_error("DMA ioctl failed: stop and retain diagnostics; no automatic retry");
  Rns result;for(size_t l=0;l<3;l++)result[l].resize(N);
  for(size_t i=0;i<N;i++){if(out[i].limb[3])throw std::runtime_error("Nonzero padding");for(size_t l=0;l<3;l++)result[l][i]=out[i].limb[l];}
  auto ns=std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-start).count();
  return {std::move(result),ns/1000.0,req.elapsed_us};
}
int main(int argc,char** argv){try{
  if(argc<3){std::cerr<<"usage: vectors.bin --cpu-check | vectors.bin --paired output.tsv rounds warmups operator_hardware_label\n";return 2;}
  auto cases=load(argv[1]);std::array<Plan,3> plans={Plan(Q[0],PSI[0]),Plan(Q[1],PSI[1]),Plan(Q[2],PSI[2])};
  for(size_t i=0;i<cases.size();i++){if(cpu(cases[i],plans)!=cases[i].gold)throw std::runtime_error("CPU/OpenFHE coefficient mismatch case="+std::to_string(i));}
  std::cout<<"CPU_OPENFHE_EXACT_PASS cases=16 coefficients=196608\n";
  if(std::string(argv[2])=="--cpu-check")return 0;
  if(std::string(argv[2])!="--paired"||argc!=7)throw std::runtime_error("Bad arguments");
  int rounds=std::stoi(argv[4]),warm=std::stoi(argv[5]);if(rounds<20||rounds>10000||warm<1||warm>1000)throw std::runtime_error("Bad sample counts");
  struct utsname env{};uname(&env);if(std::string(env.machine)!="aarch64")throw std::runtime_error("Paired board measurements require aarch64");
  std::ofstream output(argv[3],std::ios::out|std::ios::app);
  if(!output||output.tellp()!=0)throw std::runtime_error("Refusing to overwrite/append benchmark file");
  output<<"# architecture="<<env.machine<<" kernel="<<env.release<<" operator_label="<<argv[6]<<"\n";
  output<<"# CPU baseline=portable uint64 modulo DIT NTT; precomputed plans; not claimed fastest ARM implementation\n";
  output<<"# FPGA wall includes allocation+pack+ioctl+unpack, excludes open/plan/oracle/comparison; driver_us includes DMA+IP+IRQ wait\n";
  output<<"round\tcase\top\torder\tcpu_wall_us\tfpga_wall_us\tdriver_us\n";output.flush();
  int fd=open("/dev/edgefhe_rns3_n4096_dma",O_RDWR);if(fd<0)throw std::runtime_error("Device unavailable");
  for(int r=-warm;r<rounds;r++)for(size_t c=0;c<cases.size();c++){
    Rns co;HardwareResult hw;double cpu_us=0;bool cpu_first=((r+warm+int(c))&1)==0;
    auto timed_cpu=[&]{auto start=Clock::now();co=cpu(cases[c],plans);cpu_us=std::chrono::duration<double,std::micro>(Clock::now()-start).count();};
    if(cpu_first){timed_cpu();hw=hardware(fd,cases[c]);}else{hw=hardware(fd,cases[c]);timed_cpu();}
    if(co!=cases[c].gold||hw.output!=cases[c].gold)throw std::runtime_error("Paired exact comparison failed");
    if(r>=0){output<<r<<'\t'<<c<<'\t'<<cases[c].op<<'\t'<<(cpu_first?"cpu-first":"fpga-first")<<'\t'<<cpu_us<<'\t'<<hw.wall_us<<'\t'<<hw.driver_us<<'\n';output.flush();}
  }
  close(fd);std::cout<<"PAIRED_BENCHMARK_PASS samples="<<rounds*16<<" coefficients="<<uint64_t(rounds+warm)*16*12288<<"\n";return 0;
}catch(const std::exception& e){std::cerr<<"FAIL: "<<e.what()<<'\n';return 1;}}
