import copy
import json
import random

import pytest
from fleetlab.core import call
from fleetlab.engine import run, validate
from fleetlab.metrics import metrics as reference_metrics


def base():
    return {
        "regular": 1,
        "spot": 0,
        "policy": "round_robin",
        "prices": {"regular": 3, "spot": 1.2},
        "horizon_ns": 10**9,
        "faults": [],
        "discovery_ns": 5_000_000,
        "dispatch_error_ns": 5_000_000,
        "backoff_ns": 100_000,
        "report_period_ns": 1_000_000,
        "report_delay_ns": 5_000_000,
        "requests": [
            {
                "id": 0,
                "arrival_ns": 0,
                "prompt_length": 128,
                "output_length": 4,
                "prompt_tokens": list(range(128)),
                "output_tokens": list(range(4)),
            }
        ],
    }


def events(path):
    return [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]


def subset_equal(actual, expected):
    if isinstance(expected, dict):
        return all(k in actual and subset_equal(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            subset_equal(a, b) for a, b in zip(actual, expected)
        )
    return actual == expected


def execute(config, tmp_path, name):
    output = tmp_path / name
    result = run(config, output)
    summary = json.loads((output / "summary.json").read_text())
    reference = reference_metrics(summary, config)
    assert subset_equal(result, reference)
    return result, summary, events(output), output


def test_healthy_cost_tokens_replay(tmp_path):
    cfg = base()
    result, summary, journal, path = execute(cfg, tmp_path, "healthy")
    assert result["completed"] == 1 and result["delivered_tokens"] == 4
    assert abs(result["total_usd"] - 3 / 3600) < 1e-12
    token = next(e for e in journal if e["event"] == "token")
    view = call(
        {
            "op": "replay",
            "path": str(path / "events.jsonl"),
            "until_ns": token["time_ns"],
            "request": 0,
        }
    )
    assert view["requests"]["0"]["delivered"] == 1
    assert any(e["event"] == "token" for e in view["events"])
    assert all(e["time_ns"] <= token["time_ns"] for e in view["events"])
    assert any(e["event"] == "stats" and e["value"]["kv_bytes"] > 0 for e in journal)


def test_exact_tie_crash_wins_delayed_notification_and_all_down_recovery(tmp_path):
    cfg = base()
    _, _, healthy, _ = execute(cfg, tmp_path, "reference")
    first = next(e["time_ns"] for e in healthy if e["event"] == "token")
    cfg["faults"] = [
        {
            "time_ns": first,
            "workers": [0],
            "terminate": True,
            "availability_ns": 2_000_000,
            "init_ns": 1_000_000,
            "load_ns": 1_000_000,
        }
    ]
    result, summary, journal, _ = execute(cfg, tmp_path, "fault")
    assert not any(e["event"] == "token" and e["time_ns"] == first for e in journal)
    retry = next(e for e in journal if e["event"] == "request" and e["state"] == "RetryWait")
    assert retry["time_ns"] == first + cfg["discovery_ns"]
    assert result["completed"] == 1 and summary["requests"][0]["attempts"] == 2
    assert any(e["event"] == "cancelled" and e["callbacks_removed"] > 0 for e in journal)
    # terminated allocation does not bill its replacement wait.
    assert abs(result["total_usd"] - (1 - 0.002) * 3 / 3600) < 1e-12
    assert any(
        e["event"] == "stats" and e["incarnation"] == 1 and e["value"]["kv_bytes"] == 0
        for e in journal
    )


def test_interrupted_stream_no_retry(tmp_path):
    cfg = base()
    cfg["faults"] = [
        {
            "time_ns": 20_000_000,
            "workers": [0],
            "availability_ns": 2_000_000,
            "init_ns": 1_000_000,
            "load_ns": 1_000_000,
        }
    ]
    result, summary, journal, _ = execute(cfg, tmp_path, "stream")
    request = summary["requests"][0]
    assert request["state"] == "Failed" and request["delivered"] == 1 and request["attempts"] == 1
    assert request["finish_ns"] == 25_000_000
    assert result["compliant"] == 0 and result["completion_rate"] == 0
    assert not any(e["event"] == "token" and e["time_ns"] >= 20_000_000 for e in journal)


def test_duplicate_crash_and_restart_during_loading(tmp_path):
    cfg = base()
    cfg["faults"] = [
        {
            "time_ns": tick,
            "workers": [0],
            "availability_ns": 2_000_000,
            "init_ns": 2_000_000,
            "load_ns": 2_000_000,
        }
        for tick in (1_000_000, 1_500_000, 4_000_000)
    ]
    result, summary, journal, _ = execute(cfg, tmp_path, "recovery")
    assert sum(e["event"] == "duplicate_fault" for e in journal) == 1
    assert sum(e["event"] == "cancelled" for e in journal) == 2
    physical_ready = [
        e
        for e in journal
        if e["event"] == "worker" and e["state"] == "Ready" and e["incarnation"] > 0
    ]
    assert min(e["time_ns"] for e in physical_ready) == 10_000_000
    assert result["completed"] == 1


def test_horizon_censors_and_validation(tmp_path):
    cfg = base()
    cfg["horizon_ns"] = 1_000_000
    result, summary, _, _ = execute(cfg, tmp_path, "short")
    assert summary["requests"][0]["state"] == "Censored"
    assert summary["requests"][0]["finish_ns"] == cfg["horizon_ns"]
    assert result["compliant"] == 0
    cfg["requests"][0]["arrival_ns"] = cfg["horizon_ns"] + 1
    with pytest.raises(ValueError, match="horizon"):
        validate(cfg)


def test_candidate_counts_feasibility_and_ties():
    candidates = call({"op": "candidates"})
    assert len(candidates) == 304 and len({(c["regular"], c["spot"]) for c in candidates}) == 152
    rows = []
    for c in candidates:
        c["scenarios"] = {
            "healthy": {"feasible": True, "total_usd": 1, "conditional_ttft_p95_ns": 10}
        }
        rows.append(c)
    ranked = call({"op": "rank", "suite": ["healthy"], "results": rows})
    assert ranked["winner"]["regular"] == 0 and ranked["winner"]["spot"] == 1
    assert ranked["winner"]["policy"] == "least_work"
    assert (
        call({"op": "rank", "suite": ["healthy", "mandatory_failure"], "results": rows})["winner"]
        is None
    )


def test_randomized_cpp_metric_parity():
    rng = random.Random(44)
    for _ in range(100):
        cfg = base()
        rows = []
        for i in range(rng.randrange(1, 40)):
            rows.append(
                {
                    "id": i,
                    "state": rng.choice(
                        ["Completed", "Failed", "TimedOut", "Rejected", "Censored"]
                    ),
                    "arrival_ns": 0,
                    "finish_ns": rng.randrange(100 * 10**9),
                    "ttft_ns": rng.randrange(2 * 10**9),
                    "delivered": rng.randrange(1, 64),
                    "itl_ns": [rng.randrange(200_000_000) for _ in range(rng.randrange(10))],
                }
            )
        summary = {"requests": rows, "total_usd": rng.random()}
        actual = call({"op": "metrics", "summary": summary, "config": cfg})
        assert all(actual[k] == v for k, v in reference_metrics(summary, cfg).items())


def test_monotone_pruning_matches_full_run(tmp_path):
    cfg = base()
    cfg["horizon_ns"] = 3 * 10**9
    cfg["faults"] = [{"time_ns": 1_000_000, "workers": [0], "availability_ns": 60 * 10**9}]
    full, _, _, _ = execute(cfg, tmp_path, "full")
    cfg["optimizer_prune"] = True
    result = run(cfg, tmp_path / "pruned")
    assert not full["feasible"] and not result["feasible"]
    assert result["complete_metrics"] is False and "total_usd" not in result
    certificate = result["certificate"]
    assert certificate["maximum_possible_compliance"] < 0.95
    assert certificate["time_ns"] < cfg["horizon_ns"]


def test_colocated_node_failure_and_seeded_hazards(tmp_path):
    cfg = base()
    cfg["regular"] = 2
    cfg["placement"] = [0, 0]
    cfg["faults"] = [
        {
            "time_ns": 1_000_000,
            "workers": [0],
            "scope": "node",
            "availability_ns": 2_000_000,
            "init_ns": 1_000_000,
            "load_ns": 1_000_000,
        }
    ]
    result, _, journal, _ = execute(cfg, tmp_path, "node")
    assert {e["worker"] for e in journal if e["event"] == "cancelled"} == {0, 1}
    cfg = base()
    cfg["regular"] = 0
    cfg["spot"] = 1
    cfg.update(
        stochastic_failures=True,
        spot_mean_active_hours=0.000001,
        availability_ns=2_000_000,
        init_ns=1_000_000,
        load_ns=1_000_000,
    )
    a = run(cfg, tmp_path / "hazard-a")
    b = run(cfg, tmp_path / "hazard-b")
    assert a == b
    assert any(e["event"] == "assumed_hazard_fault" for e in events(tmp_path / "hazard-a"))


def test_common_horizon_and_analytic_baseline():
    from fleetlab.scenarios import SUITE, experiment, trace

    requests = trace(64, 42, 100)
    horizons = {
        experiment(regular, 16 - regular, scenario=s, requests=requests)["horizon_ns"]
        for regular in (0, 8, 16)
        for s in SUITE
    }
    assert len(horizons) == 1
    baseline = call(
        {
            "op": "analytic",
            "q": 10,
            "rate": 64,
            "horizon_seconds": 100,
            "regular": 8,
            "spot": 8,
            "failures": 8,
        }
    )
    assert baseline["survivor_capacity"] == 80 and baseline["failure_headroom"] == 16
    assert baseline["backlog_requests"] == 0


def test_zero_delay_delivery_identity_and_edited_slos_prices(tmp_path):
    cfg = base()
    cfg.update(discovery_ns=0, report_delay_ns=0, backoff_ns=0, delivery_delay_ns=0)
    result, _, journal, _ = execute(cfg, tmp_path, "zero")
    assert result["feasible"]
    assert all(
        "batch" in e and "attempt_id" in e and "incarnation" in e
        for e in journal
        if e["event"] == "token"
    )
    cfg.update(delivery_delay_ns=30_000_000, ttft_limit_ns=1, prices={"regular": 6, "spot": 2.4})
    result, _, journal, _ = execute(cfg, tmp_path, "edited")
    assert result["completed"] == 1 and result["compliant"] == 0
    assert abs(result["total_usd"] - 6 / 3600) < 1e-12
    assert result["conditional_ttft_p95_ns"] >= 30_000_000


def test_active_child_wall_time_interrupt(tmp_path):
    cfg = base()
    with pytest.raises(TimeoutError, match="active simulator"):
        run(cfg, tmp_path / "wall-time", wall_timeout=0.00001)
    assert not (tmp_path / "wall-time" / "metrics.json").exists()


def test_exact_dynamics_reuse_rebilling_matches_real_execution(tmp_path):
    from fleetlab.cli import dynamics_key, reuse

    cfg = base()
    cfg["regular"] = 2
    cfg["faults"] = [
        {
            "time_ns": 1_000_000,
            "workers": [0],
            "availability_ns": 2_000_000,
            "init_ns": 1_000_000,
            "load_ns": 1_000_000,
        }
    ]
    cfg["scenario"] = "half"
    a = run(cfg, tmp_path / "representative")
    different = copy.deepcopy(cfg)
    different["regular"] = 0
    different["spot"] = 2
    actual = run(different, tmp_path / "actual")
    assert dynamics_key(cfg) == dynamics_key(different)
    cached = reuse(different, tmp_path / "representative", tmp_path / "reused", dynamics_key(cfg))
    assert actual == cached and actual["total_usd"] < a["total_usd"]
    different["stochastic_failures"] = True
    assert dynamics_key(different) is None


def test_partial_cache_certificates_zero_prices_and_negative_keys(tmp_path):
    from fleetlab.cli import dynamics_key, reuse

    cfg = base()
    cfg.update(scenario="half", horizon_ns=3 * 10**9, optimizer_prune=True)
    cfg["faults"] = [{"time_ns": 1_000_000, "workers": [0], "availability_ns": 60 * 10**9}]
    original = run(cfg, tmp_path / "partial-representative")
    for i, price in enumerate((0, 6)):
        changed = copy.deepcopy(cfg)
        changed["prices"] = {"regular": price, "spot": price}
        changed["regular"] = 0
        changed["spot"] = 1
        actual = run(changed, tmp_path / f"partial-actual-{i}")
        cached = reuse(
            changed,
            tmp_path / "partial-representative",
            tmp_path / f"partial-cache-{i}",
            dynamics_key(cfg),
        )
        assert actual == cached and not cached["complete_metrics"]
        assert cached["certificate"] == original["certificate"] and "total_usd" not in cached
        if price == 0:
            assert cached["accrued_usd"] == 0
    changed = copy.deepcopy(cfg)
    changed["discovery_ns"] += 1
    assert dynamics_key(changed) != dynamics_key(cfg)
    changed["scenario"] = "single_spot"
    assert dynamics_key(changed) is None
    with pytest.raises(ValueError, match="does not match"):
        reuse(
            changed,
            tmp_path / "partial-representative",
            tmp_path / "invalid-cache",
            dynamics_key(cfg),
        )


def test_explicit_session_minimum_billing(tmp_path):
    cfg = base()
    cfg["minimum_billing_ns"] = 60 * 10**9
    cfg["faults"] = [
        {
            "time_ns": 1_000_000,
            "workers": [0],
            "terminate": True,
            "availability_ns": 2_000_000,
            "init_ns": 1_000_000,
            "load_ns": 1_000_000,
        }
    ]
    result, summary, _, _ = execute(cfg, tmp_path, "minimum")
    assert abs(result["total_usd"] - 0.1) < 1e-12
    assert summary["worker_costs"][0]["billed_ns"] == 120 * 10**9
    assert summary["worker_costs"][0]["allocated_ns"] == 998_000_000
