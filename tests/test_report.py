"""Tests for antfarm.core.report — mission report generator and renderers."""

from __future__ import annotations

import json
import sys

import pytest

from antfarm.core.missions import (
    MissionReport,
    MissionReportBlocked,
    MissionReportTask,
    MissionStatus,
)
from antfarm.core.report import (
    build_report,
    render_json,
    render_markdown,
    render_terminal,
    save_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mission(
    mission_id: str = "mission-001",
    status: str = "complete",
    task_ids: list[str] | None = None,
    spec: str = "Build the widget",
    created_at: str = "2026-04-01T00:00:00Z",
    completed_at: str | None = "2026-04-01T01:30:00Z",
    completion_mode: str = "best_effort",
) -> dict:
    return {
        "mission_id": mission_id,
        "spec": spec,
        "status": status,
        "task_ids": task_ids or [],
        "config": {"completion_mode": completion_mode},
        "created_at": created_at,
        "completed_at": completed_at,
        "updated_at": completed_at or created_at,
        "blocked_task_ids": [],
    }


def _make_task(
    task_id: str,
    title: str = "Do something",
    status: str = "done",
    attempts: list[dict] | None = None,
    trail: list[dict] | None = None,
    capabilities_required: list[str] | None = None,
) -> dict:
    return {
        "id": task_id,
        "title": title,
        "status": status,
        "attempts": attempts or [],
        "trail": trail or [],
        "capabilities_required": capabilities_required or [],
    }


def _make_attempt(
    attempt_id: str = "att-001",
    status: str = "merged",
    branch: str | None = "feat/task-001",
    pr: str | None = None,
    artifact: dict | None = None,
    review_verdict: dict | None = None,
) -> dict:
    d: dict = {
        "attempt_id": attempt_id,
        "worker_id": "node-1/claude-1",
        "status": status,
        "branch": branch,
        "pr": pr,
        "started_at": "2026-04-01T00:00:00Z",
        "completed_at": "2026-04-01T00:30:00Z",
    }
    if artifact is not None:
        d["artifact"] = artifact
    if review_verdict is not None:
        d["review_verdict"] = review_verdict
    return d


def _make_artifact(
    pr_url: str | None = "https://github.com/org/repo/pull/1",
    lines_added: int = 100,
    lines_removed: int = 20,
    files_changed: list[str] | None = None,
    risks: list[str] | None = None,
) -> dict:
    return {
        "pr_url": pr_url,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "files_changed": files_changed or ["src/main.py"],
        "risks": risks or [],
    }


# ---------------------------------------------------------------------------
# 1. test_build_report_empty_mission
# ---------------------------------------------------------------------------


def test_build_report_empty_mission():
    mission = _make_mission(task_ids=[])
    report = build_report(mission, [])
    assert report.mission_id == "mission-001"
    assert report.total_tasks == 0
    assert report.merged_tasks == 0
    assert report.blocked_tasks == 0
    assert report.merged == []
    assert report.blocked == []


# ---------------------------------------------------------------------------
# 2. test_build_report_all_merged
# ---------------------------------------------------------------------------


def test_build_report_all_merged():
    tasks = [
        _make_task(
            "task-001",
            title="Widget A",
            attempts=[_make_attempt(artifact=_make_artifact(lines_added=50, lines_removed=10))],
        ),
        _make_task(
            "task-002",
            title="Widget B",
            attempts=[_make_attempt(
                attempt_id="att-002",
                artifact=_make_artifact(
                    pr_url="https://github.com/org/repo/pull/2",
                    lines_added=30,
                    lines_removed=5,
                    files_changed=["src/other.py"],
                ),
            )],
        ),
    ]
    mission = _make_mission(task_ids=["task-001", "task-002"])
    report = build_report(mission, tasks)
    assert report.total_tasks == 2
    assert report.merged_tasks == 2
    assert report.blocked_tasks == 0
    assert len(report.merged) == 2
    assert report.total_lines_added == 80
    assert report.total_lines_removed == 15


# ---------------------------------------------------------------------------
# 3. test_build_report_mixed_merged_blocked
# ---------------------------------------------------------------------------


def test_build_report_mixed_merged_blocked():
    tasks = [
        _make_task(
            "task-001",
            title="Done task",
            attempts=[_make_attempt(artifact=_make_artifact())],
        ),
        _make_task(
            "task-002",
            title="Stuck task",
            status="blocked",
            attempts=[
                _make_attempt(attempt_id="att-002", status="superseded"),
                _make_attempt(attempt_id="att-003", status="superseded"),
            ],
            trail=[{"ts": "2026-04-01T01:00:00Z", "worker_id": "w", "message": "system: OOM"}],
        ),
    ]
    mission = _make_mission(task_ids=["task-001", "task-002"])
    report = build_report(mission, tasks)
    assert report.merged_tasks == 1
    assert report.blocked_tasks == 1
    assert report.blocked[0].reason == "system: OOM"
    assert report.blocked[0].attempt_count == 2


# ---------------------------------------------------------------------------
# 4. test_build_report_skips_plan_and_review_tasks_from_total
# ---------------------------------------------------------------------------


def test_build_report_skips_plan_and_review_tasks_from_total():
    tasks = [
        _make_task("task-001", title="Impl task", capabilities_required=[]),
        _make_task("plan-001", title="Plan task", capabilities_required=["plan"]),
        _make_task("review-plan-001", title="Review task", capabilities_required=["review"]),
    ]
    mission = _make_mission(task_ids=["task-001", "plan-001", "review-plan-001"])
    report = build_report(mission, tasks)
    # Only task-001 is an impl task
    assert report.total_tasks == 1


# ---------------------------------------------------------------------------
# 5. test_build_report_aggregates_lines_added_removed
# ---------------------------------------------------------------------------


def test_build_report_aggregates_lines_added_removed():
    tasks = [
        _make_task(
            "task-001",
            attempts=[_make_attempt(artifact=_make_artifact(lines_added=100, lines_removed=20))],
        ),
        _make_task(
            "task-002",
            attempts=[_make_attempt(
                attempt_id="att-002",
                artifact=_make_artifact(lines_added=200, lines_removed=50),
            )],
        ),
    ]
    mission = _make_mission(task_ids=["task-001", "task-002"])
    report = build_report(mission, tasks)
    assert report.total_lines_added == 300
    assert report.total_lines_removed == 70


# ---------------------------------------------------------------------------
# 6. test_build_report_collects_pr_urls_from_artifacts
# ---------------------------------------------------------------------------


def test_build_report_collects_pr_urls_from_artifacts():
    tasks = [
        _make_task(
            "task-001",
            attempts=[_make_attempt(artifact=_make_artifact(
                pr_url="https://github.com/org/repo/pull/1",
            ))],
        ),
        _make_task(
            "task-002",
            attempts=[_make_attempt(
                attempt_id="att-002",
                artifact=_make_artifact(pr_url="https://github.com/org/repo/pull/2"),
            )],
        ),
    ]
    mission = _make_mission(task_ids=["task-001", "task-002"])
    report = build_report(mission, tasks)
    assert len(report.pr_urls) == 2
    assert "https://github.com/org/repo/pull/1" in report.pr_urls
    assert "https://github.com/org/repo/pull/2" in report.pr_urls


# ---------------------------------------------------------------------------
# 7. test_build_report_blocked_pulls_reason_from_trail
# ---------------------------------------------------------------------------


def test_build_report_blocked_pulls_reason_from_trail():
    tasks = [
        _make_task(
            "task-001",
            title="Broken task",
            status="blocked",
            attempts=[_make_attempt(attempt_id="att-001", status="superseded")],
            trail=[
                {"ts": "t1", "worker_id": "w", "message": "started work"},
                {"ts": "t2", "worker_id": "w", "message": "review: code does not meet standards"},
            ],
        ),
    ]
    mission = _make_mission(task_ids=["task-001"])
    report = build_report(mission, tasks)
    assert report.blocked_tasks == 1
    assert report.blocked[0].reason == "review: code does not meet standards"


# ---------------------------------------------------------------------------
# 8. test_render_terminal_smoke
# ---------------------------------------------------------------------------


def test_render_terminal_smoke():
    report = MissionReport(
        mission_id="mission-001",
        spec_summary="Build widget",
        status=MissionStatus.COMPLETE,
        completion_mode="best_effort",
        duration_minutes=90.0,
        total_tasks=3,
        merged_tasks=2,
        blocked_tasks=1,
        failed_reviews=0,
        merged=[
            MissionReportTask("t-1", "Task 1", "https://github.com/pr/1", 100, 10, ["a.py"]),
        ],
        blocked=[
            MissionReportBlocked("t-2", "Task 2", "system: OOM", 3, "agent_crash"),
        ],
        generated_at="2026-04-01T01:30:00Z",
    )
    output = render_terminal(report)
    assert "mission-001" in output
    assert "90.0 minutes" in output
    assert "best_effort" in output


# ---------------------------------------------------------------------------
# 9. test_render_terminal_distinguishes_system_vs_review_prefix
# ---------------------------------------------------------------------------


def test_render_terminal_distinguishes_system_vs_review_prefix():
    report = MissionReport(
        mission_id="m-1",
        spec_summary="",
        status=MissionStatus.COMPLETE,
        completion_mode="best_effort",
        duration_minutes=10.0,
        total_tasks=2,
        merged_tasks=0,
        blocked_tasks=2,
        failed_reviews=0,
        blocked=[
            MissionReportBlocked("t-1", "Task 1", "system: worker crashed", 2, None),
            MissionReportBlocked("t-2", "Task 2", "review: code quality too low", 1, None),
        ],
        generated_at="2026-04-01T00:10:00Z",
    )
    output = render_terminal(report)
    assert "[SYSTEM]" in output
    assert "[REVIEW]" in output


# ---------------------------------------------------------------------------
# 10. test_render_terminal_use_rich_raises_not_implemented
# ---------------------------------------------------------------------------


def test_render_terminal_use_rich_raises_not_implemented():
    report = MissionReport(
        mission_id="m-1",
        spec_summary="",
        status=MissionStatus.COMPLETE,
        completion_mode="best_effort",
        duration_minutes=0,
        total_tasks=0,
        merged_tasks=0,
        blocked_tasks=0,
        failed_reviews=0,
        generated_at="",
    )
    with pytest.raises(NotImplementedError, match="rich rendering"):
        render_terminal(report, use_rich=True)


# ---------------------------------------------------------------------------
# 11. test_render_terminal_no_rich_import
# ---------------------------------------------------------------------------


def test_render_terminal_no_rich_import():
    """Importing antfarm.core.report must not pull in rich."""
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import antfarm.core.report; import sys; "
            "sys.exit(1 if 'rich' in sys.modules else 0)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "rich was imported as a side-effect of importing antfarm.core.report"
    )


# ---------------------------------------------------------------------------
# 12. test_render_markdown_smoke
# ---------------------------------------------------------------------------


def test_render_markdown_smoke():
    report = MissionReport(
        mission_id="mission-001",
        spec_summary="Build widget",
        status=MissionStatus.COMPLETE,
        completion_mode="best_effort",
        duration_minutes=90.0,
        total_tasks=2,
        merged_tasks=2,
        blocked_tasks=0,
        failed_reviews=0,
        merged=[
            MissionReportTask("t-1", "Task 1", "https://github.com/pr/1", 100, 10, ["a.py"]),
            MissionReportTask("t-2", "Task 2", "https://github.com/pr/2", 50, 5, ["b.py"]),
        ],
        pr_urls=["https://github.com/pr/1", "https://github.com/pr/2"],
        generated_at="2026-04-01T01:30:00Z",
    )
    output = render_markdown(report)
    assert "# Mission Report" in output
    assert "https://github.com/pr/1" in output
    assert "https://github.com/pr/2" in output


# ---------------------------------------------------------------------------
# 13. test_render_markdown_distinguishes_system_vs_review_prefix
# ---------------------------------------------------------------------------


def test_render_markdown_distinguishes_system_vs_review_prefix():
    report = MissionReport(
        mission_id="m-1",
        spec_summary="",
        status=MissionStatus.COMPLETE,
        completion_mode="best_effort",
        duration_minutes=10.0,
        total_tasks=2,
        merged_tasks=0,
        blocked_tasks=2,
        failed_reviews=0,
        blocked=[
            MissionReportBlocked("t-1", "Task 1", "system: worker crashed", 2, None),
            MissionReportBlocked("t-2", "Task 2", "review: code quality too low", 1, None),
        ],
        generated_at="2026-04-01T00:10:00Z",
    )
    output = render_markdown(report)
    assert "`[SYSTEM]`" in output
    assert "`[REVIEW]`" in output


# ---------------------------------------------------------------------------
# 14. test_save_report_writes_json_file
# ---------------------------------------------------------------------------


def test_save_report_writes_json_file(tmp_path):
    report = MissionReport(
        mission_id="mission-001",
        spec_summary="test",
        status=MissionStatus.COMPLETE,
        completion_mode="best_effort",
        duration_minutes=5.0,
        total_tasks=1,
        merged_tasks=1,
        blocked_tasks=0,
        failed_reviews=0,
        generated_at="2026-04-01T00:05:00Z",
    )
    path = save_report(str(tmp_path), "mission-001", report)
    assert (tmp_path / "missions" / "mission-001_report.json").exists()
    data = json.loads((tmp_path / "missions" / "mission-001_report.json").read_text())
    assert data["mission_id"] == "mission-001"
    assert data["completion_mode"] == "best_effort"
    assert path == str(tmp_path / "missions" / "mission-001_report.json")


# ---------------------------------------------------------------------------
# 15. test_cancelled_mission_report_includes_completed_tasks
# ---------------------------------------------------------------------------


def test_cancelled_mission_report_includes_completed_tasks():
    tasks = [
        _make_task(
            f"task-{i:03d}",
            title=f"Task {i}",
            attempts=[_make_attempt(
                attempt_id=f"att-{i:03d}",
                artifact=_make_artifact(pr_url=f"https://github.com/pr/{i}"),
            )],
        )
        for i in range(1, 4)  # 3 merged
    ] + [
        _make_task(f"task-{i:03d}", title=f"Task {i}", status="ready")
        for i in range(4, 6)  # 2 not yet done
    ]
    mission = _make_mission(
        task_ids=[f"task-{i:03d}" for i in range(1, 6)],
        status="cancelled",
    )
    report = build_report(mission, tasks)
    assert report.status == MissionStatus.CANCELLED
    assert report.merged_tasks == 3
    assert report.total_tasks == 5

    # Terminal rendering should show "Completed before cancellation"
    terminal_output = render_terminal(report)
    assert "Completed before cancellation" in terminal_output
    assert "task-001" in terminal_output

    # Markdown rendering should show the same
    md_output = render_markdown(report)
    assert "Completed before cancellation" in md_output


# ---------------------------------------------------------------------------
# 16. test_report_includes_completion_mode
# ---------------------------------------------------------------------------


def test_report_includes_completion_mode():
    tasks = [
        _make_task(
            "task-001",
            attempts=[_make_attempt(artifact=_make_artifact())],
        ),
    ]
    mission = _make_mission(
        task_ids=["task-001"],
        completion_mode="all_or_nothing",
    )
    report = build_report(mission, tasks)
    assert report.completion_mode == "all_or_nothing"

    # JSON output includes completion_mode
    json_output = render_json(report)
    data = json.loads(json_output)
    assert data["completion_mode"] == "all_or_nothing"

    # Terminal output includes completion_mode and warning
    terminal_output = render_terminal(report)
    assert "all_or_nothing" in terminal_output
    assert "WARNING" in terminal_output


# ---------------------------------------------------------------------------
# Extra: test_report_distinguishes_system_vs_review_prefix (build_report level)
# ---------------------------------------------------------------------------


def test_report_distinguishes_system_vs_review_prefix():
    """Verify build_report preserves system: and review: prefixes in reasons."""
    tasks = [
        _make_task(
            "task-001",
            status="blocked",
            trail=[{"ts": "t1", "worker_id": "w", "message": "system: OOM killed"}],
            attempts=[_make_attempt(status="superseded")],
        ),
        _make_task(
            "task-002",
            status="blocked",
            trail=[{"ts": "t1", "worker_id": "w", "message": "review: rejected by reviewer"}],
            attempts=[_make_attempt(attempt_id="att-002", status="superseded")],
        ),
    ]
    mission = _make_mission(task_ids=["task-001", "task-002"])
    report = build_report(mission, tasks)
    reasons = [b.reason for b in report.blocked]
    assert any(r.startswith("system: ") for r in reasons)
    assert any(r.startswith("review: ") for r in reasons)
