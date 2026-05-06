"""Human-readable formatter for ``scout --watch`` SSE event lines.

Pure module — no httpx, no click handlers, no IO. Each function takes a raw
event dict (as produced by ``serve._emit_event``) and returns either a
formatted string, a parsed sub-dict, or a boolean classification.

Public surface:

* :data:`NON_WORKER_ACTORS`  — actors that are subsystems (not worker_ids).
* :data:`SUBSYSTEM_COLORS`   — color per subsystem actor.
* :data:`WORKER_PALETTE`     — deterministic palette (mirror of
  :class:`antfarm.core.tui.AntfarmTUI` ``_WORKER_PALETTE``).
* :data:`LOW_SIGNAL_TYPES`   — event types suppressed unless ``--verbose``.
* :func:`color_for_worker`   — palette pick by stable hash.
* :func:`palette_color`      — actor -> color (subsystem-aware).
* :func:`is_low_signal`      — True if event should be hidden by default.
* :func:`format_event_human` — single-line ``HH:MM:SS  actor  detail`` row.

The formatter is defensive: every event-type-specific helper is wrapped so
unknown shapes degrade gracefully to a generic ``type detail`` fallback.
"""

from __future__ import annotations

from datetime import datetime

import click

# Subsystem actors are *not* worker_ids; they get a fixed color rather than
# a hash-derived per-worker color (mirrors tui.AntfarmTUI._NON_WORKER_ACTORS).
NON_WORKER_ACTORS: frozenset[str] = frozenset(
    {"colony", "queen", "autoscaler", "soldier", "doctor"}
)

# Per-subsystem coloring. ``autoscaler`` uses ``bright_black`` so its noisy
# spawn/retire chatter is visually de-emphasised vs the more interesting
# soldier/queen/doctor lines.
SUBSYSTEM_COLORS: dict[str, str] = {
    "soldier": "yellow",
    "doctor": "green",
    "queen": "blue",
    "autoscaler": "bright_black",
    "colony": "white",
}

# Deterministic palette, same order as tui.AntfarmTUI._WORKER_PALETTE so that
# a worker shows the same color in the TUI dashboard and in ``scout --watch``.
WORKER_PALETTE: tuple[str, ...] = (
    "cyan",
    "magenta",
    "green",
    "yellow",
    "blue",
    "bright_cyan",
    "bright_magenta",
    "bright_green",
)

# worker_activity actions and event types that fire on every poll loop tick.
# Hidden by default so the live feed surfaces only meaningful state changes.
# Pass ``-v`` / ``--verbose`` to ``scout --watch`` to include them.
_HEARTBEAT_VERBS: frozenset[str] = frozenset(
    {"heartbeat", "polling", "idle", "scanning", "cleanup"}
)
LOW_SIGNAL_TYPES: frozenset[str] = frozenset(
    {
        "heartbeat",
        "scanning",
        "idle",
        "polling",
        "cleanup",
        # Autoscaler spawn/retire events are infrastructure noise during
        # short missions / idle periods (#385). Verbose mode keeps them.
        "worker_spawned",
        "worker_retired",
    }
)


# ---------------------------------------------------------------------------
# Color selection
# ---------------------------------------------------------------------------


def color_for_worker(actor: str) -> str:
    """Return a deterministic palette color for an actor string.

    Hash-mod-N over the actor — same actor maps to the same color every
    time, across processes (stable, not Python's salted ``hash()``).
    Mirrors :meth:`antfarm.core.tui.AntfarmTUI._color_for_worker`.
    """
    if not actor:
        return WORKER_PALETTE[0]
    h = sum(ord(c) for c in actor)
    return WORKER_PALETTE[h % len(WORKER_PALETTE)]


def palette_color(actor: str) -> str:
    """Return the color to use for ``actor`` in the formatted event row.

    Subsystem actors (``soldier``, ``doctor``, ``queen``, ``autoscaler``,
    ``colony``) get their fixed :data:`SUBSYSTEM_COLORS` entry. Anything
    else is treated as a worker_id and colored via
    :func:`color_for_worker`.
    """
    if actor in NON_WORKER_ACTORS:
        return SUBSYSTEM_COLORS.get(actor, "white")
    return color_for_worker(actor)


# ---------------------------------------------------------------------------
# Low-signal classifier
# ---------------------------------------------------------------------------


def is_low_signal(event: dict) -> bool:
    """Return True if ``event`` is heartbeat-grade noise.

    An event is low-signal if either:

    * Its ``type`` is in :data:`LOW_SIGNAL_TYPES`, OR
    * Its ``type`` is ``"worker_activity"`` and its ``data.action`` is one
      of the heartbeat verbs (``polling``, ``idle``, ``scanning``,
      ``cleanup``, ``heartbeat``).

    Default scout-watch behavior hides these. ``--verbose`` shows them.
    """
    event_type = event.get("type") or ""
    if event_type in LOW_SIGNAL_TYPES:
        return True
    if event_type == "worker_activity":
        data = event.get("data") or {}
        action = (data.get("action") or "").strip()
        if action in _HEARTBEAT_VERBS:
            return True
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kv(detail: str) -> dict[str, str]:
    """Parse ``key=value key=value`` strings into a dict.

    Whitespace-delimited. Tokens missing ``=`` are dropped. Defensive:
    never raises on weird input — just returns whatever it could parse.
    """
    out: dict[str, str] = {}
    if not detail:
        return out
    for token in detail.split():
        if "=" not in token:
            continue
        k, _, v = token.partition("=")
        if k:
            out[k] = v
    return out


def _format_timestamp(ts: str) -> str:
    """Return ``HH:MM:SS`` from an ISO 8601 timestamp.

    Falls back to ``--:--:--`` for empty / malformed values.
    """
    if not ts:
        return "--:--:--"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.astimezone().strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return "--:--:--"


def _short_task(task_id: str) -> str:
    """Shorten a task id for the activity feed (last segment after ``-``)."""
    if not task_id:
        return ""
    # task-mission-slug-01 → task-mission-slug-01 (we do not abbreviate task
    # ids in the watch feed; the field is rendered inline in the detail).
    return task_id


# ---------------------------------------------------------------------------
# Per-event-type formatters
#
# Each helper returns the *detail* portion only (no timestamp, no actor).
# Wrap parsing in defensive try/except — unknown shapes fall through to the
# generic fallback in ``format_event_human``.
# ---------------------------------------------------------------------------


def _fmt_worker_activity(event: dict) -> str:
    """worker_activity already carries a synthesized line in ``detail``."""
    return event.get("detail") or "-"


def _fmt_harvested(event: dict) -> str:
    """``harvested task-1 → PR 42`` (branch dropped — PR number is enough)."""
    task_id = event.get("task_id") or ""
    fields = _kv(event.get("detail") or "")
    pr = fields.get("pr", "")
    pr_part = f" → PR {pr}" if pr else ""
    if task_id:
        return f"harvested {task_id}{pr_part}"
    return f"harvested{pr_part}"


def _fmt_merged(event: dict) -> str:
    """``merged task-1 (auto)`` — adds ``(auto)`` for auto-merge events."""
    task_id = event.get("task_id") or ""
    fields = _kv(event.get("detail") or "")
    auto = fields.get("auto_merged") == "1" or fields.get("mode") in {"auto", "squash"}
    suffix = " (auto)" if auto else ""
    if task_id:
        return f"merged {task_id}{suffix}"
    return f"merged{suffix}"


def _fmt_kickback(event: dict) -> str:
    """``kickback task-1: <reason>`` — trims the reason to a single line."""
    task_id = event.get("task_id") or ""
    reason = (event.get("detail") or "").splitlines()[0] if event.get("detail") else ""
    if task_id and reason:
        return f"kickback {task_id}: {reason}"
    if task_id:
        return f"kickback {task_id}"
    return f"kickback {reason}".strip()


def _fmt_auto_merged(event: dict) -> str:
    """``auto-merged task-1 → PR 42 (mode=squash)``."""
    task_id = event.get("task_id") or ""
    fields = _kv(event.get("detail") or "")
    pr = fields.get("pr", "")
    mode = fields.get("mode", "")
    pr_part = f" → PR {pr}" if pr else ""
    mode_part = f" (mode={mode})" if mode else ""
    return f"auto-merged {task_id}{pr_part}{mode_part}".strip()


def _fmt_auto_merge_rebasing(event: dict) -> str:
    """``rebasing task-1 PR 42 — <reason>``."""
    task_id = event.get("task_id") or ""
    fields = _kv(event.get("detail") or "")
    pr = fields.get("pr", "")
    reason = fields.get("reason", "")
    pr_part = f" PR {pr}" if pr else ""
    reason_part = f" — {reason}" if reason else ""
    return f"rebasing {task_id}{pr_part}{reason_part}".strip()


def _fmt_auto_merge_waiting_ci(event: dict) -> str:
    """``waiting on CI for task-1 PR 42``."""
    task_id = event.get("task_id") or ""
    fields = _kv(event.get("detail") or "")
    pr = fields.get("pr", "")
    pr_part = f" PR {pr}" if pr else ""
    return f"waiting on CI for {task_id}{pr_part}".strip()


def _fmt_auto_merge_kickback(event: dict) -> str:
    """``auto-merge kickback task-1 PR 42 — <reason>``."""
    task_id = event.get("task_id") or ""
    fields = _kv(event.get("detail") or "")
    pr = fields.get("pr", "")
    reason = fields.get("reason", "")
    pr_part = f" PR {pr}" if pr else ""
    reason_part = f" — {reason}" if reason else ""
    return f"auto-merge kickback {task_id}{pr_part}{reason_part}".strip()


def _fmt_repo_dirty(event: dict) -> str:
    """``repo dirty (task-1): <detail>``."""
    task_id = event.get("task_id") or ""
    detail = event.get("detail") or ""
    if task_id and detail:
        return f"repo dirty ({task_id}): {detail}"
    if task_id:
        return f"repo dirty ({task_id})"
    return f"repo dirty: {detail}".strip()


def _fmt_merge_failed(event: dict) -> str:
    """``merge failed task-1 — <reason>``."""
    task_id = event.get("task_id") or ""
    fields = _kv(event.get("detail") or "")
    reason = fields.get("reason") or (event.get("detail") or "")
    reason_part = f" — {reason}" if reason else ""
    return f"merge failed {task_id}{reason_part}".strip()


def _fmt_merge_succeeded(event: dict) -> str:
    """``merge succeeded task-1 (<branch>)``."""
    task_id = event.get("task_id") or ""
    detail = event.get("detail") or ""
    branch_part = f" ({detail})" if detail else ""
    return f"merge succeeded {task_id}{branch_part}".strip()


def _fmt_reconciled_external(event: dict) -> str:
    """``reconciled external merge: task-1 PR 42``."""
    task_id = event.get("task_id") or ""
    fields = _kv(event.get("detail") or "")
    pr = fields.get("pr", "")
    pr_part = f" PR {pr}" if pr else ""
    return f"reconciled external merge: {task_id}{pr_part}".strip()


def _fmt_worker_spawned(event: dict) -> str:
    """``spawned worker (role=builder name=builder-1)``."""
    detail = event.get("detail") or ""
    return f"spawned worker ({detail})".strip() if detail else "spawned worker"


def _fmt_worker_retired(event: dict) -> str:
    """``retired worker (role=builder name=builder-1)``."""
    detail = event.get("detail") or ""
    return f"retired worker ({detail})".strip() if detail else "retired worker"


def _fmt_worktree_pruned(event: dict) -> str:
    """``pruned worktree: <path>``."""
    detail = event.get("detail") or ""
    return f"pruned worktree: {detail}".strip() if detail else "pruned worktree"


def _fmt_worktree_reclaimed(event: dict) -> str:
    """``reclaimed worktree: <path>``."""
    fields = _kv(event.get("detail") or "")
    path = fields.get("path") or (event.get("detail") or "")
    return f"reclaimed worktree: {path}".strip() if path else "reclaimed worktree"


def _fmt_mission_complete(event: dict) -> str:
    """``mission complete: <id>``."""
    fields = _kv(event.get("detail") or "")
    mid = fields.get("mission") or (event.get("detail") or "")
    return f"mission complete: {mid}".strip() if mid else "mission complete"


def _fmt_mission_failed(event: dict) -> str:
    """``mission failed: <id> — <reason>``."""
    fields = _kv(event.get("detail") or "")
    mid = fields.get("mission") or ""
    reason = fields.get("reason") or ""
    if mid and reason:
        return f"mission failed: {mid} — {reason}"
    if mid:
        return f"mission failed: {mid}"
    return f"mission failed: {event.get('detail') or ''}".strip()


def _fmt_mission_budget_exceeded(event: dict) -> str:
    """``mission budget exceeded: <id> action=<pause|cancel>``."""
    fields = _kv(event.get("detail") or "")
    mid = fields.get("mission") or ""
    action = fields.get("action") or ""
    if mid and action:
        return f"mission budget exceeded: {mid} ({action})"
    if mid:
        return f"mission budget exceeded: {mid}"
    return "mission budget exceeded"


def _fmt_plan_approved(event: dict) -> str:
    """``plan approved: <mission> (<n> tasks)``."""
    fields = _kv(event.get("detail") or "")
    mid = fields.get("mission") or ""
    tasks = fields.get("tasks") or ""
    if mid and tasks:
        return f"plan approved: {mid} ({tasks} tasks)"
    if mid:
        return f"plan approved: {mid}"
    return "plan approved"


def _fmt_plan_ready(event: dict) -> str:
    """``plan ready: <mission>`` — defensive (event type may not yet exist)."""
    fields = _kv(event.get("detail") or "")
    mid = fields.get("mission") or (event.get("detail") or "")
    return f"plan ready: {mid}".strip() if mid else "plan ready"


def _fmt_retry_pattern(event: dict) -> str:
    """``retry pattern detected: <detail>`` — defensive."""
    detail = event.get("detail") or ""
    return f"retry pattern: {detail}".strip() if detail else "retry pattern detected"


def _fmt_fallback(event: dict) -> str:
    """``fallback: <detail>`` — defensive."""
    detail = event.get("detail") or ""
    return f"fallback: {detail}".strip() if detail else "fallback"


# Dispatch table. Keep ordering stable for readability.
_FORMATTERS: dict[str, callable] = {
    "worker_activity": _fmt_worker_activity,
    "harvested": _fmt_harvested,
    "merged": _fmt_merged,
    "kickback": _fmt_kickback,
    "auto_merged": _fmt_auto_merged,
    "auto_merge_rebasing": _fmt_auto_merge_rebasing,
    "auto_merge_waiting_ci": _fmt_auto_merge_waiting_ci,
    "auto_merge_kickback": _fmt_auto_merge_kickback,
    "repo_dirty": _fmt_repo_dirty,
    "merge_failed": _fmt_merge_failed,
    "merge_succeeded": _fmt_merge_succeeded,
    "reconciled_external": _fmt_reconciled_external,
    "worker_spawned": _fmt_worker_spawned,
    "worker_retired": _fmt_worker_retired,
    "worktree_pruned": _fmt_worktree_pruned,
    "worktree_reclaimed": _fmt_worktree_reclaimed,
    "mission_complete": _fmt_mission_complete,
    "mission_failed": _fmt_mission_failed,
    "mission_budget_exceeded": _fmt_mission_budget_exceeded,
    "plan_approved": _fmt_plan_approved,
    "plan_ready": _fmt_plan_ready,
    "retry_pattern": _fmt_retry_pattern,
    "fallback": _fmt_fallback,
}


def _fmt_unknown(event: dict) -> str:
    """Fallback for unknown event types: ``<type> <task_id> <detail>``."""
    event_type = event.get("type") or "-"
    task_id = event.get("task_id") or ""
    detail = event.get("detail") or ""
    parts = [event_type]
    if task_id:
        parts.append(task_id)
    if detail:
        parts.append(detail)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def format_event_human(event: dict, *, use_color: bool = True) -> str:
    """Format a single SSE event dict as a one-line activity row.

    Layout: ``HH:MM:SS  actor  detail``

    Color rules (mirror ``tui.AntfarmTUI._render_activity``):

    * If the event type contains ``"failed"`` -> entire line red.
    * Else if the event type contains ``"kick"`` -> entire line yellow.
    * Else the actor column is colored via :func:`palette_color`.

    With ``use_color=False`` no ANSI escapes are emitted (used by tests
    that assert plain substrings; not currently exposed via the CLI).
    """
    time_part = _format_timestamp(event.get("ts") or "")

    actor_raw = str(event.get("actor") or event.get("type") or "")
    actor_col = actor_raw[:12].ljust(12)

    event_type = event.get("type") or ""

    formatter = _FORMATTERS.get(event_type, _fmt_unknown)
    try:
        detail = formatter(event)
    except Exception:
        detail = _fmt_unknown(event)

    if not use_color:
        return f"{time_part}  {actor_col}  {detail}"

    # Row-wide color overrides for failure / kickback events.
    if "failed" in event_type:
        return click.style(f"{time_part}  {actor_col}  {detail}", fg="red")
    if "kick" in event_type:
        return click.style(f"{time_part}  {actor_col}  {detail}", fg="yellow")

    actor_styled = click.style(actor_col, fg=palette_color(actor_raw))
    return f"{time_part}  {actor_styled}  {detail}"
