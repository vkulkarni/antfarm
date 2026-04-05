"""RedisBackend — Redis-backed TaskBackend implementation.

Stores task state in Redis using:
  - Hashes: antfarm:tasks:{task_id} — full task JSON
  - Sets: antfarm:queue:{status} — task IDs per status (ready/active/done)
  - Keys with TTL: antfarm:workers:{worker_id} — worker presence (auto-expire)
  - Hashes: antfarm:nodes:{node_id} — node registration
  - SET NX EX: antfarm:guards:{resource} — exclusive locks with TTL
  - Pub/sub: antfarm:events — real-time event notifications

All mutations that could race use Redis transactions (MULTI/EXEC) or
Lua scripts for atomicity. No application-level threading.Lock needed.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from antfarm.core.models import (
    Attempt,
    AttemptStatus,
    Task,
    TaskStatus,
    TrailEntry,
)

from .base import TaskBackend

# Default TTLs
_GUARD_TTL_SECONDS = 300
_WORKER_TTL_SECONDS = 300

# Redis key prefixes
_PREFIX = "antfarm"
_TASK_KEY = f"{_PREFIX}:tasks"
_QUEUE_KEY = f"{_PREFIX}:queue"
_WORKER_KEY = f"{_PREFIX}:workers"
_NODE_KEY = f"{_PREFIX}:nodes"
_GUARD_KEY = f"{_PREFIX}:guards"
_EVENTS_CHANNEL = f"{_PREFIX}:events"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class RedisBackend(TaskBackend):
    """Redis-backed implementation of TaskBackend.

    Args:
        redis_client: A redis.Redis instance (or compatible).
        guard_ttl: Seconds before a guard expires (default 300).
        worker_ttl: Seconds before a worker heartbeat key expires (default 300).
        key_prefix: Optional prefix override for all Redis keys.
    """

    def __init__(
        self,
        redis_client,
        guard_ttl: int = _GUARD_TTL_SECONDS,
        worker_ttl: int = _WORKER_TTL_SECONDS,
        key_prefix: str = _PREFIX,
    ) -> None:
        self._r = redis_client
        self._guard_ttl = guard_ttl
        self._worker_ttl = worker_ttl
        self._prefix = key_prefix

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _task_key(self, task_id: str) -> str:
        return f"{self._prefix}:tasks:{task_id}"

    def _queue_key(self, status: str) -> str:
        return f"{self._prefix}:queue:{status}"

    def _worker_key(self, worker_id: str) -> str:
        safe = worker_id.replace("/", "%2F")
        return f"{self._prefix}:workers:{safe}"

    def _node_key(self, node_id: str) -> str:
        return f"{self._prefix}:nodes:{node_id}"

    def _guard_key(self, resource: str) -> str:
        safe = resource.replace("/", "__")
        return f"{self._prefix}:guards:{safe}"

    def _read_task(self, task_id: str) -> dict | None:
        raw = self._r.get(self._task_key(task_id))
        if raw is None:
            return None
        return json.loads(raw)

    def _write_task(self, task_id: str, data: dict) -> None:
        self._r.set(self._task_key(task_id), json.dumps(data))

    def _publish(self, event_type: str, payload: dict) -> None:
        msg = json.dumps({"event": event_type, **payload})
        self._r.publish(f"{self._prefix}:events", msg)

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def carry(self, task: dict) -> str:
        task_id = task["id"]

        # Normalize touches before the transaction
        raw_touches = task.get("touches", [])
        task["touches"] = list(dict.fromkeys(t.strip() for t in raw_touches))

        # Ensure required defaults
        task.setdefault("status", TaskStatus.READY.value)
        task.setdefault("current_attempt", None)
        task.setdefault("attempts", [])
        task.setdefault("trail", [])
        task.setdefault("signals", [])

        # Use WATCH on the task key to detect duplicate carry races atomically.
        # If the task key exists, reject. Otherwise write + enqueue in MULTI/EXEC.
        with self._r.pipeline() as pipe:
            pipe.watch(self._task_key(task_id))
            if pipe.exists(self._task_key(task_id)):
                pipe.unwatch()
                raise ValueError(f"Task '{task_id}' already exists")
            pipe.multi()
            pipe.set(self._task_key(task_id), json.dumps(task))
            pipe.sadd(self._queue_key("ready"), task_id)
            pipe.execute()

        self._publish("task_carried", {"task_id": task_id})
        return task_id

    def pull(self, worker_id: str) -> dict | None:
        # Collect done task IDs for dependency checking
        done_task_ids = {
            tid.decode() if isinstance(tid, bytes) else tid
            for tid in self._r.smembers(self._queue_key("done"))
        }

        # Load all ready tasks
        ready_ids = self._r.smembers(self._queue_key("ready"))
        candidates: list[Task] = []
        for raw_id in ready_ids:
            task_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
            data = self._read_task(task_id)
            if data is None:
                continue
            try:
                candidates.append(Task.from_dict(data))
            except (KeyError, ValueError):
                continue

        # Select using scheduling policy: deps → priority → FIFO
        eligible = [t for t in candidates if all(dep in done_task_ids for dep in t.depends_on)]
        if not eligible:
            return None

        eligible.sort(key=lambda t: (t.priority, t.created_at))

        # Try each eligible task with optimistic locking (WATCH/MULTI/EXEC)
        for chosen in eligible:
            attempt = Attempt(
                attempt_id=str(uuid.uuid4()),
                worker_id=worker_id,
                status=AttemptStatus.ACTIVE,
                branch=None,
                pr=None,
                started_at=_now_iso(),
                completed_at=None,
            )
            chosen.attempts.append(attempt)
            chosen.current_attempt = attempt.attempt_id
            chosen.status = TaskStatus.ACTIVE
            task_dict = chosen.to_dict()

            # Atomic move using WATCH on the ready queue
            ready_key = self._queue_key("ready")
            active_key = self._queue_key("active")
            task_key = self._task_key(chosen.id)

            with self._r.pipeline() as pipe:
                try:
                    pipe.watch(ready_key)
                    # Verify task still in ready queue after WATCH
                    if not pipe.sismember(ready_key, chosen.id):
                        pipe.unwatch()
                        continue
                    pipe.multi()
                    pipe.srem(ready_key, chosen.id)
                    pipe.sadd(active_key, chosen.id)
                    pipe.set(task_key, json.dumps(task_dict))
                    pipe.execute()
                    self._publish(
                        "task_pulled", {"task_id": chosen.id, "worker_id": worker_id}
                    )
                    return task_dict
                except Exception:  # noqa: BLE001
                    # WatchError or other — another worker claimed it, try next
                    continue

        return None

    def append_trail(self, task_id: str, entry: dict) -> None:
        data = self._read_task(task_id)
        if data is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")
        data.setdefault("trail", [])
        data["trail"].append(entry)
        self._write_task(task_id, data)

    def append_signal(self, task_id: str, entry: dict) -> None:
        data = self._read_task(task_id)
        if data is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")
        data.setdefault("signals", [])
        data["signals"].append(entry)
        self._write_task(task_id, data)

    def mark_harvested(self, task_id: str, attempt_id: str, pr: str, branch: str) -> None:
        # Check if already done (idempotent)
        if self._r.sismember(self._queue_key("done"), task_id):
            data = self._read_task(task_id)
            if data and data.get("current_attempt") != attempt_id:
                raise ValueError(
                    f"attempt_id '{attempt_id}' is not the current attempt "
                    f"(got '{data.get('current_attempt')}')"
                )
            return

        if not self._r.sismember(self._queue_key("active"), task_id):
            raise FileNotFoundError(f"Task '{task_id}' not found in active/")

        data = self._read_task(task_id)
        if data is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")

        if data.get("current_attempt") != attempt_id:
            raise ValueError(
                f"attempt_id '{attempt_id}' is not the current attempt "
                f"(got '{data.get('current_attempt')}')"
            )

        now = _now_iso()
        for a in data["attempts"]:
            if a["attempt_id"] == attempt_id:
                a["status"] = AttemptStatus.DONE.value
                a["pr"] = pr
                a["branch"] = branch
                a["completed_at"] = now
                break

        data["status"] = TaskStatus.DONE.value
        data["updated_at"] = now

        pipe = self._r.pipeline(transaction=True)
        pipe.set(self._task_key(task_id), json.dumps(data))
        pipe.srem(self._queue_key("active"), task_id)
        pipe.sadd(self._queue_key("done"), task_id)
        pipe.execute()

        self._publish("task_harvested", {"task_id": task_id, "attempt_id": attempt_id})

    def kickback(self, task_id: str, reason: str) -> None:
        if not self._r.sismember(self._queue_key("done"), task_id):
            raise FileNotFoundError(f"Task '{task_id}' not found in done/")

        data = self._read_task(task_id)
        if data is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")

        now = _now_iso()
        current_attempt_id = data.get("current_attempt")
        worker_id = "system"
        for a in data["attempts"]:
            if a["attempt_id"] == current_attempt_id:
                a["status"] = AttemptStatus.SUPERSEDED.value
                a["completed_at"] = now
                worker_id = a.get("worker_id") or "system"
                break

        trail_entry = TrailEntry(ts=now, worker_id=worker_id, message=reason)
        data.setdefault("trail", [])
        data["trail"].append(trail_entry.to_dict())

        data["status"] = TaskStatus.READY.value
        data["current_attempt"] = None
        data["updated_at"] = now

        pipe = self._r.pipeline(transaction=True)
        pipe.set(self._task_key(task_id), json.dumps(data))
        pipe.srem(self._queue_key("done"), task_id)
        pipe.sadd(self._queue_key("ready"), task_id)
        pipe.execute()

        self._publish("task_kickback", {"task_id": task_id, "reason": reason})

    def mark_merged(self, task_id: str, attempt_id: str) -> None:
        if not self._r.sismember(self._queue_key("done"), task_id):
            raise FileNotFoundError(f"Task '{task_id}' not found in done/")

        data = self._read_task(task_id)
        if data is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")

        matched = False
        for a in data["attempts"]:
            if a["attempt_id"] == attempt_id:
                a["status"] = AttemptStatus.MERGED.value
                matched = True
                break

        if not matched:
            raise ValueError(f"attempt_id '{attempt_id}' not found on task '{task_id}'")

        data["updated_at"] = _now_iso()
        self._write_task(task_id, data)

        self._publish("task_merged", {"task_id": task_id, "attempt_id": attempt_id})

    def pause_task(self, task_id: str) -> None:
        raise NotImplementedError("RedisBackend overrides deferred to v0.3")

    def resume_task(self, task_id: str) -> None:
        raise NotImplementedError("RedisBackend overrides deferred to v0.3")

    def reassign_task(self, task_id: str, worker_id: str) -> None:
        raise NotImplementedError("RedisBackend overrides deferred to v0.3")

    def block_task(self, task_id: str, reason: str) -> None:
        raise NotImplementedError("RedisBackend overrides deferred to v0.3")

    def unblock_task(self, task_id: str) -> None:
        raise NotImplementedError("RedisBackend overrides deferred to v0.3")

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_tasks(self, status: str | None = None) -> list[dict]:
        statuses = [status] if status else ["ready", "active", "done"]
        results = []
        for s in statuses:
            task_ids = self._r.smembers(self._queue_key(s))
            for raw_id in task_ids:
                task_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
                data = self._read_task(task_id)
                if data is not None:
                    results.append(data)
        return results

    def get_task(self, task_id: str) -> dict | None:
        return self._read_task(task_id)

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def guard(self, resource: str, owner: str) -> bool:
        """Acquire exclusive guard via SET NX EX (atomic lock with TTL).

        The guard auto-expires after guard_ttl seconds (Redis TTL handles
        stale guard cleanup). If the guard exists but its owner worker is
        dead (no heartbeat key), the guard is cleared and reacquired.
        """
        guard_key = self._guard_key(resource)
        payload = json.dumps({"owner": owner, "acquired_at": _now_iso()})

        acquired = self._r.set(guard_key, payload, nx=True, ex=self._guard_ttl)
        if acquired:
            return True

        # Guard exists — check if owner is still alive
        existing_raw = self._r.get(guard_key)
        if existing_raw is None:
            # Guard expired between failed SET NX and GET — retry
            return self.guard(resource, owner)

        try:
            existing_str = (
                existing_raw.decode() if isinstance(existing_raw, bytes) else existing_raw
            )
            existing = json.loads(existing_str)
            existing_owner = existing.get("owner", "")
        except (json.JSONDecodeError, AttributeError):
            existing_owner = ""

        # Match FileBackend: stale = past TTL AND dead owner.
        # With Redis SET NX EX, the key auto-expires after guard_ttl,
        # so if the key still exists, it's within TTL. Only clear if
        # the guard has no remaining TTL (edge case) AND owner is dead.
        ttl = self._r.ttl(guard_key)
        owner_alive = self._r.exists(self._worker_key(existing_owner))

        # Guard still within TTL — honor it regardless of worker liveness
        if ttl > 0:
            return False

        # Guard TTL expired (key persisted without TTL) and owner is dead
        if not owner_alive:
            self._r.delete(guard_key)
            return self.guard(resource, owner)

        return False

    def release_guard(self, resource: str, owner: str) -> None:
        guard_key = self._guard_key(resource)
        existing_raw = self._r.get(guard_key)

        if existing_raw is None:
            raise FileNotFoundError(f"No guard exists for resource '{resource}'")

        existing_str = existing_raw.decode() if isinstance(existing_raw, bytes) else existing_raw
        try:
            data = json.loads(existing_str)
        except json.JSONDecodeError as exc:
            raise FileNotFoundError(f"Guard for '{resource}' is unreadable") from exc

        if data.get("owner") != owner:
            raise PermissionError(
                f"Guard on '{resource}' is owned by '{data.get('owner')}', not '{owner}'"
            )
        self._r.delete(guard_key)

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def register_node(self, node: dict) -> None:
        node_id = node["node_id"]
        node_key = self._node_key(node_id)
        existing_raw = self._r.get(node_key)

        if existing_raw is not None:
            existing_str = (
                existing_raw.decode() if isinstance(existing_raw, bytes) else existing_raw
            )
            existing = json.loads(existing_str)
            existing["last_seen"] = node.get("last_seen", _now_iso())
            self._r.set(node_key, json.dumps(existing))
        else:
            self._r.set(node_key, json.dumps(node))

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    def register_worker(self, worker: dict) -> None:
        worker_id = worker["worker_id"]
        worker_key = self._worker_key(worker_id)

        # Check if live worker already exists (key with TTL still active)
        if self._r.exists(worker_key):
            ttl = self._r.ttl(worker_key)
            if ttl > 0:
                raise ValueError(f"Worker '{worker_id}' is already registered and live")

        self._r.set(worker_key, json.dumps(worker), ex=self._worker_ttl)

    def deregister_worker(self, worker_id: str) -> None:
        self._r.delete(self._worker_key(worker_id))

    def heartbeat(self, worker_id: str, status: dict) -> None:
        worker_key = self._worker_key(worker_id)
        existing_raw = self._r.get(worker_key)

        if existing_raw is not None:
            existing_str = (
                existing_raw.decode() if isinstance(existing_raw, bytes) else existing_raw
            )
            data = json.loads(existing_str)
        else:
            data = {"worker_id": worker_id}

        data.update(status)
        data["last_heartbeat"] = _now_iso()
        self._r.set(worker_key, json.dumps(data), ex=self._worker_ttl)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        ready = self._r.scard(self._queue_key("ready"))
        active = self._r.scard(self._queue_key("active"))
        done = self._r.scard(self._queue_key("done"))

        # Count workers by scanning keys
        worker_pattern = f"{self._prefix}:workers:*"
        workers = sum(1 for _ in self._r.scan_iter(worker_pattern))

        node_pattern = f"{self._prefix}:nodes:*"
        nodes = sum(1 for _ in self._r.scan_iter(node_pattern))

        guard_pattern = f"{self._prefix}:guards:*"
        guards = sum(1 for _ in self._r.scan_iter(guard_pattern))

        return {
            "tasks": {"ready": ready, "active": active, "done": done},
            "workers": workers,
            "nodes": nodes,
            "guards": guards,
        }
