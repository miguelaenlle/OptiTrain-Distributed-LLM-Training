#include "kvtransfer/staging.h"

#include <algorithm>
#include <limits>
#include <set>

namespace kvtransfer {
void validate_mapping(std::span<const uint32_t> ids, size_t capacity, size_t bytes) {
  if (ids.empty() || bytes == 0 || ids.size() > std::numeric_limits<size_t>::max() / bytes)
    throw Error("invalid block count/size");
  std::set<uint32_t> seen;
  for (auto id : ids) {
    if (id >= capacity || !seen.insert(id).second) throw Error("invalid or duplicate block ID");
  }
}
HostLayer::HostLayer(size_t blocks, size_t bytes) : blocks_(blocks), block_bytes_(bytes) {
  if (!blocks || !bytes || blocks > std::numeric_limits<size_t>::max() / bytes)
    throw Error("invalid storage size");
  bytes_.resize(blocks * bytes);
}
namespace {
template <class Op>
void segments(std::span<const uint32_t> ids, size_t block_bytes, size_t offset, size_t n, Op op) {
  if (offset > ids.size() * block_bytes || n > ids.size() * block_bytes - offset)
    throw Error("copy outside mapped blocks");
  size_t done = 0;
  while (done < n) {
    const auto logical = (offset + done) / block_bytes;
    const auto within = (offset + done) % block_bytes;
    const auto count = std::min(n - done, block_bytes - within);
    op(size_t(ids[logical]) * block_bytes + within, done, count);
    done += count;
  }
}
}  // namespace
void HostLayer::gather(std::span<const uint32_t> ids, size_t offset, std::span<std::byte> out) {
  validate_mapping(ids, blocks_, block_bytes_);
  std::lock_guard lock(mutex_);
  segments(ids, block_bytes_, offset, out.size(), [&](size_t physical, size_t done, size_t n) {
    std::copy_n(bytes_.begin() + physical, n, out.begin() + done);
  });
}
void HostLayer::scatter(std::span<const uint32_t> ids, size_t offset,
                        std::span<const std::byte> in) {
  validate_mapping(ids, blocks_, block_bytes_);
  std::lock_guard lock(mutex_);
  segments(ids, block_bytes_, offset, in.size(), [&](size_t physical, size_t done, size_t n) {
    std::copy_n(in.begin() + done, n, bytes_.begin() + physical);
  });
}
Bytes HostLayer::snapshot() const {
  std::lock_guard lock(mutex_);
  return bytes_;
}
void HostLayer::assign(std::span<const std::byte> in) {
  std::lock_guard lock(mutex_);
  if (in.size() != bytes_.size()) throw Error("storage size mismatch");
  std::copy(in.begin(), in.end(), bytes_.begin());
}
}  // namespace kvtransfer
