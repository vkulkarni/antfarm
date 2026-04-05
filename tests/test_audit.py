"""Tests for antfarm.core.audit — audit trail."""

from __future__ import annotations

from antfarm.core.audit import AuditLog


def test_record_and_get_events(tmp_path):
    log = AuditLog(str(tmp_path))
    log.record("task.carried", "task-001", "operator", "created task")
    log.record("task.foraged", "task-001", "worker-1", "claimed by worker")

    events = log.get_events()
    assert len(events) == 2
    assert events[0]["event"] == "task.foraged"  # newest first
    assert events[1]["event"] == "task.carried"


def test_get_events_empty(tmp_path):
    log = AuditLog(str(tmp_path))
    assert log.get_events() == []


def test_get_events_with_limit(tmp_path):
    log = AuditLog(str(tmp_path))
    for i in range(10):
        log.record("task.carried", f"task-{i}", "op")
    assert len(log.get_events(limit=3)) == 3


def test_get_events_filter_by_type(tmp_path):
    log = AuditLog(str(tmp_path))
    log.record("task.carried", "task-001", "op")
    log.record("worker.registered", "worker-1", "system")
    log.record("task.foraged", "task-001", "worker-1")

    events = log.get_events(event_type="task.carried")
    assert len(events) == 1
    assert events[0]["subject"] == "task-001"


def test_get_events_filter_by_subject(tmp_path):
    log = AuditLog(str(tmp_path))
    log.record("task.carried", "task-001", "op")
    log.record("task.carried", "task-002", "op")
    log.record("task.foraged", "task-001", "w1")

    events = log.get_events(subject_id="task-001")
    assert len(events) == 2


def test_record_with_metadata(tmp_path):
    log = AuditLog(str(tmp_path))
    log.record("task.merged", "task-001", "soldier", metadata={"branch": "feat/x"})

    events = log.get_events()
    assert events[0]["metadata"]["branch"] == "feat/x"


def test_record_never_raises(tmp_path):
    log = AuditLog(str(tmp_path / "no" / "such" / "dir"))
    # mkdir in __init__ may create the dir, so use a read-only path
    log._path = tmp_path / "readonly" / "audit.jsonl"
    # Should not raise even when write fails
    log.record("test", "t1", "op")


def test_audit_event_has_timestamp(tmp_path):
    log = AuditLog(str(tmp_path))
    log.record("task.carried", "task-001", "op")
    events = log.get_events()
    assert "ts" in events[0]
    assert "T" in events[0]["ts"]  # ISO format
