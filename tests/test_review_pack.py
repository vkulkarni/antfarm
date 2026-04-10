"""Tests for antfarm.core.review_pack generation."""

from antfarm.core.models import TaskArtifact
from antfarm.core.review_pack import extract_verdict_from_review_task, generate_review_pack


def _make_artifact(**overrides) -> TaskArtifact:
    defaults = dict(
        task_id="task-001",
        attempt_id="att-001",
        worker_id="w1",
        branch="feat/task-001",
        pr_url="https://github.com/org/repo/pull/1",
        base_commit_sha="abc123",
        head_commit_sha="def456",
        target_branch="dev",
        target_branch_sha_at_harvest="aaa111",
        files_changed=["src/auth.py", "tests/test_auth.py"],
        lines_added=120,
        lines_removed=5,
        tests_ran=True,
        tests_passed=True,
        lint_ran=True,
        lint_passed=True,
        merge_readiness="ready",
        summary="Added JWT auth middleware",
        risks=["Token printed at startup"],
        review_focus=["auth.py: verify token validation"],
    )
    defaults.update(overrides)
    return TaskArtifact(**defaults)


def test_review_pack_contains_task_id():
    pack = generate_review_pack(_make_artifact())
    assert "task-001" in pack


def test_review_pack_contains_title():
    pack = generate_review_pack(_make_artifact(), task_title="Build auth")
    assert '"Build auth"' in pack


def test_review_pack_contains_files():
    pack = generate_review_pack(_make_artifact())
    assert "src/auth.py" in pack
    assert "tests/test_auth.py" in pack


def test_review_pack_contains_line_counts():
    pack = generate_review_pack(_make_artifact())
    assert "+120" in pack
    assert "-5" in pack


def test_review_pack_contains_checks():
    pack = generate_review_pack(_make_artifact())
    assert "Tests: passed" in pack
    assert "Lint: passed" in pack


def test_review_pack_shows_failed_checks():
    pack = generate_review_pack(_make_artifact(tests_passed=False))
    assert "Tests: FAILED" in pack


def test_review_pack_shows_not_run():
    pack = generate_review_pack(_make_artifact(build_ran=False))
    assert "Build: not run" in pack


def test_review_pack_contains_risks():
    pack = generate_review_pack(_make_artifact())
    assert "Token printed at startup" in pack


def test_review_pack_contains_review_focus():
    pack = generate_review_pack(_make_artifact())
    assert "auth.py: verify token validation" in pack


def test_review_pack_contains_merge_readiness():
    pack = generate_review_pack(_make_artifact())
    assert "Merge Readiness: ready" in pack


def test_review_pack_shows_blocking_reasons():
    pack = generate_review_pack(
        _make_artifact(merge_readiness="blocked", blocking_reasons=["stale base"])
    )
    assert "BLOCKED: stale base" in pack


def test_review_pack_minimal_artifact():
    """Review pack works with minimal artifact (no advisory fields)."""
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
    pack = generate_review_pack(artifact)
    assert "t1" in pack
    assert "Checks" in pack


# --- extract_verdict_from_review_task tests ---


def _review_task(*, current_attempt="att-1", attempts=None, trail=None):
    task = {"current_attempt": current_attempt}
    if attempts is not None:
        task["attempts"] = attempts
    if trail is not None:
        task["trail"] = trail
    return task


def test_extract_verdict_from_artifact_key():
    task = _review_task(
        attempts=[
            {
                "attempt_id": "att-1",
                "artifact": {"verdict": "approve", "comments": "LGTM"},
            }
        ],
    )
    result = extract_verdict_from_review_task(task)
    assert result == {"verdict": "approve", "comments": "LGTM"}


def test_extract_verdict_from_review_verdict_field():
    task = _review_task(
        attempts=[
            {
                "attempt_id": "att-1",
                "artifact": None,
                "review_verdict": {"verdict": "request_changes", "reason": "missing tests"},
            }
        ],
    )
    result = extract_verdict_from_review_task(task)
    assert result == {"verdict": "request_changes", "reason": "missing tests"}


def test_extract_verdict_from_trail_fallback():
    task = _review_task(
        attempts=[{"attempt_id": "att-1"}],
        trail=[
            {"message": "started review"},
            {"message": '[REVIEW_VERDICT] {"verdict":"approve"}'},
        ],
    )
    result = extract_verdict_from_review_task(task)
    assert result == {"verdict": "approve"}


def test_extract_verdict_none_when_no_current_attempt():
    task = _review_task(current_attempt=None)
    assert extract_verdict_from_review_task(task) is None


def test_extract_verdict_none_when_malformed_trail_json():
    task = _review_task(
        attempts=[{"attempt_id": "att-1"}],
        trail=[{"message": "[REVIEW_VERDICT] {not valid json}"}],
    )
    assert extract_verdict_from_review_task(task) is None


def test_extract_verdict_most_recent_trail_entry_wins():
    task = _review_task(
        attempts=[{"attempt_id": "att-1"}],
        trail=[
            {"message": '[REVIEW_VERDICT] {"verdict":"request_changes"}'},
            {"message": '[REVIEW_VERDICT] {"verdict":"approve"}'},
        ],
    )
    result = extract_verdict_from_review_task(task)
    assert result == {"verdict": "approve"}
