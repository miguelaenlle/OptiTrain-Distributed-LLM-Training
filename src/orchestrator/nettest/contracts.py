"""Versioned resilience expectations. Budgets use inputs, never observed percentiles."""

from __future__ import annotations

import json
import math

from .report import summarize


def read_evidence(folder):
    def read(name, lines=False):
        path = folder / name
        if not path.exists():
            return [] if lines else {}
        return (
            [json.loads(line) for line in path.read_text().splitlines() if line]
            if lines
            else json.loads(path.read_text())
        )

    return {
        "contract": read("contract.json"),
        "drained": read("drained.json"),
        "worker_events": read("worker-events.jsonl", True),
        "proxy_events": read("worker-proxy-events.jsonl", True),
    }


def plan(cfg, profile):
    sample = next(
        row
        for row in profile["samples"]
        if (row["input_tokens"], row["output_tokens"])
        == (cfg["input_tokens"], cfg["output_tokens"])
    )
    # The replay fixture emits one 'x' per event, a bounded generated ID and
    # model='timing-replay'. Include generous HTTP header/chunk-framing space.
    frame_bytes, overhead_bytes = 256, 1024
    budget = {
        "version": 2,
        "scenario": cfg["scenario"],
        "cancellation_ms": 500,
        "deadline_slack_ms": 100,
        "quiet_ms": 150 if cfg["scenario"] == "idle-expiry" else 0,
        "frame_bytes_bound": frame_bytes,
        "body_bytes_bound": frame_bytes * cfg["output_tokens"] + len("data: [DONE]\n\n"),
        "http_overhead_bytes_bound": overhead_bytes,
        "fault_slo": dict(cfg["slo"]),
        "budget_basis": "Normal contract; attributable upstream failures may fail over.",
    }
    fault = cfg["fault"] or {}
    if cfg["scenario"] == "client-jitter":
        maximum_delay = fault["delay_ms"] + fault.get("jitter_ms", 0)
        gap = math.ceil((max(sample["gaps_ms"], default=0) + maximum_delay + 25) / 50) * 50
        budget["fault_slo"]["gap_ms"] = max(cfg["slo"]["gap_ms"], gap)
        budget["budget_basis"] = (
            "Maximum injected delay + profiled gap + 25ms scheduling allowance, rounded up to 50ms."
        )
    elif cfg["scenario"] == "slow-client-download":
        rate = fault["rate_bytes_s"]
        quantum = max(1, rate // 20)
        # Proxy writes slices <= rate/20, then waits n/rate; a frame can span
        # slices. The extra quantum accounts for its starting position.
        gap = max(sample["gaps_ms"], default=0) + 1000 * (frame_bytes + quantum) / rate + 50
        total = (
            sample["first_output_ms"]
            + sum(sample["gaps_ms"])
            + 1000 * (budget["body_bytes_bound"] + overhead_bytes) / rate
            + 100
        )
        first = (
            sample["first_output_ms"] + 1000 * (overhead_bytes + frame_bytes + quantum) / rate + 50
        )
        budget["fault_slo"] = {
            "ttft_ms": math.ceil(first / 50) * 50,
            "gap_ms": math.ceil(gap / 50) * 50,
            "total_ms": math.ceil(total / 50) * 50,
        }
        budget["budget_basis"] = (
            "Bounded SSE frame/HTTP bytes divided by configured bandwidth, replay timing, "
            "one pacing quantum and declared 50/100ms scheduling allowances."
        )
    return budget


def apply(report, cfg, rows, attempts, evidence):
    """Retain legacy SLO findings; separately evaluate achievable fault behavior."""
    original = report["checks"]
    report["legacy_contract_verdict"] = report["verdict"]
    report["legacy_checks"] = original
    report["serving_slo_verdict"] = (
        "PASS"
        if all(
            phase["compliance_pct"] is not None and phase["compliance_pct"] >= 99
            for phase in report["phases"].values()
        )
        else "FAIL"
    )
    report["contract_version"] = 2
    report["contract"] = evidence.get("contract", {})
    scenario = cfg["scenario"]
    special = {
        "client-disconnect",
        "client-jitter",
        "slow-client-download",
        "slow-client-upload",
        "fleet-stall",
        "worker-truncate",
        "idle-expiry",
    }
    checks = [
        c
        for c in original
        if not (
            scenario in special
            and c["name"].startswith("fault:")
            and c["name"] != "fault: offered requests"
        )
    ]

    def check(name, passed, actual=None, limit=None):
        checks.append(
            {
                "name": name,
                "actual": actual if actual is not None else passed,
                "operator": "is",
                "limit": limit if limit is not None else True,
                "verdict": "PASS" if passed else "FAIL",
            }
        )

    budget = evidence.get("contract", {})
    check("resilience: frozen contract present", budget.get("version") == 2)
    lo, hi = report["windows"]["fault"]
    fault_rows = [r for r in rows if lo <= r["dispatch_s"] < hi]
    worker_events = evidence.get("worker_events", [])
    started = {
        (e["request_id"], e["worker"]) for e in worker_events if e["event"] == "generation_started"
    }
    ended = {
        (e["request_id"], e["worker"])
        for e in worker_events
        if e["event"] in ("generation_completed", "generation_canceled")
    }
    check(
        "resilience: every started generation terminated",
        bool(started) and started <= ended,
        len(started - ended),
        0,
    )
    check(
        "resilience: complete streams contain requested outputs",
        all(not r.get("complete") or r.get("output_events") == cfg["output_tokens"] for r in rows),
    )
    snapshot = evidence.get("drained", {})
    check(
        "resilience: router drained",
        snapshot.get("router_inflight") == 0,
        snapshot.get("router_inflight"),
        0,
    )
    check(
        "resilience: worker queues drained",
        snapshot.get("worker_outstanding") == 0,
        snapshot.get("worker_outstanding"),
        0,
    )
    check(
        "resilience: proxy connections drained",
        snapshot.get("proxy_connections") == 0,
        snapshot.get("proxy_connections"),
        0,
    )
    if scenario in ("client-jitter", "slow-client-download") and budget.get("fault_slo"):
        degraded = summarize(fault_rows, hi - lo, budget["fault_slo"])
        report["degraded_fault"] = degraded
        check(
            "resilience: degraded-contract completion >=99%",
            (degraded["completion_pct"] or 0) >= 99,
            degraded["completion_pct"],
            99,
        )
        check(
            "resilience: degraded-contract compliance >=99%",
            (degraded["compliance_pct"] or 0) >= 99,
            degraded["compliance_pct"],
            99,
        )
        check(
            "resilience: replay body fits declared wire budget",
            all(
                r.get("response_bytes", float("inf")) <= budget["body_bytes_bound"]
                for r in fault_rows
            ),
        )
    if scenario == "client-disconnect":
        canceled = [
            r
            for r in fault_rows
            if r.get("error") == "client_canceled"
            and r.get("output_events") == 5
            and not r.get("complete")
        ]
        check(
            "resilience: every fault request intentionally canceled",
            bool(fault_rows) and len(canceled) == len(fault_rows),
            len(canceled),
            len(fault_rows),
        )
        terminals = {
            e["request_id"]: e for e in worker_events if e["event"] == "generation_canceled"
        }
        delays = []
        for r in canceled:
            terminal = terminals.get(r["request_id"])
            delays.append(
                terminal["unix_ms"] - (r["unix_ns"] / 1e6 + r["latency_ms"]) if terminal else None
            )
        check(
            "resilience: cancellation reaches worker within 500ms",
            bool(delays) and all(d is not None and -5 <= d <= 500 for d in delays),
            max((d for d in delays if d is not None), default=None),
            500,
        )
        by_id = {r["request_id"]: r for r in canceled}
        check(
            "resilience: no generation after cancellation acknowledgment",
            not any(
                e["event"] == "output_produced"
                and e["request_id"] in by_id
                and e["monotonic_ms"] > terminals[e["request_id"]]["monotonic_ms"]
                for e in worker_events
                if e.get("request_id") in terminals
            ),
        )
    if scenario in ("fleet-stall", "slow-client-upload"):
        check(
            "resilience: outage exercised request failures",
            any(not r.get("complete") for r in fault_rows),
        )
        check(
            "resilience: failures are explicit",
            all(r.get("complete") or bool(r.get("error")) for r in fault_rows),
        )
    if scenario in ("worker-truncate", "idle-expiry"):
        reason = "byte_cutoff" if scenario == "worker-truncate" else "idle_expiry"
        closes = [e for e in evidence.get("proxy_events", []) if e.get("reason") == reason]
        check("resilience: fault actually closed a connection", bool(closes), len(closes), ">=1")
        check(
            "resilience: interrupted stream explicitly reported",
            any(
                not r.get("complete")
                and r.get("output_events", 0) > 0
                and "incomplete_stream" in r.get("error", "")
                for r in fault_rows
            ),
        )
        if scenario == "worker-truncate" and cfg.get("routing", {}).get("failure_threshold", 0):
            check(
                "resilience: repeated upstream failures quarantined",
                any(a.get("breaker_open") for a in attempts),
            )
    report["checks"] = checks
    report["verdict"] = (
        "INVALID"
        if report["evidence_errors"]
        else "PASS"
        if all(c["verdict"] == "PASS" for c in checks)
        else "FAIL"
    )
    return report
