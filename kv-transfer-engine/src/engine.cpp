#include "kvtransfer/engine.h"

#include <sys/socket.h>

#include <algorithm>
#include <condition_variable>
#include <deque>
#include <map>
#include <mutex>
#include <optional>
#include <thread>

namespace kvtransfer {
using Clock = std::chrono::steady_clock;
struct TransferEngine::Impl {
  struct Transfer {
    TransferId id;
    bool registered = false, sending = false, announced = false, done = false;
    bool request_done_sent = false;
    size_t remote_blocks = 0, next_layer = 0, offset = 0;
    std::vector<uint32_t> blocks;
    std::vector<std::shared_ptr<LayerStorage>> layers;
    std::vector<bool> submitted, ready;
    std::unique_ptr<ReceiveSession> receive;
    std::string error;
    Clock::time_point deadline;
  };
  Socket socket;
  Config config;
  mutable std::mutex mutex;
  std::condition_variable cv;
  bool stopping = false, handshaken = false, writing = false, copy_exited = false, peer_eof = false;
  std::string failure;
  TransferId last_registered{}, last_announced{}, cursor{};
  std::map<TransferId, std::shared_ptr<Transfer>> transfers;
  std::deque<Frame> outbound, inbound;
  std::thread network, copy;
  Metrics stats;
  Clock::time_point handshake_deadline;

  Impl(Socket s, Config c) : socket(std::move(s)), config(std::move(c)) {
    validate_layout(config.layout);
    if (socket.fd() < 0 || !config.chunk_bytes || config.chunk_bytes > kMaxPayload ||
        !config.queue_frames || config.queue_frames > 1024 || !config.max_transfers ||
        config.max_transfers > 65536 || !config.max_blocks || config.max_blocks > 65536 ||
        config.timeout.count() <= 0)
      throw Error("invalid engine configuration");
    wire::Handshake h;
    *h.mutable_layout() = config.layout;
    outbound.push_back({{Kind::Handshake, {}, 0, 0}, serialize(h)});
    handshake_deadline = Clock::now() + config.timeout;
    network = std::thread([this] { network_loop(); });
    try {
      copy = std::thread([this] { copy_loop(); });
    } catch (...) {
      {
        std::lock_guard lock(mutex);
        stopping = true;
      }
      network.join();
      throw;
    }
  }
  ~Impl() {
    {
      std::lock_guard lock(mutex);
      abort_locked("engine stopped");
    }
    cv.notify_all();
    network.join();
    copy.join();
  }
  void check() const {
    if (stopping) throw Error(failure);
  }
  void abort_locked(std::string reason) {
    if (stopping) return;
    stopping = true;
    failure = std::move(reason);
    shutdown(socket.fd(), SHUT_RDWR);
    outbound.clear();
    inbound.clear();
    for (auto& [id, t] : transfers) {
      if (!t->done) {
        t->error = failure;
        t->done = true;
        ++stats.failed;
      }
    }
    cv.notify_all();
  }
  std::shared_ptr<Transfer> get(TransferId id) {
    auto it = transfers.find(id);
    if (it == transfers.end()) throw Error("unknown transfer");
    return it->second;
  }
  std::shared_ptr<Transfer> create(TransferId id) {
    if (id.incarnation == 0 || id.sequence == 0 || transfers.size() >= config.max_transfers)
      throw Error("invalid transfer ID or admission limit reached");
    auto t = std::make_shared<Transfer>();
    t->id = id;
    t->deadline = Clock::now() + config.timeout;
    t->layers.resize(config.layout.layers());
    t->ready.resize(config.layout.layers());
    t->submitted.resize(config.layout.layers());
    transfers.emplace(id, t);
    return t;
  }
  void register_id(TransferId id) {
    if (id <= last_registered) throw Error("transfer IDs must increase within a connection");
    last_registered = id;
  }
  void enqueue(Frame frame) {
    // Caller holds mutex. A bounded control allowance permits announce/ack
    // progress while the data queue is full; data is never reordered.
    if (outbound.size() >= config.queue_frames + 2 * config.max_transfers + 2)
      throw Error("control queue exhausted");
    outbound.push_back(std::move(frame));
    stats.outbound_high_water = std::max(stats.outbound_high_water, outbound.size());
    cv.notify_all();
  }
  void network_loop() noexcept {
    try {
      Readiness readiness(socket.fd());
      Parser parser;
      Bytes current;
      size_t position = 0;
      while (true) {
        bool can_read;
        {
          std::lock_guard lock(mutex);
          if (stopping) return;
          can_read = inbound.size() < config.queue_frames;
          if (current.empty() && !outbound.empty()) {
            current = encode(outbound.front());
            outbound.pop_front();
            position = 0;
            writing = true;
            cv.notify_all();
          }
        }
        bool progress = false;
        if (!current.empty()) {
          auto n = socket.write(std::span(current).subspan(position));
          position += n;
          progress = n != 0;
          {
            std::lock_guard lock(mutex);
            stats.sent_bytes += n;
          }
          if (position == current.size()) {
            current.clear();
            std::lock_guard lock(mutex);
            writing = false;
            cv.notify_all();
          }
        }
        if (can_read) {
          // Read at most the parser's remaining frame bytes, so emitting cannot
          // overflow the bounded inbound queue even for tiny control frames.
          Bytes input(std::min<size_t>(parser.needed(), 64 * 1024));
          auto n = socket.read(input);
          if (n) {
            progress = true;
            parser.feed(std::span(input).first(n), [this](Frame f) {
              std::lock_guard lock(mutex);
              inbound.push_back(std::move(f));
              stats.inbound_high_water = std::max(stats.inbound_high_water, inbound.size());
              cv.notify_all();
            });
            std::lock_guard lock(mutex);
            stats.received_bytes += n;
          }
        }
        if (!progress) readiness.wait(can_read, !current.empty(), 2);
      }
    } catch (const EndOfStream&) {
      // An Ack may already be queued when the peer closes. Let staging consume
      // all complete received frames before classifying outstanding requests.
      std::lock_guard lock(mutex);
      peer_eof = true;
      cv.notify_all();
    } catch (const std::exception& e) {
      std::lock_guard lock(mutex);
      abort_locked(e.what());
    }
  }
  void handle(Frame f, std::unique_lock<std::mutex>& lock) {
    const auto& h = f.header;
    if (h.kind != Kind::Chunk && (h.offset != 0 || (h.kind != Kind::LayerDone && h.layer != 0)))
      throw Error("invalid control frame fields");
    if (h.kind == Kind::Handshake) {
      wire::Handshake peer;
      if (handshaken || h.id != TransferId{} ||
          !peer.ParseFromArray(f.payload.data(), int(f.payload.size())) ||
          !same_layout(config.layout, peer.layout()))
        throw Error("handshake mismatch");
      handshaken = true;
      return;
    }
    if (!handshaken) throw Error("frame before handshake");
    if (h.kind == Kind::Announce) {
      wire::Announce announce;
      if (h.id <= last_announced ||
          !announce.ParseFromArray(f.payload.data(), int(f.payload.size())) ||
          !announce.destination_blocks_size() ||
          size_t(announce.destination_blocks_size()) > config.max_blocks)
        throw Error("invalid announcement");
      std::vector<uint32_t> blocks(announce.destination_blocks().begin(),
                                   announce.destination_blocks().end());
      validate_mapping(blocks, uint64_t(UINT32_MAX) + 1, block_bytes(config.layout));
      auto t = transfers.contains(h.id) ? get(h.id) : create(h.id);
      if (t->announced || (t->registered && !t->sending))
        throw Error("duplicate/conflicting announcement");
      if (t->registered && t->blocks.size() != blocks.size()) throw Error("block count mismatch");
      t->remote_blocks = blocks.size();
      t->announced = true;
      last_announced = h.id;
      return;
    }
    auto t = get(h.id);
    if (t->done || !t->registered) throw Error("frame for inactive transfer");
    if (h.kind == Kind::Failure) throw Error("peer reported transfer failure");
    if (h.kind == Kind::Ack) {
      if (!f.payload.empty() || !t->sending || !t->request_done_sent)
        throw Error("unexpected acknowledgement");
      t->done = true;
      t->layers.clear();
      ++stats.completed;
      cv.notify_all();
      return;
    }
    if (t->sending || !t->receive) throw Error("unexpected receive frame");
    if (h.kind == Kind::Chunk) {
      // Keep the session alive while scatter waits for a device event, without
      // holding the API mutex. Only this staging thread mutates ReceiveSession.
      lock.unlock();
      try {
        t->receive->chunk(h.layer, h.offset, f.payload);
      } catch (...) {
        lock.lock();
        throw;
      }
      lock.lock();
    } else if (h.kind == Kind::LayerDone) {
      if (!f.payload.empty()) throw Error("unexpected layer-done payload");
      t->receive->layer_done(h.layer);
      t->ready[h.layer] = true;
      cv.notify_all();
    } else if (h.kind == Kind::RequestDone) {
      if (!f.payload.empty()) throw Error("unexpected request-done payload");
      t->receive->request_done();
      enqueue({{Kind::Ack, t->id, 0, 0}, {}});
      t->done = true;
      ++stats.completed;
      cv.notify_all();
    } else
      throw Error("unexpected frame kind");
  }
  bool stage_one(std::unique_lock<std::mutex>& lock) {
    if (!handshaken || outbound.size() >= config.queue_frames || transfers.empty()) return false;
    auto it = transfers.upper_bound(cursor);
    for (size_t n = 0; n < transfers.size(); ++n) {
      if (it == transfers.end()) it = transfers.begin();
      auto t = (it++)->second;
      if (t->done || !t->registered || !t->sending || !t->announced || t->request_done_sent)
        continue;
      cursor = t->id;
      if (t->next_layer == t->layers.size()) {
        enqueue({{Kind::RequestDone, t->id, 0, 0}, {}});
        t->request_done_sent = true;
        return true;
      }
      auto source = t->layers[t->next_layer];
      if (!source) continue;
      const auto bytes = t->blocks.size() * source->block_bytes();
      if (t->offset == bytes) {
        enqueue({{Kind::LayerDone, t->id, uint32_t(t->next_layer), 0}, {}});
        t->layers[t->next_layer].reset();
        ++t->next_layer;
        t->offset = 0;
        cv.notify_all();
        return true;
      }
      Bytes payload(std::min(config.chunk_bytes, bytes - t->offset));
      lock.unlock();
      try {
        source->gather(t->blocks, t->offset, payload);
      } catch (...) {
        lock.lock();
        throw;
      }
      lock.lock();
      if (stopping) return false;
      enqueue({{Kind::Chunk, t->id, uint32_t(t->next_layer), t->offset}, std::move(payload)});
      t->offset += std::min(config.chunk_bytes, bytes - t->offset);
      return true;
    }
    return false;
  }
  void copy_loop() noexcept {
    std::unique_lock lock(mutex);
    try {
      while (!stopping) {
        auto now = Clock::now();
        if (!handshaken && now >= handshake_deadline) throw Error("handshake timeout");
        for (const auto& [id, t] : transfers)
          if (!t->done && now >= t->deadline) throw Error("transfer deadline expired");
        bool progress = false;
        if (!inbound.empty()) {
          Frame f = std::move(inbound.front());
          inbound.pop_front();
          handle(std::move(f), lock);
          progress = true;
        }
        if (peer_eof && inbound.empty()) {
          abort_locked("peer disconnected");
          break;
        }
        progress = stage_one(lock) || progress;
        if (!progress) cv.wait_for(lock, std::chrono::milliseconds(2));
      }
    } catch (const std::exception& e) {
      abort_locked(e.what());
    }
    copy_exited = true;
    cv.notify_all();
  }
};
TransferEngine::TransferEngine(Socket s, Config c)
    : impl_(std::make_unique<Impl>(std::move(s), std::move(c))) {}
TransferEngine::~TransferEngine() = default;
void TransferEngine::begin_send(TransferId id, std::vector<uint32_t> blocks) {
  auto& p = *impl_;
  std::lock_guard lock(p.mutex);
  p.check();
  if (blocks.size() > p.config.max_blocks) throw Error("too many blocks");
  validate_mapping(blocks, uint64_t(UINT32_MAX) + 1, block_bytes(p.config.layout));
  if (id <= p.last_registered) throw Error("transfer IDs must increase within a connection");
  auto t = p.transfers.contains(id) ? p.get(id) : p.create(id);
  if (t->registered || (t->announced && t->remote_blocks != blocks.size()))
    throw Error("invalid send registration");
  p.register_id(id);
  t->registered = true;
  t->sending = true;
  t->blocks = std::move(blocks);
  p.cv.notify_all();
}
void TransferEngine::begin_recv(TransferId id, std::vector<uint32_t> blocks,
                                std::vector<std::shared_ptr<LayerStorage>> layers) {
  auto& p = *impl_;
  std::lock_guard lock(p.mutex);
  p.check();
  if (blocks.size() > p.config.max_blocks || layers.size() != p.config.layout.layers())
    throw Error("receive shape mismatch");
  for (const auto& layer : layers)
    if (!layer || layer->block_bytes() != block_bytes(p.config.layout))
      throw Error("receive layout mismatch");
  auto receive = std::make_unique<ReceiveSession>(blocks, std::move(layers));
  if (p.transfers.contains(id)) throw Error("duplicate transfer ID");
  if (id <= p.last_registered) throw Error("transfer IDs must increase within a connection");
  auto t = p.create(id);
  p.register_id(id);
  t->registered = true;
  t->blocks = blocks;
  t->receive = std::move(receive);
  wire::Announce a;
  for (auto block : blocks) a.add_destination_blocks(block);
  p.enqueue({{Kind::Announce, id, 0, 0}, serialize(a)});
}
void TransferEngine::send_layer(TransferId id, uint32_t layer,
                                std::shared_ptr<LayerStorage> source) {
  auto& p = *impl_;
  std::lock_guard lock(p.mutex);
  p.check();
  auto t = p.get(id);
  if (!t->registered || !t->sending || t->done || layer >= t->layers.size() ||
      t->submitted[layer] || !source || source->block_bytes() != block_bytes(p.config.layout))
    throw Error("invalid send layer");
  validate_mapping(t->blocks, source->blocks(), source->block_bytes());
  t->layers[layer] = std::move(source);
  t->submitted[layer] = true;
  p.cv.notify_all();
}
void TransferEngine::wait_layer(TransferId id, uint32_t layer, std::chrono::milliseconds timeout) {
  auto& p = *impl_;
  std::unique_lock lock(p.mutex);
  auto t = p.get(id);
  if (t->sending || layer >= t->ready.size()) throw Error("invalid layer wait");
  if (!p.cv.wait_for(lock, timeout, [&] { return p.stopping || t->ready[layer] || t->done; })) {
    p.abort_locked("layer wait timeout");
  }
  if (p.stopping) p.cv.wait(lock, [&] { return p.copy_exited; });
  if (!t->error.empty()) throw Error(t->error);
  if (!t->ready[layer]) throw Error(p.failure);
}
void TransferEngine::wait_staged(TransferId id, std::chrono::milliseconds timeout) {
  auto& p = *impl_;
  std::unique_lock lock(p.mutex);
  auto t = p.get(id);
  if (!t->sending) throw Error("invalid staging wait");
  if (!p.cv.wait_for(lock, timeout,
                     [&] { return p.stopping || t->next_layer == p.config.layout.layers(); })) {
    p.abort_locked("staging wait timeout");
  }
  if (p.stopping) p.cv.wait(lock, [&] { return p.copy_exited; });
  if (!t->error.empty()) throw Error(t->error);
}
void TransferEngine::wait_all(std::chrono::milliseconds timeout) {
  auto& p = *impl_;
  std::unique_lock lock(p.mutex);
  if (!p.cv.wait_for(lock, timeout, [&] {
        return p.stopping || (p.handshaken && p.outbound.empty() && !p.writing &&
                              std::all_of(p.transfers.begin(), p.transfers.end(),
                                          [](const auto& item) { return item.second->done; }));
      })) {
    p.abort_locked("wait-all timeout");
  }
  if (p.stopping) p.cv.wait(lock, [&] { return p.copy_exited; });
  if (p.stopping &&
      (!p.handshaken || std::any_of(p.transfers.begin(), p.transfers.end(), [](const auto& item) {
        return !item.second->done || !item.second->error.empty();
      })))
    p.check();
}
std::vector<Completion> TransferEngine::poll_finished() {
  auto& p = *impl_;
  std::lock_guard lock(p.mutex);
  std::vector<Completion> result;
  for (auto it = p.transfers.begin(); it != p.transfers.end();) {
    auto t = it->second;
    if (t->done && t->registered && (!p.stopping || p.copy_exited)) {
      result.push_back({t->id, t->sending, t->error.empty(), t->error});
      it = p.transfers.erase(it);
    } else
      ++it;
  }
  return result;
}
void TransferEngine::cancel(TransferId id) {
  auto& p = *impl_;
  std::unique_lock lock(p.mutex);
  p.get(id);
  p.abort_locked("transfer cancelled");
  p.cv.wait(lock, [&] { return p.copy_exited; });
}
Metrics TransferEngine::metrics() const {
  auto& p = *impl_;
  std::lock_guard lock(p.mutex);
  auto result = p.stats;
  result.active = p.transfers.size();
  return result;
}
}  // namespace kvtransfer
