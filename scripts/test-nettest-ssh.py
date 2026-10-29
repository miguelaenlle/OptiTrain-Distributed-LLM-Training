#!/usr/bin/env python3
"""Local-only packaging/SSH smoke. No EC2, AWS credentials or provisioning.

Builds a disposable Ubuntu SSH target, uses temporary keys and strict host-key
checking, deploys the real package through SSH/SCP and runs native TCP inside
that target. --split-hosts uses three containers and also checks direct routing.
This verifies deployment mechanics, not EC2 networking or hardware fidelity.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="optitrain-nettest:ssh")
    parser.add_argument("--split-hosts", action="store_true", help="use three isolated Linux hosts")
    args = parser.parse_args()
    name = "optitrain-nettest-ssh-" + uuid.uuid4().hex[:8]
    artifact = ROOT / ".fleet" / (name + ".tar.gz")
    artifact.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="optitrain-ssh-") as temporary:
        folder = Path(temporary)
        key = folder / "identity"
        command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
        roles = ("client", "router", "workers")
        containers = [name + "-" + role for role in roles] if args.split_hosts else [name]
        network = name + "-network"
        command(["docker", "network", "create", network], capture_output=True)
        try:
            ssh_config = folder / "config"
            hosts, known_hosts, ssh_entries = {}, [], []
            for container in containers:
                command(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--rm",
                        "--name",
                        container,
                        "--network",
                        network,
                        "-p",
                        "127.0.0.1::22",
                        "-v",
                        f"{key}.pub:/root/.ssh/authorized_keys:ro",
                        args.image,
                    ],
                    capture_output=True,
                )
                port = (
                    command(["docker", "port", container, "22"], capture_output=True)
                    .stdout.strip()
                    .rsplit(":", 1)[1]
                )
                known = command(
                    ["docker", "exec", container, "cat", "/etc/ssh/ssh_host_ed25519_key.pub"],
                    capture_output=True,
                ).stdout.strip()
                known_hosts.append(f"[127.0.0.1]:{port} {known}\n")
                ssh_entries.append(
                    f"Host {container}\n  HostName 127.0.0.1\n"
                    f"  Port {port}\n  User root\n  IdentityFile {key}\n"
                    f"  UserKnownHostsFile {folder / 'known_hosts'}\n"
                    "  StrictHostKeyChecking yes\n  IdentitiesOnly yes\n"
                )
                address = "127.0.0.1"
                if args.split_hosts:
                    inspected = json.loads(
                        command(["docker", "inspect", container], capture_output=True).stdout
                    )[0]
                    address = inspected["NetworkSettings"]["Networks"][network]["IPAddress"]
                hosts[container] = {
                    "ssh_host": container,
                    "ssh_config": str(ssh_config),
                    "data_address": address,
                }
            (folder / "known_hosts").write_text("".join(known_hosts))
            ssh_config.write_text("".join(ssh_entries))
            command(["docker", "cp", f"{containers[0]}:/opt/nettest-linux.tar.gz", str(artifact)])
            arch = command(
                ["docker", "exec", containers[0], "uname", "-m"], capture_output=True
            ).stdout.strip()
            mapped = {"aarch64": "arm64", "x86_64": "amd64"}[arch]
            config = {
                "execution": "remote",
                "scenario": "baseline",
                "hosts": {
                    role: hosts[containers[i] if args.split_hosts else containers[0]]
                    for i, role in enumerate(roles)
                },
                "artifacts": {"package": str(artifact), "platform": f"linux-{mapped}"},
                "phases": {"healthy_s": 4, "fault_s": 4, "recovery_s": 6},
                "criteria": {"recovery_window_s": 2, "recovery_min_requests": 8},
                "output": str(ROOT / ".fleet/net-tests" / name),
            }
            for container in containers:
                deadline = time.monotonic() + 10
                while True:
                    probe = subprocess.run(
                        ["ssh", "-F", str(ssh_config), container, "true"], capture_output=True
                    )
                    if probe.returncode == 0:
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError(probe.stderr.decode())
                    time.sleep(0.1)
            environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
            variants = [("baseline", False)]
            if args.split_hosts:
                variants += [("baseline", True), ("worker-delay", False)]
            for index, (scenario, direct) in enumerate(variants):
                config.update(
                    scenario=scenario,
                    direct=direct,
                    output=str(ROOT / ".fleet/net-tests" / f"{name}-{index}"),
                )
                path = folder / "test.json"
                path.write_text(json.dumps(config))
                tested = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "orchestrator",
                        "fleet",
                        "net-test",
                        "--config",
                        str(path),
                    ],
                    cwd=ROOT,
                    env=environment,
                )
                if tested.returncode not in (0, 1):
                    raise RuntimeError("remote experiment was invalid")
                report = json.loads((Path(config["output"]) / "report.json").read_text())
                if (
                    report["all_requests"]["completed"] != report["all_offered"]
                    or report["all_offered"] < 50
                ):
                    raise RuntimeError(
                        "remote smoke did not complete the expected native TCP workload"
                    )
                if (
                    scenario == "worker-delay"
                    and report["phases"]["fault"]["ttft_p95_ms"]
                    < report["phases"]["healthy"]["ttft_p95_ms"] + 200
                ):
                    raise RuntimeError("worker fault did not affect measured client TTFT")
                print(
                    "SSH/package smoke verified:",
                    scenario,
                    "direct:",
                    direct,
                    "SLO verdict:",
                    report["verdict"],
                )
        finally:
            for container in containers:
                subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)
            subprocess.run(["docker", "network", "rm", network], check=False, capture_output=True)
            artifact.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
