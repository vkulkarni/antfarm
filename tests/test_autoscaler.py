"""Tests for antfarm.core.autoscaler — single-host autoscaler daemon."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from antfarm.core.autoscaler import Autoscaler, AutoscalerConfig, ManagedWorker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    task_id: str,
    status: str = "ready",
    touches: list[str] | None = None,
    capabilities_required: list[str] | None = None,
    attempts: list[dict] | None = None,
    current_attempt: str | None = None,
) -> dict:
    return {
        "id": task_id,
        "status": status,
        "touches": touches or [],
        "capabilities_required": capabilities_required or [],
        "attempts": attempts or [],
        "current_attempt": current_attempt,
    }


def _worker(
    worker_id: str,
    status: str = "idle",
    capabilities: list[str] | None = None,
    cooldown_until: str | None = None,
    last_heartbeat: str | None = None,
) -> dict:
    w: dict = {
        "worker_id": worker_id,
        "status": status,
        "capabilities": capabilities or [],
    }
    if cooldown_until is not None:
        w["cooldown_until"] = cooldown_until
    if last_heartbeat is not None:
        w["last_heartbeat"] = last_heartbeat
    return w


def _aged_hb(seconds_ago: float = 60.0) -> str:
    """Return an ISO-8601 UTC timestamp `seconds_ago` seconds in the past."""
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


def _make_autoscaler(_pm=None, **kwargs) -> Autoscaler:
    backend = MagicMock()
    config = AutoscalerConfig(**kwargs)
    pm = _pm if _pm is not None else _mock_pm()
    return Autoscaler(backend, config, _pm=pm)


def _mock_pm() -> MagicMock:
    """Create a mock ProcessManager with sensible defaults."""
    pm = MagicMock()
    pm.start.return_value = True
    pm.is_alive.return_value = True
    pm.stop.return_value = True
    pm.adopt_existing.return_value = {}
    pm.max_counter.return_value = 0
    return pm


# ---------------------------------------------------------------------------
# Colony-hashed prefix (#231)
# ---------------------------------------------------------------------------


def test_autoscaler_uses_hashed_prefix(tmp_path):
    """Autoscaler's ProcessManager prefix is ``auto-{colony_hash(data_dir)}-``."""
    from antfarm.core.process_manager import colony_hash

    data_dir = str(tmp_path / ".antfarm")
    a = _make_autoscaler(data_dir=data_dir)
    expected = f"auto-{colony_hash(data_dir)}-"
    assert a._prefix == expected


# ---------------------------------------------------------------------------
# _compute_desired tests
# ---------------------------------------------------------------------------


class TestComputeDesired:
    def test_no_ready_tasks_returns_zeros(self):
        a = _make_autoscaler()
        result = a._compute_desired([], [])
        assert result == {"planner": 0, "builder": 0, "reviewer": 0}

    def test_only_plan_task_returns_one_planner(self):
        a = _make_autoscaler()
        tasks = [_task("t1", capabilities_required=["plan"])]
        result = a._compute_desired(tasks, [])
        assert result["planner"] == 1
        assert result["builder"] == 0

    def test_three_scope_groups_returns_three_builders(self):
        a = _make_autoscaler(max_builders=10)
        tasks = [
            _task("t1", touches=["api"]),
            _task("t2", touches=["db"]),
            _task("t3", touches=["auth"]),
        ]
        result = a._compute_desired(tasks, [])
        assert result["builder"] == 3

    def test_overlapping_scopes_returns_one_builder(self):
        a = _make_autoscaler(max_builders=10)
        tasks = [
            _task("t1", touches=["api", "db"]),
            _task("t2", touches=["db", "auth"]),
        ]
        result = a._compute_desired(tasks, [])
        assert result["builder"] == 1

    def test_caps_at_max_builders(self):
        a = _make_autoscaler(max_builders=2)
        tasks = [
            _task("t1", touches=["api"]),
            _task("t2", touches=["db"]),
            _task("t3", touches=["auth"]),
            _task("t4", touches=["ui"]),
        ]
        result = a._compute_desired(tasks, [])
        assert result["builder"] == 2

    def test_caps_at_queue_depth(self):
        a = _make_autoscaler(max_builders=10)
        # Only 1 ready build task but 5 scope groups possible
        tasks = [_task("t1", touches=["api"])]
        result = a._compute_desired(tasks, [])
        assert result["builder"] == 1

    def test_rate_limited_majority_doesnt_scale_up(self):
        a = _make_autoscaler(max_builders=10)
        tasks = [
            _task("t1", touches=["api"]),
            _task("t2", touches=["db"]),
            _task("t3", touches=["auth"]),
        ]
        # 3 active builders, 2 rate-limited (majority)
        workers = [
            _worker("w1", status="active", cooldown_until="2099-01-01T00:00:00+00:00"),
            _worker("w2", status="active", cooldown_until="2099-01-01T00:00:00+00:00"),
            _worker("w3", status="active"),
        ]
        result = a._compute_desired(tasks, workers)
        # Should cap at current active count (3), not scale up
        assert result["builder"] <= 3

    def test_done_unreviewed_triggers_reviewer(self):
        a = _make_autoscaler()
        tasks = [
            _task(
                "t1",
                status="done",
                current_attempt="att-1",
                attempts=[{"attempt_id": "att-1", "status": "done"}],
            ),
        ]
        result = a._compute_desired(tasks, [])
        assert result["reviewer"] >= 1

    def test_review_task_triggers_reviewer(self):
        a = _make_autoscaler()
        tasks = [_task("r1", capabilities_required=["review"])]
        result = a._compute_desired(tasks, [])
        assert result["reviewer"] >= 1

    def test_reviewer_caps_at_max(self):
        a = _make_autoscaler(max_reviewers=1)
        tasks = [
            _task("r1", capabilities_required=["review"]),
            _task("r2", capabilities_required=["review"]),
            _task("r3", capabilities_required=["review"]),
        ]
        result = a._compute_desired(tasks, [])
        assert result["reviewer"] == 1

    def test_merged_attempt_not_counted_as_unreviewed(self):
        a = _make_autoscaler()
        tasks = [
            _task(
                "t1",
                status="done",
                current_attempt="att-1",
                attempts=[{"attempt_id": "att-1", "status": "merged"}],
            ),
        ]
        result = a._compute_desired(tasks, [])
        assert result["reviewer"] == 0

    def test_review_prefixed_tasks_not_counted_as_unreviewed(self):
        a = _make_autoscaler()
        tasks = [
            _task(
                "review-t1",
                status="done",
                current_attempt="att-1",
                attempts=[{"attempt_id": "att-1", "status": "done"}],
            ),
        ]
        result = a._compute_desired(tasks, [])
        assert result["reviewer"] == 0


# ---------------------------------------------------------------------------
# _count_scope_groups tests
# ---------------------------------------------------------------------------


class TestCountScopeGroups:
    def test_disjoint(self):
        tasks = [
            _task("t1", touches=["api"]),
            _task("t2", touches=["db"]),
            _task("t3", touches=["auth"]),
        ]
        assert Autoscaler._count_scope_groups(tasks) == 3

    def test_overlapping(self):
        tasks = [
            _task("t1", touches=["api", "db"]),
            _task("t2", touches=["db"]),
        ]
        assert Autoscaler._count_scope_groups(tasks) == 1

    def test_transitively_overlapping(self):
        # A,B share x; B,C share y; A,C share nothing directly -> 1 group
        tasks = [
            _task("t1", touches=["a", "x"]),
            _task("t2", touches=["x", "y"]),
            _task("t3", touches=["y", "c"]),
        ]
        assert Autoscaler._count_scope_groups(tasks) == 1

    def test_empty(self):
        assert Autoscaler._count_scope_groups([]) == 0

    def test_no_touches_each_separate_group(self):
        tasks = [_task("t1"), _task("t2")]
        assert Autoscaler._count_scope_groups(tasks) == 2


# ---------------------------------------------------------------------------
# Reconciliation tests (with mocked Popen)
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_reconcile_starts_workers_to_meet_desired(self):
        pm = _mock_pm()
        a = _make_autoscaler(_pm=pm, max_builders=4)
        a.backend.list_tasks.return_value = [
            _task("t1", touches=["api"]),
            _task("t2", touches=["db"]),
        ]
        a.backend.list_workers.return_value = []

        a._reconcile()

        # Should have started 2 builders (2 scope groups, 2 tasks)
        builder_starts = [
            c for c in pm.start.call_args_list if "--type" in c[0][1] and "builder" in c[0][1]
        ]
        assert len(builder_starts) == 2

    def test_stop_idle_worker_respects_colony_state(self):
        pm = _mock_pm()
        pm.is_alive.return_value = True

        a = _make_autoscaler(_pm=pm)
        a.managed["auto-builder-1"] = ManagedWorker(
            name="auto-builder-1",
            role="builder",
            worker_id="local/auto-builder-1",
        )

        # Colony says this worker is active (not idle) -> should NOT stop
        a.backend.list_workers.return_value = [
            _worker("local/auto-builder-1", status="active"),
        ]
        stopped = a._stop_idle_worker("builder")
        assert stopped is False
        pm.stop.assert_not_called()

        # Colony says this worker is idle with an aged heartbeat -> should stop.
        # last_heartbeat must be older than poll_interval (default 30s); use 60s ago.
        a.backend.list_workers.return_value = [
            _worker("local/auto-builder-1", status="idle", last_heartbeat=_aged_hb(60)),
        ]
        stopped = a._stop_idle_worker("builder")
        assert stopped is True
        pm.stop.assert_called_once_with("auto-builder-1")

    def test_cleanup_exited_removes_dead_workers(self):
        pm = _mock_pm()
        # "dead" is not alive, "alive" is alive
        pm.is_alive.side_effect = lambda name: name == "alive"

        a = _make_autoscaler(_pm=pm)
        a.managed["dead"] = ManagedWorker("dead", "builder", "local/dead")
        a.managed["alive"] = ManagedWorker("alive", "builder", "local/alive")

        a._cleanup_exited()

        assert "dead" not in a.managed
        assert "alive" in a.managed
        pm.cleanup.assert_called_once_with("dead")

    def test_stop_idle_worker_respects_fresh_claim_grace(self):
        """Worker with status=idle but fresh last_heartbeat must NOT be reaped.

        This guards against the race where pull() claims a task but the
        worker's colony-side status hasn't been flipped yet (heartbeat fires
        ~30s later). The autoscaler's own 30s poll must not kill the worker
        during that window.
        """
        pm = _mock_pm()
        pm.is_alive.return_value = True

        a = _make_autoscaler(_pm=pm, poll_interval=30.0)
        a.managed["auto-builder-1"] = ManagedWorker(
            name="auto-builder-1",
            role="builder",
            worker_id="local/auto-builder-1",
        )

        # last_heartbeat is "now" — worker just registered or just claimed a task
        a.backend.list_workers.return_value = [
            _worker("local/auto-builder-1", status="idle", last_heartbeat=_aged_hb(0)),
        ]
        stopped = a._stop_idle_worker("builder")
        assert stopped is False
        pm.stop.assert_not_called()

    def test_stop_idle_worker_reaps_truly_idle_worker(self):
        """Worker with status=idle AND heartbeat older than poll_interval IS reaped."""
        pm = _mock_pm()
        pm.is_alive.return_value = True

        a = _make_autoscaler(_pm=pm, poll_interval=30.0)
        a.managed["auto-builder-1"] = ManagedWorker(
            name="auto-builder-1",
            role="builder",
            worker_id="local/auto-builder-1",
        )

        # last_heartbeat is poll_interval + 5s ago — clearly idle
        a.backend.list_workers.return_value = [
            _worker(
                "local/auto-builder-1",
                status="idle",
                last_heartbeat=_aged_hb(30.0 + 5),
            ),
        ]
        stopped = a._stop_idle_worker("builder")
        assert stopped is True
        pm.stop.assert_called_once_with("auto-builder-1")

    def test_stop_idle_worker_skips_worker_missing_last_heartbeat(self):
        """Worker dict missing last_heartbeat must NOT be reaped (fail-safe)."""
        pm = _mock_pm()
        pm.is_alive.return_value = True

        a = _make_autoscaler(_pm=pm)
        a.managed["auto-builder-1"] = ManagedWorker(
            name="auto-builder-1",
            role="builder",
            worker_id="local/auto-builder-1",
        )

        # No last_heartbeat key at all
        a.backend.list_workers.return_value = [
            _worker("local/auto-builder-1", status="idle"),
        ]
        stopped = a._stop_idle_worker("builder")
        assert stopped is False
        pm.stop.assert_not_called()

    def test_run_once_is_idempotent_when_at_desired(self):
        pm = _mock_pm()
        a = _make_autoscaler(_pm=pm)
        a.backend.list_tasks.return_value = [_task("t1", touches=["api"])]
        a.backend.list_workers.return_value = []

        # First reconcile starts workers
        a._reconcile()
        first_count = pm.start.call_count

        # Second reconcile should not start more (already at desired)
        a._reconcile()
        assert pm.start.call_count == first_count


# ---------------------------------------------------------------------------
# Worker lifecycle helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_has_verdict_true(self):
        task = _task(
            "t1",
            current_attempt="att-1",
            attempts=[
                {"attempt_id": "att-1", "review_verdict": {"verdict": "pass"}},
            ],
        )
        assert Autoscaler._has_verdict(task) is True

    def test_has_verdict_false(self):
        task = _task(
            "t1",
            current_attempt="att-1",
            attempts=[{"attempt_id": "att-1"}],
        )
        assert Autoscaler._has_verdict(task) is False

    def test_has_merged_attempt_true(self):
        task = _task(
            "t1",
            current_attempt="att-1",
            attempts=[{"attempt_id": "att-1", "status": "merged"}],
        )
        assert Autoscaler._has_merged_attempt(task) is True

    def test_has_merged_attempt_false(self):
        task = _task(
            "t1",
            current_attempt="att-1",
            attempts=[{"attempt_id": "att-1", "status": "done"}],
        )
        assert Autoscaler._has_merged_attempt(task) is False

    def test_is_rate_limited_true(self):
        w = _worker("w1", cooldown_until="2099-01-01T00:00:00+00:00")
        assert Autoscaler._is_rate_limited(w) is True

    def test_is_rate_limited_false_expired(self):
        w = _worker("w1", cooldown_until="2000-01-01T00:00:00+00:00")
        assert Autoscaler._is_rate_limited(w) is False

    def test_is_rate_limited_false_no_field(self):
        w = _worker("w1")
        assert Autoscaler._is_rate_limited(w) is False

    def test_count_actual(self):
        pm = _mock_pm()
        # b1 alive, b2 dead, r1 alive
        pm.is_alive.side_effect = lambda name: name in ("b1", "r1")

        a = _make_autoscaler(_pm=pm)
        a.managed["b1"] = ManagedWorker("b1", "builder", "local/b1")
        a.managed["b2"] = ManagedWorker("b2", "builder", "local/b2")
        a.managed["r1"] = ManagedWorker("r1", "reviewer", "local/r1")

        counts = a._count_actual()
        assert counts["builder"] == 1
        assert counts["reviewer"] == 1

    def test_stop_calls_pm_stop_for_each_managed(self):
        pm = _mock_pm()
        a = _make_autoscaler(_pm=pm)
        a.managed["w1"] = ManagedWorker("w1", "builder", "local/w1")
        a.managed["w2"] = ManagedWorker("w2", "reviewer", "local/w2")

        a.stop()
        assert a._stopped is True
        pm.stop.assert_any_call("w1")
        pm.stop.assert_any_call("w2")
        assert pm.stop.call_count == 2


# ---------------------------------------------------------------------------
# _start_worker tests
# ---------------------------------------------------------------------------


class TestStartWorker:
    @patch("antfarm.core.autoscaler.os.makedirs")
    def test_start_worker_calls_pm_start_with_correct_args(self, mock_makedirs):
        pm = _mock_pm()
        a = _make_autoscaler(
            _pm=pm,
            agent_type="claude-code",
            node_id="node-1",
            repo_path="/repo",
            integration_branch="dev",
            workspace_root="/ws",
            colony_url="http://localhost:7433",
            data_dir=".antfarm",
        )
        a._start_worker("builder")

        assert pm.start.call_count == 1
        start_call = pm.start.call_args
        name = start_call[0][0]
        cmd = start_call[0][1]
        role = start_call[1].get("role") or start_call[0][3]

        assert name.startswith(a._prefix)
        assert name.endswith("-builder-1")
        assert "antfarm" in cmd
        assert "--type" in cmd
        idx = cmd.index("--type")
        assert cmd[idx + 1] == "builder"
        assert "--agent" in cmd
        assert "claude-code" in cmd
        assert "--node" in cmd
        assert "node-1" in cmd
        assert role == "builder"

        # Worker should be tracked in managed
        assert name in a.managed
        assert a.managed[name].role == "builder"

    @patch("antfarm.core.autoscaler.os.makedirs")
    def test_start_worker_with_token(self, mock_makedirs):
        pm = _mock_pm()
        a = _make_autoscaler(_pm=pm, token="secret123")
        a._start_worker("reviewer")

        cmd = pm.start.call_args[0][1]
        assert "--token" in cmd
        assert "secret123" in cmd

    @patch("antfarm.core.autoscaler.os.makedirs")
    def test_start_worker_name_collision_retries_with_bumped_counter(self, mock_makedirs):
        pm = _mock_pm()
        # First call fails (name collision), second succeeds
        pm.start.side_effect = [False, True]

        a = _make_autoscaler(_pm=pm)
        a._start_worker("builder")

        assert pm.start.call_count == 2
        # Counter bumped twice
        assert a._counter == 2
        # The second name was registered
        names = list(a.managed.keys())
        assert len(names) == 1
        assert names[0] == f"{a._prefix}builder-2"

    @patch("antfarm.core.autoscaler.os.makedirs")
    def test_start_worker_both_attempts_fail_skips_gracefully(self, mock_makedirs):
        pm = _mock_pm()
        pm.start.return_value = False

        a = _make_autoscaler(_pm=pm)
        a._start_worker("builder")  # should not raise

        assert pm.start.call_count == 2
        assert len(a.managed) == 0

    @patch("antfarm.core.autoscaler.os.makedirs")
    def test_start_worker_creates_log_dir(self, mock_makedirs):
        pm = _mock_pm()
        a = _make_autoscaler(_pm=pm, data_dir="/data")
        a._start_worker("planner")

        mock_makedirs.assert_called_with("/data/logs", exist_ok=True)


# ---------------------------------------------------------------------------
# _adopt_existing tests
# ---------------------------------------------------------------------------


class TestAdoptExisting:
    def test_adopt_existing_populates_managed(self):
        pm = _mock_pm()
        pm.adopt_existing.return_value = {
            "auto-builder-3": "builder",
            "auto-reviewer-1": "reviewer",
        }
        pm.max_counter.return_value = 3

        a = _make_autoscaler(_pm=pm, node_id="node-x")
        a._adopt_existing()

        assert "auto-builder-3" in a.managed
        assert a.managed["auto-builder-3"].role == "builder"
        assert a.managed["auto-builder-3"].worker_id == "node-x/auto-builder-3"
        assert "auto-reviewer-1" in a.managed
        assert a._counter == 3

    def test_adopt_existing_no_workers_leaves_counter_zero(self):
        pm = _mock_pm()
        pm.adopt_existing.return_value = {}
        pm.max_counter.return_value = 0

        a = _make_autoscaler(_pm=pm)
        a._adopt_existing()

        assert a.managed == {}
        assert a._counter == 0

    def test_adopt_existing_bumps_counter_from_max(self):
        pm = _mock_pm()
        pm.adopt_existing.return_value = {"auto-builder-7": "builder"}
        pm.max_counter.return_value = 7

        a = _make_autoscaler(_pm=pm)
        a._adopt_existing()

        assert a._counter == 7

    def test_adopt_existing_ignores_legacy_unprefixed_sessions(self, tmp_path):
        """Pre-#231 `auto-builder-3` sessions must NOT be adopted after upgrade.

        Uses a real TmuxProcessManager whose prefix is the autoscaler's current
        hashed prefix. The legacy name lacks the hash token, so list_managed()
        filters it out and adopt_existing() returns {}.
        """
        from unittest.mock import MagicMock, patch

        from antfarm.core.process_manager import TmuxProcessManager, colony_hash

        data_dir = str(tmp_path / ".antfarm")
        os.makedirs(data_dir, exist_ok=True)
        prefix = f"auto-{colony_hash(data_dir)}-"
        pm = TmuxProcessManager(prefix=prefix, state_dir=data_dir)

        list_result = MagicMock()
        list_result.returncode = 0
        list_result.stdout = "auto-builder-3\nauto-planner-1\n"

        with (
            patch("antfarm.core.process_manager.shutil.which", return_value="/usr/bin/tmux"),
            patch("antfarm.core.process_manager.subprocess.run", return_value=list_result),
        ):
            a = _make_autoscaler(_pm=pm, data_dir=data_dir, node_id="node-x")
            a._adopt_existing()

        assert a.managed == {}
        assert a._counter == 0

    def test_adopt_existing_ignores_peer_colony_sessions(self, tmp_path):
        """Sessions with a DIFFERENT colony hash must NOT be adopted.

        Peer colonies on the same host use their own hash; the autoscaler's
        prefix filter rejects them in list_managed().
        """
        from unittest.mock import MagicMock, patch

        from antfarm.core.process_manager import TmuxProcessManager, colony_hash

        data_dir = str(tmp_path / ".antfarm")
        os.makedirs(data_dir, exist_ok=True)
        prefix = f"auto-{colony_hash(data_dir)}-"
        pm = TmuxProcessManager(prefix=prefix, state_dir=data_dir)

        # Foreign hash that is very unlikely to collide with the real hash.
        list_result = MagicMock()
        list_result.returncode = 0
        list_result.stdout = "auto-deadbeef-builder-1\nauto-ffffffff-reviewer-2\n"

        with (
            patch("antfarm.core.process_manager.shutil.which", return_value="/usr/bin/tmux"),
            patch("antfarm.core.process_manager.subprocess.run", return_value=list_result),
        ):
            a = _make_autoscaler(_pm=pm, data_dir=data_dir, node_id="node-x")
            a._adopt_existing()

        assert a.managed == {}
        assert a._counter == 0
