"""Protocol adapter and immutable experiment artifacts for the C++ reducer."""

import json
import math
import os
import shutil
import signal
import subprocess
import traceback
from pathlib import Path

from .adapter import Frontend, binary_path, provenance


def validate(config):
    if any(type(config[k]) is not int for k in ("regular", "spot")):
        raise ValueError("Replica counts must be integers")
    n = config["regular"] + config["spot"]
    if not 1 <= n <= 16 or min(config["regular"], config["spot"]) < 0:
        raise ValueError("Use 1–16 replicas and nonnegative integer class counts")
    if config["policy"] not in ("round_robin", "least_work"):
        raise ValueError("Unknown routing policy")
    if not 1 <= config.get("max_attempts", 3) <= 16:
        raise ValueError("max_attempts must be 1–16")
    if config.get("report_period_ns", 10**9) <= 0:
        raise ValueError("Report period must be positive")
    for key in ("deadline_ns", "report_period_ns"):
        value = config.get(key, 1)
        if type(value) is not int or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in (
        "discovery_ns",
        "report_delay_ns",
        "delivery_delay_ns",
        "backoff_ns",
        "minimum_billing_ns",
    ):
        value = config.get(key, 0)
        if type(value) is not int or value < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    for key in ("completion_threshold", "compliance_threshold"):
        value = config.get(key, 0.99)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{key} must be a finite fraction")
    for key in ("ttft_limit_ns", "itl_limit_ns", "waiting_limit", "worker_admission_limit"):
        if config.get(key, 1) <= 0:
            raise ValueError(f"{key} must be positive")
    if (
        config.get("model", "meta-llama/Llama-3.1-8B") != "meta-llama/Llama-3.1-8B"
        or config.get("hardware", "RTXPRO6000") != "RTXPRO6000"
    ):
        raise ValueError("Unsupported model/hardware profile")
    seen = set()
    for request in config["requests"]:
        if request["id"] in seen or not 0 <= request["id"] < 2_000_000:
            raise ValueError("Request IDs must be unique nonnegative small integers")
        seen.add(request["id"])
        if not 0 <= request["arrival_ns"] <= config["horizon_ns"]:
            raise ValueError("Arrival outside the declared horizon")
        if request["prompt_length"] + request["output_length"] > 16384:
            raise ValueError("Context exceeds the measured 16,384-token attention profile")
        if request["prompt_length"] <= 0 or request["output_length"] <= 0:
            raise ValueError("Token lengths must be positive")
        if len(request["prompt_tokens"]) != request["prompt_length"]:
            raise ValueError("Prompt token count mismatch")
        if len(request["output_tokens"]) != request["output_length"]:
            raise ValueError("Output token count mismatch")
    placement = config.get("placement", list(range(n)))
    if (
        len(placement) != n
        or placement != sorted(placement)
        or sorted(set(placement)) != list(range(len(set(placement))))
    ):
        raise ValueError("Placement must group replicas under contiguous node IDs")
    for fault in config["faults"]:
        for key in ("availability_ns", "init_ns", "load_ns"):
            if type(fault.get(key, 0)) is not int or fault.get(key, 0) < 0:
                raise ValueError("Recovery delays must be nonnegative integer nanoseconds")
        if not 0 <= fault["time_ns"] <= config["horizon_ns"]:
            raise ValueError("Fault outside horizon")
        if any(w < 0 or w >= n for w in fault["workers"]):
            raise ValueError("Fault references an unknown worker")
        if len(set(fault["workers"])) != len(fault["workers"]):
            raise ValueError("Duplicate worker in fault")
    for key in ("regular_mean_active_hours", "spot_mean_active_hours"):
        if not math.isfinite(config.get(key, 1)) or config.get(key, 1) <= 0:
            raise ValueError("Assumed hazard means must be positive")
    price_info = config.get("price_provenance", {})
    if (
        price_info.get("currency", "USD") != "USD"
        or price_info.get("unit", "instance_hour") != "instance_hour"
    ):
        raise ValueError("Modeled prices require USD per instance_hour")
    for key in ("regular", "spot"):
        if not math.isfinite(config["prices"][key]) or config["prices"][key] < 0:
            raise ValueError("Prices cannot be negative")


def run(config, destination, retain=True, wall_timeout=None):
    validate(config)
    output = Path(destination).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {**config, "provenance": provenance(), "format_version": 1}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    frontend = Frontend(config, output)
    binary = binary_path()
    args = [
        str(binary),
        "--workload-configuration="
        + frontend.get_workload(
            None, None, event=True, inputs_root=frontend.cluster["inputs_root"]
        ),
    ]
    args += [
        "--" + flag + "=" + frontend.cluster[key]
        for flag, key in (
            ("system-configuration", "system_config_path"),
            ("network-configuration", "network_config_path"),
            ("memory-configuration", "memory_config_path"),
        )
    ]
    count = 0
    summary = None
    with (output / "backend.log").open("w") as log, (output / "events.jsonl").open("w") as events:
        reader, writer = os.pipe()
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            text=True,
            pass_fds=(writer,),
            env={**os.environ, "OPTITRAIN_FLEET_LAB": "1", "OPTITRAIN_FLEET_FD": str(writer)},
        )
        os.close(writer)
        protocol = os.fdopen(reader)

        def send(value):
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()

        def receive(until):
            nonlocal summary, count
            while True:
                line = protocol.readline()
                if not line:
                    raise RuntimeError(f'Backend disconnected; see {output / "backend.log"}')
                if not line.startswith("FLEETLAB "):
                    log.write(line)
                    continue
                try:
                    value = json.loads(line[9:])
                except json.JSONDecodeError:
                    raise RuntimeError(f"Invalid protocol line: {line!r}")
                value["seq"] = count
                count += 1
                events.write(json.dumps(value) + "\n")
                event = value["event"]
                if event == "error":
                    raise RuntimeError(value["message"])
                if event in ("admit", "reset", "schedule", "abort", "frontend_complete"):
                    w = value["worker"]
                    try:
                        if event == "admit":
                            result = frontend.admit(w, value["request"])
                        elif event == "reset":
                            result = frontend.reset(w)
                        elif event == "schedule":
                            result = frontend.schedule(w, value["time_ns"])
                        elif event == "abort":
                            result = frontend.abort(w, value["attempt_id"])
                        else:
                            result = frontend.complete(w, value["batch"], value["time_ns"])
                        send({"op": "response", "value": result})
                    except Exception:
                        send({"op": "response", "error": traceback.format_exc()})
                        raise
                if event == "finished":
                    summary = value["summary"]
                if event == until:
                    return value

        def expired(signum, frame):
            raise TimeoutError("Wall-time cap interrupted the active simulator")

        previous_handler = None
        if wall_timeout is not None:
            previous_handler = signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, max(0.001, wall_timeout))
        try:
            receive("ready")
            send({"op": "configure", "config": config})
            receive("ack")
            send({"op": "run"})
            receive("ack")
            send({"op": "exit"})
            receive("cleanup")
            if process.wait(timeout=10):
                raise RuntimeError("Backend failed during cleanup")
        finally:
            if wall_timeout is not None:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
            protocol.close()
            if process.poll() is None:
                process.kill()
                process.wait()
    from .core import call

    if retain:
        native_index = call({"op": "index", "path": str(output / "events.jsonl")})
        (output / "events.jsonl.index.json").write_text(json.dumps(native_index))
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    from .core import call

    result = call({"op": "metrics", "summary": summary, "config": config})
    if provenance() != manifest["provenance"]:
        raise RuntimeError("Engine/profile changed during run; artifacts are not verified")
    (output / "verification.json").write_text(json.dumps({"provenance_unchanged": True}))
    (output / "metrics.json").write_text(json.dumps(result, indent=2))
    if not retain:
        shutil.rmtree(output / "inputs", ignore_errors=True)
        for name in ("events.jsonl", "events.jsonl.index.json", "backend.log"):
            (output / name).unlink(missing_ok=True)
    return result
