"""End-to-end integration test for the full Antfarm coordination cycle.

Proves that register → carry → forage → trail → harvest → merge → unblock
works correctly with a real git repo and in-process colony.
"""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from antfarm.core.backends.file import FileBackend
from antfarm.core.colony_client import ColonyClient
from antfarm.core.doctor import run_doctor
from antfarm.core.serve import get_app
from antfarm.core.soldier import MergeResult, Soldier

# ---------------------------------------------------------------------------
# Helpers (same as test_soldier.py)
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
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_env(tmp_path):
    """Set up a full E2E test environment.

    Creates:
    - bare origin repo
    - working clone with git identity configured + initial commit on dev
    - FileBackend + Colony via TestClient
    - ColonyClient injected with TestClient transport
    - Soldier pointing at the working clone
    """
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"

    # bare origin
    _git(["git", "init", "--bare", str(origin)], cwd=str(tmp_path))

    # working clone
    _git(["git", "clone", str(origin), str(clone)], cwd=str(tmp_path))
    _configure_git(str(clone))

    # initial commit + dev branch
    _commit_file(str(clone), "README.md", "antfarm e2e test repo\n", "init")
    _git(["git", "push", "origin", "HEAD:dev"], cwd=str(clone))
    _git(["git", "fetch", "origin"], cwd=str(clone))

    # Create local dev branch tracking origin/dev
    _git(["git", "checkout", "-b", "dev", "origin/dev"], cwd=str(clone), check=False)
    _git(["git", "checkout", "dev"], cwd=str(clone), check=False)
    _git(
        ["git", "branch", "--set-upstream-to=origin/dev", "dev"],
        cwd=str(clone),
        check=False,
    )

    # colony
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend)
    http_client = TestClient(app, raise_server_exceptions=True)

    colony_client = ColonyClient("http://testserver", client=http_client)

    soldier = Soldier(
        colony_url="http://testserver",
        repo_path=str(clone),
        integration_branch="dev",
        test_command=["true"],
        poll_interval=0.0,
        client=http_client,
    )

    yield {
        "soldier": soldier,
        "colony_client": colony_client,
        "repo_path": str(clone),
        "origin_path": str(origin),
        "tmp_path": str(tmp_path),
        "backend": backend,
    }


# ---------------------------------------------------------------------------
# E2E Test
# ---------------------------------------------------------------------------


def test_e2e_full_loop(e2e_env):
    """Full coordination cycle: register → carry → forage → trail → harvest → merge.

    Steps:
    1. Register node
    2. Carry task-001 (no deps)
    3. Carry task-002 (depends on task-001)
    4. Register worker
    5. Forage → task-001 (task-002 is blocked by dep)
    6. Append trail "halfway done"
    7. Create git branch, commit, push, harvest task-001
    8. Soldier merges task-001
    9. Forage → task-002 (now unblocked)
    10. Create git branch, commit, push, harvest task-002
    11. Soldier merges task-002
    12. Status: 0 ready, 0 active, 2 done
    13. Doctor: no error findings
    """
    cc = e2e_env["colony_client"]
    soldier = e2e_env["soldier"]
    repo = e2e_env["repo_path"]
    backend = e2e_env["backend"]

    # --- Step 1: Register node ---
    cc.register_node("node-e2e")

    # --- Step 2: Carry task-001 (no deps) ---
    cc._client.post(
        "/tasks",
        json={
            "id": "task-001",
            "title": "Task 001",
            "spec": "first task",
            "depends_on": [],
            "priority": 10,
        },
    ).raise_for_status()

    # --- Step 3: Carry task-002 (depends on task-001) ---
    cc._client.post(
        "/tasks",
        json={
            "id": "task-002",
            "title": "Task 002",
            "spec": "second task, needs first",
            "depends_on": ["task-001"],
            "priority": 10,
        },
    ).raise_for_status()

    # --- Step 4: Register worker ---
    cc.register_worker(
        worker_id="worker-e2e",
        node_id="node-e2e",
        agent_type="generic",
        workspace_root="/tmp/e2e-ws",
    )

    # --- Step 5: Forage → task-001 (task-002 blocked) ---
    task = cc.forage("worker-e2e")
    assert task is not None, "forage should return task-001"
    assert task["id"] == "task-001", f"expected task-001, got {task['id']}"
    attempt_id_001 = task["current_attempt"]

    # task-002 must be blocked — forage should return None (queue empty)
    task_check = cc.forage("worker-e2e")
    assert task_check is None, "task-002 should be blocked; forage should return nothing"

    # --- Step 6: Append trail "halfway done" ---
    cc.trail("task-001", "worker-e2e", "halfway done")

    task_state = cc.get_task("task-001")
    trail_messages = [t["message"] for t in task_state.get("trail", [])]
    assert "halfway done" in trail_messages

    # --- Step 7: Create branch, commit, push, harvest task-001 ---
    branch_001 = f"feat/task-001-{attempt_id_001}"
    _git(["git", "checkout", "-b", branch_001, "origin/dev"], cwd=repo)
    _commit_file(repo, "task-001.txt", "work for task 001\n", "work for task-001")
    _git(["git", "push", "origin", branch_001], cwd=repo)
    _git(["git", "checkout", "dev"], cwd=repo)

    cc.harvest(
        task_id="task-001",
        attempt_id=attempt_id_001,
        pr="https://github.com/x/y/pull/1",
        branch=branch_001,
    )

    # --- Step 8: Soldier merges task-001 ---
    results = soldier.run_once()
    assert results == [("task-001", MergeResult.MERGED)], f"expected task-001 MERGED, got {results}"

    # dev branch should now include task-001 work
    log = _git(["git", "log", "--oneline", "dev"], cwd=repo)
    assert "work for task-001" in log.stdout

    # --- Step 9: Forage → task-002 (now unblocked) ---
    # After soldier merges, update clone's dev so subsequent branches start from updated origin/dev
    _git(["git", "fetch", "origin"], cwd=repo)
    _git(["git", "reset", "--hard", "origin/dev"], cwd=repo)

    task2 = cc.forage("worker-e2e")
    assert task2 is not None, "task-002 should now be available after task-001 merged"
    assert task2["id"] == "task-002", f"expected task-002, got {task2['id']}"
    attempt_id_002 = task2["current_attempt"]

    # --- Step 10: Create branch, commit, push, harvest task-002 ---
    branch_002 = f"feat/task-002-{attempt_id_002}"
    _git(["git", "checkout", "-b", branch_002, "origin/dev"], cwd=repo)
    _commit_file(repo, "task-002.txt", "work for task 002\n", "work for task-002")
    _git(["git", "push", "origin", branch_002], cwd=repo)
    _git(["git", "checkout", "dev"], cwd=repo)

    cc.harvest(
        task_id="task-002",
        attempt_id=attempt_id_002,
        pr="https://github.com/x/y/pull/2",
        branch=branch_002,
    )

    # --- Step 11: Soldier merges task-002 ---
    # run_once processes the full queue; task-001 may re-appear (still done+branch),
    # so assert task-002 is present and merged rather than checking the exact list.
    results2 = soldier.run_once()
    results2_dict = dict(results2)
    assert "task-002" in results2_dict, f"task-002 not in results: {results2}"
    assert results2_dict["task-002"] == MergeResult.MERGED, f"task-002 not MERGED: {results2}"

    log2 = _git(["git", "log", "--oneline", "dev"], cwd=repo)
    assert "work for task-002" in log2.stdout

    # --- Step 12: Status check: 0 ready, 0 active, 2 done ---
    ready_tasks = cc.list_tasks(status="ready")
    active_tasks = cc.list_tasks(status="active")
    done_tasks = cc.list_tasks(status="done")

    assert len(ready_tasks) == 0, f"expected 0 ready, got {ready_tasks}"
    assert len(active_tasks) == 0, f"expected 0 active, got {active_tasks}"
    assert len(done_tasks) == 2, f"expected 2 done, got {done_tasks}"

    done_ids = {t["id"] for t in done_tasks}
    assert done_ids == {"task-001", "task-002"}

    # --- Step 13: Doctor finds no error findings ---
    config = {
        "data_dir": str(e2e_env["tmp_path"]) + "/.antfarm",
    }
    findings = run_doctor(backend, config)
    error_findings = [f for f in findings if f.severity == "error"]
    assert error_findings == [], (
        f"Doctor found error findings: {[(f.check, f.message) for f in error_findings]}"
    )
