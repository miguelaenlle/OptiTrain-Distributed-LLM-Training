"""Exhaustive bounded-grid oracle against exact pruning/reuse search."""

import json
from pathlib import Path

from fleetlab.cli import search
from fleetlab.core import call
from fleetlab.engine import run
from fleetlab.scenarios import experiment, trace

root = Path("/workspace/.cache/oracle")
root.mkdir(exist_ok=True)
checks = []
for label, suite, q, count in [
    ("positive", ["healthy"], 0.1, 12),
    ("failure", ["healthy", "half"], 10, 20),
]:
    optimized = search(
        root / f"{label}-optimized", q, count=count, max_replicas=2, suite=suite, wall_seconds=120
    )
    requests = trace(0.4 * 16 * q, 42, count)
    results = []
    for c in call({"op": "candidates", "max_replicas": 2}):
        item = {**c, "scenarios": {}}
        for scenario in suite:
            cfg = experiment(**c, scenario=scenario, requests=requests)
            cfg["horizon_ns"] = json.loads(
                (root / f"{label}-optimized" / "search-manifest.json").read_text()
            )["shared_horizon_ns"]
            name = f"r{c['regular']}-s{c['spot']}-{c['policy']}-{scenario}"
            item["scenarios"][scenario] = run(cfg, root / f"{label}-full" / name, retain=False)
        results.append(item)
    oracle = call({"op": "rank", "suite": suite, "results": results})
    assert optimized["coverage_complete"] and optimized["evaluated_pairs"] == 10
    assert oracle["feasible_pairs"] == optimized["feasible_pairs"]
    assert (oracle["winner"] is None) == (optimized["winner"] is None)
    if oracle["winner"]:
        assert {k: oracle["winner"][k] for k in ("regular", "spot", "policy", "objective_usd")} == {
            k: optimized["winner"][k] for k in ("regular", "spot", "policy", "objective_usd")
        }
    checks.append({"label": label, "oracle": oracle, "optimized": optimized, "passed": True})
    (root / "results.json").write_text(json.dumps(checks, indent=2))
    print(label, "passed", flush=True)
