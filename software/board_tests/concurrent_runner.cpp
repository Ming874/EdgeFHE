// PS/Linux only. Frozen NTT/ALU oracle helpers execute on CPU, never synthesized.
#include "soak_mixed_support.hpp"
#include <atomic>
#include <condition_variable>
#include <mutex>
#include <thread>

constexpr unsigned WORKERS=4, ROUNDS=10;
struct Result { unsigned completed=0,peak=0; uint64_t begin=0,end=0; };
static uint64_t clock_ns(){return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count();}
static int open_client(unsigned id){
#ifdef EDGEFHE_TEST_IOCTL
  return 100+static_cast<int>(id);
#else
  (void)id;return open("/dev/edgefhe_rns3_n4096_dma",O_RDWR|O_CLOEXEC);
#endif
}
static int close_client(int fd){
#ifdef EDGEFHE_TEST_IOCTL
  (void)fd;return 0;
#else
  return close(fd);
#endif
}
static void raise_peak(std::atomic<unsigned>& peak,unsigned value){
  unsigned old=peak.load();while(old<value&&!peak.compare_exchange_weak(old,value)){}
}
static Result phase_run(unsigned phase,const std::vector<Mixed>& cases,std::ofstream& rows,unsigned& row,
                        unsigned& opened,unsigned& closed){
  std::array<int,WORKERS> fds{};fds.fill(-1);
  std::array<std::vector<Beat>,WORKERS> outputs;
  for(auto& output:outputs)output.resize(N);
  const unsigned fd_count=phase==0?1:WORKERS;
  auto cleanup=[&](){
    bool bad=false;
    for(unsigned i=0;i<fd_count;i++)if(fds[i]>=0){if(close_client(fds[i]))bad=true;fds[i]=-1;closed++;}
    return bad;
  };
  for(unsigned i=0;i<fd_count;i++){
    fds[i]=open_client(phase*WORKERS+i);
    if(fds[i]<0){cleanup();throw std::runtime_error("open failed before worker launch");}opened++;
  }
  if(phase==0)for(unsigned i=1;i<WORKERS;i++)fds[i]=fds[0];
  std::mutex gate_mutex,rows_mutex,error_mutex;
  std::condition_variable gate;
  unsigned ready=0;bool go=false;
  std::atomic<bool> stop{false};std::atomic<unsigned> active{0},peak{0},completed{0};
  std::string error;
  std::vector<std::thread> threads;
  const uint64_t begin=clock_ns();
  try{
    for(unsigned worker=0;worker<WORKERS;worker++)threads.emplace_back([&,worker](){
      {
        std::unique_lock<std::mutex> lock(gate_mutex);ready++;gate.notify_all();
        gate.wait(lock,[&](){return go;});
      }
      try{
        for(unsigned iteration=0;iteration<ROUNDS*25&&!stop.load();iteration++){
          const unsigned index=(iteration%25+7*worker)%25;
          const auto& c=cases[index];auto& out=outputs[worker];
          std::fill(out.begin(),out.end(),Beat{});
          Request request{};request.operation=c.op;request.input_words=static_cast<uint32_t>(c.tx.size());
          request.output_words=N;request.input_ptr=reinterpret_cast<uint64_t>(c.tx.data());
          request.output_ptr=reinterpret_cast<uint64_t>(out.data());request.timeout_ms=5000;
          const uint64_t started=clock_ns();const unsigned overlap=active.fetch_add(1)+1;raise_peak(peak,overlap);
          const int result=checked_ioctl(fds[worker],_IOWR('E',0x04,Request),&request);
          const int saved_errno=errno;
          const bool correct=result==0&&request.status==0&&request.elapsed_us>0&&
                             std::memcmp(out.data(),c.gold.data(),N*sizeof(Beat))==0;
          const uint64_t finished=clock_ns();active.fetch_sub(1);
          {
            std::lock_guard<std::mutex> lock(rows_mutex);
            rows<<++row<<'\t'<<phase<<'\t'<<worker<<'\t'<<iteration<<'\t'<<index<<'\t'<<c.op<<'\t'
                <<started<<'\t'<<finished<<'\t'<<(finished-started)<<'\t'<<request.elapsed_us<<'\t'
                <<overlap<<'\t'<<result<<'\t'<<request.status<<'\t'<<(result<0?saved_errno:0)<<'\t'
                <<(correct?12288:0)<<'\t'<<(correct?4096:0)<<'\n';rows.flush();
            if(!rows)throw std::runtime_error("transaction evidence write failed");
          }
          if(!correct)throw std::runtime_error("ioctl/exact result failed; preserve partial evidence; no retry");
          completed.fetch_add(1);
        }
      }catch(const std::exception& e){stop.store(true);std::lock_guard<std::mutex> lock(error_mutex);if(error.empty())error=e.what();}
    });
  }catch(...){
    stop.store(true);{std::lock_guard<std::mutex> lock(gate_mutex);go=true;}gate.notify_all();
    for(auto& t:threads)t.join();
    cleanup();
    throw;
  }
  {std::unique_lock<std::mutex> lock(gate_mutex);gate.wait(lock,[&](){return ready==WORKERS;});go=true;}gate.notify_all();
  for(auto& t:threads)t.join();
  const auto end=clock_ns();const bool close_failed=cleanup();
  if(!error.empty())throw std::runtime_error(error);
  if(close_failed||completed.load()!=1000||peak.load()<2||active.load()!=0)
    throw std::runtime_error("phase counts, overlap or close mismatch");
  std::cout<<"CONCURRENCY_PHASE_PASS phase="<<phase<<" mode="<<(phase==0?"shared_fd":"separate_fd")
           <<" jobs="<<completed.load()<<" peak_call_windows="<<peak.load()<<'\n';
  return Result{completed.load(),peak.load(),begin,end};
}
int main(int argc,char** argv){try{
  if(argc!=3&&argc!=4)throw std::runtime_error("usage: vectors --cpu-check | vectors --run output_directory");
  const auto cases=read_mixed(argv[1]);
  const std::array<Plan,3> plans={Plan(Q[0],PSI[0]),Plan(Q[1],PSI[1]),Plan(Q[2],PSI[2])};
  for(const auto& c:cases){const auto gold=mixed_cpu(c,plans);
    for(size_t i=0;i<N;i++)for(unsigned l=0;l<3;l++)if(gold[l][i]!=c.gold[i].limb[l])
      throw std::runtime_error("CPU mixed oracle mismatch");}
  std::cout<<"CONCURRENCY_CPU_ORACLE_PASS cases=25 coefficients=307200\n";
  if(std::string(argv[2])=="--cpu-check"&&argc==3)return 0;
  if(std::string(argv[2])!="--run"||argc!=4)throw std::runtime_error("Invalid arguments");
#ifndef EDGEFHE_TEST_IOCTL
  struct utsname environment{};
  if(uname(&environment)||std::string(environment.machine)!="aarch64")throw std::runtime_error("Reviewed aarch64 board required");
#else
  std::cout<<"LOCAL_CONCURRENCY_MOCK_NO_BOARD\n";
#endif
  const std::string output=argv[3];std::ofstream rows(output+"/transactions.tsv");
  if(!rows)throw std::runtime_error("Need writable private evidence directory");
  rows<<"row\tphase\tthread\titeration\tcase\top\tstarted_ns\tfinished_ns\twall_ns\tdriver_us\toverlap_at_entry\treturn\tstatus\terrno\tcoefficients\tpadding\n";
  unsigned row=0,opened=0,closed=0;std::array<Result,2> result{};
  for(unsigned phase=0;phase<2;phase++)result[phase]=phase_run(phase,cases,rows,row,opened,closed);
  if(row!=2000||opened!=5||closed!=5)throw std::runtime_error("Final lifecycle/count mismatch");
  std::ofstream report(output+"/native_report.json");
  report<<"{\"status\":\"PASS_NATIVE_TWO_MODE_CONCURRENT_EXACT\",\"jobs\":2000,\"workers\":4,\"opens\":5,\"closes\":5,"
        <<"\"coefficients\":24576000,\"padding\":8192000,\"phase_peaks\":["<<result[0].peak<<','<<result[1].peak<<"],"
        <<"\"phase_begin_ns\":["<<result[0].begin<<','<<result[1].begin<<"],\"phase_end_ns\":["<<result[0].end<<','<<result[1].end<<"]}\n";
  report.flush();if(!report)throw std::runtime_error("Final native evidence write failed");
  std::cout<<"CONCURRENCY_NATIVE_PASS jobs=2000 shared_fd=1000 separate_fd=1000 workers=4 opens=5 closes=5 coefficients=24576000 padding=8192000\n";
  return 0;
}catch(const std::exception& e){std::cerr<<"FAIL: "<<e.what()<<'\n';return 1;}}
