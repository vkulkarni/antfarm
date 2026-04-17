"""Tests for antfarm.core.queen.Queen controller.

Uses a real FileBackend over tmp_path with a fake clock. No HTTP — Queen talks
directly to the backend, matching the in-process daemon thread pattern.
"""

from __future__ import annotations

import json
import time

import pytest

from antfarm.core.backends.file import FileBackend
from antfarm.core.missions import MissionConfig, MissionStatus, PlanArtifact
from antfarm.core.queen import Queen, QueenConfig, _now_iso

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_mission(
    backend: FileBackend,
    mission_id: str = "mission-test-001",
    spec: str = "Build a widget",
    config_overrides: dict | None = None,
) -> dict:
    """Create a mission in PLANNING state and return its dict."""
    cfg = MissionConfig()
    if config_overrides:
        for k, v in config_overrides.items():
            setattr(cfg, k, v)
    now = _now_iso()
    mission = {
        "mission_id": mission_id,
        "spec": spec,
        "spec_file": None,
        "status": MissionStatus.PLANNING.value,
        "plan_task_id": None,
        "plan_artifact": None,
        "task_ids": [],
        "blocked_task_ids": [],
        "config": cfg.to_dict(),
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "report": None,
        "last_progress_at": now,
        "re_plan_count": 0,
    }
    backend.create_mission(mission)
    return mission


def _create_task(
    backend: FileBackend,
    task_id: str,
    status: str = "ready",
    mission_id: str | None = None,
    capabilities_required: list[str] | None = None,
    attempts: list[dict] | None = None,
    current_attempt: str | None = None,
    depends_on: list[str] | None = None,
    **kwargs,
) -> dict:
    """Create a task directly in the backend."""
    now = _now_iso()
    task = {
        "id": task_id,
        "title": kwargs.get("title", f"Task {task_id}"),
        "spec": kwargs.get("spec", "test spec"),
        "complexity": "M",
        "priority": kwargs.get("priority", 10),
        "depends_on": depends_on or [],
        "touches": kwargs.get("touches", []),
        "capabilities_required": capabilities_required or [],
        "created_by": "test",
        "status": "ready",
        "current_attempt": None,
        "attempts": [],
        "trail": [],
        "signals": [],
        "created_at": now,
        "updated_at": now,
    }
    if mission_id:
        task["mission_id"] = mission_id
    backend.carry(task)

    # If we need a non-ready status, manually update the file
    if status != "ready" or attempts or current_attempt:
        t = backend.get_task(task_id)
        if attempts:
            t["attempts"] = attempts
        if current_attempt:
            t["current_attempt"] = current_attempt
        if status != "ready":
            t["status"] = status
        _force_task_state(backend, task_id, t)

    return backend.get_task(task_id)


def _force_task_state(backend: FileBackend, task_id: str, data: dict) -> None:
    """Force-write a task dict to the correct status directory."""
    root = backend._root
    status = data["status"]

    # Find the task file wherever it is now
    for subdir in ("ready", "active", "done", "blocked"):
        p = root / "tasks" / subdir / f"{task_id}.json"
        if p.exists():
            p.unlink()
            break

    target_dir = root / "tasks" / status
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{task_id}.json"
    with open(target, "w") as f:
        json.dump(data, f)


def _make_plan_artifact(
    plan_task_id: str = "plan-mission-test-001",
    attempt_id: str = "att-001",
    task_count: int = 2,
) -> PlanArtifact:
    """Create a PlanArtifact with N proposed tasks."""
    proposed = []
    for i in range(task_count):
        proposed.append(
            {
                "title": f"Child task {i + 1}",
                "spec": f"Implement part {i + 1}",
                "touches": ["api"],
                "depends_on": [],
                "priority": 10,
                "complexity": "M",
            }
        )
    return PlanArtifact(
        plan_task_id=plan_task_id,
        attempt_id=attempt_id,
        proposed_tasks=proposed,
        task_count=task_count,
        warnings=[],
        dependency_summary="no deps",
    )


def _harvest_plan_task_with_artifact(
    backend: FileBackend,
    plan_task_id: str,
    artifact: PlanArtifact,
) -> None:
    """Simulate a plan task being harvested with a valid PlanArtifact."""
    task = backend.get_task(plan_task_id)
    task["status"] = "done"
    task["current_attempt"] = "att-001"
    task["attempts"] = [
        {
            "attempt_id": "att-001",
            "worker_id": "test-worker",
            "status": "done",
            "branch": None,
            "pr": None,
            "started_at": _now_iso(),
            "completed_at": _now_iso(),
            "artifact": {
                "plan_artifact": artifact.to_dict(),
            },
        }
    ]
    _force_task_state(backend, plan_task_id, task)


def _make_review_verdict(verdict: str = "pass", summary: str = "looks good") -> dict:
    return {
        "verdict": verdict,
        "summary": summary,
        "reviewed_commit_sha": "abc1234",
    }


def _set_review_verdict_on_task(
    backend: FileBackend,
    review_task_id: str,
    verdict: dict,
) -> None:
    """Set a review task as done with a verdict in its artifact."""
    task = backend.get_task(review_task_id)
    task["status"] = "done"
    task["current_attempt"] = "att-r01"
    task["attempts"] = [
        {
            "attempt_id": "att-r01",
            "worker_id": "reviewer-worker",
            "status": "done",
            "branch": None,
            "pr": None,
            "started_at": _now_iso(),
            "completed_at": _now_iso(),
            "artifact": verdict,
        }
    ]
    _force_task_state(backend, review_task_id, task)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    """Create a FileBackend and Queen with a controllable fake clock."""
    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    fake_time = [time.time()]
    queen = Queen(backend, config=QueenConfig(), clock=lambda: fake_time[0])
    return {
        "backend": backend,
        "queen": queen,
        "fake_time": fake_time,
        "tmp_path": tmp_path,
    }


# ---------------------------------------------------------------------------
# Test 1: Planning creates plan task
# ---------------------------------------------------------------------------


def test_queen_planning_creates_plan_task(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["plan_task_id"] == f"plan-{mission['mission_id']}"
    assert m["plan_task_id"] in m["task_ids"]

    plan_task = backend.get_task(m["plan_task_id"])
    assert plan_task is not None
    assert "plan" in plan_task["capabilities_required"]


# ---------------------------------------------------------------------------
# Test 2: Planning waits for harvest
# ---------------------------------------------------------------------------


def test_queen_planning_waits_for_harvest(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])

    # First advance: creates plan task
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "planning"

    # Second advance: plan task is still ready, should be no-op
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "planning"


# ---------------------------------------------------------------------------
# Test 3: Harvested plan, no review → spawns children
# ---------------------------------------------------------------------------


def test_queen_planning_harvested_no_review_spawns_children(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend, config_overrides={"require_plan_review": False})
    m = backend.get_mission(mission["mission_id"])

    # Create and harvest plan task
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)

    # Advance again — should spawn children and go to BUILDING
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "building"
    assert m["plan_artifact"] is not None
    # Should have plan task + 2 child tasks
    assert len(m["task_ids"]) == 3


# ---------------------------------------------------------------------------
# Test 4: Harvested plan, with review → creates review task
# ---------------------------------------------------------------------------


def test_queen_planning_harvested_with_review_creates_review_task(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)  # require_plan_review=True by default
    m = backend.get_mission(mission["mission_id"])

    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "reviewing_plan"
    assert m["plan_artifact"] is not None

    review_task = backend.get_task(f"review-plan-{mission['mission_id']}")
    assert review_task is not None
    assert "review" in review_task["capabilities_required"]


# ---------------------------------------------------------------------------
# Test 5: Plan task blocked → mission FAILED with "system: " prefix
# ---------------------------------------------------------------------------


def test_queen_planning_plan_task_blocked_fails_mission(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])

    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])

    # Simulate plan task being blocked (max attempts exhausted)
    plan_task = backend.get_task(m["plan_task_id"])
    plan_task["status"] = "blocked"
    plan_task["attempts"] = [{"attempt_id": f"att-{i}", "status": "superseded"} for i in range(3)]
    _force_task_state(backend, m["plan_task_id"], plan_task)

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "failed"
    assert m.get("failure_reason", "").startswith("system: ")
    assert m["re_plan_count"] == 0  # re_plan_count unchanged


# ---------------------------------------------------------------------------
# Test 6: Invalid artifact defers to kickback
# ---------------------------------------------------------------------------


def test_queen_planning_invalid_artifact_defers_to_kickback(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])

    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])

    # Simulate plan task done but with no valid artifact
    plan_task = backend.get_task(m["plan_task_id"])
    plan_task["status"] = "done"
    plan_task["current_attempt"] = "att-001"
    plan_task["attempts"] = [
        {
            "attempt_id": "att-001",
            "worker_id": "test-worker",
            "status": "done",
            "started_at": _now_iso(),
            "completed_at": _now_iso(),
            "artifact": {},  # no plan_artifact key
        }
    ]
    _force_task_state(backend, m["plan_task_id"], plan_task)

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "planning"  # stays in PLANNING
    assert m["re_plan_count"] == 0  # unchanged

    # Check trail entry was appended
    plan_task = backend.get_task(m["plan_task_id"])
    assert any("awaiting kickback" in e.get("message", "") for e in plan_task.get("trail", []))


# ---------------------------------------------------------------------------
# Test 7: Review task ready is no-op
# ---------------------------------------------------------------------------


def test_queen_review_task_ready_is_noop(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])

    # Get to REVIEWING_PLAN state
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "reviewing_plan"

    # Review task should be in ready state — advance should be no-op
    review_task = backend.get_task(f"review-plan-{mission['mission_id']}")
    assert review_task["status"] == "ready"

    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "reviewing_plan"  # unchanged


# ---------------------------------------------------------------------------
# Test 8: Review task blocked → FAILED with "system: " prefix
# ---------------------------------------------------------------------------


def test_queen_review_task_blocked_fails_with_system_prefix(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])

    # Get to REVIEWING_PLAN state
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    # Block the review task
    review_task_id = f"review-plan-{mission['mission_id']}"
    review_task = backend.get_task(review_task_id)
    review_task["status"] = "blocked"
    review_task["attempts"] = [
        {"attempt_id": f"att-r{i}", "status": "superseded"} for i in range(3)
    ]
    _force_task_state(backend, review_task_id, review_task)

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "failed"
    assert m.get("failure_reason", "").startswith("system: ")


# ---------------------------------------------------------------------------
# Test 9: Review pass → spawns children
# ---------------------------------------------------------------------------


def test_queen_review_pass_spawns_children(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])

    # Get to REVIEWING_PLAN state
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    # Set review verdict to pass
    review_task_id = f"review-plan-{mission['mission_id']}"
    _set_review_verdict_on_task(backend, review_task_id, _make_review_verdict("pass"))

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "building"
    # plan task + review task + 2 child tasks
    assert len(m["task_ids"]) >= 3


# ---------------------------------------------------------------------------
# Test 10: Review needs_changes → triggers re-plan
# ---------------------------------------------------------------------------


def test_queen_review_needs_changes_triggers_re_plan(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])

    # Get to REVIEWING_PLAN state
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    # Set review verdict to needs_changes
    review_task_id = f"review-plan-{mission['mission_id']}"
    _set_review_verdict_on_task(
        backend, review_task_id, _make_review_verdict("needs_changes", "missing auth")
    )

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "planning"
    assert m["re_plan_count"] == 1
    assert m["plan_task_id"] is None
    assert m["plan_artifact"] is None


# ---------------------------------------------------------------------------
# Test 11: Second needs_changes → FAILED with "review: " prefix
# ---------------------------------------------------------------------------


def test_queen_review_verdict_needs_changes_twice_fails_with_review_prefix(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])

    # First plan cycle
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # → REVIEWING_PLAN

    review_task_id = f"review-plan-{mission['mission_id']}"
    _set_review_verdict_on_task(
        backend, review_task_id, _make_review_verdict("needs_changes", "bad plan")
    )
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # → PLANNING with re_plan_count=1

    # Second plan cycle
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # creates re-plan task
    m = backend.get_mission(mission["mission_id"])
    re_plan_task_id = m["plan_task_id"]
    assert re_plan_task_id is not None
    artifact2 = _make_plan_artifact(plan_task_id=re_plan_task_id, attempt_id="att-re1")
    _harvest_plan_task_with_artifact(backend, re_plan_task_id, artifact2)

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # → REVIEWING_PLAN again

    # Second rejection
    review_task_id_2 = f"review-plan-{mission['mission_id']}"
    # The review task already exists from first cycle, recreate it
    # Actually Queen creates the same review-plan-{id} task, which already exists.
    # The review task from cycle 1 is still there. Let's set its verdict.
    _set_review_verdict_on_task(
        backend, review_task_id_2, _make_review_verdict("needs_changes", "still bad")
    )

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "failed"
    assert m.get("failure_reason", "").startswith("review: ")


# ---------------------------------------------------------------------------
# Test 12: Review verdict blocked → FAILED with "review: " prefix
# ---------------------------------------------------------------------------


def test_queen_review_verdict_blocked_fails_with_review_prefix(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])

    # Get to REVIEWING_PLAN state
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    # Set review verdict to blocked
    review_task_id = f"review-plan-{mission['mission_id']}"
    _set_review_verdict_on_task(
        backend, review_task_id, _make_review_verdict("blocked", "infeasible spec")
    )

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "failed"
    assert m.get("failure_reason", "").startswith("review: ")


# ---------------------------------------------------------------------------
# Test 13: Building — all merged → COMPLETE
# ---------------------------------------------------------------------------


def test_queen_building_all_merged_completes(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend, config_overrides={"require_plan_review": False})
    m = backend.get_mission(mission["mission_id"])

    # Get to BUILDING state
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "building"

    # Simulate all child tasks merged
    slug = queen._mission_slug(mission["mission_id"])
    for i in range(2):
        child_id = f"task-{slug}-{i + 1:02d}"
        child = backend.get_task(child_id)
        child["status"] = "done"
        child["current_attempt"] = f"att-c{i}"
        child["attempts"] = [
            {
                "attempt_id": f"att-c{i}",
                "worker_id": "test-worker",
                "status": "merged",
                "branch": f"feat/{child_id}",
                "pr": f"https://github.com/test/pr/{i}",
                "started_at": _now_iso(),
                "completed_at": _now_iso(),
                "artifact": {
                    "pr_url": f"https://github.com/test/pr/{i}",
                    "lines_added": 50,
                    "lines_removed": 10,
                    "files_changed": ["api.py"],
                    "branch": f"feat/{child_id}",
                },
            }
        ]
        _force_task_state(backend, child_id, child)

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "complete"
    assert m["report"] is not None
    assert m["completed_at"] is not None


# ---------------------------------------------------------------------------
# Test 14: Building — mixed merged+blocked → COMPLETE (best effort)
# ---------------------------------------------------------------------------


def test_queen_building_mixed_merged_blocked_completes(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend, config_overrides={"require_plan_review": False})
    m = backend.get_mission(mission["mission_id"])

    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    slug = queen._mission_slug(mission["mission_id"])

    # Child 1: merged
    child1_id = f"task-{slug}-01"
    child1 = backend.get_task(child1_id)
    child1["status"] = "done"
    child1["current_attempt"] = "att-c0"
    child1["attempts"] = [
        {
            "attempt_id": "att-c0",
            "status": "merged",
            "branch": "b",
            "pr": "p",
            "started_at": _now_iso(),
            "completed_at": _now_iso(),
            "worker_id": "w",
        }
    ]
    _force_task_state(backend, child1_id, child1)

    # Child 2: blocked
    child2_id = f"task-{slug}-02"
    child2 = backend.get_task(child2_id)
    child2["status"] = "blocked"
    child2["blocked_reason"] = "max attempts exhausted"
    child2["attempts"] = [
        {
            "attempt_id": "att-c1",
            "status": "superseded",
            "started_at": _now_iso(),
            "completed_at": _now_iso(),
            "worker_id": "w",
        }
    ]
    _force_task_state(backend, child2_id, child2)

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "complete"
    assert m["report"] is not None


# ---------------------------------------------------------------------------
# Test 15: Building — some in-flight → stays BUILDING
# ---------------------------------------------------------------------------


def test_queen_building_some_in_flight_stays_building(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend, config_overrides={"require_plan_review": False})
    m = backend.get_mission(mission["mission_id"])

    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "building"

    # Children are still in "ready" state — in-flight
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "building"  # stays building


# ---------------------------------------------------------------------------
# Test 16: Stall detection → BLOCKED
# ---------------------------------------------------------------------------


def test_queen_stall_detection(env):
    backend = env["backend"]
    queen = env["queen"]
    fake_time = env["fake_time"]

    mission = _create_mission(
        backend,
        config_overrides={
            "require_plan_review": False,
            "stall_threshold_minutes": 30,
        },
    )
    m = backend.get_mission(mission["mission_id"])

    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "building"

    # Advance time past stall threshold
    fake_time[0] += 31 * 60  # 31 minutes

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "blocked"


# ---------------------------------------------------------------------------
# Test 17: Blocked timeout → FAILED
# ---------------------------------------------------------------------------


def test_queen_blocked_timeout_fail(env):
    backend = env["backend"]
    queen = env["queen"]
    fake_time = env["fake_time"]

    mission = _create_mission(
        backend,
        config_overrides={
            "require_plan_review": False,
            "stall_threshold_minutes": 5,
            "blocked_timeout_action": "fail",
            "blocked_timeout_minutes": 10,
        },
    )
    m = backend.get_mission(mission["mission_id"])

    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    # Trigger stall while children are in-flight (ready) → BLOCKED
    fake_time[0] += 6 * 60
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "blocked"

    # Now block all child tasks so _advance_blocked doesn't bounce back to BUILDING
    slug = queen._mission_slug(mission["mission_id"])
    for i in range(2):
        child_id = f"task-{slug}-{i + 1:02d}"
        child = backend.get_task(child_id)
        child["status"] = "blocked"
        _force_task_state(backend, child_id, child)

    # Trigger blocked timeout → FAILED
    fake_time[0] += 11 * 60
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "failed"


# ---------------------------------------------------------------------------
# Test 18: Blocked → unblocked resumes building
# ---------------------------------------------------------------------------


def test_queen_blocked_unblocked_resumes_building(env):
    backend = env["backend"]
    queen = env["queen"]
    fake_time = env["fake_time"]

    mission = _create_mission(
        backend,
        config_overrides={
            "require_plan_review": False,
            "stall_threshold_minutes": 5,
        },
    )
    m = backend.get_mission(mission["mission_id"])

    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    # Trigger stall → BLOCKED
    fake_time[0] += 6 * 60
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "blocked"

    # Simulate a child task getting unblocked (goes back to ready)
    # Child tasks are already in "ready" state, which is in-flight
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "building"


# ---------------------------------------------------------------------------
# Test 19: Terminal states are skipped
# ---------------------------------------------------------------------------


def test_queen_terminal_states_are_skipped(env):
    backend = env["backend"]
    queen = env["queen"]

    for terminal_status in ("complete", "failed", "cancelled"):
        mid = f"mission-terminal-{terminal_status}"
        _create_mission(backend, mission_id=mid)
        backend.update_mission(mid, {"status": terminal_status})

        m = backend.get_mission(mid)
        # _advance should not be called for terminal states (run() skips them)
        # but even if called directly, should be a no-op
        queen._advance(m)
        m = backend.get_mission(mid)
        assert m["status"] == terminal_status


# ---------------------------------------------------------------------------
# Test 20: Advance is idempotent
# ---------------------------------------------------------------------------


def test_queen_advance_is_idempotent(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])

    # First advance creates plan task
    queen._advance(m)
    m1 = backend.get_mission(mission["mission_id"])

    # Second advance should be no-op
    queen._advance(m1)
    m2 = backend.get_mission(mission["mission_id"])

    assert m1["status"] == m2["status"]
    assert m1["plan_task_id"] == m2["plan_task_id"]
    assert m1["task_ids"] == m2["task_ids"]


# ---------------------------------------------------------------------------
# Test 21: Crash recovery
# ---------------------------------------------------------------------------


def test_queen_crash_recovery(env):
    backend = env["backend"]

    mission = _create_mission(backend)

    # First Queen instance: advance once (creates plan task)
    queen1 = Queen(backend, config=QueenConfig())
    m = backend.get_mission(mission["mission_id"])
    queen1._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["plan_task_id"] is not None

    # "Crash" queen1 — discard the instance
    del queen1

    # New Queen instance: advance again — state should be consistent
    queen2 = Queen(backend, config=QueenConfig())
    m = backend.get_mission(mission["mission_id"])
    queen2._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "planning"
    assert m["plan_task_id"] is not None


# ---------------------------------------------------------------------------
# Test 22: all_or_nothing treated as best_effort
# ---------------------------------------------------------------------------


def test_queen_all_or_nothing_treated_as_best_effort(env):
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(
        backend,
        config_overrides={
            "require_plan_review": False,
            "completion_mode": "all_or_nothing",
        },
    )
    m = backend.get_mission(mission["mission_id"])

    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    slug = queen._mission_slug(mission["mission_id"])

    # Child 1: merged
    child1_id = f"task-{slug}-01"
    child1 = backend.get_task(child1_id)
    child1["status"] = "done"
    child1["current_attempt"] = "att-c0"
    child1["attempts"] = [
        {
            "attempt_id": "att-c0",
            "status": "merged",
            "branch": "b",
            "pr": "p",
            "started_at": _now_iso(),
            "completed_at": _now_iso(),
            "worker_id": "w",
        }
    ]
    _force_task_state(backend, child1_id, child1)

    # Child 2: blocked
    child2_id = f"task-{slug}-02"
    child2 = backend.get_task(child2_id)
    child2["status"] = "blocked"
    child2["attempts"] = []
    _force_task_state(backend, child2_id, child2)

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    # all_or_nothing treated as best_effort in v0.6.0
    assert m["status"] == "complete"


# ---------------------------------------------------------------------------
# Mission context blob generation (issue #219)
# ---------------------------------------------------------------------------


def _make_queen_with_data_dir(backend, tmp_path, subdir: str = ".antfarm"):
    """Build a Queen wired to a specific data_dir / repo_path for context tests."""
    data_dir = str(tmp_path / subdir)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    queen = Queen(
        backend,
        config=QueenConfig(),
        data_dir=data_dir,
        repo_path=str(repo_dir),
        integration_branch="main",
    )
    return queen, data_dir


def test_queen_writes_context_on_transition_to_building(env, tmp_path):
    """When Queen flips a mission to BUILDING (no plan review), context is written."""
    backend = env["backend"]
    queen, data_dir = _make_queen_with_data_dir(backend, tmp_path)

    mission = _create_mission(
        backend,
        config_overrides={"require_plan_review": False},
    )
    m = backend.get_mission(mission["mission_id"])

    queen._advance(m)  # create plan task
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # transition to BUILDING, should write context

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "building"

    import os as _os

    context_path = _os.path.join(data_dir, "missions", f"{mission['mission_id']}_context.md")
    assert _os.path.isfile(context_path)
    with open(context_path) as f:
        body = f.read()
    assert "Mission Context" in body


def test_queen_writes_context_after_plan_review_pass(env, tmp_path):
    """Review verdict=pass path writes the mission context blob."""
    backend = env["backend"]
    queen, data_dir = _make_queen_with_data_dir(backend, tmp_path)

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])

    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # to REVIEWING_PLAN

    review_task_id = f"review-plan-{mission['mission_id']}"
    _set_review_verdict_on_task(backend, review_task_id, _make_review_verdict("pass"))

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # review=pass → BUILDING

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "building"

    import os as _os

    context_path = _os.path.join(data_dir, "missions", f"{mission['mission_id']}_context.md")
    assert _os.path.isfile(context_path)


def test_queen_writes_context_on_re_plan(env, tmp_path):
    """After needs_changes → re-plan → PASS, context is (re)written."""
    backend = env["backend"]
    queen, data_dir = _make_queen_with_data_dir(backend, tmp_path)

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])

    # Cycle 1: plan → review → needs_changes → re-plan
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact1 = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact1)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # → REVIEWING_PLAN

    review_task_id = f"review-plan-{mission['mission_id']}"
    _set_review_verdict_on_task(
        backend, review_task_id, _make_review_verdict("needs_changes", "fix it")
    )
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # → PLANNING (re-plan)
    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "planning"
    assert m["re_plan_count"] == 1

    # Cycle 2: new plan completes, no-review path to BUILDING — flip
    # require_plan_review so re-plan path has a simpler terminal transition.
    # We harvest the re-plan task with a fresh artifact.
    queen._advance(m)  # creates re-plan task
    m = backend.get_mission(mission["mission_id"])
    new_plan_task_id = m["plan_task_id"]
    artifact2 = _make_plan_artifact(plan_task_id=new_plan_task_id, task_count=3)
    _harvest_plan_task_with_artifact(backend, new_plan_task_id, artifact2)

    # For the re-plan, require_plan_review defaults stay True, so go through
    # reviewing again and mark pass.
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # → REVIEWING_PLAN
    review_task_id2 = f"review-plan-{mission['mission_id']}"
    # Reset review verdict with pass
    _set_review_verdict_on_task(backend, review_task_id2, _make_review_verdict("pass"))
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # → BUILDING, writes context

    m = backend.get_mission(mission["mission_id"])
    assert m["status"] == "building"

    import os as _os

    context_path = _os.path.join(data_dir, "missions", f"{mission['mission_id']}_context.md")
    assert _os.path.isfile(context_path)


def test_queen_populates_mission_context_path(env, tmp_path):
    """After writing context, mission.mission_context_path points to on-disk file."""
    backend = env["backend"]
    queen, data_dir = _make_queen_with_data_dir(backend, tmp_path)

    mission = _create_mission(
        backend,
        config_overrides={"require_plan_review": False},
    )
    m = backend.get_mission(mission["mission_id"])

    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    m = backend.get_mission(mission["mission_id"])
    assert m.get("mission_context_path") is not None

    import os as _os

    assert _os.path.isfile(m["mission_context_path"])
    # Must be under the configured data_dir, not the cwd
    assert m["mission_context_path"].startswith(data_dir)


def test_queen_context_written_in_correct_data_dir(env, tmp_path):
    """Context file lands under configured data_dir, not ./.antfarm (cwd-relative)."""
    backend = env["backend"]
    queen, data_dir = _make_queen_with_data_dir(backend, tmp_path, subdir=".custom")

    mission = _create_mission(
        backend,
        mission_id="mission-dirpath-001",
        config_overrides={"require_plan_review": False},
    )
    m = backend.get_mission(mission["mission_id"])

    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    import os as _os

    # The context file must exist under the custom data_dir
    expected = _os.path.join(data_dir, "missions", f"{mission['mission_id']}_context.md")
    assert _os.path.isfile(expected)


# ---------------------------------------------------------------------------
# Activity-feed lifecycle events (#191)
# ---------------------------------------------------------------------------


@pytest.fixture
def event_bus():
    """Reset the SSE event queue around each event-emission test."""
    from antfarm.core import serve as serve_mod

    serve_mod._event_queue.clear()
    serve_mod._event_counter = 0
    yield serve_mod._event_queue
    serve_mod._event_queue.clear()
    serve_mod._event_counter = 0


def _find_events(event_queue, *, type_: str, actor: str = "queen") -> list[dict]:
    return [e for e in event_queue if e["type"] == type_ and e["actor"] == actor]


def test_queen_emits_mission_created_on_first_advance(env, event_bus):
    """First _advance on a fresh mission should emit mission_created (actor=queen)."""
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend, spec="Build an activity feed panel")
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    events = _find_events(event_bus, type_="mission_created")
    assert len(events) == 1
    assert events[0]["task_id"] == ""
    assert mission["mission_id"] in events[0]["detail"]


def test_queen_emits_mission_created_only_once_per_mission(env, event_bus):
    """Second _advance before harvest must NOT re-emit mission_created."""
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # plan task still ready — no-op

    assert len(_find_events(event_bus, type_="mission_created")) == 1


def test_queen_emits_plan_task_created(env, event_bus):
    """Creating the plan task emits plan_task_created with the plan task id."""
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    events = _find_events(event_bus, type_="plan_task_created")
    assert len(events) == 1
    assert events[0]["task_id"] == f"plan-{mission['mission_id']}"
    assert mission["mission_id"] in events[0]["detail"]


def test_queen_emits_plan_approved_on_review_pass(env, event_bus):
    """When a plan review verdict is pass, plan_approved is emitted."""
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # → REVIEWING_PLAN

    review_task_id = f"review-plan-{mission['mission_id']}"
    _set_review_verdict_on_task(backend, review_task_id, _make_review_verdict("pass"))

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # review=pass → BUILDING

    events = _find_events(event_bus, type_="plan_approved")
    assert len(events) == 1
    assert events[0]["task_id"] == f"plan-{mission['mission_id']}"
    assert mission["mission_id"] in events[0]["detail"]


def test_queen_does_not_emit_plan_approved_on_needs_changes(env, event_bus):
    """needs_changes must not trigger plan_approved."""
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    review_task_id = f"review-plan-{mission['mission_id']}"
    _set_review_verdict_on_task(
        backend, review_task_id, _make_review_verdict("needs_changes", "redo")
    )

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    assert _find_events(event_bus, type_="plan_approved") == []


def test_queen_emits_tasks_seeded_with_count(env, event_bus):
    """_spawn_child_tasks emits tasks_seeded with count in detail."""
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend, config_overrides={"require_plan_review": False})
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"], task_count=3)
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # spawns children → BUILDING

    events = _find_events(event_bus, type_="tasks_seeded")
    assert len(events) == 1
    assert events[0]["task_id"] == ""
    assert "count=3" in events[0]["detail"]
    assert mission["mission_id"] in events[0]["detail"]


def test_queen_emits_mission_complete_on_complete_transition(env, event_bus):
    """Mission transitioning to COMPLETE emits mission_complete."""
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend, config_overrides={"require_plan_review": False})
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])
    artifact = _make_plan_artifact(plan_task_id=m["plan_task_id"])
    _harvest_plan_task_with_artifact(backend, m["plan_task_id"], artifact)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)  # → BUILDING

    # Mark both child tasks merged.
    slug = queen._mission_slug(mission["mission_id"])
    for i in range(2):
        child_id = f"task-{slug}-{i + 1:02d}"
        child = backend.get_task(child_id)
        child["status"] = "done"
        child["current_attempt"] = f"att-c{i}"
        child["attempts"] = [
            {
                "attempt_id": f"att-c{i}",
                "worker_id": "w",
                "status": "merged",
                "branch": f"feat/{child_id}",
                "pr": f"https://example.com/pr/{i}",
                "started_at": _now_iso(),
                "completed_at": _now_iso(),
                "artifact": {},
            }
        ]
        _force_task_state(backend, child_id, child)

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    assert backend.get_mission(mission["mission_id"])["status"] == "complete"

    events = _find_events(event_bus, type_="mission_complete")
    assert len(events) == 1
    assert events[0]["task_id"] == ""
    assert mission["mission_id"] in events[0]["detail"]


def test_queen_does_not_emit_mission_complete_on_failed_transition(env, event_bus):
    """Terminal=FAILED should not emit mission_complete."""
    backend = env["backend"]
    queen = env["queen"]

    mission = _create_mission(backend)
    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)
    m = backend.get_mission(mission["mission_id"])

    plan_task = backend.get_task(m["plan_task_id"])
    plan_task["status"] = "blocked"
    plan_task["attempts"] = [{"attempt_id": f"att-{i}", "status": "superseded"} for i in range(3)]
    _force_task_state(backend, m["plan_task_id"], plan_task)

    m = backend.get_mission(mission["mission_id"])
    queen._advance(m)

    assert backend.get_mission(mission["mission_id"])["status"] == "failed"
    assert _find_events(event_bus, type_="mission_complete") == []
