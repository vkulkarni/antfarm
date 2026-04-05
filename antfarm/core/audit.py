"""Audit trail for Antfarm.

Append-only event log recording significant system events: task transitions,
worker lifecycle, merge outcomes, and operator actions. Stored as JSONL in
.antfarm/audit.jsonl.

Events are advisory and never block operations. All writes are best-effort.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class AuditLog:
    """Append-only audit event log.

    Args:
        data_dir: Path to the .antfarm directory.
    """

    def __init__(self, data_dir: str | Path = ".antfarm") -> None:
        self._path = Path(data_dir) / "audit.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        event_type: str,
        subject_id: str,
        actor: str,
        detail: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Record an audit event (best-effort, never raises)."""
        entry = {
            "ts": _now_iso(),
            "event": event_type,
            "subject": subject_id,
            "actor": actor,
            "detail": detail,
        }
        if metadata:
            entry["metadata"] = metadata
        try:
            with _lock, open(self._path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.debug("audit write failed: %s", exc)

    def get_events(
        self,
        limit: int = 100,
        event_type: str | None = None,
        subject_id: str | None = None,
    ) -> list[dict]:
        """Read recent audit events (newest first).

        Args:
            limit: Maximum number of events to return.
            event_type: Filter by event type (e.g., "task.carried").
            subject_id: Filter by subject ID (e.g., task or worker ID).
        """
        if not self._path.exists():
            return []

        entries: list[dict] = []
        try:
            lines = self._path.read_text().strip().split("\n")
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type and entry.get("event") != event_type:
                    continue
                if subject_id and entry.get("subject") != subject_id:
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    break
        except Exception as exc:
            logger.debug("audit read failed: %s", exc)

        return entries


# ---------------------------------------------------------------------------
# Convenience event types
# ---------------------------------------------------------------------------

# Task events
TASK_CARRIED = "task.carried"
TASK_FORAGED = "task.foraged"
TASK_HARVESTED = "task.harvested"
TASK_KICKED_BACK = "task.kicked_back"
TASK_MERGED = "task.merged"
TASK_PAUSED = "task.paused"
TASK_RESUMED = "task.resumed"
TASK_BLOCKED = "task.blocked"
TASK_UNBLOCKED = "task.unblocked"

# Worker events
WORKER_REGISTERED = "worker.registered"
WORKER_DEREGISTERED = "worker.deregistered"

# Review events
REVIEW_CREATED = "review.created"
REVIEW_VERDICT_STORED = "review.verdict_stored"

# Operator events
OPERATOR_OVERRIDE = "operator.override"
