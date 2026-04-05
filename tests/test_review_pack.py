"""Tests for antfarm.core.review_pack generation."""

from antfarm.core.models import TaskArtifact
from antfarm.core.review_pack import generate_review_pack


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
