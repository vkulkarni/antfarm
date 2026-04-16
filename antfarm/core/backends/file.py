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
Scheduling is delegated entirely to scheduler.select_task().
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from antfarm.core.lifecycle import assert_task_transition
from antfarm.core.models import (
    Attempt,
    AttemptStatus,
    Task,
    TaskStatus,
    TrailEntry,
)
from antfarm.core.rate_limiter import is_worker_rate_limited
from antfarm.core.scheduler import select_task

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
            "tasks/paused",
            "tasks/blocked",
            "workers",
            "nodes",
            "guards",
            "missions",
        ]:
            (self._root / subdir).mkdir(parents=True, exist_ok=True)

    def _ready_path(self, task_id: str) -> Path:
        return self._root / "tasks" / "ready" / f"{task_id}.json"

    def _active_path(self, task_id: str) -> Path:
        return self._root / "tasks" / "active" / f"{task_id}.json"

    def _done_path(self, task_id: str) -> Path:
        return self._root / "tasks" / "done" / f"{task_id}.json"

    def _paused_path(self, task_id: str) -> Path:
        return self._root / "tasks" / "paused" / f"{task_id}.json"

    def _blocked_path(self, task_id: str) -> Path:
        return self._root / "tasks" / "blocked" / f"{task_id}.json"

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
        for p in [
            self._ready_path(task_id),
            self._active_path(task_id),
            self._done_path(task_id),
            self._paused_path(task_id),
            self._blocked_path(task_id),
        ]:
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
        if not re.match(r"^[a-zA-Z0-9_-]+$", task_id):
            raise ValueError(
                f"Invalid task_id '{task_id}': must contain only alphanumeric, dash, underscore"
            )
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

        Delegates task selection entirely to scheduler.select_task().
        Backend only handles state persistence and atomic file rename.

        Returns:
            The updated task dict with a new ACTIVE attempt, or None.
        """
        with self._lock:
            # Read worker file — check rate limit and capabilities
            worker_capabilities: set[str] | None = None
            worker_path = self._worker_path(worker_id)
            if worker_path.exists():
                try:
                    worker_data = self._read_json(worker_path)
                    cooldown_until = worker_data.get("cooldown_until")
                    if is_worker_rate_limited(cooldown_until):
                        return None
                    worker_capabilities = set(worker_data.get("capabilities", []))
                except (json.JSONDecodeError, KeyError):
                    pass

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

            # Load active tasks for scope preference
            active_dir = self._root / "tasks" / "active"
            active_tasks: list[Task] = []
            for p in active_dir.iterdir():
                if p.suffix != ".json":
                    continue
                try:
                    data = self._read_json(p)
                    active_tasks.append(Task.from_dict(data))
                except (json.JSONDecodeError, KeyError):
                    continue

            # Load hotspot data for scheduler weighting
            hotspots: dict[str, float] | None = None
            try:
                from antfarm.core.memory import MemoryStore

                memory = MemoryStore(self._root)
                hotspots = memory.get_hotspots() or None
            except Exception:
                pass

            # Delegate ALL scheduling to the canonical scheduler
            chosen = select_task(
                ready_tasks=candidates,
                done_task_ids=done_task_ids,
                active_tasks=active_tasks,
                worker_capabilities=worker_capabilities,
                worker_id=worker_id,
                hotspots=hotspots,
            )

            if chosen is None:
                return None

            assert_task_transition(chosen.status.value, TaskStatus.ACTIVE.value)

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

    def mark_harvested(
        self,
        task_id: str,
        attempt_id: str,
        pr: str,
        branch: str,
        artifact: dict | None = None,
    ) -> None:
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

            # Accept both active (legacy) and harvest_pending (v0.5 proper)
            # since mark_harvest_pending is best-effort
            current_status = data.get("status", "")
            if current_status not in ("active", "harvest_pending"):
                assert_task_transition(current_status, TaskStatus.DONE.value)

            # Update attempt
            now = _now_iso()
            for a in data["attempts"]:
                if a["attempt_id"] == attempt_id:
                    a["status"] = AttemptStatus.DONE.value
                    a["pr"] = pr
                    a["branch"] = branch
                    a["completed_at"] = now
                    if artifact is not None:
                        a["artifact"] = artifact
                    break

            data["status"] = TaskStatus.DONE.value
            data["updated_at"] = now

            self._write_json(active_path, data)
            os.rename(active_path, done_path)

    def kickback(
        self, task_id: str, reason: str, max_attempts: int = 3
    ) -> None:
        """Move task from done/ to ready/ or blocked/ if max attempts exhausted.

        Soldier calls kickback after a failed integration merge. The task
        is in done/ at that point (harvested but not yet merged).
        Sets current_attempt to None. Adds failure TrailEntry.

        If total completed/superseded attempts >= effective max, transitions
        to BLOCKED instead of READY. Per-task ``max_attempts`` overrides the
        function parameter.
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
            trail_entry = TrailEntry(
                ts=now, worker_id=worker_id, message=reason, action_type="kickback"
            )
            data.setdefault("trail", [])
            data["trail"].append(trail_entry.to_dict())

            data["current_attempt"] = None
            data["updated_at"] = now

            # Determine effective max_attempts (per-task overrides parameter)
            effective_max = data.get("max_attempts") or max_attempts

            # Count completed/superseded attempts
            finished = sum(
                1
                for a in data["attempts"]
                if a["status"]
                in (AttemptStatus.DONE.value, AttemptStatus.SUPERSEDED.value)
            )

            if finished >= effective_max:
                assert_task_transition(
                    data["status"], TaskStatus.BLOCKED.value
                )
                data["status"] = TaskStatus.BLOCKED.value
                self._write_json(done_path, data)
                os.rename(done_path, self._blocked_path(task_id))
            else:
                assert_task_transition(
                    data["status"], TaskStatus.READY.value
                )
                data["status"] = TaskStatus.READY.value
                self._write_json(done_path, data)
                os.rename(done_path, self._ready_path(task_id))

    def mark_harvest_pending(self, task_id: str, attempt_id: str) -> None:
        """Transition task from ACTIVE to HARVEST_PENDING.

        Task stays in active/ directory but status field changes.
        """
        with self._lock:
            active_path = self._active_path(task_id)
            if not active_path.exists():
                raise FileNotFoundError(f"Task '{task_id}' not found in active/")

            data = self._read_json(active_path)
            if data.get("current_attempt") != attempt_id:
                raise ValueError(
                    f"attempt_id '{attempt_id}' is not the current attempt "
                    f"(got '{data.get('current_attempt')}')"
                )

            assert_task_transition(data["status"], "harvest_pending")
            data["status"] = "harvest_pending"
            data["updated_at"] = _now_iso()
            self._write_json(active_path, data)

    def store_review_verdict(
        self, task_id: str, attempt_id: str, verdict: dict
    ) -> None:
        """Store a ReviewVerdict on the task's current attempt in done/."""
        with self._lock:
            done_path = self._done_path(task_id)
            if not done_path.exists():
                raise FileNotFoundError(f"Task '{task_id}' not found in done/")

            data = self._read_json(done_path)
            if data.get("current_attempt") != attempt_id:
                raise ValueError(
                    f"attempt_id '{attempt_id}' is not the current attempt "
                    f"(got '{data.get('current_attempt')}')"
                )

            for a in data["attempts"]:
                if a["attempt_id"] == attempt_id:
                    a["review_verdict"] = verdict
                    break

            data["updated_at"] = _now_iso()
            self._write_json(done_path, data)

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

    def rereview(
        self,
        review_task_id: str,
        new_spec: str,
        touches: list[str],
    ) -> None:
        """Re-ready an existing review task with an updated spec.

        Finds the review task wherever it currently sits (done/, active/,
        ready/, paused/, blocked/), supersedes its current attempt if any,
        updates spec + touches, appends a trail entry, and moves the file
        back into ready/ (unless it is already there).
        """
        with self._lock:
            path = self._find_task_path(review_task_id)
            if path is None:
                raise FileNotFoundError(
                    f"Review task '{review_task_id}' not found"
                )

            data = self._read_json(path)
            now = _now_iso()

            current_attempt_id = data.get("current_attempt")
            if current_attempt_id:
                for a in data.get("attempts", []):
                    if a.get("attempt_id") == current_attempt_id:
                        if a.get("status") != AttemptStatus.SUPERSEDED.value:
                            a["status"] = AttemptStatus.SUPERSEDED.value
                            a["completed_at"] = now
                        break
                data["current_attempt"] = None

            # Normalize touches: trim + dedupe (preserve case)
            data["touches"] = list(
                dict.fromkeys(t.strip() for t in (touches or []))
            )
            data["spec"] = new_spec

            trail_entry = TrailEntry(
                ts=now,
                worker_id="system",
                message="Re-review triggered: parent task has a new attempt",
                action_type="rereview",
            )
            data.setdefault("trail", [])
            data["trail"].append(trail_entry.to_dict())

            data["status"] = TaskStatus.READY.value
            data["updated_at"] = now

            ready_path = self._ready_path(review_task_id)
            self._write_json(path, data)
            if path != ready_path:
                os.rename(path, ready_path)

    def override_merge_order(self, task_id: str, position: int) -> None:
        """Set merge_override on a task in done/."""
        with self._lock:
            done_path = self._done_path(task_id)
            if not done_path.exists():
                raise FileNotFoundError(f"Task '{task_id}' not found in done/")
            data = self._read_json(done_path)
            data["merge_override"] = position
            data["updated_at"] = _now_iso()
            self._write_json(done_path, data)

    def clear_merge_override(self, task_id: str) -> None:
        """Clear merge_override on a task in done/."""
        with self._lock:
            done_path = self._done_path(task_id)
            if not done_path.exists():
                raise FileNotFoundError(f"Task '{task_id}' not found in done/")
            data = self._read_json(done_path)
            data["merge_override"] = None
            data["updated_at"] = _now_iso()
            self._write_json(done_path, data)

    def pause_task(self, task_id: str) -> None:
        """Pause an active task. Moves from active/ to paused/."""
        with self._lock:
            active_path = self._active_path(task_id)
            if not active_path.exists():
                if self._find_task_path(task_id) is None:
                    raise FileNotFoundError(f"Task '{task_id}' not found")
                raise ValueError(f"Task '{task_id}' is not in ACTIVE state")

            data = self._read_json(active_path)
            assert_task_transition(data["status"], TaskStatus.PAUSED.value)
            now = _now_iso()
            data["status"] = TaskStatus.PAUSED.value
            data["updated_at"] = now

            self._write_json(active_path, data)
            os.rename(active_path, self._paused_path(task_id))

    def resume_task(self, task_id: str) -> None:
        """Resume a paused task. Moves from paused/ to ready/.

        Supersedes the current attempt so the task re-enters the queue cleanly.
        """
        with self._lock:
            paused_path = self._paused_path(task_id)
            if not paused_path.exists():
                if self._find_task_path(task_id) is None:
                    raise FileNotFoundError(f"Task '{task_id}' not found")
                raise ValueError(f"Task '{task_id}' is not in PAUSED state")

            data = self._read_json(paused_path)
            assert_task_transition(data["status"], TaskStatus.READY.value)
            now = _now_iso()

            # Supersede current attempt so next pull creates a fresh one
            current_attempt_id = data.get("current_attempt")
            if current_attempt_id:
                for a in data["attempts"]:
                    if a["attempt_id"] == current_attempt_id:
                        a["status"] = AttemptStatus.SUPERSEDED.value
                        a["completed_at"] = now
                        break
                data["current_attempt"] = None

            data["status"] = TaskStatus.READY.value
            data["updated_at"] = now

            self._write_json(paused_path, data)
            os.rename(paused_path, self._ready_path(task_id))

    def reassign_task(self, task_id: str, worker_id: str) -> None:
        """Reassign an active task. Supersedes current attempt, returns to ready/."""
        with self._lock:
            active_path = self._active_path(task_id)
            if not active_path.exists():
                if self._find_task_path(task_id) is None:
                    raise FileNotFoundError(f"Task '{task_id}' not found")
                raise ValueError(f"Task '{task_id}' is not in ACTIVE state")

            data = self._read_json(active_path)
            # Skip lifecycle assertion — reassign is an operator override (active→ready)
            now = _now_iso()

            current_attempt_id = data.get("current_attempt")
            if current_attempt_id:
                for a in data["attempts"]:
                    if a["attempt_id"] == current_attempt_id:
                        a["status"] = AttemptStatus.SUPERSEDED.value
                        a["completed_at"] = now
                        break

            trail_entry = TrailEntry(
                ts=now,
                worker_id="system",
                message=f"Reassigned to {worker_id}",
                action_type="reassign",
            )
            data.setdefault("trail", [])
            data["trail"].append(trail_entry.to_dict())

            data["status"] = TaskStatus.READY.value
            data["current_attempt"] = None
            data["updated_at"] = now

            self._write_json(active_path, data)
            os.rename(active_path, self._ready_path(task_id))

    def block_task(self, task_id: str, reason: str) -> None:
        """Block a ready task. Moves from ready/ to blocked/."""
        with self._lock:
            ready_path = self._ready_path(task_id)
            if not ready_path.exists():
                if self._find_task_path(task_id) is None:
                    raise FileNotFoundError(f"Task '{task_id}' not found")
                raise ValueError(f"Task '{task_id}' is not in READY state")

            data = self._read_json(ready_path)
            assert_task_transition(data["status"], TaskStatus.BLOCKED.value)
            now = _now_iso()

            trail_entry = TrailEntry(
                ts=now, worker_id="system", message=f"Blocked: {reason}", action_type="block"
            )
            data.setdefault("trail", [])
            data["trail"].append(trail_entry.to_dict())

            data["status"] = TaskStatus.BLOCKED.value
            data["updated_at"] = now

            self._write_json(ready_path, data)
            os.rename(ready_path, self._blocked_path(task_id))

    def unblock_task(self, task_id: str) -> None:
        """Unblock a blocked task. Moves from blocked/ to ready/."""
        with self._lock:
            blocked_path = self._blocked_path(task_id)
            if not blocked_path.exists():
                if self._find_task_path(task_id) is None:
                    raise FileNotFoundError(f"Task '{task_id}' not found")
                raise ValueError(f"Task '{task_id}' is not in BLOCKED state")

            data = self._read_json(blocked_path)
            assert_task_transition(data["status"], TaskStatus.READY.value)
            now = _now_iso()
            data["status"] = TaskStatus.READY.value
            data["updated_at"] = now

            self._write_json(blocked_path, data)
            os.rename(blocked_path, self._ready_path(task_id))

    def pin_task(self, task_id: str, worker_id: str) -> None:
        """Pin a ready task to a specific worker."""
        with self._lock:
            ready_path = self._ready_path(task_id)
            if not ready_path.exists():
                raise FileNotFoundError(f"Task '{task_id}' not found in ready/")
            data = self._read_json(ready_path)
            data["pinned_to"] = worker_id
            data["updated_at"] = _now_iso()
            self._write_json(ready_path, data)

    def unpin_task(self, task_id: str) -> None:
        """Clear the pin on a ready task."""
        with self._lock:
            ready_path = self._ready_path(task_id)
            if not ready_path.exists():
                raise FileNotFoundError(f"Task '{task_id}' not found in ready/")
            data = self._read_json(ready_path)
            data["pinned_to"] = None
            data["updated_at"] = _now_iso()
            self._write_json(ready_path, data)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_tasks(self, status: str | None = None) -> list[dict]:
        """List tasks, optionally filtered by status string."""
        folders: list[tuple[str, Path]] = [
            ("ready", self._root / "tasks" / "ready"),
            ("active", self._root / "tasks" / "active"),
            ("done", self._root / "tasks" / "done"),
            ("paused", self._root / "tasks" / "paused"),
            ("blocked", self._root / "tasks" / "blocked"),
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
        """Register a node. Idempotent — merges all fields on re-registration."""
        with self._lock:
            node_id = node["node_id"]
            node_path = self._node_path(node_id)
            if node_path.exists():
                existing = self._read_json(node_path)
                for key, value in node.items():
                    if key == "node_id":
                        continue
                    existing[key] = value
                if "last_seen" not in node:
                    existing["last_seen"] = _now_iso()
                self._write_json(node_path, existing)
            else:
                self._write_json(node_path, node)

    def list_nodes(self) -> list[dict]:
        """Return all registered nodes."""
        nodes_dir = self._root / "nodes"
        results = []
        for p in sorted(nodes_dir.glob("*.json")):
            results.append(self._read_json(p))
        return results

    def get_node(self, node_id: str) -> dict | None:
        """Return a single node by ID, or None if not found."""
        node_path = self._node_path(node_id)
        if not node_path.exists():
            return None
        return self._read_json(node_path)

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    def register_worker(self, worker: dict) -> None:
        """Register a worker (stale-tolerant).

        Re-registration is allowed when the existing worker file's mtime is
        older than ``self._guard_ttl`` (the stale heartbeat TTL); the prior
        record is overwritten. When the existing file is fresh, registration
        raises ValueError. When no file exists, registration succeeds.

        The existence-and-mtime check and the file write are performed under
        ``self._lock`` to close the TOCTOU window between deciding the prior
        record is stale and overwriting it.

        Raises:
            ValueError: If a live (non-stale heartbeat) worker with the same ID exists.
        """
        worker_id = worker["worker_id"]
        worker_path = self._worker_path(worker_id)
        with self._lock:
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
    # Workers (list)
    # ------------------------------------------------------------------

    def list_workers(self) -> list[dict]:
        """Read all worker files from workers/ and return them as dicts."""
        workers_dir = self._root / "workers"
        results = []
        for p in workers_dir.iterdir():
            if p.suffix != ".json":
                continue
            try:
                results.append(self._read_json(p))
            except (json.JSONDecodeError, KeyError):
                continue
        return results

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return a summary of current backend state."""
        ready = len(list((self._root / "tasks" / "ready").glob("*.json")))
        active = len(list((self._root / "tasks" / "active").glob("*.json")))
        done = len(list((self._root / "tasks" / "done").glob("*.json")))
        paused = len(list((self._root / "tasks" / "paused").glob("*.json")))
        blocked = len(list((self._root / "tasks" / "blocked").glob("*.json")))
        workers = len(list((self._root / "workers").glob("*.json")))
        nodes = len(list((self._root / "nodes").glob("*.json")))
        guards = len(list((self._root / "guards").glob("*.lock")))
        return {
            "tasks": {
                "ready": ready,
                "active": active,
                "done": done,
                "paused": paused,
                "blocked": blocked,
            },
            "workers": workers,
            "nodes": nodes,
            "guards": guards,
        }

    # ------------------------------------------------------------------
    # Missions
    # ------------------------------------------------------------------

    def _missions_dir(self) -> Path:
        return self._root / "missions"

    def _mission_path(self, mission_id: str) -> Path:
        return self._missions_dir() / f"{mission_id}.json"

    def create_mission(self, mission: dict) -> str:
        with self._lock:
            self._missions_dir().mkdir(parents=True, exist_ok=True)
            path = self._mission_path(mission["mission_id"])
            if path.exists():
                raise ValueError(f"mission '{mission['mission_id']}' already exists")
            self._write_json(path, mission)
            return mission["mission_id"]

    def get_mission(self, mission_id: str) -> dict | None:
        path = self._mission_path(mission_id)
        if not path.exists():
            return None
        try:
            return self._read_json(path)
        except (json.JSONDecodeError, KeyError):
            return None

    def list_missions(self, status: str | None = None) -> list[dict]:
        missions_dir = self._missions_dir()
        if not missions_dir.exists():
            return []
        results = []
        for p in missions_dir.iterdir():
            if p.suffix != ".json":
                continue
            try:
                data = self._read_json(p)
                if status is not None and data.get("status") != status:
                    continue
                results.append(data)
            except (json.JSONDecodeError, KeyError):
                continue
        return results

    def update_mission(self, mission_id: str, updates: dict) -> None:
        with self._lock:
            path = self._mission_path(mission_id)
            if not path.exists():
                raise FileNotFoundError(f"mission '{mission_id}' not found")
            data = self._read_json(path)
            data.update(updates)
            data["updated_at"] = _now_iso()
            self._write_json(path, data)
