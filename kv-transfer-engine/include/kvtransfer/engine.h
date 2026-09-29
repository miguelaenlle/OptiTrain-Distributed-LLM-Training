#pragma once
#include <chrono>
#include <memory>

#include "kvtransfer/session.h"
#include "kvtransfer/transport.h"

namespace kvtransfer {
struct Config {
  wire::Layout layout;
  size_t chunk_bytes = 256 * 1024;
  size_t queue_frames = 8;
  size_t max_transfers = 128;
  size_t max_blocks = 65536;
  std::chrono::milliseconds timeout{30000};
};
struct Completion {
  TransferId id;
  bool sending;
  bool success;
  std::string error;
};
struct Metrics {
  uint64_t sent_bytes = 0, received_bytes = 0, completed = 0, failed = 0;
  size_t active = 0, outbound_high_water = 0, inbound_high_water = 0;
};
// A single peer connection, multiplexing many requests. One network thread and
// one staging thread. Invalid frames, cancellation, and deadlines abort the
// connection and fail every unfinished transfer, rather than attempt resumption.
class TransferEngine {
 public:
  TransferEngine(Socket socket, Config config);
  ~TransferEngine();
  TransferEngine(const TransferEngine&) = delete;
  TransferEngine& operator=(const TransferEngine&) = delete;
  void begin_send(TransferId, std::vector<uint32_t> source_blocks);
  void begin_recv(TransferId, std::vector<uint32_t> destination_blocks,
                  std::vector<std::shared_ptr<LayerStorage>> layers);
  void send_layer(TransferId, uint32_t layer, std::shared_ptr<LayerStorage> source);
  void wait_layer(TransferId, uint32_t layer, std::chrono::milliseconds timeout);
  void wait_staged(TransferId, std::chrono::milliseconds timeout);
  void wait_all(std::chrono::milliseconds timeout);
  std::vector<Completion> poll_finished();
  void cancel(TransferId);
  Metrics metrics() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
}  // namespace kvtransfer
