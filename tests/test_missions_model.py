"""Tests for antfarm.core.missions dataclasses and helpers."""

import pytest

from antfarm.core.missions import (
    Mission,
    MissionConfig,
    MissionReport,
    MissionReportBlocked,
    MissionReportTask,
    MissionStatus,
    PlanArtifact,
    is_infra_task,
)


def _make_mission_config() -> MissionConfig:
    return MissionConfig(
        max_attempts=5,
        max_parallel_builders=2,
        require_plan_review=False,
        stall_threshold_minutes=60,
        completion_mode="all_or_nothing",
        test_command=["pytest", "-x"],
        integration_branch="dev",
        blocked_timeout_action="fail",
        blocked_timeout_minutes=60,
    )


def _make_plan_artifact() -> PlanArtifact:
    return PlanArtifact(
        plan_task_id="plan-001",
        attempt_id="att-001",
        proposed_tasks=[{"id": "task-1", "title": "Do stuff"}],
        task_count=1,
        warnings=["might be slow"],
        dependency_summary="task-1 has no deps",
    )


def _make_mission_report_task() -> MissionReportTask:
    return MissionReportTask(
        task_id="task-1",
        title="Add login",
        pr_url="https://github.com/org/repo/pull/1",
        lines_added=50,
        lines_removed=10,
        files_changed=["src/auth.py"],
    )


def _make_mission_report_blocked() -> MissionReportBlocked:
    return MissionReportBlocked(
        task_id="task-2",
        title="Fix bug",
        reason="merge conflict",
        attempt_count=3,
        last_failure_type="merge_conflict",
    )


def _make_mission_report() -> MissionReport:
    return MissionReport(
        mission_id="mission-login-123",
        spec_summary="Add login feature",
        status=MissionStatus.COMPLETE,
        completion_mode="best_effort",
        duration_minutes=45.5,
        total_tasks=3,
        merged_tasks=2,
        blocked_tasks=1,
        failed_reviews=0,
        merged=[_make_mission_report_task()],
        blocked=[_make_mission_report_blocked()],
        risks=["auth might break"],
        pr_urls=["https://github.com/org/repo/pull/1"],
        branches=["feat/task-1"],
        total_lines_added=50,
        total_lines_removed=10,
        files_changed=["src/auth.py"],
        generated_at="2026-04-09T10:00:00Z",
    )


def _make_mission() -> Mission:
    return Mission(
        mission_id="mission-login-123",
        spec="Build a login flow",
        spec_file="specs/login.md",
        status=MissionStatus.BUILDING,
        plan_task_id="plan-001",
        plan_artifact=_make_plan_artifact(),
        task_ids=["task-1", "task-2"],
        blocked_task_ids=["task-2"],
        config=_make_mission_config(),
        created_at="2026-04-09T09:00:00Z",
        updated_at="2026-04-09T10:00:00Z",
        completed_at=None,
        report=None,
        last_progress_at="2026-04-09T09:30:00Z",
        re_plan_count=0,
    )


# ---------------------------------------------------------------------------
# Roundtrip tests
# ---------------------------------------------------------------------------


def test_mission_roundtrip():
    mission = _make_mission()
    d = mission.to_dict()
    restored = Mission.from_dict(d)
    assert restored.mission_id == mission.mission_id
    assert restored.spec == mission.spec
    assert restored.spec_file == mission.spec_file
    assert restored.status == mission.status
    assert restored.plan_task_id == mission.plan_task_id
    assert restored.plan_artifact == mission.plan_artifact
    assert restored.task_ids == mission.task_ids
    assert restored.blocked_task_ids == mission.blocked_task_ids
    assert restored.config == mission.config
    assert restored.created_at == mission.created_at
    assert restored.updated_at == mission.updated_at
    assert restored.completed_at == mission.completed_at
    assert restored.report == mission.report
    assert restored.last_progress_at == mission.last_progress_at
    assert restored.re_plan_count == mission.re_plan_count
    assert restored == mission


def test_mission_config_defaults():
    config = MissionConfig()
    assert config.max_attempts == 3
    assert config.max_parallel_builders == 4
    assert config.require_plan_review is True
    assert config.stall_threshold_minutes == 30
    assert config.completion_mode == "best_effort"
    assert config.test_command is None
    assert config.integration_branch == "main"
    assert config.blocked_timeout_action == "wait"
    assert config.blocked_timeout_minutes == 120


def test_mission_config_rejects_invalid_completion_mode():
    with pytest.raises(ValueError, match="completion_mode must be one of"):
        MissionConfig(completion_mode="invalid")


def test_mission_config_rejects_invalid_blocked_timeout_action():
    with pytest.raises(ValueError, match="blocked_timeout_action must be one of"):
        MissionConfig(blocked_timeout_action="invalid")


def test_mission_config_accepts_all_or_nothing():
    config = MissionConfig(completion_mode="all_or_nothing")
    assert config.completion_mode == "all_or_nothing"
    d = config.to_dict()
    restored = MissionConfig.from_dict(d)
    assert restored.completion_mode == "all_or_nothing"
    assert restored == config


def test_plan_artifact_roundtrip():
    artifact = _make_plan_artifact()
    d = artifact.to_dict()
    restored = PlanArtifact.from_dict(d)
    assert restored == artifact


def test_mission_report_roundtrip():
    report = _make_mission_report()
    d = report.to_dict()
    assert d["status"] == "complete"
    restored = MissionReport.from_dict(d)
    assert restored == report


def test_mission_report_task_roundtrip():
    task = _make_mission_report_task()
    d = task.to_dict()
    restored = MissionReportTask.from_dict(d)
    assert restored == task


def test_mission_report_blocked_roundtrip():
    blocked = _make_mission_report_blocked()
    d = blocked.to_dict()
    restored = MissionReportBlocked.from_dict(d)
    assert restored == blocked


def test_mission_status_enum_values():
    assert MissionStatus.PLANNING.value == "planning"
    assert MissionStatus.REVIEWING_PLAN.value == "reviewing_plan"
    assert MissionStatus.BUILDING.value == "building"
    assert MissionStatus.BLOCKED.value == "blocked"
    assert MissionStatus.COMPLETE.value == "complete"
    assert MissionStatus.FAILED.value == "failed"
    assert MissionStatus.CANCELLED.value == "cancelled"
    assert isinstance(MissionStatus.PLANNING, str)


# ---------------------------------------------------------------------------
# is_infra_task
# ---------------------------------------------------------------------------


def test_is_infra_task_plan():
    assert is_infra_task({"id": "plan-001", "capabilities_required": ["plan"]}) is True
    assert is_infra_task({"id": "task-001", "capabilities_required": ["review"]}) is True


def test_is_infra_task_review_prefix():
    assert is_infra_task({"id": "review-task-001", "capabilities_required": []}) is True


def test_is_infra_task_builder():
    assert is_infra_task({"id": "task-001", "capabilities_required": ["build"]}) is False
    assert is_infra_task({"id": "task-001"}) is False
