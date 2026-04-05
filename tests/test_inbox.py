"""Tests for antfarm.core.inbox operator inbox collection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from antfarm.core.inbox import collect_inbox_items


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _past(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def test_inbox_finds_stale_workers():
    """Workers with expired heartbeat appear in inbox."""
    workers = [
        {"worker_id": "w1", "last_heartbeat": _past(600)},
    ]
    items = collect_inbox_items(tasks=[], workers=workers, stale_worker_ttl=300)
    assert len(items) == 1
    assert items[0]["type"] == "stale_worker"
    assert items[0]["severity"] == "error"
    assert "w1" in items[0]["message"]


def test_inbox_healthy_worker_not_flagged():
    """Workers with recent heartbeat do not appear in inbox."""
    workers = [
        {"worker_id": "w1", "last_heartbeat": _now()},
    ]
    items = collect_inbox_items(tasks=[], workers=workers)
    assert len(items) == 0


def test_inbox_finds_blocked_tasks():
    """Tasks blocked by unmet deps appear in inbox."""
    tasks = [
        {"id": "task-2", "status": "ready", "depends_on": ["task-1"],
         "attempts": [], "trail": [], "signals": []},
    ]
    items = collect_inbox_items(tasks=tasks, workers=[])
    assert len(items) == 1
    assert items[0]["type"] == "blocked_by_deps"
    assert "task-1" in items[0]["message"]


def test_inbox_blocked_task_cleared_when_dep_done():
    """Tasks whose deps are done do not appear as blocked."""
    tasks = [
        {"id": "task-1", "status": "done", "depends_on": [],
         "attempts": [], "trail": [], "signals": []},
        {"id": "task-2", "status": "ready", "depends_on": ["task-1"],
         "attempts": [], "trail": [], "signals": []},
    ]
    items = collect_inbox_items(tasks=tasks, workers=[])
    blocked_items = [i for i in items if i["type"] == "blocked_by_deps"]
    assert len(blocked_items) == 0


def test_inbox_finds_failed_tasks():
    """Tasks with failed status appear in inbox."""
    tasks = [
        {"id": "task-1", "status": "failed", "depends_on": [],
         "attempts": [], "trail": [{"message": "test_failure: tests failed"}],
         "signals": []},
    ]
    items = collect_inbox_items(tasks=tasks, workers=[])
    failed = [i for i in items if i["type"] == "failed_task"]
    assert len(failed) == 1
    assert failed[0]["severity"] == "error"


def test_inbox_finds_harvest_pending():
    """Tasks stuck in harvest_pending appear in inbox."""
    tasks = [
        {"id": "task-1", "status": "harvest_pending", "depends_on": [],
         "attempts": [], "trail": [], "signals": []},
    ]
    items = collect_inbox_items(tasks=tasks, workers=[])
    hp = [i for i in items if i["type"] == "harvest_interrupted"]
    assert len(hp) == 1
    assert hp[0]["severity"] == "error"


def test_inbox_finds_long_running_tasks():
    """Active tasks running longer than threshold appear in inbox."""
    tasks = [
        {"id": "task-1", "status": "active", "depends_on": [],
         "current_attempt": "att-1",
         "attempts": [{"attempt_id": "att-1", "started_at": _past(7200), "worker_id": "w1"}],
         "trail": [], "signals": []},
    ]
    items = collect_inbox_items(tasks=tasks, workers=[], long_running_threshold=3600)
    lr = [i for i in items if i["type"] == "long_running"]
    assert len(lr) == 1
    assert lr[0]["severity"] == "warning"


def test_inbox_finds_kicked_back_tasks():
    """Kicked-back tasks appear in inbox."""
    tasks = [
        {"id": "task-1", "status": "kicked_back", "depends_on": [],
         "attempts": [], "trail": [{"message": "merge conflict"}],
         "signals": []},
    ]
    items = collect_inbox_items(tasks=tasks, workers=[])
    kb = [i for i in items if i["type"] == "kicked_back"]
    assert len(kb) == 1
    assert kb[0]["severity"] == "info"


def test_inbox_finds_tasks_with_signals():
    """Tasks with signals appear in inbox."""
    tasks = [
        {"id": "task-1", "status": "active", "depends_on": [],
         "current_attempt": "att-1",
         "attempts": [{"attempt_id": "att-1", "started_at": _now(), "worker_id": "w1"}],
         "trail": [],
         "signals": [{"worker_id": "w1", "message": "needs re-scoping"}]},
    ]
    items = collect_inbox_items(tasks=tasks, workers=[])
    sig = [i for i in items if i["type"] == "has_signal"]
    assert len(sig) == 1


def test_inbox_empty_when_healthy():
    """No inbox items when everything is healthy."""
    tasks = [
        {"id": "task-1", "status": "active", "depends_on": [],
         "current_attempt": "att-1",
         "attempts": [{"attempt_id": "att-1", "started_at": _now(), "worker_id": "w1"}],
         "trail": [], "signals": []},
    ]
    workers = [
        {"worker_id": "w1", "last_heartbeat": _now()},
    ]
    items = collect_inbox_items(tasks=tasks, workers=workers)
    assert len(items) == 0


def test_inbox_sorts_by_severity():
    """Errors come before warnings, warnings before info."""
    tasks = [
        {"id": "t-kicked", "status": "kicked_back", "depends_on": [],
         "attempts": [], "trail": [{"message": "conflict"}], "signals": []},
        {"id": "t-failed", "status": "failed", "depends_on": [],
         "attempts": [], "trail": [{"message": "crash"}], "signals": []},
    ]
    workers = [
        {"worker_id": "w-stale", "last_heartbeat": _past(600)},
    ]
    items = collect_inbox_items(tasks=tasks, workers=workers, stale_worker_ttl=300)
    severities = [i["severity"] for i in items]
    assert severities == sorted(severities, key=lambda s: {"error": 0, "warning": 1, "info": 2}[s])
