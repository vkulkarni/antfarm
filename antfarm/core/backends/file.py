"""FileBackend — filesystem-backed TaskBackend implementation.

Stores task state as JSON files in a directory tree:

    .antfarm/
      tasks/ready/    — queued tasks
      tasks/active/   — claimed tasks (one per worker attempt)
      tasks/done/     — completed and merged tasks
      workers/        — worker presence files (mtime = heartbeat)
      nodes/          — node registration files
      guards/         — exclusive lock files (owner + timestamp)
      config.json     — colony config

All mutations that could race are protected by a threading.Lock().
File renames (os.rename) are used for atomic state transitions.

Scheduler note: pull() currently uses an inline scheduling policy
(dependency check → priority → FIFO). Full scheduler integration will
land when antfarm.core.scheduler is merged.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from antfarm.core.models import (
    Attempt,
    AttemptStatus,
    Task,
    TaskStatus,
    TrailEntry,
)

from .base import TaskBackend

# Default heartbeat TTL for stale guard detection (seconds)
_GUARD_TTL_SECONDS = 300


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class FileBackend(TaskBackend):
    """Filesystem-backed implementation of TaskBackend.

    Args:
        root: Path to the .antfarm directory. Created if it doesn't exist.
        guard_ttl: Seconds before a guard is considered stale (default 300).
    """

    def __init__(self, root: str | Path, guard_ttl: int = _GUARD_TTL_SECONDS) -> None:
        self._root = Path(root)
        self._guard_ttl = guard_ttl
        self._lock = threading.Lock()
        self._init_dirs()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_dirs(self) -> None:
        for subdir in [
            "tasks/ready",
            "tasks/active",
            "tasks/done",
            "workers",
            "nodes",
            "guards",
        ]:
            (self._root / subdir).mkdir(parents=True, exist_ok=True)

    def _ready_path(self, task_id: str) -> Path:
        return self._root / "tasks" / "ready" / f"{task_id}.json"

    def _active_path(self, task_id: str) -> Path:
        return self._root / "tasks" / "active" / f"{task_id}.json"

    def _done_path(self, task_id: str) -> Path:
        return self._root / "tasks" / "done" / f"{task_id}.json"

    def _worker_path(self, worker_id: str) -> Path:
        safe = worker_id.replace("/", "%2F")
        return self._root / "workers" / f"{safe}.json"

    def _node_path(self, node_id: str) -> Path:
        return self._root / "nodes" / f"{node_id}.json"

    def _guard_path(self, resource: str) -> Path:
        # Replace slashes to keep as a single filename
        safe = resource.replace("/", "__")
        return self._root / "guards" / f"{safe}.lock"

    def _write_json(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)

    def _read_json(self, path: Path) -> dict:
        return json.loads(path.read_text())

    def _find_task_path(self, task_id: str) -> Path | None:
        """Return the path to a task file regardless of which folder it lives in."""
        for p in [self._ready_path(task_id), self._active_path(task_id), self._done_path(task_id)]:
            if p.exists():
                return p
        return None

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def carry(self, task: dict) -> str:
        """Write task JSON to tasks/ready/{task_id}.json.

        Normalizes touches: trims whitespace, deduplicates (preserves case).

        Raises:
            ValueError: If a task with this ID already exists in any state folder.
        """
        task_id = task["id"]
        with self._lock:
            if self._find_task_path(task_id) is not None:
                raise ValueError(f"Task '{task_id}' already exists")

            # Normalize touches
            raw_touches = task.get("touches", [])
            task["touches"] = list(dict.fromkeys(t.strip() for t in raw_touches))

            # Ensure required defaults
            task.setdefault("status", TaskStatus.READY.value)
            task.setdefault("current_attempt", None)
            task.setdefault("attempts", [])
            task.setdefault("trail", [])
            task.setdefault("signals", [])

            self._write_json(self._ready_path(task_id), task)
        return task_id

    def pull(self, worker_id: str) -> dict | None:
        """Claim the next eligible task. Creates a new Attempt. Atomic.

        Scheduling policy (inline, v0.1):
        1. Dependency check — skip if depends_on not all in done_task_ids
        2. Priority — lower number = higher priority
        3. FIFO — oldest created_at first among equals

        Returns:
            The updated task dict with a new ACTIVE attempt, or None.
        """
        with self._lock:
            # Collect done task IDs for dependency checking
            done_task_ids = {
                p.stem for p in (self._root / "tasks" / "done").iterdir() if p.suffix == ".json"
            }

            # Load all ready tasks
            ready_dir = self._root / "tasks" / "ready"
            candidates: list[Task] = []
            for p in ready_dir.iterdir():
                if p.suffix != ".json":
                    continue
                try:
                    data = self._read_json(p)
                    candidates.append(Task.from_dict(data))
                except (json.JSONDecodeError, KeyError):
                    continue

            # Select using inline scheduling policy
            eligible = [t for t in candidates if all(dep in done_task_ids for dep in t.depends_on)]
            if not eligible:
                return None

            eligible.sort(key=lambda t: (t.priority, t.created_at))
            chosen = eligible[0]

            # Create new attempt
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
            # Atomic: rename from ready/ to active/
            self._write_json(self._ready_path(chosen.id), task_dict)
            os.rename(self._ready_path(chosen.id), self._active_path(chosen.id))
            return task_dict

    def append_trail(self, task_id: str, entry: dict) -> None:
        """Append a trail entry to the task (read-modify-write under lock)."""
        with self._lock:
            path = self._find_task_path(task_id)
            if path is None:
                raise FileNotFoundError(f"Task '{task_id}' not found")
            data = self._read_json(path)
            data.setdefault("trail", [])
            data["trail"].append(entry)
            self._write_json(path, data)

    def append_signal(self, task_id: str, entry: dict) -> None:
        """Append a signal entry to the task (read-modify-write under lock)."""
        with self._lock:
            path = self._find_task_path(task_id)
            if path is None:
                raise FileNotFoundError(f"Task '{task_id}' not found")
            data = self._read_json(path)
            data.setdefault("signals", [])
            data["signals"].append(entry)
            self._write_json(path, data)

    def mark_harvested(self, task_id: str, attempt_id: str, pr: str, branch: str) -> None:
        """Move task from active/ to done/. Set task DONE, attempt DONE.

        Idempotent: if already in done/ with the same attempt_id, no-op.

        Raises:
            ValueError: If attempt_id is not the current_attempt on the task.
        """
        with self._lock:
            done_path = self._done_path(task_id)
            if done_path.exists():
                # Already harvested — idempotent no-op only if attempt_id matches
                data = self._read_json(done_path)
                if data.get("current_attempt") != attempt_id:
                    raise ValueError(
                        f"attempt_id '{attempt_id}' is not the current attempt "
                        f"(got '{data.get('current_attempt')}')"
                    )
                return

            active_path = self._active_path(task_id)
            if not active_path.exists():
                raise FileNotFoundError(f"Task '{task_id}' not found in active/")

            data = self._read_json(active_path)
            if data.get("current_attempt") != attempt_id:
                raise ValueError(
                    f"attempt_id '{attempt_id}' is not the current attempt "
                    f"(got '{data.get('current_attempt')}')"
                )

            # Update attempt
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

            self._write_json(active_path, data)
            os.rename(active_path, done_path)

    def kickback(self, task_id: str, reason: str) -> None:
        """Move task from done/ to ready/. SUPERSEDE current attempt.

        Soldier calls kickback after a failed integration merge. The task
        is in done/ at that point (harvested but not yet merged).
        Sets current_attempt to None. Adds failure TrailEntry.
        """
        with self._lock:
            done_path = self._done_path(task_id)
            if not done_path.exists():
                raise FileNotFoundError(f"Task '{task_id}' not found in done/")

            data = self._read_json(done_path)
            now = _now_iso()

            current_attempt_id = data.get("current_attempt")
            worker_id = "system"
            for a in data["attempts"]:
                if a["attempt_id"] == current_attempt_id:
                    a["status"] = AttemptStatus.SUPERSEDED.value
                    a["completed_at"] = now
                    worker_id = a.get("worker_id") or "system"
                    break

            # Add failure trail entry
            trail_entry = TrailEntry(ts=now, worker_id=worker_id, message=reason)
            data.setdefault("trail", [])
            data["trail"].append(trail_entry.to_dict())

            data["status"] = TaskStatus.READY.value
            data["current_attempt"] = None
            data["updated_at"] = now

            self._write_json(done_path, data)
            os.rename(done_path, self._ready_path(task_id))

    def mark_merged(self, task_id: str, attempt_id: str) -> None:
        """Update attempt status to MERGED in done/ task file. Task stays DONE."""
        with self._lock:
            done_path = self._done_path(task_id)
            if not done_path.exists():
                raise FileNotFoundError(f"Task '{task_id}' not found in done/")

            data = self._read_json(done_path)
            matched = False
            for a in data["attempts"]:
                if a["attempt_id"] == attempt_id:
                    a["status"] = AttemptStatus.MERGED.value
                    matched = True
                    break

            if not matched:
                raise ValueError(f"attempt_id '{attempt_id}' not found on task '{task_id}'")

            data["updated_at"] = _now_iso()
            self._write_json(done_path, data)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_tasks(self, status: str | None = None) -> list[dict]:
        """List tasks, optionally filtered by status string."""
        folders: list[tuple[str, Path]] = [
            ("ready", self._root / "tasks" / "ready"),
            ("active", self._root / "tasks" / "active"),
            ("done", self._root / "tasks" / "done"),
        ]
        results = []
        for folder_status, folder in folders:
            if status is not None and folder_status != status:
                continue
            for p in folder.iterdir():
                if p.suffix != ".json":
                    continue
                try:
                    results.append(self._read_json(p))
                except (json.JSONDecodeError, KeyError):
                    continue
        return results

    def get_task(self, task_id: str) -> dict | None:
        """Get a task by ID, regardless of folder."""
        path = self._find_task_path(task_id)
        if path is None:
            return None
        try:
            return self._read_json(path)
        except (json.JSONDecodeError, KeyError):
            return None

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def guard(self, resource: str, owner: str) -> bool:
        """Acquire exclusive guard via O_CREAT | O_EXCL.

        If an existing guard file's mtime is older than guard_ttl AND the
        owner worker is not registered/live, the stale guard is cleared and
        reacquired.

        Returns:
            True if guard acquired, False if held by another live owner.
        """
        guard_path = self._guard_path(resource)
        try:
            fd = os.open(str(guard_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = json.dumps({"owner": owner, "acquired_at": _now_iso()})
            os.write(fd, payload.encode())
            os.close(fd)
            return True
        except FileExistsError:
            # Check if stale
            try:
                stat = os.stat(str(guard_path))
                age = datetime.now(UTC).timestamp() - stat.st_mtime
                if age > self._guard_ttl:
                    try:
                        existing = json.loads(guard_path.read_text())
                        existing_owner = existing.get("owner", "")
                    except (json.JSONDecodeError, FileNotFoundError):
                        existing_owner = ""

                    # Guard is stale only if owner is not a live worker
                    owner_alive = self._worker_path(existing_owner).exists()
                    if not owner_alive:
                        # Clear and reacquire
                        guard_path.unlink(missing_ok=True)
                        return self.guard(resource, owner)
            except FileNotFoundError:
                # Guard disappeared between ExistsError and stat — retry
                return self.guard(resource, owner)
            return False

    def release_guard(self, resource: str, owner: str) -> None:
        """Release a guard. Only the owner can release.

        Raises:
            PermissionError: If the owner field doesn't match.
            FileNotFoundError: If no guard exists for this resource.
        """
        guard_path = self._guard_path(resource)
        if not guard_path.exists():
            raise FileNotFoundError(f"No guard exists for resource '{resource}'")

        try:
            data = json.loads(guard_path.read_text())
        except (json.JSONDecodeError, FileNotFoundError) as exc:
            raise FileNotFoundError(f"Guard file for '{resource}' is unreadable") from exc

        if data.get("owner") != owner:
            raise PermissionError(
                f"Guard on '{resource}' is owned by '{data.get('owner')}', not '{owner}'"
            )
        guard_path.unlink()

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def register_node(self, node: dict) -> None:
        """Register a node. Idempotent — updates last_seen if already exists."""
        node_id = node["node_id"]
        node_path = self._node_path(node_id)
        if node_path.exists():
            existing = self._read_json(node_path)
            existing["last_seen"] = node.get("last_seen", _now_iso())
            self._write_json(node_path, existing)
        else:
            self._write_json(node_path, node)

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    def register_worker(self, worker: dict) -> None:
        """Register a worker.

        Raises:
            ValueError: If a live (non-stale heartbeat) worker with the same ID exists.
        """
        worker_id = worker["worker_id"]
        worker_path = self._worker_path(worker_id)
        if worker_path.exists():
            stat = os.stat(str(worker_path))
            age = datetime.now(UTC).timestamp() - stat.st_mtime
            if age <= self._guard_ttl:
                raise ValueError(f"Worker '{worker_id}' is already registered and live")
        self._write_json(worker_path, worker)

    def deregister_worker(self, worker_id: str) -> None:
        """Deregister a worker. No-op if not found (idempotent)."""
        worker_path = self._worker_path(worker_id)
        worker_path.unlink(missing_ok=True)

    def heartbeat(self, worker_id: str, status: dict) -> None:
        """Write/update worker file in workers/. mtime = heartbeat timestamp."""
        worker_path = self._worker_path(worker_id)
        data = self._read_json(worker_path) if worker_path.exists() else {"worker_id": worker_id}
        data.update(status)
        data["last_heartbeat"] = _now_iso()
        self._write_json(worker_path, data)
        # Touch to ensure mtime reflects now (write_json via replace does this)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return a summary of current backend state."""
        ready = len(list((self._root / "tasks" / "ready").glob("*.json")))
        active = len(list((self._root / "tasks" / "active").glob("*.json")))
        done = len(list((self._root / "tasks" / "done").glob("*.json")))
        workers = len(list((self._root / "workers").glob("*.json")))
        nodes = len(list((self._root / "nodes").glob("*.json")))
        guards = len(list((self._root / "guards").glob("*.lock")))
        return {
            "tasks": {"ready": ready, "active": active, "done": done},
            "workers": workers,
            "nodes": nodes,
            "guards": guards,
        }
