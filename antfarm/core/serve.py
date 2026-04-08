"""Colony API server for Antfarm.

FastAPI application exposing the colony's task queue, worker registry, guard locks,
and status over HTTP. Workers use this API to register, forage for tasks, append
trail/signal entries, and harvest completed work.

Dependency injection via get_app(backend, data_dir) supports isolated testing.
Mutation-critical paths (pull, guard, trail, signal) are protected by _lock.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from antfarm.core.backends.base import TaskBackend

logger = logging.getLogger(__name__)

# Module-level state — set by get_app()
_lock = threading.Lock()
_backend: TaskBackend | None = None
_max_attempts: int = 3
_soldier_status: str = "not started"
_soldier_thread: threading.Thread | None = None

# SSE event bus
_event_queue: collections.deque = collections.deque(maxlen=1000)
_event_counter: int = 0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _emit_event(event_type: str, task_id: str, detail: str = "") -> None:
    """Append an event to the SSE event bus."""
    global _event_counter
    _event_counter += 1
    _event_queue.append({
        "id": _event_counter,
        "type": event_type,
        "task_id": task_id,
        "detail": detail,
        "ts": _now_iso(),
    })


def _start_soldier_thread(backend: TaskBackend, data_dir: str) -> None:
    """Start the Soldier as a daemon thread (singleton guard)."""
    global _soldier_thread, _soldier_status

    if _soldier_thread is not None and _soldier_thread.is_alive():
        return  # already running

    from pathlib import Path

    from antfarm.core.soldier import Soldier

    # Soldier needs the git repo root, not the .antfarm data dir.
    # Resolve: if data_dir is inside a git repo, use its parent.
    data_path = Path(data_dir).resolve()
    repo_path = str(data_path.parent) if data_path.name == ".antfarm" else str(data_path)

    soldier = Soldier.from_backend(backend, repo_path=repo_path)

    def _soldier_loop():
        global _soldier_status
        _soldier_status = "running"
        try:
            soldier.run()
        except Exception as e:
            _soldier_status = f"error: {e}"

    _soldier_thread = threading.Thread(target=_soldier_loop, daemon=True, name="soldier")
    _soldier_thread.start()


_doctor_thread: threading.Thread | None = None
_doctor_status: str = "not started"


def _start_doctor_thread(
    backend: TaskBackend, data_dir: str, interval: float = 300.0
) -> None:
    """Start the Doctor as a daemon thread (singleton guard)."""
    global _doctor_thread, _doctor_status

    if _doctor_thread is not None and _doctor_thread.is_alive():
        return

    from antfarm.core.doctor import run_doctor

    # Load doctor config from colony config, with sensible defaults
    doctor_config: dict = {"data_dir": data_dir, "worker_ttl": 300, "guard_ttl": 300}
    config_path = os.path.join(data_dir, "config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            colony_cfg = json.load(f)
        doctor_config["worker_ttl"] = colony_cfg.get("worker_ttl", 300)
        doctor_config["guard_ttl"] = colony_cfg.get("guard_ttl", 300)
        interval = colony_cfg.get("doctor_interval", interval)

    def _doctor_loop():
        global _doctor_status
        _doctor_status = "running"
        try:
            while True:
                time.sleep(interval)
                try:
                    findings = run_doctor(backend, doctor_config, fix=True)
                    for f in findings:
                        if f.severity == "error":
                            logger.warning("doctor: %s", f.message)
                except Exception as e:
                    logger.error("doctor daemon check failed: %s", e)
        except Exception as e:
            _doctor_status = f"error: {e}"

    _doctor_thread = threading.Thread(
        target=_doctor_loop, daemon=True, name="doctor"
    )
    _doctor_thread.start()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CarryRequest(BaseModel):
    id: str
    title: str
    spec: str
    complexity: str = "M"
    priority: int = 10
    depends_on: list[str] = []
    touches: list[str] = []
    capabilities_required: list[str] = []
    created_by: str = "api"
    spawned_by: dict | None = None


class NodeRequest(BaseModel):
    node_id: str


class WorkerRegisterRequest(BaseModel):
    worker_id: str
    node_id: str
    agent_type: str
    workspace_root: str
    capabilities: list[str] = []


class HeartbeatRequest(BaseModel):
    status: dict | None = None
    remaining: int | None = None
    reset_at: str | None = None
    cooldown_until: str | None = None


class PullRequest(BaseModel):
    worker_id: str


class TrailRequest(BaseModel):
    worker_id: str
    message: str


class SignalRequest(BaseModel):
    worker_id: str
    message: str


class HarvestPendingRequest(BaseModel):
    attempt_id: str


class HarvestRequest(BaseModel):
    attempt_id: str
    pr: str
    branch: str
    artifact: dict | None = None


class KickbackRequest(BaseModel):
    reason: str
    max_attempts: int | None = None


class ReviewVerdictRequest(BaseModel):
    attempt_id: str
    verdict: dict


class MergeRequest(BaseModel):
    attempt_id: str


class ReassignRequest(BaseModel):
    worker_id: str


class BlockRequest(BaseModel):
    reason: str


class PinRequest(BaseModel):
    worker_id: str


class GuardRequest(BaseModel):
    owner: str


class OverrideOrderRequest(BaseModel):
    position: int


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def get_app(
    backend: TaskBackend | None = None,
    data_dir: str = ".antfarm",
    auth_secret: str | None = None,
    enable_soldier: bool = False,
    enable_doctor: bool = False,
) -> FastAPI:
    """Create and return the FastAPI application.

    Args:
        backend: TaskBackend instance to use. If None, a FileBackend rooted at
                 data_dir is created automatically.
        data_dir: Path to .antfarm directory (only used when backend is None).
        auth_secret: Optional shared secret for bearer token auth. When set,
                     all endpoints except GET /status require a valid token.
        enable_soldier: If True (default), start the Soldier merge engine as a
                        daemon thread.
        enable_doctor: If True, start the Doctor health-check daemon thread.

    Returns:
        Configured FastAPI application.
    """
    global _backend, _max_attempts

    if backend is not None:
        _backend = backend
    else:
        from antfarm.core.backends.file import FileBackend

        _backend = FileBackend(root=data_dir)

    # Load max_attempts default from config
    config_path = os.path.join(data_dir, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            _max_attempts = cfg.get("max_attempts", 3)
        except (json.JSONDecodeError, OSError):
            pass

    app = FastAPI(title="Antfarm Colony")

    if auth_secret:
        from antfarm.core.auth import create_auth_middleware

        app.middleware("http")(create_auth_middleware(auth_secret))

    if enable_soldier:
        _start_soldier_thread(_backend, data_dir)

    if enable_doctor:
        _start_doctor_thread(_backend, data_dir)

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    @app.post("/nodes", status_code=200)
    def register_node(req: NodeRequest):
        """Register a node. Idempotent — re-registering updates last_seen."""
        now = _now_iso()
        node = {"node_id": req.node_id, "joined_at": now, "last_seen": now}
        _backend.register_node(node)
        return {"node_id": req.node_id}

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    @app.post("/workers/register", status_code=201)
    def register_worker(req: WorkerRegisterRequest):
        """Register a worker. Returns 409 if a live worker with the same ID exists."""
        now = _now_iso()
        worker = {
            "worker_id": req.worker_id,
            "node_id": req.node_id,
            "agent_type": req.agent_type,
            "workspace_root": req.workspace_root,
            "capabilities": req.capabilities,
            "status": "idle",
            "registered_at": now,
            "last_heartbeat": now,
        }
        try:
            _backend.register_worker(worker)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"worker_id": req.worker_id}

    @app.post("/workers/{worker_id:path}/heartbeat", status_code=200)
    def worker_heartbeat(worker_id: str, req: HeartbeatRequest):
        """Update worker heartbeat. Accepts optional status dict and rate limit fields."""
        update: dict = dict(req.status or {})
        if req.remaining is not None:
            update["remaining"] = req.remaining
        if req.reset_at is not None:
            update["reset_at"] = req.reset_at
        # Always persist cooldown_until (including None to clear it)
        if req.cooldown_until is not None or "cooldown_until" not in (req.status or {}):
            update["cooldown_until"] = req.cooldown_until
        _backend.heartbeat(worker_id, update)
        return {"ok": True}

    @app.get("/workers", status_code=200)
    def list_workers():
        """List all registered workers with their status and rate limit state."""
        return _backend.list_workers()

    @app.delete("/workers/{worker_id:path}", status_code=200)
    def deregister_worker(worker_id: str):
        """Deregister a worker. Idempotent — unknown worker returns 200."""
        _backend.deregister_worker(worker_id)
        return {"ok": True}

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    @app.post("/tasks", status_code=201)
    def carry_task(req: CarryRequest):
        """Carry (enqueue) a task. Returns 409 if a task with this ID already exists."""
        now = _now_iso()
        task = {
            "id": req.id,
            "title": req.title,
            "spec": req.spec,
            "complexity": req.complexity,
            "priority": req.priority,
            "depends_on": req.depends_on,
            "touches": req.touches,
            "capabilities_required": req.capabilities_required,
            "created_by": req.created_by,
            "status": "ready",
            "current_attempt": None,
            "attempts": [],
            "trail": [],
            "signals": [],
            "created_at": now,
            "updated_at": now,
        }
        if req.spawned_by:
            task["spawned_by"] = req.spawned_by
        try:
            task_id = _backend.carry(task)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # Check for overlap warnings with active tasks
        warnings: list[str] = []
        if req.touches:
            try:
                from antfarm.core.memory import MemoryStore

                resolved_dir = str(_backend._root) if hasattr(_backend, "_root") else data_dir
                active = _backend.list_tasks(status="active")
                memory = MemoryStore(resolved_dir)
                warnings = memory.check_overlap_warnings(req.touches, active)
            except Exception:
                pass

        result: dict = {"task_id": task_id}
        if warnings:
            result["warnings"] = warnings
        return result

    @app.post("/tasks/pull")
    def forage(req: PullRequest, response: Response):
        """Pull the next eligible task for a worker. Returns 204 if queue is empty."""
        with _lock:
            task = _backend.pull(req.worker_id)
        if task is None:
            response.status_code = 204
            return None
        return task

    @app.post("/tasks/{task_id}/trail", status_code=200)
    def append_trail(task_id: str, req: TrailRequest):
        """Append a trail entry to a task (read-modify-write under lock)."""
        entry = {"ts": _now_iso(), "worker_id": req.worker_id, "message": req.message}
        with _lock:
            try:
                _backend.append_trail(task_id, entry)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/tasks/{task_id}/signal", status_code=200)
    def append_signal(task_id: str, req: SignalRequest):
        """Append a signal entry to a task (read-modify-write under lock)."""
        entry = {"ts": _now_iso(), "worker_id": req.worker_id, "message": req.message}
        with _lock:
            try:
                _backend.append_signal(task_id, entry)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/tasks/{task_id}/harvest", status_code=200)
    def harvest_task(task_id: str, req: HarvestRequest):
        """Mark a task as harvested (done). Idempotent for same attempt.

        Returns 409 for wrong attempt.
        """
        try:
            _backend.mark_harvested(
                task_id, req.attempt_id, req.pr, req.branch, artifact=req.artifact
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _emit_event("harvested", task_id, f"pr={req.pr} branch={req.branch}")
        return {"ok": True}

    @app.post("/tasks/{task_id}/harvest-pending", status_code=200)
    def mark_harvest_pending(task_id: str, req: HarvestPendingRequest):
        """Transition task to HARVEST_PENDING before writing artifact/failure."""
        try:
            _backend.mark_harvest_pending(task_id, req.attempt_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/tasks/{task_id}/kickback", status_code=200)
    def kickback_task(task_id: str, req: KickbackRequest):
        """Return a task to ready (or blocked if max attempts exhausted)."""
        effective = (
            req.max_attempts
            if req.max_attempts is not None
            else _max_attempts
        )
        try:
            _backend.kickback(
                task_id, req.reason, max_attempts=effective
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _emit_event("kickback", task_id, req.reason)
        return {"ok": True}

    @app.post("/tasks/{task_id}/review-verdict", status_code=200)
    def store_review_verdict(task_id: str, req: ReviewVerdictRequest):
        """Store a ReviewVerdict on the task's current attempt."""
        try:
            _backend.store_review_verdict(task_id, req.attempt_id, req.verdict)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/tasks/{task_id}/merge", status_code=200)
    def merge_task(task_id: str, req: MergeRequest):
        """Mark a task attempt as merged. Soldier only."""
        try:
            _backend.mark_merged(task_id, req.attempt_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _emit_event("merged", task_id, f"attempt={req.attempt_id}")
        return {"ok": True}

    @app.post("/tasks/{task_id}/pause", status_code=200)
    def pause_task(task_id: str):
        """Pause an active task."""
        try:
            _backend.pause_task(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/tasks/{task_id}/resume", status_code=200)
    def resume_task(task_id: str):
        """Resume a paused task."""
        try:
            _backend.resume_task(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/tasks/{task_id}/reassign", status_code=200)
    def reassign_task(task_id: str, req: ReassignRequest):
        """Reassign an active task to a different worker."""
        try:
            _backend.reassign_task(task_id, req.worker_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/tasks/{task_id}/block", status_code=200)
    def block_task(task_id: str, req: BlockRequest):
        """Block a ready task with a reason."""
        try:
            _backend.block_task(task_id, req.reason)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/tasks/{task_id}/unblock", status_code=200)
    def unblock_task(task_id: str):
        """Unblock a blocked task."""
        try:
            _backend.unblock_task(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/tasks/{task_id}/pin", status_code=200)
    def pin_task(task_id: str, req: PinRequest):
        """Pin a ready task to a specific worker."""
        try:
            _backend.pin_task(task_id, req.worker_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/tasks/{task_id}/unpin", status_code=200)
    def unpin_task(task_id: str):
        """Clear the pin on a ready task."""
        try:
            _backend.unpin_task(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/tasks/{task_id}/override-order", status_code=200)
    def override_order(task_id: str, req: OverrideOrderRequest):
        """Set merge queue position override on a done task."""
        try:
            _backend.override_merge_order(task_id, req.position)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.delete("/tasks/{task_id}/override-order", status_code=200)
    def clear_override_order(task_id: str):
        """Clear merge queue position override on a done task."""
        try:
            _backend.clear_merge_override(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/tasks/count", status_code=200)
    def task_count():
        """Return task counts by status."""
        return _backend.status()["tasks"]

    @app.get("/tasks", status_code=200)
    def list_tasks(status: str | None = Query(default=None)):
        """List tasks with optional ?status= filter."""
        return _backend.list_tasks(status=status)

    @app.get("/tasks/{task_id}", status_code=200)
    def get_task(task_id: str):
        """Get a task by ID. Returns 404 if not found."""
        task = _backend.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
        return task

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    @app.post("/guards/{resource:path}", status_code=200)
    def acquire_guard(resource: str, req: GuardRequest):
        """Try to acquire an exclusive guard on a resource."""
        with _lock:
            acquired = _backend.guard(resource, req.owner)
        return {"acquired": acquired}

    @app.delete("/guards/{resource:path}", status_code=200)
    def release_guard(resource: str, owner: str = Query(...)):
        """Release a guard. Only the owner can release. Returns 403 on mismatch."""
        try:
            _backend.release_guard(resource, owner)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    # ------------------------------------------------------------------
    # Scent (SSE trail streaming)
    # ------------------------------------------------------------------

    @app.get("/scent/{task_id}")
    def scent_task(
        task_id: str,
        poll_interval: float = Query(default=1.0),
        timeout: float = Query(default=0.0),
    ):
        """Stream trail entries for a task as Server-Sent Events.

        Args:
            task_id: Task to stream trail for.
            poll_interval: Seconds between backend polls.
            timeout: If > 0, stop streaming after this many seconds.
        """
        task = _backend.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

        def _generate():
            seen = 0
            start = time.monotonic()
            try:
                while True:
                    if timeout > 0 and (time.monotonic() - start) >= timeout:
                        break
                    current = _backend.get_task(task_id)
                    if current is not None:
                        trail = current.get("trail", [])
                        for entry in trail[seen:]:
                            yield f"data: {json.dumps(entry)}\n\n"
                        seen = len(trail)
                    time.sleep(poll_interval)
            except GeneratorExit:
                pass

        return StreamingResponse(_generate(), media_type="text/event-stream")

    # ------------------------------------------------------------------
    # SSE event stream
    # ------------------------------------------------------------------

    @app.get("/events")
    def event_stream(
        after: int = Query(default=0, description="Cursor: return events with id > after"),
        timeout: float = Query(default=30.0, description="Max seconds to hold connection"),
    ):
        """Stream colony events (harvest, kickback, merge) as SSE."""

        def _generate():
            cursor = after
            start = time.monotonic()
            try:
                while True:
                    elapsed = time.monotonic() - start
                    if elapsed >= timeout:
                        break
                    for event in list(_event_queue):
                        if event["id"] > cursor:
                            cursor = event["id"]
                            yield f"data: {json.dumps(event)}\n\n"
                    time.sleep(0.5)
            except GeneratorExit:
                pass

        return StreamingResponse(_generate(), media_type="text/event-stream")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @app.get("/status", status_code=200)
    def colony_status():
        """Return colony status summary."""
        result = _backend.status()
        result["soldier"] = _soldier_status
        result["doctor"] = _doctor_status
        return result

    @app.get("/status/full", status_code=200)
    def colony_status_full():
        """Return colony status, all tasks, and all workers in one call.

        Reduces polling overhead for TUI clients that need all three.

        Returns:
            Dict with 'status', 'tasks', and 'workers' keys.
        """
        return {
            "status": _backend.status(),
            "tasks": _backend.list_tasks(),
            "workers": _backend.list_workers(),
            "soldier": _soldier_status,
            "doctor": _doctor_status,
        }

    # ------------------------------------------------------------------
    # Backup status
    # ------------------------------------------------------------------

    @app.get("/backup/status", status_code=200)
    def backup_status():
        """Return last backup result from backup_status.json.

        Returns 404 if no backup has been run yet.
        """
        import os

        status_path = os.path.join(data_dir, "backup_status.json")
        if not os.path.exists(status_path):
            raise HTTPException(
                status_code=404, detail="No backup status found. Run a backup first."
            )
        with open(status_path) as f:
            return json.load(f)

    return app
