"""Tests for the AntfarmTUI pipeline dashboard.

Tests classification, helpers, render methods, and pipeline bar
without any live terminal or network I/O.
"""

from rich.table import Table
from rich.text import Text

from antfarm.core.tui import AntfarmTUI, PipelineSnapshot


def _make_tui() -> AntfarmTUI:
    return AntfarmTUI(colony_url="http://localhost:7433", token=None)


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
    }


# ---------------------------------------------------------------------------
# _classify_tasks
# ---------------------------------------------------------------------------


def test_classify_building():
    tui = _make_tui()
    tasks = [_task(status="active", current_attempt="att-001",
                   attempts=[_attempt()])]
    snap = tui._classify_tasks(tasks)
    assert len(snap.building) == 1
    assert len(snap.under_review) == 0


def test_classify_backlog():
    tui = _make_tui()
    tasks = [_task(status="ready")]
    snap = tui._classify_tasks(tasks)
    assert len(snap.backlog) == 1


def test_classify_awaiting_review():
    tui = _make_tui()
    att = _attempt(status="done")
    tasks = [_task(status="done", current_attempt="att-001", attempts=[att])]
    snap = tui._classify_tasks(tasks)
    assert len(snap.awaiting_review) == 1


def test_classify_under_review():
    tui = _make_tui()
    tasks = [_task(task_id="review-001", status="active",
                   current_attempt="att-001",
                   attempts=[_attempt()],
                   touches=["review"])]
    snap = tui._classify_tasks(tasks)
    assert len(snap.under_review) == 1
    assert len(snap.building) == 0


def test_classify_merge_ready():
    tui = _make_tui()
    att = _attempt(status="done", review_verdict={"result": "pass", "freshness": "fresh"})
    tasks = [_task(status="done", current_attempt="att-001", attempts=[att])]
    snap = tui._classify_tasks(tasks)
    assert len(snap.merge_ready) == 1


def test_classify_merge_blocked():
    tui = _make_tui()
    att = _attempt(status="done", merge_block_reason="conflict with task-002")
    tasks = [_task(status="done", current_attempt="att-001", attempts=[att])]
    snap = tui._classify_tasks(tasks)
    assert len(snap.merge_blocked) == 1


def test_classify_kicked_back():
    tui = _make_tui()
    att = _attempt(attempt_id="att-001", status="superseded")
    tasks = [_task(status="ready", attempts=[att])]
    snap = tui._classify_tasks(tasks)
    assert len(snap.kicked_back) == 1
    assert len(snap.backlog) == 0


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
        _task(task_id="review-task", status="active",
              current_attempt="att-r",
              attempts=[_attempt(attempt_id="att-r")],
              touches=["review"]),
        _task(task_id="build-task", status="active",
              current_attempt="att-b",
              attempts=[_attempt(attempt_id="att-b")]),
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


def test_get_merge_block_reason_found():
    tui = _make_tui()
    att = _attempt(merge_block_reason="deps not merged")
    t = _task(current_attempt="att-001", attempts=[att])
    assert tui._get_merge_block_reason(t) == "deps not merged"


def test_get_merge_block_reason_none():
    tui = _make_tui()
    att = _attempt()
    t = _task(current_attempt="att-001", attempts=[att])
    assert tui._get_merge_block_reason(t) is None


# ---------------------------------------------------------------------------
# Pipeline bar
# ---------------------------------------------------------------------------


def test_pipeline_bar_renders():
    tui = _make_tui()
    counts = {"building": 3, "backlog": 2, "merged": 5,
              "awaiting_review": 0, "under_review": 0,
              "merge_ready": 1, "merge_blocked": 0, "kicked_back": 0}
    result = tui._render_pipeline_bar(counts)
    assert isinstance(result, Text)
    assert len(str(result)) > 0


def test_pipeline_bar_empty():
    tui = _make_tui()
    counts = {"building": 0, "backlog": 0, "merged": 0,
              "awaiting_review": 0, "under_review": 0,
              "merge_ready": 0, "merge_blocked": 0, "kicked_back": 0}
    result = tui._render_pipeline_bar(counts)
    assert "no tasks" in str(result)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_summary_node_names():
    tui = _make_tui()
    workers = [
        {"worker_id": "mini/w1", "node_id": "mini", "agent_type": "claude-code"},
        {"worker_id": "mac/w2", "node_id": "mac", "agent_type": "claude-code"},
    ]
    snap = PipelineSnapshot()
    result = tui._render_summary({}, [], workers, snap, "unknown")
    assert isinstance(result, Table)


def test_summary_worker_types():
    tui = _make_tui()
    workers = [
        {"worker_id": "n1/b1", "node_id": "n1", "agent_type": "claude-code"},
        {"worker_id": "n1/r1", "node_id": "n1", "agent_type": "claude-code-review"},
    ]
    snap = PipelineSnapshot()
    result = tui._render_summary({}, [], workers, snap, "running")
    assert isinstance(result, Table)


def test_summary_soldier_status():
    tui = _make_tui()
    snap = PipelineSnapshot()
    result = tui._render_summary({"nodes": 1}, [], [], snap, "running")
    assert isinstance(result, Table)


def test_summary_review_pressure():
    tui = _make_tui()
    snap = PipelineSnapshot(
        awaiting_review=[_task(), _task(task_id="t2"), _task(task_id="t3"), _task(task_id="t4")]
    )
    result = tui._render_summary({}, [], [], snap, "idle")
    assert isinstance(result, Table)


# ---------------------------------------------------------------------------
# Render methods — empty + populated
# ---------------------------------------------------------------------------


def test_render_building_empty():
    tui = _make_tui()
    result = tui._render_building([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_building_populated():
    tui = _make_tui()
    t = _task(status="active", current_attempt="att-001",
              attempts=[_attempt()],
              trail=[{"ts": "2026-01-01T00:00:00", "worker_id": "w1", "message": "working"}])
    result = tui._render_building([t])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_backlog_empty():
    tui = _make_tui()
    result = tui._render_backlog([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_backlog_populated():
    tui = _make_tui()
    result = tui._render_backlog([_task(touches=["api", "db"])])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_awaiting_review_empty():
    tui = _make_tui()
    result = tui._render_awaiting_review([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_awaiting_review_populated():
    tui = _make_tui()
    result = tui._render_awaiting_review([_task()])
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
    att = _attempt(review_verdict={"result": "pass", "freshness": "fresh"})
    t = _task(status="done", current_attempt="att-001", attempts=[att])
    result = tui._render_merge_ready([t])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_merge_blocked_empty():
    tui = _make_tui()
    result = tui._render_merge_blocked([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_merge_blocked_populated():
    tui = _make_tui()
    att = _attempt(merge_block_reason="conflict")
    t = _task(status="done", current_attempt="att-001", attempts=[att])
    result = tui._render_merge_blocked([t])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_kicked_back_empty():
    tui = _make_tui()
    result = tui._render_kicked_back([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_kicked_back_populated():
    tui = _make_tui()
    t = _task(trail=[{"ts": "t", "worker_id": "w", "message": "tests failed"}])
    result = tui._render_kicked_back([t])
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
        att = _attempt(attempt_id=f"att-{i}", status="merged",
                       completed_at="2026-04-05T01:00:00+00:00")
        tasks.append(_task(task_id=f"task-{i}", status="done",
                           current_attempt=f"att-{i}", attempts=[att]))
    result = tui._render_recently_merged(tasks)
    assert isinstance(result, Table)
    # 5 shown + 1 "... +3 more" row
    assert result.row_count == 6


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


def test_render_workers_empty():
    tui = _make_tui()
    result = tui._render_workers([])
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_workers_type_column_builder():
    tui = _make_tui()
    workers = [{"worker_id": "n1/w1", "status": "idle", "node_id": "n1",
                "agent_type": "claude-code", "rate_limited": False}]
    result = tui._render_workers(workers)
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_render_workers_type_column_reviewer():
    tui = _make_tui()
    workers = [{"worker_id": "n1/r1", "status": "busy", "node_id": "n1",
                "agent_type": "claude-code-review", "rate_limited": False}]
    result = tui._render_workers(workers)
    assert isinstance(result, Table)
    assert result.row_count == 1


def test_get_worker_type_builder():
    tui = _make_tui()
    assert tui._get_worker_type({"agent_type": "claude-code"}) == "builder"


def test_get_worker_type_reviewer_by_agent_type():
    tui = _make_tui()
    assert tui._get_worker_type({"agent_type": "claude-code-review"}) == "reviewer"


def test_get_worker_type_reviewer_by_capability():
    tui = _make_tui()
    assert tui._get_worker_type({"agent_type": "claude-code",
                                  "capabilities": ["review"]}) == "reviewer"
