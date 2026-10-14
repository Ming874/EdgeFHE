// PS/Linux only. Real mmap protections exercise two recoverable usercopy paths.
#include "soak_mixed_support.hpp"
#include <sys/mman.h>
#include <unistd.h>
#include <limits>
struct Irqs { uint64_t rx=0,tx=0; unsigned rx_id=0,tx_id=0; };
#ifdef EDGEFHE_TEST_IOCTL
extern Irqs edgefhe_test_irqs();
#endif
static Irqs read_irqs(){
#ifdef EDGEFHE_TEST_IOCTL
  return edgefhe_test_irqs();
#else
  Irqs result{};unsigned count=0;std::istringstream lines(interrupts());std::string line;
  while(std::getline(lines,line)){
    std::smatch match;
    if(!std::regex_search(line,match,std::regex("GICv2\\s+(121|136)\\s+")))continue;
    const auto pos=line.find(':');if(pos==std::string::npos)throw std::runtime_error("IRQ syntax");
    unsigned id=std::stoul(line.substr(0,pos));std::istringstream fields(line.substr(pos+1));
    uint64_t total=0;std::string token;unsigned cpus=0;
    while(fields>>token){if(token=="GICv2")break;
      if(token.find_first_not_of("0123456789")!=std::string::npos)throw std::runtime_error("IRQ counters");
      auto value=std::stoull(token);if(value>std::numeric_limits<uint64_t>::max()-total)throw std::runtime_error("IRQ overflow");total+=value;cpus++;
    }
    if(cpus!=4)throw std::runtime_error("Reviewed four CPU IRQ counters required");
    if(match[1]=="136"){if(result.rx_id)throw std::runtime_error("Duplicate RX IRQ");result.rx=total;result.rx_id=id;}
    else {if(result.tx_id)throw std::runtime_error("Duplicate TX IRQ");result.tx=total;result.tx_id=id;}count++;
  }
  if(count!=2||!result.rx_id||!result.tx_id||result.rx_id==result.tx_id)throw std::runtime_error("Both registered RNS IRQs required");
  return result;
#endif
}
struct Mapping {
  void* base;size_t size;
  explicit Mapping(size_t bytes):base(mmap(nullptr,bytes,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0)),size(bytes){
    if(base==MAP_FAILED)throw std::runtime_error("mmap failed");}
  Mapping(const Mapping&)=delete;Mapping& operator=(const Mapping&)=delete;
  ~Mapping(){if(base!=MAP_FAILED)munmap(base,size);}
  void protect(void* address,size_t bytes,int flags){if(mprotect(address,bytes,flags))throw std::runtime_error("mprotect failed");}
};
static void execute(int fd,const Mixed& c,unsigned index,unsigned row,const char* kind,size_t page,
                    std::ofstream& rows,std::ofstream& results){
  const bool input_fault=std::string(kind)=="input_copy_fault";
  const bool readonly=std::string(kind)=="descriptor_copyback_fault";
  std::vector<Beat> out(N);std::memset(out.data(),0xa5,out.size()*sizeof(Beat));
  Request request=descriptor(c,out);request.status=-777;request.elapsed_us=0x12345678;
  const size_t bytes=c.tx.size()*sizeof(Beat);
  Mapping input(bytes+page),desc(page);
  if(input_fault){
    auto* ptr=static_cast<unsigned char*>(input.base)+page-16;
    std::memcpy(ptr,c.tx.data(),bytes);
    input.protect(static_cast<unsigned char*>(input.base)+page,page,PROT_NONE);
    request.input_ptr=reinterpret_cast<uint64_t>(ptr);
  }
  auto* supplied=static_cast<Request*>(desc.base);*supplied=request;
  if(readonly)desc.protect(desc.base,page,PROT_READ);
  const auto before=read_irqs();const auto started=Clock::now();errno=0;
  const int ret=checked_ioctl(fd,_IOWR('E',0x04,Request),supplied);const int got=ret<0?errno:0;
  const auto wall=std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-started).count();
  const auto after=read_irqs();const uint64_t expected=input_fault?0:1;
  const bool irq_ok=after.rx_id==before.rx_id&&after.tx_id==before.tx_id&&
    after.rx>=before.rx&&after.tx>=before.tx&&after.rx-before.rx==expected&&after.tx-before.tx==expected;
  bool output_ok=false;
  if(input_fault){output_ok=true;const auto* p=reinterpret_cast<const unsigned char*>(out.data());
    for(size_t i=0;i<out.size()*sizeof(Beat);i++)if(p[i]!=0xa5){output_ok=false;break;}}
  else output_ok=std::memcmp(out.data(),c.gold.data(),N*sizeof(Beat))==0;
  const bool descriptor_ok=readonly?std::memcmp(supplied,&request,sizeof(Request))==0:
    supplied->status==(input_fault?-EFAULT:0)&&(input_fault?supplied->elapsed_us==0:supplied->elapsed_us>0);
  const bool return_ok=(input_fault||readonly)?ret==-1&&got==EFAULT:ret==0;
  const bool ok=irq_ok&&output_ok&&descriptor_ok&&return_ok;
  rows<<row<<'\t'<<index<<'\t'<<c.op<<'\t'<<kind<<'\t'<<ret<<'\t'<<got<<'\t'
      <<(readonly?0:1)<<'\t';
  if(readonly)rows<<"NA\tNA";else rows<<supplied->status<<'\t'<<supplied->elapsed_us;
  rows<<'\t'<<wall<<'\t'<<before.rx_id<<'\t'<<before.tx_id<<'\t'<<before.rx<<'\t'<<after.rx
      <<'\t'<<before.tx<<'\t'<<after.tx<<'\t'<<(input_fault?0:12288)<<'\t'<<(input_fault?0:4096)
      <<'\t'<<(output_ok?1:0)<<'\t'<<(descriptor_ok?1:0)<<'\t'<<(ok?"PASS":"FAIL")<<'\n';rows.flush();
  if(!rows)throw std::runtime_error("Row write failed");
  if(!ok)throw std::runtime_error("Fault/probe mismatch row="+std::to_string(row)+"; no retry");
  if(!input_fault){results.write(reinterpret_cast<const char*>(out.data()),N*sizeof(Beat));results.flush();
    if(!results)throw std::runtime_error("Raw RX archive write failed");}
  std::cout<<"USERCOPY_CASE_PASS row="<<row<<" op="<<c.op<<" kind="<<kind<<" errno="<<got<<" DMA="<<expected<<'\n';
}
int main(int argc,char** argv){int fd=-1;try{
  if(argc!=3&&argc!=4)throw std::runtime_error("usage: vectors --cpu-check | vectors --run private_output");
  const auto cases=read_mixed(argv[1]);const std::array<Plan,3> plans={Plan(Q[0],PSI[0]),Plan(Q[1],PSI[1]),Plan(Q[2],PSI[2])};
  for(const auto& c:cases){const auto gold=mixed_cpu(c,plans);for(size_t i=0;i<N;i++)for(unsigned l=0;l<3;l++)
    if(gold[l][i]!=c.gold[i].limb[l])throw std::runtime_error("CPU oracle mismatch");}
  std::cout<<"USERCOPY_CPU_ORACLE_PASS cases=25 coefficients=307200\n";
  if(argc==3&&std::string(argv[2])=="--cpu-check")return 0;
  if(argc!=4||std::string(argv[2])!="--run")throw std::runtime_error("Arguments");
#ifndef EDGEFHE_TEST_IOCTL
  struct utsname env{};if(uname(&env)||std::string(env.machine)!="aarch64")throw std::runtime_error("Reviewed aarch64 board required before device open");
#else
  std::cout<<"LOCAL_USERCOPY_MOCK_NO_BOARD\n";
#endif
  const long actual=sysconf(_SC_PAGESIZE);if(actual<4096||(actual&(actual-1))||actual%16)throw std::runtime_error("Unsupported page geometry");
  const size_t page=static_cast<size_t>(actual);const std::string dir=argv[3];
  std::ofstream rows(dir+"/transactions.tsv"),results(dir+"/results.bin",std::ios::binary);
  if(!rows||!results)throw std::runtime_error("Private output required");
  rows<<"row\tcase\top\tkind\treturn\terrno\tdescriptor_delivered\tstatus\tdriver_us\twall_ns\trx_id\ttx_id\trx_before\trx_after\ttx_before\ttx_after\tcoefficients\tpadding\toutput_ok\tdescriptor_ok\tresult\n";
#ifdef EDGEFHE_TEST_IOCTL
  fd=100;
#else
  fd=open("/dev/edgefhe_rns3_n4096_dma",O_RDWR|O_CLOEXEC);
#endif
  if(fd<0)throw std::runtime_error("Device open failed");
  unsigned row=0;const std::array<unsigned,6> indices={8,9,15,22,23,24};
  for(auto index:indices)for(const char* kind:{"input_copy_fault","probe_after_input","descriptor_copyback_fault","probe_after_descriptor"})
    execute(fd,cases[index],index,++row,kind,page,rows,results);
#ifndef EDGEFHE_TEST_IOCTL
  const int closed=close(fd);fd=-1;if(closed)throw std::runtime_error("Device close failed");
#else
  fd=-1;
#endif
  std::ofstream report(dir+"/native_report.json");
  report<<"{\"status\":\"PASS_NATIVE_RECOVERABLE_USERCOPY_EXACT\",\"ioctls\":24,\"expected_EFAULT\":12,\"DMA\":18,\"opens\":1,\"closes\":1,\"coefficients\":221184,\"padding\":73728,\"page_size_bytes\":"<<page<<",\"input_readable_prefix_bytes\":16,\"raw_RX_bytes\":1179648}\n";
  report.flush();if(!report)throw std::runtime_error("Native report write failed");
  std::cout<<"USERCOPY_NATIVE_PASS ioctls=24 expected_EFAULT=12 DMA=18 exact_coefficients=221184 padding=73728 raw_RX=1179648\n";return 0;
}catch(const std::exception& e){
#ifndef EDGEFHE_TEST_IOCTL
  if(fd>=0)close(fd);
#endif
  std::cerr<<"FAIL: "<<e.what()<<'\n';return 1;}}
