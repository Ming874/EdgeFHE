// Contract vectors of the demo gateway (demo/edge/efgw/permit.py), printed as JSON.
//
// Every value comes from the unchanged v8b_rev5/software/client/invoke_permit.hpp (invocation_contract,
// private_invocation_contract, permit_canonical, validate_invocation_permit) and from the contract layer expressions
// of client.cpp:144-149, copied verbatim into layered() below. demo/edge/tests/contract_vectors.json is the output
// of this program; test_core.py recomputes every contract and permit text in Python and requires equality, and it
// replays the permit cases against the fake client's permit check.
//
// build and compare (Ubuntu 22.04 with g++ and libssl-dev; from the repository root):
//   g++ -std=c++17 -I v8b_rev5/software/client demo/edge/tests/contract_vector.cpp -lssl -lcrypto -o /tmp/cv
//   /tmp/cv | diff - demo/edge/tests/contract_vectors.json && echo CONTRACT_VECTORS_MATCH
#include "invoke_permit.hpp"
#include <iostream>
#include <string>
#include <vector>

namespace {
using namespace efnet;

struct Flags {
  std::string mode="serial";size_t slots=2;bool duplex=false,hash_io=false,decrypt_thread=false;size_t inflight=0;
  std::string io_cpu="none";bool io_isolate=false;unsigned read_every=1;
};
struct Vector {
  std::string name;size_t rows;uint64_t requests,window_ms;Flags flags;std::string input,output;
};
struct Case {std::string name;InvocationIdentity id;std::string text,contract;};

// client.cpp:144-149, verbatim; the client's local variables are bound to the fields of Flags first.
std::string layered(const std::string& base_contract,const Flags& f){
  const std::string& mode=f.mode;const size_t capacity=mode=="pipeline"?f.slots:1;
  const bool duplex=f.duplex,hash_io=f.hash_io,decrypt_thread=f.decrypt_thread,io_isolate=f.io_isolate;
  const size_t inflight=f.inflight;const std::string& io_cpu_text=f.io_cpu;const unsigned read_every=f.read_every;
  const auto depth_contract=capacity==2||mode!="pipeline"?base_contract:hex(digest("EDGEFHE_SLOTS_V1\n"+base_contract+'\n'+std::to_string(capacity)+'\n'));
  auto io_contract=duplex?hex(digest("EDGEFHE_DUPLEX_V1\n"+depth_contract+'\n')):depth_contract;
  if(hash_io)io_contract=hex(digest("EDGEFHE_HASH_IO_V1\n"+io_contract+'\n'));
  if(decrypt_thread)io_contract=hex(digest("EDGEFHE_DECRYPT_THREAD_V1\n"+io_contract+'\n'));
  if(inflight)io_contract=hex(digest("EDGEFHE_INFLIGHT_V1\n"+io_contract+'\n'+std::to_string(inflight)+'\n'));
  if(io_cpu_text!="none"||read_every!=1)io_contract=hex(digest("EDGEFHE_IO_PLACEMENT_V1\n"+io_contract+'\n'+io_cpu_text+'\n'+(io_isolate?"1":"0")+'\n'+std::to_string(read_every)+'\n'));
  return io_contract;
}

std::string js(const std::string& s){
  std::string out="\"";
  for(const char c:s){
    if(c=='"'||c=='\\'){out+='\\';out+=c;}
    else if(c=='\n')out+="\\n";
    else out+=c;
  }
  return out+"\"";
}
const char* boolean(bool value){return value?"true":"false";}
std::string optional(const std::string& s){return s.empty()?"null":js(s);}

std::string outcome(const Case& c){
  try{validate_invocation_permit(c.text,c.id,c.contract);return "ACCEPTED";}
  catch(const std::exception& e){return e.what();}
}
}

int main(){
  const std::string model="cdc-diabetes-lr4-v1",host="192.0.2.10";
  const std::string plugin="/home/ubuntu/edgefhe-demo/build-0123abcd/app/frozen_build/baseline/libedgefhe_os_prng.so";
  const uint16_t port=18443;const unsigned timeout=30000,workers=3;
  TlsConfig cfg;
  cfg.ca="/home/ubuntu/edgefhe-demo/pki/ca.pem";cfg.certificate="/home/ubuntu/edgefhe-demo/pki/client.pem";
  cfg.key="/home/ubuntu/edgefhe-demo/pki/client.key";cfg.peer_name="edgefhe-cloud";
  cfg.peer_pin=digest(std::string("EdgeFHE demo contract vector: example server certificate pin"));

  Flags best;best.mode="pipeline";best.slots=8;best.duplex=best.hash_io=best.decrypt_thread=true;best.inflight=8;best.io_cpu="auto";
  Flags two;two.mode="pipeline";
  Flags placed;placed.mode="pipeline";placed.slots=4;placed.duplex=true;placed.inflight=4;placed.io_cpu="1";placed.io_isolate=true;placed.read_every=4;
  const std::vector<Vector> vectors{
    {"form",1,1,0,Flags{},"/run/edgefhe-demo/x/in.csv","/run/edgefhe-demo/x/score.txt"},
    {"synthetic",2048,20,0,Flags{},"",""},
    {"pipeline",2048,100000,30000,best,"",""},
    {"pipeline_two_slots",2048,50,0,two,"",""},
    {"pipeline_io_placement",2048,1000,0,placed,"",""},
  };

  std::cout<<"{\n  \"schema\": \"edgefhe.demo.contract-vectors.v1\",\n";
  std::cout<<"  \"generator\": \"demo/edge/tests/contract_vector.cpp\",\n";
  std::cout<<"  \"profile_hex\": "<<js(hex(profile()))<<",\n";
  std::cout<<"  \"config\": {\n";
  std::cout<<"    \"model\": "<<js(model)<<",\n    \"workers\": "<<workers<<",\n    \"host\": "<<js(host)<<",\n";
  std::cout<<"    \"port\": "<<port<<",\n    \"timeout_ms\": "<<timeout<<",\n    \"ca\": "<<js(cfg.ca)<<",\n";
  std::cout<<"    \"cert\": "<<js(cfg.certificate)<<",\n    \"key\": "<<js(cfg.key)<<",\n";
  std::cout<<"    \"peer_name\": "<<js(cfg.peer_name)<<",\n    \"peer_sha256\": "<<js(hex(cfg.peer_pin))<<",\n";
  std::cout<<"    \"plugin\": "<<js(plugin)<<"\n  },\n";
  std::cout<<"  \"vectors\": [\n";
  std::string form_contract;
  for(size_t i=0;i<vectors.size();i++){
    const auto& v=vectors[i];const auto& f=v.flags;
    const auto base=invocation_contract(model,v.rows,f.mode,v.requests,v.window_ms,workers,host,port,timeout,cfg,plugin);
    const auto io=layered(base,f);
    const auto contract=v.input.empty()?io:private_invocation_contract(io,v.input,v.output);
    if(v.name=="form")form_contract=contract;
    std::cout<<"    {\n      \"name\": "<<js(v.name)<<",\n      \"rows\": "<<v.rows<<",\n      \"mode\": "<<js(f.mode)<<",\n";
    std::cout<<"      \"requests\": "<<v.requests<<",\n      \"window_ms\": "<<v.window_ms<<",\n";
    std::cout<<"      \"flags\": {\"slots\": "<<f.slots<<", \"duplex\": "<<boolean(f.duplex)<<", \"hash_io\": "<<boolean(f.hash_io)
             <<", \"decrypt_thread\": "<<boolean(f.decrypt_thread)<<", \"inflight\": "<<f.inflight<<", \"io_cpu\": "<<js(f.io_cpu)
             <<", \"io_isolate\": "<<boolean(f.io_isolate)<<", \"read_every\": "<<f.read_every<<"},\n";
    std::cout<<"      \"input\": "<<optional(v.input)<<",\n      \"output\": "<<optional(v.output)<<",\n";
    std::cout<<"      \"base\": "<<js(base)<<",\n      \"layered\": "<<js(io)<<",\n      \"contract\": "<<js(contract)<<"\n";
    std::cout<<"    }"<<(i+1<vectors.size()?",":"")<<"\n";
  }
  std::cout<<"  ],\n";

  InvocationIdentity id;id.root=id.regular=id.readonly=true;id.uid=0;id.mode=0600;id.links=1;id.parent=4242;id.device=66306;id.inode=1311490;
  id.boot="da6d5dd4-c8c1-41fa-bec7-544176e23cc1";
  const auto text=permit_canonical(id,form_contract);
  std::string flipped=text;char& digit=flipped[flipped.size()-65];digit=digit=='0'?'1':'0';
  const std::string zero(64,'0');
  std::vector<Case> cases{{"accepted",id,text,form_contract},{"contract_byte_flipped",id,flipped,form_contract},
                          {"text_extra_byte",id,text+"x",form_contract},{"zero_contract",id,permit_canonical(id,zero),zero}};
  auto mutate=[&](const std::string& name,auto change){auto x=id;change(x);cases.push_back({name,x,text,form_contract});};
  mutate("mode_0644",[](InvocationIdentity& x){x.mode=0644;});
  mutate("mode_04600",[](InvocationIdentity& x){x.mode=04600;});
  mutate("links_2",[](InvocationIdentity& x){x.links=2;});
  mutate("uid_1000",[](InvocationIdentity& x){x.uid=1000;});
  mutate("not_regular",[](InvocationIdentity& x){x.regular=false;});
  mutate("not_readonly",[](InvocationIdentity& x){x.readonly=false;});
  mutate("parent_1",[](InvocationIdentity& x){x.parent=1;});
  mutate("parent_other",[](InvocationIdentity& x){x.parent=4243;});
  mutate("boot_other",[](InvocationIdentity& x){x.boot[0]='e';});
  mutate("device_other",[](InvocationIdentity& x){x.device++;});
  mutate("inode_other",[](InvocationIdentity& x){x.inode++;});
  std::cout<<"  \"permit\": {\n    \"vector\": \"form\",\n    \"boot_id\": "<<js(id.boot)<<",\n";
  std::cout<<"    \"controller_pid\": "<<id.parent<<",\n    \"exe_dev\": "<<id.device<<",\n    \"exe_ino\": "<<id.inode<<",\n";
  std::cout<<"    \"text\": "<<js(text)<<",\n    \"cases\": [\n";
  for(size_t i=0;i<cases.size();i++){
    const auto& c=cases[i];const auto& x=c.id;
    std::cout<<"      {\"name\": "<<js(c.name)<<", \"code\": "<<js(outcome(c))<<",\n";
    std::cout<<"       \"identity\": {\"root\": "<<boolean(x.root)<<", \"regular\": "<<boolean(x.regular)<<", \"readonly\": "<<boolean(x.readonly)
             <<", \"uid\": "<<x.uid<<", \"mode\": "<<x.mode<<", \"links\": "<<x.links<<", \"parent\": "<<x.parent
             <<", \"device\": "<<x.device<<", \"inode\": "<<x.inode<<", \"boot\": "<<js(x.boot)<<"},\n";
    std::cout<<"       \"contract\": "<<js(c.contract)<<",\n       \"text\": "<<js(c.text)<<"}"<<(i+1<cases.size()?",":"")<<"\n";
  }
  std::cout<<"    ]\n  }\n}\n";
  return 0;
}
