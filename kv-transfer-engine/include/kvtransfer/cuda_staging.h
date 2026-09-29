#pragma once
#include <cuda_runtime_api.h>

#include "kvtransfer/staging.h"

namespace kvtransfer {
// Shared staging context: one bounded pinned buffer and stream. The engine's
// staging thread performs copies while its network thread transmits prior data.
class CudaStaging {
 public:
  explicit CudaStaging(int device, size_t capacity = kMaxPayload);
  ~CudaStaging();
  CudaStaging(const CudaStaging&) = delete;
  CudaStaging& operator=(const CudaStaging&) = delete;

 private:
  friend class CudaLayer;
  int device_;
  size_t capacity_;
  std::byte* pinned_ = nullptr;
  cudaStream_t stream_ = nullptr;
  std::mutex mutex_;
};
// Supported physical layout: contiguous [2, blocks, tokens, heads, dim].
// The owner must retain the underlying allocation until this object is freed.
// Record readiness on the producer stream by constructing after compute has
// been enqueued. Destinations must be unused until wait_layer completes.
class CudaLayer final : public LayerStorage {
 public:
  CudaLayer(std::shared_ptr<CudaStaging>, void* device_pointer, size_t blocks,
            size_t bytes_per_kv_block, cudaStream_t producer_stream, std::shared_ptr<void> owner);
  ~CudaLayer();
  size_t blocks() const override { return blocks_; }
  size_t block_bytes() const override { return block_bytes_; }
  void gather(std::span<const uint32_t>, size_t, std::span<std::byte>) override;
  void scatter(std::span<const uint32_t>, size_t, std::span<const std::byte>) override;

 private:
  void copy(std::span<const uint32_t>, size_t, size_t, bool to_host);
  std::shared_ptr<CudaStaging> context_;
  std::shared_ptr<void> owner_;
  std::byte* pointer_;
  size_t blocks_, block_bytes_;
  cudaEvent_t ready_ = nullptr;
};
}  // namespace kvtransfer
