"""Single-host autoscaler daemon for Antfarm.

Starts and stops worker subprocesses based on queue state. Opt-in via
``antfarm colony --autoscaler``. Manages its own workers only — manually
started workers on other machines are untouched.

Scope-aware: groups ready build tasks by ``touches`` overlap and caps
builder count to the number of non-overlapping scope groups, preventing
over-allocation to a single scope.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from antfarm.core.backends.base import TaskBackend

logger = logging.getLogger(__name__)


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
    process: subprocess.Popen


class Autoscaler:
    """Single-host autoscaler that spawns/stops worker subprocesses."""

    def __init__(
        self,
        backend: TaskBackend,
        config: AutoscalerConfig,
        clock=time.time,
    ):
        self.backend = backend
        self.config = config
        self._clock = clock
        self.managed: dict[str, ManagedWorker] = {}
        self._stopped = False
        self._counter = 0

    def run(self) -> None:
        """Main loop: reconcile desired vs actual workers every poll interval."""
        while not self._stopped:
            try:
                self._reconcile()
            except Exception as e:
                logger.exception("autoscaler reconcile failed: %s", e)
            time.sleep(self.config.poll_interval)

    def stop(self) -> None:
        """Signal shutdown and terminate all managed workers."""
        self._stopped = True
        for mw in list(self.managed.values()):
            if mw.process.poll() is None:
                mw.process.terminate()

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
            and not self._has_verdict(t)
            and not self._has_merged_attempt(t)
        ]

        scope_groups = self._count_scope_groups(ready_build)

        active_builders = [
            w
            for w in workers
            if "review" not in w.get("capabilities", [])
            and "plan" not in w.get("capabilities", [])
            and w.get("status") != "offline"
        ]
        rate_limited = [w for w in active_builders if self._is_rate_limited(w)]

        desired_builders = min(
            scope_groups,
            self.config.max_builders,
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
                self.config.max_reviewers,
            ),
        }

    @staticmethod
    def _count_scope_groups(tasks: list[dict]) -> int:
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

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

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
        """Spawn a new worker subprocess for the given role."""
        self._counter += 1
        name = f"auto-{role}-{self._counter}"
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

        # Log to .antfarm/logs/autoscaler-{name}.log
        log_dir = os.path.join(self.config.data_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"autoscaler-{name}.log")
        log_file = open(log_path, "a")  # noqa: SIM115

        process = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
        )

        self.managed[name] = ManagedWorker(
            name=name,
            role=role,
            worker_id=worker_id,
            process=process,
        )
        logger.info("autoscaler started worker name=%s role=%s pid=%d", name, role, process.pid)

    def _stop_idle_worker(self, role: str) -> bool:
        """Stop one idle worker of the given role. Returns True if stopped."""
        colony_workers = self.backend.list_workers()
        colony_status_map = {w["worker_id"]: w for w in colony_workers}

        for name, mw in list(self.managed.items()):
            if mw.role != role:
                continue
            if mw.process.poll() is not None:
                continue  # already exited
            cw = colony_status_map.get(mw.worker_id)
            if cw and cw.get("status") == "idle":
                mw.process.terminate()
                logger.info("autoscaler stopped idle worker name=%s role=%s", name, role)
                del self.managed[name]
                return True
        return False

    def _cleanup_exited(self) -> None:
        """Remove managed workers whose processes have exited."""
        for name in list(self.managed):
            mw = self.managed[name]
            if mw.process.poll() is not None:
                logger.info(
                    "autoscaler cleaned up exited worker name=%s exit=%d",
                    name,
                    mw.process.returncode,
                )
                del self.managed[name]

    def _count_actual(self) -> dict[str, int]:
        """Count running managed workers by role."""
        counts: dict[str, int] = {}
        for mw in self.managed.values():
            if mw.process.poll() is None:  # still running
                counts[mw.role] = counts.get(mw.role, 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _has_verdict(task: dict) -> bool:
        """Check if a task's current attempt has a review verdict."""
        for att in task.get("attempts", []):
            if att.get("attempt_id") == task.get("current_attempt"):
                return bool(att.get("review_verdict"))
        return False

    @staticmethod
    def _has_merged_attempt(task: dict) -> bool:
        """Check if a task's current attempt has been merged."""
        for att in task.get("attempts", []):
            if att.get("attempt_id") == task.get("current_attempt"):
                return att.get("status") == "merged"
        return False

    @staticmethod
    def _is_rate_limited(worker: dict) -> bool:
        """Check if a worker is currently rate-limited."""
        cooldown = worker.get("cooldown_until")
        if not cooldown:
            return False
        try:
            cooldown_dt = datetime.fromisoformat(cooldown)
            return cooldown_dt > datetime.now(UTC)
        except (ValueError, TypeError):
            return False
