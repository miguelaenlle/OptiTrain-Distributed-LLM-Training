import argparse
import hashlib
import json
import resource
import time
from pathlib import Path

from .adapter import provenance
from .core import call
from .engine import run, validate
from .scenarios import SUITE, experiment, trace


def dump(path, value):
    path.write_text(json.dumps(value, indent=2))


def calibrate(root, seed=42, count=1000):
    root.mkdir(parents=True, exist_ok=False)
    measured = []
    # Bracket tested sustainable rate using the same real frontend/profile path.
    for rate in (5, 10, 20, 40, 80, 160):
        cfg = experiment(1, 0, scenario="healthy", rate=rate, seed=seed, count=count)
        result = run(cfg, root / f"rate-{rate}", retain=False)
        measured.append({"rate": rate, "metrics": result})
        dump(root / "calibration.json", {"seed": seed, "count": count, "measured": measured})
        if not result["feasible"]:
            break
    passing = [r["rate"] for r in measured if r["metrics"]["feasible"]]
    value = {
        "q": max(passing) if passing else None,
        "definition": "highest tested passing healthy single-worker rate",
        "seed": seed,
        "count": count,
        "measured": measured,
        "upper_bound_tested": not measured[-1]["metrics"]["feasible"],
    }
    dump(root / "calibration.json", value)
    return value


def dynamics_key(config):
    if config.get("stochastic_failures") or config["scenario"] not in ("healthy", "half"):
        return None
    dynamics = dict(config)
    dynamics["replicas"] = dynamics.pop("regular") + dynamics.pop("spot")
    dynamics.pop("prices", None)
    dynamics.pop("price_provenance", None)
    return hashlib.sha256(
        json.dumps(dynamics, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def reuse(config, source, destination, key):
    destination.mkdir(parents=True, exist_ok=False)
    source_manifest = json.loads((source / "manifest.json").read_text())
    source_verification = json.loads((source / "verification.json").read_text())
    current_provenance = provenance()
    source_config = {k: source_manifest[k] for k in config}
    if (
        source_manifest["provenance"] != current_provenance
        or not source_verification.get("provenance_unchanged")
        or dynamics_key(source_config) != key
        or dynamics_key(config) != key
    ):
        raise ValueError("Representative provenance or exact dynamics key does not match")
    summary = json.loads((source / "summary.json").read_text())
    result = call({"op": "rebill", "summary": summary, "config": config})
    dump(
        destination / "manifest.json",
        {
            **config,
            "provenance": provenance(),
            "format_version": 1,
            "exact_dynamics_reuse": {
                "representative": str(source.name),
                "equivalence_key": key,
                "proof_scope": (
                    "homogeneous healthy/class-neutral half dynamics; hazards disabled; "
                    "all nonbilling fields identical"
                ),
            },
        },
    )
    if provenance() != current_provenance:
        raise RuntimeError("Engine changed during reuse")
    dump(destination / "summary.json", result["summary"])
    dump(destination / "metrics.json", result["metrics"])
    dump(
        destination / "verification.json",
        {"provenance_unchanged": True, "reused_exact_dynamics": True},
    )
    return result["metrics"]


def search(
    root,
    q,
    seed=42,
    count=1000,
    wall_seconds=1800,
    max_runs=3200,
    max_replicas=16,
    suite=None,
    overrides=None,
):
    root.mkdir(parents=True, exist_ok=False)
    suite = suite or SUITE
    overrides = overrides or {}
    if any(
        key in overrides
        for key in (
            "regular",
            "spot",
            "policy",
            "requests",
            "horizon_ns",
            "fault_targets",
            "fault_count",
            "placement",
        )
    ):
        raise ValueError(
            (
                "Search uses relative scenario targets; "
                "fixed counts/targets/placement are for fixed-fleet runs"
            )
        )
    if any(name not in SUITE + ["staggered", "all_down", "half_high"] for name in suite):
        raise ValueError("Unknown required scenario")
    for name in suite:
        validate(
            experiment(
                8, 8, scenario="half" if name == "half_high" else name, count=1, overrides=overrides
            )
        )
    requests = trace(0.4 * 16 * q, seed, count)
    shared_horizon = max(
        experiment(
            8,
            8,
            scenario="half" if name == "half_high" else name,
            requests=trace(0.75 * 16 * q, seed, count) if name == "half_high" else requests,
            overrides=overrides,
        )["horizon_ns"]
        for name in suite
    )
    candidates = call({"op": "candidates", "max_replicas": max_replicas})
    completed = []
    started = time.monotonic()
    runs = 0
    physical_runs = 0
    reused_runs = 0
    cache = {}
    reason = None
    invalid_evaluations = []
    frozen = provenance()
    dump(
        root / "search-manifest.json",
        {
            "provenance": frozen,
            "q": q,
            "seed": seed,
            "count": count,
            "suite": suite,
            "candidates": candidates,
            "overrides": overrides,
            "shared_horizon_ns": shared_horizon,
        },
    )
    for candidate in candidates:
        item = {**candidate, "scenarios": {}}
        for scenario in suite:
            if provenance() != frozen:
                reason = "engine or profile changed"
                break
            if runs >= max_runs or time.monotonic() - started >= wall_seconds:
                reason = "run cap" if runs >= max_runs else "wall-time cap"
                break
            if sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) > 20 * 1024**3:
                reason = "20 GiB storage cap"
                break
            name = f"r{candidate['regular']}-s{candidate['spot']}-{candidate['policy']}-{scenario}"
            scenario_requests = (
                trace(0.75 * 16 * q, seed, count) if scenario == "half_high" else requests
            )
            cfg = experiment(
                **candidate,
                scenario="half" if scenario == "half_high" else scenario,
                requests=scenario_requests,
                seed=seed,
                overrides=overrides,
            )
            cfg["horizon_ns"] = shared_horizon
            cfg["calibration_q"] = q
            cfg["synthetic_rate_per_second"] = (0.75 if scenario == "half_high" else 0.4) * 16 * q
            cfg["optimizer_prune"] = True
            try:
                validate(cfg)
                key = dynamics_key(cfg)
                if key is not None and key in cache:
                    item["scenarios"][scenario] = reuse(cfg, cache[key], root / name, key)
                    item.setdefault("reused_scenarios", {})[scenario] = cache[key].name
                    reused_runs += 1
                else:
                    item["scenarios"][scenario] = run(
                        cfg,
                        root / name,
                        retain=False,
                        wall_timeout=wall_seconds - (time.monotonic() - started),
                    )
                    physical_runs += 1
                    if key is not None:
                        cache[key] = root / name
            except TimeoutError:
                reason = "wall-time cap interrupted active simulator"
                break
            except Exception as exc:
                reason = "invalid evaluation; no infeasibility certificate"
                invalid_evaluations.append(
                    {"candidate": candidate, "scenario": scenario, "error": str(exc)}
                )
                break
            runs += 1
            if not item["scenarios"][scenario]["feasible"]:
                item["skipped_scenarios"] = [s for s in suite if s not in item["scenarios"]]
                item["rejection_certificate"] = {
                    "scenario": scenario,
                    "metrics": item["scenarios"][scenario],
                }
            dump(
                root / "progress.json",
                {
                    "runs": runs,
                    "planned_runs": len(candidates) * len(suite),
                    "elapsed_seconds": time.monotonic() - started,
                    "current": name,
                },
            )
            if item.get("rejection_certificate"):
                break
        if len(item["scenarios"]) == len(suite) or item.get("rejection_certificate"):
            completed.append(item)
        dump(root / "results.json", completed)
        if reason:
            break
    result = call({"op": "rank", "suite": suite, "results": completed})
    result.update(
        {
            "suite": suite,
            "runs": runs,
            "physical_runs": physical_runs,
            "reused_runs": reused_runs,
            "peak_child_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
            "planned_runs": len(candidates) * len(suite),
            "coverage_complete": reason is None,
            "stop_reason": reason,
            "elapsed_seconds": time.monotonic() - started,
            "q": q,
            "seed": seed,
            "count": count,
            "absolute_rate": 0.4 * 16 * q,
            "status": "complete" if reason is None else "incomplete",
            "invalid_evaluations": invalid_evaluations,
            "certified_rejected_pairs": sum(
                bool(x.get("rejection_certificate")) for x in completed
            ),
            "skipped_scenarios": sum(len(x.get("skipped_scenarios", [])) for x in completed),
            "coverage_definition": (
                "every pair fully evaluated or rejected by an exact "
                "completed-scenario or monotone SLO certificate"
            ),
        }
    )
    dump(root / "search.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description="CPU-only Fleet Lab")
    parser.add_argument("command", choices=["run", "calibrate", "search", "replay"])
    parser.add_argument("--output", required=True)
    parser.add_argument("--config")
    parser.add_argument("--suite", default=",".join(SUITE))
    parser.add_argument("--overrides")
    parser.add_argument("--q", type=float)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--max-replicas", type=int, default=16)
    parser.add_argument("--wall-seconds", type=int, default=1800)
    parser.add_argument("--max-runs", type=int, default=3200)
    args = parser.parse_args()
    root = Path(args.output).resolve()
    if args.command == "run":
        value = run(
            json.loads(Path(args.config).read_text()) if args.config else experiment(), root
        )
    elif args.command == "calibrate":
        value = calibrate(root, args.seed, args.count)
    elif args.command == "search":
        if args.q is None:
            parser.error("search requires the measured calibration --q")
        value = search(
            root,
            args.q,
            args.seed,
            args.count,
            args.wall_seconds,
            args.max_runs,
            args.max_replicas,
            args.suite.split(","),
            json.loads(Path(args.overrides).read_text()) if args.overrides else None,
        )
    else:
        value = call({"op": "replay", "path": str(root / "events.jsonl")})
    print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
