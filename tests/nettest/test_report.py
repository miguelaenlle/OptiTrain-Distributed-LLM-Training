from __future__ import annotations

import copy
import json

import pytest

from orchestrator.nettest.config import DEFAULT, load
from orchestrator.nettest.report import compliant, delta, evaluate, percentile, summarize


def row(identifier="r", complete=True, ttft=100, dispatch=0):
    return {
        "request_id": identifier,
        "complete": complete,
        "ttft_ms": ttft,
        "dispatch_s": dispatch,
        "latency_ms": 400,
        "gaps_ms": [20] * 15,
        "generator_drop": False,
    }


def test_all_offered_denominator_includes_failures_and_drops():
    rows = [row(), row("failed", False), {**row("drop", False), "generator_drop": True}]
    summary = summarize(rows, 1, DEFAULT["slo"])
    assert summary["offered"] == 3
    assert summary["compliant"] == 1
    assert summary["completion_pct"] == pytest.approx(100 / 3)
    assert summary["goodput_rps"] == 1


def test_every_gap_and_completion_required_for_slo():
    assert compliant(row(), DEFAULT["slo"])
    assert not compliant({**row(), "gaps_ms": [20, 101]}, DEFAULT["slo"])
    assert not compliant(row(complete=False), DEFAULT["slo"])
    assert not compliant(row(ttft=251), DEFAULT["slo"])


def test_nearest_rank_and_zero_reference_are_explicit():
    assert percentile(list(range(1, 101)), 0.95) == 95
    assert percentile([], 0.95) is None
    assert delta(1, 0) == "N/A"
    assert delta(66.75, 99.5, True) == "-32.75 pp"


def test_missing_attempt_evidence_invalidates_run():
    cfg = copy.deepcopy(DEFAULT)
    windows = {"healthy": (0, 1), "fault": (1, 2), "recovery": (2, 3)}
    result = evaluate(cfg, [row()], windows, [])
    assert result["verdict"] == "INVALID"
    assert "missing router attempt evidence" in result["evidence_errors"]


def test_slo_failure_is_not_hidden_by_permissive_fault_allowance():
    cfg = copy.deepcopy(DEFAULT)
    cfg["criteria"]["recovery_min_requests"] = 1
    cfg["criteria"]["recovery_window_s"] = 0.5
    rows = [row("h", dispatch=0), row("f", ttft=400, dispatch=1), row("r", dispatch=2)]
    attempts = [{"request_id": r["request_id"], "attempt": 1, "committed": True} for r in rows]
    result = evaluate(cfg, rows, {"healthy": (0, 1), "fault": (1, 2), "recovery": (2, 3)}, attempts)
    assert result["verdict"] == "FAIL"
    assert result["normal_slo_breached_during_fault"]
    assert result["all_requests"]["compliant"] == 2


@pytest.mark.parametrize(
    "override",
    [
        {"rps": 0},
        {"rps": float("nan")},
        {"workers": -1},
        {"unknown": 1},
        {"phases": {"healthy_s": -1}},
        {"fault": {"target": "worker-99"}},
    ],
)
def test_config_rejects_invalid_inputs(tmp_path, override):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(override))
    with pytest.raises(ValueError):
        load(path)
