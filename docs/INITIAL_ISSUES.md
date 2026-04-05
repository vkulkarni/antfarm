# Antfarm v0.1 — Initial GitHub Issues

Issue list derived from `docs/IMPLEMENTATION.md` (frozen) and the build sequence in `docs/DEVELOPMENT.md`.

**GitHub project:** [antfarm](https://github.com/users/vkulkarni/projects/2)

---

## Issue 1: Repo bootstrap

**GitHub:** [#1](https://github.com/vkulkarni/antfarm/issues/1)
**Title:** `chore: repo bootstrap (pyproject.toml, .gitignore, ruff config)`

**Goal:** Establish the Python package skeleton so all subsequent PRs can import, lint, and test.

**Scope:**
- `pyproject.toml` with project metadata, dependencies, ruff config, and `[project.scripts]` entry point
- `.gitignore` for Python, `.antfarm/`, IDE files
- `LICENSE` (MIT)
- Package dirs: `antfarm/core/__init__.py`, `antfarm/adapters/__init__.py`, `antfarm/__main__.py`
- Empty `tests/conftest.py`

**Non-goals:**
- No application code
- No CI pipeline (separate issue)

**Acceptance criteria:**
- [ ] `pip install -e ".[dev]"` succeeds
- [ ] `ruff check .` passes
- [ ] `pytest tests/ -x -q` passes (0 tests collected is fine)
- [ ] `python -m antfarm` doesn't crash (may print usage or nothing)

---

## Issue 2: Add project documentation

**GitHub:** [#2](https://github.com/vkulkarni/antfarm/issues/2)
**Title:** `docs: add SPEC.md, IMPLEMENTATION.md, DEVELOPMENT.md, README.md`

**Goal:** Commit the frozen spec and development docs so they're available to all contributors.

**Scope:**
- `docs/SPEC.md` — product and architecture spec (frozen)
- `docs/IMPLEMENTATION.md` — v0.1 build plan
- `docs/DEVELOPMENT.md` — dev workflow, branching, CI, release
- `README.md` — project overview with quick start placeholder
- `CLAUDE.md`, `AGENTS.md` — AI agent context files

**Non-goals:**
- No code changes
- README quick start is a placeholder — finalized in Issue 18

**Acceptance criteria:**
- [ ] All docs committed and readable
- [ ] No broken internal links

---

## Issue 3: GitHub Actions CI workflow

**GitHub:** [#3](https://github.com/vkulkarni/antfarm/issues/3)
**Title:** `chore(ci): add GitHub Actions CI workflow for lint + test`

**Goal:** Enforce lint and test guardrails on every push and PR from the start.

**Scope:**
- `.github/workflows/ci.yml`
- Runs on push to `main` and all PRs to `main`
- Steps: checkout, setup Python 3.12, `pip install -e ".[dev]"`, `ruff check .`, `pytest tests/ -x -q`

**Non-goals:**
- No multi-version matrix (3.12 only for v0.1)
- No PyPI publishing
- No coverage reporting

**Acceptance criteria:**
- [ ] CI passes on `main` after merge
- [ ] PRs show check status

---

## Issue 4: Task, Attempt, Worker, Node dataclasses and enums

**GitHub:** [#4](https://github.com/vkulkarni/antfarm/issues/4)
**Title:** `feat(models): add Task, Attempt, Worker, Node dataclasses and enums`

**Goal:** Define all v0.1 data shapes with JSON round-tripping. No behavior, just data.

**Scope:**
- `antfarm/core/models.py`
- Enums: `TaskStatus` (ready/active/done), `AttemptStatus` (active/done/merged/superseded), `WorkerStatus` (idle/active/offline)
- Dataclasses: `Task`, `Attempt`, `Worker`, `Node`, `TrailEntry`, `SignalEntry`
- `to_dict()` / `from_dict()` class methods on each dataclass
- `tests/test_models.py`

**Non-goals:**
- No validation logic beyond type correctness
- No `MERGED` task status — merge state lives on the attempt
- No `blocked` or `paused` statuses (v0.2)

**Acceptance criteria:**
- [ ] All dataclasses serialize to JSON and back without data loss
- [ ] Enum values serialize as plain strings
- [ ] `test_task_roundtrip` passes (Implementation Plan "First 10 Tests" #1)
- [ ] `ruff check .` and `pytest tests/ -x -q` pass

---

## Issue 5: TaskBackend ABC and FileBackend skeleton

**GitHub:** [#5](https://github.com/vkulkarni/antfarm/issues/5)
**Title:** `feat(backend): add TaskBackend ABC, FileBackend skeleton, and basic task ops`

**Goal:** Define the backend interface and implement the FileBackend skeleton with basic task operations.

**Scope:**
- `antfarm/core/backends/base.py` — `TaskBackend` ABC with all explicit mutation method signatures
- `antfarm/core/backends/file.py` — `FileBackend` class init + directory layout creation
- `antfarm/core/backends/__init__.py` — `get_backend()` factory
- Directory layout: `tasks/ready/`, `tasks/active/`, `tasks/done/`, `workers/`, `nodes/`, `guards/`, `config.json`
- Implement: `carry()`, `get_task()`, `list_tasks()`

**Non-goals:**
- No `pull()`, attempt creation, `mark_harvested()`, `kickback()`, `mark_merged()` (Issue 6)
- No guards, worker/node registration, or `status()` (Issue 6)
- No scheduler logic
- No Redis backend (v0.2)
- No generic `update(**fields)` method

**Acceptance criteria:**
- [ ] `TaskBackend` ABC defines all method signatures from the spec
- [ ] `FileBackend.__init__()` creates the `.antfarm/` directory layout
- [ ] `carry()` writes task JSON to `ready/`
- [ ] `get_task()` reads task by ID from any status folder
- [ ] `list_tasks()` returns tasks, optionally filtered by status
- [ ] `ruff check .` and `pytest tests/ -x -q` pass

---

## Issue 6: FileBackend lifecycle ops, guards, and worker/node registration

**GitHub:** [#18](https://github.com/vkulkarni/antfarm/issues/18)
**Title:** `feat(backend): add FileBackend lifecycle ops, guards, and worker/node registration`

**Goal:** Complete the FileBackend with task lifecycle mutations, guards, and worker/node management.

**Scope:**
- `pull()` — atomic `os.rename()` from `ready/` to `active/`, creates new Attempt
- `mark_harvested()` — move `active/` to `done/`, set task DONE, attempt DONE
- `kickback()` — move `done/` to `ready/`, supersede attempt, add failure trail, clear `current_attempt`
- `mark_merged()` — update attempt status to MERGED in `done/` (task stays DONE)
- `append_trail()` / `append_signal()` — read-modify-write on task JSON
- `guard()` — exclusive file create via `os.open(O_CREAT | O_EXCL)`
- `release_guard()` — `os.unlink()`, owner validation
- `register_node()`, `register_worker()`, `deregister_worker()`, `heartbeat()`
- `status()` — summary counts

**Non-goals:**
- No scheduler logic in `pull()` yet (wired in scheduler issue)
- No stale detection logic (that's doctor)

**Acceptance criteria:**
- [ ] `pull()` atomically moves task and creates an Attempt
- [ ] `mark_harvested()` moves task to `done/`
- [ ] `kickback()` moves task to `ready/`, supersedes attempt, adds trail
- [ ] `mark_merged()` updates attempt in `done/`
- [ ] `append_trail()` / `append_signal()` append without clobbering
- [ ] `guard()` / `release_guard()` work with exclusive file create
- [ ] Worker/node registration and heartbeat work
- [ ] `ruff check .` and `pytest tests/ -x -q` pass

---

## Issue 7: FileBackend unit tests

**GitHub:** [#6](https://github.com/vkulkarni/antfarm/issues/6)
**Title:** `test(backend): add FileBackend unit tests`

**Goal:** Full test coverage for FileBackend, including concurrency and edge cases.

**Scope:**
- `tests/test_file_backend.py`
- Tests from Implementation Plan Phase 1c:
  - `test_carry_creates_file`
  - `test_pull_moves_to_active`
  - `test_pull_creates_attempt`
  - `test_pull_returns_none_when_empty`
  - `test_pull_is_atomic` (concurrent pulls)
  - `test_mark_harvested`
  - `test_kickback`
  - `test_guard_release`
  - `test_stale_guard_recovery`
- Edge cases (backend invariants, not API behavior):
  - Duplicate task ID on carry → reject
  - Idempotent harvest
  - Harvest from non-current attempt → reject

**Non-goals:**
- No integration tests with the API server
- No scheduler tests

**Acceptance criteria:**
- [ ] All listed tests pass
- [ ] Concurrent pull test proves atomicity
- [ ] `ruff check .` and `pytest tests/ -x -q` pass

---

## Issue 8: Scope-aware task scheduler

**GitHub:** [#7](https://github.com/vkulkarni/antfarm/issues/7)
**Title:** `feat(scheduler): add scope-aware task scheduler`

**Goal:** Implement the v0.1 scheduling policy and wire it into FileBackend's `pull()`.

**Scope:**
- `antfarm/core/scheduler.py` — `select_task()` function
- Policy order: dependency check → scope preference → priority → FIFO
- Wire `select_task()` into `FileBackend.pull()`
- `tests/test_scheduler.py`

**Non-goals:**
- No machine affinity or capability-aware scheduling (v0.2)
- No deadline awareness
- Scope overlap is a soft preference, not a hard block

**Acceptance criteria:**
- [ ] `test_dependency_blocks` — unmet deps skipped
- [ ] `test_dependency_allows` — met deps eligible
- [ ] `test_scope_prefers_non_overlapping` — non-overlapping preferred
- [ ] `test_scope_allows_overlap_when_no_alternative` — falls back to overlapping
- [ ] `test_priority_ordering` — lower number wins
- [ ] `test_fifo_among_equals` — oldest first
- [ ] `test_empty_queue` — returns None
- [ ] `ruff check .` and `pytest tests/ -x -q` pass

---

## Issue 9: Colony API server

**GitHub:** [#8](https://github.com/vkulkarni/antfarm/issues/8)
**Title:** `feat(server): add colony API server (FastAPI)`

**Goal:** HTTP interface for all colony operations. Single-process, `threading.Lock()` on mutations.

**Scope:**
- `antfarm/core/serve.py`
- All v0.1 endpoints:
  - Nodes: `POST /nodes`
  - Workers: `POST /workers/register`, `POST /workers/{id}/heartbeat`, `DELETE /workers/{id}`
  - Tasks: `POST /tasks`, `POST /tasks/pull`, `GET /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/trail`, `POST /tasks/{id}/signal`, `POST /tasks/{id}/harvest`, `POST /tasks/{id}/kickback`, `POST /tasks/{id}/merge`
  - Guards: `POST /guards/{resource}`, `DELETE /guards/{resource}`
  - Status: `GET /status`
- `threading.Lock()` on `pull`, `trail`, `signal`, `guard`
- `tests/test_serve.py`

**Non-goals:**
- No authentication (v0.2)
- No SSE or WebSocket (v0.3)
- No background scheduler — runs on-demand in `pull`

**Acceptance criteria:**
- [ ] `test_carry_and_list`, `test_forage_returns_task`, `test_forage_empty_returns_204`
- [ ] `test_forage_creates_attempt`, `test_trail_appends`, `test_harvest_transitions`
- [ ] `test_heartbeat_updates_worker`, `test_status_returns_summary`
- [ ] Concurrent `pull` cannot claim the same task
- [ ] Concurrent `trail`/`signal` appends do not lose writes
- [ ] Concurrent `guard` cannot double-acquire the same resource
- [ ] `ruff check .` and `pytest tests/ -x -q` pass

---

## Issue 10: Git worktree manager

**GitHub:** [#9](https://github.com/vkulkarni/antfarm/issues/9)
**Title:** `feat(workspace): add git worktree manager`

**Goal:** Create, validate, and list git worktrees for task attempts.

**Scope:**
- `antfarm/core/workspace.py` — `WorkspaceManager` class
- `create(task_id, attempt_id)` — creates worktree with branch `feat/<task_id>-<attempt_id>`
- `validate(workspace_path)` — checks valid worktree + clean state
- `list_orphans()` — worktrees without active workers
- `tests/test_workspace.py`

**Non-goals:**
- No auto-cleanup of worktrees (v0.1 leaves them for debugging)
- No shallow clone support

**Acceptance criteria:**
- [ ] `test_create_worktree` — correct path and branch
- [ ] `test_validate_clean` — passes on valid worktree
- [ ] `test_validate_dirty` — fails on uncommitted changes
- [ ] `test_list_orphans` — orphan detection works
- [ ] `ruff check .` and `pytest tests/ -x -q` pass

---

## Issue 11: Worker runtime lifecycle

**GitHub:** [#10](https://github.com/vkulkarni/antfarm/issues/10)
**Title:** `feat(worker): add worker runtime lifecycle`

**Goal:** Implement the `worker start` loop: register → forage → workspace → launch agent → harvest → repeat.

**Scope:**
- `antfarm/core/worker.py` — `WorkerRuntime` class + `ColonyClient` (thin httpx wrapper)
- Lifecycle: register → forage → create workspace → launch agent subprocess → harvest → loop or exit
- Background heartbeat thread (every 30s)
- Agent launch by type: claude-code, codex, aider, generic
- Ownership-loss handling: if harvest is rejected (attempt no longer current), worker exits cleanly
- `tests/test_worker.py`

**Non-goals:**
- No agent-internal logic — worker launches a subprocess and waits
- No thinking signal handling (v0.2)

**Acceptance criteria:**
- [ ] `test_register_sends_payload`
- [ ] `test_forage_returns_task_spec`
- [ ] `test_harvest_marks_done`
- [ ] `test_lifecycle_loop` — processes multiple tasks, exits on empty queue
- [ ] `test_exit_deregisters` — cleanup on normal and error exit
- [ ] `test_ownership_loss` — harvest rejected for non-current attempt, worker exits cleanly
- [ ] `ruff check .` and `pytest tests/ -x -q` pass

---

## Issue 12: Doctor pre-flight checks and stale recovery

**GitHub:** [#11](https://github.com/vkulkarni/antfarm/issues/11)
**Title:** `feat(doctor): add pre-flight checks and stale recovery`

**Goal:** `antfarm doctor` detects problems and optionally fixes them with `--fix`.

**Scope:**
- `antfarm/core/doctor.py` — `run_doctor()` + individual check functions
- `Finding` dataclass with severity, check name, message, auto_fixable, fixed
- Checks:
  - filesystem (`.antfarm/` missing, dirs not writable)
  - colony reachable
  - git config (git missing, not a repo, no integration branch)
  - stale workers (heartbeat > TTL)
  - stale tasks (active task with no live worker)
  - stale guards (mtime > TTL)
  - workspace conflicts (two workers same dir)
  - orphan workspaces (worktrees with no active task/worker)
  - dependency cycles (report-only)
  - dangling dependency references (report-only)
  - task state/folder mismatch (e.g., task in `ready/` but status says `done`)
  - malformed `current_attempt` references (pointing to non-existent attempt)
  - malformed task JSON (corrupt file from crash)
- Dry-run (default) vs `--fix` mode
- `tests/test_doctor.py`

**Non-goals:**
- No auto-delete of orphan worktrees (report only in v0.1)
- No auto-fix for cycles or dangling deps (report only)

**Acceptance criteria:**
- [ ] `test_stale_worker_detected` and `test_stale_worker_fixed`
- [ ] `test_stale_task_recovery` and `test_stale_task_fixed`
- [ ] `test_stale_guard_cleanup`
- [ ] `test_workspace_conflict_detected`
- [ ] `test_orphan_workspace_reported`
- [ ] `test_dependency_cycle_detected`
- [ ] `test_state_folder_mismatch_detected`
- [ ] `test_malformed_attempt_ref_detected`
- [ ] Dry-run reports but doesn't change state
- [ ] `ruff check .` and `pytest tests/ -x -q` pass

---

## Issue 13: Soldier merge queue and integration engine

**GitHub:** [#12](https://github.com/vkulkarni/antfarm/issues/12)
**Title:** `feat(soldier): add merge queue and integration engine`

**Goal:** Deterministic merge gate: temp branch → test → fast-forward or kickback.

**Scope:**
- `antfarm/core/soldier.py` — `Soldier` class + `MergeResult` enum
- `get_merge_queue()` — filters done tasks by deps-merged + has-PR, orders by dependency → priority → FIFO
- `attempt_merge()` — create temp branch, merge PR branch, run tests, FF or kickback
- Hard policy: no auto-fix, no AI, any conflict or test failure = immediate kickback
- Repo cleanup after failed merge (temp branch deleted, working tree clean)
- `tests/test_soldier.py`

**Non-goals:**
- No AI-assisted conflict resolution (v0.2)
- No trivial conflict auto-fix (v0.2)
- Soldier never modifies worker code

**Acceptance criteria:**
- [ ] `test_merge_queue_respects_deps`
- [ ] `test_merge_green_fast_forwards`
- [ ] `test_merge_conflict_kicks_back`
- [ ] `test_test_failure_kicks_back`
- [ ] `test_kickback_supersedes_attempt`
- [ ] `test_independent_tasks_not_blocked`
- [ ] `test_only_current_attempt_merged`
- [ ] Temp branch is always cleaned up after merge attempt
- [ ] Working tree is clean after failed merge attempt
- [ ] `ruff check .` and `pytest tests/ -x -q` pass

---

## Issue 14: End-to-end integration test

**GitHub:** [#13](https://github.com/vkulkarni/antfarm/issues/13)
**Title:** `test(e2e): add end-to-end integration test`

**Goal:** Prove the full loop works in a single test before polishing adapters.

**Scope:**
- `tests/test_e2e.py` — `test_e2e_full_loop`
- Flow: start colony (in-process) → register node → carry 2 tasks (with dependency) → register worker → forage task-001 → trail → harvest → soldier merges → forage task-002 (unblocked) → harvest → soldier merges → verify all merged, doctor clean

**Non-goals:**
- No real git operations (mock workspace/soldier git calls)
- No real agent subprocess launch
- No network — in-process colony with FileBackend in tmp_path

**Acceptance criteria:**
- [ ] `test_e2e_full_loop` passes
- [ ] All task statuses correct at end (both merged)
- [ ] Doctor finds no issues on final state
- [ ] `ruff check .` and `pytest tests/ -x -q` pass

---

## Issue 15: CLI — wire up all v0.1 commands

**GitHub:** [#14](https://github.com/vkulkarni/antfarm/issues/14)
**Title:** `feat(cli): wire up all v0.1 CLI commands`

**Goal:** All v0.1 commands usable from the terminal via `antfarm <command>`.

**Scope:**
- `antfarm/core/cli.py` using Click
- Core commands: `colony`, `join`, `worker start`, `carry`, `scout`, `doctor`
- Low-level commands: `hatch`, `forage`, `trail`, `harvest`, `guard`, `release`, `signal`
- `carry --file` for loading task from JSON file
- `tests/test_cli.py` — smoke tests using Click's `CliRunner`

**Non-goals:**
- No TUI dashboard (v0.3)
- No `scent`, `pause`, `resume`, `reassign`, `deploy` (v0.2+)

**Acceptance criteria:**
- [ ] `test_cli_colony_starts`
- [ ] `test_cli_carry_creates_task`
- [ ] `test_cli_scout_shows_status`
- [ ] `test_cli_doctor_runs_checks` and `test_cli_doctor_fix`
- [ ] All commands print help with `--help`
- [ ] `ruff check .` and `pytest tests/ -x -q` pass

---

## Issue 16: Claude Code reference adapter

**GitHub:** [#15](https://github.com/vkulkarni/antfarm/issues/15)
**Title:** `feat(adapter): add Claude Code reference adapter`

**Goal:** Ship agent definitions and hooks for Claude Code integration.

**Scope:**
- `antfarm/adapters/claude_code/agents/worker.md` — worker agent system prompt
- `antfarm/adapters/claude_code/agents/soldier.md` — soldier agent definition (placeholder only, not used by v0.1 runtime)
- `antfarm/adapters/claude_code/agents/queen.md` — example task decomposition workflow
- `antfarm/adapters/claude_code/hooks/heartbeat.sh` — PostToolUse heartbeat hook
- `antfarm/adapters/claude_code/hooks/pre_forage.sh` — git sync before work
- `antfarm/adapters/claude_code/setup.sh` — symlinks agents + configures hooks

**Non-goals:**
- No Codex, Aider, or Cursor adapters (v0.4+)
- Queen is an example, not core platform

**Acceptance criteria:**
- [ ] Agent definitions are valid markdown
- [ ] Hooks are executable and have `|| true` + timeout guards
- [ ] `setup.sh` works on macOS and Linux
- [ ] `ruff check .` passes (no Python in this PR is fine)

---

## Issue 17: Generic curl adapter

**GitHub:** [#16](https://github.com/vkulkarni/antfarm/issues/16)
**Title:** `feat(adapter): add generic curl adapter`

**Goal:** Copy-paste curl examples so any agent tool can integrate with Antfarm.

**Scope:**
- `antfarm/adapters/generic/README.md` — full curl examples for forage, trail, heartbeat, harvest
- `antfarm/adapters/generic/forage.sh` — standalone forage script
- `antfarm/adapters/generic/heartbeat.sh` — standalone heartbeat script

**Non-goals:**
- No agent-specific logic
- No Python code

**Acceptance criteria:**
- [ ] README has working curl examples for all 4 required adapter calls (forage, trail, heartbeat, harvest)
- [ ] Shell scripts are executable and handle errors gracefully

---

## Issue 18: Finalize README with real quick start

**GitHub:** [#17](https://github.com/vkulkarni/antfarm/issues/17)
**Title:** `docs: finalize README with real quick start`

**Goal:** Replace placeholder quick start with tested, working instructions.

**Scope:**
- Update `README.md` with:
  - Real installation instructions
  - Working quick start (single-machine demo — this is the v0.1 baseline)
  - Multi-machine setup example (documented, not necessarily CI-exercised)
  - Link to docs/SPEC.md for architecture
  - Link to adapters for agent integration

**Non-goals:**
- No new features
- No API reference docs (derive from code)

**Acceptance criteria:**
- [ ] Single-machine quick start commands work on a fresh clone
- [ ] Multi-machine example is documented (may not be exercised in CI)
- [ ] All links resolve
- [ ] `ruff check .` passes

---

## Build sequence

| Order | Issue | GitHub | Title |
|-------|-------|--------|-------|
| 1 | Issue 1 | [#1](https://github.com/vkulkarni/antfarm/issues/1) | chore: repo bootstrap |
| 2 | Issue 2 | [#2](https://github.com/vkulkarni/antfarm/issues/2) | docs: project documentation |
| 3 | Issue 3 | [#3](https://github.com/vkulkarni/antfarm/issues/3) | chore(ci): GitHub Actions CI |
| 4 | Issue 4 | [#4](https://github.com/vkulkarni/antfarm/issues/4) | feat(models): dataclasses + enums |
| 5 | Issue 5 | [#5](https://github.com/vkulkarni/antfarm/issues/5) | feat(backend): ABC + FileBackend skeleton |
| 6 | Issue 6 | [#18](https://github.com/vkulkarni/antfarm/issues/18) | feat(backend): lifecycle ops, guards, registration |
| 7 | Issue 7 | [#6](https://github.com/vkulkarni/antfarm/issues/6) | test(backend): FileBackend unit tests |
| 8 | Issue 8 | [#7](https://github.com/vkulkarni/antfarm/issues/7) | feat(scheduler): scope-aware scheduler |
| 9 | Issue 9 | [#8](https://github.com/vkulkarni/antfarm/issues/8) | feat(server): colony API server |
| 10 | Issue 10 | [#9](https://github.com/vkulkarni/antfarm/issues/9) | feat(workspace): git worktree manager |
| 11 | Issue 11 | [#10](https://github.com/vkulkarni/antfarm/issues/10) | feat(worker): worker runtime |
| 12 | Issue 12 | [#11](https://github.com/vkulkarni/antfarm/issues/11) | feat(doctor): pre-flight + recovery |
| 13 | Issue 13 | [#12](https://github.com/vkulkarni/antfarm/issues/12) | feat(soldier): merge queue |
| 14 | Issue 14 | [#13](https://github.com/vkulkarni/antfarm/issues/13) | test(e2e): end-to-end test |
| 15 | Issue 15 | [#14](https://github.com/vkulkarni/antfarm/issues/14) | feat(cli): all v0.1 commands |
| 16 | Issue 16 | [#15](https://github.com/vkulkarni/antfarm/issues/15) | feat(adapter): Claude Code |
| 17 | Issue 17 | [#16](https://github.com/vkulkarni/antfarm/issues/16) | feat(adapter): generic curl |
| 18 | Issue 18 | [#17](https://github.com/vkulkarni/antfarm/issues/17) | docs: finalize README |
