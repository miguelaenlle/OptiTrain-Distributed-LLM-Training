"""Full, unpruned profile-backed held-out seeds and declared sensitivities."""

import json
from pathlib import Path

from fleetlab.engine import run
from fleetlab.scenarios import experiment

root = Path("/workspace/.cache/heldout")
root.mkdir(exist_ok=False)
results = []
cases = [
    (f"seed-{seed}-{policy}", seed, policy, {})
    for seed in (99, 100)
    for policy in ("round_robin", "least_work")
]
cases += [
    ("fast-discovery", 99, "least_work", {"discovery_ns": 0}),
    ("slow-discovery", 99, "least_work", {"discovery_ns": 10_000_000_000}),
    ("short-recovery", 99, "least_work", {"availability_ns": 5_000_000_000}),
    ("long-recovery", 99, "least_work", {"availability_ns": 120_000_000_000}),
]
for name, seed, policy, overrides in cases:
    cfg = experiment(8, 8, policy, "half", seed=seed, overrides=overrides)
    m = run(cfg, root / name, retain=False)
    results.append(
        {"name": name, "seed": seed, "policy": policy, "overrides": overrides, "metrics": m}
    )
    (root / "results.json").write_text(json.dumps(results, indent=2))
    print(name, m["completion_rate"], m["compliance_rate"], flush=True)
