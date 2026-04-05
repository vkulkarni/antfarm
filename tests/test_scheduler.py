"""Tests for the scope-aware task scheduler."""

from antfarm.core.models import Task, TaskStatus
from antfarm.core.scheduler import select_task


def make_task(
    id: str,
    priority: int = 10,
    created_at: str = "2026-01-01T00:00:00Z",
    depends_on: list[str] | None = None,
    touches: list[str] | None = None,
    capabilities_required: list[str] | None = None,
    status: TaskStatus = TaskStatus.READY,
) -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        spec="",
        created_at=created_at,
        updated_at=created_at,
        created_by="test",
        priority=priority,
        depends_on=depends_on or [],
        touches=touches or [],
        capabilities_required=capabilities_required or [],
        status=status,
    )


def test_dependency_blocks():
    """Task with unmet dependency is skipped."""
    t = make_task("t1", depends_on=["t0"])
    result = select_task([t], done_task_ids=set(), active_tasks=[])
    assert result is None


def test_dependency_allows():
    """Task with all dependencies met is eligible."""
    t = make_task("t1", depends_on=["t0"])
    result = select_task([t], done_task_ids={"t0"}, active_tasks=[])
    assert result is t


def test_scope_prefers_non_overlapping():
    """Non-overlapping task is chosen over overlapping task."""
    active = make_task("active", touches=["file_a.py"], status=TaskStatus.ACTIVE)
    overlapping = make_task("t_overlap", touches=["file_a.py"], priority=1)
    non_overlapping = make_task("t_clean", touches=["file_b.py"], priority=10)

    result = select_task(
        [overlapping, non_overlapping],
        done_task_ids=set(),
        active_tasks=[active],
    )
    assert result is non_overlapping


def test_scope_allows_overlap_when_no_alternative():
    """Overlapping task is returned when it's the only option."""
    active = make_task("active", touches=["file_a.py"], status=TaskStatus.ACTIVE)
    overlapping = make_task("t_overlap", touches=["file_a.py"])

    result = select_task(
        [overlapping],
        done_task_ids=set(),
        active_tasks=[active],
    )
    assert result is overlapping


def test_priority_ordering():
    """Lower priority number wins (priority 1 before priority 10)."""
    low_prio = make_task("t_low", priority=10)
    high_prio = make_task("t_high", priority=1)

    result = select_task([low_prio, high_prio], done_task_ids=set(), active_tasks=[])
    assert result is high_prio


def test_fifo_among_equals():
    """Among tasks with equal priority, oldest created_at wins."""
    newer = make_task("t_newer", priority=5, created_at="2026-01-02T00:00:00Z")
    older = make_task("t_older", priority=5, created_at="2026-01-01T00:00:00Z")

    result = select_task([newer, older], done_task_ids=set(), active_tasks=[])
    assert result is older


def test_empty_queue():
    """Returns None when there are no ready tasks."""
    result = select_task([], done_task_ids=set(), active_tasks=[])
    assert result is None


def test_all_deps_unmet():
    """Returns None when all tasks have unmet dependencies."""
    t1 = make_task("t1", depends_on=["missing1"])
    t2 = make_task("t2", depends_on=["missing2"])

    result = select_task([t1, t2], done_task_ids=set(), active_tasks=[])
    assert result is None


def test_no_touches():
    """Tasks without touches field work fine (treated as empty set)."""
    t = make_task("t1", touches=[])
    active = make_task("active", touches=["file_a.py"], status=TaskStatus.ACTIVE)

    result = select_task([t], done_task_ids=set(), active_tasks=[active])
    assert result is t


def test_multiple_active_scopes():
    """Multiple active tasks' touches are unioned for overlap checking."""
    active1 = make_task("a1", touches=["file_a.py"], status=TaskStatus.ACTIVE)
    active2 = make_task("a2", touches=["file_b.py"], status=TaskStatus.ACTIVE)

    overlaps_a = make_task("t_a", touches=["file_a.py"], priority=1)
    overlaps_b = make_task("t_b", touches=["file_b.py"], priority=2)
    clean = make_task("t_clean", touches=["file_c.py"], priority=10)

    result = select_task(
        [overlaps_a, overlaps_b, clean],
        done_task_ids=set(),
        active_tasks=[active1, active2],
    )
    # clean has no overlap, so it wins despite lower priority
    assert result is clean


def test_capability_match_allows_task():
    """Task is eligible when worker has all required capabilities."""
    t = make_task("t1", capabilities_required=["gpu", "docker"])
    result = select_task(
        [t],
        done_task_ids=set(),
        active_tasks=[],
        worker_capabilities={"gpu", "docker", "extra"},
    )
    assert result is t


def test_capability_mismatch_skips_task():
    """Task is skipped when worker is missing a required capability."""
    t = make_task("t1", capabilities_required=["gpu"])
    result = select_task(
        [t],
        done_task_ids=set(),
        active_tasks=[],
        worker_capabilities={"docker"},
    )
    assert result is None


def test_no_capabilities_required_always_eligible():
    """Task with no capabilities_required is eligible regardless of worker_capabilities."""
    t = make_task("t1", capabilities_required=[])
    result = select_task(
        [t],
        done_task_ids=set(),
        active_tasks=[],
        worker_capabilities=set(),
    )
    assert result is t


def test_worker_capabilities_none_skips_filter():
    """When worker_capabilities is None, capability filtering is skipped (backward compatible)."""
    t = make_task("t1", capabilities_required=["gpu"])
    result = select_task(
        [t],
        done_task_ids=set(),
        active_tasks=[],
        worker_capabilities=None,
    )
    assert result is t
