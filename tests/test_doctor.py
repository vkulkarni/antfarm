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
    from antfarm.core.process_manager import colony_hash

    data_dir = tmp_path / ".antfarm"
    processes_dir = data_dir / "processes"
    processes_dir.mkdir(parents=True)

    h = colony_hash(str(data_dir))
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
    from antfarm.core.process_manager import colony_hash

    data_dir = tmp_path / ".antfarm"
    (data_dir / "processes").mkdir(parents=True)

    h = colony_hash(str(data_dir))
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
    from antfarm.core.process_manager import colony_hash

    data_dir = tmp_path / ".antfarm"
    (data_dir / "processes").mkdir(parents=True)

    h = colony_hash(str(data_dir))
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
    from antfarm.core.process_manager import colony_hash

    data_dir = tmp_path / ".antfarm"
    (data_dir / "processes").mkdir(parents=True)

    h = colony_hash(str(data_dir))
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
    from antfarm.core.process_manager import colony_hash

    data_dir = tmp_path / ".antfarm"
    (data_dir / "processes").mkdir(parents=True)

    h = colony_hash(str(data_dir))
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
    from antfarm.core.process_manager import colony_hash

    dir_a = tmp_path / "colony-a"
    dir_b = tmp_path / "colony-b"
    (dir_a / "processes").mkdir(parents=True)
    (dir_b / "processes").mkdir(parents=True)

    h_a = colony_hash(str(dir_a))
    h_b = colony_hash(str(dir_b))
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
