#pragma once
#include <string>

#include "control.pb.h"
#include "kvtransfer/staging.h"

namespace kvtransfer {
size_t block_bytes(const wire::Layout& layout);
void validate_layout(const wire::Layout& layout);
bool same_layout(const wire::Layout&, const wire::Layout&);
Bytes serialize(const google::protobuf::MessageLite& message);
enum class State { Announced, Streaming, Complete, Failed };
class ReceiveSession {
 public:
  ReceiveSession(std::vector<uint32_t> destination,
                 std::vector<std::shared_ptr<LayerStorage>> layers);
  void chunk(uint32_t layer, uint64_t offset, std::span<const std::byte> bytes);
  void layer_done(uint32_t layer);
  void request_done();
  bool ready(uint32_t layer) const;
  State state() const { return state_; }

 private:
  std::vector<uint32_t> destination_;
  std::vector<std::shared_ptr<LayerStorage>> layers_;
  std::vector<size_t> received_;
  std::vector<bool> ready_;
  State state_ = State::Announced;
  size_t layer_bytes_ = 0;
};
}  // namespace kvtransfer
