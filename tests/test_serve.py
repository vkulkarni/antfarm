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


def test_task_count_endpoint(client):
    """GET /tasks/count returns task counts by status."""
    _carry(client, task_id="task-001")
    _carry(client, task_id="task-002")

    r = client.get("/tasks/count")
    assert r.status_code == 200
    data = r.json()
    assert data["ready"] == 2
    assert data["active"] == 0
    assert data["done"] == 0


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



def test_carry_with_capabilities_and_pull_with_capable_worker(tmp_path):
    """carry accepts capabilities_required; capable worker gets the task, incapable gets none."""
    from antfarm.core.backends.file import FileBackend

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend)
    client = TestClient(app)

    # Submit a task requiring "gpu"
    r = client.post(
        "/tasks",
        json={
            "id": "task-gpu",
            "title": "GPU task",
            "spec": "needs gpu",
            "capabilities_required": ["gpu"],
        },
    )
    assert r.status_code == 201

    # Register a capable worker
    r = client.post(
        "/workers/register",
        json={
            "worker_id": "worker-gpu",
            "node_id": "node-1",
            "agent_type": "generic",
            "workspace_root": "/tmp",
            "capabilities": ["gpu"],
        },
    )
    assert r.status_code == 201

    # Register a non-capable worker
    r = client.post(
        "/workers/register",
        json={
            "worker_id": "worker-cpu",
            "node_id": "node-1",
            "agent_type": "generic",
            "workspace_root": "/tmp",
            "capabilities": [],
        },
    )
    assert r.status_code == 201

    # Non-capable worker gets nothing
    r = client.post("/tasks/pull", json={"worker_id": "worker-cpu"})
    assert r.status_code == 204

    # Capable worker gets the task
    r = client.post("/tasks/pull", json={"worker_id": "worker-gpu"})
    assert r.status_code == 200
    assert r.json()["id"] == "task-gpu"


# ---------------------------------------------------------------------------
# Pin / unpin endpoints
# ---------------------------------------------------------------------------


def test_pin_task_endpoint(client):
    """POST /tasks/{id}/pin sets pinned_to and returns ok."""
    _carry(client)
    r = client.post("/tasks/task-001/pin", json={"worker_id": "worker-pinned"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    task = client.get("/tasks/task-001").json()
    assert task["pinned_to"] == "worker-pinned"


def test_unpin_task_endpoint(client):
    """POST /tasks/{id}/unpin clears pinned_to and returns ok."""
    _carry(client)
    client.post("/tasks/task-001/pin", json={"worker_id": "worker-pinned"})
    r = client.post("/tasks/task-001/unpin")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    task = client.get("/tasks/task-001").json()
    assert task["pinned_to"] is None


def test_pin_nonexistent_task_returns_404(client):
    """POST /tasks/{id}/pin returns 404 for unknown task."""
    r = client.post("/tasks/no-such-task/pin", json={"worker_id": "worker-1"})
    assert r.status_code == 404


def test_unpin_nonexistent_task_returns_404(client):
    """POST /tasks/{id}/unpin returns 404 for unknown task."""
    r = client.post("/tasks/no-such-task/unpin")
    assert r.status_code == 404


def test_pinned_task_not_pulled_by_wrong_worker(client):
    """Worker that doesn't match pinned_to gets 204 on pull."""
    _carry(client)
    client.post("/tasks/task-001/pin", json={"worker_id": "worker-pinned"})
    r = client.post("/tasks/pull", json={"worker_id": "worker-other"})
    assert r.status_code == 204


def test_pinned_task_pulled_by_correct_worker(client):
    """Worker matching pinned_to successfully pulls the task."""
    _carry(client)
    client.post("/tasks/task-001/pin", json={"worker_id": "worker-pinned"})
    r = client.post("/tasks/pull", json={"worker_id": "worker-pinned"})
    assert r.status_code == 200
    assert r.json()["id"] == "task-001"


# ---------------------------------------------------------------------------
# Override order
# ---------------------------------------------------------------------------


def _carry_and_harvest_serve(client, task_id="task-001"):
    """Carry a task, pull it as worker-1, and harvest it."""
    _register_worker(client)
    _carry(client, task_id=task_id)
    forage_r = _forage(client)
    assert forage_r.status_code == 200
    task = forage_r.json()
    client.post(
        f"/tasks/{task_id}/harvest",
        json={"attempt_id": task["current_attempt"], "pr": "pr", "branch": "branch"},
    ).raise_for_status()
    return task


def test_override_order_sets_and_returns_ok(client):
    """POST /tasks/{id}/override-order sets merge_override and returns ok."""
    _carry_and_harvest_serve(client)
    r = client.post("/tasks/task-001/override-order", json={"position": 3})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    task = client.get("/tasks/task-001").json()
    assert task["merge_override"] == 3


def test_clear_override_order_resets_field(client):
    """DELETE /tasks/{id}/override-order clears merge_override to None."""
    _carry_and_harvest_serve(client)
    client.post("/tasks/task-001/override-order", json={"position": 3}).raise_for_status()

    r = client.delete("/tasks/task-001/override-order")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    task = client.get("/tasks/task-001").json()
    assert task["merge_override"] is None


def test_override_order_not_found_returns_404(client):
    """POST /tasks/{id}/override-order returns 404 for unknown task."""
    r = client.post("/tasks/nonexistent/override-order", json={"position": 1})
    assert r.status_code == 404


def test_clear_override_order_not_found_returns_404(client):
    """DELETE /tasks/{id}/override-order returns 404 for unknown task."""
    r = client.delete("/tasks/nonexistent/override-order")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Rate limit: heartbeat with rate limit fields
# ---------------------------------------------------------------------------


def test_heartbeat_stores_rate_limit_fields(client):
    """POST /workers/{id}/heartbeat persists remaining, reset_at, cooldown_until."""
    from datetime import UTC, datetime, timedelta

    _register_worker(client, "worker-rl")
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    r = client.post(
        "/workers/worker-rl/heartbeat",
        json={
            "status": {},
            "remaining": 10,
            "reset_at": "2026-01-01T00:00:00+00:00",
            "cooldown_until": future,
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # Confirm the fields are reflected in the workers list
    workers = client.get("/workers").json()
    worker = next(w for w in workers if w["worker_id"] == "worker-rl")
    assert worker["remaining"] == 10
    assert worker["reset_at"] == "2026-01-01T00:00:00+00:00"
    assert worker["cooldown_until"] == future


def test_heartbeat_without_rate_limit_fields(client):
    """POST /workers/{id}/heartbeat with no rate limit fields still succeeds."""
    _register_worker(client, "worker-basic")
    r = client.post("/workers/worker-basic/heartbeat", json={"status": {}})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Rate limit: GET /workers endpoint
# ---------------------------------------------------------------------------


def test_workers_list_empty(client):
    """GET /workers returns empty list when no workers registered."""
    r = client.get("/workers")
    assert r.status_code == 200
    assert r.json() == []


def test_workers_list_returns_registered(client):
    """GET /workers lists all registered workers."""
    _register_worker(client, "worker-a")
    _register_worker(client, "worker-b")

    r = client.get("/workers")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    ids = {w["worker_id"] for w in data}
    assert ids == {"worker-a", "worker-b"}


def test_forage_skips_rate_limited_worker(client):
    """POST /tasks/pull returns 204 when worker is in cooldown."""
    from datetime import UTC, datetime, timedelta

    _carry(client)
    _register_worker(client, "worker-rl")
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    # Set cooldown via heartbeat
    client.post(
        "/workers/worker-rl/heartbeat",
        json={"status": {}, "cooldown_until": future},
    ).raise_for_status()

    r = client.post("/tasks/pull", json={"worker_id": "worker-rl"})
    assert r.status_code == 204
