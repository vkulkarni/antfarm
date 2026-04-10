"""Tests for FileBackend mission CRUD and link_task_to_mission."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from antfarm.core.backends.file import FileBackend
from antfarm.core.backends.github import GitHubBackend
from antfarm.core.missions import link_task_to_mission

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _make_mission(mission_id: str = "mission-login-123", status: str = "building") -> dict:
    now = _now_iso()
    return {
        "mission_id": mission_id,
        "spec": "Build a login flow",
        "spec_file": None,
        "status": status,
        "plan_task_id": None,
        "plan_artifact": None,
        "task_ids": [],
        "blocked_task_ids": [],
        "config": {},
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "report": None,
        "last_progress_at": now,
        "re_plan_count": 0,
    }


def _make_task(task_id: str = "task-1", mission_id: str | None = None) -> dict:
    now = _now_iso()
    task = {
        "id": task_id,
        "title": f"Task {task_id}",
        "spec": "Do something",
        "complexity": "M",
        "priority": 10,
        "depends_on": [],
        "touches": [],
        "created_at": now,
        "updated_at": now,
        "created_by": "test",
    }
    if mission_id is not None:
        task["mission_id"] = mission_id
    return task


@pytest.fixture()
def backend(tmp_path: Path) -> FileBackend:
    return FileBackend(root=tmp_path / ".antfarm")


# ---------------------------------------------------------------------------
# create_mission
# ---------------------------------------------------------------------------


def test_create_mission_writes_file(backend: FileBackend, tmp_path: Path) -> None:
    mission = _make_mission()
    result = backend.create_mission(mission)
    assert result == "mission-login-123"

    path = tmp_path / ".antfarm" / "missions" / "mission-login-123.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["mission_id"] == "mission-login-123"
    assert data["status"] == "building"


def test_create_mission_duplicate_raises(backend: FileBackend) -> None:
    backend.create_mission(_make_mission())
    with pytest.raises(ValueError, match="already exists"):
        backend.create_mission(_make_mission())


# ---------------------------------------------------------------------------
# get_mission
# ---------------------------------------------------------------------------


def test_get_mission_returns_data(backend: FileBackend) -> None:
    backend.create_mission(_make_mission())
    result = backend.get_mission("mission-login-123")
    assert result is not None
    assert result["mission_id"] == "mission-login-123"


def test_get_mission_not_found(backend: FileBackend) -> None:
    result = backend.get_mission("nonexistent")
    assert result is None


# ---------------------------------------------------------------------------
# update_mission
# ---------------------------------------------------------------------------


def test_update_mission_shallow_merge(backend: FileBackend) -> None:
    backend.create_mission(_make_mission())
    backend.update_mission("mission-login-123", {"status": "complete", "completed_at": _now_iso()})
    result = backend.get_mission("mission-login-123")
    assert result["status"] == "complete"
    assert result["completed_at"] is not None
    # Original fields preserved
    assert result["spec"] == "Build a login flow"


def test_update_mission_sets_updated_at(backend: FileBackend) -> None:
    mission = _make_mission()
    original_updated = mission["updated_at"]
    backend.create_mission(mission)
    backend.update_mission("mission-login-123", {"status": "blocked"})
    result = backend.get_mission("mission-login-123")
    assert result["updated_at"] != original_updated


def test_update_mission_not_found_raises(backend: FileBackend) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        backend.update_mission("nonexistent", {"status": "complete"})


# ---------------------------------------------------------------------------
# list_missions
# ---------------------------------------------------------------------------


def test_list_missions_all(backend: FileBackend) -> None:
    backend.create_mission(_make_mission("m-1", status="building"))
    backend.create_mission(_make_mission("m-2", status="complete"))
    results = backend.list_missions()
    assert len(results) == 2
    ids = {m["mission_id"] for m in results}
    assert ids == {"m-1", "m-2"}


def test_list_missions_filter_by_status(backend: FileBackend) -> None:
    backend.create_mission(_make_mission("m-1", status="building"))
    backend.create_mission(_make_mission("m-2", status="complete"))
    results = backend.list_missions(status="building")
    assert len(results) == 1
    assert results[0]["mission_id"] == "m-1"


# ---------------------------------------------------------------------------
# carry preserves mission_id
# ---------------------------------------------------------------------------


def test_carry_preserves_mission_id(backend: FileBackend) -> None:
    task = _make_task("task-m1", mission_id="mission-login-123")
    backend.carry(task)
    result = backend.get_task("task-m1")
    assert result is not None
    assert result["mission_id"] == "mission-login-123"


# ---------------------------------------------------------------------------
# update_mission atomicity
# ---------------------------------------------------------------------------


def test_update_mission_atomic_under_lock(backend: FileBackend) -> None:
    backend.create_mission(_make_mission())
    errors = []

    def updater(i: int) -> None:
        """Each thread updates a different field so both writes are preserved."""
        try:
            backend.update_mission("mission-login-123", {f"custom_field_{i}": f"value-{i}"})
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=updater, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    result = backend.get_mission("mission-login-123")
    assert result is not None
    for i in range(5):
        assert result[f"custom_field_{i}"] == f"value-{i}", (
            f"custom_field_{i} missing — concurrent updates clobbered each other"
        )


# ---------------------------------------------------------------------------
# link_task_to_mission
# ---------------------------------------------------------------------------


def test_link_task_to_mission_appends_task_id(backend: FileBackend) -> None:
    backend.create_mission(_make_mission())
    task = _make_task("task-linked", mission_id="mission-login-123")
    result = link_task_to_mission(backend, task, "mission-login-123")
    assert result == "task-linked"

    mission = backend.get_mission("mission-login-123")
    assert mission is not None
    assert "task-linked" in mission["task_ids"]

    task_data = backend.get_task("task-linked")
    assert task_data is not None
    assert task_data["mission_id"] == "mission-login-123"


def test_link_task_to_mission_missing_mission_raises(backend: FileBackend) -> None:
    task = _make_task("task-orphan", mission_id="nonexistent")
    with pytest.raises(FileNotFoundError, match="not found"):
        link_task_to_mission(backend, task, "nonexistent")


def test_link_task_to_mission_terminal_mission_raises(backend: FileBackend) -> None:
    for terminal_status in ("complete", "failed", "cancelled"):
        mid = f"mission-{terminal_status}"
        backend.create_mission(_make_mission(mid, status=terminal_status))
        task = _make_task(f"task-{terminal_status}", mission_id=mid)
        with pytest.raises(ValueError, match="terminal state"):
            link_task_to_mission(backend, task, mid)


# ---------------------------------------------------------------------------
# GitHubBackend stubs
# ---------------------------------------------------------------------------


def test_github_backend_stubs_raise() -> None:
    # We can't instantiate GitHubBackend without a real repo, so test
    # that the stubs are defined and raise correctly via the class directly.
    gb = GitHubBackend.__new__(GitHubBackend)
    with pytest.raises(NotImplementedError, match="Mission mode requires FileBackend"):
        gb.create_mission({})
    with pytest.raises(NotImplementedError, match="Mission mode requires FileBackend"):
        gb.get_mission("x")
    with pytest.raises(NotImplementedError, match="Mission mode requires FileBackend"):
        gb.list_missions()
    with pytest.raises(NotImplementedError, match="Mission mode requires FileBackend"):
        gb.update_mission("x", {})
