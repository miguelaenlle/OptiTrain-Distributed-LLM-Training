import json
from pathlib import Path

from fleetlab.engine import run

config = {
    "regular": 1,
    "spot": 0,
    "policy": "round_robin",
    "prices": {"regular": 3, "spot": 1.2},
    "horizon_ns": 10 * 10**9,
    "faults": [],
    "requests": [
        {
            "id": i,
            "arrival_ns": i * 100_000_000,
            "prompt_length": 128,
            "output_length": 4,
            "prompt_tokens": list(range(128)),
            "output_tokens": list(range(4)),
        }
        for i in range(3)
    ],
}
result = run(config, Path("/workspace/.cache/smoke2"))
assert result["completed"] == 3, result
assert result["delivered_tokens"] == 12, result
assert abs(result["total_usd"] - 10 * 3 / 3600) < 1e-12, result
print(json.dumps(result))
