"""PR lifecycle operations (close/comment), abstracted so backends stay platform-agnostic.

Backends call ``PROps.close_pr`` when transitioning an attempt to SUPERSEDED so
stale PRs do not accumulate. Implementations must be safe to call outside any
backend lock — subprocess calls inside a lock would stall all other workers.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Protocol

logger = logging.getLogger(__name__)


class PROps(Protocol):
    """Interface for PR lifecycle operations."""

    def close_pr(self, pr: str, comment: str | None = None) -> bool:
        """Close a PR, optionally with a comment. Returns True on success."""
        ...


class GhPROps:
    """Close PRs via ``gh pr close <url|number> --comment <msg>``.

    Treats "already closed" / "already merged" responses as success. Never
    raises — returns False on any failure so callers (e.g. kickback) cannot be
    blocked by cosmetic PR-close issues such as a missing ``gh`` binary,
    network errors, or rate limits.
    """

    def __init__(self, cwd: str | None = None, timeout: float = 10.0) -> None:
        self._cwd = cwd
        self._timeout = timeout
        self._gh_missing_logged = False

    def close_pr(self, pr: str, comment: str | None = None) -> bool:
        if not pr:
            return True
        args = ["gh", "pr", "close", pr]
        if comment:
            args.extend(["--comment", comment])
        try:
            result = subprocess.run(
                args,
                cwd=self._cwd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except FileNotFoundError:
            if not self._gh_missing_logged:
                logger.warning("gh CLI not found; cannot close superseded PRs")
                self._gh_missing_logged = True
            return False
        except subprocess.TimeoutExpired:
            logger.warning("gh pr close timed out for %s", pr)
            return False

        if result.returncode == 0:
            return True

        stderr = (result.stderr or "").lower()
        # gh wording varies: "already closed", "has already been closed",
        # "already been merged", "has been merged" — all indicate terminal
        # state we asked for, so treat as success.
        if (
            "already closed" in stderr
            or "already been closed" in stderr
            or "already been merged" in stderr
            or "has been merged" in stderr
        ):
            return True

        logger.warning("gh pr close failed for %s: %s", pr, (result.stderr or "").strip())
        return False


class NullPROps:
    """No-op PROps implementation for tests and when ``gh`` is not configured."""

    def close_pr(self, pr: str, comment: str | None = None) -> bool:
        return True
