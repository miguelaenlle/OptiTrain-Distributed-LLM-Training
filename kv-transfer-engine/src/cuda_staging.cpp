#include "kvtransfer/cuda_staging.h"

#include <algorithm>
#include <cstring>
#include <limits>

namespace kvtransfer {
namespace {
void cuda_check(cudaError_t status) {
  if (status != cudaSuccess) throw Error(std::string("CUDA: ") + cudaGetErrorString(status));
}
}  // namespace
CudaStaging::CudaStaging(int device, size_t capacity) : device_(device), capacity_(capacity) {
  if (!capacity || capacity > kMaxPayload) throw Error("invalid pinned capacity");
  cuda_check(cudaSetDevice(device_));
  cuda_check(cudaStreamCreateWithFlags(&stream_, cudaStreamNonBlocking));
  try {
    cuda_check(cudaHostAlloc(reinterpret_cast<void**>(&pinned_), capacity_, cudaHostAllocDefault));
  } catch (...) {
    cudaStreamDestroy(stream_);
    throw;
  }
}
CudaStaging::~CudaStaging() {
  cudaSetDevice(device_);
  cudaStreamSynchronize(stream_);
  cudaFreeHost(pinned_);
  cudaStreamDestroy(stream_);
}
CudaLayer::CudaLayer(std::shared_ptr<CudaStaging> context, void* pointer, size_t blocks,
                     size_t bytes, cudaStream_t stream, std::shared_ptr<void> owner)
    : context_(std::move(context)),
      owner_(std::move(owner)),
      pointer_(static_cast<std::byte*>(pointer)),
      blocks_(blocks),
      block_bytes_(bytes) {
  if (!context_ || !owner_ || !pointer || !blocks || !bytes || bytes % 2 ||
      blocks > std::numeric_limits<size_t>::max() / bytes)
    throw Error("invalid CUDA layer");
  cuda_check(cudaSetDevice(context_->device_));
  cudaPointerAttributes attributes{};
  cuda_check(cudaPointerGetAttributes(&attributes, pointer_));
  if (attributes.type != cudaMemoryTypeDevice || attributes.device != context_->device_)
    throw Error("CUDA allocation device mismatch");
  cuda_check(cudaEventCreateWithFlags(&ready_, cudaEventDisableTiming));
  try {
    cuda_check(cudaEventRecord(ready_, stream));
  } catch (...) {
    cudaEventDestroy(ready_);
    throw;
  }
}
CudaLayer::~CudaLayer() {
  cudaSetDevice(context_->device_);
  cudaEventDestroy(ready_);
}
void CudaLayer::copy(std::span<const uint32_t> ids, size_t offset, size_t count, bool to_host) {
  validate_mapping(ids, blocks_, block_bytes_);
  if (offset > ids.size() * block_bytes_ || count > ids.size() * block_bytes_ - offset ||
      count > context_->capacity_)
    throw Error("CUDA copy range exceeds mapping/staging capacity");
  cuda_check(cudaSetDevice(context_->device_));
  cuda_check(cudaStreamWaitEvent(context_->stream_, ready_, 0));
  const size_t plane_block = block_bytes_ / 2;
  try {
    for (size_t done = 0; done < count;) {
      const size_t logical = offset + done, block = logical / block_bytes_,
                   within = logical % block_bytes_;
      const size_t plane = within / plane_block, plane_offset = within % plane_block;
      const size_t n = std::min(count - done, plane_block - plane_offset);
      auto* device =
          pointer_ + plane * blocks_ * plane_block + ids[block] * plane_block + plane_offset;
      auto* host = context_->pinned_ + done;
      cuda_check(cudaMemcpyAsync(to_host ? host : device, to_host ? device : host, n,
                                 to_host ? cudaMemcpyDeviceToHost : cudaMemcpyHostToDevice,
                                 context_->stream_));
      done += n;
    }
    cuda_check(cudaStreamSynchronize(context_->stream_));
  } catch (...) {
    // Never recycle the pinned buffer while already-enqueued operations use it.
    cudaStreamSynchronize(context_->stream_);
    throw;
  }
}
void CudaLayer::gather(std::span<const uint32_t> ids, size_t offset, std::span<std::byte> out) {
  std::lock_guard lock(context_->mutex_);
  copy(ids, offset, out.size(), true);
  std::memcpy(out.data(), context_->pinned_, out.size());
}
void CudaLayer::scatter(std::span<const uint32_t> ids, size_t offset,
                        std::span<const std::byte> in) {
  std::lock_guard lock(context_->mutex_);
  if (in.size() > context_->capacity_) throw Error("CUDA chunk exceeds pinned capacity");
  std::memcpy(context_->pinned_, in.data(), in.size());
  copy(ids, offset, in.size(), false);
}
}  // namespace kvtransfer
