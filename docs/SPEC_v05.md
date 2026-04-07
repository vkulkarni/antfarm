# Antfarm v0.5 — Specification

**Status:** FROZEN — approved for implementation
**Date:** 2026-04-05
**Goal:** Antfarm v0.5 should not become broader. It should become more trustworthy.
**Philosophy:** Deterministic core, AI at the edges. Repo execution coordinator, not agent civilization simulator.

---

## What v0.5 IS

Tighten the loop. Every piece that already exists should work better together.

The product promise:

1. Take a spec / issue / bug report
2. Break it into good parallel tasks
3. Run multiple workers safely
4. Produce clean review artifacts
5. Merge in a deterministic order
6. Leave enough memory that the next run is better
7. Give the operator clear visibility

## What v0.5 IS NOT

- No new backends (file + GitHub is enough)
- No more auth / platform admin features
- No LLM-first Soldier
- No recursive agent orchestration
- No vector DB / semantic memory
- No web app

---

## Product Principle

**Antfarm v0.5 makes the runtime trustworthy first, then makes planning smarter.**

The system should be excellent at three things:

1. Decomposing work into reasonably parallel tasks
2. Avoiding needless collisions
3. Producing reviewable, merge-safe outputs

Everything in v0.5 should strengthen one of those three outcomes.

---

## Non-Negotiable Invariants

1. **One scheduling brain only.**
   `scheduler.select_task()` is the sole authority for task eligibility and selection. Backends persist state; they do not implement scheduling policy.

2. **Soldier gates only on deterministic evidence.**
   Soldier may read AI-generated summaries, risks, or review notes, but merge decisions may only depend on dependency completion, branch freshness, artifact completeness, verification status, and explicit operator overrides.

3. **AI may propose, but not authorize.**
   AI can help decompose specs, suggest `touches`, summarize changes, and suggest review focus. It cannot decide merge eligibility, retries, or core state transitions.

4. **Every attempt ends in an artifact or a classified failure.**
   A worker attempt must produce either:
   - a valid `TaskArtifact`, or
   - a terminal `FailureRecord` with `failure_type`

5. **Recovery must be invariant-driven.**
   Worker death, stale heartbeats, interrupted harvests, and stale branches must be recoverable without guesswork or manual JSON edits.

6. **Operator visibility is mandatory.**
   For any blocked, stale, failed, or merge-waiting task, Antfarm must be able to explain why it is in that state.

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

## Task and Attempt Lifecycle

Antfarm needs an explicit lifecycle contract so retries, recovery, and Soldier behavior remain predictable.

### Task States

```python
class TaskState(StrEnum):
    QUEUED = "queued"
    BLOCKED = "blocked"
    CLAIMED = "claimed"
    ACTIVE = "active"
    HARVEST_PENDING = "harvest_pending"
    DONE = "done"
    KICKED_BACK = "kicked_back"
    MERGE_READY = "merge_ready"
    MERGED = "merged"
    FAILED = "failed"
    PAUSED = "paused"
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

- `QUEUED → CLAIMED → ACTIVE` only through the canonical scheduler and forage path
- `ACTIVE → HARVEST_PENDING` only when worker execution ends
- `HARVEST_PENDING → DONE` only if a valid artifact is written
- `HARVEST_PENDING → FAILED` only if a classified failure is written
- `DONE → MERGE_READY → MERGED` only through Soldier
- `DONE → KICKED_BACK` when Soldier rejects for deterministic reasons such as stale base, failed checks, missing artifact fields, or merge conflict
- Any stale `ACTIVE` attempt becomes `STALE`; the task is either re-queued or escalated based on retry policy
- No direct `ACTIVE → MERGED`, `QUEUED → DONE`, or `FAILED → MERGED` transitions

### Recovery Semantics

- If a worker dies before harvest, the attempt becomes `STALE`, never silently `DONE`
- If code changed but artifact write failed, the task remains `HARVEST_PENDING` and is surfaced in the inbox
- If Soldier sees the branch base is stale, the task becomes `KICKED_BACK`, not merged optimistically

---

## P0 Features (Must Ship)

### 1. Canonical Scheduler (#72)

**Problem:** `FileBackend.pull()` has inline scheduling logic that duplicates `scheduler.py`. Two brains = drift.

**Solution:**

- Remove all scheduling logic from `file.py` `pull()`
- `pull()` calls `scheduler.select_task()` with all parameters
- `scheduler.select_task()` is the only place that decides task eligibility
- Backend is pure state persistence; no business logic

**Files:**

- `antfarm/core/scheduler.py` — all filters live here (deps, caps, pin, rate limit, scope, hotspot weighting)
- `antfarm/core/backends/file.py` — `pull()` becomes: read ready tasks, read active tasks, read worker info, call scheduler, move winner

**Tests:**

- Changing scheduling policy in `scheduler.py` changes `pull()` behavior
- No backend-specific selection logic remains
- Active tasks are loaded and passed to scheduler for scope preference

**Complexity:** M

---

### 2. Structured Task Output Contract (#77)

**Problem:** Workers currently harvest with branch / PR information, but Antfarm lacks a structured, deterministic artifact for merge gating and review.

**Solution:** Define a `TaskArtifact` with a strict split between hard evidence and advisory commentary.

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

    # Change facts
    files_changed: list[str]
    lines_added: int
    lines_removed: int

    # Verification facts
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

### Artifact Rules

- `base_commit_sha`, `head_commit_sha`, and `target_branch_sha_at_harvest` are required for Soldier freshness checks
- `summary`, `risks`, and `review_focus` are optional and may be AI-generated
- Soldier may display advisory fields but may not gate on them
- `merge_readiness="ready"` is necessary but not sufficient

### Soldier Gating Rules

Soldier may merge only if:

- all dependencies are satisfied
- artifact exists and is valid
- branch freshness policy passes (base SHA check)
- required verification signals passed
- no blocking reasons remain
- no serialization rule prevents merge

### Freshness Policy

A branch is considered **fresh** if `target_branch_sha_at_harvest` still matches the current target branch HEAD at merge time. If they differ (another task merged since harvest), the task is `KICKED_BACK` for rebase and re-validation. Soldier does not merge optimistically on stale branches.

### Idempotency

- Repeated harvest calls for the same task+attempt must be safe (no-op if already harvested)
- Repeated Soldier evaluations must not duplicate merges or PR comments
- Retries after partial failure must not corrupt task state
- Recovery paths must be safe to run multiple times

### Operator Override Boundaries

Operators may:
- Override `needs_review` → `ready`
- Requeue, pause, reassign, or unblock tasks
- Force-harvest a stale task

Operators may NOT (without explicit audit trail):
- Bypass a missing artifact
- Merge a task with unknown freshness
- Skip verification checks

Every operator override is recorded as a trail entry with the operator's identity and reason.

**Integration:**

- `mark_harvested()` accepts artifact dict alongside existing params
- Artifact is stored on the attempt in task state
- Worker collects deterministic fields automatically (git diff stat, SHAs, test results)
- AI-generated fields filled by Claude Code adapter, empty for generic
- TUI and CLI display artifact summary for completed tasks

**Files:**

- `antfarm/core/models.py` — add TaskArtifact dataclass
- `antfarm/core/worker.py` — build artifact after agent completes
- `antfarm/core/backends/base.py` — update mark_harvested() signature
- `antfarm/core/backends/file.py` — store artifact on attempt
- `antfarm/core/soldier.py` — artifact gating + freshness check
- `antfarm/core/tui.py` — display artifact in TUI

**Complexity:** M

---

### 3. Failure Taxonomy + Retry Policy (#83)

**Problem:** All failures currently look too similar. A flaky infrastructure issue should not be treated the same way as a real implementation bug.

**Solution:** Classify failures and attach default system behavior.

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

### Default Retry Policy

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

### FailureRecord Shape

Since "artifact or failure record" is an invariant, both sides must be equally concrete:

```python
@dataclass
class FailureRecord:
    task_id: str
    attempt_id: str
    worker_id: str
    failure_type: FailureType
    message: str
    retryable: bool
    captured_at: str                # ISO 8601
    stderr_summary: str             # first 500 chars of stderr
    verification_snapshot: dict     # partial artifact data if available
    recommended_action: str         # "retry", "kickback", "escalate", "quarantine_worker"
```

**Integration:**

- Worker classifies failure based on exit code, stderr, git state, and verification signals
- Failure type is stored on attempt as `FailureRecord`
- Soldier and recovery logic use failure type to decide retry vs kickback vs escalation
- Memory tracks failure patterns by type
- TUI inbox shows failure type and recommended action

**Files:**

- `antfarm/core/models.py` — add FailureType enum, FailureRecord, failure_type on Attempt
- `antfarm/core/worker.py` — classify failure after agent exit
- `antfarm/core/soldier.py` — use failure type in retry/kickback decision
- `antfarm/core/memory.py` — track failure patterns by type

**Complexity:** S

---

## P1 Features (Strongly Recommended)

### 4. Lightweight Repo Memory (#78)

**Problem:** Every task starts from zero. Workers rediscover build commands, test commands, hot files, and past failures.

**Solution:** JSONL-based memory in `.antfarm/memory/`.

```
.antfarm/memory/
  repo_facts.json          # TRUSTED: operator-curated + auto-detected durable facts
  task_outcomes.jsonl       # APPEND-ONLY: run history
  hotspots.json             # HEURISTIC: computed from outcomes, may be noisy
  failure_patterns.json     # HEURISTIC: derived failure clusters
  touch_observations.jsonl  # HEURISTIC: actual files/scopes touched by completed tasks
```

### Memory Trust Model

- `repo_facts.json` contains **trusted facts**: build command, test command, language, framework. Operator-curated and auto-detected.
- `task_outcomes.jsonl` is **append-only run history** — factual record of what happened.
- `hotspots.json`, `failure_patterns.json`, and `touch_observations.jsonl` are **heuristics derived from prior runs** and may be noisy.

### How It's Populated

- `repo_facts.json` — detect from repo structure (pyproject.toml, package.json, Makefile), then allow manual edits via `antfarm memory set-fact`
- `task_outcomes.jsonl` — append after every completed or failed attempt
- `hotspots.json` — computed from recent failed and conflicting work
- `failure_patterns.json` — grouped recurring failure causes
- `touch_observations.jsonl` — generated from completed task artifacts (actual files changed)

### How It's Consumed

- Workers receive `repo_facts` as execution context (trusted facts only, not heuristics)
- Planner uses hotspots and observed touches to improve decomposition and `touches` prediction
- Scheduler uses hotspots as a weighting signal, not a hard ban
- TUI shows hotspot warnings and recent repeated failures

**Files:**

- NEW: `antfarm/core/memory.py` — MemoryStore class (~150 lines)
- `antfarm/core/worker.py` — inject repo_facts into agent prompt, record outcome after harvest
- `antfarm/core/cli.py` — `antfarm memory show`, `antfarm memory set-fact <key> <value>`

**Complexity:** M

---

### 5. Conflict Prevention Layer (#80)

**Problem:** Two workers can claim tasks touching the same areas. Conflicts are found late instead of prevented early.

**Solution:** Add conflict awareness to planning, carry, and scheduling.

> `touches` are predictive hints, not exact file locks. Antfarm uses them to reduce conflict likelihood, not to claim perfect conflict freedom. Completed task artifacts feed observed changed files back into memory so future `touches` predictions improve over time.

### 5a. Overlap Warnings at Carry Time

When a new task is carried, warn if it overlaps active task scopes.

```bash
antfarm carry --title "Update auth" --touches "api,auth"
# WARNING: Active task "Build login" also touches: api, auth
# Likely conflict. Carry anyway? [y/N]
```

### 5b. Hotspot Detection

- Files or scopes that frequently correlate with failed or conflicting work are marked hot
- Scheduler prefers serialization on hot scopes
- TUI surfaces hotspot warnings

### 5c. Module Claim Hints

- When a worker forages, scheduler records which scopes are effectively claimed
- Later forage calls prefer non-overlapping work
- Claims remain soft guidance, not hard locks

### 5d. Conflict Risk Score

Each task gets a `conflict_risk: float` based on:

- overlap with active tasks
- whether touches map to hotspots
- whether dependencies are actively changing nearby areas

TUI shows conflict risk. Operator can decide to pause risky tasks.

**Files:**

- `antfarm/core/scheduler.py` — enhanced scope overlap + hotspot weighting
- `antfarm/core/memory.py` — hotspot tracking + touch observation feedback
- `antfarm/core/cli.py` — overlap warning on carry
- `antfarm/core/tui.py` — conflict risk display

**Complexity:** M

---

### 6. Operator Inbox in TUI (#81)

**Problem:** Status is visible, but what needs attention is not obvious.

**Solution:** Add an inbox panel to surface:

- kicked-back tasks — **why:** stale base / test failure / merge conflict
- stale workers — **why:** worker_id, last heartbeat time
- blocked tasks — **why:** which dep is blocking
- high conflict-risk tasks — **why:** overlapping touches with active tasks
- failed harvest attempts — **why:** failure type classification
- tasks active for too long — **why:** duration, worker_id
- tasks waiting on human input or override — **why:** signal content

Each item explains: what happened, what likely action is needed, whether action is optional or urgent.

**Files:**

- `antfarm/core/tui.py` — add inbox panel
- `antfarm/core/cli.py` — `antfarm inbox` as standalone command (non-TUI)

**Complexity:** S-M

---

### 7. Review Pack Generation (#82)

**Problem:** Reviewers get raw diffs without enough context.

**Solution:** Generate a review pack from the `TaskArtifact`.

```markdown
## Review Pack: task-001 "Build auth middleware"

### Summary
Added JWT auth middleware with token validation and route protection.

### Files Changed (4 files, +120 -5)
- antfarm/core/auth.py (new, 81 lines)
- antfarm/core/serve.py (modified, +13 -2)
- tests/test_auth.py (new, 220 lines)

### Checks
- Build: passed ✓
- Tests: 155 passed, 0 failed ✓
- Lint: clean ✓
- Base SHA: fresh against target ✓

### Risks
- Token printed at colony startup (log exposure risk)

### Suggested Review Focus
- auth.py: verify token validation logic
- serve.py: middleware bypass for status endpoints
```

**Integration:**

- Generated from artifact deterministic fields
- Displayed in TUI / CLI for done tasks
- Optionally posted as PR comment for GitHub workflows

**Files:**

- NEW: `antfarm/core/review_pack.py` — generate_review_pack(artifact) -> str
- `antfarm/core/soldier.py` — post review pack as PR comment before merge decision
- `antfarm/core/tui.py` — display review pack for selected task

**Complexity:** S

---

### 8. Planner / Decomposer (#79)

**Problem:** Tasks are created manually via `antfarm carry`. There is no assisted decomposition from specs or issues.

> `antfarm plan` is optional. Manual `antfarm carry` remains a first-class workflow.

**Solution:** Add `antfarm plan`.

```bash
antfarm plan --spec "Build user authentication with JWT login, logout, and profile endpoints"
antfarm plan --file feature_spec.md
antfarm plan --issue 42 --repo owner/repo
```

### Output

```
Proposed tasks:
  1. [api] JWT auth middleware       touches: api,auth   deps: none
  2. [api] Login endpoint            touches: api,auth   deps: 1
  3. [api] Logout endpoint           touches: api,auth   deps: 1
  4. [api] Profile endpoint          touches: api        deps: 1
  5. [test] Auth integration tests   touches: tests,auth deps: 2,3,4

Conflict warnings:
  - Tasks 2,3,4 all touch api/auth — consider serializing
  - Task 1 is a dependency for all others — merge first

Carry these tasks? [y/N]
```

### Planner Rules

- `antfarm plan` is optional; manual `antfarm carry` remains first-class
- Planner proposes tasks into the same schema used by manual carry
- Planner may use AI to produce a first draft
- Planner is informed by `repo_facts`, hotspots, and `touch_observations`
- Planner output is validated before carry

### Implementation

For v0.5, planner can remain simple:

- Prompt an AI tool with a strict schema
- Parse structured output
- Validate `depends_on`, `touches`, and task shape
- Let operator approve before carry

**Files:**

- NEW: `antfarm/core/planner.py` — PlannerEngine class
- `antfarm/core/cli.py` — `antfarm plan` command

**Complexity:** L

---

### 9. Docs Rewrite (#73)

**Problem:** Docs describe an earlier version of the system and understate what Antfarm is now.

**Solution:**

- README: what Antfarm is today
- Architecture doc: scheduler, worker, Soldier, memory, TUI, backends
- Operator guide: day-to-day use, monitoring, recovery, troubleshooting
- Contributor guide: how to extend safely
- Archive / deprecate the frozen v0.1 framing

**Complexity:** M

---

## Release Slices

**Top-level v0.5 goal:** Close the autonomous loop so Antfarm can be used for real development without manual review. Review integration is the #1 priority — but it depends on runtime truth and artifacts being in place first.

### v0.5.1 — Runtime Foundation

Make the runtime deterministic and observable. Everything else builds on this.

- Canonical scheduler (#72) — single scheduling brain
- Task / attempt lifecycle + invariants (new states, transition rules)
- Failure taxonomy + default retry policy (#83)
- Initial inbox surfacing for stale / blocked / failed work (#81 partial)

### v0.5.2 — Artifact + Review Contract

Define the evidence contracts that Soldier needs for safe merging and review gating.

- Structured task output contract (#77) — hard evidence + advisory split, base/head SHA, freshness
- ReviewVerdict contract (#85) — provider, verdict, findings, reviewed_commit_sha
- Soldier gating — artifact exists + fresh + tests passed + review verdict pass
- Review pack generation (#82)

### v0.5.3 — Review Execution (THE GOAL)

Close the autonomous loop. This is why v0.5 exists.

- Review-as-task flow — Soldier creates review task → reviewer worker produces verdict → Soldier merges if pass + fresh
- Reviewer agent definitions for Claude Code and Codex adapters
- Merge only after fresh pass verdict
- End-to-end test: carry → build → review → merge without human intervention

**Why this is the goal:** Every other improvement is wasted if review is still manual. Once this ships, Antfarm can be used on BrahmaOS.

### v0.5.4 — Memory + Prevention

Make parallelism smarter with data.

- Repo memory (#78) — trusted facts + heuristic observations
- Conflict prevention layer (#80) — overlap warnings, hotspots, claim hints
- Touch observation feedback loop (artifacts → memory → better touches)

### v0.5.5 — Planning

Add AI-assisted planning on top of a stable substrate.

- Planner / decomposer (#79) — `antfarm plan` CLI
- Planner informed by repo facts + hotspots + observed touches
- Conflict warnings in plan output

### v0.5.6 — Docs + Polish

- Docs rewrite (#73)
- Audit trail (#75)
- Bug fixes from testing

### v0.5.75 — TUI Pipeline Redesign

Redesign the TUI to show the full review pipeline, not just ready/active/done.

**Problem:** The v0.5.0 TUI collapses "done" into one bucket. The review pipeline (v0.5.3) is invisible — operators can't see tasks awaiting review, under review, or merge-ready. During dogfooding, 9 tasks completed silently with no visibility into the review stage gap.

**Solution:** 7 panels in pipeline-flow order (top to bottom: waiting → building → review → merge → done). Colony summary and Workers stacked full-width at top. Every panel shows task count in title and time-in-queue per task. Overflow shows "+N more — run: antfarm inbox".

- **Colony Summary** — nodes with hostnames, Soldier status, review queue pressure, progress bar (merged/total), pipeline distribution bar
- **Workers** — full-width table: Worker, Node, Status, Type (builder/reviewer), Rate Limit
- **Waiting: New | Rework** — side-by-side sub-panels. New = fresh tasks from carry. Rework = kicked-back tasks with review findings. Rework items are higher priority (already been through the pipeline).
- **Building** — active implementation tasks with worker name, trail message, and elapsed time
- **Awaiting Review | Under Review** — side-by-side. Awaiting = done tasks with no reviewer yet. Under Review = reviewer worker actively processing.
- **Planning** — plan tasks being actively decomposed by planner workers
- **Merge Ready** — tasks with passing verdict, fresh SHA, deps satisfied
- **Recently Merged** — compact horizontal layout, last N merged tasks with time since merged

TUI mockup:

```
┌──────── Antfarm Colony ─────────────────────────────────────────────┐
│  Nodes (2): mini-1, mini-2          Soldier: active                │
│  Reviews: 2 waiting, 1 active                                      │
│                                                                     │
│  Progress  [██████████░░░░░░] 5/10 merged                          │
│  Pipeline  ████ ██ ░░ ██ ██                                        │
│            wt:3 bld:2 rev:2 mrg:2                                   │
├──────── Workers (3) ────────────────────────────────────────────────┤
│  Worker            Node     Status   Type       Rate Limit          │
│  mini-1/builder    mini-1   active   builder    ok                  │
│  mini-2/builder    mini-2   active   builder    ok                  │
│  mini-2/reviewer   mini-2   active   reviewer   ok                  │
├──── Waiting: New (3) ───────────────┬──── Waiting: Rework (2) ──────┤
│  task-login     [M] api       2h   │  task-auth-v2  ❌ SQL inj 15m │
│  task-tests     [S] tests     2h   │  task-cache-v2 ❌ no test 10m │
│  task-api       [M] api       1h   │                                │
├──────── Planning (1) ────────────────────────────────────────────────┤
│  ID              Title                  Worker       Trail     Time │
│  plan-v058       Plan v0.5.8 planner..  mini-2/plnr  decomp.. 2m   │
├──────── Building (2) ───────────────┴───────────────────────────────┤
│  ID              Title                  Worker       Trail     Time │
│  task-auth       Add JWT auth midlw..   mini-1/bldr  routes    3m  │
│  task-cache      Add Redis caching..    mini-2/bldr  tests     1m  │
├──── Awaiting Review (9) ────────┬──── Under Review (1) ─────────────┤
│  task-models  ⏳ waiting   45m  │  review-task-api                   │
│  task-schema  ⏳ waiting   30m  │    ← mini-2/reviewer         2m   │
│  task-db      ⏳ waiting   25m  │                                    │
│  task-utils   ⏳ waiting   20m  │                                    │
│  task-api     ⏳ waiting   15m  │                                    │
│  +4 more — run: antfarm inbox   │                                    │
├──── Merge Ready (2) ─────────────────────────────────────────────────┤
│  task-config  ✅ pass       5m     task-utils   ✅ pass       2m    │
├──────── Recently Merged (3) ────────────────────────────────────────┤
│  task-init   ✅ 2m ago    task-setup  ✅ 5m ago    task-db  ✅ 8m  │
└─────────────────────────────────────────────────────────────────────┘
```

**Panel sizing:** Awaiting Review gets the most vertical space (likely bottleneck — tasks wait here for reviewer capacity). Building gets second most (active work with trail info). Waiting, Merge, and Recently Merged are compact. Workers panel grows with worker count.

**Review task identity:** Review tasks are attempt-scoped, not task-scoped. Naming convention: `review-{task_id}-{attempt_id}`. This prevents ambiguity after kickback and rework — each attempt gets its own review.

**Derived views (no model changes needed for v0.5.75):**
- Waiting: New = `status=="ready"` + no superseded attempts
- Waiting: Rework = `status=="ready"` + has superseded attempt + kickback trail
- Planning = `status=="active"` + `"plan" in capabilities_required`
- Building = `status=="active"` + not a review or plan task
- Awaiting Review = `status=="done"` + no `review_verdict` + not a review task
- Under Review = `review-*` task exists + `status=="active"`
- Merge Ready = `review_verdict` exists + `verdict=="pass"` (tasks not yet merge-eligible simply don't appear)
- Recently Merged = any task with a merged attempt (last N)

**Time in queue:** Every panel shows how long each task has been in its current stage — not total lifetime, just time since entering the stage. Long wait times signal bottlenecks (e.g., `45m` in Awaiting Review = no reviewer capacity).

**Overflow:** Panels show top N tasks by time-in-queue (longest first). If more exist, show `+N more — run: antfarm inbox`.

**Note:** These heuristic views are sufficient for v0.5.75. If rework cycles reveal ambiguity, v0.5.78+ can add explicit `review_for_attempt_id` fields to the model.

**Files:** `antfarm/core/tui.py` (rewrite), `antfarm/core/serve.py` (add soldier status to /status/full)

**Complexity:** M

### v0.5.76 — Review Pipeline Operational

Fix all bugs found during dogfooding that prevent the review pipeline from working end-to-end. The v0.5.3 review code exists but doesn't function in practice.

**Bugs to fix (found during dogfooding):**

| Issue | Bug | Why it breaks the pipeline |
|-------|-----|--------------------------|
| #99 | Soldier not auto-started with colony | Review tasks never created |
| #100 | No notification on task completion | Operator unaware tasks finished |
| #101 | Workers exit silently when queue empties | Can't tell if worker died or finished |
| #102 | Review flow needs turnkey setup | Too many manual steps |
| #103 | Workers never create GitHub PRs | Reviewer has nothing to review |
| #104 | Workers never build TaskArtifact | Soldier can't gate on freshness/merge readiness |
| #105 | Zero trail entries during execution | No progress visibility |
| #91 | recompute_hotspots() never called automatically | Hotspot weighting is a no-op |
| #92 | MemoryStore uses wrong data_dir when backend injected | Overlap warnings silently fail |
| #93 | plan --carry does not resolve index dependencies | Carried tasks permanently blocked |

**Changes:**

1. **Colony auto-starts Soldier thread** (#99) — Soldier runs as a singleton daemon thread inside the colony process. Only one Soldier loop may be active per colony. Review-task creation is idempotent (check before create). Soldier health surfaced in `/status/full`. `--no-soldier` flag disables.

2. **Worker builds TaskArtifact before harvest** (#104) — After agent completes, worker collects git diff stats, SHAs, files_changed, lines_added/removed, and builds a TaskArtifact dict. Passed to `harvest(artifact=...)`.

3. **Worker produces a reviewable change handle** (#103) — On GitHub backend, worker creates a PR via `gh pr create`. On file/local backend, the branch + diff summary serves as the reviewable handle. PR URL (or branch ref) stored in harvest. Implementation starts GitHub-only but the interface is backend-agnostic.

4. **Worker produces trail entries** (#105) — Worker trails at key lifecycle points: "task claimed", "workspace ready", "agent running", "agent completed (Ns)", "harvesting". Heartbeat thread optionally trails periodic status.

5. **Worker announces exit** (#101) — On queue empty, worker explicitly transitions status to offline and deregisters. If a last task exists, trail "worker exiting, queue empty". If worker starts and finds nothing, still deregisters cleanly. TUI distinguishes intentional exit from crash (offline vs stale heartbeat).

6. **Task completion notifications** (#100) — Colony emits SSE events on `/events` when task status changes. Notifications are informational only — they do not drive truth. System correctness must not depend on SSE delivery. TUI subscribes for live updates. Optional webhook URL in colony config.

7. **Turnkey review setup** (#102) — `antfarm colony` auto-starts Soldier thread. `antfarm worker start --type reviewer` registers with `capabilities=["review"]`. Single command per role.

8. **Review task identity is attempt-scoped** — Review tasks use naming convention `review-{task_id}-{attempt_id}`. Soldier creates one review task per attempt, not per task. Idempotent: checks for existing review task before creating.

9. **Soldier writes merge block reason** — When Soldier evaluates a done task and finds it not mergeable (stale SHA, unsatisfied deps, missing artifact), it writes a `merge_block_reason` string on the attempt. The TUI displays this reason rather than guessing merge readiness.

10. **Soldier.from_backend()** — Soldier gains a classmethod that operates directly on the backend, avoiding HTTP loopback. Used by the colony's auto-start thread.

**Invariants:**
- Only one Soldier loop active per colony process (singleton)
- Review-task creation is idempotent (no duplicates on restart)
- Soldier restart is safe — resumes from current state
- Notifications are advisory, not authoritative
- TUI does not guess merge readiness — it displays Soldier-produced state

**Files:** `antfarm/core/worker.py`, `antfarm/core/soldier.py`, `antfarm/core/serve.py`, `antfarm/core/cli.py`

**Complexity:** L

### v0.5.77 — Dogfood Validation

Zero new code. Run the system end-to-end and validate the full pipeline works.

1. Restart colony with v0.5.76 code (Soldier auto-starts)
2. Carry fresh tasks (re-run the TUI + bug fix tasks from earlier, or new work)
3. Start builder workers on mini-1 + mini-2
4. Start reviewer worker on mini-2
5. Watch in the v0.5.75 TUI as tasks flow: Backlog → Building → Awaiting Review → Under Review → Merge Ready → Merged
6. Verify: artifacts exist, PRs created, trail entries visible, review verdicts produced, Soldier merges automatically
7. Any bugs found become v0.5.78

**Success criteria:**
- Tasks flow through the entire pipeline without manual intervention
- The TUI shows every stage accurately
- No silent failures — every state change is visible
- No task remains in Awaiting Review longer than 60s without a visible reason (no reviewer, Soldier down, etc.)
- No duplicate review tasks or duplicate merges during restart/recovery
- Worker exit is distinguishable from worker crash in the TUI

### v0.5.8 — Planner Worker

Complete the autonomous pipeline by adding a planner worker that decomposes specs into tasks. Removes the human from the front of the pipeline.

**Problem:** Currently, a human must manually decompose work and run `antfarm carry` for each task. The `antfarm plan` CLI exists but requires human interaction (run, review, confirm). The pipeline starts at "tasks exist" — someone has to create them.

**Solution:** A planner worker that forages "plan tasks" (high-level specs), decomposes them into sub-tasks using the AI agent, and auto-carries them into the colony.

**Pipeline becomes fully autonomous:**
```
Current:   [Human carries tasks] → Builder → Reviewer → Soldier merge
With v0.5.8:
    Spec → Planner → Tasks → Builder → Reviewer → Soldier merge
```

**How it works:**

1. Operator creates a plan task:
```bash
antfarm carry --type plan --title "Auth system" \
  --spec "Build JWT auth with login, logout, profile endpoints"
```
Creates task with `id="plan-{id}"`, `capabilities_required=["plan"]`.

2. Planner worker forages the plan task (`antfarm worker start --type planner`).

3. The worker's claude-code agent receives the spec as prompt with instructions to output a JSON array of tasks (same schema as PlannerEngine). The agent has full repo context from the worktree.

4. Worker parses the agent's JSON output, validates (deps, touches, complexity), resolves index-based dependencies to actual task IDs.

5. Worker auto-carries each sub-task via the colony API (`self.colony.carry()`).

6. Worker harvests the plan task with an artifact listing the created task IDs.

7. Builder workers forage the sub-tasks → normal build → review → merge flow.

**Lineage metadata:** Child tasks carry `spawned_by` field linking back to the plan task:
- `spawned_by: {"task_id": "plan-auth", "attempt_id": "att-001"}`
- TUI can show "this plan created 6 tasks"
- Retries and rework traceable to the originating plan

**Deterministic child IDs:** Child task IDs are derived from the parent plan task ID:
- `{plan_task_id}-01`, `{plan_task_id}-02`, etc.
- Example: `plan-auth-01`, `plan-auth-02`, `plan-auth-03`
- Idempotent: if planner crashes after carrying 4 of 6 tasks and retries, duplicate IDs are rejected by `carry()` (409), remaining tasks are carried. No duplicates.

**Planner worker details:**
- `antfarm worker start --type planner` — registers with `capabilities=["plan"]`
- Scheduler restricts planner workers to only forage plan tasks (same pattern as reviewer)
- Agent prompt includes repo context from memory (repo_facts, hotspots, touch_observations)
- Output is parsed from `[PLAN_RESULT]...[/PLAN_RESULT]` tags in agent stdout (same pattern as `[REVIEW_VERDICT]`)
- Raw agent output is validated and dependency-resolved by shared `PlannerEngine` logic (validation, cycle detection, conflict warnings) — not duplicated in worker
- Planner worker reuses: `PlannerEngine._parse_tasks()`, `_validate_tasks()`, `_detect_cycles()`, `_generate_warnings()`, `resolve_dependencies()`

**Guardrails (deterministic, not AI-gated):**
- Max 10 child tasks per plan (configurable, prevents colony flooding)
- No recursive plan spawning — child tasks cannot have `capabilities_required=["plan"]`
- Dependency graph must be acyclic (validated by `_detect_cycles()`)
- Every child task must have non-empty title and spec
- Duplicate task IDs rejected (carry returns 409)
- Child tasks default to implementation — never review/test/plan unless explicitly configured
- Operator approval is the act of creating the plan task. Planner autonomy is bounded by these deterministic rules.

**Plan task harvest artifact:**
```python
{
    "plan_task_id": "plan-auth",
    "created_task_ids": ["plan-auth-01", "plan-auth-02", ...],
    "task_count": 6,
    "warnings": ["Tasks 2,3 overlap on 'api' scope"],
    "dependency_summary": "plan-auth-01 → plan-auth-02, plan-auth-03",
}
```

**Input sources for plan tasks:**
```bash
# Inline spec
antfarm carry --type plan --title "Auth" --spec "Build JWT auth..."

# From file
antfarm carry --type plan --title "Auth" --spec "$(cat auth-spec.md)"

# From GitHub issue (operator copies issue body into spec)
antfarm carry --type plan --title "Auth" --spec "..." --issue 42
```

**TUI visibility:**
- Plan tasks show in Waiting: New / Building with worker type "planner"
- Sub-tasks created by planner appear in Waiting: New immediately
- Plan task in Recently Merged shows "spawned N tasks" badge
- No separate "Planning" panel — planner is just another worker type

**What the planner worker does NOT do:**
- Does not approve its own plan — operator approved by carrying the plan task
- Does not modify code — it only creates tasks
- Does not use nested `PlannerEngine._call_agent()` subprocess — the worker's own agent does the planning, but output is validated by shared PlannerEngine logic
- Does not create review or test tasks — those are Soldier's responsibility after builders complete
- Does not spawn recursive plan tasks — child tasks are always implementation tasks

**Files to change:**
- `antfarm/core/worker.py` — planner mode in `_launch_agent()` and `_process_one_task()`
- `antfarm/core/planner.py` — refactor validation into reusable functions (already mostly there)
- `antfarm/core/cli.py` — `--type plan` in carry, `planner` worker type
- `antfarm/core/scheduler.py` — planner workers only forage plan tasks
- `antfarm/core/tui.py` — plan task badge, spawned count
- `tests/` — planner worker flow tests, idempotent retry test, guardrail tests

**Complexity:** M

### v0.5.9 — Tester Worker (future)

Add a **tester** worker type that independently verifies builder branches (#106). Acts as CI inside antfarm.

- `antfarm worker start --type tester` — registers with `capabilities=["test"]`
- Soldier creates test tasks: `test-{task_id}-{attempt_id}`
- Tester checks out branch, runs tests/lint/build, populates artifact verification fields
- Does NOT modify code — read-only execution
- Pipeline becomes: planner → builder → tester → reviewer → soldier merge

This completes the artifact verification story — builders can't self-certify.

**Why this order:**
1. v0.5.75 (TUI) first — see the pipeline ✅
2. v0.5.76 (pipeline fixes) — make review flow work ✅
3. v0.5.77 (validation) — prove end-to-end ✅
4. v0.5.8 (planner worker) — remove human from front of pipeline
5. v0.5.9 (tester worker) — add independent verification

---

## Success Criteria

### Scenario A: Runtime Integrity

Given worker death, stale heartbeats, or interrupted harvest, Antfarm recovers the task without silent corruption or manual JSON edits. Every stuck task has an explanation visible in the inbox.

### Scenario B: Safe Parallel Execution

With 3 workers on one repo, Antfarm materially reduces preventable overlap by preferring non-conflicting tasks and surfacing conflict risk early. Stale tasks are recovered automatically.

### Scenario C: Reviewable Output

Every completed attempt produces either:

- a valid `TaskArtifact`, or
- a classified `FailureRecord`

Review packs are generated from artifacts, not raw diff guessing.

### Scenario D: Deterministic Merging

Soldier merges only when:

- dependencies are satisfied
- artifact is valid
- freshness checks pass
- required verification checks pass
- `merge_readiness == "ready"`
- no blocking reasons remain

### Scenario F: Review Orchestration

Given a completed task with artifact, Soldier creates a review task, a reviewer worker (Claude Code or Codex) produces a fresh `ReviewVerdict`, and Soldier merges only if verdict is `pass` and `reviewed_commit_sha` matches current `head_commit_sha`. No human intervention required.

### Scenario G: Operational Liveness

Given completed implementation tasks and at least one active reviewer worker, the colony does not go silent. Within a bounded time, each completed task is either: assigned a review task, explicitly shown as waiting for reviewer capacity, kicked back with findings, or moved to merge-ready / merged. No task stalls invisibly.

### Scenario H: Autonomous Planning

Given a high-level spec carried as a plan task, the planner worker decomposes it into parallelizable sub-tasks with correct dependencies and touches, auto-carries them, and builder workers begin foraging immediately. The operator's only action is `antfarm carry --type plan`. No manual task decomposition, no manual carry of sub-tasks.

### Scenario E: Useful Memory

On the second run in the same repo, workers reuse repo facts, scheduler benefits from hotspot data, and planner proposes better `touches` based on prior observed changes.

---

## Review Integration Contract

Code review is Claude Code's or Codex's job. Review orchestration and merge policy is Antfarm's job.

### ReviewVerdict

```python
@dataclass
class ReviewVerdict:
    provider: str                    # "claude_code", "codex", "human"
    verdict: str                     # "pass", "needs_changes", "blocked"
    summary: str
    findings: list[str]
    severity: str | None             # "low", "medium", "high", "critical"
    reviewed_commit_sha: str         # must match head_commit_sha in artifact
    reviewer_run_id: str | None
```

### Soldier Review Gating

Soldier gates on review as deterministic evidence (not AI judgment):

- Review verdict exists on the attempt
- Verdict is `pass`
- `reviewed_commit_sha` matches current `head_commit_sha` (review is fresh)
- No critical findings remain

### How Review Happens

Antfarm does not perform reviews. It triggers and consumes them:

1. Worker harvests → task is DONE with artifact
2. Soldier sees DONE task → creates a **review task** in the queue
3. A reviewer worker (Claude Code or Codex) forages the review task
4. Reviewer reads the PR diff, posts comments, produces ReviewVerdict
5. Reviewer harvests the review task with the verdict
6. Soldier reads the verdict → if PASS + fresh SHA → merge

This makes review a task like any other — Antfarm orchestrates, agents execute.

### Implementation Note

ReviewVerdict is defined in v0.5 but implementation lands in v0.5.x or v0.6, depending on whether the Claude plugin (MCP + slash commands) ships first. The contract is frozen now so Soldier can be built to expect it.

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

1. ~~Untracked `redis.py` + `test_redis_backend.py`~~ — removed from tracking in v0.5.0
2. Branch protection CI check `test` never runs — fix GitHub Actions or relax check
3. Engineer self-merge prevention — add to CLAUDE.md guardrails
4. ~~Multiple scheduling brains~~ — resolved in v0.5.1 (canonical scheduler)
5. ~~TUI rendering bug~~ — fixed in v0.5.6 (current_attempt lookup)
6. Worker doesn't auto-register node (#98) — fixed in v0.5.0
7. Review pipeline not operational (#99-#105) — 7 bugs found during dogfooding, planned for v0.5.76
8. Soldier singleton / duplicate review protection — auto-start requires idempotent review-task creation and merge processing
9. Review task attempt linkage — review must link to attempt, not only task (naming: `review-{task_id}-{attempt_id}`)
10. Merge queue TUI semantics — separate merge-ready from merge-blocked/stale

---

## Summary

Antfarm v0.5 should not become broader. It should become more trustworthy.

The order of operations is:

1. Make scheduling singular (v0.5.1) ✅
2. Make task completion explicit and reviewable (v0.5.2) ✅
3. Make merge gating deterministic and freshness-aware (v0.5.3) ✅
4. Make memory lightweight and useful (v0.5.4) ✅
5. Add planning on top of a stable runtime (v0.5.5) ✅
6. Polish and release (v0.5.6) ✅
7. Make the pipeline visible to operators (v0.5.75) ✅
8. Make the review pipeline actually work in production (v0.5.76) ✅
9. Validate end-to-end with real dogfooding (v0.5.77) ✅
10. Remove the human from the front of the pipeline (v0.5.8)
11. Add independent test verification (v0.5.9)

v0.5.0 shipped the core architecture. v0.5.75-v0.5.77 made the review pipeline visible, operational, and dogfood-validated. v0.5.8 completes the autonomous loop — spec in, merged code out.
