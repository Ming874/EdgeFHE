// Stateless P1 normalization candidate. No key, FPGA, entropy or network API.
#include <cmath>
#include <cstddef>
#include <cstdint>
namespace {
constexpr std::size_t maximum=2048*32;
void erase(double* out,std::size_t n) noexcept {
  if(out&&n<=maximum){volatile double* p=out;for(std::size_t i=0;i<n;i++)p[i]=0.0;}
}
}
extern "C" int ef_normalize_checked(const double* raw,std::size_t rows,std::size_t features,
  const double* mean,const double* sd,int uci529,int dictionary_valid,double* out,std::size_t count) noexcept {
  auto fail=[&](int code){erase(out,count);return code;};
  if(!raw||!mean||!sd||!out||rows==0||rows>2048||features==0||features>32||
     count!=rows*features||(uci529!=0&&uci529!=1)||(dictionary_valid!=0&&dictionary_valid!=1))return fail(1);
  for(std::size_t f=0;f<features;f++)if(!std::isfinite(mean[f])||!std::isfinite(sd[f])||sd[f]<=0)return fail(7);
  // Keep original Python row-level refusal order: numeric/finite, dictionary,
  // UCI age/binary semantics, then normalized-domain checks. No clipping.
  for(std::size_t r=0;r<rows;r++){
    const double* row=raw+r*features;
    for(std::size_t f=0;f<features;f++)if(!std::isfinite(row[f]))return fail(2);
    if(uci529){
      if(!dictionary_valid)return fail(3);
      if(std::trunc(row[0])!=row[0]||row[0]<16||row[0]>90)return fail(4);
      for(std::size_t f=1;f<features;f++)if(row[f]!=0.0&&row[f]!=1.0)return fail(5);
    }
    for(std::size_t f=0;f<features;f++){
      const double delta=row[f]-mean[f];const double value=delta/sd[f];
      if(!std::isfinite(value)||std::abs(value)>8.0)return fail(6);
      out[r*features+f]=value;
    }
  }
  return 0;
}
