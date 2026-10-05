from fastapi.testclient import TestClient
from fleetlab import api


def test_api_run_definition_and_query_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "RUNS", tmp_path)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(api, "JOBS", jobs)
    queued = []
    monkeypatch.setattr(api.POOL, "submit", lambda *args: queued.append(args))
    client = TestClient(api.app)
    response = client.post(
        "/api/runs", json={"regular": 1, "spot": 0, "count": 3, "scenario": "healthy"}
    )
    assert response.status_code == 202
    run_id = response.json()["id"]
    assert client.get("/api/runs/" + run_id).json()["state"] == "queued"
    assert len(queued) == 1 and "--config" in queued[0][2]
    assert client.get("/api/runs/not-a-uuid").status_code == 400
    assert client.get("/api/runs/" + run_id + "/replay").status_code == 404
    assert client.post("/api/runs", json={"regular": 16, "spot": 1}).status_code == 422
    assert client.post("/api/search", json={"q": 0}).status_code == 422


def test_editor_overrides_search_suite_and_cancel(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(api, "RUNS", tmp_path)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(api, "JOBS", jobs)
    queued = []
    monkeypatch.setattr(api.POOL, "submit", lambda *args: queued.append(args))
    client = TestClient(api.app)
    overrides = {
        "prices": {"regular": 9, "spot": 2},
        "fault_time_ns": 123,
        "fault_targets": [1],
        "discovery_ns": 0,
        "completion_threshold": 0.8,
        "availability_ns": 5,
    }
    value = client.post(
        "/api/runs", json={"regular": 1, "spot": 1, "count": 3, "overrides": overrides}
    ).json()
    cfg = json.loads((jobs / (value["id"] + ".config.json")).read_text())
    assert cfg["prices"]["regular"] == 9 and cfg["faults"][0]["time_ns"] == 123
    assert cfg["faults"][0]["workers"] == [1] and cfg["discovery_ns"] == 0
    response = client.post(
        "/api/search",
        json={
            "q": 10,
            "suite": ["healthy", "all_down"],
            "overrides": {k: v for k, v in overrides.items() if k != "fault_targets"},
        },
    )
    assert response.status_code == 202
    command = queued[-1][2]
    assert command[command.index("--suite") + 1] == "healthy,all_down"
    saved = json.loads(open(command[command.index("--overrides") + 1]).read())
    assert saved == {k: v for k, v in overrides.items() if k != "fault_targets"}
    assert client.post("/api/search", json={"q": 10, "overrides": overrides}).status_code == 422
    canceled = client.post("/api/runs/" + value["id"] + "/cancel")
    assert canceled.json()["state"] == "canceled"
    assert client.post("/api/search", json={"q": 10, "suite": []}).status_code == 422


def test_cancel_queued_startup_and_running_atomicity(tmp_path, monkeypatch):
    import threading
    import uuid

    monkeypatch.setattr(api, "JOBS", tmp_path)
    monkeypatch.setattr(api, "RUNS", tmp_path)
    stopped = threading.Event()
    spawning = threading.Event()
    release = threading.Event()
    calls = []

    class Process:
        pid = 4321
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            assert stopped.wait(2)
            self.returncode = -15
            return self.returncode

    def spawn(*args, **kwargs):
        calls.append(kwargs)
        spawning.set()
        assert release.wait(2)
        return Process()

    monkeypatch.setattr(api.subprocess, "Popen", spawn)
    monkeypatch.setattr(api.os, "killpg", lambda pid, sig: stopped.set())
    queued = str(uuid.uuid4())
    api.status(queued, {"id": queued, "state": "queued"})
    api.cancel_run(queued)
    api.execute(queued, ["unused"])
    assert calls == []
    run_id = str(uuid.uuid4())
    api.status(run_id, {"id": run_id, "state": "queued"})
    worker = threading.Thread(target=api.execute, args=(run_id, ["unused"]))
    worker.start()
    assert spawning.wait(2)
    cancel = threading.Thread(target=api.cancel_run, args=(run_id,))
    cancel.start()
    release.set()
    worker.join(3)
    cancel.join(3)
    assert not worker.is_alive() and not cancel.is_alive()
    assert api.status(run_id)["state"] == "canceled" and run_id not in api.ACTIVE
    assert calls[0]["start_new_session"] is True
