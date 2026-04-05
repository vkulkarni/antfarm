# Antfarm Architecture

System architecture guide for Antfarm v0.5.

## Overview

Antfarm is a coordination layer for distributing coding work across machines running AI coding agents. It does not write code — it manages task assignment, workspace isolation, review orchestration, and safe integration.

```
carry → schedule → forage → work → harvest → review → merge
         ↑                                         ↓
         └── memory (outcomes, hotspots) ←─────────┘
```

## Core Modules

### Colony Server (`serve.py`)

FastAPI application exposing the task queue, worker registry, and guard locks over HTTP. Single-process with `threading.Lock()` on mutation-critical paths.

Key endpoints:
- `POST /tasks` — carry (enqueue) a task
- `POST /tasks/pull` — forage (claim) next eligible task
- `POST /tasks/{id}/harvest` — mark task complete
- `POST /tasks/{id}/review-verdict` — store review verdict
- `GET /tasks/{id}/conflict-risk` — compute conflict risk score

### Scheduler (`scheduler.py`)

Deterministic task selection with policy applied in order:
1. Dependency check — skip tasks with unmet deps
2. Capability check — match worker capabilities to task requirements
3. Pin check — respect task-to-worker pinning
4. Scope preference — prefer non-overlapping touches with active tasks
5. Hotspot weighting — deprioritize tasks touching failure-prone scopes
6. Priority + FIFO — lower number = higher priority, oldest first among equals

### Backends (`backends/`)

Pluggable task storage via the `TaskBackend` ABC. Explicit mutation methods enforce valid state transitions.

- **FileBackend** — filesystem-backed queue. `os.rename()` for atomic state transitions. Zero dependencies.
- **GitHubBackend** — GitHub Issues as task storage. Labels for state, issue body for task JSON.

### Worker Runtime (`worker.py`)

Orchestrates: register, forage, workspace setup, agent launch, harvest, repeat. Handles failure classification, heartbeat threads, and review verdict parsing.

Agent types: `claude-code`, `codex`, `aider`, `generic` (any executable).

### Soldier (`soldier.py`)

Deterministic merge gate. No AI, no auto-fix.

Flow:
1. Poll for done tasks
2. Create review tasks for unreviewed work (`process_done_tasks()`)
3. Gate merge queue on review verdict + freshness
4. Merge to temp branch, run tests, fast-forward on green
5. Kickback on conflict or test failure

When `require_review=True`:
- `run_once_with_review()` orchestrates the full review flow
- Extracts verdicts from completed review tasks
- Stores verdicts on original task attempts

### Memory (`memory.py`)

Lightweight JSONL-based repo memory in `.antfarm/memory/`:

| File | Trust | Purpose |
|------|-------|---------|
| `repo_facts.json` | Trusted | Operator-curated + auto-detected facts |
| `task_outcomes.jsonl` | Factual | Append-only run history |
| `hotspots.json` | Heuristic | Failure-correlated scopes |
| `failure_patterns.json` | Heuristic | Grouped failure type counts |
| `touch_observations.jsonl` | Heuristic | Actual files changed per task |

### Conflict Prevention (`conflict.py`)

- `compute_overlap_warnings()` — warn when new task touches overlap active tasks
- `compute_conflict_risk()` — 0.0-1.0 score from overlap ratio + hotspot heat

### Planner (`planner.py`)

AI-assisted task decomposition:
- Parse specs into `ProposedTask` objects
- Validate dependencies, detect cycles
- Generate overlap and hotspot warnings
- Resolve 1-based index deps to actual task IDs
- Optional AI agent for decomposition via subprocess

### Lifecycle (`lifecycle.py`)

Task and attempt state transition validators. Maps old status names to v0.5 equivalents for backward compatibility.

### Doctor (`doctor.py`)

Pre-flight checks and stale recovery. Detects stale workers, orphaned tasks, expired guards, and filesystem inconsistencies.

### Inbox (`inbox.py`)

Surfaces items needing operator attention: stale workers, failed tasks, blocked deps, long-running tasks, hotspot overlaps.

## Data Flow

```
Operator                    Colony                    Workers
   │                          │                          │
   ├── carry task ───────────►│                          │
   │   (or antfarm plan)      │                          │
   │                          │◄── forage ──────────────┤
   │                          │── task + attempt ───────►│
   │                          │                          ├── work
   │                          │◄── trail entries ────────┤
   │                          │◄── harvest ──────────────┤
   │                          │                          │
   │                       Soldier                       │
   │                          │── create review task ───►│
   │                          │◄── review verdict ───────┤
   │                          │── merge (if pass) ──────►│
   │                          │                          │
   │                       Memory                        │
   │                          │── record outcome         │
   │                          │── recompute hotspots     │
```

## Key Design Decisions

1. **FileBackend IS the queue.** Claiming = `os.rename()`. Atomic on POSIX.
2. **Soldier is deterministic.** No AI in the merge gate. Workers fix; Soldier gates.
3. **Review is a task.** Review tasks go through the same queue as build tasks.
4. **Memory is advisory.** Hotspots influence scheduling but never block. All memory access is wrapped in try/except.
5. **No background scheduler.** Scheduling runs on-demand during forage.
6. **Lifecycle enforcement.** State transitions are validated before mutation.
