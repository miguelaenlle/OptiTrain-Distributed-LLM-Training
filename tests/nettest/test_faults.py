"""Real sockets exercise fault semantics independently of inference timing."""

from __future__ import annotations

import json
import os
import socket
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BINARY = Path(
    os.environ.get(
        "NETFAULT_BINARY",
        Path(__file__).resolve().parents[2] / ".fleet/nettest-bin/optitrain-netfault",
    )
)
pytestmark = pytest.mark.skipif(
    not BINARY.is_file(), reason="build netfault or set NETFAULT_BINARY"
)


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            while data := self.request.recv(65536):
                self.request.sendall(data)
        except (ConnectionError, OSError):
            pass


@pytest.fixture
def proxy(tmp_path):
    echo = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
    echo.daemon_threads = True
    threading.Thread(target=echo.serve_forever, daemon=True).start()
    data_port, control_port = port(), port()
    config = {
        "control": f"127.0.0.1:{control_port}",
        "events": str(tmp_path / "events.jsonl"),
        "max_runtime_s": 30,
        "listeners": [
            {
                "name": "worker-0",
                "listen": f"127.0.0.1:{data_port}",
                "upstream": f"127.0.0.1:{echo.server_address[1]}",
            }
        ],
    }
    path = tmp_path / "proxy.json"
    path.write_text(json.dumps(config))
    log = (tmp_path / "proxy.log").open("wb")
    process = subprocess.Popen(
        [str(BINARY), "proxy", "--config", str(path)], stdout=log, stderr=log
    )

    def control(method="GET", body=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{control_port}/" + ("healthz" if method == "GET" else "rules"),
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)

    deadline = time.monotonic() + 5
    while True:
        try:
            control()
            break
        except OSError:
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail((tmp_path / "proxy.log").read_text())
            time.sleep(0.02)

    def rule(action, direction="response", **kwargs):
        return control(
            "POST",
            {
                "target": "worker-0",
                "direction": direction,
                "action": action,
                "duration_ms": 1500,
                **kwargs,
            },
        )

    yield data_port, rule, control
    process.terminate()
    process.wait(timeout=5)
    log.close()
    echo.shutdown()
    echo.server_close()
    assert "AddressSanitizer" not in (tmp_path / "proxy.log").read_text()
    assert "runtime error:" not in (tmp_path / "proxy.log").read_text()


def receive(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def connect(data_port):
    return socket.create_connection(("127.0.0.1", data_port), timeout=3)


def test_transparent_large_transfer_and_half_close(proxy):
    data_port, _, _ = proxy
    payload = bytes(range(256)) * 4096
    with connect(data_port) as sock:
        thread = threading.Thread(
            target=lambda: (sock.sendall(payload), sock.shutdown(socket.SHUT_WR))
        )
        thread.start()
        assert receive(sock, len(payload)) == payload
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert sock.recv(1) == b""


@pytest.mark.parametrize("direction", ["request", "response"])
def test_delay_is_directional_and_pipelined(proxy, direction):
    data_port, rule, _ = proxy
    rule("delay", direction=direction, delay_ms=100)
    with connect(data_port) as sock:
        began = time.monotonic()
        sock.sendall(b"first")
        assert receive(sock, 5) == b"first"
        assert 0.075 <= time.monotonic() - began < 0.7
        # A burst is delayed once, not once per byte.
        began = time.monotonic()
        sock.sendall(b"x" * 4096)
        assert receive(sock, 4096) == b"x" * 4096
        assert time.monotonic() - began < 0.7


def test_pause_preserves_bytes_and_resumes_on_expiry(proxy):
    data_port, rule, _ = proxy
    rule("stall", duration_ms=200)
    with connect(data_port) as sock:
        began = time.monotonic()
        sock.sendall(b"held")
        assert receive(sock, 4) == b"held"
        assert 0.15 <= time.monotonic() - began < 0.8


def test_pause_can_be_cleared_while_connection_is_active(proxy):
    data_port, rule, control = proxy
    rule("stall")
    with connect(data_port) as sock:
        sock.sendall(b"held")
        time.sleep(0.05)
        control("DELETE")
        assert receive(sock, 4) == b"held"


def test_byte_cutoff_truncates_exactly(proxy):
    data_port, rule, _ = proxy
    rule("cutoff", bytes=5)
    with connect(data_port) as sock:
        sock.sendall(b"abcdefghij")
        assert receive(sock, 100) == b"abcde"


def test_bandwidth_limits_delivery_rate(proxy):
    data_port, rule, _ = proxy
    rule("bandwidth", rate_bytes_s=4096, duration_ms=3000)
    with connect(data_port) as sock:
        began = time.monotonic()
        sock.sendall(b"x" * 4096)
        assert receive(sock, 4096) == b"x" * 4096
        assert 0.75 <= time.monotonic() - began < 2.5


def test_jitter_preserves_byte_order(proxy):
    data_port, rule, _ = proxy
    rule("delay", delay_ms=50, jitter_ms=50, seed=42)
    with connect(data_port) as sock:
        for i in range(20):
            sock.sendall(bytes([i]))
            time.sleep(0.002)
        assert receive(sock, 20) == bytes(range(20))


def test_idle_expiry_closes_quiet_connection(proxy):
    data_port, rule, _ = proxy
    rule("idle", idle_ms=100)
    with connect(data_port) as sock:
        began = time.monotonic()
        assert sock.recv(1) == b""
        assert 0.06 <= time.monotonic() - began < 0.7


def test_disable_refuses_new_connections_but_preserves_existing(proxy):
    data_port, rule, _ = proxy
    with connect(data_port) as existing:
        existing.sendall(b"a")
        assert receive(existing, 1) == b"a"
        rule("disable", direction="request", duration_ms=200)
        with pytest.raises(OSError):
            connect(data_port)
        existing.sendall(b"b")
        assert receive(existing, 1) == b"b"
        time.sleep(0.25)
        with connect(data_port) as restored:
            restored.sendall(b"c")
            assert receive(restored, 1) == b"c"


def test_rejects_unknown_targets_and_overlapping_rules(proxy):
    _, rule, control = proxy
    with pytest.raises(urllib.error.HTTPError):
        control("POST", {"target": "missing", "direction": "response", "action": "stall"})
    rule("delay", delay_ms=20)
    with pytest.raises(urllib.error.HTTPError):
        rule("stall")
    control("DELETE")
    assert rule("stall", duration_ms=10)["ok"]
