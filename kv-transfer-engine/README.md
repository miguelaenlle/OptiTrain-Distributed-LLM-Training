# Optitrain KV transfer engine

C++20 engine that multiplexes layerwise KV-cache transfers over a TCP connection. A CPU backend makes transport, block mapping, resource limits, and failures testable without GPUs. An optional CUDA backend and experimental vLLM connector provide the next integration step.

**Status:** CPU implementation tested on Linux. CUDA staging compile-checked, but no GPU execution or vLLM parity result yet. The full serving/benchmark project is not complete. See [integration contract and remaining gates](docs/integration.md).

## Implemented

- Versioned, checksummed frames and Protobuf control messages, with bounded parsing.
- Nonblocking TCP using epoll on Linux (poll fallback on macOS).
- One network thread and one staging thread per peer connection.
- Multiplexed transfer sessions and round-robin chunk scheduling.
- Explicit source/destination block remapping and per-layer readiness.
- Bounded frame queues, transfer admission, absolute deadlines, and single-delivery completion polling.
- Receiver acknowledgements after destination copies complete.
- Safe cancellation: failure completion is withheld until staging exits.
- Host gather/scatter, optional CUDA pinned staging and readiness events.
- Python bindings, a restricted vLLM 0.10.1 connector, fake-KV harness, and a 200-prompt hardware parity runner.

Malformed messages, deadlines, cancellation, and disconnect abort the peer connection and fail its outstanding transfers. There is no mid-stream resumption or automatic recomputation. Use on a trusted network; transport authentication/encryption is not implemented.

## Build and CPU tests

The Docker build provides a reproducible Linux dependency environment without requiring CUDA:

```sh
docker build -t kv-build .
docker run --rm -v "$PWD:/src" kv-build sh -c '
  cmake -S . -B build -G Ninja -DKVT_PYTHON=ON -DCMAKE_BUILD_TYPE=Debug &&
  cmake --build build -j 4 &&
  ctest --test-dir build --output-on-failure &&
  PYTHONPATH=build:python python3 -m pytest -q tests/test_native.py tests/test_planning.py
'
```

For native Linux builds, install CMake, a C++20 compiler, Protobuf development headers/compiler, and GoogleTest. Python bindings additionally need Python development headers and pybind11.

The separate-process harness uses synthetic Qwen2.5-0.5B-shaped data (24 layers, 2 KV heads, head dimension 64, 16-token blocks). Its default 100 requests transfer two blocks per layer, check every destination byte, and preserve guard blocks. This is a correctness workload, not the 4K–8K-token serving benchmark.

```sh
docker run --rm --cap-add NET_ADMIN -v "$PWD:/src" kv-build \
  python3 tools/test_loopback.py --netem
```

Network shaping is confined to the disposable container's loopback interface: 1 ms delay and a 10 Gbps cap. To test across two hosts, start `build/fake_kv recv 0.0.0.0 19091 100` on the receiver, then `build/fake_kv send RECEIVER_IPV4 19091 100` on the sender.

## Sanitizers

Build address/undefined-behavior and thread sanitizers separately:

```sh
cmake -S . -B build-asan -DKVT_SANITIZER=address -DCMAKE_BUILD_TYPE=Debug
cmake --build build-asan -j 4
ctest --test-dir build-asan --output-on-failure

cmake -S . -B build-tsan -DKVT_SANITIZER=thread -DCMAKE_BUILD_TYPE=Debug
cmake --build build-tsan -j 4
ctest --test-dir build-tsan --output-on-failure
```

On the tested Docker Desktop ARM64 environment, ThreadSanitizer needs `--security-opt seccomp=unconfined` on the disposable test container to configure its address-space layout. This is a test runner constraint, not an engine runtime requirement.

## API and lifetime contract

Create a `TransferEngine` from a connected socket and matching `Config` on both peers. Transfer IDs must be nonzero and strictly increasing per connection.

1. Sender calls `begin_send(id, source_block_ids)`.
2. Receiver calls `begin_recv(id, destination_block_ids, destination_layers)`.
3. Sender calls `send_layer(id, layer_index, source_layer)` as each layer is ready.
4. Receiver calls `wait_layer` before consuming that layer.
5. Both peers call `poll_finished`; sender success requires receiver Ack.

The engine retains layer storage references. The caller must not overwrite source blocks until `wait_staged` or successful completion; destination blocks cannot be consumed until `wait_layer`. On failure, wait for a completion record or destroy the engine before reusing allocations. Completion polling removes session state; poll regularly to reclaim admission slots. `wait_all` also waits for outgoing acknowledgements to drain.

Source/destination physical IDs are local; the wire carries logical block order. The receiver alone chooses the destination allocation. Headers contain transfer ID, layer, byte offset, payload length, payload CRC32, and header CRC32. Control messages cannot mark missing data complete.

## CUDA and Python

On a supported Linux CUDA machine with Protobuf installed:

```sh
CMAKE_ARGS=-DKVT_CUDA=ON python -m pip install .
python -m pytest -q tests/test_gpu.py
```

CUDA staging currently supports contiguous `[2, blocks, tokens, heads, dim]` tensors and one shared pinned buffer per staging context. Copies complete on a dedicated stream before a chunk is published or acknowledged. The network thread can transmit the previous chunk while staging the next. There are still host copies between pinned staging and owned frame storage; this is not a zero-copy implementation. Dedicated dual pinned buffers, coalescing, and graph support remain optimization work.

The Python connector is restricted to one scheduled request at a time until actual GPU/vLLM parity is established. The native concurrent-transfer tests do not establish concurrent serving correctness. Consult [the exact integration constraints](docs/integration.md) before using it.

## Hardware acceptance and remaining work

Run the GPU round-trip test, then launch an ordinary colocated vLLM endpoint and a paired prefill/decode deployment with the pinned settings. Use `tools/parity.py --baseline URL --prefill URL --decode URL --mode streaming --output results/parity-streaming.jsonl`, and repeat with whole-request mode after relaunching the pair. The runner compares token IDs, checks baseline repeatability, retains the first mismatch, and runs paired prefill/decode requests concurrently.

Still required before claiming the original project's completion:

- Actual CUDA runtime tests and 200-prompt parity in both transfer modes.
- GPU cancellation/timeout testing and measured compute/transfer overlap.
- Broader connector scheduling support, a serving router, and graph-mode validation.
- Optimization ablations, Prometheus histograms, equal-total-GPU goodput/latency benchmarks, and raw results.
- Optional three/four-host scaling.

No GPU speedup or serving goodput claim is supported by the current CPU tests.
