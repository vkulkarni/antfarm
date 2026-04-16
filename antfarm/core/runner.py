"""Runner daemon for Antfarm nodes.

Manages worker subprocesses on a single node via desired-state reconciliation.
Exposes a local HTTP API for the Orchestrator to push desired state and query
actual state. Handles PID file persistence for crash recovery (adopt-on-restart).

Unlike the Autoscaler (which computes desired state locally from queue state),
the Runner is a pure executor: it receives desired state from the Orchestrator
and reconciles toward it.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field

from fastapi import FastAPI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class DesiredState:
    generation: int = 0
    desired: dict[str, int] = field(default_factory=dict)
    drain: list[str] = field(default_factory=list)


@dataclass
class ManagedWorker:
    name: str
    role: str
    pid: int
    process: subprocess.Popen | None = None

    def is_alive(self) -> bool:
        """Check if the managed worker process is still running."""
        if self.process is not None:
            return self.process.poll() is None
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def terminate(self) -> None:
        """Terminate the managed worker process."""
        if self.process is not None:
            self.process.terminate()
        else:
            with suppress(OSError):
                os.kill(self.pid, signal.SIGTERM)


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class DesiredStateRequest(BaseModel):
    generation: int
    desired: dict[str, int] = Field(default_factory=dict)
    drain: list[str] = Field(default_factory=list)


class WorkerInfo(BaseModel):
    name: str
    role: str
    pid: int
    alive: bool


class ActualStateResponse(BaseModel):
    applied_generation: int
    workers: dict[str, dict] = Field(default_factory=dict)
    capacity: dict = Field(default_factory=dict)


class CapacityResponse(BaseModel):
    cpus: int
    max_workers: int
    available: int


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class Runner:
    """Node-level daemon that manages worker subprocesses via desired-state reconciliation."""

    def __init__(
        self,
        node_id: str,
        colony_url: str,
        repo_path: str,
        workspace_root: str,
        integration_branch: str = "main",
        max_workers: int = 4,
        capabilities: list[str] | None = None,
        host: str = "127.0.0.1",
        port: int = 7434,
        agent_type: str = "claude-code",
        token: str | None = None,
        reconcile_interval: float = 15.0,
        fetch_interval: float = 300.0,
        state_dir: str | None = None,
    ):
        self.node_id = node_id
        self.colony_url = colony_url
        self.repo_path = repo_path
        self.workspace_root = workspace_root
        self.integration_branch = integration_branch
        self.max_workers = max_workers
        self.capabilities = capabilities or []
        self.host = host
        self.port = port
        self.agent_type = agent_type
        self.token = token
        self.reconcile_interval = reconcile_interval
        self.fetch_interval = fetch_interval
        self.state_dir = state_dir or os.path.join(workspace_root, ".runner")

        self.managed: dict[str, ManagedWorker] = {}
        self._desired = DesiredState()
        self._applied_generation: int = 0
        self._counter: int = 0
        self._stopped = False
        self._lock = threading.Lock()
        self._colony = None  # ColonyClient, set in run()

    def run(self) -> None:
        """Start the Runner daemon.

        Creates state directories, adopts existing workers from PID files,
        registers with Colony (non-fatal if unreachable), starts background
        threads for reconciliation and git fetch, then starts the HTTP API.
        """
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(os.path.join(self.state_dir, "pids"), exist_ok=True)

        self._adopt_existing_workers()

        # Register node with Colony (non-fatal if unreachable)
        try:
            from antfarm.core.colony_client import ColonyClient

            self._colony = ColonyClient(self.colony_url, token=self.token)
            self._colony.register_node(
                self.node_id,
                runner_url=f"http://{self.host}:{self.port}",
                max_workers=self.max_workers,
                capabilities=self.capabilities,
            )
        except Exception:
            logger.warning("runner: could not register node with colony (non-fatal)")

        # Start background threads
        reconcile_thread = threading.Thread(
            target=self._reconcile_loop, daemon=True, name="runner-reconcile"
        )
        reconcile_thread.start()

        fetch_thread = threading.Thread(
            target=self._git_fetch_loop, daemon=True, name="runner-fetch"
        )
        fetch_thread.start()

        # Start HTTP API
        import uvicorn

        app = self._build_app()
        uvicorn.run(app, host=self.host, port=self.port, log_level="warning")

    def reconcile(self) -> None:
        """Reconcile actual worker state toward desired state.

        Under lock: clean up exited workers, start missing workers,
        stop excess idle workers, restart crashed workers, update
        applied generation.
        """
        with self._lock:
            self._cleanup_exited()

            desired = self._desired.desired
            for role, count in desired.items():
                actual = self._count_role(role)
                while actual < count:
                    self._start_worker(role)
                    actual += 1
                while actual > count:
                    if not self._stop_idle_worker(role):
                        break
                    actual -= 1

            # Handle drain: stop all workers of drained roles
            for role in self._desired.drain:
                for name in list(self.managed):
                    mw = self.managed[name]
                    if mw.role == role and mw.is_alive():
                        if not self._is_worker_idle(name):
                            continue  # don't stop active workers during drain
                        mw.terminate()
                        self._remove_pid_file(name)
                        del self.managed[name]

            self._restart_crashed()
            self._applied_generation = self._desired.generation

    def apply_desired_state(self, state: DesiredState) -> bool:
        """Apply a new desired state. Rejects if generation is stale.

        Returns True if accepted, False if rejected.
        """
        with self._lock:
            if state.generation < self._applied_generation:
                return False
            self._desired = state
            return True

    def get_actual_state(self) -> dict:
        """Build actual state from managed workers."""
        with self._lock:
            workers = {}
            for name, mw in self.managed.items():
                workers[name] = {
                    "name": mw.name,
                    "role": mw.role,
                    "pid": mw.pid,
                    "alive": mw.is_alive(),
                }
            cpus = os.cpu_count() or 1
            alive_count = sum(1 for mw in self.managed.values() if mw.is_alive())
            return {
                "applied_generation": self._applied_generation,
                "workers": workers,
                "capacity": {
                    "cpus": cpus,
                    "max_workers": self.max_workers,
                    "available": max(0, self.max_workers - alive_count),
                },
            }

    def stop(self) -> None:
        """Stop all managed workers and clean up."""
        self._stopped = True
        with self._lock:
            for name, mw in list(self.managed.items()):
                if mw.is_alive():
                    mw.terminate()
                self._remove_pid_file(name)
            self.managed.clear()

    # ------------------------------------------------------------------
    # Worker lifecycle (must be called under _lock)
    # ------------------------------------------------------------------

    def _start_worker(self, role: str) -> None:
        """Spawn a new worker subprocess for the given role."""
        self._counter += 1
        name = f"runner-{role}-{self._counter}"

        cmd = [
            "antfarm",
            "worker",
            "start",
            "--agent",
            self.agent_type,
            "--type",
            role,
            "--node",
            self.node_id,
            "--name",
            name,
            "--repo-path",
            self.repo_path,
            "--integration-branch",
            self.integration_branch,
            "--workspace-root",
            self.workspace_root,
            "--colony-url",
            self.colony_url,
        ]
        if self.token:
            cmd.extend(["--token", self.token])

        log_dir = os.path.join(self.state_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"{name}.log")
        log_file = open(log_path, "a")  # noqa: SIM115

        process = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
        )

        mw = ManagedWorker(name=name, role=role, pid=process.pid, process=process)
        self.managed[name] = mw
        self._write_pid_file(name, process.pid)
        logger.info("runner started worker name=%s role=%s pid=%d", name, role, process.pid)

    def _stop_idle_worker(self, role: str) -> bool:
        """Stop one idle worker of the given role. Returns True if one was stopped.

        Queries Colony for worker idle status. If Colony is unreachable,
        does not stop any workers (safe default).
        """
        if self._colony is None:
            return False

        try:
            colony_workers = self._colony.list_workers()
        except Exception:
            return False  # Colony unreachable — safe default: don't stop

        colony_status_map = {w["worker_id"]: w for w in colony_workers}

        for name, mw in list(self.managed.items()):
            if mw.role != role:
                continue
            if not mw.is_alive():
                continue
            worker_id = f"{self.node_id}/{name}"
            cw = colony_status_map.get(worker_id)
            if cw and cw.get("status") == "idle":
                mw.terminate()
                self._remove_pid_file(name)
                del self.managed[name]
                logger.info("runner stopped idle worker name=%s role=%s", name, role)
                return True
        return False

    def _restart_crashed(self) -> None:
        """Replace crashed workers that are still desired."""
        desired = self._desired.desired
        for name in list(self.managed):
            mw = self.managed[name]
            if not mw.is_alive():
                role = mw.role
                self._remove_pid_file(name)
                del self.managed[name]
                # Only restart if still desired
                if desired.get(role, 0) > self._count_role(role):
                    self._start_worker(role)

    def _cleanup_exited(self) -> None:
        """Remove managed workers whose processes have exited."""
        for name in list(self.managed):
            mw = self.managed[name]
            if not mw.is_alive():
                logger.info("runner cleaned up exited worker name=%s", name)
                self._remove_pid_file(name)
                del self.managed[name]

    def _count_role(self, role: str) -> int:
        """Count alive managed workers of a given role."""
        return sum(1 for mw in self.managed.values() if mw.role == role and mw.is_alive())

    def _is_worker_idle(self, name: str) -> bool:
        """Check if a worker is idle via Colony. Returns True if idle or Colony unreachable."""
        if self._colony is None:
            return False
        try:
            colony_workers = self._colony.list_workers()
        except Exception:
            return False
        worker_id = f"{self.node_id}/{name}"
        for w in colony_workers:
            if w["worker_id"] == worker_id:
                return w.get("status") == "idle"
        return True  # Not found in Colony = treat as idle

    # ------------------------------------------------------------------
    # PID file management
    # ------------------------------------------------------------------

    def _write_pid_file(self, name: str, pid: int) -> None:
        pid_path = os.path.join(self.state_dir, "pids", f"{name}.pid")
        with open(pid_path, "w") as f:
            f.write(str(pid))

    def _remove_pid_file(self, name: str) -> None:
        pid_path = os.path.join(self.state_dir, "pids", f"{name}.pid")
        with suppress(FileNotFoundError):
            os.unlink(pid_path)

    def _adopt_existing_workers(self) -> None:
        """Scan PID files and adopt live processes on restart."""
        pids_dir = os.path.join(self.state_dir, "pids")
        if not os.path.isdir(pids_dir):
            return
        for filename in os.listdir(pids_dir):
            if not filename.endswith(".pid"):
                continue
            name = filename[:-4]  # strip .pid
            pid_path = os.path.join(pids_dir, filename)
            try:
                with open(pid_path) as f:
                    pid = int(f.read().strip())
            except (ValueError, OSError):
                with suppress(FileNotFoundError):
                    os.unlink(pid_path)
                continue

            # Check if process is alive
            try:
                os.kill(pid, 0)
            except OSError:
                # Process dead — clean up stale PID file
                with suppress(FileNotFoundError):
                    os.unlink(pid_path)
                continue

            # Infer role from name: "runner-{role}-{counter}"
            parts = name.split("-")
            role = parts[1] if len(parts) >= 3 else "unknown"

            # Update counter to avoid name collisions
            if len(parts) >= 3:
                with suppress(ValueError):
                    counter_val = int(parts[-1])
                    if counter_val >= self._counter:
                        self._counter = counter_val

            self.managed[name] = ManagedWorker(name=name, role=role, pid=pid)
            logger.info("runner adopted worker name=%s pid=%d", name, pid)

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    def _reconcile_loop(self) -> None:
        """Periodic reconciliation loop."""
        while not self._stopped:
            try:
                self.reconcile()
            except Exception:
                logger.exception("runner reconcile failed")
            time.sleep(self.reconcile_interval)

    def _git_fetch_loop(self) -> None:
        """Periodic git fetch origin."""
        while not self._stopped:
            try:
                subprocess.run(
                    ["git", "fetch", "origin"],
                    cwd=self.repo_path,
                    capture_output=True,
                    timeout=60,
                )
            except Exception:
                logger.warning("runner: git fetch failed (non-fatal)")
            time.sleep(self.fetch_interval)

    # ------------------------------------------------------------------
    # HTTP API
    # ------------------------------------------------------------------

    def _build_app(self) -> FastAPI:
        """Create FastAPI app with Runner endpoints."""
        app = FastAPI(title="Antfarm Runner")

        @app.put("/desired-state")
        def put_desired_state(req: DesiredStateRequest):
            state = DesiredState(
                generation=req.generation,
                desired=req.desired,
                drain=req.drain,
            )
            accepted = self.apply_desired_state(state)
            if not accepted:
                return {"status": "rejected", "reason": "stale generation"}
            return {"status": "ok"}

        @app.get("/actual-state")
        def get_actual_state():
            return self.get_actual_state()

        @app.get("/capacity")
        def get_capacity():
            cpus = os.cpu_count() or 1
            with self._lock:
                alive = sum(1 for mw in self.managed.values() if mw.is_alive())
            return {
                "cpus": cpus,
                "max_workers": self.max_workers,
                "available": max(0, self.max_workers - alive),
            }

        @app.get("/health")
        def health():
            return {"status": "ok", "node_id": self.node_id}

        return app
