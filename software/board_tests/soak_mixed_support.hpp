// PS/host-only mixed exact stress runner. This file is never HLS/PL source.
#define main frozen_public_reference_main
#include "paired_public_vectors.cpp"
#undef main
#include <cerrno>
#include <cstring>
#include <regex>
#include <sstream>

#ifdef EDGEFHE_TEST_IOCTL
extern int edgefhe_test_ioctl(int,unsigned long,Request*);
#define checked_ioctl edgefhe_test_ioctl
#else
static int checked_ioctl(int fd,unsigned long cmd,Request* req){return ioctl(fd,cmd,req);}
#endif
struct Mixed {uint32_t op;std::vector<Beat> tx,gold;};
static uint32_t word(std::ifstream& f){uint32_t v;f.read(reinterpret_cast<char*>(&v),4);if(!f)throw std::runtime_error("Truncated mixed vectors");return v;}
static std::vector<Mixed> read_mixed(const char* path){
  std::ifstream f(path,std::ios::binary);char magic[8]{};f.read(magic,8);
  if(std::memcmp(magic,"EFMIXV1\0",8)||word(f)!=1||word(f)!=N||word(f)!=25)throw std::runtime_error("Wrong mixed header");
  for(auto q:Q)if(word(f)!=q)throw std::runtime_error("Wrong mixed primes");
  std::vector<Mixed> out;
  for(unsigned c=0;c<25;c++){
    Mixed v{};v.op=word(f);auto count=word(f);
    auto want=(v.op==4?3:(v.op>=2?2:1))*N;
    if(v.op>5||count!=want)throw std::runtime_error("Mixed operation/length mismatch");
    v.tx.resize(count);v.gold.resize(N);
    for(auto* vec:{&v.tx,&v.gold})for(auto& beat:*vec){
      for(auto& x:beat.limb)x=word(f);
      if(beat.limb[3])throw std::runtime_error("Nonzero vector padding");
      for(unsigned l=0;l<3;l++)if(beat.limb[l]>=Q[l])throw std::runtime_error("Noncanonical mixed vector");
    }
    out.push_back(std::move(v));
  }
  char extra;if(f.read(&extra,1))throw std::runtime_error("Trailing mixed vector bytes");
  return out;
}
static Rns mixed_cpu(const Mixed& v,const std::array<Plan,3>& plans){
  Case c{};c.op=v.op;
  for(unsigned l=0;l<3;l++){c.a[l].resize(N);c.b[l].resize(N);}
  if(v.op<=2){
    for(size_t i=0;i<N;i++)for(unsigned l=0;l<3;l++){
      c.a[l][i]=v.tx[i].limb[l];if(v.op==2)c.b[l][i]=v.tx[N+i].limb[l];
    }
    return cpu(c,plans);
  }
  Rns result;for(auto& l:result)l.resize(N);
  unsigned operands=v.op==4?3:2;
  for(size_t i=0;i<N;i++)for(unsigned l=0;l<3;l++){
    uint64_t a=v.tx[operands*i].limb[l],b=v.tx[operands*i+1].limb[l];
    result[l][i]=(v.op==5?b+Q[l]-a:a+b+(v.op==4?v.tx[operands*i+2].limb[l]:0))%Q[l];
  }
  return result;
}
static std::string interrupts(){std::ifstream f("/proc/interrupts");std::ostringstream s;s<<f.rdbuf();return s.str();}
static std::string selected(const std::string& raw){
#ifdef EDGEFHE_TEST_IOCTL
  (void)raw;return "LOCAL_MOCK_IRQ_NO_HARDWARE";
#else
  std::istringstream f(raw);std::string line,result;unsigned count=0;
  while(std::getline(f,line))if(std::regex_search(line,std::regex("GICv2\\s+(121|136)\\s+"))){result+=line+'\n';count++;}
  if(count!=2)throw std::runtime_error("Need both RNS IRQ lines");
  return result;
#endif
}
static void save(const std::string& path,const std::string& text){std::ofstream f(path);if(!f)throw std::runtime_error("Evidence write failed");f<<text;}
static Request descriptor(const Mixed& v,std::vector<Beat>& out){
  Request r{};r.operation=v.op;r.input_words=v.tx.size();r.output_words=N;
  r.input_ptr=reinterpret_cast<uint64_t>(v.tx.data());r.output_ptr=reinterpret_cast<uint64_t>(out.data());r.timeout_ms=5000;return r;
}
static unsigned rejects(int fd,Mixed& v,std::vector<Beat>& out,const std::string& dir){
  const auto cmd=_IOWR('E',0x04,Request);unsigned n=0;
  auto before=interrupts();save(dir+"/reject_interrupts_before.txt",before);
  auto test=[&](const std::string& label,Request req,int expected,unsigned long command=0,bool null_descriptor=false){
    int ret=checked_ioctl(fd,command?command:cmd,null_descriptor?nullptr:&req);int got=errno;
    if(ret!=-1||got!=expected)throw std::runtime_error("Wrong rejection "+label+" errno="+std::to_string(got));
    if(!null_descriptor&&(!command||command==cmd)&&req.status!=-expected)throw std::runtime_error("Rejection status differs "+label);
    ++n;std::cout<<"REJECTION_PASS label="<<label<<" errno="<<expected<<'\n';
  };
  auto base=descriptor(v,out);test("wrong_command",base,ENOTTY,cmd^1);
  test("null_descriptor",base,EFAULT,0,true);
  for(unsigned field=0;field<8;field++){
    auto r=base;
    if(field==0)r.reserved0=1;
    if(field==1)r.reserved=1;
    if(field==2)r.timeout_ms=30001;
    if(field==3)r.operation=6;
    if(field==4)r.input_words=0;
    if(field==5)r.output_words=N-1;
    if(field==6)r.input_ptr=0;
    if(field==7)r.output_ptr=0;
    test("descriptor_"+std::to_string(field),r,field>=6?EFAULT:EINVAL);
  }
  auto r=base;r.input_ptr=1;test("unmapped_input",r,EFAULT);
  for(auto index:{size_t(0),v.tx.size()-1})for(unsigned limb=0;limb<4;limb++){
    auto saved=v.tx[index].limb[limb];v.tx[index].limb[limb]=limb==3?1:Q[limb];
    try{test("noncanonical_or_padding_"+std::to_string(index)+"_"+std::to_string(limb),base,EINVAL);}
    catch(...){v.tx[index].limb[limb]=saved;throw;}v.tx[index].limb[limb]=saved;
  }
  auto after=interrupts();save(dir+"/reject_interrupts_after.txt",after);
  if(n!=19||selected(before)!=selected(after))throw std::runtime_error("Rejected requests changed IRQ/count");
  std::cout<<"REJECTION_PHASE_ZERO_IRQ_PASS cases=19\n";return n;
}
int frozen_mixed_runner_main(int argc,char** argv){try{
  if(argc<3)throw std::runtime_error("usage: mixed.bin --cpu-check | mixed.bin --run output_directory");
  auto cases=read_mixed(argv[1]);const std::array<Plan,3> plans={Plan(Q[0],PSI[0]),Plan(Q[1],PSI[1]),Plan(Q[2],PSI[2])};
  for(const auto& c:cases){auto gold=mixed_cpu(c,plans);for(size_t i=0;i<N;i++)for(unsigned l=0;l<3;l++)
    if(gold[l][i]!=c.gold[i].limb[l])throw std::runtime_error("CPU mixed oracle mismatch");}
  std::cout<<"MIXED_CPU_ORACLE_PASS cases=25 coefficients=307200\n";
  if(std::string(argv[2])=="--cpu-check")return 0;
  if(std::string(argv[2])!="--run"||argc!=4)throw std::runtime_error("Bad run arguments");
#ifndef EDGEFHE_TEST_IOCTL
  struct utsname env{};uname(&env);if(std::string(env.machine)!="aarch64")throw std::runtime_error("Only reviewed aarch64 board run supported");
#endif
  std::string dir=argv[3];std::ofstream rows(dir+"/transactions.tsv");
  if(!rows)throw std::runtime_error("Cannot write transaction rows");
  rows<<"job\tblock\tround\tcase\top\tkind\tnative_wall_ns\tdriver_us\tcoefficients\tpadding\n";
  std::vector<Beat> out(N);unsigned jobs=0,opened=0,closed=0,rejected=0;
  auto run=[&](int fd,unsigned block,int round,unsigned index,const char* kind){
    auto& c=cases[index];std::fill(out.begin(),out.end(),Beat{});auto req=descriptor(c,out);
    auto start=Clock::now();if(checked_ioctl(fd,_IOWR('E',0x04,Request),&req)<0||req.status)
      throw std::runtime_error("DMA failed; stop without retry job="+std::to_string(jobs+1));
    if(std::memcmp(out.data(),c.gold.data(),N*sizeof(Beat))||req.elapsed_us==0)
      throw std::runtime_error("Output/padding or driver timing mismatch");
    auto wall=std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-start).count();
    rows<<++jobs<<'\t'<<block<<'\t'<<round<<'\t'<<index<<'\t'<<c.op<<'\t'<<kind<<'\t'<<wall<<'\t'<<req.elapsed_us<<'\t'<<3*N<<'\t'<<N<<'\n';rows.flush();
  };
  for(unsigned block=0;block<4;block++){
#ifdef EDGEFHE_TEST_IOCTL
    int fd=100;
#else
    int fd=open("/dev/edgefhe_rns3_n4096_dma",O_RDWR|O_CLOEXEC);
#endif
    if(fd<0)throw std::runtime_error("Device open failed");
    opened++;
    try{
      if(block==0)rejected=rejects(fd,cases[0],out,dir);
      run(fd,block,-1,0,"post_open_probe");
      for(unsigned round=10*block;round<10*(block+1);round++)for(unsigned i=0;i<25;i++)run(fd,block,round,i,"matrix");
    }catch(...){
#ifndef EDGEFHE_TEST_IOCTL
      close(fd);
#endif
      throw;
    }
#ifndef EDGEFHE_TEST_IOCTL
    if(close(fd))throw std::runtime_error("Device close failed");
#endif
    closed++;std::cout<<"MIXED_BLOCK_PASS block="<<block<<" completed="<<jobs<<'\n';
  }
  if(jobs!=1004||opened!=4||closed!=4||rejected!=19)throw std::runtime_error("Wrong final counts");
  std::cout<<"MIXED_STRESS_NATIVE_PASS jobs=1004 rejects=19 opens=4 closes=4 coefficients=12337152 padding=4112384\n";
  return 0;
}catch(const std::exception& e){std::cerr<<"FAIL: "<<e.what()<<'\n';return 1;}}
