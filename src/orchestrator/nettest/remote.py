"""Existing-host SSH adapter. Host placement lives in the experiment config."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import select
import shlex
import subprocess
import threading
from pathlib import Path


class SSHRuntime:
    def __init__(self, host, root, package, expected_platform):
        alias = host["ssh_host"]
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", alias):
            raise ValueError("ssh_host must be an SSH config alias or user@hostname")
        self.root = Path(root)
        self.alias = alias
        self.lock = threading.Lock()
        options = (
            ["-F", str(Path(host["ssh_config"]).expanduser().resolve())]
            if host.get("ssh_config")
            else []
        )
        self.ssh = [
            "ssh",
            *options,
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=10",
            alias,
        ]
        package = Path(package).resolve()
        if not package.is_file():
            raise ValueError("remote mode requires an existing Linux package")
        self._shell(["mkdir", "-p", str(root)])
        subprocess.run(
            [
                "scp",
                *options,
                "-q",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                str(package),
                f"{alias}:{root}/bundle.tar.gz",
            ],
            check=True,
            timeout=120,
        )
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        # Python 3.12 on the supported Ubuntu 24.04 target supplies safe tar filters.
        script = (
            "import hashlib, pathlib, tarfile; "
            f"p=pathlib.Path({str(root)!r})/'bundle.tar.gz'; "
            f"assert hashlib.sha256(p.read_bytes()).hexdigest()=={digest!r}; "
            "tarfile.open(p).extractall(p.parent, filter='data')"
        )
        self._shell(["python3", "-c", script])
        self.process = subprocess.Popen(
            [*self.ssh, shlex.join(["python3", str(self.root / "agent.py"), str(root)])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            info = self.call({"op": "ping"})
            arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(info["machine"])
            if info["system"] != "Linux" or f"linux-{arch}" != expected_platform:
                raise ValueError(
                    f"artifact platform {expected_platform} does not match remote {info}"
                )
            # Fail before a measured run if glibc/runtime dependencies are missing.
            self._shell([str(self.root / "bin/optitrain-netfault"), "--version"])
        except Exception:
            self.close()
            raise

    def _shell(self, argv):
        return subprocess.run(
            [*self.ssh, shlex.join(argv)], check=True, capture_output=True, text=True, timeout=30
        )

    def call(self, command):
        with self.lock:
            if self.process.poll() is not None:
                raise RuntimeError("SSH control agent exited")
            self.process.stdin.write(json.dumps(command) + "\n")
            self.process.stdin.flush()
            ready, _, _ = select.select([self.process.stdout], [], [], 15)
            if not ready:
                raise TimeoutError("SSH control response timed out")
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("SSH control connection closed")
            response = json.loads(line)
            if not response["ok"]:
                raise RuntimeError(response["error"])
            return response["result"]

    def close(self):
        process = getattr(self, "process", None)
        if process is None:
            return
        try:
            if process.poll() is None:
                # The remote lease still cleans up if the control channel died.
                with contextlib.suppress(OSError, RuntimeError, TimeoutError, ValueError):
                    self.call({"op": "shutdown"})
        finally:
            if process.stdin:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
