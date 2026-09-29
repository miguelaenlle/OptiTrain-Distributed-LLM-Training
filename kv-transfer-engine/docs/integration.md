# Integration contract and acceptance status

## Pinned target

- vLLM `0.10.1`, commit `aab549870df50edf0512f0a59b574f692f546465`.
- Model and tokenizer: `Qwen/Qwen2.5-0.5B-Instruct`, revision `7ae557604adf67be50417f59c2c2f167def9a775`.
- CUDA-enabled PyTorch, Linux, one GPU per worker; eager execution and FlashAttention NHD.
- C++ compiler with C++20 support; tested CPU toolchain: GCC 12.2, Protobuf 3.21.12, GoogleTest 1.12.1 on Debian bookworm ARM64.

Inspected upstream sources:

- [Connector base](https://github.com/vllm-project/vllm/blob/aab549870df50edf0512f0a59b574f692f546465/vllm/distributed/kv_transfer/kv_connector/v1/base.py)
- [Shared-storage reference](https://github.com/vllm-project/vllm/blob/aab549870df50edf0512f0a59b574f692f546465/vllm/distributed/kv_transfer/kv_connector/v1/shared_storage_connector.py)
- [NIXL lifecycle reference](https://github.com/vllm-project/vllm/blob/aab549870df50edf0512f0a59b574f692f546465/vllm/distributed/kv_transfer/kv_connector/v1/nixl_connector.py)

## Experimental connector

`kvtransfer.connector.TCPConnector` is implemented against the pinned interface but has **not passed a real vLLM/GPU run**. Do not describe it as production-ready or claim token parity. Its initial contract deliberately rejects chunked prefill, prefix caching, graph execution, TP/PP > 1, and more than one scheduled request per worker. The native engine independently supports concurrent transfers.

Configure `kv_connector="TCPConnector"`, `kv_connector_module_path="kvtransfer.connector"`, `kv_role="kv_producer"` on prefill and `"kv_consumer"` on decode. Extra configuration:

```json
{"side":"prefill","address":"0.0.0.0","port":19090,"streaming":true,"timeout_ms":30000}
```

Decode uses `side="decode"` and the prefill host's IPv4 address. Each pair has one TCP connection. Peers establish it in a background thread during worker startup. There is no reconnection after failure; restart the pair.

Each paired inference request must carry the same transfer parameters:

```json
{"kv_transfer_params":{"kvtransfer":{"incarnation":42,"sequence":1}}}
```

Use a fresh nonzero incarnation for each pair and strictly increasing sequences on that connection. Submit paired requests in the same order. Request IDs need not match between vLLM instances; the transfer key does. A production router and arbitrary out-of-order scheduling are not implemented.

## First-token and block semantics

The engine transfers complete blocks for the first `floor((prompt_length - 1) / block_size) * block_size` prompt tokens. Decode recomputes the remaining prompt tail, including at least the final prompt token, and generates the output itself. Discard prefill's generated token. This avoids forwarding an ambiguous first token and avoids transferring uninitialized bytes in a partial block. Prompts too short for one full prefix block use ordinary decode.

Prefill and decode requests must run concurrently. Waiting for prefill to return before submitting decode can deadlock until timeout because destination allocation announces the transfer.

`update_state_after_alloc` maps vLLM's allocated block IDs. `start_load_kv` registers send/receive sessions. Layer hooks submit readiness events or wait for copied destination data. `wait_for_save` waits for source staging, protecting source writes; with a bounded staging queue this can experience backpressure. `request_finished` defers source block release until `get_finished` observes receiver acknowledgement.

`streaming=false` defers submission of all layers until the forward pass exits. `streaming=true` submits from each layer hook. Both modes still require hardware parity and trace validation.

## Required hardware gates

1. Install the CUDA-enabled wheel, then run `pytest tests/test_gpu.py` on a GPU. A skip is not a pass.
2. Confirm actual vLLM KV tensor layout and hook ordering using the pinned model/backend.
3. Test short prompts, lengths 15/16/17 and 31/32/33, then the fixed 200-prompt corpus.
4. Compare exact generated token IDs against repeatable colocated greedy output in both transfer modes.
5. Exercise cancellation, destination exhaustion, timeouts, and source block reuse on GPU.
6. Trace layer compute, D2H, wire transfer, and H2D to establish actual overlap.
7. Only then broaden scheduling concurrency/graph modes and collect equal-total-GPU serving benchmarks.

No GPU runtime results, token-parity results, or serving speedups have been measured in the current local environment.
