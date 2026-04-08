"""Task and attempt lifecycle transition validators (v0.5).

Defines legal state transitions for the enriched lifecycle model.
All validators operate on string values for compatibility with both
the old (TaskStatus) and new (TaskState) enum systems.

Old state names ("ready", "active", "done") are mapped to their
v0.5 equivalents before validation.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Old → new state mapping (backward compatibility)
# ---------------------------------------------------------------------------

_OLD_TASK_STATE_MAP: dict[str, str] = {
    "ready": "queued",
    "active": "active",
    "done": "done",
    "paused": "paused",
    "blocked": "blocked",
}

_OLD_ATTEMPT_STATE_MAP: dict[str, str] = {
    "active": "started",
    "done": "harvested",
    "merged": "harvested",  # old merged maps to harvested (merge tracked separately)
    "superseded": "abandoned",
}


def _normalize_task_state(state: str) -> str:
    """Map old state names to v0.5 equivalents."""
    return _OLD_TASK_STATE_MAP.get(state, state)


def _normalize_attempt_state(state: str) -> str:
    """Map old attempt state names to v0.5 equivalents."""
    return _OLD_ATTEMPT_STATE_MAP.get(state, state)


# ---------------------------------------------------------------------------
# Legal task state transitions
# ---------------------------------------------------------------------------

LEGAL_TASK_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"claimed", "blocked", "paused", "active"},  # "active" = legacy pull (ready→active)
    "blocked": {"queued", "paused"},
    "claimed": {"active"},
    "active": {"harvest_pending", "paused"},
    "harvest_pending": {"done", "failed"},
    # "queued" = legacy kickback, "blocked" = max-attempt exhaustion
    "done": {"merge_ready", "kicked_back", "queued", "blocked"},
    "kicked_back": {"queued"},
    "merge_ready": {"merged"},
    "merged": set(),  # terminal
    "failed": {"queued"},  # retry path
    "paused": {"queued", "active", "blocked"},
}


def validate_task_transition(from_state: str, to_state: str) -> bool:
    """Return True if the task transition is legal, False otherwise."""
    from_norm = _normalize_task_state(from_state)
    to_norm = _normalize_task_state(to_state)
    return to_norm in LEGAL_TASK_TRANSITIONS.get(from_norm, set())


def assert_task_transition(from_state: str, to_state: str) -> None:
    """Raise ValueError if the task transition is illegal."""
    from_norm = _normalize_task_state(from_state)
    to_norm = _normalize_task_state(to_state)
    if to_norm not in LEGAL_TASK_TRANSITIONS.get(from_norm, set()):
        legal = LEGAL_TASK_TRANSITIONS.get(from_norm, set())
        raise ValueError(
            f"Illegal task transition: {from_state} → {to_state}. "
            f"Legal from '{from_norm}': {legal}"
        )


# ---------------------------------------------------------------------------
# Legal attempt state transitions
# ---------------------------------------------------------------------------

LEGAL_ATTEMPT_TRANSITIONS: dict[str, set[str]] = {
    "started": {"heartbeating", "agent_failed", "stale"},
    "heartbeating": {"agent_succeeded", "agent_failed", "stale"},
    "agent_succeeded": {"harvested"},
    "agent_failed": {"harvested"},  # failure record written at harvest
    "harvested": set(),  # terminal
    "stale": {"abandoned"},
    "abandoned": set(),  # terminal
}


def validate_attempt_transition(from_state: str, to_state: str) -> bool:
    """Return True if the attempt transition is legal, False otherwise."""
    from_norm = _normalize_attempt_state(from_state)
    to_norm = _normalize_attempt_state(to_state)
    return to_norm in LEGAL_ATTEMPT_TRANSITIONS.get(from_norm, set())


def assert_attempt_transition(from_state: str, to_state: str) -> None:
    """Raise ValueError if the attempt transition is illegal."""
    from_norm = _normalize_attempt_state(from_state)
    to_norm = _normalize_attempt_state(to_state)
    if to_norm not in LEGAL_ATTEMPT_TRANSITIONS.get(from_norm, set()):
        legal = LEGAL_ATTEMPT_TRANSITIONS.get(from_norm, set())
        raise ValueError(
            f"Illegal attempt transition: {from_state} → {to_state}. "
            f"Legal from '{from_norm}': {legal}"
        )
