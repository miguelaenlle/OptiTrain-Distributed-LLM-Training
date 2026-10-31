"""Small process/file control layer, shared by localhost and the SSH agent.

All test data goes through native TCP, never through this control protocol.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import platform
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


class Runtime:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.processes = {}
        self.logs = []

    def path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("path escapes run directory")
        return path

    def call(self, command):
        operation = command["op"]
        if operation == "ping":
            return {
                "system": platform.system(),
                "machine": platform.machine(),
                "unix_s": time.time(),
            }
        if operation == "stats":
            snapshot = {
                "load_average": list(os.getloadavg()),
                "cpu_count": os.cpu_count(),
                "processes": {},
            }
            for name, process in self.processes.items():
                row = {"pid": process.pid, "returncode": process.poll()}
                try:
                    if platform.system() == "Linux":
                        status = Path(f"/proc/{process.pid}/status").read_text()
                        for line in status.splitlines():
                            if line.startswith("VmRSS:"):
                                row["rss_bytes"] = int(line.split()[1]) * 1024
                    elif row["returncode"] is None:
                        observed = subprocess.run(
                            ["/bin/ps", "-o", "rss=,%cpu=", "-p", str(process.pid)],
                            capture_output=True,
                            text=True,
                            timeout=2,
                        )
                        values = observed.stdout.split()
                        if len(values) == 2:
                            row.update(
                                rss_bytes=int(values[0]) * 1024, cpu_percent=float(values[1])
                            )
                except (OSError, ValueError, subprocess.SubprocessError):
                    row["resource_observation"] = "unavailable"
                snapshot["processes"][name] = row
            return snapshot
        if operation == "write":
            path = self.path(command["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            data = base64.b64decode(command["data"], validate=True)
            temporary.write_bytes(data)
            temporary.replace(path)
            return {"bytes": len(data)}
        if operation == "registry":
            folder = self.path("registry")
            folder.mkdir(exist_ok=True)
            for worker in command["workers"]:
                doc = {**worker, "last_seen": time.time()}
                path = folder / (worker["worker_id"] + ".json")
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(doc))
                temporary.replace(path)
            return {"ok": True}
        if operation == "read":
            path = self.path(command["path"])
            return {"data": base64.b64encode(path.read_bytes()).decode() if path.exists() else ""}
        if operation == "ports":
            sockets = []
            try:
                for _ in range(command["count"]):
                    sock = socket.socket()
                    sock.bind(("0.0.0.0", 0))
                    sockets.append(sock)
                return {"ports": [sock.getsockname()[1] for sock in sockets]}
            finally:
                for sock in sockets:
                    sock.close()
        if operation == "spawn":
            name = command["name"]
            if name in self.processes:
                raise ValueError("duplicate process name")
            argv = command["argv"]
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                raise ValueError("argv must be a string list")
            log = self.path(name + ".log").open("wb")
            self.logs.append(log)
            env = {**os.environ, **command.get("env", {})}
            self.processes[name] = subprocess.Popen(
                argv,
                cwd=self.root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            return {"pid": self.processes[name].pid}
        if operation == "poll":
            return {"returncode": self.processes[command["name"]].poll()}
        if operation == "http":
            # Control is deliberately loopback-only, including from the remote agent.
            url = command["url"]
            if not url.startswith("http://127.0.0.1:"):
                raise ValueError("management HTTP must be loopback")
            request = urllib.request.Request(
                url,
                method=command.get("method", "GET"),
                data=json.dumps(command["body"]).encode() if "body" in command else None,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.load(response)
        if operation == "stop":
            self.close()
            return {"ok": True}
        raise ValueError(f"unknown runtime operation: {operation}")

    def close(self):
        live = [p for p in self.processes.values() if p.poll() is None]
        for process in live:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        for process in live:
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        for log in self.logs:
            log.close()
        self.logs.clear()


def write_json(runtime, name, data):
    runtime.call(
        {
            "op": "write",
            "path": name,
            "data": base64.b64encode((json.dumps(data, indent=2) + "\n").encode()).decode(),
        }
    )


def read_bytes(runtime, name):
    return base64.b64decode(runtime.call({"op": "read", "path": name})["data"])
