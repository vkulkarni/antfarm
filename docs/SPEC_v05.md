# Antfarm v0.5 — Specification

**Status:** Draft (revised with principal-engineer review feedback)
**Date:** 2026-04-05
**Goal:** Make the runtime trustworthy first, then make planning smarter.
**Philosophy:** Deterministic core, AI at the edges. Repo execution coordinator, not agent civilization simulator.

---

## What v0.5 IS

Consolidation, not expansion. Every piece that already exists should work better together:

1. Take a spec / issue / bug report
2. Break it into good parallel tasks (planner — optional, manual carry remains first-class)
3. Run multiple workers safely (conflict prevention)
4. Produce clean review artifacts (structured output with hard evidence + advisory)
5. Merge in a deterministic order (soldier with artifact gating + freshness checks)
6. Leave enough memory that the next run is better (repo memory with trust levels)
7. Give the operator clear visibility (TUI inbox — explain why anything is stuck)

## What v0.5 IS NOT

- No new backends (file + GitHub is enough)
- No more auth/platform admin features
- No LLM-first Soldier
- No recursive agent orchestration
- No vector DB / semantic memory
- No web app

---

## Non-Negotiable Invariants

These rules are absolute. No feature in v0.5 may violate them.

1. **One scheduling brain only.**
   `scheduler.select_task()` is the sole authority for task eligibility and selection. Backends persist state; they do not implement scheduling policy.

2. **Soldier gates only on deterministic evidence.**
   Soldier may read AI-generated summaries, risks, or review notes, but merge decisions may only depend on hard signals: dependency completion, branch freshness, artifact completeness, tests/lint/build status, and explicit operator overrides.

3. **AI may propose, but not authorize.**
   AI can help decompose specs, suggest `touches`, summarize changes, and suggest review focus. It cannot decide merge eligibility, retries, or state transitions.

4. **Every attempt ends in an artifact or a classified failure.**
   A worker attempt must produce either a valid `TaskArtifact` or a terminal `FailureRecord` with `failure_type`. No silent exits.

5. **Recovery must be invariant-driven.**
   Worker death, stale heartbeats, interrupted harvests, and stale branches must be recoverable without guesswork or manual state edits.

6. **Operator visibility is mandatory.**
   For any blocked, stale, failed, or merge-waiting task, Antfarm must be able to explain why it is in that state.

---

## Task and Attempt Lifecycle

Antfarm needs an explicit lifecycle contract so retries, recovery, and Soldier behavior remain predictable.

### Task States

```python
class TaskState(StrEnum):
    READY = "ready"                  # Eligible to be scheduled once deps are satisfied
    BLOCKED = "blocked"              # Waiting on deps or operator unblock
    ACTIVE = "active"                # Worker is executing the task
    HARVEST_PENDING = "harvest_pending"  # Worker finished, artifact/failure being recorded
    DONE = "done"                    # Harvested successfully; waiting for Soldier
    KICKED_BACK = "kicked_back"      # Returned for rework
    MERGE_READY = "merge_ready"      # Soldier approved for merge
    MERGED = "merged"                # Integrated into target branch
    FAILED = "failed"                # Terminal failure; no automatic retry remains
    PAUSED = "paused"                # Operator pause
```

### Attempt States

```python
class AttemptState(StrEnum):
    STARTED = "started"
    HEARTBEATING = "heartbeating"
    AGENT_SUCCEEDED = "agent_succeeded"
    AGENT_FAILED = "agent_failed"
    HARVESTED = "harvested"
    STALE = "stale"
    ABANDONED = "abandoned"
```

### State Transition Rules

- `READY → ACTIVE` only through the canonical scheduler and forage path.
- `ACTIVE → HARVEST_PENDING` only when worker execution ends.
- `HARVEST_PENDING → DONE` only if a valid artifact is written.
- `HARVEST_PENDING → FAILED` only if a classified failure is written.
- `DONE → MERGE_READY → MERGED` only through Soldier.
- `DONE → KICKED_BACK` when Soldier rejects for deterministic reasons: stale base, failed checks, missing artifact fields, or merge conflict.
- Any stale `ACTIVE` attempt becomes `STALE`; the task is either re-queued or escalated based on retry policy.
- No direct `ACTIVE → MERGED`, `READY → DONE`, or `FAILED → MERGED` transitions.

### Recovery Semantics

- If a worker dies before harvest, the attempt becomes `STALE`, never silently `DONE`.
- If code changed but artifact write failed, the task remains `HARVEST_PENDING` and is surfaced in the inbox.
- If Soldier sees the branch base is stale, the task becomes `KICKED_BACK`, not merged optimistically.

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
spec → PLANNER (optional) → tasks with deps/touches/risks
              ↓
    carry → CONFLICT PREVENTION → overlap warnings, claim hints
              ↓
    queue → CANONICAL SCHEDULER (single brain) → forage
              ↓
         work → STRUCTURED OUTPUT → artifact (hard evidence + advisory)
              ↓
         harvest → SOLDIER (artifact gating + freshness check)
              ↓
         merge → MEMORY (repo facts, outcomes, hotspots)
              ↓
         OPERATOR INBOX ← what needs attention and why?
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
- `antfarm/core/backends/file.py` — pull() becomes: read ready tasks, read active tasks, read worker info, call scheduler, move winner

**Tests:** Verify that changing scheduling policy in scheduler.py affects pull() behavior (single source of truth)

**Complexity:** M

---

### 2. Structured Task Output Contract (#77)

**Problem:** Workers currently harvest with branch/PR information, but Antfarm lacks a structured, deterministic artifact for merge gating and review.

**Solution:** Define a `TaskArtifact` with a strict split between **hard evidence** and **advisory commentary**.

```python
@dataclass
class TaskArtifact:
    # Identity
    task_id: str
    attempt_id: str
    worker_id: str

    # Source / freshness
    branch: str
    pr_url: str | None
    base_commit_sha: str
    head_commit_sha: str
    target_branch: str
    target_branch_sha_at_harvest: str

    # Change facts (deterministic)
    files_changed: list[str]
    lines_added: int
    lines_removed: int

    # Verification facts (deterministic)
    build_ran: bool
    build_passed: bool | None
    tests_ran: bool
    tests_passed: bool | None
    lint_ran: bool
    lint_passed: bool | None
    verification_commands: list[str]

    # Deterministic merge gate
    merge_readiness: Literal["ready", "needs_review", "blocked"]
    blocking_reasons: list[str]

    # Advisory / optional (AI-generated, not used for merge gating)
    summary: str | None
    risks: list[str]
    review_focus: list[str]
```

**Artifact Rules:**
- `base_commit_sha`, `head_commit_sha`, and `target_branch_sha_at_harvest` are required for Soldier freshness checks.
- `summary`, `risks`, and `review_focus` are optional and may be AI-generated.
- Soldier may **display** advisory fields but may not **gate** on them.
- `merge_readiness="ready"` is necessary but not sufficient; Soldier still verifies freshness, dependencies, and deterministic checks.

**Soldier Gating Rules:**
Soldier may merge only if:
- All dependencies are merged or otherwise marked satisfied
- Artifact exists and is valid
- Branch is fresh against target branch (base SHA check)
- Required verification signals passed (tests, lint, build)
- No blocking reasons remain
- No higher-priority serialization rule prevents merge

**Worker changes:**
- `_launch_agent()` collects git diff stat, SHAs, test results after agent completes
- Builds TaskArtifact automatically (deterministic fields from git metadata)
- AI-generated fields (summary, risks, review_focus) are optional — filled by Claude Code adapter, empty for generic

**Files:**
- `antfarm/core/models.py` — add TaskArtifact dataclass
- `antfarm/core/worker.py` — build artifact after agent completes
- `antfarm/core/backends/base.py` + `file.py` — artifact in mark_harvested()
- `antfarm/core/soldier.py` — artifact gating + freshness check
- `antfarm/core/tui.py` — display artifact in TUI

**Complexity:** M

---

### 3. Failure Taxonomy (#83)

**Problem:** All failures look the same. A flaky test failure gets the same treatment as a real logic bug.

**Solution:** Classify failures into categories:

```python
class FailureType(StrEnum):
    AGENT_CRASH = "agent_crash"
    AGENT_TIMEOUT = "agent_timeout"
    TEST_FAILURE = "test_failure"
    LINT_FAILURE = "lint_failure"
    MERGE_CONFLICT = "merge_conflict"
    BUILD_FAILURE = "build_failure"
    INFRA_FAILURE = "infra_failure"
    INVALID_TASK = "invalid_task"
```

**Default Retry Policy:**

| Failure Type | Default Action |
|-------------|---------------|
| `INFRA_FAILURE` | Automatic retry up to N times with backoff |
| `AGENT_CRASH` | Retry up to N times, then quarantine worker |
| `AGENT_TIMEOUT` | Retry once with longer timeout, then kick back |
| `TEST_FAILURE` | Kick back with artifact and failure summary |
| `LINT_FAILURE` | Kick back with artifact and failure summary |
| `BUILD_FAILURE` | Retry once if setup-related, else kick back |
| `MERGE_CONFLICT` | Refresh/rebase, retry once, then kick back |
| `INVALID_TASK` | No automatic retry; escalate to planner/operator |

> Failure type controls default system behavior. Classification without policy is incomplete.

**Integration:**
- Worker classifies failure based on agent exit code, stderr, git status
- Stored on attempt as `FailureRecord`
- Soldier uses failure type to decide: retry, kickback, or escalate
- Memory tracks failure patterns by type
- TUI shows failure type in inbox

**Files:**
- `antfarm/core/models.py` — add FailureType enum, FailureRecord, failure_type on Attempt
- `antfarm/core/worker.py` — classify failure after agent exit
- `antfarm/core/soldier.py` — use failure type in kickback/retry decision
- `antfarm/core/memory.py` — track by type

**Complexity:** S-M

---

### 4. Lightweight Repo Memory (#78)

**Problem:** Every task starts from zero. Workers rediscover build commands, test commands, hot files, past failures.

**Solution:** JSONL-based memory in `.antfarm/memory/` with explicit trust levels:

```
.antfarm/memory/
  repo_facts.json          # TRUSTED: operator-curated + auto-detected durable facts
  task_outcomes.jsonl       # APPEND-ONLY: run history
  hotspots.json             # HEURISTIC: computed from outcomes, may be noisy
  failure_patterns.json     # HEURISTIC: derived failure clusters
  touch_observations.jsonl  # HEURISTIC: actual files/scopes touched by completed tasks
```

**Trust levels:**
- `repo_facts.json` contains **trusted facts** (build command, test command, language, framework). Workers consume these as execution context.
- `hotspots.json`, `failure_patterns.json`, and `touch_observations.jsonl` are **heuristics derived from runs** and may be noisy. Scheduler treats hotspots as a weighting signal, not a hard ban. Workers do NOT receive heuristics as context — only trusted repo facts.

**How it's populated:**
- `repo_facts.json` — auto-detected on first run (scan for pyproject.toml, package.json, Makefile, etc.) + manually enrichable via `antfarm memory set-fact`
- `task_outcomes.jsonl` — appended after every harvest
- `hotspots.json` — computed periodically from task_outcomes
- `failure_patterns.json` — grouped failure reasons with frequency
- `touch_observations.jsonl` — actual files changed by completed tasks (from artifacts)

**How it's consumed:**
- Workers receive `repo_facts` as context in their prompt (test command, lint command, etc.)
- Planner uses `touch_observations` and hotspots to improve future `touches` prediction
- Scheduler treats hotspots as a weighting signal, not a hard ban
- TUI shows hotspot warnings

**Files:**
- NEW: `antfarm/core/memory.py` — MemoryStore class (~150 lines)
- `antfarm/core/worker.py` — inject repo_facts into agent prompt, record outcome after harvest
- `antfarm/core/cli.py` — `antfarm memory show`, `antfarm memory set-fact <key> <value>`

**Complexity:** M

---

### 5. Conflict Prevention Layer (#80)

**Problem:** Two workers can claim tasks touching the same files. Conflicts found at merge time, not prevented.

> `touches` are predictive hints, not exact file locks. Antfarm uses them to reduce conflict likelihood, not to claim perfect conflict freedom. Completed task artifacts feed observed changed files back into memory so future `touches` predictions improve over time.

**Solution:** Add conflict awareness to the scheduling layer:

**5a. Overlap Warnings at Carry Time:**
```bash
antfarm carry --title "Update auth" --touches "api,auth"
# WARNING: Task "Build login" (active, worker node-1/claude-1) also touches: api, auth
# Likely conflict. Consider waiting or serializing.
# Carry anyway? [y/N]
```

**5b. Hotspot Detection:**
- Files that appear in >2 failed tasks in the last 10 outcomes are "hot"
- Scheduler prefers serializing tasks that touch hot files (stronger scope preference)
- TUI shows hotspot warnings

**5c. Module Claim Hints:**
- When a worker forages, the scheduler records which `touches` scopes are "claimed"
- Subsequent forage calls see claimed scopes and prefer non-overlapping tasks

**5d. Conflict Risk Score:**
- Each task gets a `conflict_risk: float` (0.0-1.0) based on:
  - How many active tasks share its touches
  - Whether any touches are hotspots
  - Whether dependencies are being actively worked on
- TUI shows conflict risk. Operator can decide to pause risky tasks.

**Files:**
- `antfarm/core/scheduler.py` — enhanced scope overlap + hotspot weighting
- `antfarm/core/memory.py` — hotspot tracking + touch observations
- `antfarm/core/cli.py` — overlap warning on carry
- `antfarm/core/tui.py` — conflict risk display

**Complexity:** M

---

## P1 Features (Strongly Recommended)

### 6. Operator Inbox in TUI (#81)

**Problem:** TUI shows status but doesn't highlight what needs attention or explain why.

**Solution:** Add an "inbox" panel to the TUI that surfaces actionable items:

- Tasks kicked back by Soldier (need re-work) — **why:** stale base / test failure / merge conflict
- Tasks with signals (need human input) — **why:** worker escalated
- Stale workers (heartbeat expired) — **why:** worker_id, last heartbeat time
- Blocked tasks (dependency stuck) — **why:** which dep is blocking
- High conflict-risk tasks — **why:** overlapping touches with active tasks
- Failed harvest attempts — **why:** failure type classification
- Tasks active for too long (may be stuck) — **why:** duration, worker_id

Each item explains **what happened** and **what to do** (reassign, unblock, kill worker, retry).

**Files:**
- `antfarm/core/tui.py` — add inbox panel
- `antfarm/core/cli.py` — `antfarm inbox` as standalone command

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
- Tests: 155 passed, 0 failed ✓
- Lint: clean ✓
- Base SHA: fresh against target ✓

### Risks
- Token printed at colony startup (log exposure risk)

### Suggested Review Focus
- auth.py: verify HMAC-SHA256 implementation
- serve.py: middleware bypass for /status endpoint
```

**Integration:**
- Generated from TaskArtifact deterministic fields
- Posted as PR comment automatically (if GitHub backend or via `gh pr comment`)
- Displayed in TUI for done tasks

**Files:**
- NEW: `antfarm/core/review_pack.py` — generate_review_pack(artifact) -> str
- `antfarm/core/soldier.py` — post review pack as PR comment before merge decision
- `antfarm/core/tui.py` — display review pack for selected task

**Complexity:** S

---

### 8. Planner/Decomposer (#79)

**Problem:** Tasks are created manually via `antfarm carry`. No automated decomposition from specs/issues.

> `antfarm plan` is optional. Manual `antfarm carry` remains a first-class workflow.

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
- Uses hotspots and touch_observations from memory to improve touches prediction
- On confirmation, calls `antfarm carry` for each task

**Implementation:**
- NEW: `antfarm/core/planner.py` — PlannerEngine class
- For v0.5: planner calls `claude -p` with a decomposition prompt. Not a complex framework — just a well-crafted prompt + structured output parsing.
- `antfarm/core/cli.py` — `antfarm plan` command

**Complexity:** L (but bounded — it's a prompt + JSON parser, not a reasoning engine)

---

### 9. Docs Rewrite (#73)

**Problem:** README says "v0.1.0 core loop complete." Spec is frozen at v0.1. Code is at v0.4+.

**Solution:** Full docs rewrite:

- README: what Antfarm is TODAY, what's shipped, how to use it
- Architecture doc: how the pieces fit together (colony, scheduler, worker, soldier, memory, TUI)
- Operator guide: day-to-day usage, monitoring, troubleshooting
- Contributor guide: how to add adapters, backends, features
- Deprecate/archive the v0.1 frozen spec

**Complexity:** M (writing, not coding)

---

## Release Slices

### v0.5.0-alpha.1 — Runtime Truth

Make the runtime deterministic and observable.

- Canonical scheduler (#72) — single scheduling brain
- Failure taxonomy + default retry policy (#83)
- Task/attempt lifecycle + invariants (new states, transition rules)
- Initial inbox surfacing for stale/blocked/failed work (#81 partial)

### v0.5.0-alpha.2 — Artifact Gating

Make merges safe with evidence-based gating.

- Structured task output contract (#77) — hard evidence + advisory split
- Soldier artifact gating — freshness checks, base SHA validation
- Review pack generation (#82)

### v0.5.0-alpha.3 — Memory + Prevention

Make parallelism smarter with data.

- Repo memory (#78) — trusted facts + heuristic observations
- Conflict prevention layer (#80) — overlap warnings, hotspots, claim hints
- Touch observation feedback loop (artifacts → memory → better touches)

### v0.5.0-alpha.4 — Planning

Add AI-assisted planning on top of a stable substrate.

- Planner/decomposer (#79) — `antfarm plan` CLI
- Planner informed by repo facts + hotspots + observed touches
- Conflict warnings in plan output

### v0.5.0 — Docs + Polish

- Docs rewrite (#73)
- Audit trail (#75)
- Bug fixes from alpha testing

**Why this order:** First make the runtime deterministic, then make merges safe, then make parallelism smarter, then add AI-assisted planning on top of a stable substrate.

---

## Success Criteria

### Scenario A: Runtime Integrity
Given worker death, stale heartbeats, or interrupted harvest, Antfarm recovers the task without silent corruption or manual JSON edits. Every stuck task has an explanation visible in the inbox.

### Scenario B: Safe Parallel Execution
With 3 workers on one repo, Antfarm materially reduces preventable overlap by preferring non-conflicting tasks and surfacing conflict risk early. Stale tasks are recovered automatically.

### Scenario C: Reviewable Output
Every completed attempt produces either a valid `TaskArtifact` or a classified `FailureRecord`. Review packs are generated from artifacts, not raw diff guessing.

### Scenario D: Deterministic Merging
Soldier merges only when: dependencies are satisfied, artifact is valid, freshness checks pass, required verification checks pass, merge readiness is "ready", and no blocking reasons remain.

### Scenario E: Useful Memory
On the second run in the same repo, workers reuse repo facts (test/build commands), scheduler benefits from hotspot data, and planner proposes better `touches` based on prior observed changes.

---

## Explicitly Deferred

- Redis backend enhancements
- Jira / Linear / Notion backends
- Cursor / Windsurf adapters
- Web UI
- Multi-repo support
- Recursive agent orchestration
- Vector DB / semantic memory
- Claude Code plugin (v0.6)

---

## Technical Debt to Address

1. Untracked redis.py + test_redis_backend.py on local filesystem — clean up or gitignore
2. Branch protection CI check "test" never runs — fix GitHub Actions or relax check
3. Engineer self-merge prevention — add to CLAUDE.md guardrails
4. Multiple scheduling brains — the #1 refactor target (addressed by #72)
5. TUI rendering bug — current_attempt is string ID not dict (worker_id extraction broken)
