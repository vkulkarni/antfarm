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

    ``ci_pending`` and ``ci_failing`` are summary flags derived from the
    ``statusCheckRollup`` in :func:`parse_pr_state`. They let
    :func:`decide` disambiguate ``UNSTABLE`` (#381) into "still pending"
    vs. "already failing terminally" without re-walking the rollup. They
    default to ``False`` so existing positional/test callers keep working.
    """

    mergeStateStatus: str
    mergeable: str
    review_decision: str
    ci_conclusion: str | None
    ci_pending: bool = False
    ci_failing: bool = False


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

    Precedence inside DIRTY/BEHIND (#365): GitHub keeps reporting
    ``mergeStateStatus=DIRTY`` (or BEHIND) even after a rebase resolves
    nothing — when ``mergeable=="CONFLICTING"`` the branch has real merge
    conflicts that no amount of rebasing will clear. Detect that case
    BEFORE dispatching ``rebase`` and route to ``kickback_ci`` so the worker
    fixes the conflict in a fresh attempt instead of looping the soldier
    on a doomed rebase.

    Args:
        mode: One of ``never``, ``on-review-pass``,
            ``on-review-pass-and-ci-green``, ``on-review-pass-and-local-tests``.
            Unknown modes collapse to ``skip``.
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

    if mode in ("on-review-pass", "on-review-pass-and-local-tests"):
        if status == "CLEAN":
            return AutoMergeOutcome(action="merge", pr=pr, mode=mode, reason="CLEAN")
        if status == "UNSTABLE":
            # CI-agnostic mode: proceed despite failing/pending checks.
            return AutoMergeOutcome(action="merge", pr=pr, mode=mode, reason="UNSTABLE ci-agnostic")
        if status in ("DIRTY", "BEHIND") and (pr_state.mergeable or "").upper() == "CONFLICTING":
            return AutoMergeOutcome(action="kickback_ci", pr=pr, mode=mode, reason="merge_conflict")
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
            # #381: UNSTABLE conflates "still pending" with "already failed".
            # Inspect the summarized rollup flags to decide which one we hit.
            if pr_state.ci_failing:
                return AutoMergeOutcome(
                    action="kickback_ci", pr=pr, mode=mode, reason="ci_check_failed"
                )
            if pr_state.ci_pending:
                return AutoMergeOutcome(
                    action="wait_ci", pr=pr, mode=mode, reason="UNSTABLE ci still pending"
                )
            # Defensive: rollup parse failed or pre-existing tests construct
            # PRState without the new flags. Wait rather than guess wrong.
            return AutoMergeOutcome(
                action="wait_ci", pr=pr, mode=mode, reason="UNSTABLE ci unknown, waiting"
            )
        if status == "PENDING":
            return AutoMergeOutcome(action="wait_ci", pr=pr, mode=mode, reason="PENDING")
        if status in ("DIRTY", "BEHIND") and (pr_state.mergeable or "").upper() == "CONFLICTING":
            return AutoMergeOutcome(action="kickback_ci", pr=pr, mode=mode, reason="merge_conflict")
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
    if isinstance(rollup, list):
        ci_conclusion, ci_pending, ci_failing = _summarize_rollup(rollup)
    else:
        ci_conclusion, ci_pending, ci_failing = None, False, False

    return PRState(
        mergeStateStatus=str(data.get("mergeStateStatus") or ""),
        mergeable=str(data.get("mergeable") or ""),
        review_decision=str(data.get("reviewDecision") or ""),
        ci_conclusion=ci_conclusion,
        ci_pending=ci_pending,
        ci_failing=ci_failing,
    )


def _summarize_rollup(rollup: list) -> tuple[str | None, bool, bool]:
    """Reduce ``statusCheckRollup`` to ``(ci_conclusion, ci_pending, ci_failing)``.

    Single pass over the rollup so :func:`decide` can both pick a coarse
    aggregate conclusion and ALSO see whether any individual check is still
    in flight or has terminally failed (#381).

    Per-entry classification:

    - ``conclusion`` in ``{FAILURE, TIMED_OUT}`` — terminal failure → set
      ``saw_failing``.
    - ``conclusion == "CANCELLED"`` — operators commonly re-trigger
      cancelled runs, so treat as transient pending rather than terminal
      failure.
    - ``conclusion == "PENDING"`` OR ``status`` in
      ``{IN_PROGRESS, QUEUED}`` — still in flight → set ``saw_pending``.
    - ``conclusion`` in ``{SUCCESS, NEUTRAL, SKIPPED}`` — counted as
      success; does not set the failing/pending flags.

    Aggregate ``ci_conclusion`` precedence (most severe wins):

    1. any failing → ``"FAILURE"``
    2. else any pending → ``"PENDING"``
    3. else any success → ``"SUCCESS"``
    4. else → ``None``
    """
    if not rollup:
        return None, False, False
    saw_failing = False
    saw_pending = False
    saw_success = False
    for entry in rollup:
        if not isinstance(entry, dict):
            continue
        conclusion = str(entry.get("conclusion") or "").upper()
        status = str(entry.get("status") or "").upper()
        if conclusion in ("FAILURE", "TIMED_OUT"):
            saw_failing = True
            continue
        if conclusion == "CANCELLED":
            saw_pending = True
            continue
        if conclusion == "PENDING" or status in ("IN_PROGRESS", "QUEUED"):
            saw_pending = True
            continue
        if conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            saw_success = True

    if saw_failing:
        ci_conclusion: str | None = "FAILURE"
    elif saw_pending:
        ci_conclusion = "PENDING"
    elif saw_success:
        ci_conclusion = "SUCCESS"
    else:
        ci_conclusion = None
    return ci_conclusion, saw_pending, saw_failing
