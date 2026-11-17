#include "tls.hpp"
#include <iostream>
extern "C" int ef_cloud_affine(const uint32_t*,size_t,size_t,const int64_t*,int64_t,uint32_t*,size_t);
int main(int argc,char** argv){
  using namespace efnet;
  try{
    process_signals();Options o(argc,argv);const auto c=tls_options(o);
    const auto ip=o.take("--listen","127.0.0.1");const auto port=uint16_t(number(o.take("--port","0"),0,65535));
    const auto connections=number(o.take("--connections","1"),1,10000);
    const auto timeout=unsigned(number(o.take("--timeout-ms","30000"),100,120000));
    const auto delay=unsigned(number(o.take("--qa-delay-ms","0"),0,10000));const auto fault=o.take("--qa-fault");o.finish();
    need((delay==0&&fault.empty())||ip=="127.0.0.1","FAULT_INJECTION_LOOPBACK_ONLY");
    need(fault.empty()||fault=="key"||fault=="model"||fault=="profile"||fault=="sequence"||fault=="digest"||fault=="noncanonical"||fault=="short"||fault=="replay","FAULT_NAME");
    TlsContext context(c,true);Listener listener(ip,port);
    uint64_t evaluated=0,written=0,rejected=0;
    std::cout<<"{\"status\":\"CPP_E2E_SERVER_READY\",\"port\":"<<listener.port()<<",\"test_injection\":"<<((delay||!fault.empty())?"true":"false")<<"}\n"<<std::flush;
    for(uint64_t connection=0;connection<connections;connection++){
      try{
        Channel channel(context,listener.accept(deadline(timeout)),c,true,deadline(timeout));ConnectionSequence ledger(channel.binding());
        Header last{};std::vector<uint8_t> last_body;
        for(;;){
          const auto end=deadline(timeout);Header request;if(!read_header(channel,request,end,true))break;
          ledger.header(request);std::vector<uint8_t> body(size_t(request.bytes));channel.read(body.data(),body.size(),end);
          // Digest and canonical validation precede cloud evaluation. Consume
          // before invoking cloud: any subsequent failure permanently closes
          // this channel; there is no replay/retry on an uncertain operation.
          ledger.consume(request,body);auto padded=unpack(body,request.features,true);
          const auto& model=model_digest(request.model_id);auto weights=model.weights_int;weights.resize((request.features+3)/4*4,0);
          std::vector<uint32_t> output(ct_words);const uint64_t start=now();
          need(ef_cloud_affine(padded.data(),padded.size(),weights.size(),weights.data(),model.bias_int,output.data(),output.size())==0,"CLOUD_AFFINE_REJECTED");
          const auto cloud_ns=now()-start;++evaluated;
          Header response=request;response.kind=2;response.bytes=ct_bytes;auto payload=pack_response(output);
          if(delay)std::this_thread::sleep_for(std::chrono::milliseconds(delay));
          if(fault=="key")response.key_id=random_digest();
          else if(fault=="model")response.model_id=random_digest();
          else if(fault=="profile")response.profile_id=random_digest();
          else if(fault=="sequence")++response.sequence;
          else if(fault=="digest")response.request_hash=random_digest();
          else if(fault=="noncanonical")put32(payload.data(),q[0]);
          else if(fault=="replay"&&request.sequence>1){response=last;payload=last_body;}
          if(fault=="short"){
            const auto h=encode(response);channel.write(h.data(),h.size(),end);channel.write(payload.data(),3,end);channel.close_clean(deadline(timeout));break;
          }
          write_frame(channel,response,payload,end);++written;
          std::cout<<"{\"status\":\"CPP_E2E_RESPONSE_WRITTEN_NOT_OWNER_ACCEPTANCE\",\"sequence\":"<<request.sequence
                   <<",\"request_sha256\":\""<<hex(request.request_hash)<<"\",\"response_payload_sha256\":\""<<hex(digest(payload.data(),payload.size()))
                   <<"\",\"cloud_ns\":"<<cloud_ns<<"}\n"<<std::flush;
          last=response;last_body=payload;
        }
        channel.close_clean(deadline(timeout));
      }catch(const std::exception& e){++rejected;std::cerr<<"CPP_E2E_SERVER_CONNECTION_STOP "<<e.what()<<'\n'<<std::flush;}
    }
    std::cout<<"{\"status\":\"CPP_E2E_SERVER_STOPPED\",\"evaluated\":"<<evaluated<<",\"responses_written\":"<<written<<",\"rejected_connections\":"<<rejected
             <<",\"owner_secrets_available\":false,\"automatic_retries\":0}\n";
    return rejected?2:0;
  }catch(const std::exception& e){std::cerr<<"STOP_CPP_E2E_SERVER "<<e.what()<<'\n';return 1;}
}
