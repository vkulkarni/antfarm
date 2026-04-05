"""Integration tests verifying FileBackend.pull() delegates to scheduler.

Ensures pull() is no longer using inline scheduling logic and that
scope preference (active task avoidance) works end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from antfarm.core.backends.file import FileBackend


def _make_task(
    task_id: str = "task-1",
    priority: int = 10,
    depends_on: list | None = None,
    touches: list | None = None,
) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "spec": "Do something",
        "complexity": "M",
        "priority": priority,
        "depends_on": depends_on or [],
        "touches": touches or [],
        "created_at": now,
        "updated_at": now,
        "created_by": "test",
    }


@pytest.fixture()
def backend(tmp_path: Path) -> FileBackend:
    return FileBackend(root=tmp_path / ".antfarm")


def test_pull_delegates_to_scheduler(backend: FileBackend) -> None:
    """pull() must call scheduler.select_task() — not use inline logic."""
    backend.carry(_make_task("task-001", touches=["api"]))
    backend.carry(_make_task("task-002", touches=["frontend"]))

    with patch("antfarm.core.backends.file.select_task") as mock:
        mock.return_value = None
        result = backend.pull("w1")
        mock.assert_called_once()
        assert result is None


def test_pull_passes_active_tasks_to_scheduler(backend: FileBackend) -> None:
    """Scope preference requires active tasks in scheduler input."""
    import time

    backend.carry(_make_task("task-001", touches=["api"]))
    time.sleep(0.01)
    backend.carry(_make_task("task-002", touches=["api"]))
    time.sleep(0.01)
    backend.carry(_make_task("task-003", touches=["frontend"]))

    r1 = backend.pull("w1")
    assert r1 is not None
    assert r1["id"] == "task-001"  # first api task claimed

    # Second pull should prefer non-overlapping (frontend) over overlapping (api)
    r2 = backend.pull("w2")
    assert r2 is not None
    assert r2["id"] == "task-003"  # frontend preferred over second api task


def test_pull_scope_preference_falls_back_when_only_overlapping(
    backend: FileBackend,
) -> None:
    """When only overlapping tasks remain, scheduler still returns one."""
    import time

    backend.carry(_make_task("task-api-1", touches=["api"]))
    time.sleep(0.01)
    backend.carry(_make_task("task-api-2", touches=["api"]))

    r1 = backend.pull("w1")
    assert r1 is not None

    r2 = backend.pull("w2")
    assert r2 is not None
    assert r2["id"] == "task-api-2"  # falls back to overlapping


def test_pull_scheduler_receives_done_task_ids(backend: FileBackend) -> None:
    """pull() passes completed task IDs so deps are checked correctly."""
    # task-002 depends on task-001
    backend.carry(_make_task("task-001"))
    backend.carry(_make_task("task-002", depends_on=["task-001"]))

    # Only task-001 should be pulled (task-002 blocked by dep)
    r1 = backend.pull("w1")
    assert r1 is not None
    assert r1["id"] == "task-001"

    # task-002 still blocked
    r2 = backend.pull("w2")
    assert r2 is None

    # Complete task-001
    backend.mark_harvested("task-001", r1["current_attempt"], pr="pr", branch="b")

    # Now task-002 should be available
    r3 = backend.pull("w2")
    assert r3 is not None
    assert r3["id"] == "task-002"
