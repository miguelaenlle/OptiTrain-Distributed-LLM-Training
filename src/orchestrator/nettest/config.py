"""Resolve and validate one experiment file before launching any process."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT = {
    "version": 1,
    "contract_version": 1,
    "routing": {
        "failure_threshold": 0,
        "cooldown_s": 1.0,
        "least_inflight_retries": False,
        "probe_when_all_open": False,
    },
    "execution": "local",
    "backend": "replay",
    "workers": 3,
    "profile": "netfault/profiles/h100-llama31-8b.json",
    "scenario": "worker-delay",
    "rps": 6.0,
    "concurrency": 32,
    "input_tokens": 512,
    "output_tokens": 16,
    "prompt": "Explain why network timeouts matter in one short paragraph.",
    "phases": {"healthy_s": 30.0, "fault_s": 30.0, "recovery_s": 60.0},
    "timeouts": {"request_s": 2.0, "connect_s": 0.5, "first_output_s": 1.0, "idle_s": 1.0},
    "slo": {"ttft_ms": 250.0, "gap_ms": 100.0, "total_ms": 2000.0},
    "criteria": {
        "completion_pct": 99.0,
        "healthy_compliance_pct": 99.0,
        "fault_compliance_pct": 65.0,
        "fault_max_drop_pp": 35.0,
        "fault_goodput_ratio": 0.65,
        "fault_ttft_ms": 600.0,
        "fault_added_ttft_ms": 350.0,
        "fault_gap_ms": 50.0,
        "fault_gap_ratio": 1.25,
        "recovery_compliance_pct": 99.0,
        "recovery_latency_ratio": 1.10,
        "recovery_goodput_ratio": 0.95,
        "recovery_start_s": 5.0,
        "recovery_window_s": 5.0,
        "recovery_min_requests": 20,
    },
    "fault": {
        "target": "worker-1",
        "direction": "response",
        "action": "delay",
        "delay_ms": 300,
        "seed": 42,
    },
}
PRESETS = {
    "baseline": None,
    "worker-delay": {
        "target": "worker-1",
        "direction": "response",
        "action": "delay",
        "delay_ms": 300,
    },
    "client-delay": {
        "target": "client",
        "direction": "response",
        "action": "delay",
        "delay_ms": 100,
    },
    "client-jitter": {
        "target": "client",
        "direction": "response",
        "action": "delay",
        "delay_ms": 75,
        "jitter_ms": 75,
    },
    "slow-client-download": {
        "target": "client",
        "direction": "response",
        "action": "bandwidth",
        "rate_bytes_s": 3000,
    },
    "slow-client-upload": {
        "target": "client",
        "direction": "request",
        "action": "bandwidth",
        "rate_bytes_s": 100,
    },
    "worker-stall": {"target": "worker-1", "direction": "response", "action": "stall"},
    "worker-truncate": {
        "target": "worker-1",
        "direction": "response",
        "action": "cutoff",
        "bytes": 1024,
    },
    "worker-unreachable": {"target": "worker-1", "direction": "request", "action": "disable"},
    "idle-expiry": {"target": "worker-1", "direction": "response", "action": "idle", "idle_ms": 50},
    "client-disconnect": None,
    "fleet-stall": {"target": "*", "direction": "response", "action": "stall"},
    "combined-path-delay": {
        "target": "*",
        "direction": "response",
        "action": "delay",
        "delay_ms": 100,
    },
    "worker-flapping": {"target": "worker-1", "direction": "response", "action": "stall"},
}


def merge(base, override, location="config"):
    if not isinstance(override, dict):
        raise ValueError(f"{location} must be an object")
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key not in base:
            raise ValueError(f"unknown field: {location}.{key}")
        if key in ("hosts", "artifacts", "ports"):
            if not isinstance(value, dict):
                raise ValueError(f"{location}.{key} must be an object")
            result[key] = copy.deepcopy(value)
        elif isinstance(base[key], dict) and isinstance(value, dict):
            # Fault action attributes differ by action; validated by the C++ rule parser too.
            result[key] = (
                {**base[key], **value}
                if key == "fault"
                else merge(base[key], value, f"{location}.{key}")
            )
        else:
            if isinstance(base[key], dict) and not (key == "fault" and value is None):
                raise ValueError(f"{location}.{key} must be an object")
            result[key] = value
    return result


def load(path=None, backend=None, scenario=None):
    raw = {}
    if path:
        text = Path(path).read_text()
        if Path(path).suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError as exc:
                raise ValueError(
                    "YAML requires PyYAML; install the nettest extra or use JSON"
                ) from exc
            raw = yaml.safe_load(text)
        else:
            raw = json.loads(text)
    extra = {
        "hosts": {},
        "artifacts": {},
        "model_file": "",
        "model": "",
        "llama_server": "llama-server",
        "endpoints": [],
        "output": "",
        "max_runtime_s": 600,
        "ports": {},
        "direct": False,
    }
    cfg = merge({**DEFAULT, **extra}, raw)
    selected = scenario or raw.get("scenario")
    if selected:
        if selected not in PRESETS:
            raise ValueError(f"unknown scenario: {selected}")
        cfg["scenario"] = selected
        cfg["fault"] = copy.deepcopy(PRESETS[selected])
        if "fault" in raw and raw["fault"] is not None:
            cfg["fault"] = {**(cfg["fault"] or {}), **raw["fault"]}
    if backend:
        cfg["backend"] = backend
    if cfg["backend"] == "cpu-model":
        cfg["workers"] = 1
        if cfg["fault"] and cfg["fault"]["target"] == "worker-1":
            cfg["fault"]["target"] = "worker-0"
        # CPU inference has its own declared budgets, not H100/replay promises.
        cfg["timeouts"] = merge(
            {
                "request_s": 10.0,
                "connect_s": 1.0,
                "first_output_s": 5.0,
                "idle_s": 2.0,
            },
            raw.get("timeouts", {}),
            "timeouts",
        )
        cfg["slo"] = merge(
            {"ttft_ms": 5000.0, "gap_ms": 2000.0, "total_ms": 10000.0},
            raw.get("slo", {}),
            "slo",
        )
        cfg["criteria"] = merge(
            {
                **DEFAULT["criteria"],
                "recovery_min_requests": 2,
                "fault_ttft_ms": 6000.0,
                "fault_gap_ms": 2000.0,
            },
            raw.get("criteria", {}),
            "criteria",
        )
        if "rps" not in raw:
            cfg["rps"] = 0.5
    validate(cfg)
    return cfg


def positive(value, key, maximum=3600):
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or not 0 < value <= maximum
    ):
        raise ValueError(f"{key} must be finite, positive and <= {maximum}")


def validate(cfg):
    if (
        cfg["version"] != 1
        or cfg["execution"] not in ("local", "remote")
        or cfg["backend"] not in ("replay", "cpu-model", "external")
    ):
        raise ValueError("unsupported config version, execution or backend")
    if cfg.get("contract_version", 1) not in (1, 2):
        raise ValueError("contract_version must be 1 or 2")
    if cfg.get("contract_version") == 2 and (
        cfg["execution"] != "local" or cfg["backend"] != "replay" or cfg["direct"]
    ):
        raise ValueError("contract v2 currently requires local replay through both proxies")
    routing = cfg.get("routing", DEFAULT["routing"])
    if (
        type(routing["failure_threshold"]) is not int
        or not 0 <= routing["failure_threshold"] <= 100
    ):
        raise ValueError("routing.failure_threshold must be an integer in 0..100")
    positive(routing["cooldown_s"], "routing.cooldown_s", 300)
    if type(routing["least_inflight_retries"]) is not bool:
        raise ValueError("routing.least_inflight_retries must be boolean")
    if type(routing["probe_when_all_open"]) is not bool:
        raise ValueError("routing.probe_when_all_open must be boolean")
    for key, maximum in (
        ("workers", 64),
        ("concurrency", 4096),
        ("output_tokens", 4096),
        ("input_tokens", 1000000),
    ):
        positive(cfg[key], key, maximum)
        if not isinstance(cfg[key], int):
            raise ValueError(f"{key} must be an integer")
    positive(cfg["rps"], "rps", 100000)
    if cfg["rps"] * sum(cfg["phases"].values()) * cfg["output_tokens"] > 2000000:
        raise ValueError("experiment exceeds two million requested output events")
    for name, values in (
        ("phases", cfg["phases"]),
        ("timeouts", cfg["timeouts"]),
        ("slo", cfg["slo"]),
    ):
        for key, value in values.items():
            positive(value, f"{name}.{key}", 300000)
    if sum(cfg["phases"].values()) + cfg["timeouts"]["request_s"] + 10 > cfg["max_runtime_s"]:
        raise ValueError("experiment plus draining exceeds maximum runtime")
    positive(cfg["max_runtime_s"], "max_runtime_s", 3600)
    if cfg["max_runtime_s"] > 3600:
        raise ValueError("maximum runtime is 3600 seconds")
    for key, value in cfg["criteria"].items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or not 0 <= value <= 100000
        ):
            raise ValueError(f"criteria.{key} must be a finite non-negative number")
        if key in ("recovery_window_s", "recovery_min_requests") and value <= 0:
            raise ValueError(f"criteria.{key} must be positive")
        if (key.endswith("_pct") or key.endswith("_pp")) and value > 100:
            raise ValueError(f"{key} must be <=100")
    fault = cfg["fault"]
    if fault:
        if fault.get("target") not in {
            "client",
            "*",
            *(f"worker-{i}" for i in range(cfg["workers"])),
        }:
            raise ValueError("fault target does not exist")
        if fault.get("direction") not in ("request", "response"):
            raise ValueError("invalid fault direction")
        if fault.get("action") not in ("delay", "stall", "bandwidth", "cutoff", "idle", "disable"):
            raise ValueError("invalid fault action")
    if fault:
        allowed = {
            "target",
            "direction",
            "action",
            "delay_ms",
            "jitter_ms",
            "rate_bytes_s",
            "bytes",
            "idle_ms",
            "seed",
        }
        if set(fault) - allowed:
            raise ValueError(f"unknown fault fields: {set(fault) - allowed}")
        limits = {
            "delay_ms": 60000,
            "jitter_ms": 60000,
            "rate_bytes_s": 1000000000,
            "bytes": 1000000000,
            "idle_ms": 300000,
            "seed": 2**63 - 1,
        }
        for key, maximum in limits.items():
            value = fault.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"fault.{key} must be an integer in 0..{maximum}")
        for action, field in (
            ("bandwidth", "rate_bytes_s"),
            ("cutoff", "bytes"),
            ("idle", "idle_ms"),
        ):
            if fault["action"] == action and fault.get(field, 0) == 0:
                raise ValueError(f"{action} requires positive {field}")
        if fault["action"] == "disable" and fault["direction"] != "request":
            raise ValueError("disable uses the request direction")
    if cfg["direct"] and (cfg["scenario"] != "baseline" or cfg["fault"] is not None):
        raise ValueError("direct mode only supports the baseline scenario")
    if cfg["backend"] == "external" and len(cfg["endpoints"]) != cfg["workers"]:
        raise ValueError("external mode needs one endpoint per worker")
    if cfg["execution"] == "remote" and set(cfg["hosts"]) != {"client", "router", "workers"}:
        raise ValueError("remote hosts must contain client, router and workers")
    if cfg["fault"] and cfg["phases"]["fault_s"] > 300:
        raise ValueError("fault phase must be <=300s; rules are never silently shortened")
    if cfg["fault"] and cfg["phases"]["healthy_s"] <= cfg["timeouts"]["request_s"] + 0.1:
        raise ValueError("healthy phase must exceed the pre-fault request deadline guard")
    if cfg["execution"] == "remote":
        import ipaddress

        for host in cfg["hosts"].values():
            if not isinstance(host, dict) or not host.get("ssh_host"):
                raise ValueError("each host requires ssh_host and data_address")
            address = ipaddress.ip_address(host["data_address"])
            if address.is_loopback and len({h["ssh_host"] for h in cfg["hosts"].values()}) > 1:
                raise ValueError(
                    "different remote hosts must advertise non-loopback data addresses"
                )
            if address.version != 4:
                raise ValueError("remote mode currently supports IPv4 data addresses")
    expected_ports = {"router": 3, "workers": cfg["workers"] * 2 + 1}
    if set(cfg["ports"]) - set(expected_ports):
        raise ValueError("ports supports router and workers lists")
    for role, ports in cfg["ports"].items():
        if not isinstance(ports, list) or len(ports) != expected_ports[role]:
            raise ValueError(f"ports.{role} requires {expected_ports[role]} entries")
        if any(
            isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
            for port in ports
        ) or len(set(ports)) != len(ports):
            raise ValueError("ports must be distinct integers in 1..65535")
    colocated = (
        cfg["execution"] == "local"
        or cfg["hosts"]["router"]["ssh_host"] == cfg["hosts"]["workers"]["ssh_host"]
    )
    if colocated and set(cfg["ports"].get("router", [])) & set(cfg["ports"].get("workers", [])):
        raise ValueError("co-located role port assignments overlap")
    if cfg["execution"] == "local" and cfg["hosts"]:
        raise ValueError("hosts requires execution=remote")
