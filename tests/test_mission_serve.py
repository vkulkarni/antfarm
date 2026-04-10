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
    assert r.json() == {"ok": True}

    mission = client.get("/missions/mission-001").json()
    assert mission["status"] == "cancelled"


def test_cancel_mission_idempotent(client):
    """Cancelling a cancelled mission returns 200 both times."""
    _create_mission(client)
    r1 = client.post("/missions/mission-001/cancel")
    assert r1.status_code == 200

    r2 = client.post("/missions/mission-001/cancel")
    assert r2.status_code == 200
    assert r2.json() == {"ok": True}


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
