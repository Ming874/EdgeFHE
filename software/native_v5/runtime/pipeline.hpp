// Bounded N-stage pipeline (plan "H" in docs/ARCHITECTURE.md). Every item goes
// through the same named stages in order. With capacity 0 the caller runs each item
// through all stages before the next item (the serial reference). With capacity >= 1
// each stage has its own thread and queues of that capacity connect them: while stage
// k handles batch i, stage k-1 already works on batch i+1. One thread per stage keeps
// the output order equal to the input order, and a stage that owns a device is never
// entered concurrently. A slow stage applies back-pressure instead of buffering without
// bound. The first exception cancels every stage and is rethrown by run_stages().
#pragma once
#include "common/stats.hpp"
#include "core.hpp"
#include <algorithm>
#include <condition_variable>
#include <deque>
#include <exception>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace edgefhe::v5 {
template <class T>
class BoundedQueue {
 public:
  explicit BoundedQueue(size_t capacity) : capacity_(capacity) {
    require(capacity >= 1, "Queue capacity must be at least 1");
  }

  // Blocks while full. Returns false if the queue was closed (the item is dropped).
  bool push(T value) {
    std::unique_lock<std::mutex> lock(mutex_);
    not_full_.wait(lock, [&] { return closed_ || items_.size() < capacity_; });
    if (closed_) return false;
    items_.push_back(std::move(value));
    high_water_ = std::max(high_water_, items_.size());
    not_empty_.notify_one();
    return true;
  }

  // Blocks while empty. Returns nullopt once closed and drained, or after cancel().
  std::optional<T> pop() {
    std::unique_lock<std::mutex> lock(mutex_);
    not_empty_.wait(lock, [&] { return closed_ || !items_.empty(); });
    if (items_.empty()) return std::nullopt;
    T value = std::move(items_.front());
    items_.pop_front();
    not_full_.notify_one();
    return value;
  }

  void close() {  // producer finished: consumers drain what is left
    std::lock_guard<std::mutex> lock(mutex_);
    closed_ = true;
    not_full_.notify_all();
    not_empty_.notify_all();
  }

  void cancel() {  // failure elsewhere: drop pending items and release everyone
    std::lock_guard<std::mutex> lock(mutex_);
    closed_ = true;
    items_.clear();
    not_full_.notify_all();
    not_empty_.notify_all();
  }

  size_t high_water() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return high_water_;
  }

 private:
  const size_t capacity_;
  mutable std::mutex mutex_;
  std::condition_variable not_full_, not_empty_;
  std::deque<T> items_;
  size_t high_water_ = 0;
  bool closed_ = false;
};

template <class Item>
struct Stage {
  std::string name;
  std::function<void(Item&)> work;
};

struct PipelineStats {
  size_t items = 0;
  size_t capacity = 0;                  // 0 = serial
  std::vector<std::string> names;       // one per stage
  std::vector<uint64_t> busy_ns;        // time each stage spent working
  std::vector<size_t> queue_high_water; // between stage k and k+1; never above capacity
  uint64_t wall_ns = 0;
};

template <class Item>
std::vector<Item> run_stages(std::vector<Item> items, const std::vector<Stage<Item>>& stages, size_t capacity,
                             PipelineStats* stats = nullptr) {
  require(!stages.empty(), "Pipeline needs at least one stage");
  const auto started = Clock::now();
  const size_t count = stages.size();
  std::vector<uint64_t> busy(count, 0);
  std::vector<size_t> high_water(count - 1, 0);
  const auto timed = [&](size_t k, Item& item) {
    const auto begin = Clock::now();
    stages[k].work(item);
    busy[k] += elapsed_ns(begin);
  };

  if (capacity == 0 || count == 1) {
    for (auto& item : items)
      for (size_t k = 0; k < count; k++) timed(k, item);
  } else {
    // queues[k] carries items from stage k to stage k+1.
    std::vector<std::unique_ptr<BoundedQueue<Item>>> queues;
    for (size_t k = 0; k + 1 < count; k++) queues.push_back(std::make_unique<BoundedQueue<Item>>(capacity));
    std::mutex error_mutex;
    std::exception_ptr error;
    const auto fail = [&] {
      {
        std::lock_guard<std::mutex> lock(error_mutex);
        if (!error) error = std::current_exception();
      }
      for (auto& queue : queues) queue->cancel();
    };
    const auto stage_loop = [&](size_t k) {
      try {
        if (k == 0) {
          for (auto& input : items) {
            Item item = std::move(input);
            timed(0, item);
            if (!queues[0]->push(std::move(item))) return;
          }
        } else {
          while (auto item = queues[k - 1]->pop()) {
            timed(k, *item);
            if (!queues[k]->push(std::move(*item))) return;
          }
        }
        queues[k]->close();
      } catch (...) {
        fail();
      }
    };

    // Allocate before any thread exists, so nothing below can throw past a joinable thread.
    std::vector<Item> outputs;
    outputs.reserve(items.size());
    // Stages 0..count-2 get threads; the last stage runs on the caller.
    std::vector<std::thread> threads;
    try {
      for (size_t k = 0; k + 1 < count; k++) threads.emplace_back(stage_loop, k);
    } catch (...) {
      for (auto& queue : queues) queue->cancel();
      for (auto& thread : threads) thread.join();
      throw;
    }
    try {
      while (auto item = queues.back()->pop()) {
        timed(count - 1, *item);
        outputs.push_back(std::move(*item));
      }
    } catch (...) {
      fail();
    }
    for (auto& thread : threads) thread.join();
    if (error) std::rethrow_exception(error);
    for (size_t k = 0; k + 1 < count; k++) high_water[k] = queues[k]->high_water();
    items = std::move(outputs);
  }

  if (stats) {
    stats->items = items.size();
    stats->capacity = capacity;
    stats->names.clear();
    for (const auto& stage : stages) stats->names.push_back(stage.name);
    stats->busy_ns = busy;
    stats->queue_high_water = high_water;
    stats->wall_ns = elapsed_ns(started);
  }
  return items;
}
}  // namespace edgefhe::v5
