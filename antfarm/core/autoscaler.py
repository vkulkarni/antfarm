"""Autoscaler daemon for Antfarm (single-host and multi-node).

Starts and stops worker subprocesses based on queue state. Opt-in via
``antfarm colony --autoscaler``. Manages its own workers only — manually
started workers on other machines are untouched.

Scope-aware: groups ready build tasks by ``touches`` overlap and caps
builder count to the number of non-overlapping scope groups, preventing
over-allocation to a single scope.

Standalone functions (``compute_desired``, ``count_scope_groups``, etc.)
are shared by both the single-host ``Autoscaler`` and the
``MultiNodeAutoscaler``.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from antfarm.core.backends.base import TaskBackend
from antfarm.core.process_manager import (
    ProcessManager,
    colony_session_hash,
    get_process_manager,
)

if TYPE_CHECKING:
    from antfarm.core.actuator import Actuator  # noqa: F401

logger = logging.getLogger(__name__)


def _emit(event_type: str, task_id: str, detail: str = "") -> None:
    """Emit an SSE event tagged with actor='autoscaler'.

    Lazy import of ``_emit_event`` keeps autoscaler decoupled from FastAPI
    transitive imports at module load time.
    """
    try:
        from antfarm.core.serve import _emit_event
    except Exception:
        return
    _emit_event(event_type, task_id, detail, actor="autoscaler")


# ---------------------------------------------------------------------------
# Standalone helpers — shared by Autoscaler and MultiNodeAutoscaler
# ---------------------------------------------------------------------------


def compute_desired(
    tasks: list[dict], workers: list[dict], config: AutoscalerConfig
) -> dict[str, int]:
    """Compute desired worker counts by role.

    Shared by single-host and multi-node autoscalers.
    """
    ready_plan = [
        t
        for t in tasks
        if t["status"] == "ready"
        and "plan" in t.get("capabilities_required", [])
    ]
    ready_build = [
        t
        for t in tasks
        if t["status"] == "ready" and not t.get("capabilities_required")
    ]
    ready_review = [
        t
        for t in tasks
        if t["status"] == "ready"
        and "review" in t.get("capabilities_required", [])
    ]
    done_unreviewed = [
        t
        for t in tasks
        if t["status"] == "done"
        and not t["id"].startswith("review-")
        and not has_verdict(t)
        and not has_merged_attempt(t)
    ]

    scope_groups = count_scope_groups(ready_build)

    active_builders = [
        w
        for w in workers
        if "review" not in w.get("capabilities", [])
        and "plan" not in w.get("capabilities", [])
        and w.get("status") != "offline"
    ]
    rate_limited = [w for w in active_builders if is_rate_limited(w)]

    desired_builders = min(
        scope_groups,
        config.max_builders,
        len(ready_build),
    )
    if rate_limited and len(rate_limited) > len(active_builders) // 2:
        desired_builders = min(desired_builders, len(active_builders))

    return {
        "planner": 1 if ready_plan else 0,
        "builder": desired_builders,
        "reviewer": min(
            max(
                1 if (done_unreviewed or ready_review) else 0,
                len(ready_review),
            ),
            config.max_reviewers,
        ),
    }


def count_scope_groups(tasks: list[dict]) -> int:
    """Count non-overlapping scope groups (union-find by touches)."""
    if not tasks:
        return 0
    groups: list[set[str]] = []
    for t in tasks:
        touches = set(t.get("touches", []))
        if not touches:
            groups.append(set())
            continue
        hit = None
        for g in groups:
            if g & touches:
                g.update(touches)
                hit = g
                break
        if hit is None:
            groups.append(touches)
    return len(groups)


def has_verdict(task: dict) -> bool:
    """Check if a task's current attempt has a review verdict."""
    for att in task.get("attempts", []):
        if att.get("attempt_id") == task.get("current_attempt"):
            return bool(att.get("review_verdict"))
    return False


def has_merged_attempt(task: dict) -> bool:
    """Check if a task's current attempt has been merged."""
    for att in task.get("attempts", []):
        if att.get("attempt_id") == task.get("current_attempt"):
            return att.get("status") == "merged"
    return False


def is_rate_limited(worker: dict) -> bool:
    """Check if a worker is currently rate-limited."""
    cooldown = worker.get("cooldown_until")
    if not cooldown:
        return False
    try:
        cooldown_dt = datetime.fromisoformat(cooldown)
        return cooldown_dt > datetime.now(UTC)
    except (ValueError, TypeError):
        return False


@dataclass
class AutoscalerConfig:
    enabled: bool = False
    agent_type: str = "claude-code"
    node_id: str = "local"
    repo_path: str = "."
    integration_branch: str = "main"
    workspace_root: str = "./.antfarm/workspaces"
    max_builders: int = 4
    max_reviewers: int = 2
    token: str | None = None
    poll_interval: float = 30.0
    colony_url: str = "http://127.0.0.1:7433"
    data_dir: str = ".antfarm"


@dataclass
class ManagedWorker:
    name: str
    role: str  # "planner" | "builder" | "reviewer"
    worker_id: str


class Autoscaler:
    """Single-host autoscaler that spawns/stops worker subprocesses."""

    def __init__(
        self,
        backend: TaskBackend,
        config: AutoscalerConfig,
        clock=time.time,
        _pm: ProcessManager | None = None,
    ):
        self.backend = backend
        self.config = config
        self._clock = clock
        self.managed: dict[str, ManagedWorker] = {}
        self._stopped = False
        self._counter = 0
        # Ensure data_dir exists before deriving the session prefix — the
        # persisted-UUID identity needs a real directory. See also
        # Runner.__init__ for the matching guard.
        os.makedirs(config.data_dir, exist_ok=True)
        self._prefix = f"auto-{colony_session_hash(config.data_dir)}-"
        self._pm = (
            _pm
            if _pm is not None
            else get_process_manager(prefix=self._prefix, state_dir=config.data_dir)
        )

    def run(self) -> None:
        """Main loop: reconcile desired vs actual workers every poll interval."""
        self._adopt_existing()
        while not self._stopped:
            try:
                self._reconcile()
            except Exception as e:
                logger.exception("autoscaler reconcile failed: %s", e)
            time.sleep(self.config.poll_interval)

    def stop(self) -> None:
        """Shutdown: stop all managed workers.

        Metadata cleanup policy: stop() kills sessions/processes but does NOT
        remove metadata files. Metadata is cleaned up during the next adoption
        pass (stale metadata with dead processes gets removed). This is
        intentional — metadata should outlive the colony process for tmux
        restart adoption to work. Same policy applies in runner.stop().
        """
        self._stopped = True
        for mw in list(self.managed.values()):
            self._pm.stop(mw.name)

    # ------------------------------------------------------------------
    # Core reconciliation
    # ------------------------------------------------------------------

    def _reconcile(self) -> None:
        self._cleanup_exited()
        tasks = self.backend.list_tasks()
        workers = self.backend.list_workers()
        desired = self._compute_desired(tasks, workers)
        actual = self._count_actual()
        for role in ("planner", "builder", "reviewer"):
            self._reconcile_role(role, desired[role], actual.get(role, 0))

    def _compute_desired(
        self, tasks: list[dict], workers: list[dict]
    ) -> dict[str, int]:
        return compute_desired(tasks, workers, self.config)

    @staticmethod
    def _count_scope_groups(tasks: list[dict]) -> int:
        return count_scope_groups(tasks)

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    def _adopt_existing(self) -> None:
        """Adopt workers from a previous colony run."""
        adopted = self._pm.adopt_existing()
        max_n = self._pm.max_counter()
        for name, role in adopted.items():
            worker_id = f"{self.config.node_id}/{name}"
            self.managed[name] = ManagedWorker(name=name, role=role, worker_id=worker_id)
            logger.info("adopted worker %s (role=%s)", name, role)
        if max_n > 0:
            self._counter = max_n

    def _reconcile_role(self, role: str, desired: int, actual: int) -> None:
        delta = desired - actual
        while delta > 0:
            self._start_worker(role)
            delta -= 1
        while delta < 0:
            if not self._stop_idle_worker(role):
                break  # no idle workers to stop this tick
            delta += 1

    def _start_worker(self, role: str) -> None:
        """Spawn a new worker via ProcessManager. Retries once on name collision."""
        for attempt in range(2):
            self._counter += 1
            name = f"{self._prefix}{role}-{self._counter}"
            worker_id = f"{self.config.node_id}/{name}"

            cmd = [
                "antfarm",
                "worker",
                "start",
                "--agent",
                self.config.agent_type,
                "--type",
                role,
                "--node",
                self.config.node_id,
                "--name",
                name,
                "--repo-path",
                self.config.repo_path,
                "--integration-branch",
                self.config.integration_branch,
                "--workspace-root",
                self.config.workspace_root,
                "--colony-url",
                self.config.colony_url,
            ]
            if self.config.token:
                cmd.extend(["--token", self.config.token])

            log_path = os.path.join(self.config.data_dir, "logs", f"autoscaler-{name}.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)

            if self._pm.start(name, cmd, log_path, role=role):
                self.managed[name] = ManagedWorker(name=name, role=role, worker_id=worker_id)
                logger.info("autoscaler started worker name=%s role=%s", name, role)
                _emit("worker_spawned", "", f"role={role} name={name}")
                return
            if attempt == 0:
                logger.info("start failed for %s, retrying with bumped counter", name)
        logger.warning("failed to start worker after retry — skipping")

    def _stop_idle_worker(self, role: str) -> bool:
        """Stop one idle worker of the given role. Returns True if stopped.

        A worker is reap-eligible only when BOTH:
        - colony-side status == "idle", AND
        - last_heartbeat is older than poll_interval

        This grace period protects workers that have just claimed a task via
        pull() but whose heartbeat hasn't ticked yet — the colony-side status
        stays "idle" until the first heartbeat arrives (~30s), and the
        autoscaler's own poll can fire in that window.

        If last_heartbeat is missing or unparseable, the worker is NOT reaped
        (fail-safe: treat as fresh).
        """
        colony_workers = self.backend.list_workers()
        colony_status_map = {w["worker_id"]: w for w in colony_workers}

        for name, mw in list(self.managed.items()):
            if mw.role != role:
                continue
            if not self._pm.is_alive(name):
                continue  # already exited
            cw = colony_status_map.get(mw.worker_id)
            if not (cw and cw.get("status") == "idle"):
                continue
            # Guard: only reap if last_heartbeat is older than poll_interval.
            last_hb = cw.get("last_heartbeat")
            if not last_hb:
                continue  # no heartbeat recorded — treat as fresh, skip
            try:
                hb_dt = datetime.fromisoformat(last_hb)
                age = (datetime.now(UTC) - hb_dt).total_seconds()
            except (ValueError, TypeError):
                continue  # unparseable — fail-safe, skip
            if age < self.config.poll_interval:
                continue  # heartbeat is still fresh — worker just registered or claimed
            self._pm.stop(name)
            logger.info("autoscaler stopped idle worker name=%s role=%s", name, role)
            del self.managed[name]
            _emit("worker_retired", "", f"role={role} name={name}")
            return True
        return False

    def _cleanup_exited(self) -> None:
        """Remove managed workers whose processes have exited."""
        for name in list(self.managed):
            if not self._pm.is_alive(name):
                logger.info("autoscaler cleaned up exited worker name=%s", name)
                self._pm.cleanup(name)
                del self.managed[name]

    def _count_actual(self) -> dict[str, int]:
        """Count running managed workers by role."""
        counts: dict[str, int] = {}
        for mw in self.managed.values():
            if self._pm.is_alive(mw.name):
                counts[mw.role] = counts.get(mw.role, 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Helpers — delegate to module-level standalone functions
    # ------------------------------------------------------------------

    @staticmethod
    def _has_verdict(task: dict) -> bool:
        return has_verdict(task)

    @staticmethod
    def _has_merged_attempt(task: dict) -> bool:
        return has_merged_attempt(task)

    @staticmethod
    def _is_rate_limited(worker: dict) -> bool:
        return is_rate_limited(worker)


# ---------------------------------------------------------------------------
# Multi-node autoscaler
# ---------------------------------------------------------------------------


class MultiNodeAutoscaler:
    """Multi-node autoscaler using actuators and placement.

    Reads queue state from the backend, computes desired worker counts via
    the shared ``compute_desired`` function, then distributes across nodes
    using ``compute_placement`` and pushes desired state via an actuator.
    """

    def __init__(
        self,
        backend: TaskBackend,
        config: AutoscalerConfig,
        actuator: Actuator,
        clock=time.time,
    ):
        self.backend = backend
        self.config = config
        self.actuator = actuator
        self._clock = clock
        self._generation = 0
        self._stopped = False
        self._node_urls: dict[str, str] = {}  # refreshed from backend each cycle

    def run(self) -> None:
        """Main loop: reconcile desired vs actual across nodes every poll interval."""
        while not self._stopped:
            try:
                self._reconcile()
            except Exception as e:
                logger.exception("multi-node autoscaler reconcile failed: %s", e)
            time.sleep(self.config.poll_interval)

    def stop(self) -> None:
        """Signal shutdown."""
        self._stopped = True

    def _reconcile(self) -> None:
        from antfarm.core.placement import compute_placement

        tasks = self.backend.list_tasks()
        workers = self.backend.list_workers()
        nodes = self._get_node_capacities()
        desired_total = compute_desired(tasks, workers, self.config)
        placement = compute_placement(desired_total, nodes)
        self._generation += 1
        for node_id, desired in placement.items():
            runner_url = self._node_urls.get(node_id)
            if not runner_url:
                continue
            try:
                self.actuator.apply(runner_url, desired, self._generation)
            except Exception as e:
                logger.warning("push failed to %s: %s", node_id, e)

    def _get_node_capacities(self) -> list:
        """Read nodes from backend, check reachability. Refreshes _node_urls."""
        from antfarm.core.placement import NodeCapacity

        nodes = self.backend.list_nodes()
        self._node_urls = {}
        capacities: list[NodeCapacity] = []
        for n in nodes:
            runner_url = n.get("runner_url")
            if not runner_url:
                continue
            self._node_urls[n["node_id"]] = runner_url
            reachable = self.actuator.is_reachable(runner_url)
            current = 0
            if reachable:
                try:
                    actual = self.actuator.get_actual(runner_url)
                    current = len(actual.get("workers", {}))
                except Exception:
                    reachable = False
            capacities.append(
                NodeCapacity(
                    node_id=n["node_id"],
                    max_workers=n.get("max_workers", 4),
                    current_workers=current,
                    capabilities=n.get("capabilities", []),
                    reachable=reachable,
                )
            )
        return capacities
