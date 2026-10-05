"""Measure a fresh retained analytical run using immutable saved inputs."""

import argparse
import hashlib
import json
import platform
import resource
import time
from collections import defaultdict
from pathlib import Path

from fleetlab.engine import run


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True, help="Retained before-run directory")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    before_manifest = json.loads((args.inputs / "manifest.json").read_text())
    config = {
        k: v
        for k, v in before_manifest.items()
        if k not in ("provenance", "format_version", "exact_dynamics_reuse")
    }
    self_before = resource.getrusage(resource.RUSAGE_SELF)
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    metrics = run(config, args.output, retain=True, wall_timeout=1800)
    wall = time.perf_counter() - started
    self_after = resource.getrusage(resource.RUSAGE_SELF)
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    files = [p for p in args.output.rglob("*") if p.is_file()]
    sizes = defaultdict(int)
    for path in files:
        sizes[path.relative_to(args.output).parts[0]] += path.stat().st_size
    event_count = sum(1 for _ in (args.output / "events.jsonl").open())
    virtual_seconds = config["horizon_ns"] / 1e9
    before_metrics = json.loads((args.inputs / "metrics.json").read_text())
    report = {
        "scope": "One fresh retained analytical run, identical saved inputs",
        "offered_requests": len(config["requests"]),
        "wall_scope": "run() including frontend graph generation, backend, indexing and metrics",
        "cpu_scope": "Measured getrusage deltas around run(); self and completed child processes",
        "platform": platform.platform(),
        "wall_seconds": wall,
        "self_user_cpu_seconds": self_after.ru_utime - self_before.ru_utime,
        "self_system_cpu_seconds": self_after.ru_stime - self_before.ru_stime,
        "child_user_cpu_seconds": child_after.ru_utime - child_before.ru_utime,
        "child_system_cpu_seconds": child_after.ru_stime - child_before.ru_stime,
        "self_peak_rss_before_kib": self_before.ru_maxrss,
        "self_peak_rss_after_kib": self_after.ru_maxrss,
        "child_peak_rss_kib": child_after.ru_maxrss,
        "rss_scope": (
            "Linux KiB; process-lifetime self peak and maximum completed-child peak, "
            "not aggregate RSS"
        ),
        "journal_events": event_count,
        "journal_events_per_wall_second": event_count / wall,
        "virtual_duration_seconds": virtual_seconds,
        "virtual_seconds_per_wall_second": virtual_seconds / wall,
        "acceleration_label": (
            "Analytical virtual-clock throughput; not measured GPU or service speedup"
        ),
        "artifact_files": len(files),
        "artifact_bytes_before_benchmark_report": sum(sizes.values()),
        "artifact_bytes_by_top_level_path": dict(sizes),
        "saved_inputs_sha256": digest(config),
        "numerical_metrics_equal": metrics == before_metrics,
        "before_metrics_sha256": digest(before_metrics),
        "after_metrics_sha256": digest(metrics),
        "before_provenance": before_manifest["provenance"],
        "after_provenance": json.loads((args.output / "manifest.json").read_text())["provenance"],
    }
    (args.output / "performance.json").write_text(json.dumps(report, indent=2))
    assert report["numerical_metrics_equal"], "Saved-input numerical metrics changed"
    print(json.dumps({k: v for k, v in report.items() if "provenance" not in k}, indent=2))


if __name__ == "__main__":
    main()
