"""Controller loss, process ownership and persisted evidence across the agent."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

from orchestrator.nettest.config import ROOT
from orchestrator.nettest.runtime import Runtime


def test_agent_stops_children_on_controller_disconnect(tmp_path):
    code = "from orchestrator.nettest.agent import serve; import sys; serve(sys.argv[1], .5)"
    agent = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    command = {
        "op": "spawn",
        "name": "owned",
        "argv": [sys.executable, "-c", "import time; time.sleep(60)"],
    }
    agent.stdin.write(json.dumps(command) + "\n")
    agent.stdin.flush()
    response = json.loads(agent.stdout.readline())
    assert response["ok"]
    pid = response["result"]["pid"]
    agent.stdin.close()
    assert agent.wait(timeout=5) == 0
    agent.stdout.close()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("agent left an owned process alive")


def test_stopping_processes_preserves_readable_logs(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.call(
        {
            "op": "write",
            "path": "evidence.jsonl",
            "data": base64.b64encode(b'{"ok":true}\n').decode(),
        }
    )
    runtime.call({"op": "stop"})
    response = runtime.call({"op": "read", "path": "evidence.jsonl"})
    assert base64.b64decode(response["data"]) == b'{"ok":true}\n'


def test_runtime_refuses_evidence_path_escape(tmp_path):
    import pytest

    runtime = Runtime(tmp_path)
    with pytest.raises(ValueError):
        runtime.call({"op": "write", "path": "../outside", "data": "eA=="})
