"""SSH control agent: JSON lines, process ownership and a controller-loss lease.

Invoked only by the remote runner, never by an inference request. No AWS APIs.
"""

from __future__ import annotations

import json
import select
import sys

# Also runnable as a staged standalone pair of Python files.
try:
    from .runtime import Runtime
except ImportError:
    from runtime import Runtime


def serve(root, lease_s=60):
    runtime = Runtime(root)
    try:
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], lease_s)
            if not ready:
                break
            line = sys.stdin.readline()
            if not line:
                break
            command = {}
            try:
                command = json.loads(line)
                result = {
                    "ok": True,
                    "result": runtime.call(
                        {"op": "stop"} if command.get("op") == "shutdown" else command
                    ),
                }
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            print(json.dumps(result), flush=True)
            if command.get("op") == "shutdown":
                break
    finally:
        runtime.close()


if __name__ == "__main__":
    serve(sys.argv[1])
