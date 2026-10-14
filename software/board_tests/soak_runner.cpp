// PS-only duration-controlled exact soak. No PL or driver modification.
#include "soak_mixed_support.hpp"
#include <filesystem>
int main(int argc,char** argv){try{
  if(argc!=5)throw std::runtime_error("usage: vectors --cpu-check|--run output seconds");
  const auto seconds=std::stoul(argv[4]);
  auto cases=read_mixed(argv[1]);
  const std::array<Plan,3> plans={Plan(Q[0],PSI[0]),Plan(Q[1],PSI[1]),Plan(Q[2],PSI[2])};
  for(const auto& c:cases){auto gold=mixed_cpu(c,plans);for(size_t i=0;i<N;i++)for(unsigned l=0;l<3;l++)
    if(gold[l][i]!=c.gold[i].limb[l])throw std::runtime_error("CPU soak oracle mismatch");}
  std::cout<<"SOAK_CPU_ORACLE_PASS cases=25 coefficients=307200\n";
  if(std::string(argv[2])=="--cpu-check")return 0;
  if(std::string(argv[2])!="--run")throw std::runtime_error("Wrong mode");
#ifndef EDGEFHE_TEST_IOCTL
  struct utsname env{};uname(&env);
  if(std::string(env.machine)!="aarch64" || seconds!=21600)throw std::runtime_error("Production run requires aarch64 and exactly 21600 seconds");
#else
  if(seconds<1 || seconds>3)throw std::runtime_error("Local mock duration only 1..3 seconds");
#endif
  std::string dir=argv[3];
  if(std::filesystem::exists(dir+"/transactions.tsv"))throw std::runtime_error("Fresh output required");
  std::ofstream rows(dir+"/transactions.tsv");if(!rows)throw std::runtime_error("Cannot write rows");
  rows<<"job\tblock\tround\tcase\top\tkind\tnative_wall_ns\tdriver_us\tcoefficients\tpadding\n";
  std::vector<Beat> out(N);uint64_t jobs=0,round=0,opens=0,closes=0;
  const auto begin=Clock::now();
  auto elapsed=[&](){return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-begin).count();};
  do{
#ifdef EDGEFHE_TEST_IOCTL
    int fd=100;
#else
    int fd=open("/dev/edgefhe_rns3_n4096_dma",O_RDWR|O_CLOEXEC);
#endif
    if(fd<0)throw std::runtime_error("Open failed; no retry");
    const auto block=opens++;const auto block_start=Clock::now();
    auto job=[&](unsigned index,const char* kind,int64_t round_id){
      if(jobs>=10000000)throw std::runtime_error("Bounded job limit exceeded");
      const auto& c=cases[index];std::fill(out.begin(),out.end(),Beat{});auto req=descriptor(c,out);
      const auto start=Clock::now();
      if(checked_ioctl(fd,_IOWR('E',0x04,Request),&req)<0 || req.status)throw std::runtime_error("DMA error; stop without retry");
      if(!req.elapsed_us || std::memcmp(out.data(),c.gold.data(),N*sizeof(Beat)))throw std::runtime_error("Gold/padding mismatch");
      const auto ns=std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-start).count();
      rows<<++jobs<<'\t'<<block<<'\t'<<round_id<<'\t'<<index<<'\t'<<c.op<<'\t'<<kind<<'\t'<<ns<<'\t'<<req.elapsed_us<<'\t'<<3*N<<'\t'<<N<<'\n';
      rows.flush();if(!rows)throw std::runtime_error("Evidence disk write failed");
    };
    try{
      job(0,"post_open_probe",-1);
      do{
        for(unsigned i=0;i<25;i++)job(i,"matrix",round);
        round++;
        if(round%100==0)std::cout<<"SOAK_PROGRESS jobs="<<jobs<<" rounds="<<round<<" elapsed_s="<<elapsed()/1000000000<<'\n'<<std::flush;
      }while(elapsed()<int64_t(seconds)*1000000000 && Clock::now()-block_start<std::chrono::seconds(300));
    }catch(...){
#ifndef EDGEFHE_TEST_IOCTL
      close(fd);
#endif
      throw;
    }
#ifndef EDGEFHE_TEST_IOCTL
    if(close(fd))throw std::runtime_error("Close failed");
#endif
    closes++;
    std::cout<<"SOAK_BLOCK_PASS block="<<block<<" jobs="<<jobs<<" elapsed_s="<<elapsed()/1000000000<<'\n'<<std::flush;
  }while(elapsed()<int64_t(seconds)*1000000000);
  const auto ns=elapsed();
  if(jobs!=25*round+opens || opens!=closes || ns<int64_t(seconds)*1000000000)throw std::runtime_error("Final duration/count mismatch");
  std::ofstream report(dir+"/native_report.json");if(!report)throw std::runtime_error("Native report write failed");
  report<<"{\"status\":\"PASS_NATIVE_DURATION_EXACT_SOAK\",\"jobs\":"<<jobs<<",\"rounds\":"<<round<<",\"opens\":"<<opens
        <<",\"closes\":"<<closes<<",\"elapsed_ns\":"<<ns<<",\"duration_seconds\":"<<seconds
        <<",\"coefficients\":"<<jobs*12288<<",\"padding\":"<<jobs*4096<<"}\n";
  report.close();if(!report)throw std::runtime_error("Native report final write failed");
  std::cout<<"SOAK_NATIVE_PASS jobs="<<jobs<<" rounds="<<round<<" opens="<<opens<<" closes="<<closes<<" elapsed_ns="<<ns<<'\n';
  return 0;
}catch(const std::exception& e){std::cerr<<"FAIL: "<<e.what()<<'\n';return 1;}}
