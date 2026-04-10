# Antfarm v0.6.0 — Implementation Plan

**Status:** DRAFT — awaiting approval
**Derived from:** `docs/SPEC_v06.md` (frozen 2026-04-07, commit `a4a5cdb`)
**Plugin spec (out of scope here):** `docs/SPEC_v06_plugin.md` covers v0.6.2 and is NOT part of this plan.
**Prerequisite:** v0.5.9 safety hardening (max-attempt enforcement, doctor daemon, cascade invalidation) — **shipped on `main` as of `2f83d1a`**. Verified signatures:
- `FileBackend.kickback(self, task_id, reason, max_attempts=3)` at `antfarm/core/backends/file.py:345`
- `_BackendAdapter.kickback(task_id, reason, max_attempts=max_attempts)` at `antfarm/core/soldier.py:842`

No rebase or signature repair is required. This plan branches off `main`.
**Scope:** v0.6.0 "Autonomous Runs" only. v0.6.1 (multi-node autoscaler, GitHub issue sync, prompt-cache sharing) and v0.6.2 (Claude Code plugin) are explicitly deferred. All previously-open design questions are now resolved — see "Resolved Decisions (locked)" below.
**Goal:** `antfarm colony --autoscaler` + `antfarm mission --spec spec.md` — one command in, morning digest out. Zero human intervention between.

---

## What's in / what's out (v0.6.0)

**IN:**
1. Mission model (`Mission`, `MissionConfig`, `PlanArtifact`, `MissionReport`) persisted under `.antfarm/missions/`
2. `mission_id` reverse linkage on every task created under a mission
3. Queen controller daemon thread (`antfarm/core/queen.py`) — advances missions, deterministic, crash-recoverable
4. Plan-review flow — planner emits a plan artifact only, Queen optionally creates a `review-plan-*` task before spawning child tasks
5. Single-host Autoscaler daemon thread (`antfarm/core/autoscaler.py`) — subprocess-based, scope-aware
6. Mission report (morning digest) — JSON, terminal, markdown
7. CLI: `antfarm mission {create|status|report|cancel|list}`, `antfarm carry --mission`, `antfarm colony --autoscaler|--no-doctor` flags
8. API: `/missions*` endpoints, `mission_id` on `POST /tasks` and `?mission_id=` filter on `GET /tasks`
9. Backend: `TaskBackend.create_mission/get_mission/list_missions/update_mission`, FileBackend implementation, `Task.mission_id` field
10. TUI: Mission panel (thin — list + status, reusing existing scout polling)

**OUT (deferred):**
- Multi-node autoscaler / node agent (v0.6.1)
- Prompt-cache / shared mission context (v0.6.1)
- GitHub issue sync (v0.6.1)
- MCP server, slash commands, plugin packaging (v0.6.2)
- AI-assisted Soldier conflict triage
- Recursive missions (missions spawning sub-missions)
- Web dashboard

---

## Current codebase anchors (grounding)

These real names from the current codebase matter — the plan uses them verbatim, not spec-prose aliases.

| Concept in spec | Real symbol / file |
|-----------------|-------------------|
| "kickback" | `FileBackend.kickback()` at `antfarm/core/backends/file.py:345` |
| "max-attempt enforcement" | shipped as v0.5.9 PR #146 — `FileBackend.kickback(..., max_attempts=...)` + `mark_blocked` path |
| "cascade invalidation" | `Soldier.kickback_with_cascade()` at `antfarm/core/soldier.py:458` (already on `main` as of #148) |
| "doctor daemon" | shipped as v0.5.9 PR #147/#151 — `_start_doctor_thread()` in `serve.py` |
| "planner worker mode" | `WorkerRuntime._process_plan_output()` at `antfarm/core/worker.py:692`, `[PLAN_RESULT]` tags, shared engine in `antfarm/core/planner.py` |
| "reviewer worker / verdict" | `store_review_verdict()` + `[REVIEW_VERDICT]` tags, `ReviewVerdict` in `models.py` |
| "capabilities-based routing" | `Task.capabilities_required: list[str]`, `Worker.capabilities: list[str]` |
| "Soldier singleton daemon" | `_start_soldier_thread()` in `serve.py:53` |
| "BackendAdapter (soldier uses it)" | `_BackendAdapter` at `soldier.py:802` — exposes `carry(**kwargs)`, `kickback(task_id, reason, max_attempts=3)` |
| "colony HTTP client" | `ColonyClient` at `antfarm/core/colony_client.py` |
| "scheduler selection order" | `scheduler.select_task()` at `antfarm/core/scheduler.py:103` (deps → caps → pin → scope → hotspots → priority → FIFO) |
| "task dict on the wire" | `CarryRequest` pydantic model in `serve.py:88` (already carries `spawned_by: dict | None`) |
| "existing task types" | `Task.capabilities_required = ["plan"]` for planner tasks; review tasks use ID prefix `review-*` and `["review"]` capability |
| "task artifact" | `TaskArtifact` in `models.py` (harvest output) |

**Branch off `main` (tip `2f83d1a`).** All v0.5.9 prerequisites are in place; no preliminary fix-up commits are needed.

---

## Module Map

### New files

```
antfarm/core/
  missions.py                # Mission, MissionConfig, PlanArtifact, MissionReport dataclasses + is_infra_task() filter
  queen.py                   # Queen controller (daemon thread, mission lifecycle)
  autoscaler.py              # Single-host autoscaler (subprocess worker lifecycle)
  report.py                  # MissionReport generator + formatters (json/terminal/markdown)

tests/
  test_missions_model.py     # dataclass roundtrip, MissionConfig defaults
  test_queen.py              # Queen._advance() unit tests per phase
  test_autoscaler.py         # _compute_desired, _reconcile, scope grouping
  test_report.py             # report generation from mission + tasks
  test_mission_backend.py    # FileBackend mission CRUD
  test_mission_serve.py      # /missions endpoints
  test_mission_cli.py        # antfarm mission subcommands
  test_e2e_mission.py        # full mission loop (mock workers, fake git)
```

### Modified files

```
antfarm/core/models.py       # add Task.mission_id field (persist through to_dict/from_dict)
antfarm/core/backends/base.py  # add create_mission/get_mission/list_missions/update_mission
antfarm/core/backends/file.py  # FileBackend impl of mission CRUD + mission_id in carry()
antfarm/core/backends/github.py# mission methods raise an actionable NotImplementedError with a user-facing message; see Phase 1 NB-3
antfarm/core/serve.py        # CarryRequest.mission_id, /missions* endpoints, _start_queen_thread, _start_autoscaler_thread, --no-doctor/--autoscaler wiring
antfarm/core/cli.py          # `mission` command group, --mission on carry, --autoscaler/--no-doctor on colony
antfarm/core/worker.py       # mission-mode planner path: store plan as artifact, do NOT carry child tasks
antfarm/core/soldier.py      # create_review_task: propagate mission_id from parent task
antfarm/core/colony_client.py# mission methods (create_mission, get_mission, list_missions, cancel_mission, carry with mission_id)
antfarm/core/tui.py          # Mission panel (renders from /missions + /status/full)
antfarm/core/scheduler.py    # unchanged (routing already handles capabilities); add docstring note re: mission tasks are routed by capabilities
antfarm/core/review_pack.py  # Phase 0: add public extract_verdict_from_review_task (moved from soldier.py)
```

### Unchanged but depended on

```
antfarm/core/planner.py      # PlannerEngine.parse_structured_plan() / validate_plan()
antfarm/core/doctor.py       # run_doctor() — used by doctor daemon (already shipped in v0.5.9)
antfarm/core/workspace.py    # unchanged
```

---

## State Machines

### Mission state machine

```
 antfarm mission create --spec spec.md
         |
         v
   +-----------+
   | PLANNING  |  plan task created, planner working
   +-----+-----+
         | plan task harvested with PlanArtifact
         v
   +-----------------+  (require_plan_review=false skips this state
   | REVIEWING_PLAN  |   and goes straight to BUILDING)
   +---+-------------+
       |
   pass| needs_changes (max 1 re-plan allowed)
       |        |
       |     re-plan task created; planner re-runs
       |     second review also fails -> FAILED
       v
   +-----------+
   | BUILDING  | <--------+
   +-----+-----+          |
         |                | operator unblock OR
         |                | cascade recovery
   +-----+------+         |
   |            |         |
   v            v         |
 +----------+ +---------+ |
 | COMPLETE | | BLOCKED |-+
 +----------+ +----+----+
                   | blocked_timeout_action="fail"
                   | AND blocked_timeout_minutes exceeded
                   v
              +----------+
              |  FAILED  |
              +----------+

 Operator cancel at any non-terminal state:
              +-----------+
              | CANCELLED |
              +-----------+
```

Terminal: `COMPLETE`, `FAILED`, `CANCELLED`. `BLOCKED` is recoverable.

### Task lifecycle additions (v0.6)

No new task statuses — v0.5.9 already introduced `BLOCKED`. What's new in v0.6 is only the `mission_id` reverse linkage and the mission-mode branch in the planner path. The task state machine from v0.5 stands.

### Queen decision loop (per tick)

```
for mission in backend.list_missions():
    if mission.status in {complete, failed, cancelled}: continue
    match mission.status:
      planning         -> _check_plan_complete(mission)
      reviewing_plan   -> _check_plan_review(mission)
      building         -> _check_build_progress(mission); _check_stall(mission)
      blocked          -> _check_unblocked(mission); _check_stall_timeout(mission)
sleep(_adaptive_interval(missions))
```

---

## Build Phases

---

### Phase 0 — Prep refactor: extract review-verdict helper

**PR 1 of sequence. Tiny, low-risk, unblocks Queen + Soldier sharing one code path.**

**Goal:** Move `Soldier._extract_verdict_from_review_task` out of `soldier.py` and into `review_pack.py` as a public function so both the Soldier (existing caller) and the Queen (new caller in Phase 3) import from the same place. No behavior change. Ships before any v0.6 feature work.

**Why:** Both Queen and Soldier need to read `[REVIEW_VERDICT]` output from a completed review task. Duplicating the logic would drift; creating a new `verdict.py` is YAGNI for one function; `review_pack.py` is already the review-domain module and has no current circular-import concerns.

**Files touched:**

- `antfarm/core/review_pack.py` — add public function `extract_verdict_from_review_task`
- `antfarm/core/soldier.py` — delete the private `@staticmethod _extract_verdict_from_review_task` at line 738; update the sole caller at line 249 to use the imported public function
- `tests/test_review_pack.py` — add unit tests for the extracted function (does not currently have coverage for this)
- `tests/test_soldier.py` — existing tests must still pass unchanged (acts as a regression gate)

**Anchors (verified against `main` / `2f83d1a`):**

- `soldier.py:738` — `def _extract_verdict_from_review_task(review_task: dict) -> dict | None:` (staticmethod on `Soldier`, body reads `current_attempt`, `attempts[*].artifact`, `attempts[*].review_verdict`, and falls back to trail entries starting with `[REVIEW_VERDICT] `)
- `soldier.py:249` — `review_verdict = self._extract_verdict_from_review_task(review_task)` (sole caller)
- `review_pack.py` — 91 lines, single public function `generate_review_pack()`, imports only from `antfarm.core.models` (no circular risk)

**Key interface:**

```python
# antfarm/core/review_pack.py — new function

def extract_verdict_from_review_task(review_task: dict) -> dict | None:
    """Extract a ReviewVerdict dict from a completed review task.

    Looks for the verdict in three places, in order:
    1. ``attempt.artifact`` if the artifact itself has a ``verdict`` key
    2. ``attempt.review_verdict`` (stored via ``store_review_verdict``)
    3. Trail entries starting with ``[REVIEW_VERDICT] `` (fallback for
       adapters that only emit trail messages)

    Args:
        review_task: Full task dict for a review task (from ``get_task``).

    Returns:
        The verdict dict (shape of ``ReviewVerdict.to_dict()``), or ``None``
        if no verdict is present yet.
    """
    current_attempt_id = review_task.get("current_attempt")
    if not current_attempt_id:
        return None
    for attempt in review_task.get("attempts", []):
        if attempt.get("attempt_id") == current_attempt_id:
            artifact = attempt.get("artifact")
            if artifact and "verdict" in artifact:
                return artifact
            rv = attempt.get("review_verdict")
            if rv:
                return rv
    for entry in reversed(review_task.get("trail", [])):
        msg = entry.get("message", "")
        if msg.startswith("[REVIEW_VERDICT] "):
            import json
            try:
                return json.loads(msg[len("[REVIEW_VERDICT] "):])
            except (json.JSONDecodeError, ValueError):
                continue
    return None
```

**Soldier delta:**

```python
# antfarm/core/soldier.py — top-of-file import
from antfarm.core.review_pack import extract_verdict_from_review_task

# antfarm/core/soldier.py — line 249 (update caller)
review_verdict = extract_verdict_from_review_task(review_task)

# antfarm/core/soldier.py — delete the staticmethod at line 738
```

**Edge cases + invariants:**

- Pure refactor: no behavior change. The function body is copied verbatim (only the `@staticmethod` decorator and leading underscore drop).
- `review_pack.py` does not currently depend on anything in `soldier.py`, so no circular import risk.
- The `json` import stays local to the function body (matches the original, keeps `review_pack.py` import-free at module load).

**Tests to write first:**

1. `test_extract_verdict_from_artifact_key` — verdict embedded directly in `attempt.artifact["verdict"]` → returned
2. `test_extract_verdict_from_review_verdict_field` — verdict in `attempt.review_verdict` → returned
3. `test_extract_verdict_from_trail_fallback` — only present as `[REVIEW_VERDICT] {...}` trail entry → parsed + returned
4. `test_extract_verdict_none_when_no_current_attempt` — `current_attempt=None` → returns None
5. `test_extract_verdict_none_when_malformed_trail_json` — malformed trail JSON → skips to next trail entry or returns None
6. `test_extract_verdict_most_recent_trail_entry_wins` — multiple `[REVIEW_VERDICT]` trail entries → last one is returned
7. `test_soldier_still_imports_and_uses_helper` — regression gate: existing `test_soldier.py` tests for plan-review + build-review flows pass unchanged

**Release note:** This PR is a strict refactor — no docs changes, no API changes, no CLI changes. It exists solely so Phase 3 (Queen) and later phases don't have to either duplicate the helper or reach into a private method on `Soldier`.

---

### Phase 1 — Mission model + backend CRUD

**PR 2 of sequence.**

**Goal:** Mission and associated dataclasses exist, persist to disk, and round-trip through TaskBackend. No Queen, no API yet.

**Files touched:**

- `antfarm/core/missions.py` (new)
- `antfarm/core/models.py` (add `Task.mission_id: str | None = None`)
- `antfarm/core/backends/base.py` (add 4 abstract methods)
- `antfarm/core/backends/file.py` (implement mission CRUD, propagate `mission_id` through `carry()`)
- `antfarm/core/backends/github.py` (add stubs raising an actionable `NotImplementedError` with the canonical message — see "GitHubBackend mission stubs" below)
- `tests/test_missions_model.py` (new)
- `tests/test_mission_backend.py` (new)
- `tests/test_models.py` (extend for `Task.mission_id` round trip)

**Key interfaces:**

```python
# antfarm/core/missions.py

from __future__ import annotations
from dataclasses import dataclass, field
from enum import StrEnum


class MissionStatus(StrEnum):
    PLANNING = "planning"
    REVIEWING_PLAN = "reviewing_plan"
    BUILDING = "building"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class MissionConfig:
    max_attempts: int = 3
    max_parallel_builders: int = 4
    require_plan_review: bool = True
    stall_threshold_minutes: int = 30
    completion_mode: str = "best_effort"       # "best_effort" | "all_or_nothing"
    test_command: list[str] | None = None
    integration_branch: str = "main"
    blocked_timeout_action: str = "wait"       # "wait" | "fail"
    blocked_timeout_minutes: int = 120

    # v0.6.0: "all_or_nothing" is accepted, persisted, and validated, but
    # behaves identically to "best_effort" with a warning log at mission
    # creation time. Real all-or-nothing semantics (roll back merged tasks
    # if any child fails) land in v0.6.1+.
    _VALID_COMPLETION_MODES = ("best_effort", "all_or_nothing")
    _VALID_BLOCKED_TIMEOUT_ACTIONS = ("wait", "fail")

    def __post_init__(self) -> None:
        if self.completion_mode not in self._VALID_COMPLETION_MODES:
            raise ValueError(
                f"completion_mode must be one of {self._VALID_COMPLETION_MODES}"
            )
        if self.blocked_timeout_action not in self._VALID_BLOCKED_TIMEOUT_ACTIONS:
            raise ValueError(
                f"blocked_timeout_action must be one of "
                f"{self._VALID_BLOCKED_TIMEOUT_ACTIONS}"
            )

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, data: dict) -> "MissionConfig": ...


@dataclass
class PlanArtifact:
    plan_task_id: str
    attempt_id: str
    proposed_tasks: list[dict]          # validated, parsed from [PLAN_RESULT]
    task_count: int
    warnings: list[str] = field(default_factory=list)
    dependency_summary: str = ""

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, data: dict) -> "PlanArtifact": ...


@dataclass
class MissionReportTask:
    task_id: str
    title: str
    pr_url: str | None
    lines_added: int
    lines_removed: int
    files_changed: list[str]

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, data: dict) -> "MissionReportTask": ...


@dataclass
class MissionReportBlocked:
    task_id: str
    title: str
    reason: str
    attempt_count: int
    last_failure_type: str | None

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, data: dict) -> "MissionReportBlocked": ...


@dataclass
class MissionReport:
    mission_id: str
    spec_summary: str
    status: MissionStatus
    completion_mode: str              # "best_effort" or "all_or_nothing" — echoed from MissionConfig
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

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, data: dict) -> "MissionReport": ...


@dataclass
class Mission:
    mission_id: str                       # "mission-<slug>-<ms>"
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
    # Progress tracking (written by Queen, used for stall detection)
    last_progress_at: str                 # ISO 8601
    re_plan_count: int = 0                # guardrail: max 1 re-plan

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, data: dict) -> "Mission": ...


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
    return (
        "plan" in caps
        or "review" in caps
        or task.get("id", "").startswith("review-")
    )
```

```python
# antfarm/core/backends/base.py — additions

@abstractmethod
def create_mission(self, mission: dict) -> str:
    """Create a mission. Raises ValueError if mission_id already exists."""
    ...

@abstractmethod
def get_mission(self, mission_id: str) -> dict | None: ...

@abstractmethod
def list_missions(self, status: str | None = None) -> list[dict]: ...

@abstractmethod
def update_mission(self, mission_id: str, updates: dict) -> None:
    """Shallow-merge `updates` into the mission JSON. Atomic write."""
    ...
```

```python
# antfarm/core/backends/file.py — additions

def _missions_dir(self) -> Path:
    return self.root / "missions"

def _mission_path(self, mission_id: str) -> Path:
    return self._missions_dir() / f"{mission_id}.json"

def create_mission(self, mission: dict) -> str:
    with self._lock:
        self._missions_dir().mkdir(parents=True, exist_ok=True)
        path = self._mission_path(mission["mission_id"])
        if path.exists():
            raise ValueError(f"mission '{mission['mission_id']}' already exists")
        self._write_json(path, mission)
        return mission["mission_id"]

def list_missions(self, status: str | None = None) -> list[dict]:
    # Read all .antfarm/missions/*.json, optionally filter by status field
    ...

def update_mission(self, mission_id: str, updates: dict) -> None:
    with self._lock:
        path = self._mission_path(mission_id)
        if not path.exists():
            raise FileNotFoundError(f"mission '{mission_id}' not found")
        data = self._read_json(path)
        data.update(updates)
        data["updated_at"] = _now_iso()
        self._write_json(path, data)
```

**Task.mission_id:** Add to `Task` dataclass; persist in `to_dict`/`from_dict`. `FileBackend.carry()` must read `task.get("mission_id")` and preserve it through the ready file.

**Edge cases + invariants:**

- `create_mission` is NOT idempotent — duplicate IDs raise. CLI uses a time-suffixed slug to avoid collisions.
- `update_mission` does a shallow merge. Nested updates (e.g., appending to `task_ids`) are read-modify-write and must be under the same `_lock` that protects task I/O.
- `mission_id` on a task is immutable after creation — there's no `reassign_mission` method.

**`link_task_to_mission()` — shared atomicity helper (lives in `missions.py`):**

This is the canonical path for creating a task that belongs to a mission. All callers (Phase 2 HTTP handler, Phase 3 Queen, Phase 5 Soldier) MUST use this helper — never call `carry()` + `update_mission()` separately when a `mission_id` is involved.

```python
# antfarm/core/missions.py

def link_task_to_mission(
    backend: "TaskBackend",
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
            f"cannot add tasks to mission '{mission_id}' "
            f"in terminal state '{mission['status']}'"
        )
    task_id = backend.carry(task_dict)
    backend.update_mission(mission_id, {
        "task_ids": mission["task_ids"] + [task_id],
    })
    return task_id
```

**Note on FileBackend atomicity:** `link_task_to_mission` calls `carry()` then `update_mission()` sequentially. For `FileBackend`, both are already guarded by `_lock` individually — but the two-call sequence is NOT atomic across both operations. If the process crashes between `carry()` and `update_mission()`, the task exists but isn't linked. **This is acceptable for v0.6.0** — Doctor should detect orphaned mission tasks (task has `mission_id` but isn't in `mission.task_ids`) and report them. A truly atomic `carry_and_link()` backend method is a v0.6.1 enhancement if crash-between-calls proves to be a real problem.

**Tests (added to Phase 1 test list):**

13. `test_link_task_to_mission_appends_task_id` — task is created and mission's `task_ids` grows by one
14. `test_link_task_to_mission_missing_mission_raises` — `FileNotFoundError` when mission doesn't exist
15. `test_link_task_to_mission_terminal_mission_raises` — `ValueError` when mission is COMPLETE/FAILED/CANCELLED
- Missions do not live in `ready/active/done` — they live in one flat `.antfarm/missions/` directory. State lives in the `status` field only.
- **Cross-backend (NB-3):** `GitHubBackend` mission methods raise an actionable `NotImplementedError` with the canonical message below. A preflight check in the `POST /missions` handler (and any in-process `colony.create_mission()` path) MUST short-circuit with the SAME message when `isinstance(backend, GitHubBackend)` so users do NOT hit the stub mid-Queen-loop after a half-created mission.

  **Canonical error message (use verbatim in every raise + preflight path):**

  ```python
  _GITHUB_BACKEND_MSG = (
      "Mission mode requires FileBackend in v0.6.0. "
      "Use --backend file or wait for v0.6.1."
  )
  ```

  **Stub pattern in `backends/github.py`:**

  ```python
  def create_mission(self, mission: dict) -> str:
      raise NotImplementedError(_GITHUB_BACKEND_MSG)

  def get_mission(self, mission_id: str) -> dict | None:
      raise NotImplementedError(_GITHUB_BACKEND_MSG)

  def list_missions(self, status: str | None = None) -> list[dict]:
      raise NotImplementedError(_GITHUB_BACKEND_MSG)

  def update_mission(self, mission_id: str, updates: dict) -> None:
      raise NotImplementedError(_GITHUB_BACKEND_MSG)
  ```

  **Preflight in `serve.py` POST /missions handler (returns HTTP 400):**

  ```python
  from antfarm.core.backends.github import GitHubBackend
  if isinstance(_backend, GitHubBackend):
      raise HTTPException(status_code=400, detail=_GITHUB_BACKEND_MSG)
  ```

  The `list_missions()` stub is still called by Queen on every tick — Queen must catch `NotImplementedError` from `list_missions` and treat it as "no missions in this backend, skip the loop" so a colony started against GitHubBackend does not crash. Alternatively, Queen's startup detects the backend type once and short-circuits `run()`. **Chosen: Queen's `_start_queen_thread` checks `isinstance(backend, GitHubBackend)` at thread-start and does not start the thread at all.** Log a single-line info message.

**Tests to write first:**

1. `test_mission_roundtrip` — `Mission.to_dict()`/`from_dict()` preserves all fields including nested `MissionConfig`, `PlanArtifact`, `MissionReport`
2. `test_mission_config_defaults` — default values match spec (max_attempts=3, max_parallel_builders=4, etc.)
3. `test_mission_config_rejects_invalid_completion_mode` — `MissionConfig(completion_mode="nope")` raises ValueError
4. `test_mission_config_accepts_all_or_nothing` — `MissionConfig(completion_mode="all_or_nothing")` constructs successfully (behavior stubbed — see Phase 2 warning test)
5. `test_task_mission_id_roundtrip` — `Task.mission_id` persists through to_dict/from_dict
6. `test_create_mission_writes_file` — mission JSON appears in `.antfarm/missions/`
7. `test_create_mission_duplicate_raises` — second create with same ID raises ValueError
8. `test_update_mission_shallow_merge` — existing fields not in updates are preserved
9. `test_update_mission_not_found_raises` — unknown mission_id raises FileNotFoundError
10. `test_list_missions_filter_by_status` — `list_missions(status="building")` only returns building
11. `test_carry_preserves_mission_id` — `carry({"mission_id": "m1", ...})` → `get_task()` returns task with `mission_id="m1"`
12. `test_update_mission_atomic_under_lock` — concurrent updates don't clobber (spawn threads, assert final state has both changes)

---

### Phase 2 — Colony API: /missions endpoints

**PR 3 of sequence.**

**Goal:** HTTP surface for mission CRUD. Queen and CLI will be on top of this.

**Files touched:**

- `antfarm/core/serve.py` (add endpoints, request models, extend `CarryRequest` with `mission_id`)
- `antfarm/core/colony_client.py` (mirror methods client-side)
- `tests/test_mission_serve.py` (new)
- `tests/test_serve.py` (extend: `carry` accepts `mission_id`, `GET /tasks?mission_id=` filter)

**Request/response models:**

```python
# In serve.py

class MissionCreateRequest(BaseModel):
    mission_id: str | None = None        # server generates if None
    spec: str
    spec_file: str | None = None
    config: dict | None = None            # MissionConfig dict; server fills defaults


class MissionUpdateRequest(BaseModel):
    # Internal — used by Queen. Never exposed through CLI.
    updates: dict


# Extend existing:
class CarryRequest(BaseModel):
    ...
    mission_id: str | None = None
```

**Endpoints:**

| Method | Path | Behavior |
|--------|------|---------|
| `POST` | `/missions` | Create mission. Returns `{"mission_id": "..."}`. 409 on duplicate. Generates id if not provided. Persists `MissionStatus.PLANNING`, empty `task_ids`. |
| `GET` | `/missions` | List missions. Optional `?status=` filter. |
| `GET` | `/missions/{mission_id}` | Full mission JSON or 404. |
| `PATCH` | `/missions/{mission_id}` | Apply shallow updates. 404 if missing. Used by Queen only (not for cancel). |
| `POST` | `/missions/{mission_id}/cancel` | Transition to `CANCELLED` if non-terminal. Idempotent — already-cancelled returns 200. |
| `GET` | `/missions/{mission_id}/report` | Return `mission.report` or 404 if not yet generated. |
| `GET` | `/tasks?mission_id=...` | Extend existing `GET /tasks` to filter by `mission_id`. |
| `POST` | `/tasks` | Extend `CarryRequest`: if `mission_id` is set, stamp it on task JSON and append to `mission.task_ids` atomically. |

**Atomicity rules:**

- `POST /tasks` with `mission_id` set must call `link_task_to_mission(backend, task_dict, mission_id)` (introduced in Phase 1). This helper verifies the mission exists and is non-terminal, carries the task, and appends the task ID to `mission.task_ids` — all without the HTTP handler touching backend locks. On carry failure, the mission is not modified.
- `POST /missions/{id}/cancel` is a no-op if the mission is already in a terminal state.

**`completion_mode="all_or_nothing"` handling (v0.6.0 stub):**

`POST /missions` accepts `completion_mode="all_or_nothing"` and persists it verbatim, but the handler logs a warning at creation time and Queen treats the mission identically to `best_effort`:

```python
# In serve.py POST /missions handler
if cfg.completion_mode == "all_or_nothing":
    logger.warning(
        "mission %s requested completion_mode='all_or_nothing'; "
        "treated as best_effort for v0.6.0 (real semantics land in v0.6.1+)",
        mission_id,
    )
```

Queen's completion logic always uses the best-effort rule (`no in-flight tasks → COMPLETE`). A follow-up `test_create_mission_all_or_nothing_warns` asserts the warning fires and the mission still completes on best-effort terms.

**Colony client additions:**

```python
# antfarm/core/colony_client.py

def create_mission(self, spec: str, spec_file: str | None = None,
                   config: dict | None = None) -> dict: ...
def get_mission(self, mission_id: str) -> dict: ...
def list_missions(self, status: str | None = None) -> list[dict]: ...
def update_mission(self, mission_id: str, updates: dict) -> None: ...  # Queen only
def cancel_mission(self, mission_id: str) -> None: ...
def get_mission_report(self, mission_id: str) -> dict | None: ...

# Extend:
def carry(self, ..., mission_id: str | None = None) -> dict: ...
```

**Edge cases + invariants:**

- `POST /missions` with `mission_id=None` generates `f"mission-{int(time.time() * 1000)}"`. If caller provides a slug via CLI, it's prefixed to `mission-<slug>-<ms>`.
- `/missions/{id}/cancel` does NOT kick back active tasks. Cancel only flips the mission status. Explicit cancellation rules:
  1. **In-flight workers finish their current attempt.** Harvests still succeed. These tasks remain linked to the cancelled mission.
  2. **Queen stops spawning new child tasks** for a cancelled mission. On the next tick, Queen sees `status=CANCELLED` and skips the mission entirely.
  3. **Soldier suppresses new review-task creation** for tasks whose `mission_id` points to a CANCELLED mission. Soldier checks `mission.status` before calling `create_review_task`. Already-created review tasks are allowed to complete (they're in the queue and harmless).
  4. **Completed tasks appear in the mission report** under a "completed before cancellation" heading. The report is generated at cancel time (or on the next Queen tick) so the operator can see what shipped.
- `GET /tasks?mission_id=...` must union `ready/`, `active/`, `done/`, `blocked/`, `paused/` folders — use existing `list_tasks()` then filter in-memory.

**Tests to write first:**

1. `test_create_mission_endpoint_returns_id`
2. `test_create_mission_duplicate_returns_409`
3. `test_create_mission_all_or_nothing_warns` — `completion_mode="all_or_nothing"` logs the warning and still persists the field; mission completes on best-effort terms (assert via captured log)
4. `test_list_missions_empty_returns_empty_list`
5. `test_list_missions_filter_by_status`
6. `test_get_mission_404`
7. `test_patch_mission_merges_fields`
8. `test_cancel_mission_terminal_state`
9. `test_cancel_mission_idempotent`
10. `test_carry_with_mission_id_appends_to_task_ids`
11. `test_carry_with_unknown_mission_id_404`
12. `test_list_tasks_filter_by_mission_id`
13. `test_create_mission_rejects_github_backend` — colony bound to a `GitHubBackend` instance, `POST /missions` returns 400 with the canonical `_GITHUB_BACKEND_MSG` string; no mission file is written; Queen thread was not started for this backend
14. `test_github_backend_create_mission_raises_friendly_error` — direct call to `GitHubBackend.create_mission({...})` raises `NotImplementedError` whose `str()` equals `_GITHUB_BACKEND_MSG`

---

### Phase 3 — Queen controller (daemon thread)

**PR 4 of sequence.**

**Goal:** Queen advances missions through their lifecycle. Deterministic, stateless between ticks, crash-recoverable.

**Files touched:**

- `antfarm/core/queen.py` (new)
- `antfarm/core/serve.py` (add `_start_queen_thread`, gate with `enable_queen` flag and config)
- `tests/test_queen.py` (new)

**Key interfaces:**

```python
# antfarm/core/queen.py

import logging
import time
from dataclasses import dataclass

from antfarm.core.backends.base import TaskBackend
from antfarm.core.missions import Mission, MissionStatus, PlanArtifact

logger = logging.getLogger(__name__)


@dataclass
class QueenConfig:
    base_interval: float = 30.0
    active_interval: float = 10.0
    idle_interval: float = 60.0
    max_re_plans: int = 1


class Queen:
    def __init__(self, backend: TaskBackend, config: QueenConfig | None = None,
                 clock=time.time):
        self.backend = backend
        self.config = config or QueenConfig()
        self._clock = clock
        self._stopped = False

    # --- main loop ---

    def run(self) -> None:
        while not self._stopped:
            missions = self.backend.list_missions()
            for m in missions:
                if m["status"] in ("complete", "failed", "cancelled"):
                    continue
                try:
                    self._advance(m)
                except Exception as e:
                    logger.exception("queen: failed to advance mission %s: %s",
                                     m["mission_id"], e)
            time.sleep(self._adaptive_interval(missions))

    def stop(self) -> None:
        self._stopped = True

    # --- per-tick phase dispatch (all idempotent, all read fresh state) ---

    def _advance(self, mission: dict) -> None:
        status = mission["status"]
        if status == MissionStatus.PLANNING:
            self._advance_planning(mission)
        elif status == MissionStatus.REVIEWING_PLAN:
            self._advance_reviewing_plan(mission)
        elif status == MissionStatus.BUILDING:
            self._advance_building(mission)
        elif status == MissionStatus.BLOCKED:
            self._advance_blocked(mission)

    # --- phase handlers ---

    def _advance_planning(self, mission: dict) -> None:
        """If plan task does not yet exist, create it.
        If plan task is harvested, extract PlanArtifact, transition state.

        Plan task failure modes are split:
        - Plan task is BLOCKED (exhausted FileBackend.max_attempts via normal
          Soldier kickback path) → mission FAILED. This is the retry ceiling
          for malformed/unusable planner output. The re-plan budget is NOT
          consumed because this path is not driven by reviewer feedback.
        - Plan task harvested but artifact is missing/invalid → treat like
          a normal task failure: append a failure trail entry, do nothing.
          Soldier will kickback (which counts against FileBackend.max_attempts
          on the plan task, not re_plan_count). Queen retries the read on the
          next tick.
        - Plan task harvested WITH valid artifact and plan-review NEEDS_CHANGES
          → consume re-plan budget (handled in _advance_reviewing_plan, max 1).
        """
        plan_task_id = mission.get("plan_task_id")
        if plan_task_id is None:
            plan_task_id = self._create_plan_task(mission)
            self.backend.update_mission(mission["mission_id"], {
                "plan_task_id": plan_task_id,
                "task_ids": mission["task_ids"] + [plan_task_id],
                "last_progress_at": _now_iso(),
            })
            return

        plan_task = self.backend.get_task(plan_task_id)
        if plan_task is None:
            # Plan task vanished — serious error, mark failed
            self._fail(mission, f"plan task {plan_task_id} disappeared")
            return

        # Plan task exhausted FileBackend retry budget → mission failed.
        # This path does NOT consume re_plan_count.
        # Prefix convention (NB-4): "system: " signals an infra/worker
        # failure that should page a human — not a content-level rejection.
        if plan_task["status"] == "blocked":
            attempt_count = len(plan_task.get("attempts", []))
            self._fail(
                mission,
                f"system: plan task {plan_task_id} blocked after "
                f"{attempt_count} attempts (malformed or unusable planner output)",
            )
            return

        if plan_task["status"] != "done":
            return  # still waiting (ready/active/harvest_pending)

        # Plan task is done. Extract artifact from current attempt.
        artifact = self._extract_plan_artifact(plan_task)
        if artifact is None:
            # Invalid/malformed artifact on a harvested plan task. Do NOT
            # consume re_plan_count. Append a trail entry and let Soldier's
            # normal review path kick this back through the standard
            # max_attempts retry loop. Queen returns and re-reads next tick.
            logger.warning(
                "queen: mission %s plan task %s harvested with no valid "
                "PlanArtifact; deferring to Soldier kickback loop",
                mission["mission_id"], plan_task_id,
            )
            try:
                self.backend.append_trail(plan_task_id, {
                    "ts": _now_iso(),
                    "worker_id": "queen",
                    "message": "plan artifact invalid/missing; awaiting kickback",
                    "action_type": "failure",
                })
            except Exception:
                pass
            return

        require_review = mission["config"]["require_plan_review"]
        if require_review:
            self._transition(mission, MissionStatus.REVIEWING_PLAN,
                             extras={"plan_artifact": artifact.to_dict()})
            self._create_plan_review_task(mission, artifact)
        else:
            self._spawn_child_tasks(mission, artifact)
            self._transition(mission, MissionStatus.BUILDING,
                             extras={"plan_artifact": artifact.to_dict()})

    def _advance_reviewing_plan(self, mission: dict) -> None:
        """Check plan-review task state and act accordingly.

        Failure mode split (NB-4):

        1. Review task in ``ready`` (typically after doctor recovered a
           stuck active reviewer) → Queen is a no-op. Poll next tick.
        2. Review task in ``blocked`` (FileBackend max_attempts exhausted,
           reviewer worker kept crashing) → system failure. Mission FAILED
           with reason prefix ``"system: "`` so the morning digest can
           surface it as an infra issue, not a code issue.
        3. Review task in ``done`` but no verdict dict yet → Queen no-ops
           (reviewer hasn't persisted the verdict) and retries next tick.
        4. Review task in ``done`` with verdict=``pass`` → spawn children,
           transition to BUILDING.
        5. Review task in ``done`` with verdict=``needs_changes`` → consume
           ``re_plan_count``. Second NEEDS_CHANGES fails the mission with
           reason prefix ``"review: "`` to distinguish from (2).
        6. Review task in ``done`` with verdict=``blocked`` → mission FAILED
           with reason prefix ``"review: "``.
        """
        from antfarm.core.review_pack import extract_verdict_from_review_task

        review_task_id = self._plan_review_task_id(mission)
        review_task = self.backend.get_task(review_task_id)
        if review_task is None:
            return  # just created; next tick will find it

        status = review_task["status"]
        # (1) Doctor recovered a stuck reviewer — wait for next attempt
        if status == "ready":
            return
        # (2) Reviewer keeps crashing — infra failure, NOT a code failure
        if status == "blocked":
            attempt_count = len(review_task.get("attempts", []))
            self._fail(
                mission,
                f"system: plan review task blocked after {attempt_count} attempts",
            )
            return
        if status != "done":
            return  # active / harvest_pending — still working

        verdict = extract_verdict_from_review_task(review_task)
        # (3) Harvested but verdict not yet persisted — retry next tick
        if verdict is None:
            return

        if verdict["verdict"] == "pass":
            artifact = PlanArtifact.from_dict(mission["plan_artifact"])
            self._spawn_child_tasks(mission, artifact)
            self._transition(mission, MissionStatus.BUILDING)
        elif verdict["verdict"] == "needs_changes":
            if mission["re_plan_count"] >= self.config.max_re_plans:
                # (5b) Second genuine rejection — distinct from system failure
                summary = verdict.get("summary", "no summary")
                self._fail(mission, f"review: plan rejected - {summary}")
                return
            self._create_re_plan_task(mission, verdict)
            self.backend.update_mission(mission["mission_id"], {
                "re_plan_count": mission["re_plan_count"] + 1,
                "status": MissionStatus.PLANNING.value,
                "plan_task_id": None,    # new plan task will be created
                "plan_artifact": None,
            })
        else:  # verdict == "blocked" — reviewer classified plan as unfixable
            self._fail(
                mission,
                f"review: plan rejected - {verdict.get('summary', 'blocked')}",
            )

    def _advance_building(self, mission: dict) -> None:
        """Check child task status. Complete mission if all accounted for."""
        child_tasks = [self.backend.get_task(tid) for tid in mission["task_ids"]]
        child_tasks = [t for t in child_tasks if t is not None
                       and not is_infra_task(t)]

        if not child_tasks:
            return  # no impl tasks yet — still spawning

        merged = [t for t in child_tasks if self._has_merged_attempt(t)]
        blocked = [t for t in child_tasks if t["status"] == "blocked"]
        in_flight = [t for t in child_tasks
                     if t["status"] in ("ready", "active", "done", "harvest_pending")]

        # Track any task progress so stall detector stays fresh.
        if self._had_progress_since_last_tick(mission, child_tasks):
            self.backend.update_mission(mission["mission_id"], {
                "last_progress_at": _now_iso(),
            })

        # Blocked task bookkeeping
        blocked_ids = [t["id"] for t in blocked]
        if set(blocked_ids) != set(mission["blocked_task_ids"]):
            self.backend.update_mission(mission["mission_id"], {
                "blocked_task_ids": blocked_ids,
            })

        if not in_flight:
            # Terminal: everything is either merged or blocked.
            report = self._generate_report(mission)
            self._transition(mission, MissionStatus.COMPLETE,
                             extras={"report": report.to_dict(),
                                     "completed_at": _now_iso()})
            return

        self._check_stall(mission)

    def _advance_blocked(self, mission: dict) -> None:
        """Check for unblock (operator ran `antfarm unblock`) or timeout."""
        child_tasks = [self.backend.get_task(tid) for tid in mission["task_ids"]]
        child_tasks = [t for t in child_tasks if t is not None
                       and not is_infra_task(t)]
        in_flight = [t for t in child_tasks
                     if t["status"] in ("ready", "active", "done")]
        if in_flight:
            self._transition(mission, MissionStatus.BUILDING)
            return
        self._check_stall_timeout(mission)

    def _check_stall(self, mission: dict) -> None:
        threshold = mission["config"]["stall_threshold_minutes"] * 60
        last = _parse_iso(mission["last_progress_at"])
        if self._clock() - last > threshold:
            logger.warning("queen: mission %s stalled after %s minutes",
                           mission["mission_id"],
                           mission["config"]["stall_threshold_minutes"])
            self._transition(mission, MissionStatus.BLOCKED)

    def _check_stall_timeout(self, mission: dict) -> None:
        if mission["config"]["blocked_timeout_action"] != "fail":
            return
        threshold = mission["config"]["blocked_timeout_minutes"] * 60
        last = _parse_iso(mission["last_progress_at"])
        if self._clock() - last > threshold:
            self._fail(mission, "blocked_timeout exceeded")

    # --- helpers (see full impl in the module) ---

    def _create_plan_task(self, mission: dict) -> str: ...
    def _create_plan_review_task(self, mission: dict, artifact: PlanArtifact) -> str: ...
    def _create_re_plan_task(self, mission: dict, verdict: dict) -> str: ...
    def _spawn_child_tasks(self, mission: dict, artifact: PlanArtifact) -> list[str]: ...
    def _extract_plan_artifact(self, plan_task: dict) -> PlanArtifact | None: ...
    def _generate_report(self, mission: dict) -> "MissionReport": ...
    def _adaptive_interval(self, missions: list[dict]) -> float: ...
    def _transition(self, mission, new_status, extras: dict | None = None) -> None: ...
    def _fail(self, mission: dict, reason: str) -> None: ...
    @staticmethod
    def _has_merged_attempt(task: dict) -> bool: ...   # reuse Soldier._has_merged_attempt
    # NOTE: task-kind filtering uses missions.is_infra_task() — see below.
```

```python
# antfarm/core/serve.py additions

def _start_queen_thread(backend: TaskBackend, enabled: bool) -> None:
    global _queen_thread
    if not enabled:
        return
    if _queen_thread is not None and _queen_thread.is_alive():
        return
    from antfarm.core.queen import Queen
    queen = Queen(backend)
    def _loop():
        try:
            queen.run()
        except Exception as e:
            logger.error("queen thread crashed: %s", e)
    _queen_thread = threading.Thread(target=_loop, daemon=True, name="queen")
    _queen_thread.start()
```

**Queen is ON by default.** The colony unconditionally starts the Queen thread at boot. `antfarm colony --no-queen` exists solely as a debug/test escape hatch — it is documented as "for tests, not production" in the CLI help and README. Autoscaler is the opposite (opt-in via `--autoscaler`, see Phase 7).

**Child task ID naming:** `task-{mission_slug}-{NN:02d}`. Example: `mission-auth-jwt-1712634560000` → `task-auth-jwt-01`, `task-auth-jwt-02`, ... Child task IDs include the mission slug (stripped of the numeric suffix) for readability. Reuses the existing planner `resolve_dependencies` for index-to-ID rewriting.

**Re-plan task:** ID is `plan-{mission_id}-re1`. Max 1 re-plan. `re_plan_count` on the mission tracks it.

**Plan review task spec:** Generated by `_create_plan_review_task`. Uses the template from spec §v0.6.0.4. Capabilities: `["review"]`. Priority: `1`.

**Plan task spec (initial):** The plan task spec includes the mission spec verbatim, plus a preamble instructing the planner to output a JSON array with max 10 tasks. Capabilities: `["plan"]`. Priority: `1`.

**Edge cases + invariants:**

- Queen must be re-runnable after a crash. Every `_advance_*` method reads fresh state from the backend and is idempotent — no in-memory transient state between ticks.
- `_spawn_child_tasks` must use deterministic IDs so that a crash between spawning task N and task N+1 allows resume without duplicates. Child IDs are computed from `(mission_id, index)`. Carry is idempotent on duplicate ID (returns 409, caller treats as success — same pattern as `WorkerRuntime._process_plan_output`).
- Queen never calls `backend.kickback()` directly. Kickback is the Soldier's job. Queen only reads state.
- Queen never starts workers. That's the Autoscaler.
- `_transition` is the only write path that changes `status`. All field updates go through `update_mission`.
- Plan-review verdict extraction uses `antfarm.core.review_pack.extract_verdict_from_review_task` — the public helper created in Phase 0. **No duplication, no reaching into `Soldier._extract_verdict_from_review_task`.** The function is shared between Queen and Soldier.
- **Failure-reason prefix convention (NB-4):** every string passed to `_fail()` MUST start with one of two prefixes:
  - `"system: "` — infrastructure or worker-level failure (plan task blocked, review task blocked, stall timeout). These signal "page a human, something is broken in the pipeline."
  - `"review: "` — content-level rejection (reviewer returned `needs_changes` twice, or verdict=`blocked`). These signal "the work product was unacceptable; no infra issue."
  The prefix is load-bearing: Phase 6's report formatter is required to surface `system:` and `review:` entries differently so the morning digest distinguishes infra failures from content rejections. (The formatter's exact rendering is Phase 6's decision; only the requirement is specified here.)
  This is a convention enforced in code and asserted in tests — no `MissionFailureReason` enum (YAGNI).
- Stall detection compares the wall-clock delta against the last progress timestamp. Progress = any child task changing status since last tick. Queen maintains `last_progress_at` on the mission JSON. No per-tick diff memory — Queen persists a `_last_task_status_hash` field as part of the mission and compares.

**Tests to write first (write unit tests with a fake clock and in-memory `FileBackend` over tmp_path):**

1. `test_queen_planning_creates_plan_task` — fresh mission → Queen creates plan task with `capabilities_required=["plan"]`, mission gains `plan_task_id`
2. `test_queen_planning_waits_for_harvest` — plan task `status="ready"` → Queen is a no-op
3. `test_queen_planning_harvested_no_review_spawns_children` — `require_plan_review=false` + harvested plan task → child tasks created, status → BUILDING
4. `test_queen_planning_harvested_with_review_creates_review_task` — default config → review-plan task created, status → REVIEWING_PLAN
5. `test_queen_planning_plan_task_blocked_fails_mission` — plan task status=BLOCKED (FileBackend max_attempts exhausted) → mission → FAILED, `re_plan_count` unchanged, failure reason begins with `"system: "`
6. `test_queen_planning_invalid_artifact_defers_to_kickback` — plan task `status="done"` with no `PlanArtifact` → Queen is a no-op, appends failure trail entry, `re_plan_count` unchanged, mission stays in PLANNING (waiting for Soldier kickback)
7. `test_queen_review_task_ready_is_noop` — plan-review task `status="ready"` (doctor recovered a stuck reviewer) → Queen does nothing, mission stays in REVIEWING_PLAN
8. `test_queen_review_task_blocked_fails_with_system_prefix` — plan-review task `status="blocked"` (reviewer worker keeps crashing) → mission FAILED, reason starts with `"system: "`
9. `test_queen_review_pass_spawns_children`
10. `test_queen_review_needs_changes_triggers_re_plan` — valid artifact, reviewer NEEDS_CHANGES → `re_plan_count` increments to 1, status → PLANNING, `plan_task_id=None`, `plan_artifact=None`
11. `test_queen_review_verdict_needs_changes_twice_fails_with_review_prefix` — second reviewer NEEDS_CHANGES → FAILED with reason starting with `"review: "` (distinct from the `"system: "` path)
12. `test_queen_review_verdict_blocked_fails_with_review_prefix` — reviewer returns verdict `"blocked"` → FAILED, reason starts with `"review: "`
13. `test_queen_building_all_merged_completes` — all child tasks have `MERGED` attempts → status → COMPLETE with report
14. `test_queen_building_mixed_merged_blocked_completes` — best-effort: merged + blocked + nothing in-flight → COMPLETE
15. `test_queen_building_some_in_flight_stays_building`
16. `test_queen_stall_detection` — fake clock advances past stall threshold, no progress → BLOCKED
17. `test_queen_blocked_timeout_fail` — `blocked_timeout_action="fail"` + time passes → FAILED
18. `test_queen_blocked_unblocked_resumes_building` — task moves back to ready → BUILDING
19. `test_queen_terminal_states_are_skipped` — COMPLETE/FAILED/CANCELLED missions are never advanced
20. `test_queen_advance_is_idempotent` — two consecutive `_advance` calls on same mission → same state
21. `test_queen_crash_recovery` — simulate: create mission, advance once, discard Queen instance, create new Queen, advance again — state is consistent
22. `test_queen_all_or_nothing_treated_as_best_effort` — mission with `completion_mode="all_or_nothing"` + mixed merged/blocked children → COMPLETE (same as best_effort)

---

### Phase 4 — Planner worker: mission mode

**PR 5 of sequence.**

**Goal:** When a planner worker harvests a mission plan task, it stores the plan as an artifact on the task's attempt and does NOT carry child tasks. Non-mission plans retain v0.5.8 behavior (planner carries children directly).

**Files touched:**

- `antfarm/core/worker.py` (extend `_process_plan_output` with a mission-mode branch)
- `antfarm/core/missions.py` (ensure `PlanArtifact` serializes into `attempt.artifact`)
- `tests/test_worker.py` (extend)

**Key change in `_process_plan_output`:**

```python
def _process_plan_output(self, task: dict, attempt_id: str, output: str) -> dict | None:
    # ... existing parse + validate ...

    resolved_tasks = resolve_dependencies(tasks, child_ids)
    warnings = engine.generate_warnings(plan_result)
    warn_strs = [str(w) for w in warnings] if isinstance(warnings, list) else []

    dep_summary = _build_dep_summary(resolved_tasks, child_ids)

    # ---- MISSION MODE ----
    if task.get("mission_id"):
        # Do NOT carry child tasks. Store a PlanArtifact the Queen will consume.
        plan_artifact = PlanArtifact(
            plan_task_id=task["id"],
            attempt_id=attempt_id,
            proposed_tasks=[t.to_carry_dict() for t in resolved_tasks],
            task_count=len(resolved_tasks),
            warnings=warn_strs,
            dependency_summary=dep_summary,
        )
        return {
            "mission_mode": True,
            "plan_artifact": plan_artifact.to_dict(),
            "warnings": warn_strs,
            "dep_summary": dep_summary,
        }

    # ---- LEGACY (non-mission) MODE: existing path ----
    # ... carry each child task ...
```

And in the harvest path, when `mission_mode=True`, the harvest artifact includes the plan under a new key `artifact["plan_result"]` (spec-compatible — `TaskArtifact` is untyped about extra keys; or add `plan_artifact: dict | None` as a new optional field on `TaskArtifact`). **Decision: add optional `plan_artifact` field to `TaskArtifact` to stay schema-explicit.**

**`TaskArtifact` addition:**

```python
@dataclass
class TaskArtifact:
    ...existing fields...
    # Mission-mode plan output (only present on planner tasks in a mission)
    plan_artifact: dict | None = None
```

**Edge cases + invariants:**

- A plan task with `mission_id` MUST NOT carry children — the `mission_mode=True` branch is the only one that runs.
- A plan task without `mission_id` retains the legacy v0.5.8 behavior (backwards compat for `antfarm carry --type plan`).
- Plan validation errors in mission mode trail to the plan task just like legacy mode, and the task harvests with no artifact. Queen detects the missing artifact and **defers to the Soldier kickback loop** (Phase 3 `_advance_planning` path for invalid artifacts) — this counts against `FileBackend.max_attempts` on the plan task, and `re_plan_count` is untouched. Only after the plan task reaches `status=blocked` does Queen fail the mission (with reason prefix `"system: "`).
- Harvest must set `attempt.artifact.plan_artifact = {...}` — Queen reads from `task.current_attempt.artifact.plan_artifact`.

**Tests:**

1. `test_planner_mission_mode_stores_artifact` — plan task with `mission_id` → harvest artifact has `plan_artifact`, no children carried
2. `test_planner_legacy_mode_carries_children` — plan task without `mission_id` → children carried (existing behavior)
3. `test_planner_mission_mode_invalid_plan_trails_and_no_artifact`
4. `test_task_artifact_plan_artifact_roundtrip`

---

### Phase 5 — Soldier: mission_id propagation

**PR 6 of sequence.**

**Goal:** Soldier stamps `mission_id` on review tasks it creates, so they're counted as part of the parent mission.

**Files touched:**

- `antfarm/core/soldier.py` (`create_review_task` reads parent `mission_id` and passes it to `BackendAdapter.carry`)
- `antfarm/core/soldier.py` (`_BackendAdapter.carry` passes `mission_id` through)
- `tests/test_soldier.py` (extend)

**Key change:**

```python
# In soldier.create_review_task
parent_mission_id = task.get("mission_id")
...
self._backend.carry(
    id=review_task_id,
    title=...,
    spec=...,
    mission_id=parent_mission_id,   # NEW
    capabilities_required=["review"],
    priority=1,
)
```

**Also**: when a review task is created for a mission-mode task, its `id` must be added to `mission.task_ids`. Soldier cannot write to the mission directly — it must go through the colony API (`POST /tasks` with `mission_id` set, which already performs the atomic `update_mission`). **Decision: Soldier uses `ColonyClient` (already does via `_BackendAdapter`'s `carry`) — extend the adapter to POST through the API instead of the backend directly when `mission_id` is set.** But the adapter is built over the backend, not an HTTP client. Cleanest fix: Soldier does two calls — `backend.carry()` then `backend.update_mission(parent_mission_id, {"task_ids": [..., review_id]})` inside the backend `_lock` scope.

**Cleaner decision (chosen):** Use the shared `link_task_to_mission()` helper introduced in Phase 1 (`antfarm/core/missions.py`). Both the HTTP handler (Phase 2) and Soldier already consume it — no new code needed here, just wire it into the review-task creation path.

**Tests:**

1. `test_soldier_review_task_inherits_mission_id`
2. `test_soldier_review_task_appended_to_mission_task_ids`
3. `test_soldier_review_task_no_mission_id_when_parent_has_none` (backwards compat)
4. `test_soldier_suppresses_review_for_cancelled_mission` — done task with `mission_id` pointing to a CANCELLED mission → Soldier does NOT create a review task

---

### Phase 6 — Mission report generator

**PR 7 of sequence.**

**Goal:** `MissionReport` can be generated from a mission + its child tasks and rendered as JSON, terminal, and markdown. **Dependency-free** — terminal rendering uses only `textwrap` from the stdlib so the morning digest works in headless CI/cron jobs without `rich`.

**Files touched:**

- `antfarm/core/report.py` (new)
- `antfarm/core/queen.py` (wire `_generate_report` to call `report.build_report`)
- `tests/test_report.py` (new)

**Key interfaces:**

```python
# antfarm/core/report.py
#
# DEPENDENCY-FREE MODULE. Imports only from stdlib (textwrap, json, pathlib)
# and antfarm.core.missions. MUST NOT import rich, colorama, or any TUI
# framework — the morning digest must run headless in CI/cron.

from antfarm.core.backends.base import TaskBackend
from antfarm.core.missions import (
    Mission, MissionReport, MissionReportTask, MissionReportBlocked, MissionStatus,
)


def build_report(mission: dict, tasks: list[dict]) -> MissionReport:
    """Build a MissionReport from a mission dict and its child task dicts.

    Pure function. No I/O. Reads attempts/artifacts for line counts and PR URLs.
    Surfaces failure-reason prefixes (``"system: "`` vs ``"review: "``) on
    ``MissionReportBlocked.reason`` so the terminal/markdown renderers can
    distinguish infra failures from content rejections.
    """
    ...


def render_terminal(report: MissionReport, use_rich: bool = False) -> str:
    """Return a plain-text string suitable for stdout printing.

    v0.6.0: ``use_rich`` MUST be False. The parameter exists as a
    forward-compat hook — a future version can lazy-import ``rich``
    inside the method to enable colour output without a dependency bump
    or breaking headless callers.

    Uses only ``textwrap`` from the stdlib. 80-column wrap by default.
    """
    if use_rich:
        raise NotImplementedError(
            "rich rendering is a v0.6.1+ opt-in; v0.6.0 is dependency-free"
        )
    ...


def render_markdown(report: MissionReport) -> str:
    """Return a markdown string suitable for pasting into GitHub issues/PRs."""
    ...


def save_report(data_dir: str, mission_id: str, report: MissionReport) -> str:
    """Write report JSON to .antfarm/missions/{mission_id}_report.json.
    Returns the path written."""
    ...
```

**Report derivation rules:**

- `total_tasks` = count of tasks in `mission.task_ids` where `not is_infra_task(task)` (excludes plan + review tasks)
- `merged_tasks` = count with any attempt in status MERGED
- `blocked_tasks` = count with `status=blocked`
- `failed_reviews` = count of review tasks with verdict `needs_changes` or `blocked`
- `merged` list: per task, pull `current_attempt.artifact` → `files_changed`, `lines_added`, `lines_removed`, `pr_url`
- `blocked` list: per blocked task, pull `reason` from the last failure trail entry and `attempt_count` from `len(attempts)`, `last_failure_type` from the last `FailureRecord` if present
- `risks`: union of `attempt.artifact.risks` across merged tasks
- Terminal render: 80-column table. Use Python's built-in `textwrap` — NO new dependency. (Current TUI uses `rich` but we keep the report renderer dependency-free for use in headless CI.)
- **Failure-reason prefix surfacing:** both `render_terminal` and `render_markdown` MUST visually distinguish `reason.startswith("system: ")` from `reason.startswith("review: ")`. Exact formatting is left to the implementer — examples: a `[SYSTEM]` / `[REVIEW]` tag in terminal output, or a `[infra]` / `[plan]` markdown badge. The requirement is that an operator reading the digest can tell at a glance whether they're looking at a broken worker or a rejected plan.

**Tests:**

1. `test_build_report_empty_mission`
2. `test_build_report_all_merged`
3. `test_build_report_mixed_merged_blocked`
4. `test_build_report_skips_plan_and_review_tasks_from_total`
5. `test_build_report_aggregates_lines_added_removed`
6. `test_build_report_collects_pr_urls_from_artifacts`
7. `test_build_report_blocked_pulls_reason_from_trail`
8. `test_render_terminal_smoke` — no exceptions, contains mission_id and duration
9. `test_render_terminal_distinguishes_system_vs_review_prefix` — a report with one `"system: ..."` blocked entry and one `"review: ..."` blocked entry renders them visually differently (assert both tags or both markers are present in the output)
10. `test_render_terminal_use_rich_raises_not_implemented` — `render_terminal(report, use_rich=True)` raises NotImplementedError (forward-compat hook gate)
11. `test_render_terminal_no_rich_import` — assert `rich` is not in `sys.modules` after importing `antfarm.core.report` (dependency hygiene regression gate)
12. `test_render_markdown_smoke` — contains headings and PR URLs
13. `test_render_markdown_distinguishes_system_vs_review_prefix` — same requirement as #9 but for markdown output
14. `test_save_report_writes_json_file`
15. `test_cancelled_mission_report_includes_completed_tasks` — mission cancelled after 3/5 tasks merged → report lists the 3 merged tasks under "completed before cancellation"
16. `test_report_includes_completion_mode` — report JSON and terminal output both include `completion_mode` field; `all_or_nothing` renders with the v0.6.0 warning suffix

---

### Phase 7 — Autoscaler

**PR 8 of sequence.**

**Goal:** Single-host autoscaler starts/stops worker subprocesses based on queue state. Opt-in via `--autoscaler`.

**Files touched:**

- `antfarm/core/autoscaler.py` (new)
- `antfarm/core/serve.py` (add `_start_autoscaler_thread` + `--autoscaler` plumbing)
- `antfarm/core/cli.py` (add `--autoscaler`, `--max-builders`, `--max-reviewers` options on `colony` command)
- `tests/test_autoscaler.py` (new)

**Key interfaces:**

```python
# antfarm/core/autoscaler.py

import logging
import subprocess
import time
from dataclasses import dataclass, field

from antfarm.core.backends.base import TaskBackend

logger = logging.getLogger(__name__)


@dataclass
class AutoscalerConfig:
    enabled: bool = False          # OFF by default; opt-in via `antfarm colony --autoscaler`
    agent_type: str = "claude-code"
    node_id: str = "local"
    repo_path: str = "."
    integration_branch: str = "main"
    workspace_root: str = "./.antfarm/workspaces"
    max_builders: int = 4
    max_reviewers: int = 2
    token: str | None = None
    poll_interval: float = 30.0


@dataclass
class ManagedWorker:
    name: str
    role: str           # "planner" | "builder" | "reviewer"
    worker_id: str
    process: subprocess.Popen


class Autoscaler:
    def __init__(self, backend: TaskBackend, config: AutoscalerConfig, clock=time.time):
        self.backend = backend
        self.config = config
        self._clock = clock
        self.managed: dict[str, ManagedWorker] = {}
        self._stopped = False

    def run(self) -> None:
        while not self._stopped:
            try:
                self._reconcile()
            except Exception as e:
                logger.exception("autoscaler reconcile failed: %s", e)
            time.sleep(self.config.poll_interval)

    def stop(self) -> None:
        self._stopped = True
        # Terminate managed workers on shutdown
        for mw in list(self.managed.values()):
            if mw.process.poll() is None:
                mw.process.terminate()

    # --- core loop ---

    def _reconcile(self) -> None:
        self._cleanup_exited()
        tasks = self.backend.list_tasks()
        workers = self.backend.list_workers()
        desired = self._compute_desired(tasks, workers)
        actual = self._count_actual()
        for role in ("planner", "builder", "reviewer"):
            self._reconcile_role(role, desired[role], actual.get(role, 0))

    def _compute_desired(self, tasks: list[dict],
                         workers: list[dict]) -> dict[str, int]:
        ready_plan = [t for t in tasks
                      if t["status"] == "ready"
                      and "plan" in t.get("capabilities_required", [])]
        ready_build = [t for t in tasks
                       if t["status"] == "ready"
                       and not t.get("capabilities_required")]
        ready_review = [t for t in tasks
                        if t["status"] == "ready"
                        and "review" in t.get("capabilities_required", [])]
        done_unreviewed = [t for t in tasks
                           if t["status"] == "done"
                           and not t["id"].startswith("review-")
                           and not self._has_verdict(t)
                           and not self._has_merged_attempt(t)]

        scope_groups = self._count_scope_groups(ready_build)

        active_builders = [w for w in workers
                           if "review" not in w.get("capabilities", [])
                           and "plan" not in w.get("capabilities", [])
                           and w.get("status") != "offline"]
        rate_limited = [w for w in active_builders if self._is_rate_limited(w)]

        desired_builders = min(
            scope_groups,
            self.config.max_builders,
            len(ready_build),
        )
        if rate_limited and len(rate_limited) > len(active_builders) // 2:
            desired_builders = min(desired_builders, len(active_builders))

        return {
            "planner": 1 if ready_plan else 0,
            "builder": desired_builders,
            "reviewer": min(
                max(1 if (done_unreviewed or ready_review) else 0, len(ready_review)),
                self.config.max_reviewers,
            ),
        }

    @staticmethod
    def _count_scope_groups(tasks: list[dict]) -> int:
        """Count non-overlapping scope groups (union-find by touches)."""
        if not tasks:
            return 0
        groups: list[set[str]] = []
        for t in tasks:
            touches = set(t.get("touches", []))
            if not touches:
                groups.append(set())
                continue
            hit = None
            for g in groups:
                if g & touches:
                    g.update(touches)
                    hit = g
                    break
            if hit is None:
                groups.append(touches)
        return len(groups)

    # --- worker lifecycle ---

    def _reconcile_role(self, role: str, desired: int, actual: int) -> None:
        delta = desired - actual
        while delta > 0:
            self._start_worker(role)
            delta -= 1
        while delta < 0:
            if not self._stop_idle_worker(role):
                break    # no idle workers to stop this tick
            delta += 1

    def _start_worker(self, role: str) -> None: ...
    def _stop_idle_worker(self, role: str) -> bool: ...
    def _cleanup_exited(self) -> None: ...
    def _count_actual(self) -> dict[str, int]: ...
    @staticmethod
    def _has_verdict(task: dict) -> bool: ...
    @staticmethod
    def _has_merged_attempt(task: dict) -> bool: ...
    @staticmethod
    def _is_rate_limited(worker: dict) -> bool: ...
```

**Worker start command template:**

```python
# `--type` on `antfarm worker start` already accepts
# click.Choice(["builder", "reviewer", "planner"]) — verified in
# antfarm/core/cli.py:304-310 (shipped in v0.5.x). Autoscaler consumes
# the existing flag; no CLI changes are required for this phase.
cmd = [
    "antfarm", "worker", "start",
    "--agent", self.config.agent_type,
    "--type", role,                     # role ∈ {"builder","reviewer","planner"}
    "--node", self.config.node_id,
    "--name", name,
    "--repo-path", self.config.repo_path,
    "--integration-branch", self.config.integration_branch,
    "--workspace-root", self.config.workspace_root,
]
if self.config.token:
    cmd.extend(["--token", self.config.token])
```

**Role naming:** The autoscaler's internal `role` strings are `"planner"`, `"builder"`, `"reviewer"` — identical to the `--type` choices on `worker start`. `_compute_desired` and `_reconcile_role` use these strings verbatim. No translation layer needed.

**Stop policy:** Only terminate workers whose colony-reported `status == "idle"`. The local `process.poll()` state is a liveness check, not a stop gate. This prevents killing a worker mid-task.

**Edge cases + invariants:**

- Planner is 0 or 1 — never more (enforced in `_compute_desired`).
- The autoscaler manages its OWN workers only. It doesn't touch workers it didn't spawn (those may be manually started on other machines). Identity: local process + colony-registered `worker_id`.
- Rate-limit back-off: read `cooldown_until` from each worker. `_is_rate_limited(w) = w.get("cooldown_until") and parse(w["cooldown_until"]) > now`.
- Reviewer/builder role detection for existing workers uses the `capabilities` field on the worker registration (`["plan"]`, `["review"]`, or `[]`). `antfarm/core/cli.py:336-339` already auto-injects `"review"` / `"plan"` into the caps list based on `--type`, so the worker registry stays consistent without any CLI changes.
- Worker type routing: `--type {builder,reviewer,planner}` is the existing shipped flag (`antfarm/core/cli.py:304-310`). Autoscaler just consumes it.
- Child processes inherit stdout/stderr but the autoscaler captures them into logs under `.antfarm/logs/autoscaler-{name}.log`. Log rotation is out of scope — use append mode.

**Tests to write first (unit, with `subprocess.Popen` mocked):**

1. `test_compute_desired_no_ready_tasks_returns_zeros`
2. `test_compute_desired_only_plan_task_returns_one_planner`
3. `test_compute_desired_three_scope_groups_returns_three_builders`
4. `test_compute_desired_overlapping_scopes_returns_one_builder`
5. `test_compute_desired_caps_at_max_builders`
6. `test_compute_desired_caps_at_queue_depth`
7. `test_compute_desired_rate_limited_majority_doesnt_scale_up`
8. `test_compute_desired_done_unreviewed_triggers_reviewer`
9. `test_count_scope_groups_disjoint`
10. `test_count_scope_groups_overlapping`
11. `test_count_scope_groups_transitively_overlapping` — tasks A,B share x; B,C share y; A,C share nothing → 1 group
12. `test_reconcile_starts_workers_to_meet_desired` — mocked Popen
13. `test_stop_idle_worker_respects_colony_state` — don't stop busy workers
14. `test_cleanup_exited_removes_dead_workers`
15. `test_run_once_is_idempotent_when_at_desired`

---

### Phase 8 — CLI: `antfarm mission` command group

**PR 9 of sequence.**

**Goal:** `antfarm mission create|status|report|cancel|list` commands. `antfarm carry --mission`. `antfarm colony --autoscaler|--no-doctor|--no-queen`.

**Files touched:**

- `antfarm/core/cli.py`
- `tests/test_mission_cli.py` (new)

**Commands:**

```python
@main.group()
def mission():
    """Manage autonomous missions."""


@mission.command("create")
@click.option("--spec", "spec_path", required=True, type=click.Path(exists=True))
@click.option("--mission-id", default=None, help="Optional explicit mission id slug.")
@click.option("--no-plan-review", is_flag=True, default=False)
@click.option("--max-builders", type=int, default=None)
@click.option("--max-attempts", type=int, default=None)
@click.option("--integration-branch", default=None)
@COLONY_URL_OPTION
@TOKEN_OPTION
def mission_create(...): ...


@mission.command("status")
@click.argument("mission_id")
@COLONY_URL_OPTION
@TOKEN_OPTION
def mission_status(mission_id, ...): ...


@mission.command("report")
@click.argument("mission_id")
@click.option("--format", "fmt", type=click.Choice(["terminal", "md", "json"]),
              default="terminal")
@COLONY_URL_OPTION
@TOKEN_OPTION
def mission_report(mission_id, fmt, ...): ...


@mission.command("cancel")
@click.argument("mission_id")
@COLONY_URL_OPTION
@TOKEN_OPTION
def mission_cancel(mission_id, ...): ...


@mission.command("list")
@click.option("--status", default=None)
@COLONY_URL_OPTION
@TOKEN_OPTION
def mission_list(...): ...
```

Also:

- `antfarm carry --mission <mission_id>` — attach a manually-carried task to an existing mission
- `antfarm colony --autoscaler / --no-autoscaler` (default: off)
- `antfarm colony --no-queen` (default: on — Queen runs by default)
- `antfarm colony --max-builders INT` / `--max-reviewers INT` pass-through to AutoscalerConfig
- `antfarm colony --no-doctor` already shipped in v0.5.9 — confirm still works

**CLI → API mapping:**

| CLI command | HTTP call |
|------|------|
| `mission create --spec FILE` | `POST /missions` with `spec=read(FILE)`, `spec_file=FILE`, `config=MissionConfig(...)` |
| `mission status ID` | `GET /missions/{id}` → render a short overview with phase, task counts, last progress, `completion_mode`. If `completion_mode == "all_or_nothing"`, append `"(treated as best_effort in v0.6.0)"` in the status output. |
| `mission report ID` | `GET /missions/{id}/report` → `render_terminal`/`render_markdown`/`json.dumps` |
| `mission cancel ID` | `POST /missions/{id}/cancel` |
| `mission list` | `GET /missions` with optional `?status=` |

**Tests to write first (Click CliRunner against a mocked colony):**

1. `test_mission_create_reads_spec_file_and_posts`
2. `test_mission_create_no_plan_review_flag_sets_config`
3. `test_mission_create_max_builders_overrides_config`
4. `test_mission_status_formats_output`
5. `test_mission_report_terminal_format`
6. `test_mission_report_markdown_format`
7. `test_mission_report_json_format`
8. `test_mission_cancel_success`
9. `test_mission_list_filters_by_status`
10. `test_carry_with_mission_option`
11. `test_mission_status_shows_completion_mode_warning` — `mission status` output includes `completion_mode`; `all_or_nothing` renders with `"(treated as best_effort in v0.6.0)"` suffix
12. `test_colony_autoscaler_flag_parsing` — smoke: command accepts flags
13. `test_colony_no_queen_flag`

---

### Phase 9 — TUI: Mission panel

**PR 10 of sequence.**

**Goal:** `antfarm scout` (the TUI in `tui.py`) gains a Mission panel showing mission status, task progress, last progress timestamp.

**Files touched:**

- `antfarm/core/tui.py`
- `tests/test_tui.py` (extend)

**Panel contents:**

```
Missions
───────────────────────────────────────────
ID                  Status    Tasks  Progress
mission-auth-jwt    building   6/8    5m ago
mission-api-v2      complete   4/4    done
mission-migrate     blocked    2/5    stalled 45m
```

Polls `GET /missions` every 5s (same poll loop as existing panels). Renders using the `rich` library (already a dep via TUI).

**Edge cases:**

- No missions → panel shows "No active missions."
- `progress` column shows `time since mission.last_progress_at` for active missions, `done` for complete, `stalled <time>` for blocked.
- Keep the panel thin — no drill-down in v0.6.0. Drill-down via `antfarm mission status ID`.

**Tests:**

1. `test_tui_mission_panel_renders_empty`
2. `test_tui_mission_panel_renders_multi`
3. `test_tui_mission_panel_formats_progress_time`

---

### Phase 10 — End-to-end mission test

**PR 11 of sequence.**

**Goal:** One integration test covering the full loop with mocked workers (no real git, no real claude).

**File:** `tests/test_e2e_mission.py`

**Test shape:**

```python
def test_e2e_mission_full_loop(tmp_path, monkeypatch):
    """
    Full mission loop with stub workers.

    Scenario:
      1. Start colony in-process with FileBackend in tmp_path.
      2. Start Queen thread.
      3. POST /missions with a trivial spec.
      4. Simulate planner worker: claim plan task, harvest with a PlanArtifact
         containing 2 child task specs (one depends on the other).
      5. Plan review enabled → simulate reviewer harvesting with verdict=pass.
      6. Queen spawns 2 child tasks → mission.status=BUILDING.
      7. Simulate builder harvesting task-01 with a fake artifact + PR url.
      8. Simulate Soldier creating review-task-01; simulate reviewer passing it.
      9. Simulate Soldier merging task-01.
     10. Builder harvests task-02 (unblocked), review passes, merge.
     11. Assert mission.status=COMPLETE, report has both tasks merged,
         report.merged_tasks == 2, report.blocked_tasks == 0.
    """
```

This test does NOT drive subprocess Popen — it calls backend methods directly to simulate workers. Queen runs as a real thread with a compressed clock (5s base interval → 0.2s for the test).

**Additional scenarios (separate tests in the same file):**

- `test_e2e_mission_blocked_task` — one task hits max attempts → mission still completes best-effort with a blocked entry in report
- `test_e2e_mission_plan_review_rejected_triggers_replan` — first plan reviewed `needs_changes`; second plan passes; build proceeds
- `test_e2e_mission_cancel_stops_spawning` — cancel during BUILDING → new child tasks are not spawned

---

## Edge cases and invariants (v0.6.0)

This section mirrors the IMPLEMENTATION.md treatment. Every rule below is enforced in code + covered by tests.

### Mission state

| Rule | Details |
|------|---------|
| **Mission IDs are unique** | `create_mission` raises on duplicate. CLI mitigates with millisecond suffix. |
| **task_ids is append-only during an active mission** | Queen and Soldier only ever append, never remove. Removal only happens on re-plan (the old plan task stays in task_ids; a new plan task is appended; old child tasks were never created because child spawn is the Queen's decision after plan review). |
| **Re-plan invalidates prior plan_artifact** | Queen sets `plan_artifact=None, plan_task_id=None` when transitioning back to PLANNING. |
| **Terminal states are absorbing** | COMPLETE, FAILED, CANCELLED never transition again. Queen skips them at the top of `_advance`. |
| **BLOCKED is NOT terminal** | Queen checks for unblocked tasks every tick and resumes BUILDING. |
| **Mission cancel does not kickback tasks** | In-flight workers finish their current attempt. Queen stops spawning new child tasks. Soldier suppresses new review-task creation for the cancelled mission. Completed tasks remain linked and appear in the mission report under "completed before cancellation." |
| **Mission completion is best-effort by default** | `completion_mode="best_effort"` — COMPLETE fires when no tasks are in flight, regardless of blocked count. `"all_or_nothing"` is accepted and persisted but behaves identically to `best_effort` in v0.6.0 (warning logged at mission creation). Real all-or-nothing semantics land in v0.6.1+. |

### Task ↔ Mission linkage

| Rule | Details |
|------|---------|
| **Every task created under a mission has mission_id** | Plan task, re-plan task, child tasks, review tasks, plan-review tasks. |
| **mission_id is immutable** | No `reassign_mission` method. If a task needs to switch missions, delete and recreate. |
| **Reverse linkage is consistent** | `task.mission_id == "m1"` iff `"task_id" in mission_m1.task_ids`. Doctor check verifies this. |
| **Orphan detection** | Doctor flags tasks with a `mission_id` pointing to a non-existent mission. |
| **Task-kind filtering uses `missions.is_infra_task()`** | Queen, `report.py`, `autoscaler.py`, and TUI all import `is_infra_task()` from `missions.py` to partition tasks into infra (plan/review) vs impl (builder). Do not reimplement the filter. |

### Queen idempotency

| Rule | Details |
|------|---------|
| **Every _advance_* is safe to call twice** | Creating a plan task twice is prevented by `plan_task_id` check. Spawning children twice is prevented by deterministic child IDs + carry idempotency. |
| **Queen never holds transient in-memory state** | All mission progress lives in the backend. Restart = fresh read, no replay. |
| **Queen does not write tasks directly** | Queen uses `link_task_to_mission()` from `missions.py` (introduced in Phase 1). The helper owns atomicity — callers never touch backend locks. |

### Autoscaler

| Rule | Details |
|------|---------|
| **Autoscaler manages only its own workers** | Tracked by local `ManagedWorker` dict. Doesn't touch unmanaged workers. |
| **Stop decisions use colony state, not local process** | Only terminates a worker whose colony `status=="idle"`. |
| **Rate-limit back-off is majority-based** | If >50% of active builders are in cooldown, don't scale up. |
| **Planner is 0 or 1** | Enforced in `_compute_desired`. |
| **Scope group cap** | Builders are capped by non-overlapping scope group count — scheduler already prevents two workers from picking overlapping tasks. |
| **Exited processes are cleaned up every tick** | `_cleanup_exited` runs at the start of every `_reconcile`. |
| **Shutdown terminates managed workers** | `Autoscaler.stop()` issues `terminate()` to each; waits up to 10s; escalates to `kill()`. |

### Plan review

| Rule | Details |
|------|---------|
| **Max 1 re-plan** | `re_plan_count` gate. |
| **Plan review verdict comes from the existing [REVIEW_VERDICT] protocol** | Reused from v0.5 reviewer. |
| **Plan review task has `capabilities_required=["review"]` + priority 1** | Preempts impl reviews. |
| **Plan-review content** | The review task spec embeds the full `proposed_tasks` JSON, the mission spec summary, and the review checklist from SPEC_v06.md §4. |

### Backend integrity

| Rule | Details |
|------|---------|
| **`.antfarm/missions/` is runtime state** | Not committed. Added to `.gitignore` (already covered by `.antfarm/`). |
| **Mission JSON corruption** | Doctor adds a check: malformed mission JSON → report finding (no auto-fix). |
| **Backend _lock covers mission CRUD** | Same lock that protects task folder moves. |

---

## First 10 Tests (write these before any code)

| # | Test | Module | What it proves |
|---|------|--------|----------------|
| 1 | `test_extract_verdict_from_review_verdict_field` | review_pack (Phase 0) | The extracted public helper reads `attempt.review_verdict` correctly — Phase 0 regression gate |
| 2 | `test_soldier_still_imports_and_uses_helper` | soldier (Phase 0) | Existing soldier verdict-extraction behavior is preserved after the move — refactor is truly a no-op |
| 3 | `test_mission_roundtrip` | missions model | `Mission.to_dict()/from_dict()` preserves every nested field |
| 4 | `test_mission_config_accepts_all_or_nothing` | missions model | Stub-accepts the value; invalid enums rejected |
| 5 | `test_create_mission_writes_file` | file backend | `.antfarm/missions/{id}.json` created |
| 6 | `test_create_mission_rejects_github_backend` | serve (NB-3) | Colony bound to GitHubBackend returns 400 with the canonical `_GITHUB_BACKEND_MSG`; no half-created missions |
| 7 | `test_queen_planning_creates_plan_task` | queen | Planning phase spawns a `capabilities_required=["plan"]` task with mission_id linkage |
| 8 | `test_queen_planning_invalid_artifact_defers_to_kickback` | queen (NB-6 split) | Malformed plan artifact does NOT consume `re_plan_count`; Queen no-ops and waits for Soldier kickback |
| 9 | `test_queen_review_verdict_needs_changes_twice_fails_with_review_prefix` | queen (NB-4) | Valid-but-rejected plan twice → FAILED, reason starts with `"review: "` (distinct from `"system: "`) |
| 10 | `test_queen_review_task_blocked_fails_with_system_prefix` | queen (NB-4) | Reviewer worker keeps crashing (review task blocked) → FAILED, reason starts with `"system: "` |

---

## Recommended PR sequence

Each PR leaves `main` healthy, green, and mergeable. PRs are ordered to minimize merge conflicts and allow parallel review.

| PR | Branch | Scope |
|----|--------|-------|
| 1 | `refactor/extract-verdict-helper` | **Phase 0** — Move `_extract_verdict_from_review_task` from `soldier.py:738` to `review_pack.py` as a public function. Pure refactor, zero behavior change. Unblocks Queen + Soldier sharing one code path from day one. |
| 2 | `feat/mission-model` | Phase 1 — missions module, `Task.mission_id`, backend CRUD, `link_task_to_mission()` helper (incl. GitHubBackend stubs) + tests |
| 3 | `feat/mission-api` | Phase 2 — `/missions*` endpoints, colony client, carry `mission_id`, GitHubBackend preflight |
| 4 | `feat/queen-controller` | Phase 3 — Queen daemon thread, lifecycle phases, stall detection, wired to `serve.py`, imports the Phase 0 helper |
| 5 | `feat/planner-mission-mode` | Phase 4 — `WorkerRuntime._process_plan_output` mission branch, `TaskArtifact.plan_artifact` |
| 6 | `feat/soldier-mission-id` | Phase 5 — Soldier propagates `mission_id` to review tasks (uses `link_task_to_mission` from Phase 1) |
| 7 | `feat/mission-report` | Phase 6 — `report.py` (dependency-free), Queen wiring, renderers, prefix-aware formatting |
| 8 | `feat/autoscaler` | Phase 7 — single-host autoscaler, `--autoscaler` flag |
| 9 | `feat/mission-cli` | Phase 8 — `antfarm mission *` commands, `--mission` on carry |
| 10 | `feat/mission-tui-panel` | Phase 9 — TUI Mission panel |
| 11 | `test/e2e-mission` | Phase 10 — end-to-end tests |

**Dependencies between PRs:**

- PR1 (Phase 0) is a standalone refactor — no dependencies. Merges first so PR4 (Queen) can import the public helper without any circular-dep dance.
- PR3 depends on PR2
- PR4 depends on PR1, PR3 (Queen imports `review_pack.extract_verdict_from_review_task` + uses colony client + backend CRUD)
- PR5 can run in parallel with PR4 (independent module)
- PR6 depends on PR2 (needs `mission_id` on tasks) but NOT on PR4
- PR7 depends on PR2, PR4 (Queen calls the report builder)
- PR8 depends on PR2, PR3 (reads tasks + workers via backend; no Queen dependency)
- PR9 depends on PR3, PR8 (CLI exercises API + autoscaler flags)
- PR10 depends on PR3
- PR11 depends on everything

**Parallelism:** PR1 merges first (isolated refactor). After PR2 and PR3 land, PRs 4, 5, 6, 8 can progress concurrently in worktrees.

---

## Resolved Decisions (locked)

These were open questions in earlier drafts. They are now resolved by the user and are load-bearing in the relevant phases above. **Open Questions is empty.**

### Prerequisite / environment

- **v0.5.9 is on `main` (tip `2f83d1a`).** `FileBackend.kickback(self, task_id, reason, max_attempts=3)` and `_BackendAdapter.kickback(task_id, reason, max_attempts=max_attempts)` signatures match. No rebase/repair work. Verified by reading `antfarm/core/backends/file.py:345` and `antfarm/core/soldier.py:842` on `main`.
- This plan branches off `origin/main` on the `docs/v06-plan` doc branch. No preliminary fix-up commits required.

### Daemon defaults

- **Queen runs ON by default.** `--no-queen` exists purely as a debug/test escape hatch, documented as "for tests, not production" in CLI help.
- **Autoscaler is OFF by default**, opt-in via `--autoscaler` (spec `SPEC_v06.md:701-703`). `AutoscalerConfig.enabled = False`.

### `completion_mode`

- **`completion_mode="all_or_nothing"` is a stub in v0.6.0.** `MissionConfig` accepts and persists both values; the `POST /missions` handler logs a warning when `all_or_nothing` is requested; Queen's completion logic always applies best-effort rules. Real semantics land in v0.6.1+. Tests: `test_mission_config_accepts_all_or_nothing`, `test_create_mission_all_or_nothing_warns`, `test_queen_all_or_nothing_treated_as_best_effort`.

### Worker type flag

- **`worker start --type {builder,reviewer,planner}` already exists** (`antfarm/core/cli.py:304-310`, shipped in v0.5.x, verified against `main`). Autoscaler consumes the existing flag. No CLI additions in Phase 7.

### Planner failure recovery (3-way split)

- *Invalid/malformed plan artifact* on a harvested plan task → Queen defers to the normal Soldier kickback loop, which counts against `FileBackend.max_attempts` (default 3). `re_plan_count` is untouched.
- *Plan task hits `status=blocked`* (retry ceiling exhausted) → mission → FAILED with reason `"system: ..."`. Still no `re_plan_count` consumption.
- *Reviewer returns NEEDS_CHANGES on a valid plan* → consume `re_plan_count` (max 1 per spec `SPEC_v06.md:523`). Second NEEDS_CHANGES → mission FAILED with reason `"review: ..."`.

### NB-1 · `_extract_verdict_from_review_task` home

**Moved to `review_pack.py` as the public function `extract_verdict_from_review_task`.** Phase 0 (PR1) is a dedicated refactor PR that performs this move and updates the sole caller in `soldier.py:249`. Rationale: `review_pack.py` is already the review-domain module (no circular import risk — it imports only `antfarm.core.models`); don't duplicate (drift risk); don't create a one-function `verdict.py` (YAGNI).

### NB-2 · `report.py` dependencies

**Dependency-free. Stdlib only (`textwrap`, `json`, `pathlib`).** Morning digest must run headless in CI/cron. `rich` stays isolated to `tui.py`.

Forward-compat hook locked in the code sketch: `render_terminal(report, use_rich: bool = False)`. In v0.6.0, `use_rich=True` raises `NotImplementedError` — a later version can lazy-import `rich` inside the method without a dependency bump or breaking any headless caller. A dedicated test (`test_render_terminal_no_rich_import`) asserts `rich` is not in `sys.modules` after importing `antfarm.core.report`.

### NB-3 · `GitHubBackend` mission stubs

Mission methods on `GitHubBackend` raise a **specific actionable error**, not a bare `NotImplementedError`:

```python
_GITHUB_BACKEND_MSG = (
    "Mission mode requires FileBackend in v0.6.0. "
    "Use --backend file or wait for v0.6.1."
)
```

Plus a **preflight check** in the `POST /missions` HTTP handler (and any in-process `colony.create_mission()` path) that short-circuits with the SAME message when `isinstance(backend, GitHubBackend)` — so users don't hit the stub mid-Queen-loop after a half-created mission. Queen's `_start_queen_thread` also detects the backend type at thread-start and does not start the thread at all (log a single info line). Tests: `test_create_mission_rejects_github_backend`, `test_github_backend_create_mission_raises_friendly_error`.

### NB-4 · Plan-review task `blocked` vs reviewer verdict

Two distinct paths in `_advance_reviewing_plan`, both required:

| Condition | Action | Failure reason prefix |
|-----------|--------|----------------------|
| Review task → `ready` (doctor recovered a stuck active reviewer) | No-op, poll next tick | n/a |
| Review task → `blocked` (max_attempts exhausted, reviewer keeps crashing) | Mission → FAILED | `"system: "` |
| Review task → `done` with verdict `pass` | Spawn children, → BUILDING | n/a |
| Review task → `done` with verdict `needs_changes` (1st) | Create re-plan, → PLANNING | n/a |
| Review task → `done` with verdict `needs_changes` (2nd) | Mission → FAILED | `"review: "` |
| Review task → `done` with verdict `blocked` | Mission → FAILED | `"review: "` |

**Prefix convention enforced in code**, no `MissionFailureReason` enum (YAGNI). The Phase 6 report formatter MUST visually distinguish `system:` vs `review:` entries — exact formatting is the Phase 6 implementer's decision but the distinction is load-bearing (see `test_render_terminal_distinguishes_system_vs_review_prefix` and `test_render_markdown_distinguishes_system_vs_review_prefix`). Tests: `test_queen_review_task_ready_is_noop`, `test_queen_review_task_blocked_fails_with_system_prefix`, `test_queen_review_verdict_needs_changes_twice_fails_with_review_prefix`, `test_queen_review_verdict_blocked_fails_with_review_prefix`, `test_queen_planning_plan_task_blocked_fails_mission` (system prefix).

### NB-5 · API stability commitment

**Once v0.6.0 ships, the `/missions` HTTP schema is frozen for the v0.6.x line.** Specifically stable (v0.6.2 plugin spec wraps these):

- `POST /missions` request schema
- `GET /missions` response schema
- `GET /missions/{id}` response schema
- `GET /missions/{id}/report` response schema
- Every field name in `MissionReport` and its nested `MissionReportTask` / `MissionReportBlocked` types

**Stability rules:**
- Adding new optional fields: OK
- Renaming existing fields: FORBIDDEN in v0.6.x
- Removing existing fields: FORBIDDEN in v0.6.x
- Changing field semantics (e.g., repurposing `status` values): FORBIDDEN in v0.6.x

Rationale: `SPEC_v06_plugin.md` (v0.6.2) describes an MCP server and slash commands that wrap these endpoints. Any churn breaks the plugin. No code changes for v0.6.0 — this is a commitment documented in the plan and enforced in review.

A companion entry goes in the "Spec follow-ups" section below.

---

## Spec follow-ups (DO NOT edit spec in this plan; track for later)

These are notes for whoever next revises `docs/SPEC_v06.md`. The plan does not edit the spec; it only tracks the TODOs.

1. **Scope limitations section** (NB-3): `SPEC_v06.md` should explicitly state "Mission mode requires FileBackend in v0.6.0. GitHubBackend mission support is deferred to v0.6.1+." Currently the spec is silent on backend compatibility.
2. **API stability section** (NB-5): `SPEC_v06.md` should add an "API stability" subsection (either under "v0.6.0 — Autonomous Runs" or as a top-level heading) stating that `/missions*` and `MissionReport` field names are frozen for v0.6.x, with the add/rename/remove rules listed above.
3. **Failure-reason prefixes** (NB-4): `SPEC_v06.md` could mention the `"system: "` vs `"review: "` convention in the MissionReport section. Not strictly necessary — the convention lives in the implementation plan — but would help external readers understand digest output.

## Open Questions

**None.** All design decisions are resolved. Ready for Phase 0 start.

---

## What this plan does NOT cover

- Multi-node autoscaling (node-agent HTTP API) — v0.6.1
- Mission context prompt cache sharing — v0.6.1
- GitHub issue sync — v0.6.1
- MCP server / plugin UX — v0.6.2 (see `SPEC_v06_plugin.md`)
- Soldier AI-assisted conflict resolution
- Mission-level budget / cost tracking
- Recursive missions (mission spawning sub-missions)

These are explicit non-goals for v0.6.0. If any show up in engineer planning sessions, flag them back to the user rather than absorbing them.
