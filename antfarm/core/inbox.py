"""Inbox data collection — surfaces items needing operator attention.

Reusable by both the CLI (`antfarm inbox`) and the TUI dashboard.
Each item explains: what happened, why, and what to do.
"""

from __future__ import annotations

from datetime import UTC, datetime


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO 8601 timestamp, returning None on failure."""
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _age_seconds(ts: str) -> float:
    """Return age in seconds from an ISO timestamp to now."""
    dt = _parse_iso(ts)
    if dt is None:
        return 0.0
    return (datetime.now(UTC) - dt).total_seconds()


def _is_infra_task_id(task_id: str) -> bool:
    """Return True if the task id identifies plan/review infrastructure work."""
    return task_id.startswith(("plan-", "review-", "review-plan-"))


def _last_failure_reason(trail: list[dict]) -> str:
    """Extract the most recent kickback reason, else the last trail message."""
    for entry in reversed(trail):
        if entry.get("action_type") == "kickback":
            msg = entry.get("message", "")
            if msg:
                return msg
    if trail:
        return trail[-1].get("message", "") or "unknown"
    return "unknown"


def collect_inbox_items(
    tasks: list[dict],
    workers: list[dict],
    *,
    stale_worker_ttl: float = 300.0,
    long_running_threshold: float = 3600.0,
    max_attempts_default: int = 3,
) -> list[dict]:
    """Collect actionable items from colony state.

    Args:
        tasks: List of task dicts from the colony.
        workers: List of worker dicts from the colony.
        stale_worker_ttl: Seconds after which a worker heartbeat is stale.
        long_running_threshold: Seconds after which an active task is flagged.
        max_attempts_default: Default attempt ceiling when a task has no
            ``max_attempts`` override. Finished attempts (DONE or SUPERSEDED)
            are counted against this value.

    Returns:
        List of inbox item dicts with keys:
        - severity: "error", "warning", "info"
        - type: category string
        - message: human-readable explanation
        - action: recommended action
        - task_id or worker_id: relevant entity
    """
    items: list[dict] = []

    # --- Stale workers ---
    live_worker_ids: set[str] = set()
    for w in workers:
        wid = w.get("worker_id", "")
        hb = w.get("last_heartbeat", "")
        age = _age_seconds(hb)
        if age > stale_worker_ttl:
            items.append({
                "severity": "error",
                "type": "stale_worker",
                "message": (
                    f"Worker '{wid}' last heartbeat {int(age)}s ago "
                    f"(TTL={int(stale_worker_ttl)}s)"
                ),
                "action": f"Run: antfarm doctor --fix (deregisters stale worker '{wid}')",
                "worker_id": wid,
            })
        else:
            live_worker_ids.add(wid)

    # Build done/merged sets for dep checking
    done_task_ids: set[str] = set()
    merged_task_ids: set[str] = set()
    for t in tasks:
        tid = t.get("id", "")
        status = t.get("status", "")
        if status == "done":
            done_task_ids.add(tid)
            # Check if any attempt is merged
            for a in t.get("attempts", []):
                if a.get("status") == "merged":
                    merged_task_ids.add(tid)
                    break
        elif status == "merged":
            merged_task_ids.add(tid)

    for t in tasks:
        tid = t.get("id", "")
        status = t.get("status", "")

        # --- Failed tasks ---
        if status == "failed":
            # Check trail for failure type
            trail = t.get("trail", [])
            last_msg = trail[-1].get("message", "") if trail else "unknown"
            items.append({
                "severity": "error",
                "type": "failed_task",
                "message": f"Task '{tid}' failed: {last_msg[:100]}",
                "action": "Review failure, fix, and requeue or escalate",
                "task_id": tid,
            })

        # --- Harvest-pending tasks (interrupted harvest) ---
        elif status == "harvest_pending":
            items.append({
                "severity": "error",
                "type": "harvest_interrupted",
                "message": (
                    f"Task '{tid}' stuck in harvest_pending "
                    "(worker may have died mid-harvest)"
                ),
                "action": "Run: antfarm doctor --fix or manually retry harvest",
                "task_id": tid,
            })

        # --- Blocked tasks with unmet deps ---
        elif status in ("blocked", "ready"):
            deps = t.get("depends_on", [])
            unmet = [d for d in deps if d not in done_task_ids and d not in merged_task_ids]
            if unmet:
                items.append({
                    "severity": "warning",
                    "type": "blocked_by_deps",
                    "message": f"Task '{tid}' blocked by unmet deps: {', '.join(unmet)}",
                    "action": f"Complete or unblock: {', '.join(unmet)}",
                    "task_id": tid,
                })

        # --- Active tasks running too long ---
        elif status == "active":
            for a in t.get("attempts", []):
                if a.get("attempt_id") == t.get("current_attempt"):
                    started = a.get("started_at", "")
                    duration = _age_seconds(started)
                    if duration > long_running_threshold:
                        items.append({
                            "severity": "warning",
                            "type": "long_running",
                            "message": (
                                f"Task '{tid}' active for {int(duration / 60)}min "
                                f"(worker: {a.get('worker_id', '?')})"
                            ),
                            "action": "Check worker health or pause task",
                            "task_id": tid,
                        })
                    break

        # --- Kicked-back tasks (detected from trail on ready tasks) ---
        # FileBackend moves kicked-back tasks to ready/ with status "ready",
        # so we detect kickbacks by checking trail entries with superseded attempts.
        if status == "ready":
            trail = t.get("trail", [])
            # A ready task with a superseded attempt was kicked back
            has_superseded = any(
                a.get("status") == "superseded" for a in t.get("attempts", [])
            )
            if has_superseded and trail:
                reason = trail[-1].get("message", "unknown")
                items.append({
                    "severity": "info",
                    "type": "kicked_back",
                    "message": f"Task '{tid}' was kicked back: {reason[:100]}",
                    "action": "Review rejection reason and requeue",
                    "task_id": tid,
                })

        # --- Retry-pattern failures ---
        # Count finished (DONE or SUPERSEDED) attempts and compare to the
        # effective max_attempts budget. Blocked tasks at the ceiling become
        # errors (retry_ceiling); non-blocked tasks one attempt away from the
        # ceiling become warnings (retrying). Infra tasks (plan/review) are
        # skipped — they're handled by their own lifecycle.
        if not _is_infra_task_id(tid):
            attempts = t.get("attempts", [])
            finished = sum(
                1 for a in attempts if a.get("status") in ("done", "superseded")
            )
            effective_max = t.get("max_attempts") or max_attempts_default
            trail = t.get("trail", [])
            if finished >= effective_max and status == "blocked":
                reason = _last_failure_reason(trail)
                items.append({
                    "severity": "error",
                    "type": "retry_ceiling",
                    "message": (
                        f"Task '{tid}' has failed {finished}/{effective_max} "
                        f"attempts. Last failure: {reason[:120]}"
                    ),
                    "action": (
                        "Task is at the retry ceiling. Inspect trail or "
                        f"unblock via: antfarm kickback {tid}"
                    ),
                    "task_id": tid,
                })
            elif (
                finished >= effective_max - 1
                and finished > 0
                and status != "blocked"
            ):
                reason = _last_failure_reason(trail)
                items.append({
                    "severity": "warning",
                    "type": "retrying",
                    "message": (
                        f"Task '{tid}' has failed {finished} of max "
                        f"{effective_max} attempts. Last failure: {reason[:120]}"
                    ),
                    "action": (
                        "Task may block the mission if the next attempt fails. "
                        "Inspect trail before it hits the retry ceiling."
                    ),
                    "task_id": tid,
                })

        # --- Tasks with signals (need human input) ---
        signals = t.get("signals", [])
        if signals:
            last_signal = signals[-1]
            items.append({
                "severity": "info",
                "type": "has_signal",
                "message": f"Task '{tid}' has signal: {last_signal.get('message', '')[:100]}",
                "action": "Review signal and take action",
                "task_id": tid,
            })

    # Sort: errors first, then warnings, then info
    severity_order = {"error": 0, "warning": 1, "info": 2}
    items.sort(key=lambda x: severity_order.get(x["severity"], 9))

    return items
