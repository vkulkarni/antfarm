"""Colony-level warning detection for Antfarm.

Pure-function module. No side effects, no I/O. Receives pre-fetched lists of
tasks and workers and returns structured warning dicts suitable for the
/status response and operator-facing UIs.
"""

from __future__ import annotations


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
