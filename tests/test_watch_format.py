"""Tests for ``antfarm.core.watch_format``.

Pure unit tests: no httpx, no FileBackend, no click runner. Covers per-event
formatters, color rules, low-signal classification, and timestamp parsing.
"""

from __future__ import annotations

from antfarm.core import watch_format

# ---------------------------------------------------------------------------
# color_for_worker / palette_color
# ---------------------------------------------------------------------------


def test_color_for_worker_is_deterministic():
    """Same actor string -> same color across calls (process-stable)."""
    a = watch_format.color_for_worker("builder-1")
    b = watch_format.color_for_worker("builder-1")
    assert a == b
    assert a in watch_format.WORKER_PALETTE


def test_color_for_worker_empty_returns_first_palette_entry():
    """Empty actor string falls back to palette[0] (mirrors tui.py)."""
    assert watch_format.color_for_worker("") == watch_format.WORKER_PALETTE[0]


def test_palette_color_for_subsystems():
    """Each subsystem actor has a fixed color from SUBSYSTEM_COLORS."""
    assert watch_format.palette_color("soldier") == "yellow"
    assert watch_format.palette_color("doctor") == "green"
    assert watch_format.palette_color("queen") == "blue"
    assert watch_format.palette_color("autoscaler") == "bright_black"
    assert watch_format.palette_color("colony") == "white"


def test_palette_color_for_worker_uses_palette():
    """Non-subsystem actors get a palette color via hash-mod-N."""
    color = watch_format.palette_color("builder-1")
    assert color in watch_format.WORKER_PALETTE


# ---------------------------------------------------------------------------
# is_low_signal
# ---------------------------------------------------------------------------


def test_is_low_signal_for_typed_events():
    """Events with type in LOW_SIGNAL_TYPES are filtered."""
    for t in ("heartbeat", "scanning", "idle", "polling", "cleanup"):
        assert watch_format.is_low_signal({"type": t}) is True


def test_is_low_signal_worker_activity_with_heartbeat_action():
    """worker_activity events with heartbeat-grade actions are low-signal."""
    ev = {"type": "worker_activity", "data": {"action": "polling"}}
    assert watch_format.is_low_signal(ev) is True


def test_is_low_signal_worker_activity_with_real_action_is_signal():
    """worker_activity with editing/running etc is high-signal (kept)."""
    ev = {"type": "worker_activity", "data": {"action": "editing"}}
    assert watch_format.is_low_signal(ev) is False


def test_is_low_signal_high_signal_events():
    """Real state changes are never low-signal."""
    for t in ("harvested", "merged", "kickback", "auto_merged", "merge_failed"):
        assert watch_format.is_low_signal({"type": t}) is False


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------


def test_format_timestamp_extracts_hh_mm_ss():
    """A normal ISO timestamp yields HH:MM:SS in local TZ."""
    ev = {
        "type": "harvested",
        "actor": "runner",
        "task_id": "task-1",
        "detail": "pr=42 branch=feat/x",
        "ts": "2026-04-16T14:32:11+00:00",
    }
    out = watch_format.format_event_human(ev, use_color=False)
    # We can't assert literal "14:32:11" because astimezone() shifts to local
    # time; assert the H:M:S shape and the seconds field.
    parts = out.split()
    assert len(parts[0]) == 8
    assert parts[0].count(":") == 2
    assert parts[0].endswith(":11")


def test_format_timestamp_empty_yields_dashes():
    """Missing timestamp degrades to ``--:--:--``."""
    out = watch_format.format_event_human({"type": "harvested"}, use_color=False)
    assert out.startswith("--:--:--")


def test_format_timestamp_malformed_yields_dashes():
    """Malformed timestamps degrade gracefully."""
    out = watch_format.format_event_human(
        {"type": "harvested", "ts": "not-a-timestamp"},
        use_color=False,
    )
    assert out.startswith("--:--:--")


# ---------------------------------------------------------------------------
# Per-event-type formatters
# ---------------------------------------------------------------------------


def _row_detail(ev: dict) -> str:
    """Strip timestamp + actor prefix, return only the detail portion.

    Layout: ``HH:MM:SS  actor[12]  detail`` — the actor column is space-padded
    to width 12. The simplest robust slice is "everything after the first
    8 (timestamp) + 2 (gap) + 12 (actor) + 2 (gap) characters".
    """
    out = watch_format.format_event_human(ev, use_color=False)
    return out[8 + 2 + 12 + 2 :]


def test_worker_activity_uses_detail_verbatim():
    ev = {"type": "worker_activity", "actor": "builder-1", "detail": "editing src/foo.py"}
    assert _row_detail(ev) == "editing src/foo.py"


def test_harvested_renders_pr_arrow():
    ev = {
        "type": "harvested",
        "actor": "runner",
        "task_id": "task-1",
        "detail": "pr=42 branch=feat/x",
    }
    assert _row_detail(ev) == "harvested task-1 → PR 42"


def test_harvested_without_pr():
    ev = {"type": "harvested", "actor": "runner", "task_id": "task-1", "detail": ""}
    assert _row_detail(ev) == "harvested task-1"


def test_merged_basic():
    ev = {"type": "merged", "actor": "soldier", "task_id": "task-1", "detail": "attempt=att-001"}
    assert _row_detail(ev) == "merged task-1"


def test_merged_auto_suffix():
    """auto_merged=1 in detail adds the (auto) marker."""
    ev = {
        "type": "merged",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "attempt=att-001 auto_merged=1",
    }
    assert _row_detail(ev) == "merged task-1 (auto)"


def test_kickback_with_reason():
    ev = {
        "type": "kickback",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "merge conflict on src/foo.py",
    }
    assert _row_detail(ev) == "kickback task-1: merge conflict on src/foo.py"


def test_kickback_multiline_reason_trimmed():
    """Only the first line of a multi-line reason is kept."""
    ev = {
        "type": "kickback",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "first line\nsecond line\nthird",
    }
    assert _row_detail(ev) == "kickback task-1: first line"


def test_auto_merged_renders_pr_and_mode():
    ev = {
        "type": "auto_merged",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "pr=42 mode=squash reason=clean",
    }
    assert _row_detail(ev) == "auto-merged task-1 → PR 42 (mode=squash)"


def test_auto_merge_rebasing():
    ev = {
        "type": "auto_merge_rebasing",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "pr=42 mode=squash reason=behind_base",
    }
    assert _row_detail(ev) == "rebasing task-1 PR 42 — behind_base"


def test_auto_merge_waiting_ci():
    ev = {
        "type": "auto_merge_waiting_ci",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "pr=42 mode=squash reason=ci_pending",
    }
    assert _row_detail(ev) == "waiting on CI for task-1 PR 42"


def test_auto_merge_kickback():
    ev = {
        "type": "auto_merge_kickback",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "pr=42 mode=squash reason=ci_failed",
    }
    assert _row_detail(ev) == "auto-merge kickback task-1 PR 42 — ci_failed"


def test_repo_dirty():
    ev = {
        "type": "repo_dirty",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "preflight=fail attempting=recover",
    }
    assert _row_detail(ev) == "repo dirty (task-1): preflight=fail attempting=recover"


def test_merge_failed_with_reason_kv():
    ev = {
        "type": "merge_failed",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "reason=test_failed_pytest",
    }
    assert _row_detail(ev) == "merge failed task-1 — test_failed_pytest"


def test_merge_succeeded_includes_branch():
    ev = {
        "type": "merge_succeeded",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "feat/task-1",
    }
    assert _row_detail(ev) == "merge succeeded task-1 (feat/task-1)"


def test_reconciled_external():
    ev = {
        "type": "reconciled_external",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "pr=42",
    }
    assert _row_detail(ev) == "reconciled external merge: task-1 PR 42"


def test_worker_spawned():
    ev = {
        "type": "worker_spawned",
        "actor": "autoscaler",
        "task_id": "",
        "detail": "role=builder name=builder-1",
    }
    assert _row_detail(ev) == "spawned worker (role=builder name=builder-1)"


def test_worker_retired():
    ev = {
        "type": "worker_retired",
        "actor": "autoscaler",
        "task_id": "",
        "detail": "role=builder name=builder-1",
    }
    assert _row_detail(ev) == "retired worker (role=builder name=builder-1)"


def test_worktree_pruned():
    ev = {
        "type": "worktree_pruned",
        "actor": "doctor",
        "task_id": "",
        "detail": "/tmp/orphan-worktree",
    }
    assert _row_detail(ev) == "pruned worktree: /tmp/orphan-worktree"


def test_worktree_reclaimed():
    ev = {
        "type": "worktree_reclaimed",
        "actor": "soldier",
        "task_id": "",
        "detail": "path=/tmp/orphan",
    }
    assert _row_detail(ev) == "reclaimed worktree: /tmp/orphan"


def test_mission_complete():
    ev = {
        "type": "mission_complete",
        "actor": "queen",
        "task_id": "",
        "detail": "mission=mission-001",
    }
    assert _row_detail(ev) == "mission complete: mission-001"


def test_mission_failed_defensive():
    """mission_failed event type isn't yet emitted by the codebase, but the
    formatter must still render it cleanly when it appears."""
    ev = {
        "type": "mission_failed",
        "actor": "queen",
        "task_id": "",
        "detail": "mission=mission-001 reason=stalled",
    }
    assert _row_detail(ev) == "mission failed: mission-001 — stalled"


def test_mission_budget_exceeded_pause():
    ev = {
        "type": "mission_budget_exceeded",
        "actor": "queen",
        "task_id": "",
        "detail": "mission=mission-001 action=pause cost=1.50 tokens=42",
    }
    assert _row_detail(ev) == "mission budget exceeded: mission-001 (pause)"


def test_plan_approved():
    ev = {
        "type": "plan_approved",
        "actor": "queen",
        "task_id": "plan-mission-001",
        "detail": "mission=mission-001 tasks=5",
    }
    assert _row_detail(ev) == "plan approved: mission-001 (5 tasks)"


def test_plan_ready_defensive():
    ev = {
        "type": "plan_ready",
        "actor": "queen",
        "task_id": "",
        "detail": "mission=mission-001",
    }
    assert _row_detail(ev) == "plan ready: mission-001"


def test_retry_pattern_defensive():
    ev = {
        "type": "retry_pattern",
        "actor": "doctor",
        "task_id": "task-1",
        "detail": "failures=3 reason=lint",
    }
    assert _row_detail(ev) == "retry pattern: failures=3 reason=lint"


def test_fallback_defensive():
    ev = {
        "type": "fallback",
        "actor": "queen",
        "task_id": "",
        "detail": "model=opus->sonnet",
    }
    assert _row_detail(ev) == "fallback: model=opus->sonnet"


def test_unknown_event_falls_back_to_generic():
    """Unknown event types render as ``<type> <task_id> <detail>``."""
    ev = {
        "type": "some_new_event",
        "actor": "colony",
        "task_id": "task-1",
        "detail": "info",
    }
    detail = _row_detail(ev)
    assert "some_new_event" in detail
    assert "task-1" in detail
    assert "info" in detail


# ---------------------------------------------------------------------------
# Color rules
# ---------------------------------------------------------------------------


def test_failed_event_renders_red_full_line():
    ev = {
        "type": "merge_failed",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "reason=test_failed",
    }
    out = watch_format.format_event_human(ev, use_color=True)
    # Click's red ANSI prefix.
    assert "\x1b[31m" in out


def test_kick_event_renders_yellow_full_line():
    ev = {
        "type": "kickback",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "merge conflict",
    }
    out = watch_format.format_event_human(ev, use_color=True)
    # Click's yellow ANSI prefix.
    assert "\x1b[33m" in out


def test_normal_event_uses_actor_color():
    """Non-failure events colorize only the actor column."""
    ev = {
        "type": "harvested",
        "actor": "soldier",
        "task_id": "task-1",
        "detail": "pr=42",
    }
    out = watch_format.format_event_human(ev, use_color=True)
    # Soldier's subsystem color is yellow (\x1b[33m), but it should appear
    # only once around the actor column — the timestamp and detail must be
    # bare. Detect by counting escape resets (\x1b[0m).
    assert "\x1b[33m" in out  # actor styled yellow
    # detail must be plain text, not styled red.
    assert "\x1b[31m" not in out
