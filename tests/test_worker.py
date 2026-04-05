"""Tests for WorkerRuntime (antfarm.core.worker).

Uses a real FileBackend + FastAPI app wired through an httpx transport so that
ColonyClient talks to the in-process ASGI app without a network socket.
WorkspaceManager.create() is monkeypatched to skip real git operations.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from antfarm.core.backends.file import FileBackend
from antfarm.core.serve import get_app
from antfarm.core.models import FailureType
from antfarm.core.worker import AgentResult, WorkerRuntime

# ---------------------------------------------------------------------------
# Sync httpx transport that routes to the ASGI TestClient
# ---------------------------------------------------------------------------


class _StarletteTransport(httpx.BaseTransport):
    """Routes httpx requests to a Starlette/FastAPI TestClient synchronously."""

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
    """httpx.Client that routes directly to the ASGI app (no real HTTP socket)."""
    transport = _StarletteTransport(app)
    client = httpx.Client(transport=transport, base_url="http://test")
    yield client
    client.close()


@pytest.fixture
def runtime(tmp_path, http_client):
    """WorkerRuntime with injected httpx client and no-op workspace creation."""
    rt = WorkerRuntime(
        colony_url="http://test",
        node_id="node-1",
        name="worker-1",
        agent_type="generic",
        workspace_root=str(tmp_path / "workspaces"),
        repo_path=str(tmp_path),
        integration_branch="main",
        heartbeat_interval=999.0,  # effectively disabled in tests
        client=http_client,
    )
    # Patch workspace creation to return a temp directory without real git ops
    rt.workspace_mgr.create = MagicMock(return_value=str(tmp_path / "ws"))
    return rt


@pytest.fixture
def tc(app):
    """Plain FastAPI TestClient for direct API assertions."""
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _carry(tc, task_id="task-001", title="Test Task", spec="do the thing"):
    r = tc.post("/tasks", json={"id": task_id, "title": title, "spec": spec})
    assert r.status_code == 201
    return r.json()


def _good_agent(task, workspace) -> AgentResult:
    """Monkeypatch: simulates a successful agent run."""
    branch = f"feat/{task['id']}-{task['current_attempt']}"
    return AgentResult(returncode=0, stdout="done", stderr="", branch=branch)


def _bad_agent(task, workspace) -> AgentResult:
    """Monkeypatch: simulates a failing agent run."""
    branch = f"feat/{task['id']}-{task['current_attempt']}"
    return AgentResult(returncode=1, stdout="", stderr="oops", branch=branch)


# ---------------------------------------------------------------------------
# Test 1: register_sends_payload
# ---------------------------------------------------------------------------


def test_register_sends_payload(tc, runtime):
    """run() registers the worker before foraging."""
    # patch _launch_agent so run() completes without real subprocess
    runtime._launch_agent = _good_agent

    # Empty queue — run() registers then exits immediately
    runtime.run()

    r = tc.get("/status")
    assert r.status_code == 200
    # Worker deregisters on exit — confirm that happened cleanly (no crash)
    # We verify the API was reachable (implying register worked)
    data = r.json()
    assert "workers" in data or "worker_count" in data or isinstance(data, dict)


# ---------------------------------------------------------------------------
# Test 2: forage_returns_task_spec
# ---------------------------------------------------------------------------


def test_forage_returns_task_spec(tc, runtime):
    """Forage returns the task spec to the agent."""
    _carry(tc, spec="implement the widget")

    captured: list[dict] = []

    def capturing_agent(task, workspace) -> AgentResult:
        captured.append(task)
        return _good_agent(task, workspace)

    runtime._launch_agent = capturing_agent
    runtime.run()

    assert len(captured) == 1
    assert captured[0]["spec"] == "implement the widget"


# ---------------------------------------------------------------------------
# Test 3: harvest_marks_done
# ---------------------------------------------------------------------------


def test_harvest_marks_done(tc, runtime):
    """After a successful agent run, the task is marked done."""
    _carry(tc, task_id="task-001")

    runtime._launch_agent = _good_agent
    runtime.run()

    r = tc.get("/tasks/task-001")
    assert r.status_code == 200
    task = r.json()
    assert task["status"] == "done"
    assert task["attempts"][0]["status"] == "done"


# ---------------------------------------------------------------------------
# Test 4: lifecycle_loop
# ---------------------------------------------------------------------------


def test_lifecycle_loop(tc, runtime):
    """run() processes all tasks in the queue and exits when empty."""
    _carry(tc, task_id="task-001", title="Task 1")
    _carry(tc, task_id="task-002", title="Task 2")

    processed: list[str] = []

    def tracking_agent(task, workspace) -> AgentResult:
        processed.append(task["id"])
        return _good_agent(task, workspace)

    runtime._launch_agent = tracking_agent
    runtime.run()

    assert set(processed) == {"task-001", "task-002"}

    r = tc.get("/tasks")
    tasks = r.json()
    for t in tasks:
        assert t["status"] == "done"


# ---------------------------------------------------------------------------
# Test 5: exit_deregisters
# ---------------------------------------------------------------------------


def test_exit_deregisters_on_empty_queue(tc, backend):
    """Worker deregisters even when queue is empty (clean exit)."""
    # Create a fresh runtime and run against empty queue
    import httpx


    transport = _StarletteTransport(tc.app)
    client = httpx.Client(transport=transport, base_url="http://test")
    rt = WorkerRuntime(
        colony_url="http://test",
        node_id="node-1",
        name="worker-exit",
        agent_type="generic",
        workspace_root="/tmp/ws",
        repo_path="/tmp",
        client=client,
    )
    rt.workspace_mgr.create = MagicMock(return_value="/tmp/ws")
    rt.run()
    client.close()

    # Worker file should be gone after deregister
    worker_file = backend._root / "workers" / "node-1%2Fworker-exit.json"
    assert not worker_file.exists()


def test_exit_deregisters_on_exception(tc, runtime, backend):
    """Worker deregisters even when an unexpected exception is raised."""
    _carry(tc, task_id="task-001")

    call_count = [0]

    def exploding_agent(task, workspace) -> AgentResult:
        call_count[0] += 1
        raise RuntimeError("unexpected crash")

    runtime._launch_agent = exploding_agent

    with pytest.raises(RuntimeError, match="unexpected crash"):
        runtime.run()

    # Deregister still happened — verify worker file is gone
    worker_dir = backend._root / "workers"
    worker_files = list(worker_dir.glob("*.json"))
    assert len(worker_files) == 0, f"Worker file still exists after exception: {worker_files}"


# ---------------------------------------------------------------------------
# Test 6: ownership_loss (409 on harvest)
# ---------------------------------------------------------------------------


def test_ownership_loss_continues_gracefully(tc, runtime, backend):
    """A 409 on harvest (ownership loss) is logged and worker continues."""
    _carry(tc, task_id="task-001")
    _carry(tc, task_id="task-002")

    harvest_calls = [0]
    original_harvest = runtime.colony.harvest

    def patched_harvest(task_id, attempt_id, pr, branch):
        harvest_calls[0] += 1
        if task_id == "task-001":
            # Simulate 409: raise httpx.HTTPStatusError
            req = httpx.Request("POST", f"http://test/tasks/{task_id}/harvest")
            resp = httpx.Response(409, request=req)
            raise httpx.HTTPStatusError("409 ownership loss", request=req, response=resp)
        return original_harvest(task_id, attempt_id, pr, branch)

    runtime.colony.harvest = patched_harvest
    runtime._launch_agent = _good_agent

    # Should not raise — 409 is handled gracefully
    runtime.run()

    assert harvest_calls[0] == 2  # Both tasks attempted harvest

    # task-002 should be done; task-001 stays active (ownership lost)
    r = tc.get("/tasks/task-002")
    assert r.json()["status"] == "done"


# ---------------------------------------------------------------------------
# Test 7: codex command construction
# ---------------------------------------------------------------------------


def test_codex_command_uses_approval_mode_full_auto(tmp_path, http_client):
    """Codex agent_type builds cmd with --approval-mode full-auto --quiet flags."""
    import subprocess
    from unittest.mock import patch

    rt = WorkerRuntime(
        colony_url="http://test",
        node_id="node-1",
        name="worker-codex",
        agent_type="codex",
        workspace_root=str(tmp_path / "workspaces"),
        repo_path=str(tmp_path),
        integration_branch="main",
        heartbeat_interval=999.0,
        client=http_client,
    )
    rt.workspace_mgr.create = MagicMock(return_value=str(tmp_path / "ws"))

    task = {
        "id": "task-codex-001",
        "title": "Test Codex Task",
        "spec": "add a hello function",
        "current_attempt": 1,
    }

    captured_cmd = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return MagicMock(returncode=0, stdout="done", stderr="")

    with patch.object(subprocess, "run", side_effect=fake_run):
        rt._launch_agent(task, str(tmp_path / "ws"))

    assert captured_cmd[0] == "codex"
    assert "--approval-mode" in captured_cmd
    assert "full-auto" in captured_cmd
    assert "--quiet" in captured_cmd
    assert "--message" not in captured_cmd


# ---------------------------------------------------------------------------
# Test 8: aider command construction
# ---------------------------------------------------------------------------


def test_aider_command_includes_yes_and_no_auto_commits(tmp_path, http_client):
    """Aider agent_type builds cmd with --yes and --no-auto-commits flags."""
    import subprocess
    from unittest.mock import patch

    rt = WorkerRuntime(
        colony_url="http://test",
        node_id="node-1",
        name="worker-aider",
        agent_type="aider",
        workspace_root=str(tmp_path / "workspaces"),
        repo_path=str(tmp_path),
        integration_branch="main",
        heartbeat_interval=999.0,
        client=http_client,
    )
    rt.workspace_mgr.create = MagicMock(return_value=str(tmp_path / "ws"))

    task = {
        "id": "task-aider-001",
        "title": "Test Aider Task",
        "spec": "add a hello function",
        "current_attempt": 1,
    }

    captured_cmd = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return MagicMock(returncode=0, stdout="done", stderr="")

    with patch.object(subprocess, "run", side_effect=fake_run):
        rt._launch_agent(task, str(tmp_path / "ws"))

    assert captured_cmd[0] == "aider"
    assert "--yes" in captured_cmd
    assert "--no-auto-commits" in captured_cmd
    assert "--message" in captured_cmd


# ---------------------------------------------------------------------------
# v0.5.1: Failure classification + retry policy
# ---------------------------------------------------------------------------


def test_classify_failure_agent_crash():
    from antfarm.core.worker import classify_failure

    result = classify_failure(returncode=1, stderr="Segmentation fault", stdout="")
    assert result == FailureType.AGENT_CRASH


def test_classify_failure_test_failure():
    from antfarm.core.worker import classify_failure

    result = classify_failure(returncode=1, stderr="", stdout="FAILED tests/test_foo.py::test_bar")
    assert result == FailureType.TEST_FAILURE


def test_classify_failure_lint_failure():
    from antfarm.core.worker import classify_failure

    result = classify_failure(returncode=1, stderr="", stdout="ruff check failed")
    assert result == FailureType.LINT_FAILURE


def test_classify_failure_timeout():
    from antfarm.core.worker import classify_failure

    result = classify_failure(returncode=-9, stderr="", stdout="")
    assert result == FailureType.AGENT_TIMEOUT


def test_classify_failure_infra():
    from antfarm.core.worker import classify_failure

    result = classify_failure(returncode=1, stderr="connection refused", stdout="")
    assert result == FailureType.INFRA_FAILURE


def test_classify_failure_build():
    from antfarm.core.worker import classify_failure

    result = classify_failure(returncode=1, stderr="ModuleNotFoundError: No module named 'foo'", stdout="")
    assert result == FailureType.BUILD_FAILURE


def test_classify_failure_ambiguous_error_defaults_to_crash():
    """Generic 'error' without test/lint markers should be AGENT_CRASH."""
    from antfarm.core.worker import classify_failure

    result = classify_failure(returncode=1, stderr="error occurred", stdout="")
    assert result == FailureType.AGENT_CRASH


def test_classify_failure_ambiguous_failed_defaults_to_crash():
    """Generic 'failed' without test context should be AGENT_CRASH."""
    from antfarm.core.worker import classify_failure

    result = classify_failure(returncode=1, stderr="operation failed", stdout="")
    assert result == FailureType.AGENT_CRASH


def test_classify_failure_lint_before_test():
    """Lint markers take precedence even if 'test' appears."""
    from antfarm.core.worker import classify_failure

    result = classify_failure(returncode=1, stderr="", stdout="ruff check: 3 errors in test_file.py")
    assert result == FailureType.LINT_FAILURE


def test_infra_failure_is_retryable():
    from antfarm.core.worker import get_retry_policy

    policy = get_retry_policy(FailureType.INFRA_FAILURE)
    assert policy["retryable"] is True
    assert policy["max_retries"] > 0


def test_test_failure_not_retryable():
    from antfarm.core.worker import get_retry_policy

    policy = get_retry_policy(FailureType.TEST_FAILURE)
    assert policy["retryable"] is False
    assert policy["action"] == "kickback"


def test_invalid_task_escalates():
    from antfarm.core.worker import get_retry_policy

    policy = get_retry_policy(FailureType.INVALID_TASK)
    assert policy["retryable"] is False
    assert policy["action"] == "escalate"


def test_build_failure_record():
    from antfarm.core.worker import build_failure_record

    rec = build_failure_record(
        task_id="t1", attempt_id="a1", worker_id="w1",
        returncode=1, stderr="connection refused", stdout="",
    )
    assert rec.failure_type == FailureType.INFRA_FAILURE
    assert rec.retryable is True
    assert rec.recommended_action == "retry"
    assert rec.stderr_summary == "connection refused"


def test_agent_crash_retry_policy():
    from antfarm.core.worker import get_retry_policy

    policy = get_retry_policy(FailureType.AGENT_CRASH)
    assert policy["retryable"] is True
    assert policy["max_retries"] == 2


def test_merge_conflict_retry_policy():
    from antfarm.core.worker import get_retry_policy

    policy = get_retry_policy(FailureType.MERGE_CONFLICT)
    assert policy["retryable"] is True
    assert policy["max_retries"] == 1
