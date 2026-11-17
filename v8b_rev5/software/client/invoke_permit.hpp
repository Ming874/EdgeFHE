#pragma once
#include "tls.hpp"
#include <fstream>
#include <sys/stat.h>
namespace efnet {
inline std::string invocation_contract(const std::string& model,size_t rows,const std::string& mode,uint64_t requests,uint64_t window,unsigned workers,
  const std::string& ip,uint16_t port,unsigned timeout,const TlsConfig& cfg,const std::string& plugin){
  std::string out;
  for(const auto& s:std::vector<std::string>{model,std::to_string(rows),mode,std::to_string(requests),std::to_string(window),std::to_string(workers),ip,
      std::to_string(port),std::to_string(timeout),cfg.ca,cfg.certificate,cfg.key,cfg.peer_name,hex(cfg.peer_pin),plugin,hex(profile())}){
    need(!s.empty()&&s.find('\n')==std::string::npos&&s.find('\r')==std::string::npos&&s.find('\0')==std::string::npos,"PERMIT_CONFIG_STRING");out+=s+'\n';
  }
  return hex(digest(out));
}
// A private-input invocation (live demo) additionally binds the exact CSV input and score output paths,
// so a permit issued for one owner request cannot be replayed against another file.
inline std::string private_invocation_contract(const std::string& base,const std::string& input,const std::string& output){
  for(const auto& s:{input,output})need(!s.empty()&&s.size()<4096&&s.find('\n')==std::string::npos&&s.find('\r')==std::string::npos&&s.find('\0')==std::string::npos,"PERMIT_PRIVATE_PATH");
  need(base.size()==64,"PERMIT_BASE_CONTRACT");
  return hex(digest("EDGEFHE_PRIVATE_INPUT_V1\n"+base+'\n'+input+'\n'+output+'\n'));
}
struct InvocationIdentity {
  bool root=false,regular=false,readonly=false;unsigned uid=1,mode=0,links=0;
  uint64_t parent=0,device=0,inode=0;std::string boot;
};
inline std::string permit_canonical(const InvocationIdentity& id,const std::string& contract){
  return "EDGEFHE_CPP_MTLS_PL_V1\n"+id.boot+'\n'+std::to_string(id.parent)+'\n'+std::to_string(id.device)+'\n'+std::to_string(id.inode)+'\n'+contract+'\n';
}
inline void validate_invocation_permit(const std::string& text,const InvocationIdentity& id,const std::string& contract){
  need(id.root&&id.regular&&id.readonly&&id.uid==0&&id.mode==0600&&id.links==1&&id.boot.size()==36&&id.parent>1&&id.inode>0,"PL_PERMIT_IDENTITY");
  need(contract.size()==64&&nonzero(unhex(contract))&&text.size()<1024&&text==permit_canonical(id,contract),"PL_PERMIT_INVOCATION_BINDING");
}
inline void check_invocation_permit(int fd,const std::string& contract){
  need(fd>=3&&getuid()==0&&geteuid()==0,"PL_ROOT_CONTROLLER_REQUIRED");
  struct stat token{},exe{};need(fstat(fd,&token)==0&&stat("/proc/self/exe",&exe)==0&&token.st_size>0&&token.st_size<1024,"PL_PERMIT_STAT");
  char text[1024]{};need(pread(fd,text,size_t(token.st_size),0)==token.st_size,"PL_PERMIT_READ");
  std::ifstream file("/proc/sys/kernel/random/boot_id");std::string boot;std::getline(file,boot);need(bool(file),"PL_PERMIT_BOOT_READ");
  const int flags=fcntl(fd,F_GETFL);need(flags>=0,"PL_PERMIT_FLAGS");InvocationIdentity id;
  id.root=true;id.regular=S_ISREG(token.st_mode);id.readonly=(flags&O_ACCMODE)==O_RDONLY;id.uid=token.st_uid;id.mode=token.st_mode&07777;id.links=token.st_nlink;
  id.parent=getppid();id.device=exe.st_dev;id.inode=exe.st_ino;id.boot=boot;
  validate_invocation_permit(std::string(text,size_t(token.st_size)),id,contract);
  need(close(fd)==0,"PL_PERMIT_REVOKE_BEFORE_OPEN");
}
}
