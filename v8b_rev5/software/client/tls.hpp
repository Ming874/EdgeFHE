#pragma once
#include "wire.hpp"
#include <arpa/inet.h>
#include <cerrno>
#include <climits>
#include <csignal>
#include <fcntl.h>
#include <memory>
#include <thread>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>

namespace efnet {
using Deadline=Clock::time_point;
inline Deadline deadline(unsigned milliseconds){return Clock::now()+std::chrono::milliseconds(milliseconds);}
inline void remaining(Deadline end){need(Clock::now()<end,"TLS_DEADLINE");}
struct Socket {
  int fd=-1;
  explicit Socket(int value=-1):fd(value){}
  ~Socket(){if(fd>=0)::close(fd);}
  Socket(const Socket&)=delete;Socket& operator=(const Socket&)=delete;
  Socket(Socket&& other)noexcept:fd(other.fd){other.fd=-1;}
  void nonblock(){const int f=::fcntl(fd,F_GETFL);need(f>=0&&::fcntl(fd,F_SETFL,f|O_NONBLOCK)==0,"SOCKET_NONBLOCK");}
};
inline void wait_fd(int fd,short events,Deadline end){
  for(;;){
    remaining(end);
    const auto left=std::chrono::duration_cast<std::chrono::milliseconds>(end-Clock::now()).count();
    pollfd p{fd,events,0};const int rc=::poll(&p,1,int(std::min<int64_t>(INT_MAX,std::max<int64_t>(1,left))));
    if(rc>0){need(!(p.revents&POLLNVAL),"SOCKET_INVALID");return;}
    if(rc<0&&errno!=EINTR)throw std::runtime_error("SOCKET_POLL");
  }
}
inline sockaddr_in address(const std::string& ip,uint16_t port){
  sockaddr_in a{};a.sin_family=AF_INET;a.sin_port=htons(port);
  need(::inet_pton(AF_INET,ip.c_str(),&a.sin_addr)==1,"LITERAL_IPV4_REQUIRED");return a;
}
inline Socket connect_socket(const std::string& ip,uint16_t port,Deadline end){
  Socket fd(::socket(AF_INET,SOCK_STREAM|SOCK_CLOEXEC,0));need(fd.fd>=0,"SOCKET_CREATE");fd.nonblock();
  const auto a=address(ip,port);const int rc=::connect(fd.fd,reinterpret_cast<const sockaddr*>(&a),sizeof a);
  need(rc==0||(rc<0&&errno==EINPROGRESS),"SOCKET_CONNECT");
  if(rc!=0){wait_fd(fd.fd,POLLOUT,end);int e=0;socklen_t n=sizeof e;need(::getsockopt(fd.fd,SOL_SOCKET,SO_ERROR,&e,&n)==0&&e==0,"SOCKET_CONNECT_ERROR");}
  int yes=1;need(::setsockopt(fd.fd,IPPROTO_TCP,TCP_NODELAY,&yes,sizeof yes)==0,"SOCKET_NODELAY");return fd;
}
class Listener {
  Socket socket_;
 public:
  Listener(const std::string& ip,uint16_t port):socket_(::socket(AF_INET,SOCK_STREAM|SOCK_CLOEXEC,0)){
    need(socket_.fd>=0,"LISTEN_SOCKET");const auto a=address(ip,port);
    need(::bind(socket_.fd,reinterpret_cast<const sockaddr*>(&a),sizeof a)==0&&::listen(socket_.fd,4)==0,"LISTEN_BIND");socket_.nonblock();
  }
  uint16_t port()const{sockaddr_in a{};socklen_t n=sizeof a;need(::getsockname(socket_.fd,reinterpret_cast<sockaddr*>(&a),&n)==0,"LISTEN_PORT");return ntohs(a.sin_port);}
  Socket accept(Deadline end){
    for(;;){remaining(end);const int fd=::accept4(socket_.fd,nullptr,nullptr,SOCK_CLOEXEC|SOCK_NONBLOCK);
      if(fd>=0){Socket out(fd);int yes=1;need(::setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&yes,sizeof yes)==0,"SOCKET_NODELAY");return out;}
      need(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR,"SOCKET_ACCEPT");wait_fd(socket_.fd,POLLIN,end);
    }
  }
};
struct TlsConfig {std::string ca,certificate,key,peer_name;Digest peer_pin{};};
inline TlsConfig tls_options(Options& options){
  TlsConfig c;c.ca=options.required("--ca");c.certificate=options.required("--cert");c.key=options.required("--key");
  c.peer_name=options.required("--peer-name");c.peer_pin=unhex(options.required("--peer-sha256"));
  need(c.peer_name.size()<=253&&c.peer_name.find('\0')==std::string::npos&&nonzero(c.peer_pin),"TLS_PEER_ID");return c;
}
class TlsContext {
  std::unique_ptr<SSL_CTX,decltype(&SSL_CTX_free)> context_{nullptr,SSL_CTX_free};
 public:
  TlsContext(const TlsConfig& c,bool server):context_(SSL_CTX_new(server?TLS_server_method():TLS_client_method()),SSL_CTX_free){
    need(context_!=nullptr,"TLS_CONTEXT");SSL_CTX* ctx=context_.get();
    need(SSL_CTX_set_min_proto_version(ctx,TLS1_3_VERSION)==1&&SSL_CTX_set_max_proto_version(ctx,TLS1_3_VERSION)==1,"TLS_VERSION");
    need(SSL_CTX_load_verify_locations(ctx,c.ca.c_str(),nullptr)==1,"TLS_CA");
    need(SSL_CTX_use_certificate_chain_file(ctx,c.certificate.c_str())==1&&SSL_CTX_use_PrivateKey_file(ctx,c.key.c_str(),SSL_FILETYPE_PEM)==1&&SSL_CTX_check_private_key(ctx)==1,"TLS_LOCAL_CREDENTIALS");
    SSL_CTX_set_verify(ctx,SSL_VERIFY_PEER|(server?SSL_VERIFY_FAIL_IF_NO_PEER_CERT:0),nullptr);
    SSL_CTX_set_verify_depth(ctx,3);SSL_CTX_set_session_cache_mode(ctx,SSL_SESS_CACHE_OFF);
    SSL_CTX_set_options(ctx,SSL_OP_NO_TICKET|SSL_OP_NO_RENEGOTIATION);
    need(SSL_CTX_set_max_early_data(ctx,0)==1,"TLS_EARLY_DATA");
  }
  SSL_CTX* get()const{return context_.get();}
};
class Channel {
  Socket socket_;std::unique_ptr<SSL,decltype(&SSL_free)> ssl_{nullptr,SSL_free};
  std::thread::id owner_=std::this_thread::get_id();
  void owner()const{need(owner_==std::this_thread::get_id(),"TLS_SINGLE_OWNER");}
  int result(int rc,Deadline end){
    // SSL_get_error must immediately follow its operation on this same thread.
    const int e=SSL_get_error(ssl_.get(),rc);
    if(e==SSL_ERROR_WANT_READ||e==SSL_ERROR_WANT_WRITE){wait_fd(socket_.fd,e==SSL_ERROR_WANT_READ?POLLIN:POLLOUT,end);return 1;}
    if(e==SSL_ERROR_ZERO_RETURN)return 0;
    throw std::runtime_error("TLS_FATAL_OR_UNEXPECTED_EOF");
  }
 public:
  Channel(TlsContext& ctx,Socket socket,const TlsConfig& c,bool server,Deadline end):socket_(std::move(socket)),ssl_(SSL_new(ctx.get()),SSL_free){
    need(ssl_!=nullptr&&SSL_set_fd(ssl_.get(),socket_.fd)==1,"TLS_SOCKET");
    X509_VERIFY_PARAM* param=SSL_get0_param(ssl_.get());
    X509_VERIFY_PARAM_set_hostflags(param,X509_CHECK_FLAG_NEVER_CHECK_SUBJECT|X509_CHECK_FLAG_NO_WILDCARDS);
    if(!server){
      in_addr ip{};
      if(::inet_pton(AF_INET,c.peer_name.c_str(),&ip)==1)need(X509_VERIFY_PARAM_set1_ip_asc(param,c.peer_name.c_str())==1,"TLS_IP_SAN");
      else need(SSL_set1_host(ssl_.get(),c.peer_name.c_str())==1&&SSL_set_tlsext_host_name(ssl_.get(),c.peer_name.c_str())==1,"TLS_DNS_SAN");
    }
    for(;;){remaining(end);ERR_clear_error();const int rc=server?SSL_accept(ssl_.get()):SSL_connect(ssl_.get());if(rc==1)break;need(result(rc,end)==1,"TLS_HANDSHAKE_EOF");}
    need(SSL_get_verify_result(ssl_.get())==X509_V_OK&&SSL_version(ssl_.get())==TLS1_3_VERSION,"TLS_PEER_CHAIN");
    std::unique_ptr<X509,decltype(&X509_free)> cert(SSL_get1_peer_certificate(ssl_.get()),X509_free);need(cert!=nullptr,"TLS_PEER_CERT_REQUIRED");
    Digest pin{};unsigned length=0;need(X509_digest(cert.get(),EVP_sha256(),pin.data(),&length)==1&&length==32&&equal(pin,c.peer_pin),"TLS_PEER_PIN");
    if(server)need(X509_check_host(cert.get(),c.peer_name.c_str(),c.peer_name.size(),X509_CHECK_FLAG_NEVER_CHECK_SUBJECT|X509_CHECK_FLAG_NO_WILDCARDS,nullptr)==1,"TLS_CLIENT_SAN");
  }
  Channel(const Channel&)=delete;Channel& operator=(const Channel&)=delete;
  Digest binding()const{
    owner();Digest out{};const auto p=profile();constexpr char label[]="EXPERIMENTAL-EdgeFHE-Wire-v1";
    need(SSL_export_keying_material(ssl_.get(),out.data(),out.size(),label,sizeof(label)-1,p.data(),p.size(),1)==1&&nonzero(out),"TLS_EXPORTER");return out;
  }
  void write(const uint8_t* p,size_t size,Deadline end){
    owner();size_t offset=0;
    while(offset<size){remaining(end);const size_t block=std::min<size_t>(16384,size-offset);size_t n=0;ERR_clear_error();const int rc=SSL_write_ex(ssl_.get(),p+offset,block,&n);
      if(rc==1){need(n>0&&n<=block,"TLS_WRITE_LENGTH");offset+=n;}else need(result(rc,end)==1,"TLS_WRITE_EOF");
    }
  }
  bool read(uint8_t* p,size_t size,Deadline end,bool clean_empty_eof=false){
    owner();size_t offset=0;
    while(offset<size){remaining(end);size_t n=0;ERR_clear_error();const int rc=SSL_read_ex(ssl_.get(),p+offset,size-offset,&n);
      if(rc==1){need(n>0&&n<=size-offset,"TLS_READ_LENGTH");offset+=n;}
      else if(result(rc,end)==0){need(clean_empty_eof&&offset==0,"TLS_TRUNCATED_MESSAGE");return false;}
    }return true;
  }
  // O3 (2026-10-24, v2): send `out` completely while draining incoming bytes into in[got..in_size). Both directions make
  // progress on the same non-blocking SSL object in this single owner thread, so a peer that writes its previous response
  // before reading the next request can never deadlock us, whatever the socket buffer sizes. Returns the new `got`.
  size_t send_while_receiving(const uint8_t* out,size_t out_size,uint8_t* in,size_t in_size,size_t got,Deadline end){
    owner();size_t sent=0;
    while(sent<out_size){
      remaining(end);bool progress=false;short events=0;
      if(got<in_size){size_t n=0;ERR_clear_error();const int rc=SSL_read_ex(ssl_.get(),in+got,in_size-got,&n);
        if(rc==1){need(n>0&&n<=in_size-got,"TLS_READ_LENGTH");got+=n;progress=true;}
        else{const int e=SSL_get_error(ssl_.get(),rc);need(e==SSL_ERROR_WANT_READ||e==SSL_ERROR_WANT_WRITE,"TLS_FATAL_OR_UNEXPECTED_EOF");events|=e==SSL_ERROR_WANT_READ?POLLIN:POLLOUT;}}
      const size_t block=std::min<size_t>(16384,out_size-sent);size_t n=0;ERR_clear_error();const int rc=SSL_write_ex(ssl_.get(),out+sent,block,&n);
      if(rc==1){need(n>0&&n<=block,"TLS_WRITE_LENGTH");sent+=n;progress=true;}
      else{const int e=SSL_get_error(ssl_.get(),rc);need(e==SSL_ERROR_WANT_READ||e==SSL_ERROR_WANT_WRITE,"TLS_FATAL_OR_UNEXPECTED_EOF");events|=e==SSL_ERROR_WANT_READ?POLLIN:POLLOUT;}
      if(!progress)wait_fd(socket_.fd,events,end);
    }
    return got;
  }
  // O7 (2026-10-24): like send_while_receiving, but returns as soon as in[] is full (in_size>0 and got==in_size) so the
  // caller can settle that response and continue with the next one; any number of responses can therefore be drained
  // while one request is sent. `sent` and `got` are updated in place. in_size==0: nothing outstanding, send only.
  // S1b (2026-10-25): read_every>1 attempts the receive only on every read_every-th iteration and after any write that did
  // not progress (S1a: the read attempt per 16 KiB record costs ~0.6 ms/MB on the A53). It never waits without having
  // attempted the receive in the same iteration, so a peer blocked on writing its response is always drained (no deadlock).
  void send_some_while_receiving(const uint8_t* out,size_t out_size,size_t& sent,uint8_t* in,size_t in_size,size_t& got,Deadline end,unsigned read_every=1){
    owner();need(read_every>=1,"READ_EVERY_RANGE");unsigned iteration=0;bool wrote=true;
    while(sent<out_size&&(in_size==0||got<in_size)){
      remaining(end);bool progress=false,tried=false;short events=0;
      if(got<in_size&&(!wrote||iteration%read_every==0)){size_t n=0;ERR_clear_error();const int rc=SSL_read_ex(ssl_.get(),in+got,in_size-got,&n);tried=true;
        if(rc==1){need(n>0&&n<=in_size-got,"TLS_READ_LENGTH");got+=n;progress=true;if(got==in_size)return;}
        else{const int e=SSL_get_error(ssl_.get(),rc);need(e==SSL_ERROR_WANT_READ||e==SSL_ERROR_WANT_WRITE,"TLS_FATAL_OR_UNEXPECTED_EOF");events|=e==SSL_ERROR_WANT_READ?POLLIN:POLLOUT;}}
      ++iteration;
      const size_t block=std::min<size_t>(16384,out_size-sent);size_t n=0;ERR_clear_error();const int rc=SSL_write_ex(ssl_.get(),out+sent,block,&n);
      if(rc==1){need(n>0&&n<=block,"TLS_WRITE_LENGTH");sent+=n;progress=true;wrote=true;}
      else{const int e=SSL_get_error(ssl_.get(),rc);need(e==SSL_ERROR_WANT_READ||e==SSL_ERROR_WANT_WRITE,"TLS_FATAL_OR_UNEXPECTED_EOF");events|=e==SSL_ERROR_WANT_READ?POLLIN:POLLOUT;wrote=false;}
      if(!progress&&(tried||got>=in_size))wait_fd(socket_.fd,events,end);
    }
  }
  // O7: one non-blocking receive attempt into in[got..in_size). Returns the poll events still needed (0 after progress).
  short receive_some(uint8_t* in,size_t in_size,size_t& got){
    owner();if(got>=in_size)return 0;size_t n=0;ERR_clear_error();const int rc=SSL_read_ex(ssl_.get(),in+got,in_size-got,&n);
    if(rc==1){need(n>0&&n<=in_size-got,"TLS_READ_LENGTH");got+=n;return 0;}
    const int e=SSL_get_error(ssl_.get(),rc);need(e==SSL_ERROR_WANT_READ||e==SSL_ERROR_WANT_WRITE,"TLS_FATAL_OR_UNEXPECTED_EOF");return e==SSL_ERROR_WANT_READ?POLLIN:POLLOUT;
  }
  // O7: wait until the socket is ready for `events`, the wake descriptor is readable, or at most `slice_ms` passed.
  void wait_socket_or(short events,int wake_fd,int slice_ms,Deadline end){
    owner();remaining(end);pollfd p[2]{{socket_.fd,events,0},{wake_fd,POLLIN,0}};const int rc=::poll(p,2,slice_ms);
    if(rc<0&&errno!=EINTR)throw std::runtime_error("SOCKET_POLL");
    need(!(p[0].revents&POLLNVAL)&&!(p[1].revents&POLLNVAL),"SOCKET_INVALID");
  }
  void close_clean(Deadline end){
    owner();for(;;){remaining(end);ERR_clear_error();const int rc=SSL_shutdown(ssl_.get());if(rc==1)return;if(rc==0)continue;need(result(rc,end)==1,"TLS_SHUTDOWN");}
  }
};
inline void write_frame(Channel& ch,const Header& h,const std::vector<uint8_t>& body,Deadline end){need(body.size()==h.bytes,"WIRE_WRITE_LENGTH");const auto b=encode(h);ch.write(b.data(),b.size(),end);ch.write(body.data(),body.size(),end);}
inline bool read_header(Channel& ch,Header& h,Deadline end,bool eof=false){HeaderBytes b{};if(!ch.read(b.data(),b.size(),end,eof))return false;h=decode(b.data(),b.size());return true;}
inline void process_signals(){std::signal(SIGPIPE,SIG_IGN);}
}
