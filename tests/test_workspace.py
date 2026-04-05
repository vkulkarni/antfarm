"""Tests for antfarm.core.workspace.WorkspaceManager."""

import os
import subprocess

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
    (os.path.join(path, "dirty.txt"),)
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
