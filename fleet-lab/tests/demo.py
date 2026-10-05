"""Mandatory 16-replica, half-down low/high and all-down correctness runs."""

import json
from pathlib import Path

from fleetlab.engine import run
from fleetlab.scenarios import experiment

root = Path("/workspace/.cache/flagships")
root.mkdir(exist_ok=True)
results = {}
for policy in ("round_robin", "least_work"):
    for label, rate in (("low", 64), ("high", 120)):
        cfg = experiment(8, 8, policy, "half", rate=rate)
        result = run(cfg, root / f"{label}-{policy}")
        results[f"{label}-{policy}"] = result
        (root / "results.json").write_text(json.dumps(results, indent=2))
        print(label, policy, json.dumps(result), flush=True)
cfg = experiment(8, 8, "least_work", "all_down", rate=64)
fault = cfg["faults"][0]
probe_time = (
    fault["time_ns"]
    + fault["availability_ns"]
    + fault["init_ns"]
    + fault["load_ns"]
    + cfg["discovery_ns"]
    + 100_000_000
)
cfg["requests"].append(
    {
        "id": 1000,
        "arrival_ns": probe_time,
        "prompt_length": 128,
        "output_length": 4,
        "prompt_tokens": list(range(128)),
        "output_tokens": list(range(4)),
    }
)
cfg["recovery_probe_request_id"] = 1000
result = run(cfg, root / "all-down")
summary = json.loads((root / "all-down" / "summary.json").read_text())
assert next(r for r in summary["requests"] if r["id"] == 1000)["state"] == "Completed"
results["all-down"] = result
(root / "results.json").write_text(json.dumps(results, indent=2))
print("all-down", json.dumps(result), flush=True)
