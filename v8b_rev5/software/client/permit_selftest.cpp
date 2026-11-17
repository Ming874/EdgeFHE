#include "invoke_permit.hpp"
#include <iostream>
int main(){using namespace efnet;
  InvocationIdentity id;id.root=id.regular=id.readonly=true;id.uid=0;id.mode=0600;id.links=1;id.parent=123;id.device=88;id.inode=99;id.boot="da6d5dd4-c8c1-41fa-bec7-544176e23cc1";
  const auto contract=hex(digest(std::string("public invocation fixture")));const auto token=permit_canonical(id,contract);validate_invocation_permit(token,id,contract);size_t rejected=0;
  auto reject=[&](InvocationIdentity value,const std::string& t,const std::string& c){try{validate_invocation_permit(t,value,c);}catch(const std::exception&){++rejected;return;}throw std::runtime_error("PERMIT_MUTANT_ACCEPTED");};
  for(unsigned i=0;i<11;i++){auto x=id;switch(i){case 0:x.root=false;break;case 1:x.regular=false;break;case 2:x.readonly=false;break;case 3:x.uid=1000;break;case 4:x.mode=0644;break;case 5:x.links=2;break;case 6:x.parent++;break;case 7:x.device++;break;case 8:x.inode++;break;case 9:x.boot[0]='b';break;case 10:x.mode=04600;break;}reject(x,token,contract);}
  reject(id,token+'x',contract);reject(id,token,hex(digest(std::string("other invocation"))));reject(id,token,std::string(64,'0'));
  need(rejected==14,"PERMIT_SELFTEST_COUNT");std::cout<<"CPP_MTLS_INVOCATION_PERMIT_PASS positive=1 refused=14 NO_DEVICE\n";
}
