"""Same-profile/trace healthy comparison with the preserved upstream frontend."""

import ast
import csv
import json
import os
import subprocess
import time
from pathlib import Path

from fleetlab.adapter import UPSTREAM
from fleetlab.engine import run
from fleetlab.scenarios import experiment

root = Path("/workspace/.cache/healthy-parity")
root.mkdir(exist_ok=True)
requests = [
    {
        "id": i,
        "arrival_ns": 0,
        "prompt_length": 128,
        "output_length": 8,
        "prompt_tokens": list(range(128)),
        "output_tokens": list(range(8)),
    }
    for i in range(10)
]
trace = root / "trace.jsonl"
trace.write_text(
    "".join(
        json.dumps(
            {
                "input_toks": r["prompt_length"],
                "output_toks": r["output_length"],
                "arrival_time_ns": r["arrival_ns"],
                "input_tok_ids": r["prompt_tokens"],
                "output_tok_ids": r["output_tokens"],
            }
        )
        + "\n"
        for r in requests
    )
)
args = [
    "python",
    "-m",
    "serving",
    "--cluster-config",
    "configs/cluster/single_node_single_instance.json",
    "--dataset",
    os.path.relpath(trace, UPSTREAM),
    "--output",
    str(root / "baseline.csv"),
    "--num-reqs",
    "10",
    "--dtype",
    "bfloat16",
    "--max-num-seqs",
    "32",
    "--block-size",
    "16",
    "--enable-prefix-caching",
    "--log-level",
    "WARNING",
]
with (root / "baseline.log").open("w") as log:
    start = time.perf_counter()
    subprocess.run(
        args,
        cwd=UPSTREAM,
        stdout=log,
        stderr=log,
        check=True,
        env={**os.environ, "PYTHONPATH": "/workspace/.cache/chakra-runtime"},
    )
    upstream_seconds = time.perf_counter() - start
config = experiment(1, 0, scenario="healthy", requests=requests)
start = time.perf_counter()
run(config, root / "fleet")
fleet_seconds = time.perf_counter() - start
actual = json.loads((root / "fleet" / "summary.json").read_text())["requests"]
rows = list(csv.DictReader((root / "baseline.csv").open()))
assert len(rows) == len(actual) == 10
comparison = []
for old, new in zip(rows, actual):
    assert abs(int(old["TTFT"]) - new["ttft_ns"]) <= 1, (old, new)
    assert ast.literal_eval(old["ITL"]) == new["itl_ns"]
    assert abs(int(old["end_time"]) - new["finish_ns"]) <= 1
    comparison.append(
        {
            "id": new["id"],
            "ttft_delta_ns": new["ttft_ns"] - int(old["TTFT"]),
            "e2e_delta_ns": new["finish_ns"] - int(old["end_time"]),
        }
    )
value = {
    "passed": True,
    "requests": comparison,
    "upstream_wall_seconds": upstream_seconds,
    "fleet_wall_seconds": fleet_seconds,
    "overhead_ratio": fleet_seconds / upstream_seconds - 1,
    "clock_tolerance_reason": "upstream bootstrap graph advances 1 ns; fleet time zero is explicit",
    "overhead_scope": (
        "small end-to-end fixture including Python startup and retained replay; "
        "not a broad capacity benchmark"
    ),
    "target_overhead_fraction": 0.25,
    "target_met": fleet_seconds / upstream_seconds <= 1.25,
}
(root / "results.json").write_text(json.dumps(value, indent=2))
print(json.dumps(value), flush=True)
