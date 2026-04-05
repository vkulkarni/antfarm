"""Tests for bearer token authentication (antfarm.core.auth + serve integration)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from antfarm.core.auth import generate_token, verify_token
from antfarm.core.serve import get_app  # noqa: I001

SECRET = "test-secret-key"


# ---------------------------------------------------------------------------
# Unit tests for auth module
# ---------------------------------------------------------------------------


def test_generate_token_deterministic():
    t1 = generate_token(SECRET)
    t2 = generate_token(SECRET)
    assert t1 == t2
    assert len(t1) == 64  # SHA-256 hex digest


def test_generate_token_different_secrets():
    assert generate_token("secret-a") != generate_token("secret-b")


def test_verify_token_valid():
    token = generate_token(SECRET)
    assert verify_token(token, SECRET) is True


def test_verify_token_invalid():
    assert verify_token("bad-token", SECRET) is False


def test_verify_token_wrong_secret():
    token = generate_token("other-secret")
    assert verify_token(token, SECRET) is False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_client(tmp_path):
    """TestClient with auth enabled."""
    from antfarm.core.backends.file import FileBackend

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend, auth_secret=SECRET)
    return TestClient(app)


@pytest.fixture
def noauth_client(tmp_path):
    """TestClient without auth (baseline)."""
    from antfarm.core.backends.file import FileBackend

    backend = FileBackend(root=str(tmp_path / ".antfarm"))
    app = get_app(backend=backend)
    return TestClient(app)


@pytest.fixture
def valid_token():
    return generate_token(SECRET)


@pytest.fixture
def auth_headers(valid_token):
    return {"Authorization": f"Bearer {valid_token}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _carry(client, headers=None, task_id="task-001"):
    return client.post(
        "/tasks",
        json={"id": task_id, "title": "Test Task", "spec": "Do the thing"},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Integration tests: auth enabled
# ---------------------------------------------------------------------------


class TestAuthEnabled:
    def test_status_requires_no_auth(self, auth_client):
        """GET /status is always public, even with auth enabled."""
        r = auth_client.get("/status")
        assert r.status_code == 200

    def test_carry_without_token_returns_401(self, auth_client):
        r = _carry(auth_client)
        assert r.status_code == 401
        assert "Authorization" in r.json()["detail"]

    def test_carry_with_invalid_token_returns_401(self, auth_client):
        r = _carry(auth_client, headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code == 401

    def test_carry_with_valid_token_succeeds(self, auth_client, auth_headers):
        r = _carry(auth_client, headers=auth_headers)
        assert r.status_code == 201

    def test_list_tasks_without_token_returns_401(self, auth_client):
        r = auth_client.get("/tasks")
        assert r.status_code == 401

    def test_list_tasks_with_valid_token_succeeds(self, auth_client, auth_headers):
        _carry(auth_client, headers=auth_headers)
        r = auth_client.get("/tasks", headers=auth_headers)
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_forage_without_token_returns_401(self, auth_client):
        r = auth_client.post("/tasks/pull", json={"worker_id": "w-1"})
        assert r.status_code == 401

    def test_forage_with_valid_token_succeeds(self, auth_client, auth_headers):
        _carry(auth_client, headers=auth_headers)
        r = auth_client.post("/tasks/pull", json={"worker_id": "w-1"}, headers=auth_headers)
        assert r.status_code == 200

    def test_register_node_requires_auth(self, auth_client, auth_headers):
        r = auth_client.post("/nodes", json={"node_id": "node-1"})
        assert r.status_code == 401

        r = auth_client.post("/nodes", json={"node_id": "node-1"}, headers=auth_headers)
        assert r.status_code == 200

    def test_register_worker_requires_auth(self, auth_client, auth_headers):
        payload = {
            "worker_id": "w-1",
            "node_id": "n-1",
            "agent_type": "generic",
            "workspace_root": "/tmp/ws",
        }
        r = auth_client.post("/workers/register", json=payload)
        assert r.status_code == 401

        r = auth_client.post("/workers/register", json=payload, headers=auth_headers)
        assert r.status_code == 201

    def test_guard_requires_auth(self, auth_client, auth_headers):
        r = auth_client.post("/guards/repo", json={"owner": "w-1"})
        assert r.status_code == 401

        r = auth_client.post("/guards/repo", json={"owner": "w-1"}, headers=auth_headers)
        assert r.status_code == 200

    def test_delete_guard_requires_auth(self, auth_client, auth_headers):
        auth_client.post("/guards/repo", json={"owner": "w-1"}, headers=auth_headers)
        r = auth_client.delete("/guards/repo", params={"owner": "w-1"})
        assert r.status_code == 401

        r = auth_client.delete(
            "/guards/repo", params={"owner": "w-1"}, headers=auth_headers
        )
        assert r.status_code == 200

    def test_malformed_auth_header_returns_401(self, auth_client):
        r = _carry(auth_client, headers={"Authorization": "Basic abc123"})
        assert r.status_code == 401

    def test_full_lifecycle_with_auth(self, auth_client, auth_headers):
        """Carry → forage → trail → harvest works end-to-end with auth."""
        # Carry
        r = _carry(auth_client, headers=auth_headers)
        assert r.status_code == 201

        # Forage
        r = auth_client.post(
            "/tasks/pull", json={"worker_id": "w-1"}, headers=auth_headers
        )
        assert r.status_code == 200
        task = r.json()
        attempt_id = task["current_attempt"]

        # Trail
        r = auth_client.post(
            f"/tasks/{task['id']}/trail",
            json={"worker_id": "w-1", "message": "working"},
            headers=auth_headers,
        )
        assert r.status_code == 200

        # Harvest
        r = auth_client.post(
            f"/tasks/{task['id']}/harvest",
            json={"attempt_id": attempt_id, "pr": "pr-1", "branch": "feat/x"},
            headers=auth_headers,
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Integration tests: auth disabled (no regression)
# ---------------------------------------------------------------------------


class TestAuthDisabled:
    def test_carry_works_without_auth(self, noauth_client):
        """When no auth_secret is set, endpoints work without tokens."""
        r = _carry(noauth_client)
        assert r.status_code == 201

    def test_status_works_without_auth(self, noauth_client):
        r = noauth_client.get("/status")
        assert r.status_code == 200
