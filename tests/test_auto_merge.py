"""Tests for antfarm.core.auto_merge — pure decision engine for #353."""

from __future__ import annotations

from antfarm.core.auto_merge import (
    AutoMergeOutcome,
    PRState,
    decide,
    parse_pr_state,
)


def _state(
    mergeStateStatus: str = "CLEAN",
    mergeable: str = "MERGEABLE",
    review_decision: str = "APPROVED",
    ci_conclusion: str | None = "SUCCESS",
    ci_pending: bool = False,
    ci_failing: bool = False,
) -> PRState:
    return PRState(
        mergeStateStatus=mergeStateStatus,
        mergeable=mergeable,
        review_decision=review_decision,
        ci_conclusion=ci_conclusion,
        ci_pending=ci_pending,
        ci_failing=ci_failing,
    )


# ---------------------------------------------------------------------------
# decide() — truth table coverage
# ---------------------------------------------------------------------------


def test_decide_never_mode_always_skips():
    out = decide("never", verdict_passed=True, pr_state=_state(), pr="https://gh/x/1")
    assert isinstance(out, AutoMergeOutcome)
    assert out.action == "skip"
    assert out.pr == "https://gh/x/1"
    assert out.mode == "never"
    assert out.merged is False


def test_decide_skip_when_verdict_not_passed():
    out = decide("on-review-pass", verdict_passed=False, pr_state=_state(), pr="p")
    assert out.action == "skip"


def test_decide_skip_when_pr_state_missing():
    out = decide("on-review-pass", verdict_passed=True, pr_state=None, pr="p")
    assert out.action == "skip"


def test_decide_on_review_pass_clean_merges():
    out = decide(
        "on-review-pass",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="CLEAN"),
        pr="p",
    )
    assert out.action == "merge"


def test_decide_on_review_pass_unstable_merges_ci_agnostic():
    out = decide(
        "on-review-pass",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="UNSTABLE"),
        pr="p",
    )
    assert out.action == "merge"


def test_decide_on_review_pass_dirty_rebase():
    out = decide(
        "on-review-pass",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="DIRTY"),
        pr="p",
    )
    assert out.action == "rebase"


def test_decide_on_review_pass_behind_rebase():
    out = decide(
        "on-review-pass",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="BEHIND"),
        pr="p",
    )
    assert out.action == "rebase"


def test_decide_on_review_pass_dirty_conflicting_kicks_back():
    """#365: DIRTY + mergeable=CONFLICTING means real merge conflict.
    Must route to kickback_ci, not rebase, so the worker fixes it in a
    fresh attempt instead of looping the soldier on a doomed rebase."""
    out = decide(
        "on-review-pass",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="DIRTY", mergeable="CONFLICTING"),
        pr="p",
    )
    assert out.action == "kickback_ci"
    assert out.reason == "merge_conflict"


def test_decide_on_review_pass_dirty_mergeable_still_rebases():
    """#365: DIRTY + mergeable=MERGEABLE remains a rebase (no conflict)."""
    out = decide(
        "on-review-pass",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="DIRTY", mergeable="MERGEABLE"),
        pr="p",
    )
    assert out.action == "rebase"


def test_decide_on_review_pass_behind_mergeable_rebases():
    """#365: BEHIND + mergeable=MERGEABLE remains a rebase."""
    out = decide(
        "on-review-pass",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="BEHIND", mergeable="MERGEABLE"),
        pr="p",
    )
    assert out.action == "rebase"


def test_decide_on_review_pass_blocked_ci_failing_kicks_back():
    out = decide(
        "on-review-pass",
        verdict_passed=True,
        pr_state=_state(
            mergeStateStatus="BLOCKED",
            review_decision="APPROVED",
            ci_conclusion="FAILURE",
        ),
        pr="p",
    )
    assert out.action == "kickback_ci"


def test_decide_on_review_pass_blocked_missing_reviews_pauses():
    out = decide(
        "on-review-pass",
        verdict_passed=True,
        pr_state=_state(
            mergeStateStatus="BLOCKED",
            review_decision="REVIEW_REQUIRED",
            ci_conclusion="SUCCESS",
        ),
        pr="p",
    )
    assert out.action == "pause_mission"
    assert "missing_reviews" in out.reason


def test_decide_on_review_pass_blocked_other_pauses():
    out = decide(
        "on-review-pass",
        verdict_passed=True,
        pr_state=_state(
            mergeStateStatus="BLOCKED",
            review_decision="APPROVED",
            ci_conclusion=None,
        ),
        pr="p",
    )
    assert out.action == "pause_mission"


def test_decide_on_review_pass_and_ci_green_clean_merges():
    out = decide(
        "on-review-pass-and-ci-green",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="CLEAN"),
        pr="p",
    )
    assert out.action == "merge"


def test_decide_on_review_pass_and_ci_green_unstable_defensive_waits():
    """No flags set (defensive fallback) — wait rather than guess wrong."""
    out = decide(
        "on-review-pass-and-ci-green",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="UNSTABLE"),
        pr="p",
    )
    assert out.action == "wait_ci"
    assert "unknown" in out.reason


def test_decide_on_review_pass_and_ci_green_unstable_with_failing_check_kicks_back():
    """#381: UNSTABLE + a terminal-failed check must escalate to kickback."""
    out = decide(
        "on-review-pass-and-ci-green",
        verdict_passed=True,
        pr_state=_state(
            mergeStateStatus="UNSTABLE",
            ci_conclusion="FAILURE",
            ci_failing=True,
        ),
        pr="p",
    )
    assert out.action == "kickback_ci"
    assert out.reason == "ci_check_failed"


def test_decide_on_review_pass_and_ci_green_unstable_with_pending_only_waits():
    """#381: UNSTABLE with only pending checks keeps existing wait behavior."""
    out = decide(
        "on-review-pass-and-ci-green",
        verdict_passed=True,
        pr_state=_state(
            mergeStateStatus="UNSTABLE",
            ci_conclusion="PENDING",
            ci_pending=True,
        ),
        pr="p",
    )
    assert out.action == "wait_ci"
    assert "still pending" in out.reason


def test_decide_on_review_pass_and_ci_green_unstable_with_mixed_failing_and_pending_kicks_back():
    """#381: failing dominates — even with concurrent pending, kick back."""
    out = decide(
        "on-review-pass-and-ci-green",
        verdict_passed=True,
        pr_state=_state(
            mergeStateStatus="UNSTABLE",
            ci_conclusion="FAILURE",
            ci_failing=True,
            ci_pending=True,
        ),
        pr="p",
    )
    assert out.action == "kickback_ci"
    assert out.reason == "ci_check_failed"


def test_decide_on_review_pass_and_ci_green_pending_waits():
    out = decide(
        "on-review-pass-and-ci-green",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="PENDING", ci_conclusion="PENDING"),
        pr="p",
    )
    assert out.action == "wait_ci"


def test_decide_on_review_pass_and_ci_green_dirty_rebase():
    out = decide(
        "on-review-pass-and-ci-green",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="DIRTY"),
        pr="p",
    )
    assert out.action == "rebase"


def test_decide_on_review_pass_and_ci_green_dirty_conflicting_kicks_back():
    """#365: same precedence in CI-green mode."""
    out = decide(
        "on-review-pass-and-ci-green",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="DIRTY", mergeable="CONFLICTING"),
        pr="p",
    )
    assert out.action == "kickback_ci"
    assert out.reason == "merge_conflict"


def test_decide_on_review_pass_and_ci_green_blocked_ci_kicks_back():
    out = decide(
        "on-review-pass-and-ci-green",
        verdict_passed=True,
        pr_state=_state(
            mergeStateStatus="BLOCKED",
            ci_conclusion="FAILURE",
        ),
        pr="p",
    )
    assert out.action == "kickback_ci"


def test_decide_on_review_pass_and_ci_green_blocked_missing_reviews_pauses():
    out = decide(
        "on-review-pass-and-ci-green",
        verdict_passed=True,
        pr_state=_state(
            mergeStateStatus="BLOCKED",
            review_decision="REVIEW_REQUIRED",
        ),
        pr="p",
    )
    assert out.action == "pause_mission"


# ---------------------------------------------------------------------------
# on-review-pass-and-local-tests — routing mirrors on-review-pass (#374).
# The local pre-merge test gate is enforced in the soldier handler, NOT in
# decide(). decide() must classify identically to on-review-pass so the
# state-machine transitions stay aligned.
# ---------------------------------------------------------------------------


def test_decide_on_review_pass_and_local_tests_clean_merges():
    out = decide(
        "on-review-pass-and-local-tests",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="CLEAN"),
        pr="p",
    )
    assert out.action == "merge"
    assert out.mode == "on-review-pass-and-local-tests"


def test_decide_on_review_pass_and_local_tests_dirty_rebase():
    out = decide(
        "on-review-pass-and-local-tests",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="DIRTY", mergeable="MERGEABLE"),
        pr="p",
    )
    assert out.action == "rebase"


def test_decide_on_review_pass_and_local_tests_blocked_ci_failing_kicks_back():
    out = decide(
        "on-review-pass-and-local-tests",
        verdict_passed=True,
        pr_state=_state(
            mergeStateStatus="BLOCKED",
            review_decision="APPROVED",
            ci_conclusion="FAILURE",
        ),
        pr="p",
    )
    assert out.action == "kickback_ci"


def test_decide_on_review_pass_and_local_tests_unstable_merges_ci_agnostic():
    out = decide(
        "on-review-pass-and-local-tests",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="UNSTABLE"),
        pr="p",
    )
    assert out.action == "merge"


def test_decide_has_hooks_is_skip():
    out = decide(
        "on-review-pass",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="HAS_HOOKS"),
        pr="p",
    )
    assert out.action == "skip"


def test_decide_unknown_status_is_skip():
    out = decide(
        "on-review-pass-and-ci-green",
        verdict_passed=True,
        pr_state=_state(mergeStateStatus="WHATEVER"),
        pr="p",
    )
    assert out.action == "skip"


# ---------------------------------------------------------------------------
# parse_pr_state()
# ---------------------------------------------------------------------------


def test_parse_pr_state_happy():
    payload = """
    {
      "mergeStateStatus": "CLEAN",
      "mergeable": "MERGEABLE",
      "reviewDecision": "APPROVED",
      "statusCheckRollup": [
        {"conclusion": "SUCCESS", "status": "COMPLETED"}
      ]
    }
    """
    state = parse_pr_state(payload)
    assert state is not None
    assert state.mergeStateStatus == "CLEAN"
    assert state.mergeable == "MERGEABLE"
    assert state.review_decision == "APPROVED"
    assert state.ci_conclusion == "SUCCESS"


def test_parse_pr_state_failure_dominates():
    payload = """
    {
      "mergeStateStatus": "BLOCKED",
      "mergeable": "MERGEABLE",
      "reviewDecision": "APPROVED",
      "statusCheckRollup": [
        {"conclusion": "SUCCESS", "status": "COMPLETED"},
        {"conclusion": "FAILURE", "status": "COMPLETED"}
      ]
    }
    """
    state = parse_pr_state(payload)
    assert state is not None
    assert state.ci_conclusion == "FAILURE"


def test_parse_pr_state_pending_without_failure():
    payload = """
    {
      "mergeStateStatus": "UNSTABLE",
      "mergeable": "MERGEABLE",
      "reviewDecision": "APPROVED",
      "statusCheckRollup": [
        {"conclusion": "SUCCESS", "status": "COMPLETED"},
        {"conclusion": "", "status": "IN_PROGRESS"}
      ]
    }
    """
    state = parse_pr_state(payload)
    assert state is not None
    assert state.ci_conclusion == "PENDING"


def test_parse_pr_state_malformed_returns_none():
    assert parse_pr_state("") is None
    assert parse_pr_state("   ") is None
    assert parse_pr_state("not json") is None
    assert parse_pr_state("[1, 2, 3]") is None  # top-level array


def test_parse_pr_state_missing_fields_coerced_to_empty():
    state = parse_pr_state("{}")
    assert state is not None
    assert state.mergeStateStatus == ""
    assert state.mergeable == ""
    assert state.review_decision == ""
    assert state.ci_conclusion is None
    assert state.ci_pending is False
    assert state.ci_failing is False


def test_parse_pr_state_extracts_ci_pending_and_ci_failing_flags():
    """#381: parse_pr_state must surface pending/failing summary flags."""
    failing_with_in_progress = """
    {
      "mergeStateStatus": "UNSTABLE",
      "mergeable": "MERGEABLE",
      "reviewDecision": "APPROVED",
      "statusCheckRollup": [
        {"conclusion": "FAILURE", "status": "COMPLETED"},
        {"conclusion": "", "status": "IN_PROGRESS"}
      ]
    }
    """
    state = parse_pr_state(failing_with_in_progress)
    assert state is not None
    assert state.ci_conclusion == "FAILURE"
    assert state.ci_failing is True
    assert state.ci_pending is True

    only_pending = """
    {
      "mergeStateStatus": "UNSTABLE",
      "mergeable": "MERGEABLE",
      "reviewDecision": "APPROVED",
      "statusCheckRollup": [
        {"conclusion": "", "status": "IN_PROGRESS"},
        {"conclusion": "PENDING", "status": "QUEUED"}
      ]
    }
    """
    state = parse_pr_state(only_pending)
    assert state is not None
    assert state.ci_conclusion == "PENDING"
    assert state.ci_pending is True
    assert state.ci_failing is False

    all_success = """
    {
      "mergeStateStatus": "CLEAN",
      "mergeable": "MERGEABLE",
      "reviewDecision": "APPROVED",
      "statusCheckRollup": [
        {"conclusion": "SUCCESS", "status": "COMPLETED"},
        {"conclusion": "SUCCESS", "status": "COMPLETED"}
      ]
    }
    """
    state = parse_pr_state(all_success)
    assert state is not None
    assert state.ci_conclusion == "SUCCESS"
    assert state.ci_pending is False
    assert state.ci_failing is False


def test_summarize_rollup_treats_cancelled_as_pending():
    """#381 deviation: CANCELLED is transient (operator re-triggers), not terminal."""
    payload = """
    {
      "mergeStateStatus": "UNSTABLE",
      "mergeable": "MERGEABLE",
      "reviewDecision": "APPROVED",
      "statusCheckRollup": [
        {"conclusion": "CANCELLED", "status": "COMPLETED"}
      ]
    }
    """
    state = parse_pr_state(payload)
    assert state is not None
    assert state.ci_pending is True
    assert state.ci_failing is False
    assert state.ci_conclusion == "PENDING"
