"""Tests for MissionUsage aggregation and idempotency."""

from __future__ import annotations

from antfarm.core.missions import MissionUsage
from antfarm.core.models import UsageEvent


def _make_event(
    event_id: str = "e-001",
    task_id: str = "task-1",
    attempt_id: str = "att-1",
    cost: float = 0.25,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    ts: str = "2026-04-22T10:00:00Z",
) -> UsageEvent:
    return UsageEvent(
        event_id=event_id,
        worker_id="node-1/w-1",
        task_id=task_id,
        attempt_id=attempt_id,
        mission_id="mission-x",
        ts=ts,
        model="claude-sonnet-4-7",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cost_usd=cost,
        source="claude_stop_hook",
    )


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def test_apply_single_event_sets_totals_and_timestamps():
    usage = MissionUsage(mission_id="mission-x")
    event = _make_event(ts="2026-04-22T10:00:00Z")
    usage.apply(event)

    assert usage.total_cost_usd == 0.25
    assert usage.total_input_tokens == 100
    assert usage.total_output_tokens == 50
    assert usage.event_count == 1
    assert usage.first_event_at == "2026-04-22T10:00:00Z"
    assert usage.last_event_at == "2026-04-22T10:00:00Z"
    assert "task-1" in usage.per_task
    assert usage.per_task["task-1"]["cost_usd"] == 0.25


def test_apply_multiple_events_sum_per_task():
    usage = MissionUsage(mission_id="mission-x")
    usage.apply(_make_event(event_id="e-001", task_id="task-1", cost=0.10, input_tokens=10))
    usage.apply(_make_event(event_id="e-002", task_id="task-1", cost=0.20, input_tokens=20))
    usage.apply(_make_event(event_id="e-003", task_id="task-2", cost=0.05, input_tokens=5))

    assert round(usage.total_cost_usd, 6) == 0.35
    assert usage.total_input_tokens == 35
    assert usage.event_count == 3
    assert round(usage.per_task["task-1"]["cost_usd"], 6) == 0.30
    assert round(usage.per_task["task-2"]["cost_usd"], 6) == 0.05


def test_apply_is_idempotent_on_event_id():
    usage = MissionUsage(mission_id="mission-x")
    event = _make_event(event_id="dupe-1", cost=0.10, input_tokens=10)
    usage.apply(event)
    usage.apply(event)
    usage.apply(event)
    assert usage.event_count == 1
    assert usage.total_cost_usd == 0.10
    assert usage.total_input_tokens == 10


def test_apply_tracks_first_and_last_timestamp():
    usage = MissionUsage(mission_id="mission-x")
    usage.apply(_make_event(event_id="e-1", ts="2026-04-22T10:00:00Z"))
    usage.apply(_make_event(event_id="e-2", ts="2026-04-22T11:00:00Z"))
    usage.apply(_make_event(event_id="e-3", ts="2026-04-22T12:00:00Z"))

    assert usage.first_event_at == "2026-04-22T10:00:00Z"
    assert usage.last_event_at == "2026-04-22T12:00:00Z"


def test_top_attempt_tracked_per_task():
    usage = MissionUsage(mission_id="mission-x")
    # attempt A: 2 events summing to $0.30
    usage.apply(_make_event(event_id="e-1", task_id="task-1", attempt_id="att-A", cost=0.10))
    usage.apply(_make_event(event_id="e-2", task_id="task-1", attempt_id="att-A", cost=0.20))
    # attempt B: one expensive event
    usage.apply(_make_event(event_id="e-3", task_id="task-1", attempt_id="att-B", cost=0.50))
    # attempt C: cheap
    usage.apply(_make_event(event_id="e-4", task_id="task-1", attempt_id="att-C", cost=0.01))

    bucket = usage.per_task["task-1"]
    assert bucket["top_attempt_id"] == "att-B"
    assert round(bucket["top_attempt_cost"], 6) == 0.50


def test_roundtrip_to_and_from_dict():
    usage = MissionUsage(mission_id="mission-x")
    usage.apply(_make_event(event_id="e-1", cost=0.10))
    usage.apply(_make_event(event_id="e-2", cost=0.20))

    d = usage.to_dict()
    restored = MissionUsage.from_dict(d)

    assert restored.mission_id == usage.mission_id
    assert round(restored.total_cost_usd, 6) == round(usage.total_cost_usd, 6)
    assert restored.total_input_tokens == usage.total_input_tokens
    assert restored.event_count == usage.event_count
    assert restored.first_event_at == usage.first_event_at
    assert restored.last_event_at == usage.last_event_at
    assert restored.seen_event_ids == usage.seen_event_ids


def test_apply_accepts_dict_form():
    """apply() should accept either a UsageEvent or its to_dict() form."""
    usage = MissionUsage(mission_id="mission-x")
    event = _make_event(event_id="e-1", cost=0.25)
    usage.apply(event.to_dict())
    assert usage.event_count == 1
    assert round(usage.total_cost_usd, 6) == 0.25
