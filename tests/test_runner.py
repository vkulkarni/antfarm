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
    return r


def _mock_pm(alive: bool = True) -> MagicMock:
    """Create a mock ProcessManager."""
    pm = MagicMock()
    pm.start.return_value = True
    pm.is_alive.return_value = alive
    pm.stop.return_value = True
    pm.adopt_existing.return_value = {}
    pm.max_counter.return_value = 0
    pm.cleanup.return_value = None
    return pm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunnerDefaults:
    def test_default_bind_loopback(self, tmp_path):
        """Runner binds to 127.0.0.1 by default."""
        r = _make_runner(tmp_path)
        assert r.host == "127.0.0.1"

    def test_pm_instantiated(self, tmp_path):
        """Runner instantiates a ProcessManager on __init__."""
        r = _make_runner(tmp_path)
        assert r._pm is not None

    def test_runner_uses_hashed_prefix(self, tmp_path):
        """Runner's ProcessManager prefix is ``runner-{colony_hash(state_dir)}-`` (#231)."""
        from antfarm.core.process_manager import colony_hash

        r = _make_runner(tmp_path)
        expected = f"runner-{colony_hash(r.state_dir)}-"
        assert r._prefix == expected


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
    def test_reconcile_starts_missing(self, tmp_path):
        """Reconcile starts workers to match desired count via ProcessManager."""
        r = _make_runner(tmp_path)
        r._pm = _mock_pm()

        r._desired = DesiredState(generation=1, desired={"builder": 2})
        r.reconcile()

        assert r._pm.start.call_count == 2
        assert len(r.managed) == 2
        for mw in r.managed.values():
            assert mw.role == "builder"

    def test_reconcile_stops_excess(self, tmp_path):
        """Reconcile stops excess idle workers when desired count decreases."""
        r = _make_runner(tmp_path)
        r._pm = _mock_pm()

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

    def test_drain_does_not_stop_active(self, tmp_path):
        """Drain does not stop active workers."""
        r = _make_runner(tmp_path)
        r._pm = _mock_pm()

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

        # Active workers should not be stopped — still 2 alive
        assert len(r.managed) == 2

    def test_restart_crashed(self, tmp_path):
        """Crashed workers are replaced if still desired."""
        r = _make_runner(tmp_path)
        pm = _mock_pm()
        r._pm = pm

        r._desired = DesiredState(generation=1, desired={"builder": 1})
        r.reconcile()

        assert len(r.managed) == 1
        original_name = list(r.managed.keys())[0]

        # Simulate crash: make is_alive return False for original, True for new
        def is_alive_side_effect(name):
            return name != original_name

        pm.is_alive.side_effect = is_alive_side_effect

        r.reconcile()

        # Old worker removed, new one started (2 total starts)
        assert pm.start.call_count == 2
        assert original_name not in r.managed


class TestActualState:
    def test_actual_state_reports_correctly(self, tmp_path):
        """get_actual_state reflects managed workers and applied generation."""
        r = _make_runner(tmp_path)
        r._pm = _mock_pm()

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


class TestStartWorker:
    def test_start_worker_calls_pm_start(self, tmp_path):
        """_start_worker calls _pm.start with correct args (name, cmd, log_path, role)."""
        r = _make_runner(tmp_path)
        r._pm = _mock_pm()

        with r._lock:
            r._start_worker("builder")

        r._pm.start.assert_called_once()
        call_args = r._pm.start.call_args
        name = call_args[0][0]
        cmd = call_args[0][1]
        log_path = call_args[0][2]
        role = call_args[1].get("role") or call_args[0][3]

        assert name.startswith(r._prefix)
        assert name.endswith("-builder-1")
        assert "antfarm" in cmd[0]
        assert "--type" in cmd
        assert "builder" in cmd
        assert log_path.endswith(f"{name}.log")
        assert role == "builder"

    def test_start_worker_name_collision_retry(self, tmp_path):
        """_start_worker retries with bumped counter on first failure."""
        r = _make_runner(tmp_path)
        pm = _mock_pm()
        # First call fails, second succeeds
        pm.start.side_effect = [False, True]
        r._pm = pm

        with r._lock:
            r._start_worker("builder")

        assert pm.start.call_count == 2
        assert len(r.managed) == 1
        # Counter should have been bumped twice
        assert r._counter == 2

    def test_start_worker_all_retries_fail(self, tmp_path):
        """_start_worker warns and skips if all retries fail."""
        r = _make_runner(tmp_path)
        pm = _mock_pm()
        pm.start.return_value = False
        r._pm = pm

        with r._lock:
            r._start_worker("builder")

        assert pm.start.call_count == 2
        assert len(r.managed) == 0


class TestAdoptExistingWorkers:
    def test_adopt_reads_from_pm(self, tmp_path):
        """_adopt_existing_workers uses _pm.adopt_existing(), not raw PID files."""
        r = _make_runner(tmp_path)
        pm = _mock_pm()
        pm.adopt_existing.return_value = {
            "runner-builder-3": "builder",
            "runner-planner-1": "planner",
        }
        pm.max_counter.return_value = 3
        r._pm = pm

        r._adopt_existing_workers()

        pm.adopt_existing.assert_called_once()
        assert "runner-builder-3" in r.managed
        assert "runner-planner-1" in r.managed
        assert r.managed["runner-builder-3"].role == "builder"
        assert r.managed["runner-planner-1"].role == "planner"
        assert r._counter == 3

    def test_adopt_empty(self, tmp_path):
        """_adopt_existing_workers handles empty adoption gracefully."""
        r = _make_runner(tmp_path)
        r._pm = _mock_pm()

        r._adopt_existing_workers()

        assert len(r.managed) == 0
        assert r._counter == 0


class TestStalePidsDirSweep:
    def test_stale_pids_dir_swept_on_run(self, tmp_path):
        """run() sweeps stale v0.6.1 pids/ directory if it exists."""
        r = _make_runner(tmp_path)

        # Create a stale pids/ directory with a file in it
        pids_dir = os.path.join(r.state_dir, "pids")
        os.makedirs(pids_dir, exist_ok=True)
        stale_file = os.path.join(pids_dir, "runner-builder-1.pid")
        with open(stale_file, "w") as f:
            f.write("12345")

        # Patch out uvicorn and the blocking parts so run() doesn't hang
        import uvicorn

        with (
            patch("antfarm.core.runner.Runner._adopt_existing_workers"),
            patch("antfarm.core.runner.threading.Thread"),
            patch.object(uvicorn, "run"),
            patch("antfarm.core.colony_client.ColonyClient"),
        ):
            r.run()

        # pids/ directory should have been removed
        assert not os.path.isdir(pids_dir)

    def test_no_pids_dir_does_not_crash(self, tmp_path):
        """run() does not crash if pids/ directory does not exist."""
        r = _make_runner(tmp_path)
        pids_dir = os.path.join(r.state_dir, "pids")
        assert not os.path.isdir(pids_dir)

        import uvicorn

        with (
            patch("antfarm.core.runner.Runner._adopt_existing_workers"),
            patch("antfarm.core.runner.threading.Thread"),
            patch.object(uvicorn, "run"),
            patch("antfarm.core.colony_client.ColonyClient"),
        ):
            r.run()  # should not raise
