// Three math lanes: the caller plus two persistent helpers. No nested dispatch.
#pragma once
#include <array>
#include <condition_variable>
#include <exception>
#include <functional>
#include <mutex>
#include <thread>
#include <stdexcept>
#include <cfenv>

namespace edgefhe {
class WorkerPool {
  unsigned count_;
  std::array<std::thread,2> threads_;
  std::mutex dispatch_,mutex_;
  std::condition_variable ready_,done_;
  std::function<void(size_t)> task_;
  std::array<std::exception_ptr,3> errors_{};
  uint64_t epoch_=0;
  unsigned completed_=0;
  bool stop_=false;
  void loop(size_t lane) {
    std::fesetround(FE_TONEAREST);
    uint64_t seen=0;
    for(;;) {
      std::unique_lock<std::mutex> guard(mutex_);
      ready_.wait(guard,[&]{return stop_||epoch_!=seen;});
      if(stop_)return;
      seen=epoch_;guard.unlock();
      try{task_(lane);}catch(...){errors_[lane]=std::current_exception();}
      guard.lock();completed_++;done_.notify_one();
    }
  }
 public:
  explicit WorkerPool(unsigned count):count_(count) {
    if(count!=1&&count!=3)throw std::invalid_argument("Worker count must be 1 or 3");
    if(count==1)return;
    try{for(size_t i=0;i<2;i++)threads_[i]=std::thread([&,i]{loop(i+1);});}
    catch(...){ {std::lock_guard<std::mutex> g(mutex_);stop_=true;}ready_.notify_all();
      for(auto& t:threads_){if(t.joinable())t.join();}
      throw;
    }
  }
  ~WorkerPool(){ {std::lock_guard<std::mutex> g(mutex_);stop_=true;}ready_.notify_all();for(auto& t:threads_)if(t.joinable())t.join(); }
  WorkerPool(const WorkerPool&)=delete;
  void run(const std::function<void(size_t)>& task) {
    std::lock_guard<std::mutex> owner(dispatch_);
    if(count_==1){for(size_t i=0;i<3;i++)task(i);return;}
    {std::lock_guard<std::mutex> g(mutex_);task_=task;errors_={};completed_=0;epoch_++;}
    ready_.notify_all();
    try{task(0);}catch(...){errors_[0]=std::current_exception();}
    std::unique_lock<std::mutex> g(mutex_);done_.wait(g,[&]{return completed_==2;});
    task_={};auto errors=errors_;g.unlock();for(auto e:errors)if(e)std::rethrow_exception(e);
  }
};
}
