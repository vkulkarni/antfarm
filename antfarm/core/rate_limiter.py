"""Rate limit awareness for Antfarm workers.

Provides helpers to track and check API rate limit state reported
by workers via heartbeat. Workers that are in cooldown are skipped
by the pull() scheduler until their cooldown expires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class RateLimitState:
    """Rate limit state reported by a worker.

    Args:
        worker_id: The worker this state belongs to.
        remaining: Remaining API requests in the current window, or None if unknown.
        reset_at: ISO 8601 timestamp when the rate limit window resets, or None.
        cooldown_until: ISO 8601 timestamp until which the worker should not pull
                        new tasks, or None if not in cooldown.
    """

    worker_id: str
    remaining: int | None = None
    reset_at: str | None = None
    cooldown_until: str | None = None


def is_worker_rate_limited(cooldown_until: str | None) -> bool:
    """Check whether a worker is currently rate limited.

    Args:
        cooldown_until: ISO 8601 timestamp string, or None.

    Returns:
        True if cooldown_until is set and is in the future, False otherwise.
    """
    if cooldown_until is None:
        return False
    try:
        cutoff = datetime.fromisoformat(cooldown_until)
        # Ensure timezone-aware comparison
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        return datetime.now(UTC) < cutoff
    except ValueError:
        return False
