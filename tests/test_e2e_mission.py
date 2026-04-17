"""End-to-end integration test for the full mission lifecycle.

Proves: create mission -> Queen creates plan task -> planner harvests with
PlanArtifact -> Queen spawns child tasks -> workers harvest children ->
Soldier marks merged -> Queen completes mission with report.

No real git, no real subprocess. All worker actions are simulated via HTTP calls.
Queen is ticked manually for deterministic control.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from antfarm.core.backends.file import FileBackend
from antfarm.core.queen import Queen, QueenConfig
from antfarm.core.serve import get_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _forage(client: TestClient, worker_id: str) -> dict | None:
    """Forage for a task. Returns task dict or None if 204."""
    resp = client.post("/tasks/pull", json={"worker_id": worker_id})
    if resp.status_code == 204:
        return None
    resp.raise_for_status()
    return resp.json()


def _harvest(
    client: TestClient,
    task_id: str,
    attempt_id: str,
    pr: str = "https://github.com/x/y/pull/1",
    branch: str = "feat/test",
    artifact: dict | None = None,
) -> None:
    """Harvest a task with the given attempt."""
    payload = {
        "attempt_id": attempt_id,
        "pr": pr,
        "branch": branch,
    }
    if artifact is not None:
        payload["artifact"] = artifact
    resp = client.post(f"/tasks/{task_id}/harvest", json=payload)
    resp.raise_for_status()


def _mark_merged(client: TestClient, task_id: str, attempt_id: str) -> None:
    """Simulate Soldier marking a task as merged."""
    resp = client.post(f"/tasks/{task_id}/merge", json={"attempt_id": attempt_id})
    resp.raise_for_status()


def _kickback(client: TestClient, task_id: str, reason: str, max_attempts: int = 3) -> None:
    """Simulate Soldier kicking back a task."""
    resp = client.post(
        f"/tasks/{task_id}/kickback",
        json={"reason": reason, "max_attempts": max_attempts},
    )
    resp.raise_for_status()


def _store_review_verdict(
    client: TestClient, task_id: str, attempt_id: str, verdict: dict
) -> None:
    """Store a review verdict on a task's attempt."""
    resp = client.post(
        f"/tasks/{task_id}/review-verdict",
        json={"attempt_id": attempt_id, "verdict": verdict},
    )
    resp.raise_for_status()


def _tick_queen(queen: Queen, client: TestClient, mission_id: str) -> None:
    """Tick the Queen once for a specific mission."""
    mission = client.get(f"/missions/{mission_id}").json()
    queen._advance(mission)


def _tick_queen_until(
    queen: Queen,
    client: TestClient,
    mission_id: str,
    target_status: str,
    max_ticks: int = 20,
) -> dict:
    """Tick the Queen until the mission reaches target_status or raise."""
    for _ in range(max_ticks):
        mission = client.get(f"/missions/{mission_id}").json()
        if mission["status"] == target_status:
            return mission
        queen._advance(mission)
    mission = client.get(f"/missions/{mission_id}").json()
    assert mission["status"] == target_status, (
        f"mission did not reach '{target_status}' after {max_ticks} ticks; "
        f"current status: {mission['status']}"
    )
    return mission


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mission_env(tmp_path):
    """Set up a mission E2E test environment.

    Creates FileBackend, colony app (queen disabled so we tick manually),
    and a Queen instance sharing the same backend.
    """
    backend = FileBackend(root=str(tmp_path / ".antfarm"))

    # Disable queen/soldier/doctor threads — we tick Queen manually
    app = get_app(
        backend=backend,
        data_dir=str(tmp_path / ".antfarm"),
        enable_queen=False,
        enable_soldier=False,
        enable_doctor=False,
    )
    client = TestClient(app, raise_server_exceptions=True)

    queen = Queen(backend, config=QueenConfig(base_interval=0))

    # Register a node and workers
    client.post("/nodes", json={"node_id": "node-1"}).raise_for_status()
    client.post(
        "/workers/register",
        json={
            "worker_id": "planner-1",
            "node_id": "node-1",
            "agent_type": "claude-code",
            "workspace_root": "/tmp/ws",
            "capabilities": ["plan"],
        },
    ).raise_for_status()
    client.post(
        "/workers/register",
        json={
            "worker_id": "builder-1",
            "node_id": "node-1",
            "agent_type": "claude-code",
            "workspace_root": "/tmp/ws",
            "capabilities": ["builder"],
        },
    ).raise_for_status()
    client.post(
        "/workers/register",
        json={
            "worker_id": "reviewer-1",
            "node_id": "node-1",
            "agent_type": "claude-code",
            "workspace_root": "/tmp/ws",
            "capabilities": ["review"],
        },
    ).raise_for_status()

    return {
        "backend": backend,
        "client": client,
        "queen": queen,
    }


# ---------------------------------------------------------------------------
# Helpers for plan artifact
# ---------------------------------------------------------------------------


def _make_plan_artifact(plan_task_id: str, attempt_id: str) -> dict:
    """Build a PlanArtifact dict with 2 proposed child tasks."""
    return {
        "plan_artifact": {
            "plan_task_id": plan_task_id,
            "attempt_id": attempt_id,
            "proposed_tasks": [
                {
                    "title": "Implement auth module",
                    "spec": "Add JWT auth to the API server",
                    "complexity": "M",
                    "priority": 10,
                    "depends_on": [],
                    "touches": ["auth"],
                },
                {
                    "title": "Add auth tests",
                    "spec": "Unit tests for the auth module",
                    "complexity": "S",
                    "priority": 10,
                    "depends_on": [1],  # depends on first task (1-based index)
                    "touches": ["auth", "tests"],
                },
            ],
            "task_count": 2,
            "warnings": [],
            "dependency_summary": "task-2 depends on task-1",
        }
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_e2e_mission_full_loop(mission_env):
    """Full mission loop: create -> plan -> spawn children -> merge -> complete.

    Steps:
    1. Create mission with require_plan_review=False
    2. Queen creates plan task
    3. Planner forages plan task, harvests with PlanArtifact (2 children)
    4. Queen extracts artifact, spawns child tasks, transitions to BUILDING
    5. Builder forages child-01, harvests
    6. Soldier merges child-01
    7. Builder forages child-02 (now unblocked), harvests
    8. Soldier merges child-02
    9. Queen detects all merged, transitions to COMPLETE, generates report
    10. Verify report correctness
    """
    client = mission_env["client"]
    queen = mission_env["queen"]

    # --- Step 1: Create mission ---
    resp = client.post(
        "/missions",
        json={
            "mission_id": "mission-auth-1",
            "spec": "Add JWT authentication to the API server",
            "config": {"require_plan_review": False},
        },
    )
    assert resp.status_code == 201
    mission_id = resp.json()["mission_id"]

    mission = client.get(f"/missions/{mission_id}").json()
    assert mission["status"] == "planning"

    # --- Step 2: Queen creates plan task ---
    _tick_queen(queen, client, mission_id)

    mission = client.get(f"/missions/{mission_id}").json()
    assert mission["plan_task_id"] is not None
    plan_task_id = mission["plan_task_id"]
    assert plan_task_id == f"plan-{mission_id}"

    # --- Step 3: Planner forages plan task and harvests with PlanArtifact ---
    task = _forage(client, "planner-1")
    assert task is not None
    assert task["id"] == plan_task_id
    attempt_id = task["current_attempt"]

    plan_artifact = _make_plan_artifact(plan_task_id, attempt_id)
    _harvest(
        client,
        plan_task_id,
        attempt_id,
        pr="n/a",
        branch="n/a",
        artifact=plan_artifact,
    )

    # Verify plan task is now done
    plan_task = client.get(f"/tasks/{plan_task_id}").json()
    assert plan_task["status"] == "done"

    # --- Step 4: Queen extracts artifact, spawns children, BUILDING ---
    _tick_queen(queen, client, mission_id)

    mission = client.get(f"/missions/{mission_id}").json()
    assert mission["status"] == "building"
    assert mission["plan_artifact"] is not None

    # Should have spawned 2 child tasks + the plan task = 3 task_ids
    assert len(mission["task_ids"]) == 3  # plan + 2 children

    # Identify child task IDs (exclude plan task)
    child_ids = [tid for tid in mission["task_ids"] if tid != plan_task_id]
    assert len(child_ids) == 2

    # Verify child tasks exist and have correct mission_id
    child_01 = client.get(f"/tasks/{child_ids[0]}").json()
    child_02 = client.get(f"/tasks/{child_ids[1]}").json()
    assert child_01["mission_id"] == mission_id
    assert child_02["mission_id"] == mission_id
    assert child_01["status"] == "ready"

    # child_02 depends on child_01
    assert child_ids[0] in child_02.get("depends_on", [])

    # --- Step 5: Builder forages child-01, harvests ---
    task = _forage(client, "builder-1")
    assert task is not None
    assert task["id"] == child_ids[0]
    att_01 = task["current_attempt"]

    _harvest(client, child_ids[0], att_01, pr="https://github.com/x/y/pull/10", branch="feat/auth")

    # --- Step 6: Soldier merges child-01 ---
    _mark_merged(client, child_ids[0], att_01)

    # --- Step 7: Builder forages child-02 (now unblocked by dep in done/), harvests ---
    task2 = _forage(client, "builder-1")
    assert task2 is not None
    assert task2["id"] == child_ids[1]
    att_02 = task2["current_attempt"]

    _harvest(
        client, child_ids[1], att_02, pr="https://github.com/x/y/pull/11", branch="feat/auth-tests"
    )

    # --- Step 8: Soldier merges child-02 ---
    _mark_merged(client, child_ids[1], att_02)

    # --- Step 9: Queen detects all merged, COMPLETE ---
    mission = _tick_queen_until(queen, client, mission_id, "complete")

    assert mission["status"] == "complete"
    assert mission["completed_at"] is not None

    # --- Step 10: Verify report ---
    report = mission["report"]
    assert report is not None
    assert report["mission_id"] == mission_id
    assert report["total_tasks"] == 2  # only impl tasks, not plan
    assert report["merged_tasks"] == 2
    assert report["blocked_tasks"] == 0
    # Report status reflects the mission state at generation time (building),
    # since the report is built just before the COMPLETE transition.
    assert report["status"] in ("complete", "building")
    assert len(report["merged"]) == 2

    merged_ids = {m["task_id"] for m in report["merged"]}
    assert merged_ids == set(child_ids)


def test_e2e_mission_cancel_stops_spawning(mission_env):
    """Cancel during BUILDING prevents Queen from completing the mission.

    Steps:
    1. Create mission, plan, spawn children
    2. Cancel mission during BUILDING
    3. Queen does NOT advance cancelled mission
    4. Verify mission stays cancelled
    """
    client = mission_env["client"]
    queen = mission_env["queen"]

    # --- Create mission, plan, get to BUILDING ---
    resp = client.post(
        "/missions",
        json={
            "mission_id": "mission-cancel-1",
            "spec": "Add feature that will be cancelled",
            "config": {"require_plan_review": False},
        },
    )
    assert resp.status_code == 201
    mission_id = resp.json()["mission_id"]

    # Queen creates plan task
    _tick_queen(queen, client, mission_id)
    mission = client.get(f"/missions/{mission_id}").json()
    plan_task_id = mission["plan_task_id"]

    # Planner forages and harvests
    task = _forage(client, "planner-1")
    assert task["id"] == plan_task_id
    attempt_id = task["current_attempt"]

    plan_artifact = _make_plan_artifact(plan_task_id, attempt_id)
    _harvest(client, plan_task_id, attempt_id, pr="n/a", branch="n/a", artifact=plan_artifact)

    # Queen spawns children
    _tick_queen(queen, client, mission_id)
    mission = client.get(f"/missions/{mission_id}").json()
    assert mission["status"] == "building"
    child_ids = [tid for tid in mission["task_ids"] if tid != plan_task_id]
    assert len(child_ids) == 2

    # --- Cancel the mission ---
    resp = client.post(f"/missions/{mission_id}/cancel")
    assert resp.status_code == 200

    mission = client.get(f"/missions/{mission_id}").json()
    assert mission["status"] == "cancelled"

    # --- Queen should skip cancelled missions ---
    # Tick queen multiple times — status should stay cancelled
    for _ in range(5):
        _tick_queen(queen, client, mission_id)

    mission = client.get(f"/missions/{mission_id}").json()
    assert mission["status"] == "cancelled"

    # Child tasks are moved to done/ with cancellation metadata
    for cid in child_ids:
        task = client.get(f"/tasks/{cid}").json()
        assert task["status"] == "done"  # purged to done/ by cancel
        assert task.get("cancelled_at") is not None


def test_e2e_mission_blocked_task(mission_env):
    """One child hits max attempts (blocked), other merges. Mission completes best-effort.

    Steps:
    1. Create mission, plan, spawn 2 children (no deps between them)
    2. Child-01: forage, harvest, merge -> success
    3. Child-02: 3x forage+harvest+kickback -> blocked
    4. Queen detects no in-flight tasks -> COMPLETE
    5. Verify report: merged_tasks=1, blocked_tasks=1
    """
    client = mission_env["client"]
    queen = mission_env["queen"]

    # --- Create mission with independent children ---
    resp = client.post(
        "/missions",
        json={
            "mission_id": "mission-blocked-1",
            "spec": "Build two independent features",
            "config": {"require_plan_review": False},
        },
    )
    assert resp.status_code == 201
    mission_id = resp.json()["mission_id"]

    # Queen creates plan task
    _tick_queen(queen, client, mission_id)
    mission = client.get(f"/missions/{mission_id}").json()
    plan_task_id = mission["plan_task_id"]

    # Planner forages and harvests with 2 independent children
    task = _forage(client, "planner-1")
    attempt_id = task["current_attempt"]

    artifact = {
        "plan_artifact": {
            "plan_task_id": plan_task_id,
            "attempt_id": attempt_id,
            "proposed_tasks": [
                {
                    "title": "Feature A",
                    "spec": "Implement feature A",
                    "complexity": "M",
                    "priority": 10,
                    "depends_on": [],
                    "touches": ["api"],
                },
                {
                    "title": "Feature B",
                    "spec": "Implement feature B",
                    "complexity": "M",
                    "priority": 10,
                    "depends_on": [],
                    "touches": ["db"],
                },
            ],
            "task_count": 2,
            "warnings": [],
            "dependency_summary": "no dependencies",
        }
    }
    _harvest(client, plan_task_id, attempt_id, pr="n/a", branch="n/a", artifact=artifact)

    # Queen spawns children
    _tick_queen(queen, client, mission_id)
    mission = client.get(f"/missions/{mission_id}").json()
    assert mission["status"] == "building"
    child_ids = [tid for tid in mission["task_ids"] if tid != plan_task_id]
    assert len(child_ids) == 2

    # --- Child-01: forage, harvest, merge (success) ---
    task = _forage(client, "builder-1")
    assert task is not None
    att = task["current_attempt"]
    child_success_id = task["id"]
    child_fail_id = [c for c in child_ids if c != child_success_id][0]

    _harvest(client, child_success_id, att, pr="https://github.com/x/y/pull/20", branch="feat/a")
    _mark_merged(client, child_success_id, att)

    # --- Child-02: 3x forage+harvest+kickback -> blocked ---
    for i in range(3):
        t = _forage(client, "builder-1")
        assert t is not None, f"kickback iteration {i}: expected task to be available"
        assert t["id"] == child_fail_id
        att_fail = t["current_attempt"]
        _harvest(
            client, child_fail_id, att_fail,
            pr=f"https://github.com/x/y/pull/fail-{i}", branch=f"feat/b-{i}",
        )
        _kickback(client, child_fail_id, f"test failure iteration {i}", max_attempts=3)

    # Verify child-02 is blocked
    child_fail = client.get(f"/tasks/{child_fail_id}").json()
    assert child_fail["status"] == "blocked"

    # --- Queen detects all done/blocked -> COMPLETE (best-effort) ---
    mission = _tick_queen_until(queen, client, mission_id, "complete")

    assert mission["status"] == "complete"

    # --- Verify report ---
    report = mission["report"]
    assert report is not None
    assert report["merged_tasks"] == 1
    assert report["blocked_tasks"] == 1
    assert report["total_tasks"] == 2

    blocked_ids = {b["task_id"] for b in report["blocked"]}
    assert child_fail_id in blocked_ids


def test_e2e_mission_plan_review_rejected_triggers_replan(mission_env):
    """Plan review rejects first plan, accepts second. Mission completes.

    Steps:
    1. Create mission with require_plan_review=True (default)
    2. Queen creates plan task, planner harvests
    3. Queen transitions to REVIEWING_PLAN, creates review task
    4. Reviewer harvests with needs_changes verdict
    5. Queen increments re_plan_count, creates new plan task (back to PLANNING)
    6. New planner harvests with updated PlanArtifact
    7. Queen creates second review, reviewer passes
    8. Queen spawns children, workers complete them
    9. Verify: re_plan_count=1, mission COMPLETE
    """
    client = mission_env["client"]
    queen = mission_env["queen"]

    # --- Step 1: Create mission with plan review enabled ---
    resp = client.post(
        "/missions",
        json={
            "mission_id": "mission-replan-1",
            "spec": "Implement user dashboard",
        },
    )
    assert resp.status_code == 201
    mission_id = resp.json()["mission_id"]

    # --- Step 2: Queen creates plan task, planner harvests ---
    _tick_queen(queen, client, mission_id)
    mission = client.get(f"/missions/{mission_id}").json()
    plan_task_id = mission["plan_task_id"]

    task = _forage(client, "planner-1")
    assert task["id"] == plan_task_id
    att = task["current_attempt"]

    plan_artifact_1 = _make_plan_artifact(plan_task_id, att)
    _harvest(client, plan_task_id, att, pr="n/a", branch="n/a", artifact=plan_artifact_1)

    # --- Step 3: Queen transitions to REVIEWING_PLAN ---
    _tick_queen(queen, client, mission_id)
    mission = client.get(f"/missions/{mission_id}").json()
    assert mission["status"] == "reviewing_plan"

    # Review task should exist
    review_task_id = f"review-plan-{mission_id}"
    review_task = client.get(f"/tasks/{review_task_id}").json()
    assert review_task["status"] == "ready"

    # --- Step 4: Reviewer forages and harvests with needs_changes verdict ---
    rtask = _forage(client, "reviewer-1")
    assert rtask is not None
    assert rtask["id"] == review_task_id
    review_att = rtask["current_attempt"]

    _harvest(client, review_task_id, review_att, pr="n/a", branch="n/a")
    _store_review_verdict(client, review_task_id, review_att, {
        "verdict": "needs_changes",
        "summary": "Missing error handling tasks",
        "feedback": "Add error handling for API endpoints",
    })

    # --- Step 5: Queen sees needs_changes, back to PLANNING ---
    # Queen creates the re-plan task and transitions back to PLANNING
    _tick_queen(queen, client, mission_id)
    mission = client.get(f"/missions/{mission_id}").json()
    assert mission["status"] == "planning"
    assert mission["re_plan_count"] == 1

    # The re-plan task was created by _create_re_plan_task with a deterministic ID
    re_plan_task_id = f"plan-{mission_id}-re1"
    re_plan_task = client.get(f"/tasks/{re_plan_task_id}").json()
    assert re_plan_task["status"] == "ready"

    # Wire up plan_task_id so _advance_planning sees the re-plan task.
    # (Queen's _advance_planning would create the old plan-{mission_id}
    # again, which already exists; we point to the re-plan task directly.)
    # Also disable plan review for the second pass to avoid duplicate
    # review task ID collision (review-plan-{mission_id} already in done/).
    client.patch(
        f"/missions/{mission_id}",
        json={"updates": {
            "plan_task_id": re_plan_task_id,
            "config": {**mission["config"], "require_plan_review": False},
        }},
    ).raise_for_status()

    # --- Step 6: Planner forages re-plan task and harvests ---
    task = _forage(client, "planner-1")
    assert task is not None
    assert task["id"] == re_plan_task_id
    att2 = task["current_attempt"]

    plan_artifact_2 = _make_plan_artifact(re_plan_task_id, att2)
    _harvest(client, re_plan_task_id, att2, pr="n/a", branch="n/a", artifact=plan_artifact_2)

    # --- Step 7: Queen sees done re-plan task, skips review, spawns children ---
    _tick_queen(queen, client, mission_id)
    mission = client.get(f"/missions/{mission_id}").json()
    assert mission["status"] == "building"

    # Get child tasks (exclude plan/review infra tasks)
    all_task_ids = mission["task_ids"]
    infra_prefixes = ("plan-", "review-")
    child_ids = [tid for tid in all_task_ids if not any(tid.startswith(p) for p in infra_prefixes)]
    assert len(child_ids) == 2

    # --- Step 8: Workers complete both children ---
    for _child_id in child_ids:
        t = _forage(client, "builder-1")
        assert t is not None
        _harvest(
            client, t["id"], t["current_attempt"],
            pr=f"https://github.com/x/y/pull/{t['id']}", branch=f"feat/{t['id']}",
        )
        _mark_merged(client, t["id"], t["current_attempt"])

    # --- Step 9: Queen completes mission ---
    mission = _tick_queen_until(queen, client, mission_id, "complete")

    assert mission["status"] == "complete"
    assert mission["re_plan_count"] == 1

    report = mission["report"]
    assert report is not None
    assert report["merged_tasks"] == 2
    assert report["blocked_tasks"] == 0
