"""Tests for Soldier auto-merge integration (#353).

All subprocess calls (``git``, ``gh``) are mocked via monkeypatched
``subprocess.run``. The colony is a plain MagicMock — we exercise the
Soldier's decision/dispatch wiring, not the colony API.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from antfarm.core.auto_merge import PRState
from antfarm.core.soldier import MergeResult, Soldier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_soldier(integration_branch: str = "main", monkeypatch=None) -> Soldier:
    """Construct a Soldier with a bypassed __init__ so we can drop in mocks.

    Avoids any real HTTP, git, or backend calls during construction.
    """
    s = Soldier.__new__(Soldier)
    s.colony = MagicMock()
    s.colony_url = ""
    s.repo_path = "/fake/repo"
    s.integration_branch = integration_branch
    s.test_command = ["true"]
    s.poll_interval = 0.0
    s.require_review = True
    s.poll_external_merges = False
    s.data_dir_name = ".antfarm"
    s.last_failure_reason = ""
    s._event_cursor = 0
    s._preflight_done = True
    s._auto_merge_last_checked = {}
    s._repo_permission_cache = {}
    s.auto_merge_poll_backoff_seconds = 30.0
    s._auto_merge_rebase_attempts = {}
    s.auto_merge_max_rebase_attempts = 2
    s._reconcile_last_checked = {}
    s.reconcile_backoff_seconds = 60.0
    return s


def _task(
    task_id: str = "task-1",
    mission_id: str | None = "mission-a",
    pr: str | None = "https://github.com/org/repo/pull/1",
    branch: str | None = "feat/task-1",
) -> dict:
    return {
        "id": task_id,
        "mission_id": mission_id,
        "current_attempt": "att-1",
        "attempts": [
            {
                "attempt_id": "att-1",
                "worker_id": "w1",
                "status": "done",
                "branch": branch,
                "pr": pr,
            }
        ],
    }


def _mission(auto_merge: str = "on-review-pass", allow_external: bool = False) -> dict:
    return {
        "mission_id": "mission-a",
        "status": "building",
        "config": {
            "auto_merge": auto_merge,
            "allow_auto_merge_on_external": allow_external,
            "completion_mode": "best_effort",
        },
        "task_ids": ["task-1"],
    }


# ---------------------------------------------------------------------------
# 1. never-mode regression: _attempt_auto_merge returns None, no gh calls
# ---------------------------------------------------------------------------


def test_never_mode_returns_none_and_makes_no_gh_call(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="never")

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    outcome = s._attempt_auto_merge(_task())
    assert outcome is None
    # No subprocess was invoked — permission check / gh calls suppressed.
    assert calls == []


# ---------------------------------------------------------------------------
# 2. on-review-pass CLEAN => merge dispatches gh pr merge + mark_merged(auto)
# ---------------------------------------------------------------------------


def test_on_review_pass_clean_triggers_gh_merge(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")

    # Pretend PR state is CLEAN. We mock the whole query helper.
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("CLEAN", "MERGEABLE", "APPROVED", "SUCCESS"),
    )
    # Auto-merge security gate: simulate ADMIN on main.
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")
    merge_calls: list[tuple[str, str | None]] = []

    def fake_merge(pr: str, branch: str | None = None):
        merge_calls.append((pr, branch))
        return True, ""

    monkeypatch.setattr(s, "_gh_pr_merge_squash", fake_merge)
    monkeypatch.setattr(s, "_sync_integration_branch_after_auto_merge", lambda: None)

    task = _task()
    outcome = s._attempt_auto_merge(task)
    assert outcome is not None
    assert outcome.action == "merge"

    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.MERGED
    assert merge_calls == [("https://github.com/org/repo/pull/1", "feat/task-1")]
    # mark_merged should receive auto_merged=True
    s.colony.mark_merged.assert_called_once_with("task-1", "att-1", auto_merged=True)


# ---------------------------------------------------------------------------
# 3. on-review-pass UNSTABLE => merge (CI-agnostic)
# ---------------------------------------------------------------------------


def test_on_review_pass_unstable_merges_ci_agnostic(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("UNSTABLE", "MERGEABLE", "APPROVED", "FAILURE"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")
    monkeypatch.setattr(s, "_gh_pr_merge_squash", lambda pr, branch=None: (True, ""))
    monkeypatch.setattr(s, "_sync_integration_branch_after_auto_merge", lambda: None)

    outcome = s._attempt_auto_merge(_task())
    assert outcome.action == "merge"


# ---------------------------------------------------------------------------
# 4. on-review-pass-and-ci-green UNSTABLE => wait_ci, no merge, no kickback
# ---------------------------------------------------------------------------


def test_ci_green_mode_unstable_waits(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass-and-ci-green")
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("UNSTABLE", "MERGEABLE", "APPROVED", "PENDING"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")

    task = _task()
    outcome = s._attempt_auto_merge(task)
    assert outcome.action == "wait_ci"
    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.NEEDS_REVIEW
    s.colony.mark_merged.assert_not_called()
    s.colony.kickback.assert_not_called()


# ---------------------------------------------------------------------------
# 5. BLOCKED with CI failing => kickback_ci => kickback_with_cascade called
# ---------------------------------------------------------------------------


def test_blocked_ci_failing_kicks_back(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("BLOCKED", "MERGEABLE", "APPROVED", "FAILURE"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")
    kickback_calls = []

    def fake_kickback(task_id, reason, **kw):
        kickback_calls.append((task_id, reason))

    monkeypatch.setattr(s, "kickback_with_cascade", fake_kickback)

    task = _task()
    outcome = s._attempt_auto_merge(task)
    assert outcome.action == "kickback_ci"
    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.FAILED
    assert kickback_calls and kickback_calls[0][0] == "task-1"
    assert "auto_merge_ci_failed" in kickback_calls[0][1]


# ---------------------------------------------------------------------------
# 6. BLOCKED missing_reviews => pause_mission (BLOCKED status)
# ---------------------------------------------------------------------------


def test_blocked_missing_reviews_pauses_mission(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("BLOCKED", "MERGEABLE", "REVIEW_REQUIRED", "SUCCESS"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")

    task = _task()
    outcome = s._attempt_auto_merge(task)
    assert outcome.action == "pause_mission"
    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.NEEDS_REVIEW
    # update_mission should be called with status=blocked + reason
    assert s.colony.update_mission.called
    call_args = s.colony.update_mission.call_args
    assert call_args.args[0] == "mission-a"
    updates = call_args.args[1]
    assert updates["status"] == "blocked"
    assert "auto_merge_pause_reason" in updates


# ---------------------------------------------------------------------------
# 7. DIRTY => rebase dispatch (rebase helper invoked once)
# ---------------------------------------------------------------------------


def test_dirty_triggers_rebase(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    # #365: DIRTY only triggers rebase when mergeable=MERGEABLE. The
    # CONFLICTING case is now routed to kickback_ci by the decide() module.
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("DIRTY", "MERGEABLE", "APPROVED", "SUCCESS"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")
    rebase_called = []
    monkeypatch.setattr(
        s,
        "_rebase_pr_branch_for_auto_merge",
        lambda task, pr: rebase_called.append((task["id"], pr)),
    )

    task = _task()
    outcome = s._attempt_auto_merge(task)
    assert outcome.action == "rebase"
    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.NEEDS_REVIEW
    assert rebase_called == [("task-1", "https://github.com/org/repo/pull/1")]


# ---------------------------------------------------------------------------
# #365: CONFLICTING => kickback_ci, no rebase
# ---------------------------------------------------------------------------


def test_conflicting_triggers_kickback_no_rebase(monkeypatch):
    """#365: DIRTY + mergeable=CONFLICTING must route to kickback with the
    auto_merge_merge_conflict reason, and the rebase helper must NOT fire."""
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("DIRTY", "CONFLICTING", "APPROVED", "SUCCESS"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")

    rebase_called: list[tuple] = []
    monkeypatch.setattr(
        s,
        "_rebase_pr_branch_for_auto_merge",
        lambda task, pr: rebase_called.append((task["id"], pr)),
    )

    kickback_calls: list[tuple[str, str]] = []

    def fake_kickback(task_id, reason, **kw):
        kickback_calls.append((task_id, reason))

    monkeypatch.setattr(s, "kickback_with_cascade", fake_kickback)

    task = _task()
    outcome = s._attempt_auto_merge(task)
    assert outcome.action == "kickback_ci"
    assert outcome.reason == "merge_conflict"

    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.FAILED
    assert rebase_called == [], "rebase helper must not fire on CONFLICTING"
    assert kickback_calls and kickback_calls[0][0] == "task-1"
    assert "auto_merge_merge_conflict" in kickback_calls[0][1]
    assert "merge_conflict" in kickback_calls[0][1]


# ---------------------------------------------------------------------------
# #365: bounded rebase attempts — exhaustion triggers kickback
# ---------------------------------------------------------------------------


def test_rebase_attempts_counter_increments_then_kicks_back(monkeypatch):
    """#365: two consecutive rebase outcomes increment the counter; the
    third dispatch finds the cap reached and kicks back with
    auto_merge_rebase_exhausted."""
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("BEHIND", "MERGEABLE", "APPROVED", "SUCCESS"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")

    # Rebase always fails — every attempt bumps the counter.
    monkeypatch.setattr(
        s,
        "_rebase_and_retry_merge",
        lambda task_id, branch, temp_branch, initial_conflict_stderr: MergeResult.FAILED,
    )
    monkeypatch.setattr(s, "_get_attempt_branch", lambda task: "feat/task-1")

    kickback_calls: list[tuple[str, str]] = []

    def fake_kickback(task_id, reason, **kw):
        kickback_calls.append((task_id, reason))

    monkeypatch.setattr(s, "kickback_with_cascade", fake_kickback)

    task = _task()

    # First two ticks: rebase dispatched, counter increments.
    for _ in range(s.auto_merge_max_rebase_attempts):
        # Reset poll backoff so each tick re-queries.
        s._auto_merge_last_checked.clear()
        outcome = s._attempt_auto_merge(task)
        assert outcome.action == "rebase"
        result = s._handle_auto_merge_outcome(outcome, task)
        assert result == MergeResult.NEEDS_REVIEW

    assert s._auto_merge_rebase_attempts.get("att-1") == s.auto_merge_max_rebase_attempts
    assert kickback_calls == []  # not yet exhausted at dispatch time

    # Third tick: counter at cap → kickback.
    s._auto_merge_last_checked.clear()
    outcome = s._attempt_auto_merge(task)
    assert outcome.action == "rebase"
    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.FAILED
    assert kickback_calls and kickback_calls[0][0] == "task-1"
    assert "auto_merge_rebase_exhausted" in kickback_calls[0][1]
    assert f"attempts={s.auto_merge_max_rebase_attempts}" in kickback_calls[0][1]
    # Counter cleared after exhaustion.
    assert "att-1" not in s._auto_merge_rebase_attempts


def test_rebase_attempts_counter_clears_on_success(monkeypatch):
    """#365: a MERGED outcome from the rebase helper pops the counter."""
    s = _make_soldier()
    monkeypatch.setattr(s, "_get_attempt_branch", lambda task: "feat/task-1")
    monkeypatch.setattr(
        s,
        "_rebase_and_retry_merge",
        lambda task_id, branch, temp_branch, initial_conflict_stderr: MergeResult.MERGED,
    )

    task = _task()
    # Pre-seed a counter to verify it is cleared.
    s._auto_merge_rebase_attempts["att-1"] = 1
    s._rebase_pr_branch_for_auto_merge(task, "https://github.com/org/repo/pull/1")
    assert "att-1" not in s._auto_merge_rebase_attempts


def test_rebase_attempts_counter_keyed_by_attempt_id(monkeypatch):
    """#365: distinct attempt_ids must keep independent counters so the
    same task being retried under a fresh attempt doesn't inherit prior
    rebase exhaustion."""
    s = _make_soldier()
    monkeypatch.setattr(s, "_get_attempt_branch", lambda task: "feat/x")
    monkeypatch.setattr(
        s,
        "_rebase_and_retry_merge",
        lambda task_id, branch, temp_branch, initial_conflict_stderr: MergeResult.FAILED,
    )

    task_a = _task(task_id="task-a")
    task_a["current_attempt"] = "att-A"
    task_a["attempts"][0]["attempt_id"] = "att-A"

    task_b = _task(task_id="task-b")
    task_b["current_attempt"] = "att-B"
    task_b["attempts"][0]["attempt_id"] = "att-B"

    s._rebase_pr_branch_for_auto_merge(task_a, "pr-a")
    s._rebase_pr_branch_for_auto_merge(task_a, "pr-a")
    s._rebase_pr_branch_for_auto_merge(task_b, "pr-b")

    assert s._auto_merge_rebase_attempts["att-A"] == 2
    assert s._auto_merge_rebase_attempts["att-B"] == 1


# ---------------------------------------------------------------------------
# 8. Security guard: non-WRITE permission refuses auto-merge
# ---------------------------------------------------------------------------


def test_security_guard_refuses_low_permission(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "READ")
    # _query_pr_state should NEVER be called — short-circuited by guard.
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    outcome = s._attempt_auto_merge(_task())
    assert outcome.action == "skip"
    assert "refused" in outcome.reason


# ---------------------------------------------------------------------------
# 9. Security guard EXTRA: WRITE permission on main requires explicit opt-in
# ---------------------------------------------------------------------------


def test_security_guard_write_on_main_requires_opt_in(monkeypatch):
    s = _make_soldier(integration_branch="main")
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass", allow_external=False)
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "WRITE")

    outcome = s._attempt_auto_merge(_task())
    assert outcome.action == "skip"
    assert "refused" in outcome.reason


def test_security_guard_write_on_main_allowed_with_opt_in(monkeypatch):
    s = _make_soldier(integration_branch="main")
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass", allow_external=True)
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "WRITE")
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("CLEAN", "MERGEABLE", "APPROVED", "SUCCESS"),
    )

    outcome = s._attempt_auto_merge(_task())
    assert outcome.action == "merge"


# ---------------------------------------------------------------------------
# 10. Poll backoff: second call within 30s returns skip without gh
# ---------------------------------------------------------------------------


def test_poll_backoff_suppresses_redundant_polls(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")

    state_calls = []

    def fake_state(pr):
        state_calls.append(pr)
        return PRState("UNSTABLE", "MERGEABLE", "APPROVED", "PENDING")

    monkeypatch.setattr(s, "_query_pr_state", fake_state)

    # First call: hits gh.
    s._attempt_auto_merge(_task())
    assert len(state_calls) == 1
    # Second call within the 30s window: returns skip, no gh call.
    out2 = s._attempt_auto_merge(_task())
    assert len(state_calls) == 1
    assert out2.action == "skip"
    assert "backoff" in out2.reason


# ---------------------------------------------------------------------------
# 11. Emits `auto_merged` event on successful merge
# ---------------------------------------------------------------------------


def test_emits_auto_merged_event_on_success(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("CLEAN", "MERGEABLE", "APPROVED", "SUCCESS"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")
    monkeypatch.setattr(s, "_gh_pr_merge_squash", lambda pr, branch=None: (True, ""))
    monkeypatch.setattr(s, "_sync_integration_branch_after_auto_merge", lambda: None)

    emitted = []

    def fake_emit(event_type, task_id, detail="", actor="soldier"):
        emitted.append((event_type, task_id, detail))

    # Patch the _emit_event dispatcher used by the soldier._emit shim.
    with patch("antfarm.core.serve._emit_event", side_effect=fake_emit):
        task = _task()
        outcome = s._attempt_auto_merge(task)
        result = s._handle_auto_merge_outcome(outcome, task)
        assert result == MergeResult.MERGED

    event_types = [e[0] for e in emitted]
    assert "auto_merged" in event_types


# ---------------------------------------------------------------------------
# 12. Emits `auto_merge_refused` when security guard blocks
# ---------------------------------------------------------------------------


def test_emits_auto_merge_refused_event(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "READ")

    emitted = []

    def fake_emit(event_type, task_id, detail="", actor="soldier"):
        emitted.append((event_type, task_id, detail))

    with patch("antfarm.core.serve._emit_event", side_effect=fake_emit):
        s._attempt_auto_merge(_task())

    event_types = [e[0] for e in emitted]
    assert "auto_merge_refused" in event_types


# ---------------------------------------------------------------------------
# 13. gh pr merge failure => FAILED, no mark_merged
# ---------------------------------------------------------------------------


def test_gh_pr_merge_failure_returns_failed(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("CLEAN", "MERGEABLE", "APPROVED", "SUCCESS"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")
    monkeypatch.setattr(
        s, "_gh_pr_merge_squash", lambda pr, branch=None: (False, "remote rejected")
    )

    task = _task()
    outcome = s._attempt_auto_merge(task)
    assert outcome.action == "merge"
    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.FAILED
    s.colony.mark_merged.assert_not_called()


# ---------------------------------------------------------------------------
# 14. Race condition: gh pr view returns None (network error) => skip
# ---------------------------------------------------------------------------


def test_pr_state_none_returns_skip(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")
    monkeypatch.setattr(s, "_query_pr_state", lambda pr: None)

    outcome = s._attempt_auto_merge(_task())
    assert outcome.action == "skip"


# ---------------------------------------------------------------------------
# 15. No PR on attempt => skip outcome (no merge attempted)
# ---------------------------------------------------------------------------


def test_no_pr_on_attempt_returns_skip(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    task = _task(pr=None)
    task["attempts"][0]["pr"] = None
    # Even so, _get_attempt_pr looks at pr field; empty → "".
    outcome = s._attempt_auto_merge(task)
    assert outcome is not None
    assert outcome.action == "skip"


# ---------------------------------------------------------------------------
# 16. Dependent task rebase: when outcome.action=='rebase', rebase helper fires
# ---------------------------------------------------------------------------


def test_rebase_helper_invokes_existing_rebase_infrastructure(monkeypatch):
    s = _make_soldier()
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("BEHIND", "MERGEABLE", "APPROVED", "SUCCESS"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")

    rebased = []

    def fake_rebase(task_id, branch, temp_branch, initial_conflict_stderr):
        rebased.append((task_id, branch, temp_branch))
        return MergeResult.NEEDS_REVIEW

    monkeypatch.setattr(s, "_rebase_and_retry_merge", fake_rebase)

    task = _task()
    outcome = s._attempt_auto_merge(task)
    assert outcome.action == "rebase"
    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.NEEDS_REVIEW
    assert rebased, "expected _rebase_and_retry_merge to be invoked"


# ---------------------------------------------------------------------------
# 17. Task with no mission_id resolves to never-mode (skips auto-merge)
# ---------------------------------------------------------------------------


def test_task_without_mission_id_returns_none():
    s = _make_soldier()
    task = _task(mission_id=None)
    task.pop("mission_id", None)
    outcome = s._attempt_auto_merge(task)
    assert outcome is None


# ---------------------------------------------------------------------------
# 18. Mission without auto_merge key defaults to never
# ---------------------------------------------------------------------------


def test_mission_without_auto_merge_key_is_never():
    s = _make_soldier()
    s.colony.get_mission.return_value = {"mission_id": "m", "status": "building", "config": {}}
    outcome = s._attempt_auto_merge(_task())
    assert outcome is None


# ---------------------------------------------------------------------------
# #360: _gh_pr_merge_squash decouples remote merge from branch cleanup.
#
# The helper now runs ``gh pr merge --squash`` without ``--delete-branch`` and
# drives local + remote branch deletes itself, so a janky cleanup (worktree
# collision, remote already gone) never turns a successful merge into a
# kickback. Regression tests for each branch of the new contract.
# ---------------------------------------------------------------------------


def _make_soldier_for_gh() -> Soldier:
    """Bare soldier suitable for exercising ``_gh_pr_merge_squash`` directly."""
    s = Soldier.__new__(Soldier)
    s.colony = MagicMock()
    s.colony_url = ""
    s.repo_path = "/fake/repo"
    s.integration_branch = "main"
    s.test_command = ["true"]
    s.poll_interval = 0.0
    s.require_review = True
    s.poll_external_merges = False
    s.data_dir_name = ".antfarm"
    s.last_failure_reason = ""
    s._event_cursor = 0
    s._preflight_done = True
    s._auto_merge_last_checked = {}
    s._repo_permission_cache = {}
    s.auto_merge_poll_backoff_seconds = 30.0
    s._auto_merge_rebase_attempts = {}
    s.auto_merge_max_rebase_attempts = 2
    s._reconcile_last_checked = {}
    s.reconcile_backoff_seconds = 60.0
    return s


def test_gh_pr_merge_squash_success_with_branch(monkeypatch):
    """gh returns 0 → branch-delete attempts are issued (both local + remote)
    but their failures are swallowed; helper returns (True, "")."""
    s = _make_soldier_for_gh()

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        # Both local and remote deletes simulate failures — must be tolerated.
        if cmd[:3] == ["git", "branch", "-D"]:
            return SimpleNamespace(
                returncode=1,
                stdout=b"",
                stderr=b"error: branch 'feat/x' not found.\n",
            )
        if cmd[:4] == ["git", "push", "origin", "--delete"]:
            return SimpleNamespace(
                returncode=1,
                stdout=b"",
                stderr=b"error: unable to delete 'feat/x': remote ref does not exist\n",
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, tail = s._gh_pr_merge_squash("PR-1", branch="feat/x")
    assert ok is True
    assert tail == ""
    # gh invocation must NOT include --delete-branch.
    gh_cmds = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert gh_cmds and "--delete-branch" not in gh_cmds[0]
    # Cleanup attempts were made despite failures.
    assert any(c[:3] == ["git", "branch", "-D"] for c in calls)
    assert any(c[:4] == ["git", "push", "origin", "--delete"] for c in calls)


def test_gh_pr_merge_squash_local_branch_delete_reclaim(monkeypatch):
    """gh 0 → local ``git branch -D`` first fails with 'used by worktree at',
    reclaim path runs, retry succeeds; helper still returns (True, "")."""
    s = _make_soldier_for_gh()

    branch_delete_calls = {"n": 0}
    reclaim_calls: list[str] = []

    def fake_reclaim(stderr: str) -> bool:
        reclaim_calls.append(stderr)
        return True

    monkeypatch.setattr(s, "_remove_blocking_worktree", fake_reclaim)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["git", "branch", "-D"]:
            branch_delete_calls["n"] += 1
            if branch_delete_calls["n"] == 1:
                return SimpleNamespace(
                    returncode=1,
                    stdout=b"",
                    stderr=(
                        b"error: cannot delete branch 'feat/x' used by worktree "
                        b"at '/fake/repo/.antfarm/workspaces/task-x-att-001'\n"
                    ),
                )
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if cmd[:4] == ["git", "push", "origin", "--delete"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, tail = s._gh_pr_merge_squash("PR-1", branch="feat/x")
    assert ok is True
    assert tail == ""
    assert reclaim_calls, "reclaim helper should have been invoked"
    assert branch_delete_calls["n"] == 2  # initial fail + retry success


def test_gh_pr_merge_squash_local_branch_delete_unrelated_error(monkeypatch):
    """gh 0 → local ``git branch -D`` fails with a non-worktree stderr.
    No reclaim attempted. Helper still returns (True, "")."""
    s = _make_soldier_for_gh()

    reclaim_calls: list[str] = []
    monkeypatch.setattr(
        s,
        "_remove_blocking_worktree",
        lambda stderr: reclaim_calls.append(stderr) or True,
    )

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["git", "branch", "-D"]:
            return SimpleNamespace(
                returncode=1,
                stdout=b"",
                stderr=b"error: branch 'feat/x' not found.\n",
            )
        if cmd[:4] == ["git", "push", "origin", "--delete"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, tail = s._gh_pr_merge_squash("PR-1", branch="feat/x")
    assert ok is True
    assert tail == ""
    # Non-worktree stderr must NOT route through the reclaim helper.
    assert reclaim_calls == []


def test_gh_pr_merge_squash_nonzero_but_merged(monkeypatch):
    """gh returns 1 but origin reports MERGED → treat as success.

    Emits ``auto_merge_gh_nonzero_but_merged`` and still attempts branch
    cleanup. Returns (True, "")."""
    s = _make_soldier_for_gh()

    monkeypatch.setattr(s, "_check_pr_merged_on_origin", lambda pr: True)

    cleanup_calls: list[tuple] = []

    def fake_cleanup(branch):
        cleanup_calls.append(("cleanup", branch))

    monkeypatch.setattr(s, "_cleanup_merged_branch", fake_cleanup)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="already merged or transient")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    emitted: list[tuple[str, str, str]] = []

    def fake_emit(event_type, task_id, detail="", actor="soldier"):
        emitted.append((event_type, task_id, detail))

    with patch("antfarm.core.serve._emit_event", side_effect=fake_emit):
        ok, tail = s._gh_pr_merge_squash("PR-1", branch="feat/x")

    assert ok is True
    assert tail == ""
    assert any(e[0] == "auto_merge_gh_nonzero_but_merged" for e in emitted)
    assert cleanup_calls == [("cleanup", "feat/x")]


def test_gh_pr_merge_squash_nonzero_not_merged(monkeypatch):
    """gh returns 1 and origin reports OPEN → surface failure with tail."""
    s = _make_soldier_for_gh()
    monkeypatch.setattr(s, "_check_pr_merged_on_origin", lambda pr: False)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(
                returncode=1, stdout="", stderr="remote rejected\nbecause reasons\n"
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, tail = s._gh_pr_merge_squash("PR-1", branch="feat/x")
    assert ok is False
    assert "remote rejected" in tail


def test_gh_pr_merge_squash_nonzero_state_unknown(monkeypatch):
    """gh returns 1 and origin state is unknown (None) → conservative FAIL."""
    s = _make_soldier_for_gh()
    monkeypatch.setattr(s, "_check_pr_merged_on_origin", lambda pr: None)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="network blip")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, tail = s._gh_pr_merge_squash("PR-1", branch="feat/x")
    assert ok is False
    assert "network blip" in tail


def test_gh_pr_merge_squash_remote_branch_delete_fails(monkeypatch):
    """gh 0 → local delete OK, ``git push origin --delete`` fails; still
    returns (True, "") — cleanup must never poison a successful merge."""
    s = _make_soldier_for_gh()

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["git", "branch", "-D"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if cmd[:4] == ["git", "push", "origin", "--delete"]:
            return SimpleNamespace(
                returncode=1,
                stdout=b"",
                stderr=b"error: unable to delete 'feat/x': remote ref does not exist\n",
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, tail = s._gh_pr_merge_squash("PR-1", branch="feat/x")
    assert ok is True
    assert tail == ""


# ---------------------------------------------------------------------------
# #374: on-review-pass-and-local-tests pre-merge gate
# ---------------------------------------------------------------------------


def test_local_tests_mode_clean_runs_tests_then_merges(monkeypatch):
    """CLEAN PR + local tests pass → gh pr merge fires, MergeResult.MERGED,
    and the integration branch is restored as the last checkout call."""
    s = _make_soldier()
    s.test_command = ["pytest", "-x", "-q"]
    s.colony.get_mission.return_value = _mission(
        auto_merge="on-review-pass-and-local-tests"
    )
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("CLEAN", "MERGEABLE", "APPROVED", "SUCCESS"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")

    merge_calls: list[tuple[str, str | None]] = []

    def fake_merge(pr: str, branch: str | None = None):
        merge_calls.append((pr, branch))
        return True, ""

    monkeypatch.setattr(s, "_gh_pr_merge_squash", fake_merge)
    monkeypatch.setattr(s, "_sync_integration_branch_after_auto_merge", lambda: None)

    checkout_calls: list[list[str]] = []

    def fake_checkout(args):
        checkout_calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(s, "_checkout_with_reclaim", fake_checkout)

    run_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        run_calls.append(list(cmd))
        # Both git fetch and the test_command return rc=0.
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    task = _task()
    outcome = s._attempt_auto_merge(task)
    assert outcome.action == "merge"
    assert outcome.mode == "on-review-pass-and-local-tests"

    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.MERGED
    # gh pr merge invoked once with the expected branch.
    assert merge_calls == [("https://github.com/org/repo/pull/1", "feat/task-1")]
    # The test_command was run in the repo (subprocess.run was called with it).
    assert any(cmd == s.test_command for cmd in run_calls), (
        f"expected test_command in subprocess.run calls, got {run_calls}"
    )
    # Last checkout call must restore the integration branch.
    assert checkout_calls, "expected at least one _checkout_with_reclaim call"
    assert checkout_calls[-1] == ["checkout", s.integration_branch]


def test_local_tests_mode_clean_test_failure_kicks_back_no_merge(monkeypatch):
    """CLEAN PR + tests fail (rc=1) → kickback with auto_merge_local_test_failed,
    gh pr merge NOT called, integration branch still restored."""
    s = _make_soldier()
    s.test_command = ["pytest", "-x", "-q"]
    s.colony.get_mission.return_value = _mission(
        auto_merge="on-review-pass-and-local-tests"
    )
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("CLEAN", "MERGEABLE", "APPROVED", "SUCCESS"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")

    merge_calls: list[tuple[str, str | None]] = []

    def fake_merge(pr, branch=None):
        merge_calls.append((pr, branch))
        return True, ""

    monkeypatch.setattr(s, "_gh_pr_merge_squash", fake_merge)
    monkeypatch.setattr(s, "_sync_integration_branch_after_auto_merge", lambda: None)

    checkout_calls: list[list[str]] = []

    def fake_checkout(args):
        checkout_calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(s, "_checkout_with_reclaim", fake_checkout)

    def fake_run(cmd, **kwargs):
        # git fetch returns 0; test_command returns 1.
        if cmd[:2] == ["git", "fetch"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if list(cmd) == s.test_command:
            return SimpleNamespace(
                returncode=1, stdout=b"", stderr=b"E   assert 1 == 2\n"
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    kickback_calls: list[tuple[str, str]] = []

    def fake_kickback(task_id, reason, **kw):
        kickback_calls.append((task_id, reason))

    monkeypatch.setattr(s, "kickback_with_cascade", fake_kickback)

    task = _task()
    outcome = s._attempt_auto_merge(task)
    assert outcome.action == "merge"

    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.FAILED
    assert merge_calls == [], "gh pr merge must not be called on test failure"
    assert kickback_calls and kickback_calls[0][0] == "task-1"
    assert kickback_calls[0][1].startswith("auto_merge_local_test_failed")
    # Integration branch still restored as the LAST checkout call.
    assert checkout_calls, "expected at least one _checkout_with_reclaim call"
    assert checkout_calls[-1] == ["checkout", s.integration_branch]


def test_local_tests_mode_checkout_failure_kicks_back(monkeypatch):
    """Checkout to PR branch fails → kickback fires, no test_command run,
    no gh pr merge."""
    s = _make_soldier()
    s.test_command = ["pytest", "-x", "-q"]
    s.colony.get_mission.return_value = _mission(
        auto_merge="on-review-pass-and-local-tests"
    )
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("CLEAN", "MERGEABLE", "APPROVED", "SUCCESS"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")

    merge_called: list[tuple[str, str | None]] = []

    def fake_merge(pr, branch=None):
        merge_called.append((pr, branch))
        return True, ""

    monkeypatch.setattr(s, "_gh_pr_merge_squash", fake_merge)

    checkout_calls: list[list[str]] = []

    def fake_checkout(args):
        checkout_calls.append(list(args))
        # First call (checkout -B PR branch) fails. Subsequent restore call
        # to integration branch succeeds.
        if args[:1] == ["checkout"] and len(args) >= 2 and args[1] == "-B":
            return SimpleNamespace(
                returncode=1,
                stdout=b"",
                stderr=b"error: pathspec 'origin/feat/task-1' did not match\n",
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(s, "_checkout_with_reclaim", fake_checkout)

    run_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        run_calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    kickback_calls: list[tuple[str, str]] = []

    def fake_kickback(task_id, reason, **kw):
        kickback_calls.append((task_id, reason))

    monkeypatch.setattr(s, "kickback_with_cascade", fake_kickback)

    task = _task()
    outcome = s._attempt_auto_merge(task)
    assert outcome.action == "merge"

    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.FAILED
    assert merge_called == [], "gh pr merge must not run after checkout failure"
    assert kickback_calls and kickback_calls[0][0] == "task-1"
    assert kickback_calls[0][1].startswith("auto_merge_local_test_failed")
    assert "checkout_failed" in kickback_calls[0][1]
    # test_command must NOT have been executed.
    assert not any(list(cmd) == s.test_command for cmd in run_calls), (
        f"test_command should not run after checkout failure, got {run_calls}"
    )
    # Integration branch restore still attempted.
    assert checkout_calls[-1] == ["checkout", s.integration_branch]


def test_local_tests_mode_dirty_takes_rebase_path_no_extra_pretest(monkeypatch):
    """DIRTY PR in local-tests mode → rebase path; the new pre-test gate
    must NOT be entered (no test_command runs, no PR-branch checkout)."""
    s = _make_soldier()
    s.test_command = ["pytest", "-x", "-q"]
    s.colony.get_mission.return_value = _mission(
        auto_merge="on-review-pass-and-local-tests"
    )
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("DIRTY", "MERGEABLE", "APPROVED", "SUCCESS"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")

    rebase_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        s,
        "_rebase_pr_branch_for_auto_merge",
        lambda task, pr: rebase_calls.append((task["id"], pr)),
    )

    run_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        run_calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    checkout_calls: list[list[str]] = []

    def fake_checkout(args):
        checkout_calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(s, "_checkout_with_reclaim", fake_checkout)

    task = _task()
    outcome = s._attempt_auto_merge(task)
    assert outcome.action == "rebase"

    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.NEEDS_REVIEW
    assert rebase_calls == [("task-1", "https://github.com/org/repo/pull/1")]
    # Pre-test gate must not have run the test_command.
    assert not any(list(cmd) == s.test_command for cmd in run_calls), (
        f"test_command must not run on rebase path, got {run_calls}"
    )


def test_on_review_pass_clean_does_not_run_local_tests_regression(monkeypatch):
    """Default on-review-pass mode + CLEAN PR must NOT trigger the local
    test gate. subprocess.run is never called with self.test_command."""
    s = _make_soldier()
    s.test_command = ["pytest", "-x", "-q"]
    s.colony.get_mission.return_value = _mission(auto_merge="on-review-pass")
    monkeypatch.setattr(
        s,
        "_query_pr_state",
        lambda pr: PRState("CLEAN", "MERGEABLE", "APPROVED", "SUCCESS"),
    )
    monkeypatch.setattr(s, "_resolve_repo_slug", lambda: "org/repo")
    monkeypatch.setattr(s, "_query_viewer_permission", lambda: "ADMIN")
    monkeypatch.setattr(s, "_gh_pr_merge_squash", lambda pr, branch=None: (True, ""))
    monkeypatch.setattr(s, "_sync_integration_branch_after_auto_merge", lambda: None)

    run_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        run_calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    task = _task()
    outcome = s._attempt_auto_merge(task)
    assert outcome.action == "merge"
    assert outcome.mode == "on-review-pass"

    result = s._handle_auto_merge_outcome(outcome, task)
    assert result == MergeResult.MERGED
    # Default mode must NOT call subprocess.run with the test_command.
    assert not any(list(cmd) == s.test_command for cmd in run_calls), (
        f"on-review-pass mode must not run test_command, got {run_calls}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-q"])
