#include "tls.hpp"
#include "span_wire.hpp"
#include "invoke_permit.hpp"
#include <condition_variable>
#include <deque>
#include <exception>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <sched.h>
#include <sstream>
#include <sys/stat.h>

extern "C" {
void* ef_owner_create(unsigned,unsigned,const char*);
void ef_owner_destroy(void*);
const char* ef_owner_error();
int ef_owner_encrypt(void*,const double*,size_t,size_t,uint32_t*,size_t);
int ef_owner_decrypt(void*,const uint32_t*,size_t,size_t,double*);
int ef_owner_decrypt_concurrent(void*,const uint32_t*,size_t,size_t,double*);   // O5 owner library
int ef_owner_key_id(void*,uint8_t*);
int ef_owner_device_calls(void*,uint64_t*);
int ef_owner_pl_hz(void*,uint32_t*);
int ef_normalize_checked(const double*,size_t,size_t,const double*,const double*,int,int,double*,size_t) noexcept;
}
namespace efnet {
struct Owner {
  void* ptr=nullptr;
  Owner(unsigned workers,unsigned backend,const std::string& plugin){need(backend<=2,"OWNER_BACKEND_RANGE");ptr=ef_owner_create(workers,backend,plugin.c_str());need(ptr!=nullptr,"OWNER_CREATE");}
  ~Owner(){if(ptr)ef_owner_destroy(ptr);}
  Owner(const Owner&)=delete;Owner& operator=(const Owner&)=delete;
};
inline std::vector<std::string> fields(const std::string& line){
  std::vector<std::string> out;size_t start=0;
  for(;;){size_t end=line.find(',',start);out.push_back(line.substr(start,end-start));if(end==std::string::npos)break;start=end+1;}
  return out;
}
inline size_t read_private_csv(const std::string& path,const Model& m,Secret<double>& raw){
  std::ifstream file(path,std::ios::binary);need(bool(file),"CSV_OPEN");file.seekg(0,std::ios::end);const auto size=file.tellg();
  need(size>=0&&size<=2*1024*1024,"CSV_MAX_BYTES");file.seekg(0);std::string line;
  struct Wipe{std::string& s;~Wipe(){if(!s.empty())OPENSSL_cleanse(s.data(),s.size());}} wipe{line};
  need(bool(std::getline(file,line)),"CSV_HEADER");if(!line.empty()&&line.back()=='\r')line.pop_back();need(fields(line)==m.features,"CSV_FEATURE_ORDER");
  raw.v.resize(2048*m.features.size());size_t rows=0;
  while(std::getline(file,line)){
    if(!line.empty()&&line.back()=='\r'){line.pop_back();}
    need(rows<2048&&!line.empty(),"CSV_ROW_COUNT");auto row=fields(line);
    // Avoid persisting private numeric text copies after parsing.
    struct Clear{std::vector<std::string>& rows;~Clear(){for(auto& s:rows)if(!s.empty())OPENSSL_cleanse(s.data(),s.size());}} clear{row};
    need(row.size()==m.features.size(),"CSV_DIMENSIONS");
    for(size_t f=0;f<row.size();f++){double v=0;const auto r=std::from_chars(row[f].data(),row[f].data()+row[f].size(),v);need(r.ec==std::errc{}&&r.ptr==row[f].data()+row[f].size()&&std::isfinite(v),"CSV_NUMERIC");raw.v[rows*m.features.size()+f]=v;}
    ++rows;OPENSSL_cleanse(line.data(),line.size());
  }
  need(file.eof()&&rows>0,"CSV_READ");raw.v.resize(rows*m.features.size());return rows;
}
inline void write_scores(const std::string& path,const std::vector<double>& values){
  Socket fd(::open(path.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600));need(fd.fd>=0,"PRIVATE_OUTPUT_EXISTS_OR_OPEN");
  for(double value:values){
    char text[64]{};
    struct WipeText{char* p;size_t n;~WipeText(){OPENSSL_cleanse(p,n);}} wipe_text{text,sizeof text};
    const auto r=std::to_chars(text,text+sizeof(text)-1,value,std::chars_format::general,17);need(r.ec==std::errc{},"SCORE_FORMAT");*r.ptr='\n';
    size_t offset=0,length=size_t(r.ptr-text)+1;
    while(offset<length){const auto n=::write(fd.fd,text+offset,length-offset);if(n<0&&errno==EINTR)continue;need(n>0,"PRIVATE_OUTPUT_WRITE_PRESERVE_PARTIAL");offset+=size_t(n);}
    OPENSSL_cleanse(text,sizeof text);
  }
  need(::fsync(fd.fd)==0,"PRIVATE_OUTPUT_FSYNC_PRESERVE_PARTIAL");
}
struct FrameSlot {
  Secret<double> normalized,scores;std::vector<uint32_t> ct,response;
  std::vector<uint8_t> payload;Header header{};Digest response_sha{};
  int state=0;uint64_t admitted=0,prepare_ns=0,io_ns=0,decrypt_ns=0,io_start=0;
  FrameSlot(size_t rows,size_t f):normalized(rows*f),scores(rows),ct((f+3)/4*4*ct_words),response(ct_words){}
};
struct Trace {uint64_t admitted,completed,prepare_ns,io_ns,decrypt_ns;Digest request,response;};
// S1b (2026-10-25): I/O thread placement. On the KV260 every eth0 interrupt and its NET_RX/NET_TX softirq work runs on
// one A53 (S1a: cpu3, ~7 ms per MB of traffic) and the O7 I/O thread ran there too (S0b: cpu3 ~99 % busy, ~40 % softirq).
// --io-cpu auto pins the I/O thread to the allowed CPU with the fewest NET_RX+NET_TX softirqs since boot (/proc/softirqs;
// ties -> highest number), --io-cpu N to CPU N; --io-isolate 1 keeps the other client threads (main, owner workers,
// decrypt) off that CPU. Only this process's own affinity is changed; interrupt routing is left as the system has it.
inline std::map<int,uint64_t> net_softirqs(){
  std::ifstream f("/proc/softirqs");need(f.good(),"PROC_SOFTIRQS");std::string line;need(bool(std::getline(f,line)),"PROC_SOFTIRQS_HEADER");
  std::vector<int> cpus;{std::istringstream h(line);std::string w;while(h>>w){need(w.rfind("CPU",0)==0,"PROC_SOFTIRQS_HEADER");cpus.push_back(int(number(w.substr(3),0,CPU_SETSIZE-1)));}}
  std::map<int,uint64_t> sum;unsigned rows=0;
  while(std::getline(f,line)){std::istringstream s(line);std::string name;s>>name;if(name!="NET_RX:"&&name!="NET_TX:")continue;++rows;
    for(int c:cpus){uint64_t v=0;need(bool(s>>v),"PROC_SOFTIRQS_ROW");sum[c]+=v;}}
  need(rows==2&&!cpus.empty(),"PROC_SOFTIRQS_NET");return sum;
}
inline int choose_io_cpu(const std::string& spec,const std::map<int,uint64_t>& net){
  cpu_set_t allowed;CPU_ZERO(&allowed);need(::sched_getaffinity(0,sizeof allowed,&allowed)==0,"AFFINITY_GET");
  if(spec!="auto"){const int c=int(number(spec,0,CPU_SETSIZE-1));need(CPU_ISSET(c,&allowed),"IO_CPU_NOT_ALLOWED");return c;}
  int best=-1;uint64_t low=0;
  for(const auto& [c,v]:net)if(CPU_ISSET(c,&allowed)&&(best<0||v<=low)){best=c;low=v;}
  need(best>=0,"IO_CPU_AUTO_NONE");return best;
}
inline void pin_self(int cpu){cpu_set_t s;CPU_ZERO(&s);CPU_SET(cpu,&s);need(::sched_setaffinity(0,sizeof s,&s)==0,"AFFINITY_SET_IO");}
inline void avoid_self(int cpu){
  cpu_set_t s;CPU_ZERO(&s);need(::sched_getaffinity(0,sizeof s,&s)==0,"AFFINITY_GET");CPU_CLR(cpu,&s);need(CPU_COUNT(&s)>=1,"IO_ISOLATE_NEEDS_TWO_CPUS");
  need(::sched_setaffinity(0,sizeof s,&s)==0,"AFFINITY_SET_OTHERS");
}
inline std::string json_counts(const std::map<int,uint64_t>& a,const std::map<int,uint64_t>* base=nullptr){
  std::string s="{";for(const auto& [c,v]:a){if(s.size()>1)s+=',';s+="\""+std::to_string(c)+"\":"+std::to_string(base?v-base->at(c):v);}return s+"}";
}
}
int main(int argc,char** argv){
  using namespace efnet;
  try{
    process_signals();Options o(argc,argv);const auto cfg=tls_options(o);const auto ip=o.required("--host");
    const auto port=uint16_t(number(o.required("--port"),1,65535));const auto timeout=unsigned(number(o.take("--timeout-ms","30000"),100,120000));
    const auto backend=o.take("--backend","cpu");need(backend=="cpu"||backend=="spec"||backend=="pl","BACKEND_NAME");
    const auto workers=unsigned(number(o.take("--workers","3"),1,3));need(workers==1||workers==3,"WORKERS_1_OR_3");
    const auto plugin=o.required("--plugin");const auto model_id=o.required("--model");const auto& model=model_name(model_id);
    size_t rows=size_t(number(o.take("--rows","1"),1,2048));const auto mode=o.take("--mode","serial");need(mode=="serial"||mode=="pipeline","MODE_NAME");
    const auto slots_text=o.take("--slots","2");const size_t slots_wanted=size_t(number(slots_text,2,8));
    need(mode=="pipeline"||slots_wanted==2,"SLOTS_PIPELINE_ONLY");
    // O3 (2026-10-24): full-duplex I/O. With --duplex 1 the I/O thread sends request i+1 before reading response i, so the
    // upload of the next request overlaps the server work and the download of the previous one (responses still read in order).
    const auto duplex_text=o.take("--duplex","0");need(duplex_text=="0"||duplex_text=="1","DUPLEX_0_OR_1");const bool duplex=duplex_text=="1";
    need(!duplex||mode=="pipeline","DUPLEX_PIPELINE_ONLY");
    // O4: request digest computed by the I/O thread just before sending (wire bytes unchanged).
    // O5: decryption on a dedicated thread through ef_owner_decrypt_concurrent (bit-identical arithmetic).
    const auto hash_text=o.take("--hash-io","0"),dec_text=o.take("--decrypt-thread","0");
    need((hash_text=="0"||hash_text=="1")&&(dec_text=="0"||dec_text=="1"),"O4_O5_FLAGS_0_OR_1");const bool hash_io=hash_text=="1",decrypt_thread=dec_text=="1";
    need((!hash_io&&!decrypt_thread)||mode=="pipeline","O4_O5_PIPELINE_ONLY");
    // O7: up to K requests outstanding on the wire (sent, response not yet read); 0 = the O3 v2 duplex path (at most 2).
    // Responses are drained while sending and whenever the next request is not ready, so no K can deadlock.
    const auto inflight=size_t(number(o.take("--inflight","0"),0,8));const size_t capacity=mode=="pipeline"?slots_wanted:1;
    need(inflight==0||(duplex&&inflight>=2&&inflight<=capacity),"INFLIGHT_REQUIRES_DUPLEX_2_TO_SLOTS");const auto maximum=number(o.take("--requests","1"),1,100000);
    // S1b: I/O thread placement and receive cadence (choose_io_cpu, Channel::send_some_while_receiving); bound into the contract.
    const auto io_cpu_text=o.take("--io-cpu","none");
    need(io_cpu_text=="none"||io_cpu_text=="auto"||(!io_cpu_text.empty()&&io_cpu_text.size()<=4&&std::all_of(io_cpu_text.begin(),io_cpu_text.end(),[](char c){return c>='0'&&c<='9';})),"IO_CPU_NONE_AUTO_OR_NUMBER");
    const auto isolate_text=o.take("--io-isolate","0");need(isolate_text=="0"||isolate_text=="1","IO_ISOLATE_0_OR_1");const bool io_isolate=isolate_text=="1";
    need(!io_isolate||io_cpu_text!="none","IO_ISOLATE_REQUIRES_IO_CPU");
    const auto read_every=unsigned(number(o.take("--read-every","1"),1,64));need(read_every==1||inflight,"READ_EVERY_REQUIRES_INFLIGHT");
    const auto window_ms=number(o.take("--window-ms","0"),0,60000);const auto permit_text=o.take("--permit-fd");const auto input=o.take("--input");const auto output=o.take("--scores-output");o.finish();
    need(input.empty()||(maximum==1&&window_ms==0),"PRIVATE_CSV_SINGLE_REQUEST_ONLY");need(output.empty()||!input.empty(),"PRIVATE_OUTPUT_REQUIRES_PRIVATE_INPUT");
    const bool synthetic=input.empty();const size_t F=model.features.size();Secret<double> raw;
    const uint64_t input_start=now();
    if(synthetic){raw.v.resize(rows*F);for(size_t r=0;r<rows;r++)for(size_t f=0;f<F;f++)raw.v[r*F+f]=model.uci?(f?double((r+f)%2):40.0):model.mean[f]+model.sd[f]*(double((r+f)%17)-8.0)/8.0;}
    else rows=read_private_csv(input,model,raw);
    const uint64_t input_ns=now()-input_start,cold=now();
    const auto base_contract=invocation_contract(model.id,rows,mode,maximum,window_ms,workers,ip,port,timeout,cfg,plugin);
    // Live demo: a private single-row request may use PL only with scores output and a permit bound to both paths.
    // Suite: a pipeline depth other than the accepted 2 slots is bound into the permit contract as well.
    const auto depth_contract=capacity==2||mode!="pipeline"?base_contract:hex(digest("EDGEFHE_SLOTS_V1\n"+base_contract+'\n'+std::to_string(capacity)+'\n'));
    auto io_contract=duplex?hex(digest("EDGEFHE_DUPLEX_V1\n"+depth_contract+'\n')):depth_contract;
    if(hash_io)io_contract=hex(digest("EDGEFHE_HASH_IO_V1\n"+io_contract+'\n'));
    if(decrypt_thread)io_contract=hex(digest("EDGEFHE_DECRYPT_THREAD_V1\n"+io_contract+'\n'));
    if(inflight)io_contract=hex(digest("EDGEFHE_INFLIGHT_V1\n"+io_contract+'\n'+std::to_string(inflight)+'\n'));
    if(io_cpu_text!="none"||read_every!=1)io_contract=hex(digest("EDGEFHE_IO_PLACEMENT_V1\n"+io_contract+'\n'+io_cpu_text+'\n'+(io_isolate?"1":"0")+'\n'+std::to_string(read_every)+'\n'));
    const auto contract=synthetic?io_contract:private_invocation_contract(io_contract,input,output);
    if(backend=="pl"){
      need(!permit_text.empty()&&(synthetic||!output.empty()),"PL_REQUIRES_CONTROLLED_INVOCATION");
      check_invocation_permit(int(number(permit_text,3,1024)),contract);
    }else need(permit_text.empty(),"CPU_SPEC_TAKES_NO_PL_PERMIT");
    std::map<int,uint64_t> net_start;bool net_ok=true;
    try{net_start=net_softirqs();}catch(const std::exception&){net_ok=false;need(io_cpu_text!="auto","IO_CPU_AUTO_NEEDS_PROC_SOFTIRQS");}
    const int io_cpu=io_cpu_text=="none"?-1:choose_io_cpu(io_cpu_text,net_start);
    if(io_isolate)avoid_self(io_cpu);   // before the owner workers and the decrypt thread exist: they inherit this mask
    Owner owner(workers,backend=="cpu"?0:backend=="spec"?1:2,plugin);
    Digest key{};need(ef_owner_key_id(owner.ptr,key.data())==0&&nonzero(key),"OWNER_KEY_ID");
    const auto randomness=random_digest();uint64_t generation=get64(randomness.data());if(!generation)generation=1;
    std::vector<std::unique_ptr<FrameSlot>> slots;for(size_t k=0;k<capacity;k++)slots.emplace_back(std::make_unique<FrameSlot>(rows,F));
    int wake[2]{-1,-1};need(::pipe2(wake,O_NONBLOCK|O_CLOEXEC)==0,"WAKE_PIPE");
    struct ClosePipe {int* w;~ClosePipe(){::close(w[0]);::close(w[1]);}} close_pipe{wake};
    auto poke=[&]{const char b=1;(void)!::write(wake[1],&b,1);};
    std::mutex mu;std::condition_variable cv;std::exception_ptr failure;bool ready=false,closed=false,stop=false;
    size_t admitted=0;Digest channel_id{};int io_last_cpu=-1;
    // The I/O thread creates, uses and destroys SSL. It never sees raw input,
    // normalized values, a secret key or the owner Context.
    std::thread io([&]{
      try{
        if(io_cpu>=0)pin_self(io_cpu);
        TlsContext context(cfg,false);const auto end=deadline(timeout);Channel channel(context,connect_socket(ip,port,end),cfg,false,end);
        {std::lock_guard<std::mutex> lock(mu);channel_id=channel.binding();ready=true;}cv.notify_all();
        auto receive=[&](size_t i){
          auto& slot=*slots[i%capacity];const auto end_frame=deadline(timeout);Header response;read_header(channel,response,end_frame);validate_response(response,slot.header);
          need(response.bytes==ct_bytes,"RESPONSE_SPAN_SIZE");
          channel.read(reinterpret_cast<uint8_t*>(slot.response.data()),ct_bytes,end_frame);
          canonical(slot.response.data(),slot.response.size());
          slot.response_sha=digest(reinterpret_cast<const uint8_t*>(slot.response.data()),ct_bytes);slot.io_ns=now()-slot.io_start;
          {std::lock_guard<std::mutex> lock(mu);slot.state=3;}cv.notify_all();
        };
        auto settle=[&](size_t i,const std::vector<uint8_t>& buf){   // duplex: response bytes already received into buf
          auto& slot=*slots[i%capacity];const Header response=decode(buf.data(),header_bytes);validate_response(response,slot.header);
          need(response.bytes==ct_bytes,"RESPONSE_SPAN_SIZE");std::memcpy(slot.response.data(),buf.data()+header_bytes,ct_bytes);
          canonical(slot.response.data(),slot.response.size());
          slot.response_sha=digest(reinterpret_cast<const uint8_t*>(slot.response.data()),ct_bytes);slot.io_ns=now()-slot.io_start;
          {std::lock_guard<std::mutex> lock(mu);slot.state=3;}cv.notify_all();
        };
        std::vector<uint8_t> rx(header_bytes+ct_bytes);
        if(inflight){
          std::deque<size_t> out;size_t got=0;
          auto settle_front=[&]{settle(out.front(),rx);out.pop_front();got=0;};
          auto send_draining=[&](const uint8_t* p,size_t n,Deadline e){
            size_t sent=0;
            while(sent<n){
              channel.send_some_while_receiving(p,n,sent,out.empty()?nullptr:rx.data(),out.empty()?0:rx.size(),got,e,read_every);
              if(!out.empty()&&got==rx.size())settle_front();
            }
          };
          for(size_t index=0;;index++){
            bool ready_now=false,finished=false;
            while(!ready_now&&!finished){
              {
                std::unique_lock<std::mutex> lock(mu);
                if(stop)return;
                if(index<admitted&&slots[index%capacity]->state==1){slots[index%capacity]->state=2;ready_now=true;break;}
                if(closed&&index>=admitted){finished=true;break;}
                if(out.empty()){cv.wait(lock,[&]{return stop||(index<admitted&&slots[index%capacity]->state==1)||(closed&&index>=admitted);});continue;}
              }
              // next request not ready and responses outstanding: receive without blocking the main thread's progress
              const auto e=deadline(timeout);const short need_events=channel.receive_some(rx.data(),rx.size(),got);
              if(got==rx.size()){settle_front();continue;}
              if(need_events){channel.wait_socket_or(need_events,wake[0],5,e);char b[64];while(::read(wake[0],b,sizeof b)>0){}}
            }
            if(finished)break;
            auto& slot=*slots[index%capacity];slot.io_start=now();const PayloadView body{reinterpret_cast<const uint8_t*>(slot.ct.data()),F*ct_bytes};
            if(hash_io)slot.header.request_hash=span_request_hash(slot.header,body);
            const auto end_frame=deadline(timeout);const auto head=encode(slot.header);need(body.bytes==slot.header.bytes,"SPAN_FRAME_LENGTH");
            send_draining(head.data(),head.size(),end_frame);send_draining(body.data,body.bytes,end_frame);out.push_back(index);
            while(out.size()>inflight){const auto e=deadline(timeout);if(got<rx.size())channel.read(rx.data()+got,rx.size()-got,e);got=rx.size();settle_front();}
          }
          while(!out.empty()){const auto e=deadline(timeout);if(got<rx.size())channel.read(rx.data()+got,rx.size()-got,e);got=rx.size();settle_front();}
          io_last_cpu=::sched_getcpu();channel.close_clean(deadline(timeout));return;
        }
        constexpr size_t none=~size_t(0);size_t pending=none;
        for(size_t index=0;;index++){
          {
            std::unique_lock<std::mutex> lock(mu);cv.wait(lock,[&]{return stop||(index<admitted&&slots[index%capacity]->state==1)||(closed&&index>=admitted);});
            if(stop){return;}
            if(closed&&index>=admitted){break;}
            slots[index%capacity]->state=2;
          }
          auto& slot=*slots[index%capacity];slot.io_start=now();const PayloadView body{reinterpret_cast<const uint8_t*>(slot.ct.data()),F*ct_bytes};
          if(hash_io)slot.header.request_hash=span_request_hash(slot.header,body);
          if(!duplex||pending==none){write_span_frame(channel,slot.header,body,deadline(timeout));if(duplex)pending=index;else receive(index);continue;}
          const auto end_frame=deadline(timeout);const auto head=encode(slot.header);need(body.bytes==slot.header.bytes,"SPAN_FRAME_LENGTH");
          size_t got=channel.send_while_receiving(head.data(),head.size(),rx.data(),rx.size(),0,end_frame);
          got=channel.send_while_receiving(body.data,body.bytes,rx.data(),rx.size(),got,end_frame);
          if(got<rx.size())channel.read(rx.data()+got,rx.size()-got,end_frame);
          settle(pending,rx);pending=index;
        }
        if(pending!=none)receive(pending);
        io_last_cpu=::sched_getcpu();channel.close_clean(deadline(timeout));
      }catch(...){std::lock_guard<std::mutex> lock(mu);failure=std::current_exception();stop=true;cv.notify_all();}
    });
    struct Join {
      std::thread& io;std::mutex& mu;std::condition_variable& cv;bool& stop;
      ~Join(){{std::lock_guard<std::mutex> lock(mu);stop=true;}cv.notify_all();if(io.joinable())io.join();}
    } join{io,mu,cv,stop};
    auto check_failure=[&]{if(failure)std::rethrow_exception(failure);need(!stop,"PIPELINE_STOPPED");};
    std::thread dec;
    if(decrypt_thread)dec=std::thread([&]{
      try{
        for(size_t index=0;;index++){
          {std::unique_lock<std::mutex> lock(mu);cv.wait(lock,[&]{return stop||(index<admitted&&slots[index%capacity]->state==3)||(closed&&index>=admitted);});
           if(stop||(closed&&index>=admitted&&slots[index%capacity]->state!=3))return;}
          auto& slot=*slots[index%capacity];const uint64_t dstart=now();
          need(ef_owner_decrypt_concurrent(owner.ptr,slot.response.data(),slot.response.size(),rows,slot.scores.v.data())==0,"OWNER_DECRYPT_CONCURRENT");slot.decrypt_ns=now()-dstart;
          {std::lock_guard<std::mutex> lock(mu);slot.state=4;}cv.notify_all();
        }
      }catch(...){std::lock_guard<std::mutex> lock(mu);failure=std::current_exception();stop=true;cv.notify_all();}
    });
    struct JoinDec {std::thread& t;std::mutex& mu;std::condition_variable& cv;bool& stop;
      ~JoinDec(){{std::lock_guard<std::mutex> lock(mu);stop=true;}cv.notify_all();if(t.joinable())t.join();}} join_dec{dec,mu,cv,stop};
    {std::unique_lock<std::mutex> lock(mu);cv.wait(lock,[&]{return ready||stop;});check_failure();}
    const uint64_t cold_ns=now()-cold,start=now();std::vector<Trace> trace;double max_error=0;uint64_t over=0,checked_scores=0;
    const double hard=std::string(model.id).find("ridge")!=std::string::npos?5e-4:2e-3;
    auto finish=[&](size_t index){
      {std::unique_lock<std::mutex> lock(mu);cv.wait(lock,[&]{return slots[index%capacity]->state==(decrypt_thread?4:3)||stop;});check_failure();}
      auto& slot=*slots[index%capacity];
      if(!decrypt_thread){const uint64_t dstart=now();need(ef_owner_decrypt(owner.ptr,slot.response.data(),slot.response.size(),rows,slot.scores.v.data())==0,"OWNER_DECRYPT");slot.decrypt_ns=now()-dstart;}
      double error=0;
      for(size_t r=0;r<rows;r++){
        need(std::isfinite(slot.scores.v[r]),"SCORE_NONFINITE");
        if(synthetic){double expected=model.bias;for(size_t f=0;f<F;f++)expected+=model.weights[f]*((raw.v[r*F+f]-model.mean[f])/model.sd[f]);error=std::max(error,std::abs(expected-slot.scores.v[r]));}
      }
      if(synthetic){need(std::isfinite(error)&&error<=hard,"PUBLIC_AFFINE_ORACLE");max_error=std::max(max_error,error);if(error>5e-4)++over;checked_scores+=rows;}
      if(!output.empty())write_scores(output,slot.scores.v);
      slot.scores.wipe();slot.payload.clear();const uint64_t completed=now()-start;
      trace.push_back({slot.admitted,completed,slot.prepare_ns,slot.io_ns,slot.decrypt_ns,slot.header.request_hash,slot.response_sha});
      {std::lock_guard<std::mutex> lock(mu);slot.state=0;}cv.notify_all();
    };
    size_t produced=0,completed=0;
    for(;produced<maximum;produced++){
      if(produced>=capacity){finish(produced-capacity);++completed;}
      const uint64_t timestamp=now()-start;if(window_ms&&timestamp>=window_ms*1000000)break;
      auto& slot=*slots[produced%capacity];
      {std::lock_guard<std::mutex> lock(mu);check_failure();need(slot.state==0,"SLOT_NOT_FREE");slot.state=-1;slot.admitted=timestamp;++admitted;}
      const uint64_t prep=now();need(ef_normalize_checked(raw.v.data(),rows,F,model.mean.data(),model.sd.data(),model.uci,model.dictionary,slot.normalized.v.data(),slot.normalized.v.size())==0,"NORMALIZATION_REFUSED");
      need(ef_owner_encrypt(owner.ptr,slot.normalized.v.data(),rows,F,slot.ct.data(),slot.ct.size())==0,"OWNER_ENCRYPT");slot.normalized.wipe();const auto payload=payload_view(slot.ct,F);
      slot.header={};auto& h=slot.header;h.features=uint32_t(F);h.rows=uint32_t(rows);h.bytes=payload.bytes;h.generation=generation;h.sequence=produced+1;
      h.profile_id=profile();h.model_id=unhex(model.source_sha256);h.key_id=key;h.session_id=channel_id;h.request_id=request_id(h);if(!hash_io)h.request_hash=span_request_hash(h,payload);
      slot.prepare_ns=now()-prep;
      {std::lock_guard<std::mutex> lock(mu);check_failure();slot.state=1;}cv.notify_all();if(inflight)poke();
    }
    {std::lock_guard<std::mutex> lock(mu);closed=true;}cv.notify_all();poke();
    for(;completed<produced;completed++)finish(completed);
    io.join();if(dec.joinable())dec.join();{std::lock_guard<std::mutex> lock(mu);check_failure();}
    const uint64_t elapsed=now()-start;std::map<int,uint64_t> net_end;if(net_ok)net_end=net_softirqs();
    need(produced==admitted&&trace.size()==produced&&produced>0,"PIPELINE_DRAIN_COUNT");
    uint64_t frames=0;need(ef_owner_device_calls(owner.ptr,&frames)==0,"OWNER_FRAME_QUERY");
    need(frames==(backend=="cpu"?0:1+produced*((F+3)/4)),"CPU_SPEC_PL_FRAME_ACCOUNTING");
    uint32_t pl_hz=0;need(ef_owner_pl_hz(owner.ptr,&pl_hz)==0,"OWNER_CLOCK_QUERY");
    need(backend!="pl"||(pl_hz>=199800000&&pl_hz<=200000000),"PL_CLOCK_RANGE");
    raw.wipe();std::cout<<std::setprecision(17)<<"{\"status\":\""<<(backend=="pl"?"CPP_E2E_PL_NUMERIC_AND_DRAIN_PASS":"CPP_E2E_CPU_SPEC_NUMERIC_AND_DRAIN_PASS")<<"\",\"backend\":\""<<backend<<"\",\"model\":\""<<model.id<<"\",\"features\":"<<F<<",\"rows\":"<<rows
      <<",\"mode\":\""<<mode<<"\",\"slots\":"<<capacity<<",\"duplex\":"<<(duplex?"true":"false")<<",\"hash_io\":"<<(hash_io?"true":"false")<<",\"decrypt_thread\":"<<(decrypt_thread?"true":"false")<<",\"inflight\":"<<inflight<<",\"io_cpu\":\""<<io_cpu_text<<"\",\"io_cpu_chosen\":"<<io_cpu<<",\"io_isolate\":"<<(io_isolate?"true":"false")<<",\"read_every\":"<<read_every<<",\"io_thread_last_cpu\":"<<io_last_cpu
      <<",\"net_softirq_start\":"<<(net_ok?json_counts(net_start):"null")<<",\"net_softirq_delta\":"<<(net_ok?json_counts(net_end,&net_start):"null")<<",\"owner_workers\":"<<workers<<",\"input_read_ns\":"<<input_ns<<",\"cold_context_and_TLS_ns\":"<<cold_ns
      <<",\"completed_requests\":"<<trace.size()<<",\"elapsed_ns\":"<<elapsed<<",\"admission_limit_ns\":"<<window_ms*1000000<<",\"records_per_s\":"<<double(produced)*double(rows)*1e9/double(elapsed)
      <<",\"synthetic_input\":"<<(synthetic?"true":"false")<<",\"checked_public_scores\":"<<checked_scores<<",\"max_public_error\":";
    if(synthetic)std::cout<<max_error;else std::cout<<"null";
    std::cout<<",\"requests_over_5e4\":"<<(synthetic?std::to_string(over):"null")<<",\"spec_frames\":"<<(backend=="spec"?frames:0)<<",\"device_calls\":"<<frames<<",\"PL_hz\":"<<pl_hz<<",\"actual_DMA\":"<<(backend=="pl"?frames:0)<<",\"real_PL\":"<<(backend=="pl"?"true":"false")<<",\"network_included\":true,\"formal_campaign\":false,\"automatic_retries\":0,\"trace\":[";
    for(size_t i=0;i<trace.size();i++){
      const auto& t=trace[i];if(i)std::cout<<',';
      std::cout<<"{\"admitted_ns\":"<<t.admitted<<",\"completed_ns\":"<<t.completed<<",\"prepare_ns\":"<<t.prepare_ns<<",\"TLS_roundtrip_and_validate_ns\":"<<t.io_ns<<",\"decrypt_ns\":"<<t.decrypt_ns
        <<",\"request_sha256\":\""<<hex(t.request)<<"\",\"response_payload_sha256\":\""<<hex(t.response)<<"\"}";
    }
    std::cout<<"]}\n";return 0;
  }catch(const std::exception& e){std::cerr<<"STOP_CPP_E2E_CLIENT_PRESERVE_NO_RETRY "<<e.what()<<'\n';return 1;}
}
