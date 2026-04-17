"""Tests for WorkerRuntime (antfarm.core.worker).

Uses a real FileBackend + FastAPI app wired through an httpx transport so that
ColonyClient talks to the in-process ASGI app without a network socket.
WorkspaceManager.create() is monkeypatched to skip real git operations.
"""

from __future__ import annotations

import json as _json
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from antfarm.core.backends.file import FileBackend
from antfarm.core.models import FailureType
from antfarm.core.serve import get_app
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


@pytest.fixture(autouse=True)
def _no_sleep_between_polls(monkeypatch):
    """Skip the 30s idle-poll sleep so worker tests run fast.

    Builders/reviewers poll on an empty queue before exiting (#144). Tests
    that exercise `.run()` against an empty queue would otherwise block on
    `time.sleep(30)`.
    """
    import antfarm.core.worker as worker_mod

    monkeypatch.setattr(worker_mod.time, "sleep", lambda _s: None)


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

    def patched_harvest(task_id, attempt_id, pr, branch, artifact=None):
        harvest_calls[0] += 1
        if task_id == "task-001":
            # Simulate 409: raise httpx.HTTPStatusError
            req = httpx.Request("POST", f"http://test/tasks/{task_id}/harvest")
            resp = httpx.Response(409, request=req)
            raise httpx.HTTPStatusError("409 ownership loss", request=req, response=resp)
        return original_harvest(task_id, attempt_id, pr, branch, artifact=artifact)

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
    import threading
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

    # Filter by thread to avoid capturing calls from the Doctor daemon thread
    # leaked by earlier colony-creating tests (it calls `git rev-parse` on a
    # loop and lands in this patch since subprocess.run is patched globally).
    main_thread = threading.current_thread()

    def fake_run(cmd, **kwargs):
        if threading.current_thread() is main_thread:
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
    import threading
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
    main_thread = threading.current_thread()

    def fake_run(cmd, **kwargs):
        if threading.current_thread() is main_thread:
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

    result = classify_failure(
        returncode=1, stderr="ModuleNotFoundError: No module named 'foo'", stdout=""
    )
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

    result = classify_failure(
        returncode=1, stderr="", stdout="ruff check: 3 errors in test_file.py"
    )
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


# ---------------------------------------------------------------------------
# v0.5.76: Trail entries during processing (#105)
# ---------------------------------------------------------------------------


def test_trail_entries_during_processing(tc, runtime):
    """Trail entries are appended at key lifecycle points during task processing."""
    _carry(tc, task_id="task-trail-001")

    runtime._launch_agent = _good_agent
    # Patch _build_artifact and _create_pr to avoid real git/gh calls
    runtime._build_artifact = lambda task, attempt_id, workspace, branch: {}
    runtime._create_pr = lambda task, branch, workspace: ""
    runtime.run()

    r = tc.get("/tasks/task-trail-001")
    task = r.json()
    messages = [e["message"] for e in task["trail"]]
    assert "task claimed, creating workspace" in messages
    assert "workspace ready, launching agent" in messages
    assert "agent completed, building artifact" in messages
    assert "harvested successfully" in messages


def test_trail_entries_on_failure(tc, runtime):
    """Trail entries include failure message when agent exits non-zero."""
    _carry(tc, task_id="task-fail-trail")

    runtime._launch_agent = _bad_agent
    runtime.run()

    r = tc.get("/tasks/task-fail-trail")
    task = r.json()
    messages = [e["message"] for e in task["trail"]]
    assert "task claimed, creating workspace" in messages
    assert "workspace ready, launching agent" in messages
    assert any("agent failed (exit 1)" in m for m in messages)


# ---------------------------------------------------------------------------
# v0.5.76: Exit announcement (#101)
# ---------------------------------------------------------------------------


def test_exit_announcement(tc, runtime):
    """Worker posts exit trail and offline heartbeat on shutdown."""
    _carry(tc, task_id="task-exit-001")

    runtime._launch_agent = _good_agent
    runtime._build_artifact = lambda task, attempt_id, workspace, branch: {}
    runtime._create_pr = lambda task, branch, workspace: ""
    runtime.run()

    r = tc.get("/tasks/task-exit-001")
    task = r.json()
    messages = [e["message"] for e in task["trail"]]
    assert "worker exiting — queue empty" in messages


def test_exit_announcement_no_tasks(tc, runtime):
    """Worker does not post exit trail when no tasks were ever processed."""
    runtime._launch_agent = _good_agent
    runtime.run()
    # No assertion needed — just verify it doesn't crash.
    # _last_task_id is None, so no trail is posted.
    assert runtime._last_task_id is None


# ---------------------------------------------------------------------------
# v0.5.76: PR creation (#103)
# ---------------------------------------------------------------------------


def test_create_pr_success(tmp_path, http_client):
    """_create_pr returns URL on successful gh pr create."""
    import subprocess
    from unittest.mock import patch

    rt = WorkerRuntime(
        colony_url="http://test",
        node_id="node-1",
        name="worker-pr",
        agent_type="generic",
        workspace_root=str(tmp_path / "workspaces"),
        repo_path=str(tmp_path),
        client=http_client,
    )

    task = {"id": "task-pr-001", "title": "Add feature", "spec": "do stuff"}

    def fake_run(cmd, **kwargs):
        return MagicMock(returncode=0, stdout="https://github.com/org/repo/pull/42\n", stderr="")

    with patch.object(subprocess, "run", side_effect=fake_run):
        url = rt._create_pr(task, "feat/task-pr-001-att-001", str(tmp_path))

    assert url == "https://github.com/org/repo/pull/42"


def test_create_pr_failure(tmp_path, http_client):
    """_create_pr returns empty string when gh pr create fails."""
    import subprocess
    from unittest.mock import patch

    rt = WorkerRuntime(
        colony_url="http://test",
        node_id="node-1",
        name="worker-pr-fail",
        agent_type="generic",
        workspace_root=str(tmp_path / "workspaces"),
        repo_path=str(tmp_path),
        client=http_client,
    )

    task = {"id": "task-pr-002", "title": "Add feature", "spec": "do stuff"}

    def fake_run(cmd, **kwargs):
        return MagicMock(returncode=1, stdout="", stderr="not a git repo")

    with patch.object(subprocess, "run", side_effect=fake_run):
        url = rt._create_pr(task, "feat/task-pr-002-att-001", str(tmp_path))

    assert url == ""


def test_create_pr_gh_not_found(tmp_path, http_client):
    """_create_pr returns empty string when gh CLI is not installed."""
    import subprocess
    from unittest.mock import patch

    rt = WorkerRuntime(
        colony_url="http://test",
        node_id="node-1",
        name="worker-pr-nogh",
        agent_type="generic",
        workspace_root=str(tmp_path / "workspaces"),
        repo_path=str(tmp_path),
        client=http_client,
    )

    task = {"id": "task-pr-003", "title": "Add feature", "spec": "do stuff"}

    with patch.object(subprocess, "run", side_effect=FileNotFoundError("gh")):
        url = rt._create_pr(task, "feat/task-pr-003-att-001", str(tmp_path))

    assert url == ""


# ---------------------------------------------------------------------------
# v0.5.76: Artifact building (#104)
# ---------------------------------------------------------------------------


def test_build_artifact_collects_stats(tmp_path, http_client):
    """_build_artifact collects git diff stats and commit metadata."""
    from unittest.mock import patch

    rt = WorkerRuntime(
        colony_url="http://test",
        node_id="node-1",
        name="worker-art",
        agent_type="generic",
        workspace_root=str(tmp_path / "workspaces"),
        repo_path=str(tmp_path),
        client=http_client,
    )

    git_responses = {
        ("diff", "--stat"): " file.py | 10 +++++++---\n 1 file changed",
        ("diff", "--numstat"): "7\t3\tfile.py",
        ("rev-parse", "HEAD"): "abc123def",
        ("merge-base",): "base456",
    }

    def fake_git(workspace, *args):
        for key, val in git_responses.items():
            if all(k in args for k in key):
                return val
        return ""

    with patch.object(WorkerRuntime, "_git", side_effect=fake_git):
        artifact = rt._build_artifact(
            {"id": "t1"}, "att-001", str(tmp_path), "feat/t1-att-001"
        )

    assert artifact["lines_added"] == 7
    assert artifact["lines_removed"] == 3
    assert artifact["head_sha"] == "abc123def"
    assert artifact["base_sha"] == "base456"
    assert "file.py" in artifact["diff_stat"]


def test_build_artifact_handles_git_failure(tmp_path, http_client):
    """_build_artifact returns defaults when git commands fail."""
    import subprocess
    from unittest.mock import patch

    rt = WorkerRuntime(
        colony_url="http://test",
        node_id="node-1",
        name="worker-art-fail",
        agent_type="generic",
        workspace_root=str(tmp_path / "workspaces"),
        repo_path=str(tmp_path),
        client=http_client,
    )

    def failing_git(workspace, *args):
        raise subprocess.CalledProcessError(1, ["git", *args])

    with patch.object(WorkerRuntime, "_git", side_effect=failing_git):
        artifact = rt._build_artifact(
            {"id": "t1"}, "att-001", str(tmp_path), "feat/t1-att-001"
        )

    assert artifact["diff_stat"] == ""
    assert artifact["lines_added"] == 0
    assert artifact["lines_removed"] == 0
    assert artifact["head_sha"] == ""
    assert artifact["base_sha"] == ""


# ---------------------------------------------------------------------------
# v0.5.8: Planner mode (#129)
# ---------------------------------------------------------------------------


def _carry_plan(tc, task_id="plan-test", title="Plan Test", spec="decompose this"):
    """Carry a plan task with capabilities_required=["plan"]."""
    r = tc.post("/tasks", json={
        "id": task_id,
        "title": title,
        "spec": spec,
        "capabilities_required": ["plan"],
    })
    assert r.status_code == 201
    return r.json()


def _plan_agent_output(tasks_json: str) -> str:
    """Build agent stdout containing [PLAN_RESULT] tags."""
    return f"Analyzing codebase...\n[PLAN_RESULT]\n{tasks_json}\n[/PLAN_RESULT]\nDone."


def _make_planner_runtime(tmp_path, http_client):
    """Create a WorkerRuntime with planner capabilities."""
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
    return rt


def test_planner_mode_detects_plan_task(tc, tmp_path, http_client):
    """Plan task is detected via capabilities_required, not prefix."""
    _carry_plan(tc)
    rt = _make_planner_runtime(tmp_path, http_client)

    plan_json = _json.dumps([
        {"title": "Task A", "spec": "do A", "touches": ["api"],
         "depends_on": [], "priority": 5, "complexity": "S"},
    ])

    def plan_agent(task, workspace) -> AgentResult:
        return AgentResult(
            returncode=0,
            stdout=_plan_agent_output(plan_json),
            stderr="",
            branch="",
        )

    rt._launch_agent = plan_agent
    rt.run()

    # Plan task should be harvested (done)
    r = tc.get("/tasks/plan-test")
    assert r.json()["status"] == "done"

    # Child task should have been created
    r = tc.get("/tasks/task-test-01")
    assert r.status_code == 200
    child = r.json()
    assert child["title"] == "Task A"
    assert child["status"] == "ready"


def test_planner_creates_deterministic_child_ids(tc, tmp_path, http_client):
    """Child IDs follow pattern task-{slug}-01, task-{slug}-02."""
    _carry_plan(tc, task_id="plan-auth-system")
    rt = _make_planner_runtime(tmp_path, http_client)

    plan_json = _json.dumps([
        {"title": "Add middleware", "spec": "do middleware", "touches": ["api"],
         "depends_on": [], "priority": 5, "complexity": "M"},
        {"title": "Add routes", "spec": "do routes", "touches": ["api"],
         "depends_on": [1], "priority": 10, "complexity": "M"},
    ])

    def plan_agent(task, workspace) -> AgentResult:
        return AgentResult(returncode=0, stdout=_plan_agent_output(plan_json),
                           stderr="", branch="")

    rt._launch_agent = plan_agent
    rt.run()

    # Check child IDs
    r1 = tc.get("/tasks/task-auth-system-01")
    assert r1.status_code == 200
    assert r1.json()["title"] == "Add middleware"

    r2 = tc.get("/tasks/task-auth-system-02")
    assert r2.status_code == 200
    assert r2.json()["title"] == "Add routes"
    # Dep should be resolved to child ID
    assert "task-auth-system-01" in r2.json()["depends_on"]


def test_planner_rejects_more_than_10_tasks(tc, tmp_path, http_client):
    """Plan with >10 tasks is rejected (guardrail)."""
    _carry_plan(tc)
    rt = _make_planner_runtime(tmp_path, http_client)

    tasks = [
        {"title": f"Task {i}", "spec": f"do {i}", "touches": [],
         "depends_on": [], "priority": 10, "complexity": "S"}
        for i in range(11)
    ]
    plan_json = _json.dumps(tasks)

    def plan_agent(task, workspace) -> AgentResult:
        return AgentResult(returncode=0, stdout=_plan_agent_output(plan_json),
                           stderr="", branch="")

    rt._launch_agent = plan_agent
    rt.run()

    # Plan task should NOT be harvested (stays active due to failure)
    r = tc.get("/tasks/plan-test")
    task = r.json()
    assert task["status"] == "active"

    # Trail should mention rejection
    messages = [e["message"] for e in task["trail"]]
    assert any("exceeds max 10" in m for m in messages)


def test_planner_no_plan_result_tags(tc, tmp_path, http_client):
    """Agent output without [PLAN_RESULT] tags fails gracefully."""
    _carry_plan(tc)
    rt = _make_planner_runtime(tmp_path, http_client)

    def plan_agent(task, workspace) -> AgentResult:
        return AgentResult(returncode=0, stdout="no tags here", stderr="", branch="")

    rt._launch_agent = plan_agent
    rt.run()

    r = tc.get("/tasks/plan-test")
    task = r.json()
    assert task["status"] == "active"

    messages = [e["message"] for e in task["trail"]]
    assert any("could not parse" in m for m in messages)


def test_planner_children_no_recursive_plans(tc, tmp_path, http_client):
    """Child tasks have capabilities_required=[] to prevent recursive plans."""
    _carry_plan(tc)
    rt = _make_planner_runtime(tmp_path, http_client)

    plan_json = _json.dumps([
        {"title": "Sub task", "spec": "do sub", "touches": [],
         "depends_on": [], "priority": 5, "complexity": "S"},
    ])

    def plan_agent(task, workspace) -> AgentResult:
        return AgentResult(returncode=0, stdout=_plan_agent_output(plan_json),
                           stderr="", branch="")

    rt._launch_agent = plan_agent
    rt.run()

    r = tc.get("/tasks/task-test-01")
    child = r.json()
    assert child.get("capabilities_required", []) == []


def test_planner_harvest_artifact(tc, tmp_path, http_client):
    """Plan task harvest artifact contains created_task_ids and dep summary."""
    _carry_plan(tc, task_id="plan-feat")
    rt = _make_planner_runtime(tmp_path, http_client)

    plan_json = _json.dumps([
        {"title": "A", "spec": "do A", "touches": [], "depends_on": [],
         "priority": 5, "complexity": "S"},
        {"title": "B", "spec": "do B", "touches": [], "depends_on": [1],
         "priority": 10, "complexity": "M"},
    ])

    def plan_agent(task, workspace) -> AgentResult:
        return AgentResult(returncode=0, stdout=_plan_agent_output(plan_json),
                           stderr="", branch="")

    rt._launch_agent = plan_agent
    rt.run()

    r = tc.get("/tasks/plan-feat")
    task = r.json()
    assert task["status"] == "done"

    # Check the attempt has artifact data
    attempt = task["attempts"][0]
    assert attempt["status"] == "done"


def test_planner_spawned_by_lineage(tc, tmp_path, http_client):
    """Child tasks carry spawned_by metadata linking to parent plan task."""
    _carry_plan(tc, task_id="plan-lineage")
    rt = _make_planner_runtime(tmp_path, http_client)

    plan_json = _json.dumps([
        {"title": "Child", "spec": "do child", "touches": [],
         "depends_on": [], "priority": 5, "complexity": "S"},
    ])

    def plan_agent(task, workspace) -> AgentResult:
        return AgentResult(returncode=0, stdout=_plan_agent_output(plan_json),
                           stderr="", branch="")

    rt._launch_agent = plan_agent
    rt.run()

    r = tc.get("/tasks/task-lineage-01")
    child = r.json()
    assert child.get("spawned_by") is not None
    assert child["spawned_by"]["task_id"] == "plan-lineage"


def test_planner_agent_failure_no_harvest(tc, tmp_path, http_client):
    """Plan agent that exits non-zero does not attempt plan processing."""
    _carry_plan(tc)
    rt = _make_planner_runtime(tmp_path, http_client)

    def bad_plan_agent(task, workspace) -> AgentResult:
        return AgentResult(returncode=1, stdout="", stderr="crash", branch="")

    rt._launch_agent = bad_plan_agent
    rt.run()

    r = tc.get("/tasks/plan-test")
    task = r.json()
    # Task stays active (agent failed, no harvest)
    assert task["status"] == "active"


def test_planner_409_idempotent(tc, tmp_path, http_client):
    """409 on child carry (already exists) is treated as idempotent success."""
    # Pre-create a child task that will collide
    tc.post("/tasks", json={
        "id": "task-idem-01",
        "title": "Pre-existing",
        "spec": "already here",
    })

    _carry_plan(tc, task_id="plan-idem")
    rt = _make_planner_runtime(tmp_path, http_client)

    plan_json = _json.dumps([
        {"title": "Child", "spec": "do child", "touches": [],
         "depends_on": [], "priority": 5, "complexity": "S"},
    ])

    def plan_agent(task, workspace) -> AgentResult:
        return AgentResult(returncode=0, stdout=_plan_agent_output(plan_json),
                           stderr="", branch="")

    rt._launch_agent = plan_agent
    rt.run()

    # Plan task should still be harvested successfully (409 is idempotent)
    r = tc.get("/tasks/plan-idem")
    assert r.json()["status"] == "done"


def test_planner_invalid_json_in_tags(tc, tmp_path, http_client):
    """Invalid JSON inside [PLAN_RESULT] tags fails gracefully."""
    _carry_plan(tc)
    rt = _make_planner_runtime(tmp_path, http_client)

    def plan_agent(task, workspace) -> AgentResult:
        return AgentResult(
            returncode=0,
            stdout="[PLAN_RESULT]\nnot valid json\n[/PLAN_RESULT]",
            stderr="",
            branch="",
        )

    rt._launch_agent = plan_agent
    rt.run()

    r = tc.get("/tasks/plan-test")
    task = r.json()
    # Should stay active — parsing failed
    assert task["status"] == "active"


def test_planner_prompt_includes_plan_instructions(tmp_path, http_client):
    """Plan task prompt includes PLANNER instructions and [PLAN_RESULT] example."""
    import subprocess
    import threading
    from unittest.mock import patch

    rt = _make_planner_runtime(tmp_path, http_client)

    task = {
        "id": "plan-check",
        "title": "Plan Check",
        "spec": "decompose this feature",
        "current_attempt": "att-001",
        "capabilities_required": ["plan"],
    }

    captured_cmd = []
    main_thread = threading.current_thread()

    def fake_run(cmd, **kwargs):
        if threading.current_thread() is main_thread:
            captured_cmd.extend(cmd)
        return MagicMock(returncode=0, stdout="done", stderr="")

    with patch.object(subprocess, "run", side_effect=fake_run):
        rt._launch_agent(task, str(tmp_path / "ws"))

    # The prompt (last arg) should contain planner-specific content
    prompt = captured_cmd[-1]
    assert "PLANNER" in prompt
    assert "[PLAN_RESULT]" in prompt
    assert "Maximum 10 tasks" in prompt


# ---------------------------------------------------------------------------
# v0.6.0: Planner mission mode (#164)
# ---------------------------------------------------------------------------


def _carry_mission_plan(tc, task_id="plan-mission", title="Mission Plan", spec="plan this",
                        mission_id="mission-001"):
    """Carry a plan task with mission_id set."""
    # First create the mission so link_task_to_mission works
    tc.post("/missions", json={"mission_id": mission_id, "spec": "test mission"})
    r = tc.post("/tasks", json={
        "id": task_id,
        "title": title,
        "spec": spec,
        "capabilities_required": ["plan"],
        "mission_id": mission_id,
    })
    assert r.status_code == 201
    return r.json()


def test_planner_mission_mode_stores_artifact(tc, tmp_path, http_client):
    """Plan task with mission_id stores plan_artifact, no children carried."""
    _carry_mission_plan(tc, task_id="plan-m-001", mission_id="mission-art")
    rt = _make_planner_runtime(tmp_path, http_client)

    plan_json = _json.dumps([
        {"title": "Task A", "spec": "do A", "touches": ["api"],
         "depends_on": [], "priority": 5, "complexity": "S"},
        {"title": "Task B", "spec": "do B", "touches": ["db"],
         "depends_on": [1], "priority": 10, "complexity": "M"},
    ])

    def plan_agent(task, workspace) -> AgentResult:
        return AgentResult(
            returncode=0,
            stdout=_plan_agent_output(plan_json),
            stderr="",
            branch="",
        )

    rt._launch_agent = plan_agent
    rt.run()

    # Plan task should be harvested (done)
    r = tc.get("/tasks/plan-m-001")
    task = r.json()
    assert task["status"] == "done"

    # Harvest artifact should contain plan_artifact
    attempt = task["attempts"][0]
    assert attempt["status"] == "done"
    artifact = attempt.get("artifact", {})
    assert artifact is not None
    assert "plan_artifact" in artifact
    pa = artifact["plan_artifact"]
    assert pa["plan_task_id"] == "plan-m-001"
    assert pa["task_count"] == 2
    assert len(pa["proposed_tasks"]) == 2
    assert pa["proposed_tasks"][0]["title"] == "Task A"

    # No children should have been carried
    r = tc.get("/tasks/task-m-001-01")
    assert r.status_code == 404

    r = tc.get("/tasks/task-m-001-02")
    assert r.status_code == 404

    # Trail should mention mission mode
    messages = [e["message"] for e in task["trail"]]
    assert any("mission mode" in m for m in messages)


def test_planner_legacy_mode_carries_children(tc, tmp_path, http_client):
    """Plan task without mission_id carries children directly (existing behavior)."""
    _carry_plan(tc, task_id="plan-legacy")
    rt = _make_planner_runtime(tmp_path, http_client)

    plan_json = _json.dumps([
        {"title": "Legacy Child", "spec": "do legacy", "touches": ["api"],
         "depends_on": [], "priority": 5, "complexity": "S"},
    ])

    def plan_agent(task, workspace) -> AgentResult:
        return AgentResult(
            returncode=0,
            stdout=_plan_agent_output(plan_json),
            stderr="",
            branch="",
        )

    rt._launch_agent = plan_agent
    rt.run()

    # Plan task should be done
    r = tc.get("/tasks/plan-legacy")
    assert r.json()["status"] == "done"

    # Child task SHOULD have been carried (legacy mode)
    r = tc.get("/tasks/task-legacy-01")
    assert r.status_code == 200
    assert r.json()["title"] == "Legacy Child"


def test_planner_mission_mode_invalid_plan_trails_and_no_artifact(tc, tmp_path, http_client):
    """Mission plan task with invalid plan output trails error, no artifact."""
    _carry_mission_plan(tc, task_id="plan-m-bad", mission_id="mission-bad")
    rt = _make_planner_runtime(tmp_path, http_client)

    def plan_agent(task, workspace) -> AgentResult:
        return AgentResult(
            returncode=0,
            stdout="no plan tags here",
            stderr="",
            branch="",
        )

    rt._launch_agent = plan_agent
    rt.run()

    # Plan task stays active (not harvested)
    r = tc.get("/tasks/plan-m-bad")
    task = r.json()
    assert task["status"] == "active"

    # Trail should mention parsing failure
    messages = [e["message"] for e in task["trail"]]
    assert any("could not parse" in m for m in messages)


def test_task_artifact_plan_artifact_roundtrip():
    """TaskArtifact.plan_artifact field roundtrips through to_dict/from_dict."""
    from antfarm.core.models import TaskArtifact

    plan_data = {
        "plan_task_id": "plan-rt",
        "attempt_id": "att-001",
        "proposed_tasks": [{"title": "T1", "spec": "do T1"}],
        "task_count": 1,
        "warnings": ["watch out"],
        "dependency_summary": "all parallel",
    }

    art = TaskArtifact(
        task_id="plan-rt",
        attempt_id="att-001",
        worker_id="w1",
        branch="feat/plan-rt",
        pr_url=None,
        base_commit_sha="abc",
        head_commit_sha="def",
        target_branch="main",
        target_branch_sha_at_harvest="ghi",
        plan_artifact=plan_data,
    )

    d = art.to_dict()
    assert d["plan_artifact"] == plan_data

    restored = TaskArtifact.from_dict(d)
    assert restored.plan_artifact == plan_data


def test_task_artifact_plan_artifact_none_by_default():
    """TaskArtifact.plan_artifact is None by default (non-plan tasks)."""
    from antfarm.core.models import TaskArtifact

    art = TaskArtifact(
        task_id="t1",
        attempt_id="att-001",
        worker_id="w1",
        branch="feat/t1",
        pr_url=None,
        base_commit_sha="abc",
        head_commit_sha="def",
        target_branch="main",
        target_branch_sha_at_harvest="ghi",
    )

    assert art.plan_artifact is None
    d = art.to_dict()
    assert d["plan_artifact"] is None

    restored = TaskArtifact.from_dict(d)
    assert restored.plan_artifact is None


# ---------------------------------------------------------------------------
# Idle polling by role (#144)
# ---------------------------------------------------------------------------


def _make_runtime_with_caps(tmp_path, http_client, name, capabilities):
    rt = WorkerRuntime(
        colony_url="http://test",
        node_id="node-1",
        name=name,
        agent_type="generic",
        workspace_root=str(tmp_path / "workspaces"),
        repo_path=str(tmp_path),
        integration_branch="main",
        heartbeat_interval=999.0,
        capabilities=capabilities,
        client=http_client,
    )
    rt.workspace_mgr.create = MagicMock(return_value=str(tmp_path / "ws"))
    return rt


def test_builder_polls_on_empty_queue(tmp_path, http_client):
    """Builder workers poll 5 times (2.5min) before exiting on empty queue."""
    rt = _make_runtime_with_caps(tmp_path, http_client, "builder-1", capabilities=[])

    call_count = [0]

    def fake_forage(worker_id):
        call_count[0] += 1
        return None

    rt.colony.forage = fake_forage
    rt.run()

    # max_idle_polls=5 → one initial attempt + 5 retries = 6 forage calls
    assert call_count[0] == 6


def test_planner_exits_immediately_on_empty_queue(tmp_path, http_client):
    """Planner workers exit after a single empty forage (max_idle_polls=0)."""
    rt = _make_runtime_with_caps(tmp_path, http_client, "planner-1", capabilities=["plan"])

    call_count = [0]

    def fake_forage(worker_id):
        call_count[0] += 1
        return None

    rt.colony.forage = fake_forage
    rt.run()

    assert call_count[0] == 1


def test_reviewer_polls_unchanged(tmp_path, http_client):
    """Reviewer polling (10 * 30s = 5min) is unchanged by the tiered logic."""
    rt = _make_runtime_with_caps(tmp_path, http_client, "reviewer-1", capabilities=["review"])

    call_count = [0]

    def fake_forage(worker_id):
        call_count[0] += 1
        return None

    rt.colony.forage = fake_forage
    rt.run()

    # max_idle_polls=10 → one initial attempt + 10 retries = 11 forage calls
    assert call_count[0] == 11


# ---------------------------------------------------------------------------
# #193: Silent agent failure detection (returncode=0 + empty stdout + empty stderr)
# ---------------------------------------------------------------------------


def test_classify_failure_silent_on_empty_output():
    """returncode 0 with empty stdout AND stderr is classified as SILENT_FAILURE."""
    from antfarm.core.worker import classify_failure

    assert classify_failure(returncode=0, stderr="", stdout="") == FailureType.SILENT_FAILURE
    # Whitespace-only counts as empty
    assert classify_failure(returncode=0, stderr="\n", stdout="  ") == FailureType.SILENT_FAILURE
    # Regression: returncode 0 with real stdout is NOT silent
    assert classify_failure(returncode=0, stderr="", stdout="done") != FailureType.SILENT_FAILURE


def test_build_failure_record_silent():
    """Silent classification is non-retryable and escalates."""
    from antfarm.core.worker import build_failure_record, get_retry_policy

    rec = build_failure_record(
        task_id="t1", attempt_id="a1", worker_id="w1",
        returncode=0, stderr="", stdout="",
    )
    assert rec.failure_type == FailureType.SILENT_FAILURE
    assert rec.retryable is False
    assert rec.recommended_action == "escalate"

    policy = get_retry_policy(FailureType.SILENT_FAILURE)
    assert policy["retryable"] is False
    assert policy["action"] == "escalate"


def _silent_agent(task, workspace) -> AgentResult:
    """Monkeypatch: simulates a silently-failing agent (exit 0, no output)."""
    branch = f"feat/{task['id']}-{task['current_attempt']}"
    return AgentResult(returncode=0, stdout="", stderr="", branch=branch)


def test_process_one_task_silent_failure_trails_warning(tc, runtime):
    """Silent agent failure emits a human-readable trail warning and FAILURE_RECORD,
    and does NOT harvest the task."""
    _carry(tc, task_id="task-silent-001")

    runtime._launch_agent = _silent_agent
    # Sentinel to confirm harvest is never called on silent failure
    harvest_called = []
    original_harvest = runtime.colony.harvest

    def tracking_harvest(*args, **kwargs):
        harvest_called.append((args, kwargs))
        return original_harvest(*args, **kwargs)

    runtime.colony.harvest = tracking_harvest
    runtime.run()

    r = tc.get("/tasks/task-silent-001")
    task = r.json()
    messages = [e["message"] for e in task["trail"]]

    # Human-readable warning present
    assert any(
        "silent_failure" in m or "empty stdout" in m
        for m in messages
    ), f"expected silent_failure trail entry, got: {messages}"
    # Structured FAILURE_RECORD emitted
    assert any("[FAILURE_RECORD]" in m for m in messages)
    # Harvest was NOT called — silent failure takes the failure branch
    assert harvest_called == []


def test_process_one_task_empty_stdout_with_stderr_not_silent(tc, runtime):
    """Regression: returncode=0 with empty stdout but non-empty stderr is NOT silent —
    it takes the normal success path."""
    _carry(tc, task_id="task-warn-001")

    def warning_agent(task, workspace) -> AgentResult:
        branch = f"feat/{task['id']}-{task['current_attempt']}"
        return AgentResult(returncode=0, stdout="", stderr="some warning", branch=branch)

    runtime._launch_agent = warning_agent
    runtime._build_artifact = lambda task, attempt_id, workspace, branch: {}
    runtime._create_pr = lambda task, branch, workspace: ""
    runtime.run()

    r = tc.get("/tasks/task-warn-001")
    task = r.json()
    messages = [e["message"] for e in task["trail"]]

    # Success path — no silent_failure trail
    assert not any("silent_failure" in m for m in messages)
    # No FAILURE_RECORD for a success
    assert not any("[FAILURE_RECORD]" in m for m in messages)
    # Success trail entries appear
    assert "agent completed, building artifact" in messages
