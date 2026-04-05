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


def collect_inbox_items(
    tasks: list[dict],
    workers: list[dict],
    *,
    stale_worker_ttl: float = 300.0,
    long_running_threshold: float = 3600.0,
) -> list[dict]:
    """Collect actionable items from colony state.

    Args:
        tasks: List of task dicts from the colony.
        workers: List of worker dicts from the colony.
        stale_worker_ttl: Seconds after which a worker heartbeat is stale.
        long_running_threshold: Seconds after which an active task is flagged.

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

        # --- Kicked-back tasks ---
        elif status == "kicked_back":
            trail = t.get("trail", [])
            reason = trail[-1].get("message", "unknown") if trail else "unknown"
            items.append({
                "severity": "info",
                "type": "kicked_back",
                "message": f"Task '{tid}' was kicked back: {reason[:100]}",
                "action": "Review rejection reason and requeue",
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
