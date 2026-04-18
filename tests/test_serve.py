"""Tests for the Colony API server (antfarm.core.serve).

Uses FastAPI TestClient with a fresh FileBackend per test via tmp_path fixture.
"""

from __future__ import annotations

import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

import antfarm.core.serve as serve_mod
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


def test_register_worker_duplicate_returns_409(client):
    """Registering the same live worker twice returns 409 on the second call."""
    r = _register_worker(client, "worker-dup")
    assert r.status_code == 201

    r = _register_worker(client, "worker-dup")
    assert r.status_code == 409


def test_register_worker_stale_allows_reregister(client):
    """After the worker file's mtime is aged past guard_ttl, re-register returns 201."""
    r = _register_worker(client, "worker-stale")
    assert r.status_code == 201

    backend = serve_mod._backend
    worker_path = backend._worker_path("worker-stale")
    stale = time.time() - (backend._guard_ttl + 60)
    os.utime(str(worker_path), (stale, stale))

    r = _register_worker(client, "worker-stale")
    assert r.status_code == 201


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


def test_task_count(client):
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


def test_register_node_extended(client):
    r = client.post(
        "/nodes",
        json={
            "node_id": "node-ext",
            "runner_url": "http://mini-2:7433",
            "max_workers": 2,
            "capabilities": ["gpu", "large-context"],
        },
    )
    assert r.status_code == 200
    assert r.json()["node_id"] == "node-ext"

    # Verify fields persisted
    r = client.get("/nodes/node-ext")
    assert r.status_code == 200
    data = r.json()
    assert data["runner_url"] == "http://mini-2:7433"
    assert data["max_workers"] == 2
    assert data["capabilities"] == ["gpu", "large-context"]


def test_get_nodes_list(client):
    client.post("/nodes", json={"node_id": "node-a"})
    client.post("/nodes", json={"node_id": "node-b"})

    r = client.get("/nodes")
    assert r.status_code == 200
    nodes = r.json()
    node_ids = [n["node_id"] for n in nodes]
    assert "node-a" in node_ids
    assert "node-b" in node_ids


def test_get_node_detail(client):
    client.post("/nodes", json={"node_id": "node-detail"})
    r = client.get("/nodes/node-detail")
    assert r.status_code == 200
    assert r.json()["node_id"] == "node-detail"


def test_get_node_not_found(client):
    r = client.get("/nodes/nonexistent")
    assert r.status_code == 404


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
                entry = json.loads(line[len("data: ") :])
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
# Worker activity endpoint (#239)
# ---------------------------------------------------------------------------


def test_activity_endpoint_stamps_timestamp(client):
    """POST /workers/{id}/activity sets current_action and current_action_at."""
    _register_worker(client, "worker-act")
    r = client.post(
        "/workers/worker-act/activity",
        json={"action": "Running: Bash"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    workers = client.get("/workers").json()
    worker = next(w for w in workers if w["worker_id"] == "worker-act")
    assert worker["current_action"] == "Running: Bash"
    assert worker["current_action_at"] is not None


def test_activity_endpoint_clears_with_null(client):
    """POST /workers/{id}/activity with null action clears both fields."""
    _register_worker(client, "worker-act")
    client.post("/workers/worker-act/activity", json={"action": "Running: Bash"})
    client.post("/workers/worker-act/activity", json={"action": None})

    worker = next(w for w in client.get("/workers").json() if w["worker_id"] == "worker-act")
    assert worker["current_action"] is None
    assert worker["current_action_at"] is None


def test_activity_independent_from_heartbeat(client):
    """Activity endpoint must not touch last_heartbeat."""
    _register_worker(client, "worker-act")
    before = next(w for w in client.get("/workers").json() if w["worker_id"] == "worker-act")
    original_hb = before["last_heartbeat"]

    import time as _time

    _time.sleep(0.01)
    r = client.post("/workers/worker-act/activity", json={"action": "Running: Bash"})
    assert r.status_code == 200

    after = next(w for w in client.get("/workers").json() if w["worker_id"] == "worker-act")
    assert after["last_heartbeat"] == original_hb


def test_activity_endpoint_unknown_worker_noop(client):
    """Unknown worker is a silent 200 no-op (never creates a worker record)."""
    r = client.post("/workers/ghost-worker/activity", json={"action": "Running: Bash"})
    assert r.status_code == 200
    # No worker record should have been created
    assert client.get("/workers").json() == []


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


# ---------------------------------------------------------------------------
# Soldier auto-start (#99)
# ---------------------------------------------------------------------------


def test_soldier_disabled_flag(tmp_path):
    """get_app(enable_soldier=False) leaves _soldier_status as 'not started'."""
    from antfarm.core.backends.file import FileBackend

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend, enable_soldier=False)
    client = TestClient(app)

    r = client.get("/status")
    assert r.status_code == 200
    assert r.json()["soldier"] == "not started"


def test_soldier_singleton_guard(tmp_path):
    """Calling _start_soldier_thread twice doesn't spawn a second thread."""
    import antfarm.core.serve as serve_mod
    from antfarm.core.backends.file import FileBackend

    backend = FileBackend(root=str(tmp_path / ".antfarm"))

    # Reset module state
    serve_mod._soldier_thread = None
    serve_mod._soldier_status = "not started"

    serve_mod._start_soldier_thread(backend, str(tmp_path / ".antfarm"))
    first_thread = serve_mod._soldier_thread
    assert first_thread is not None

    serve_mod._start_soldier_thread(backend, str(tmp_path / ".antfarm"))
    assert serve_mod._soldier_thread is first_thread  # same thread, not a new one

    # Cleanup
    serve_mod._soldier_thread = None
    serve_mod._soldier_status = "not started"


def test_status_full_includes_soldier(tmp_path):
    """GET /status/full includes 'soldier' key."""
    from antfarm.core.backends.file import FileBackend

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend, enable_soldier=False)
    client = TestClient(app)

    r = client.get("/status/full")
    assert r.status_code == 200
    data = r.json()
    assert "soldier" in data


def test_from_backend_works(tmp_path):
    """Soldier.from_backend creates a Soldier with a _BackendAdapter."""
    from antfarm.core.backends.file import FileBackend
    from antfarm.core.soldier import Soldier, _BackendAdapter

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    soldier = Soldier.from_backend(backend, repo_path=str(tmp_path))

    assert isinstance(soldier.colony, _BackendAdapter)
    assert soldier.repo_path == str(tmp_path)
    # Backend adapter should work for listing tasks
    assert soldier.colony.list_tasks() == []


def test_from_backend_idempotent_review_creation(tmp_path):
    """from_backend soldier's create_review_task is idempotent via _BackendAdapter.carry."""
    from antfarm.core.backends.file import FileBackend
    from antfarm.core.soldier import Soldier

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    soldier = Soldier.from_backend(backend, repo_path=str(tmp_path), require_review=True)

    # Carry a task, forage, and harvest it manually
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    backend.carry(
        {
            "id": "task-001",
            "title": "Test",
            "spec": "spec",
            "complexity": "M",
            "priority": 10,
            "depends_on": [],
            "touches": [],
            "capabilities_required": [],
            "created_by": "test",
            "status": "ready",
            "current_attempt": None,
            "attempts": [],
            "trail": [],
            "signals": [],
            "created_at": now,
            "updated_at": now,
        }
    )
    task = backend.pull("worker-1")
    assert task is not None
    backend.mark_harvested("task-001", task["current_attempt"], "pr-url", "feat/branch")

    # Create review task
    done_task = backend.get_task("task-001")
    review_id = soldier.create_review_task(done_task)
    assert review_id == "review-task-001"

    # Second call should return None (idempotent)
    done_task = backend.get_task("task-001")
    review_id2 = soldier.create_review_task(done_task)
    assert review_id2 is None


# ---------------------------------------------------------------------------
# SSE events (#100)
# ---------------------------------------------------------------------------


def test_sse_events_on_harvest(tmp_path):
    """Harvest emits an SSE event visible on GET /events."""
    import json as json_mod

    import antfarm.core.serve as serve_mod
    from antfarm.core.backends.file import FileBackend

    # Reset event state
    serve_mod._event_queue.clear()
    serve_mod._event_counter = 0

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend, enable_soldier=False)
    client = TestClient(app)

    _carry(client)
    task = _forage(client).json()
    attempt_id = task["current_attempt"]

    client.post(
        f"/tasks/{task['id']}/harvest",
        json={"attempt_id": attempt_id, "pr": "pr-1", "branch": "feat/x"},
    )

    # Read SSE events
    with client.stream("GET", "/events?after=0&timeout=2") as r:
        assert r.status_code == 200
        events = []
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json_mod.loads(line[len("data: ") :]))

    assert len(events) >= 1
    assert events[0]["type"] == "harvested"
    assert events[0]["task_id"] == "task-001"


def test_emit_event_default_actor_is_colony():
    """_emit_event defaults actor to 'colony' so legacy emitters keep working."""
    serve_mod._event_queue.clear()
    serve_mod._event_counter = 0

    serve_mod._emit_event("harvested", "task-001", "pr=pr-1 branch=feat/x")

    assert len(serve_mod._event_queue) == 1
    event = serve_mod._event_queue[0]
    assert event["actor"] == "colony"
    assert event["type"] == "harvested"
    assert event["task_id"] == "task-001"


def test_emit_event_custom_actor():
    """_emit_event honors explicit actor argument."""
    serve_mod._event_queue.clear()
    serve_mod._event_counter = 0

    serve_mod._emit_event("x", "t", "d", actor="queen")

    assert len(serve_mod._event_queue) == 1
    event = serve_mod._event_queue[0]
    assert event["actor"] == "queen"
    assert event["type"] == "x"
    assert event["task_id"] == "t"
    assert event["detail"] == "d"


def test_sse_payload_includes_actor(tmp_path):
    """Events streamed via /events include the actor field."""
    import json as json_mod

    from antfarm.core.backends.file import FileBackend

    serve_mod._event_queue.clear()
    serve_mod._event_counter = 0

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend, enable_soldier=False)
    client = TestClient(app)

    _carry(client)
    task = _forage(client).json()
    attempt_id = task["current_attempt"]

    client.post(
        f"/tasks/{task['id']}/harvest",
        json={"attempt_id": attempt_id, "pr": "pr-1", "branch": "feat/x"},
    )

    with client.stream("GET", "/events?after=0&timeout=2") as r:
        assert r.status_code == 200
        events = []
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json_mod.loads(line[len("data: ") :]))

    assert len(events) >= 1
    assert "actor" in events[0]
    assert events[0]["actor"] == "colony"


# ---------------------------------------------------------------------------
# Doctor daemon thread (#147)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def reset_doctor_globals():
    """Reset doctor daemon globals to prevent bleed between tests."""
    old_thread = serve_mod._doctor_thread
    old_status = serve_mod._doctor_status
    serve_mod._doctor_thread = None
    serve_mod._doctor_status = "not started"
    yield
    # Restore (thread is daemon, will die with process)
    serve_mod._doctor_thread = old_thread
    serve_mod._doctor_status = old_status


def test_doctor_thread_starts_with_colony(tmp_path, reset_doctor_globals):
    """Doctor daemon thread starts when enable_doctor=True."""
    from antfarm.core.backends.file import FileBackend

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    get_app(backend=backend, enable_doctor=True)
    time.sleep(0.3)

    assert serve_mod._doctor_thread is not None
    assert serve_mod._doctor_thread.is_alive()


def test_doctor_thread_not_started_when_disabled(tmp_path, reset_doctor_globals):
    """Doctor daemon does not start when enable_doctor=False."""
    from antfarm.core.backends.file import FileBackend

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    get_app(backend=backend, enable_doctor=False)

    assert serve_mod._doctor_thread is None


# ---------------------------------------------------------------------------
# Mission-linked carry (v0.6)
# ---------------------------------------------------------------------------


def test_carry_accepts_mission_id(client):
    """POST /tasks with mission_id stamps it on the task and links to mission."""
    # Create a mission first
    client.post("/missions", json={"mission_id": "m-1", "spec": "test"})

    r = client.post(
        "/tasks",
        json={"id": "task-linked", "title": "Linked", "spec": "do it", "mission_id": "m-1"},
    )
    assert r.status_code == 201

    task = client.get("/tasks/task-linked").json()
    assert task["mission_id"] == "m-1"

    mission = client.get("/missions/m-1").json()
    assert "task-linked" in mission["task_ids"]


def test_startup_logs_colony_hash(tmp_path, caplog):
    """Colony id+hash log fires once when the server actually starts (lifespan startup)."""
    import logging

    from antfarm.core.backends.file import FileBackend
    from antfarm.core.process_manager import colony_id, colony_session_hash

    data_dir = str(tmp_path / ".antfarm")
    backend = FileBackend(root=data_dir)

    app = get_app(backend=backend, data_dir=data_dir)

    # Log must NOT appear from get_app() alone — only on server startup.
    pre_start_matching = [r for r in caplog.records if "colony id:" in r.getMessage()]
    assert not pre_start_matching, "colony id log must not fire on get_app(), only on startup"

    # Startup fires when TestClient enters its context manager.
    with caplog.at_level(logging.INFO, logger="antfarm.core.serve"), TestClient(app):
        pass

    expected_id = colony_id(data_dir)
    expected_hash = colony_session_hash(data_dir)
    matching = [
        r
        for r in caplog.records
        if "colony id:" in r.getMessage()
        and expected_id in r.getMessage()
        and expected_hash in r.getMessage()
    ]
    assert matching, (
        f"expected colony id log on startup, got: {[r.getMessage() for r in caplog.records]}"
    )
    assert os.path.realpath(data_dir) in matching[0].getMessage()


def test_get_app_does_not_log_colony_hash(tmp_path, caplog):
    """get_app() must not emit colony id/hash log — it only fires on server startup."""
    import logging

    from antfarm.core.backends.file import FileBackend

    data_dir = str(tmp_path / ".antfarm")
    backend = FileBackend(root=data_dir)

    with caplog.at_level(logging.INFO, logger="antfarm.core.serve"):
        get_app(backend=backend, data_dir=data_dir)

    noisy = [r for r in caplog.records if "colony id:" in r.getMessage()]
    assert not noisy, (
        f"get_app() must not log colony id, but got: {[r.getMessage() for r in noisy]}"
    )


# ---------------------------------------------------------------------------
# Warnings in /status and /status/full
# ---------------------------------------------------------------------------


def _carry_review_task(client, task_id="review-001"):
    """Carry a task with capabilities_required=['review']."""
    return client.post(
        "/tasks",
        json={
            "id": task_id,
            "title": "Review this",
            "spec": "Review the PR",
            "capabilities_required": ["review"],
        },
    )


def test_status_includes_warnings_when_no_reviewer_capacity(tmp_path):
    """GET /status and GET /status/full both surface no_reviewer_capacity warning."""
    from antfarm.core.backends.file import FileBackend
    from antfarm.core.serve import get_app

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend)
    test_client = TestClient(app)

    _carry_review_task(test_client)

    r = test_client.get("/status")
    assert r.status_code == 200
    data = r.json()
    assert "warnings" in data
    codes = [w["code"] for w in data["warnings"]]
    assert "no_reviewer_capacity" in codes

    r = test_client.get("/status/full")
    assert r.status_code == 200
    data = r.json()
    assert "warnings" in data
    codes = [w["code"] for w in data["warnings"]]
    assert "no_reviewer_capacity" in codes


def test_status_warnings_empty_when_reviewer_present(tmp_path):
    """When a reviewer worker is registered, no_reviewer_capacity warning is absent."""
    from antfarm.core.backends.file import FileBackend
    from antfarm.core.serve import get_app

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend)
    test_client = TestClient(app)

    _carry_review_task(test_client)
    test_client.post(
        "/workers/register",
        json={
            "worker_id": "local/reviewer-1",
            "node_id": "local",
            "agent_type": "reviewer",
            "workspace_root": "/tmp/ws",
            "capabilities": ["review"],
        },
    )

    r = test_client.get("/status")
    assert r.status_code == 200
    data = r.json()
    codes = [w.get("code") for w in data.get("warnings", [])]
    assert "no_reviewer_capacity" not in codes


# ---------------------------------------------------------------------------
# SSE epoch (#306) — server tags events + client can detect restarts
# ---------------------------------------------------------------------------


def _harvest_one_task(client, task_id="task-001"):
    """Carry, forage, and harvest a task so _emit_event appends a harvested event."""
    _carry(client, task_id=task_id)
    task = _forage(client).json()
    attempt_id = task["current_attempt"]
    client.post(
        f"/tasks/{task['id']}/harvest",
        json={"attempt_id": attempt_id, "pr": "pr-1", "branch": "feat/x"},
    )


def _read_sse_events(client, query: str) -> list[dict]:
    import json as json_mod

    events: list[dict] = []
    with client.stream("GET", f"/events{query}") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json_mod.loads(line[len("data: ") :]))
    return events


def test_events_include_epoch(tmp_path):
    """Every streamed event carries a non-empty `epoch` field."""
    from antfarm.core.backends.file import FileBackend

    serve_mod._event_queue.clear()
    serve_mod._event_counter = 0

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend, enable_soldier=False)
    client = TestClient(app)

    _harvest_one_task(client)
    events = _read_sse_events(client, "?after=0&timeout=0.2")

    assert len(events) >= 1
    for ev in events:
        assert "epoch" in ev
        assert ev["epoch"] == serve_mod._server_epoch
        assert ev["epoch"]  # non-empty


def test_events_epoch_match_filters_by_cursor(tmp_path):
    """When epoch matches, `after` filters out already-seen events."""
    from antfarm.core.backends.file import FileBackend

    serve_mod._event_queue.clear()
    serve_mod._event_counter = 0

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend, enable_soldier=False)
    client = TestClient(app)

    # Emit 3 events so ids 1, 2, 3 exist
    for i in range(3):
        serve_mod._emit_event("harvested", f"task-{i}", "d")

    current_epoch = serve_mod._server_epoch
    events = _read_sse_events(client, f"?after=1&epoch={current_epoch}&timeout=0.3")

    ids = [ev["id"] for ev in events]
    assert ids == [2, 3]


def test_events_epoch_mismatch_resets_cursor(tmp_path):
    """Stale epoch signals a colony restart — cursor resets to 0."""
    from antfarm.core.backends.file import FileBackend

    serve_mod._event_queue.clear()
    serve_mod._event_counter = 0

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend, enable_soldier=False)
    client = TestClient(app)

    for i in range(3):
        serve_mod._emit_event("harvested", f"task-{i}", "d")

    events = _read_sse_events(client, "?after=2&epoch=stale-uuid&timeout=0.3")
    ids = [ev["id"] for ev in events]
    assert ids == [1, 2, 3]


def test_events_empty_epoch_uses_after_literally(tmp_path):
    """Empty epoch = old TUI or first connect — `after` is obeyed as-is."""
    from antfarm.core.backends.file import FileBackend

    serve_mod._event_queue.clear()
    serve_mod._event_counter = 0

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend, enable_soldier=False)
    client = TestClient(app)

    for i in range(3):
        serve_mod._emit_event("harvested", f"task-{i}", "d")

    events = _read_sse_events(client, "?epoch=&after=2&timeout=0.3")
    ids = [ev["id"] for ev in events]
    assert ids == [3]


def test_events_epoch_endpoint(tmp_path):
    """GET /events/epoch returns a parseable UUID under the `epoch` key."""
    import uuid as _uuid

    from antfarm.core.backends.file import FileBackend

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend, enable_soldier=False)
    client = TestClient(app)

    r = client.get("/events/epoch")
    assert r.status_code == 200
    payload = r.json()
    assert set(payload.keys()) == {"epoch"}
    epoch = payload["epoch"]
    assert isinstance(epoch, str)
    assert len(epoch) == 36
    assert epoch.count("-") == 4
    _uuid.UUID(epoch)  # raises if not a valid UUID
    assert epoch == serve_mod._server_epoch


# ---------------------------------------------------------------------------
# _emit_event concurrency / locking (issue #309)
# ---------------------------------------------------------------------------


def test_emit_event_concurrent_ids_are_unique():
    """Concurrent emits must produce strictly unique, monotonically-assigned ids.

    Pre-fix, the `_event_counter += 1` and queue append were not serialized,
    so two threads could read the same counter value, both increment to N+1,
    and both append events with id=N+1. That breaks SSE cursor semantics
    (after=N+1 would skip a real event). 10 threads x 100 emits = 1000 ids;
    the deque has maxlen=1000, so all retained.
    """
    serve_mod._event_queue.clear()
    serve_mod._event_counter = 0

    n_threads = 10
    n_per_thread = 100
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def emit_burst() -> None:
        try:
            barrier.wait()
            for i in range(n_per_thread):
                serve_mod._emit_event("burst", f"task-{i}", "d")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=emit_burst) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert serve_mod._event_counter == n_threads * n_per_thread
    queued = list(serve_mod._event_queue)
    assert len(queued) == n_threads * n_per_thread
    ids = [e["id"] for e in queued]
    assert len(set(ids)) == len(ids), "duplicate event ids under concurrent emits"
    assert queued[-1]["id"] == n_threads * n_per_thread


def test_emit_event_serial_still_works():
    """Baseline preservation: serial emits still yield contiguous ids."""
    serve_mod._event_queue.clear()
    serve_mod._event_counter = 0

    serve_mod._emit_event("a", "t-1", "")
    serve_mod._emit_event("b", "t-2", "")
    serve_mod._emit_event("c", "t-3", "")

    assert serve_mod._event_counter == 3
    ids = [e["id"] for e in serve_mod._event_queue]
    assert ids == [1, 2, 3]


def test_emit_event_ordering_preserved_under_concurrency():
    """Insertion order in the deque must be strictly monotonic.

    Counter increment + queue append are inside the same critical section,
    so the queue's recorded order matches the assigned id order.
    """
    serve_mod._event_queue.clear()
    serve_mod._event_counter = 0

    n_threads = 4
    n_per_thread = 50
    barrier = threading.Barrier(n_threads)

    def emit_burst() -> None:
        barrier.wait()
        for i in range(n_per_thread):
            serve_mod._emit_event("burst", f"task-{i}", "d")

    threads = [threading.Thread(target=emit_burst) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = [e["id"] for e in serve_mod._event_queue]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids), "deque insertion order does not match id order"
