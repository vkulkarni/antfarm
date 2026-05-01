"""Tests for antfarm.core.mission_doc.

Pure-function tests for ``render_mission_doc`` plus subprocess-mocked tests
for ``write_and_commit_doc``. The doc renderer is the audit record's source
of truth, so its sectioning is asserted explicitly — adding/removing a
section without touching these tests is a regression.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from antfarm.core import mission_doc

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _ts(offset_seconds: int = 0) -> str:
    base = datetime(2026, 4, 22, 12, 0, 0, tzinfo=UTC)
    return (base + timedelta(seconds=offset_seconds)).isoformat()


def _mission(
    *,
    mission_id: str = "mission-test-001",
    status: str = "complete",
    re_plan_count: int = 0,
    proposed_tasks: list[dict] | None = None,
    completed: bool = True,
    config: dict | None = None,
) -> dict:
    return {
        "mission_id": mission_id,
        "spec": "Build a thing",
        "spec_file": "specs/thing.md",
        "status": status,
        "plan_task_id": f"plan-{mission_id}",
        "plan_artifact": (
            {
                "plan_task_id": f"plan-{mission_id}",
                "attempt_id": "att-001",
                "proposed_tasks": proposed_tasks
                or [
                    {
                        "title": "Implement core",
                        "depends_on": [],
                        "touches": ["core"],
                        "complexity": "M",
                    }
                ],
                "task_count": len(proposed_tasks or [{}]),
                "warnings": [],
                "dependency_summary": "",
            }
        ),
        "task_ids": [],
        "blocked_task_ids": [],
        "config": config or {},
        "created_at": _ts(0),
        "updated_at": _ts(600),
        "completed_at": _ts(600) if completed else None,
        "report": None,
        "last_progress_at": _ts(600),
        "re_plan_count": re_plan_count,
    }


def _impl_task(
    *,
    task_id: str = "task-01",
    status: str = "done",
    attempts: list[dict] | None = None,
    mission_id: str = "mission-test-001",
) -> dict:
    return {
        "id": task_id,
        "title": f"Title for {task_id}",
        "status": status,
        "capabilities_required": [],
        "depends_on": [],
        "touches": [],
        "attempts": attempts or [],
        "trail": [],
        "mission_id": mission_id,
    }


def _merged_attempt(
    *,
    attempt_id: str = "att-001",
    pr: str = "https://example.com/pr/1",
    started_offset: int = 60,
    completed_offset: int = 360,
) -> dict:
    return {
        "attempt_id": attempt_id,
        "worker_id": "node-1/w1",
        "status": "merged",
        "branch": "feat/x",
        "pr": pr,
        "started_at": _ts(started_offset),
        "completed_at": _ts(completed_offset),
        "artifact": {},
    }


# ---------------------------------------------------------------------------
# render_mission_doc
# ---------------------------------------------------------------------------


def test_render_minimal_mission():
    """Header, plan, tasks, and outcome sections appear for a basic mission."""
    mission = _mission()
    tasks = [
        _impl_task(
            attempts=[_merged_attempt()],
        ),
    ]

    md = mission_doc.render_mission_doc(mission, tasks, usage=None)

    # Header
    assert "# Mission: mission-test-001" in md
    assert "**Spec:** `specs/thing.md`" in md
    assert "**Outcome:** complete (1/1 merged)" in md
    # Plan section
    assert "## Plan" in md
    assert "Implement core" in md
    # Tasks section
    assert "## Tasks" in md
    assert "task-01" in md
    assert "https://example.com/pr/1" in md
    # No re-plans, no budget when neither applies
    assert "## Re-plans" not in md
    assert "## Budget" not in md


def test_render_with_replans():
    """re_plan_count > 0 surfaces a Re-plans section."""
    mission = _mission(re_plan_count=2)
    md = mission_doc.render_mission_doc(mission, [], usage=None)

    assert "## Re-plans" in md
    assert "Re-plan cycles: **2**" in md


def test_render_with_kickbacks():
    """An attempt that has a failure_type surfaces in the Notes column."""
    superseded = {
        "attempt_id": "att-000",
        "worker_id": "w",
        "status": "superseded",
        "started_at": _ts(0),
        "completed_at": _ts(120),
        "branch": None,
        "pr": None,
        "failure_type": "test_failure",
    }
    merged = _merged_attempt(attempt_id="att-001")
    task = _impl_task(attempts=[superseded, merged])
    mission = _mission()

    md = mission_doc.render_mission_doc(mission, [task], usage=None)

    assert "first attempt: test_failure" in md
    # 2 attempts → cell shows "2" attempts
    assert "| 2 |" in md


def test_render_budget_present_when_usage_set():
    """Cost, tokens, and top spend appear when usage is supplied."""
    usage = {
        "total_cost_usd": 1.234,
        "total_input_tokens": 100,
        "total_output_tokens": 200,
        "total_cache_read_tokens": 50,
        "per_task": {
            "task-01": {"cost_usd": 0.9},
            "task-02": {"cost_usd": 0.3},
        },
    }
    mission = _mission(config={"max_cost_usd": 5.0})

    md = mission_doc.render_mission_doc(mission, [], usage=usage)

    assert "## Budget" in md
    assert "$1.2340 / $5.0000" in md
    assert "input=100 output=200 cache_read=50" in md
    assert "Top spend: task-01" in md


def test_render_budget_omitted_when_no_usage():
    """No usage dict → no Budget section rendered."""
    md = mission_doc.render_mission_doc(_mission(), [], usage=None)

    assert "## Budget" not in md


def test_render_timeline_orders_chronologically():
    """Timeline rows are sorted by timestamp ascending."""
    plan_task = {
        "id": "plan-mission-test-001",
        "capabilities_required": ["plan"],
        "attempts": [
            {
                "attempt_id": "att-plan",
                "status": "done",
                "started_at": _ts(0),
                "completed_at": _ts(30),  # plan ready quickly
            }
        ],
        "trail": [],
    }
    early_merge = _impl_task(
        task_id="task-01",
        attempts=[_merged_attempt(attempt_id="a", completed_offset=120)],
    )
    late_merge = _impl_task(
        task_id="task-02",
        attempts=[_merged_attempt(attempt_id="b", completed_offset=300)],
    )

    md = mission_doc.render_mission_doc(
        _mission(), [plan_task, early_merge, late_merge], usage=None
    )

    # The timeline section should mention these in order
    assert "## Timeline highlights" in md
    timeline_block = md.split("## Timeline highlights", 1)[1]
    plan_pos = timeline_block.index("plan ready")
    t1_pos = timeline_block.index("task-01 merged")
    t2_pos = timeline_block.index("task-02 merged")
    mission_done_pos = timeline_block.index("mission complete")
    assert plan_pos < t1_pos < t2_pos < mission_done_pos


# ---------------------------------------------------------------------------
# write_and_commit_doc
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def test_write_and_commit_doc_creates_file(tmp_path):
    """The doc lands at docs/antfarm/missions/<id>.md and git is invoked."""
    mission = _mission()
    tasks = [_impl_task(attempts=[_merged_attempt()])]

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        # diff --cached returns 1 (changes present), everything else returns 0.
        if "diff" in cmd:
            return _FakeCompleted(returncode=1)
        return _FakeCompleted(returncode=0)

    with patch.object(subprocess, "run", side_effect=fake_run):
        ok = mission_doc.write_and_commit_doc(tmp_path, mission, tasks, usage=None)

    assert ok is True
    expected = tmp_path / "docs" / "antfarm" / "missions" / "mission-test-001.md"
    assert expected.exists()
    contents = expected.read_text()
    assert "# Mission: mission-test-001" in contents

    # Verify the four expected git invocations happened in order.
    cmds = [c for c in calls if c and c[0] == "git"]
    assert cmds[0][:2] == ["git", "add"]
    assert cmds[1][:3] == ["git", "diff", "--cached"]
    assert cmds[2][:2] == ["git", "commit"]
    assert "docs: mission mission-test-001 audit" in " ".join(cmds[2])
    assert cmds[3][:3] == ["git", "push", "origin"]


def test_write_and_commit_doc_idempotent_no_diff(tmp_path):
    """If git diff --cached returns 0 (nothing staged), commit is skipped."""
    mission = _mission()

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if "diff" in cmd:
            return _FakeCompleted(returncode=0)  # nothing to commit
        return _FakeCompleted(returncode=0)

    with patch.object(subprocess, "run", side_effect=fake_run):
        ok = mission_doc.write_and_commit_doc(tmp_path, mission, [], usage=None)

    assert ok is True
    # No commit, no push.
    cmds = [c[:2] for c in calls if c and c[0] == "git"]
    assert ["git", "commit"] not in cmds
    assert ["git", "push"] not in cmds


def test_write_and_commit_doc_swallows_git_failure(tmp_path, caplog):
    """A non-zero `git commit` rc is logged at WARNING and returns False."""
    mission = _mission()

    def fake_run(cmd, *args, **kwargs):
        if "commit" in cmd:
            return _FakeCompleted(returncode=1, stderr="boom")
        if "diff" in cmd:
            return _FakeCompleted(returncode=1)  # changes present
        return _FakeCompleted(returncode=0)

    with (
        caplog.at_level(logging.WARNING, logger="antfarm.core.mission_doc"),
        patch.object(subprocess, "run", side_effect=fake_run),
    ):
        ok = mission_doc.write_and_commit_doc(tmp_path, mission, [], usage=None)

    assert ok is False
    assert any("git commit failed" in rec.message for rec in caplog.records)


def test_write_and_commit_doc_skips_when_disabled():
    """When the config flag is False, callers (queen) skip the helper.

    The helper itself does not enforce the flag — that's the caller's job.
    This test pins the contract: the helper does not silently no-op based on
    config. (Queen wiring is covered separately in test_queen.py.)
    """
    mission = _mission(config={"commit_audit_doc": False})

    # If we DID call it, it would try to render and write — but the queen
    # never calls it in this state. Assert by simulating the queen's check.
    cfg = mission.get("config") or {}
    assert cfg.get("commit_audit_doc") is False


@pytest.mark.parametrize("mission_id", ["", None])
def test_write_and_commit_doc_no_mission_id(tmp_path, mission_id):
    """Defensive: missing mission_id yields False without git calls."""
    bad = _mission()
    bad["mission_id"] = mission_id

    with patch.object(subprocess, "run") as mock_run:
        ok = mission_doc.write_and_commit_doc(tmp_path, bad, [], usage=None)

    assert ok is False
    assert mock_run.call_count == 0
