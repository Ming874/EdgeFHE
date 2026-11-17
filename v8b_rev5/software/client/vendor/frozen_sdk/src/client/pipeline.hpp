// Bounded multi-stage encryption pipeline for the V7 client (PS/PL parallelism).
//
//   Stage A  (P "prepare" threads): CKKS encode of 4 features, OS-entropy sampling of u/e0/e1,
//                                   signed-integer -> RNS residues.            [CPU cores in parallel]
//   Stage B  (the CALLING thread) : PAIR4 on the accelerator + PS fusion c0 = bu+e0+m, c1 = au+e1.
//                                   It must be the thread that opened the device: UnifiedDevice enforces a
//                                   single DMA owner thread (2026-10-14 board run N7GHU1 failed when stage B
//                                   ran on a worker thread; refused before any ioctl). While it sleeps in the
//                                   ioctl, stages A/C keep the other cores busy.
//   Stage C  (1 delivery thread)  : in-order delivery to a sink (serialization / TLS send).
//
// Options::fast_path (added 2026-10-14, plan steps 09-11; default off = the board-accepted path above):
//   Stage A keeps the encoded message and the signed u/e0/e1 (no RNS copies) and samples with the lock-free
//   sample_secure_encryption_concurrent, so P preparers no longer serialize on one sampler mutex;
//   Stage B packs u straight into the PAIR4 frame and fuses c0/c1 from the signed noise
//   (encrypt_secure_paired_fast: same validation, canonical and padding checks; one PAIR4 per job).
// Options::direct_sampler (added 2026-10-15, E02 candidate A2; default off): the fast path samples with
//   sample_secure_encryption_fast<DirectKernels> (same algorithms and draw order, bit-identical on an identical
//   word stream per sampler_profile_selftest det) instead of sample_secure_encryption_concurrent.
//
// Queues are bounded (default 2 = "two slots"), so at most two prepared records and two finished
// records are buffered: memory stays constant and back-pressure reaches the producer.
// Any exception stops every stage, joins every thread and is rethrown once; nothing is retried.
// The device session is touched only by the calling thread (single DMA owner contract).
#pragma once
#include "encrypted_session.hpp"
#include "baseline/secure.hpp"
#include "baseline/fast_sampler.hpp"
#include "fast_encrypt.hpp"
#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <deque>
#include <exception>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <thread>
#include <vector>

namespace edgefhe_v7 {

template<class T> class BoundedQueue {
  std::mutex m_; std::condition_variable not_full_, not_empty_;
  std::deque<T> q_; size_t cap_; bool closed_ = false;
 public:
  explicit BoundedQueue(size_t cap) : cap_(cap) { if (!cap) throw std::invalid_argument("queue capacity"); }
  bool push(T v) {  // false when closed
    std::unique_lock<std::mutex> g(m_);
    not_full_.wait(g, [&] { return closed_ || q_.size() < cap_; });
    if (closed_) return false;
    q_.push_back(std::move(v)); not_empty_.notify_one(); return true;
  }
  std::optional<T> pop() {  // nullopt when closed and drained
    std::unique_lock<std::mutex> g(m_);
    not_empty_.wait(g, [&] { return closed_ || !q_.empty(); });
    if (q_.empty()) return std::nullopt;
    T v = std::move(q_.front()); q_.pop_front(); not_full_.notify_one(); return v;
  }
  void close() { std::lock_guard<std::mutex> g(m_); closed_ = true; not_full_.notify_all(); not_empty_.notify_all(); }
};

struct PipelineJob { uint64_t id; std::array<std::vector<std::complex<double>>, 4> slots; };

struct PreparedRecord {
  uint64_t id = 0;
  std::array<Poly, 4> message, mask, e0, e1;   // canonical RNS residues (accepted path)
  std::array<edgefhe::Message, 4> message_int;  // fast path: encoded message, signed
  std::unique_ptr<edgefhe::SecureMaterial> material;  // fast path: signed u/e0/e1, wiped by ~SecureMaterial
  ~PreparedRecord() {                          // the residues are secret-derived: wipe on destruction
    for (auto *a : {&message, &mask, &e0, &e1}) for (auto& p : *a) for (auto& l : p) {
      volatile uint32_t *v = l.data(); for (size_t i = 0; i < l.size(); i++) v[i] = 0;
    }
    for (auto& m : message_int) { volatile int64_t *v = m.data(); for (size_t i = 0; i < m.size(); i++) v[i] = 0; }
  }
  PreparedRecord() = default;
  PreparedRecord(PreparedRecord&&) = default;
  PreparedRecord& operator=(PreparedRecord&&) = default;
};

struct EncryptedRecord {
  uint64_t id = 0;
  std::array<Ciphertext, 4> cipher;
  std::shared_ptr<const PreparedRecord> inputs;  // test-only: populated when keep_inputs_for_test
};

struct PipelineStats { uint64_t prepared = 0, encrypted = 0, delivered = 0; size_t max_reorder = 0; };

// Admission window: a job may enter only while fewer than `limit` records are between the feeder and the
// sink. Without it a slow preparer holding record k lets the reorder map grow without bound (one record of
// four ciphertexts is ~384 KiB), so memory would depend on scheduling instead of on the configuration.
class InflightWindow {
  std::mutex m_; std::condition_variable cv_; size_t used_ = 0, limit_; bool closed_ = false;
 public:
  explicit InflightWindow(size_t limit) : limit_(limit) {}
  bool acquire() {
    std::unique_lock<std::mutex> g(m_);
    cv_.wait(g, [&] { return closed_ || used_ < limit_; });
    if (closed_) return false;
    used_++; return true;
  }
  void release() { std::lock_guard<std::mutex> g(m_); used_--; cv_.notify_one(); }
  void close() { std::lock_guard<std::mutex> g(m_); closed_ = true; cv_.notify_all(); }
};

class EncryptPipeline {
 public:
  // Records in flight are bounded by inflight_limit() = 2*slots + prepare_threads + 1 (two queues, one record
  // per preparer, one in the device), so the reorder map never holds more than that many records.
  struct Options { unsigned prepare_threads = 2; size_t slots = 2; bool keep_inputs_for_test = false; bool fast_path = false;
                   bool direct_sampler = false; };
  using Source = std::function<std::optional<PipelineJob>()>;   // called on one feeder thread only
  using Sink = std::function<void(EncryptedRecord&&)>;          // called on the delivery thread, in id order

  EncryptPipeline(PairedSession& session, Options options) : session_(session), opt_(options) {
    if (opt_.prepare_threads < 1 || opt_.prepare_threads > 3) throw std::invalid_argument("prepare threads 1..3");
    if (opt_.slots < 1 || opt_.slots > 8) throw std::invalid_argument("slots 1..8");
    if (opt_.direct_sampler && !opt_.fast_path) throw std::invalid_argument("direct_sampler needs fast_path");
  }
  size_t inflight_limit() const { return 2 * opt_.slots + opt_.prepare_threads + 1; }

  // Not reentrant: one run() at a time per pipeline object. Job ids must be 0,1,2,... in source order.
  PipelineStats run(const Source& source, const Sink& sink) {
    { std::lock_guard<std::mutex> g(error_mutex_); error_ = nullptr; }  // a previous failed run must not leak
    BoundedQueue<PipelineJob> jobs(opt_.slots);
    BoundedQueue<std::shared_ptr<PreparedRecord>> prepared(opt_.slots);
    BoundedQueue<EncryptedRecord> done(opt_.slots);
    InflightWindow window(inflight_limit());
    std::atomic<unsigned> preparers_left{opt_.prepare_threads};
    auto fail = [&](std::exception_ptr e) {
      { std::lock_guard<std::mutex> g(error_mutex_); if (!error_) error_ = e; }
      window.close(); jobs.close(); prepared.close(); done.close();
    };
    std::vector<std::thread> workers;
    std::thread feeder;
    PipelineStats stats;
    std::atomic<uint64_t> prepared_count{0}, encrypted_count{0};
    uint64_t delivered = 0; size_t max_reorder = 0;   // written by stage C only, read after join
    auto failed = [&] { std::lock_guard<std::mutex> g(error_mutex_); return bool(error_); };
    try {
      for (unsigned t = 0; t < opt_.prepare_threads; t++) workers.emplace_back([&] {
        try {
          edgefhe::SecureMaterial material;  // per-thread scratch; refreshed by the OS sampler
          while (auto job = jobs.pop()) {
            auto record = std::make_shared<PreparedRecord>();
            record->id = job->id;
            if (opt_.fast_path) {
              for (size_t f = 0; f < 4; f++) record->message_int[f] = edgefhe::encode(job->slots[f]);
              record->material = std::make_unique<edgefhe::SecureMaterial>();
              if (opt_.direct_sampler) edgefhe::sample_secure_encryption_fast<edgefhe::DirectKernels>(*record->material);
              else edgefhe::sample_secure_encryption_concurrent(*record->material);
              prepared_count++;
              if (!prepared.push(std::move(record))) return;
              continue;
            }
            for (size_t f = 0; f < 4; f++) {
              auto msg = edgefhe::encode(job->slots[f]);
              record->message[f] = edgefhe::residues(msg);
            }
            edgefhe::sample_secure_encryption(material);
            for (size_t f = 0; f < 4; f++) {
              record->mask[f] = edgefhe::residues(material.mask[f]);
              record->e0[f] = edgefhe::residues(material.e0[f]);
              record->e1[f] = edgefhe::residues(material.e1[f]);
            }
            prepared_count++;
            if (!prepared.push(std::move(record))) return;
          }
        } catch (...) { fail(std::current_exception()); }
        if (--preparers_left == 0) prepared.close();
      });
      // Stage C on its own thread: in-order delivery; releases the admission window per delivered record.
      workers.emplace_back([&] {
        try {
          std::map<uint64_t, EncryptedRecord> reorder;
          uint64_t next = 0;
          while (auto r = done.pop()) {
            if (r->id < next || !reorder.emplace(r->id, std::move(*r)).second)
              throw std::logic_error("pipeline duplicate record id");
            max_reorder = std::max(max_reorder, reorder.size());
            for (auto it = reorder.find(next); it != reorder.end(); it = reorder.find(next)) {
              sink(std::move(it->second)); reorder.erase(it); next++; delivered++; window.release();
            }
          }
          if (!reorder.empty() && !failed()) throw std::logic_error("pipeline ended with undeliverable records");
        } catch (...) { fail(std::current_exception()); }
      });
      feeder = std::thread([&] {
        try {
          for (uint64_t expected = 0; window.acquire(); expected++) {
            auto job = source();
            if (!job) { window.release(); break; }
            if (job->id != expected) throw std::invalid_argument("pipeline job ids must be consecutive from 0");
            if (!jobs.push(std::move(*job))) break;
          }
        } catch (...) { fail(std::current_exception()); }
        jobs.close();
      });
      // Stage B on the calling thread (the device owner).
      try {
        while (auto record = prepared.pop()) {
          EncryptedRecord out;
          out.id = (*record)->id;
          if (opt_.fast_path) {
            auto c = encrypt_secure_paired_fast(session_, (*record)->message_int, *(*record)->material);
            for (size_t f = 0; f < 4; f++) { out.cipher[f].c0 = std::move(c[f].c0); out.cipher[f].c1 = std::move(c[f].c1); }
          } else {
            out.cipher = encrypt_four(session_, (*record)->mask, (*record)->message, (*record)->e0, (*record)->e1);
          }
          if (opt_.keep_inputs_for_test) out.inputs = *record;
          encrypted_count++;
          if (!done.push(std::move(out))) break;
        }
      } catch (...) { fail(std::current_exception()); }
      done.close();
    } catch (...) { fail(std::current_exception()); }
    if (feeder.joinable()) feeder.join();   // every started thread is joined on every path
    for (auto& w : workers) if (w.joinable()) w.join();
    stats.prepared = prepared_count; stats.encrypted = encrypted_count; stats.delivered = delivered; stats.max_reorder = max_reorder;
    if (error_) std::rethrow_exception(error_);
    return stats;
  }

 private:
  PairedSession& session_;
  Options opt_;
  std::mutex error_mutex_;
  std::exception_ptr error_;
};
}  // namespace edgefhe_v7
