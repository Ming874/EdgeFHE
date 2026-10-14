// Persistent worker threads. V4 Engine::parallel starts two std::threads on every call;
// these are created once and reused, so short batches do not pay thread start-up.
#pragma once
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <exception>
#include <functional>
#include <future>
#include <mutex>
#include <thread>
#include <vector>

namespace edgefhe::v5 {
// Fork-join pool. run() blocks until every index has finished; the calling thread also
// executes tasks, so `workers` counts the caller. After the first exception no new
// index starts; that exception is rethrown once all running tasks have returned.
// Not reentrant: a task must not call run() on the same pool.
class ThreadPool {
 public:
  explicit ThreadPool(unsigned workers);
  ~ThreadPool();
  ThreadPool(const ThreadPool&) = delete;
  ThreadPool& operator=(const ThreadPool&) = delete;

  unsigned workers() const { return workers_; }
  void run(size_t tasks, const std::function<void(size_t)>& task);

 private:
  void work();
  void execute(std::unique_lock<std::mutex>& lock);
  void shutdown();

  unsigned workers_;
  std::vector<std::thread> threads_;
  std::mutex run_mutex_;  // serializes concurrent run() callers
  std::mutex mutex_;
  std::condition_variable wake_, done_;
  const std::function<void(size_t)>* task_ = nullptr;
  size_t tasks_ = 0, next_ = 0, active_ = 0;
  uint64_t generation_ = 0;
  bool stop_ = false;
  std::exception_ptr error_;
};

// One background thread that runs submitted jobs in order. Used to overlap packing and
// unpacking with the blocking DMA call. Pending jobs still run before destruction.
class SerialWorker {
 public:
  SerialWorker();
  ~SerialWorker();
  SerialWorker(const SerialWorker&) = delete;
  SerialWorker& operator=(const SerialWorker&) = delete;

  std::future<void> submit(std::function<void()> job);

 private:
  void work();

  std::mutex mutex_;
  std::condition_variable wake_;
  std::deque<std::packaged_task<void()>> jobs_;
  bool stop_ = false;
  std::thread thread_;  // last: starts after the members it uses exist
};
}  // namespace edgefhe::v5
