"""All-offered SLO denominators and explicitly conditional latency metrics."""

import math


def percentile(values, p=0.95):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def cohort(rows, config):
    completed = [r for r in rows if r["state"] == "Completed"]
    ttft_limit = config.get("ttft_limit_ns", 10**9)
    itl_limit = config.get("itl_limit_ns", 100_000_000)
    compliant = [
        r
        for r in completed
        if r["finish_ns"] <= r["arrival_ns"] + config.get("deadline_ns", 60 * 10**9)
        and r["ttft_ns"] <= ttft_limit
        and all(g <= itl_limit for g in r["itl_ns"])
    ]
    ttfts = [r["ttft_ns"] for r in completed]
    gaps = [g for r in completed for g in r["itl_ns"]]
    denominator = len(rows)
    return {
        "offered": denominator,
        "completed": len(completed),
        "compliant": len(compliant),
        "completion_rate": len(completed) / denominator if denominator else None,
        "compliance_rate": len(compliant) / denominator if denominator else None,
        "conditional_ttft_p95_ns": percentile(ttfts),
        "conditional_itl_p95_ns": percentile(gaps),
        "ttft_sample_count": len(ttfts),
        "itl_sample_count": len(gaps),
        "outcomes": {
            state: sum(r["state"] == state for r in rows)
            for state in sorted(set(r["state"] for r in rows))
        },
    }


def metrics(summary, config):
    result = cohort(summary["requests"], config)
    result["total_usd"] = summary["total_usd"]
    result["usd_per_completed_request"] = (
        summary["total_usd"] / result["completed"] if result["completed"] else None
    )
    result["usd_per_compliant_request"] = (
        summary["total_usd"] / result["compliant"] if result["compliant"] else None
    )
    result["delivered_tokens"] = sum(r["delivered"] for r in summary["requests"])
    result["usd_per_delivered_token"] = (
        summary["total_usd"] / result["delivered_tokens"] if result["delivered_tokens"] else None
    )
    result["feasible"] = (
        result["completion_rate"] is not None
        and result["completion_rate"] >= config.get("completion_threshold", 0.99)
        and result["compliance_rate"] >= config.get("compliance_threshold", 0.95)
        and result["conditional_ttft_p95_ns"] is not None
        and result["conditional_ttft_p95_ns"] <= config.get("ttft_limit_ns", 10**9)
        and (
            result["conditional_itl_p95_ns"] is None
            or result["conditional_itl_p95_ns"] <= config.get("itl_limit_ns", 100_000_000)
        )
    )
    result["incident_cohorts"] = []
    for fault in config["faults"]:
        start = fault["time_ns"]
        end = min(
            config["horizon_ns"],
            start
            + fault.get("availability_ns", 60 * 10**9)
            + fault.get("init_ns", 5 * 10**9)
            + fault.get("load_ns", 30 * 10**9)
            + config.get("discovery_ns", 5 * 10**9)
            + 120 * 10**9,
        )
        result["incident_cohorts"].append(
            {
                "start_ns": start,
                "end_ns": end,
                **cohort(
                    [r for r in summary["requests"] if start <= r["arrival_ns"] <= end], config
                ),
            }
        )
    return result
