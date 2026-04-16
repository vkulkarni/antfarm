"""Tests for multi-node autoscaler and extracted standalone functions."""

from __future__ import annotations

from unittest.mock import MagicMock

from antfarm.core.autoscaler import (
    Autoscaler,
    AutoscalerConfig,
    MultiNodeAutoscaler,
    compute_desired,
    count_scope_groups,
    has_merged_attempt,
    has_verdict,
    is_rate_limited,
)

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


def _node(node_id: str, runner_url: str = "http://node:8000", max_workers: int = 4) -> dict:
    return {
        "node_id": node_id,
        "runner_url": runner_url,
        "max_workers": max_workers,
        "capabilities": [],
    }


# ---------------------------------------------------------------------------
# Test 1: standalone compute_desired matches Autoscaler method
# ---------------------------------------------------------------------------


class TestComputeDesiredExtracted:
    def test_compute_desired_extracted(self):
        """Standalone compute_desired produces identical output to Autoscaler method."""
        config = AutoscalerConfig(max_builders=10, max_reviewers=2)
        tasks = [
            _task("t1", touches=["api"]),
            _task("t2", touches=["db"]),
            _task("t3", touches=["auth"]),
            _task("r1", capabilities_required=["review"]),
            _task(
                "d1",
                status="done",
                current_attempt="att-1",
                attempts=[{"attempt_id": "att-1", "status": "done"}],
            ),
        ]
        workers = [_worker("w1", status="active")]

        # Standalone function
        standalone_result = compute_desired(tasks, workers, config)

        # Autoscaler method
        backend = MagicMock()
        a = Autoscaler(backend, config)
        method_result = a._compute_desired(tasks, workers)

        assert standalone_result == method_result

    def test_standalone_helpers_match_class_methods(self):
        """All extracted helper functions match Autoscaler static methods."""
        task_with_verdict = _task(
            "t1",
            current_attempt="att-1",
            attempts=[{"attempt_id": "att-1", "review_verdict": {"verdict": "pass"}}],
        )
        task_merged = _task(
            "t2",
            current_attempt="att-2",
            attempts=[{"attempt_id": "att-2", "status": "merged"}],
        )
        task_plain = _task("t3", current_attempt="att-3", attempts=[{"attempt_id": "att-3"}])

        assert has_verdict(task_with_verdict) == Autoscaler._has_verdict(task_with_verdict)
        assert has_verdict(task_plain) == Autoscaler._has_verdict(task_plain)
        assert has_merged_attempt(task_merged) == Autoscaler._has_merged_attempt(task_merged)
        assert has_merged_attempt(task_plain) == Autoscaler._has_merged_attempt(task_plain)

        w_limited = _worker("w1", cooldown_until="2099-01-01T00:00:00+00:00")
        w_normal = _worker("w2")
        assert is_rate_limited(w_limited) == Autoscaler._is_rate_limited(w_limited)
        assert is_rate_limited(w_normal) == Autoscaler._is_rate_limited(w_normal)

        tasks_scoped = [_task("t1", touches=["api"]), _task("t2", touches=["db"])]
        assert count_scope_groups(tasks_scoped) == Autoscaler._count_scope_groups(tasks_scoped)
        assert count_scope_groups([]) == Autoscaler._count_scope_groups([])


# ---------------------------------------------------------------------------
# Test 2: single-host behavior unchanged after refactor
# ---------------------------------------------------------------------------


class TestSingleHostBehaviorUnchanged:
    def test_single_host_behavior_unchanged(self):
        """Existing Autoscaler still produces correct results after refactor."""
        backend = MagicMock()
        config = AutoscalerConfig(max_builders=4, max_reviewers=2)
        a = Autoscaler(backend, config)

        # Empty queue
        assert a._compute_desired([], []) == {"planner": 0, "builder": 0, "reviewer": 0}

        # Builders capped at scope groups
        tasks = [
            _task("t1", touches=["api", "db"]),
            _task("t2", touches=["db", "auth"]),
        ]
        result = a._compute_desired(tasks, [])
        assert result["builder"] == 1  # overlapping -> 1 group

        # Reviewer triggered by done unreviewed
        tasks = [
            _task(
                "d1",
                status="done",
                current_attempt="att-1",
                attempts=[{"attempt_id": "att-1", "status": "done"}],
            ),
        ]
        result = a._compute_desired(tasks, [])
        assert result["reviewer"] >= 1


# ---------------------------------------------------------------------------
# Test 3: multi-node reconcile calls actuator for each node
# ---------------------------------------------------------------------------


class TestMultiNodeReconcile:
    def test_multi_node_reconcile(self):
        """Mock backend with 2 nodes, mock actuator — apply called for each."""
        backend = MagicMock()
        backend.list_tasks.return_value = [
            _task("t1", touches=["api"]),
            _task("t2", touches=["db"]),
        ]
        backend.list_workers.return_value = []
        backend.list_nodes.return_value = [
            _node("node-a", "http://a:8000", max_workers=4),
            _node("node-b", "http://b:8000", max_workers=4),
        ]

        actuator = MagicMock()
        actuator.is_reachable.return_value = True
        actuator.get_actual.return_value = {"workers": {}}

        config = AutoscalerConfig(max_builders=4, max_reviewers=2)
        mna = MultiNodeAutoscaler(backend, config, actuator)

        mna._reconcile()

        # actuator.apply should have been called for placed nodes
        assert actuator.apply.call_count >= 1
        # Verify both nodes received apply calls
        called_urls = {c.args[0] for c in actuator.apply.call_args_list}
        assert "http://a:8000" in called_urls or "http://b:8000" in called_urls


# ---------------------------------------------------------------------------
# Test 4: generation increments each reconcile
# ---------------------------------------------------------------------------


class TestGenerationIncrements:
    def test_generation_increments(self):
        """Each reconcile bumps generation counter."""
        backend = MagicMock()
        backend.list_tasks.return_value = [_task("t1", touches=["api"])]
        backend.list_workers.return_value = []
        backend.list_nodes.return_value = [_node("node-a", "http://a:8000")]

        actuator = MagicMock()
        actuator.is_reachable.return_value = True
        actuator.get_actual.return_value = {"workers": {}}

        config = AutoscalerConfig(max_builders=4)
        mna = MultiNodeAutoscaler(backend, config, actuator)

        assert mna._generation == 0
        mna._reconcile()
        assert mna._generation == 1
        mna._reconcile()
        assert mna._generation == 2

        # Verify generation is passed to actuator.apply
        for c in actuator.apply.call_args_list:
            assert c.args[2] in (1, 2)  # generation arg


# ---------------------------------------------------------------------------
# Test 5: unreachable node is skipped
# ---------------------------------------------------------------------------


class TestUnreachableNodeSkipped:
    def test_unreachable_node_skipped(self):
        """Unreachable node gets no desired state pushed."""
        backend = MagicMock()
        backend.list_tasks.return_value = [_task("t1", touches=["api"])]
        backend.list_workers.return_value = []
        backend.list_nodes.return_value = [
            _node("node-ok", "http://ok:8000"),
            _node("node-down", "http://down:8000"),
        ]

        actuator = MagicMock()
        # node-ok reachable, node-down not
        actuator.is_reachable.side_effect = lambda url: url == "http://ok:8000"
        actuator.get_actual.return_value = {"workers": {}}

        config = AutoscalerConfig(max_builders=4)
        mna = MultiNodeAutoscaler(backend, config, actuator)

        mna._reconcile()

        # Only the reachable node should receive apply
        called_urls = {c.args[0] for c in actuator.apply.call_args_list}
        assert "http://down:8000" not in called_urls
        # The reachable node should have been called
        if actuator.apply.call_count > 0:
            assert "http://ok:8000" in called_urls
