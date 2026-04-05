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
) -> Task | None:
    """Select next task using v0.1 scheduling policy.

    Policy (applied in order):
    1. Dependency check — skip if depends_on not all in done_task_ids
    2. Capability check — skip if capabilities_required not a subset of worker_capabilities
    3. Scope preference — prefer non-overlapping touches with active tasks
    4. Priority — lower number = higher priority
    5. FIFO — oldest created_at first among equals

    Args:
        ready_tasks: Tasks with status READY that are candidates for scheduling.
        done_task_ids: Set of task IDs that have been completed.
        active_tasks: Tasks currently being executed by workers.
        worker_capabilities: Set of capabilities the worker has. If None, capability
            filtering is skipped (backward compatible).

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

    # Step 5: Sort by priority (ascending), then by created_at (ascending = oldest first)
    chosen_group.sort(key=lambda t: (t.priority, t.created_at))

    return chosen_group[0]
