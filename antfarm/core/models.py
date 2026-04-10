"""Data model definitions for Antfarm.

Core dataclasses and enums representing Tasks, Attempts, Workers, and Nodes
in the Antfarm distributed agent orchestration system.

All timestamps are ISO 8601 strings. Priority follows Unix nice convention:
lower number = higher priority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskStatus(StrEnum):
    READY = "ready"
    ACTIVE = "active"
    DONE = "done"
    PAUSED = "paused"
    BLOCKED = "blocked"
    HARVEST_PENDING = "harvest_pending"


class AttemptStatus(StrEnum):
    ACTIVE = "active"
    DONE = "done"
    MERGED = "merged"
    SUPERSEDED = "superseded"


class WorkerStatus(StrEnum):
    IDLE = "idle"
    ACTIVE = "active"
    OFFLINE = "offline"


# ---------------------------------------------------------------------------
# v0.5 enriched lifecycle states
# ---------------------------------------------------------------------------


class TaskState(StrEnum):
    """Enriched task lifecycle states (v0.5).

    Maps to filesystem directories:
    - QUEUED, KICKED_BACK → ready/
    - BLOCKED → blocked/
    - CLAIMED, ACTIVE, HARVEST_PENDING → active/
    - DONE, MERGE_READY, MERGED, FAILED → done/
    - PAUSED → paused/
    """

    QUEUED = "queued"
    BLOCKED = "blocked"
    CLAIMED = "claimed"
    ACTIVE = "active"
    HARVEST_PENDING = "harvest_pending"
    DONE = "done"
    KICKED_BACK = "kicked_back"
    MERGE_READY = "merge_ready"
    MERGED = "merged"
    FAILED = "failed"
    PAUSED = "paused"


class AttemptState(StrEnum):
    """Enriched attempt lifecycle states (v0.5)."""

    STARTED = "started"
    HEARTBEATING = "heartbeating"
    AGENT_SUCCEEDED = "agent_succeeded"
    AGENT_FAILED = "agent_failed"
    HARVESTED = "harvested"
    STALE = "stale"
    ABANDONED = "abandoned"


class FailureType(StrEnum):
    """Classified failure types for structured failure records."""

    AGENT_CRASH = "agent_crash"
    AGENT_TIMEOUT = "agent_timeout"
    TEST_FAILURE = "test_failure"
    LINT_FAILURE = "lint_failure"
    MERGE_CONFLICT = "merge_conflict"
    BUILD_FAILURE = "build_failure"
    INFRA_FAILURE = "infra_failure"
    INVALID_TASK = "invalid_task"


# ---------------------------------------------------------------------------
# Simple entry types
# ---------------------------------------------------------------------------


@dataclass
class TrailEntry:
    ts: str
    worker_id: str
    message: str
    action_type: str | None = None  # "carry", "forage", "harvest", "kickback",
    # "merge", "review", "pause", "resume", "reassign", "block", "unblock", "failure"

    def to_dict(self) -> dict:
        d = {
            "ts": self.ts,
            "worker_id": self.worker_id,
            "message": self.message,
        }
        if self.action_type is not None:
            d["action_type"] = self.action_type
        return d

    @classmethod
    def from_dict(cls, data: dict) -> TrailEntry:
        return cls(
            ts=data["ts"],
            worker_id=data["worker_id"],
            message=data["message"],
            action_type=data.get("action_type"),
        )


@dataclass
class SignalEntry:
    ts: str
    worker_id: str
    message: str

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "worker_id": self.worker_id,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SignalEntry:
        return cls(
            ts=data["ts"],
            worker_id=data["worker_id"],
            message=data["message"],
        )


# ---------------------------------------------------------------------------
# TaskArtifact (v0.5.2)
# ---------------------------------------------------------------------------


@dataclass
class TaskArtifact:
    """Structured task output with hard evidence and advisory commentary."""

    # Identity
    task_id: str
    attempt_id: str
    worker_id: str

    # Source / freshness
    branch: str
    pr_url: str | None
    base_commit_sha: str
    head_commit_sha: str
    target_branch: str
    target_branch_sha_at_harvest: str

    # Change facts
    files_changed: list[str] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0

    # Verification facts
    build_ran: bool = False
    build_passed: bool | None = None
    tests_ran: bool = False
    tests_passed: bool | None = None
    lint_ran: bool = False
    lint_passed: bool | None = None
    verification_commands: list[str] = field(default_factory=list)

    # Deterministic merge gate
    merge_readiness: str = "needs_review"  # "ready", "needs_review", "blocked"
    blocking_reasons: list[str] = field(default_factory=list)

    # Advisory / optional (AI-generated, not used for merge gating)
    summary: str | None = None
    risks: list[str] = field(default_factory=list)
    review_focus: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "worker_id": self.worker_id,
            "branch": self.branch,
            "pr_url": self.pr_url,
            "base_commit_sha": self.base_commit_sha,
            "head_commit_sha": self.head_commit_sha,
            "target_branch": self.target_branch,
            "target_branch_sha_at_harvest": self.target_branch_sha_at_harvest,
            "files_changed": list(self.files_changed),
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "build_ran": self.build_ran,
            "build_passed": self.build_passed,
            "tests_ran": self.tests_ran,
            "tests_passed": self.tests_passed,
            "lint_ran": self.lint_ran,
            "lint_passed": self.lint_passed,
            "verification_commands": list(self.verification_commands),
            "merge_readiness": self.merge_readiness,
            "blocking_reasons": list(self.blocking_reasons),
            "summary": self.summary,
            "risks": list(self.risks),
            "review_focus": list(self.review_focus),
        }

    @classmethod
    def from_dict(cls, data: dict) -> TaskArtifact:
        return cls(
            task_id=data["task_id"],
            attempt_id=data["attempt_id"],
            worker_id=data["worker_id"],
            branch=data["branch"],
            pr_url=data.get("pr_url"),
            base_commit_sha=data["base_commit_sha"],
            head_commit_sha=data["head_commit_sha"],
            target_branch=data["target_branch"],
            target_branch_sha_at_harvest=data["target_branch_sha_at_harvest"],
            files_changed=list(data.get("files_changed", [])),
            lines_added=data.get("lines_added", 0),
            lines_removed=data.get("lines_removed", 0),
            build_ran=data.get("build_ran", False),
            build_passed=data.get("build_passed"),
            tests_ran=data.get("tests_ran", False),
            tests_passed=data.get("tests_passed"),
            lint_ran=data.get("lint_ran", False),
            lint_passed=data.get("lint_passed"),
            verification_commands=list(data.get("verification_commands", [])),
            merge_readiness=data.get("merge_readiness", "needs_review"),
            blocking_reasons=list(data.get("blocking_reasons", [])),
            summary=data.get("summary"),
            risks=list(data.get("risks", [])),
            review_focus=list(data.get("review_focus", [])),
        )


# ---------------------------------------------------------------------------
# ReviewVerdict (v0.5.2)
# ---------------------------------------------------------------------------


@dataclass
class ReviewVerdict:
    """Structured review output from a reviewer worker."""

    provider: str  # "claude_code", "codex", "human"
    verdict: str  # "pass", "needs_changes", "blocked"
    summary: str
    findings: list[str] = field(default_factory=list)
    severity: str | None = None  # "low", "medium", "high", "critical"
    reviewed_commit_sha: str = ""
    reviewer_run_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "verdict": self.verdict,
            "summary": self.summary,
            "findings": list(self.findings),
            "severity": self.severity,
            "reviewed_commit_sha": self.reviewed_commit_sha,
            "reviewer_run_id": self.reviewer_run_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ReviewVerdict:
        return cls(
            provider=data["provider"],
            verdict=data["verdict"],
            summary=data["summary"],
            findings=list(data.get("findings", [])),
            severity=data.get("severity"),
            reviewed_commit_sha=data.get("reviewed_commit_sha", ""),
            reviewer_run_id=data.get("reviewer_run_id"),
        )


# ---------------------------------------------------------------------------
# FailureRecord
# ---------------------------------------------------------------------------


@dataclass
class FailureRecord:
    """Structured failure record for classified attempt failures (v0.5)."""

    task_id: str
    attempt_id: str
    worker_id: str
    failure_type: FailureType
    message: str
    retryable: bool
    captured_at: str
    stderr_summary: str
    verification_snapshot: dict = field(default_factory=dict)
    recommended_action: str = "kickback"

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "worker_id": self.worker_id,
            "failure_type": self.failure_type.value,
            "message": self.message,
            "retryable": self.retryable,
            "captured_at": self.captured_at,
            "stderr_summary": self.stderr_summary,
            "verification_snapshot": dict(self.verification_snapshot),
            "recommended_action": self.recommended_action,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FailureRecord:
        return cls(
            task_id=data["task_id"],
            attempt_id=data["attempt_id"],
            worker_id=data["worker_id"],
            failure_type=FailureType(data["failure_type"]),
            message=data["message"],
            retryable=data["retryable"],
            captured_at=data["captured_at"],
            stderr_summary=data["stderr_summary"],
            verification_snapshot=data.get("verification_snapshot", {}),
            recommended_action=data.get("recommended_action", "kickback"),
        )


# ---------------------------------------------------------------------------
# Attempt
# ---------------------------------------------------------------------------


@dataclass
class Attempt:
    attempt_id: str
    worker_id: str | None
    status: AttemptStatus
    branch: str | None
    pr: str | None
    started_at: str
    completed_at: str | None
    artifact: dict | None = None
    review_verdict: dict | None = None

    def to_dict(self) -> dict:
        d = {
            "attempt_id": self.attempt_id,
            "worker_id": self.worker_id,
            "status": self.status.value,
            "branch": self.branch,
            "pr": self.pr,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }
        if self.artifact is not None:
            d["artifact"] = self.artifact
        if self.review_verdict is not None:
            d["review_verdict"] = self.review_verdict
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Attempt:
        return cls(
            attempt_id=data["attempt_id"],
            worker_id=data.get("worker_id"),
            status=AttemptStatus(data["status"]),
            branch=data.get("branch"),
            pr=data.get("pr"),
            started_at=data["started_at"],
            completed_at=data.get("completed_at"),
            artifact=data.get("artifact"),
            review_verdict=data.get("review_verdict"),
        )


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


@dataclass
class Task:
    id: str
    title: str
    spec: str
    created_at: str
    updated_at: str
    created_by: str
    complexity: str = "M"
    priority: int = field(default=10)
    depends_on: list[str] = field(default_factory=list)
    touches: list[str] = field(default_factory=list)
    capabilities_required: list[str] = field(default_factory=list)
    pinned_to: str | None = None
    merge_override: int | None = None
    max_attempts: int | None = None
    status: TaskStatus = TaskStatus.READY
    current_attempt: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    trail: list[TrailEntry] = field(default_factory=list)
    signals: list[SignalEntry] = field(default_factory=list)
    mission_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "spec": self.spec,
            "complexity": self.complexity,
            "priority": self.priority,
            "depends_on": list(self.depends_on),
            "touches": list(self.touches),
            "capabilities_required": list(self.capabilities_required),
            "pinned_to": self.pinned_to,
            "merge_override": self.merge_override,
            "max_attempts": self.max_attempts,
            "status": self.status.value,
            "current_attempt": self.current_attempt,
            "attempts": [a.to_dict() for a in self.attempts],
            "trail": [t.to_dict() for t in self.trail],
            "signals": [s.to_dict() for s in self.signals],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_by": self.created_by,
            "mission_id": self.mission_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Task:
        return cls(
            id=data["id"],
            title=data["title"],
            spec=data["spec"],
            complexity=data.get("complexity", "M"),
            priority=data.get("priority", 10),
            depends_on=list(data.get("depends_on", [])),
            touches=list(data.get("touches", [])),
            capabilities_required=list(data.get("capabilities_required", [])),
            pinned_to=data.get("pinned_to"),
            merge_override=data.get("merge_override"),
            max_attempts=data.get("max_attempts"),
            status=TaskStatus(data.get("status", TaskStatus.READY)),
            current_attempt=data.get("current_attempt"),
            attempts=[Attempt.from_dict(a) for a in data.get("attempts", [])],
            trail=[TrailEntry.from_dict(t) for t in data.get("trail", [])],
            signals=[SignalEntry.from_dict(s) for s in data.get("signals", [])],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            created_by=data["created_by"],
            mission_id=data.get("mission_id"),
        )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@dataclass
class Worker:
    worker_id: str
    node_id: str
    agent_type: str
    workspace_root: str
    registered_at: str
    last_heartbeat: str
    status: WorkerStatus = WorkerStatus.IDLE
    capabilities: list[str] = field(default_factory=list)
    cooldown_until: str | None = None

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "node_id": self.node_id,
            "agent_type": self.agent_type,
            "workspace_root": self.workspace_root,
            "status": self.status.value,
            "capabilities": list(self.capabilities),
            "registered_at": self.registered_at,
            "last_heartbeat": self.last_heartbeat,
            "cooldown_until": self.cooldown_until,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Worker:
        return cls(
            worker_id=data["worker_id"],
            node_id=data["node_id"],
            agent_type=data["agent_type"],
            workspace_root=data["workspace_root"],
            status=WorkerStatus(data.get("status", WorkerStatus.IDLE)),
            capabilities=list(data.get("capabilities", [])),
            registered_at=data["registered_at"],
            last_heartbeat=data["last_heartbeat"],
            cooldown_until=data.get("cooldown_until"),
        )


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


@dataclass
class Node:
    node_id: str
    joined_at: str
    last_seen: str

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "joined_at": self.joined_at,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Node:
        return cls(
            node_id=data["node_id"],
            joined_at=data["joined_at"],
            last_seen=data["last_seen"],
        )
