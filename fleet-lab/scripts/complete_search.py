"""Explicit bounded continuation of an immutable search, preserving its status.

The primary batch remains incomplete if it exhausted its budget. This harness
reports a supplemental budget and combined coverage separately; it never raises
the primary cap or changes the selected suite, thresholds, workload or provenance.
"""

import argparse
import json
import resource
import time
from pathlib import Path

from fleetlab.adapter import provenance
from fleetlab.cli import dump, dynamics_key, reuse
from fleetlab.core import call
from fleetlab.engine import run, validate
from fleetlab.scenarios import experiment, trace


def read(path):
    return json.loads(path.read_text())


def storage_bytes(path):
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def complete(source, root, wall_seconds=1800, max_runs=3200, storage_gib=20):
    started = time.monotonic()
    source = source.resolve()
    root = root.resolve()
    manifest = read(source / "search-manifest.json")
    primary = read(source / "search.json")
    if provenance() != manifest["provenance"]:
        raise ValueError("Continuation requires the exact original engine/profile provenance")
    root.mkdir(parents=True, exist_ok=False)
    dump(
        root / "continuation-manifest.json",
        {
            "source": str(source),
            "original_search_manifest": manifest,
            "wall_seconds": wall_seconds,
            "max_combined_runs": max_runs,
            "max_combined_storage_gib": storage_gib,
        },
    )
    completed = read(source / "results.json")
    finished = set()
    for item in completed:
        for scenario, metrics in item["scenarios"].items():
            name = f"r{item['regular']}-s{item['spot']}-{item['policy']}-{scenario}"
            path = source / name
            if (
                read(path / "manifest.json")["provenance"] != manifest["provenance"]
                or not read(path / "verification.json").get("provenance_unchanged")
                or read(path / "metrics.json") != metrics
            ):
                raise ValueError(f"Primary evidence verification failed: {name}")
        finished.add((item["regular"], item["spot"], item["policy"]))
    cache = {}
    for path in source.iterdir():
        if not path.is_dir() or not (path / "verification.json").exists():
            continue
        config = {
            k: v
            for k, v in read(path / "manifest.json").items()
            if k not in ("provenance", "format_version", "exact_dynamics_reuse")
        }
        key = dynamics_key(config)
        if key is not None:
            cache.setdefault(key, path)
    base_bytes = storage_bytes(source)
    runs = physical = reused = 0
    reason = None
    invalid = []
    name = None
    for candidate in manifest["candidates"]:
        if (candidate["regular"], candidate["spot"], candidate["policy"]) in finished:
            continue
        item = {**candidate, "scenarios": {}}
        for scenario in manifest["suite"]:
            if time.monotonic() - started >= wall_seconds:
                reason = "supplemental wall-time cap"
                break
            if primary["runs"] + runs >= max_runs:
                reason = "combined run cap"
                break
            if base_bytes + storage_bytes(root) > storage_gib * 1024**3:
                reason = "combined storage cap"
                break
            if provenance() != manifest["provenance"]:
                reason = "engine or profile changed"
                break
            rate = (0.75 if scenario == "half_high" else 0.4) * 16 * manifest["q"]
            config = experiment(
                **candidate,
                scenario="half" if scenario == "half_high" else scenario,
                requests=trace(rate, manifest["seed"], manifest["count"]),
                seed=manifest["seed"],
                overrides=manifest["overrides"],
            )
            config.update(
                horizon_ns=manifest["shared_horizon_ns"],
                calibration_q=manifest["q"],
                synthetic_rate_per_second=rate,
                optimizer_prune=True,
            )
            name = f"r{candidate['regular']}-s{candidate['spot']}-{candidate['policy']}-{scenario}"
            try:
                validate(config)
                key = dynamics_key(config)
                if key is not None and key in cache:
                    metrics = reuse(config, cache[key], root / name, key)
                    reused += 1
                else:
                    metrics = run(
                        config,
                        root / name,
                        retain=False,
                        wall_timeout=wall_seconds - (time.monotonic() - started),
                    )
                    physical += 1
                    if key is not None:
                        cache[key] = root / name
            except TimeoutError:
                reason = "supplemental wall-time cap interrupted active simulator"
                break
            except Exception as exc:
                reason = "invalid evaluation; no infeasibility certificate"
                invalid.append({"candidate": candidate, "scenario": scenario, "error": str(exc)})
                break
            item["scenarios"][scenario] = metrics
            runs += 1
            if not metrics["feasible"]:
                item["skipped_scenarios"] = [
                    s for s in manifest["suite"] if s not in item["scenarios"]
                ]
                item["rejection_certificate"] = {"scenario": scenario, "metrics": metrics}
                break
        if len(item["scenarios"]) == len(manifest["suite"]) or item.get("rejection_certificate"):
            completed.append(item)
        dump(root / "results.json", completed)
        dump(
            root / "progress.json",
            {
                "covered_pairs": len(completed),
                "runs": runs,
                "seconds": time.monotonic() - started,
                "current": name,
            },
        )
        if reason:
            break
    result = call({"op": "rank", "suite": manifest["suite"], "results": completed})
    coverage = reason is None and len(completed) == len(manifest["candidates"])
    result.update(
        coverage_complete=coverage,
        status="complete" if coverage else "incomplete",
        supplemental_runs=runs,
        physical_runs=physical,
        reused_runs=reused,
        elapsed_seconds=time.monotonic() - started,
        stop_reason=reason,
        invalid_evaluations=invalid,
        peak_child_rss_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        primary_batch={k: v for k, v in primary.items() if k not in ("ranking", "winner")},
        combined_runs=primary["runs"] + runs,
        combined_elapsed_seconds=primary["elapsed_seconds"] + time.monotonic() - started,
        combined_storage_bytes=base_bytes + storage_bytes(root),
        provenance=manifest["provenance"],
        note="Separate bounded continuation; the original 1800-second batch remains incomplete.",
    )
    dump(root / "search.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wall-seconds", type=int, default=1800)
    parser.add_argument("--max-runs", type=int, default=3200)
    parser.add_argument("--storage-gib", type=int, default=20)
    args = parser.parse_args()
    if min(args.wall_seconds, args.max_runs, args.storage_gib) <= 0:
        parser.error("Budgets must be positive")
    result = complete(args.source, args.output, args.wall_seconds, args.max_runs, args.storage_gib)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("ranking", "provenance")}, indent=2
        )
    )


if __name__ == "__main__":
    main()
