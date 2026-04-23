"""Colony-level warning detection for Antfarm.

Pure-function module. No side effects, no I/O. Receives pre-fetched lists of
tasks and workers and returns structured warning dicts suitable for the
/status response and operator-facing UIs.
"""

from __future__ import annotations

from datetime import datetime


def detect_no_reviewer_capacity(tasks: list[dict], workers: list[dict]) -> dict | None:
    """Return a warning dict if ready review tasks exist but no worker has the review capability.

    Args:
        tasks: List of task dicts (as returned by backend.list_tasks()).
        workers: List of worker dicts (as returned by backend.list_workers()).

    Returns:
        Dict with keys ``code``, ``message``, ``hint``, ``count`` if the
        condition is detected, or ``None`` when capacity is fine.
    """
    ready_review_count = sum(
        1
        for t in tasks
        if t.get("status") == "ready" and "review" in t.get("capabilities_required", [])
    )
    if ready_review_count == 0:
        return None

    reviewer_count = sum(1 for w in workers if "review" in w.get("capabilities", []))
    if reviewer_count > 0:
        return None

    return {
        "code": "no_reviewer_capacity",
        "message": (
            f"{ready_review_count} review task(s) ready but no worker has 'review' capability"
        ),
        "hint": "Start one: antfarm worker start --agent reviewer",
        "count": ready_review_count,
    }


def _has_merged_attempt(task: dict) -> bool:
    return any(attempt.get("status") == "merged" for attempt in task.get("attempts", []))


def _get_current_verdict(task: dict) -> dict | None:
    current_id = task.get("current_attempt")
    if not current_id:
        return None
    for attempt in task.get("attempts", []):
        if attempt.get("attempt_id") == current_id:
            return attempt.get("review_verdict")
    return None


def _is_infra_task(task: dict) -> bool:
    caps = task.get("capabilities_required", []) or []
    return "plan" in caps or "review" in caps or task.get("id", "").startswith("review-")


def _count_awaiting_review(tasks: list[dict]) -> int:
    """Count tasks awaiting review right now.

    Matches TUI's ``awaiting_review`` bucket (``tui.py:410-424``) plus review
    tasks still sitting in ``ready`` (not yet picked up by a reviewer):

      * status ``done``, not cancelled, not merged, not an infra container,
        with no passing review verdict on the current attempt.
      * status ``ready`` with ``review`` in ``capabilities_required`` or an
        ``id`` starting with ``review-``.
    """
    count = 0
    for task in tasks:
        status = task.get("status")
        if status == "ready":
            caps = task.get("capabilities_required", []) or []
            if "review" in caps or task.get("id", "").startswith("review-"):
                count += 1
            continue
        if status != "done":
            continue
        if task.get("cancelled_at"):
            continue
        if _has_merged_attempt(task):
            continue
        if _is_infra_task(task):
            continue
        verdict = _get_current_verdict(task)
        if verdict and verdict.get("verdict") == "pass":
            continue
        count += 1
    return count


def detect_review_queue_saturated(
    tasks: list[dict],
    max_reviewers: int,
    awaiting_first_seen_at: str | None,
    now: datetime,
    dwell_seconds: float = 120.0,
) -> dict | None:
    """Return a warning if the review queue has been saturated for ``dwell_seconds``.

    Saturation fires when ``awaiting_review_count > max_reviewers * 2``.
    The caller is responsible for persisting ``awaiting_first_seen_at`` across
    ticks; this function is pure. When ``awaiting_first_seen_at`` is None,
    saturation is treated as "just observed" and no warning is returned yet —
    the caller should record ``now`` and try again on the next tick.

    See issue #347.
    """
    awaiting_review_count = _count_awaiting_review(tasks)
    threshold = max_reviewers * 2
    if awaiting_review_count <= threshold:
        return None
    if awaiting_first_seen_at is None:
        return None
    try:
        first_seen = datetime.fromisoformat(awaiting_first_seen_at)
    except ValueError:
        return None
    elapsed = (now - first_seen).total_seconds()
    if elapsed < dwell_seconds:
        return None
    return {
        "code": "review_queue_saturated",
        "message": (
            f"{awaiting_review_count} task(s) awaiting review with max_reviewers="
            f"{max_reviewers} (threshold {threshold}); review capacity is saturated."
        ),
        "hint": "Increase --max-reviewers or reduce --max-builders. See issue #347.",
        "count": awaiting_review_count,
    }
