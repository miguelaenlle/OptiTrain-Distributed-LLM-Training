"""Real ASTRA integration spike: profile graph cancellation and fresh context."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = ROOT / ".cache/LLMServingSim"
sys.path.insert(0, str(UPSTREAM))
sys.path.insert(0, str(ROOT / ".cache/chakra-runtime"))
os.chdir(UPSTREAM / "astra-sim")

from serving.core.config_builder import build_cluster_config
from serving.core.graph_generator import generate_graph
from serving.core.scheduler import Scheduler
from serving.core.trace_generator import generate_event, generate_trace
from serving.core.utils import get_workload


def main():
    output = ROOT / ".cache/spike"
    output.mkdir(exist_ok=True)
    config = json.loads((UPSTREAM / "configs/cluster/single_node_single_instance.json").read_text())
    config_path = output / "cluster.json"
    config_path.write_text(json.dumps(config))
    cluster = build_cluster_config(
        str(UPSTREAM / "astra-sim"),
        os.path.relpath(config_path, UPSTREAM),
        inputs_root=str(output / "inputs"),
    )
    # Explicit independent per-replica memory, no shared fabric/collective claims.
    memory = json.loads(Path(cluster["memory_config_path"]).read_text())
    memory["remote_mem"]["memory-type"] = "PER_NPU_MEMORY_EXPANSION"
    Path(cluster["memory_config_path"]).write_text(json.dumps(memory))
    generate_graph(
        None,
        None,
        1,
        event=True,
        inputs_root=cluster["inputs_root"],
        trace=generate_event(0, inputs_root=cluster["inputs_root"]),
    )
    scheduler = Scheduler(
        "meta-llama/Llama-3.1-8B",
        0,
        0,
        32,
        2048,
        1,
        1,
        1,
        96,
        512,
        0,
        None,
        16,
        16,
        1,
        True,
        False,
        None,
        None,
        enable_chunked_prefill=True,
        npu_memory_utilization=0.9,
    )
    scheduler.add_request([0, scheduler.model, 1024, 1024 + 64, 0, 0])
    batch = scheduler.schedule(0, 0)
    trace = generate_trace(
        batch,
        "RTXPRO6000",
        1,
        1,
        1,
        1,
        max_num_seqs=32,
        placement=cluster["placement"][0],
        enable_prefix_caching=True,
        dtype="bfloat16",
        inputs_root=cluster["inputs_root"],
    )
    generate_graph(batch, "RTXPRO6000", 1, inputs_root=cluster["inputs_root"], trace=trace)
    graph = get_workload(batch, "RTXPRO6000", inputs_root=cluster["inputs_root"])
    binary = (
        UPSTREAM
        / "astra-sim/build/astra_analytical/build/bin/AstraSim_Analytical_Congestion_Unaware"
    )
    process = subprocess.Popen(
        [
            str(binary),
            "--workload-configuration="
            + get_workload(None, None, event=True, inputs_root=cluster["inputs_root"]),
            "--system-configuration=" + cluster["system_config_path"],
            "--network-configuration=" + cluster["network_config_path"],
            "--memory-configuration=" + cluster["memory_config_path"],
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "OPTITRAIN_FLEET_LAB": "1"},
    )
    journal = []

    def read_until(event):
        while True:
            line = process.stdout.readline()
            if not line:
                raise RuntimeError(process.stderr.read())
            if line.startswith("FLEETLAB "):
                value = json.loads(line[9:])
                journal.append(value)
                if value["event"] == "error":
                    raise RuntimeError(value)
                if value["event"] == event:
                    return value

    def send(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()
        return read_until("ack")

    read_until("ready")
    send({"op": "start", "worker": 0, "batch": "old", "graph": graph})
    send({"op": "crash", "worker": 0, "time_ns": 1000})
    send({"op": "advance", "time_ns": 1000})
    cancelled = next(x for x in journal if x["event"] == "cancelled")
    assert cancelled["time_ns"] == 1000
    assert cancelled["callbacks_removed"] > 0
    # Empty compute cluster still advances to a future lifecycle event.
    send({"op": "advance", "time_ns": 1_000_000_000})
    send({"op": "start", "worker": 0, "batch": "fresh", "graph": graph})
    send({"op": "advance", "time_ns": 2_000_000_000})
    completed = [x for x in journal if x["event"] == "batch_completed"]
    assert len(completed) == 1, completed
    assert completed[0]["batch"] == "fresh" and completed[0]["incarnation"] == 1
    process.stdin.write('{"op":"exit"}\n')
    process.stdin.flush()
    assert read_until("cleanup")["owned_callbacks"] == 0
    assert process.wait(timeout=10) == 0, process.stderr.read()
    (output / "journal.json").write_text(json.dumps(journal, indent=2))
    print(
        json.dumps(
            {
                "passed": True,
                "cancelled_at_ns": 1000,
                "callbacks_removed": cancelled["callbacks_removed"],
                "fresh_completion_ns": completed[0]["time_ns"],
            }
        )
    )


if __name__ == "__main__":
    main()
