"""Tests for antfarm.core.workspace.WorkspaceManager."""

import logging
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from antfarm.core.workspace import WorkspaceManager


@pytest.fixture()
def git_repo(tmp_path):
    """Set up a bare origin and a working clone with a dev branch.

    Yields a dict with:
        repo_path   — path to the working clone
        workspace_root — scratch directory for worktrees
    """
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    # Create bare origin
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)

    # Clone origin
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)

    # Configure identity inside the clone so commits work in CI
    subprocess.run(
        ["git", "-C", str(clone), "config", "user.email", "test@antfarm.test"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "config", "user.name", "Antfarm Test"],
        check=True, capture_output=True,
    )

    # Make an initial commit so HEAD exists
    readme = clone / "README.md"
    readme.write_text("antfarm test repo")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(clone), "commit", "-m", "init"],
        check=True, capture_output=True,
    )

    # Push to origin and create dev branch tracking remote
    subprocess.run(
        ["git", "-C", str(clone), "push", "-u", "origin", "HEAD:dev"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "fetch", "origin"],
        check=True, capture_output=True,
    )

    yield {"repo_path": str(clone), "workspace_root": str(workspaces)}


def test_create_worktree(git_repo):
    mgr = WorkspaceManager(
        workspace_root=git_repo["workspace_root"],
        repo_path=git_repo["repo_path"],
        integration_branch="dev",
    )
    path = mgr.create("task-001", "att-001")

    expected = os.path.join(git_repo["workspace_root"], "task-001-att-001")
    assert path == expected
    assert os.path.exists(path)

    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    assert result.stdout.strip() == "feat/task-001-att-001"


def test_validate_clean(git_repo):
    mgr = WorkspaceManager(
        workspace_root=git_repo["workspace_root"],
        repo_path=git_repo["repo_path"],
        integration_branch="dev",
    )
    path = mgr.create("task-002", "att-001")
    assert mgr.validate(path) is True


def test_validate_dirty(git_repo):
    mgr = WorkspaceManager(
        workspace_root=git_repo["workspace_root"],
        repo_path=git_repo["repo_path"],
        integration_branch="dev",
    )
    path = mgr.create("task-003", "att-001")

    # Write an untracked file to make the tree dirty
    with open(os.path.join(path, "dirty.txt"), "w") as f:
        f.write("dirty")

    assert mgr.validate(path) is False


def test_list_orphans(git_repo):
    mgr = WorkspaceManager(
        workspace_root=git_repo["workspace_root"],
        repo_path=git_repo["repo_path"],
        integration_branch="dev",
    )
    path1 = mgr.create("task-004", "att-001")
    path2 = mgr.create("task-005", "att-001")

    orphans = mgr.list_orphans()

    assert path1 in orphans
    assert path2 in orphans


def test_validate_nonexistent_path(git_repo):
    mgr = WorkspaceManager(
        workspace_root=git_repo["workspace_root"],
        repo_path=git_repo["repo_path"],
        integration_branch="dev",
    )
    assert mgr.validate("/nonexistent/path") is False


# ---------------------------------------------------------------------------
# v0.6.7 P2: dep_branches base-ref selection
#
# These tests mock subprocess.run so that only the base-ref selection logic
# is exercised. We assert on the `worktree add` command's final positional
# argument, which is the base ref the new branch is created from.
# ---------------------------------------------------------------------------


def _mk_run_capture(worktree_calls: list[list[str]], rev_parse_ok: bool = True):
    """Build a subprocess.run stub.

    - ``git fetch origin`` — returns success.
    - ``git rev-parse --verify <ref>`` — returns 0 if rev_parse_ok else 1.
    - ``git worktree add ...`` — records the command and returns success.
    """
    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "fetch"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"]:
            return MagicMock(
                returncode=0 if rev_parse_ok else 1, stdout="", stderr=""
            )
        if cmd[:3] == ["git", "worktree", "add"]:
            worktree_calls.append(list(cmd))
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return fake_run


def test_create_no_deps_uses_integration_branch(tmp_path):
    """dep_branches=None → base is origin/<integration_branch>."""
    mgr = WorkspaceManager(
        workspace_root=str(tmp_path / "ws"),
        repo_path=str(tmp_path / "repo"),
        integration_branch="main",
    )
    captured: list[list[str]] = []
    with patch.object(subprocess, "run", side_effect=_mk_run_capture(captured)):
        mgr.create("task-001", "att-001", dep_branches=None)

    assert len(captured) == 1
    assert captured[0][-1] == "origin/main"


def test_create_empty_deps_uses_integration_branch(tmp_path):
    """dep_branches=[] behaves identically to None."""
    mgr = WorkspaceManager(
        workspace_root=str(tmp_path / "ws"),
        repo_path=str(tmp_path / "repo"),
        integration_branch="main",
    )
    captured: list[list[str]] = []
    with patch.object(subprocess, "run", side_effect=_mk_run_capture(captured)):
        mgr.create("task-001", "att-001", dep_branches=[])

    assert len(captured) == 1
    assert captured[0][-1] == "origin/main"


def test_create_one_unmerged_dep_uses_dep_branch(tmp_path):
    """Exactly one dep branch → base is origin/<dep_branch>."""
    mgr = WorkspaceManager(
        workspace_root=str(tmp_path / "ws"),
        repo_path=str(tmp_path / "repo"),
        integration_branch="main",
    )
    captured: list[list[str]] = []
    with patch.object(subprocess, "run", side_effect=_mk_run_capture(captured)):
        mgr.create(
            "task-002", "att-001",
            dep_branches=["feat/task-dep-att-001"],
        )

    assert len(captured) == 1
    assert captured[0][-1] == "origin/feat/task-dep-att-001"


def test_create_multiple_deps_falls_back_with_warning(tmp_path, caplog):
    """More than one dep branch → falls back to integration with a warning."""
    mgr = WorkspaceManager(
        workspace_root=str(tmp_path / "ws"),
        repo_path=str(tmp_path / "repo"),
        integration_branch="main",
    )
    captured: list[list[str]] = []
    with caplog.at_level(logging.WARNING, logger="antfarm.core.workspace"), \
         patch.object(subprocess, "run", side_effect=_mk_run_capture(captured)):
        mgr.create("task-003", "att-001", dep_branches=["a", "b"])

    assert len(captured) == 1
    assert captured[0][-1] == "origin/main"
    assert any("multi-dep" in r.message for r in caplog.records)


def test_create_missing_dep_ref_falls_back_with_warning(tmp_path, caplog):
    """rev-parse --verify failure → falls back to integration with a warning."""
    mgr = WorkspaceManager(
        workspace_root=str(tmp_path / "ws"),
        repo_path=str(tmp_path / "repo"),
        integration_branch="main",
    )
    captured: list[list[str]] = []
    with caplog.at_level(logging.WARNING, logger="antfarm.core.workspace"), \
         patch.object(
             subprocess, "run",
             side_effect=_mk_run_capture(captured, rev_parse_ok=False),
         ):
        mgr.create(
            "task-004", "att-001",
            dep_branches=["feat/missing-dep-att-001"],
        )

    assert len(captured) == 1
    assert captured[0][-1] == "origin/main"
    assert any("not found" in r.message for r in caplog.records)
