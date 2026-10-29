"""All-offered accounting and a frozen, explicit acceptance contract."""

from __future__ import annotations

import math
import os


def percentile(values, q):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * q) - 1)] if values else None


def compliant(row, slo):
    return (
        row.get("complete", False)
        and row.get("ttft_ms") is not None
        and row["ttft_ms"] <= slo["ttft_ms"]
        and row["latency_ms"] <= slo["total_ms"]
        and all(g <= slo["gap_ms"] for g in row.get("gaps_ms", []))
    )


def summarize(rows, duration, slo):
    offered = len(rows)
    completed = sum(bool(r.get("complete")) for r in rows)
    good = sum(compliant(r, slo) for r in rows)
    return {
        "offered": offered,
        "completed": completed,
        "compliant": good,
        "completion_pct": 100 * completed / offered if offered else None,
        "compliance_pct": 100 * good / offered if offered else None,
        "goodput_rps": good / duration if duration > 0 else None,
        "ttft_p95_ms": percentile(
            [r["ttft_ms"] for r in rows if r.get("ttft_ms") is not None], 0.95
        ),
        "gap_p99_ms": percentile([g for r in rows for g in r.get("gaps_ms", [])], 0.99),
        "latency_p95_ms": percentile(
            [r["latency_ms"] for r in rows if not r.get("generator_drop")], 0.95
        ),
        "drops": sum(bool(r.get("generator_drop")) for r in rows),
    }


def evaluate(cfg, rows, windows, attempts, evidence_errors=()):
    slo, criteria = cfg["slo"], cfg["criteria"]
    stats = {
        name: summarize([r for r in rows if lo <= r["dispatch_s"] < hi], hi - lo, slo)
        for name, (lo, hi) in windows.items()
    }
    checks = []

    def check(name, actual, limit, operator="ge"):
        passed = actual is not None and (actual >= limit if operator == "ge" else actual <= limit)
        checks.append(
            {
                "name": name,
                "actual": actual,
                "operator": operator,
                "limit": limit,
                "verdict": "PASS" if passed else "FAIL",
            }
        )

    healthy, fault, recovery = (stats[n] for n in ("healthy", "fault", "recovery"))
    for name, phase in stats.items():
        check(f"{name}: completion %", phase["completion_pct"], criteria["completion_pct"])
        check(f"{name}: offered requests", phase["offered"], 1)
    check(
        "healthy: normal-SLO compliance %",
        healthy["compliance_pct"],
        criteria["healthy_compliance_pct"],
    )
    check(
        "fault: normal-SLO compliance %", fault["compliance_pct"], criteria["fault_compliance_pct"]
    )
    drop = (
        healthy["compliance_pct"] - fault["compliance_pct"]
        if healthy["compliance_pct"] is not None and fault["compliance_pct"] is not None
        else None
    )
    check("fault: compliance drop (pp)", drop, criteria["fault_max_drop_pp"], "le")
    check(
        "fault: goodput",
        fault["goodput_rps"],
        (healthy["goodput_rps"] or 0) * criteria["fault_goodput_ratio"],
    )
    check("fault: TTFT p95 ms", fault["ttft_p95_ms"], criteria["fault_ttft_ms"], "le")
    added = (
        fault["ttft_p95_ms"] - healthy["ttft_p95_ms"]
        if fault["ttft_p95_ms"] is not None and healthy["ttft_p95_ms"] is not None
        else None
    )
    check("fault: added TTFT p95 ms", added, criteria["fault_added_ttft_ms"], "le")
    check("fault: gap p99 ms", fault["gap_p99_ms"], criteria["fault_gap_ms"], "le")
    check(
        "fault: gap versus healthy",
        fault["gap_p99_ms"],
        (healthy["gap_p99_ms"] or 0) * criteria["fault_gap_ratio"],
        "le",
    )
    check(
        "recovery: normal-SLO compliance %",
        recovery["compliance_pct"],
        criteria["recovery_compliance_pct"],
    )
    check(
        "recovery: goodput",
        recovery["goodput_rps"],
        (healthy["goodput_rps"] or 0) * criteria["recovery_goodput_ratio"],
    )
    for metric in ("ttft_p95_ms", "gap_p99_ms"):
        check(
            f"recovery: {metric}",
            recovery[metric],
            (healthy[metric] or 0) * criteria["recovery_latency_ratio"],
            "le",
        )
    start, end = windows["recovery"]
    restoration = None
    window = criteria["recovery_window_s"]
    candidate = 0.0
    while start + candidate + window <= end + 1e-9:
        subset = [
            r for r in rows if start + candidate <= r["dispatch_s"] < start + candidate + window
        ]
        measured = summarize(subset, window, slo)
        if (
            len(subset) >= criteria["recovery_min_requests"]
            and measured["compliance_pct"] >= criteria["recovery_compliance_pct"]
            and measured["completion_pct"] >= criteria["completion_pct"]
            and measured["ttft_p95_ms"] is not None
            and healthy["ttft_p95_ms"] is not None
            and measured["ttft_p95_ms"]
            <= healthy["ttft_p95_ms"] * criteria["recovery_latency_ratio"]
            and measured["gap_p99_ms"] is not None
            and healthy["gap_p99_ms"] is not None
            and measured["gap_p99_ms"] <= healthy["gap_p99_ms"] * criteria["recovery_latency_ratio"]
        ):
            restoration = {
                "window_start_s": candidate,
                "confirmed_s": candidate + window,
                "requests": len(subset),
            }
            break
        candidate += 0.25
    check(
        "recovery: qualifying window starts within seconds",
        restoration["window_start_s"] if restoration else None,
        criteria["recovery_start_s"],
        "le",
    )
    check("generator drops", sum(r.get("generator_drop", False) for r in rows), 0, "le")
    check(
        "request deadline overruns",
        sum(r.get("latency_ms", 0) > cfg["timeouts"]["request_s"] * 1000 + 100 for r in rows),
        0,
        "le",
    )
    by_id = {}
    for attempt in attempts:
        by_id.setdefault(attempt["request_id"], []).append(attempt)
    invalid_retries = 0
    for group in by_id.values():
        group.sort(key=lambda a: a["attempt"])
        invalid_retries += any(a["committed"] for a in group[:-1])
    check("maximum attempts per request", max((len(v) for v in by_id.values()), default=0), 3, "le")
    check("retry after committed output", invalid_retries, 0, "le")
    covered = {
        r["request_id"]
        for r in rows
        if any(lo <= r["dispatch_s"] < hi for lo, hi in windows.values())
    }
    transitional = [r for r in rows if r["request_id"] not in covered]
    errors = list(evidence_errors)
    dispatched = [r for r in rows if not r.get("generator_drop")]
    if not rows:
        errors.append("no client request evidence")
    if len({r["request_id"] for r in rows}) != len(rows):
        errors.append("duplicate client request IDs")
    if any(
        r["request_id"] not in by_id
        for r in dispatched
        if r.get("complete") or r.get("output_events", 0) > 0
    ):
        errors.append("missing router attempt evidence")
    verdict = (
        "INVALID" if errors else "PASS" if all(c["verdict"] == "PASS" for c in checks) else "FAIL"
    )
    return {
        "verdict": verdict,
        "evidence_errors": errors,
        "phases": stats,
        "windows": windows,
        "transition": summarize(transitional, 0, slo),
        "all_offered": len(rows),
        "all_requests": summarize(rows, max(hi for lo, hi in windows.values()), slo),
        "recovery": restoration,
        "checks": checks,
        "normal_slo_breached_during_fault": any(
            not compliant(r, slo)
            for r in rows
            if windows["fault"][0] <= r["dispatch_s"] < windows["fault"][1]
        ),
        "latency_population": (
            "TTFT among requests with output; gaps among received "
            "output events, including interrupted requests"
        ),
        "percentile_method": "nearest-rank",
        "attempts": len(attempts),
    }


def delta(value, reference, rate=False):
    if value is None or reference is None:
        return "N/A"
    if rate:
        return f"{value - reference:+.2f} pp"
    return f"{100 * (value / reference - 1):+.1f}%" if reference else "N/A"


def render(report, cfg, color=False):
    colored = color and "NO_COLOR" not in os.environ
    colors = {"PASS": "\033[32m", "FAIL": "\033[31m", "INVALID": "\033[31m", "WARN": "\033[33m"}

    def label(status):
        text = f"[{status}]"
        return f"{colors[status]}{text}\033[0m" if colored else text

    lines = [
        f"Execution: {report['verdict']} | backend: {cfg['backend']} | scenario: {cfg['scenario']}",
        f"Normal SLO: TTFT <= {cfg['slo']['ttft_ms']} ms; "
        f"every gap <= {cfg['slo']['gap_ms']} ms; total <= {cfg['slo']['total_ms']} ms",
        "Healthy reference excludes the declared pre-fault deadline guard; "
        "boundary requests remain in transition/all-offered totals.",
        "Metric                     Healthy            Fault (+change)       Recovery (+change)",
    ]
    healthy = report["phases"]["healthy"]
    for key in (
        "offered",
        "completion_pct",
        "compliance_pct",
        "ttft_p95_ms",
        "gap_p99_ms",
        "goodput_rps",
    ):
        values = []
        for phase in ("healthy", "fault", "recovery"):
            value = report["phases"][phase][key]
            shown = f"{value:.2f}" if isinstance(value, (float, int)) else "N/A"
            if phase != "healthy" and key != "offered":
                shown += f" ({delta(value, healthy[key], key.endswith('_pct'))})"
            values.append(shown)
        lines.append(f"{key:25} {values[0]:18} {values[1]:22} {values[2]}")
    if report["recovery"]:
        recovery = report["recovery"]
        lines.append(
            f"Recovery qualification: window starts +{recovery['window_start_s']:.2f}s "
            f"after removal, confirmed +{recovery['confirmed_s']:.2f}s "
            f"({recovery['requests']} requests). This is a stability check, not node restart time."
        )
    if report["normal_slo_breached_during_fault"]:
        lines.append(
            f"{label('WARN')} Normal SLO breached during fault; "
            "fault allowance is evaluated separately."
        )
    for check in report["checks"]:
        op = ">=" if check["operator"] == "ge" else "<="
        lines.append(
            f"{label(check['verdict'])} {check['name']}: {check['actual']} {op} {check['limit']}"
        )
    for error in report["evidence_errors"]:
        lines.append(f"{label('INVALID')} {error}")
    lines.append(
        f"{label(report['verdict'])} Overall result. "
        f"Requests in control-transition bounds: {report['transition']['offered']}"
    )
    return "\n".join(lines) + "\n"
