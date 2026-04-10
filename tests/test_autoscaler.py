"""Tests for antfarm.core.autoscaler — single-host autoscaler daemon."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from antfarm.core.autoscaler import Autoscaler, AutoscalerConfig

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
) -> dict:
    w: dict = {
        "worker_id": worker_id,
        "status": status,
        "capabilities": capabilities or [],
    }
    if cooldown_until is not None:
        w["cooldown_until"] = cooldown_until
    return w


def _make_autoscaler(**kwargs) -> Autoscaler:
    backend = MagicMock()
    config = AutoscalerConfig(**kwargs)
    return Autoscaler(backend, config)


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
            _task("review-t1", status="done", current_attempt="att-1",
                  attempts=[{"attempt_id": "att-1", "status": "done"}]),
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
    @patch("antfarm.core.autoscaler.subprocess.Popen")
    @patch("antfarm.core.autoscaler.os.makedirs")
    @patch("builtins.open", new_callable=MagicMock)
    def test_reconcile_starts_workers_to_meet_desired(self, mock_open, mock_makedirs, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        a = _make_autoscaler(max_builders=4)
        a.backend.list_tasks.return_value = [
            _task("t1", touches=["api"]),
            _task("t2", touches=["db"]),
        ]
        a.backend.list_workers.return_value = []

        a._reconcile()

        # Should have started 2 builders (2 scope groups, 2 tasks)
        builder_starts = [
            call for call in mock_popen.call_args_list
            if "--type" in call[0][0] and "builder" in call[0][0]
        ]
        assert len(builder_starts) == 2

    @patch("antfarm.core.autoscaler.subprocess.Popen")
    @patch("antfarm.core.autoscaler.os.makedirs")
    @patch("builtins.open", new_callable=MagicMock)
    def test_stop_idle_worker_respects_colony_state(self, mock_open, mock_makedirs, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_popen.return_value = mock_proc

        a = _make_autoscaler()
        # Simulate a managed worker
        from antfarm.core.autoscaler import ManagedWorker

        mw = ManagedWorker(
            name="auto-builder-1",
            role="builder",
            worker_id="local/auto-builder-1",
            process=mock_proc,
        )
        a.managed["auto-builder-1"] = mw

        # Colony says this worker is active (not idle) -> should NOT stop
        a.backend.list_workers.return_value = [
            _worker("local/auto-builder-1", status="active"),
        ]
        stopped = a._stop_idle_worker("builder")
        assert stopped is False
        mock_proc.terminate.assert_not_called()

        # Colony says this worker is idle -> should stop
        a.backend.list_workers.return_value = [
            _worker("local/auto-builder-1", status="idle"),
        ]
        stopped = a._stop_idle_worker("builder")
        assert stopped is True
        mock_proc.terminate.assert_called_once()

    def test_cleanup_exited_removes_dead_workers(self):
        a = _make_autoscaler()
        from antfarm.core.autoscaler import ManagedWorker

        dead_proc = MagicMock()
        dead_proc.poll.return_value = 0
        dead_proc.returncode = 0

        alive_proc = MagicMock()
        alive_proc.poll.return_value = None

        a.managed["dead"] = ManagedWorker("dead", "builder", "local/dead", dead_proc)
        a.managed["alive"] = ManagedWorker("alive", "builder", "local/alive", alive_proc)

        a._cleanup_exited()

        assert "dead" not in a.managed
        assert "alive" in a.managed

    @patch("antfarm.core.autoscaler.subprocess.Popen")
    @patch("antfarm.core.autoscaler.os.makedirs")
    @patch("builtins.open", new_callable=MagicMock)
    def test_run_once_is_idempotent_when_at_desired(
        self, mock_open, mock_makedirs, mock_popen
    ):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99
        mock_popen.return_value = mock_proc

        a = _make_autoscaler()
        a.backend.list_tasks.return_value = [_task("t1", touches=["api"])]
        a.backend.list_workers.return_value = []

        # First reconcile starts workers
        a._reconcile()
        first_count = mock_popen.call_count

        # Second reconcile should not start more (already at desired)
        a._reconcile()
        assert mock_popen.call_count == first_count


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
        a = _make_autoscaler()
        from antfarm.core.autoscaler import ManagedWorker

        alive = MagicMock()
        alive.poll.return_value = None
        dead = MagicMock()
        dead.poll.return_value = 1

        a.managed["b1"] = ManagedWorker("b1", "builder", "local/b1", alive)
        a.managed["b2"] = ManagedWorker("b2", "builder", "local/b2", dead)
        a.managed["r1"] = ManagedWorker("r1", "reviewer", "local/r1", alive)

        counts = a._count_actual()
        assert counts["builder"] == 1
        assert counts["reviewer"] == 1

    def test_stop_terminates_all_managed(self):
        a = _make_autoscaler()
        from antfarm.core.autoscaler import ManagedWorker

        proc = MagicMock()
        proc.poll.return_value = None
        a.managed["w1"] = ManagedWorker("w1", "builder", "local/w1", proc)

        a.stop()
        assert a._stopped is True
        proc.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# _start_worker tests
# ---------------------------------------------------------------------------


class TestStartWorker:
    @patch("antfarm.core.autoscaler.subprocess.Popen")
    @patch("antfarm.core.autoscaler.os.makedirs")
    @patch("builtins.open", new_callable=MagicMock)
    def test_start_worker_builds_correct_command(self, mock_open, mock_makedirs, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        a = _make_autoscaler(
            agent_type="claude-code",
            node_id="node-1",
            repo_path="/repo",
            integration_branch="dev",
            workspace_root="/ws",
            colony_url="http://localhost:7433",
        )
        a._start_worker("builder")

        cmd = mock_popen.call_args[0][0]
        assert "antfarm" in cmd
        assert "--type" in cmd
        idx = cmd.index("--type")
        assert cmd[idx + 1] == "builder"
        assert "--agent" in cmd
        assert "claude-code" in cmd
        assert "--node" in cmd
        assert "node-1" in cmd

    @patch("antfarm.core.autoscaler.subprocess.Popen")
    @patch("antfarm.core.autoscaler.os.makedirs")
    @patch("builtins.open", new_callable=MagicMock)
    def test_start_worker_with_token(self, mock_open, mock_makedirs, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        a = _make_autoscaler(token="secret123")
        a._start_worker("reviewer")

        cmd = mock_popen.call_args[0][0]
        assert "--token" in cmd
        assert "secret123" in cmd

    @patch("antfarm.core.autoscaler.subprocess.Popen")
    @patch("antfarm.core.autoscaler.os.makedirs")
    @patch("builtins.open", new_callable=MagicMock)
    def test_start_worker_creates_log_dir(self, mock_open, mock_makedirs, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        a = _make_autoscaler(data_dir="/data")
        a._start_worker("planner")

        mock_makedirs.assert_called_with("/data/logs", exist_ok=True)
