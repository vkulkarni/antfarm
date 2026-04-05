# Antfarm v0.5 — Specification

**Status:** Draft
**Date:** 2026-04-05
**Goal:** Consolidation, not expansion. Make Antfarm the safest, simplest way to run multiple coding agents against one real repo without merge chaos.
**Philosophy:** Deterministic core, AI at the edges. Repo execution coordinator, not agent civilization simulator.

---

## What v0.5 IS

Tighten the loop. Every piece that already exists should work better together. The product promise:

1. Take a spec / issue / bug report
2. Break it into good parallel tasks (planner)
3. Run multiple workers safely (conflict prevention)
4. Produce clean review artifacts (structured output)
5. Merge in a deterministic order (soldier)
6. Leave enough memory that the next run is better (repo memory)
7. Give the operator clear visibility (TUI inbox)

## What v0.5 IS NOT

- No new backends (file + GitHub is enough)
- No more auth/platform admin features
- No LLM-first Soldier
- No recursive agent orchestration
- No vector DB / semantic memory
- No web app

---

## Architecture Changes

### Current State (v0.4)

```
carry → queue → forage → work → harvest → soldier merge
                                    ↑ no structured output
                                    ↑ no conflict prevention
                                    ↑ no memory
         ↑ manual decomposition
         ↑ two scheduling brains (scheduler.py + inline in file.py)
```

### Target State (v0.5)

```
spec → PLANNER → tasks with deps/touches/risks
                    ↓
         CONFLICT PREVENTION → overlap warnings, claim hints
                    ↓
    queue → CANONICAL SCHEDULER → forage
                    ↓
         work → STRUCTURED OUTPUT → review pack
                    ↓
         harvest → SOLDIER (with artifact gating)
                    ↓
         merge → MEMORY (repo facts, outcomes, hotspots)
                    ↓
         OPERATOR INBOX ← what needs attention?
```

---

## P0 Features (Must Ship)

### 1. Canonical Scheduler (#72)

**Problem:** FileBackend.pull() has inline scheduling logic that duplicates scheduler.py. Two brains = drift.

**Solution:** 
- Remove ALL scheduling logic from `file.py` pull()
- pull() calls `scheduler.select_task()` with all parameters
- scheduler.select_task() is the ONLY place that decides task eligibility
- Backend is pure state persistence — no business logic

**Files:**
- `antfarm/core/scheduler.py` — add all filters (deps, caps, pin, rate limit, scope)
- `antfarm/core/backends/file.py` — pull() becomes: read ready tasks, read worker info, call scheduler, move winner

**Tests:** Verify that changing scheduling policy in scheduler.py affects pull() behavior (single source of truth)

**Complexity:** M

---

### 2. Structured Task Output Contract (#77)

**Problem:** Workers just harvest with branch/PR. No structured artifact. Soldier can't make informed merge decisions. Humans can't review efficiently.

**Solution:** Define a `TaskArtifact` dataclass that workers emit on completion:

```python
@dataclass
class TaskArtifact:
    task_id: str
    attempt_id: str
    worker_id: str
    branch: str
    pr_url: str | None
    
    # What changed
    files_changed: list[str]
    lines_added: int
    lines_removed: int
    
    # What was verified
    tests_run: bool
    tests_passed: bool
    test_output_summary: str
    lint_clean: bool
    
    # Assessment
    summary: str              # 1-3 sentence description of what was done
    risks: list[str]          # known risks or concerns
    merge_readiness: str      # "ready", "needs_review", "blocked"
    review_focus: list[str]   # suggested areas for reviewer to check
```

**Integration:**
- `mark_harvested()` accepts artifact dict alongside existing params
- Artifact stored on the attempt in the task JSON
- Soldier checks `merge_readiness` and `tests_passed` before merging
- `scout --tui` displays artifact summary for done tasks

**Worker changes:**
- `_launch_agent()` collects git diff stat, test results after agent completes
- Builds TaskArtifact automatically (worker doesn't need AI to fill this — it's git metadata)
- AI-generated fields (summary, risks, review_focus) are optional — filled by Claude Code adapter, empty for generic

**Files:**
- `antfarm/core/models.py` — add TaskArtifact dataclass
- `antfarm/core/worker.py` — build artifact after agent completes
- `antfarm/core/backends/base.py` + `file.py` — artifact in mark_harvested()
- `antfarm/core/soldier.py` — check artifact before merging
- `antfarm/core/tui.py` — display artifact in TUI

**Complexity:** M

---

### 3. Lightweight Repo Memory (#78)

**Problem:** Every task starts from zero. Workers rediscover build commands, test commands, hot files, past failures.

**Solution:** JSONL-based memory in `.antfarm/memory/`:

```
.antfarm/memory/
  repo_facts.json      # build/test commands, language, framework, entry points
  task_outcomes.jsonl   # append-only log of task results
  hotspots.json         # files that frequently cause conflicts or failures
  failure_patterns.json # recurring failure types and resolutions
```

**Not a vector DB. Not semantic search.** Simple key-value and append-only logs.

**How it's populated:**
- `repo_facts.json` — auto-detected on first run (scan for pyproject.toml, package.json, Makefile, etc.) + manually enrichable
- `task_outcomes.jsonl` — appended after every harvest (task_id, success, files_changed, duration, failure_reason if any)
- `hotspots.json` — computed periodically from task_outcomes (files that appear in failed tasks)
- `failure_patterns.json` — grouped failure reasons with frequency

**How it's consumed:**
- Workers receive repo_facts as context in their prompt (test command, lint command, etc.)
- Planner uses hotspots to set `touches` and flag risky tasks
- Scheduler uses hotspots to prefer serializing work on risky files
- TUI shows hotspot warnings

**Files:**
- NEW: `antfarm/core/memory.py` — MemoryStore class (~150 lines)
- `antfarm/core/worker.py` — inject repo_facts into agent prompt, record outcome after harvest
- `antfarm/core/cli.py` — `antfarm memory show`, `antfarm memory set-fact <key> <value>`

**Complexity:** M

---

### 4. Planner/Decomposer (#79)

**Problem:** Tasks are created manually via `antfarm carry`. No automated decomposition from specs/issues.

**Solution:** `antfarm plan` command that takes a spec and produces tasks:

```bash
antfarm plan --spec "Build user authentication with JWT login, logout, and profile endpoints"
# Or from a file
antfarm plan --file feature_spec.md
# Or from a GitHub issue
antfarm plan --issue 42 --repo owner/repo
```

**Output:**
```
Proposed tasks:
  1. [api] JWT auth middleware          touches: api,auth      deps: none
  2. [api] Login endpoint              touches: api,auth      deps: 1
  3. [api] Logout endpoint             touches: api,auth      deps: 1
  4. [api] Profile endpoint            touches: api            deps: 1
  5. [test] Auth integration tests     touches: tests,auth     deps: 2,3,4

Conflict warnings:
  - Tasks 2,3,4 all touch api/auth — consider serializing 2→3→4
  - Task 1 is a dependency for all others — merge first

Carry these tasks? [y/N]
```

**How it works:**
- Uses Claude Code (or any AI agent) to decompose the spec
- Generates task JSON with title, spec, depends_on, touches, priority
- Uses repo_facts from memory to inform decomposition (language, structure, conventions)
- Uses hotspots from memory to flag risky areas
- On confirmation, calls `antfarm carry` for each task

**Implementation:**
- NEW: `antfarm/core/planner.py` — PlannerEngine class
- For v0.5: planner calls `claude -p` with a decomposition prompt. Not a complex framework — just a well-crafted prompt + structured output parsing.
- `antfarm/core/cli.py` — `antfarm plan` command

**Complexity:** L (but bounded — it's a prompt + JSON parser, not a reasoning engine)

---

### 5. Conflict Prevention Layer (#80)

**Problem:** Two workers can claim tasks touching the same files. Conflicts found at merge time, not prevented.

**Solution:** Add conflict awareness to the scheduling layer:

**5a. Overlap Warnings at Carry Time:**
```bash
antfarm carry --title "Update auth" --touches "api,auth"
# WARNING: Task "Build login" (active, worker mini-1/claude-1) also touches: api, auth
# Likely conflict. Consider waiting or serializing.
# Carry anyway? [y/N]
```

**5b. Hotspot Detection:**
- Files that appear in >2 failed tasks in the last 10 outcomes are "hot"
- Scheduler prefers serializing tasks that touch hot files (stronger scope preference)
- TUI shows hotspot warnings

**5c. Module Claim Hints:**
- When a worker forages, the scheduler records which `touches` scopes are "claimed"
- Subsequent forage calls see claimed scopes and prefer non-overlapping tasks (already exists as soft preference — make it stronger and more visible)

**5d. Conflict Risk Score:**
- Each task gets a `conflict_risk: float` (0.0-1.0) based on:
  - How many active tasks share its touches (higher = riskier)
  - Whether any touches are hotspots
  - Whether dependencies are being actively worked on
- TUI shows conflict risk. Operator can decide to pause risky tasks.

**Files:**
- `antfarm/core/scheduler.py` — enhanced scope overlap + hotspot weighting
- `antfarm/core/memory.py` — hotspot tracking
- `antfarm/core/cli.py` — overlap warning on carry
- `antfarm/core/tui.py` — conflict risk display

**Complexity:** M

---

## P1 Features (Strongly Recommended)

### 6. Operator Inbox in TUI (#81)

**Problem:** TUI shows status but doesn't highlight what needs attention.

**Solution:** Add an "inbox" panel to the TUI that surfaces:

- Tasks that were kicked back by Soldier (need re-work)
- Tasks with signals (need human input)
- Stale workers (heartbeat expired)
- Blocked tasks (dependency stuck)
- High conflict-risk tasks
- Failed harvest attempts
- Tasks that have been active for too long (may be stuck)

Each item is actionable: the inbox shows what to do (reassign, unblock, kill worker, etc.)

**Files:**
- `antfarm/core/tui.py` — add inbox panel, surface from status + task data
- `antfarm/core/cli.py` — `antfarm inbox` as standalone command (non-TUI)

**Complexity:** S-M

---

### 7. Review Pack Generation (#82)

**Problem:** When a task is done, reviewer gets a raw PR diff with no context.

**Solution:** Auto-generate a review pack from the TaskArtifact:

```markdown
## Review Pack: task-001 "Build auth middleware"

### Summary
Added JWT auth middleware with token validation and route protection.

### Files Changed (4 files, +120 -5)
- antfarm/core/auth.py (new, 81 lines)
- antfarm/core/serve.py (modified, +13 -2)
- antfarm/core/cli.py (modified, +25 -3)
- tests/test_auth.py (new, 220 lines)

### Checks
- Tests: 155 passed, 0 failed
- Lint: clean

### Risks
- Token printed at colony startup (log exposure risk)

### Suggested Review Focus
- auth.py: verify HMAC-SHA256 implementation
- serve.py: middleware bypass for /status endpoint
```

**Integration:**
- Generated from TaskArtifact data
- Posted as PR comment automatically (if GitHub backend or via `gh pr comment`)
- Displayed in TUI for done tasks

**Files:**
- NEW: `antfarm/core/review_pack.py` — generate_review_pack(artifact) -> str
- `antfarm/core/soldier.py` — post review pack as PR comment before merge decision
- `antfarm/core/tui.py` — display review pack for selected task

**Complexity:** S

---

### 8. Failure Taxonomy (#83)

**Problem:** All failures look the same. A flaky test failure gets the same treatment as a real logic bug.

**Solution:** Classify failures into categories:

```python
class FailureType(StrEnum):
    AGENT_CRASH = "agent_crash"           # Agent process died
    AGENT_TIMEOUT = "agent_timeout"       # Agent exceeded time limit
    TEST_FAILURE = "test_failure"         # Tests failed
    LINT_FAILURE = "lint_failure"         # Lint/type errors
    MERGE_CONFLICT = "merge_conflict"     # Git merge failed
    BUILD_FAILURE = "build_failure"       # Build/install failed
    INFRA_FAILURE = "infra_failure"       # Network, disk, permission
    INVALID_TASK = "invalid_task"         # Task spec is ambiguous/impossible
```

**Integration:**
- Worker classifies failure based on agent exit code, stderr, git status
- Stored on attempt
- Soldier uses failure type to decide: retry (infra), kickback (test/lint), escalate (invalid_task)
- Memory tracks failure patterns by type
- TUI shows failure type in inbox

**Files:**
- `antfarm/core/models.py` — add FailureType enum, failure_type on Attempt
- `antfarm/core/worker.py` — classify failure after agent exit
- `antfarm/core/soldier.py` — use failure type in kickback decision
- `antfarm/core/memory.py` — track by type

**Complexity:** S

---

### 9. Docs Rewrite (#73)

**Problem:** README says "v0.1.0 core loop complete." Spec is frozen at v0.1. Code is at v0.4.

**Solution:** Full docs rewrite:

- README: what Antfarm is TODAY (v0.5), what's shipped, how to use it
- Architecture doc: how the pieces fit together (colony, scheduler, worker, soldier, memory, TUI)
- Operator guide: day-to-day usage, monitoring, troubleshooting
- Contributor guide: how to add adapters, backends, features
- Deprecate/archive the v0.1 frozen spec

**Complexity:** M (writing, not coding)

---

## Release Slices

### v0.5.0-alpha.1 — Foundation
- Canonical scheduler (#72)
- Structured task output contract (#77)
- Failure taxonomy (#83)

### v0.5.0-alpha.2 — Memory + Prevention
- Repo memory (#78)
- Conflict prevention layer (#80)
- Hotspot detection

### v0.5.0-alpha.3 — Planner + Review
- Planner/decomposer (#79)
- Review pack generation (#82)
- Operator inbox (#81)

### v0.5.0 — Docs + Polish
- Docs rewrite (#73)
- Audit trail (#75)
- Bug fixes from alpha testing

---

## Success Criteria

### Scenario A: Feature Decomposition
Given a medium-sized feature spec, `antfarm plan` produces 5-10 tasks with correct dependencies, touch annotations, and conflict warnings.

### Scenario B: Safe Parallel Execution
3 workers on one repo. No merge conflicts that could have been prevented. Stale tasks recovered automatically. Operator sees blockers in inbox.

### Scenario C: Reviewable Output
Every completed task produces a review pack with: summary, files changed, tests run, risks, review focus. Posted on the PR automatically.

### Scenario D: Deterministic Merging
Soldier merges only when: deps complete, artifact shows tests_passed=True, lint_clean=True, merge_readiness="ready".

### Scenario E: Useful Memory
Second run in the same repo: workers know the test command, avoid hot files, planner produces better task decomposition.

---

## Explicitly Deferred

- Redis backend enhancements
- Jira / Linear / Notion backends
- Cursor / Windsurf adapters
- Web UI
- Multi-repo support
- Recursive agent orchestration
- Vector DB / semantic memory

---

## Technical Debt to Address

1. Untracked redis.py + test_redis_backend.py on local filesystem — clean up or gitignore
2. Branch protection CI check "test" never runs — fix GitHub Actions or relax check
3. Engineer self-merge prevention — add to CLAUDE.md guardrails
4. Multiple scheduling brains — the #1 refactor target
