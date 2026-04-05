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
# Simple entry types
# ---------------------------------------------------------------------------


@dataclass
class TrailEntry:
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
    def from_dict(cls, data: dict) -> TrailEntry:
        return cls(
            ts=data["ts"],
            worker_id=data["worker_id"],
            message=data["message"],
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

    def to_dict(self) -> dict:
        return {
            "attempt_id": self.attempt_id,
            "worker_id": self.worker_id,
            "status": self.status.value,
            "branch": self.branch,
            "pr": self.pr,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

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
    status: TaskStatus = TaskStatus.READY
    current_attempt: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    trail: list[TrailEntry] = field(default_factory=list)
    signals: list[SignalEntry] = field(default_factory=list)

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
            "status": self.status.value,
            "current_attempt": self.current_attempt,
            "attempts": [a.to_dict() for a in self.attempts],
            "trail": [t.to_dict() for t in self.trail],
            "signals": [s.to_dict() for s in self.signals],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_by": self.created_by,
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
            status=TaskStatus(data.get("status", TaskStatus.READY)),
            current_attempt=data.get("current_attempt"),
            attempts=[Attempt.from_dict(a) for a in data.get("attempts", [])],
            trail=[TrailEntry.from_dict(t) for t in data.get("trail", [])],
            signals=[SignalEntry.from_dict(s) for s in data.get("signals", [])],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            created_by=data["created_by"],
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
