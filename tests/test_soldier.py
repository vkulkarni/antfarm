"""Tests for antfarm.core.soldier.Soldier integration engine.

Each test uses a real git repository (bare origin + working clone) and an
in-process Colony API server with a FileBackend, so the Soldier's git logic
and colony interactions are fully exercised without mocking.
"""

from __future__ import annotations

import contextlib
import subprocess

import pytest
from fastapi.testclient import TestClient

from antfarm.core.backends.file import FileBackend
from antfarm.core.colony_client import ColonyClient
from antfarm.core.serve import get_app
from antfarm.core.soldier import MergeResult, Soldier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=check)


def _configure_git(repo: str) -> None:
    _git(["git", "config", "user.email", "test@antfarm.test"], cwd=repo)
    _git(["git", "config", "user.name", "Antfarm Test"], cwd=repo)


def _commit_file(repo: str, filename: str, content: str, message: str) -> None:
    path = f"{repo}/{filename}"
    with open(path, "w") as f:
        f.write(content)
    _git(["git", "add", filename], cwd=repo)
    _git(["git", "commit", "-m", message], cwd=repo)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def soldier_env(tmp_path):
    """Set up a full soldier test environment.

    Creates:
    - bare origin repo
    - working clone with git identity configured
    - initial commit on 'dev' branch, pushed to origin
    - FileBackend + Colony via TestClient
    - ColonyClient injected with TestClient transport
    - Soldier instance pointing at the working clone

    Yields a dict with:
        soldier       — Soldier instance
        colony_client — ColonyClient for driving tasks
        repo_path     — path to working clone (str)
        origin_path   — path to bare origin (str)
    """
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"

    # --- bare origin ---
    _git(["git", "init", "--bare", str(origin)], cwd=str(tmp_path))

    # --- working clone ---
    _git(["git", "clone", str(origin), str(clone)], cwd=str(tmp_path))
    _configure_git(str(clone))

    # initial commit + dev branch
    _commit_file(str(clone), "README.md", "antfarm test repo\n", "init")
    _git(["git", "push", "origin", "HEAD:dev"], cwd=str(clone))
    _git(["git", "fetch", "origin"], cwd=str(clone))

    # Create local dev branch tracking origin/dev and check it out
    _git(["git", "checkout", "-b", "dev", "origin/dev"], cwd=str(clone), check=False)
    # If dev already exists, just switch to it and reset
    _git(["git", "checkout", "dev"], cwd=str(clone), check=False)
    _git(
        ["git", "branch", "--set-upstream-to=origin/dev", "dev"],
        cwd=str(clone),
        check=False,
    )

    # --- colony ---
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend)
    http_client = TestClient(app, raise_server_exceptions=True)

    colony_client = ColonyClient("http://testserver", client=http_client)

    soldier = Soldier(
        colony_url="http://testserver",
        repo_path=str(clone),
        integration_branch="dev",
        test_command=["true"],  # always pass by default
        poll_interval=0.0,
        client=http_client,
    )

    yield {
        "soldier": soldier,
        "colony_client": colony_client,
        "repo_path": str(clone),
        "origin_path": str(origin),
        "tmp_path": str(tmp_path),
    }


def _carry_and_harvest(
    colony_client: ColonyClient,
    repo_path: str,
    task_id: str,
    branch_name: str,
    *,
    depends_on: list[str] | None = None,
    priority: int = 10,
    file_name: str | None = None,
    file_content: str = "change\n",
) -> dict:
    """Create a task, forage it, make a commit on a branch, push, and harvest.

    Returns the harvested task dict.
    """
    # Register a dummy worker so forage works
    worker_id = f"worker-{task_id}"
    colony_client.register_worker(
        worker_id=worker_id,
        node_id="node-1",
        agent_type="generic",
        workspace_root="/tmp/ws",
    )

    # Create task
    colony_client._client.post(
        "/tasks",
        json={
            "id": task_id,
            "title": f"Task {task_id}",
            "spec": "do the thing",
            "depends_on": depends_on or [],
            "priority": priority,
        },
    ).raise_for_status()

    # Forage (assigns attempt)
    task = colony_client.forage(worker_id)
    assert task is not None, f"forage returned None for {task_id}"
    attempt_id = task["current_attempt"]

    # Create branch in working repo matching the attempt
    _git(["git", "checkout", "-b", branch_name, "origin/dev"], cwd=repo_path)
    fname = file_name or f"{task_id}.txt"
    _commit_file(repo_path, fname, file_content, f"work for {task_id}")
    _git(["git", "push", "origin", branch_name], cwd=repo_path)

    # Return to dev
    _git(["git", "checkout", "dev"], cwd=repo_path)

    # Harvest (mark done with branch info)
    colony_client.harvest(
        task_id=task_id,
        attempt_id=attempt_id,
        pr=f"https://github.com/x/y/pull/{task_id}",
        branch=branch_name,
    )

    return colony_client.get_task(task_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_merge_queue_respects_deps(soldier_env):
    """Task B (depends on A) is not in the queue until A is merged."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-a", "feat/task-a")
    _carry_and_harvest(cc, repo, "task-b", "feat/task-b", depends_on=["task-a"])

    queue = soldier.get_merge_queue()
    ids = [t["id"] for t in queue]
    # Only task-a eligible; task-b blocked by dep
    assert "task-a" in ids
    assert "task-b" not in ids


def test_merge_green_fast_forwards(soldier_env):
    """A clean merge with passing tests fast-forwards dev."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-001", "feat/task-001")

    results = soldier.run_once()
    assert results == [("task-001", MergeResult.MERGED)]

    # dev should now have the merged commit
    log = _git(["git", "log", "--oneline", "dev"], cwd=repo)
    assert "work for task-001" in log.stdout


def test_merge_conflict_kicks_back(soldier_env):
    """Conflicting changes cause a FAILED result and kickback."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    # Create a conflicting file on dev first
    _commit_file(repo, "conflict.txt", "original content\n", "base content on dev")
    _git(["git", "push", "origin", "dev"], cwd=repo)

    # Create task branch with conflicting change to same file
    _carry_and_harvest(
        cc,
        repo,
        "task-conflict",
        "feat/task-conflict",
        file_name="conflict.txt",
        file_content="conflicting content\n",
    )

    # Now put conflicting change on dev AFTER the branch was made
    _commit_file(
        repo, "conflict.txt", "dev diverged content\n", "conflict on dev"
    )
    _git(["git", "push", "origin", "dev"], cwd=repo)

    # Reset local dev to track origin/dev
    _git(["git", "fetch", "origin"], cwd=repo)
    _git(["git", "reset", "--hard", "origin/dev"], cwd=repo)

    results = soldier.run_once()
    assert results == [("task-conflict", MergeResult.FAILED)]

    # Task should be back in ready (kicked back)
    task = cc.get_task("task-conflict")
    assert task["status"] == "ready"


def test_test_failure_kicks_back(soldier_env):
    """Clean merge but test_command exits non-zero → FAILED."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    failing_soldier = Soldier(
        colony_url="http://testserver",
        repo_path=repo,
        integration_branch="dev",
        test_command=["false"],  # always fails
        poll_interval=0.0,
        client=soldier_env["soldier"].colony._client,
    )

    _carry_and_harvest(cc, repo, "task-fail", "feat/task-fail")

    results = failing_soldier.run_once()
    assert results == [("task-fail", MergeResult.FAILED)]

    task = cc.get_task("task-fail")
    assert task["status"] == "ready"


def test_kickback_supersedes_attempt(soldier_env):
    """After kickback, the original attempt is superseded and task is ready."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    failing_soldier = Soldier(
        colony_url="http://testserver",
        repo_path=repo,
        integration_branch="dev",
        test_command=["false"],
        poll_interval=0.0,
        client=cc._client,
    )

    task_before = _carry_and_harvest(cc, repo, "task-kb", "feat/task-kb")
    attempt_id = task_before["current_attempt"]

    failing_soldier.run_once()

    task_after = cc.get_task("task-kb")
    assert task_after["status"] == "ready"
    assert task_after["current_attempt"] is None

    # Original attempt should be superseded
    attempts = task_after["attempts"]
    superseded = [a for a in attempts if a["attempt_id"] == attempt_id]
    assert len(superseded) == 1
    assert superseded[0]["status"] == "superseded"


def test_independent_tasks_not_blocked(soldier_env):
    """A task kicked back to ready doesn't block an independent task from merging."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]
    soldier = soldier_env["soldier"]

    # Carry and harvest both tasks independently
    _carry_and_harvest(cc, repo, "task-ia", "feat/task-ia")
    _carry_and_harvest(cc, repo, "task-ic", "feat/task-ic")

    # Manually kickback task-ia (simulating a merge failure for task-ia)
    cc.kickback(task_id="task-ia", reason="test failure in task-ia")

    # Confirm task-ia is kicked back
    assert cc.get_task("task-ia")["status"] == "ready"

    # task-ic is still in the merge queue (independent, no deps)
    queue = soldier.get_merge_queue()
    ids = [t["id"] for t in queue]
    assert "task-ic" in ids
    assert "task-ia" not in ids  # kicked back, no longer done

    # task-ic should merge successfully
    results = soldier.run_once()
    merged = [r[0] for r in results if r[1] == MergeResult.MERGED]
    assert "task-ic" in merged


def test_only_current_attempt_merged(soldier_env):
    """A superseded attempt's branch is ignored — only the current attempt is merged."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    # First attempt — harvest then kickback manually
    worker_id = "worker-only-current"
    cc.register_worker(
        worker_id=worker_id,
        node_id="node-1",
        agent_type="generic",
        workspace_root="/tmp/ws",
    )
    cc._client.post(
        "/tasks",
        json={"id": "task-oc", "title": "Task OC", "spec": "spec", "depends_on": []},
    ).raise_for_status()

    task = cc.forage(worker_id)
    first_attempt_id = task["current_attempt"]

    # Create OLD branch and push (this is the superseded attempt branch)
    _git(["git", "checkout", "-b", "feat/task-oc-v1", "origin/dev"], cwd=repo)
    _commit_file(repo, "task-oc-v1.txt", "first attempt\n", "v1 work")
    _git(["git", "push", "origin", "feat/task-oc-v1"], cwd=repo)
    _git(["git", "checkout", "dev"], cwd=repo)

    # Harvest with old branch
    cc.harvest(
        task_id="task-oc",
        attempt_id=first_attempt_id,
        pr="https://github.com/x/y/pull/old",
        branch="feat/task-oc-v1",
    )

    # Kick back to supersede the first attempt
    cc.kickback(task_id="task-oc", reason="needs rework")

    # Second attempt
    task2 = cc.forage(worker_id)
    second_attempt_id = task2["current_attempt"]
    assert second_attempt_id != first_attempt_id

    # Create NEW branch
    _git(["git", "checkout", "-b", "feat/task-oc-v2", "origin/dev"], cwd=repo)
    _commit_file(repo, "task-oc-v2.txt", "second attempt\n", "v2 work")
    _git(["git", "push", "origin", "feat/task-oc-v2"], cwd=repo)
    _git(["git", "checkout", "dev"], cwd=repo)

    cc.harvest(
        task_id="task-oc",
        attempt_id=second_attempt_id,
        pr="https://github.com/x/y/pull/new",
        branch="feat/task-oc-v2",
    )

    results = soldier.run_once()
    assert results == [("task-oc", MergeResult.MERGED)]

    # Only v2 commit should appear on dev
    log = _git(["git", "log", "--oneline", "dev"], cwd=repo)
    assert "v2 work" in log.stdout
    assert "v1 work" not in log.stdout


def test_cleanup_after_conflict(soldier_env):
    """After a conflict, working tree is clean and on integration branch."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _commit_file(repo, "clash.txt", "original\n", "base")
    _git(["git", "push", "origin", "dev"], cwd=repo)

    _carry_and_harvest(
        cc,
        repo,
        "task-cl",
        "feat/task-cl",
        file_name="clash.txt",
        file_content="task version\n",
    )

    # Diverge dev after the branch was created
    _commit_file(repo, "clash.txt", "dev diverged\n", "dev diverges")
    _git(["git", "push", "origin", "dev"], cwd=repo)
    _git(["git", "fetch", "origin"], cwd=repo)
    _git(["git", "reset", "--hard", "origin/dev"], cwd=repo)

    soldier.run_once()

    # Working tree should be clean
    status = _git(["git", "status", "--porcelain"], cwd=repo)
    assert status.stdout.strip() == ""

    # Should be on dev
    branch = _git(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    assert branch.stdout.strip() == "dev"

    # Temp branch must not exist
    branches = _git(["git", "branch"], cwd=repo)
    assert "antfarm/temp-merge" not in branches.stdout


def test_override_order_sorts_before_normal(soldier_env):
    """Tasks with merge_override sort before normal tasks in the merge queue."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    # Create two tasks with default priority — task-normal carries first (lower ts)
    _carry_and_harvest(cc, repo, "task-normal", "feat/task-normal")
    _carry_and_harvest(cc, repo, "task-override", "feat/task-override")

    # Set merge_override=1 on the second task to force it to merge first
    cc._client.post("/tasks/task-override/override-order", json={"position": 1}).raise_for_status()

    queue = soldier.get_merge_queue()
    ids = [t["id"] for t in queue]
    assert ids[0] == "task-override", f"Expected task-override first, got {ids}"
    assert "task-normal" in ids


def test_clearing_override_restores_normal_order(soldier_env):
    """Clearing merge_override returns task to normal priority/FIFO ordering."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-a", "feat/task-a2")
    _carry_and_harvest(cc, repo, "task-b", "feat/task-b2")

    # Set override on task-b, then clear it
    cc._client.post("/tasks/task-b/override-order", json={"position": 1}).raise_for_status()
    cc._client.delete("/tasks/task-b/override-order").raise_for_status()

    queue = soldier.get_merge_queue()
    ids = [t["id"] for t in queue]
    # task-a was created first so should sort first after clearing override
    assert ids[0] == "task-a", f"Expected task-a first after clearing override, got {ids}"


def test_cleanup_after_test_failure(soldier_env):
    """After a test failure, working tree is clean and on integration branch."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    failing_soldier = Soldier(
        colony_url="http://testserver",
        repo_path=repo,
        integration_branch="dev",
        test_command=["false"],
        poll_interval=0.0,
        client=cc._client,
    )

    _carry_and_harvest(cc, repo, "task-tf", "feat/task-tf")

    failing_soldier.run_once()

    status = _git(["git", "status", "--porcelain"], cwd=repo)
    assert status.stdout.strip() == ""

    branch = _git(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    assert branch.stdout.strip() == "dev"

    branches = _git(["git", "branch"], cwd=repo)
    assert "antfarm/temp-merge" not in branches.stdout


# ---------------------------------------------------------------------------
# Cascade invalidation tests
# ---------------------------------------------------------------------------


def test_cascade_kickback_downstream_done(soldier_env):
    """When A is kicked back, B (depends on A, status=done) is also kicked back."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]
    soldier = soldier_env["soldier"]

    _carry_and_harvest(cc, repo, "task-a", "feat/task-a-cascade")
    _carry_and_harvest(cc, repo, "task-b", "feat/task-b-cascade", depends_on=["task-a"])

    # Both are done. Kick back A via soldier's cascade method.
    soldier.kickback_with_cascade("task-a", "merge conflict")

    task_a = cc.get_task("task-a")
    task_b = cc.get_task("task-b")
    assert task_a["status"] == "ready"
    assert task_b["status"] == "ready"

    # B's trail should mention cascade
    b_trail = [e["message"] for e in task_b["trail"]]
    assert any("cascade" in msg.lower() for msg in b_trail)


def test_cascade_does_not_interrupt_active(soldier_env):
    """Active downstream tasks are not cascade-kicked-back."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]
    soldier = soldier_env["soldier"]

    _carry_and_harvest(cc, repo, "task-p", "feat/task-p-active")

    # Create task-q that depends on task-p, but only forage it (leave active)
    worker_id = "worker-active-q"
    cc.register_worker(
        worker_id=worker_id,
        node_id="node-1",
        agent_type="generic",
        workspace_root="/tmp/ws",
    )
    cc._client.post(
        "/tasks",
        json={
            "id": "task-q",
            "title": "Task Q",
            "spec": "spec",
            "depends_on": ["task-p"],
        },
    ).raise_for_status()
    task_q = cc.forage(worker_id)
    assert task_q is not None  # task-q is now active

    soldier.kickback_with_cascade("task-p", "failure")

    task_q_after = cc.get_task("task-q")
    assert task_q_after["status"] == "active"  # NOT kicked back


def test_cascade_does_not_touch_merged(soldier_env):
    """Cascade from a kicked-back task must not affect downstream merged tasks."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]
    soldier = soldier_env["soldier"]

    _carry_and_harvest(cc, repo, "task-m1", "feat/task-m1")

    # Merge task-m1 first (unblocks task-m2)
    results = soldier.run_once()
    assert results == [("task-m1", MergeResult.MERGED)]

    # Create and harvest task-m2 that depends on task-m1
    _carry_and_harvest(cc, repo, "task-m2", "feat/task-m2", depends_on=["task-m1"])

    # Merge m2
    results2 = soldier.run_once()
    assert results2 == [("task-m2", MergeResult.MERGED)]

    # Verify both are merged
    task_m1 = cc.get_task("task-m1")
    task_m2 = cc.get_task("task-m2")
    assert any(a["status"] == "merged" for a in task_m1["attempts"])
    assert any(a["status"] == "merged" for a in task_m2["attempts"])

    # Trigger cascade on task-m1. The kickback on an already-merged task
    # may error (task is in done/ but state transition may fail). Either way,
    # the cascade guard must protect task-m2 from being touched.
    with contextlib.suppress(Exception):
        soldier.kickback_with_cascade("task-m1", "retroactive invalidation")

    # Critical assertion: task-m2 must still have exactly 1 merged attempt
    m2_after = cc.get_task("task-m2")
    m2_merged = [a for a in m2_after["attempts"] if a["status"] == "merged"]
    assert len(m2_merged) == 1, (
        f"task-m2 should still have exactly 1 merged attempt, got {m2_after['attempts']}"
    )


def test_cascade_recursive(soldier_env):
    """Cascade propagates recursively: A kicked -> B kicked -> C kicked."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]
    soldier = soldier_env["soldier"]

    _carry_and_harvest(cc, repo, "task-r1", "feat/task-r1")
    _carry_and_harvest(cc, repo, "task-r2", "feat/task-r2", depends_on=["task-r1"])
    _carry_and_harvest(cc, repo, "task-r3", "feat/task-r3", depends_on=["task-r2"])

    soldier.kickback_with_cascade("task-r1", "root failure")

    assert cc.get_task("task-r1")["status"] == "ready"
    assert cc.get_task("task-r2")["status"] == "ready"
    assert cc.get_task("task-r3")["status"] == "ready"

    # All should have trail entries
    for tid in ["task-r2", "task-r3"]:
        trail = [e["message"] for e in cc.get_task(tid)["trail"]]
        assert any("cascade" in msg.lower() for msg in trail)


def test_cascade_does_not_affect_independent(soldier_env):
    """Independent done tasks are not cascade-kicked-back."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]
    soldier = soldier_env["soldier"]

    _carry_and_harvest(cc, repo, "task-ind-a", "feat/task-ind-a")
    _carry_and_harvest(cc, repo, "task-ind-b", "feat/task-ind-b")  # no dep on A

    soldier.kickback_with_cascade("task-ind-a", "failure")

    assert cc.get_task("task-ind-a")["status"] == "ready"
    assert cc.get_task("task-ind-b")["status"] == "done"  # untouched
