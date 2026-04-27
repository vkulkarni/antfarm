"""Mission model and helpers for Antfarm v0.6.

Defines Mission, MissionConfig, PlanArtifact, MissionReport, and related
dataclasses. Also provides is_infra_task() and link_task_to_mission().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from antfarm.core.backends.base import TaskBackend

# ---------------------------------------------------------------------------
# Validation constants (module-level to avoid dataclass ClassVar confusion)
# ---------------------------------------------------------------------------

VALID_COMPLETION_MODES = ("best_effort", "all_or_nothing")
VALID_BLOCKED_TIMEOUT_ACTIONS = ("wait", "fail")
VALID_AUTO_MERGE_MODES = ("never", "on-review-pass", "on-review-pass-and-ci-green")
VALID_BUDGET_ACTIONS = ("pause", "cancel")

# Bounded FIFO of recent UsageEvent IDs kept per-mission for dedup.
_SEEN_EVENT_ID_CAP = 1000


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MissionStatus(StrEnum):
    PLANNING = "planning"
    REVIEWING_PLAN = "reviewing_plan"
    BUILDING = "building"
    BLOCKED = "blocked"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# MissionConfig
# ---------------------------------------------------------------------------


@dataclass
class MissionConfig:
    max_attempts: int = 3
    # Per-mission override for Queen.config.max_re_plans. None defers to the
    # Queen-wide default. Negative values are rejected.
    max_re_plans: int | None = None
    max_parallel_builders: int = 4
    require_plan_review: bool = True
    stall_threshold_minutes: int = 30
    completion_mode: str = "best_effort"
    test_command: list[str] | None = None
    integration_branch: str = "main"
    blocked_timeout_action: str = "wait"
    blocked_timeout_minutes: int = 120
    auto_merge: str = "never"
    allow_auto_merge_on_external: bool = False
    # Budget tripwire (v0.6.14 — issue #354). None means no limit.
    max_cost_usd: float | None = None
    max_tokens: int | None = None
    budget_action: str = "pause"

    def __post_init__(self) -> None:
        if self.completion_mode not in VALID_COMPLETION_MODES:
            raise ValueError(f"completion_mode must be one of {VALID_COMPLETION_MODES}")
        if self.blocked_timeout_action not in VALID_BLOCKED_TIMEOUT_ACTIONS:
            raise ValueError(
                f"blocked_timeout_action must be one of {VALID_BLOCKED_TIMEOUT_ACTIONS}"
            )
        if self.auto_merge not in VALID_AUTO_MERGE_MODES:
            raise ValueError(f"auto_merge must be one of {VALID_AUTO_MERGE_MODES}")
        if self.budget_action not in VALID_BUDGET_ACTIONS:
            raise ValueError(f"budget_action must be one of {VALID_BUDGET_ACTIONS}")
        if self.max_re_plans is not None and self.max_re_plans < 0:
            raise ValueError("max_re_plans must be non-negative")

    def to_dict(self) -> dict:
        return {
            "max_attempts": self.max_attempts,
            "max_re_plans": self.max_re_plans,
            "max_parallel_builders": self.max_parallel_builders,
            "require_plan_review": self.require_plan_review,
            "stall_threshold_minutes": self.stall_threshold_minutes,
            "completion_mode": self.completion_mode,
            "test_command": list(self.test_command) if self.test_command is not None else None,
            "integration_branch": self.integration_branch,
            "blocked_timeout_action": self.blocked_timeout_action,
            "blocked_timeout_minutes": self.blocked_timeout_minutes,
            "auto_merge": self.auto_merge,
            "allow_auto_merge_on_external": self.allow_auto_merge_on_external,
            "max_cost_usd": self.max_cost_usd,
            "max_tokens": self.max_tokens,
            "budget_action": self.budget_action,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MissionConfig:
        return cls(
            max_attempts=data.get("max_attempts", 3),
            max_re_plans=data.get("max_re_plans"),
            max_parallel_builders=data.get("max_parallel_builders", 4),
            require_plan_review=data.get("require_plan_review", True),
            stall_threshold_minutes=data.get("stall_threshold_minutes", 30),
            completion_mode=data.get("completion_mode", "best_effort"),
            test_command=(
                list(data["test_command"]) if data.get("test_command") is not None else None
            ),
            integration_branch=data.get("integration_branch", "main"),
            blocked_timeout_action=data.get("blocked_timeout_action", "wait"),
            blocked_timeout_minutes=data.get("blocked_timeout_minutes", 120),
            auto_merge=data.get("auto_merge", "never"),
            allow_auto_merge_on_external=data.get("allow_auto_merge_on_external", False),
            max_cost_usd=data.get("max_cost_usd"),
            max_tokens=data.get("max_tokens"),
            budget_action=data.get("budget_action", "pause"),
        )


# ---------------------------------------------------------------------------
# PlanArtifact
# ---------------------------------------------------------------------------


@dataclass
class PlanArtifact:
    plan_task_id: str
    attempt_id: str
    proposed_tasks: list[dict]
    task_count: int
    warnings: list[str] = field(default_factory=list)
    dependency_summary: str = ""

    def to_dict(self) -> dict:
        return {
            "plan_task_id": self.plan_task_id,
            "attempt_id": self.attempt_id,
            "proposed_tasks": list(self.proposed_tasks),
            "task_count": self.task_count,
            "warnings": list(self.warnings),
            "dependency_summary": self.dependency_summary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PlanArtifact:
        return cls(
            plan_task_id=data["plan_task_id"],
            attempt_id=data["attempt_id"],
            proposed_tasks=list(data.get("proposed_tasks", [])),
            task_count=data["task_count"],
            warnings=list(data.get("warnings", [])),
            dependency_summary=data.get("dependency_summary", ""),
        )


# ---------------------------------------------------------------------------
# MissionReportTask
# ---------------------------------------------------------------------------


@dataclass
class MissionReportTask:
    task_id: str
    title: str
    pr_url: str | None
    lines_added: int
    lines_removed: int
    files_changed: list[str]

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "pr_url": self.pr_url,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "files_changed": list(self.files_changed),
        }

    @classmethod
    def from_dict(cls, data: dict) -> MissionReportTask:
        return cls(
            task_id=data["task_id"],
            title=data["title"],
            pr_url=data.get("pr_url"),
            lines_added=data.get("lines_added", 0),
            lines_removed=data.get("lines_removed", 0),
            files_changed=list(data.get("files_changed", [])),
        )


# ---------------------------------------------------------------------------
# MissionReportBlocked
# ---------------------------------------------------------------------------


@dataclass
class MissionReportBlocked:
    task_id: str
    title: str
    reason: str
    attempt_count: int
    last_failure_type: str | None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "reason": self.reason,
            "attempt_count": self.attempt_count,
            "last_failure_type": self.last_failure_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MissionReportBlocked:
        return cls(
            task_id=data["task_id"],
            title=data["title"],
            reason=data["reason"],
            attempt_count=data.get("attempt_count", 0),
            last_failure_type=data.get("last_failure_type"),
        )


# ---------------------------------------------------------------------------
# MissionReport
# ---------------------------------------------------------------------------


@dataclass
class MissionReport:
    mission_id: str
    spec_summary: str
    status: MissionStatus
    completion_mode: str
    duration_minutes: float
    total_tasks: int
    merged_tasks: int
    blocked_tasks: int
    failed_reviews: int
    merged: list[MissionReportTask] = field(default_factory=list)
    blocked: list[MissionReportBlocked] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    pr_urls: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    total_lines_added: int = 0
    total_lines_removed: int = 0
    files_changed: list[str] = field(default_factory=list)
    generated_at: str = ""
    auto_merged_tasks: int = 0
    manual_merged_tasks: int = 0

    def to_dict(self) -> dict:
        return {
            "mission_id": self.mission_id,
            "spec_summary": self.spec_summary,
            "status": self.status.value,
            "completion_mode": self.completion_mode,
            "duration_minutes": self.duration_minutes,
            "total_tasks": self.total_tasks,
            "merged_tasks": self.merged_tasks,
            "blocked_tasks": self.blocked_tasks,
            "failed_reviews": self.failed_reviews,
            "merged": [m.to_dict() for m in self.merged],
            "blocked": [b.to_dict() for b in self.blocked],
            "risks": list(self.risks),
            "pr_urls": list(self.pr_urls),
            "branches": list(self.branches),
            "total_lines_added": self.total_lines_added,
            "total_lines_removed": self.total_lines_removed,
            "files_changed": list(self.files_changed),
            "generated_at": self.generated_at,
            "auto_merged_tasks": self.auto_merged_tasks,
            "manual_merged_tasks": self.manual_merged_tasks,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MissionReport:
        return cls(
            mission_id=data["mission_id"],
            spec_summary=data["spec_summary"],
            status=MissionStatus(data["status"]),
            completion_mode=data["completion_mode"],
            duration_minutes=data["duration_minutes"],
            total_tasks=data["total_tasks"],
            merged_tasks=data["merged_tasks"],
            blocked_tasks=data["blocked_tasks"],
            failed_reviews=data["failed_reviews"],
            merged=[MissionReportTask.from_dict(m) for m in data.get("merged", [])],
            blocked=[MissionReportBlocked.from_dict(b) for b in data.get("blocked", [])],
            risks=list(data.get("risks", [])),
            pr_urls=list(data.get("pr_urls", [])),
            branches=list(data.get("branches", [])),
            total_lines_added=data.get("total_lines_added", 0),
            total_lines_removed=data.get("total_lines_removed", 0),
            files_changed=list(data.get("files_changed", [])),
            generated_at=data.get("generated_at", ""),
            auto_merged_tasks=data.get("auto_merged_tasks", 0),
            manual_merged_tasks=data.get("manual_merged_tasks", 0),
        )


# ---------------------------------------------------------------------------
# Mission
# ---------------------------------------------------------------------------


@dataclass
class Mission:
    mission_id: str
    spec: str
    spec_file: str | None
    status: MissionStatus
    plan_task_id: str | None
    plan_artifact: PlanArtifact | None
    task_ids: list[str]
    blocked_task_ids: list[str]
    config: MissionConfig
    created_at: str
    updated_at: str
    completed_at: str | None
    report: MissionReport | None
    last_progress_at: str
    re_plan_count: int = 0
    mission_context_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "mission_id": self.mission_id,
            "spec": self.spec,
            "spec_file": self.spec_file,
            "status": self.status.value,
            "plan_task_id": self.plan_task_id,
            "plan_artifact": self.plan_artifact.to_dict() if self.plan_artifact else None,
            "task_ids": list(self.task_ids),
            "blocked_task_ids": list(self.blocked_task_ids),
            "config": self.config.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "report": self.report.to_dict() if self.report else None,
            "last_progress_at": self.last_progress_at,
            "re_plan_count": self.re_plan_count,
            "mission_context_path": self.mission_context_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Mission:
        return cls(
            mission_id=data["mission_id"],
            spec=data["spec"],
            spec_file=data.get("spec_file"),
            status=MissionStatus(data["status"]),
            plan_task_id=data.get("plan_task_id"),
            plan_artifact=(
                PlanArtifact.from_dict(data["plan_artifact"]) if data.get("plan_artifact") else None
            ),
            task_ids=list(data.get("task_ids", [])),
            blocked_task_ids=list(data.get("blocked_task_ids", [])),
            config=MissionConfig.from_dict(data.get("config", {})),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            completed_at=data.get("completed_at"),
            report=(MissionReport.from_dict(data["report"]) if data.get("report") else None),
            last_progress_at=data.get("last_progress_at", ""),
            re_plan_count=data.get("re_plan_count", 0),
            mission_context_path=data.get("mission_context_path"),
        )


# ---------------------------------------------------------------------------
# Task-kind filter (canonical, shared)
# ---------------------------------------------------------------------------


def is_infra_task(task: dict) -> bool:
    """Return True if the task is a plan or review task (infrastructure),
    not an implementation task.

    Used by Queen, report.py, autoscaler.py, and TUI to partition mission
    tasks into "infra" (plan/review) vs "impl" (builder work). All callers
    MUST use this function — do not reimplement the filter.
    """
    caps = task.get("capabilities_required", [])
    return "plan" in caps or "review" in caps or task.get("id", "").startswith("review-")


# ---------------------------------------------------------------------------
# link_task_to_mission
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MissionUsage (v0.6 cost tripwire)
# ---------------------------------------------------------------------------


@dataclass
class MissionUsage:
    """Aggregated cost/token counters for a mission.

    Persisted as a sidecar JSON file at
    ``.antfarm/missions/<mission_id>_usage.json``. Mutations happen under
    FileBackend._lock; idempotency against duplicate UsageEvents is enforced
    by a bounded FIFO of seen event IDs.
    """

    mission_id: str
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    event_count: int = 0
    first_event_at: str | None = None
    last_event_at: str | None = None
    # task_id → {cost_usd, input_tokens, output_tokens, cache_read_tokens,
    #            cache_creation_tokens, event_count, top_attempt_id,
    #            top_attempt_cost}
    per_task: dict[str, dict] = field(default_factory=dict)
    # FIFO of already-applied UsageEvent IDs, capped at _SEEN_EVENT_ID_CAP.
    seen_event_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mission_id": self.mission_id,
            "total_cost_usd": self.total_cost_usd,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "total_cache_creation_tokens": self.total_cache_creation_tokens,
            "event_count": self.event_count,
            "first_event_at": self.first_event_at,
            "last_event_at": self.last_event_at,
            "per_task": {k: dict(v) for k, v in self.per_task.items()},
            "seen_event_ids": list(self.seen_event_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> MissionUsage:
        return cls(
            mission_id=data["mission_id"],
            total_cost_usd=float(data.get("total_cost_usd", 0.0)),
            total_input_tokens=int(data.get("total_input_tokens", 0)),
            total_output_tokens=int(data.get("total_output_tokens", 0)),
            total_cache_read_tokens=int(data.get("total_cache_read_tokens", 0)),
            total_cache_creation_tokens=int(data.get("total_cache_creation_tokens", 0)),
            event_count=int(data.get("event_count", 0)),
            first_event_at=data.get("first_event_at"),
            last_event_at=data.get("last_event_at"),
            per_task={k: dict(v) for k, v in data.get("per_task", {}).items()},
            seen_event_ids=list(data.get("seen_event_ids", [])),
        )

    def apply(self, event) -> None:
        """Apply a UsageEvent to the aggregates. Idempotent by event_id.

        ``event`` is either a ``UsageEvent`` dataclass or its ``to_dict()``
        form — both are accepted so callers don't need to round-trip.
        """
        # Accept dict or dataclass — keeps the call sites simple.
        ed = event.to_dict() if hasattr(event, "to_dict") else dict(event)

        event_id = ed.get("event_id")
        if not event_id:
            return
        if event_id in self.seen_event_ids:
            return

        cost = float(ed.get("cost_usd", 0.0))
        input_tok = int(ed.get("input_tokens", 0))
        output_tok = int(ed.get("output_tokens", 0))
        cache_r = int(ed.get("cache_read_tokens", 0))
        cache_c = int(ed.get("cache_creation_tokens", 0))
        ts = ed.get("ts")

        self.total_cost_usd += cost
        self.total_input_tokens += input_tok
        self.total_output_tokens += output_tok
        self.total_cache_read_tokens += cache_r
        self.total_cache_creation_tokens += cache_c
        self.event_count += 1

        if ts is not None:
            if self.first_event_at is None:
                self.first_event_at = ts
            self.last_event_at = ts

        task_id = ed.get("task_id")
        attempt_id = ed.get("attempt_id")
        if task_id:
            bucket = self.per_task.setdefault(
                task_id,
                {
                    "cost_usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "event_count": 0,
                    "top_attempt_id": None,
                    "top_attempt_cost": 0.0,
                    "per_attempt": {},
                },
            )
            bucket["cost_usd"] = float(bucket.get("cost_usd", 0.0)) + cost
            bucket["input_tokens"] = int(bucket.get("input_tokens", 0)) + input_tok
            bucket["output_tokens"] = int(bucket.get("output_tokens", 0)) + output_tok
            bucket["cache_read_tokens"] = int(bucket.get("cache_read_tokens", 0)) + cache_r
            bucket["cache_creation_tokens"] = int(bucket.get("cache_creation_tokens", 0)) + cache_c
            bucket["event_count"] = int(bucket.get("event_count", 0)) + 1

            # Track per-attempt cost so we can surface the most expensive
            # attempt for this task — useful when a task churned and cost
            # blew up across kickbacks.
            per_attempt = bucket.setdefault("per_attempt", {})
            if attempt_id:
                prev = float(per_attempt.get(attempt_id, 0.0))
                new_attempt_cost = prev + cost
                per_attempt[attempt_id] = new_attempt_cost
                if new_attempt_cost > float(bucket.get("top_attempt_cost", 0.0)):
                    bucket["top_attempt_id"] = attempt_id
                    bucket["top_attempt_cost"] = new_attempt_cost

        # Record seen event_id; drop oldest when over the cap.
        self.seen_event_ids.append(event_id)
        overflow = len(self.seen_event_ids) - _SEEN_EVENT_ID_CAP
        if overflow > 0:
            del self.seen_event_ids[:overflow]


def link_task_to_mission(
    backend: TaskBackend,
    task_dict: dict,
    mission_id: str,
) -> str:
    """Carry a task and atomically append its ID to the parent mission's task_ids.

    Both operations happen under the backend's internal lock (for FileBackend,
    this is ``_lock``). The HTTP handler and Soldier do NOT reference the lock
    directly — this helper owns the atomicity contract.

    Args:
        backend: The active TaskBackend instance.
        task_dict: Full task dict (must already have ``mission_id`` set).
        mission_id: The parent mission ID.

    Returns:
        The task ID of the newly created task.

    Raises:
        FileNotFoundError: If the mission does not exist.
        ValueError: If the mission is in a terminal state.
    """
    mission = backend.get_mission(mission_id)
    if mission is None:
        raise FileNotFoundError(f"mission '{mission_id}' not found")
    if mission["status"] in ("complete", "failed", "cancelled"):
        raise ValueError(
            f"cannot add tasks to mission '{mission_id}' in terminal state '{mission['status']}'"
        )
    task_id = backend.carry(task_dict)
    backend.update_mission(
        mission_id,
        {
            "task_ids": mission["task_ids"] + [task_id],
        },
    )
    return task_id
