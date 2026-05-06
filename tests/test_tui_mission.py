"""Tests for the mission-scoped TUI panel (#386).

Exercises ``AntfarmTUI._update_mission_state``,
``_refresh_mission_state``, and ``_render_mission_panel`` in isolation —
no live colony, no SSE stream. State is shaped via direct calls to
``_ingest_event`` and the seeding helper.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rich.table import Table

from antfarm.core.tui import AntfarmTUI, MissionTaskState


def _tui_with_mission(mission_id: str = "m1") -> AntfarmTUI:
    """Return a TUI instance pinned to a mission, no SSE thread, no fetches."""
    return AntfarmTUI(
        colony_url="http://localhost:7433",
        token=None,
        autostart_activity=False,
        mission_id=mission_id,
    )


def _seed_state(
    tui: AntfarmTUI,
    task_id: str = "task-01",
    title: str = "Test task",
    deps: list[str] | None = None,
) -> MissionTaskState:
    state = MissionTaskState(task_id=task_id, title=title, deps=deps or [])
    tui._mission_task_states[task_id] = state
    return state


# ---------------------------------------------------------------------------
# State updates
# ---------------------------------------------------------------------------


def test_state_record_updates_on_worker_activity():
    """worker_activity event flips builder + last_tool_* fields on the row."""
    tui = _tui_with_mission()
    _seed_state(tui, "task-01")
    tui._worker_to_task["worker-3"] = "task-01"

    tui._ingest_event(
        {
            "id": 1,
            "type": "worker_activity",
            "actor": "worker-3",
            "task_id": "",
            "detail": "editing src/foo.py",
            "data": {"action": "editing", "target": "src/foo.py", "text": "editing src/foo.py"},
            "ts": "2026-04-22T12:00:00+00:00",
        }
    )
    s = tui._mission_task_states["task-01"]
    assert s.builder_worker == "worker-3"
    assert s.last_tool_action == "editing"
    assert s.last_tool_target == "src/foo.py"
    assert s.last_tool_ts is not None
    assert s.last_tool_ts.tzinfo is not None


def test_state_record_updates_on_harvested():
    """harvested event sets harvested_at + pr_url and resets review_status."""
    tui = _tui_with_mission()
    _seed_state(tui, "task-01")

    tui._ingest_event(
        {
            "id": 2,
            "type": "harvested",
            "actor": "worker-3",
            "task_id": "task-01",
            "detail": "pr=https://github.com/o/r/pull/42 branch=feat/x",
            "ts": "2026-04-22T12:05:00+00:00",
        }
    )
    s = tui._mission_task_states["task-01"]
    assert s.harvested_at is not None
    assert s.pr_url == "https://github.com/o/r/pull/42"
    assert s.review_status == "queued"


def test_state_record_updates_on_auto_merged():
    """auto_merged event flips merge_status to 'merged'."""
    tui = _tui_with_mission()
    _seed_state(tui, "task-01")

    tui._ingest_event(
        {
            "id": 3,
            "type": "auto_merged",
            "actor": "soldier",
            "task_id": "task-01",
            "detail": "attempt=att-001 auto_merged=1",
            "ts": "2026-04-22T12:10:00+00:00",
        }
    )
    s = tui._mission_task_states["task-01"]
    assert s.merge_status == "merged"


def test_state_record_updates_on_merged():
    """merged event flips merge_status to 'merged' (manual merge path)."""
    tui = _tui_with_mission()
    _seed_state(tui, "task-01")
    tui._ingest_event(
        {
            "id": 4,
            "type": "merged",
            "actor": "soldier",
            "task_id": "task-01",
            "detail": "attempt=att-001",
            "ts": "2026-04-22T12:11:00+00:00",
        }
    )
    assert tui._mission_task_states["task-01"].merge_status == "merged"


def test_state_record_updates_on_kickback():
    """kickback bumps attempts and clears builder/last_tool/harvest fields."""
    tui = _tui_with_mission()
    s = _seed_state(tui, "task-01")
    s.builder_worker = "worker-3"
    s.last_tool_action = "editing"
    s.last_tool_target = "src/foo.py"
    s.harvested_at = datetime.now(UTC)
    s.merge_status = "merged"
    s.review_status = "pass"
    tui._worker_to_task["worker-3"] = "task-01"

    tui._ingest_event(
        {
            "id": 5,
            "type": "kickback",
            "actor": "soldier",
            "task_id": "task-01",
            "detail": "review:needs_changes",
            "ts": "2026-04-22T12:15:00+00:00",
        }
    )
    s = tui._mission_task_states["task-01"]
    assert s.attempts == 2
    assert s.builder_worker is None
    assert s.last_tool_action is None
    assert s.last_tool_target is None
    assert s.last_tool_ts is None
    assert s.harvested_at is None
    assert s.merge_status == "—"
    assert s.review_status == "queued"
    assert "worker-3" not in tui._worker_to_task


def test_state_record_updates_on_auto_merge_waiting_ci():
    tui = _tui_with_mission()
    _seed_state(tui, "task-01")
    tui._ingest_event(
        {
            "id": 6,
            "type": "auto_merge_waiting_ci",
            "actor": "soldier",
            "task_id": "task-01",
            "detail": "pr=42",
            "ts": "2026-04-22T12:20:00+00:00",
        }
    )
    assert tui._mission_task_states["task-01"].merge_status == "waiting_ci"


def test_state_record_updates_on_auto_merge_rebasing():
    tui = _tui_with_mission()
    _seed_state(tui, "task-01")
    tui._ingest_event(
        {
            "id": 7,
            "type": "auto_merge_rebasing",
            "actor": "soldier",
            "task_id": "task-01",
            "detail": "pr=42",
            "ts": "2026-04-22T12:21:00+00:00",
        }
    )
    assert tui._mission_task_states["task-01"].merge_status == "rebasing"


def test_state_record_updates_on_merge_failed():
    tui = _tui_with_mission()
    _seed_state(tui, "task-01")
    tui._ingest_event(
        {
            "id": 8,
            "type": "merge_failed",
            "actor": "soldier",
            "task_id": "task-01",
            "detail": "reason=conflict",
            "ts": "2026-04-22T12:22:00+00:00",
        }
    )
    assert tui._mission_task_states["task-01"].merge_status == "failed"


def test_state_record_updates_on_review_verdict_pass():
    tui = _tui_with_mission()
    _seed_state(tui, "task-01")
    tui._ingest_event(
        {
            "id": 9,
            "type": "review_pass",
            "actor": "reviewer-1",
            "task_id": "task-01",
            "detail": "pass",
            "ts": "2026-04-22T12:23:00+00:00",
        }
    )
    s = tui._mission_task_states["task-01"]
    assert s.review_status == "pass"
    assert s.review_verdict == "pass"


def test_state_record_updates_on_review_needs_changes():
    tui = _tui_with_mission()
    _seed_state(tui, "task-01")
    tui._ingest_event(
        {
            "id": 10,
            "type": "review_needs_changes",
            "actor": "reviewer-1",
            "task_id": "task-01",
            "detail": "missing tests",
            "ts": "2026-04-22T12:24:00+00:00",
        }
    )
    s = tui._mission_task_states["task-01"]
    assert s.review_status == "needs_changes"
    assert s.review_verdict == "missing tests"


def test_state_ignores_events_outside_mission():
    """A task_id not present in the mission's state map is dropped."""
    tui = _tui_with_mission()
    _seed_state(tui, "task-01")
    tui._ingest_event(
        {
            "id": 11,
            "type": "merged",
            "actor": "soldier",
            "task_id": "task-99",
            "detail": "attempt=att-001",
            "ts": "2026-04-22T12:25:00+00:00",
        }
    )
    assert tui._mission_task_states["task-01"].merge_status == "—"


def test_state_ingest_skipped_when_no_mission_id():
    """When mission_id is None the per-task ingest is a no-op."""
    tui = AntfarmTUI(
        colony_url="http://localhost:7433",
        token=None,
        autostart_activity=False,
    )
    # Even if we plant some state by hand, the code path should never read it.
    tui._mission_task_states["task-01"] = MissionTaskState(task_id="task-01", title="x")
    tui._ingest_event(
        {
            "id": 1,
            "type": "merged",
            "actor": "soldier",
            "task_id": "task-01",
            "detail": "attempt=att-001",
            "ts": "2026-04-22T12:00:00+00:00",
        }
    )
    # mission_id is None → state untouched.
    assert tui._mission_task_states["task-01"].merge_status == "—"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _row_strings(table: Table) -> list[list[str]]:
    """Return rendered cells per row as plain strings (one per column)."""
    rows: list[list[str]] = []
    n_rows = len(table.rows)
    for i in range(n_rows):
        row_cells: list[str] = []
        for col in table.columns:
            cell = col._cells[i]  # type: ignore[attr-defined]
            row_cells.append(cell.plain if hasattr(cell, "plain") else str(cell))
        rows.append(row_cells)
    return rows


def test_render_mission_panel_renders_active_task():
    """Active task row shows builder + tool action with elapsed seconds."""
    tui = _tui_with_mission()
    s = MissionTaskState(task_id="task-02", title="Edit foo")
    s.builder_worker = "worker-4"
    s.last_tool_action = "editing"
    s.last_tool_target = "src/foo.py"
    s.last_tool_ts = datetime(2026, 4, 22, 12, 0, 0, tzinfo=UTC)
    tui._mission_task_states["task-02"] = s

    mission = {"mission_id": "m1", "status": "building"}
    table = tui._render_mission_panel(
        mission,
        tui._mission_task_states,
        now=datetime(2026, 4, 22, 12, 4, 12, tzinfo=UTC),
    )
    rows = _row_strings(table)
    assert any("task-02" in r[0] for r in rows)
    builder_cell = next(r[1] for r in rows if "task-02" in r[0])
    assert "worker-4" in builder_cell
    assert "editing" in builder_cell
    assert "src/foo.py" in builder_cell
    assert "4m12s" in builder_cell
    build_cell = next(r[2] for r in rows if "task-02" in r[0])
    assert "in flight" in build_cell


def test_render_mission_panel_renders_completed_task():
    """Merged task row shows ✓ done in build column and ✓ merged + PR# in merge column."""
    tui = _tui_with_mission()
    s = MissionTaskState(task_id="task-01", title="Done task")
    s.builder_worker = "worker-3"
    s.harvested_at = datetime(2026, 4, 22, 11, 50, 0, tzinfo=UTC)
    s.review_status = "pass"
    s.merge_status = "merged"
    s.pr_url = "https://github.com/o/r/pull/3"
    tui._mission_task_states["task-01"] = s

    mission = {"mission_id": "m1", "status": "building"}
    table = tui._render_mission_panel(mission, tui._mission_task_states)
    rows = _row_strings(table)
    row = next(r for r in rows if "task-01" in r[0])
    assert "✓ done" in row[2]
    assert "✓ pass" in row[3]
    assert "merged" in row[4]
    assert "PR#3" in row[4]


def test_render_mission_panel_renders_blocked_task():
    """Task with unmet deps shows queued (deps: …) in build column."""
    tui = _tui_with_mission()
    s = MissionTaskState(task_id="task-03", title="Blocked", deps=["task-02"])
    tui._mission_task_states["task-03"] = s
    # Note: task-02 not present -> dep not satisfied.

    mission = {"mission_id": "m1", "status": "building"}
    table = tui._render_mission_panel(mission, tui._mission_task_states)
    rows = _row_strings(table)
    row = next(r for r in rows if "task-03" in r[0])
    assert "—" in row[1]
    assert "queued" in row[2]
    assert "deps" in row[2]
    assert "—" in row[3]
    assert "—" in row[4]


def test_render_mission_panel_handles_missing_mission(monkeypatch):
    """Mission id not in /missions list -> _build_display short-circuits."""
    tui = _tui_with_mission(mission_id="missing-1")

    def _fake_fetch(path):
        if path == "/status/full":
            return {
                "status": {"nodes": 0},
                "tasks": [],
                "workers": [],
                "soldier": "idle",
            }
        if path == "/missions":
            return []
        return {}

    monkeypatch.setattr(tui, "_fetch", _fake_fetch)
    tui._build_display()
    # Cheap fingerprint: ensure we hit the not-found path. The layout's
    # repr embeds the panel's inner Text object.
    assert tui._mission_not_found is True


def test_render_mission_panel_title_includes_progress():
    """Panel title shows Mission/Status/Tasks header line."""
    tui = _tui_with_mission()
    s1 = MissionTaskState(task_id="task-01", title="A", merge_status="merged")
    s2 = MissionTaskState(task_id="task-02", title="B")
    tui._mission_task_states = {"task-01": s1, "task-02": s2}
    mission = {"mission_id": "m1", "status": "building"}
    title = tui._mission_panel_title(mission, tui._mission_task_states)
    assert "Mission: m1" in title
    assert "Status: building" in title
    assert "Tasks: 1/2 merged" in title


def test_format_pr_extracts_number():
    assert AntfarmTUI._format_pr("https://github.com/o/r/pull/42") == "PR#42"
    assert AntfarmTUI._format_pr("#42") == "PR#42"
    assert AntfarmTUI._format_pr("42") == "PR#42"
    assert AntfarmTUI._format_pr("") == ""


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_refresh_mission_state_seeds_from_snapshot():
    """First refresh populates state from /missions + /tasks."""
    tui = _tui_with_mission(mission_id="m1")
    missions = [
        {
            "mission_id": "m1",
            "status": "building",
            "task_ids": ["task-01", "task-02", "review-001"],
        }
    ]
    tasks = [
        {
            "id": "task-01",
            "title": "First",
            "status": "active",
            "current_attempt": "att-001",
            "attempts": [
                {
                    "attempt_id": "att-001",
                    "worker_id": "worker-3",
                    "status": "active",
                    "started_at": "2026-04-22T11:00:00+00:00",
                    "completed_at": None,
                }
            ],
            "depends_on": [],
            "capabilities_required": [],
        },
        {
            "id": "task-02",
            "title": "Second",
            "status": "ready",
            "current_attempt": None,
            "attempts": [],
            "depends_on": ["task-01"],
            "capabilities_required": [],
        },
        {
            "id": "review-001",
            "title": "Review",
            "status": "active",
            "capabilities_required": ["review"],
            "attempts": [],
            "current_attempt": None,
            "depends_on": [],
        },
    ]
    workers = [{"worker_id": "worker-3"}]

    tui._refresh_mission_state(missions, tasks, workers)
    assert "task-01" in tui._mission_task_states
    assert "task-02" in tui._mission_task_states
    # review-001 is infra and excluded from the impl-task panel
    assert "review-001" not in tui._mission_task_states
    assert tui._mission_task_states["task-01"].builder_worker == "worker-3"
    assert tui._worker_to_task["worker-3"] == "task-01"
    assert tui._mission_seeded is True


def test_refresh_mission_state_marks_not_found():
    """No matching mission on first refresh sets the sticky not-found flag."""
    tui = _tui_with_mission(mission_id="nope")
    tui._refresh_mission_state(missions=[], tasks=[], workers=[])
    assert tui._mission_not_found is True


def test_refresh_mission_state_preserves_event_state():
    """Re-seeding does not clobber event-driven last_tool_* fields."""
    tui = _tui_with_mission(mission_id="m1")
    # First seed
    missions = [{"mission_id": "m1", "status": "building", "task_ids": ["task-01"]}]
    tasks = [
        {
            "id": "task-01",
            "title": "x",
            "status": "active",
            "current_attempt": "att-001",
            "attempts": [
                {
                    "attempt_id": "att-001",
                    "worker_id": "worker-3",
                    "status": "active",
                    "started_at": "2026-04-22T11:00:00+00:00",
                }
            ],
            "depends_on": [],
            "capabilities_required": [],
        }
    ]
    tui._refresh_mission_state(missions, tasks, [{"worker_id": "worker-3"}])
    # Event injects extra detail
    tui._ingest_event(
        {
            "id": 1,
            "type": "worker_activity",
            "actor": "worker-3",
            "task_id": "",
            "detail": "editing src/foo.py",
            "data": {"action": "editing", "target": "src/foo.py"},
            "ts": "2026-04-22T11:05:00+00:00",
        }
    )
    # Re-seed should preserve the live action info.
    tui._refresh_mission_state(missions, tasks, [{"worker_id": "worker-3"}])
    s = tui._mission_task_states["task-01"]
    assert s.last_tool_action == "editing"
    assert s.last_tool_target == "src/foo.py"
