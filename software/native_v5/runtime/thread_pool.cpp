#include "runtime/thread_pool.hpp"
#include "core.hpp"

namespace edgefhe::v5 {
ThreadPool::ThreadPool(unsigned workers) : workers_(workers) {
  require(workers >= 1 && workers <= 64, "Thread pool needs 1..64 workers");
  threads_.reserve(workers - 1);
  try {
    for (unsigned i = 1; i < workers; i++) threads_.emplace_back([this] { work(); });
  } catch (...) {
    shutdown();
    throw;
  }
}

ThreadPool::~ThreadPool() { shutdown(); }

void ThreadPool::shutdown() {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    stop_ = true;
  }
  wake_.notify_all();
  for (auto& thread : threads_)
    if (thread.joinable()) thread.join();
}

// Claims indices until none remain. A worker that wakes late for a finished run sees
// next_ >= tasks_ and never touches the (already released) task pointer.
void ThreadPool::execute(std::unique_lock<std::mutex>& lock) {
  while (next_ < tasks_) {
    const size_t index = next_++;
    active_++;
    lock.unlock();
    std::exception_ptr failure;
    try {
      (*task_)(index);
    } catch (...) {
      failure = std::current_exception();
    }
    lock.lock();
    active_--;
    if (failure) {
      if (!error_) error_ = failure;
      next_ = tasks_;  // stop handing out new indices
    }
  }
  if (active_ == 0) done_.notify_all();
}

void ThreadPool::work() {
  std::unique_lock<std::mutex> lock(mutex_);
  uint64_t seen = 0;
  for (;;) {
    wake_.wait(lock, [&] { return stop_ || generation_ != seen; });
    if (stop_) return;
    seen = generation_;
    execute(lock);
  }
}

void ThreadPool::run(size_t tasks, const std::function<void(size_t)>& task) {
  if (tasks == 0) return;
  std::lock_guard<std::mutex> serial(run_mutex_);
  std::unique_lock<std::mutex> lock(mutex_);
  task_ = &task;
  tasks_ = tasks;
  next_ = 0;
  error_ = nullptr;
  generation_++;
  lock.unlock();
  wake_.notify_all();
  lock.lock();
  execute(lock);
  done_.wait(lock, [&] { return next_ >= tasks_ && active_ == 0; });
  task_ = nullptr;
  tasks_ = next_ = 0;
  const auto error = error_;
  error_ = nullptr;
  lock.unlock();
  if (error) std::rethrow_exception(error);
}

SerialWorker::SerialWorker() : thread_([this] { work(); }) {}

SerialWorker::~SerialWorker() {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    stop_ = true;
  }
  wake_.notify_all();
  thread_.join();
}

std::future<void> SerialWorker::submit(std::function<void()> job) {
  std::packaged_task<void()> task(std::move(job));
  auto result = task.get_future();
  {
    std::lock_guard<std::mutex> lock(mutex_);
    require(!stop_, "Serial worker is stopping");
    jobs_.push_back(std::move(task));
  }
  wake_.notify_one();
  return result;
}

void SerialWorker::work() {
  std::unique_lock<std::mutex> lock(mutex_);
  for (;;) {
    wake_.wait(lock, [&] { return stop_ || !jobs_.empty(); });
    if (jobs_.empty()) return;  // stop_ and fully drained
    auto job = std::move(jobs_.front());
    jobs_.pop_front();
    lock.unlock();
    job();  // packaged_task stores any exception in the future
    lock.lock();
  }
}
}  // namespace edgefhe::v5
