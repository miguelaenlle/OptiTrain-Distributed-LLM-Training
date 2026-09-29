#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <span>
#include <stdexcept>
#include <vector>

namespace kvtransfer {
using Bytes = std::vector<std::byte>;
struct Error : std::runtime_error {
  using std::runtime_error::runtime_error;
};
struct TransferId {
  uint64_t incarnation = 0;
  uint64_t sequence = 0;
  auto operator<=>(const TransferId&) const = default;
};
enum class Kind : uint16_t { Handshake = 1, Announce, Chunk, LayerDone, RequestDone, Ack, Failure };
struct Header {
  Kind kind = Kind::Handshake;
  TransferId id;
  uint32_t layer = 0;
  uint64_t offset = 0;
};
struct Frame {
  Header header;
  Bytes payload;
};
inline constexpr size_t kHeaderBytes = 48;
inline constexpr size_t kMaxPayload = 1 << 20;
uint32_t crc32(std::span<const std::byte> bytes);
std::array<std::byte, kHeaderBytes> encode_header(const Frame& frame);
Bytes encode(const Frame& frame);
// Incremental parser; never buffers more than one bounded frame.
class Parser {
 public:
  void feed(std::span<const std::byte> input, const std::function<void(Frame)>& emit);
  bool empty() const { return pending_.empty(); }
  size_t needed() const { return expected_ - pending_.size(); }

 private:
  Bytes pending_;
  size_t expected_ = kHeaderBytes;
};
}  // namespace kvtransfer
