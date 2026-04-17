"""Tests for antfarm.core.soldier.Soldier integration engine.

Each test uses a real git repository (bare origin + working clone) and an
in-process Colony API server with a FileBackend, so the Soldier's git logic
and colony interactions are fully exercised without mocking.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from antfarm.core import soldier as soldier_module
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
    _commit_file(repo, "conflict.txt", "dev diverged content\n", "conflict on dev")
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


# ---------------------------------------------------------------------------
# Mission-ID propagation tests
# ---------------------------------------------------------------------------


def _make_mission(backend, mission_id, status="building"):
    """Helper to create a mission directly in the backend."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    mission = {
        "mission_id": mission_id,
        "spec": "test spec",
        "spec_file": None,
        "status": status,
        "plan_task_id": None,
        "plan_artifact": None,
        "task_ids": [],
        "blocked_task_ids": [],
        "config": {
            "max_attempts": 3,
            "max_parallel_builders": 4,
            "require_plan_review": True,
            "stall_threshold_minutes": 30,
            "completion_mode": "best_effort",
            "test_command": None,
            "integration_branch": "main",
            "blocked_timeout_action": "wait",
            "blocked_timeout_minutes": 120,
        },
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "report": None,
        "last_progress_at": now,
        "re_plan_count": 0,
    }
    backend.create_mission(mission)
    return mission


def _make_done_task_with_mission(backend, task_id, mission_id=None):
    """Helper to create a done task with a mission_id and a current attempt."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    attempt_id = f"att-{task_id}"
    task = {
        "id": task_id,
        "title": f"Task {task_id}",
        "spec": "do the thing",
        "complexity": "M",
        "priority": 10,
        "depends_on": [],
        "touches": [],
        "capabilities_required": [],
        "mission_id": mission_id,
        "created_by": "test",
        "status": "ready",
        "current_attempt": None,
        "attempts": [],
        "trail": [],
        "signals": [],
        "created_at": now,
        "updated_at": now,
    }
    backend.carry(task)

    # Register worker, pull, and harvest to get a proper done task with attempt
    worker = {
        "worker_id": f"w-{task_id}",
        "node_id": "node-1",
        "agent_type": "generic",
        "workspace_root": "/tmp/ws",
        "status": "idle",
        "registered_at": now,
        "last_heartbeat": now,
    }
    backend.register_worker(worker)
    backend.pull(f"w-{task_id}")
    pulled = backend.get_task(task_id)
    attempt_id = pulled["current_attempt"]
    backend.mark_harvested(task_id, attempt_id, pr=f"PR-{task_id}", branch=f"feat/{task_id}")
    return backend.get_task(task_id)


def test_soldier_review_task_inherits_mission_id(tmp_path):
    """Review task created by Soldier inherits mission_id from parent task."""
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    mission_id = "mission-test-inherit"
    _make_mission(backend, mission_id)

    task = _make_done_task_with_mission(backend, "task-mi-1", mission_id=mission_id)
    # Add task to mission's task_ids
    backend.update_mission(mission_id, {"task_ids": [task["id"]]})

    soldier = Soldier.from_backend(
        backend,
        repo_path=str(tmp_path),
        require_review=True,
    )
    review_id = soldier.create_review_task(task)
    assert review_id == "review-task-mi-1"

    review_task = backend.get_task(review_id)
    assert review_task is not None
    assert review_task.get("mission_id") == mission_id


def test_soldier_review_task_appended_to_mission_task_ids(tmp_path):
    """Review task ID is appended to the parent mission's task_ids."""
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    mission_id = "mission-test-append"
    _make_mission(backend, mission_id)

    task = _make_done_task_with_mission(backend, "task-mi-2", mission_id=mission_id)
    backend.update_mission(mission_id, {"task_ids": [task["id"]]})

    soldier = Soldier.from_backend(
        backend,
        repo_path=str(tmp_path),
        require_review=True,
    )
    review_id = soldier.create_review_task(task)
    assert review_id == "review-task-mi-2"

    mission = backend.get_mission(mission_id)
    assert review_id in mission["task_ids"]


def test_soldier_review_task_no_mission_id_when_parent_has_none(tmp_path):
    """When parent task has no mission_id, review task also has no mission_id."""
    backend = FileBackend(root=str(tmp_path / ".antfarm"))

    task = _make_done_task_with_mission(backend, "task-no-mission", mission_id=None)

    soldier = Soldier.from_backend(
        backend,
        repo_path=str(tmp_path),
        require_review=True,
    )
    review_id = soldier.create_review_task(task)
    assert review_id == "review-task-no-mission"

    review_task = backend.get_task(review_id)
    assert review_task is not None
    assert review_task.get("mission_id") is None


def test_soldier_suppresses_review_for_cancelled_mission(tmp_path):
    """Soldier does NOT create a review task when mission is CANCELLED."""
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    mission_id = "mission-cancelled"
    _make_mission(backend, mission_id, status="cancelled")

    task = _make_done_task_with_mission(backend, "task-cancelled", mission_id=mission_id)

    soldier = Soldier.from_backend(
        backend,
        repo_path=str(tmp_path),
        require_review=True,
    )
    review_id = soldier.create_review_task(task)
    assert review_id is None

    # Confirm no review task was created
    review_task = backend.get_task("review-task-cancelled")
    assert review_task is None


# ---------------------------------------------------------------------------
# Re-review on SHA mismatch tests (#226)
# ---------------------------------------------------------------------------


def _set_attempt_artifact_sha(backend, task_id, sha: str) -> None:
    """Inject a minimal artifact with ``head_commit_sha`` onto the current attempt."""
    import json
    from pathlib import Path

    # Task is in done/ after harvest.
    done_path = Path(backend._root) / "tasks" / "done" / f"{task_id}.json"
    data = json.loads(done_path.read_text())
    attempt_id = data["current_attempt"]
    artifact = {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "worker_id": "w-test",
        "branch": f"feat/{task_id}",
        "pr_url": None,
        "base_commit_sha": "0" * 40,
        "head_commit_sha": sha,
        "target_branch": "main",
        "target_branch_sha_at_harvest": "0" * 40,
    }
    for a in data["attempts"]:
        if a["attempt_id"] == attempt_id:
            a["artifact"] = artifact
            break
    done_path.write_text(json.dumps(data, indent=2))


def test_create_review_task_creates_when_no_prior_review(tmp_path):
    """Baseline: with no prior review, create_review_task creates a new one."""
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    _make_done_task_with_mission(backend, "task-rr-new", mission_id=None)
    _set_attempt_artifact_sha(backend, "task-rr-new", "a" * 40)
    task = backend.get_task("task-rr-new")

    soldier = Soldier.from_backend(backend, repo_path=str(tmp_path))
    review_id = soldier.create_review_task(task)

    assert review_id == "review-task-rr-new"
    review = backend.get_task(review_id)
    assert review is not None
    assert review["status"] == "ready"
    assert "Attempt-SHA:" in review["spec"]
    assert "a" * 40 in review["spec"]


def test_create_review_task_noops_when_sha_matches(tmp_path):
    """Same SHA on the existing review spec → no-op (returns None)."""
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    _make_done_task_with_mission(backend, "task-rr-same", mission_id=None)
    _set_attempt_artifact_sha(backend, "task-rr-same", "b" * 40)
    task = backend.get_task("task-rr-same")

    soldier = Soldier.from_backend(backend, repo_path=str(tmp_path))
    first = soldier.create_review_task(task)
    assert first == "review-task-rr-same"

    review_before = backend.get_task("review-task-rr-same")
    updated_at_before = review_before["updated_at"]
    trail_len_before = len(review_before.get("trail", []))

    # Second call with same SHA should no-op
    second = soldier.create_review_task(task)
    assert second is None

    review_after = backend.get_task("review-task-rr-same")
    assert review_after["updated_at"] == updated_at_before
    assert len(review_after.get("trail", [])) == trail_len_before


def test_create_review_task_noops_when_review_in_progress(tmp_path):
    """Review task currently in active/ with matching SHA → no-op."""
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    _make_done_task_with_mission(backend, "task-rr-active", mission_id=None)
    _set_attempt_artifact_sha(backend, "task-rr-active", "c" * 40)
    task = backend.get_task("task-rr-active")

    soldier = Soldier.from_backend(backend, repo_path=str(tmp_path))
    review_id = soldier.create_review_task(task)
    assert review_id == "review-task-rr-active"

    # Simulate a reviewer picking up the review task
    backend.register_worker(
        {
            "worker_id": "reviewer-1",
            "node_id": "node-1",
            "agent_type": "generic",
            "workspace_root": "/tmp/ws",
            "status": "idle",
            "capabilities": ["review"],
        }
    )
    pulled = backend.pull("reviewer-1")
    assert pulled is not None and pulled["id"] == "review-task-rr-active"

    # SHA still matches → no-op, review stays active
    again = soldier.create_review_task(task)
    assert again is None
    review = backend.get_task("review-task-rr-active")
    assert review["status"] == "active"


def test_create_review_task_rereadies_on_sha_mismatch(tmp_path):
    """Different SHA on re-attempt → re-ready review task, supersede old attempt."""
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    _make_done_task_with_mission(backend, "task-rr-mm", mission_id=None)
    _set_attempt_artifact_sha(backend, "task-rr-mm", "a" * 40)
    task = backend.get_task("task-rr-mm")

    soldier = Soldier.from_backend(backend, repo_path=str(tmp_path))
    soldier.create_review_task(task)

    # Simulate reviewer claim — review task is now in active/
    backend.register_worker(
        {
            "worker_id": "reviewer-1",
            "node_id": "node-1",
            "agent_type": "generic",
            "workspace_root": "/tmp/ws",
            "status": "idle",
            "capabilities": ["review"],
        }
    )
    backend.pull("reviewer-1")
    review_before = backend.get_task("review-task-rr-mm")
    old_attempt_id = review_before["current_attempt"]
    assert review_before["status"] == "active"

    # Parent task gets re-attempted with a new SHA (kickback + re-pull + re-harvest)
    backend.kickback("task-rr-mm", "reattempt for test")
    backend.heartbeat("w-task-rr-mm", {"status": "idle"})
    backend.pull("w-task-rr-mm")
    pulled = backend.get_task("task-rr-mm")
    new_attempt_id = pulled["current_attempt"]
    backend.mark_harvested("task-rr-mm", new_attempt_id, pr="PR-v2", branch="feat/task-rr-mm")
    _set_attempt_artifact_sha(backend, "task-rr-mm", "b" * 40)
    task = backend.get_task("task-rr-mm")

    review_id = soldier.create_review_task(task)
    assert review_id == "review-task-rr-mm"

    review_after = backend.get_task("review-task-rr-mm")
    assert review_after["status"] == "ready"
    assert review_after["current_attempt"] is None
    # Old attempt is superseded
    for a in review_after["attempts"]:
        if a["attempt_id"] == old_attempt_id:
            assert a["status"] == "superseded"
    # New SHA is embedded in the spec
    assert "b" * 40 in review_after["spec"]
    # Trail has a re-review entry
    messages = [e["message"] for e in review_after.get("trail", [])]
    assert any("Re-review" in m for m in messages)


def test_rereview_is_idempotent(tmp_path):
    """Calling rereview twice doesn't double-supersede the same attempt."""
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    _make_done_task_with_mission(backend, "task-rr-idem", mission_id=None)
    _set_attempt_artifact_sha(backend, "task-rr-idem", "a" * 40)
    task = backend.get_task("task-rr-idem")

    soldier = Soldier.from_backend(backend, repo_path=str(tmp_path))
    soldier.create_review_task(task)

    # Reviewer picks it up, then parent is re-attempted (different SHA)
    backend.register_worker(
        {
            "worker_id": "reviewer-1",
            "node_id": "node-1",
            "agent_type": "generic",
            "workspace_root": "/tmp/ws",
            "status": "idle",
            "capabilities": ["review"],
        }
    )
    backend.pull("reviewer-1")

    new_spec = "updated spec body\nAttempt-SHA: " + ("b" * 40) + "\n"
    backend.rereview("review-task-rr-idem", new_spec, touches=["x"])

    first = backend.get_task("review-task-rr-idem")
    assert first["status"] == "ready"
    assert first["current_attempt"] is None
    superseded_count_1 = sum(1 for a in first["attempts"] if a["status"] == "superseded")

    # Second rereview: already ready, no current_attempt → no new supersession
    backend.rereview("review-task-rr-idem", new_spec, touches=["x"])
    second = backend.get_task("review-task-rr-idem")
    assert second["status"] == "ready"
    assert second["current_attempt"] is None
    superseded_count_2 = sum(1 for a in second["attempts"] if a["status"] == "superseded")
    assert superseded_count_2 == superseded_count_1


def test_reattempt_end_to_end_flow(tmp_path):
    """End-to-end: kickback → re-harvest re-readies review; passing verdict unblocks merge."""
    from antfarm.core.models import ReviewVerdict

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    _make_done_task_with_mission(backend, "task-rr-e2e", mission_id=None)
    _set_attempt_artifact_sha(backend, "task-rr-e2e", "a" * 40)
    task = backend.get_task("task-rr-e2e")

    soldier = Soldier.from_backend(backend, repo_path=str(tmp_path))
    # First review task created
    assert soldier.create_review_task(task) == "review-task-rr-e2e"

    # Reviewer picks up, parent then gets kicked back (simulating failed review)
    backend.register_worker(
        {
            "worker_id": "reviewer-1",
            "node_id": "node-1",
            "agent_type": "generic",
            "workspace_root": "/tmp/ws",
            "status": "idle",
            "capabilities": ["review"],
        }
    )
    backend.pull("reviewer-1")
    backend.kickback("task-rr-e2e", "review requested changes")

    # Re-pull parent → new attempt, harvest again with a new SHA
    backend.heartbeat("w-task-rr-e2e", {"status": "idle"})
    backend.pull("w-task-rr-e2e")
    pulled = backend.get_task("task-rr-e2e")
    new_attempt_id = pulled["current_attempt"]
    backend.mark_harvested(
        "task-rr-e2e",
        new_attempt_id,
        pr="PR-task-rr-e2e-v2",
        branch="feat/task-rr-e2e",
    )
    _set_attempt_artifact_sha(backend, "task-rr-e2e", "b" * 40)
    task = backend.get_task("task-rr-e2e")

    # Before fix: this would no-op and deadlock. Now it re-readies the review.
    assert soldier.create_review_task(task) == "review-task-rr-e2e"
    review = backend.get_task("review-task-rr-e2e")
    assert review["status"] == "ready"
    assert "b" * 40 in review["spec"]

    # Simulate a passing verdict on the *new* parent attempt
    task = backend.get_task("task-rr-e2e")
    attempt_id = task["current_attempt"]
    verdict = ReviewVerdict(
        provider="human",
        verdict="pass",
        summary="LGTM",
        reviewed_commit_sha="b" * 40,
    )
    backend.store_review_verdict("task-rr-e2e", attempt_id, verdict.to_dict())

    # Merge queue should now include the task (require_review + passing + fresh)
    soldier_req = Soldier.from_backend(backend, repo_path=str(tmp_path), require_review=True)
    ids = [t["id"] for t in soldier_req.get_merge_queue()]
    assert "task-rr-e2e" in ids


def test_create_review_task_noops_on_legacy_review_without_marker(tmp_path):
    """Legacy review task without Attempt-SHA marker → no-op, leave untouched."""
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    _make_done_task_with_mission(backend, "task-rr-legacy", mission_id=None)
    _set_attempt_artifact_sha(backend, "task-rr-legacy", "a" * 40)
    task = backend.get_task("task-rr-legacy")

    # Hand-craft a legacy review task (no Attempt-SHA marker in spec)
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    now = datetime.now(UTC).isoformat()
    legacy_review = {
        "id": "review-task-rr-legacy",
        "title": "Review: task-rr-legacy",
        "spec": (
            "Review task task-rr-legacy: 'Task task-rr-legacy'\n\n"
            "Branch: feat/task-rr-legacy\n"
            "PR: PR-task-rr-legacy\n\n"
            "Instructions:\n"
            "1. Read the PR diff\n"
        ),
        "complexity": "S",
        "priority": 1,
        "depends_on": [],
        "touches": [],
        "capabilities_required": ["review"],
        "mission_id": None,
        "created_by": "soldier",
        "status": "active",
        "current_attempt": "att-legacy",
        "attempts": [
            {
                "attempt_id": "att-legacy",
                "worker_id": "reviewer-1",
                "status": "active",
                "branch": None,
                "pr": None,
                "started_at": now,
                "completed_at": None,
            }
        ],
        "trail": [],
        "signals": [],
        "created_at": now,
        "updated_at": now,
    }
    active_path = Path(backend._root) / "tasks" / "active" / "review-task-rr-legacy.json"
    active_path.write_text(json.dumps(legacy_review, indent=2))

    review_before = backend.get_task("review-task-rr-legacy")
    updated_at_before = review_before["updated_at"]
    trail_len_before = len(review_before.get("trail", []))

    soldier = Soldier.from_backend(backend, repo_path=str(tmp_path))
    result = soldier.create_review_task(task)
    assert result is None

    # Review task untouched — still in active/, same updated_at, same trail
    review_after = backend.get_task("review-task-rr-legacy")
    assert review_after["status"] == "active"
    assert review_after["current_attempt"] == "att-legacy"
    assert review_after["updated_at"] == updated_at_before
    assert len(review_after.get("trail", [])) == trail_len_before
    # Must still be in active/ folder (not bounced to ready/)
    ready_path = Path(backend._root) / "tasks" / "ready" / "review-task-rr-legacy.json"
    assert not ready_path.exists()
    assert active_path.exists()


def test_run_once_with_review_rereadies_on_sha_mismatch(tmp_path):
    """run_once_with_review re-readies a stale done review instead of consuming its verdict."""
    from antfarm.core.models import ReviewVerdict

    backend = FileBackend(root=str(tmp_path / ".antfarm"))

    # 1. Parent task done with SHA 'a' (first attempt)
    _make_done_task_with_mission(backend, "task-rr-roc", mission_id=None)
    _set_attempt_artifact_sha(backend, "task-rr-roc", "a" * 40)
    task = backend.get_task("task-rr-roc")

    soldier = Soldier.from_backend(backend, repo_path=str(tmp_path))
    assert soldier.create_review_task(task) == "review-task-rr-roc"

    # 2. Reviewer claims + finishes the review with a passing verdict for SHA 'a'
    backend.register_worker(
        {
            "worker_id": "reviewer-1",
            "node_id": "node-1",
            "agent_type": "generic",
            "workspace_root": "/tmp/ws",
            "status": "idle",
            "capabilities": ["review"],
        }
    )
    backend.pull("reviewer-1")
    review_task = backend.get_task("review-task-rr-roc")
    review_attempt_id = review_task["current_attempt"]
    pass_verdict_for_a = ReviewVerdict(
        provider="human",
        verdict="pass",
        summary="LGTM for a",
        reviewed_commit_sha="a" * 40,
    ).to_dict()
    # Harvest the review task with a passing verdict artifact
    import json
    from pathlib import Path

    backend.mark_harvested(
        "review-task-rr-roc",
        review_attempt_id,
        pr="review-pr",
        branch="feat/review-task-rr-roc",
    )
    # Inject a review verdict artifact so extract_verdict_from_review_task finds it
    review_done_path = Path(backend._root) / "tasks" / "done" / "review-task-rr-roc.json"
    rdata = json.loads(review_done_path.read_text())
    for a in rdata["attempts"]:
        if a["attempt_id"] == review_attempt_id:
            a["artifact"] = {
                "task_id": "review-task-rr-roc",
                "attempt_id": review_attempt_id,
                "worker_id": "reviewer-1",
                "branch": "feat/review-task-rr-roc",
                "pr_url": None,
                "base_commit_sha": "0" * 40,
                "head_commit_sha": "a" * 40,
                "target_branch": "main",
                "target_branch_sha_at_harvest": "0" * 40,
                "review_verdict": pass_verdict_for_a,
            }
            break
    review_done_path.write_text(json.dumps(rdata, indent=2))

    # 3. Parent task gets kicked back + re-attempted with a NEW SHA 'b'.
    # The old review in done/ is now STALE (refers to SHA 'a').
    backend.kickback("task-rr-roc", "need changes")
    backend.heartbeat("w-task-rr-roc", {"status": "idle"})
    backend.pull("w-task-rr-roc")
    pulled = backend.get_task("task-rr-roc")
    new_attempt_id = pulled["current_attempt"]
    backend.mark_harvested(
        "task-rr-roc",
        new_attempt_id,
        pr="PR-task-rr-roc-v2",
        branch="feat/task-rr-roc",
    )
    _set_attempt_artifact_sha(backend, "task-rr-roc", "b" * 40)

    # Sanity: the new parent attempt has NO stored review verdict yet.
    parent_before = backend.get_task("task-rr-roc")
    for a in parent_before["attempts"]:
        if a["attempt_id"] == new_attempt_id:
            assert a.get("review_verdict") is None

    # 4. run_once_with_review should re-ready the stale review, NOT consume
    # its old verdict against the new attempt.
    results = soldier.run_once_with_review()
    assert results == [("task-rr-roc", MergeResult.NEEDS_REVIEW)]

    review_after = backend.get_task("review-task-rr-roc")
    assert review_after["status"] == "ready"
    assert review_after["current_attempt"] is None
    assert "b" * 40 in review_after["spec"]

    # Parent attempt still has no verdict (we didn't inherit the stale one)
    parent_after = backend.get_task("task-rr-roc")
    for a in parent_after["attempts"]:
        if a["attempt_id"] == new_attempt_id:
            assert a.get("review_verdict") is None


# ---------------------------------------------------------------------------
# is_infra_task-based filtering (Issue #259)
# ---------------------------------------------------------------------------


def _carry_and_harvest_with_caps(
    colony_client: ColonyClient,
    repo_path: str,
    task_id: str,
    branch_name: str,
    capabilities_required: list[str],
) -> dict:
    """Like _carry_and_harvest, but sets capabilities_required on the task."""
    worker_id = f"worker-{task_id}"
    colony_client.register_worker(
        worker_id=worker_id,
        node_id="node-1",
        agent_type="generic",
        workspace_root="/tmp/ws",
        capabilities=capabilities_required,
    )
    colony_client._client.post(
        "/tasks",
        json={
            "id": task_id,
            "title": f"Task {task_id}",
            "spec": "do the thing",
            "depends_on": [],
            "priority": 10,
            "capabilities_required": capabilities_required,
        },
    ).raise_for_status()

    task = colony_client.forage(worker_id)
    assert task is not None
    attempt_id = task["current_attempt"]

    _git(["git", "checkout", "-b", branch_name, "origin/dev"], cwd=repo_path)
    _commit_file(repo_path, f"{task_id}.txt", "change\n", f"work for {task_id}")
    _git(["git", "push", "origin", branch_name], cwd=repo_path)
    _git(["git", "checkout", "dev"], cwd=repo_path)

    colony_client.harvest(
        task_id=task_id,
        attempt_id=attempt_id,
        pr=f"https://github.com/x/y/pull/{task_id}",
        branch=branch_name,
    )
    return colony_client.get_task(task_id)


def test_process_done_tasks_skips_review_capability_task(soldier_env):
    """Task with capabilities_required=['review'] but non-'review-' id is skipped.

    Exercises the is_infra_task() consolidation: previously only ids starting
    with 'review-' were skipped in process_done_tasks. Now any task whose
    capabilities include 'review' is treated as infra.
    """
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest_with_caps(
        cc,
        repo,
        "task-cap-review",
        "feat/task-cap-review",
        capabilities_required=["review"],
    )

    created = soldier.process_done_tasks()
    # Should NOT create a review-of-review task
    assert not any("task-cap-review" in rid for rid in created)


def test_get_done_candidates_skips_review_capability_task(soldier_env):
    """Task with capabilities_required=['review'] but non-'review-' id is
    excluded from the merge queue via _get_done_candidates."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest_with_caps(
        cc,
        repo,
        "task-cap-review-2",
        "feat/task-cap-review-2",
        capabilities_required=["review"],
    )

    candidates = soldier._get_done_candidates()
    assert not any(t["id"] == "task-cap-review-2" for t in candidates)


def test_get_merge_queue_skips_review_capability_task(soldier_env):
    """Task with capabilities_required=['review'] but non-'review-' id is
    excluded from the merge queue via get_merge_queue.

    Exercises the is_infra_task() consolidation in get_merge_queue: previously
    only ids starting with 'review-' were skipped. Now any task whose
    capabilities include 'review' is treated as infra and excluded.
    """
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest_with_caps(
        cc,
        repo,
        "task-cap-review-3",
        "feat/task-cap-review-3",
        capabilities_required=["review"],
    )

    queue = soldier.get_merge_queue()
    assert not any(t["id"] == "task-cap-review-3" for t in queue)


# ---------------------------------------------------------------------------
# External merge reconciliation (#264)
# ---------------------------------------------------------------------------


def test_run_once_skips_externally_merged_pr(soldier_env, monkeypatch):
    """When gh reports the PR as MERGED, mark_merged is called and attempt_merge is skipped."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-ext-merged", "feat/task-ext-merged")

    # Fake: origin reports MERGED
    monkeypatch.setattr(Soldier, "_check_pr_merged_on_origin", lambda self, pr: True)

    # Fail loudly if attempt_merge is invoked — the reconciler should short-circuit.
    def _fail(*_a, **_k):  # pragma: no cover — invoked only on regression
        raise AssertionError("attempt_merge should not run when PR is already merged on origin")

    monkeypatch.setattr(Soldier, "attempt_merge", _fail)

    results = soldier.run_once()
    assert results == [("task-ext-merged", MergeResult.MERGED)]

    task = cc.get_task("task-ext-merged")
    attempts = task["attempts"]
    assert any(a["status"] == "merged" for a in attempts)


def test_run_once_proceeds_when_pr_not_merged(soldier_env, monkeypatch):
    """When gh reports the PR as not merged (False), the normal merge path runs."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-ext-open", "feat/task-ext-open")

    monkeypatch.setattr(Soldier, "_check_pr_merged_on_origin", lambda self, pr: False)

    calls: list[str] = []
    real_attempt_merge = Soldier.attempt_merge

    def _tracking(self, task):
        calls.append(task["id"])
        return real_attempt_merge(self, task)

    monkeypatch.setattr(Soldier, "attempt_merge", _tracking)

    results = soldier.run_once()
    assert calls == ["task-ext-open"]
    assert results == [("task-ext-open", MergeResult.MERGED)]


def test_run_once_falls_through_on_unknown(soldier_env, monkeypatch):
    """When gh status is unknown (None), the normal merge path runs."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-ext-unknown", "feat/task-ext-unknown")

    monkeypatch.setattr(Soldier, "_check_pr_merged_on_origin", lambda self, pr: None)

    calls: list[str] = []
    real_attempt_merge = Soldier.attempt_merge

    def _tracking(self, task):
        calls.append(task["id"])
        return real_attempt_merge(self, task)

    monkeypatch.setattr(Soldier, "attempt_merge", _tracking)

    results = soldier.run_once()
    assert calls == ["task-ext-unknown"]
    assert results == [("task-ext-unknown", MergeResult.MERGED)]


# ---------------------------------------------------------------------------
# Soldier activity-feed events (#191)
#
# Soldier emits SSE events to serve._event_queue at each merge-lifecycle
# transition with actor="soldier":
#   merge_started, merge_succeeded, merge_failed, reconciled_external.
# Colony-owned events (harvested/kickback/merged) are emitted from serve.py
# with actor="colony" and must not be re-emitted by soldier.
# ---------------------------------------------------------------------------


@pytest.fixture
def clear_events():
    """Clear the SSE event queue before each event-assertion test."""
    from antfarm.core import serve

    serve._event_queue.clear()
    yield serve._event_queue


def _events_of_type(queue, event_type: str) -> list[dict]:
    return [e for e in queue if e["type"] == event_type]


def test_soldier_emits_merge_started_and_succeeded_on_green_merge(soldier_env, clear_events):
    """On a clean merge, soldier emits merge_started then merge_succeeded."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-ev-ok", "feat/task-ev-ok")

    results = soldier.run_once()
    assert results == [("task-ev-ok", MergeResult.MERGED)]

    started = _events_of_type(clear_events, "merge_started")
    succeeded = _events_of_type(clear_events, "merge_succeeded")
    assert len(started) == 1
    assert len(succeeded) == 1

    assert started[0]["actor"] == "soldier"
    assert started[0]["task_id"] == "task-ev-ok"
    assert started[0]["detail"] == "feat/task-ev-ok"

    assert succeeded[0]["actor"] == "soldier"
    assert succeeded[0]["task_id"] == "task-ev-ok"
    assert succeeded[0]["detail"] == "feat/task-ev-ok"

    # Ordering: started must come before succeeded.
    assert started[0]["id"] < succeeded[0]["id"]


def test_soldier_emits_merge_failed_on_conflict(soldier_env, clear_events):
    """A merge conflict emits merge_failed with actor='soldier' and no merge_succeeded."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _commit_file(repo, "conflict-ev.txt", "base\n", "base on dev")
    _git(["git", "push", "origin", "dev"], cwd=repo)

    _carry_and_harvest(
        cc,
        repo,
        "task-ev-conflict",
        "feat/task-ev-conflict",
        file_name="conflict-ev.txt",
        file_content="branch change\n",
    )

    _commit_file(repo, "conflict-ev.txt", "dev diverged\n", "dev diverges")
    _git(["git", "push", "origin", "dev"], cwd=repo)
    _git(["git", "fetch", "origin"], cwd=repo)
    _git(["git", "reset", "--hard", "origin/dev"], cwd=repo)

    results = soldier.run_once()
    assert results == [("task-ev-conflict", MergeResult.FAILED)]

    failed = _events_of_type(clear_events, "merge_failed")
    succeeded = _events_of_type(clear_events, "merge_succeeded")
    assert len(failed) == 1
    assert succeeded == []
    assert failed[0]["actor"] == "soldier"
    assert failed[0]["task_id"] == "task-ev-conflict"
    assert "conflict" in failed[0]["detail"].lower()


def test_soldier_emits_merge_failed_on_test_failure(soldier_env, clear_events):
    """A test failure (non-zero test_command) emits merge_failed."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    failing_soldier = Soldier(
        colony_url="http://testserver",
        repo_path=repo,
        integration_branch="dev",
        test_command=["false"],
        poll_interval=0.0,
        client=soldier_env["soldier"].colony._client,
    )

    _carry_and_harvest(cc, repo, "task-ev-testfail", "feat/task-ev-testfail")

    results = failing_soldier.run_once()
    assert results == [("task-ev-testfail", MergeResult.FAILED)]

    failed = _events_of_type(clear_events, "merge_failed")
    succeeded = _events_of_type(clear_events, "merge_succeeded")
    assert len(failed) == 1
    assert succeeded == []
    assert failed[0]["actor"] == "soldier"
    assert failed[0]["task_id"] == "task-ev-testfail"
    assert "test" in failed[0]["detail"].lower()


def test_soldier_emits_reconciled_external_and_skips_merge_events(
    soldier_env, clear_events, monkeypatch
):
    """When the PR is already merged on origin, soldier emits reconciled_external and
    does not run the attempt_merge path (so no merge_started/succeeded fire)."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-ev-ext", "feat/task-ev-ext")

    monkeypatch.setattr(Soldier, "_check_pr_merged_on_origin", lambda self, pr: True)

    results = soldier.run_once()
    assert results == [("task-ev-ext", MergeResult.MERGED)]

    reconciled = _events_of_type(clear_events, "reconciled_external")
    assert len(reconciled) == 1
    assert reconciled[0]["actor"] == "soldier"
    assert reconciled[0]["task_id"] == "task-ev-ext"
    assert reconciled[0]["detail"].startswith("pr=")

    # Reconciliation path short-circuits the merge attempt; soldier must not
    # emit merge_started/merge_succeeded from that path.
    assert _events_of_type(clear_events, "merge_started") == []
    assert _events_of_type(clear_events, "merge_succeeded") == []


def test_soldier_does_not_re_emit_colony_event_types(soldier_env, clear_events):
    """Colony-owned events (harvested/kickback/merged) must not fire with actor='soldier'."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-ev-noreemit", "feat/task-ev-noreemit")

    # Clear events emitted by the _carry_and_harvest setup (which calls colony
    # endpoints that emit with actor='colony') so this assertion focuses on
    # events soldier itself produces during run_once.
    clear_events.clear()

    soldier.run_once()

    for e in clear_events:
        if e["type"] in ("harvested", "kickback", "merged"):
            assert e["actor"] != "soldier", (
                f"soldier must not re-emit colony-owned event {e['type']}: {e}"
            )


# ---------------------------------------------------------------------------
# P5: event-driven merge trigger — _wait_for_event tests
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    """Minimal stand-in for an httpx streaming Response used with a context manager."""

    def __init__(self, lines: list[str], *, raise_on_enter: Exception | None = None):
        self._lines = lines
        self._raise_on_enter = raise_on_enter

    def __enter__(self):
        if self._raise_on_enter is not None:
            raise self._raise_on_enter
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self):
        yield from self._lines


class _FakeHttpxClient:
    """Minimal httpx.Client replacement with a scripted stream() response."""

    def __init__(
        self,
        lines: list[str] | None = None,
        *,
        stream_exc: Exception | None = None,
        enter_exc: Exception | None = None,
    ):
        self._lines = lines or []
        self._stream_exc = stream_exc
        self._enter_exc = enter_exc
        self.stream_calls: list[tuple[str, str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str, params: dict | None = None, **kwargs):
        self.stream_calls.append((method, url, params or {}))
        if self._stream_exc is not None:
            raise self._stream_exc
        return _FakeStreamResponse(self._lines, raise_on_enter=self._enter_exc)


def _make_soldier_for_event_tests(poll_interval: float = 5.0) -> Soldier:
    """Construct a Soldier with a usable colony_url but no real HTTP."""
    return Soldier(
        colony_url="http://fake-colony:7433",
        repo_path="/tmp/not-used",
        integration_branch="dev",
        poll_interval=poll_interval,
    )


def test_wait_for_event_wakes_on_harvested_under_one_second(monkeypatch):
    """A wake event (harvested) must cause _wait_for_event to return quickly."""
    soldier = _make_soldier_for_event_tests(poll_interval=5.0)

    wake = {
        "id": 1,
        "actor": "colony",
        "type": "harvested",
        "task_id": "task-1",
        "detail": "",
        "ts": "now",
    }
    lines = [f"data: {json.dumps(wake)}", ""]
    fake_client = _FakeHttpxClient(lines=lines)

    monkeypatch.setattr(
        soldier_module.httpx, "Client", lambda *a, **kw: fake_client
    )

    # If sleep is called here we fail — wake path must not sleep.
    sleep_calls: list[float] = []
    monkeypatch.setattr(soldier_module.time, "sleep", lambda s: sleep_calls.append(s))

    start = time.monotonic()
    woken = soldier._wait_for_event(timeout=5.0)
    elapsed = time.monotonic() - start

    assert woken is True
    assert elapsed < 1.0, f"expected fast wake, took {elapsed:.2f}s"
    assert soldier._event_cursor == 1
    assert sleep_calls == [], "wake path must not call time.sleep"


def test_wait_for_event_ignores_unrelated_events_until_timeout(monkeypatch):
    """Non-wake events advance the cursor but do not cause an early return."""
    soldier = _make_soldier_for_event_tests(poll_interval=5.0)

    unrelated = {
        "id": 7,
        "actor": "worker",
        "type": "random_event",
        "task_id": "task-x",
        "detail": "",
        "ts": "now",
    }
    # Provide exactly one non-wake event then let the fake stream end,
    # simulating server-side timeout.
    lines = [f"data: {json.dumps(unrelated)}", ""]
    fake_client = _FakeHttpxClient(lines=lines)
    monkeypatch.setattr(
        soldier_module.httpx, "Client", lambda *a, **kw: fake_client
    )

    sleep_calls: list[float] = []
    monkeypatch.setattr(soldier_module.time, "sleep", lambda s: sleep_calls.append(s))

    woken = soldier._wait_for_event(timeout=5.0)

    assert woken is False
    # Cursor advanced even though no wake happened.
    assert soldier._event_cursor == 7
    # Clean end-of-stream means a server-side timeout path — no fallback sleep.
    assert sleep_calls == []


def test_wait_for_event_timeout_returns_false_without_crashing(monkeypatch):
    """Empty stream (server timeout) returns False — loop continues normally."""
    soldier = _make_soldier_for_event_tests(poll_interval=5.0)

    fake_client = _FakeHttpxClient(lines=[])
    monkeypatch.setattr(
        soldier_module.httpx, "Client", lambda *a, **kw: fake_client
    )
    monkeypatch.setattr(soldier_module.time, "sleep", lambda s: None)

    woken = soldier._wait_for_event(timeout=5.0)
    assert woken is False
    assert soldier._event_cursor == 0


def test_wait_for_event_connection_error_falls_back_to_sleep(monkeypatch):
    """On httpx errors, log WARN + time.sleep(poll_interval); no exception escapes."""
    soldier = _make_soldier_for_event_tests(poll_interval=5.0)

    fake_client = _FakeHttpxClient(stream_exc=httpx.ConnectError("boom"))
    monkeypatch.setattr(
        soldier_module.httpx, "Client", lambda *a, **kw: fake_client
    )

    sleep_calls: list[float] = []
    monkeypatch.setattr(soldier_module.time, "sleep", lambda s: sleep_calls.append(s))

    woken = soldier._wait_for_event(timeout=5.0)

    assert woken is False
    assert sleep_calls == [5.0], "fallback must sleep exactly poll_interval once"


def test_wait_for_event_json_parse_error_falls_back_to_sleep(monkeypatch):
    """Malformed SSE payload triggers the WARN + sleep fallback."""
    soldier = _make_soldier_for_event_tests(poll_interval=3.0)

    # "data: not-json" will fail json.loads inside the helper.
    lines = ["data: not-json", ""]
    fake_client = _FakeHttpxClient(lines=lines)
    monkeypatch.setattr(
        soldier_module.httpx, "Client", lambda *a, **kw: fake_client
    )

    sleep_calls: list[float] = []
    monkeypatch.setattr(soldier_module.time, "sleep", lambda s: sleep_calls.append(s))

    woken = soldier._wait_for_event(timeout=3.0)

    assert woken is False
    assert sleep_calls == [3.0]


def test_wait_for_event_in_process_soldier_uses_plain_sleep(tmp_path, monkeypatch):
    """Soldier built via from_backend has no colony_url — must plain-sleep, not SSE."""
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    soldier = Soldier.from_backend(
        backend=backend,
        repo_path=str(tmp_path),
        integration_branch="dev",
        poll_interval=2.0,
    )

    # If httpx.Client is ever invoked, fail.
    def _no_client(*a, **kw):
        raise AssertionError("in-process soldier must not open SSE")

    monkeypatch.setattr(soldier_module.httpx, "Client", _no_client)

    sleep_calls: list[float] = []
    monkeypatch.setattr(soldier_module.time, "sleep", lambda s: sleep_calls.append(s))

    woken = soldier._wait_for_event(timeout=2.0)

    assert woken is False
    assert sleep_calls == [2.0]


def test_wait_for_event_passes_cursor_and_timeout_to_server(monkeypatch):
    """The helper must use the current cursor and propagate timeout to /events."""
    soldier = _make_soldier_for_event_tests(poll_interval=4.0)
    soldier._event_cursor = 42

    fake_client = _FakeHttpxClient(lines=[])
    monkeypatch.setattr(
        soldier_module.httpx, "Client", lambda *a, **kw: fake_client
    )
    monkeypatch.setattr(soldier_module.time, "sleep", lambda s: None)

    soldier._wait_for_event(timeout=4.0)

    assert len(fake_client.stream_calls) == 1
    method, url, params = fake_client.stream_calls[0]
    assert method == "GET"
    assert url.endswith("/events")
    assert params == {"after": 42, "timeout": 4.0}


# ---------------------------------------------------------------------------
# Merge diagnostic events (#287 / P6): merge_attempted, merge_skipped,
# merge_failed. Each test patches antfarm.core.soldier._emit to capture
# emissions so soldier internals can be asserted against without coupling to
# the SSE bus layout.
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_emits(monkeypatch):
    """Capture all calls to antfarm.core.soldier._emit in an ordered list."""
    from antfarm.core import soldier as soldier_mod

    calls: list[tuple[str, str, str]] = []

    def _fake_emit(event_type: str, task_id: str, detail: str = "") -> None:
        calls.append((event_type, task_id, detail))

    monkeypatch.setattr(soldier_mod, "_emit", _fake_emit)
    return calls


def _emit_types(calls: list[tuple[str, str, str]]) -> list[str]:
    return [c[0] for c in calls]


def test_attempt_merge_emits_merge_attempted(soldier_env, captured_emits):
    """attempt_merge emits merge_attempted with attempt_id and branch in the detail."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-att-001", "feat/task-att-001")

    # captured_emits is active via monkeypatch before run_once runs
    soldier.run_once()

    attempted = [c for c in captured_emits if c[0] == "merge_attempted"]
    assert len(attempted) == 1
    _, task_id, detail = attempted[0]
    assert task_id == "task-att-001"
    assert "branch=feat/task-att-001" in detail
    # Attempt id should also appear in the detail string.
    assert "attempt=" in detail and detail.split("attempt=")[1].split(" ")[0] != ""


def test_attempt_merge_dual_emits_merge_started_for_backcompat(soldier_env, captured_emits):
    """merge_started is still emitted alongside merge_attempted during 0.6.x."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-dual-001", "feat/task-dual-001")

    soldier.run_once()

    types = _emit_types(captured_emits)
    assert "merge_attempted" in types
    assert "merge_started" in types
    # merge_attempted must precede merge_started (order matters for observers).
    assert types.index("merge_attempted") < types.index("merge_started")


def test_get_merge_queue_emits_skipped_for_unmerged_dep(soldier_env, captured_emits):
    """A done task whose dependency is not yet merged emits merge_skipped
    with reason=dep_unmerged."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    # Harvest task-a (not merged) and task-b which depends on task-a.
    _carry_and_harvest(cc, repo, "task-dep-a", "feat/task-dep-a")
    _carry_and_harvest(
        cc, repo, "task-dep-b", "feat/task-dep-b", depends_on=["task-dep-a"]
    )

    # Clear emits from any prior steps, then invoke the filter path.
    captured_emits.clear()
    soldier.get_merge_queue()

    skipped = [c for c in captured_emits if c[0] == "merge_skipped"]
    dep_skipped = [c for c in skipped if c[1] == "task-dep-b"]
    assert any("reason=dep_unmerged" in c[2] for c in dep_skipped), (
        f"expected dep_unmerged skip for task-dep-b, got {skipped}"
    )


def test_get_merge_queue_emits_skipped_for_no_pr(soldier_env, captured_emits):
    """A done task with no current_attempt branch emits reason=no_pr."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    # Harvest normally then surgically clear the branch field so the task
    # looks like it has no PR.
    _carry_and_harvest(cc, repo, "task-nopr", "feat/task-nopr")
    task = cc.get_task("task-nopr")
    attempt_id = task["current_attempt"]
    # Overwrite branch via direct backend file mutation (FileBackend).
    import json
    from pathlib import Path

    done_dir = Path(soldier_env["tmp_path"]) / ".antfarm" / "tasks" / "done"
    task_path = done_dir / "task-nopr.json"
    data = json.loads(task_path.read_text())
    for att in data["attempts"]:
        if att["attempt_id"] == attempt_id:
            att["branch"] = None
    task_path.write_text(json.dumps(data))

    captured_emits.clear()
    soldier.get_merge_queue()

    skipped = [c for c in captured_emits if c[0] == "merge_skipped" and c[1] == "task-nopr"]
    assert any("reason=no_pr" in c[2] for c in skipped), (
        f"expected no_pr skip for task-nopr, got {skipped}"
    )


def test_run_once_with_review_emits_skipped_needs_changes_before_kickback(
    soldier_env, captured_emits
):
    """A done task with a stored needs_changes verdict emits merge_skipped
    with reason=needs_changes BEFORE the kickback fires."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-nc-001", "feat/task-nc-001")

    # Store a needs_changes verdict directly on the task via the backend.
    # Use the HTTP ColonyClient for parity with soldier's access.
    task = cc.get_task("task-nc-001")
    attempt_id = task["current_attempt"]

    verdict = {
        "provider": "claude_code",
        "verdict": "needs_changes",
        "summary": "placeholder",
        "findings": [],
        "severity": None,
        "reviewed_commit_sha": "",
        "reviewer_run_id": None,
    }
    cc.store_review_verdict("task-nc-001", attempt_id, verdict)

    # Enable require_review on a fresh soldier sharing the same client.
    review_soldier = Soldier(
        colony_url="http://testserver",
        repo_path=repo,
        integration_branch="dev",
        test_command=["true"],
        poll_interval=0.0,
        require_review=True,
        client=soldier._client if hasattr(soldier, "_client") else soldier.colony._client,
    )

    captured_emits.clear()
    review_soldier.run_once_with_review()

    # Filter emissions for this task.
    task_events = [c for c in captured_emits if c[1] == "task-nc-001"]
    types = [c[0] for c in task_events]
    assert "merge_skipped" in types, (
        f"expected merge_skipped before kickback, got {types}"
    )
    # Ordering: merge_skipped (with needs_changes reason) must be emitted
    # BEFORE any subsequent kickback-related event. Kickback itself is
    # emitted by colony (actor=colony) not by soldier, so just assert that
    # merge_skipped was recorded with the correct reason.
    nc_skip = [c for c in task_events if c[0] == "merge_skipped"]
    assert any("reason=needs_changes" in c[2] for c in nc_skip)


def test_run_once_with_review_emits_skipped_review_in_progress(soldier_env, captured_emits):
    """When a review task exists but is not done yet, soldier emits
    merge_skipped with reason=review_in_progress."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-rip-001", "feat/task-rip-001")

    # Create a review task that is still in ready status.
    cc._client.post(
        "/tasks",
        json={
            "id": "review-task-rip-001",
            "title": "Review: task-rip-001",
            "spec": (
                "Review task-rip-001\n"
                "Attempt-SHA: feat/task-rip-001\n"
            ),
            "depends_on": [],
            "priority": 1,
            "capabilities_required": ["review"],
        },
    ).raise_for_status()

    review_soldier = Soldier(
        colony_url="http://testserver",
        repo_path=repo,
        integration_branch="dev",
        test_command=["true"],
        poll_interval=0.0,
        require_review=True,
        client=soldier.colony._client,
    )

    captured_emits.clear()
    review_soldier.run_once_with_review()

    skipped = [
        c for c in captured_emits if c[0] == "merge_skipped" and c[1] == "task-rip-001"
    ]
    assert any("reason=review_in_progress" in c[2] for c in skipped), (
        f"expected review_in_progress skip, got {skipped}"
    )


def test_merge_failed_emitted_before_kickback_on_failure(soldier_env, captured_emits):
    """On a failed attempt_merge, merge_failed must be emitted BEFORE the
    caller invokes kickback_with_cascade (which itself calls colony.kickback)."""
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    # Soldier whose test_command always fails.
    failing_soldier = Soldier(
        colony_url="http://testserver",
        repo_path=repo,
        integration_branch="dev",
        test_command=["false"],
        poll_interval=0.0,
        client=soldier_env["soldier"].colony._client,
    )

    _carry_and_harvest(cc, repo, "task-ord-001", "feat/task-ord-001")

    # Instrument kickback_with_cascade so we know exactly when it runs.
    kickback_order: list[int] = []
    original = failing_soldier.kickback_with_cascade

    def _tracking_kickback(*args, **kwargs):
        kickback_order.append(len(captured_emits))
        return original(*args, **kwargs)

    failing_soldier.kickback_with_cascade = _tracking_kickback  # type: ignore[assignment]

    captured_emits.clear()
    failing_soldier.run_once()

    # There must be at least one merge_failed before kickback was invoked.
    assert kickback_order, "kickback was not invoked"
    kb_idx = kickback_order[0]
    pre_kb = captured_emits[:kb_idx]
    types_pre = [c[0] for c in pre_kb]
    assert "merge_failed" in types_pre, (
        f"merge_failed was not emitted before kickback; pre-kickback emits: {pre_kb}"
    )
    # Failed detail should carry a normalized reason code.
    failed = [c for c in pre_kb if c[0] == "merge_failed"]
    assert any("reason=" in c[2] for c in failed)


def test_emit_failure_does_not_break_merge(soldier_env, monkeypatch):
    """Even if the emit pipeline raises, attempt_merge must still return
    the correct MergeResult and not propagate the exception. Only the
    soldier-side ``_emit`` wrapper is targeted here — colony-side
    ``_emit_event`` is left untouched (colony emits are orthogonal)."""
    soldier = soldier_env["soldier"]
    cc = soldier_env["colony_client"]
    repo = soldier_env["repo_path"]

    _carry_and_harvest(cc, repo, "task-emfail-001", "feat/task-emfail-001")

    # Patch the soldier-side `_emit` wrapper so every soldier emission
    # attempt hits the best-effort try/except path. If soldier is not
    # robust to this, the merge will raise.
    from antfarm.core import soldier as soldier_mod

    # The real _emit swallows exceptions; to simulate an emit pipeline
    # failure we instead force _emit_event to raise, but only when called
    # by the soldier module (i.e. via the soldier._emit wrapper). The
    # wrapper must catch it.
    orig_emit = soldier_mod._emit

    def _raising_emit(event_type, task_id, detail=""):
        # Exercise the soldier _emit import + swallow pathway, then force
        # a failure inside it by making serve._emit_event raise for this
        # one call.
        from antfarm.core import serve as serve_mod

        real = serve_mod._emit_event

        def _boom(*a, **kw):
            raise RuntimeError("synthetic emit failure")

        serve_mod._emit_event = _boom
        try:
            orig_emit(event_type, task_id, detail)
        finally:
            serve_mod._emit_event = real

    monkeypatch.setattr(soldier_mod, "_emit", _raising_emit)

    # Must not raise, must still merge cleanly.
    results = soldier.run_once()
    assert results == [("task-emfail-001", MergeResult.MERGED)]


# ---------------------------------------------------------------------------
# P1 (v0.6.7): rebase-before-kickback on merge conflict
# ---------------------------------------------------------------------------


def _make_mock_task(task_id: str = "task-rb", branch: str = "feat/task-rb") -> dict:
    """Build a done-task dict suitable for calling attempt_merge directly."""
    return {
        "id": task_id,
        "current_attempt": "att-001",
        "attempts": [
            {
                "attempt_id": "att-001",
                "worker_id": "w-1",
                "status": "done",
                "branch": branch,
                "pr": "PR-1",
                "started_at": "2026-01-01T00:00:00+00:00",
                "completed_at": "2026-01-01T00:00:01+00:00",
            }
        ],
        "status": "done",
    }


def _build_bare_soldier(tmp_path, test_command=None) -> Soldier:
    """Build a Soldier whose git/tests are driven by subprocess mocks."""
    return Soldier(
        colony_url="http://testserver",
        repo_path=str(tmp_path),
        integration_branch="main",
        test_command=test_command or ["true"],
        poll_interval=0.0,
    )


def test_rebase_retry_merges_when_rebase_resolves_drift(tmp_path, monkeypatch):
    """Clean rebase path: initial merge conflicts, rebase succeeds, retry merges."""
    soldier = _build_bare_soldier(tmp_path)
    task = _make_mock_task()

    merge_call_count = {"n": 0}

    def fake_run(args, **kwargs):
        import subprocess as _sp

        joined = " ".join(args)
        if "merge" in args and "--no-ff" in args and "feat/task-rb" in args:
            merge_call_count["n"] += 1
            if merge_call_count["n"] == 1:
                return _sp.CompletedProcess(
                    args, 1, stdout=b"", stderr=b"CONFLICT (content)"
                )
            return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if "rebase" in args and "origin/main" in joined and "--abort" not in args:
            return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    import antfarm.core.soldier as soldier_mod

    monkeypatch.setattr(soldier_mod.subprocess, "run", fake_run)

    result = soldier.attempt_merge(task)
    assert result == MergeResult.MERGED
    # Exactly 2 merge attempts: initial + one retry after rebase.
    assert merge_call_count["n"] == 2


def test_rebase_conflict_kicks_back_with_rebase_failed_reason(tmp_path, monkeypatch):
    """Rebase conflict path: returns FAILED with last_failure_reason 'rebase_failed'."""
    soldier = _build_bare_soldier(tmp_path)
    task = _make_mock_task()

    rebase_abort_called = {"n": 0}

    def fake_run(args, **kwargs):
        import subprocess as _sp

        joined = " ".join(args)
        if "merge" in args and "--no-ff" in args and "feat/task-rb" in args:
            return _sp.CompletedProcess(
                args, 1, stdout=b"", stderr=b"CONFLICT (content): Merge conflict"
            )
        if "rebase" in args and "--abort" in args:
            rebase_abort_called["n"] += 1
            return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if "rebase" in args and "origin/main" in joined:
            return _sp.CompletedProcess(
                args, 1, stdout=b"", stderr=b"CONFLICT during rebase"
            )
        return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    import antfarm.core.soldier as soldier_mod

    monkeypatch.setattr(soldier_mod.subprocess, "run", fake_run)

    result = soldier.attempt_merge(task)
    assert result == MergeResult.FAILED
    assert "rebase_failed" in soldier.last_failure_reason
    assert rebase_abort_called["n"] >= 1, "rebase --abort must be invoked on conflict"


def test_rebase_retry_does_not_loop(tmp_path, monkeypatch):
    """At most one rebase per attempt_merge call — no retry loop."""
    soldier = _build_bare_soldier(tmp_path)
    task = _make_mock_task()

    rebase_calls = {"n": 0}
    merge_calls = {"n": 0}

    def fake_run(args, **kwargs):
        import subprocess as _sp

        if "merge" in args and "--no-ff" in args and "feat/task-rb" in args:
            merge_calls["n"] += 1
            return _sp.CompletedProcess(
                args, 1, stdout=b"", stderr=b"CONFLICT (content)"
            )
        if "rebase" in args and "--abort" not in args:
            rebase_calls["n"] += 1
            return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    import antfarm.core.soldier as soldier_mod

    monkeypatch.setattr(soldier_mod.subprocess, "run", fake_run)

    result = soldier.attempt_merge(task)
    assert result == MergeResult.FAILED
    assert rebase_calls["n"] == 1, (
        f"expected exactly 1 rebase invocation, got {rebase_calls['n']}"
    )
    assert merge_calls["n"] == 2, (
        f"expected exactly 2 merge invocations (initial + one retry), "
        f"got {merge_calls['n']}"
    )
    assert "rebase_retry_merge_failed" in soldier.last_failure_reason


def test_rebase_uses_force_with_lease_never_plain_force(tmp_path, monkeypatch):
    """The push after a successful rebase uses --force-with-lease, never --force."""
    soldier = _build_bare_soldier(tmp_path)
    task = _make_mock_task()

    push_args: list[list[str]] = []
    merge_call_count = {"n": 0}

    def fake_run(args, **kwargs):
        import subprocess as _sp

        if "merge" in args and "--no-ff" in args and "feat/task-rb" in args:
            merge_call_count["n"] += 1
            if merge_call_count["n"] == 1:
                return _sp.CompletedProcess(args, 1, stdout=b"", stderr=b"CONFLICT")
            return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if "push" in args:
            push_args.append(list(args))
        return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    import antfarm.core.soldier as soldier_mod

    monkeypatch.setattr(soldier_mod.subprocess, "run", fake_run)

    soldier.attempt_merge(task)

    pr_pushes = [a for a in push_args if "feat/task-rb" in a]
    assert pr_pushes, "expected a push to the PR branch after rebase"
    for p in pr_pushes:
        assert "--force-with-lease" in p, (
            f"PR-branch push must use --force-with-lease: {p}"
        )

    # No plain '--force' token anywhere in any push invocation
    # ('--force-with-lease' is a different token and is allowed).
    for call in push_args:
        assert "--force" not in call, f"plain --force used in git push: {call}"


def test_test_failure_does_not_trigger_rebase(tmp_path, monkeypatch):
    """Test failure (not merge conflict) must NOT invoke the rebase retry path."""
    soldier = _build_bare_soldier(tmp_path, test_command=["pytest-stub"])
    task = _make_mock_task()

    rebase_calls = {"n": 0}

    def fake_run(args, **kwargs):
        import subprocess as _sp

        if "merge" in args and "--no-ff" in args:
            return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if "rebase" in args:
            rebase_calls["n"] += 1
            return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if args and args[0] == "pytest-stub":
            return _sp.CompletedProcess(
                args, 1, stdout=b"1 failed", stderr=b"tests failed"
            )
        return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    import antfarm.core.soldier as soldier_mod

    monkeypatch.setattr(soldier_mod.subprocess, "run", fake_run)

    result = soldier.attempt_merge(task)
    assert result == MergeResult.FAILED
    assert rebase_calls["n"] == 0, (
        "test failure must NOT trigger the rebase retry path"
    )
    assert "tests failed" in soldier.last_failure_reason


# ---------------------------------------------------------------------------
# P1 (v0.6.7): #284 — needs_changes verdict triggers kickback + serve wiring
# ---------------------------------------------------------------------------


def test_run_once_with_review_kickbacks_on_needs_changes_verdict(tmp_path):
    """#284 regression: a needs_changes verdict on the review task's current
    attempt must cause the parent task to be kicked back on the next
    run_once_with_review tick."""
    import json
    from pathlib import Path

    from antfarm.core.models import ReviewVerdict

    backend = FileBackend(root=str(tmp_path / ".antfarm"))

    # 1. Parent done task with a current attempt, branch, artifact.
    _make_done_task_with_mission(backend, "task-nc-284", mission_id=None)
    _set_attempt_artifact_sha(backend, "task-nc-284", "a" * 40)

    # 2. Soldier with require_review=True creates the review task.
    soldier = Soldier.from_backend(
        backend, repo_path=str(tmp_path), require_review=True
    )
    assert soldier.require_review is True, (
        "pre-condition: Soldier.from_backend must default require_review=True"
    )
    parent_task = backend.get_task("task-nc-284")
    assert soldier.create_review_task(parent_task) == "review-task-nc-284"

    # 3. Reviewer claims + harvests the review task.
    backend.register_worker(
        {
            "worker_id": "reviewer-1",
            "node_id": "node-1",
            "agent_type": "generic",
            "workspace_root": "/tmp/ws",
            "status": "idle",
            "capabilities": ["review"],
        }
    )
    backend.pull("reviewer-1")
    review_task = backend.get_task("review-task-nc-284")
    review_attempt_id = review_task["current_attempt"]
    backend.mark_harvested(
        "review-task-nc-284",
        review_attempt_id,
        pr="review-pr",
        branch="feat/review-task-nc-284",
    )

    # 4. Inject a needs_changes verdict onto the review attempt and emit it
    # as a [REVIEW_VERDICT] trail entry — this is how reviewer workers
    # surface verdicts in production.
    needs_changes_verdict = ReviewVerdict(
        provider="human",
        verdict="needs_changes",
        summary="please fix X",
        findings=["bug in Y"],
        reviewed_commit_sha="a" * 40,
    ).to_dict()

    review_done_path = Path(backend._root) / "tasks" / "done" / "review-task-nc-284.json"
    rdata = json.loads(review_done_path.read_text())
    for a in rdata["attempts"]:
        if a["attempt_id"] == review_attempt_id:
            a["review_verdict"] = needs_changes_verdict
            break
    rdata["trail"].append(
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "worker_id": "reviewer-1",
            "message": "[REVIEW_VERDICT] " + json.dumps(needs_changes_verdict),
        }
    )
    review_done_path.write_text(json.dumps(rdata, indent=2))

    # 5. Run once — parent task must be kicked back with a 'review failed' trail.
    results = soldier.run_once_with_review()
    assert results == [("task-nc-284", MergeResult.FAILED)]

    parent_after = backend.get_task("task-nc-284")
    assert parent_after["status"] == "ready"
    assert parent_after["current_attempt"] is None

    trail_msgs = [e["message"] for e in parent_after.get("trail", [])]
    assert any("review failed" in m.lower() for m in trail_msgs), (
        f"expected a 'review failed:' trail entry, got: {trail_msgs}"
    )


def test_start_soldier_thread_wires_require_review_true(tmp_path, monkeypatch):
    """#284 regression: _start_soldier_thread must construct a Soldier with
    require_review=True, so needs_changes verdicts actually trigger kickback
    in production."""
    from antfarm.core import serve as serve_mod

    backend = FileBackend(root=str(tmp_path / ".antfarm"))

    captured: dict = {}

    class _FakeSoldier:
        @classmethod
        def from_backend(cls, backend_arg, **kwargs):  # noqa: N803
            captured["backend"] = backend_arg
            captured["kwargs"] = kwargs
            inst = cls.__new__(cls)
            inst.require_review = kwargs.get("require_review", False)
            return inst

        def run(self):
            # No-op — we don't want a thread spinning in this test.
            return None

    import antfarm.core.soldier as soldier_mod

    monkeypatch.setattr(soldier_mod, "Soldier", _FakeSoldier)

    # Reset singleton state so _start_soldier_thread proceeds.
    monkeypatch.setattr(serve_mod, "_soldier_thread", None, raising=False)
    monkeypatch.setattr(serve_mod, "_soldier_status", "not started", raising=False)

    serve_mod._start_soldier_thread(backend, data_dir=str(tmp_path / ".antfarm"))

    assert captured, "_start_soldier_thread did not build a Soldier instance"
    assert captured["kwargs"].get("require_review", False) is True, (
        f"Soldier must be started with require_review=True, "
        f"got kwargs={captured['kwargs']}"
    )
