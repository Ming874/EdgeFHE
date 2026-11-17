// Portable reference for the future driver's single-owner cache state machine.
// No DMA/MMIO here. All methods require the device mutex in the eventual driver.
#pragma once
#include <array>
#include <cstdint>
#include <limits>
#include <stdexcept>
namespace edgefhe_v7 {
using Digest=std::array<uint8_t,32>;
struct Completion {
    bool tx_done=false,rx_done=false,tx_ok=false,rx_ok=false,ap_done=false;
    uint32_t hw_status=0;
    bool zero_ack=false;
};
class LeaseState {
    uint64_t next_=0,generation_=0,owner_=0;
    bool valid_=false,active_=false,poisoned_=false,quarantined_=false;
    Digest profile_{},key_{};
    void invalidate(){valid_=false;generation_=0;owner_=0;profile_.fill(0);key_.fill(0);}
public:
    // Reference model can start at a persisted counter for wrap-boundary tests.
    explicit LeaseState(uint64_t initial_counter=0):next_(initial_counter){}
    void require_available()const{if(poisoned_||quarantined_||active_)throw std::runtime_error("Owner unavailable");}
    uint64_t begin_load(uint64_t owner,const Digest& profile,const Digest& key) {
        require_available();if(!owner)throw std::invalid_argument("Zero owner");
        invalidate();if(next_==std::numeric_limits<uint64_t>::max()){poisoned_=true;throw std::overflow_error("Generation exhausted");}
        generation_=++next_;owner_=owner;profile_=profile;key_=key;active_=true;return generation_;
    }
    void begin_pair(uint64_t owner,uint64_t generation,const Digest& profile,const Digest& key){
        require_available();if(!valid_||!generation||owner!=owner_||generation!=generation_||profile!=profile_||key!=key_)throw std::invalid_argument("Cache lease mismatch");active_=true;
    }
    void begin_legacy(){require_available();active_=true;}
    // Missing completion is a timeout/fatal transport failure; never publishes a key.
    bool finish(const Completion& c,bool load,bool rx_only=false){
        if(!active_)throw std::logic_error("No active transaction");
        bool complete=c.rx_done&&c.rx_ok&&c.ap_done&&(rx_only||(c.tx_done&&c.tx_ok));
        active_=false;
        if(!complete){poisoned_=true;invalidate();return false;}
        if(c.hw_status||(load&&!c.zero_ack)){invalidate();return false;}
        if(load)valid_=true;
        return true;
    }
    void close(uint64_t owner){if(owner==owner_)invalidate();}
    void invalidate_command(){require_available();invalidate();active_=true;}
    void fatal(bool dma_termination_proven){active_=false;poisoned_=true;quarantined_=!dma_termination_proven;invalidate();}
    // Hardware reset alone cannot clear a poison state. Recovery is separately gated.
    void hardware_reset_observed(){invalidate();if(active_){active_=false;poisoned_=true;}}
    bool valid_for(uint64_t owner,uint64_t gen)const{return valid_&&!poisoned_&&!quarantined_&&!active_&&owner==owner_&&gen==generation_;}
    bool active()const{return active_;}bool poisoned()const{return poisoned_;}bool quarantined()const{return quarantined_;}
};
}
