"""Local CPU job API. Immutable run artifacts are owned by the C++ protocol."""

import json
import math
import os
import signal
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .adapter import ROOT
from .core import call
from .engine import validate
from .scenarios import SUITE, experiment

RUNS = Path(os.environ.get("FLEETLAB_RUNS", ROOT / "fleet-lab/runs")).resolve()
RUNS.mkdir(parents=True, exist_ok=True)
JOBS = RUNS / "jobs"
JOBS.mkdir(exist_ok=True)
POOL = ThreadPoolExecutor(max_workers=2)
LOCK = threading.RLock()
ACTIVE = {}
app = FastAPI(title="OptiTrain Fleet Lab", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


def location(run_id):
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(400, "Invalid run ID")
    return RUNS / run_id


def status(run_id, value=None):
    with LOCK:
        path = JOBS / f"{run_id}.status.json"
        if value is not None:
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(value))
            temp.replace(path)
        return json.loads(path.read_text()) if path.exists() else None


def execute(run_id, command):
    process = None
    try:
        with (JOBS / f"{run_id}.log").open("w") as log:
            with LOCK:
                if status(run_id).get("state") == "canceled":
                    return
                status(run_id, {"id": run_id, "state": "running"})
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    env={**os.environ, "PYTHONPATH": str(ROOT / "fleet-lab/python")},
                )
                ACTIVE[run_id] = process
                if status(run_id).get("state") == "canceled":
                    os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1900)
        if status(run_id).get("state") != "canceled":
            status(
                run_id,
                {
                    "id": run_id,
                    "state": "completed" if process.returncode == 0 else "failed",
                    "returncode": process.returncode,
                },
            )
    except Exception as exc:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        if status(run_id).get("state") != "canceled":
            status(run_id, {"id": run_id, "state": "failed", "error": str(exc)})
    finally:
        with LOCK:
            ACTIVE.pop(run_id, None)


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    location(run_id)
    with LOCK:
        current = status(run_id)
        if current is None:
            raise HTTPException(404, "Unknown run")
        if current["state"] in ("completed", "failed", "canceled"):
            return current
        status(run_id, {"id": run_id, "state": "canceled"})
        process = ACTIVE.get(run_id)
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        return status(run_id)


@app.get("/api/runs")
def runs():
    values = []
    for p in sorted(JOBS.glob("*.status.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        value = json.loads(p.read_text())
        path = location(value["id"])
        for name in ("metrics", "search"):
            if (path / f"{name}.json").exists():
                value[name] = json.loads((path / f"{name}.json").read_text())
        values.append(value)
    return values


@app.post("/api/runs", status_code=202)
def create_run(value: dict):
    # The visual editor reruns from time zero; a replay query cannot alter a run.
    try:
        cfg = experiment(
            regular=value.get("regular", 8),
            spot=value.get("spot", 8),
            policy=value.get("policy", "round_robin"),
            scenario=value.get("scenario", "half"),
            rate=value.get("rate", 64),
            seed=value.get("seed", 42),
            count=value.get("count", 1000),
            overrides=value.get("overrides"),
        )
        validate(cfg)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, str(exc))
    run_id = str(uuid.uuid4())
    cfg_path = JOBS / f"{run_id}.config.json"
    cfg_path.write_text(json.dumps(cfg))
    status(run_id, {"id": run_id, "state": "queued"})
    POOL.submit(
        execute,
        run_id,
        [
            sys.executable,
            "-m",
            "fleetlab.cli",
            "run",
            "--config",
            str(cfg_path),
            "--output",
            str(location(run_id)),
        ],
    )
    return {"id": run_id, "state": "queued"}


@app.post("/api/search", status_code=202)
def create_search(value: dict):
    q = value.get("q")
    if not isinstance(q, (float, int)) or not math.isfinite(q) or q <= 0:
        raise HTTPException(422, "Provide the measured calibration q")
    suite = value.get("suite", SUITE)
    overrides = value.get("overrides", {})
    if not isinstance(suite, list) or not suite or len(suite) != len(set(suite)):
        raise HTTPException(422, "Select a nonempty unique required suite")
    if any(
        key in overrides
        for key in (
            "fault_targets",
            "fault_count",
            "placement",
            "regular",
            "spot",
            "policy",
            "requests",
            "horizon_ns",
        )
    ):
        raise HTTPException(
            422,
            (
                "Search uses relative scenario targets; "
                "exact targets/counts are for fixed-fleet runs"
            ),
        )
    try:
        for name in suite:
            validate(
                experiment(
                    8,
                    8,
                    scenario="half" if name == "half_high" else name,
                    count=1,
                    overrides=overrides,
                )
            )
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, str(exc))
    run_id = str(uuid.uuid4())
    overrides_path = JOBS / f"{run_id}.overrides.json"
    overrides_path.write_text(json.dumps(overrides))
    status(run_id, {"id": run_id, "state": "queued"})
    POOL.submit(
        execute,
        run_id,
        [
            sys.executable,
            "-m",
            "fleetlab.cli",
            "search",
            "--q",
            str(q),
            "--suite",
            ",".join(suite),
            "--overrides",
            str(overrides_path),
            "--count",
            str(value.get("count", 1000)),
            "--seed",
            str(value.get("seed", 42)),
            "--wall-seconds",
            str(min(value.get("wall_seconds", 1800), 1800)),
            "--max-runs",
            str(min(value.get("max_runs", 3200), 3200)),
            "--output",
            str(location(run_id)),
        ],
    )
    return {"id": run_id, "state": "queued"}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    path = location(run_id)
    state = status(run_id)
    if state is None:
        raise HTTPException(404, "Unknown run")
    result = dict(state)
    for name in ("manifest", "metrics", "summary", "search", "progress"):
        file = path / f"{name}.json"
        if file.exists():
            result[name] = json.loads(file.read_text())
    return result


@app.get("/api/runs/{run_id}/replay")
def replay(
    run_id: str,
    until_ns: int = 2**63 - 1,
    from_ns: int = 0,
    limit: int = 1000,
    worker: int | None = None,
    request: int | None = None,
    event: str | None = None,
):
    path = location(run_id) / "events.jsonl"
    if not path.exists():
        raise HTTPException(404, "Full replay is unavailable for this run")
    command = {
        "op": "replay",
        "path": str(path),
        "until_ns": until_ns,
        "from_ns": from_ns,
        "limit": max(1, min(limit, 5000)),
    }
    for key, value in (("worker", worker), ("request", request), ("event", event)):
        if value is not None:
            command[key] = value
    return call(command)


WEB = ROOT / "fleet-lab/web/dist"
if WEB.exists():
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
