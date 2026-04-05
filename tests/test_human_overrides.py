"""Tests for human override commands: pause, resume, reassign, block, unblock.

Covers FileBackend methods and API endpoints.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from antfarm.core.backends.file import FileBackend
from antfarm.core.models import AttemptStatus, TaskStatus
from antfarm.core.serve import get_app

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _make_task(task_id: str = "task-1", priority: int = 10, depends_on: list | None = None) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "spec": "Do something",
        "complexity": "M",
        "priority": priority,
        "depends_on": depends_on or [],
        "touches": ["src/foo.py"],
        "created_at": now,
        "updated_at": now,
        "created_by": "test",
    }


@pytest.fixture()
def backend(tmp_path: Path) -> FileBackend:
    return FileBackend(root=tmp_path / ".antfarm")


@pytest.fixture()
def client(tmp_path):
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend)
    return TestClient(app)


def _carry(client, task_id="task-001", title="Test Task", spec="Do the thing"):
    return client.post("/tasks", json={"id": task_id, "title": title, "spec": spec})


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
# FileBackend: pause_task
# ---------------------------------------------------------------------------


class TestPauseTask:
    def test_pause_active_task(self, backend):
        backend.carry(_make_task("t1"))
        backend.pull("w1")
        backend.pause_task("t1")

        task = backend.get_task("t1")
        assert task["status"] == TaskStatus.PAUSED.value
        assert backend._paused_path("t1").exists()
        assert not backend._active_path("t1").exists()

    def test_pause_non_active_raises(self, backend):
        backend.carry(_make_task("t1"))
        with pytest.raises(ValueError, match="not in ACTIVE state"):
            backend.pause_task("t1")

    def test_pause_not_found_raises(self, backend):
        with pytest.raises(FileNotFoundError):
            backend.pause_task("nonexistent")


# ---------------------------------------------------------------------------
# FileBackend: resume_task
# ---------------------------------------------------------------------------


class TestResumeTask:
    def test_resume_paused_task(self, backend):
        backend.carry(_make_task("t1"))
        pulled = backend.pull("w1")
        attempt_id = pulled["current_attempt"]

        backend.pause_task("t1")
        backend.resume_task("t1")

        task = backend.get_task("t1")
        assert task["status"] == TaskStatus.READY.value
        assert task["current_attempt"] is None
        assert backend._ready_path("t1").exists()

        # Original attempt should be superseded
        for a in task["attempts"]:
            if a["attempt_id"] == attempt_id:
                assert a["status"] == AttemptStatus.SUPERSEDED.value

    def test_resume_non_paused_raises(self, backend):
        backend.carry(_make_task("t1"))
        with pytest.raises(ValueError, match="not in PAUSED state"):
            backend.resume_task("t1")

    def test_resume_not_found_raises(self, backend):
        with pytest.raises(FileNotFoundError):
            backend.resume_task("nonexistent")

    def test_resumed_task_can_be_pulled(self, backend):
        backend.carry(_make_task("t1"))
        backend.pull("w1")
        backend.pause_task("t1")
        backend.resume_task("t1")

        pulled = backend.pull("w2")
        assert pulled is not None
        assert pulled["id"] == "t1"
        assert pulled["current_attempt"] is not None


# ---------------------------------------------------------------------------
# FileBackend: reassign_task
# ---------------------------------------------------------------------------


class TestReassignTask:
    def test_reassign_active_task(self, backend):
        backend.carry(_make_task("t1"))
        pulled = backend.pull("w1")
        old_attempt = pulled["current_attempt"]

        backend.reassign_task("t1", "w2")

        task = backend.get_task("t1")
        assert task["status"] == TaskStatus.READY.value
        assert task["current_attempt"] is None
        assert backend._ready_path("t1").exists()

        # Old attempt superseded
        for a in task["attempts"]:
            if a["attempt_id"] == old_attempt:
                assert a["status"] == AttemptStatus.SUPERSEDED.value

        # Trail entry recorded
        trail_messages = [t["message"] for t in task["trail"]]
        assert any("Reassigned to w2" in m for m in trail_messages)

    def test_reassign_non_active_raises(self, backend):
        backend.carry(_make_task("t1"))
        with pytest.raises(ValueError, match="not in ACTIVE state"):
            backend.reassign_task("t1", "w2")

    def test_reassign_not_found_raises(self, backend):
        with pytest.raises(FileNotFoundError):
            backend.reassign_task("nonexistent", "w2")

    def test_reassigned_task_can_be_pulled(self, backend):
        backend.carry(_make_task("t1"))
        backend.pull("w1")
        backend.reassign_task("t1", "w2")

        pulled = backend.pull("w2")
        assert pulled is not None
        assert pulled["id"] == "t1"


# ---------------------------------------------------------------------------
# FileBackend: block_task
# ---------------------------------------------------------------------------


class TestBlockTask:
    def test_block_ready_task(self, backend):
        backend.carry(_make_task("t1"))
        backend.block_task("t1", "needs clarification")

        task = backend.get_task("t1")
        assert task["status"] == TaskStatus.BLOCKED.value
        assert backend._blocked_path("t1").exists()
        assert not backend._ready_path("t1").exists()

        trail_messages = [t["message"] for t in task["trail"]]
        assert any("Blocked: needs clarification" in m for m in trail_messages)

    def test_block_non_ready_raises(self, backend):
        backend.carry(_make_task("t1"))
        backend.pull("w1")
        with pytest.raises(ValueError, match="not in READY state"):
            backend.block_task("t1", "reason")

    def test_block_not_found_raises(self, backend):
        with pytest.raises(FileNotFoundError):
            backend.block_task("nonexistent", "reason")

    def test_blocked_task_not_pullable(self, backend):
        backend.carry(_make_task("t1"))
        backend.block_task("t1", "reason")

        pulled = backend.pull("w1")
        assert pulled is None


# ---------------------------------------------------------------------------
# FileBackend: unblock_task
# ---------------------------------------------------------------------------


class TestUnblockTask:
    def test_unblock_blocked_task(self, backend):
        backend.carry(_make_task("t1"))
        backend.block_task("t1", "reason")
        backend.unblock_task("t1")

        task = backend.get_task("t1")
        assert task["status"] == TaskStatus.READY.value
        assert backend._ready_path("t1").exists()
        assert not backend._blocked_path("t1").exists()

    def test_unblock_non_blocked_raises(self, backend):
        backend.carry(_make_task("t1"))
        with pytest.raises(ValueError, match="not in BLOCKED state"):
            backend.unblock_task("t1")

    def test_unblock_not_found_raises(self, backend):
        with pytest.raises(FileNotFoundError):
            backend.unblock_task("nonexistent")

    def test_unblocked_task_can_be_pulled(self, backend):
        backend.carry(_make_task("t1"))
        backend.block_task("t1", "reason")
        backend.unblock_task("t1")

        pulled = backend.pull("w1")
        assert pulled is not None
        assert pulled["id"] == "t1"


# ---------------------------------------------------------------------------
# FileBackend: status includes new states
# ---------------------------------------------------------------------------


class TestStatusCounts:
    def test_status_includes_paused_and_blocked(self, backend):
        backend.carry(_make_task("t1"))
        backend.carry(_make_task("t2"))
        backend.carry(_make_task("t3"))

        backend.pull("w1")  # t1 → active
        backend.block_task("t2", "blocked")  # t2 → blocked

        # Pull t3 and pause it
        backend.pull("w2")  # t3 → active
        backend.pause_task("t3")  # t3 → paused

        status = backend.status()
        assert status["tasks"]["active"] == 1
        assert status["tasks"]["blocked"] == 1
        assert status["tasks"]["paused"] == 1
        assert status["tasks"]["ready"] == 0


# ---------------------------------------------------------------------------
# FileBackend: list_tasks filters new statuses
# ---------------------------------------------------------------------------


class TestListTasksFilter:
    def test_list_paused_tasks(self, backend):
        backend.carry(_make_task("t1"))
        backend.pull("w1")
        backend.pause_task("t1")

        paused = backend.list_tasks(status="paused")
        assert len(paused) == 1
        assert paused[0]["id"] == "t1"

    def test_list_blocked_tasks(self, backend):
        backend.carry(_make_task("t1"))
        backend.block_task("t1", "reason")

        blocked = backend.list_tasks(status="blocked")
        assert len(blocked) == 1
        assert blocked[0]["id"] == "t1"


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestPauseEndpoint:
    def test_pause_active_task(self, client):
        _carry(client, "t1")
        _register_worker(client)
        _forage(client)

        r = client.post("/tasks/t1/pause")
        assert r.status_code == 200

        task = client.get("/tasks/t1").json()
        assert task["status"] == "paused"

    def test_pause_not_active_returns_409(self, client):
        _carry(client, "t1")
        r = client.post("/tasks/t1/pause")
        assert r.status_code == 409

    def test_pause_not_found_returns_404(self, client):
        r = client.post("/tasks/nonexistent/pause")
        assert r.status_code == 404


class TestResumeEndpoint:
    def test_resume_paused_task(self, client):
        _carry(client, "t1")
        _register_worker(client)
        _forage(client)
        client.post("/tasks/t1/pause")

        r = client.post("/tasks/t1/resume")
        assert r.status_code == 200

        task = client.get("/tasks/t1").json()
        assert task["status"] == "ready"

    def test_resume_not_paused_returns_409(self, client):
        _carry(client, "t1")
        r = client.post("/tasks/t1/resume")
        assert r.status_code == 409


class TestReassignEndpoint:
    def test_reassign_active_task(self, client):
        _carry(client, "t1")
        _register_worker(client)
        _forage(client)

        r = client.post("/tasks/t1/reassign", json={"worker_id": "worker-2"})
        assert r.status_code == 200

        task = client.get("/tasks/t1").json()
        assert task["status"] == "ready"

    def test_reassign_not_active_returns_409(self, client):
        _carry(client, "t1")
        r = client.post("/tasks/t1/reassign", json={"worker_id": "w2"})
        assert r.status_code == 409


class TestBlockEndpoint:
    def test_block_ready_task(self, client):
        _carry(client, "t1")

        r = client.post("/tasks/t1/block", json={"reason": "needs spec"})
        assert r.status_code == 200

        task = client.get("/tasks/t1").json()
        assert task["status"] == "blocked"

    def test_block_not_ready_returns_409(self, client):
        _carry(client, "t1")
        _register_worker(client)
        _forage(client)

        r = client.post("/tasks/t1/block", json={"reason": "reason"})
        assert r.status_code == 409


class TestUnblockEndpoint:
    def test_unblock_blocked_task(self, client):
        _carry(client, "t1")
        client.post("/tasks/t1/block", json={"reason": "needs spec"})

        r = client.post("/tasks/t1/unblock")
        assert r.status_code == 200

        task = client.get("/tasks/t1").json()
        assert task["status"] == "ready"

    def test_unblock_not_blocked_returns_409(self, client):
        _carry(client, "t1")
        r = client.post("/tasks/t1/unblock")
        assert r.status_code == 409
