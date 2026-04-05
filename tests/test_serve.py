"""Tests for the Colony API server (antfarm.core.serve).

Uses FastAPI TestClient with a fresh FileBackend per test via tmp_path fixture.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from antfarm.core.serve import get_app


@pytest.fixture
def client(tmp_path):
    from antfarm.core.backends.file import FileBackend

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _carry(client, task_id="task-001", title="Test Task", spec="Do the thing"):
    return client.post(
        "/tasks",
        json={"id": task_id, "title": title, "spec": spec},
    )


def _register_worker(client, worker_id="worker-1"):
    return client.post(
        "/workers/register",
        json={
            "worker_id": worker_id,
            "node_id": "node-1",
            "agent_type": "claude-code",
            "workspace_root": "/tmp/ws",
        },
    )


def _forage(client, worker_id="worker-1"):
    return client.post("/tasks/pull", json={"worker_id": worker_id})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_carry_and_list(client):
    r = _carry(client)
    assert r.status_code == 201
    assert r.json()["task_id"] == "task-001"

    r = client.get("/tasks")
    assert r.status_code == 200
    tasks = r.json()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "task-001"


def test_carry_duplicate_returns_409(client):
    _carry(client)
    r = _carry(client)
    assert r.status_code == 409


def test_forage_returns_task(client):
    _carry(client)
    r = _forage(client)
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "task-001"
    assert data["status"] == "active"


def test_forage_empty_returns_204(client):
    r = _forage(client)
    assert r.status_code == 204


def test_forage_creates_attempt(client):
    _carry(client)
    r = _forage(client)
    assert r.status_code == 200
    data = r.json()
    assert data["current_attempt"] is not None
    assert len(data["attempts"]) == 1
    assert data["attempts"][0]["status"] == "active"


def test_trail_appends(client):
    _carry(client)
    task = _forage(client).json()
    task_id = task["id"]

    r = client.post(
        f"/tasks/{task_id}/trail",
        json={"worker_id": "worker-1", "message": "halfway done"},
    )
    assert r.status_code == 200

    r = client.get(f"/tasks/{task_id}")
    assert r.status_code == 200
    trail = r.json()["trail"]
    assert len(trail) == 1
    assert trail[0]["message"] == "halfway done"
    assert trail[0]["worker_id"] == "worker-1"


def test_harvest_transitions(client):
    _carry(client)
    task = _forage(client).json()
    task_id = task["id"]
    attempt_id = task["current_attempt"]

    r = client.post(
        f"/tasks/{task_id}/harvest",
        json={
            "attempt_id": attempt_id,
            "pr": "https://github.com/x/y/pull/1",
            "branch": "feat/task-001",
        },
    )
    assert r.status_code == 200

    r = client.get(f"/tasks/{task_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "done"
    assert data["attempts"][0]["status"] == "done"
    assert data["attempts"][0]["pr"] == "https://github.com/x/y/pull/1"


def test_harvest_wrong_attempt_returns_409(client):
    _carry(client)
    task = _forage(client).json()
    task_id = task["id"]

    r = client.post(
        f"/tasks/{task_id}/harvest",
        json={"attempt_id": "wrong-attempt-id", "pr": "pr-url", "branch": "feat/x"},
    )
    assert r.status_code == 409


def test_heartbeat_updates_worker(client):
    _register_worker(client)
    r = client.post(
        "/workers/worker-1/heartbeat",
        json={"status": {"state": "working"}},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_status_returns_summary(client):
    _carry(client)
    r = client.get("/status")
    assert r.status_code == 200
    data = r.json()
    assert "tasks" in data
    assert data["tasks"]["ready"] == 1
    assert data["tasks"]["active"] == 0
    assert data["tasks"]["done"] == 0


def test_guard_acquire_and_release(client):
    r = client.post("/guards/repo/main", json={"owner": "node-1/worker-1"})
    assert r.status_code == 200
    assert r.json()["acquired"] is True

    # Second acquire from different owner should fail
    r = client.post("/guards/repo/main", json={"owner": "node-1/worker-2"})
    assert r.status_code == 200
    assert r.json()["acquired"] is False

    # Release by owner
    r = client.delete("/guards/repo/main", params={"owner": "node-1/worker-1"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Now re-acquire should succeed
    r = client.post("/guards/repo/main", json={"owner": "node-1/worker-2"})
    assert r.status_code == 200
    assert r.json()["acquired"] is True


def test_register_node_idempotent(client):
    r = client.post("/nodes", json={"node_id": "node-1"})
    assert r.status_code == 200

    # Second registration should also succeed (idempotent)
    r = client.post("/nodes", json={"node_id": "node-1"})
    assert r.status_code == 200

    r = client.get("/status")
    assert r.json()["nodes"] == 1


def test_signal_appends(client):
    _carry(client)
    task = _forage(client).json()
    task_id = task["id"]

    r = client.post(
        f"/tasks/{task_id}/signal",
        json={"worker_id": "worker-1", "message": "task needs re-scoping"},
    )
    assert r.status_code == 200

    r = client.get(f"/tasks/{task_id}")
    assert r.status_code == 200
    signals = r.json()["signals"]
    assert len(signals) == 1
    assert signals[0]["message"] == "task needs re-scoping"
    assert signals[0]["worker_id"] == "worker-1"


# ---------------------------------------------------------------------------
# Scent (SSE) tests
# ---------------------------------------------------------------------------


def test_scent_returns_sse_stream(tmp_path):
    """Carry a task, add a trail entry, then verify it appears in SSE output."""
    from antfarm.core.backends.file import FileBackend

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend)
    client = TestClient(app)

    _carry(client)
    task = _forage(client).json()
    task_id = task["id"]

    client.post(
        f"/tasks/{task_id}/trail",
        json={"worker_id": "worker-1", "message": "step one done"},
    )

    with client.stream("GET", f"/scent/{task_id}?timeout=2") as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        lines = []
        for line in r.iter_lines():
            if line.startswith("data: "):
                lines.append(line)
        assert any("step one done" in line for line in lines)


def test_scent_404_unknown_task(client):
    """GET /scent/{nonexistent} returns 404."""
    r = client.get("/scent/nonexistent?timeout=1")
    assert r.status_code == 404


def test_scent_new_entries(tmp_path):
    """Entries appended mid-stream appear in SSE output."""
    import json

    from antfarm.core.backends.file import FileBackend

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend)
    client = TestClient(app)

    _carry(client)
    task = _forage(client).json()
    task_id = task["id"]

    # Append a trail entry from a background thread after a short delay
    def _append_later():
        time.sleep(0.5)
        client.post(
            f"/tasks/{task_id}/trail",
            json={"worker_id": "worker-1", "message": "late entry"},
        )

    t = threading.Thread(target=_append_later, daemon=True)
    t.start()

    messages = []
    with client.stream("GET", f"/scent/{task_id}?timeout=3&poll_interval=0.2") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data: "):
                entry = json.loads(line[len("data: "):])
                messages.append(entry.get("message", ""))

    t.join(timeout=5)
    assert "late entry" in messages
