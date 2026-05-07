"""Per-worktree Claude Code hook registration.

Claude Code reads hook configuration from `.claude/settings.json` in the
project (the directory it is invoked in). For per-mission usage telemetry
(#354) the worker must register the Stop hook before launching `claude`,
otherwise `stop.sh` never fires and cost tracking is silently broken (#391).

This module performs an idempotent, deep-merging write to the worktree-local
settings file: existing keys are preserved, the antfarm Stop entry is added
exactly once, and re-running on the same worktree is a no-op.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def register_stop_hook(workspace: Path | str, hook_script: Path | str) -> None:
    """Register `hook_script` as a Stop hook in `<workspace>/.claude/settings.json`.

    The file is created if absent. If present, the antfarm Stop hook entry is
    deep-merged into the existing `hooks.Stop` array, preserving any other
    user/project-level hooks and unrelated keys. Calling twice with the same
    arguments is a no-op (idempotent).

    Args:
        workspace: Absolute path to the git worktree where `claude` will run.
        hook_script: Absolute path to the executable hook script (e.g. stop.sh).
    """
    workspace_path = Path(workspace)
    hook_command = str(hook_script)

    settings_dir = workspace_path / ".claude"
    settings_path = settings_dir / "settings.json"
    settings_dir.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    if settings_path.exists():
        try:
            with settings_path.open() as fh:
                settings = json.load(fh) or {}
        except (json.JSONDecodeError, OSError) as exc:
            # Corrupt or unreadable settings file. Don't clobber it — log and
            # bail out so the operator notices via the missing usage telemetry.
            logger.warning(
                "could not parse %s (%s); leaving file untouched",
                settings_path,
                exc,
            )
            return

    if not isinstance(settings, dict):
        logger.warning(
            "%s is not a JSON object (got %s); leaving file untouched",
            settings_path,
            type(settings).__name__,
        )
        return

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        logger.warning("%s has non-object 'hooks' key; leaving file untouched", settings_path)
        return

    stop_matchers = hooks.setdefault("Stop", [])
    if not isinstance(stop_matchers, list):
        logger.warning("%s has non-array 'hooks.Stop' key; leaving file untouched", settings_path)
        return

    # Idempotency: if any existing matcher already references our hook command,
    # nothing to do.
    for matcher in stop_matchers:
        if not isinstance(matcher, dict):
            continue
        for hook in matcher.get("hooks", []) or []:
            if isinstance(hook, dict) and hook.get("command") == hook_command:
                return

    stop_matchers.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": hook_command,
                }
            ]
        }
    )

    with settings_path.open("w") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")


def stop_hook_path() -> Path:
    """Return the absolute path to the bundled Claude Code Stop hook script."""
    return (
        Path(__file__).resolve().parent.parent
        / "adapters"
        / "claude_code"
        / "hooks"
        / "stop.sh"
    )
