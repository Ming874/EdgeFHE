#include "buffer_slots.hpp"
#include <iostream>
#include <thread>
#include <atomic>
using namespace edgefhe;
static void require(bool ok){if(!ok)throw std::runtime_error("Queue invariant");}
template<class F>static void refuses(F action){bool rejected=false;try{action();}catch(const std::logic_error&){rejected=true;}require(rejected);}
int main(){try{
  BufferSlots q;auto first=*q.acquire(),second=*q.acquire();require(!q.acquire());
  refuses([&]{q.release(first);});q.submit(first);refuses([&]{q.release(first);});q.complete(first);q.release(first);
  auto renewed=*q.acquire();require(renewed.generation!=first.generation);refuses([&]{q.submit(first);});
  q.submit(second);q.fail(second);require(q.poisoned()&&!q.acquire());refuses([&]{q.submit(renewed);});refuses([&]{q.complete(second);});
  BufferSlots concurrent;std::atomic<unsigned> completed{0};std::array<std::thread,4> t;
  for(auto& worker:t)worker=std::thread([&]{for(unsigned i=0;i<1000;i++){
    std::optional<BufferSlots::Lease> lease;while(!(lease=concurrent.acquire()))std::this_thread::yield();
    concurrent.submit(*lease);concurrent.complete(*lease);concurrent.release(*lease);completed++;}});
  for(auto& worker:t){worker.join();}require(completed==4000);
  std::cout<<"V7_BUFFER_OWNERSHIP_SOFTWARE_PASS slots=2 completed=4000 stale_rejected=1 quarantine_reuse_forbidden=1 hardware_quiescence_accepted=0\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
