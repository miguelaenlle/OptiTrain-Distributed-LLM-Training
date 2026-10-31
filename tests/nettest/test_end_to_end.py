"""Cross-language smoke and negative contract: real router, proxy and client."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.nettest.config import ROOT, load
from orchestrator.nettest.runner import run

BIN = ROOT / ".fleet/nettest-bin"
pytestmark = pytest.mark.skipif(
    not all((BIN / name).is_file() for name in ("router", "loadgen", "optitrain-netfault")),
    reason="build all net-test executables first",
)


def experiment(tmp_path, scenario):
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "kind": "synthetic",
                "samples": [
                    {
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "first_output_ms": 20,
                        "gaps_ms": [10] * 3,
                    }
                ],
            }
        )
    )
    cfg = load(scenario=scenario)
    cfg.update(
        profile=str(profile),
        input_tokens=10,
        output_tokens=4,
        rps=30,
        output=str(tmp_path / "evidence"),
        phases={"healthy_s": 3, "fault_s": 3, "recovery_s": 3},
    )
    cfg["timeouts"].update(request_s=1, first_output_s=0.8, idle_s=0.8)
    cfg["criteria"].update(
        recovery_window_s=1, recovery_min_requests=20, recovery_latency_ratio=10, fault_gap_ratio=10
    )
    return cfg


def test_baseline_runs_all_components_and_retains_evidence(tmp_path):
    cfg = experiment(tmp_path, "baseline")
    assert run(cfg) == 0
    folder = Path(cfg["output"])
    report = json.loads((folder / "report.json").read_text())
    assert report["profile_kind"] == "synthetic"
    assert report["all_offered"] == 270
    assert report["all_requests"]["completed"] == 270
    assert report["verdict"] == "PASS"
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["topology"]["registered_endpoints"]
    assert manifest["artifacts"]["router"]["sha256"]


def test_strict_slo_fails_honestly_when_fault_delays_output(tmp_path):
    cfg = experiment(tmp_path, "worker-delay")
    # Require normal service during the fault. We expect this contract to FAIL;
    # no production threshold is relaxed to make the demonstration look good.
    cfg["criteria"]["fault_compliance_pct"] = 99
    assert run(cfg) == 1
    folder = Path(cfg["output"])
    report = json.loads((folder / "report.json").read_text())
    assert report["verdict"] == "FAIL" and not report["evidence_errors"]
    assert report["normal_slo_breached_during_fault"]
    assert (
        report["phases"]["fault"]["ttft_p95_ms"] >= report["phases"]["healthy"]["ttft_p95_ms"] + 200
    )
    assert report["all_requests"]["completed"] == report["all_offered"]
    assert (
        sum(p["offered"] for p in report["phases"].values()) + report["transition"]["offered"]
        == report["all_offered"]
    )
    assert "\x1b" not in (folder / "report.txt").read_text()
