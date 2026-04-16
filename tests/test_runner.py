"""Tests for antfarm.core.runner — Runner daemon with desired-state reconciliation."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from antfarm.core.runner import DesiredState, Runner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runner(tmp_path, **kwargs) -> Runner:
    defaults = {
        "node_id": "test-node",
        "colony_url": "http://localhost:7433",
        "repo_path": str(tmp_path / "repo"),
        "workspace_root": str(tmp_path / "ws"),
        "state_dir": str(tmp_path / "state"),
        "max_workers": 4,
    }
    defaults.update(kwargs)
    r = Runner(**defaults)
    os.makedirs(r.state_dir, exist_ok=True)
    os.makedirs(os.path.join(r.state_dir, "pids"), exist_ok=True)
    return r


def _mock_popen():
    """Create a mock Popen that looks alive."""
    p = MagicMock()
    p.poll.return_value = None  # alive
    p.pid = 12345
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunnerDefaults:
    def test_default_bind_loopback(self, tmp_path):
        """Runner binds to 127.0.0.1 by default."""
        r = _make_runner(tmp_path)
        assert r.host == "127.0.0.1"


class TestDesiredState:
    def test_apply_desired_state(self, tmp_path):
        """apply_desired_state stores the desired state and returns True."""
        r = _make_runner(tmp_path)
        state = DesiredState(generation=1, desired={"builder": 2})
        result = r.apply_desired_state(state)
        assert result is True
        assert r._desired.generation == 1
        assert r._desired.desired == {"builder": 2}

    def test_generation_monotonic(self, tmp_path):
        """Rejects desired state with generation older than applied."""
        r = _make_runner(tmp_path)
        # Apply generation 5
        r.apply_desired_state(DesiredState(generation=5, desired={"builder": 2}))
        # Reconcile to set applied_generation
        with r._lock:
            r._applied_generation = 5

        # Attempt to apply generation 3 — should be rejected
        result = r.apply_desired_state(DesiredState(generation=3, desired={"builder": 1}))
        assert result is False
        # Desired should still be the old one
        assert r._desired.desired == {"builder": 2}


class TestReconcile:
    @patch("antfarm.core.runner.subprocess.Popen")
    def test_reconcile_starts_missing(self, mock_popen_cls, tmp_path):
        """Reconcile starts workers to match desired count."""
        mock_proc = _mock_popen()
        mock_popen_cls.return_value = mock_proc

        r = _make_runner(tmp_path)
        r._desired = DesiredState(generation=1, desired={"builder": 2})
        r.reconcile()

        assert mock_popen_cls.call_count == 2
        assert len(r.managed) == 2
        for mw in r.managed.values():
            assert mw.role == "builder"

    @patch("antfarm.core.runner.subprocess.Popen")
    def test_reconcile_stops_excess(self, mock_popen_cls, tmp_path):
        """Reconcile stops excess idle workers when desired count decreases."""
        mock_proc = _mock_popen()
        mock_popen_cls.return_value = mock_proc

        r = _make_runner(tmp_path)

        # Start with 3 builders
        r._desired = DesiredState(generation=1, desired={"builder": 3})
        r.reconcile()
        assert len(r.managed) == 3

        # Mock Colony to report 2 of 3 as idle
        mock_colony = MagicMock()
        names = list(r.managed.keys())
        mock_colony.list_workers.return_value = [
            {"worker_id": f"test-node/{names[0]}", "status": "idle"},
            {"worker_id": f"test-node/{names[1]}", "status": "idle"},
            {"worker_id": f"test-node/{names[2]}", "status": "active"},
        ]
        r._colony = mock_colony

        # Reduce desired to 1
        r._desired = DesiredState(generation=2, desired={"builder": 1})
        r.reconcile()

        # Should have stopped 2 idle workers, leaving 1
        assert len(r.managed) == 1

    @patch("antfarm.core.runner.subprocess.Popen")
    def test_drain_finishes_active(self, mock_popen_cls, tmp_path):
        """Drain does not stop active workers."""
        mock_proc = _mock_popen()
        mock_popen_cls.return_value = mock_proc

        r = _make_runner(tmp_path)

        # Start 2 builders
        r._desired = DesiredState(generation=1, desired={"builder": 2})
        r.reconcile()
        assert len(r.managed) == 2

        # Mock Colony: all workers active
        mock_colony = MagicMock()
        names = list(r.managed.keys())
        mock_colony.list_workers.return_value = [
            {"worker_id": f"test-node/{names[0]}", "status": "active"},
            {"worker_id": f"test-node/{names[1]}", "status": "active"},
        ]
        r._colony = mock_colony

        # Desire 0 builders but drain
        r._desired = DesiredState(generation=2, desired={"builder": 0}, drain=["builder"])
        r.reconcile()

        # _stop_idle_worker won't stop active workers, and drain skips active too
        # All should still be running (Colony says active)
        alive_count = sum(1 for mw in r.managed.values() if mw.is_alive())
        assert alive_count == 2

    @patch("antfarm.core.runner.subprocess.Popen")
    def test_restart_crashed(self, mock_popen_cls, tmp_path):
        """Crashed workers are replaced if still desired."""
        mock_proc = _mock_popen()
        mock_popen_cls.return_value = mock_proc

        r = _make_runner(tmp_path)
        r._desired = DesiredState(generation=1, desired={"builder": 1})
        r.reconcile()

        assert len(r.managed) == 1
        original_name = list(r.managed.keys())[0]

        # Simulate crash: make process report as dead
        r.managed[original_name].process.poll.return_value = 1  # exited

        # New Popen for restart
        new_proc = _mock_popen()
        mock_popen_cls.return_value = new_proc

        r.reconcile()

        # Old worker removed, new one started
        assert len(r.managed) == 1
        new_name = list(r.managed.keys())[0]
        assert new_name != original_name


class TestActualState:
    @patch("antfarm.core.runner.subprocess.Popen")
    def test_actual_state_reports_correctly(self, mock_popen_cls, tmp_path):
        """get_actual_state reflects managed workers and applied generation."""
        mock_proc = _mock_popen()
        mock_popen_cls.return_value = mock_proc

        r = _make_runner(tmp_path)
        r._desired = DesiredState(generation=3, desired={"builder": 1})
        r.reconcile()

        state = r.get_actual_state()
        assert state["applied_generation"] == 3
        assert len(state["workers"]) == 1
        worker = list(state["workers"].values())[0]
        assert worker["role"] == "builder"
        assert worker["alive"] is True
        assert state["capacity"]["max_workers"] == 4
        assert state["capacity"]["available"] == 3


class TestHealthEndpoint:
    def test_health_endpoint(self, tmp_path):
        """GET /health returns 200 with status ok."""
        r = _make_runner(tmp_path)
        app = r._build_app()
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["node_id"] == "test-node"


class TestPidFiles:
    @patch("antfarm.core.runner.subprocess.Popen")
    def test_pid_file_written(self, mock_popen_cls, tmp_path):
        """_start_worker creates a PID file."""
        mock_proc = _mock_popen()
        mock_popen_cls.return_value = mock_proc

        r = _make_runner(tmp_path)
        with r._lock:
            r._start_worker("builder")

        # Check PID file exists
        pids_dir = os.path.join(r.state_dir, "pids")
        pid_files = os.listdir(pids_dir)
        assert len(pid_files) == 1
        assert pid_files[0].endswith(".pid")

        with open(os.path.join(pids_dir, pid_files[0])) as f:
            content = f.read().strip()
        assert content == str(mock_proc.pid)

    def test_adopt_existing_on_restart(self, tmp_path):
        """Runner adopts live processes from PID files on startup."""
        r = _make_runner(tmp_path)

        # Write a PID file for our own process (guaranteed alive)
        my_pid = os.getpid()
        pid_path = os.path.join(r.state_dir, "pids", "runner-builder-5.pid")
        with open(pid_path, "w") as f:
            f.write(str(my_pid))

        r._adopt_existing_workers()

        assert "runner-builder-5" in r.managed
        mw = r.managed["runner-builder-5"]
        assert mw.pid == my_pid
        assert mw.role == "builder"
        assert mw.is_alive()

    def test_stale_pid_cleaned(self, tmp_path):
        """Runner removes PID files for dead processes."""
        r = _make_runner(tmp_path)

        # Write a PID file with a very high PID unlikely to exist
        pid_path = os.path.join(r.state_dir, "pids", "runner-builder-99.pid")
        with open(pid_path, "w") as f:
            f.write("999999999")

        r._adopt_existing_workers()

        # Worker should not be adopted
        assert "runner-builder-99" not in r.managed
        # PID file should be cleaned up
        assert not os.path.exists(pid_path)
