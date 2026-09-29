#include "kvtransfer/protocol.h"

#include <algorithm>

namespace kvtransfer {
namespace {
void put(std::span<std::byte> out, size_t at, uint64_t value, size_t n) {
  for (size_t i = 0; i < n; ++i) out[at + i] = std::byte(value >> ((n - i - 1) * 8));
}
uint64_t get(std::span<const std::byte> in, size_t at, size_t n) {
  uint64_t value = 0;
  for (size_t i = 0; i < n; ++i) value = (value << 8) | std::to_integer<uint8_t>(in[at + i]);
  return value;
}
void validate_header(std::span<const std::byte> h) {
  if (get(h, 0, 4) != 0x4b565431 || get(h, 4, 2) != 1) throw Error("bad protocol magic/version");
  auto kind = get(h, 6, 2);
  if (kind < 1 || kind > 7) throw Error("unknown frame kind");
  if (get(h, 8, 4) > kMaxPayload) throw Error("frame too large");
  if (get(h, 44, 4) != crc32(h.first(44))) throw Error("header checksum mismatch");
}
}  // namespace
uint32_t crc32(std::span<const std::byte> bytes) {
  uint32_t crc = 0xffffffff;
  for (auto byte : bytes) {
    crc ^= std::to_integer<uint8_t>(byte);
    for (int bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
  }
  return ~crc;
}
std::array<std::byte, kHeaderBytes> encode_header(const Frame& f) {
  if (f.payload.size() > kMaxPayload) throw Error("frame too large");
  std::array<std::byte, kHeaderBytes> h{};
  put(h, 0, 0x4b565431, 4);
  put(h, 4, 1, 2);
  put(h, 6, uint16_t(f.header.kind), 2);
  put(h, 8, f.payload.size(), 4);
  put(h, 12, f.header.id.incarnation, 8);
  put(h, 20, f.header.id.sequence, 8);
  put(h, 28, f.header.layer, 4);
  put(h, 32, f.header.offset, 8);
  put(h, 40, crc32(f.payload), 4);
  put(h, 44, crc32(std::span(h).first(44)), 4);
  return h;
}
Bytes encode(const Frame& f) {
  auto h = encode_header(f);
  Bytes out(h.begin(), h.end());
  out.insert(out.end(), f.payload.begin(), f.payload.end());
  return out;
}
void Parser::feed(std::span<const std::byte> input, const std::function<void(Frame)>& emit) {
  while (!input.empty() || pending_.size() == expected_) {
    const auto n = std::min(input.size(), expected_ - pending_.size());
    pending_.insert(pending_.end(), input.begin(), input.begin() + n);
    input = input.subspan(n);
    if (pending_.size() != expected_) break;
    if (expected_ == kHeaderBytes) {
      validate_header(pending_);
      expected_ += get(pending_, 8, 4);
      if (pending_.size() != expected_) continue;
    }
    Frame f{{Kind(get(pending_, 6, 2)),
             {get(pending_, 12, 8), get(pending_, 20, 8)},
             uint32_t(get(pending_, 28, 4)),
             get(pending_, 32, 8)},
            Bytes(pending_.begin() + kHeaderBytes, pending_.end())};
    if (crc32(f.payload) != get(pending_, 40, 4)) throw Error("payload checksum mismatch");
    pending_.clear();
    expected_ = kHeaderBytes;
    emit(std::move(f));
  }
}
}  // namespace kvtransfer
