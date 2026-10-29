"""Prepare a GPU-free replay schedule using NVIDIA AIConfigurator, once.

Uses EngineHandle's public prefill/decode-step API. No custom performance
estimator and no silent fallback to synthetic values. Runtime replay needs only
this JSON file, not the SDK. See docs/network-testing.md for provenance limits.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import sys
from pathlib import Path


def schedule(engine, input_tokens, output_tokens):
    if not 1 <= input_tokens <= 1000000 or not 1 <= output_tokens <= 4096:
        raise ValueError("unsupported input/output shape")
    first = float(engine.predict_prefill_latency(1, input_tokens, 0))
    # Prefill produces the first output. A batch-one decode step at the growing
    # context produces each subsequent output; there is no simulated batching.
    provenance = getattr(engine, "last_provenance", lambda: "unavailable")
    first_provenance = provenance()
    gaps, gap_provenance = [], []
    for index in range(1, output_tokens):
        gaps.append(float(engine.decode_step_latency(1, input_tokens + index - 1, 2)))
        gap_provenance.append(provenance())
    if any(not math.isfinite(value) or value <= 0 or value > 60000 for value in (first, *gaps)):
        raise ValueError("estimator returned invalid milliseconds; no profile written")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "first_output_ms": first,
        "gaps_ms": gaps,
        "prediction_provenance": {"first_output": first_provenance, "gaps": gap_provenance},
    }


def export(args):
    try:
        from aiconfigurator.sdk.engine import EngineHandle
    except ImportError as exc:
        raise ValueError(
            "Install NVIDIA aiconfigurator with EngineHandle support in a separate "
            "CPU preparation environment; it is not required for net-test"
        ) from exc
    engine = EngineHandle.compile(
        args.model,
        args.system,
        args.backend,
        backend_version=args.backend_version,
        tp_size=1,
        pp_size=1,
        systems_path=args.systems_path,
    )
    metadata = {
        "estimator": "NVIDIA AIConfigurator EngineHandle",
        "sdk_version": importlib.metadata.version("aiconfigurator"),
        "model": args.model,
        "system": args.system,
        "backend": args.backend,
        "backend_version": args.backend_version,
        "tp": 1,
        "pp": 1,
        "batch_size": 1,
        "prefix_tokens": 0,
        "units": "milliseconds",
        "pacing": "model-derived",
        "engine_spec_sha256": hashlib.sha256(engine.spec_bytes).hexdigest(),
        "systems_path": args.systems_path,
        "note": "Not a measured request/token trace or a continuous-batching model.",
    }
    system = (
        Path(args.systems_path)
        if args.systems_path
        else Path(
            importlib.metadata.distribution("aiconfigurator-core").locate_file(
                "aiconfigurator_core/systems"
            )
        )
    )
    if system.is_dir():
        hashes = {}
        for path in sorted(system.rglob("*")):
            if path.is_file() and (args.system in path.parts or path.stem == args.system):
                hashes[str(path.relative_to(system))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        if not hashes:
            raise ValueError("systems_path does not contain the selected system's data")
        metadata["source_sha256"] = hashes
        metadata["systems_origin"] = (
            "explicit database"
            if args.systems_path
            else "installed aiconfigurator-core distribution"
        )
    profile = {
        "version": 1,
        "kind": "model-derived",
        "metadata": metadata,
        "samples": [schedule(engine, length, args.output_tokens) for length in args.input_tokens],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        json.dump(profile, handle, indent=2)
        handle.write("\n")
    return profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="meta-llama/Meta-Llama-3.1-8B")
    parser.add_argument("--system", default="h100_sxm")
    parser.add_argument("--backend", default="vllm")
    parser.add_argument("--backend-version", required=True)
    parser.add_argument(
        "--systems-path", help="complete pinned AIC systems database, not partial tables"
    )
    parser.add_argument("--input-tokens", nargs="+", type=int, default=[512])
    parser.add_argument("--output-tokens", type=int, default=16)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        export(args)
    except Exception as exc:
        print(f"profile export failed: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {args.output}: model-derived timings, batch=TP=PP=1. No GPU used.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
