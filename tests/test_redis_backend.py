"""Tests for RedisBackend implementation.

Mirrors test coverage from test_file_backend.py, adapted for Redis.
Uses fakeredis for in-memory Redis simulation (no real server needed).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from antfarm.core.models import AttemptStatus, TaskStatus

fakeredis = pytest.importorskip("fakeredis", reason="fakeredis required for Redis backend tests")

from antfarm.core.backends.redis import RedisBackend  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
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
def backend() -> RedisBackend:
    r = fakeredis.FakeRedis()
    return RedisBackend(redis_client=r, guard_ttl=300, worker_ttl=300)


# ---------------------------------------------------------------------------
# 1. test_carry_creates_task
# ---------------------------------------------------------------------------


def test_carry_creates_task(backend: RedisBackend) -> None:
    task = _make_task("task-1")
    backend.carry(task)

    data = backend.get_task("task-1")
    assert data is not None
    assert data["id"] == "task-1"
    assert data["status"] == TaskStatus.READY.value


# ---------------------------------------------------------------------------
# 2. test_pull_moves_to_active
# ---------------------------------------------------------------------------


def test_pull_moves_to_active(backend: RedisBackend) -> None:
    backend.carry(_make_task("task-1"))
    result = backend.pull("worker-1")

    assert result is not None
    assert result["id"] == "task-1"

    # Task should be in active queue, not ready
    assert not backend._r.sismember(backend._queue_key("ready"), "task-1")
    assert backend._r.sismember(backend._queue_key("active"), "task-1")


# ---------------------------------------------------------------------------
# 3. test_pull_creates_attempt
# ---------------------------------------------------------------------------


def test_pull_creates_attempt(backend: RedisBackend) -> None:
    backend.carry(_make_task("task-1"))
    result = backend.pull("worker-1")

    assert result is not None
    assert result["status"] == TaskStatus.ACTIVE.value
    assert len(result["attempts"]) == 1

    attempt = result["attempts"][0]
    assert attempt["status"] == AttemptStatus.ACTIVE.value
    assert attempt["worker_id"] == "worker-1"
    assert attempt["attempt_id"] == result["current_attempt"]


# ---------------------------------------------------------------------------
# 4. test_pull_returns_none_when_empty
# ---------------------------------------------------------------------------


def test_pull_returns_none_when_empty(backend: RedisBackend) -> None:
    result = backend.pull("worker-1")
    assert result is None


# ---------------------------------------------------------------------------
# 5. test_pull_is_atomic
# ---------------------------------------------------------------------------


def test_pull_is_atomic(backend: RedisBackend) -> None:
    """Two concurrent pulls must not return the same task."""
    for i in range(3):
        backend.carry(_make_task(f"task-{i}"))

    claimed: list[str] = []
    errors: list[Exception] = []

    def do_pull(worker_id: str) -> None:
        try:
            for _ in range(5):
                result = backend.pull(worker_id)
                if result is not None:
                    claimed.append(result["id"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=do_pull, args=(f"worker-{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(claimed) == len(set(claimed)), f"Duplicate claims: {claimed}"


# ---------------------------------------------------------------------------
# 6. test_mark_harvested
# ---------------------------------------------------------------------------


def test_mark_harvested(backend: RedisBackend) -> None:
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None

    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-1", attempt_id, pr="https://gh/pr/1", branch="feat/task-1")

    assert not backend._r.sismember(backend._queue_key("active"), "task-1")
    assert backend._r.sismember(backend._queue_key("done"), "task-1")

    data = backend.get_task("task-1")
    assert data is not None
    assert data["status"] == TaskStatus.DONE.value

    attempt = next(a for a in data["attempts"] if a["attempt_id"] == attempt_id)
    assert attempt["status"] == AttemptStatus.DONE.value
    assert attempt["pr"] == "https://gh/pr/1"
    assert attempt["branch"] == "feat/task-1"


# ---------------------------------------------------------------------------
# 7. test_kickback
# ---------------------------------------------------------------------------


def test_kickback(backend: RedisBackend) -> None:
    """Kickback moves done→ready (Soldier calls it after failed integration)."""
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None

    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-1", attempt_id, pr="https://gh/pr/1", branch="feat/task-1")
    backend.kickback("task-1", reason="tests failed")

    assert not backend._r.sismember(backend._queue_key("done"), "task-1")
    assert backend._r.sismember(backend._queue_key("ready"), "task-1")

    data = backend.get_task("task-1")
    assert data is not None
    assert data["status"] == TaskStatus.READY.value
    assert data["current_attempt"] is None

    attempt = next(a for a in data["attempts"] if a["attempt_id"] == attempt_id)
    assert attempt["status"] == AttemptStatus.SUPERSEDED.value

    assert any("tests failed" in e["message"] for e in data["trail"])


# ---------------------------------------------------------------------------
# 8. test_guard_release
# ---------------------------------------------------------------------------


def test_guard_release(backend: RedisBackend) -> None:
    assert backend.guard("repo/main", "worker-1") is True
    assert backend.guard("repo/main", "worker-2") is False

    backend.release_guard("repo/main", "worker-1")
    assert backend.guard("repo/main", "worker-2") is True


# ---------------------------------------------------------------------------
# 9. test_stale_guard_recovery
# ---------------------------------------------------------------------------


def test_stale_guard_recovery(backend: RedisBackend) -> None:
    """Guard with expired TTL and dead owner is treated as released."""
    import json

    # Manually write a guard key WITHOUT TTL (simulating an expired/stale guard)
    guard_key = backend._guard_key("repo/main")
    payload = json.dumps({"owner": "dead-worker", "acquired_at": "old"})
    backend._r.set(guard_key, payload)  # no ex= → no TTL → ttl returns -1

    # New worker should be able to acquire (stale guard cleared)
    result = backend.guard("repo/main", "worker-new")
    assert result is True


# ---------------------------------------------------------------------------
# 10. test_carry_duplicate_rejects
# ---------------------------------------------------------------------------


def test_carry_duplicate_rejects(backend: RedisBackend) -> None:
    backend.carry(_make_task("task-1"))
    with pytest.raises(ValueError, match="task-1"):
        backend.carry(_make_task("task-1"))


# ---------------------------------------------------------------------------
# 11. test_harvest_non_current_attempt_rejects
# ---------------------------------------------------------------------------


def test_harvest_non_current_attempt_rejects(backend: RedisBackend) -> None:
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None

    with pytest.raises(ValueError, match="not the current attempt"):
        backend.mark_harvested("task-1", "wrong-attempt-id", pr="pr", branch="branch")


# ---------------------------------------------------------------------------
# 12. test_release_guard_wrong_owner_rejects
# ---------------------------------------------------------------------------


def test_release_guard_wrong_owner_rejects(backend: RedisBackend) -> None:
    backend.guard("repo/main", "worker-1")
    with pytest.raises(PermissionError):
        backend.release_guard("repo/main", "worker-2")


# ---------------------------------------------------------------------------
# 13. test_append_trail
# ---------------------------------------------------------------------------


def test_append_trail(backend: RedisBackend) -> None:
    backend.carry(_make_task("task-1"))
    now = datetime.now(UTC).isoformat()
    entry = {"ts": now, "worker_id": "worker-1", "message": "started work"}
    backend.append_trail("task-1", entry)

    data = backend.get_task("task-1")
    assert data is not None
    assert any(e["message"] == "started work" for e in data["trail"])


# ---------------------------------------------------------------------------
# 14. test_append_signal
# ---------------------------------------------------------------------------


def test_append_signal(backend: RedisBackend) -> None:
    backend.carry(_make_task("task-1"))
    now = datetime.now(UTC).isoformat()
    entry = {"ts": now, "worker_id": "worker-1", "message": "build passed"}
    backend.append_signal("task-1", entry)

    data = backend.get_task("task-1")
    assert data is not None
    assert any(e["message"] == "build passed" for e in data["signals"])


# ---------------------------------------------------------------------------
# 15. test_register_node_idempotent
# ---------------------------------------------------------------------------


def test_register_node_idempotent(backend: RedisBackend) -> None:
    now = datetime.now(UTC).isoformat()
    node = {"node_id": "node-1", "joined_at": now, "last_seen": now}
    backend.register_node(node)
    backend.register_node(node)  # second call must not raise

    later = datetime.now(UTC).isoformat()
    node_updated = {"node_id": "node-1", "joined_at": now, "last_seen": later}
    backend.register_node(node_updated)

    import json

    raw = backend._r.get(backend._node_key("node-1"))
    data = json.loads(raw)
    assert data["last_seen"] == later


# ---------------------------------------------------------------------------
# 16. test_register_deregister_worker
# ---------------------------------------------------------------------------


def test_register_deregister_worker(backend: RedisBackend) -> None:
    now = datetime.now(UTC).isoformat()
    worker = {
        "worker_id": "worker-1",
        "node_id": "node-1",
        "agent_type": "engineer",
        "workspace_root": "/tmp/ws",
        "registered_at": now,
        "last_heartbeat": now,
    }
    backend.register_worker(worker)
    assert backend._r.exists(backend._worker_key("worker-1"))

    backend.deregister_worker("worker-1")
    assert not backend._r.exists(backend._worker_key("worker-1"))

    # Deregister non-existent is a no-op
    backend.deregister_worker("unknown-worker")


# ---------------------------------------------------------------------------
# 17. test_heartbeat_updates
# ---------------------------------------------------------------------------


def test_heartbeat_updates(backend: RedisBackend) -> None:
    now = datetime.now(UTC).isoformat()
    worker = {
        "worker_id": "worker-1",
        "node_id": "node-1",
        "agent_type": "engineer",
        "workspace_root": "/tmp/ws",
        "registered_at": now,
        "last_heartbeat": now,
    }
    backend.register_worker(worker)

    status = {"status": "active", "current_task": "task-42"}
    backend.heartbeat("worker-1", status)

    import json

    raw = backend._r.get(backend._worker_key("worker-1"))
    data = json.loads(raw)
    assert data["current_task"] == "task-42"
    assert data["last_heartbeat"] >= now

    # Heartbeat for unregistered worker creates the key
    backend.heartbeat("new-worker", {"status": "idle"})
    assert backend._r.exists(backend._worker_key("new-worker"))


# ---------------------------------------------------------------------------
# Bug fix regression tests (mirrored from test_file_backend.py)
# ---------------------------------------------------------------------------


def test_mark_harvested_idempotent_wrong_attempt_rejects(backend: RedisBackend) -> None:
    """Already-done task with wrong attempt_id must raise, not silently no-op."""
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None

    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-1", attempt_id, pr="pr", branch="branch")

    # Second call with correct attempt_id is idempotent (no-op)
    backend.mark_harvested("task-1", attempt_id, pr="pr", branch="branch")

    # Second call with wrong attempt_id must raise
    with pytest.raises(ValueError, match="not the current attempt"):
        backend.mark_harvested("task-1", "wrong-attempt-id", pr="pr", branch="branch")


def test_mark_merged_unknown_attempt_rejects(backend: RedisBackend) -> None:
    """mark_merged with unknown attempt_id must raise ValueError."""
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None

    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-1", attempt_id, pr="pr", branch="branch")

    with pytest.raises(ValueError, match="not found on task"):
        backend.mark_merged("task-1", "nonexistent-attempt-id")


def test_kickback_requires_done_not_active(backend: RedisBackend) -> None:
    """Kickback must operate on done/ tasks, not active/ tasks."""
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None

    # Task is active — kickback should raise (not in done/)
    with pytest.raises(FileNotFoundError):
        backend.kickback("task-1", reason="should fail")

    # After harvesting, kickback succeeds
    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-1", attempt_id, pr="pr", branch="branch")
    backend.kickback("task-1", reason="integration failed")

    data = backend.get_task("task-1")
    assert data is not None
    assert data["status"] == TaskStatus.READY.value


# ---------------------------------------------------------------------------
# Status endpoint test
# ---------------------------------------------------------------------------


def test_status_returns_counts(backend: RedisBackend) -> None:
    backend.carry(_make_task("task-1"))
    backend.carry(_make_task("task-2"))
    backend.pull("worker-1")

    s = backend.status()
    assert s["tasks"]["ready"] == 1
    assert s["tasks"]["active"] == 1
    assert s["tasks"]["done"] == 0


# ---------------------------------------------------------------------------
# Dependency-aware pull test
# ---------------------------------------------------------------------------


def test_pull_respects_dependencies(backend: RedisBackend) -> None:
    """Task with unmet dependency is skipped."""
    backend.carry(_make_task("task-1"))
    backend.carry(_make_task("task-2", depends_on=["task-1"]))

    # Only task-1 should be eligible
    result = backend.pull("worker-1")
    assert result is not None
    assert result["id"] == "task-1"

    # task-2 still blocked (task-1 not in done)
    result2 = backend.pull("worker-2")
    assert result2 is None
