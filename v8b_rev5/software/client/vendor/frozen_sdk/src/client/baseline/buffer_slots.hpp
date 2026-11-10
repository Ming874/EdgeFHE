// Software ownership gate. Hardware quiescence must be proven by the driver.
#pragma once
#include <array>
#include <cstdint>
#include <mutex>
#include <optional>
#include <stdexcept>
namespace edgefhe {
class BufferSlots {
 public:
  enum class State{Free,CpuReady,Inflight,CpuResult,Quarantined};
  struct Lease{size_t slot;uint64_t generation;};
 private:
  struct Slot{State state=State::Free;uint64_t generation=0;};
  std::array<Slot,2> slots_{};
  mutable std::mutex mutex_;
  bool poisoned_=false;
  Slot& check(Lease l,State expected){
    if(l.slot>=slots_.size()||slots_[l.slot].generation!=l.generation||slots_[l.slot].state!=expected)
      throw std::logic_error("Stale lease or invalid buffer ownership transition");
    return slots_[l.slot];
  }
 public:
  std::optional<Lease> acquire(){
    std::lock_guard<std::mutex> g(mutex_);if(poisoned_)return {};
    for(size_t i=0;i<2;i++)if(slots_[i].state==State::Free){
      if(slots_[i].generation==UINT64_MAX)throw std::overflow_error("Lease generation exhausted");
      slots_[i].generation++;slots_[i].state=State::CpuReady;return Lease{i,slots_[i].generation};}
    return {};
  }
  void submit(Lease l){std::lock_guard<std::mutex> g(mutex_);if(poisoned_)throw std::logic_error("DMA owner poisoned");check(l,State::CpuReady).state=State::Inflight;}
  void complete(Lease l){std::lock_guard<std::mutex> g(mutex_);check(l,State::Inflight).state=State::CpuResult;}
  void release(Lease l){std::lock_guard<std::mutex> g(mutex_);check(l,State::CpuResult).state=State::Free;}
  void fail(Lease l){std::lock_guard<std::mutex> g(mutex_);check(l,State::Inflight).state=State::Quarantined;poisoned_=true;}
  bool poisoned()const{std::lock_guard<std::mutex> g(mutex_);return poisoned_;}
  // Intentionally no reset/unquarantine API: an application boolean cannot
  // prove that PL/DDR/callbacks stopped accessing memory. Destroy only after
  // actual driver teardown; a future reset-capable driver owns that proof.
};
}
