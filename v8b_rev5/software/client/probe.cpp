// Local negative-test peer only; not an application client or a benchmark.
#include "tls.hpp"
#include <iostream>
int main(int argc,char** argv){
  using namespace efnet;
  try{
    process_signals();Options o(argc,argv);const auto c=tls_options(o);const auto ip=o.required("--host");need(ip=="127.0.0.1","PROBE_LOOPBACK_ONLY");
    const auto port=uint16_t(number(o.required("--port"),1,65535));const auto name=o.required("--case");o.finish();
    need(name=="replay"||name=="hash"||name=="canonical"||name=="oversize"||name=="profile"||name=="short"||name=="channel"||name=="key"||name=="id","PROBE_CASE");
    TlsContext context(c,false);Channel channel(context,connect_socket(ip,port,deadline(3000)),c,false,deadline(3000));
    Header h;h.features=4;h.rows=1;h.bytes=4*ct_bytes;h.generation=1;h.sequence=1;h.profile_id=profile();h.model_id=unhex(catalogue()[0].source_sha256);h.key_id=random_digest();h.session_id=channel.binding();
    std::vector<uint8_t> body(4*ct_bytes,0);h.request_id=request_id(h);h.request_hash=request_hash(h,body);
    if(name=="replay"||name=="key"){
      write_frame(channel,h,body,deadline(3000));Header r;read_header(channel,r,deadline(3000));validate_response(r,h);std::vector<uint8_t> b(ct_bytes);channel.read(b.data(),b.size(),deadline(3000));
      if(name=="key"){++h.sequence;h.key_id=random_digest();h.request_id=request_id(h);h.request_hash=request_hash(h,body);}
    }else if(name=="hash")h.request_hash=random_digest();
    else if(name=="canonical"){put32(body.data(),q[0]);h.request_hash=request_hash(h,body);}
    else if(name=="oversize")h.bytes=UINT64_MAX;
    else if(name=="profile")h.profile_id=random_digest();
    else if(name=="channel"){h.session_id=random_digest();h.request_id=request_id(h);h.request_hash=request_hash(h,body);}
    else if(name=="id")h.request_id=random_digest();
    bool rejected=false;
    try{
      const auto bytes=encode(h);channel.write(bytes.data(),bytes.size(),deadline(3000));
      if(name=="short"){channel.write(body.data(),3,deadline(3000));channel.close_clean(deadline(3000));rejected=true;}
      else{
        if(name!="oversize"&&name!="profile"&&name!="channel"&&name!="id"&&name!="replay"&&name!="key")channel.write(body.data(),body.size(),deadline(3000));
        Header ignored;const bool got=read_header(channel,ignored,deadline(3000),true);need(!got,"BAD_REQUEST_GOT_RESPONSE");rejected=true;
      }
    }catch(const std::runtime_error& e){
      const std::string error=e.what();need(error.rfind("TLS_",0)==0||error.rfind("SOCKET_",0)==0,"PROBE_UNEXPECTED_LOCAL_FAILURE");rejected=true;
    }
    need(rejected,"PROBE_NOT_REJECTED");std::cout<<"CPP_E2E_REMOTE_REJECTION_OBSERVED "<<name<<" NOT_CRYPTO_TEST\n";return 0;
  }catch(const std::exception& e){std::cerr<<"STOP_CPP_E2E_PROBE "<<e.what()<<'\n';return 1;}
}
