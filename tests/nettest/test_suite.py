"""A real multi-scenario run and tamper-evident, deterministic offline replay."""

from __future__ import annotations

import json

import pytest

from orchestrator.nettest.config import ROOT, load
from orchestrator.nettest.suite import bundle, execute, regenerate

pytestmark = pytest.mark.skipif(
    not all(
        (ROOT / ".fleet/nettest-bin" / name).is_file()
        for name in ("router", "loadgen", "optitrain-netfault")
    ),
    reason="build all net-test executables first",
)


def test_suite_keeps_failures_continues_and_rebuilds_identically(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "kind": "synthetic",
                "samples": [
                    {
                        "input_tokens": 512,
                        "output_tokens": 16,
                        "first_output_ms": 5,
                        "gaps_ms": [1] * 15,
                    }
                ],
            }
        )
    )
    cfg = load(scenario="baseline")
    cfg.update(profile=str(profile), rps=30, phases={"healthy_s": 3, "fault_s": 3, "recovery_s": 3})
    cfg["timeouts"].update(request_s=1, first_output_s=0.8, idle_s=0.8)
    cfg["criteria"].update(
        recovery_window_s=1, recovery_min_requests=20, recovery_latency_ratio=10, fault_gap_ratio=10
    )
    root = tmp_path / "suite"
    assert execute(cfg, root, ["client-disconnect", "baseline"], skip_build=True) == 1
    summary = json.loads((root / "summary.json").read_text())
    assert [case["report"]["verdict"] for case in summary["cases"]] == ["FAIL", "PASS"]
    assert summary["cases"][0]["report"]["all_requests"]["completed"] < 270
    assert summary["cases"][1]["report"]["all_requests"]["completed"] == 270
    before = {
        name: (root / name).read_bytes()
        for name in ("report.md", "summary.json", "evidence.tar.gz")
    }
    assert regenerate(root) == 1
    bundle(root)
    assert before == {name: (root / name).read_bytes() for name in before}
    text = before["report.md"].decode()
    assert "**synthetic**" in text and "NOT RUN" not in text
    # Failed integrity verification must not replace the last valid report.
    requests = root / "01-client-disconnect/requests.jsonl"
    requests.write_bytes(requests.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed evidence"):
        regenerate(root)
    assert (root / "report.md").read_bytes() == before["report.md"]
