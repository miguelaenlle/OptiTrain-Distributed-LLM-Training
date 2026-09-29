#include "kvtransfer/session.h"

#include <algorithm>
#include <limits>

namespace kvtransfer {
size_t block_bytes(const wire::Layout& layout) {
  size_t value = 2;
  for (auto n :
       {layout.kv_heads(), layout.head_dim(), layout.block_tokens(), layout.element_bytes()}) {
    if (n == 0 || value > std::numeric_limits<size_t>::max() / n)
      throw Error("invalid layout size");
    value *= n;
  }
  return value;
}
void validate_layout(const wire::Layout& layout) {
  if (layout.model_revision().empty() || !layout.layers() || layout.layers() > 1024 ||
      layout.ordering() != "block-kv-token-head-dim" ||
      !((layout.dtype() == "float16" || layout.dtype() == "bfloat16") &&
        layout.element_bytes() == 2))
    throw Error("unsupported layout");
  if (block_bytes(layout) > (1ull << 30)) throw Error("block exceeds size limit");
}
bool same_layout(const wire::Layout& a, const wire::Layout& b) {
  return a.model_revision() == b.model_revision() && a.layers() == b.layers() &&
         a.kv_heads() == b.kv_heads() && a.head_dim() == b.head_dim() &&
         a.block_tokens() == b.block_tokens() && a.element_bytes() == b.element_bytes() &&
         a.dtype() == b.dtype() && a.ordering() == b.ordering();
}
Bytes serialize(const google::protobuf::MessageLite& message) {
  const auto s = message.SerializeAsString();
  return Bytes(reinterpret_cast<const std::byte*>(s.data()),
               reinterpret_cast<const std::byte*>(s.data() + s.size()));
}
ReceiveSession::ReceiveSession(std::vector<uint32_t> destination,
                               std::vector<std::shared_ptr<LayerStorage>> layers)
    : destination_(std::move(destination)),
      layers_(std::move(layers)),
      received_(layers_.size()),
      ready_(layers_.size()) {
  if (layers_.empty() || !layers_[0]) throw Error("no destination layers");
  const auto bytes = layers_[0]->block_bytes();
  for (const auto& layer : layers_) {
    if (!layer || layer->block_bytes() != bytes) throw Error("layer layout mismatch");
    validate_mapping(destination_, layer->blocks(), bytes);
  }
  layer_bytes_ = destination_.size() * bytes;
}
void ReceiveSession::chunk(uint32_t layer, uint64_t offset, std::span<const std::byte> bytes) {
  if (state_ == State::Complete || layer >= layers_.size() || ready_[layer] || bytes.empty() ||
      offset != received_[layer] || bytes.size() > layer_bytes_ - received_[layer])
    throw Error("invalid chunk ordering/range");
  layers_[layer]->scatter(destination_, size_t(offset), bytes);
  received_[layer] += bytes.size();
  state_ = State::Streaming;
}
void ReceiveSession::layer_done(uint32_t layer) {
  if (layer >= layers_.size() || ready_[layer] || received_[layer] != layer_bytes_)
    throw Error("premature or duplicate layer completion");
  ready_[layer] = true;
}
void ReceiveSession::request_done() {
  if (state_ == State::Complete ||
      !std::all_of(ready_.begin(), ready_.end(), [](bool x) { return x; }))
    throw Error("premature or duplicate request completion");
  state_ = State::Complete;
}
bool ReceiveSession::ready(uint32_t layer) const {
  if (layer >= ready_.size()) throw Error("invalid layer");
  return ready_[layer];
}
}  // namespace kvtransfer
