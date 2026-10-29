"""One-command launch, scheduled faults, evidence and cleanup."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config
from .remote import SSHRuntime
from .report import evaluate, render
from .runtime import Runtime, read_bytes, write_json


def build(root=config.ROOT):
    for tool in ("cmake", "go"):
        if not shutil.which(tool):
            raise ValueError(f"{tool} is required to build net-test; see docs/network-testing.md")
    binaries = root / ".fleet/nettest-bin"
    binaries.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "cmake",
            "-S",
            str(root / "netfault"),
            "-B",
            str(root / ".fleet/netfault-build"),
            "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
        ],
        check=True,
    )
    subprocess.run(
        ["cmake", "--build", str(root / ".fleet/netfault-build"), "--parallel", "4"], check=True
    )
    shutil.copy2(root / ".fleet/netfault-build/optitrain-netfault", binaries / "optitrain-netfault")
    for directory, name in (("router-go", "router"), ("loadgen", "loadgen")):
        subprocess.run(
            ["go", "build", "-trimpath", "-o", str(binaries / name), "."],
            cwd=root / directory,
            check=True,
        )
    return {name: str(binaries / name) for name in ("optitrain-netfault", "router", "loadgen")}


def jsonl(data):
    return [json.loads(line) for line in data.decode().splitlines() if line.strip()]


def wait_for(predicate, description, timeout=15):
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ValueError, RuntimeError) as exc:
            last = str(exc)
        time.sleep(0.05)
    raise RuntimeError(f"readiness failed: {description}: {last}")


def _process(runtime, name, argv, env=None):
    return runtime.call(
        {"op": "spawn", "name": name, "argv": [str(a) for a in argv], "env": env or {}}
    )


def _management(runtime, port, method="GET", body=None):
    command = {
        "op": "http",
        "url": f"http://127.0.0.1:{port}/" + ("healthz" if method == "GET" else "rules"),
        "method": method,
    }
    if body is not None:
        command["body"] = body
    return runtime.call(command)


def run(cfg, *, color=False):
    started = time.monotonic()
    run_id = "nettest-" + uuid.uuid4().hex[:12]
    output = Path(cfg["output"] or config.ROOT / ".fleet/net-tests" / run_id).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    runtimes = []
    evidence_errors = []
    report = None
    rows, attempts = [], []
    profile_kind = "real-inference"
    windows = {
        "healthy": (0, cfg["phases"]["healthy_s"]),
        "fault": (
            cfg["phases"]["healthy_s"],
            cfg["phases"]["healthy_s"] + cfg["phases"]["fault_s"],
        ),
        "recovery": (
            cfg["phases"]["healthy_s"] + cfg["phases"]["fault_s"],
            sum(cfg["phases"].values()),
        ),
    }
    events = []
    manifest = {
        "run_id": run_id,
        "config": cfg,
        "profile_kind": profile_kind,
        "execution": cfg["execution"],
        "cloud_validated": False,
        "phase_control_events": events,
        "resource_observations": [],
        "percentiles": "nearest-rank",
    }
    try:
        if cfg["execution"] == "local":
            supplied = cfg["artifacts"]
            binaries = {
                name: str(
                    Path(supplied.get(name, config.ROOT / ".fleet/nettest-bin" / name)).resolve()
                )
                for name in ("optitrain-netfault", "router", "loadgen")
            }
            if not all(Path(path).is_file() for path in binaries.values()):
                binaries = build()
            runtime = Runtime(output / "processes")
            runtimes.append(runtime)
            client = router = workers = runtime
            addresses = dict.fromkeys(("client", "router", "workers"), "127.0.0.1")
            host_binaries = dict.fromkeys(addresses, binaries)
        else:
            package = cfg["artifacts"].get("package", "")
            platform = cfg["artifacts"].get("platform", "linux-amd64")
            host_runtimes, host_binaries, addresses = {}, {}, {}
            # One agent per distinct SSH host, so shared placement cannot kill
            # sibling processes or lose evidence through duplicate cleanup.
            agents = {}
            for role, host in cfg["hosts"].items():
                alias = host["ssh_host"]
                if alias not in agents:
                    agent = SSHRuntime(host, f"/tmp/optitrain-nettest/{run_id}", package, platform)
                    agents[alias] = agent
                    runtimes.append(agent)
                host_runtimes[role] = agents[alias]
                host_binaries[role] = {
                    name: str(agents[alias].root / "bin" / name)
                    for name in ("optitrain-netfault", "router", "loadgen")
                }
                addresses[role] = host["data_address"]
            client, router, workers = (
                host_runtimes[key] for key in ("client", "router", "workers")
            )
        manifest["hosts"] = [runtime.call({"op": "ping"}) for runtime in runtimes]
        if cfg["execution"] == "local":
            manifest["artifacts"] = {
                name: {"sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
                for name, path in binaries.items()
            }
        else:
            manifest["package_sha256"] = hashlib.sha256(
                Path(cfg["artifacts"]["package"]).read_bytes()
            ).hexdigest()
        router_ports = (
            cfg["ports"].get("router") or router.call({"op": "ports", "count": 3})["ports"]
        )
        router_port, client_proxy, control_router = router_ports
        worker_ports = (
            cfg["ports"].get("workers")
            or workers.call({"op": "ports", "count": cfg["workers"] * 2 + 1})["ports"]
        )
        control_workers = worker_ports[-1]
        bind_host = "127.0.0.1" if cfg["execution"] == "local" else "0.0.0.0"
        listeners = []
        worker_configs = []
        docs = []
        if cfg["backend"] == "replay":
            profile_path = Path(cfg["profile"])
            if not profile_path.is_absolute():
                profile_path = config.ROOT / profile_path
            profile = json.loads(profile_path.read_text())
            expected = (cfg["input_tokens"], cfg["output_tokens"])
            if not any(
                (row["input_tokens"], row["output_tokens"]) == expected
                for row in profile["samples"]
            ):
                raise ValueError(f"profile has no sample for {expected}; no fallback is permitted")
            profile_kind = profile["kind"]
            manifest["profile_sha256"] = hashlib.sha256(profile_path.read_bytes()).hexdigest()
            write_json(workers, "profile.json", profile)
            if cfg.get("contract_version") == 2:
                from .contracts import plan

                (output / "contract.json").write_text(
                    json.dumps(plan(cfg, profile), indent=2) + "\n"
                )
        if cfg["backend"] == "cpu-model":
            if cfg["execution"] != "local":
                raise ValueError(
                    "cpu-model launcher is local; use external endpoints for remote model servers"
                )
            model_file = Path(cfg["model_file"]).expanduser().resolve()
            if not model_file.is_file():
                raise ValueError(
                    "cpu-model requires model_file pointing to one local GGUF; "
                    "no model is downloaded implicitly"
                )
            server = shutil.which(cfg["llama_server"])
            if not server:
                raise ValueError(
                    "llama-server not found; install a native llama.cpp server "
                    "or configure llama_server"
                )
            manifest["model_file_bytes"] = model_file.stat().st_size
            model_hash = hashlib.sha256()
            with model_file.open("rb") as model_data:
                for chunk in iter(lambda: model_data.read(1024 * 1024), b""):
                    model_hash.update(chunk)
            manifest["model_file_sha256"] = model_hash.hexdigest()
            manifest["model_server_sha256"] = hashlib.sha256(Path(server).read_bytes()).hexdigest()
            _process(
                workers,
                "model",
                [
                    server,
                    "-m",
                    model_file,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    worker_ports[0],
                    "--ctx-size",
                    "512",
                    "--n-gpu-layers",
                    "0",
                    "--device",
                    "none",
                    "--no-op-offload",
                    "--threads",
                    "2",
                    "--parallel",
                    "1",
                ],
            )
            wait_for(
                lambda: workers.call(
                    {"op": "http", "url": f"http://127.0.0.1:{worker_ports[0]}/health"}
                ),
                "real CPU model",
                90,
            )
        for i in range(cfg["workers"]):
            worker_id = f"worker-{i}"
            upstream = f"127.0.0.1:{worker_ports[i * 2]}"
            if cfg["backend"] == "external":
                upstream = cfg["endpoints"][i]
                if "://" in upstream:
                    raise ValueError(
                        "external endpoints must be host:port on the worker proxy's network, "
                        "without scheme"
                    )
            if cfg["backend"] == "replay":
                worker_configs.append(
                    {
                        "id": worker_id,
                        "listen": (
                            f"{bind_host}:{worker_ports[i * 2]}" if cfg["direct"] else upstream
                        ),
                        "profile": str(workers.root / "profile.json"),
                        "max_active": 1,
                    }
                )
            proxy_port = worker_ports[i * 2 + 1]
            listeners.append(
                {"name": worker_id, "listen": f"{bind_host}:{proxy_port}", "upstream": upstream}
            )
            registered = f"{addresses['workers']}:{proxy_port}"
            if cfg["direct"]:
                registered = (
                    f"{addresses['workers']}:{worker_ports[i * 2]}"
                    if cfg["backend"] == "replay"
                    else upstream
                )
            docs.append({"worker_id": worker_id, "addr": registered})
        manifest["topology"] = {
            "data_addresses": addresses,
            "worker_proxies": listeners,
            "registered_endpoints": docs,
            "router_port": router_port,
            "client_proxy_port": client_proxy,
        }
        maximum = cfg["max_runtime_s"]
        if cfg["backend"] == "replay":
            write_json(
                workers,
                "replay.json",
                {
                    "workers": worker_configs,
                    "events": str(workers.root / "worker-events.jsonl"),
                    "max_runtime_s": maximum,
                },
            )
            _process(
                workers,
                "replay",
                [
                    host_binaries["workers"]["optitrain-netfault"],
                    "replay",
                    "--config",
                    workers.root / "replay.json",
                ],
            )
            for item in worker_configs:
                port = item["listen"].rsplit(":", 1)[1]
                wait_for(
                    lambda p=port: workers.call(
                        {"op": "http", "url": f"http://127.0.0.1:{p}/healthz"}
                    ),
                    item["id"],
                )
        write_json(
            workers,
            "worker-proxy.json",
            {
                "control": f"127.0.0.1:{control_workers}",
                "listeners": listeners,
                "events": str(workers.root / "worker-proxy-events.jsonl"),
                "max_runtime_s": maximum,
            },
        )
        _process(
            workers,
            "worker-proxy",
            [
                host_binaries["workers"]["optitrain-netfault"],
                "proxy",
                "--config",
                workers.root / "worker-proxy.json",
            ],
        )
        wait_for(lambda: _management(workers, control_workers), "worker proxy")
        router.call({"op": "registry", "workers": docs})
        timeouts = cfg["timeouts"]
        env = {
            "HOST": bind_host,
            "PORT": str(router_port),
            "FLEET_WORKERS_URI": str(router.root / "registry"),
            "ROUTER_POLL_SECONDS": "0.1",
            "WORKER_TTL_SECONDS": str(maximum + 60),
            "REQUEST_TIMEOUT_SECONDS": str(timeouts["request_s"]),
            "ROUTER_CONNECT_TIMEOUT_SECONDS": str(timeouts["connect_s"]),
            "ROUTER_FIRST_OUTPUT_TIMEOUT_SECONDS": str(timeouts["first_output_s"]),
            "ROUTER_STREAM_IDLE_TIMEOUT_SECONDS": str(timeouts["idle_s"]),
            "ROUTER_MAX_ATTEMPTS": "3",
            "ROUTER_ATTEMPTS_FILE": str(router.root / "attempts.jsonl"),
        }
        routing = cfg.get("routing", config.DEFAULT["routing"])
        env.update(
            {
                "ROUTER_STREAM_FAILURE_THRESHOLD": str(routing["failure_threshold"]),
                "ROUTER_STREAM_COOLDOWN_SECONDS": str(routing["cooldown_s"]),
                "ROUTER_STREAM_LEAST_INFLIGHT_RETRIES": str(
                    routing["least_inflight_retries"]
                ).lower(),
                "ROUTER_STREAM_PROBE_WHEN_ALL_OPEN": str(routing["probe_when_all_open"]).lower(),
            }
        )
        _process(router, "router", [host_binaries["router"]["router"]], env)
        wait_for(
            lambda: (
                router.call({"op": "http", "url": f"http://127.0.0.1:{router_port}/healthz"}).get(
                    "live_workers"
                )
                == cfg["workers"]
            ),
            "Go router worker discovery",
        )
        write_json(
            router,
            "client-proxy.json",
            {
                "control": f"127.0.0.1:{control_router}",
                "listeners": [
                    {
                        "name": "client",
                        "listen": f"{bind_host}:{client_proxy}",
                        "upstream": f"127.0.0.1:{router_port}",
                    }
                ],
                "events": str(router.root / "client-proxy-events.jsonl"),
                "max_runtime_s": maximum,
            },
        )
        _process(
            router,
            "client-proxy",
            [
                host_binaries["router"]["optitrain-netfault"],
                "proxy",
                "--config",
                router.root / "client-proxy.json",
            ],
        )
        wait_for(lambda: _management(router, control_router), "client proxy")
        total = sum(cfg["phases"].values())
        url = f"http://{addresses['router']}:{router_port if cfg['direct'] else client_proxy}"
        argv = [
            host_binaries["client"]["loadgen"],
            "-stream",
            "-url",
            url,
            "-rps",
            str(cfg["rps"]),
            "-duration",
            f"{total}s",
            "-concurrency",
            str(cfg["concurrency"]),
            "-timeout",
            f"{timeouts['request_s']}s",
            "-max-tokens",
            str(cfg["output_tokens"]),
            "-temperature",
            "0",
            "-request-prefix",
            run_id,
            "-requests-out",
            str(client.root / "requests.jsonl"),
            "-meta-out",
            str(client.root / "client-meta.json"),
        ]
        if cfg["backend"] == "replay":
            argv += ["-replay-input-tokens", str(cfg["input_tokens"])]
        if cfg["scenario"] == "client-disconnect":
            argv += [
                "-cancel-after-events",
                "5",
                "-cancel-window-start",
                str(cfg["phases"]["healthy_s"]),
                "-cancel-window-end",
                str(cfg["phases"]["healthy_s"] + cfg["phases"]["fault_s"]),
            ]
        if cfg["model"]:
            argv += ["-model", cfg["model"]]
        client.call(
            {
                "op": "write",
                "path": "prompts.txt",
                "data": base64.b64encode((cfg["prompt"] + "\n").encode()).decode(),
            }
        )
        argv += ["-prompts", str(client.root / "prompts.txt")]
        manifest["profile_kind"] = profile_kind
        print(
            f"Timing source: {profile_kind}; backend: {cfg['backend']}; "
            f"fixed offered rate: {cfg['rps']} RPS",
            flush=True,
        )
        print(f"Frozen workload, SLO and acceptance criteria: {output / 'config.json'}", flush=True)
        # The probe is a real streamed request through the full native data path.
        probe = [*argv]
        probe[probe.index("-request-prefix") + 1] = run_id + "-probe"
        for key, value in (
            ("-rps", "1"),
            ("-duration", "0.01s"),
            ("-requests-out", str(client.root / "probe.jsonl")),
            ("-meta-out", str(client.root / "probe-meta.json")),
        ):
            probe[probe.index(key) + 1] = value
        _process(client, "probe", probe)
        wait_for(
            lambda: client.call({"op": "poll", "name": "probe"})["returncode"] is not None,
            "end-to-end stream probe",
            timeouts["request_s"] + 3,
        )
        probe_rows = jsonl(read_bytes(client, "probe.jsonl"))
        if not probe_rows or not probe_rows[0]["complete"]:
            raise RuntimeError(f"end-to-end streaming probe failed: {probe_rows}")
        # Keep the warm-up attempt out of measured per-request integrity checks.
        _process(client, "loadgen", argv)
        metadata = wait_for(
            lambda: json.loads(read_bytes(client, "client-meta.json")), "client start"
        )
        # Controller and client can be on different hosts. Bound start-time
        # uncertainty using an agent clock probe; never derive one-way network latency.
        before = time.time()
        clock = client.call({"op": "ping"})["unix_s"]
        after = time.time()
        skew = clock - (before + after) / 2
        uncertainty = (after - before) / 2
        if abs(skew) > 2:
            raise RuntimeError(
                "client/controller clocks differ by >2s; "
                "run controller on the client host or synchronize clocks"
            )
        origin = time.monotonic() - (time.time() + skew - metadata["start_unix_ns"] / 1e9)
        manifest["client_clock_skew_s"] = skew
        manifest["phase_clock_uncertainty_s"] = uncertainty
        healthy_end = cfg["phases"]["healthy_s"]
        fault_end = healthy_end + cfg["phases"]["fault_s"]

        heartbeat = [time.monotonic()]

        def until(offset):
            while time.monotonic() < origin + offset:
                if time.monotonic() - heartbeat[0] >= 5:
                    for runtime in runtimes:
                        observed = runtime.call({"op": "stats"})
                        manifest["resource_observations"].append(
                            {"offset_s": time.monotonic() - origin, **observed}
                        )
                        if any(
                            value["returncode"] is not None
                            for name, value in observed["processes"].items()
                            if name not in ("probe", "loadgen")
                        ):
                            raise RuntimeError(
                                "an owned serving component exited during measurement"
                            )
                    heartbeat[0] = time.monotonic()
                if time.monotonic() - started > maximum:
                    raise TimeoutError("maximum runtime exceeded")
                time.sleep(min(0.2, max(0, origin + offset - time.monotonic())))

        def change(method, fault=None):
            begin = time.monotonic() - origin
            targets = []
            if fault:
                names = (
                    (
                        ["client", *(f"worker-{i}" for i in range(cfg["workers"]))]
                        if cfg["scenario"] == "combined-path-delay"
                        else [f"worker-{i}" for i in range(cfg["workers"])]
                    )
                    if fault["target"] == "*"
                    else [fault["target"]]
                )
                for name in names:
                    host, port = (
                        (router, control_router) if name == "client" else (workers, control_workers)
                    )
                    body = {
                        **fault,
                        "target": name,
                        "duration_ms": min(300000, max(1, int(cfg["phases"]["fault_s"] * 1000))),
                    }
                    _management(host, port, method, body)
                    targets.append(name)
            else:
                _management(router, control_router, method)
                _management(workers, control_workers, method)
            end = time.monotonic() - origin
            events.append({"method": method, "targets": targets, "send_s": begin, "ack_s": end})
            return begin, end

        def quiet(ms):
            target = cfg["fault"]["target"]
            index = int(target.removeprefix("worker-"))
            workers.call(
                {
                    "op": "http",
                    "url": f"http://127.0.0.1:{worker_ports[index * 2]}/replay/quiet",
                    "method": "POST",
                    "body": {"quiet_ms": ms},
                }
            )

        print("Healthy phase started", flush=True)
        until(healthy_end)
        if cfg.get("contract_version") == 2 and cfg["scenario"] == "idle-expiry":
            quiet(150)
        begin, ack = change("POST", cfg["fault"]) if cfg["fault"] else (healthy_end, healthy_end)
        # A fixed, declared deadline guard prevents in-flight requests at
        # activation from contaminating the unexposed healthy reference.
        # Those requests remain in transition and all-offered accounting.
        guard = timeouts["request_s"] + 0.1 if cfg["fault"] else 0
        windows["healthy"] = (0, max(0, begin - uncertainty - guard))
        manifest["healthy_guard_s"] = guard
        print("Fault phase started:", cfg["fault"], flush=True)
        if cfg["scenario"] == "worker-flapping":
            while time.monotonic() - origin < fault_end - 1:
                until(min(fault_end, time.monotonic() - origin + 2))
                change("DELETE")
                until(min(fault_end, time.monotonic() - origin + 2))
                if time.monotonic() - origin < fault_end:
                    change("POST", cfg["fault"])
        until(fault_end)
        remove_begin, remove_ack = change("DELETE")
        if cfg.get("contract_version") == 2 and cfg["scenario"] == "idle-expiry":
            quiet(0)
        windows["fault"] = (ack + uncertainty, max(ack + uncertainty, remove_begin - uncertainty))
        windows["recovery"] = (remove_ack + uncertainty, total)
        if not cfg["fault"]:
            # Cancellation is controlled by the client's exact dispatch window,
            # not a proxy-rule acknowledgment. No rule changes in baseline.
            windows["fault"] = (healthy_end, fault_end)
            windows["recovery"] = (fault_end, total)
        print("Recovery phase started", flush=True)
        until(total)
        code = wait_for(
            lambda: (
                {"code": client.call({"op": "poll", "name": "loadgen"})["returncode"]}
                if client.call({"op": "poll", "name": "loadgen"})["returncode"] is not None
                else None
            ),
            "client draining",
            timeouts["request_s"] + 5,
        )["code"]
        if code not in (0, 1):
            evidence_errors.append(f"load generator exited {code}")
        if cfg.get("contract_version") == 2:
            # Observe quiescence before stopping processes, so cleanup cannot
            # manufacture evidence that abandoned work was released correctly.
            snapshot = {}
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                status = router.call(
                    {"op": "http", "url": f"http://127.0.0.1:{router_port}/fleet/metrics"}
                )
                worker_status = [
                    workers.call(
                        {"op": "http", "url": f"http://127.0.0.1:{worker_ports[i * 2]}/stats"}
                    )
                    for i in range(cfg["workers"])
                ]
                snapshot = {
                    "router_inflight": status["router"]["in_flight"],
                    "worker_outstanding": sum(w["active"] + w["waiting"] for w in worker_status),
                    "proxy_connections": _management(router, control_router)["connections"]
                    + _management(workers, control_workers)["connections"],
                }
                if all(value == 0 for value in snapshot.values()):
                    break
                time.sleep(0.02)
            (output / "drained.json").write_text(json.dumps(snapshot, indent=2) + "\n")
        rows = jsonl(read_bytes(client, "requests.jsonl"))
        measured_ids = {row["request_id"] for row in rows}
        attempts = [
            row
            for row in jsonl(read_bytes(router, "attempts.jsonl"))
            if row["request_id"] in measured_ids
        ]
        for _role, runtime, filename in (
            ("client", client, "requests.jsonl"),
            ("router", router, "attempts.jsonl"),
            ("client-proxy", router, "client-proxy-events.jsonl"),
            ("worker-proxy", workers, "worker-proxy-events.jsonl"),
            ("replay", workers, "worker-events.jsonl"),
        ):
            data = read_bytes(runtime, filename)
            if data:
                (output / filename).write_bytes(data)
        if cfg["fault"]:
            injected = jsonl(read_bytes(router, "client-proxy-events.jsonl")) + jsonl(
                read_bytes(workers, "worker-proxy-events.jsonl")
            )
            applied = {
                event["rule"]["target"]
                for event in injected
                if event.get("event") == "rule_applied"
            }
            expected_targets = {name for event in events for name in event["targets"]}
            if expected_targets - applied:
                evidence_errors.append(
                    f"no proxy activation evidence for {sorted(expected_targets - applied)}"
                )
            if cfg["fault"]["action"] != "disable":
                matched = {
                    event["target"] for event in injected if event.get("event") == "fault_matched"
                }
                if expected_targets - matched:
                    evidence_errors.append(
                        f"fault never matched a connection for {sorted(expected_targets - matched)}"
                    )
    except (Exception, KeyboardInterrupt) as exc:
        evidence_errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        for runtime in runtimes:
            try:
                runtime.call({"op": "stop"})
                for filename in (
                    "requests.jsonl",
                    "attempts.jsonl",
                    "worker-events.jsonl",
                    "client-proxy-events.jsonl",
                    "worker-proxy-events.jsonl",
                ):
                    data = read_bytes(runtime, filename)
                    if data:
                        (output / filename).write_bytes(data)
                for name in (
                    "router",
                    "replay",
                    "worker-proxy",
                    "client-proxy",
                    "loadgen",
                    "probe",
                    "model",
                ):
                    data = read_bytes(runtime, name + ".log")
                    if data:
                        (output / (name + ".log")).write_bytes(data)
            except Exception as exc:
                evidence_errors.append(f"cleanup/evidence: {exc}")
            finally:
                try:
                    runtime.close()
                except Exception as exc:
                    evidence_errors.append(f"process cleanup: {exc}")
    # Preserve partial request evidence on startup/control failure too.
    try:
        if not rows and (output / "requests.jsonl").exists():
            rows = jsonl((output / "requests.jsonl").read_bytes())
        if not attempts and (output / "attempts.jsonl").exists():
            identities = {row["request_id"] for row in rows}
            attempts = [
                row
                for row in jsonl((output / "attempts.jsonl").read_bytes())
                if row["request_id"] in identities
            ]
    except ValueError as exc:
        evidence_errors.append(f"invalid partial JSONL evidence: {exc}")
    from .contracts import read_evidence

    report = evaluate(
        cfg,
        rows,
        windows,
        attempts,
        evidence_errors,
        read_evidence(output) if cfg.get("contract_version") == 2 else None,
    )
    report["profile_kind"] = profile_kind
    manifest["profile_kind"] = profile_kind
    manifest["elapsed_s"] = time.monotonic() - started
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    plain = render(report, cfg)
    (output / "report.txt").write_text(plain)
    print(render(report, cfg, color=color), end="")
    print("Evidence:", output)
    return {"PASS": 0, "FAIL": 1, "INVALID": 2}[report["verdict"]]


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--backend", choices=("replay", "cpu-model", "external"))
    parser.add_argument("--scenario", choices=sorted(config.PRESETS))
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--output")
    parser.add_argument("--build", action="store_true", help="rebuild local C++ and Go binaries")
    parser.add_argument(
        "--dry-run", action="store_true", help="validate and print resolved settings only"
    )
    parser.add_argument("--build-only", action="store_true", help="build local binaries and exit")
    parsed = parser.parse_args(args)
    try:
        cfg = config.load(parsed.config, parsed.backend, parsed.scenario)
        if parsed.output:
            cfg["output"] = parsed.output
        if parsed.dry_run:
            print(json.dumps(cfg, indent=2))
            return 0
        if parsed.build_only:
            print(json.dumps(build(), indent=2))
            return 0
        if parsed.build:
            build()
        return run(
            cfg, color=parsed.color == "always" or parsed.color == "auto" and sys.stdout.isatty()
        )
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"net-test: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
