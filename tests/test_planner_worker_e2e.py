"""End-to-end test for the planner worker flow.

Proves: carry plan task -> planner worker decomposes -> child tasks appear
in queue with deterministic IDs, resolved deps, spawned_by lineage,
and plan task is harvested with artifact listing created IDs.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from antfarm.core.backends.file import FileBackend
from antfarm.core.serve import get_app
from antfarm.core.worker import AgentResult, WorkerRuntime

# ---------------------------------------------------------------------------
# Sync httpx transport (same pattern as test_worker.py)
# ---------------------------------------------------------------------------


class _StarletteTransport(httpx.BaseTransport):
    def __init__(self, app):
        self._tc = TestClient(app, raise_server_exceptions=True)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        resp = self._tc.request(
            request.method,
            str(request.url.path),
            content=request.content,
            headers=dict(request.headers),
            params=dict(request.url.params),
        )
        return httpx.Response(
            resp.status_code,
            headers=dict(resp.headers),
            content=resp.content,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(tmp_path):
    return FileBackend(root=str(tmp_path / ".antfarm"))


@pytest.fixture
def app(backend):
    return get_app(backend=backend)


@pytest.fixture
def http_client(app):
    transport = _StarletteTransport(app)
    client = httpx.Client(transport=transport, base_url="http://test")
    yield client
    client.close()


@pytest.fixture
def tc(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# E2E test
# ---------------------------------------------------------------------------


def test_e2e_plan_to_build_flow(tc, tmp_path, http_client):
    """Full planner flow: carry plan -> planner decomposes -> children in queue."""
    # 1. Carry a plan task
    r = tc.post("/tasks", json={
        "id": "plan-auth",
        "title": "Plan auth system",
        "spec": "Build a complete authentication system with login, signup, and tokens",
        "capabilities_required": ["plan"],
    })
    assert r.status_code == 201

    # 2. Create planner worker runtime
    rt = WorkerRuntime(
        colony_url="http://test",
        node_id="node-1",
        name="planner-1",
        agent_type="generic",
        workspace_root=str(tmp_path / "workspaces"),
        repo_path=str(tmp_path),
        integration_branch="main",
        heartbeat_interval=999.0,
        capabilities=["plan"],
        client=http_client,
    )
    rt.workspace_mgr.create = MagicMock(return_value=str(tmp_path / "ws"))

    # 3. Simulate agent output with 3 tasks, task 3 depends on task 1
    plan_tasks = [
        {
            "title": "Add login endpoint",
            "spec": "Implement POST /login with JWT",
            "touches": ["api", "auth"],
            "depends_on": [],
            "priority": 5,
            "complexity": "M",
        },
        {
            "title": "Add signup endpoint",
            "spec": "Implement POST /signup with validation",
            "touches": ["api", "auth"],
            "depends_on": [],
            "priority": 5,
            "complexity": "M",
        },
        {
            "title": "Add token refresh",
            "spec": "Implement POST /refresh using existing login",
            "touches": ["api"],
            "depends_on": [1],  # depends on login endpoint
            "priority": 10,
            "complexity": "S",
        },
    ]
    plan_output = f"[PLAN_RESULT]\n{json.dumps(plan_tasks)}\n[/PLAN_RESULT]"

    def planner_agent(task, workspace) -> AgentResult:
        return AgentResult(returncode=0, stdout=plan_output, stderr="", branch="")

    rt._launch_agent = planner_agent
    rt.run()

    # 4. Verify child tasks were created with deterministic IDs
    r = tc.get("/tasks")
    all_tasks = {t["id"]: t for t in r.json()}

    assert "task-auth-01" in all_tasks
    assert "task-auth-02" in all_tasks
    assert "task-auth-03" in all_tasks

    # 5. Verify dependency resolution: task-auth-03 depends on task-auth-01
    assert "task-auth-01" in all_tasks["task-auth-03"]["depends_on"]

    # 6. Verify spawned_by lineage
    child_01 = all_tasks["task-auth-01"]
    assert child_01.get("spawned_by", {}).get("task_id") == "plan-auth"
    assert child_01["spawned_by"]["attempt_id"] is not None

    # 7. Verify no recursive plans (capabilities_required empty)
    assert child_01["capabilities_required"] == []
    assert all_tasks["task-auth-02"]["capabilities_required"] == []
    assert all_tasks["task-auth-03"]["capabilities_required"] == []

    # 8. Verify plan task was harvested
    plan_task = all_tasks["plan-auth"]
    assert plan_task["status"] == "done"

    # 9. Verify child tasks are ready for builders
    assert all_tasks["task-auth-01"]["status"] == "ready"
    assert all_tasks["task-auth-02"]["status"] == "ready"
    assert all_tasks["task-auth-03"]["status"] == "ready"
