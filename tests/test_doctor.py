"""Tests for antfarm.core.doctor — pre-flight diagnostic and stale recovery."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
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
    #
    # orphan_tmux_session is now scoped by colony hash — peer-colony sessions
    # on the same host carry a different hash and are ignored, so it is no
    # longer broadened into this exclusion tuple.
    errors_warnings = [
        f
        for f in findings
        if f.severity in ("error", "warning") and f.check not in ("tmux_available",)
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
# Stuck worker checks (#239)
# ---------------------------------------------------------------------------


def test_stuck_worker_detected(setup):
    """Fresh heartbeat + stale current_action_at emits a stuck_worker warning."""
    backend, config = setup
    backend.register_worker(_make_worker("worker-stuck"))

    # Simulate an activity set in the past by writing directly into the worker file.
    worker_file = Path(config["data_dir"]) / "workers" / "worker-stuck.json"
    data = json.loads(worker_file.read_text())
    stale_ts = datetime.now(UTC).timestamp() - 600
    data["current_action"] = "Running: Bash"
    data["current_action_at"] = datetime.fromtimestamp(stale_ts, tz=UTC).isoformat()
    worker_file.write_text(json.dumps(data))
    # Keep heartbeat fresh: touch mtime to now so check_stale_workers doesn't fire.
    now = time.time()
    os.utime(str(worker_file), (now, now))

    findings = run_doctor(backend, config, fix=False)
    stuck = [f for f in findings if f.check == "stuck_worker"]
    assert len(stuck) == 1
    assert "worker-stuck" in stuck[0].message
    assert "Running: Bash" in stuck[0].message
    assert stuck[0].severity == "warning"
    assert stuck[0].auto_fixable is False
    assert stuck[0].fixed is False


def test_stuck_worker_not_detected_when_recent(setup):
    """Action set recently does not trigger the stuck_worker check."""
    backend, config = setup
    backend.register_worker(_make_worker("worker-ok"))
    backend.update_worker_activity("worker-ok", "Running: Bash")

    findings = run_doctor(backend, config, fix=False)
    stuck = [f for f in findings if f.check == "stuck_worker"]
    assert stuck == []


def test_stuck_worker_not_detected_without_action(setup):
    """Worker without current_action is never stuck."""
    backend, config = setup
    backend.register_worker(_make_worker("worker-idle"))

    findings = run_doctor(backend, config, fix=False)
    stuck = [f for f in findings if f.check == "stuck_worker"]
    assert stuck == []


def test_stuck_worker_not_double_reported_when_heartbeat_also_stale(setup):
    """A worker with stale heartbeat is only reported as stale, not also stuck."""
    backend, config = setup
    backend.register_worker(_make_worker("worker-dead"))

    worker_file = Path(config["data_dir"]) / "workers" / "worker-dead.json"
    data = json.loads(worker_file.read_text())
    data["current_action"] = "Running: Bash"
    data["current_action_at"] = datetime.fromtimestamp(time.time() - 600, tz=UTC).isoformat()
    worker_file.write_text(json.dumps(data))
    # Backdate heartbeat mtime — stale worker
    _backdate(worker_file, seconds=1200)

    findings = run_doctor(backend, config, fix=False)
    stale = [f for f in findings if f.check == "stale_worker"]
    stuck = [f for f in findings if f.check == "stuck_worker"]
    assert len(stale) == 1
    assert stuck == []


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
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    (repo / "file.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True)

    # Create a bare remote so worktree has an upstream
    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(bare)],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=str(repo),
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "master"],
        cwd=str(repo),
        capture_output=True,
        check=False,
    )

    # Create a worktree with upstream tracking
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir(parents=True)
    wt_path = ws_root / "task-orphan-att-001"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feat/orphan", str(wt_path)],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    # Push the branch so it has an upstream
    subprocess.run(
        ["git", "push", "-u", "origin", "feat/orphan"],
        cwd=str(wt_path),
        capture_output=True,
        check=True,
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
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    (repo / "file.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True)

    # Create a worktree (no remote/upstream)
    wt_path = tmp_path / "workspaces" / "task-orphan-att-001"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feat/orphan", str(wt_path)],
        cwd=str(repo),
        capture_output=True,
        check=True,
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
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    (repo / "file.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True)

    wt_path = tmp_path / "workspaces" / "task-dirty"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feat/dirty", str(wt_path)],
        cwd=str(repo),
        capture_output=True,
        check=True,
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
    """Own-hash session without metadata emits warning finding; one WITH metadata does not."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import check_orphan_tmux_sessions
    from antfarm.core.process_manager import colony_session_hash

    data_dir = tmp_path / ".antfarm"
    processes_dir = data_dir / "processes"
    processes_dir.mkdir(parents=True)

    h = colony_session_hash(str(data_dir))
    known = f"auto-{h}-builder-1"
    orphan = f"auto-{h}-planner-2"

    # Write metadata for the non-orphan session
    (processes_dir / f"{known}.json").write_text(
        f'{{"name": "{known}", "role": "builder", "manager_type": "tmux"}}'
    )

    config = {"data_dir": str(data_dir)}

    # Two own-hash sessions: one with metadata, one orphan; plus one non-antfarm session
    session_names = f"{known}\n{orphan}\nsome-unrelated-session\n"

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
    assert orphan in orphan_checks[0].message
    assert orphan_checks[0].severity == "warning"
    assert orphan_checks[0].auto_fixable is True


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


# ---------------------------------------------------------------------------
# Regression: peer-colony tmux sessions must not trip the Soldier merge gate
# ---------------------------------------------------------------------------


def test_run_doctor_on_colony_with_peer_auto_tmux_sessions_produces_no_warnings(setup, monkeypatch):
    """Peer-colony tmux sessions must be ignored entirely (different hash prefix).

    Scenario: another antfarm colony (different ``data_dir``, same host) owns
    live ``auto-*`` tmux sessions. Their session names carry that peer's
    colony hash (derived from its ``data_dir`` realpath), which will not
    match THIS colony's hash. ``check_orphan_tmux_sessions`` must therefore
    produce zero findings for peer sessions — not even ``info``.
    """
    import subprocess as real_subprocess
    from unittest.mock import MagicMock

    import antfarm.core.doctor as doctor_mod

    backend, config = setup

    # Simulate peer-colony sessions: `auto-{foreign_hash}-...`. Any hex digest
    # other than this colony's own hash exercises the ownership filter.
    tmux_result = MagicMock()
    tmux_result.returncode = 0
    tmux_result.stdout = "auto-ffffffff-reviewer-99\nauto-deadbeef-builder-42\n"

    real_run = real_subprocess.run

    def fake_run(cmd, *args, **kwargs):
        # Only intercept the tmux list-sessions call — delegate everything else
        # (e.g., git rev-parse for git_config) to the real subprocess.run so
        # unrelated checks behave normally.
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "tmux":
            return tmux_result
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(doctor_mod.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

    findings = run_doctor(backend, config, fix=False)

    orphan_findings = [f for f in findings if f.check == "orphan_tmux_session"]
    assert orphan_findings == [], (
        f"peer-colony sessions (foreign hash) must be ignored, got: {orphan_findings}"
    )

    # The real regression guard: Soldier's gate asserts no errors/warnings.
    blocking = [f for f in findings if f.severity in ("error", "warning")]
    assert blocking == [], (
        f"peer-colony tmux sessions must not produce error/warning findings: {blocking}"
    )


# ---------------------------------------------------------------------------
# Colony-hashed orphan scoping (#231)
# ---------------------------------------------------------------------------


def test_orphan_own_prefix_detected_as_warning(tmp_path):
    """Own-hash session without metadata is flagged warning + auto_fixable."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import check_orphan_tmux_sessions
    from antfarm.core.process_manager import colony_session_hash

    data_dir = tmp_path / ".antfarm"
    (data_dir / "processes").mkdir(parents=True)

    h = colony_session_hash(str(data_dir))
    orphan = f"runner-{h}-builder-7"

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = f"{orphan}\n"

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", return_value=mock_result),
    ):
        findings = check_orphan_tmux_sessions({"data_dir": str(data_dir)}, fix=False)

    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert findings[0].auto_fixable is True
    assert findings[0].fixed is False
    assert orphan in findings[0].message


def test_orphan_peer_prefix_ignored(tmp_path):
    """Foreign-hash session produces zero findings — not even info."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import check_orphan_tmux_sessions

    data_dir = tmp_path / ".antfarm"
    (data_dir / "processes").mkdir(parents=True)

    mock_result = MagicMock()
    mock_result.returncode = 0
    # Use 'ffffffff' which will not collide with the real hash of data_dir.
    mock_result.stdout = "auto-ffffffff-reviewer-99\nrunner-deadbeef-planner-3\n"

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", return_value=mock_result),
    ):
        findings = check_orphan_tmux_sessions({"data_dir": str(data_dir)}, fix=False)

    assert findings == []


def test_orphan_fix_kills_session(tmp_path):
    """With fix=True, tmux kill-session is invoked for own orphans only."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import check_orphan_tmux_sessions
    from antfarm.core.process_manager import colony_session_hash

    data_dir = tmp_path / ".antfarm"
    (data_dir / "processes").mkdir(parents=True)

    h = colony_session_hash(str(data_dir))
    own_orphan = f"auto-{h}-builder-3"
    peer = "auto-ffffffff-builder-3"

    list_result = MagicMock()
    list_result.returncode = 0
    list_result.stdout = f"{own_orphan}\n{peer}\n"

    kill_result = MagicMock()
    kill_result.returncode = 0

    calls: list = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["tmux", "list-sessions"]:
            return list_result
        if cmd[:2] == ["tmux", "kill-session"]:
            return kill_result
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", side_effect=fake_run),
    ):
        findings = check_orphan_tmux_sessions({"data_dir": str(data_dir)}, fix=True)

    # Exactly one finding (our orphan); peer session is ignored entirely.
    assert len(findings) == 1
    assert findings[0].fixed is True

    # Exactly one kill invocation, targeting our orphan (NOT the peer).
    kill_calls = [c for c in calls if c[:2] == ["tmux", "kill-session"]]
    assert kill_calls == [["tmux", "kill-session", "-t", own_orphan]]


def _run_orphan_fix_with_kill_result(tmp_path, kill_returncode: int, kill_stderr: str):
    """Shared helper: run check_orphan_tmux_sessions(fix=True) with a canned kill result."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import check_orphan_tmux_sessions
    from antfarm.core.process_manager import colony_session_hash

    data_dir = tmp_path / ".antfarm"
    (data_dir / "processes").mkdir(parents=True)

    h = colony_session_hash(str(data_dir))
    own_orphan = f"auto-{h}-builder-3"

    list_result = MagicMock()
    list_result.returncode = 0
    list_result.stdout = f"{own_orphan}\n"

    kill_result = MagicMock()
    kill_result.returncode = kill_returncode
    kill_result.stderr = kill_stderr

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["tmux", "list-sessions"]:
            return list_result
        if cmd[:2] == ["tmux", "kill-session"]:
            return kill_result
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", side_effect=fake_run),
    ):
        findings = check_orphan_tmux_sessions({"data_dir": str(data_dir)}, fix=True)

    return findings, own_orphan


def test_orphan_fix_race_session_gone_marks_fixed(tmp_path):
    """kill-session racing with tmux auto-cleanup ('can't find session') counts as fixed."""
    findings, _ = _run_orphan_fix_with_kill_result(
        tmp_path,
        kill_returncode=1,
        kill_stderr="can't find session: auto-abcdef-builder-3\n",
    )
    assert len(findings) == 1
    assert findings[0].fixed is True
    assert "already gone" in findings[0].message


def test_orphan_fix_race_no_server_marks_fixed(tmp_path):
    """kill-session when tmux server has exited ('no server running') counts as fixed."""
    findings, _ = _run_orphan_fix_with_kill_result(
        tmp_path,
        kill_returncode=1,
        kill_stderr="no server running on /tmp/tmux-501/default\n",
    )
    assert len(findings) == 1
    assert findings[0].fixed is True
    assert "already gone" in findings[0].message


def test_orphan_fix_genuine_failure_surfaces_stderr(tmp_path):
    """Genuine kill failure surfaces stderr's first line and leaves fixed=False."""
    findings, _ = _run_orphan_fix_with_kill_result(
        tmp_path,
        kill_returncode=1,
        kill_stderr="permission denied\nother line\n",
    )
    assert len(findings) == 1
    assert findings[0].fixed is False
    assert "kill failed" in findings[0].message
    assert "permission denied" in findings[0].message
    # Only first line surfaced.
    assert "other line" not in findings[0].message


def test_orphan_fix_empty_stderr_nonzero_returncode(tmp_path):
    """Nonzero exit with empty stderr surfaces returncode instead."""
    findings, _ = _run_orphan_fix_with_kill_result(
        tmp_path,
        kill_returncode=2,
        kill_stderr="",
    )
    assert len(findings) == 1
    assert findings[0].fixed is False
    assert "kill failed" in findings[0].message
    assert "returncode=2" in findings[0].message


def test_orphan_fix_success_regression(tmp_path):
    """Regression guard: successful kill still marks fixed and does not annotate message."""
    findings, own_orphan = _run_orphan_fix_with_kill_result(
        tmp_path,
        kill_returncode=0,
        kill_stderr="",
    )
    assert len(findings) == 1
    assert findings[0].fixed is True
    # Message must match the original format exactly — no "already gone" / "kill failed" suffixes.
    assert findings[0].message == f"orphan tmux session: {own_orphan} (no matching metadata)"


def test_check_orphan_tmux_sessions_fix_mixed_batch(tmp_path):
    """Three orphans: clean kill, benign race, genuine failure — all classified correctly."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import check_orphan_tmux_sessions
    from antfarm.core.process_manager import colony_session_hash

    data_dir = tmp_path / ".antfarm"
    (data_dir / "processes").mkdir(parents=True)

    h = colony_session_hash(str(data_dir))
    session_clean = f"auto-{h}-builder-1"
    session_race = f"auto-{h}-builder-2"
    session_fail = f"auto-{h}-builder-3"

    list_result = MagicMock()
    list_result.returncode = 0
    list_result.stdout = f"{session_clean}\n{session_race}\n{session_fail}\n"

    kill_clean = MagicMock()
    kill_clean.returncode = 0
    kill_clean.stderr = ""

    kill_race = MagicMock()
    kill_race.returncode = 1
    kill_race.stderr = "can't find session: " + session_race + "\n"

    kill_fail = MagicMock()
    kill_fail.returncode = 1
    kill_fail.stderr = "some other error\n"

    kill_calls: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["tmux", "list-sessions"]:
            return list_result
        if cmd[:2] == ["tmux", "kill-session"]:
            target = cmd[cmd.index("-t") + 1]
            kill_calls.append(target)
            if target == session_clean:
                return kill_clean
            if target == session_race:
                return kill_race
            if target == session_fail:
                return kill_fail
            raise AssertionError(f"unexpected kill target: {target}")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", side_effect=fake_run),
    ):
        findings = check_orphan_tmux_sessions({"data_dir": str(data_dir)}, fix=True)

    assert len(findings) == 3

    by_name = {f.message.split(":")[1].split("(")[0].strip(): f for f in findings}

    f_clean = by_name[session_clean]
    assert f_clean.fixed is True
    assert "already gone" not in f_clean.message
    assert "kill failed" not in f_clean.message

    f_race = by_name[session_race]
    assert f_race.fixed is True
    assert "already gone" in f_race.message

    f_fail = by_name[session_fail]
    assert f_fail.fixed is False
    assert "kill failed" in f_fail.message


def test_two_mock_colonies_dont_cross_see(tmp_path):
    """Two colonies with distinct data_dirs each see only their own orphans."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import check_orphan_tmux_sessions
    from antfarm.core.process_manager import colony_session_hash

    dir_a = tmp_path / "colony-a"
    dir_b = tmp_path / "colony-b"
    (dir_a / "processes").mkdir(parents=True)
    (dir_b / "processes").mkdir(parents=True)

    h_a = colony_session_hash(str(dir_a))
    h_b = colony_session_hash(str(dir_b))
    assert h_a != h_b

    name_a = f"auto-{h_a}-builder-1"
    name_b = f"auto-{h_b}-reviewer-2"

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = f"{name_a}\n{name_b}\nnot-antfarm\n"

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", return_value=mock_result),
    ):
        findings_a = check_orphan_tmux_sessions({"data_dir": str(dir_a)}, fix=False)
        findings_b = check_orphan_tmux_sessions({"data_dir": str(dir_b)}, fix=False)

    assert len(findings_a) == 1 and name_a in findings_a[0].message
    assert name_b not in findings_a[0].message

    assert len(findings_b) == 1 and name_b in findings_b[0].message
    assert name_a not in findings_b[0].message


# ---------------------------------------------------------------------------
# Legacy tmux sweep (#237)
# ---------------------------------------------------------------------------


def test_legacy_regex_matches_auto_role_n():
    """Pre-#231 ``auto-<role>-<N>`` sessions match, including multi-word roles."""
    from antfarm.core.doctor import LEGACY_TMUX_RE

    assert LEGACY_TMUX_RE.match("auto-builder-3")
    assert LEGACY_TMUX_RE.match("auto-code-reviewer-12")
    assert LEGACY_TMUX_RE.match("auto-planner-1")


def test_legacy_regex_matches_runner_role_n():
    """Pre-#231 ``runner-<role>-<N>`` sessions match, including multi-word roles."""
    from antfarm.core.doctor import LEGACY_TMUX_RE

    assert LEGACY_TMUX_RE.match("runner-planner-1")
    assert LEGACY_TMUX_RE.match("runner-code-reviewer-7")


def test_legacy_regex_matches_antfarm_deploy():
    """Pre-#235 deploy sessions (``antfarm-<node>-<agent>-<idx>``) match."""
    from antfarm.core.doctor import LEGACY_TMUX_RE

    assert LEGACY_TMUX_RE.match("antfarm-node1-claude-0")
    assert LEGACY_TMUX_RE.match("antfarm-node-2-codex-11")


def test_legacy_regex_rejects_hashed_names():
    """New-format hash-prefixed names for all three prefixes do NOT match."""
    from antfarm.core.doctor import LEGACY_TMUX_RE

    assert not LEGACY_TMUX_RE.match("auto-a1b2c3d4-builder-3")
    assert not LEGACY_TMUX_RE.match("runner-deadbeef-planner-1")
    assert not LEGACY_TMUX_RE.match("antfarm-ffffffff-node1-claude-0")
    # Also don't trip on unrelated session names
    assert not LEGACY_TMUX_RE.match("some-unrelated-session")
    assert not LEGACY_TMUX_RE.match("auto-builder")  # missing -N
    assert not LEGACY_TMUX_RE.match("runner-3")  # missing role


def test_sweep_legacy_tmux_dry_run_reports_only():
    """confirmed=False emits info findings only; no kill-session invoked."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import sweep_legacy_tmux_sessions

    list_result = MagicMock()
    list_result.returncode = 0
    list_result.stdout = "auto-builder-3\nrunner-planner-1\nantfarm-a1b2c3d4-node-x-0\n"

    calls: list = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return list_result

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", side_effect=fake_run),
    ):
        findings = sweep_legacy_tmux_sessions({"data_dir": "/x"}, confirmed=False)

    assert len(findings) == 2
    names = sorted(f.message for f in findings)
    assert any("auto-builder-3" in n for n in names)
    assert any("runner-planner-1" in n for n in names)
    for f in findings:
        assert f.severity == "info"
        assert f.check == "legacy_tmux_session"
        assert f.auto_fixable is True
        assert f.fixed is False
    # Only list-sessions was called — no kill-session
    assert all(c[:2] == ["tmux", "list-sessions"] for c in calls)


def test_sweep_legacy_tmux_confirmed_kills():
    """confirmed=True runs tmux kill-session per match and marks fixed=True."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import sweep_legacy_tmux_sessions

    list_result = MagicMock()
    list_result.returncode = 0
    list_result.stdout = "auto-builder-3\nrunner-planner-1\n"

    kill_result = MagicMock()
    kill_result.returncode = 0
    kill_result.stderr = ""

    calls: list = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["tmux", "list-sessions"]:
            return list_result
        return kill_result

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", side_effect=fake_run),
    ):
        findings = sweep_legacy_tmux_sessions({"data_dir": "/x"}, confirmed=True)

    assert len(findings) == 2
    assert all(f.fixed for f in findings)
    kill_cmds = [c for c in calls if c[:2] == ["tmux", "kill-session"]]
    targets = sorted(c[3] for c in kill_cmds)
    assert targets == ["auto-builder-3", "runner-planner-1"]


def test_sweep_legacy_tmux_handles_already_gone():
    """Benign-race stderr from kill-session → fixed=True with "(already gone)"."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import sweep_legacy_tmux_sessions

    list_result = MagicMock()
    list_result.returncode = 0
    list_result.stdout = "auto-builder-3\n"

    kill_result = MagicMock()
    kill_result.returncode = 1
    kill_result.stderr = "can't find session: auto-builder-3"

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["tmux", "list-sessions"]:
            return list_result
        return kill_result

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", side_effect=fake_run),
    ):
        findings = sweep_legacy_tmux_sessions({"data_dir": "/x"}, confirmed=True)

    assert len(findings) == 1
    assert findings[0].fixed is True
    assert "(already gone)" in findings[0].message


def test_sweep_legacy_tmux_handles_kill_failure():
    """Real kill-session failure → fixed=False with "kill failed:" detail."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import sweep_legacy_tmux_sessions

    list_result = MagicMock()
    list_result.returncode = 0
    list_result.stdout = "runner-planner-1\n"

    kill_result = MagicMock()
    kill_result.returncode = 2
    kill_result.stderr = "permission denied: something real and bad"

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["tmux", "list-sessions"]:
            return list_result
        return kill_result

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", side_effect=fake_run),
    ):
        findings = sweep_legacy_tmux_sessions({"data_dir": "/x"}, confirmed=True)

    assert len(findings) == 1
    assert findings[0].fixed is False
    assert "kill failed:" in findings[0].message
    assert "permission denied" in findings[0].message


def test_sweep_legacy_tmux_no_tmux_returns_empty():
    """tmux binary missing → sweep returns [] without raising."""
    from unittest.mock import patch

    from antfarm.core.doctor import sweep_legacy_tmux_sessions

    with patch("antfarm.core.doctor.shutil.which", return_value=None):
        assert sweep_legacy_tmux_sessions({"data_dir": "/x"}, confirmed=False) == []
        assert sweep_legacy_tmux_sessions({"data_dir": "/x"}, confirmed=True) == []


def test_sweep_legacy_tmux_no_server_returns_empty():
    """tmux server not running (list-sessions returncode != 0) → []."""
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import sweep_legacy_tmux_sessions

    list_result = MagicMock()
    list_result.returncode = 1
    list_result.stdout = ""

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", return_value=list_result),
    ):
        assert sweep_legacy_tmux_sessions({"data_dir": "/x"}, confirmed=True) == []


def test_run_doctor_sweep_legacy_tmux_flag_invokes_sweep():
    """run_doctor(sweep_legacy_tmux=True) appends legacy_tmux_session findings."""
    from unittest.mock import MagicMock, patch

    import antfarm.core.doctor as doctor_mod

    list_result = MagicMock()
    list_result.returncode = 0
    list_result.stdout = "auto-builder-3\n"

    kill_result = MagicMock()
    kill_result.returncode = 0
    kill_result.stderr = ""

    import subprocess as real_subprocess

    real_run = real_subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "tmux":
            if cmd[:2] == ["tmux", "list-sessions"]:
                return list_result
            return kill_result
        return real_run(cmd, *args, **kwargs)

    backend = MagicMock()
    backend.list_tasks.return_value = []
    backend.list_nodes.return_value = []

    config = {"data_dir": "/tmp/antfarm-test-doctor-sweep"}
    os.makedirs(config["data_dir"], exist_ok=True)

    with (
        patch.object(doctor_mod.shutil, "which", return_value="/usr/bin/tmux"),
        patch.object(doctor_mod.subprocess, "run", side_effect=fake_run),
    ):
        findings = doctor_mod.run_doctor(backend, config, fix=False, sweep_legacy_tmux=True)

    legacy = [f for f in findings if f.check == "legacy_tmux_session"]
    assert len(legacy) == 1
    assert legacy[0].fixed is True


# ---------------------------------------------------------------------------
# Defensive subprocess hardening (issue #211)
# ---------------------------------------------------------------------------


def test_check_orphan_tmux_sessions_returns_empty_on_subprocess_timeout():
    """tmux list-sessions hanging (TimeoutExpired) → [] rather than crashing."""
    import subprocess
    from unittest.mock import patch

    from antfarm.core.doctor import check_orphan_tmux_sessions

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch(
            "antfarm.core.doctor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["tmux"], timeout=5),
        ),
    ):
        assert check_orphan_tmux_sessions({"data_dir": "/x"}) == []


def test_check_orphan_tmux_sessions_returns_empty_on_oserror():
    """tmux binary unavailable (OSError from subprocess) → [] rather than crashing."""
    from unittest.mock import patch

    from antfarm.core.doctor import check_orphan_tmux_sessions

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", side_effect=OSError("boom")),
    ):
        assert check_orphan_tmux_sessions({"data_dir": "/x"}) == []


def test_sweep_legacy_returns_empty_on_subprocess_error():
    """sweep_legacy_tmux_sessions: SubprocessError from list-sessions → [] (no crash)."""
    import subprocess
    from unittest.mock import patch

    from antfarm.core.doctor import sweep_legacy_tmux_sessions

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch(
            "antfarm.core.doctor.subprocess.run",
            side_effect=subprocess.SubprocessError("boom"),
        ),
    ):
        assert sweep_legacy_tmux_sessions({"data_dir": "/x"}, confirmed=False) == []
        assert sweep_legacy_tmux_sessions({"data_dir": "/x"}, confirmed=True) == []


def test_check_orphan_kill_timeout_does_not_mark_fixed(tmp_path):
    """Orphan detected, but tmux kill-session hangs → finding.fixed=False with timeout note."""
    import subprocess
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import check_orphan_tmux_sessions
    from antfarm.core.process_manager import colony_session_hash

    data_dir = tmp_path / ".antfarm"
    (data_dir / "processes").mkdir(parents=True)

    h = colony_session_hash(str(data_dir))
    own_orphan = f"auto-{h}-builder-7"

    list_result = MagicMock()
    list_result.returncode = 0
    list_result.stdout = f"{own_orphan}\n"

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["tmux", "list-sessions"]:
            return list_result
        if cmd[:2] == ["tmux", "kill-session"]:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", side_effect=fake_run),
    ):
        findings = check_orphan_tmux_sessions({"data_dir": str(data_dir)}, fix=True)

    assert len(findings) == 1
    assert findings[0].fixed is False
    assert "kill timed out" in findings[0].message


def test_sweep_legacy_matches_user_sessions_by_design():
    """Legacy pattern matches user sessions like ``auto-save-5`` by design.

    Interactive confirmation in the CLI is the safety net — the pattern
    cannot distinguish antfarm's legacy sessions from unrelated user-owned
    sessions that happen to share the shape. See
    :func:`antfarm.core.doctor.sweep_legacy_tmux_sessions` for the documented
    false-positive risk.
    """
    from unittest.mock import MagicMock, patch

    from antfarm.core.doctor import sweep_legacy_tmux_sessions

    list_result = MagicMock()
    list_result.returncode = 0
    list_result.stdout = "auto-save-5\n"

    with (
        patch("antfarm.core.doctor.shutil.which", return_value="/usr/bin/tmux"),
        patch("antfarm.core.doctor.subprocess.run", return_value=list_result),
    ):
        findings = sweep_legacy_tmux_sessions({"data_dir": "/x"}, confirmed=False)

    assert len(findings) == 1
    assert findings[0].check == "legacy_tmux_session"
    assert "auto-save-5" in findings[0].message


# ---------------------------------------------------------------------------
# Doctor activity-feed events (#191)
#
# Doctor emits SSE events to serve._event_queue when fix=True actually
# applies a repair:
#   stale_worker_recovered, stale_task_recovered, stale_guard_cleared
# all with actor="doctor". Dry-run (fix=False) must not emit.
# ---------------------------------------------------------------------------


@pytest.fixture
def clear_events():
    """Clear the SSE event queue before each event-assertion test."""
    from antfarm.core import serve

    serve._event_queue.clear()
    yield serve._event_queue


def _find_event(queue, event_type: str) -> dict | None:
    for e in queue:
        if e["type"] == event_type:
            return e
    return None


def test_doctor_emits_stale_worker_recovered_on_fix(setup, clear_events):
    backend, config = setup
    backend.register_worker(_make_worker("worker-dead"))

    data_dir = Path(config["data_dir"])
    worker_file = data_dir / "workers" / "worker-dead.json"
    _backdate(worker_file, seconds=600)

    run_doctor(backend, config, fix=True)

    ev = _find_event(clear_events, "stale_worker_recovered")
    assert ev is not None
    assert ev["actor"] == "doctor"
    assert ev["task_id"] == ""
    assert "worker-dead" in ev["detail"]


def test_doctor_dry_run_does_not_emit_stale_worker_event(setup, clear_events):
    backend, config = setup
    backend.register_worker(_make_worker("worker-dead"))

    data_dir = Path(config["data_dir"])
    worker_file = data_dir / "workers" / "worker-dead.json"
    _backdate(worker_file, seconds=600)

    run_doctor(backend, config, fix=False)

    assert _find_event(clear_events, "stale_worker_recovered") is None


def test_doctor_emits_stale_task_recovered_on_fix(setup, clear_events):
    backend, config = setup
    backend.register_worker(_make_worker("worker-dead"))
    backend.carry(_make_task("task-stale"))
    backend.pull("worker-dead")
    backend.deregister_worker("worker-dead")

    run_doctor(backend, config, fix=True)

    ev = _find_event(clear_events, "stale_task_recovered")
    assert ev is not None
    assert ev["actor"] == "doctor"
    assert ev["task_id"] == "task-stale"


def test_doctor_dry_run_does_not_emit_stale_task_event(setup, clear_events):
    backend, config = setup
    backend.register_worker(_make_worker("worker-dead"))
    backend.carry(_make_task("task-stale"))
    backend.pull("worker-dead")
    backend.deregister_worker("worker-dead")

    run_doctor(backend, config, fix=False)

    assert _find_event(clear_events, "stale_task_recovered") is None


def test_doctor_emits_stale_guard_cleared_on_fix(setup, clear_events):
    backend, config = setup
    backend.guard("resource/lock", "worker-gone")

    data_dir = Path(config["data_dir"])
    guard_file = data_dir / "guards" / "resource__lock.lock"
    _backdate(guard_file, seconds=600)

    run_doctor(backend, config, fix=True)

    ev = _find_event(clear_events, "stale_guard_cleared")
    assert ev is not None
    assert ev["actor"] == "doctor"
    assert ev["task_id"] == ""
    assert "resource/lock" in ev["detail"]


def test_doctor_dry_run_does_not_emit_stale_guard_event(setup, clear_events):
    backend, config = setup
    backend.guard("resource/lock", "worker-gone")

    data_dir = Path(config["data_dir"])
    guard_file = data_dir / "guards" / "resource__lock.lock"
    _backdate(guard_file, seconds=600)

    run_doctor(backend, config, fix=False)

    assert _find_event(clear_events, "stale_guard_cleared") is None


# ---------------------------------------------------------------------------
# check_no_reviewer_capacity tests
# ---------------------------------------------------------------------------


def _make_review_task(task_id: str = "review-1") -> dict:
    """Make a task that requires the 'review' capability."""
    now = datetime.now(UTC).isoformat()
    return {
        "id": task_id,
        "title": f"Review {task_id}",
        "spec": "Review this PR",
        "complexity": "S",
        "priority": 5,
        "depends_on": [],
        "touches": [],
        "capabilities_required": ["review"],
        "created_at": now,
        "updated_at": now,
        "created_by": "test",
    }


def _make_reviewer_worker(worker_id: str = "reviewer-1") -> dict:
    """Make a worker dict with the 'review' capability."""
    now = datetime.now(UTC).isoformat()
    return {
        "worker_id": worker_id,
        "node_id": "node-1",
        "agent_type": "reviewer",
        "workspace_root": "/tmp/ws-review",
        "capabilities": ["review"],
        "status": "idle",
        "registered_at": now,
        "last_heartbeat": now,
    }


def test_check_no_reviewer_capacity_fires(setup):
    """Ready review task + zero reviewer workers → one warning finding."""
    backend, config = setup
    backend.carry(_make_review_task("review-001"))

    findings = run_doctor(backend, config)

    capacity_findings = [f for f in findings if f.check == "no_reviewer_capacity"]
    assert len(capacity_findings) == 1
    f = capacity_findings[0]
    assert f.severity == "warning"
    assert f.auto_fixable is False
    assert "review" in f.message.lower()


def test_check_no_reviewer_capacity_silent_when_reviewer_present(setup):
    """Ready review task + a registered reviewer worker → no capacity finding."""
    backend, config = setup
    backend.carry(_make_review_task("review-001"))
    backend.register_worker(_make_reviewer_worker("local/reviewer-1"))

    findings = run_doctor(backend, config)

    capacity_findings = [f for f in findings if f.check == "no_reviewer_capacity"]
    assert len(capacity_findings) == 0


def test_check_no_reviewer_capacity_silent_when_no_ready_review_tasks(setup):
    """No ready review tasks → no capacity finding, even without reviewer workers."""
    backend, config = setup
    # Regular task (no capabilities_required) and no workers
    backend.carry(_make_task("task-regular"))

    findings = run_doctor(backend, config)

    capacity_findings = [f for f in findings if f.check == "no_reviewer_capacity"]
    assert len(capacity_findings) == 0


# ---------------------------------------------------------------------------
# Issue #310: doctor --fix must re-check state atomically before mutating.
# Simulate a late heartbeat arriving between the doctor's stale detection and
# the backend-side mutation by monkeypatching each *_if_* helper.
# ---------------------------------------------------------------------------


def test_check_stale_workers_fix_respects_late_heartbeat(setup, monkeypatch):
    """Late heartbeat → backend refuses to deregister → finding reports unfixed."""
    backend, config = setup
    backend.register_worker(_make_worker("worker-late"))
    _backdate(Path(config["data_dir"]) / "workers" / "worker-late.json", seconds=600)

    real = backend.deregister_worker_if_stale

    def wrapped(wid: str, max_age: float) -> bool:
        # Simulate a concurrent heartbeat landing just before the backend
        # re-checks staleness under its lock.
        backend.heartbeat(wid, {})
        return real(wid, max_age)

    monkeypatch.setattr(backend, "deregister_worker_if_stale", wrapped)

    findings = run_doctor(backend, config, fix=True)
    stale = [f for f in findings if f.check == "stale_worker"]
    assert len(stale) == 1
    assert stale[0].fixed is False
    assert "recovered" in stale[0].message
    # Worker file must still exist — the late heartbeat kept it alive.
    assert (Path(config["data_dir"]) / "workers" / "worker-late.json").exists()


def test_check_stale_tasks_fix_respects_late_heartbeat(setup, monkeypatch):
    """Late worker re-registration → backend refuses recovery → finding reports unfixed."""
    backend, config = setup
    backend.register_worker(_make_worker("worker-late"))
    backend.carry(_make_task("task-racey"))
    backend.pull("worker-late")

    # Kill the worker so the doctor detects the stale task.
    backend.deregister_worker("worker-late")

    real = backend.recover_stale_task_if_worker_dead

    def wrapped(task_id: str, attempt_id: str, max_attempts: int = 3) -> bool:
        # Simulate the worker reviving between detection and mutation.
        backend.register_worker(_make_worker("worker-late"))
        return real(task_id, attempt_id, max_attempts=max_attempts)

    monkeypatch.setattr(backend, "recover_stale_task_if_worker_dead", wrapped)

    findings = run_doctor(backend, config, fix=True)
    stale = [f for f in findings if f.check == "stale_task"]
    assert len(stale) == 1
    assert stale[0].fixed is False
    assert "no action taken" in stale[0].message
    # Task must still be in active/ since recovery was refused.
    assert (Path(config["data_dir"]) / "tasks" / "active" / "task-racey.json").exists()
    assert not (Path(config["data_dir"]) / "tasks" / "ready" / "task-racey.json").exists()


def test_check_stale_guards_fix_respects_owner_revival(setup, monkeypatch):
    """Owner revives between detection and release → backend refuses → finding unfixed."""
    backend, config = setup
    backend.guard("resource/lock", "owner-gone")
    guard_file = Path(config["data_dir"]) / "guards" / "resource__lock.lock"
    _backdate(guard_file, seconds=600)

    real = backend.release_guard_if_owner_dead

    def wrapped(resource: str) -> bool:
        # Simulate owner coming back online right as doctor --fix acts.
        backend.register_worker(_make_worker("owner-gone"))
        return real(resource)

    monkeypatch.setattr(backend, "release_guard_if_owner_dead", wrapped)

    findings = run_doctor(backend, config, fix=True)
    stale = [f for f in findings if f.check == "stale_guard"]
    assert len(stale) == 1
    assert stale[0].fixed is False
    assert "recovered" in stale[0].message
    assert guard_file.exists()


def test_check_stale_tasks_fix_respects_max_attempts(setup):
    """After max_attempts stale recoveries, doctor --fix routes task to blocked/ (issue #333).

    This is the integration-level proof that doctor passes max_attempts through
    to recover_stale_task_if_worker_dead so a flapping worker cannot bypass the
    blocked/ routing that kickback() enforces.
    """
    backend, config = setup
    config["max_attempts"] = 2  # smaller budget to keep the test compact

    backend.carry(_make_task("task-runaway"))

    # Cycle 1: worker claims, dies, doctor --fix recovers → ready/
    backend.register_worker(_make_worker("w1"))
    backend.pull("w1")
    backend.deregister_worker("w1")
    run_doctor(backend, config, fix=True)
    assert (Path(config["data_dir"]) / "tasks" / "ready" / "task-runaway.json").exists()

    # Cycle 2: same pattern. finished attempts now = 2, which hits max_attempts=2.
    backend.register_worker(_make_worker("w2"))
    backend.pull("w2")
    backend.deregister_worker("w2")
    findings = run_doctor(backend, config, fix=True)

    stale = [f for f in findings if f.check == "stale_task"]
    assert len(stale) == 1
    assert stale[0].fixed is True

    blocked_path = Path(config["data_dir"]) / "tasks" / "blocked" / "task-runaway.json"
    ready_path = Path(config["data_dir"]) / "tasks" / "ready" / "task-runaway.json"
    assert blocked_path.exists()
    assert not ready_path.exists()

    data = json.loads(blocked_path.read_text())
    assert data["status"] == "blocked"
    assert any("moved to blocked" in t["message"] for t in data.get("trail", []))


# ---------------------------------------------------------------------------
# Retry-pattern checks (#325)
# ---------------------------------------------------------------------------


def _write_task_file(
    data_dir: Path,
    folder: str,
    task_id: str,
    *,
    status: str,
    attempts: list[dict] | None = None,
    trail: list[dict] | None = None,
    title: str | None = None,
    max_attempts: int | None = None,
) -> Path:
    """Place a task JSON file directly into {data_dir}/tasks/{folder}/."""
    task = _make_task(task_id)
    task["status"] = status
    task["current_attempt"] = None
    task["attempts"] = attempts or []
    task["trail"] = trail or []
    task["signals"] = []
    if title is not None:
        task["title"] = title
    if max_attempts is not None:
        task["max_attempts"] = max_attempts
    path = data_dir / "tasks" / folder / f"{task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(task))
    return path


def _att(attempt_id: str, status: str) -> dict:
    return {
        "attempt_id": attempt_id,
        "worker_id": "worker-x",
        "status": status,
        "branch": None,
        "pr": None,
        "started_at": datetime.now(UTC).isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
    }


def test_retry_patterns_flags_retry_ceiling(setup):
    """3 superseded attempts + status=blocked → one retry_ceiling error."""
    backend, config = setup
    data_dir = Path(config["data_dir"])
    _write_task_file(
        data_dir,
        "blocked",
        "task-dead",
        status="blocked",
        attempts=[
            _att("att-001", "superseded"),
            _att("att-002", "superseded"),
            _att("att-003", "superseded"),
        ],
    )

    findings = run_doctor(backend, config, fix=False)
    retry = [f for f in findings if f.check == "retry_ceiling"]
    assert len(retry) == 1
    assert retry[0].severity == "error"
    assert retry[0].auto_fixable is False
    assert "task-dead" in retry[0].message
    assert "3/3" in retry[0].message


def test_retry_patterns_flags_retrying_at_two_of_three(setup):
    """2 finished attempts, task still ready → one retrying warning."""
    backend, config = setup
    data_dir = Path(config["data_dir"])
    _write_task_file(
        data_dir,
        "ready",
        "task-warn",
        status="ready",
        attempts=[
            _att("att-001", "superseded"),
            _att("att-002", "superseded"),
        ],
    )

    findings = run_doctor(backend, config, fix=False)
    retrying = [f for f in findings if f.check == "retrying"]
    ceilings = [f for f in findings if f.check == "retry_ceiling"]
    assert ceilings == []
    assert len(retrying) == 1
    assert retrying[0].severity == "warning"
    assert retrying[0].auto_fixable is False
    assert "task-warn" in retrying[0].message
    assert "2 of max 3" in retrying[0].message


def test_retry_patterns_ignores_fresh_task(setup):
    """A task with zero finished attempts triggers no retry findings."""
    backend, config = setup
    backend.carry(_make_task("task-fresh"))

    findings = run_doctor(backend, config, fix=False)
    retry = [f for f in findings if f.check in ("retry_ceiling", "retrying")]
    assert retry == []


def test_retry_patterns_skips_infra_tasks(setup):
    """Plan/review infra tasks are never flagged for retry patterns."""
    backend, config = setup
    data_dir = Path(config["data_dir"])
    infra_ids = ["plan-foo", "review-foo", "review-plan-foo"]
    for tid in infra_ids:
        _write_task_file(
            data_dir,
            "blocked",
            tid,
            status="blocked",
            attempts=[
                _att("att-001", "superseded"),
                _att("att-002", "superseded"),
                _att("att-003", "superseded"),
            ],
        )

    findings = run_doctor(backend, config, fix=False)
    retry = [f for f in findings if f.check in ("retry_ceiling", "retrying")]
    assert retry == []


def test_retry_patterns_extracts_last_failure_reason(setup):
    """The most recent kickback trail message is surfaced in the finding."""
    backend, config = setup
    data_dir = Path(config["data_dir"])
    now = datetime.now(UTC).isoformat()
    trail = [
        {"ts": now, "worker_id": "w1", "message": "started work"},
        {
            "ts": now,
            "worker_id": "system",
            "message": "tests failed: ImportError while loading conftest",
            "action_type": "kickback",
        },
        {"ts": now, "worker_id": "w2", "message": "later noise unrelated to failure"},
    ]
    _write_task_file(
        data_dir,
        "blocked",
        "task-reason",
        status="blocked",
        attempts=[
            _att("att-001", "superseded"),
            _att("att-002", "superseded"),
            _att("att-003", "superseded"),
        ],
        trail=trail,
    )

    findings = run_doctor(backend, config, fix=False)
    retry = [f for f in findings if f.check == "retry_ceiling"]
    assert len(retry) == 1
    assert "tests failed: ImportError while loading conftest" in retry[0].message


# ---------------------------------------------------------------------------
# check_review_queue_saturated (#347)
# ---------------------------------------------------------------------------


def _seed_done_unreviewed(data_dir: Path, count: int) -> None:
    """Seed ``count`` done-unreviewed tasks (no merge, no verdict, not infra)."""
    for i in range(count):
        _write_task_file(
            data_dir,
            "done",
            f"awaiting-{i}",
            status="done",
            attempts=[_att(f"att-{i}", "done")],
        )


def test_check_review_queue_saturated_fires_after_dwell(setup):
    """5 done-unreviewed, max_reviewers=2 (threshold=4), sidecar 130s old → fires."""
    from datetime import timedelta

    backend, config = setup
    data_dir = Path(config["data_dir"])
    config["max_reviewers"] = 2

    _seed_done_unreviewed(data_dir, 5)

    # Pre-populate sidecar with a first_seen_at 130s in the past.
    sidecar_dir = data_dir / "doctor_state"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    past = (datetime.now(UTC) - timedelta(seconds=130)).isoformat()
    (sidecar_dir / "review_saturation.json").write_text(json.dumps({"first_seen_at": past}))

    findings = run_doctor(backend, config, fix=False)
    saturated = [f for f in findings if f.check == "review_queue_saturated"]
    assert len(saturated) == 1
    assert saturated[0].severity == "warning"
    assert "awaiting review" in saturated[0].message


def test_check_review_queue_saturated_silent_below_threshold(setup):
    """4 done-unreviewed with max_reviewers=2 (threshold=4); 4 !> 4 → no finding."""
    backend, config = setup
    data_dir = Path(config["data_dir"])
    config["max_reviewers"] = 2

    _seed_done_unreviewed(data_dir, 4)

    findings = run_doctor(backend, config, fix=False)
    saturated = [f for f in findings if f.check == "review_queue_saturated"]
    assert saturated == []


def test_check_review_queue_saturated_silent_within_dwell(setup):
    """Above threshold but first_seen_at only 30s ago → no finding yet."""
    from datetime import timedelta

    backend, config = setup
    data_dir = Path(config["data_dir"])
    config["max_reviewers"] = 2

    _seed_done_unreviewed(data_dir, 5)

    sidecar_dir = data_dir / "doctor_state"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    recent = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    (sidecar_dir / "review_saturation.json").write_text(json.dumps({"first_seen_at": recent}))

    findings = run_doctor(backend, config, fix=False)
    saturated = [f for f in findings if f.check == "review_queue_saturated"]
    assert saturated == []


def test_check_review_queue_saturated_clears_sidecar_when_healthy(setup):
    """Sidecar exists but current state is below threshold → sidecar is removed."""
    from datetime import timedelta

    backend, config = setup
    data_dir = Path(config["data_dir"])
    config["max_reviewers"] = 2

    # No tasks — below threshold.
    sidecar_dir = data_dir / "doctor_state"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    past = (datetime.now(UTC) - timedelta(seconds=300)).isoformat()
    sidecar_path = sidecar_dir / "review_saturation.json"
    sidecar_path.write_text(json.dumps({"first_seen_at": past}))
    assert sidecar_path.exists()

    run_doctor(backend, config, fix=False)
    assert not sidecar_path.exists()


# ---------------------------------------------------------------------------
# #352: check_stale_worktrees — prune stale antfarm worktrees
# ---------------------------------------------------------------------------


def _init_repo_with_worktree(
    tmp_path: Path, dir_name: str, branch: str = "feat/stale"
) -> tuple[Path, Path]:
    """Create a tiny git repo at tmp_path/'repo' with a worktree at
    tmp_path/'repo'/.antfarm/workspaces/<dir_name>. Returns (repo, wt)."""
    import subprocess as _sp

    repo = tmp_path / "repo"
    repo.mkdir()
    _sp.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    _sp.run(
        ["git", "config", "user.email", "test@antfarm.test"],
        cwd=str(repo),
        capture_output=True,
    )
    _sp.run(
        ["git", "config", "user.name", "Antfarm Test"],
        cwd=str(repo),
        capture_output=True,
    )
    (repo / "file.txt").write_text("init")
    _sp.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    _sp.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )

    ws_root = repo / ".antfarm" / "workspaces"
    ws_root.mkdir(parents=True)
    wt = ws_root / dir_name
    _sp.run(
        ["git", "worktree", "add", "-b", branch, str(wt)],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    return repo, wt


def _make_done_merged_task(task_id: str, attempt_id: str, completed_at: str) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "id": task_id,
        "title": task_id,
        "spec": "",
        "complexity": "M",
        "priority": 10,
        "depends_on": [],
        "touches": [],
        "status": "done",
        "current_attempt": attempt_id,
        "attempts": [
            {
                "attempt_id": attempt_id,
                "worker_id": "w-1",
                "status": "merged",
                "branch": "feat/stale",
                "pr": "PR-1",
                "started_at": now,
                "completed_at": completed_at,
            }
        ],
        "trail": [],
        "signals": [],
        "created_at": now,
        "updated_at": now,
        "created_by": "test",
    }


def test_stale_worktree_merged_past_cool_down_pruned_dry_run(tmp_path):
    """Rule 1 (merged cool-down): task done, attempt merged, age past
    cool-down → dry-run finding with reason=merged, fixed=False."""
    from antfarm.core.doctor import check_stale_worktrees

    attempt_id = "12345678-1234-4123-8123-123456789abc"
    task_id = "task-stale"
    dir_name = f"{task_id}-{attempt_id}"
    repo, _wt = _init_repo_with_worktree(tmp_path, dir_name)

    backend = FileBackend(root=str(repo / ".antfarm"))
    # completed 48h ago — past the 24h cool-down default.
    completed = (datetime.now(UTC) - timedelta(hours=48)).isoformat()

    # Write the task JSON directly into done/ — carry() has its own state
    # machine we don't need to replay here.
    done_dir = repo / ".antfarm" / "tasks" / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    task = _make_done_merged_task(task_id, attempt_id, completed)
    (done_dir / f"{task_id}.json").write_text(json.dumps(task))

    config = {"data_dir": str(repo / ".antfarm"), "repo_path": str(repo)}
    findings = check_stale_worktrees(backend, config, fix=False)

    matches = [f for f in findings if task_id in f.message]
    assert matches, f"expected a stale_worktree finding for {task_id}, got {findings}"
    f = matches[0]
    assert f.check == "stale_worktree"
    assert "reason=merged" in f.message
    assert f.fixed is False
    assert f.auto_fixable is True


def test_stale_worktree_merged_past_cool_down_fix_removes(tmp_path, monkeypatch):
    """Rule 1 with fix=True: git worktree remove is called, SSE event
    emitted, finding.fixed=True, worktree dir gone."""
    from antfarm.core import doctor as doctor_mod
    from antfarm.core.doctor import check_stale_worktrees

    attempt_id = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
    task_id = "task-fx"
    dir_name = f"{task_id}-{attempt_id}"
    repo, wt = _init_repo_with_worktree(tmp_path, dir_name)

    backend = FileBackend(root=str(repo / ".antfarm"))
    completed = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    done_dir = repo / ".antfarm" / "tasks" / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / f"{task_id}.json").write_text(
        json.dumps(_make_done_merged_task(task_id, attempt_id, completed))
    )

    emitted: list[tuple] = []

    def fake_emit(event_type, task_id="", detail="", actor="doctor"):  # noqa: ARG001
        emitted.append((event_type, detail, actor))

    monkeypatch.setattr(doctor_mod, "_emit_event", fake_emit)

    config = {"data_dir": str(repo / ".antfarm"), "repo_path": str(repo)}
    assert wt.exists()
    findings = check_stale_worktrees(backend, config, fix=True)

    matches = [f for f in findings if task_id in f.message]
    assert matches, f"expected a stale_worktree finding, got {findings}"
    assert matches[0].fixed is True
    assert not wt.exists(), "worktree directory should be gone after fix"

    pruned_events = [e for e in emitted if e[0] == "worktree_pruned"]
    assert pruned_events, f"expected worktree_pruned SSE event, got {emitted}"
    assert "reason=merged" in pruned_events[0][1]


def test_stale_worktree_superseded_attempt_pruned(tmp_path):
    """Rule 2: superseded attempt → candidate regardless of age."""
    from antfarm.core.doctor import check_stale_worktrees

    attempt_id = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
    task_id = "task-sup"
    dir_name = f"{task_id}-{attempt_id}"
    repo, _wt = _init_repo_with_worktree(tmp_path, dir_name)

    backend = FileBackend(root=str(repo / ".antfarm"))
    now = datetime.now(UTC).isoformat()
    task = {
        "id": task_id,
        "title": task_id,
        "spec": "",
        "complexity": "M",
        "priority": 10,
        "depends_on": [],
        "touches": [],
        "status": "ready",
        "current_attempt": None,
        "attempts": [
            {
                "attempt_id": attempt_id,
                "worker_id": "w-1",
                "status": "superseded",
                "branch": "feat/stale",
                "pr": None,
                "started_at": now,
                "completed_at": None,
            }
        ],
        "trail": [],
        "signals": [],
        "created_at": now,
        "updated_at": now,
        "created_by": "test",
    }
    ready_dir = repo / ".antfarm" / "tasks" / "ready"
    ready_dir.mkdir(parents=True, exist_ok=True)
    (ready_dir / f"{task_id}.json").write_text(json.dumps(task))

    config = {"data_dir": str(repo / ".antfarm"), "repo_path": str(repo)}
    findings = check_stale_worktrees(backend, config, fix=False)

    matches = [f for f in findings if task_id in f.message]
    assert matches, f"expected a stale_worktree finding for superseded, got {findings}"
    assert "reason=superseded" in matches[0].message


def test_stale_worktree_orphan_no_task_pruned(tmp_path):
    """Rule 3: backend.get_task returns None → candidate regardless of age."""
    from antfarm.core.doctor import check_stale_worktrees

    attempt_id = "cccccccc-3333-4333-8333-cccccccccccc"
    task_id = "task-orph"
    dir_name = f"{task_id}-{attempt_id}"
    repo, _wt = _init_repo_with_worktree(tmp_path, dir_name)

    backend = FileBackend(root=str(repo / ".antfarm"))
    # No task JSON written — backend.get_task returns None.

    config = {"data_dir": str(repo / ".antfarm"), "repo_path": str(repo)}
    findings = check_stale_worktrees(backend, config, fix=False)

    matches = [f for f in findings if task_id in f.message]
    assert matches, f"expected a stale_worktree finding for orphan, got {findings}"
    assert "reason=orphan" in matches[0].message


def test_stale_worktree_ttl_catchall(tmp_path):
    """Rule 4: dir mtime > TTL triggers pruning regardless of task state
    (use a small TTL and backdate the worktree)."""
    from antfarm.core.doctor import check_stale_worktrees

    attempt_id = "dddddddd-4444-4444-8444-dddddddddddd"
    task_id = "task-ttl"
    dir_name = f"{task_id}-{attempt_id}"
    repo, wt = _init_repo_with_worktree(tmp_path, dir_name)

    # Task is healthy but the worktree is ancient.
    backend = FileBackend(root=str(repo / ".antfarm"))
    now = datetime.now(UTC).isoformat()
    task = _make_done_merged_task(task_id, attempt_id, now)
    done_dir = repo / ".antfarm" / "tasks" / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / f"{task_id}.json").write_text(json.dumps(task))

    # Backdate the worktree dir mtime by 10 days.
    old = time.time() - 10 * 86400
    os.utime(str(wt), (old, old))

    config = {
        "data_dir": str(repo / ".antfarm"),
        "repo_path": str(repo),
        "worktree_prune_ttl_days": 7,
        # Keep merged cool-down large so rule 1 does NOT fire (we want to
        # isolate the TTL catchall). Task's completed_at is "now" so merged
        # cool-down of 24h is never met anyway.
    }
    findings = check_stale_worktrees(backend, config, fix=False)

    matches = [f for f in findings if task_id in f.message]
    assert matches, f"expected a stale_worktree finding, got {findings}"
    # Either ttl or merged depending on whose age check wins; since completed
    # is "now" rule 1 can't fire — must be ttl.
    assert "reason=ttl" in matches[0].message


def test_stale_worktree_refuses_symlink_outside_antfarm(tmp_path, monkeypatch):
    """Safety guard: the fix path refuses to git-worktree-remove any path
    whose realpath does not live under .antfarm/workspaces/. We simulate
    this by making git worktree list report a path outside the antfarm
    workspaces tree and confirming no git worktree remove is invoked."""
    from antfarm.core import doctor as doctor_mod
    from antfarm.core.doctor import check_stale_worktrees

    repo = tmp_path / "repo"
    repo.mkdir()
    # Set up just enough of the .antfarm/workspaces tree so the check
    # enumerates at all.
    (repo / ".antfarm" / "workspaces").mkdir(parents=True)
    # Pretend a rogue worktree entry outside antfarm is "seen" by git.
    outside = tmp_path / "outside-wt"
    outside.mkdir()

    # Bypass real enumeration: return a path OUTSIDE .antfarm/workspaces/.
    monkeypatch.setattr(
        doctor_mod,
        "_enumerate_antfarm_worktrees",
        lambda repo_path: [str(outside)],
    )

    calls: list[list[str]] = []
    import subprocess as _sp

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return _sp.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

    backend = FileBackend(root=str(repo / ".antfarm"))
    config = {
        "data_dir": str(repo / ".antfarm"),
        "repo_path": str(repo),
        # Force rule 4 to match so the fix path is exercised.
        "worktree_prune_ttl_days": 0,
    }
    findings = check_stale_worktrees(backend, config, fix=True)

    # No git worktree remove invocation against a path outside antfarm.
    remove_calls = [c for c in calls if c[:3] == ["git", "worktree", "remove"]]
    assert remove_calls == [], f"unexpected worktree remove: {remove_calls}"

    # Must have emitted a refusal finding.
    assert any(f.check == "stale_worktree" and f.fixed is False for f in findings)


def test_stale_worktree_keep_worktree_flag_exempts_path(tmp_path):
    """A path listed in keep_worktrees must not be pruned even when it
    qualifies; instead a Finding with fixed=False is returned."""
    from antfarm.core.doctor import check_stale_worktrees

    attempt_id = "eeeeeeee-5555-4555-8555-eeeeeeeeeeee"
    task_id = "task-keep"
    dir_name = f"{task_id}-{attempt_id}"
    repo, wt = _init_repo_with_worktree(tmp_path, dir_name)

    backend = FileBackend(root=str(repo / ".antfarm"))
    # Make it qualify under rule 3 (orphan: no task JSON written).
    config = {"data_dir": str(repo / ".antfarm"), "repo_path": str(repo)}
    findings = check_stale_worktrees(backend, config, fix=True, keep_worktrees=[str(wt)])
    matches = [f for f in findings if task_id in f.message]
    assert matches, f"expected a stale_worktree finding, got {findings}"
    # Skipped because of --keep-worktree — dir must still exist.
    assert wt.exists(), "keep_worktrees path should not be pruned"
    assert matches[0].fixed is False
    assert "keep" in matches[0].message.lower()


def test_stale_worktree_parsing_hazard_unparseable_name(tmp_path):
    """A directory name that does not match ``{task}-{uuid4}`` must not
    trigger rules 1-3 (they need a valid task/attempt pair) — only the
    TTL catchall (rule 4) applies via dir mtime."""
    from antfarm.core.doctor import check_stale_worktrees

    # Unparseable — no UUID4 tail.
    dir_name = "legacy-worktree-no-uuid"
    repo, wt = _init_repo_with_worktree(tmp_path, dir_name)

    backend = FileBackend(root=str(repo / ".antfarm"))
    config = {
        "data_dir": str(repo / ".antfarm"),
        "repo_path": str(repo),
        # TTL high enough that fresh dir doesn't qualify yet.
        "worktree_prune_ttl_days": 30,
    }
    findings = check_stale_worktrees(backend, config, fix=False)
    # No TTL hit because dir is fresh, no rule 1-3 because name is unparseable.
    stale = [f for f in findings if f.check == "stale_worktree"]
    assert stale == [], f"unparseable fresh dir must not qualify yet; got findings: {stale}"

    # Now backdate and verify rule 4 fires.
    old = time.time() - 60 * 86400
    os.utime(str(wt), (old, old))
    config["worktree_prune_ttl_days"] = 7
    findings = check_stale_worktrees(backend, config, fix=False)
    stale = [f for f in findings if f.check == "stale_worktree"]
    assert stale, "rule 4 should fire once dir mtime is past TTL"
    assert "reason=ttl" in stale[0].message


# ---------------------------------------------------------------------------
# Activity emission (#348)
# ---------------------------------------------------------------------------


def test_run_doctor_emits_activity_per_check(setup, monkeypatch):
    """run_doctor publishes 'scanning' activity for each phase (#348)."""
    from antfarm.core import serve

    backend, config = setup
    calls: list[tuple[str, str, str]] = []

    def _capture(kind, action, target=""):
        calls.append((kind, action, target))

    monkeypatch.setattr(serve, "_set_colony_activity", _capture)

    run_doctor(backend, config, fix=False)

    doctor_calls = [c for c in calls if c[0] == "doctor"]
    scanning = {c[2] for c in doctor_calls if c[1] == "scanning"}
    # A handful of critical stages — don't pin the full list so new checks
    # can be added without churning this test.
    for expected in {"filesystem", "workers", "tasks", "guards"}:
        assert expected in scanning, f"expected scanning target {expected!r}; got {scanning}"

    # Final call is idle — doctor is done.
    assert doctor_calls[-1] == ("doctor", "idle", "")
