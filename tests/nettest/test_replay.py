"""The CPU replay endpoint produces a real stream and frees canceled work."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from orchestrator.nettest.config import ROOT

BINARY = Path(os.environ.get("NETFAULT_BINARY", ROOT / ".fleet/nettest-bin/optitrain-netfault"))
pytestmark = pytest.mark.skipif(
    not BINARY.is_file(), reason="build netfault or set NETFAULT_BINARY"
)


@pytest.fixture
def worker(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    profile = {
        "kind": "synthetic",
        "samples": [
            {"input_tokens": 10, "output_tokens": 4, "first_output_ms": 80, "gaps_ms": [30] * 3}
        ],
    }
    (tmp_path / "profile.json").write_text(json.dumps(profile))
    config = {
        "max_runtime_s": 10,
        "events": str(tmp_path / "events.jsonl"),
        "workers": [
            {
                "id": "worker-0",
                "listen": f"127.0.0.1:{port}",
                "profile": str(tmp_path / "profile.json"),
            }
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    log = (tmp_path / "worker.log").open("wb")
    process = subprocess.Popen(
        [str(BINARY), "replay", "--config", str(path)], stdout=log, stderr=log
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5
    while True:
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=1):
                break
        except OSError:
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail((tmp_path / "worker.log").read_text())
            time.sleep(0.01)
    yield url, tmp_path
    process.terminate()
    assert process.wait(timeout=5) == 0
    log.close()
    assert "runtime error:" not in (tmp_path / "worker.log").read_text()


def request(url, **kwargs):
    return urllib.request.urlopen(
        urllib.request.Request(
            url + "/v1/completions",
            method="POST",
            data=json.dumps(
                {"stream": True, "replay_input_tokens": 10, "max_tokens": 4, **kwargs}
            ).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=2,
    )


def test_replay_emits_output_before_completion(worker):
    url, _ = worker
    began = time.monotonic()
    with request(url) as response:
        assert response.headers["Content-Type"] == "text/event-stream"
        first = response.readline()
        first_delay = time.monotonic() - began
        rest = response.read()
    assert b'"text":"x"' in first
    assert 0.06 <= first_delay < 0.4
    assert rest.count(b'"text":"x"') == 3
    assert b"data: [DONE]" in rest
    assert time.monotonic() - began >= 0.15


def test_unsupported_shape_is_rejected_without_fallback(worker):
    url, _ = worker
    with pytest.raises(urllib.error.HTTPError) as error:
        request(url, max_tokens=99)
    assert error.value.code == 422


def test_canceled_generation_releases_worker(worker):
    url, folder = worker
    with request(url, request_id="canceled") as response:
        response.readline()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        records = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
        if any(
            r.get("event") == "generation_canceled" and r.get("request_id") == "canceled"
            for r in records
        ):
            break
        time.sleep(0.01)
    else:
        pytest.fail("generation was not canceled")
    with request(url, request_id="next") as response:
        assert b"[DONE]" in response.read()
