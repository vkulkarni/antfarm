"""Tests for FileBackend implementation.

Covers all 17 test cases from IMPLEMENTATION.md section 1c,
plus edge case invariants from the Edge Cases section.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from antfarm.core.backends.file import FileBackend
from antfarm.core.models import AttemptStatus, TaskStatus

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
def backend(tmp_path: Path) -> FileBackend:
    return FileBackend(root=tmp_path / ".antfarm")


# ---------------------------------------------------------------------------
# 1. test_carry_creates_file
# ---------------------------------------------------------------------------


def test_carry_creates_file(backend: FileBackend, tmp_path: Path) -> None:
    task = _make_task("task-1")
    backend.carry(task)

    ready_file = tmp_path / ".antfarm" / "tasks" / "ready" / "task-1.json"
    assert ready_file.exists()
    data = json.loads(ready_file.read_text())
    assert data["id"] == "task-1"
    assert data["status"] == TaskStatus.READY.value


# ---------------------------------------------------------------------------
# 2. test_pull_moves_to_active
# ---------------------------------------------------------------------------


def test_pull_moves_to_active(backend: FileBackend, tmp_path: Path) -> None:
    backend.carry(_make_task("task-1"))
    result = backend.pull("worker-1")

    assert result is not None
    assert result["id"] == "task-1"

    ready_file = tmp_path / ".antfarm" / "tasks" / "ready" / "task-1.json"
    active_file = tmp_path / ".antfarm" / "tasks" / "active" / "task-1.json"
    assert not ready_file.exists()
    assert active_file.exists()


# ---------------------------------------------------------------------------
# 3. test_pull_creates_attempt
# ---------------------------------------------------------------------------


def test_pull_creates_attempt(backend: FileBackend) -> None:
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


def test_pull_returns_none_when_empty(backend: FileBackend) -> None:
    result = backend.pull("worker-1")
    assert result is None


# ---------------------------------------------------------------------------
# 5. test_pull_is_atomic
# ---------------------------------------------------------------------------


def test_pull_is_atomic(backend: FileBackend) -> None:
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
    # No duplicate task IDs should be claimed
    assert len(claimed) == len(set(claimed)), f"Duplicate claims: {claimed}"


# ---------------------------------------------------------------------------
# 6. test_mark_harvested
# ---------------------------------------------------------------------------


def test_mark_harvested(backend: FileBackend, tmp_path: Path) -> None:
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None

    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-1", attempt_id, pr="https://gh/pr/1", branch="feat/task-1")

    active_file = tmp_path / ".antfarm" / "tasks" / "active" / "task-1.json"
    done_file = tmp_path / ".antfarm" / "tasks" / "done" / "task-1.json"
    assert not active_file.exists()
    assert done_file.exists()

    data = json.loads(done_file.read_text())
    assert data["status"] == TaskStatus.DONE.value

    attempt = next(a for a in data["attempts"] if a["attempt_id"] == attempt_id)
    assert attempt["status"] == AttemptStatus.DONE.value
    assert attempt["pr"] == "https://gh/pr/1"
    assert attempt["branch"] == "feat/task-1"


# ---------------------------------------------------------------------------
# 7. test_kickback
# ---------------------------------------------------------------------------


def test_kickback(backend: FileBackend, tmp_path: Path) -> None:
    """Kickback moves done→ready (Soldier calls it after failed integration)."""
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None

    attempt_id = pulled["current_attempt"]
    # Harvest first (task moves to done/) — then kickback
    backend.mark_harvested("task-1", attempt_id, pr="https://gh/pr/1", branch="feat/task-1")
    backend.kickback("task-1", reason="tests failed")

    done_file = tmp_path / ".antfarm" / "tasks" / "done" / "task-1.json"
    ready_file = tmp_path / ".antfarm" / "tasks" / "ready" / "task-1.json"
    assert not done_file.exists()
    assert ready_file.exists()

    data = json.loads(ready_file.read_text())
    assert data["status"] == TaskStatus.READY.value
    assert data["current_attempt"] is None

    attempt = next(a for a in data["attempts"] if a["attempt_id"] == attempt_id)
    assert attempt["status"] == AttemptStatus.SUPERSEDED.value

    # Trail should contain the failure reason
    assert any("tests failed" in e["message"] for e in data["trail"])


# ---------------------------------------------------------------------------
# 8. test_guard_release
# ---------------------------------------------------------------------------


def test_guard_release(backend: FileBackend) -> None:
    # Acquire succeeds
    assert backend.guard("repo/main", "worker-1") is True

    # Second attempt by different owner fails
    assert backend.guard("repo/main", "worker-2") is False

    # Release by correct owner
    backend.release_guard("repo/main", "worker-1")

    # After release, reacquire succeeds
    assert backend.guard("repo/main", "worker-2") is True


# ---------------------------------------------------------------------------
# 9. test_stale_guard_recovery
# ---------------------------------------------------------------------------


def test_stale_guard_recovery(backend: FileBackend, tmp_path: Path) -> None:
    """Guard with expired mtime and dead owner is treated as released."""
    # Write guard file manually with old mtime (no live worker)
    guard_path = tmp_path / ".antfarm" / "guards" / "repo__main.lock"
    guard_path.write_text(json.dumps({"owner": "dead-worker", "acquired_at": "old"}))

    # Set mtime to far in the past (beyond TTL)
    old_time = time.time() - (backend._guard_ttl + 60)
    os.utime(str(guard_path), (old_time, old_time))

    # New worker should be able to acquire (stale guard cleared)
    result = backend.guard("repo/main", "worker-new")
    assert result is True


# ---------------------------------------------------------------------------
# 10. test_carry_duplicate_rejects
# ---------------------------------------------------------------------------


def test_carry_duplicate_rejects(backend: FileBackend) -> None:
    backend.carry(_make_task("task-1"))
    with pytest.raises(ValueError, match="task-1"):
        backend.carry(_make_task("task-1"))


# ---------------------------------------------------------------------------
# 11. test_harvest_non_current_attempt_rejects
# ---------------------------------------------------------------------------


def test_harvest_non_current_attempt_rejects(backend: FileBackend) -> None:
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None

    with pytest.raises(ValueError, match="not the current attempt"):
        backend.mark_harvested("task-1", "wrong-attempt-id", pr="pr", branch="branch")


# ---------------------------------------------------------------------------
# 12. test_release_guard_wrong_owner_rejects
# ---------------------------------------------------------------------------


def test_release_guard_wrong_owner_rejects(backend: FileBackend) -> None:
    backend.guard("repo/main", "worker-1")
    with pytest.raises(PermissionError):
        backend.release_guard("repo/main", "worker-2")


# ---------------------------------------------------------------------------
# 13. test_append_trail
# ---------------------------------------------------------------------------


def test_append_trail(backend: FileBackend) -> None:
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


def test_append_signal(backend: FileBackend) -> None:
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


def test_register_node_idempotent(backend: FileBackend) -> None:
    now = datetime.now(UTC).isoformat()
    node = {"node_id": "node-1", "joined_at": now, "last_seen": now}
    backend.register_node(node)
    backend.register_node(node)  # second call must not raise

    later = datetime.now(UTC).isoformat()
    node_updated = {"node_id": "node-1", "joined_at": now, "last_seen": later}
    backend.register_node(node_updated)

    # last_seen should be updated
    node_path = backend._node_path("node-1")
    data = json.loads(node_path.read_text())
    assert data["last_seen"] == later


# ---------------------------------------------------------------------------
# 16. test_register_deregister_worker
# ---------------------------------------------------------------------------


def test_register_deregister_worker(backend: FileBackend) -> None:
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
    assert backend._worker_path("worker-1").exists()

    backend.deregister_worker("worker-1")
    assert not backend._worker_path("worker-1").exists()

    # Deregister non-existent is a no-op
    backend.deregister_worker("unknown-worker")


# ---------------------------------------------------------------------------
# 17. test_heartbeat_updates
# ---------------------------------------------------------------------------


def test_heartbeat_updates(backend: FileBackend) -> None:
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

    data = json.loads(backend._worker_path("worker-1").read_text())
    assert data["current_task"] == "task-42"
    assert data["last_heartbeat"] >= now

    # Heartbeat for unregistered worker creates the file
    backend.heartbeat("new-worker", {"status": "idle"})
    assert backend._worker_path("new-worker").exists()


# ---------------------------------------------------------------------------
# Bug fix regression tests
# ---------------------------------------------------------------------------


def test_mark_harvested_idempotent_wrong_attempt_rejects(backend: FileBackend) -> None:
    """BUG 2 regression: already-done task with wrong attempt_id must raise, not silently no-op."""
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


def test_mark_merged_unknown_attempt_rejects(backend: FileBackend) -> None:
    """BUG 3 regression: mark_merged with unknown attempt_id must raise ValueError."""
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None

    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-1", attempt_id, pr="pr", branch="branch")

    with pytest.raises(ValueError, match="not found on task"):
        backend.mark_merged("task-1", "nonexistent-attempt-id")


def test_kickback_requires_done_not_active(backend: FileBackend) -> None:
    """BUG 1 regression: kickback must operate on done/ tasks, not active/ tasks."""
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None

    # Task is in active/ — kickback should raise (not in done/)
    with pytest.raises(FileNotFoundError):
        backend.kickback("task-1", reason="should fail")

    # After harvesting (task moves to done/), kickback succeeds
    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-1", attempt_id, pr="pr", branch="branch")
    backend.kickback("task-1", reason="integration failed")

    data = backend.get_task("task-1")
    assert data is not None
    assert data["status"] == TaskStatus.READY.value


# ---------------------------------------------------------------------------
# Capability filtering in pull()
# ---------------------------------------------------------------------------


def _make_task_with_caps(task_id: str, capabilities_required: list) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "spec": "Do something",
        "complexity": "M",
        "priority": 10,
        "depends_on": [],
        "touches": [],
        "capabilities_required": capabilities_required,
        "created_at": now,
        "updated_at": now,
        "created_by": "test",
    }


def test_pull_skips_tasks_with_unmet_capabilities(backend: FileBackend, tmp_path: Path) -> None:
    """pull() skips a task requiring capabilities the worker doesn't have."""
    # Register a worker with no capabilities
    worker_data = {
        "worker_id": "worker-1",
        "node_id": "node-1",
        "agent_type": "generic",
        "workspace_root": "/tmp",
        "capabilities": [],
        "status": "idle",
        "registered_at": datetime.now(UTC).isoformat(),
        "last_heartbeat": datetime.now(UTC).isoformat(),
    }
    backend.register_worker(worker_data)

    # Carry a task requiring "gpu"
    backend.carry(_make_task_with_caps("task-gpu", ["gpu"]))

    # Worker without gpu should get nothing
    result = backend.pull("worker-1")
    assert result is None


def test_pull_matches_tasks_with_met_capabilities(backend: FileBackend, tmp_path: Path) -> None:
    """pull() returns a task when worker has all required capabilities."""
    worker_data = {
        "worker_id": "worker-2",
        "node_id": "node-1",
        "agent_type": "generic",
        "workspace_root": "/tmp",
        "capabilities": ["gpu", "docker"],
        "status": "idle",
        "registered_at": datetime.now(UTC).isoformat(),
        "last_heartbeat": datetime.now(UTC).isoformat(),
    }
    backend.register_worker(worker_data)

    backend.carry(_make_task_with_caps("task-gpu", ["gpu"]))

    result = backend.pull("worker-2")
    assert result is not None
    assert result["id"] == "task-gpu"


# ---------------------------------------------------------------------------
# Pin / unpin
# ---------------------------------------------------------------------------


def test_pin_task_sets_pinned_to(backend: FileBackend, tmp_path: Path) -> None:
    """pin_task() writes pinned_to field on the ready task."""
    backend.carry(_make_task("task-1"))
    backend.pin_task("task-1", "worker-7")

    data = backend.get_task("task-1")
    assert data is not None
    assert data["pinned_to"] == "worker-7"


def test_unpin_task_clears_pinned_to(backend: FileBackend) -> None:
    """unpin_task() clears pinned_to back to None."""
    backend.carry(_make_task("task-1"))
    backend.pin_task("task-1", "worker-7")
    backend.unpin_task("task-1")

    data = backend.get_task("task-1")
    assert data is not None
    assert data["pinned_to"] is None


def test_pin_task_not_found_raises(backend: FileBackend) -> None:
    """pin_task() raises FileNotFoundError for unknown task."""
    with pytest.raises(FileNotFoundError):
        backend.pin_task("no-such-task", "worker-1")


def test_unpin_task_not_found_raises(backend: FileBackend) -> None:
    """unpin_task() raises FileNotFoundError for unknown task."""
    with pytest.raises(FileNotFoundError):
        backend.unpin_task("no-such-task")


def test_pull_skips_task_pinned_to_other_worker(backend: FileBackend) -> None:
    """pull() skips tasks where pinned_to != worker_id."""
    backend.carry(_make_task("task-1"))
    backend.pin_task("task-1", "worker-pinned")

    result = backend.pull("worker-other")
    assert result is None


def test_pull_returns_task_pinned_to_correct_worker(backend: FileBackend) -> None:
    """pull() returns task when pinned_to matches worker_id."""
    backend.carry(_make_task("task-1"))
    backend.pin_task("task-1", "worker-pinned")

    result = backend.pull("worker-pinned")
    assert result is not None
    assert result["id"] == "task-1"


def test_pull_returns_unpinned_task_to_any_worker(backend: FileBackend) -> None:
    """pull() returns tasks with pinned_to=None to any worker."""
    backend.carry(_make_task("task-1"))

    result = backend.pull("worker-any")
    assert result is not None
    assert result["id"] == "task-1"


# ---------------------------------------------------------------------------
# Override merge order
# ---------------------------------------------------------------------------


def _carry_harvest(backend: FileBackend, task_id: str) -> str:
    """Carry a task, pull it, harvest it, and return the attempt_id."""
    backend.carry(_make_task(task_id))
    pulled = backend.pull("worker-1")
    assert pulled is not None
    attempt_id = pulled["current_attempt"]
    backend.mark_harvested(task_id, attempt_id, pr="pr", branch="branch")
    return attempt_id


def test_override_merge_order_sets_field(backend: FileBackend) -> None:
    """override_merge_order() writes merge_override to the done task."""
    _carry_harvest(backend, "task-1")
    backend.override_merge_order("task-1", 1)

    data = backend.get_task("task-1")
    assert data is not None
    assert data["merge_override"] == 1


def test_clear_merge_override_resets_to_none(backend: FileBackend) -> None:
    """clear_merge_override() sets merge_override back to None."""
    _carry_harvest(backend, "task-1")
    backend.override_merge_order("task-1", 2)
    backend.clear_merge_override("task-1")

    data = backend.get_task("task-1")
    assert data is not None
    assert data["merge_override"] is None


def test_override_merge_order_not_found_raises(backend: FileBackend) -> None:
    """override_merge_order() raises FileNotFoundError for unknown task."""
    with pytest.raises(FileNotFoundError):
        backend.override_merge_order("no-such-task", 1)


def test_clear_merge_override_not_found_raises(backend: FileBackend) -> None:
    """clear_merge_override() raises FileNotFoundError for unknown task."""
    with pytest.raises(FileNotFoundError):
        backend.clear_merge_override("no-such-task")


# ---------------------------------------------------------------------------
# Rate limit: pull() returns None for rate-limited worker
# ---------------------------------------------------------------------------


def test_pull_returns_none_for_rate_limited_worker(backend: FileBackend) -> None:
    """pull() skips tasks when the worker has an active cooldown."""
    from datetime import UTC, datetime, timedelta

    backend.carry(_make_task("task-1"))

    # Register the worker with a future cooldown
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    backend.register_worker({
        "worker_id": "worker-rl",
        "node_id": "node-1",
        "agent_type": "claude-code",
        "workspace_root": "/tmp/ws",
        "capabilities": [],
        "status": "idle",
        "registered_at": datetime.now(UTC).isoformat(),
        "last_heartbeat": datetime.now(UTC).isoformat(),
        "cooldown_until": future,
    })

    result = backend.pull("worker-rl")
    assert result is None


def test_pull_succeeds_after_cooldown_expires(backend: FileBackend) -> None:
    """pull() claims a task when the worker's cooldown is in the past."""
    from datetime import UTC, datetime, timedelta

    backend.carry(_make_task("task-1"))

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    backend.register_worker({
        "worker_id": "worker-past",
        "node_id": "node-1",
        "agent_type": "claude-code",
        "workspace_root": "/tmp/ws",
        "capabilities": [],
        "status": "idle",
        "registered_at": datetime.now(UTC).isoformat(),
        "last_heartbeat": datetime.now(UTC).isoformat(),
        "cooldown_until": past,
    })

    result = backend.pull("worker-past")
    assert result is not None
    assert result["id"] == "task-1"


# ---------------------------------------------------------------------------
# Rate limit: list_workers() returns all worker dicts
# ---------------------------------------------------------------------------


def test_list_workers_empty(backend: FileBackend) -> None:
    """list_workers() returns [] when no workers are registered."""
    assert backend.list_workers() == []


def test_list_workers_returns_all(backend: FileBackend) -> None:
    """list_workers() returns one entry per registered worker."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    for i in range(3):
        backend.register_worker({
            "worker_id": f"worker-{i}",
            "node_id": "node-1",
            "agent_type": "claude-code",
            "workspace_root": f"/tmp/ws-{i}",
            "capabilities": [],
            "status": "idle",
            "registered_at": now,
            "last_heartbeat": now,
        })

    workers = backend.list_workers()
    assert len(workers) == 3
    ids = {w["worker_id"] for w in workers}
    assert ids == {"worker-0", "worker-1", "worker-2"}


# ---------------------------------------------------------------------------
# v0.5.1: mark_harvest_pending
# ---------------------------------------------------------------------------


def test_mark_harvest_pending_updates_status(backend: FileBackend) -> None:
    """mark_harvest_pending() transitions active task to harvest_pending."""
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None
    attempt_id = pulled["current_attempt"]

    backend.mark_harvest_pending("task-1", attempt_id)

    task = backend.get_task("task-1")
    assert task is not None
    assert task["status"] == "harvest_pending"


def test_mark_harvest_pending_wrong_attempt_raises(backend: FileBackend) -> None:
    """mark_harvest_pending() rejects wrong attempt_id."""
    backend.carry(_make_task("task-1"))
    backend.pull("worker-1")

    with pytest.raises(ValueError, match="not the current attempt"):
        backend.mark_harvest_pending("task-1", "bogus-attempt")


def test_mark_harvest_pending_not_active_raises(backend: FileBackend) -> None:
    """mark_harvest_pending() raises for task not in active/."""
    backend.carry(_make_task("task-1"))
    with pytest.raises(FileNotFoundError):
        backend.mark_harvest_pending("task-1", "att-1")


def test_harvest_pending_then_harvested(backend: FileBackend) -> None:
    """Full flow: active → harvest_pending → done (via mark_harvested)."""
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None
    attempt_id = pulled["current_attempt"]

    backend.mark_harvest_pending("task-1", attempt_id)
    task = backend.get_task("task-1")
    assert task["status"] == "harvest_pending"

    # mark_harvested should still work from harvest_pending (file still in active/)
    backend.mark_harvested("task-1", attempt_id, pr="pr-1", branch="feat/t1")
    task = backend.get_task("task-1")
    assert task["status"] == "done"


# ---------------------------------------------------------------------------
# v0.5.1: backward compatibility for old persisted state
# ---------------------------------------------------------------------------


def test_old_state_loads_with_new_lifecycle(tmp_path: Path) -> None:
    """Existing task JSON with old 'ready'/'active'/'done' values loads correctly."""
    data_dir = tmp_path / ".antfarm"
    ready_dir = data_dir / "tasks" / "ready"
    ready_dir.mkdir(parents=True)
    # Create minimal required dirs
    for d in ["tasks/active", "tasks/done", "tasks/paused", "tasks/blocked",
              "workers", "nodes", "guards"]:
        (data_dir / d).mkdir(parents=True, exist_ok=True)

    old_task = {
        "id": "task-old",
        "title": "Old task",
        "spec": "x",
        "status": "ready",  # old state name
        "complexity": "M",
        "priority": 10,
        "depends_on": [],
        "touches": [],
        "capabilities_required": [],
        "pinned_to": None,
        "merge_override": None,
        "current_attempt": None,
        "attempts": [],
        "trail": [],
        "signals": [],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "created_by": "test",
    }
    (ready_dir / "task-old.json").write_text(json.dumps(old_task))

    backend = FileBackend(root=str(data_dir))

    tasks = backend.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "task-old"

    # Should be pullable
    backend.register_worker({
        "worker_id": "w1",
        "node_id": "n1",
        "agent_type": "generic",
        "workspace_root": "/tmp",
        "capabilities": [],
        "status": "idle",
        "registered_at": "2026-01-01T00:00:00Z",
        "last_heartbeat": "2026-01-01T00:00:00Z",
    })
    result = backend.pull("w1")
    assert result is not None
    assert result["id"] == "task-old"


# ---------------------------------------------------------------------------
# v0.5.2: artifact storage on harvest
# ---------------------------------------------------------------------------


def test_harvest_stores_artifact(backend: FileBackend) -> None:
    """mark_harvested() with artifact stores it on the attempt."""
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None
    attempt_id = pulled["current_attempt"]

    artifact = {
        "task_id": "task-1",
        "attempt_id": attempt_id,
        "worker_id": "worker-1",
        "branch": "feat/task-1",
        "files_changed": ["src/foo.py"],
        "lines_added": 10,
        "merge_readiness": "ready",
    }
    backend.mark_harvested("task-1", attempt_id, pr="pr-1", branch="feat/t1", artifact=artifact)

    task = backend.get_task("task-1")
    assert task is not None
    for a in task["attempts"]:
        if a["attempt_id"] == attempt_id:
            assert a.get("artifact") is not None
            assert a["artifact"]["files_changed"] == ["src/foo.py"]
            break
    else:
        pytest.fail("Attempt not found")


def test_harvest_without_artifact_backward_compat(backend: FileBackend) -> None:
    """mark_harvested() without artifact still works (no artifact key)."""
    backend.carry(_make_task("task-1"))
    pulled = backend.pull("worker-1")
    assert pulled is not None
    attempt_id = pulled["current_attempt"]

    backend.mark_harvested("task-1", attempt_id, pr="pr-1", branch="feat/t1")

    task = backend.get_task("task-1")
    assert task is not None
    for a in task["attempts"]:
        if a["attempt_id"] == attempt_id:
            assert "artifact" not in a
            break


# ---------------------------------------------------------------------------
# Max-attempt enforcement
# ---------------------------------------------------------------------------


def _kickback_cycle(backend: FileBackend, task_id: str) -> None:
    """Helper: pull -> harvest -> kickback a task through one full cycle."""
    pulled = backend.pull("worker-1")
    assert pulled is not None
    assert pulled["id"] == task_id
    attempt_id = pulled["current_attempt"]
    backend.mark_harvested(task_id, attempt_id, pr="pr", branch="branch")
    backend.kickback(task_id, reason="tests failed", max_attempts=3)


def test_kickback_blocks_after_max_attempts(
    backend: FileBackend, tmp_path: Path
) -> None:
    """After max_attempts kickbacks, task transitions to blocked/."""
    backend.carry(_make_task("task-1"))

    # First two kickbacks: task goes back to ready
    _kickback_cycle(backend, "task-1")
    assert backend.get_task("task-1")["status"] == TaskStatus.READY.value

    _kickback_cycle(backend, "task-1")
    assert backend.get_task("task-1")["status"] == TaskStatus.READY.value

    # Third kickback: blocked (max_attempts=3 means 3 total attempts)
    pulled = backend.pull("worker-1")
    assert pulled is not None
    attempt_id = pulled["current_attempt"]
    backend.mark_harvested(
        "task-1", attempt_id, pr="pr", branch="branch"
    )
    backend.kickback("task-1", reason="tests failed again", max_attempts=3)

    task = backend.get_task("task-1")
    assert task is not None
    assert task["status"] == TaskStatus.BLOCKED.value
    blocked_file = (
        tmp_path / ".antfarm" / "tasks" / "blocked" / "task-1.json"
    )
    assert blocked_file.exists()


def test_blocked_task_not_forageable(backend: FileBackend) -> None:
    """A task in blocked/ should not be returned by pull()."""
    backend.carry(_make_task("task-1"))

    # Exhaust max attempts (max_attempts=1 -> blocked after first kickback)
    pulled = backend.pull("worker-1")
    assert pulled is not None
    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-1", attempt_id, pr="pr", branch="branch")
    backend.kickback("task-1", reason="fail", max_attempts=1)

    # Task is now blocked — pull should return nothing
    result = backend.pull("worker-1")
    assert result is None


def test_kickback_under_max_attempts_goes_to_ready(
    backend: FileBackend,
) -> None:
    """Kickback before reaching max_attempts transitions task to ready."""
    backend.carry(_make_task("task-1"))

    # With max_attempts=5, first kickback should go to ready
    pulled = backend.pull("worker-1")
    assert pulled is not None
    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-1", attempt_id, pr="pr", branch="branch")
    backend.kickback("task-1", reason="fail", max_attempts=5)

    task = backend.get_task("task-1")
    assert task is not None
    assert task["status"] == TaskStatus.READY.value


def test_per_task_max_attempts_override(backend: FileBackend) -> None:
    """Task-level max_attempts field overrides the function parameter."""
    task = _make_task("task-1")
    task["max_attempts"] = 1  # task says 1 attempt max
    backend.carry(task)

    # Even though function default is 3, task-level override of 1 wins
    pulled = backend.pull("worker-1")
    assert pulled is not None
    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-1", attempt_id, pr="pr", branch="branch")
    backend.kickback("task-1", reason="fail", max_attempts=3)

    task = backend.get_task("task-1")
    assert task is not None
    assert task["status"] == TaskStatus.BLOCKED.value


def test_unblock_does_not_reset_attempt_counter(
    backend: FileBackend,
) -> None:
    """After unblocking, the next kickback blocks again (no reset)."""
    backend.carry(_make_task("task-1"))

    # Exhaust max attempts (max_attempts=1)
    pulled = backend.pull("worker-1")
    assert pulled is not None
    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-1", attempt_id, pr="pr", branch="branch")
    backend.kickback("task-1", reason="fail", max_attempts=1)
    assert (
        backend.get_task("task-1")["status"] == TaskStatus.BLOCKED.value
    )

    # Unblock it
    backend.unblock_task("task-1")
    assert (
        backend.get_task("task-1")["status"] == TaskStatus.READY.value
    )

    # Another cycle should block again since attempts still counted
    pulled = backend.pull("worker-1")
    assert pulled is not None
    attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-1", attempt_id, pr="pr", branch="branch")
    backend.kickback("task-1", reason="fail again", max_attempts=1)
    assert (
        backend.get_task("task-1")["status"] == TaskStatus.BLOCKED.value
    )


# ---------------------------------------------------------------------------
# Node list/get tests
# ---------------------------------------------------------------------------


def test_register_node_with_runner_url(backend: FileBackend) -> None:
    now = datetime.now(UTC).isoformat()
    node = {
        "node_id": "node-r1",
        "joined_at": now,
        "last_seen": now,
        "runner_url": "http://localhost:7433",
        "max_workers": 4,
        "capabilities": ["claude-code", "codex"],
    }
    backend.register_node(node)
    data = backend.get_node("node-r1")
    assert data is not None
    assert data["runner_url"] == "http://localhost:7433"
    assert data["max_workers"] == 4
    assert data["capabilities"] == ["claude-code", "codex"]

    # Re-register with updated fields — should merge
    later = datetime.now(UTC).isoformat()
    node_updated = {
        "node_id": "node-r1",
        "last_seen": later,
        "runner_url": "http://newhost:8000",
        "max_workers": 8,
    }
    backend.register_node(node_updated)
    data = backend.get_node("node-r1")
    assert data["runner_url"] == "http://newhost:8000"
    assert data["max_workers"] == 8
    assert data["last_seen"] == later
    # Original fields preserved
    assert data["joined_at"] == now
    assert data["capabilities"] == ["claude-code", "codex"]


def test_list_nodes(backend: FileBackend) -> None:
    now = datetime.now(UTC).isoformat()
    backend.register_node({"node_id": "node-a", "joined_at": now, "last_seen": now})
    backend.register_node({"node_id": "node-b", "joined_at": now, "last_seen": now})

    nodes = backend.list_nodes()
    assert len(nodes) == 2
    node_ids = {n["node_id"] for n in nodes}
    assert node_ids == {"node-a", "node-b"}


def test_list_nodes_empty(backend: FileBackend) -> None:
    nodes = backend.list_nodes()
    assert nodes == []


# ---------------------------------------------------------------------------
# Stale-tolerant register_worker (issue #194)
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _make_worker(worker_id: str = "worker-1") -> dict:
    now = _iso_now()
    return {
        "worker_id": worker_id,
        "node_id": "node-1",
        "agent_type": "claude-code",
        "workspace_root": "/tmp/ws",
        "registered_at": now,
        "last_heartbeat": now,
    }


def test_register_worker_rejects_fresh_duplicate(tmp_path: Path) -> None:
    """Re-registering a worker with a fresh heartbeat raises ValueError."""
    backend = FileBackend(root=tmp_path / ".antfarm", guard_ttl=5)
    backend.register_worker(_make_worker("worker-1"))

    with pytest.raises(ValueError, match="already registered and live"):
        backend.register_worker(_make_worker("worker-1"))


def test_register_worker_accepts_stale_duplicate(tmp_path: Path) -> None:
    """Re-registering over a stale (mtime > guard_ttl) worker file succeeds and overwrites it."""
    backend = FileBackend(root=tmp_path / ".antfarm", guard_ttl=5)
    first = _make_worker("worker-1")
    first["workspace_root"] = "/tmp/old"
    backend.register_worker(first)

    worker_path = backend._worker_path("worker-1")
    old_time = time.time() - (backend._guard_ttl + 60)
    os.utime(str(worker_path), (old_time, old_time))

    new_worker = _make_worker("worker-1")
    new_worker["workspace_root"] = "/tmp/new"
    backend.register_worker(new_worker)

    data = json.loads(worker_path.read_text())
    assert data["workspace_root"] == "/tmp/new"


def test_register_worker_new_id_succeeds(tmp_path: Path) -> None:
    """Registering a never-seen worker_id writes the file."""
    backend = FileBackend(root=tmp_path / ".antfarm", guard_ttl=5)
    backend.register_worker(_make_worker("worker-new"))

    assert backend._worker_path("worker-new").exists()


def test_register_worker_boundary_at_guard_ttl_rejects(tmp_path: Path) -> None:
    """Boundary direction: `age <= _guard_ttl` rejects.

    We cannot set mtime to *exactly* guard_ttl ago (clock drift between utime() and
    register_worker() pushes age > guard_ttl). Instead, we pick a large guard_ttl
    and an age just inside the boundary — any age <= guard_ttl must reject.
    """
    backend = FileBackend(root=tmp_path / ".antfarm", guard_ttl=3600)
    backend.register_worker(_make_worker("worker-1"))

    worker_path = backend._worker_path("worker-1")
    # Age = guard_ttl - 1s: well inside the rejection window, robust to drift.
    inside_boundary = time.time() - (backend._guard_ttl - 1)
    os.utime(str(worker_path), (inside_boundary, inside_boundary))

    with pytest.raises(ValueError, match="already registered and live"):
        backend.register_worker(_make_worker("worker-1"))

