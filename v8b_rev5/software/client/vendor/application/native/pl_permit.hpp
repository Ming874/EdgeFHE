#pragma once
#include <fcntl.h>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>
namespace edgefhe {
struct PermitIdentity {
  bool root=false,regular=false,readonly=false;
  unsigned uid=1,mode=0,links=0;
  unsigned long long parent=0,device=0,inode=0,rows=0,rounds=0;
  std::string boot,model;
};
inline void permit_need(bool ok){if(!ok)throw std::runtime_error("PL permit identity/contract refused before device open");}
inline void validate_permit(const std::string& text,const PermitIdentity& actual){
  permit_need(actual.root&&actual.regular&&actual.readonly&&actual.uid==0&&actual.mode==0600&&actual.links==1);
  std::vector<std::string> expected={"EDGEFHE_NATIVE_CPP_PL_V1",actual.boot,std::to_string(actual.parent),std::to_string(actual.device),
    std::to_string(actual.inode),actual.model,std::to_string(actual.rows),std::to_string(actual.rounds)};
  std::string canonical;for(const auto& row:expected)canonical+=row+'\n';
  permit_need(text==canonical&&text.size()<1024&&actual.rows>=1&&actual.rows<=2048&&actual.rounds==2);
}
inline void check_pl_permit(int fd,const std::string& model,size_t rows,size_t rounds){
  permit_need(fd>=3&&::geteuid()==0&&::getuid()==0);
  struct stat token{},exe{};permit_need(::fstat(fd,&token)==0&&::stat("/proc/self/exe",&exe)==0);
  permit_need(token.st_size>0&&token.st_size<1024);
  char raw[1024]{};const auto size=::pread(fd,raw,size_t(token.st_size),0);permit_need(size==token.st_size);
  std::ifstream bootfile("/proc/sys/kernel/random/boot_id");std::string boot;std::getline(bootfile,boot);permit_need(bool(bootfile)&&boot.size()==36);
  const int flags=::fcntl(fd,F_GETFL);permit_need(flags>=0);
  PermitIdentity info;info.root=true;info.regular=S_ISREG(token.st_mode);info.readonly=(flags&O_ACCMODE)==O_RDONLY;
  info.uid=token.st_uid;info.mode=token.st_mode&0777;info.links=token.st_nlink;info.parent=::getppid();
  info.device=exe.st_dev;info.inode=exe.st_ino;info.rows=rows;info.rounds=rounds;info.boot=boot;info.model=model;
  validate_permit(std::string(raw,size_t(size)),info);
  permit_need(::close(fd)==0); // Revoke this inherited capability before the owner opens the node.
}
} // namespace edgefhe
