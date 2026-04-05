"""Tests for the AntfarmTUI render helpers.

Tests _render_summary, _render_tasks, and _render_workers in isolation
without any live terminal or network I/O.
"""

from rich.table import Table

from antfarm.core.tui import AntfarmTUI


def _make_tui() -> AntfarmTUI:
    return AntfarmTUI(colony_url="http://localhost:7433", token=None)


# ---------------------------------------------------------------------------
# _render_summary
# ---------------------------------------------------------------------------


def test_render_summary_returns_table():
    tui = _make_tui()
    status = {
        "nodes": 2,
        "workers": 3,
        "tasks_ready": 1,
        "tasks_active": 2,
        "tasks_done": 5,
        "tasks_paused": 0,
        "tasks_blocked": 0,
    }
    result = tui._render_summary(status)
    assert isinstance(result, Table)


def test_render_summary_empty_status():
    tui = _make_tui()
    result = tui._render_summary({})
    assert isinstance(result, Table)


def test_render_summary_partial_status():
    tui = _make_tui()
    result = tui._render_summary({"nodes": 1, "workers": 1})
    assert isinstance(result, Table)


# ---------------------------------------------------------------------------
# _render_tasks
# ---------------------------------------------------------------------------


def test_render_tasks_empty_list():
    tui = _make_tui()
    result = tui._render_tasks([], "Active Tasks", ["active"])
    assert isinstance(result, Table)
    # Should have one row with empty indicator
    assert result.row_count == 1


def test_render_tasks_with_active_task():
    tui = _make_tui()
    tasks = [
        {
            "id": "task-001",
            "title": "Implement feature X",
            "status": "active",
            "current_attempt": {"worker_id": "node1/worker-a"},
            "trail": [
                {"ts": "2026-01-01T00:00:00", "worker_id": "node1/worker-a",
                 "message": "Working on it"},
            ],
        }
    ]
    result = tui._render_tasks(tasks, "Active", ["active"])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_tasks_with_done_task():
    tui = _make_tui()
    tasks = [
        {
            "id": "task-002",
            "title": "Fix bug Y",
            "status": "done",
            "current_attempt": None,
            "trail": [],
        }
    ]
    result = tui._render_tasks(tasks, "Done", ["done"])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_tasks_no_trail():
    tui = _make_tui()
    tasks = [
        {
            "id": "task-003",
            "title": "Empty trail task",
            "status": "ready",
            "current_attempt": None,
            "trail": [],
        }
    ]
    result = tui._render_tasks(tasks, "Ready", ["ready"])
    assert isinstance(result, Table)


def test_render_tasks_long_trail_message_truncated():
    tui = _make_tui()
    long_msg = "A" * 100
    tasks = [
        {
            "id": "task-004",
            "title": "Long message task",
            "status": "active",
            "current_attempt": {"worker_id": "w1"},
            "trail": [
                {"ts": "2026-01-01T00:00:00", "worker_id": "w1", "message": long_msg},
            ],
        }
    ]
    result = tui._render_tasks(tasks, "Active", ["active"])
    assert isinstance(result, Table)


def test_render_tasks_multiple_tasks():
    tui = _make_tui()
    tasks = [
        {
            "id": f"task-{i}", "title": f"Task {i}", "status": "ready",
            "current_attempt": None, "trail": [],
        }
        for i in range(5)
    ]
    result = tui._render_tasks(tasks, "Ready", ["ready"])
    assert isinstance(result, Table)
    assert result.row_count == 5


# ---------------------------------------------------------------------------
# _render_workers
# ---------------------------------------------------------------------------


def test_render_workers_empty_list():
    tui = _make_tui()
    result = tui._render_workers([])
    assert isinstance(result, Table)
    assert result.row_count == 1  # empty indicator row


def test_render_workers_idle_worker():
    tui = _make_tui()
    workers = [
        {
            "worker_id": "node1/worker-a",
            "status": "idle",
            "node_id": "node1",
            "rate_limited": False,
            "rate_limit_until": None,
        }
    ]
    result = tui._render_workers(workers)
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_workers_rate_limited():
    tui = _make_tui()
    workers = [
        {
            "worker_id": "node2/worker-b",
            "status": "idle",
            "node_id": "node2",
            "rate_limited": True,
            "rate_limit_until": "2026-04-04T10:30:00",
        }
    ]
    result = tui._render_workers(workers)
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_workers_rate_limited_no_until():
    tui = _make_tui()
    workers = [
        {
            "worker_id": "node3/worker-c",
            "status": "busy",
            "node_id": "node3",
            "rate_limited": True,
            "rate_limit_until": None,
        }
    ]
    result = tui._render_workers(workers)
    assert isinstance(result, Table)


def test_render_workers_multiple():
    tui = _make_tui()
    workers = [
        {"worker_id": f"node1/w{i}", "status": "idle", "node_id": "node1", "rate_limited": False}
        for i in range(3)
    ]
    result = tui._render_workers(workers)
    assert isinstance(result, Table)
    assert result.row_count == 3
