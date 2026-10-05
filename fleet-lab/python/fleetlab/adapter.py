"""Narrow adapter to the pinned Python scheduler, KV and graph frontend.

Each experiment runs in a separate process because upstream resolves profiles
from its ASTRA working directory. This module never advances virtual time.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
UPSTREAM = ROOT / ".cache/LLMServingSim"
MODEL = "meta-llama/Llama-3.1-8B"
HARDWARE = "RTXPRO6000"


def binary_path():
    return Path(
        os.environ.get(
            "FLEETLAB_BINARY",
            UPSTREAM
            / "astra-sim/build/astra_analytical/build/bin/AstraSim_Analytical_Congestion_Unaware",
        )
    )


def load_upstream():
    sys.path.insert(0, str(UPSTREAM))
    sys.path.insert(0, str(ROOT / ".cache/chakra-runtime"))
    os.chdir(UPSTREAM / "astra-sim")
    from serving.core.config_builder import build_cluster_config
    from serving.core.graph_generator import generate_graph
    from serving.core.request import RequestStatus
    from serving.core.scheduler import Scheduler
    from serving.core.trace_generator import generate_event, generate_trace
    from serving.core.utils import get_workload

    return (
        build_cluster_config,
        generate_graph,
        RequestStatus,
        Scheduler,
        generate_event,
        generate_trace,
        get_workload,
    )


class Frontend:
    def __init__(self, config, output):
        (
            self.build_cluster,
            self.generate_graph,
            self.RequestStatus,
            self.Scheduler,
            self.generate_event,
            self.generate_trace,
            self.get_workload,
        ) = load_upstream()
        self.config = config
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.schedulers = {}
        self.requests = {}
        placement = config.get("placement", list(range(config["regular"] + config["spot"])))
        nodes = []
        for node in sorted(set(placement)):
            members = [i for i, mapped in enumerate(placement) if mapped == node]
            nodes.append(
                {
                    "num_instances": len(members),
                    "cpu_mem": {"mem_size": 512, "mem_bw": 256, "mem_latency": 0},
                    "instances": [
                        {
                            "model_name": MODEL,
                            "hardware": HARDWARE,
                            "npu_mem": {"mem_size": 96, "mem_bw": 1597, "mem_latency": 0},
                            "tp_size": 1,
                            "pp_size": 1,
                            "pd_type": None,
                        }
                        for _ in members
                    ],
                }
            )
        # Contiguous node placement is validated by experiment input schema.
        cluster = {"num_nodes": len(nodes), "link_bw": 16, "link_latency": 20000, "nodes": nodes}
        cluster_path = self.output / "cluster.json"
        cluster_path.write_text(json.dumps(cluster, indent=2))
        self.cluster = self.build_cluster(
            str(UPSTREAM / "astra-sim"),
            os.path.relpath(cluster_path, UPSTREAM),
            inputs_root=str(self.output / "inputs"),
        )
        memory_path = Path(self.cluster["memory_config_path"])
        memory = json.loads(memory_path.read_text())
        memory["remote_mem"]["memory-type"] = "PER_NPU_MEMORY_EXPANSION"
        memory_path.write_text(json.dumps(memory, indent=2))
        self.generate_graph(
            None,
            None,
            len(placement),
            event=True,
            inputs_root=self.cluster["inputs_root"],
            trace=self.generate_event(0, inputs_root=self.cluster["inputs_root"]),
        )
        for worker in range(len(placement)):
            self.reset(worker)

    def reset(self, worker):
        node = self.cluster["inst2node_mapping"][worker]
        self.schedulers[worker] = self.Scheduler(
            MODEL,
            node,
            worker,
            self.config.get("max_num_seqs", 32),
            2048,
            1,
            1,
            1,
            96,
            512,
            worker,
            None,
            16,
            16,
            len(self.config["requests"]),
            self.config.get("prefix_cache", True),
            False,
            None,
            None,
            enable_chunked_prefill=True,
            npu_memory_utilization=0.9,
        )
        self.requests[worker] = {}
        return self.stats(worker)

    def admit(self, worker, request):
        scheduler = self.schedulers[worker]
        scheduler.add_request(
            [
                request["attempt_id"],
                MODEL,
                request["prompt_length"],
                request["prompt_length"] + request["output_length"],
                request["arrival_ns"],
                worker,
                request["prompt_tokens"],
                request["output_tokens"],
            ]
        )
        self.requests[worker][request["attempt_id"]] = next(
            r for r in scheduler.waiting if r.id == request["attempt_id"]
        )
        return self.stats(worker)

    def abort(self, worker, attempt):
        scheduler = self.schedulers[worker]
        req = self.requests[worker].pop(attempt, None)
        if req is not None:
            scheduler.kv.free(req)
            req.status = self.RequestStatus.FINISHED
            scheduler.running = [r for r in scheduler.running if r is not req]
            scheduler.waiting = [r for r in scheduler.waiting if r is not req]
        return self.stats(worker)

    def schedule(self, worker, time_ns):
        scheduler = self.schedulers[worker]
        batch = scheduler.schedule(time_ns, worker)
        if batch is None:
            return {"batch": None, **self.stats(worker)}
        trace = self.generate_trace(
            batch,
            HARDWARE,
            1,
            1,
            1,
            1,
            node_id=self.cluster["inst2node_mapping"][worker],
            instance_id=worker,
            max_num_seqs=scheduler.max_num_seqs,
            placement=self.cluster["placement"][worker],
            enable_prefix_caching=scheduler.enable_prefix_caching,
            dtype="bfloat16",
            inputs_root=self.cluster["inputs_root"],
        )
        self.generate_graph(
            batch,
            HARDWARE,
            1,
            node_id=self.cluster["inst2node_mapping"][worker],
            instance_id=worker,
            npu_offset=worker,
            inputs_root=self.cluster["inputs_root"],
            trace=trace,
        )
        return {
            "batch": batch.batch_id,
            "graph": self.get_workload(
                batch, HARDWARE, worker, inputs_root=self.cluster["inputs_root"]
            ),
            "attempts": [
                {
                    "id": r.id,
                    "scheduled": batch.scheduled_tokens[r.id],
                    "computed": r.num_computed_tokens,
                    "prompt": r.original_input,
                    "cache_hit": r.prefix_cache_hit,
                }
                for r in batch.requests
            ],
            **self.stats(worker),
        }

    def complete(self, worker, batch, time_ns):
        scheduler = self.schedulers[worker]
        active = next(b for b in scheduler.inflight if b.batch_id == batch)
        before = {r.id: r.num_tokens_reached for r in active.requests}
        scheduler.add_done(batch + 1, worker, time_ns)
        produced = [
            {
                "id": r.id,
                "produced": r.num_tokens_reached - before[r.id],
                "computed": r.num_computed_tokens,
                "cache_hit": r.prefix_cache_hit,
            }
            for r in active.requests
            if r.status != self.RequestStatus.FINISHED or r.end_time == time_ns
        ]
        return {"tokens": produced, **self.stats(worker)}

    def stats(self, worker):
        scheduler = self.schedulers[worker]
        pool = scheduler.memory.npu_pool
        return {
            "waiting": len(scheduler.waiting),
            "active": len(scheduler.running),
            "kv_bytes": scheduler.kv.npu_used_bytes(),
            "kv_capacity_tokens": pool.num_blocks * pool.block_size,
            "kv_capacity_bytes": pool.num_blocks * pool.bytes_per_block,
            "estimated_work": sum(
                max(0, r.output - r.num_computed_tokens)
                for r in scheduler.running + scheduler.waiting
            ),
            "recompute_tokens": scheduler.recompute_tokens,
            "preemptions": scheduler.num_preemptions,
            "prefix_hit_tokens": sum(r.prefix_cache_hit for r in self.requests[worker].values()),
        }


def provenance():
    lock = json.loads((ROOT / "fleet-lab/upstream-lock.json").read_text())
    files = [
        UPSTREAM / lock["profile"] / name
        for name in ("dense.csv", "per_sequence.csv", "attention.csv", "skew.csv", "skew_fit.csv")
    ]
    files += [
        UPSTREAM / "profiler/perf/RTXPRO6000/meta-llama/Llama-3.1-8B/bf16/meta.yaml",
        UPSTREAM / "configs/model/meta-llama/Llama-3.1-8B.json",
    ]
    lock["sha256"] = {
        str(p.relative_to(UPSTREAM)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
    }
    authored = sorted((ROOT / "fleet-lab/cpp").glob("*")) + sorted(
        (ROOT / "fleet-lab/python/fleetlab").glob("*.py")
    )
    authored += sorted((ROOT / "fleet-lab/patches").glob("*.patch"))
    lock["authored_sha256"] = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in authored
    }
    lock["binary_sha256"] = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in {
            "core": ROOT / ".cache/fleetlab-core",
            "astra": binary_path(),
        }.items()
    }
    lock["limitations"] = [
        "kernel profile, not AWS/full-server fidelity",
        "full-model KV is modeled upper bound without CUDA/activation reserve",
        "independent PER_NPU remote memory; shared fabric cancellation excluded",
    ]
    return lock
