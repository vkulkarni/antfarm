"""Scope-aware task scheduler for Antfarm.

Selects the next task from a queue of ready tasks using a deterministic
scheduling policy that respects dependencies, scope isolation, priority,
and FIFO ordering.
"""

from antfarm.core.models import Task


def select_task(
    ready_tasks: list[Task],
    done_task_ids: set[str],
    active_tasks: list[Task],
    worker_capabilities: set[str] | None = None,
    worker_id: str | None = None,
    hotspots: dict[str, float] | None = None,
) -> Task | None:
    """Select next task using scheduling policy with hotspot awareness.

    Policy (applied in order):
    1. Dependency check — skip if depends_on not all in done_task_ids
    2. Capability check — skip if capabilities_required not a subset of worker_capabilities
    3. Pin check — skip if pinned_to is set and does not match worker_id
    4. Scope preference — prefer non-overlapping touches with active tasks
    5. Hotspot weighting — among non-overlapping, prefer tasks touching cooler scopes
    6. Priority — lower number = higher priority
    7. FIFO — oldest created_at first among equals

    Args:
        ready_tasks: Tasks with status READY that are candidates for scheduling.
        done_task_ids: Set of task IDs that have been completed.
        active_tasks: Tasks currently being executed by workers.
        worker_capabilities: Set of capabilities the worker has. If None, capability
            filtering is skipped (backward compatible).
        worker_id: ID of the worker pulling tasks. If None, pin filtering is skipped.
        hotspots: Optional dict of scope → heat score (0.0-1.0). Tasks touching
            hotter scopes are deprioritized (sorted later). Soft signal, not a ban.

    Returns:
        The selected Task, or None if no eligible task exists.
    """
    # Step 1: Filter to tasks with all dependencies satisfied
    eligible = [
        t for t in ready_tasks
        if all(dep in done_task_ids for dep in t.depends_on)
    ]

    # Step 2: Filter by capability requirements (skip if worker_capabilities is None)
    if worker_capabilities is not None:
        eligible = [
            t for t in eligible
            if set(t.capabilities_required).issubset(worker_capabilities)
        ]

    # Step 3: Filter by pin — skip tasks pinned to a different worker
    if worker_id is not None:
        eligible = [
            t for t in eligible
            if t.pinned_to is None or t.pinned_to == worker_id
        ]

    if not eligible:
        return None

    # Step 2: Collect all file/scope touches from active tasks
    active_touches: set[str] = set()
    for t in active_tasks:
        active_touches.update(t.touches)

    # Step 3: Split into non-overlapping and overlapping groups
    non_overlapping = [t for t in eligible if not set(t.touches) & active_touches]
    overlapping = [t for t in eligible if set(t.touches) & active_touches]

    # Step 4: Prefer non-overlapping; fall back to overlapping
    chosen_group = non_overlapping if non_overlapping else overlapping

    if not chosen_group:
        return None

    # Step 5: Sort by hotspot heat (cooler first), priority, then FIFO
    def _sort_key(t: Task) -> tuple:
        heat = 0.0
        if hotspots and t.touches:
            heat = max((hotspots.get(s, 0.0) for s in t.touches), default=0.0)
        return (heat, t.priority, t.created_at)

    chosen_group.sort(key=_sort_key)

    return chosen_group[0]
