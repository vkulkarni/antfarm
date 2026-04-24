"""Auto-merge decision engine (#353).

Pure module. No I/O, no subprocess calls, no HTTP. Exists so the decision
truth table can be unit-tested without mocking git, gh, or the colony.

The mapping from ``(mode, verdict_passed, pr_state)`` to an
:class:`AutoMergeOutcome` lives in :func:`decide`. The helper
:func:`parse_pr_state` converts raw ``gh pr view --json ...`` output into a
:class:`PRState` consumed by :func:`decide`.

Consumers (the Soldier orchestrator) are responsible for performing the
actual git/gh/colony side effects and for wiring the outcome back into the
merge pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

AutoMergeAction = Literal[
    "merge",
    "rebase",
    "wait_ci",
    "kickback_ci",
    "pause_mission",
    "skip",
]


@dataclass
class PRState:
    """Snapshot of a GitHub PR's mergeability state.

    Field names mirror the ``gh pr view --json`` keys so the adapter in
    :func:`parse_pr_state` stays literal.
    """

    mergeStateStatus: str
    mergeable: str
    review_decision: str
    ci_conclusion: str | None


@dataclass
class AutoMergeOutcome:
    """Decision result from :func:`decide`.

    ``merged`` is reserved for downstream helpers to mark after the merge
    actually completes — :func:`decide` always returns ``merged=False``.
    """

    action: AutoMergeAction
    pr: str
    mode: str
    reason: str
    merged: bool = False


_BLOCKED_CI_FAILING = "ci_failing"
_BLOCKED_MISSING_REVIEWS = "missing_reviews"
_BLOCKED_OTHER = "other"


def _classify_blocked(pr_state: PRState) -> str:
    """Classify a ``BLOCKED`` mergeStateStatus into a subtype.

    Priority (per #353 plan):
    1. ``reviewDecision == "REVIEW_REQUIRED"`` → missing_reviews
    2. any status check entry with conclusion ``FAILURE`` → ci_failing
    3. otherwise → other

    ``pr_state.ci_conclusion`` carries the already-aggregated verdict from
    :func:`parse_pr_state`. It is set to ``"FAILURE"`` only when at least
    one ``statusCheckRollup`` entry reports ``conclusion == "FAILURE"``.
    """
    if (pr_state.review_decision or "").upper() == "REVIEW_REQUIRED":
        return _BLOCKED_MISSING_REVIEWS
    if (pr_state.ci_conclusion or "").upper() == "FAILURE":
        return _BLOCKED_CI_FAILING
    return _BLOCKED_OTHER


def decide(
    mode: str,
    verdict_passed: bool,
    pr_state: PRState | None,
    pr: str,
) -> AutoMergeOutcome:
    """Map ``(mode, verdict, pr_state)`` to an :class:`AutoMergeOutcome`.

    Truth table is documented in issue #353 and mirrored in
    ``tests/test_auto_merge.py``. Kept deterministic: any unknown or
    unexpected mergeStateStatus collapses to ``skip`` rather than guessing
    an action.

    Args:
        mode: One of ``never``, ``on-review-pass``,
            ``on-review-pass-and-ci-green``. Unknown modes collapse to
            ``skip``.
        verdict_passed: Whether the internal antfarm review verdict passed.
        pr_state: Freshly fetched PR mergeability snapshot, or ``None`` when
            the lookup failed.
        pr: PR identifier (URL or number). Echoed back on the outcome.
    """
    if mode == "never":
        return AutoMergeOutcome(action="skip", pr=pr, mode=mode, reason="auto_merge disabled")
    if not verdict_passed:
        return AutoMergeOutcome(action="skip", pr=pr, mode=mode, reason="internal verdict not pass")
    if pr_state is None:
        return AutoMergeOutcome(action="skip", pr=pr, mode=mode, reason="pr state unavailable")

    status = (pr_state.mergeStateStatus or "").upper()

    if mode == "on-review-pass":
        if status == "CLEAN":
            return AutoMergeOutcome(action="merge", pr=pr, mode=mode, reason="CLEAN")
        if status == "UNSTABLE":
            # CI-agnostic mode: proceed despite failing/pending checks.
            return AutoMergeOutcome(action="merge", pr=pr, mode=mode, reason="UNSTABLE ci-agnostic")
        if status in ("DIRTY", "BEHIND"):
            return AutoMergeOutcome(
                action="rebase", pr=pr, mode=mode, reason=f"{status} needs rebase"
            )
        if status == "BLOCKED":
            subtype = _classify_blocked(pr_state)
            if subtype == _BLOCKED_CI_FAILING:
                return AutoMergeOutcome(
                    action="kickback_ci", pr=pr, mode=mode, reason="BLOCKED ci_failing"
                )
            if subtype == _BLOCKED_MISSING_REVIEWS:
                return AutoMergeOutcome(
                    action="pause_mission",
                    pr=pr,
                    mode=mode,
                    reason="BLOCKED missing_reviews",
                )
            return AutoMergeOutcome(
                action="pause_mission",
                pr=pr,
                mode=mode,
                reason="BLOCKED other",
            )
        # HAS_HOOKS, UNKNOWN, or anything else → conservative skip.
        return AutoMergeOutcome(
            action="skip", pr=pr, mode=mode, reason=f"unhandled mergeStateStatus={status}"
        )

    if mode == "on-review-pass-and-ci-green":
        if status == "CLEAN":
            return AutoMergeOutcome(action="merge", pr=pr, mode=mode, reason="CLEAN")
        if status == "UNSTABLE":
            return AutoMergeOutcome(
                action="wait_ci", pr=pr, mode=mode, reason="UNSTABLE ci still pending"
            )
        if status == "PENDING":
            return AutoMergeOutcome(action="wait_ci", pr=pr, mode=mode, reason="PENDING")
        if status in ("DIRTY", "BEHIND"):
            return AutoMergeOutcome(
                action="rebase", pr=pr, mode=mode, reason=f"{status} needs rebase"
            )
        if status == "BLOCKED":
            subtype = _classify_blocked(pr_state)
            if subtype == _BLOCKED_CI_FAILING:
                return AutoMergeOutcome(
                    action="kickback_ci", pr=pr, mode=mode, reason="BLOCKED ci_failing"
                )
            if subtype == _BLOCKED_MISSING_REVIEWS:
                return AutoMergeOutcome(
                    action="pause_mission",
                    pr=pr,
                    mode=mode,
                    reason="BLOCKED missing_reviews",
                )
            return AutoMergeOutcome(
                action="pause_mission",
                pr=pr,
                mode=mode,
                reason="BLOCKED other",
            )
        return AutoMergeOutcome(
            action="skip", pr=pr, mode=mode, reason=f"unhandled mergeStateStatus={status}"
        )

    # Unknown mode — stay conservative.
    return AutoMergeOutcome(action="skip", pr=pr, mode=mode, reason=f"unknown mode={mode}")


def parse_pr_state(gh_json_output: str) -> PRState | None:
    """Parse ``gh pr view --json mergeStateStatus,mergeable,reviewDecision,statusCheckRollup``.

    Returns ``None`` when:
    - the payload is empty / whitespace
    - the payload is not valid JSON
    - the top-level payload is not a JSON object

    Missing fields are tolerated and coerced to empty strings so downstream
    :func:`decide` sees a uniform shape. ``statusCheckRollup`` aggregates to
    a single ``ci_conclusion`` using the following precedence (most severe
    wins):

    - any entry with ``conclusion == "FAILURE"`` → ``"FAILURE"``
    - else any entry with ``conclusion == "PENDING"`` (or blank while
      ``status == "IN_PROGRESS"`` / ``"QUEUED"``) → ``"PENDING"``
    - else if every entry is ``SUCCESS`` → ``"SUCCESS"``
    - else → ``None``
    """
    if not gh_json_output or not gh_json_output.strip():
        return None
    try:
        data = json.loads(gh_json_output)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    rollup = data.get("statusCheckRollup") or []
    ci_conclusion: str | None = _aggregate_ci(rollup) if isinstance(rollup, list) else None

    return PRState(
        mergeStateStatus=str(data.get("mergeStateStatus") or ""),
        mergeable=str(data.get("mergeable") or ""),
        review_decision=str(data.get("reviewDecision") or ""),
        ci_conclusion=ci_conclusion,
    )


def _aggregate_ci(rollup: list) -> str | None:
    """Reduce ``statusCheckRollup`` to a single overall conclusion.

    See :func:`parse_pr_state` docstring for precedence rules.
    """
    if not rollup:
        return None
    saw_pending = False
    saw_success = False
    for entry in rollup:
        if not isinstance(entry, dict):
            continue
        conclusion = str(entry.get("conclusion") or "").upper()
        status = str(entry.get("status") or "").upper()
        if conclusion == "FAILURE":
            return "FAILURE"
        if conclusion == "PENDING" or status in ("IN_PROGRESS", "QUEUED"):
            saw_pending = True
            continue
        if conclusion == "SUCCESS":
            saw_success = True
    if saw_pending:
        return "PENDING"
    if saw_success:
        return "SUCCESS"
    return None
