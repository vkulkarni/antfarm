"""Tests for antfarm.core.pr_ops.

Covers the GhPROps subprocess wrapper (success, already-closed, gh-missing,
timeout) and the NullPROps no-op implementation.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from antfarm.core.pr_ops import GhPROps, NullPROps


class _CompletedProcess:
    def __init__(self, returncode: int, stderr: str = "", stdout: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def test_ghprops_close_success() -> None:
    ops = GhPROps()
    with patch("antfarm.core.pr_ops.subprocess.run", return_value=_CompletedProcess(0)) as mock:
        assert ops.close_pr("https://gh/pr/1", comment="hi") is True
    args, kwargs = mock.call_args
    assert args[0][:3] == ["gh", "pr", "close"]
    assert args[0][3] == "https://gh/pr/1"
    assert "--comment" in args[0]
    assert "hi" in args[0]


def test_ghprops_already_closed_is_ok() -> None:
    ops = GhPROps()
    resp = _CompletedProcess(1, stderr="Pull request has already been closed")
    with patch("antfarm.core.pr_ops.subprocess.run", return_value=resp):
        assert ops.close_pr("https://gh/pr/1") is True


def test_ghprops_already_merged_is_ok() -> None:
    ops = GhPROps()
    resp = _CompletedProcess(1, stderr="This PR has been merged")
    with patch("antfarm.core.pr_ops.subprocess.run", return_value=resp):
        assert ops.close_pr("https://gh/pr/1") is True


def test_ghprops_gh_missing_returns_false() -> None:
    ops = GhPROps()
    with patch("antfarm.core.pr_ops.subprocess.run", side_effect=FileNotFoundError()):
        # Must not raise; returns False.
        assert ops.close_pr("https://gh/pr/1") is False
        # Second call with gh still missing also returns False.
        assert ops.close_pr("https://gh/pr/2") is False


def test_ghprops_timeout_returns_false() -> None:
    ops = GhPROps(timeout=0.01)
    with patch(
        "antfarm.core.pr_ops.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=0.01),
    ):
        assert ops.close_pr("https://gh/pr/1") is False


def test_ghprops_empty_pr_is_noop() -> None:
    ops = GhPROps()
    with patch("antfarm.core.pr_ops.subprocess.run") as mock:
        assert ops.close_pr("") is True
    mock.assert_not_called()


def test_ghprops_arbitrary_failure_returns_false() -> None:
    ops = GhPROps()
    resp = _CompletedProcess(1, stderr="network unreachable")
    with patch("antfarm.core.pr_ops.subprocess.run", return_value=resp):
        assert ops.close_pr("https://gh/pr/1") is False


def test_nullprops_always_true() -> None:
    ops = NullPROps()
    assert ops.close_pr("") is True
    assert ops.close_pr("https://gh/pr/1") is True
    assert ops.close_pr("https://gh/pr/1", comment="hi") is True
