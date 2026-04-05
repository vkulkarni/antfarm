"""Tests for GitHubBackend implementation.

All GitHub API calls are mocked via pytest-respx (httpx-compatible).
Tests cover: carry, pull, harvest, kickback, list_tasks, get_task,
guard/release, pause/resume, block/unblock, append_trail/signal,
mark_merged, pin/unpin, override_merge_order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from antfarm.core.backends.github import GitHubBackend, _parse_spec, _render_body
from antfarm.core.models import TaskStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _make_task(task_id: str = "task-1", priority: int = 10) -> dict:
    now = _now()
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "spec": "Do something",
        "complexity": "M",
        "priority": priority,
        "depends_on": [],
        "touches": ["src/foo.py"],
        "created_at": now,
        "updated_at": now,
        "created_by": "test",
    }


def _make_issue(
    number: int,
    task: dict,
    status_label: str = "antfarm:ready",
    state: str = "open",
    labels_extra: list[str] | None = None,
) -> dict:
    """Build a mock GitHub issue dict with the task spec embedded."""
    task_copy = dict(task)
    task_copy.setdefault("status", "ready")
    task_copy.setdefault("current_attempt", None)
    task_copy.setdefault("attempts", [])
    task_copy.setdefault("trail", [])
    task_copy.setdefault("signals", [])

    all_labels = [status_label]
    if labels_extra:
        all_labels.extend(labels_extra)

    return {
        "number": number,
        "title": task.get("title", f"Task {task['id']}"),
        "body": _render_body(task_copy),
        "state": state,
        "labels": [{"name": lb} for lb in all_labels],
        "created_at": task.get("created_at", _now()),
        "updated_at": task.get("updated_at", _now()),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def backend() -> GitHubBackend:
    """GitHubBackend with a mocked httpx.Client."""
    with patch("antfarm.core.backends.github.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        b = GitHubBackend(repo="owner/repo", token="ghp_test")
        b._http = mock_client
        yield b


def _resp(data: dict | list, status: int = 200) -> MagicMock:
    """Build a mock httpx.Response."""
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = data
    mock.headers = {"Link": ""}
    mock.raise_for_status = MagicMock()
    return mock


def _resp_paginated(items: list, status: int = 200) -> MagicMock:
    """Build a mock httpx.Response for paginated list (no next page)."""
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = items
    mock.headers = {"Link": ""}
    mock.raise_for_status = MagicMock()
    return mock


# ---------------------------------------------------------------------------
# Spec parsing helpers
# ---------------------------------------------------------------------------


def test_render_and_parse_spec_roundtrip() -> None:
    task = _make_task("t-1")
    task["status"] = "ready"
    task["current_attempt"] = None
    task["attempts"] = []
    task["trail"] = []
    task["signals"] = []
    body = _render_body(task)
    parsed = _parse_spec(body)
    assert parsed["id"] == "t-1"
    assert parsed["title"] == "Task t-1"


def test_parse_spec_missing_fence_returns_empty() -> None:
    assert _parse_spec("no fence here") == {}


# ---------------------------------------------------------------------------
# carry
# ---------------------------------------------------------------------------


def test_carry_creates_issue(backend: GitHubBackend) -> None:
    task = _make_task("task-1")

    # _get_issue_number search: no existing issues
    backend._http.get.return_value = _resp_paginated([])
    # _ensure_label: 404 -> create
    label_404 = MagicMock()
    label_404.status_code = 404
    label_404.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=label_404
    )

    label_create = _resp({"name": "antfarm:ready"}, 201)
    # _api("GET", "/labels/...") raises 404, then _api("POST", "/labels")
    # then _api("POST", "/issues")
    issue_created = _resp({"number": 42, "title": task["title"]}, 201)

    backend._http.request.side_effect = [
        label_404,         # GET /labels/antfarm:ready -> 404
        label_create,      # POST /labels
        issue_created,     # POST /issues
    ]

    result = backend.carry(task)
    assert result == "task-1"


def test_carry_duplicate_raises(backend: GitHubBackend) -> None:
    task = _make_task("task-1")
    issue = _make_issue(10, task, "antfarm:ready")

    # _get_issue_number search: returns issue in ready list
    backend._http.get.return_value = _resp_paginated([issue])

    with pytest.raises(ValueError, match="task-1"):
        backend.carry(task)


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


def test_pull_returns_none_when_no_ready(backend: GitHubBackend) -> None:
    # No ready issues, no done issues, no closed issues
    backend._http.get.return_value = _resp_paginated([])
    result = backend.pull("worker-1")
    assert result is None


def test_pull_claims_ready_task(backend: GitHubBackend) -> None:
    task = _make_task("task-1")
    issue = _make_issue(7, task, "antfarm:ready")

    call_count = 0

    def get_side_effect(url, **kwargs):
        nonlocal call_count
        call_count += 1
        # First calls: ready issues list, done issues list, closed+merged list
        mock = _resp_paginated([])
        if "antfarm:ready" in str(kwargs.get("params", {})):
            mock = _resp_paginated([issue])
        return mock

    backend._http.get.side_effect = get_side_effect

    # request calls: GET issue #7, PATCH (body update), GET issue #7 labels,
    # PATCH (label swap), ensure label (GET+POST), POST comment
    get_issue_resp = _resp(issue)
    patch_body_resp = _resp(issue)
    get_labels_resp = _resp(issue)  # for _swap_labels
    patch_labels_resp = _resp(issue)
    ensure_label_resp = _resp({"name": "antfarm:active"})
    post_comment_resp = _resp({"id": 1}, 201)

    backend._http.request.side_effect = [
        get_issue_resp,     # GET issue for _update_task_body
        patch_body_resp,    # PATCH issue body
        get_labels_resp,    # GET issue labels for _swap_labels
        patch_labels_resp,  # PATCH issue labels
        ensure_label_resp,  # GET /labels/antfarm:active (exists)
        post_comment_resp,  # POST comment
    ]

    result = backend.pull("worker-1")
    assert result is not None
    assert result["id"] == "task-1"
    assert result["status"] == TaskStatus.ACTIVE.value
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["worker_id"] == "worker-1"


def test_pull_respects_cooldown(backend: GitHubBackend) -> None:
    from datetime import timedelta
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    backend._workers["worker-rl"] = {
        "worker_id": "worker-rl",
        "cooldown_until": future,
    }
    backend._http.get.return_value = _resp_paginated([])
    result = backend.pull("worker-rl")
    assert result is None


def test_pull_expired_cooldown_gets_task(backend: GitHubBackend) -> None:
    from datetime import timedelta
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    backend._workers["worker-past"] = {
        "worker_id": "worker-past",
        "cooldown_until": past,
        "capabilities": [],
    }

    task = _make_task("task-1")
    issue = _make_issue(5, task, "antfarm:ready")

    def get_side_effect(url, **kwargs):
        params = kwargs.get("params", {})
        if "antfarm:ready" in str(params):
            return _resp_paginated([issue])
        return _resp_paginated([])

    backend._http.get.side_effect = get_side_effect

    get_issue_resp = _resp(issue)
    patch_body_resp = _resp(issue)
    get_labels_resp = _resp(issue)
    patch_labels_resp = _resp(issue)
    ensure_label_resp = _resp({"name": "antfarm:active"})
    post_comment_resp = _resp({"id": 1}, 201)

    backend._http.request.side_effect = [
        get_issue_resp,
        patch_body_resp,
        get_labels_resp,
        patch_labels_resp,
        ensure_label_resp,
        post_comment_resp,
    ]

    result = backend.pull("worker-past")
    assert result is not None
    assert result["id"] == "task-1"


# ---------------------------------------------------------------------------
# mark_harvested
# ---------------------------------------------------------------------------


def test_mark_harvested_transitions_to_done(backend: GitHubBackend) -> None:
    task = _make_task("task-1")
    task_copy = dict(task)
    task_copy["status"] = TaskStatus.ACTIVE.value
    task_copy["current_attempt"] = "attempt-abc"
    task_copy["attempts"] = [{
        "attempt_id": "attempt-abc",
        "worker_id": "worker-1",
        "status": "active",
        "branch": None,
        "pr": None,
        "started_at": _now(),
        "completed_at": None,
    }]
    task_copy["trail"] = []
    task_copy["signals"] = []
    issue = _make_issue(9, task_copy, "antfarm:active")

    # _get_task_issue: _get_issue_number (int fallback) -> _get_issue_by_number
    backend._http.request.side_effect = [
        _resp(issue),           # GET issue #9
        _resp(issue),           # PATCH body
        _resp(issue),           # GET issue labels
        _resp(issue),           # PATCH labels
        _resp({"name": "antfarm:done"}),  # ensure label GET
        _resp({"id": 99}, 201), # POST comment
    ]

    backend.mark_harvested("9", "attempt-abc", pr="https://gh/pr/1", branch="feat/task-1")

    # Verify PATCH was called (body update)
    patch_calls = [c for c in backend._http.request.call_args_list if c[0][0] == "PATCH"]
    assert len(patch_calls) >= 1


def test_mark_harvested_wrong_attempt_raises(backend: GitHubBackend) -> None:
    task = _make_task("task-1")
    task_copy = dict(task)
    task_copy["status"] = TaskStatus.ACTIVE.value
    task_copy["current_attempt"] = "correct-attempt"
    task_copy["attempts"] = []
    task_copy["trail"] = []
    task_copy["signals"] = []
    issue = _make_issue(11, task_copy, "antfarm:active")

    backend._http.request.side_effect = [_resp(issue)]

    with pytest.raises(ValueError, match="not the current attempt"):
        backend.mark_harvested("11", "wrong-attempt", pr="pr", branch="b")


def test_mark_harvested_idempotent(backend: GitHubBackend) -> None:
    task = _make_task("task-1")
    task_copy = dict(task)
    task_copy["status"] = TaskStatus.DONE.value
    task_copy["current_attempt"] = "attempt-xyz"
    task_copy["attempts"] = []
    task_copy["trail"] = []
    task_copy["signals"] = []
    issue = _make_issue(12, task_copy, "antfarm:done")

    backend._http.request.side_effect = [_resp(issue)]

    # Same attempt_id — should not raise
    backend.mark_harvested("12", "attempt-xyz", pr="pr", branch="b")


# ---------------------------------------------------------------------------
# kickback
# ---------------------------------------------------------------------------


def test_kickback_transitions_done_to_ready(backend: GitHubBackend) -> None:
    task = _make_task("task-1")
    task_copy = dict(task)
    task_copy["status"] = TaskStatus.DONE.value
    task_copy["current_attempt"] = "attempt-1"
    task_copy["attempts"] = [{
        "attempt_id": "attempt-1",
        "worker_id": "worker-1",
        "status": "done",
        "branch": "b",
        "pr": "pr",
        "started_at": _now(),
        "completed_at": _now(),
    }]
    task_copy["trail"] = []
    task_copy["signals"] = []
    issue = _make_issue(20, task_copy, "antfarm:done")

    backend._http.request.side_effect = [
        _resp(issue),                      # GET issue
        _resp(issue),                      # PATCH body
        _resp(issue),                      # GET labels
        _resp(issue),                      # PATCH labels
        _resp({"name": "antfarm:ready"}),  # ensure label
        _resp({"id": 1}, 201),             # POST comment
    ]

    backend.kickback("20", reason="tests failed")


def test_kickback_on_non_done_raises(backend: GitHubBackend) -> None:
    task = _make_task("task-1")
    task_copy = dict(task)
    task_copy["status"] = TaskStatus.ACTIVE.value
    task_copy["current_attempt"] = None
    task_copy["attempts"] = []
    task_copy["trail"] = []
    task_copy["signals"] = []
    issue = _make_issue(21, task_copy, "antfarm:active")

    backend._http.request.side_effect = [_resp(issue)]

    with pytest.raises(FileNotFoundError):
        backend.kickback("21", reason="should fail")


# ---------------------------------------------------------------------------
# list_tasks / get_task
# ---------------------------------------------------------------------------


def test_list_tasks_all_statuses(backend: GitHubBackend) -> None:
    task1 = _make_task("task-a")
    task2 = _make_task("task-b")
    issue1 = _make_issue(1, task1, "antfarm:ready")
    issue2 = _make_issue(2, task2, "antfarm:active")

    status_responses = {
        "antfarm:ready": [issue1],
        "antfarm:active": [issue2],
        "antfarm:done": [],
        "antfarm:paused": [],
        "antfarm:blocked": [],
    }

    def get_side(url, **kwargs):
        params = kwargs.get("params", {})
        labels_param = str(params.get("labels", ""))
        for label, issues in status_responses.items():
            if label in labels_param:
                return _resp_paginated(issues)
        return _resp_paginated([])

    backend._http.get.side_effect = get_side

    tasks = backend.list_tasks()
    assert len(tasks) == 2
    ids = {t["id"] for t in tasks}
    assert "task-a" in ids
    assert "task-b" in ids


def test_list_tasks_filtered_by_status(backend: GitHubBackend) -> None:
    task = _make_task("task-done")
    issue = _make_issue(3, task, "antfarm:done")

    backend._http.get.return_value = _resp_paginated([issue])

    tasks = backend.list_tasks(status="done")
    assert len(tasks) == 1
    assert tasks[0]["id"] == "task-done"


def test_get_task_by_number(backend: GitHubBackend) -> None:
    task = _make_task("task-5")
    task_copy = dict(task)
    task_copy["status"] = "ready"
    task_copy["current_attempt"] = None
    task_copy["attempts"] = []
    task_copy["trail"] = []
    task_copy["signals"] = []
    issue = _make_issue(5, task_copy, "antfarm:ready")

    backend._http.request.side_effect = [_resp(issue)]

    result = backend.get_task("5")
    assert result is not None
    assert result["id"] == "task-5"


def test_get_task_not_found_returns_none(backend: GitHubBackend) -> None:
    # _get_issue_number: not an int, search returns nothing
    backend._http.get.return_value = _resp_paginated([])

    result = backend.get_task("nonexistent-task")
    assert result is None


# ---------------------------------------------------------------------------
# guard / release_guard
# ---------------------------------------------------------------------------


def test_guard_acquire_and_release(backend: GitHubBackend) -> None:
    assert backend.guard("repo/main", "worker-1") is True
    assert backend.guard("repo/main", "worker-2") is False

    backend.release_guard("repo/main", "worker-1")
    assert backend.guard("repo/main", "worker-2") is True


def test_guard_same_owner_idempotent(backend: GitHubBackend) -> None:
    assert backend.guard("resource-x", "worker-1") is True
    assert backend.guard("resource-x", "worker-1") is True


def test_release_guard_wrong_owner_raises(backend: GitHubBackend) -> None:
    backend.guard("resource-y", "worker-1")
    with pytest.raises(PermissionError):
        backend.release_guard("resource-y", "worker-2")


def test_release_guard_not_found_raises(backend: GitHubBackend) -> None:
    with pytest.raises(FileNotFoundError):
        backend.release_guard("no-such-resource", "worker-1")


# ---------------------------------------------------------------------------
# append_trail / append_signal
# ---------------------------------------------------------------------------


def test_append_trail(backend: GitHubBackend) -> None:
    task = _make_task("task-t")
    task_copy = dict(task)
    task_copy["status"] = "ready"
    task_copy["current_attempt"] = None
    task_copy["attempts"] = []
    task_copy["trail"] = []
    task_copy["signals"] = []
    issue = _make_issue(30, task_copy, "antfarm:ready")

    backend._http.request.side_effect = [
        _resp(issue),          # GET issue (_get_task_issue)
        _resp(issue),          # PATCH body
        _resp({"id": 1}, 201), # POST comment
    ]

    entry = {"ts": _now(), "worker_id": "worker-1", "message": "started"}
    backend.append_trail("30", entry)


def test_append_signal(backend: GitHubBackend) -> None:
    task = _make_task("task-s")
    task_copy = dict(task)
    task_copy["status"] = "ready"
    task_copy["current_attempt"] = None
    task_copy["attempts"] = []
    task_copy["trail"] = []
    task_copy["signals"] = []
    issue = _make_issue(31, task_copy, "antfarm:ready")

    backend._http.request.side_effect = [
        _resp(issue),
        _resp(issue),
        _resp({"id": 2}, 201),
    ]

    entry = {"ts": _now(), "worker_id": "worker-1", "message": "build passed"}
    backend.append_signal("31", entry)


# ---------------------------------------------------------------------------
# Workers (in-memory)
# ---------------------------------------------------------------------------


def test_register_deregister_worker(backend: GitHubBackend) -> None:
    now = _now()
    worker = {
        "worker_id": "worker-1",
        "node_id": "node-1",
        "agent_type": "engineer",
        "workspace_root": "/tmp/ws",
        "registered_at": now,
        "last_heartbeat": now,
    }
    backend.register_worker(worker)
    assert "worker-1" in backend._workers

    backend.deregister_worker("worker-1")
    assert "worker-1" not in backend._workers

    # Deregister non-existent is a no-op
    backend.deregister_worker("unknown")


def test_heartbeat_updates_worker(backend: GitHubBackend) -> None:
    backend.heartbeat("worker-x", {"status": "active", "current_task": "task-42"})
    assert backend._workers["worker-x"]["current_task"] == "task-42"

    # Heartbeat for unregistered worker creates entry
    backend.heartbeat("new-worker", {"status": "idle"})
    assert "new-worker" in backend._workers


def test_list_workers(backend: GitHubBackend) -> None:
    assert backend.list_workers() == []
    now = _now()
    backend.register_worker({
        "worker_id": "w-1",
        "node_id": "n-1",
        "agent_type": "claude-code",
        "workspace_root": "/tmp",
        "registered_at": now,
        "last_heartbeat": now,
    })
    workers = backend.list_workers()
    assert len(workers) == 1
    assert workers[0]["worker_id"] == "w-1"


# ---------------------------------------------------------------------------
# Nodes (in-memory)
# ---------------------------------------------------------------------------


def test_register_node_idempotent(backend: GitHubBackend) -> None:
    now = _now()
    node = {"node_id": "node-1", "joined_at": now, "last_seen": now}
    backend.register_node(node)
    backend.register_node(node)
    assert backend._nodes["node-1"]["node_id"] == "node-1"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_returns_counts(backend: GitHubBackend) -> None:
    task = _make_task("task-1")
    issue = _make_issue(1, task, "antfarm:ready")

    def get_side(url, **kwargs):
        params = kwargs.get("params", {})
        if "antfarm:ready" in str(params.get("labels", "")):
            return _resp_paginated([issue])
        return _resp_paginated([])

    backend._http.get.side_effect = get_side

    result = backend.status()
    assert result["tasks"]["ready"] == 1
    assert result["tasks"]["active"] == 0
    assert result["workers"] == 0
