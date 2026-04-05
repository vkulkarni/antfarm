# Antfarm v0.1 — Implementation Plan

**Status:** FROZEN — approved for implementation
**Derived from:** SPEC.md v1.0 (frozen)
**Goal:** Prove one end-to-end loop works reliably:
`colony → join → carry → worker start → forage → work → trail → harvest → soldier integrates`

---

## Module Map

```
antfarm/
  core/
    __init__.py           # version, package metadata
    models.py             # dataclasses + enums: Task, Attempt, Worker, Node, TrailEntry, SignalEntry
    backends/
      base.py             # TaskBackend ABC with explicit mutation methods
      file.py             # FileBackend implementation
    scheduler.py          # scope-aware task scheduling
    serve.py              # FastAPI colony server
    worker.py             # worker start lifecycle (orchestration only)
    workspace.py          # git worktree creation, validation, orphan detection
    soldier.py            # merge queue + integration logic (deterministic, no AI)
    doctor.py             # pre-flight checks + stale recovery (dry-run and --fix modes)
    cli.py                # click CLI entry point
  adapters/
    claude_code/
      agents/
        worker.md         # Claude Code worker agent definition
        soldier.md        # Claude Code soldier agent definition
        queen.md          # EXAMPLE ONLY: AI task decomposition (not part of v0.1 core)
      hooks/
        heartbeat.sh
        pre_forage.sh
      setup.sh
    generic/
      README.md
      forage.sh
      heartbeat.sh
  __main__.py             # python -m antfarm
tests/
  conftest.py             # shared fixtures
  test_models.py
  test_file_backend.py
  test_scheduler.py
  test_serve.py
  test_worker.py
  test_workspace.py
  test_soldier.py
  test_doctor.py
  test_cli.py
  test_e2e.py             # end-to-end integration test
pyproject.toml
README.md
SPEC.md
IMPLEMENTATION.md
DEVELOPMENT.md
LICENSE
```

---

## Build Phases

### Phase 1: State & Data Layer

**Goal:** Task and worker state can be created, read, updated, and persisted.

#### 1a. `models.py`

Dataclasses + enums. No behavior, just data shapes and status constants.

```python
from dataclasses import dataclass, field
from enum import StrEnum
from datetime import datetime


class TaskStatus(StrEnum):
    READY = "ready"
    ACTIVE = "active"
    DONE = "done"
    # No MERGED status. Merge state lives on the attempt, not the task.
    # Task stays DONE after merge. Scout derives "merged" from attempt metadata.


class AttemptStatus(StrEnum):
    ACTIVE = "active"
    DONE = "done"
    MERGED = "merged"
    SUPERSEDED = "superseded"


class WorkerStatus(StrEnum):
    IDLE = "idle"
    ACTIVE = "active"
    OFFLINE = "offline"


@dataclass
class TrailEntry:
    ts: str                       # ISO 8601 timestamp
    worker_id: str                # "node-1/claude-1"
    message: str                  # "completed API routes"


@dataclass
class SignalEntry:
    ts: str
    worker_id: str
    message: str                  # "task needs re-scoping because..."


@dataclass
class Attempt:
    attempt_id: str               # "att-001"
    worker_id: str | None         # "node-1/claude-1"
    status: AttemptStatus
    branch: str | None
    pr: str | None
    started_at: str
    completed_at: str | None


@dataclass
class Task:
    id: str                       # "task-001"
    title: str
    spec: str
    complexity: str               # "S", "M", "L"
    priority: int                 # lower = higher priority (default 10)
    depends_on: list[str]         # ["task-000"]
    touches: list[str]            # ["api", "db", "auth"]
    status: TaskStatus
    current_attempt: str | None   # attempt_id
    attempts: list[Attempt]
    trail: list[TrailEntry]
    signals: list[SignalEntry]
    created_at: str
    updated_at: str
    created_by: str


@dataclass
class Worker:
    worker_id: str                # "node-1/claude-1"
    node_id: str                  # "node-1"
    agent_type: str               # "claude-code", "codex", "aider", "generic"
    workspace_root: str
    status: WorkerStatus
    registered_at: str
    last_heartbeat: str


@dataclass
class Node:
    node_id: str                  # "node-1"
    joined_at: str
    last_seen: str
```

Each dataclass has `to_dict()` / `from_dict()` class methods for JSON round-tripping. Enum values serialize as their string values.

**Priority convention:** Lower number = higher priority (like Unix nice values). Default is 10. Priority 1 is processed before priority 10.

#### 1b. `backends/base.py`

Abstract base with explicit mutation methods. No generic `update(**fields)`.

```python
from abc import ABC, abstractmethod

class TaskBackend(ABC):
    # --- Task lifecycle ---
    @abstractmethod
    def carry(self, task: dict) -> str:
        """Add a task to the queue."""
        ...

    @abstractmethod
    def pull(self, worker_id: str) -> dict | None:
        """Claim next task. Creates a new attempt. Atomic."""
        ...

    @abstractmethod
    def append_trail(self, task_id: str, entry: dict) -> None: ...

    @abstractmethod
    def append_signal(self, task_id: str, entry: dict) -> None: ...

    @abstractmethod
    def mark_harvested(self, task_id: str, attempt_id: str, pr: str, branch: str) -> None:
        """Transition task to DONE, attempt to DONE."""
        ...

    @abstractmethod
    def kickback(self, task_id: str, reason: str) -> None:
        """Transition task to READY, current attempt to SUPERSEDED.
        Sets current_attempt to None. Next pull() creates a fresh attempt."""
        ...

    @abstractmethod
    def mark_merged(self, task_id: str, attempt_id: str) -> None:
        """Mark attempt as MERGED. Task stays DONE in done/ folder.
        Merged state is tracked on the attempt, not the task status."""
        ...

    @abstractmethod
    def list_tasks(self, status: str | None = None) -> list[dict]: ...

    @abstractmethod
    def get_task(self, task_id: str) -> dict | None: ...

    # --- Guards (locks) ---
    @abstractmethod
    def guard(self, resource: str, owner: str) -> bool: ...

    @abstractmethod
    def release_guard(self, resource: str, owner: str) -> None:
        """Release guard. Only the owner can release. Raises if owner mismatch."""
        ...

    # --- Nodes ---
    @abstractmethod
    def register_node(self, node: dict) -> None: ...

    # --- Workers ---
    @abstractmethod
    def register_worker(self, worker: dict) -> None: ...

    @abstractmethod
    def deregister_worker(self, worker_id: str) -> None: ...

    @abstractmethod
    def heartbeat(self, worker_id: str, status: dict) -> None: ...

    # --- Status ---
    @abstractmethod
    def status(self) -> dict: ...
```

Why explicit methods instead of generic `update()`:
- Prevents business logic from leaking into callers
- Each method enforces valid state transitions
- Tests can assert specific transition behavior
- Contributors can't bypass the state machine

#### 1c. `backends/file.py`

FileBackend implementation.

```
.antfarm/
  tasks/ready/      → task JSON files
  tasks/active/     → claimed tasks
  tasks/done/       → completed + merged tasks (status in metadata)
  workers/          → worker presence files (JSON, checked by mtime)
  nodes/            → node registration files
  guards/           → guard files with owner + TTL
  # logs/ omitted in v0.1 — trail is stored in task JSON. Separate log files are a v0.2 addition.
  config.json       → colony config
```

Key implementation details:

- `carry()`: write JSON to `tasks/ready/{task_id}.json`
- `pull()`: call `scheduler.select_task()`, then `os.rename()` from `ready/` to `active/`. Create new Attempt. Protected by `threading.Lock()`
- `mark_harvested()`: move from `active/` to `done/`, set task status to DONE, attempt status to DONE
- `kickback()`: move from `done/` to `ready/`, set current attempt to SUPERSEDED, add failure trail entry
- `mark_merged()`: update attempt status to MERGED in `done/` task file (task stays DONE, file stays in `done/`)
- `append_trail()`: read task JSON, append TrailEntry, write back
- `append_signal()`: read task JSON, append SignalEntry, write back
- `guard()`: attempt exclusive file create via `os.open(O_CREAT | O_EXCL)`, write owner + timestamp
- `release_guard()`: `os.unlink()` the lock file
- `heartbeat()`: write/update worker file in `workers/`, update mtime
- Stale detection: compare file mtime against TTL (default 300s)

**Tests to write first:**
1. `test_carry_creates_file` — task JSON appears in `ready/`
2. `test_pull_moves_to_active` — file moves from `ready/` to `active/`
3. `test_pull_creates_attempt` — pulled task has a new attempt with ACTIVE status
4. `test_pull_returns_none_when_empty` — empty queue returns None
5. `test_pull_is_atomic` — two concurrent pulls don't return same task
6. `test_mark_harvested` — task moves to `done/`, attempt status is DONE
7. `test_kickback` — task moves to `ready/`, attempt becomes SUPERSEDED, trail has failure context
8. `test_guard_release` — guard acquired, second attempt fails, release allows reacquire
9. `test_stale_guard_recovery` — guard with expired mtime is treated as released

---

### Phase 2: Scheduler

**Goal:** `pull()` returns the right task based on dependencies, scope, priority, FIFO.

#### `scheduler.py`

```python
def select_task(
    ready_tasks: list[Task],
    done_task_ids: set[str],
    active_tasks: list[Task],
) -> Task | None:
    """Select next task using v0.1 scheduling policy.

    Policy (applied in order):
    1. Dependency check — skip if depends_on not all in done_task_ids
    2. Scope preference — prefer non-overlapping touches with active tasks
    3. Priority — lower number = higher priority
    4. FIFO — oldest created_at first among equals

    Returns None if no eligible task exists.
    """
```

**Tests:**
1. `test_dependency_blocks` — task with unmet dep is skipped
2. `test_dependency_allows` — task with all deps met is eligible
3. `test_scope_prefers_non_overlapping` — non-overlapping task chosen over overlapping
4. `test_scope_allows_overlap_when_no_alternative` — overlapping task returned if it's the only option
5. `test_priority_ordering` — priority 1 before priority 10
6. `test_fifo_among_equals` — same priority, oldest wins
7. `test_empty_queue` — returns None

---

### Phase 3: Colony API Server

**Goal:** Workers can interact with the colony over HTTP.

#### `serve.py`

FastAPI app. Single-process, `threading.Lock()` on mutations.

```python
app = FastAPI(title="Antfarm Colony")
backend: TaskBackend  # injected at startup
_lock = threading.Lock()
```

Endpoints:

| Method | Path | Operation | Atomic? | Notes |
|--------|------|-----------|---------|-------|
| POST | `/nodes` | Register node | No | Idempotent — re-registering updates `last_seen` |
| POST | `/workers/register` | Register worker | No | Returns worker_id |
| POST | `/workers/{id}/heartbeat` | Heartbeat | No | Updates `last_heartbeat` |
| DELETE | `/workers/{id}` | Deregister | No | Removes worker presence |
| POST | `/tasks` | Carry task | No | Returns task_id |
| POST | `/tasks/pull` | Forage | **Yes** (`_lock`) | Returns task + new attempt, or 204 if empty |
| POST | `/tasks/{id}/trail` | Append trail | **Yes** (`_lock`) | Read-modify-write on task JSON |
| POST | `/tasks/{id}/signal` | Append signal | **Yes** (`_lock`) | Read-modify-write on task JSON |
| POST | `/tasks/{id}/harvest` | Mark harvested | No | Accepts attempt_id, pr, branch |
| POST | `/tasks/{id}/kickback` | Kickback | No | Accepts reason string |
| POST | `/tasks/{id}/merge` | Mark merged | No | Soldier only |
| GET | `/tasks` | List tasks | No | Optional `?status=` filter |
| GET | `/tasks/{id}` | Get task detail | No | Full task with attempts, trail, signals |
| POST | `/guards/{resource}` | Guard resource | **Yes** (`_lock`) | Returns success/fail |
| DELETE | `/guards/{resource}` | Release guard | No | |
| GET | `/status` | Colony status | No | Summary: node count, worker count, task counts by status |

All mutation-critical operations (`pull`, `guard`) and read-modify-write operations (`trail`, `signal`) are protected by `_lock`. Non-critical reads are not locked.

**Tests:**
1. `test_carry_and_list` — carry a task, list returns it
2. `test_forage_returns_task` — carry then forage returns the task
3. `test_forage_empty_returns_204` — forage on empty queue returns 204
4. `test_forage_creates_attempt` — foraged task has current_attempt set
5. `test_trail_appends` — trail endpoint appends entry to task
6. `test_harvest_transitions` — harvest moves task to done
7. `test_heartbeat_updates_worker` — heartbeat updates last_heartbeat
8. `test_status_returns_summary` — status endpoint returns counts

---

### Phase 4: Workspace Manager

**Goal:** Git worktrees are created and validated per task per worker.

#### `workspace.py`

```python
class WorkspaceManager:
    def __init__(self, workspace_root: str, repo_path: str, integration_branch: str = "dev"):
        self.workspace_root = workspace_root
        self.repo_path = repo_path
        self.integration_branch = integration_branch

    def create(self, task_id: str, attempt_id: str) -> str:
        """Create a git worktree for this task attempt.

        1. git fetch origin
        2. git worktree add -b <branch> <path> origin/<integration_branch>
        3. Returns workspace path

        Branch: feat/<task_id>-<attempt_id>
        Path:   <workspace_root>/<task_id>-<attempt_id>
        """
        ...

    def validate(self, workspace_path: str) -> bool:
        """Check workspace is a valid git worktree with clean state."""
        ...

    def list_orphans(self) -> list[str]:
        """List worktrees with no active worker. Used by doctor."""
        ...

    # NOTE: No auto-cleanup in v0.1.
    # Worktrees are left for debugging. Doctor reports orphans.
```

**Tests:**
1. `test_create_worktree` — creates worktree at expected path with correct branch
2. `test_validate_clean` — valid worktree passes validation
3. `test_validate_dirty` — worktree with uncommitted changes fails validation
4. `test_list_orphans` — worktrees without active workers are listed

---

### Phase 5: Worker Runtime

**Goal:** `antfarm worker start` runs the full lifecycle.

#### `worker.py`

Orchestration only. Delegates to WorkspaceManager for git, httpx for colony API.

```python
class WorkerRuntime:
    def __init__(self, colony_url, node_id, name, agent_type, workspace_root, repo_path):
        self.worker_id = f"{node_id}/{name}"
        self.colony = ColonyClient(colony_url)  # thin httpx wrapper
        self.workspace_mgr = WorkspaceManager(workspace_root, repo_path)

    def run(self):
        """Main lifecycle loop.

        This is a convenience flow composed of lower-level operations:
        register → forage → setup workspace → launch agent → harvest → repeat
        """
        self.colony.register_worker(self.worker_id, self.node_id, self.agent_type, self.workspace_root)
        try:
            while True:
                task = self.colony.forage(self.worker_id)
                if task is None:
                    break  # queue empty, exit
                attempt_id = task["current_attempt"]
                workspace = self.workspace_mgr.create(task["id"], attempt_id)
                self.launch_agent(task, workspace)
                # agent runs to completion — hooks handle heartbeat + trail
        finally:
            self.colony.deregister_worker(self.worker_id)

    def launch_agent(self, task, workspace):
        """Launch the coding tool as a subprocess in the workspace.

        Agent type determines the command:
        - claude-code: claude --agent worker
        - codex: codex --prompt <spec>
        - aider: aider --message <spec>
        - generic: run user-specified command

        Antfarm does not assume the tool is AI-powered.
        It is a generic execution slot.
        """
        ...

    def _heartbeat_loop(self):
        """Background thread: POST heartbeat every 30s."""
        ...

# Telemetry model by adapter type:
# - Claude Code: hooks (PostToolUse) handle heartbeat + trail natively.
#   The worker runtime only needs the background heartbeat as a fallback.
# - Codex / Aider / generic: no hook support. The wrapper drives all
#   telemetry — heartbeat loop + periodic trail updates from the runtime.
# The launch_agent() method should detect adapter type and configure accordingly.
```

**Tests:**
1. `test_register_sends_payload` — registration sends correct worker JSON
2. `test_forage_returns_task_spec` — forage returns task with spec
3. `test_harvest_marks_done` — harvest transitions task to done
4. `test_lifecycle_loop` — carry 2 tasks, worker start processes both, exits on empty
5. `test_exit_deregisters` — worker deregisters on exit (normal or error)

---

### Phase 6: Doctor & Recovery

**Goal:** `antfarm doctor` detects and recovers from failure states.

#### `doctor.py`

Two modes:
- **Dry-run (default):** Report findings, change nothing
- **Fix (`--fix`):** Apply safe automatic repairs

```python
@dataclass
class Finding:
    severity: str       # "error", "warning", "info"
    check: str          # "stale_worker", "stale_task", etc.
    message: str
    auto_fixable: bool
    fixed: bool = False


def run_doctor(backend: TaskBackend, config: dict, fix: bool = False) -> list[Finding]:
    """Run all checks. If fix=True, apply safe repairs."""
    findings = []
    findings += check_filesystem(config)
    findings += check_colony_reachable(config)
    findings += check_git_config()
    findings += check_stale_workers(backend, fix=fix)
    findings += check_stale_tasks(backend, fix=fix)
    findings += check_stale_guards(backend, fix=fix)
    findings += check_workspace_conflicts(backend)
    findings += check_orphan_workspaces(config)
    return findings
```

Checks and their fix behavior:

| Check | What it detects | Dry-run | `--fix` |
|-------|----------------|---------|---------|
| `check_filesystem` | `.antfarm/` missing, dirs not writable | Report | Create dirs |
| `check_colony_reachable` | Colony API down | Report only | Report only (can't fix) |
| `check_git_config` | Git missing, not a repo, no integration branch | Report only | Report only |
| `check_stale_workers` | Worker heartbeat > TTL | Report | Deregister stale workers |
| `check_stale_tasks` | Active task with no live worker | Report | Move task to `ready/`, preserve trail |
| `check_stale_guards` | Guard file mtime > TTL | Report | Delete stale guard files |
| `check_workspace_conflicts` | Two workers same workspace dir | Report only | Report only (manual fix) |
| `check_orphan_workspaces` | Worktrees with no active task/worker | Report | Report only (v0.1: don't auto-delete) |

**Tests:**
1. `test_stale_worker_detected` — worker file older than TTL is flagged
2. `test_stale_worker_fixed` — with `--fix`, stale worker is deregistered
3. `test_stale_task_recovery` — active task with dead worker flagged
4. `test_stale_task_fixed` — with `--fix`, task moves to ready, trail preserved
5. `test_stale_guard_cleanup` — expired guard flagged and cleaned with `--fix`
6. `test_workspace_conflict_detected` — two workers with same workspace flagged
7. `test_orphan_workspace_reported` — orphan worktree reported but not deleted

---

### Phase 7: Soldier (Integration Engine)

**Goal:** Soldier merges PRs safely through temp integration branch.

The Soldier is **fully deterministic in v0.1** — no LLM, no AI. It is a script-based merge gate:
- Clean merge + green tests → merge
- Any conflict or test failure → kick back immediately
- No auto-fix attempts in v0.1

This means more kickbacks than an AI-assisted Soldier, but maximum reliability and predictability. Workers (which may be AI-powered) handle the fixes and re-submit. This mirrors how real CI gates work — CI doesn't fix your code, it tells you what's broken.

AI-assisted conflict triage and resolution is a v0.2 enhancement.

#### `soldier.py`

```python
class Soldier:
    def __init__(self, colony_url, repo_path, integration_branch="dev"):
        self.colony = ColonyClient(colony_url)
        self.repo_path = repo_path
        self.integration_branch = integration_branch

    def run(self):
        """Main soldier loop."""
        while True:
            mergeable = self.get_merge_queue()
            if not mergeable:
                sleep(30)
                continue
            for task in mergeable:
                result = self.attempt_merge(task)
                if result == MergeResult.MERGED:
                    self.colony.mark_merged(task["id"], task["current_attempt"])
                elif result == MergeResult.FAILED:
                    self.colony.kickback(task["id"], self.last_failure_reason)

    def get_merge_queue(self) -> list[dict]:
        """Get done tasks ordered by dependency then priority.

        Filters:
        - Only tasks with status DONE
        - Only tasks where current_attempt has a PR
        - Only tasks whose depends_on are ALL status MERGED
        - Ordered: dependency-safe order, then priority, then FIFO
        """
        ...

    def attempt_merge(self, task) -> MergeResult:
        """v0.1: deterministic, no AI.

        1. Create temp integration branch from dev
        2. Merge task's PR branch into temp
        3. If merge conflict: return FAILED immediately (no auto-fix in v0.1)
        4. Run test command (configurable, default: pytest)
        5. If green: fast-forward dev to temp
        6. If tests fail: return FAILED
        """
        ...


class MergeResult(StrEnum):
    MERGED = "merged"
    FAILED = "failed"
```

**Hard policy rules (v0.1):**
1. Merge ONLY to temp integration branch
2. No auto-fix — any merge conflict = immediate kickback
3. Any test failure = immediate kickback
4. Kickback returns task to `ready` — next forage creates new attempt
5. Dependent tasks stay merge-ineligible until upstream is merged
6. Independent tasks continue merging (queue is not globally blocked)
7. Merge only `current_attempt`
8. Soldier never modifies worker code — it is a deterministic gate

**v0.2 enhancements (not in v0.1):**
- AI-assisted conflict triage and resolution
- Auto-fix for trivial conflicts (import order, whitespace)
- Smarter kickback messages with failure diagnosis
- "trivial vs real vs ambiguous" classification

**Tests:**
1. `test_merge_queue_respects_deps` — task B (depends A) not in queue until A merged
2. `test_merge_green_fast_forwards` — successful merge fast-forwards dev
3. `test_merge_conflict_kicks_back` — any conflict kicks task back to ready
4. `test_test_failure_kicks_back` — test failure kicks task back to ready
5. `test_kickback_supersedes_attempt` — kicked-back attempt becomes superseded
6. `test_independent_tasks_not_blocked` — failing task A doesn't block unrelated task C
7. `test_only_current_attempt_merged` — superseded attempt PR is ignored

---

### Phase 8: End-to-End Test

**Goal:** Full loop works in a single test. Prove before polishing adapters.

```python
def test_e2e_full_loop(tmp_path):
    """
    1. Start colony (in-process, FileBackend in tmp_path)
    2. Register node
    3. Carry 2 tasks (task-002 depends on task-001)
    4. Register worker
    5. Worker forages task-001 (task-002 blocked by dep)
    6. Worker appends trail
    7. Worker harvests task-001 with mock PR
    8. Soldier merges task-001 (mock: skip real git/tests)
    9. Worker forages task-002 (now unblocked)
    10. Worker harvests task-002
    11. Soldier merges task-002
    12. Status shows: all tasks merged, 0 active, worker idle
    13. Doctor finds no issues
    """
```

---

### Phase 9: CLI

**Goal:** All commands wired up and usable from the terminal.

#### `cli.py`

Using `click`:

```python
import click

@click.group()
def main():
    """Antfarm — lightweight orchestration for AI coding agents."""
    pass

# Core commands
@main.command()
@click.option("--port", default=7433)
@click.option("--data-dir", default=".antfarm")
def colony(port, data_dir): ...

@main.command()
@click.option("--node", required=True)
def join(node): ...

@main.group()
def worker(): ...

@worker.command()
@click.option("--agent", required=True)
@click.option("--name", default=None)
@click.option("--workspace-root", default=None)
def start(agent, name, workspace_root): ...

@main.command()
@click.option("--title", required=False)
@click.option("--spec", required=False)
@click.option("--depends-on", multiple=True)
@click.option("--touches", default="")
@click.option("--priority", default=10, type=int)
@click.option("--complexity", default="M", type=click.Choice(["S", "M", "L"]))
@click.option("--file", "task_file", default=None, type=click.Path(exists=True), help="Load task from JSON file")
def carry(title, spec, depends_on, touches, priority, complexity, task_file): ...

@main.command()
def scout(): ...

@main.command()
@click.option("--fix", is_flag=True, default=False)
def doctor(fix): ...

# Low-level commands
@main.command()
@click.option("--name", default=None)
def hatch(name): ...

@main.command()
def forage(): ...

@main.command()
@click.argument("task_id")
@click.argument("message")
def trail(task_id, message): ...

@main.command()
@click.argument("task_id")
@click.option("--pr", required=True)
@click.option("--attempt", default=None)
def harvest(task_id, pr, attempt): ...

@main.command()
@click.argument("resource")
def guard(resource): ...

@main.command()
@click.argument("resource")
def release(resource): ...

@main.command()
@click.argument("task_id")
@click.argument("message")
def signal(task_id, message): ...
```

**Tests:**
1. `test_cli_colony_starts` — colony command starts server
2. `test_cli_carry_creates_task` — carry creates task file
3. `test_cli_scout_shows_status` — scout outputs formatted table
4. `test_cli_doctor_runs_checks` — doctor outputs findings
5. `test_cli_doctor_fix` — doctor --fix applies repairs

---

### Phase 10: Adapters

**Goal:** Claude Code and generic adapters are usable.

#### Claude Code Adapter

- `agents/worker.md` — system prompt instructing Claude Code to use antfarm CLI for foraging, trail, harvest
- `agents/soldier.md` — (v0.2: AI-assisted Soldier prompt. Not used in v0.1 — Soldier is deterministic `soldier.py`)
- `agents/queen.md` — example: reads a product spec, calls `antfarm carry` per subtask
- `hooks/heartbeat.sh` — PostToolUse hook: `curl -s -m 1 $ANTFARM_URL/workers/$WORKER_ID/heartbeat -X POST || true`
- `hooks/pre_forage.sh` — git sync before work (reference implementation, not normative)
- `setup.sh` — symlinks agents + configures hooks in `.claude/`

#### Generic Adapter

- `forage.sh` — `curl -s $ANTFARM_URL/tasks/pull -X POST -d "{\"worker_id\": \"$WORKER_ID\"}"`
- `heartbeat.sh` — same pattern
- `README.md` — copy-paste curl examples for any agent

---

## Coordination Flow

Full v0.1 coordination sequence showing how Colony, FileBackend, Scheduler, Worker, and Soldier interact:

```
         You                    Colony Server                FileBackend
          │                          │                          │
  antfarm carry ──POST /tasks──►     │                          │
          │                    write task.json ──────────► ready/task-001.json
          │                          │                          │
          │                          │                          │
       Worker                        │                          │
          │                          │                          │
  antfarm forage ─POST /tasks/pull─► │                          │
          │                    threading.Lock() ──────────►     │
          │                    scheduler.select_task(           │
          │                      ready_tasks,    ◄──── read ready/
          │                      done_task_ids,  ◄──── read done/
          │                      active_tasks    ◄──── read active/
          │                    )                                │
          │                    1. skip if deps not in done/     │
          │                    2. prefer non-overlapping touches│
          │                    3. priority (lower = higher)     │
          │                    4. FIFO (oldest first)           │
          │                    ─── winner: task-001 ──────►     │
          │                    os.rename(ready/ → active/)      │
          │                    create Attempt att-001           │
          │                    release Lock()                   │
          │  ◄── returns task JSON + attempt ──                 │
          │                                                     │
    (worker works)                                              │
          │                                                     │
  antfarm trail ──PATCH /tasks/001──►                           │
          │                    append TrailEntry ──────► active/task-001.json
          │                                                     │
  antfarm harvest ─PATCH /tasks/001─►                           │
          │                    move active/ → done/             │
          │                    attempt.status = DONE            │
          │                                                     │
          │                                                     │
       Soldier                       │                          │
          │                          │                          │
    polls GET /tasks?status=done ──► │                          │
          │                    list done/ tasks  ◄──── read done/
          │                    filter: current_attempt has PR   │
          │                    filter: all depends_on merged    │
          │                    order: dependency-safe → priority│
          │  ◄── returns merge queue ──                         │
          │                                                     │
    git merge to temp branch         │                          │
    run tests                        │                          │
    if green: fast-forward dev       │                          │
          │                          │                          │
  POST /tasks/001/merge ───────────► │                          │
          │                    attempt.status = MERGED ──► done/task-001.json
          │                    (task stays DONE in done/)        │
```

### Key design decisions

- **Antfarm is a control plane, not a meta-agent system.** Colony, Scheduler, Backend, Doctor, and Soldier are all deterministic — no LLM dependency. Workers may wrap AI coding tools (Claude Code, Codex, Aider), but Antfarm doesn't care what runs inside a worker session. The Soldier kicks back on any conflict or test failure in v0.1; AI-assisted conflict resolution is a v0.2 enhancement.
- **No background scheduler.** The scheduler runs on-demand inside every `forage` call. No polling, no cron, no event loop.
- **The filesystem IS the queue.** `ready/` is the backlog. Moving a file IS claiming a task.
- **`threading.Lock()` guards atomicity.** Only one `forage` or `guard` executes at a time. Single-process server.
- **Soldier polls, doesn't push.** It checks for done tasks every 30s. Workers don't notify the Soldier directly.

---

## State Machines

### Task State Machine

Task has only 3 statuses. Merge state lives on the attempt.

```
         carry
          │
          ▼
       ┌──────┐
       │READY │◄──────────────────────────┐
       └──┬───┘                           │
          │ forage (creates attempt)      │ kickback (Soldier)
          ▼                               │
       ┌──────┐                           │
       │ACTIVE│                           │
       └──┬───┘                           │
          │ harvest (PR opened)           │
          ▼                               │
       ┌──────┐                           │
       │ DONE │───────────────────────────┘
       └──────┘
          │
          │ Task stays DONE forever.
          │ Soldier marks the ATTEMPT as MERGED.
          │ Scout derives "merged" from attempt.status == MERGED.
```

### Attempt State Machine

```
       forage
          │
          ▼
       ┌──────┐
       │ACTIVE│
       └──┬───┘
          │
     ┌────┴────┐
     │         │
     ▼         ▼
  ┌──────┐  ┌──────────┐
  │ DONE │  │SUPERSEDED│  (task reassigned or kicked back)
  └──┬───┘  └──────────┘
     │
     ▼
  ┌──────┐
  │MERGED│
  └──────┘
```

### Worker State Machine (v0.1)

```
       hatch / worker start
          │
          ▼
       ┌──────┐
       │ IDLE │◄──────────────┐
       └──┬───┘               │
          │ forage            │ harvest
          ▼                   │
       ┌──────┐               │
       │ACTIVE│───────────────┘
       └──┬───┘
          │ heartbeat TTL expires or deregister
          ▼
       ┌───────┐
       │OFFLINE│
       └───────┘
```

---

## Edge Cases and Invariants (v0.1)

These are the unhappy-path rules that make a file-backed control plane trustworthy. Each must be enforced in code and covered by tests.

### API Idempotency

| Operation | Retry behavior |
|-----------|---------------|
| `carry` with duplicate task ID | Reject with 409. No overwrite. |
| `harvest` same task + attempt twice | Second call is a no-op (idempotent). No duplicate transitions or trail entries. |
| `harvest` from non-current attempt | Reject with 409. Worker is informed it lost ownership. |
| `register_worker` with existing worker_id | Reject unless prior worker is stale (heartbeat expired). |
| `register_node` with existing node_id | Idempotent — updates `last_seen`. |
| `deregister_worker` for unknown worker | No-op (idempotent). |

### Worker Crash Scenarios

| Scenario | Behavior |
|----------|----------|
| **Crash after forage, before workspace creation** | Task is in `active/` with no worktree. Heartbeat expires → doctor moves task back to `ready/`. Next forage creates a fresh attempt. No workspace is expected or required for recovery. |
| **Crash after code changes, before harvest** | Worktree has real code but task is stuck in `active/`. Heartbeat expires → doctor moves task to `ready/`. Orphaned worktree is a debug artifact only, never authoritative state. New attempt starts fresh. |
| **Crash after harvest, before response received** | Worker retries harvest → idempotent no-op (see above). |
| **Worker loses network, keeps working locally** | Heartbeat stops → doctor may requeue task → new attempt assigned to another worker. Original worker's attempt becomes superseded. If original worker later tries to harvest, it gets 409 (non-current attempt). |

### Guard Invariants

| Rule | Details |
|------|---------|
| **Release requires owner validation** | `release_guard(resource, owner)` — only the worker that acquired the guard can release it. Doctor `--fix` can clear stale guards regardless of owner. |
| **One guard per resource** | Second `guard()` call for same resource returns `false`. |
| **Stale detection keys off TTL + owner liveness** | Guard is stale if mtime > TTL AND owner worker is not registered/live. Pure mtime-based cleanup risks clearing guards held by slow-but-alive workers. |

### Soldier Invariants

| Rule | Details |
|------|---------|
| **Repo cleanup after failed merge** | Soldier must guarantee: temp branch deleted or reset, conflict state cleared, working tree clean before attempting next task. This is a critical invariant. |
| **Reset temp branch per task, not per loop** | Every `attempt_merge()` fetches latest integration branch and creates a fresh temp branch. Never reuse a temp branch across tasks. |
| **Done tasks with `current_attempt = None` are not merge candidates** | Kickback sets `current_attempt = None`. Soldier skips these. |
| **Harvest requires branch + PR metadata** | `mark_harvested()` requires both `branch` and `pr` fields. Done-without-PR is not a valid state for merge eligibility. |
| **Manual integration branch changes are tolerated** | If humans merge directly to the integration branch outside Antfarm, Soldier still works — it always fetches latest before merging. |

### Task / Scheduler Invariants

| Rule | Details |
|------|---------|
| **Cyclic dependencies** | Scheduler will never select tasks in a cycle (all deps perpetually unmet). Doctor should detect and report cycles. No auto-fix. |
| **Dangling dependency references** | If a task depends on `task-999` and that task doesn't exist, scheduler treats it as unmet. Doctor should report dangling references. |
| **`touches` normalization** | Trim whitespace, preserve case, deduplicate. Exact-match only for overlap detection. Applied on `carry()`. |

### File Atomicity

| Rule | Details |
|------|---------|
| **Append trail/signal must be atomic** | Read-modify-write under file lock (`threading.Lock()` in API server). Two concurrent appends must not clobber each other. |
| **Task file integrity** | If a crash interrupts a JSON write, the file may be corrupt. Doctor should detect malformed JSON and report it. |

### Doctor Invariant Checks

Doctor should detect and report these state inconsistencies:

| Check | What it catches |
|-------|----------------|
| Task in `ready/` but status field says `done` or `active` | Folder/status mismatch |
| Task in `active/` with no `current_attempt` | Orphaned active task |
| Task in `done/` with `current_attempt` pointing to non-existent attempt | Broken attempt reference |
| More than one `active` attempt on a single task | Duplicate claim (should be impossible but verify) |
| Merged attempt is `current_attempt` while task status is `ready` | Invalid state combination |
| Dependency cycle detected | Tasks that can never be scheduled |
| Dependency references non-existent task ID | Dangling reference |
| Malformed task JSON | Corrupt file from crash |

### Deferred to v0.2

- Trail/signal from superseded attempts (allow but mark as stale context)
- Distinguish infrastructure test failure vs code test failure in kickback messages
- Multiple guards per task (potential deadlock — ignore for v0.1, only one guard at a time)
- Richer 204 response metadata on forage (blocked-by-deps vs empty queue vs scope-overlap)

---

## First 10 Tests (Write These Before Anything Else)

| # | Test | Module | What it proves |
|---|------|--------|----------------|
| 1 | `test_task_roundtrip` | models | Task serializes to JSON and back with correct enum values |
| 2 | `test_attempt_created_on_pull` | file_backend | Pulling a task creates an Attempt with ACTIVE status |
| 3 | `test_pull_respects_dependencies` | scheduler | Blocked tasks are skipped |
| 4 | `test_pull_prefers_non_overlapping_scope` | scheduler | Scope preference works |
| 5 | `test_pull_is_atomic` | file_backend | Concurrent pulls don't return same task |
| 6 | `test_guard_exclusive` | file_backend | Second guard attempt fails |
| 7 | `test_mark_harvested` | file_backend | Task moves to done, attempt status is DONE |
| 8 | `test_kickback_returns_to_ready` | file_backend | Task goes to ready, attempt SUPERSEDED, trail has reason |
| 9 | `test_stale_worker_detected` | doctor | Worker with old heartbeat is flagged |
| 10 | `test_e2e_carry_forage_harvest` | e2e | Carry → forage → harvest cycle works end-to-end |

---

## Dependencies

```toml
[project]
name = "antfarm"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.100",
    "uvicorn>=0.20",
    "click>=8.0",
    "httpx>=0.24",
]

[project.optional-dependencies]
redis = ["redis>=5.0"]
dev = ["pytest>=7.0", "pytest-asyncio>=0.21", "ruff>=0.4"]

[project.scripts]
antfarm = "antfarm.core.cli:main"
```
