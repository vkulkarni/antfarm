"""Tests for antfarm.core.rate_limiter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from antfarm.core.rate_limiter import RateLimitState, is_worker_rate_limited


def _future(seconds: int = 3600) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _past(seconds: int = 3600) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


# ---------------------------------------------------------------------------
# is_worker_rate_limited
# ---------------------------------------------------------------------------


def test_none_cooldown_not_limited():
    assert is_worker_rate_limited(None) is False


def test_past_cooldown_not_limited():
    assert is_worker_rate_limited(_past()) is False


def test_future_cooldown_is_limited():
    assert is_worker_rate_limited(_future()) is True


def test_invalid_string_not_limited():
    assert is_worker_rate_limited("not-a-timestamp") is False


def test_naive_future_timestamp_is_limited():
    # Naive ISO timestamp (no timezone info) — should still be treated as UTC future
    naive_future = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert is_worker_rate_limited(naive_future) is True


def test_naive_past_timestamp_not_limited():
    naive_past = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert is_worker_rate_limited(naive_past) is False


# ---------------------------------------------------------------------------
# RateLimitState dataclass
# ---------------------------------------------------------------------------


def test_rate_limit_state_defaults():
    state = RateLimitState(worker_id="worker-1")
    assert state.remaining is None
    assert state.reset_at is None
    assert state.cooldown_until is None


def test_rate_limit_state_full():
    state = RateLimitState(
        worker_id="worker-1",
        remaining=5,
        reset_at="2026-01-01T00:00:00+00:00",
        cooldown_until=_future(),
    )
    assert state.remaining == 5
    assert state.reset_at == "2026-01-01T00:00:00+00:00"
    assert state.cooldown_until is not None
