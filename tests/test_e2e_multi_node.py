"""End-to-end tests for multi-node autoscaler and prompt cache flows."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from unittest.mock import MagicMock

from antfarm.core.autoscaler import AutoscalerConfig, MultiNodeAutoscaler
from antfarm.core.backends.file import FileBackend
from antfarm.core.mission_context import (
    generate_mission_context,
    load_mission_context,
    store_mission_context,
)


def _make_task(task_id, status="ready", touches=None, depends_on=None, caps=None):
    now = datetime.now(UTC).isoformat()
    task = {
        "id": task_id,
        "title": f"Task {task_id}",
        "spec": "Do something",
        "complexity": "M",
        "priority": 10,
        "depends_on": depends_on or [],
        "touches": touches or [],
        "status": status,
        "current_attempt": None,
        "attempts": [],
        "trail": [],
        "signals": [],
        "created_at": now,
        "updated_at": now,
        "created_by": "test",
    }
    if caps:
        task["capabilities_required"] = caps
    return task


def test_e2e_multi_node_placement(tmp_path):
    """Test that MultiNodeAutoscaler distributes work across nodes.

    1. Create FileBackend
    2. Register 2 nodes with runner_urls
    3. Add 4 ready builder tasks
    4. Create MultiNodeAutoscaler with mock Actuator
    5. Run one reconcile cycle
    6. Verify actuator.apply called for both nodes with distributed counts
    """
    data_dir = str(tmp_path / ".antfarm")
    backend = FileBackend(root=data_dir)

    # Register 2 nodes with runner_urls
    backend.register_node(
        {
            "node_id": "node-1",
            "runner_url": "http://node1:7434",
            "max_workers": 4,
            "capabilities": [],
        }
    )
    backend.register_node(
        {
            "node_id": "node-2",
            "runner_url": "http://node2:7434",
            "max_workers": 4,
            "capabilities": [],
        }
    )

    # Add 4 ready builder tasks with different touches (non-overlapping)
    for i in range(1, 5):
        backend.carry(_make_task(f"task-{i}", touches=[f"scope-{i}"]))

    # Create mock actuator
    mock_actuator = MagicMock()
    mock_actuator.is_reachable.return_value = True
    mock_actuator.get_actual.return_value = {"workers": {}, "applied_generation": 0}

    config = AutoscalerConfig(
        enabled=True,
        max_builders=4,
        max_reviewers=2,
        data_dir=data_dir,
    )

    autoscaler = MultiNodeAutoscaler(
        backend=backend,
        config=config,
        actuator=mock_actuator,
    )

    # Run one reconcile cycle
    autoscaler._reconcile()

    # Verify actuator.apply was called for both nodes
    assert mock_actuator.apply.call_count == 2

    # Collect the desired states pushed to each node
    calls = mock_actuator.apply.call_args_list
    node_desired = {}
    for call in calls:
        runner_url = call.args[0] if call.args else call.kwargs.get("runner_url")
        desired = call.args[1] if len(call.args) > 1 else call.kwargs.get("desired")
        node_desired[runner_url] = desired

    # Post-#320 depth-aware scaling: target = ceil(ready_unblocked * 0.5)
    # = ceil(4 * 0.5) = 2 builders total across all nodes. Threshold (2) is
    # met so both builders are placed.
    total_builders = sum(d.get("builder", 0) for d in node_desired.values())
    assert total_builders == 2


def test_e2e_prompt_cache_roundtrip(tmp_path):
    """Test mission context generation, storage, and retrieval.

    1. Create a tmp git repo with CLAUDE.md
    2. Generate mission context
    3. Store it
    4. Load it (local)
    5. Verify it's deterministic (generate again, compare)
    6. Verify no timestamps in output
    """
    # 1. Create a tmp git repo with CLAUDE.md
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    (repo / "CLAUDE.md").write_text("# Project\n\nTest project conventions.\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )

    # Determine default branch name
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    branch = result.stdout.strip()

    mission = {
        "mission_id": "test-mission",
        "spec": "Build a REST API with CRUD endpoints for users.",
    }

    plan_artifact = {
        "proposed_tasks": [
            {"title": "Create models", "depends_on": []},
            {"title": "Create routes", "depends_on": [1]},
        ],
        "dependency_summary": "models -> routes",
    }

    # 2. Generate mission context
    context1 = generate_mission_context(
        repo_path=str(repo),
        integration_branch=branch,
        mission=mission,
        plan_artifact=plan_artifact,
    )

    assert "# Mission Context" in context1
    assert "CLAUDE.md" in context1
    assert "Test project conventions" in context1
    assert "Build a REST API" in context1
    assert "Create models" in context1
    assert "Create routes" in context1

    # 3. Store it
    data_dir = str(tmp_path / ".antfarm")
    path = store_mission_context(data_dir, "test-mission", context1)
    assert os.path.isfile(path)

    # 4. Load it (local)
    loaded = load_mission_context(data_dir, "test-mission")
    assert loaded == context1

    # 5. Verify deterministic (generate again, compare)
    context2 = generate_mission_context(
        repo_path=str(repo),
        integration_branch=branch,
        mission=mission,
        plan_artifact=plan_artifact,
    )
    assert context1 == context2, "Mission context should be deterministic"

    # 6. Verify no timestamps in output (context should be stable)
    # The function intentionally avoids timestamps for cache stability
    import re

    iso_pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
    assert not iso_pattern.search(context1), "Context should not contain ISO timestamps"
