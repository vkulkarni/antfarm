"""Tests for /missions Colony API endpoints (antfarm.core.serve).

Uses FastAPI TestClient with a fresh FileBackend per test via tmp_path fixture.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from antfarm.core.serve import get_app


@pytest.fixture
def client(tmp_path):
    from antfarm.core.backends.file import FileBackend

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_mission(client, mission_id="mission-001", spec="Build the thing"):
    return client.post(
        "/missions",
        json={"mission_id": mission_id, "spec": spec},
    )


def _carry(client, task_id="task-001", title="Test Task", spec="Do the thing", **kwargs):
    payload = {"id": task_id, "title": title, "spec": spec, **kwargs}
    return client.post("/tasks", json=payload)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_create_mission_endpoint_returns_id(client):
    """POST /missions returns 201 with mission_id."""
    r = _create_mission(client)
    assert r.status_code == 201
    assert r.json()["mission_id"] == "mission-001"


def test_create_mission_duplicate_returns_409(client):
    """POST /missions with duplicate mission_id returns 409."""
    _create_mission(client)
    r = _create_mission(client)
    assert r.status_code == 409


def test_create_mission_all_or_nothing_warns(client, caplog):
    """completion_mode='all_or_nothing' logs warning and persists the field."""
    with caplog.at_level(logging.WARNING):
        r = client.post(
            "/missions",
            json={
                "mission_id": "mission-aon",
                "spec": "all or nothing test",
                "config": {"completion_mode": "all_or_nothing"},
            },
        )
    assert r.status_code == 201
    assert "all_or_nothing" in caplog.text
    assert "best_effort" in caplog.text

    # Field persisted
    mission = client.get("/missions/mission-aon").json()
    assert mission["config"]["completion_mode"] == "all_or_nothing"


def test_list_missions_empty_returns_empty_list(client):
    """GET /missions returns empty list when no missions exist."""
    r = client.get("/missions")
    assert r.status_code == 200
    assert r.json() == []


def test_list_missions_filter_by_status(client):
    """GET /missions?status= filters by mission status."""
    _create_mission(client, mission_id="m-1")
    _create_mission(client, mission_id="m-2")
    # Cancel one
    client.post("/missions/m-2/cancel")

    r = client.get("/missions", params={"status": "planning"})
    assert r.status_code == 200
    missions = r.json()
    assert len(missions) == 1
    assert missions[0]["mission_id"] == "m-1"

    r = client.get("/missions", params={"status": "cancelled"})
    assert len(r.json()) == 1
    assert r.json()[0]["mission_id"] == "m-2"


def test_get_mission_404(client):
    """GET /missions/{id} returns 404 for non-existent mission."""
    r = client.get("/missions/nonexistent")
    assert r.status_code == 404


def test_patch_mission_merges_fields(client):
    """PATCH /missions/{id} applies shallow updates."""
    _create_mission(client)
    r = client.patch(
        "/missions/mission-001",
        json={"updates": {"status": "building"}},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    mission = client.get("/missions/mission-001").json()
    assert mission["status"] == "building"


def test_cancel_mission_terminal_state(client):
    """POST /missions/{id}/cancel flips status to cancelled."""
    _create_mission(client)
    r = client.post("/missions/mission-001/cancel")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "cancelled_tasks" in data

    mission = client.get("/missions/mission-001").json()
    assert mission["status"] == "cancelled"


def test_cancel_mission_idempotent(client):
    """Cancelling a cancelled mission returns 200 both times."""
    _create_mission(client)
    r1 = client.post("/missions/mission-001/cancel")
    assert r1.status_code == 200

    r2 = client.post("/missions/mission-001/cancel")
    assert r2.status_code == 200
    assert r2.json() == {"ok": True, "cancelled_tasks": []}


def test_carry_with_mission_id_appends_to_task_ids(client):
    """POST /tasks with mission_id links the task to the mission."""
    _create_mission(client)
    r = _carry(client, task_id="task-m1", mission_id="mission-001")
    assert r.status_code == 201

    mission = client.get("/missions/mission-001").json()
    assert "task-m1" in mission["task_ids"]


def test_carry_with_unknown_mission_id_404(client):
    """POST /tasks with non-existent mission_id returns 404."""
    r = _carry(client, task_id="task-m1", mission_id="no-such-mission")
    assert r.status_code == 404


def test_list_tasks_filter_by_mission_id(client):
    """GET /tasks?mission_id= filters tasks by mission."""
    _create_mission(client)
    _carry(client, task_id="task-m1", mission_id="mission-001")
    _carry(client, task_id="task-no-mission")

    r = client.get("/tasks", params={"mission_id": "mission-001"})
    assert r.status_code == 200
    tasks = r.json()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "task-m1"


def test_get_mission_report_404_when_no_report(client):
    """GET /missions/{id}/report returns 404 when report is None."""
    _create_mission(client)
    r = client.get("/missions/mission-001/report")
    assert r.status_code == 404


def test_get_mission_report_returns_report(client):
    """GET /missions/{id}/report returns the report when set."""
    _create_mission(client)
    # Set a report via patch
    report = {"mission_id": "mission-001", "total_tasks": 5, "merged_tasks": 3}
    client.patch(
        "/missions/mission-001",
        json={"updates": {"report": report}},
    )

    r = client.get("/missions/mission-001/report")
    assert r.status_code == 200
    data = r.json()
    assert data["mission_id"] == "mission-001"
    assert data["total_tasks"] == 5


# ---------------------------------------------------------------------------
# GitHubBackend preflight
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# cancel_mission purge tests (#265)
# ---------------------------------------------------------------------------


def _forage(client, worker_id: str = "worker-1"):
    """Pull the next available task for a worker."""
    return client.post("/tasks/pull", json={"worker_id": worker_id})


def _register_worker(client, worker_id: str = "worker-1"):
    client.post(
        "/workers/register",
        json={
            "worker_id": worker_id,
            "node_id": "node-1",
            "agent_type": "generic",
            "workspace_root": "/tmp/ws",
            "registered_at": "2024-01-01T00:00:00+00:00",
            "last_heartbeat": "2024-01-01T00:00:00+00:00",
        },
    )


def test_cancel_mission_moves_ready_tasks_to_done(client, tmp_path):
    """Cancelling a mission moves all ready tasks to done/ with cancellation metadata."""
    _create_mission(client)
    _carry(client, task_id="task-001", mission_id="mission-001")
    _carry(client, task_id="task-002", mission_id="mission-001")

    r = client.post("/missions/mission-001/cancel")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert sorted(data["cancelled_tasks"]) == ["task-001", "task-002"]

    for tid in ("task-001", "task-002"):
        task = client.get(f"/tasks/{tid}").json()
        assert task["status"] == "done", f"{tid} should be done"
        assert task["cancelled_at"] is not None, f"{tid} missing cancelled_at"
        assert task["cancelled_reason"] == "mission cancelled"
        assert task["current_attempt"] is None
        # Confirm trail entry with action_type=cancel
        cancel_entries = [e for e in task.get("trail", []) if e.get("action_type") == "cancel"]
        assert cancel_entries, f"{tid} has no cancel trail entry"


def test_cancel_mission_moves_blocked_tasks_to_done(client):
    """Cancelling a mission also moves blocked tasks to done/."""
    _create_mission(client)
    _carry(client, task_id="task-blocked", mission_id="mission-001")
    # Block the task
    client.post("/tasks/task-blocked/block", json={"reason": "external dep"})

    r = client.post("/missions/mission-001/cancel")
    assert r.status_code == 200
    assert "task-blocked" in r.json()["cancelled_tasks"]

    task = client.get("/tasks/task-blocked").json()
    assert task["status"] == "done"
    assert task["cancelled_at"] is not None


def test_cancel_mission_supersedes_active_attempt(client):
    """Cancelling a mission supersedes any active attempt on each task."""
    _create_mission(client)
    _carry(client, task_id="task-active", mission_id="mission-001")
    _register_worker(client)
    forage_r = _forage(client)
    assert forage_r.status_code == 200
    attempt_id = forage_r.json()["current_attempt"]
    assert attempt_id is not None

    r = client.post("/missions/mission-001/cancel")
    assert r.status_code == 200
    assert "task-active" in r.json()["cancelled_tasks"]

    task = client.get("/tasks/task-active").json()
    assert task["status"] == "done"
    assert task["current_attempt"] is None
    assert task["cancelled_at"] is not None

    # The attempt should be SUPERSEDED
    superseded = [a for a in task["attempts"] if a["attempt_id"] == attempt_id]
    assert superseded, "original attempt not found"
    assert superseded[0]["status"] == "superseded"
    assert superseded[0]["completed_at"] is not None


def test_cancel_mission_leaves_unrelated_mission_tasks_alone(client):
    """Cancelling mission-A does not affect tasks belonging to mission-B."""
    _create_mission(client, mission_id="mission-A")
    _create_mission(client, mission_id="mission-B")
    _carry(client, task_id="task-a", mission_id="mission-A")
    _carry(client, task_id="task-b", mission_id="mission-B")

    r = client.post("/missions/mission-A/cancel")
    assert r.status_code == 200
    assert "task-a" in r.json()["cancelled_tasks"]
    assert "task-b" not in r.json()["cancelled_tasks"]

    # mission-B task untouched
    task_b = client.get("/tasks/task-b").json()
    assert task_b["status"] == "ready"
    assert task_b.get("cancelled_at") is None


def test_cancel_mission_excludes_cancelled_task_from_merge_queue(tmp_path):
    """After cancel, Soldier.get_merge_queue() returns empty list for cancelled tasks."""
    from fastapi.testclient import TestClient

    from antfarm.core.backends.file import FileBackend
    from antfarm.core.colony_client import ColonyClient
    from antfarm.core.serve import get_app
    from antfarm.core.soldier import Soldier

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend)
    http_client = TestClient(app, raise_server_exceptions=True)
    colony_client = ColonyClient("http://testserver", client=http_client)

    # Create mission + task
    http_client.post("/missions", json={"mission_id": "mission-001", "spec": "test"})
    http_client.post(
        "/tasks",
        json={"id": "task-001", "title": "t", "spec": "s", "mission_id": "mission-001"},
    ).raise_for_status()

    # Manually move task to done/ with a branch so it looks merge-eligible
    colony_client.register_worker("w1", "node-1", "generic", "/tmp/ws")
    task = colony_client.forage("w1")
    assert task is not None
    colony_client.harvest("task-001", task["current_attempt"], "http://pr/1", "feat/task-001")

    # Confirm it looks merge-eligible before cancel
    soldier = Soldier(
        colony_url="http://testserver",
        repo_path=str(tmp_path),
        client=http_client,
    )
    queue_before = soldier.get_merge_queue()
    assert any(t["id"] == "task-001" for t in queue_before)

    # Cancel mission
    http_client.post("/missions/mission-001/cancel")

    # Now the task should be excluded from the merge queue
    queue_after = soldier.get_merge_queue()
    assert not any(t["id"] == "task-001" for t in queue_after)


def test_cancel_mission_idempotent_on_already_cancelled(client):
    """Second cancel call on an already-cancelled mission returns ok with empty list."""
    _create_mission(client)
    _carry(client, task_id="task-001", mission_id="mission-001")

    r1 = client.post("/missions/mission-001/cancel")
    assert r1.status_code == 200

    r2 = client.post("/missions/mission-001/cancel")
    assert r2.status_code == 200
    assert r2.json() == {"ok": True, "cancelled_tasks": []}


def test_cancel_mission_returns_payload_with_cancelled_task_count(client):
    """Cancelling returns ok=True and the list of cancelled task IDs."""
    _create_mission(client)
    _carry(client, task_id="task-001", mission_id="mission-001")
    _carry(client, task_id="task-002", mission_id="mission-001")

    r = client.post("/missions/mission-001/cancel")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert len(data["cancelled_tasks"]) == 2


# ---------------------------------------------------------------------------
# GitHubBackend preflight
# ---------------------------------------------------------------------------


def test_create_mission_rejects_github_backend(tmp_path):
    """POST /missions returns 400 when backend is GitHubBackend."""
    from unittest.mock import MagicMock

    from antfarm.core.backends.github import GitHubBackend

    mock_backend = MagicMock(spec=GitHubBackend)
    # Make isinstance() check work
    mock_backend.__class__ = GitHubBackend
    app = get_app(backend=mock_backend)
    gh_client = TestClient(app)

    r = gh_client.post("/missions", json={"spec": "test"})
    assert r.status_code == 400
    assert "FileBackend" in r.json()["detail"]
    assert "v0.6.0" in r.json()["detail"]
