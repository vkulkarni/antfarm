"""Colony API server for Antfarm.

FastAPI application exposing the colony's task queue, worker registry, guard locks,
and status over HTTP. Workers use this API to register, forage for tasks, append
trail/signal entries, and harvest completed work.

Dependency injection via get_app(backend, data_dir) supports isolated testing.
Mutation-critical paths (pull, guard, trail, signal) are protected by _lock.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel

from antfarm.core.backends.base import TaskBackend

# Module-level state — set by get_app()
_lock = threading.Lock()
_backend: TaskBackend | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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


class PullRequest(BaseModel):
    worker_id: str


class TrailRequest(BaseModel):
    worker_id: str
    message: str


class SignalRequest(BaseModel):
    worker_id: str
    message: str


class HarvestRequest(BaseModel):
    attempt_id: str
    pr: str
    branch: str


class KickbackRequest(BaseModel):
    reason: str


class MergeRequest(BaseModel):
    attempt_id: str


class ReassignRequest(BaseModel):
    worker_id: str


class BlockRequest(BaseModel):
    reason: str


class GuardRequest(BaseModel):
    owner: str


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def get_app(
    backend: TaskBackend | None = None,
    data_dir: str = ".antfarm",
    auth_secret: str | None = None,
) -> FastAPI:
    """Create and return the FastAPI application.

    Args:
        backend: TaskBackend instance to use. If None, a FileBackend rooted at
                 data_dir is created automatically.
        data_dir: Path to .antfarm directory (only used when backend is None).
        auth_secret: Optional shared secret for bearer token auth. When set,
                     all endpoints except GET /status require a valid token.

    Returns:
        Configured FastAPI application.
    """
    global _backend

    if backend is not None:
        _backend = backend
    else:
        from antfarm.core.backends.file import FileBackend

        _backend = FileBackend(root=data_dir)

    app = FastAPI(title="Antfarm Colony")

    if auth_secret:
        from antfarm.core.auth import create_auth_middleware

        app.middleware("http")(create_auth_middleware(auth_secret))

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
        """Update worker heartbeat. Accepts optional status dict."""
        _backend.heartbeat(worker_id, req.status or {})
        return {"ok": True}

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
        try:
            task_id = _backend.carry(task)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"task_id": task_id}

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
            _backend.mark_harvested(task_id, req.attempt_id, req.pr, req.branch)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/tasks/{task_id}/kickback", status_code=200)
    def kickback_task(task_id: str, req: KickbackRequest):
        """Return a task to ready state with the given reason."""
        try:
            _backend.kickback(task_id, req.reason)
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
    # Status
    # ------------------------------------------------------------------

    @app.get("/status", status_code=200)
    def colony_status():
        """Return colony status summary."""
        return _backend.status()

    return app
