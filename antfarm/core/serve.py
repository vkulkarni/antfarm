"""Colony API server for Antfarm.

FastAPI application exposing the colony's task queue, worker registry, guard locks,
and status over HTTP. Workers use this API to register, forage for tasks, append
trail/signal entries, and harvest completed work.

Dependency injection via get_app(backend, data_dir) supports isolated testing.
Mutation-critical paths (pull, guard, trail, signal) are protected by _lock.
"""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from antfarm.core import activity
from antfarm.core.backends.base import TaskBackend

logger = logging.getLogger(__name__)

# Module-level state — set by get_app()
_lock = threading.Lock()
_backend: TaskBackend | None = None
_max_attempts: int = 3
_soldier_status: str = "not started"
_soldier_thread: threading.Thread | None = None
_queen_thread: threading.Thread | None = None
_queen_status: str = "not started"

_autoscaler_thread: threading.Thread | None = None
_autoscaler_status: str = "not started"
_autoscaler_instance = None

# Activity state for the soldier and doctor (synthetic TUI rows — #348).
# These live on the server (not in the backend) because the soldier/doctor run
# in-process as threads alongside the FastAPI app. The TUI surfaces them via
# /status/full. Appends are guarded by _colony_activity_lock so readers never
# observe a partially-mutated dict.
_colony_activity_lock = threading.Lock()
_soldier_activity: dict | None = None
_doctor_activity: dict | None = None

# SSE event bus
_event_queue: collections.deque = collections.deque(maxlen=1000)
_event_counter: int = 0
# Serializes _event_counter increment + _event_queue append. Kept separate
# from _lock so task-state mutations don't block event emission and vice
# versa. See #309.
_event_lock = threading.Lock()
# UUID regenerated every time get_app() runs. Tags every emitted event so the
# TUI can detect a colony restart and reset its cursor — see #306.
_server_epoch: str = str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _set_colony_activity(kind: str, action: str, target: str = "") -> None:
    """Update the soldier/doctor synthetic activity rows (#348).

    Intended to be called from ``antfarm.core.soldier`` and
    ``antfarm.core.doctor`` via a lazy import to avoid circular imports at
    module-load time (serve.py imports soldier/doctor indirectly).

    Best-effort: any failure is swallowed so merge/check logic never breaks
    because the TUI sidecar is unhappy.

    Args:
        kind: "soldier" or "doctor". Unknown kinds are ignored.
        action: Canonical verb (see ``activity.VERB_TEMPLATES``).
        target: Optional target; truncated by the synthesizer.
    """
    try:
        text = activity.synthesize_text(action, target)
        since = _now_iso()
        with _colony_activity_lock:
            if kind == "soldier":
                global _soldier_activity
                _soldier_activity = {
                    "action": action, "target": target, "text": text, "since": since
                }
            elif kind == "doctor":
                global _doctor_activity
                _doctor_activity = {
                    "action": action, "target": target, "text": text, "since": since
                }
    except Exception:
        logger.debug("_set_colony_activity(%s) failed", kind, exc_info=True)


def _emit_event(event_type: str, task_id: str, detail: str = "", actor: str = "colony") -> None:
    """Append an event to the SSE event bus.

    Thread-safe: counter increment and queue append are performed under
    _event_lock as one atomic step so ids are monotonic and unique under
    concurrent emits. See #309.

    Args:
        event_type: Event kind (e.g. "harvested", "kickback", "merged").
        task_id: Task identifier the event relates to. Empty string if not task-scoped.
        detail: Free-form human-readable detail.
        actor: Subsystem emitting the event (e.g. "colony", "queen", "autoscaler",
            "soldier", "doctor", "worker"). Defaults to "colony" for legacy emitters
            inside colony HTTP handlers.
    """
    global _event_counter
    with _event_lock:
        _event_counter += 1
        _event_queue.append(
            {
                "id": _event_counter,
                "epoch": _server_epoch,
                "actor": actor,
                "type": event_type,
                "task_id": task_id,
                "detail": detail,
                "ts": _now_iso(),
            }
        )


def _warn_if_data_dir_not_gitignored(repo_path: str, data_dir_name: str) -> None:
    """Warn operator if target repo's .gitignore doesn't list the colony data dir.

    Naive matcher — line-scan for literal variants. False negatives are benign
    (the -e flag on git clean still protects state); false positives just skip
    an unneeded warning.
    """
    from pathlib import Path

    gitignore = Path(repo_path) / ".gitignore"
    if not gitignore.exists():
        has_entry = False
    else:
        try:
            lines = gitignore.read_text().splitlines()
        except OSError:
            return  # unreadable — skip
        stripped = {
            line.strip().lstrip("/").rstrip("/")
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        }
        has_entry = data_dir_name.lstrip("/").rstrip("/") in stripped
    if not has_entry:
        logger.warning(
            "target repo %s lacks %s in .gitignore — soldier's git clean "
            "now excludes it via -e, but consider adding the entry for "
            "belt-and-suspenders.",
            repo_path,
            data_dir_name,
        )
        _emit_event(
            "data_dir_not_gitignored",
            "",
            f"repo={repo_path} data_dir={data_dir_name}",
            actor="soldier",
        )


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
    data_dir_name = data_path.name  # e.g. ".antfarm"

    _warn_if_data_dir_not_gitignored(repo_path, data_dir_name)

    # Explicit require_review=True guards against silent regressions like
    # issue #284 — if the Soldier default ever changes, production still
    # runs with review enabled.
    soldier = Soldier.from_backend(
        backend,
        repo_path=repo_path,
        require_review=True,
        data_dir_name=data_dir_name,
    )

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


def _start_doctor_thread(backend: TaskBackend, data_dir: str, interval: float = 300.0) -> None:
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
        doctor_config["max_reviewers"] = colony_cfg.get("max_reviewers", 2)
        doctor_config["max_builders"] = colony_cfg.get("max_builders", 4)
        doctor_config["worktree_prune_ttl_days"] = colony_cfg.get("worktree_prune_ttl_days", 7)
        doctor_config["worktree_prune_merged_min_age_hours"] = colony_cfg.get(
            "worktree_prune_merged_min_age_hours", 24
        )
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

    _doctor_thread = threading.Thread(target=_doctor_loop, daemon=True, name="doctor")
    _doctor_thread.start()


def _start_queen_thread(
    backend: TaskBackend,
    data_dir: str = ".antfarm",
    repo_path: str = ".",
    integration_branch: str = "main",
) -> None:
    """Start the Queen as a daemon thread (singleton guard)."""
    global _queen_thread, _queen_status

    if _queen_thread is not None and _queen_thread.is_alive():
        return

    from antfarm.core.queen import Queen

    queen = Queen(
        backend,
        data_dir=data_dir,
        repo_path=repo_path,
        integration_branch=integration_branch,
    )

    def _queen_loop():
        global _queen_status
        _queen_status = "running"
        try:
            queen.run()
        except Exception as e:
            _queen_status = f"error: {e}"
            logger.error("queen thread crashed: %s", e)

    _queen_thread = threading.Thread(target=_queen_loop, daemon=True, name="queen")
    _queen_thread.start()


def _start_autoscaler_thread(
    backend: TaskBackend,
    autoscaler_config,
) -> None:
    """Start the Autoscaler as a daemon thread (singleton guard)."""
    global _autoscaler_thread, _autoscaler_status, _autoscaler_instance

    if _autoscaler_thread is not None and _autoscaler_thread.is_alive():
        return

    import shutil

    from antfarm.core.autoscaler import Autoscaler

    if not shutil.which("tmux"):
        logger.warning(
            "tmux not available — using subprocess fallback (less reliable, no restart adoption)"
        )

    _autoscaler_instance = Autoscaler(backend, autoscaler_config)

    def _autoscaler_loop():
        global _autoscaler_status
        _autoscaler_status = "running"
        try:
            _autoscaler_instance.run()
        except Exception as e:
            _autoscaler_status = f"error: {e}"
            logger.error("autoscaler thread crashed: %s", e)

    _autoscaler_thread = threading.Thread(target=_autoscaler_loop, daemon=True, name="autoscaler")
    _autoscaler_thread.start()


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
    mission_id: str | None = None


class NodeRequest(BaseModel):
    node_id: str
    runner_url: str | None = None
    max_workers: int = 4
    capabilities: list[str] = Field(default_factory=list)


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


class ActivityRequest(BaseModel):
    action: str | None = None
    target: str | None = None
    source: str | None = None  # "hook" | "soldier" | "doctor"
    text: str | None = None  # caller-provided; server synthesizes if absent


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
    auto_merged: bool = False


class RereviewRequest(BaseModel):
    spec: str
    touches: list[str] = []


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


class MissionCreateRequest(BaseModel):
    mission_id: str | None = None
    spec: str
    spec_file: str | None = None
    config: dict | None = None


class MissionUpdateRequest(BaseModel):
    updates: dict


class WorkerUsageRequest(BaseModel):
    """Usage/cost ping from a worker hook — mirrors UsageEvent (minus cost)."""

    event_id: str
    ts: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    source: str = "claude_stop_hook"
    # Optional overrides — if omitted, the server derives from the worker's
    # active task. Supplied by tests and by future non-Claude adapters.
    task_id: str | None = None
    attempt_id: str | None = None
    mission_id: str | None = None


class MissionExtendRequest(BaseModel):
    additional_usd: float | None = None
    additional_tokens: int | None = None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def get_app(
    backend: TaskBackend | None = None,
    data_dir: str = ".antfarm",
    auth_secret: str | None = None,
    enable_soldier: bool = False,
    enable_doctor: bool = False,
    enable_queen: bool = True,
    autoscaler_config=None,
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
        enable_queen: If True (default), start the Queen mission controller as
                      a daemon thread. Requires FileBackend — skips for GitHubBackend.
        autoscaler_config: Optional AutoscalerConfig. When provided and
                           ``enabled=True``, starts the Autoscaler daemon thread.

    Returns:
        Configured FastAPI application.
    """
    global _backend, _max_attempts, _server_epoch

    # Fresh epoch per app instance — ensures tests and real colony restarts
    # both present a new server identity to SSE clients (#306).
    _server_epoch = str(uuid.uuid4())

    # Ensure a persisted colony id exists BEFORE any other code (including
    # the config.json read below) touches the file. colony_id() serializes
    # its own read-modify-write, so calling it first guarantees subsequent
    # readers see a consistent config.json with the colony_id key present.
    if os.path.isdir(data_dir):
        from antfarm.core.process_manager import colony_id as _ensure_colony_id

        _ensure_colony_id(data_dir)

    # Load config up front so backend construction can see repo_path.
    repo_path: str | None = None
    integration_branch = "main"
    config_path = os.path.join(data_dir, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            _max_attempts = cfg.get("max_attempts", 3)
            repo_path = cfg.get("repo_path") or None
            integration_branch = cfg.get("integration_branch", integration_branch)
        except (json.JSONDecodeError, OSError):
            pass

    if backend is not None:
        _backend = backend
    else:
        from antfarm.core.backends.file import FileBackend
        from antfarm.core.pr_ops import GhPROps, NullPROps

        pr_ops = GhPROps(cwd=repo_path) if repo_path else NullPROps()
        _backend = FileBackend(root=data_dir, pr_ops=pr_ops)

    # Fallback so downstream uses (Queen, etc.) still see a repo_path string.
    if repo_path is None:
        repo_path = "."

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(application: FastAPI):  # noqa: ARG001
        # Log colony hash once per server startup so operators can correlate
        # tmux session names (auto-<hash>-*, runner-<hash>-*) to this data_dir.
        # Fires only when uvicorn actually starts — not on every get_app() call,
        # which would spam test suite logs.
        try:
            from antfarm.core.process_manager import colony_id, colony_session_hash

            resolved = os.path.realpath(data_dir)
            logger.info(
                "colony id: %s hash: %s (data_dir: %s)",
                colony_id(data_dir),
                colony_session_hash(data_dir),
                resolved,
            )
        except ImportError:
            pass
        yield

    app = FastAPI(title="Antfarm Colony", lifespan=_lifespan)

    if auth_secret:
        from antfarm.core.auth import create_auth_middleware

        app.middleware("http")(create_auth_middleware(auth_secret))

    if enable_soldier:
        _start_soldier_thread(_backend, data_dir)

    if enable_doctor:
        _start_doctor_thread(_backend, data_dir)

    if enable_queen:
        from antfarm.core.backends.github import GitHubBackend

        if isinstance(_backend, GitHubBackend):
            logger.info("queen: skipping — GitHubBackend not supported in v0.6.0")
        else:
            _start_queen_thread(
                _backend,
                data_dir=data_dir,
                repo_path=repo_path,
                integration_branch=integration_branch,
            )

    if autoscaler_config is not None and autoscaler_config.enabled:
        _start_autoscaler_thread(_backend, autoscaler_config)

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    @app.post("/nodes", status_code=200)
    def register_node(req: NodeRequest):
        """Register a node. Idempotent — re-registering updates last_seen."""
        now = _now_iso()
        node = {
            "node_id": req.node_id,
            "joined_at": now,
            "last_seen": now,
            "runner_url": req.runner_url,
            "max_workers": req.max_workers,
            "capabilities": req.capabilities,
        }
        _backend.register_node(node)
        return {"node_id": req.node_id}

    @app.get("/nodes")
    def list_nodes():
        """List all registered nodes."""
        return _backend.list_nodes()

    @app.get("/nodes/{node_id}")
    def get_node(node_id: str):
        """Get a single node by ID, or 404."""
        node = _backend.get_node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f"Node {node_id!r} not found")
        return node

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

    @app.post("/workers/{worker_id:path}/activity", status_code=200)
    def worker_activity(worker_id: str, req: ActivityRequest):
        """Set or clear the worker's current action.

        Accepts either a freeform ``action`` string (legacy) or a structured
        ``{action, target}`` pair that the server synthesizes into a
        human-readable line (#348). Callers may also pre-compute ``text`` and
        pass it verbatim. Clearing is still done via ``action=null``.

        Does not update last_heartbeat — activity is a separate signal. Unknown
        workers are a silent no-op at the backend layer.
        """
        # Resolution order: explicit text → synthesize(action, target) →
        # legacy action-as-text → None (clear).
        if req.text is not None:
            resolved: str | None = req.text
        elif req.action is None and req.target is None:
            resolved = None
        else:
            synthesized = activity.synthesize_text(req.action, req.target)
            # Back-compat: if neither verb is canonical nor a target is set,
            # ``synthesize_text`` returns the raw action string — preserves
            # prior behavior for freeform callers like legacy hooks.
            resolved = synthesized if synthesized is not None else req.action
        _backend.update_worker_activity(worker_id, resolved)
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

    @app.post("/workers/{worker_id:path}/usage", status_code=200)
    def worker_usage(worker_id: str, req: WorkerUsageRequest):
        """Record a usage/cost event from a worker hook.

        Attribution flow:
        1. Explicit task_id/attempt_id/mission_id on the request wins.
        2. Otherwise look up the worker's current active task and attribute
           to its current_attempt.
        3. Otherwise fall back to the worker's most recent harvested/done
           attempt that still carries a mission_id — this catches the
           Stop-hook-after-harvest ordering.
        4. If no mission can be resolved, record the trail entry on the
           task (if any) but skip mission aggregation.

        The endpoint is idempotent by ``event_id`` — the backend usage
        updater dedupes.
        """
        from antfarm.core.missions import MissionUsage
        from antfarm.core.models import UsageEvent
        from antfarm.core.pricing import compute_cost

        task_id = req.task_id
        attempt_id = req.attempt_id
        mission_id = req.mission_id

        # Fall back to worker lookup only when attribution fields are absent.
        if task_id is None or mission_id is None:
            try:
                active_tasks = _backend.list_tasks(status="active")
            except Exception:
                active_tasks = []
            found: dict | None = None
            for t in active_tasks:
                current_id = t.get("current_attempt")
                if not current_id:
                    continue
                for a in t.get("attempts", []):
                    if a.get("attempt_id") == current_id and a.get("worker_id") == worker_id:
                        found = t
                        break
                if found is not None:
                    break

            if found is None:
                # Fall back to last-completed attempt with a mission_id. The
                # Stop hook frequently fires immediately after harvest, by
                # which time the task is no longer active.
                try:
                    done_tasks = _backend.list_tasks(status="done")
                except Exception:
                    done_tasks = []
                best_ts = ""
                for t in done_tasks:
                    if not t.get("mission_id"):
                        continue
                    for a in t.get("attempts", []):
                        if a.get("worker_id") != worker_id:
                            continue
                        completed = a.get("completed_at") or a.get("started_at") or ""
                        if completed > best_ts:
                            best_ts = completed
                            found = t
                            attempt_id = attempt_id or a.get("attempt_id")

            if found is not None:
                task_id = task_id or found.get("id")
                mission_id = mission_id or found.get("mission_id")
                if attempt_id is None:
                    attempt_id = found.get("current_attempt")

        cost = compute_cost(
            model=req.model,
            input_tokens=req.input_tokens,
            output_tokens=req.output_tokens,
            cache_read_tokens=req.cache_read_tokens,
            cache_creation_tokens=req.cache_creation_tokens,
        )

        event = UsageEvent(
            event_id=req.event_id,
            worker_id=worker_id,
            task_id=task_id,
            attempt_id=attempt_id,
            mission_id=mission_id,
            ts=req.ts,
            model=req.model,
            input_tokens=req.input_tokens,
            output_tokens=req.output_tokens,
            cache_read_tokens=req.cache_read_tokens,
            cache_creation_tokens=req.cache_creation_tokens,
            cost_usd=cost,
            source=req.source,
        )

        total_cost: float = 0.0
        total_tokens: int = 0
        if mission_id:

            def _updater(current: dict) -> dict:
                usage = MissionUsage.from_dict(current)
                usage.apply(event)
                return usage.to_dict()

            try:
                new_state = _backend.update_mission_usage(mission_id, _updater)
                total_cost = float(new_state.get("total_cost_usd", 0.0))
                total_tokens = int(
                    new_state.get("total_input_tokens", 0) + new_state.get("total_output_tokens", 0)
                )
            except NotImplementedError:
                logger.warning("worker_usage: backend does not support mission usage; skipping")

        # Append a trail entry on the task so operators see cost per task.
        if task_id:
            trail_msg = f"cost +${cost:.4f} model={req.model}"
            entry = {
                "ts": _now_iso(),
                "worker_id": worker_id,
                "message": trail_msg,
                "action_type": "usage",
            }
            with contextlib.suppress(Exception), _lock:
                _backend.append_trail(task_id, entry)

        return {
            "ok": True,
            "event_id": req.event_id,
            "cost_usd": cost,
            "mission_id": mission_id,
            "task_id": task_id,
            "total_cost_usd": total_cost,
            "total_tokens": total_tokens,
        }

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

        if req.mission_id:
            from antfarm.core.missions import link_task_to_mission

            task["mission_id"] = req.mission_id
            try:
                task_id = link_task_to_mission(_backend, task, req.mission_id)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        else:
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
        """Pull the next eligible task for a worker. Returns 204 if queue is empty.

        Tasks whose parent mission is paused or cancelled are returned to
        ready/ (via reassign_task) so the claim doesn't wedge them on a
        paused mission. We do this post-pull rather than pre-filtering inside
        the scheduler to keep the scheduler path simple — bounded O(pauses)
        retries per pull in the common case where no missions are paused.
        """
        # Try up to a handful of times in case we repeatedly pull tasks from
        # paused missions. In practice a paused mission will have few tasks
        # compared to the queue and the loop exits after one iteration.
        for _ in range(8):
            with _lock:
                task = _backend.pull(req.worker_id)
                if task is not None:
                    with contextlib.suppress(Exception):
                        _backend.heartbeat(req.worker_id, {"status": "active"})
            if task is None:
                break
            mission_id = task.get("mission_id")
            if not mission_id:
                break
            try:
                mission = _backend.get_mission(mission_id)
            except NotImplementedError:
                mission = None
            if mission is None:
                break
            if mission.get("status") not in ("paused", "cancelled"):
                break
            # Return the claimed task to ready/ so another pull (or a future
            # extend) can pick it up. reassign_task supersedes the attempt
            # and moves active/ → ready/.
            with contextlib.suppress(Exception):
                _backend.reassign_task(task["id"], req.worker_id)
            task = None
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
        effective = req.max_attempts if req.max_attempts is not None else _max_attempts
        try:
            _backend.kickback(task_id, req.reason, max_attempts=effective)
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

    @app.post("/tasks/{task_id}/rereview", status_code=200)
    def rereview_task(task_id: str, req: RereviewRequest):
        """Re-ready an existing review task with an updated spec + touches."""
        with _lock:
            try:
                _backend.rereview(task_id, req.spec, req.touches)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/tasks/{task_id}/merge", status_code=200)
    def merge_task(task_id: str, req: MergeRequest):
        """Mark a task attempt as merged. Soldier only."""
        try:
            _backend.mark_merged(task_id, req.attempt_id, auto_merged=req.auto_merged)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        detail = f"attempt={req.attempt_id}"
        if req.auto_merged:
            detail += " auto_merged=1"
        _emit_event("merged", task_id, detail)
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
    def list_tasks(
        status: str | None = Query(default=None),
        mission_id: str | None = Query(default=None),
    ):
        """List tasks with optional ?status= and ?mission_id= filters."""
        tasks = _backend.list_tasks(status=status)
        if mission_id is not None:
            tasks = [t for t in tasks if t.get("mission_id") == mission_id]
        return tasks

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
    # Missions
    # ------------------------------------------------------------------

    @app.post("/missions", status_code=201)
    def create_mission(req: MissionCreateRequest):
        """Create a mission. Returns 201 with mission_id. 409 on duplicate."""
        from antfarm.core.backends.github import GitHubBackend
        from antfarm.core.missions import MissionConfig, MissionStatus

        if isinstance(_backend, GitHubBackend):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Mission mode requires FileBackend in v0.6.0. "
                    "Use --backend file or wait for v0.6.1."
                ),
            )

        mission_id = req.mission_id or f"mission-{int(time.time() * 1000)}"
        now = _now_iso()

        cfg = MissionConfig.from_dict(req.config or {})
        if cfg.completion_mode == "all_or_nothing":
            logger.warning(
                "mission %s requested completion_mode='all_or_nothing'; "
                "treated as best_effort for v0.6.0 (real semantics land in v0.6.1+)",
                mission_id,
            )

        mission = {
            "mission_id": mission_id,
            "spec": req.spec,
            "spec_file": req.spec_file,
            "status": MissionStatus.PLANNING.value,
            "plan_task_id": None,
            "plan_artifact": None,
            "task_ids": [],
            "blocked_task_ids": [],
            "config": cfg.to_dict(),
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "report": None,
            "last_progress_at": now,
            "re_plan_count": 0,
        }

        try:
            _backend.create_mission(mission)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        return {"mission_id": mission_id}

    @app.get("/missions", status_code=200)
    def list_missions(status: str | None = Query(default=None)):
        """List missions with optional ?status= filter."""
        return _backend.list_missions(status=status)

    @app.get("/missions/{mission_id}", status_code=200)
    def get_mission(mission_id: str):
        """Get a mission by ID. Returns 404 if not found."""
        mission = _backend.get_mission(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail=f"Mission '{mission_id}' not found")
        return mission

    @app.patch("/missions/{mission_id}", status_code=200)
    def update_mission(mission_id: str, req: MissionUpdateRequest):
        """Apply shallow updates to a mission. 404 if missing."""
        try:
            _backend.update_mission(mission_id, req.updates)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/missions/{mission_id}/cancel", status_code=200)
    def cancel_mission(mission_id: str):
        """Cancel a mission and purge all non-terminal tasks to done/. Idempotent."""
        mission = _backend.get_mission(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail=f"Mission '{mission_id}' not found")
        terminal = {"complete", "failed", "cancelled"}
        if mission["status"] in terminal:
            return {"ok": True, "cancelled_tasks": []}
        ids = _backend.cancel_mission_tasks(mission_id, reason="mission cancelled")
        return {"ok": True, "cancelled_tasks": ids}

    @app.get("/missions/{mission_id}/usage", status_code=200)
    def get_mission_usage_endpoint(mission_id: str):
        """Return aggregated usage for a mission. Empty object if none recorded."""
        from antfarm.core.missions import MissionUsage

        mission = _backend.get_mission(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail=f"Mission '{mission_id}' not found")
        usage = _backend.get_mission_usage(mission_id)
        if usage is None:
            return MissionUsage(mission_id=mission_id).to_dict()
        return usage

    @app.post("/missions/{mission_id}/extend", status_code=200)
    def extend_mission(mission_id: str, req: MissionExtendRequest):
        """Bump a mission's budget caps and resume if paused.

        When ``additional_usd`` or ``additional_tokens`` is provided, the
        corresponding config field is increased by that amount. If the mission
        is currently PAUSED due to a budget tripwire, it is moved back to the
        status recorded in ``paused_from_status`` (falling back to BUILDING).
        """
        from antfarm.core.missions import MissionStatus

        mission = _backend.get_mission(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail=f"Mission '{mission_id}' not found")

        cfg = dict(mission.get("config") or {})
        updates: dict = {}

        if req.additional_usd is not None:
            current = cfg.get("max_cost_usd")
            new_val = (float(current) if current is not None else 0.0) + float(req.additional_usd)
            cfg["max_cost_usd"] = new_val

        if req.additional_tokens is not None:
            current_tok = cfg.get("max_tokens")
            new_tok = (int(current_tok) if current_tok is not None else 0) + int(
                req.additional_tokens
            )
            cfg["max_tokens"] = new_tok

        updates["config"] = cfg

        if mission.get("status") == MissionStatus.PAUSED.value:
            prior = mission.get("paused_from_status") or MissionStatus.BUILDING.value
            updates["status"] = prior
            updates["paused_from_status"] = None

        try:
            _backend.update_mission(mission_id, updates)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        _emit_event(
            "mission_extended",
            "",
            detail=(
                f"mission={mission_id} +usd={req.additional_usd} +tokens={req.additional_tokens}"
            ),
            actor="colony",
        )

        return _backend.get_mission(mission_id)

    @app.get("/missions/{mission_id}/context")
    def get_mission_context_endpoint(mission_id: str):
        """Return mission context blob for prompt cache sharing. 404 if not found."""
        context_path = os.path.join(data_dir, "missions", f"{mission_id}_context.md")
        if not os.path.exists(context_path):
            raise HTTPException(
                status_code=404, detail=f"Context not found for mission '{mission_id}'"
            )
        with open(context_path) as f:
            return Response(content=f.read(), media_type="text/markdown")

    @app.get("/missions/{mission_id}/report", status_code=200)
    def get_mission_report(mission_id: str):
        """Return mission report or 404 if not yet generated."""
        mission = _backend.get_mission(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail=f"Mission '{mission_id}' not found")
        report = mission.get("report")
        if report is None:
            raise HTTPException(status_code=404, detail=f"No report for mission '{mission_id}'")
        return report

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
        epoch: str = Query(
            default="",
            description=(
                "Server epoch seen by the client. If non-empty and differs from the "
                "current epoch, the cursor is reset to 0 (colony restart recovery)."
            ),
        ),
        timeout: float = Query(default=30.0, description="Max seconds to hold connection"),
    ):
        """Stream colony events (harvest, kickback, merge) as SSE."""

        def _generate():
            # If client supplies a non-empty epoch that differs from ours, the
            # client was talking to a prior server instance. Reset its cursor.
            # Empty epoch = first connect; use `after` literally for backward
            # compat with old TUIs that don't send the epoch parameter.
            cursor = 0 if epoch and epoch != _server_epoch else after
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

    @app.get("/events/epoch")
    def event_epoch():
        """Return the server's current SSE epoch.

        Used by clients (e.g. TUI) to learn the server identity without waiting
        for the first event. If the epoch changes between two reads, the colony
        restarted.
        """
        return {"epoch": _server_epoch}

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _compute_status_warnings(backend: TaskBackend) -> list[dict]:
        """Collect all colony-level warnings for the /status response.

        Each warning dict has keys: code, message, hint, count.
        Pure read — never mutates state.
        """
        from antfarm.core.warnings import detect_no_reviewer_capacity

        try:
            tasks = backend.list_tasks()
            workers = backend.list_workers() if hasattr(backend, "list_workers") else []
        except Exception:
            return []

        warnings: list[dict] = []
        w = detect_no_reviewer_capacity(tasks, workers)
        if w is not None:
            warnings.append(w)
        return warnings

    @app.get("/status", status_code=200)
    def colony_status():
        """Return colony status summary."""
        result = _backend.status()
        result["soldier"] = _soldier_status
        result["doctor"] = _doctor_status
        result["queen"] = _queen_status
        result["autoscaler"] = _autoscaler_status
        result["warnings"] = _compute_status_warnings(_backend)
        return result

    @app.get("/status/full", status_code=200)
    def colony_status_full():
        """Return colony status, all tasks, and all workers in one call.

        Reduces polling overhead for TUI clients that need all three.

        Returns:
            Dict with 'status', 'tasks', and 'workers' keys.
        """
        try:
            missions = _backend.list_missions()
        except NotImplementedError:
            missions = []
        # Copy soldier/doctor activity dicts under the lock so the TUI can
        # render them as synthetic rows (#348). Copies are shallow but all
        # values are primitives, so this is safe.
        with _colony_activity_lock:
            soldier_activity = dict(_soldier_activity) if _soldier_activity is not None else None
            doctor_activity = dict(_doctor_activity) if _doctor_activity is not None else None
        return {
            "status": _backend.status(),
            "tasks": _backend.list_tasks(),
            "workers": _backend.list_workers(),
            "missions": missions,
            "soldier": _soldier_status,
            "doctor": _doctor_status,
            "queen": _queen_status,
            "autoscaler": _autoscaler_status,
            "soldier_activity": soldier_activity,
            "doctor_activity": doctor_activity,
            "warnings": _compute_status_warnings(_backend),
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
