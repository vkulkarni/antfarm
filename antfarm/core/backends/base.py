"""Abstract base class for Antfarm task backends.

Defines the TaskBackend interface with explicit mutation methods.
No generic update(**fields) — each method enforces a valid state transition.

Backend implementations: FileBackend (v0.1), PostgresBackend (v0.2+).
"""

from abc import ABC, abstractmethod


class TaskBackend(ABC):
    # --- Task lifecycle ---

    @abstractmethod
    def carry(self, task: dict) -> str:
        """Add a task to the queue.

        Args:
            task: Task dict with at minimum 'id', 'title', 'spec', 'created_at',
                  'updated_at', 'created_by'.

        Returns:
            The task ID.

        Raises:
            ValueError: If a task with the same ID already exists.
        """
        ...

    @abstractmethod
    def pull(self, worker_id: str) -> dict | None:
        """Claim next eligible task for a worker. Creates a new attempt. Atomic.

        Args:
            worker_id: ID of the worker claiming the task.

        Returns:
            The claimed task dict (with a new ACTIVE attempt), or None if no
            eligible task exists.
        """
        ...

    @abstractmethod
    def append_trail(self, task_id: str, entry: dict) -> None:
        """Append a TrailEntry dict to the task's trail list.

        Args:
            task_id: ID of the task.
            entry: TrailEntry dict with 'ts', 'worker_id', 'message'.
        """
        ...

    @abstractmethod
    def append_signal(self, task_id: str, entry: dict) -> None:
        """Append a SignalEntry dict to the task's signals list.

        Args:
            task_id: ID of the task.
            entry: SignalEntry dict with 'ts', 'worker_id', 'message'.
        """
        ...

    @abstractmethod
    def mark_harvested(
        self,
        task_id: str,
        attempt_id: str,
        pr: str,
        branch: str,
        artifact: dict | None = None,
    ) -> None:
        """Transition task to DONE, attempt to DONE.

        Idempotent: if task is already DONE with this attempt_id, no-op.

        Args:
            task_id: ID of the task.
            attempt_id: ID of the attempt being completed.
            pr: Pull request URL or identifier.
            branch: Branch name for the completed work.
            artifact: Optional TaskArtifact dict to store on the attempt.

        Raises:
            ValueError: If attempt_id is not the current attempt on the task.
        """
        ...

    @abstractmethod
    def kickback(
        self, task_id: str, reason: str, max_attempts: int = 3
    ) -> None:
        """Transition task to READY (or BLOCKED if attempts exhausted).

        Sets current_attempt to None. Next pull() creates a fresh attempt.
        Adds a failure TrailEntry with the reason.

        If the total completed/superseded attempts >= effective max,
        the task transitions to BLOCKED instead of READY. Per-task
        ``max_attempts`` overrides the function parameter.

        Args:
            task_id: ID of the task.
            reason: Human-readable reason for the kickback.
            max_attempts: Default max before blocking.
        """
        ...

    @abstractmethod
    def mark_harvest_pending(self, task_id: str, attempt_id: str) -> None:
        """Transition task from ACTIVE to HARVEST_PENDING.

        Called by the worker after agent execution completes but before
        writing artifact/failure data. If the worker dies between this call
        and mark_harvested/mark_failed, the inbox surfaces it.

        Args:
            task_id: ID of the task.
            attempt_id: ID of the current attempt.

        Raises:
            ValueError: If attempt_id is not the current attempt.
            FileNotFoundError: If the task is not found in active state.
        """
        ...

    @abstractmethod
    def store_review_verdict(
        self, task_id: str, attempt_id: str, verdict: dict
    ) -> None:
        """Store a ReviewVerdict on the task's current attempt.

        Args:
            task_id: ID of the task being reviewed.
            attempt_id: ID of the attempt the verdict applies to.
            verdict: ReviewVerdict.to_dict() output.

        Raises:
            ValueError: If attempt_id is not the current attempt.
            FileNotFoundError: If the task is not found in done/.
        """
        ...

    @abstractmethod
    def mark_merged(self, task_id: str, attempt_id: str) -> None:
        """Mark attempt as MERGED. Task stays DONE in done/ folder.

        Merged state is tracked on the attempt, not the task status.

        Args:
            task_id: ID of the task.
            attempt_id: ID of the attempt that was merged.
        """
        ...

    @abstractmethod
    def rereview(
        self,
        review_task_id: str,
        new_spec: str,
        touches: list[str],
    ) -> None:
        """Re-ready an existing review task for a re-attempted parent task.

        Used when the parent task has a new current_attempt (new SHA) that
        needs re-review. Moves the review task back to the ready queue,
        supersedes its current attempt (if any), and updates the spec/touches
        to reference the new parent attempt.

        Args:
            review_task_id: ID of the existing review task (e.g. 'review-task-001').
            new_spec: Replacement spec text referencing the new parent attempt.
            touches: Replacement touches list.

        Raises:
            FileNotFoundError: If the review task does not exist.
        """
        ...

    @abstractmethod
    def pause_task(self, task_id: str) -> None:
        """Pause an active task. Moves task to PAUSED state.

        Args:
            task_id: ID of the task to pause.

        Raises:
            FileNotFoundError: If the task is not found.
            ValueError: If the task is not in ACTIVE state.
        """
        ...

    @abstractmethod
    def resume_task(self, task_id: str) -> None:
        """Resume a paused task. Moves task back to READY state.

        Args:
            task_id: ID of the task to resume.

        Raises:
            FileNotFoundError: If the task is not found.
            ValueError: If the task is not in PAUSED state.
        """
        ...

    @abstractmethod
    def reassign_task(self, task_id: str, worker_id: str) -> None:
        """Reassign an active task. Supersedes current attempt, returns to READY.

        Args:
            task_id: ID of the task to reassign.
            worker_id: New worker ID (recorded in trail for context).

        Raises:
            FileNotFoundError: If the task is not found.
            ValueError: If the task is not in ACTIVE state.
        """
        ...

    @abstractmethod
    def block_task(self, task_id: str, reason: str) -> None:
        """Block a task. Moves task to BLOCKED state with a reason.

        Args:
            task_id: ID of the task to block.
            reason: Human-readable reason for blocking.

        Raises:
            FileNotFoundError: If the task is not found.
            ValueError: If the task is not in READY state.
        """
        ...

    @abstractmethod
    def pin_task(self, task_id: str, worker_id: str) -> None:
        """Pin a ready task to a specific worker.

        Args:
            task_id: ID of the task to pin.
            worker_id: Worker ID the task is pinned to.

        Raises:
            FileNotFoundError: If the task is not found in ready/.
        """
        ...

    @abstractmethod
    def unpin_task(self, task_id: str) -> None:
        """Clear the pin on a ready task.

        Args:
            task_id: ID of the task to unpin.

        Raises:
            FileNotFoundError: If the task is not found in ready/.
        """
        ...

    @abstractmethod
    def override_merge_order(self, task_id: str, position: int) -> None:
        """Set merge queue position override on a done task.

        Args:
            task_id: ID of the task to override.
            position: Override position (lower = merges sooner, before non-overridden tasks).

        Raises:
            FileNotFoundError: If the task is not found in done/.
        """
        ...

    @abstractmethod
    def clear_merge_override(self, task_id: str) -> None:
        """Clear merge queue position override on a done task.

        Args:
            task_id: ID of the task to clear override for.

        Raises:
            FileNotFoundError: If the task is not found in done/.
        """
        ...

    @abstractmethod
    def unblock_task(self, task_id: str) -> None:
        """Unblock a blocked task. Moves task back to READY state.

        Args:
            task_id: ID of the task to unblock.

        Raises:
            FileNotFoundError: If the task is not found.
            ValueError: If the task is not in BLOCKED state.
        """
        ...

    @abstractmethod
    def list_tasks(self, status: str | None = None) -> list[dict]:
        """List tasks, optionally filtered by status.

        Args:
            status: Optional status filter ('ready', 'active', 'done').

        Returns:
            List of task dicts.
        """
        ...

    @abstractmethod
    def get_task(self, task_id: str) -> dict | None:
        """Get a single task by ID.

        Args:
            task_id: ID of the task.

        Returns:
            Task dict, or None if not found.
        """
        ...

    # --- Guards (distributed locks) ---

    @abstractmethod
    def guard(self, resource: str, owner: str) -> bool:
        """Acquire an exclusive guard on a resource.

        Args:
            resource: Resource identifier to lock.
            owner: Worker ID acquiring the guard.

        Returns:
            True if guard was acquired, False if already held by another owner.
        """
        ...

    @abstractmethod
    def release_guard(self, resource: str, owner: str) -> None:
        """Release a guard. Only the owner can release.

        Args:
            resource: Resource identifier to unlock.
            owner: Worker ID that holds the guard.

        Raises:
            PermissionError: If owner does not match the guard's recorded owner.
            FileNotFoundError: If no guard exists for the resource.
        """
        ...

    # --- Nodes ---

    @abstractmethod
    def register_node(self, node: dict) -> None:
        """Register a node. Idempotent — updates last_seen if already registered.

        Args:
            node: Node dict with 'node_id', 'joined_at', 'last_seen'.
        """
        ...

    @abstractmethod
    def list_nodes(self) -> list[dict]:
        """Return all registered nodes."""
        ...

    @abstractmethod
    def get_node(self, node_id: str) -> dict | None:
        """Return a single node by ID, or None if not found."""
        ...

    # --- Workers ---

    @abstractmethod
    def register_worker(self, worker: dict) -> None:
        """Register a worker.

        Args:
            worker: Worker dict with 'worker_id', 'node_id', etc.

        Raises:
            ValueError: If a live (non-stale) worker with the same ID already exists.
        """
        ...

    @abstractmethod
    def deregister_worker(self, worker_id: str) -> None:
        """Deregister a worker. No-op if the worker is not registered.

        Args:
            worker_id: ID of the worker to deregister.
        """
        ...

    @abstractmethod
    def heartbeat(self, worker_id: str, status: dict) -> None:
        """Update worker presence and status.

        Args:
            worker_id: ID of the worker sending the heartbeat.
            status: Status dict to persist (e.g. current task, state).
        """
        ...

    # --- Status ---

    @abstractmethod
    def list_workers(self) -> list[dict]:
        """List all registered workers with their current state.

        Returns:
            List of worker dicts, each including rate limit fields if present.
        """
        ...

    @abstractmethod
    def status(self) -> dict:
        """Return backend status summary.

        Returns:
            Dict with counts and health info (e.g. ready/active/done task counts,
            registered worker count).
        """
        ...

    # --- Missions ---

    @abstractmethod
    def create_mission(self, mission: dict) -> str:
        """Create a mission. Raises ValueError if mission_id already exists."""
        ...

    @abstractmethod
    def get_mission(self, mission_id: str) -> dict | None:
        """Get a mission by ID. Returns None if not found."""
        ...

    @abstractmethod
    def list_missions(self, status: str | None = None) -> list[dict]:
        """List missions, optionally filtered by status."""
        ...

    @abstractmethod
    def update_mission(self, mission_id: str, updates: dict) -> None:
        """Shallow-merge ``updates`` into the mission JSON. Atomic write."""
        ...
