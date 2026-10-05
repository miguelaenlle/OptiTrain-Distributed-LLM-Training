"""Re-run identical saved inputs and audit causal identity and numerical equality."""

import argparse
import hashlib
import json
import time
from pathlib import Path

from fleetlab.engine import run


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def audit(path):
    assigned = {}
    workers = {}
    history = tokens = mislabeled = invalid_tokens = delayed_old_identity = 0
    for line in (path / "events.jsonl").open():
        event = json.loads(line)
        if event["event"] == "worker":
            workers[event["worker"]] = event["incarnation"]
        if event["event"] == "request":
            history += 1
            if event["state"] == "Assigned":
                assigned[event["attempt_id"]] = event
            attempt = assigned.get(event.get("attempt_id"))
            if attempt:
                mislabeled += event["incarnation"] != attempt["incarnation"]
                if "attempt_incarnation" in event:
                    assert event["attempt_incarnation"] == attempt["incarnation"]
                    assert event["attempt_worker"] == attempt["worker"]
                    if event["current_worker_incarnation"] is not None:
                        delayed_old_identity += (
                            event["current_worker_incarnation"] != event["attempt_incarnation"]
                        )
        if event["event"] == "token":
            tokens += 1
            attempt = assigned[event["attempt_id"]]
            invalid_tokens += (
                event["incarnation"] != attempt["incarnation"]
                or event["incarnation"] != workers[event["worker"]]
            )
    return {
        "history_events": history,
        "tokens": tokens,
        "mislabeled_history_events": mislabeled,
        "invalid_incarnation_tokens": invalid_tokens,
        "explicitly_distinguished_delayed_old_attempt_events": delayed_old_identity,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    old_manifest = json.loads((args.before / "manifest.json").read_text())
    config = {
        k: v
        for k, v in old_manifest.items()
        if k not in ("provenance", "format_version", "exact_dynamics_reuse")
    }
    old_metrics = json.loads((args.before / "metrics.json").read_text())
    started = time.monotonic()
    new_metrics = run(config, args.output, wall_timeout=1800)
    old_audit, new_audit = audit(args.before), audit(args.output)
    result = {
        "saved_inputs_sha256": digest(config),
        "saved_inputs_identical": True,
        "numerical_metrics_equal": old_metrics == new_metrics,
        "before_metrics_sha256": digest(old_metrics),
        "after_metrics_sha256": digest(new_metrics),
        "before": old_audit,
        "after": new_audit,
        "before_provenance": old_manifest["provenance"],
        "after_provenance": json.loads((args.output / "manifest.json").read_text())["provenance"],
        "elapsed_seconds": time.monotonic() - started,
        "scope": (
            "One full 1000-request retained half-down low-load flagship; "
            "frozen exhaustive sweep is not relabeled as this revision."
        ),
    }
    (args.output / "identity-audit.json").write_text(json.dumps(result, indent=2))
    assert result["numerical_metrics_equal"], "Numerical metrics changed"
    assert new_audit["mislabeled_history_events"] == 0
    assert new_audit["invalid_incarnation_tokens"] == 0
    assert new_audit["explicitly_distinguished_delayed_old_attempt_events"] > 0
    print(json.dumps({k: v for k, v in result.items() if "provenance" not in k}, indent=2))


if __name__ == "__main__":
    main()
