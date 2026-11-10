// Cloud-only C ABI. No keygen, sampler, secret, decrypt, or device API.
#include "core.hpp"
#include <algorithm>
#include <string>
using namespace edgefhe;
static thread_local std::string last_error;
struct Context {
  Engine engine{3};std::array<Message,4> weight;Message bias;
  Context(){const std::array<double,4> w={1.1,-.7,.9,.4};
    for(size_t f=0;f<4;f++)weight[f]=encode(std::vector<std::complex<double>>(slots,w[f]));
    bias=encode(std::vector<std::complex<double>>(slots,-.35),scale*scale);}
};
static Rns read(const uint32_t* in){require(in,"Null cloud input");Rns p;for(size_t l=0;l<3;l++)p[l]=Poly(in+l*degree,in+(l+1)*degree);validate(p);return p;}
static void write(const Rns& p,uint32_t* out){for(size_t l=0;l<3;l++)std::copy(p[l].begin(),p[l].end(),out+l*degree);}
extern "C" {
const char* v7_cloud_error(){return last_error.c_str();}
void* v7_cloud_create(){try{return new Context;}catch(const std::exception& e){last_error=e.what();return nullptr;}}
void v7_cloud_destroy(void* context){delete static_cast<Context*>(context);}
int v7_cloud_affine(void* context,const uint32_t* in,uint32_t* out){try{
  require(context&&in&&out,"Null cloud arguments");auto& ctx=*static_cast<Context*>(context);std::array<Cipher,4> c;
  for(size_t f=0;f<4;f++){c[f].c0=read(in+f*6*degree);c[f].c1=read(in+(f*6+3)*degree);}
  auto result=ctx.engine.affine(c,ctx.weight,ctx.bias);
  write(result.c0,out);write(result.c1,out+3*degree);last_error.clear();return 0;
}catch(const std::exception& e){last_error=e.what();return -1;}}
}
