"""Experimental vLLM 0.10.1 connector; GPU acceptance remains required.

Only eager, non-chunked Qwen2, FlashAttention NHD, TP=PP=1, no prefix cache,
and one scheduled request per worker are supported. Fail closed otherwise.
"""

import threading
import time
from dataclasses import dataclass, field
from importlib.metadata import version

import torch
from vllm.distributed.kv_transfer.kv_connector.v1.base import (
    KVConnectorBase_V1,
    KVConnectorMetadata,
    KVConnectorRole,
)

from . import _native as native
from .planning import RequestPlan, prefix_tokens, request_plan


@dataclass
class Metadata(KVConnectorMetadata):
    requests: list[RequestPlan] = field(default_factory=list)


class TCPConnector(KVConnectorBase_V1):
    def __init__(self, vllm_config, role):
        super().__init__(vllm_config, role)
        if version("vllm") != "0.10.1":
            raise RuntimeError("TCPConnector requires vLLM 0.10.1")
        c = vllm_config
        if (
            c.parallel_config.tensor_parallel_size != 1
            or c.parallel_config.pipeline_parallel_size != 1
            or not c.model_config.enforce_eager
            or c.cache_config.enable_prefix_caching
            or c.scheduler_config.enable_chunked_prefill
            or c.scheduler_config.max_num_seqs != 1
        ):
            raise ValueError(
                "requires TP=PP=1, eager, no prefix cache/chunked prefill, max_num_seqs=1"
            )
        hf = c.model_config.hf_config
        if hf.model_type != "qwen2" or not c.model_config.revision:
            raise ValueError("requires Qwen2 with an explicit model revision")
        extra = c.kv_transfer_config.kv_connector_extra_config
        self.side = extra["side"]
        if self.side not in ("prefill", "decode"):
            raise ValueError("side must be prefill or decode")
        self.block_size = c.cache_config.block_size
        self.config = native.Config(
            f"{c.model_config.model}@{c.model_config.revision}",
            hf.num_hidden_layers,
            hf.num_key_value_heads,
            hf.hidden_size // hf.num_attention_heads,
            self.block_size,
            str(c.model_config.dtype).removeprefix("torch."),
        )
        self.config.timeout_ms = int(extra.get("timeout_ms", 30000))
        self.config.chunk_bytes = int(extra.get("chunk_bytes", 262144))
        self.streaming = bool(extra.get("streaming", True))
        self.address = extra.get("address", "127.0.0.1")
        self.port = int(extra.get("port", 19090))
        self.pending = {}
        self.scheduled = set()
        self.active = {}
        self.finished_requests = set()
        self.completed_sends = set()
        self.engine = None
        self.connection_error = None
        self.connected = threading.Event()
        self.caches = {}
        self.deferred_layers = []
        if role == KVConnectorRole.WORKER:
            if not native.cuda_enabled:
                raise RuntimeError("rebuild kvtransfer with KVT_CUDA=ON")
            self.staging = native.CudaStaging(torch.cuda.current_device())
            threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self):
        try:
            if self.side == "prefill":
                self.engine = native.Engine.accept(self.address, self.port, self.config)
            else:
                deadline = time.monotonic() + self.config.timeout_ms / 1000
                while True:
                    try:
                        self.engine = native.Engine.connect(self.address, self.port, self.config)
                        break
                    except native.TransferError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
        except Exception as error:
            self.connection_error = error
        finally:
            self.connected.set()

    def _engine(self):
        if not self.connected.wait(self.config.timeout_ms / 1000):
            raise TimeoutError("peer connection timed out")
        if self.connection_error is not None:
            raise RuntimeError("peer connection failed") from self.connection_error
        return self.engine

    @staticmethod
    def _id(plan):
        return native.TransferId(plan.incarnation, plan.sequence)

    def register_kv_caches(self, kv_caches):
        self.caches = dict(kv_caches)
        # Preserve registration order; validate dimensions again in cuda_layer.
        self.layer_names = list(kv_caches)
        self.layer_indices = {name: i for i, name in enumerate(self.layer_names)}
        hf = self._vllm_config.model_config.hf_config
        expected_tail = (
            self.block_size,
            hf.num_key_value_heads,
            hf.hidden_size // hf.num_attention_heads,
        )
        if len(self.layer_names) != hf.num_hidden_layers:
            raise ValueError("KV cache layer count mismatch")
        for tensor in kv_caches.values():
            if (
                tensor.ndim != 5
                or tensor.shape[0] != 2
                or tuple(tensor.shape[2:]) != expected_tail
                or not tensor.is_contiguous()
            ):
                raise ValueError(
                    "requires contiguous FlashAttention NHD KV layout "
                    "[2, blocks, tokens, heads, dim]"
                )

    def get_num_new_matched_tokens(self, request, num_computed_tokens):
        if self.side != "decode" or not (request.kv_transfer_params or {}).get("kvtransfer"):
            return 0, False
        return max(
            0, prefix_tokens(len(request.prompt_token_ids), self.block_size) - num_computed_tokens
        ), False

    def update_state_after_alloc(self, request, blocks, num_external_tokens):
        if request.request_id in self.scheduled:
            return
        if self.side == "decode" and not num_external_tokens:
            return
        plan = request_plan(
            request.request_id,
            request.kv_transfer_params,
            len(request.prompt_token_ids),
            self.block_size,
            blocks.get_block_ids()[0],
        )
        if plan is not None:
            self.pending[request.request_id] = plan
            self.scheduled.add(request.request_id)

    def build_connector_meta(self, scheduler_output):
        meta = Metadata(list(self.pending.values()))
        self.pending.clear()
        return meta

    def start_load_kv(self, forward_context, **kwargs):
        meta = self._get_connector_metadata()
        if not isinstance(meta, Metadata):
            raise TypeError("unexpected connector metadata")
        if not meta.requests:
            return
        engine = self._engine()
        if not self.caches:
            raise RuntimeError("KV caches were not registered")
        for plan in meta.requests:
            identifier = self._id(plan)
            if self.side == "prefill":
                engine.begin_send(identifier, plan.blocks)
            else:
                views = [
                    native.cuda_layer(
                        self.staging, self.caches[name], torch.cuda.current_stream().cuda_stream
                    )
                    for name in self.layer_names
                ]
                engine.begin_recv(identifier, plan.blocks, views)
            self.active[(plan.incarnation, plan.sequence)] = plan

    def wait_for_layer_load(self, layer_name):
        if self.side != "decode":
            return
        for plan in self.active.values():
            self._engine().wait_layer(
                self._id(plan), self.layer_indices[layer_name], self.config.timeout_ms
            )

    def save_kv_layer(self, layer_name, kv_layer, attn_metadata, **kwargs):
        if self.side != "prefill":
            return
        meta = self._get_connector_metadata()
        for plan in meta.requests:
            view = native.cuda_layer(
                self.staging, kv_layer, torch.cuda.current_stream().cuda_stream
            )
            args = (self._id(plan), self.layer_indices[layer_name], view)
            if self.streaming:
                self._engine().send_layer(*args)
            else:
                self.deferred_layers.append(args)

    def wait_for_save(self):
        if self.side != "prefill":
            return
        for args in self.deferred_layers:
            self._engine().send_layer(*args)
        self.deferred_layers.clear()
        meta = self._get_connector_metadata()
        for plan in meta.requests:
            self._engine().wait_staged(self._id(plan), self.config.timeout_ms)

    def request_finished(self, request, block_ids):
        known = request.request_id in self.scheduled
        self.scheduled.discard(request.request_id)
        if request.request_id in self.pending:
            self.pending.pop(request.request_id)
            return False, None
        # Worker reports the request only after the receiver's acknowledgement.
        return self.side == "prefill" and known, None

    def get_finished(self, finished_req_ids):
        if self.side == "prefill":
            tracked = {plan.request_id for plan in self.active.values()} | self.completed_sends
            self.finished_requests.update(finished_req_ids & tracked)
        if self.engine is None:
            return set(), set()
        for result in self.engine.poll_finished():
            plan = self.active.pop((result.id.incarnation, result.id.sequence))
            if not result.success:
                raise RuntimeError(f"KV transfer failed for {plan.request_id}: {result.error}")
            if result.sending:
                self.completed_sends.add(plan.request_id)
        released = self.finished_requests & self.completed_sends
        self.finished_requests.difference_update(released)
        self.completed_sends.difference_update(released)
        # Synchronous receive readiness is enforced by wait_for_layer_load.
        return released, set()

    @classmethod
    def get_required_kvcache_layout(cls, vllm_config):
        return "NHD"
