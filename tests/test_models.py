"""Tests for antfarm.core.models dataclasses and enums."""

from antfarm.core.models import (
    Attempt,
    AttemptState,
    AttemptStatus,
    FailureRecord,
    FailureType,
    Node,
    ReviewVerdict,
    SignalEntry,
    Task,
    TaskArtifact,
    TaskState,
    TaskStatus,
    TrailEntry,
    Worker,
    WorkerStatus,
)


def _make_attempt(attempt_id: str = "a-1") -> Attempt:
    return Attempt(
        attempt_id=attempt_id,
        worker_id="w-1",
        status=AttemptStatus.ACTIVE,
        branch="feat/test",
        pr="https://github.com/org/repo/pull/1",
        started_at="2026-04-04T10:00:00Z",
        completed_at=None,
    )


def _make_trail_entry() -> TrailEntry:
    return TrailEntry(ts="2026-04-04T10:00:00Z", worker_id="w-1", message="started work")


def _make_signal_entry() -> SignalEntry:
    return SignalEntry(ts="2026-04-04T10:01:00Z", worker_id="w-1", message="needs review")


def _make_task(include_nested: bool = True) -> Task:
    attempts = [_make_attempt("a-1"), _make_attempt("a-2")] if include_nested else []
    trail = [_make_trail_entry()] if include_nested else []
    signals = [_make_signal_entry()] if include_nested else []
    return Task(
        id="t-1",
        title="Add login flow",
        spec="Implement JWT-based login",
        complexity="L",
        priority=5,
        depends_on=["t-0"],
        touches=["auth.py", "middleware.py"],
        status=TaskStatus.ACTIVE,
        current_attempt="a-2",
        attempts=attempts,
        trail=trail,
        signals=signals,
        created_at="2026-04-04T09:00:00Z",
        updated_at="2026-04-04T10:00:00Z",
        created_by="user-1",
    )


def _make_worker() -> Worker:
    return Worker(
        worker_id="w-1",
        node_id="node-1",
        agent_type="engineer",
        workspace_root="/tmp/ws",
        status=WorkerStatus.ACTIVE,
        registered_at="2026-04-04T08:00:00Z",
        last_heartbeat="2026-04-04T10:00:00Z",
    )


def _make_node() -> Node:
    return Node(
        node_id="node-1",
        joined_at="2026-04-04T08:00:00Z",
        last_seen="2026-04-04T10:00:00Z",
    )


# ---------------------------------------------------------------------------
# Roundtrip tests
# ---------------------------------------------------------------------------


def test_trail_entry_roundtrip():
    entry = _make_trail_entry()
    assert TrailEntry.from_dict(entry.to_dict()) == entry


def test_signal_entry_roundtrip():
    entry = _make_signal_entry()
    assert SignalEntry.from_dict(entry.to_dict()) == entry


def test_attempt_roundtrip():
    attempt = _make_attempt()
    assert Attempt.from_dict(attempt.to_dict()) == attempt


def test_attempt_roundtrip_nulls():
    attempt = Attempt(
        attempt_id="a-null",
        worker_id=None,
        status=AttemptStatus.SUPERSEDED,
        branch=None,
        pr=None,
        started_at="2026-04-04T10:00:00Z",
        completed_at="2026-04-04T11:00:00Z",
    )
    assert Attempt.from_dict(attempt.to_dict()) == attempt


def test_task_roundtrip():
    task = _make_task(include_nested=True)
    assert Task.from_dict(task.to_dict()) == task


def test_worker_roundtrip():
    worker = _make_worker()
    assert Worker.from_dict(worker.to_dict()) == worker


def test_node_roundtrip():
    node = _make_node()
    assert Node.from_dict(node.to_dict()) == node


# ---------------------------------------------------------------------------
# Enum value tests
# ---------------------------------------------------------------------------


def test_enum_values():
    assert TaskStatus.READY.value == "ready"
    assert TaskStatus.ACTIVE.value == "active"
    assert TaskStatus.DONE.value == "done"

    assert AttemptStatus.ACTIVE.value == "active"
    assert AttemptStatus.DONE.value == "done"
    assert AttemptStatus.MERGED.value == "merged"
    assert AttemptStatus.SUPERSEDED.value == "superseded"

    assert WorkerStatus.IDLE.value == "idle"
    assert WorkerStatus.ACTIVE.value == "active"
    assert WorkerStatus.OFFLINE.value == "offline"


def test_enum_is_str():
    assert isinstance(TaskStatus.READY, str)
    assert isinstance(AttemptStatus.MERGED, str)
    assert isinstance(WorkerStatus.IDLE, str)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


def test_task_default_values():
    task = Task(
        id="t-min",
        title="Minimal task",
        spec="Do something",
        created_at="2026-04-04T09:00:00Z",
        updated_at="2026-04-04T09:00:00Z",
        created_by="user-1",
    )
    assert task.complexity == "M"
    assert task.priority == 10
    assert task.depends_on == []
    assert task.touches == []
    assert task.status == TaskStatus.READY
    assert task.current_attempt is None
    assert task.attempts == []
    assert task.trail == []
    assert task.signals == []


def test_worker_default_status():
    worker = Worker(
        worker_id="w-default",
        node_id="node-1",
        agent_type="researcher",
        workspace_root="/tmp/ws",
        registered_at="2026-04-04T08:00:00Z",
        last_heartbeat="2026-04-04T09:00:00Z",
    )
    assert worker.status == WorkerStatus.IDLE


# ---------------------------------------------------------------------------
# Nested serialization
# ---------------------------------------------------------------------------


def test_task_with_nested_attempts():
    task = _make_task(include_nested=True)
    d = task.to_dict()

    assert len(d["attempts"]) == 2
    assert d["attempts"][0]["attempt_id"] == "a-1"
    assert d["attempts"][1]["attempt_id"] == "a-2"
    assert d["attempts"][0]["status"] == "active"

    assert len(d["trail"]) == 1
    assert d["trail"][0]["message"] == "started work"

    assert len(d["signals"]) == 1
    assert d["signals"][0]["message"] == "needs review"

    # Verify full roundtrip still holds
    assert Task.from_dict(d) == task


def test_enum_serializes_as_value():
    task = Task(
        id="t-ser",
        title="Serialization test",
        spec="Check enum values in dict",
        status=TaskStatus.DONE,
        created_at="2026-04-04T09:00:00Z",
        updated_at="2026-04-04T09:00:00Z",
        created_by="user-1",
    )
    d = task.to_dict()
    assert d["status"] == "done"
    assert isinstance(d["status"], str)


def test_attempt_merged_status_roundtrip():
    attempt = Attempt(
        attempt_id="a-merged",
        worker_id="w-1",
        status=AttemptStatus.MERGED,
        branch="feat/done",
        pr="https://github.com/org/repo/pull/42",
        started_at="2026-04-04T10:00:00Z",
        completed_at="2026-04-04T12:00:00Z",
    )
    assert Attempt.from_dict(attempt.to_dict()) == attempt
    assert attempt.to_dict()["status"] == "merged"


# ---------------------------------------------------------------------------
# v0.5 enriched types
# ---------------------------------------------------------------------------


def test_task_state_values():
    assert TaskState.QUEUED.value == "queued"
    assert TaskState.HARVEST_PENDING.value == "harvest_pending"
    assert TaskState.MERGE_READY.value == "merge_ready"
    assert TaskState.KICKED_BACK.value == "kicked_back"
    assert isinstance(TaskState.QUEUED, str)


def test_attempt_state_values():
    assert AttemptState.STARTED.value == "started"
    assert AttemptState.HEARTBEATING.value == "heartbeating"
    assert AttemptState.STALE.value == "stale"
    assert AttemptState.ABANDONED.value == "abandoned"
    assert isinstance(AttemptState.STARTED, str)


def test_failure_type_values():
    assert FailureType.AGENT_CRASH.value == "agent_crash"
    assert FailureType.INVALID_TASK.value == "invalid_task"
    assert FailureType.TEST_FAILURE.value == "test_failure"
    assert isinstance(FailureType.AGENT_CRASH, str)


def test_failure_record_roundtrip():
    rec = FailureRecord(
        task_id="task-001",
        attempt_id="att-001",
        worker_id="w1",
        failure_type=FailureType.TEST_FAILURE,
        message="test_auth failed",
        retryable=False,
        captured_at="2026-04-05T10:00:00Z",
        stderr_summary="AssertionError: expected 200 got 401",
        recommended_action="kickback",
    )
    d = rec.to_dict()
    assert d["failure_type"] == "test_failure"
    assert d["retryable"] is False

    restored = FailureRecord.from_dict(d)
    assert restored.failure_type == FailureType.TEST_FAILURE
    assert restored.retryable is False
    assert restored.message == "test_auth failed"
    assert restored == rec


def test_failure_record_with_verification_snapshot():
    rec = FailureRecord(
        task_id="task-002",
        attempt_id="att-002",
        worker_id="w2",
        failure_type=FailureType.INFRA_FAILURE,
        message="connection refused",
        retryable=True,
        captured_at="2026-04-05T10:00:00Z",
        stderr_summary="ConnectionRefusedError",
        verification_snapshot={"tests_ran": False, "lint_ran": False},
        recommended_action="retry",
    )
    d = rec.to_dict()
    restored = FailureRecord.from_dict(d)
    assert restored.verification_snapshot == {"tests_ran": False, "lint_ran": False}
    assert restored.recommended_action == "retry"


def test_task_artifact_roundtrip():
    artifact = TaskArtifact(
        task_id="t1",
        attempt_id="a1",
        worker_id="w1",
        branch="feat/t1",
        pr_url="https://github.com/org/repo/pull/1",
        base_commit_sha="abc123",
        head_commit_sha="def456",
        target_branch="dev",
        target_branch_sha_at_harvest="aaa111",
        files_changed=["src/foo.py", "tests/test_foo.py"],
        lines_added=50,
        lines_removed=10,
        tests_ran=True,
        tests_passed=True,
        lint_ran=True,
        lint_passed=True,
        merge_readiness="ready",
        summary="Added foo feature",
        risks=["might break bar"],
        review_focus=["check foo.py line 42"],
    )
    d = artifact.to_dict()
    restored = TaskArtifact.from_dict(d)
    assert restored.task_id == "t1"
    assert restored.files_changed == ["src/foo.py", "tests/test_foo.py"]
    assert restored.merge_readiness == "ready"
    assert restored.risks == ["might break bar"]
    assert restored == artifact


def test_task_artifact_defaults():
    artifact = TaskArtifact(
        task_id="t1",
        attempt_id="a1",
        worker_id="w1",
        branch="feat/t1",
        pr_url=None,
        base_commit_sha="abc",
        head_commit_sha="def",
        target_branch="dev",
        target_branch_sha_at_harvest="aaa",
    )
    assert artifact.files_changed == []
    assert artifact.lines_added == 0
    assert artifact.merge_readiness == "needs_review"
    assert artifact.summary is None
    assert artifact.risks == []


def test_review_verdict_roundtrip():
    verdict = ReviewVerdict(
        provider="claude_code",
        verdict="pass",
        summary="LGTM",
        findings=["minor: could add docstring"],
        severity="low",
        reviewed_commit_sha="def456",
        reviewer_run_id="run-1",
    )
    d = verdict.to_dict()
    restored = ReviewVerdict.from_dict(d)
    assert restored.verdict == "pass"
    assert restored.provider == "claude_code"
    assert restored.findings == ["minor: could add docstring"]
    assert restored == verdict


def test_review_verdict_defaults():
    verdict = ReviewVerdict(
        provider="human",
        verdict="needs_changes",
        summary="Fix the bug",
    )
    assert verdict.findings == []
    assert verdict.severity is None
    assert verdict.reviewed_commit_sha == ""
    assert verdict.reviewer_run_id is None


def test_failure_record_defaults():
    rec = FailureRecord(
        task_id="t1",
        attempt_id="a1",
        worker_id="w1",
        failure_type=FailureType.AGENT_CRASH,
        message="crashed",
        retryable=True,
        captured_at="2026-04-05T10:00:00Z",
        stderr_summary="Segfault",
    )
    assert rec.verification_snapshot == {}
    assert rec.recommended_action == "kickback"


# ---------------------------------------------------------------------------
# Task.mission_id (v0.6)
# ---------------------------------------------------------------------------


def test_task_mission_id_roundtrip():
    task = Task(
        id="t-mission",
        title="Mission task",
        spec="Do stuff",
        created_at="2026-04-09T09:00:00Z",
        updated_at="2026-04-09T09:00:00Z",
        created_by="user-1",
        mission_id="mission-login-123",
    )
    d = task.to_dict()
    assert d["mission_id"] == "mission-login-123"
    restored = Task.from_dict(d)
    assert restored.mission_id == "mission-login-123"
    assert restored == task


def test_node_roundtrip_extended():
    node = Node(
        node_id="node-1",
        joined_at="2026-04-04T08:00:00Z",
        last_seen="2026-04-04T10:00:00Z",
        runner_url="http://localhost:7433",
        max_workers=8,
        capabilities=["claude-code", "codex"],
    )
    d = node.to_dict()
    assert d["runner_url"] == "http://localhost:7433"
    assert d["max_workers"] == 8
    assert d["capabilities"] == ["claude-code", "codex"]
    restored = Node.from_dict(d)
    assert restored == node


def test_node_backward_compat():
    data = {
        "node_id": "node-old",
        "joined_at": "2026-04-04T08:00:00Z",
        "last_seen": "2026-04-04T10:00:00Z",
    }
    node = Node.from_dict(data)
    assert node.runner_url is None
    assert node.max_workers == 4
    assert node.capabilities == []


def test_task_mission_id_default_none():
    task = Task(
        id="t-no-mission",
        title="No mission",
        spec="Solo task",
        created_at="2026-04-09T09:00:00Z",
        updated_at="2026-04-09T09:00:00Z",
        created_by="user-1",
    )
    assert task.mission_id is None
    d = task.to_dict()
    assert d["mission_id"] is None
    # Backward compat: from_dict without mission_id key
    del d["mission_id"]
    restored = Task.from_dict(d)
    assert restored.mission_id is None
