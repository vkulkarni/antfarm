"""Tests for antfarm.core.lifecycle state transition validators."""

import pytest

from antfarm.core.lifecycle import (
    assert_attempt_transition,
    assert_task_transition,
    validate_attempt_transition,
    validate_task_transition,
)


# ---------------------------------------------------------------------------
# Task transitions — legal
# ---------------------------------------------------------------------------


def test_legal_transition_queued_to_claimed():
    assert validate_task_transition("queued", "claimed") is True


def test_legal_transition_queued_to_blocked():
    assert validate_task_transition("queued", "blocked") is True


def test_legal_transition_queued_to_paused():
    assert validate_task_transition("queued", "paused") is True


def test_legal_transition_claimed_to_active():
    assert validate_task_transition("claimed", "active") is True


def test_legal_transition_active_to_harvest_pending():
    assert validate_task_transition("active", "harvest_pending") is True


def test_legal_transition_harvest_pending_to_done():
    assert validate_task_transition("harvest_pending", "done") is True


def test_legal_transition_harvest_pending_to_failed():
    assert validate_task_transition("harvest_pending", "failed") is True


def test_legal_transition_done_to_merge_ready():
    assert validate_task_transition("done", "merge_ready") is True


def test_legal_transition_done_to_kicked_back():
    assert validate_task_transition("done", "kicked_back") is True


def test_legal_transition_kicked_back_to_queued():
    assert validate_task_transition("kicked_back", "queued") is True


def test_legal_transition_merge_ready_to_merged():
    assert validate_task_transition("merge_ready", "merged") is True


def test_legal_transition_failed_to_queued():
    assert validate_task_transition("failed", "queued") is True


def test_legal_transition_paused_to_queued():
    assert validate_task_transition("paused", "queued") is True


def test_legal_transition_paused_to_active():
    assert validate_task_transition("paused", "active") is True


def test_legal_transition_blocked_to_queued():
    assert validate_task_transition("blocked", "queued") is True


# ---------------------------------------------------------------------------
# Task transitions — illegal
# ---------------------------------------------------------------------------


def test_illegal_transition_active_to_merged():
    assert validate_task_transition("active", "merged") is False


def test_illegal_transition_queued_to_done():
    assert validate_task_transition("queued", "done") is False


def test_illegal_transition_failed_to_merged():
    assert validate_task_transition("failed", "merged") is False


def test_illegal_transition_merged_to_anything():
    assert validate_task_transition("merged", "queued") is False
    assert validate_task_transition("merged", "active") is False
    assert validate_task_transition("merged", "done") is False


def test_illegal_transition_active_to_done_directly():
    """Active must go through harvest_pending first."""
    assert validate_task_transition("active", "done") is False


def test_illegal_transition_queued_to_merged():
    assert validate_task_transition("queued", "merged") is False


# ---------------------------------------------------------------------------
# Task transitions — assert
# ---------------------------------------------------------------------------


def test_assert_raises_on_illegal():
    with pytest.raises(ValueError, match="Illegal task transition"):
        assert_task_transition("active", "merged")


def test_assert_passes_on_legal():
    assert_task_transition("queued", "claimed")  # should not raise


# ---------------------------------------------------------------------------
# Task transitions — backward compat (old state names)
# ---------------------------------------------------------------------------


def test_old_ready_to_claimed():
    """Old 'ready' maps to 'queued' which can transition to 'claimed'."""
    assert validate_task_transition("ready", "claimed") is True


def test_old_ready_to_blocked():
    assert validate_task_transition("ready", "blocked") is True


def test_old_done_to_kicked_back():
    assert validate_task_transition("done", "kicked_back") is True


# ---------------------------------------------------------------------------
# Attempt transitions — legal
# ---------------------------------------------------------------------------


def test_legal_attempt_started_to_heartbeating():
    assert validate_attempt_transition("started", "heartbeating") is True


def test_legal_attempt_heartbeating_to_succeeded():
    assert validate_attempt_transition("heartbeating", "agent_succeeded") is True


def test_legal_attempt_heartbeating_to_failed():
    assert validate_attempt_transition("heartbeating", "agent_failed") is True


def test_legal_attempt_heartbeating_to_stale():
    assert validate_attempt_transition("heartbeating", "stale") is True


def test_legal_attempt_agent_succeeded_to_harvested():
    assert validate_attempt_transition("agent_succeeded", "harvested") is True


def test_legal_attempt_agent_failed_to_harvested():
    assert validate_attempt_transition("agent_failed", "harvested") is True


def test_legal_attempt_stale_to_abandoned():
    assert validate_attempt_transition("stale", "abandoned") is True


# ---------------------------------------------------------------------------
# Attempt transitions — illegal
# ---------------------------------------------------------------------------


def test_illegal_attempt_started_to_harvested():
    assert validate_attempt_transition("started", "harvested") is False


def test_illegal_attempt_stale_to_succeeded():
    assert validate_attempt_transition("stale", "agent_succeeded") is False


def test_illegal_attempt_harvested_terminal():
    assert validate_attempt_transition("harvested", "started") is False
    assert validate_attempt_transition("harvested", "stale") is False


def test_illegal_attempt_abandoned_terminal():
    assert validate_attempt_transition("abandoned", "started") is False


# ---------------------------------------------------------------------------
# Attempt transitions — assert
# ---------------------------------------------------------------------------


def test_attempt_assert_raises_on_illegal():
    with pytest.raises(ValueError, match="Illegal attempt transition"):
        assert_attempt_transition("started", "harvested")


def test_attempt_assert_passes_on_legal():
    assert_attempt_transition("started", "heartbeating")  # should not raise


# ---------------------------------------------------------------------------
# Attempt transitions — backward compat
# ---------------------------------------------------------------------------


def test_old_active_attempt_to_agent_failed():
    """Old 'active' maps to 'started' which can transition to 'agent_failed'."""
    assert validate_attempt_transition("active", "agent_failed") is True


def test_old_active_attempt_to_heartbeating():
    assert validate_attempt_transition("active", "heartbeating") is True
