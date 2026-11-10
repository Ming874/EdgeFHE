// OpenFHE external PRNG interface: Linux OS entropy only; no seed or fallback.
#include "utils/prng/prng.h"
#include <array>
#include <cerrno>
#include <stdexcept>
#include <sys/random.h>
class OsEngine final:public PRNG {
  std::array<uint32_t,1024> data_{};
  size_t cursor_=data_.size();
 public:
  ~OsEngine() override {volatile uint32_t* p=data_.data();for(size_t i=0;i<data_.size();i++)p[i]=0;}
  result_type operator()() override {
    if(cursor_==data_.size()) {
      auto* p=reinterpret_cast<unsigned char*>(data_.data());size_t remaining=sizeof(data_);
      while(remaining){auto n=getrandom(p,remaining,0);if(n<0&&errno==EINTR)continue;
        if(n<=0)throw std::runtime_error("OS entropy unavailable; no fallback");
        p+=n;remaining-=size_t(n);}
      cursor_=0;
    }
    auto result=data_[cursor_];data_[cursor_++]=0;return result;
  }
};
extern "C" PRNG* createEngineInstance(){return new OsEngine;}
