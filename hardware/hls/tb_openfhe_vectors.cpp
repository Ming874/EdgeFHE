#include "rns_ntt_parallel_rns3.hpp"
#include "rns_ntt_parallel_constants.hpp"
#include <cstdlib>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <vector>
static void require(bool passed,const char* message){if(!passed)throw std::runtime_error(message);}
static uint32_t read_word(std::ifstream& input){uint32_t value=0;for(int b=0;b<4;++b){int c=input.get();require(c!=EOF,"truncated vector file");value|=uint32_t(uint8_t(c))<<(8*b);}return value;}
using Poly=std::vector<std::vector<uint32_t>>;
static void push(hls::stream<rns3_axis_word_t>& stream,const Poly& p,int index,bool last){
    rns3_axis_word_t word;word.data=0;word.keep=-1;word.strb=-1;word.last=last;
    for(int limb=0;limb<3;++limb)word.data.range(32*limb+31,32*limb)=p[limb][index];
    stream.write(word);
}
int main(){try{
    const char* vector_path=std::getenv("EDGEFHE_VECTOR_FILE");
    require(vector_path!=nullptr,"EDGEFHE_VECTOR_FILE missing");
    std::ifstream file(vector_path,std::ios::binary);
    require(file.good(),"OpenFHE vector file missing");
    char magic[8];file.read(magic,8);const char expected_magic[8]={'E','F','N','T','T','V','1',0};
    for(int i=0;i<8;++i)require(magic[i]==expected_magic[i],"bad magic");
    require(read_word(file)==1 && read_word(file)==4096 && read_word(file)==3 && read_word(file)==16,"bad vector dimensions");
    for(int limb=0;limb<3;++limb)require(read_word(file)==RNS_MODULI[limb],"q mismatch");
    for(int limb=0;limb<3;++limb)require(read_word(file)==RNS_PSI[limb],"psi mismatch");
    uint64_t compared=0;
    for(int test=0;test<16;++test){
        const uint32_t op=read_word(file);require(op<=2,"unsupported vector operation");
        Poly p[3];for(int which=0;which<3;++which){p[which]=Poly(3,std::vector<uint32_t>(4096));for(int limb=0;limb<3;++limb)for(int i=0;i<4096;++i){p[which][limb][i]=read_word(file);require(p[which][limb][i]<RNS_MODULI[limb],"noncanonical vector");}}
        hls::stream<rns3_axis_word_t> input,output;
        for(int i=0;i<4096;++i)push(input,p[0],i,op!=2 && i==4095);
        if(op==2)for(int i=0;i<4096;++i)push(input,p[1],i,i==4095);
        rns3_ntt_parallel_accel_n4096(input,output,operation_t(op));
        require(input.empty(),"unconsumed input");
        for(int i=0;i<4096;++i){
            require(!output.empty(),"short output");auto word=output.read();
            require(word.last==(i==4095),"TLAST mismatch");require(word.keep==0xffff && word.strb==0xffff,"byte validity mismatch");require(word.data.range(127,96)==0,"padding mismatch");
            for(int limb=0;limb<3;++limb){uint32_t actual=word.data.range(32*limb+31,32*limb).to_uint();if(actual!=p[2][limb][i]){std::cerr<<"case="<<test<<" op="<<op<<" limb="<<limb<<" i="<<i<<" actual="<<actual<<" expected="<<p[2][limb][i]<<'\n';throw std::runtime_error("OpenFHE/HLS coefficient mismatch");}++compared;}
        }
        require(output.empty(),"extra output");
    }
    require(file.peek()==EOF,"extra vector data");
    std::cout<<"OpenFHE/HLS V1 exact parity: PASS (16 transactions, "<<compared<<" coefficients, AXI sidebands checked)\n";return 0;
}catch(const std::exception& error){std::cerr<<"OpenFHE/HLS parity FAIL: "<<error.what()<<'\n';return 1;}}
