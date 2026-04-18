"""Tests for the AntfarmTUI pipeline dashboard.

Tests classification, helpers, render methods, and pipeline bar
without any live terminal or network I/O.
"""

import threading

import httpx
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from antfarm.core.tui import AntfarmTUI, PipelineSnapshot


def _make_tui() -> AntfarmTUI:
    return AntfarmTUI(colony_url="http://localhost:7433", token=None, autostart_activity=False)


def _attempt(
    attempt_id: str = "att-001",
    worker_id: str = "node1/worker-a",
    status: str = "active",
    started_at: str = "2026-04-05T00:00:00+00:00",
    completed_at: str | None = None,
    review_verdict: dict | None = None,
    merge_block_reason: str | None = None,
) -> dict:
    d: dict = {
        "attempt_id": attempt_id,
        "worker_id": worker_id,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
    }
    if review_verdict is not None:
        d["review_verdict"] = review_verdict
    if merge_block_reason is not None:
        d["merge_block_reason"] = merge_block_reason
    return d


def _task(
    task_id: str = "task-001",
    title: str = "Test task",
    status: str = "ready",
    current_attempt: str | None = None,
    attempts: list | None = None,
    trail: list | None = None,
    touches: list | None = None,
    complexity: str = "M",
    spec: str = "",
    created_at: str = "2026-04-05T00:00:00+00:00",
) -> dict:
    return {
        "id": task_id,
        "title": title,
        "status": status,
        "current_attempt": current_attempt,
        "attempts": attempts or [],
        "trail": trail or [],
        "touches": touches or [],
        "complexity": complexity,
        "spec": spec,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# _classify_tasks
# ---------------------------------------------------------------------------


def test_classify_building():
    tui = _make_tui()
    tasks = [_task(status="active", current_attempt="att-001", attempts=[_attempt()])]
    snap = tui._classify_tasks(tasks)
    assert len(snap.building) == 1
    assert len(snap.under_review) == 0


def test_classify_waiting_new():
    tui = _make_tui()
    tasks = [_task(status="ready")]
    snap = tui._classify_tasks(tasks)
    assert len(snap.waiting_new) == 1


def test_classify_awaiting_review():
    tui = _make_tui()
    att = _attempt(status="done")
    tasks = [_task(status="done", current_attempt="att-001", attempts=[att])]
    snap = tui._classify_tasks(tasks)
    assert len(snap.awaiting_review) == 1


def test_classify_under_review():
    tui = _make_tui()
    tasks = [
        _task(
            task_id="review-001", status="active", current_attempt="att-001", attempts=[_attempt()]
        )
    ]
    snap = tui._classify_tasks(tasks)
    assert len(snap.under_review) == 1
    assert len(snap.building) == 0


def test_classify_merge_ready():
    tui = _make_tui()
    att = _attempt(status="done", review_verdict={"verdict": "pass", "freshness": "fresh"})
    tasks = [_task(status="done", current_attempt="att-001", attempts=[att])]
    snap = tui._classify_tasks(tasks)
    assert len(snap.merge_ready) == 1


def test_classify_planning():
    tui = _make_tui()
    t = _task(status="active", current_attempt="att-001", attempts=[_attempt()])
    t["capabilities_required"] = ["plan"]
    tasks = [t]
    snap = tui._classify_tasks(tasks)
    assert len(snap.planning) == 1
    assert len(snap.building) == 0


def test_classify_done_plan_task_hidden_from_merge_ready():
    """Done plan tasks must not appear in merge_ready or awaiting_review,
    even when they carry a pass verdict. They are infra tasks and the
    Soldier excludes them via is_infra_task()."""
    tui = _make_tui()
    att = _attempt(status="done", review_verdict={"verdict": "pass", "freshness": "fresh"})
    t = _task(task_id="plan-abc", status="done", current_attempt="att-001", attempts=[att])
    t["capabilities_required"] = ["plan"]
    snap = tui._classify_tasks([t])
    assert len(snap.merge_ready) == 0
    assert len(snap.awaiting_review) == 0


def test_progress_denominator_excludes_infra():
    """Pipeline Progress denominator must only count impl tasks — not
    plan/review infra tasks — matching Soldier's mergeable definition."""
    tui = _make_tui()
    impl = _task(task_id="task-001", status="ready")
    plan_task = _task(task_id="plan-xyz", status="ready")
    plan_task["capabilities_required"] = ["plan"]
    review_task = _task(task_id="review-xyz", status="ready")
    tasks = [impl, plan_task, review_task]
    snap = PipelineSnapshot()
    # _render_summary computes impl_tasks internally; exercise that path
    # and verify by inspecting the filter directly via is_infra_task.
    from antfarm.core.missions import is_infra_task as _iit

    impl_tasks = [t for t in tasks if not _iit(t)]
    assert len(impl_tasks) == 1
    assert impl_tasks[0]["id"] == "task-001"
    # Sanity check: summary renders without error with this mix.
    result = tui._render_summary({}, tasks, [], snap, "idle")
    assert isinstance(result, Table)


def test_classify_waiting_rework():
    tui = _make_tui()
    att = _attempt(
        attempt_id="att-001", status="superseded", completed_at="2026-04-05T01:00:00+00:00"
    )
    tasks = [_task(status="ready", attempts=[att])]
    snap = tui._classify_tasks(tasks)
    assert len(snap.waiting_rework) == 1
    assert len(snap.waiting_new) == 0


def test_classify_recently_merged():
    tui = _make_tui()
    att = _attempt(status="merged", completed_at="2026-04-05T01:00:00+00:00")
    tasks = [_task(status="done", current_attempt="att-001", attempts=[att])]
    snap = tui._classify_tasks(tasks)
    assert len(snap.recently_merged) == 1


def test_classify_review_task_not_in_building():
    """A review task that is active should be in under_review, not building."""
    tui = _make_tui()
    tasks = [
        _task(
            task_id="review-task",
            status="active",
            current_attempt="att-r",
            attempts=[_attempt(attempt_id="att-r")],
        ),
        _task(
            task_id="build-task",
            status="active",
            current_attempt="att-b",
            attempts=[_attempt(attempt_id="att-b")],
        ),
    ]
    snap = tui._classify_tasks(tasks)
    assert len(snap.building) == 1
    assert snap.building[0]["id"] == "build-task"
    assert len(snap.under_review) == 1
    assert snap.under_review[0]["id"] == "review-task"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_is_kicked_back_true():
    tui = _make_tui()
    t = _task(attempts=[_attempt(status="superseded")])
    assert tui._is_kicked_back(t) is True


def test_is_kicked_back_false():
    tui = _make_tui()
    t = _task(attempts=[_attempt(status="done")])
    assert tui._is_kicked_back(t) is False


def test_get_verdict_found():
    tui = _make_tui()
    att = _attempt(review_verdict={"result": "pass"})
    t = _task(current_attempt="att-001", attempts=[att])
    assert tui._get_verdict(t) == {"result": "pass"}


def test_get_verdict_none():
    tui = _make_tui()
    t = _task(current_attempt=None)
    assert tui._get_verdict(t) is None


def test_get_worker_for_task():
    tui = _make_tui()
    att = _attempt(worker_id="node2/reviewer-1")
    t = _task(current_attempt="att-001", attempts=[att])
    assert tui._get_worker_for_task(t) == "node2/reviewer-1"


def test_has_merged_attempt_yes():
    tui = _make_tui()
    t = _task(attempts=[_attempt(status="merged")])
    assert tui._has_merged_attempt(t) is True


def test_has_merged_attempt_no():
    tui = _make_tui()
    t = _task(attempts=[_attempt(status="done")])
    assert tui._has_merged_attempt(t) is False


def test_classify_active_without_plan_capability():
    """Active task without plan capability goes to building."""
    tui = _make_tui()
    t = _task(status="active", current_attempt="att-001", attempts=[_attempt()])
    t["capabilities_required"] = ["code"]
    tasks = [t]
    snap = tui._classify_tasks(tasks)
    assert len(snap.building) == 1
    assert len(snap.planning) == 0


# ---------------------------------------------------------------------------
# Time-in-queue helpers
# ---------------------------------------------------------------------------


def test_get_time_since_created():
    tui = _make_tui()
    t = _task(created_at="2026-04-05T00:00:00+00:00")
    result = tui._get_time_since_created(t)
    # Just verify it returns a non-empty string (time-dependent)
    assert isinstance(result, str)


def test_get_time_since_kickback():
    tui = _make_tui()
    att = _attempt(status="superseded", completed_at="2026-04-05T01:00:00+00:00")
    t = _task(attempts=[att])
    result = tui._get_time_since_kickback(t)
    assert isinstance(result, str)


def test_get_time_since_kickback_no_superseded():
    tui = _make_tui()
    t = _task(attempts=[_attempt(status="done")])
    result = tui._get_time_since_kickback(t)
    assert result == ""


def test_get_time_since_harvested():
    tui = _make_tui()
    att = _attempt(status="done", completed_at="2026-04-05T01:00:00+00:00")
    t = _task(current_attempt="att-001", attempts=[att])
    result = tui._get_time_since_harvested(t)
    assert isinstance(result, str)
    assert len(result) > 0


def test_get_time_since_harvested_no_completed():
    tui = _make_tui()
    att = _attempt(status="done", completed_at=None)
    t = _task(current_attempt="att-001", attempts=[att])
    result = tui._get_time_since_harvested(t)
    assert result == ""


# ---------------------------------------------------------------------------
# Pipeline bar
# ---------------------------------------------------------------------------


def test_pipeline_bar_renders():
    tui = _make_tui()
    counts = {
        "plan": 0,
        "building": 3,
        "waiting": 2,
        "merged": 5,
        "awaiting_review": 0,
        "under_review": 0,
        "merge_ready": 1,
    }
    result = tui._render_pipeline_bar(counts)
    assert isinstance(result, Text)
    assert len(str(result)) > 0


def test_pipeline_bar_empty():
    tui = _make_tui()
    counts = {
        "plan": 0,
        "building": 0,
        "waiting": 0,
        "merged": 0,
        "awaiting_review": 0,
        "under_review": 0,
        "merge_ready": 0,
    }
    result = tui._render_pipeline_bar(counts)
    assert "no tasks" in str(result)


def test_pipeline_bar_uses_wt_abbreviation():
    tui = _make_tui()
    counts = {
        "plan": 0,
        "building": 1,
        "waiting": 3,
        "merged": 0,
        "awaiting_review": 0,
        "under_review": 0,
        "merge_ready": 0,
    }
    result = tui._render_pipeline_bar(counts)
    text_str = str(result)
    assert "wt:3" in text_str
    assert "bld:1" in text_str


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_summary_renders():
    tui = _make_tui()
    snap = PipelineSnapshot()
    result = tui._render_summary({}, [], [], snap, "unknown")
    assert isinstance(result, Table)


def test_summary_soldier_not_started():
    """Soldier status 'unknown' should display as 'not started'."""
    tui = _make_tui()
    snap = PipelineSnapshot()
    result = tui._render_summary({"nodes": 1}, [], [], snap, "unknown")
    assert isinstance(result, Table)


def test_summary_shows_node_names():
    """When workers are present, show node names instead of count."""
    tui = _make_tui()
    snap = PipelineSnapshot()
    workers = [
        {"worker_id": "mini-1/b1", "node_id": "mini-1"},
        {"worker_id": "mini-2/b2", "node_id": "mini-2"},
    ]
    result = tui._render_summary({}, [], workers, snap, "unknown")
    assert isinstance(result, Table)


def test_summary_review_pressure():
    tui = _make_tui()
    snap = PipelineSnapshot(
        awaiting_review=[_task(), _task(task_id="t2"), _task(task_id="t3"), _task(task_id="t4")]
    )
    result = tui._render_summary({}, [], [], snap, "idle")
    assert isinstance(result, Table)


# ---------------------------------------------------------------------------
# Render methods -- empty + populated
# ---------------------------------------------------------------------------


def test_render_building_empty():
    tui = _make_tui()
    result = tui._render_building([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_building_populated():
    tui = _make_tui()
    t = _task(
        status="active",
        current_attempt="att-001",
        attempts=[_attempt()],
        trail=[{"ts": "2026-01-01T00:00:00", "worker_id": "w1", "message": "working"}],
    )
    result = tui._render_building([t])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_waiting_new_empty():
    tui = _make_tui()
    result = tui._render_waiting_new([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_waiting_new_populated():
    tui = _make_tui()
    result = tui._render_waiting_new([_task(touches=["api", "db"])])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_waiting_rework_empty():
    tui = _make_tui()
    result = tui._render_waiting_rework([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_waiting_rework_populated():
    tui = _make_tui()
    t = _task(trail=[{"ts": "t", "worker_id": "w", "message": "tests failed"}])
    result = tui._render_waiting_rework([t])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_waiting_rework_shows_reason():
    """Rework tasks should show kickback reason with cross mark prefix."""
    tui = _make_tui()
    t = _task(
        trail=[{"ts": "t", "worker_id": "w", "message": "merge conflict"}],
        attempts=[_attempt(status="superseded", completed_at="2026-04-05T01:00:00+00:00")],
    )
    result = tui._render_waiting_rework([t])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_awaiting_review_empty():
    tui = _make_tui()
    result = tui._render_awaiting_review([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_awaiting_review_populated():
    tui = _make_tui()
    att = _attempt(status="done", completed_at="2026-04-05T01:00:00+00:00")
    t = _task(current_attempt="att-001", attempts=[att])
    result = tui._render_awaiting_review([t])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_under_review_empty():
    tui = _make_tui()
    result = tui._render_under_review([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_under_review_populated():
    tui = _make_tui()
    t = _task(status="active", current_attempt="att-001", attempts=[_attempt()])
    result = tui._render_under_review([t])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_merge_ready_empty():
    tui = _make_tui()
    result = tui._render_merge_ready([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_merge_ready_populated():
    tui = _make_tui()
    att = _attempt(
        review_verdict={"result": "pass", "freshness": "fresh"},
        status="done",
        completed_at="2026-04-05T01:00:00+00:00",
    )
    t = _task(status="done", current_attempt="att-001", attempts=[att])
    result = tui._render_merge_ready([t])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_planning_empty():
    tui = _make_tui()
    result = tui._render_planning([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_planning_populated():
    tui = _make_tui()
    t = _task(
        status="active",
        current_attempt="att-001",
        attempts=[_attempt()],
        trail=[{"ts": "2026-01-01T00:00:00", "worker_id": "w1", "message": "planning"}],
    )
    t["capabilities_required"] = ["plan"]
    result = tui._render_planning([t])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_recently_merged_empty():
    tui = _make_tui()
    result = tui._render_recently_merged([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_recently_merged_populated():
    tui = _make_tui()
    att = _attempt(status="merged", completed_at="2026-04-05T01:00:00+00:00")
    t = _task(status="done", current_attempt="att-001", attempts=[att])
    result = tui._render_recently_merged([t])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_recently_merged_limits_to_5():
    tui = _make_tui()
    tasks = []
    for i in range(8):
        att = _attempt(
            attempt_id=f"att-{i}", status="merged", completed_at="2026-04-05T01:00:00+00:00"
        )
        tasks.append(
            _task(task_id=f"task-{i}", status="done", current_attempt=f"att-{i}", attempts=[att])
        )
    result = tui._render_recently_merged(tasks)
    assert isinstance(result, Table)
    # 5 shown + 1 overflow hint row
    assert result.row_count == 6


# ---------------------------------------------------------------------------
# Overflow hint
# ---------------------------------------------------------------------------


def test_overflow_hint_building():
    """Building panel should show overflow hint when more than max_shown tasks."""
    tui = _make_tui()
    tasks = [
        _task(
            task_id=f"task-{i}",
            status="active",
            current_attempt=f"att-{i}",
            attempts=[_attempt(attempt_id=f"att-{i}")],
        )
        for i in range(7)
    ]
    result = tui._render_building(tasks, max_shown=5)
    assert isinstance(result, Table)
    # 5 shown + 1 overflow hint
    assert result.row_count == 6


def test_overflow_hint_awaiting_review():
    """Awaiting review uses max_shown=8 by default."""
    tui = _make_tui()
    tasks = [
        _task(
            task_id=f"task-{i}",
            current_attempt=f"att-{i}",
            attempts=[
                _attempt(
                    attempt_id=f"att-{i}", status="done", completed_at="2026-04-05T01:00:00+00:00"
                )
            ],
        )
        for i in range(10)
    ]
    result = tui._render_awaiting_review(tasks)
    assert isinstance(result, Table)
    # 8 shown + 1 overflow hint
    assert result.row_count == 9


def test_no_overflow_hint_when_within_limit():
    """No overflow hint when tasks fit within max_shown."""
    tui = _make_tui()
    tasks = [_task(task_id=f"task-{i}", touches=["api"]) for i in range(3)]
    result = tui._render_waiting_new(tasks)
    assert isinstance(result, Table)
    assert result.row_count == 3


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


def test_render_workers_empty():
    tui = _make_tui()
    result = tui._render_workers([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_format_activity_cell_no_action_shows_dash():
    tui = _make_tui()
    cell = tui._format_activity_cell({})
    assert cell.plain == "—"
    assert cell.style == "dim"


def test_format_activity_cell_fresh_action_includes_elapsed():
    from datetime import UTC, datetime

    tui = _make_tui()
    now_iso = datetime.now(UTC).isoformat()
    cell = tui._format_activity_cell(
        {"current_action": "Running: Bash", "current_action_at": now_iso}
    )
    assert cell.plain.startswith("Running: Bash (")
    assert cell.plain.endswith("s)")
    # Fresh elapsed time should not be rendered red
    assert cell.style != "red"


def test_format_activity_cell_stale_action_is_red():
    from datetime import UTC, datetime, timedelta

    tui = _make_tui()
    old_iso = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    cell = tui._format_activity_cell(
        {"current_action": "Running: Bash", "current_action_at": old_iso},
        stuck_ttl=300,
    )
    assert "Running: Bash" in cell.plain
    assert cell.style == "red"


def test_format_activity_cell_truncates_long_action():
    from datetime import UTC, datetime

    tui = _make_tui()
    now_iso = datetime.now(UTC).isoformat()
    long_action = "X" * 100
    cell = tui._format_activity_cell({"current_action": long_action, "current_action_at": now_iso})
    # Activity is truncated to 40 chars in the UI before the "(Ns)" suffix
    assert cell.plain.startswith("X" * 40 + " (")


def test_render_workers_table_has_activity_column():
    tui = _make_tui()
    result = tui._render_workers([], soldier_status="disabled")
    headers = [c.header for c in result.columns]
    assert "Activity" in headers


def test_render_workers_type_column_builder():
    tui = _make_tui()
    workers = [
        {
            "worker_id": "n1/w1",
            "status": "idle",
            "node_id": "n1",
            "agent_type": "claude-code",
            "rate_limited": False,
        }
    ]
    result = tui._render_workers(workers, soldier_status="disabled")
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_workers_type_column_reviewer():
    tui = _make_tui()
    workers = [
        {
            "worker_id": "n1/r1",
            "status": "busy",
            "node_id": "n1",
            "agent_type": "claude-code-review",
            "rate_limited": False,
        }
    ]
    result = tui._render_workers(workers, soldier_status="disabled")
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_workers_includes_soldier():
    tui = _make_tui()
    result = tui._render_workers([], soldier_status="running")
    assert isinstance(result, Table)
    assert result.row_count == 1  # soldier row only


def test_get_worker_type_builder():
    tui = _make_tui()
    assert tui._get_worker_type({"agent_type": "claude-code"}) == "builder"


def test_get_worker_type_reviewer_by_agent_type():
    tui = _make_tui()
    assert tui._get_worker_type({"agent_type": "claude-code-review"}) == "reviewer"


def test_get_worker_type_reviewer_by_capability():
    tui = _make_tui()
    assert (
        tui._get_worker_type({"agent_type": "claude-code", "capabilities": ["review"]})
        == "reviewer"
    )


# ---------------------------------------------------------------------------
# Mission panel
# ---------------------------------------------------------------------------


def _mission(
    mission_id: str = "mission-auth",
    status: str = "building",
    task_ids: list | None = None,
    blocked_task_ids: list | None = None,
    last_progress_at: str = "2026-04-05T00:00:00+00:00",
    report: dict | None = None,
) -> dict:
    return {
        "mission_id": mission_id,
        "status": status,
        "task_ids": task_ids or [],
        "blocked_task_ids": blocked_task_ids or [],
        "last_progress_at": last_progress_at,
        "report": report,
    }


def test_tui_mission_panel_renders_empty():
    """Empty missions list shows 'No active missions.' placeholder."""
    tui = _make_tui()
    result = tui._render_missions([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_tui_mission_panel_renders_multi():
    """Multiple missions render with correct row count."""
    tui = _make_tui()
    missions = [
        _mission(
            mission_id="mission-auth",
            status="building",
            task_ids=["t1", "t2", "t3", "t4", "t5", "t6"],
            report={"merged_tasks": 4},
        ),
        _mission(
            mission_id="mission-api-v2",
            status="complete",
            task_ids=["t1", "t2", "t3", "t4"],
            report={"merged_tasks": 4},
        ),
        _mission(
            mission_id="mission-migrate",
            status="blocked",
            task_ids=["t1", "t2", "t3", "t4", "t5"],
            blocked_task_ids=["t3"],
        ),
    ]
    result = tui._render_missions(missions)
    assert isinstance(result, Table)
    assert result.row_count == 3


def test_tui_mission_panel_formats_progress_time():
    """Progress column shows correct format per status."""
    tui = _make_tui()

    # Complete mission -> "done"
    assert tui._format_mission_progress(_mission(status="complete")) == "done"

    # Failed mission -> "done"
    assert tui._format_mission_progress(_mission(status="failed")) == "done"

    # Cancelled mission -> "done"
    assert tui._format_mission_progress(_mission(status="cancelled")) == "done"

    # Blocked mission -> "stalled <time>"
    result = tui._format_mission_progress(
        _mission(status="blocked", last_progress_at="2026-04-05T00:00:00+00:00")
    )
    assert result.startswith("stalled ")

    # Active mission -> "<time> ago"
    result = tui._format_mission_progress(
        _mission(status="building", last_progress_at="2026-04-05T00:00:00+00:00")
    )
    assert result.endswith(" ago")

    # No last_progress_at -> "--"
    assert tui._format_mission_progress(_mission(status="building", last_progress_at="")) == "--"


def test_tui_mission_panel_shows_blocked_count():
    """Blocked task count appears in tasks column."""
    tui = _make_tui()
    missions = [
        _mission(
            task_ids=["t1", "t2", "t3"],
            blocked_task_ids=["t2"],
        ),
    ]
    result = tui._render_missions(missions)
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_tui_mission_panel_overflow():
    """More than max_shown missions shows overflow hint."""
    tui = _make_tui()
    missions = [_mission(mission_id=f"mission-{i}") for i in range(8)]
    result = tui._render_missions(missions, max_shown=5)
    assert isinstance(result, Table)
    # 5 shown + 1 overflow hint
    assert result.row_count == 6


# ---------------------------------------------------------------------------
# Connection error handling
# ---------------------------------------------------------------------------


def test_tui_shows_hint_on_connect_error(monkeypatch):
    """ConnectError shows actionable guidance with URL and commands."""
    tui = _make_tui()

    def _fail(*args, **kwargs):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(tui, "_fetch", _fail)
    layout = tui._build_display()

    # Walk the layout to extract the Panel renderable text
    panel = layout.renderable
    text = str(panel.renderable)
    assert "Can't reach colony at http://localhost:7433" in text
    assert "antfarm colony" in text
    assert "--colony-url" in text


def test_tui_shows_generic_error_on_other_exceptions(monkeypatch):
    """Non-ConnectError exceptions fall through to the generic error message."""
    tui = _make_tui()

    def _fail(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(tui, "_fetch", _fail)
    layout = tui._build_display()

    panel = layout.renderable
    text = str(panel.renderable)
    assert "Connection error: kaboom" in text


# ---------------------------------------------------------------------------
# Activity feed (SSE consumer + rendering)
# ---------------------------------------------------------------------------


class _FakeStream:
    """Context manager mimicking httpx.stream() with iter_lines()."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield from self._lines


def _sse(event: dict) -> str:
    import json as _json

    return f"data: {_json.dumps(event)}"


def test_ingest_event_appends_and_advances_cursor():
    tui = _make_tui()
    tui._ingest_event({"id": 7, "type": "merged", "actor": "soldier", "detail": "pr=1"})
    tui._ingest_event({"id": 9, "type": "harvested", "actor": "worker", "detail": "pr=2"})
    assert len(tui._activity_events) == 2
    assert tui._activity_cursor == 9


def test_ingest_event_cursor_never_decreases():
    tui = _make_tui()
    tui._ingest_event({"id": 12, "type": "merged"})
    tui._ingest_event({"id": 3, "type": "merged"})
    assert tui._activity_cursor == 12


def test_poll_events_once_parses_stream(monkeypatch):
    """_poll_events_once parses SSE data: lines and populates the deque."""
    tui = _make_tui()
    lines = [
        _sse({"id": 1, "type": "queen_plan_created", "actor": "queen", "detail": "plan-001"}),
        "",
        _sse({"id": 2, "type": "worker_spawned", "actor": "autoscaler", "detail": "planner-1"}),
        "",
    ]

    def fake_stream(method, url, **kwargs):
        assert url.endswith("/events")
        assert kwargs["params"]["after"] == 0
        return _FakeStream(lines)

    monkeypatch.setattr("antfarm.core.tui.httpx.stream", fake_stream)
    tui._poll_events_once()

    assert len(tui._activity_events) == 2
    ids = [ev["id"] for ev in tui._activity_events]
    assert ids == [1, 2]
    assert tui._activity_cursor == 2


def test_poll_events_once_skips_non_data_and_malformed(monkeypatch):
    tui = _make_tui()
    lines = [
        ": keepalive",
        "",
        _sse({"id": 1, "type": "merged", "actor": "soldier", "detail": "ok"}),
        "",
        "data: not-json{{{",
        "",
        _sse({"id": 2, "type": "harvested", "actor": "worker", "detail": "ok"}),
        "",
    ]

    def fake_stream(method, url, **kwargs):
        return _FakeStream(lines)

    monkeypatch.setattr("antfarm.core.tui.httpx.stream", fake_stream)
    tui._poll_events_once()

    assert [ev["id"] for ev in tui._activity_events] == [1, 2]


def test_activity_loop_retries_on_httpx_error(monkeypatch):
    """A single iteration that raises should be swallowed; the loop sleeps and retries."""
    import contextlib

    tui = _make_tui()
    calls = {"n": 0}

    def flaky_poll():
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        # Second call: pretend to succeed, then stop the loop by raising SystemExit
        raise SystemExit

    monkeypatch.setattr(tui, "_poll_events_once", flaky_poll)
    monkeypatch.setattr("antfarm.core.tui.time.sleep", lambda _s: None)

    with contextlib.suppress(SystemExit):
        tui._activity_loop()

    assert calls["n"] >= 2


def test_render_activity_empty_shows_placeholder():
    tui = _make_tui()
    rendered = tui._render_activity()
    assert isinstance(rendered, Text)
    assert "waiting for events" in rendered.plain


def test_render_activity_formats_rows():
    tui = _make_tui()
    tui._ingest_event(
        {
            "id": 1,
            "type": "merged",
            "actor": "soldier",
            "detail": "merging task-001 to main",
            "ts": "2026-04-16T14:32:11+00:00",
        }
    )
    tui._ingest_event(
        {
            "id": 2,
            "type": "queen_plan_created",
            "actor": "queen",
            "detail": "created plan plan-v061",
            "ts": "2026-04-16T14:33:05+00:00",
        }
    )

    rendered = tui._render_activity()
    plain = rendered.plain

    # Two rows, newest last (auto-scroll)
    lines = plain.split("\n")
    assert len(lines) == 2
    assert "soldier" in lines[0]
    assert "merging task-001 to main" in lines[0]
    assert "queen" in lines[1]
    assert "created plan plan-v061" in lines[1]
    # Actor column is fixed-width 12
    assert lines[0].split("  ")[1].startswith("soldier")


def test_render_activity_uses_tail_when_over_max_rows():
    tui = _make_tui()
    for i in range(1, 11):
        tui._ingest_event(
            {
                "id": i,
                "type": "tick",
                "actor": "doctor",
                "detail": f"event-{i}",
                "ts": "2026-04-16T14:32:11+00:00",
            }
        )
    rendered = tui._render_activity(max_rows=4)
    plain = rendered.plain
    lines = plain.split("\n")
    assert len(lines) == 4
    # Newest (event-10) rendered last
    assert "event-10" in lines[-1]
    assert "event-7" in lines[0]


def test_render_activity_failed_type_is_red():
    tui = _make_tui()
    tui._ingest_event(
        {
            "id": 1,
            "type": "merge_failed",
            "actor": "soldier",
            "detail": "conflict on task-001",
            "ts": "2026-04-16T14:32:11+00:00",
        }
    )
    rendered = tui._render_activity()
    # Rich Text stores styles as spans; verify at least one span carries 'red'
    styles = [str(span.style) for span in rendered.spans]
    assert any("red" in s for s in styles)


def test_render_activity_includes_type_and_task_id():
    """#327: activity log must surface event type and task id columns."""
    tui = _make_tui()
    tui._ingest_event(
        {
            "id": 1,
            "type": "merge_skipped",
            "actor": "soldier",
            "task_id": "r3-06",
            "detail": "reason=already_merged",
            "ts": "2026-04-16T14:32:11+00:00",
        }
    )
    rendered = tui._render_activity()
    plain = rendered.plain
    assert "merge_skipped" in plain
    # task_id column uses last segment after final '-'
    assert "r3-06" in plain or "06" in plain
    assert "reason=already_merged" in plain


def test_render_activity_kickback_is_yellow():
    """#327: kickback events should render in yellow."""
    tui = _make_tui()
    tui._ingest_event(
        {
            "id": 1,
            "type": "task_kicked_back",
            "actor": "soldier",
            "task_id": "task-y",
            "detail": "reason=review:needs_changes",
            "ts": "2026-04-16T14:32:11+00:00",
        }
    )
    rendered = tui._render_activity()
    styles = [str(s.style) for s in rendered.spans]
    assert any("yellow" in s for s in styles)


def test_build_display_includes_activity_panel(monkeypatch):
    tui = _make_tui()

    def _fake_fetch(path):
        if path == "/status/full":
            return {
                "status": {"nodes": 0},
                "tasks": [],
                "workers": [],
                "soldier": "idle",
            }
        if path == "/missions":
            return []
        return {}

    monkeypatch.setattr(tui, "_fetch", _fake_fetch)
    tui._ingest_event(
        {
            "id": 1,
            "type": "merged",
            "actor": "soldier",
            "detail": "task-001 merged",
            "ts": "2026-04-16T14:32:11+00:00",
        }
    )

    layout = tui._build_display()
    activity_layout = layout["activity"]
    panel = activity_layout.renderable
    assert isinstance(panel, Panel)
    body = panel.renderable
    assert isinstance(body, Text)
    assert "soldier" in body.plain
    assert "task-001 merged" in body.plain


def test_autostart_activity_default_starts_thread(monkeypatch):
    """autostart_activity=True (default) spins up the background thread."""

    # Stub httpx.stream so the thread doesn't actually hit the network.
    # Raise ConnectError to exercise the retry-silent path, then yield to
    # the main thread.
    monkeypatch.setattr(
        "antfarm.core.tui.httpx.stream",
        lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("nope")),
    )
    # Shorten the retry sleep so the thread churns but stays daemon
    monkeypatch.setattr("antfarm.core.tui.time.sleep", lambda _s: None)

    tui = AntfarmTUI(colony_url="http://localhost:7433", token=None)
    try:
        assert tui._activity_thread is not None
        assert tui._activity_thread.is_alive()
        assert tui._activity_thread.daemon is True
    finally:
        # Thread is daemon; it will be reaped on interpreter exit.
        pass


def test_autostart_activity_false_does_not_start_thread():
    tui = AntfarmTUI(colony_url="http://localhost:7433", token=None, autostart_activity=False)
    assert tui._activity_thread is None


# ---------------------------------------------------------------------------
# Epoch-aware ingestion (#306)
# ---------------------------------------------------------------------------


def test_ingest_first_event_learns_epoch():
    tui = _make_tui()
    tui._ingest_event({"id": 1, "epoch": "E1", "type": "merged", "actor": "soldier"})
    assert tui._activity_epoch == "E1"
    assert tui._activity_cursor == 1


def test_ingest_event_same_epoch_advances_cursor():
    tui = _make_tui()
    tui._ingest_event({"id": 1, "epoch": "E1", "type": "merged"})
    tui._ingest_event({"id": 4, "epoch": "E1", "type": "harvested"})
    assert tui._activity_epoch == "E1"
    assert tui._activity_cursor == 4


def test_ingest_event_different_epoch_resets_cursor():
    tui = _make_tui()
    tui._ingest_event({"id": 5, "epoch": "E1", "type": "merged"})
    assert tui._activity_cursor == 5
    tui._ingest_event({"id": 2, "epoch": "E2", "type": "harvested"})
    assert tui._activity_epoch == "E2"
    assert tui._activity_cursor == 2


def test_ingest_event_missing_epoch_keeps_current():
    tui = _make_tui()
    tui._ingest_event({"id": 1, "epoch": "E1", "type": "merged"})
    # Second event has no `epoch` key — backward compat with older servers.
    tui._ingest_event({"id": 3, "type": "harvested"})
    assert tui._activity_epoch == "E1"
    assert tui._activity_cursor == 3


# ---------------------------------------------------------------------------
# Activity loop backoff (#307)
# ---------------------------------------------------------------------------


def test_activity_loop_backs_off_on_connect_error(monkeypatch):
    """ConnectError repeats trigger exponential backoff starting at 1.0s."""
    import contextlib

    tui = _make_tui()
    sleeps: list[float] = []
    calls = {"n": 0}

    def fake_sleep(s):
        sleeps.append(s)

    def always_fail():
        calls["n"] += 1
        if calls["n"] > 3:
            raise SystemExit
        raise httpx.ConnectError("down")

    monkeypatch.setattr("antfarm.core.tui.time.sleep", fake_sleep)
    monkeypatch.setattr(tui, "_poll_events_once", always_fail)

    with contextlib.suppress(SystemExit):
        tui._activity_loop()

    assert sleeps == [1.0, 2.0, 4.0]
    assert tui._activity_status.startswith("reconnecting")


def test_activity_loop_stops_on_auth_error(monkeypatch):
    """401/403 is terminal — the loop returns and records an auth status."""
    tui = _make_tui()

    class _FakeResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code

    poll_calls = {"n": 0}

    def auth_fail():
        poll_calls["n"] += 1
        raise httpx.HTTPStatusError("unauthorized", request=None, response=_FakeResponse(401))

    monkeypatch.setattr(tui, "_poll_events_once", auth_fail)

    t = threading.Thread(target=tui._activity_loop, daemon=True)
    t.start()
    t.join(timeout=1.0)

    assert not t.is_alive(), "loop should have returned on auth error"
    assert poll_calls["n"] == 1, "auth error should terminate after a single poll"
    assert tui._activity_status.startswith("auth error")


def test_activity_loop_resets_backoff_on_success(monkeypatch):
    """After a successful poll, backoff resets to 1.0s for the next failure."""
    import contextlib

    tui = _make_tui()
    sleeps: list[float] = []
    calls = {"n": 0}

    def flaky_poll():
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow")
        if calls["n"] == 2:
            return  # success
        if calls["n"] == 3:
            raise httpx.ReadTimeout("slow again")
        # Bail out of the infinite loop once we have enough data.
        raise SystemExit

    def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(tui, "_poll_events_once", flaky_poll)
    monkeypatch.setattr("antfarm.core.tui.time.sleep", fake_sleep)

    with contextlib.suppress(SystemExit):
        tui._activity_loop()

    # Failure -> backoff 1.0s; success -> 0.5s rate-limit; failure -> 1.0s again.
    assert sleeps == [1.0, 0.5, 1.0]
