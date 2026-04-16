"""Tests for antfarm.core.doctor — pre-flight diagnostic and stale recovery."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from antfarm.core.backends.file import FileBackend
from antfarm.core.doctor import run_doctor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def setup(tmp_path: Path):
    data_dir = str(tmp_path / ".antfarm")
    backend = FileBackend(root=data_dir)
    config = {"data_dir": data_dir, "worker_ttl": 300, "guard_ttl": 300}
    return backend, config


def _make_task(task_id: str = "task-1", depends_on: list | None = None) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "spec": "Do something",
        "complexity": "M",
        "priority": 10,
        "depends_on": depends_on or [],
        "touches": ["src/foo.py"],
        "created_at": now,
        "updated_at": now,
        "created_by": "test",
    }


def _make_worker(worker_id: str = "worker-1", workspace_root: str = "/tmp/ws1") -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "worker_id": worker_id,
        "node_id": "node-1",
        "agent_type": "engineer",
        "workspace_root": workspace_root,
        "status": "idle",
        "registered_at": now,
        "last_heartbeat": now,
    }


def _backdate(path: str | Path, seconds: int = 600) -> None:
    """Set file mtime to `seconds` ago."""
    old_time = time.time() - seconds
    os.utime(str(path), (old_time, old_time))


# ---------------------------------------------------------------------------
# 1. test_healthy_colony_no_findings
# ---------------------------------------------------------------------------


def test_healthy_colony_no_findings(setup):
    backend, config = setup
    findings = run_doctor(backend, config)
    # Exclude tmux_available: it fires in CI/minimal environments where tmux
    # is not installed — this is expected and not a colony health issue.
    errors_warnings = [
        f for f in findings
        if f.severity in ("error", "warning") and f.check != "tmux_available"
    ]
    assert errors_warnings == [], f"Expected no errors/warnings, got: {errors_warnings}"


# ---------------------------------------------------------------------------
# 2. test_stale_worker_detected
# ---------------------------------------------------------------------------


def test_stale_worker_detected(setup):
    backend, config = setup
    worker = _make_worker("worker-stale")
    backend.register_worker(worker)

    # Backdate the worker file
    data_dir = Path(config["data_dir"])
    worker_file = data_dir / "workers" / "worker-stale.json"
    _backdate(worker_file, seconds=600)

    findings = run_doctor(backend, config, fix=False)
    stale = [f for f in findings if f.check == "stale_worker"]
    assert len(stale) == 1
    assert "worker-stale" in stale[0].message
    assert stale[0].fixed is False


# ---------------------------------------------------------------------------
# 3. test_stale_worker_fixed
# ---------------------------------------------------------------------------


def test_stale_worker_fixed(setup):
    backend, config = setup
    worker = _make_worker("worker-stale")
    backend.register_worker(worker)

    data_dir = Path(config["data_dir"])
    worker_file = data_dir / "workers" / "worker-stale.json"
    _backdate(worker_file, seconds=600)

    findings = run_doctor(backend, config, fix=True)
    stale = [f for f in findings if f.check == "stale_worker"]
    assert len(stale) == 1
    assert stale[0].fixed is True
    # Worker file should be gone
    assert not worker_file.exists()


# ---------------------------------------------------------------------------
# 4. test_stale_task_detected
# ---------------------------------------------------------------------------


def test_stale_task_detected(setup):
    backend, config = setup
    # Register a worker, carry a task, forage it (creates active task)
    worker = _make_worker("worker-dead")
    backend.register_worker(worker)
    backend.carry(_make_task("task-stale"))
    backend.pull("worker-dead")

    # Kill the worker (deregister)
    backend.deregister_worker("worker-dead")

    findings = run_doctor(backend, config, fix=False)
    stale = [f for f in findings if f.check == "stale_task"]
    assert len(stale) == 1
    assert "task-stale" in stale[0].message
    assert stale[0].fixed is False


# ---------------------------------------------------------------------------
# 5. test_stale_task_fixed
# ---------------------------------------------------------------------------


def test_stale_task_fixed(setup):
    backend, config = setup
    worker = _make_worker("worker-dead")
    backend.register_worker(worker)
    backend.carry(_make_task("task-stale"))
    task_data = backend.pull("worker-dead")
    attempt_id = task_data["current_attempt"]

    # Kill the worker
    backend.deregister_worker("worker-dead")

    findings = run_doctor(backend, config, fix=True)
    stale = [f for f in findings if f.check == "stale_task"]
    assert len(stale) == 1
    assert stale[0].fixed is True

    # Task should now be in ready/ with status "ready"
    data_dir = Path(config["data_dir"])
    active_file = data_dir / "tasks" / "active" / "task-stale.json"
    ready_file = data_dir / "tasks" / "ready" / "task-stale.json"
    assert not active_file.exists()
    assert ready_file.exists()

    recovered = json.loads(ready_file.read_text())
    assert recovered["status"] == "ready"
    assert recovered["current_attempt"] is None

    # Attempt should be superseded
    superseded = [a for a in recovered["attempts"] if a["attempt_id"] == attempt_id]
    assert len(superseded) == 1
    assert superseded[0]["status"] == "superseded"

    # Trail should have doctor entry
    trail_msgs = [t["message"] for t in recovered.get("trail", [])]
    assert any("recovered by doctor" in m for m in trail_msgs)


# ---------------------------------------------------------------------------
# 6. test_stale_guard_detected
# ---------------------------------------------------------------------------


def test_stale_guard_detected(setup):
    backend, config = setup
    # No live worker for this guard
    backend.guard("resource/lock", "worker-gone")

    data_dir = Path(config["data_dir"])
    guard_file = data_dir / "guards" / "resource__lock.lock"
    _backdate(guard_file, seconds=600)

    findings = run_doctor(backend, config, fix=False)
    stale = [f for f in findings if f.check == "stale_guard"]
    assert len(stale) == 1
    assert stale[0].fixed is False


# ---------------------------------------------------------------------------
# 7. test_stale_guard_fixed
# ---------------------------------------------------------------------------


def test_stale_guard_fixed(setup):
    backend, config = setup
    backend.guard("resource/lock", "worker-gone")

    data_dir = Path(config["data_dir"])
    guard_file = data_dir / "guards" / "resource__lock.lock"
    _backdate(guard_file, seconds=600)

    findings = run_doctor(backend, config, fix=True)
    stale = [f for f in findings if f.check == "stale_guard"]
    assert len(stale) == 1
    assert stale[0].fixed is True
    assert not guard_file.exists()


# ---------------------------------------------------------------------------
# 8. test_workspace_conflict_detected
# ---------------------------------------------------------------------------


def test_workspace_conflict_detected(setup):
    backend, config = setup
    shared_ws = "/tmp/shared-workspace"
    backend.register_worker(_make_worker("worker-a", workspace_root=shared_ws))
    backend.register_worker(_make_worker("worker-b", workspace_root=shared_ws))

    findings = run_doctor(backend, config, fix=False)
    conflicts = [f for f in findings if f.check == "workspace_conflict"]
    assert len(conflicts) == 1
    assert "worker-a" in conflicts[0].message or "worker-b" in conflicts[0].message


# ---------------------------------------------------------------------------
# 9. test_orphan_workspace_reported
# ---------------------------------------------------------------------------


def test_orphan_workspace_reported(setup, tmp_path):
    backend, config = setup
    # Create a workspace_root with a worktree dir
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    orphan = workspace_root / "orphan-worktree"
    orphan.mkdir()

    config["workspace_root"] = str(workspace_root)

    findings = run_doctor(backend, config, fix=False)
    orphans = [f for f in findings if f.check == "orphan_workspace"]
    assert len(orphans) == 1
    assert "orphan-worktree" in orphans[0].message


# ---------------------------------------------------------------------------
# 10. test_folder_status_mismatch
# ---------------------------------------------------------------------------


def test_folder_status_mismatch(setup):
    backend, config = setup
    # Carry a task (lands in ready/ with status=ready), then manually corrupt it
    backend.carry(_make_task("task-mismatch"))

    data_dir = Path(config["data_dir"])
    task_file = data_dir / "tasks" / "ready" / "task-mismatch.json"
    data = json.loads(task_file.read_text())
    data["status"] = "done"  # Mismatch: file is in ready/ but status says done
    task_file.write_text(json.dumps(data))

    findings = run_doctor(backend, config, fix=False)
    mismatches = [f for f in findings if f.check == "state_consistency" and "status" in f.message]
    assert len(mismatches) == 1
    assert "task-mismatch" in mismatches[0].message


# ---------------------------------------------------------------------------
# 11. test_dependency_cycle_detected
# ---------------------------------------------------------------------------


def test_dependency_cycle_detected(setup):
    backend, config = setup
    # task-a depends on task-b, task-b depends on task-a
    backend.carry(_make_task("task-a", depends_on=["task-b"]))
    backend.carry(_make_task("task-b", depends_on=["task-a"]))

    findings = run_doctor(backend, config, fix=False)
    cycles = [f for f in findings if f.check == "dependency_cycles"]
    assert len(cycles) >= 1
    assert any("task-a" in f.message and "task-b" in f.message for f in cycles)


# ---------------------------------------------------------------------------
# 12. test_dangling_dependency_detected
# ---------------------------------------------------------------------------


def test_dangling_dependency_detected(setup):
    backend, config = setup
    backend.carry(_make_task("task-orphan", depends_on=["task-nonexistent"]))

    findings = run_doctor(backend, config, fix=False)
    dangling = [f for f in findings if f.check == "dangling_dependency"]
    assert len(dangling) == 1
    assert "task-nonexistent" in dangling[0].message


# ---------------------------------------------------------------------------
# 13. test_malformed_json_detected
# ---------------------------------------------------------------------------


def test_malformed_json_detected(setup):
    backend, config = setup
    data_dir = Path(config["data_dir"])
    # Write garbage into a task file
    garbage_file = data_dir / "tasks" / "ready" / "task-corrupt.json"
    garbage_file.write_text("{ this is not valid JSON !!!")

    findings = run_doctor(backend, config, fix=False)
    malformed = [f for f in findings if f.check == "state_consistency" and "Malformed" in f.message]
    assert len(malformed) == 1
    assert "task-corrupt.json" in malformed[0].message


# ---------------------------------------------------------------------------
# 14. test_filesystem_check_creates_dirs
# ---------------------------------------------------------------------------


def test_filesystem_check_creates_dirs(setup):
    backend, config = setup
    data_dir = Path(config["data_dir"])

    # Delete a required subdir
    import shutil
    shutil.rmtree(str(data_dir / "guards"))
    assert not (data_dir / "guards").exists()

    findings = run_doctor(backend, config, fix=True)
    fs_findings = [f for f in findings if f.check == "filesystem"]
    assert len(fs_findings) >= 1
    assert all(f.fixed for f in fs_findings)
    # Directory should be recreated
    assert (data_dir / "guards").exists()


# ---------------------------------------------------------------------------
# 15. test_worktree_is_clean helper
# ---------------------------------------------------------------------------


def test_orphan_worktree_detected_dry_run(setup, tmp_path):
    """Orphan worktree is reported in dry-run mode (fix=False)."""
    backend, config = setup
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir(parents=True)
    orphan = ws_root / "task-orphan-att-001"
    orphan.mkdir()

    config["workspace_root"] = str(ws_root)

    findings = run_doctor(backend, config, fix=False)
    orphan_findings = [f for f in findings if f.check == "orphan_workspace"]
    assert any(str(orphan) in f.message or "task-orphan" in f.message for f in orphan_findings)


def test_orphan_worktree_clean_deleted_on_fix(setup, tmp_path):
    """Clean orphan worktree is auto-deleted when fix=True."""
    import subprocess

    backend, config = setup

    # Create a real git repo
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test"], cwd=str(repo), capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True
    )
    (repo / "file.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True
    )

    # Create a bare remote so worktree has an upstream
    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(bare)],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=str(repo), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=str(repo), capture_output=True, check=False,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "master"],
        cwd=str(repo), capture_output=True, check=False,
    )

    # Create a worktree with upstream tracking
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir(parents=True)
    wt_path = ws_root / "task-orphan-att-001"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feat/orphan", str(wt_path)],
        cwd=str(repo), capture_output=True, check=True,
    )
    # Push the branch so it has an upstream
    subprocess.run(
        ["git", "push", "-u", "origin", "feat/orphan"],
        cwd=str(wt_path), capture_output=True, check=True,
    )
    assert wt_path.exists()

    config["workspace_root"] = str(ws_root)
    # data_dir must point to repo so git worktree remove runs from correct cwd
    config["data_dir"] = str(repo / ".antfarm")

    from antfarm.core.doctor import check_orphan_workspaces

    findings = check_orphan_workspaces(config, fix=True)
    orphan_findings = [f for f in findings if f.check == "orphan_workspace"]
    assert len(orphan_findings) == 1
    assert orphan_findings[0].fixed is True
    assert "auto-deleted" in orphan_findings[0].message
    assert not wt_path.exists()


def test_worktree_is_clean_no_upstream_returns_false(tmp_path):
    """A worktree with no upstream configured returns False (safe default)."""
    import subprocess

    # Create a real git repo and worktree
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test"], cwd=str(repo), capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True
    )
    (repo / "file.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True
    )

    # Create a worktree (no remote/upstream)
    wt_path = tmp_path / "workspaces" / "task-orphan-att-001"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feat/orphan", str(wt_path)],
        cwd=str(repo), capture_output=True, check=True,
    )
    assert wt_path.exists()

    from antfarm.core.doctor import _worktree_is_clean

    # No upstream -> not provably clean -> returns False (safe default)
    assert _worktree_is_clean(str(wt_path)) is False


def test_worktree_is_clean_dirty_returns_false(tmp_path):
    """A worktree with uncommitted changes returns False."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test"], cwd=str(repo), capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True
    )
    (repo / "file.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True
    )

    wt_path = tmp_path / "workspaces" / "task-dirty"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feat/dirty", str(wt_path)],
        cwd=str(repo), capture_output=True, check=True,
    )

    # Create uncommitted changes
    (wt_path / "new_file.txt").write_text("dirty")

    from antfarm.core.doctor import _worktree_is_clean

    assert _worktree_is_clean(str(wt_path)) is False


def test_worktree_is_clean_nonexistent_returns_false():
    """A non-existent path returns False."""
    from antfarm.core.doctor import _worktree_is_clean

    assert _worktree_is_clean("/nonexistent/path") is False


# ---------------------------------------------------------------------------
# 16. test_doctor_runner_unreachable
# ---------------------------------------------------------------------------


def test_doctor_runner_unreachable(setup):
    """Runner health check reports unreachable runners."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import check_runner_health

    backend = MagicMock()
    backend.list_nodes.return_value = [
        {"node_id": "node-1", "runner_url": "http://unreachable-host:7434"},
    ]

    # Mock urllib to raise connection error
    with patch("urllib.request.urlopen", side_effect=OSError("Connection refused")):
        findings = check_runner_health(backend, {})

    assert len(findings) == 1
    assert findings[0].check == "runner_health"
    assert findings[0].severity == "warning"
    assert "node-1" in findings[0].message
    assert "unreachable" in findings[0].message.lower()


# ---------------------------------------------------------------------------
# 17. test_doctor_runner_reachable
# ---------------------------------------------------------------------------


def test_doctor_runner_reachable(setup):
    """Runner health check reports nothing when runners are reachable."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import check_runner_health

    backend = MagicMock()
    backend.list_nodes.return_value = [
        {"node_id": "node-1", "runner_url": "http://healthy-host:7434"},
    ]

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        findings = check_runner_health(backend, {})

    assert len(findings) == 0


# ---------------------------------------------------------------------------
# 18. test_check_tmux_available — tmux installed
# ---------------------------------------------------------------------------


def test_check_tmux_available_when_installed():
    """Returns [] when tmux is installed."""
    from unittest.mock import patch

    from antfarm.core.doctor import check_tmux_available

    with patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"):
        findings = check_tmux_available({})

    assert findings == []


# ---------------------------------------------------------------------------
# 19. test_check_tmux_available — tmux missing
# ---------------------------------------------------------------------------


def test_check_tmux_available_when_missing():
    """Returns a warning finding when tmux is not installed."""
    from unittest.mock import patch

    from antfarm.core.doctor import check_tmux_available

    with patch("antfarm.core.doctor.shutil.which", return_value=None):
        findings = check_tmux_available({})

    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert findings[0].check == "tmux_available"
    assert "subprocess fallback" in findings[0].message
    assert findings[0].auto_fixable is False


# ---------------------------------------------------------------------------
# 20. test_check_orphan_tmux_sessions — tmux not installed
# ---------------------------------------------------------------------------


def test_check_orphan_tmux_sessions_no_tmux():
    """Returns [] when tmux is not installed."""
    from unittest.mock import patch

    from antfarm.core.doctor import check_orphan_tmux_sessions

    with patch("antfarm.core.doctor.shutil.which", return_value=None):
        findings = check_orphan_tmux_sessions({})

    assert findings == []


# ---------------------------------------------------------------------------
# 21. test_check_orphan_tmux_sessions — orphan and non-orphan detection
# ---------------------------------------------------------------------------


def test_check_orphan_tmux_sessions_detects_orphans(tmp_path):
    """Antfarm-prefixed session without metadata emits warning; one WITH metadata does not."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import check_orphan_tmux_sessions

    data_dir = tmp_path / ".antfarm"
    processes_dir = data_dir / "processes"
    processes_dir.mkdir(parents=True)

    # Write metadata for the non-orphan session
    (processes_dir / "auto-builder-1.json").write_text(
        '{"name": "auto-builder-1", "role": "builder", "manager_type": "tmux"}'
    )

    config = {"data_dir": str(data_dir)}

    # Two antfarm sessions: one with metadata, one orphan; plus one non-antfarm session
    session_names = "auto-builder-1\nauto-planner-2\nsome-unrelated-session\n"

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = session_names

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", return_value=mock_result),
    ):
        findings = check_orphan_tmux_sessions(config)

    orphan_checks = [f for f in findings if f.check == "orphan_tmux_session"]
    assert len(orphan_checks) == 1
    assert "auto-planner-2" in orphan_checks[0].message
    assert orphan_checks[0].severity == "warning"
    assert orphan_checks[0].auto_fixable is False


def test_check_orphan_tmux_sessions_no_server():
    """Returns [] when tmux server is not running (returncode != 0)."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import check_orphan_tmux_sessions

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", return_value=mock_result),
    ):
        findings = check_orphan_tmux_sessions({})

    assert findings == []
