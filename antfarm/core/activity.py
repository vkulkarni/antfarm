"""Activity text synthesis for the TUI Activity column.

Pure module — no server or IO dependencies. Converts a structured
``(action, target)`` pair (typically posted by a Claude Code PreToolUse hook,
the Soldier, or the Doctor) into a short human-readable line suitable for the
``current_action`` field on a worker record and the Activity cell in the TUI.

The server synthesizes the text and stores it in the existing
``current_action`` field, so everything downstream (``doctor.check_stuck_workers``,
the Activity column, legacy API clients) continues to work unchanged.
"""

from __future__ import annotations

# Canonical verb → template mapping. ``{target}`` is filled in when present.
# A verb without ``{target}`` deliberately ignores whatever target the caller
# supplies (e.g. ``planning``, ``awaiting``) — the template is the full line.
VERB_TEMPLATES: dict[str, str] = {
    "editing": "editing {target}",
    "reading": "reading {target}",
    "running": "running {target}",
    "searching": "searching {target}",
    "scanning": "scanning {target}",
    "planning": "planning",
    "awaiting": "awaiting claude response",
    "rebasing": "rebasing {target}",
    "merging": "merging {target}",
    "running_tests": "running tests ({target})",
    "fast_forwarding": "fast-forwarding {target}",
    "pushing": "pushing {target}",
    "fetching": "fetching {target}",
    "cleanup": "cleanup",
    "polling": "polling",
    "idle": "idle",
}

# Claude Code tool name → canonical verb mapping for the PreToolUse hook.
_TOOL_VERBS: dict[str, str] = {
    "Edit": "editing",
    "Write": "editing",
    "Read": "reading",
    "Bash": "running",
    "WebFetch": "searching",
    "WebSearch": "searching",
    "Glob": "scanning",
    "Grep": "scanning",
    "TodoWrite": "planning",
}

_TARGET_MAX = 60


def _truncate_target(target: str) -> str:
    """Trim ``target`` to ``_TARGET_MAX`` chars with an ellipsis suffix."""
    if len(target) <= _TARGET_MAX:
        return target
    # 57 + "..." = 60. Keeps the cell readable in the TUI.
    return target[: _TARGET_MAX - 3] + "..."


def synthesize_text(action: str | None, target: str | None) -> str | None:
    """Return a human-readable activity line or None when both inputs are empty.

    Args:
        action: Canonical verb (e.g. ``"editing"``) or free-form string for
            unknown verbs. Falsy values (None, empty string) are treated as
            "no action".
        target: Optional target associated with the action (file path, branch,
            command). Falsy values are treated as "no target".

    Returns:
        A short line suitable for the ``current_action`` field, or None when
        both ``action`` and ``target`` are falsy.
    """
    action_s = (action or "").strip()
    target_s = (target or "").strip()
    if not action_s and not target_s:
        return None

    if action_s in VERB_TEMPLATES:
        template = VERB_TEMPLATES[action_s]
        if "{target}" in template:
            return template.format(target=_truncate_target(target_s))
        return template

    # Unknown verb: fall back to "<action> <target>". Either half may be empty.
    if action_s and target_s:
        return f"{action_s} {_truncate_target(target_s)}"
    return action_s or _truncate_target(target_s)


def tool_to_verb(tool_name: str) -> str:
    """Map a Claude Code tool name to a canonical verb.

    Unknown tool names fall back to their lowercased form; empty/missing names
    map to ``"tool"`` so the PreToolUse hook always has something to emit.
    """
    name = (tool_name or "").strip()
    if not name:
        return "tool"
    return _TOOL_VERBS.get(name, name.lower())
