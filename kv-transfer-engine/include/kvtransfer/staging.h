#pragma once
#include <memory>
#include <mutex>

#include "kvtransfer/protocol.h"

namespace kvtransfer {
// One logical block consists of K bytes followed by V bytes. Physical device
// storage may separate the two planes; implementations normalize on gather.
class LayerStorage {
 public:
  virtual ~LayerStorage() = default;
  virtual size_t blocks() const = 0;
  virtual size_t block_bytes() const = 0;
  virtual void gather(std::span<const uint32_t> ids, size_t offset, std::span<std::byte> out) = 0;
  virtual void scatter(std::span<const uint32_t> ids, size_t offset,
                       std::span<const std::byte> in) = 0;
};
void validate_mapping(std::span<const uint32_t> ids, size_t capacity, size_t block_bytes);
class HostLayer final : public LayerStorage {
 public:
  HostLayer(size_t blocks, size_t block_bytes);
  size_t blocks() const override { return blocks_; }
  size_t block_bytes() const override { return block_bytes_; }
  void gather(std::span<const uint32_t>, size_t, std::span<std::byte>) override;
  void scatter(std::span<const uint32_t>, size_t, std::span<const std::byte>) override;
  Bytes snapshot() const;
  void assign(std::span<const std::byte> bytes);

 private:
  size_t blocks_, block_bytes_;
  Bytes bytes_;
  mutable std::mutex mutex_;
};
}  // namespace kvtransfer
