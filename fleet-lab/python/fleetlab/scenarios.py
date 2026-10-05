"""Declared synthetic workload and scenario assumptions; no measured AWS claims."""

import copy
import random

NS = 10**9
SUITE = ["healthy", "half", "single_spot", "correlated_spot", "regular_crash"]


def trace(rate, seed=42, count=1000):
    if not 0 < rate <= 100000 or type(count) is not int or not 1 <= count <= 10000:
        raise ValueError("Use a positive finite rate and 1–10,000 requests")
    rng = random.Random(seed)
    requests = []
    t = 0.0
    shared = list(range(128))
    for i in range(count):
        t += rng.expovariate(rate)
        prompt = rng.choice([128, 256, 1024])
        output = rng.choice([32, 64, 128])
        prefix = shared if rng.random() < 0.25 else [rng.randrange(32000) for _ in range(128)]
        requests.append(
            {
                "id": i,
                "arrival_ns": round(t * NS),
                "prompt_length": prompt,
                "output_length": output,
                "prompt_tokens": prefix + [rng.randrange(32000) for _ in range(prompt - 128)],
                "output_tokens": [rng.randrange(32000) for _ in range(output)],
            }
        )
    return requests


def experiment(
    regular=8,
    spot=8,
    policy="round_robin",
    scenario="half",
    rate=64,
    seed=42,
    count=1000,
    requests=None,
    overrides=None,
):
    supplied_trace = requests is not None
    requests = copy.deepcopy(requests if requests is not None else trace(rate, seed, count))
    n = regular + spot
    fault_time = (requests[0]["arrival_ns"] + requests[-1]["arrival_ns"]) // 2 if requests else 0
    spot_workers = list(range(regular, n))
    if scenario == "healthy":
        affected = []
    elif scenario == "single_spot":
        affected = spot_workers[:1]
    elif scenario == "half":
        affected = random.Random(seed + 11).sample(list(range(n)), (n + 1) // 2)
    elif scenario == "correlated_spot":
        affected = random.Random(seed + 12).sample(spot_workers, (spot + 1) // 2)
    elif scenario == "regular_crash":
        affected = list(range(regular))[:1]
    elif scenario == "staggered":
        affected = list(range(n))[: min(3, n)]
    elif scenario == "all_down":
        affected = list(range(n))
    else:
        raise ValueError("Unknown scenario")
    fault = {
        "time_ns": fault_time,
        "workers": affected,
        "scope": "node",
        "terminate": scenario in ("single_spot", "correlated_spot"),
        "availability_ns": (120 if scenario == "correlated_spot" else 60) * NS,
        "init_ns": 5 * NS,
        "load_ns": 30 * NS,
    }
    horizon = (requests[-1]["arrival_ns"] if requests else 0) + 120 * NS
    # One shared observation/billing horizon, including no-op class scenarios.
    horizon = max(horizon, fault_time + 160 * NS + 1)
    cfg = {
        "regular": regular,
        "spot": spot,
        "policy": policy,
        "scenario": scenario,
        "requests": requests,
        "faults": [fault] if affected else [],
        "horizon_ns": horizon,
        "noop_reason": "required class absent" if scenario != "healthy" and not affected else None,
        "prices": {"regular": 3.0, "spot": 1.2},
        "seed": seed,
        "synthetic_rate_per_second": None if supplied_trace else rate,
        "deadline_ns": 60 * NS,
        "discovery_ns": 5 * NS,
        "report_period_ns": NS,
        "report_delay_ns": 5 * NS,
        "backoff_ns": 100_000_000,
        "max_attempts": 3,
        "waiting_limit": 4096,
        "worker_admission_limit": 4096,
        "price_provenance": {
            "assumed": True,
            "currency": "USD",
            "unit": "instance_hour",
            "source": "engineering planning assumption",
            "effective_date": "2026-10-04",
            "provider": "AWS EC2 Linux reference",
            "region": "us-east-1",
            "availability_zone": None,
            "instance_reference": "g7e.2xlarge",
            "regular_usd": 3.0,
            "spot_usd": 1.2,
            "billing": (
                "continuous per-second proration; allocated process downtime billed; "
                "terminated replacement wait excluded"
            ),
            "network_storage_cost": "excluded unless provided",
        },
        "profile_limit": (
            "RTX PRO 6000 Blackwell Server Edition kernel profile; "
            "AWS server fidelity unmeasured"
        ),
    }
    if overrides:
        cfg.update(copy.deepcopy(overrides))
    overrides = overrides or {}
    fault_time = overrides.get("fault_time_ns", fault_time)
    if scenario != "healthy":
        if "fault_targets" in overrides:
            affected = list(overrides["fault_targets"])
        elif "fault_count" in overrides:
            affected = random.Random(seed + 11).sample(
                list(range(n)), min(n, overrides["fault_count"])
            )
        if "fault_time_ns" in overrides:
            fault_time = overrides["fault_time_ns"]
        fault.update(time_ns=fault_time, workers=affected)
        for key in ("availability_ns", "init_ns", "load_ns"):
            if key in overrides:
                fault[key] = overrides[key]
        cfg["faults"] = [copy.deepcopy(fault)] if affected else []
        if scenario == "staggered":
            cfg["faults"] = [
                {**fault, "workers": [w], "time_ns": fault_time + i * 5 * NS}
                for i, w in enumerate(affected)
            ]
    recovery = (
        max(120 * NS, overrides.get("availability_ns", 0))
        + overrides.get("init_ns", 5 * NS)
        + overrides.get("load_ns", 30 * NS)
        + overrides.get("discovery_ns", 5 * NS)
    )
    last_fault = max((f["time_ns"] for f in cfg["faults"]), default=fault_time)
    cfg["horizon_ns"] = max(horizon, last_fault + recovery + 10 * NS + 1)
    if "prices" in overrides:
        cfg["price_provenance"].update(
            source="user scenario override",
            regular_usd=cfg["prices"]["regular"],
            spot_usd=cfg["prices"]["spot"],
        )
    return cfg
