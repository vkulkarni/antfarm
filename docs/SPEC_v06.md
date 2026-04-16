# Antfarm v0.6 — Autonomous Runs

**Status:** FROZEN — approved for implementation
**Date:** 2026-04-07
**Prerequisite:** v0.5.8 shipped (planner pipeline, review-as-task, structured artifacts, repo memory, conflict prevention)
**Goal:** Spec in, merged code out, zero human intervention. You submit a mission, go to sleep, read the report in the morning.

---

## Philosophy

**Antfarm v0.5 orchestrates tasks. Antfarm v0.6 orchestrates missions.**

A mission is a complete unit of work: one spec, one plan, all implementation tasks, all reviews, all merges, one report. The system plans, builds, reviews, merges, recovers from failures, scales workers, and reports results — autonomously.

**Core principles:**

1. **Safety before scale** — the system must stop looping, stop stalling, and stop wasting cycles before it scales to more workers
2. **Durable state, not events** — orchestration reads persisted backend state, not SSE streams. Crash-recoverable, idempotent.
3. **Mission is the primitive** — every task belongs to a mission. The mission is the answer to "is this done?" and "what do I read in the morning?"
4. **Controller coordinates, subsystems decide** — the Queen advances mission phases. She does not plan, build, review, merge, or scale. Those stay in their own components.
5. **Best-effort completion** — a mission is "done" when no more forward progress is possible. Not all-or-nothing.

---

## What v0.6 IS

1. **Mission model** — first-class entity tying spec → plan → tasks → report
2. **Queen** — thin controller that advances missions through phases
3. **Safety hardening** — max-attempt enforcement, cascade invalidation, doctor daemon
4. **Autoscaler** — starts/stops workers based on queue depth and scope overlap
5. **Morning digest** — structured mission report: what merged, what's blocked, what needs you
6. **Plan review** — reviewer validates the plan before builders burn API credits

## What v0.6 IS NOT

- Not an AI meta-agent (Queen is deterministic, like Soldier)
- Not a web dashboard (TUI scout still works)
- Not multi-node orchestration (single-host autoscaler first; multi-node is v0.6.1)
- Not a Claude Code plugin (that's v0.7)

---

## Version Plan

| Version | Theme | Scope |
|---------|-------|-------|
| **v0.5.9** | Safety hardening | Max attempts + `blocked`, doctor daemon, cascade invalidation |
| **v0.6.0** | Autonomous runs | Mission model, Queen controller, plan review, single-host autoscaler, morning digest |
| **v0.6.1** | Scale + integration | Runner, multi-node autoscaler, prompt cache sharing |
| **v0.6.2** | Claude Code plugin | MCP server, slash commands, hooks (full spec: SPEC_v06_plugin.md) |

---

## Non-Negotiable Invariants

These rules apply to ALL v0.6 work. Violations are bugs.

1. **No silent stalls.** A mission must never remain in a non-terminal state without either task progress or a visible blocking reason beyond a configured threshold (default: 30 minutes).

2. **No infinite loops.** Every task has a max attempt count (default: 3). After exhaustion, the task transitions to `blocked` with a reason. It never re-enters `ready`.

3. **No orphaned work.** Every task has a `mission_id`. Every mission tracks its `task_ids`. Reverse linkage is always consistent.

4. **No SSE-driven orchestration.** SSE is for UI/notifications only. Queen and Autoscaler read durable backend state. They must produce correct behavior after a crash and restart with no event replay.

5. **No monolithic controller.** Queen coordinates phases. She does NOT contain planner logic, review logic, merge logic, scheduling logic, or scaling logic. Those remain in their existing components.

6. **Cascade is dependency-based, not scope-based.** When a task is kicked back, only its `depends_on` descendants are invalidated. Scope overlap between independent tasks is handled by the merge gate, not cascade.

---

## v0.5.9 — Safety Hardening

**Goal:** Make the existing system safe for unattended operation. Three small, independent changes.

### 1. Max-Attempt Enforcement

**Problem:** `RETRY_POLICIES` in `worker.py:35` defines `max_retries` per failure type, but nothing enforces a global attempt limit. A task can be kicked back and re-attempted indefinitely.

**Solution:** Add a configurable `max_attempts` limit (default: 3) enforced at kickback time.

```python
# In soldier.py or file.py kickback():
def kickback(self, task_id: str, reason: str) -> None:
    task = self.get_task(task_id)
    attempt_count = len([
        a for a in task["attempts"]
        if a["status"] in ("done", "superseded")
    ])

    if attempt_count >= self.max_attempts:
        self.mark_blocked(task_id, f"max attempts ({self.max_attempts}) reached: {reason}")
        return

    # Normal kickback: done/ → ready/, attempt → superseded
    ...
```

**`blocked` status already exists** in the codebase (`TaskStatus.BLOCKED`, `cli.py` has `block`/`unblock` commands). This change makes the system USE it automatically.

**Configuration:** `max_attempts` in `.antfarm/config.json` (default: 3). Overridable per-task via `max_attempts` field on the task.

**Tests:**
1. Task kicked back 3 times → transitions to `blocked`, not `ready`
2. Blocked task is not forageable
3. `unblock` command resets attempt counter and returns to `ready`
4. Per-task `max_attempts` overrides global default

### 2. Doctor Daemon

**Problem:** Doctor only runs manually (`antfarm doctor --fix`). Stale workers and tasks pile up until a human notices.

**Solution:** Run doctor checks as a daemon thread in the colony server, alongside Soldier.

```python
# In serve.py, next to _start_soldier_thread():
def _start_doctor_thread(backend: TaskBackend, config: dict, interval: float = 300.0) -> None:
    def _loop():
        while True:
            time.sleep(interval)
            try:
                findings = run_doctor(backend, config, fix=True)
                for f in findings:
                    if f.severity == "error":
                        logger.warning("doctor: %s", f.message)
            except Exception as e:
                logger.error("doctor daemon failed: %s", e)

    thread = threading.Thread(target=_loop, daemon=True, name="doctor")
    thread.start()
```

**Safe because:** Doctor `--fix` only does safe operations: deregister stale workers, requeue stale tasks, delete stale guards. Never deletes worktrees.

**Configuration:** `doctor_interval` in `.antfarm/config.json` (default: 300 seconds). Set to 0 to disable.

**CLI flag:** `antfarm colony --no-doctor` to disable the daemon (like existing `--no-soldier`).

**Smart worktree cleanup:** Doctor daemon also cleans up orphaned worktrees — worktrees under `workspace_root` with no active task or worker. If the worktree has no git-tracked changes beyond the base branch, it is auto-deleted. If it has uncommitted or unpushed changes, it is kept for debugging. (Inspired by Claude Code's worktree cleanup: delete if no changes, keep if work exists.)

**Tests:**
1. Doctor thread starts with colony and runs on interval
2. Stale worker auto-deregistered after heartbeat TTL
3. Stale active task auto-requeued to `ready`
4. `--no-doctor` flag prevents daemon from starting
5. Orphan worktree with no changes → auto-deleted
6. Orphan worktree with uncommitted changes → kept, reported as finding

### 3. Cascade Invalidation

**Problem:** When task A is kicked back, downstream tasks that depend on A and are already in `done/` (built against stale code) waste a full build+review cycle before the merge gate catches them.

**Solution:** When kickback occurs, proactively kick back all non-merged descendants.

```python
# In soldier.py:
def kickback_with_cascade(self, task_id: str, reason: str) -> None:
    self.colony.kickback(task_id, reason)

    all_tasks = self.colony.list_tasks()
    for task in all_tasks:
        # Only cascade to done, non-merged tasks
        if task["status"] != "done":
            continue
        if self._has_merged_attempt(task):
            continue
        # Only cascade along dependency edges
        if task_id in (task.get("depends_on") or []):
            cascade_reason = f"cascade: upstream {task_id} was kicked back"
            self.kickback_with_cascade(task["id"], cascade_reason)
```

**Rules:**
- Only invalidate **non-merged descendants** (status=done, no merged attempt)
- Do NOT interrupt **active** downstream work — let it finish. The merge gate or next cascade will catch it.
- Cascade is **dependency-based only** — not scope-based
- Record the reason chain clearly: `cascade: upstream task-auth-01 was kicked back`
- Trail entry logged on each cascaded task

**Tests:**
1. A kicked back → B (depends on A, status=done) also kicked back
2. A kicked back → C (depends on A, status=active) NOT kicked back
3. A kicked back → D (depends on A, status=merged) NOT kicked back
4. A kicked back → B kicked back → E (depends on B, status=done) also kicked back (recursive)
5. A kicked back → F (independent, status=done) NOT kicked back
6. Trail shows cascade reason chain

---

## v0.6.0 — Autonomous Runs

**Goal:** `antfarm mission --spec spec.md` and walk away.

### 1. Mission Model

**The missing primitive.** A first-class entity representing one complete unit of autonomous work.

```python
class MissionStatus(StrEnum):
    PLANNING = "planning"             # plan task created, waiting for planner
    REVIEWING_PLAN = "reviewing_plan" # plan done, review-plan task created
    BUILDING = "building"             # child tasks created, builders working
    BLOCKED = "blocked"               # no forward progress possible (durable reason)
    COMPLETE = "complete"             # best-effort terminal: all tasks merged or blocked
    FAILED = "failed"                 # unrecoverable: system failure, plan rejected twice, etc.
    CANCELLED = "cancelled"           # operator cancelled via CLI


@dataclass
class Mission:
    mission_id: str              # "mission-001"
    spec: str                    # the input spec text
    spec_file: str | None        # path to spec file (for reference)
    status: MissionStatus
    plan_task_id: str | None     # "plan-mission-001"
    plan_artifact: PlanArtifact | None  # proposed plan from planner (before child task creation)
    task_ids: list[str]          # ALL tasks in this mission (impl + review + plan-review)
    blocked_task_ids: list[str]  # tasks that hit max attempts
    config: MissionConfig
    created_at: str
    updated_at: str
    completed_at: str | None
    report: MissionReport | None # generated on completion


@dataclass
class MissionConfig:
    max_attempts: int = 3             # per-task max before blocking
    max_parallel_builders: int = 4    # cap on concurrent builders
    require_plan_review: bool = True  # review plan before building
    stall_threshold_minutes: int = 30 # no-progress timeout
    completion_mode: str = "best_effort"  # "best_effort" or "all_or_nothing"
    test_command: list[str] | None = None  # override soldier test command
    integration_branch: str = "main"
    blocked_timeout_action: str = "wait"  # "wait" (stay blocked) or "fail" (transition to failed)
    blocked_timeout_minutes: int = 120    # only applies when blocked_timeout_action = "fail"


@dataclass
class PlanArtifact:
    """Structured output from a planner worker for a mission plan."""
    plan_task_id: str                  # plan task that produced this
    attempt_id: str                    # which attempt
    proposed_tasks: list[dict]         # JSON array of proposed tasks
    task_count: int                    # len(proposed_tasks)
    warnings: list[str]               # planner/validator warnings
    dependency_summary: str            # "all parallel" or "task-01 → task-03, ..."
```

**`task_ids` contains ALL tasks in the mission:** implementation tasks, review tasks, the plan-review task, and re-plan tasks. Derived subsets (implementation-only, review-only) are computed dynamically by filtering on task ID prefix (`review-*`) and `capabilities_required` (`plan`, `review`).

**v0.6.0 convention (not a permanent classification model):** Task kind is inferred from `capabilities_required` (empty = implementation, `["plan"]` = planner, `["review"]` = reviewer) and ID prefix (`review-*`). Future versions may introduce an explicit `task_kind` field if more task types are added.

**Terminal state semantics:**
- `COMPLETE` — best-effort terminal. All tasks are either merged or blocked. The morning digest tells you what succeeded and what needs human attention. This is the normal end state for overnight runs.
- `BLOCKED` — forward progress has stopped for a durable reason (task hit max attempts, all remaining tasks depend on a blocked task). An operator can unblock a task to resume. Stays blocked indefinitely by default (`blocked_timeout_action = "wait"`). If configured with `blocked_timeout_action = "fail"`, transitions to `FAILED` after `blocked_timeout_minutes` (default: 120) with no operator action.
- `FAILED` — unrecoverable. Plan review failed twice, system error, or stall timeout exceeded. Requires operator to investigate and potentially create a new mission.
- `CANCELLED` — operator ran `antfarm mission cancel`.

**Storage:** `.antfarm/missions/{mission_id}.json` — same pattern as tasks.

**Reverse linkage:** Every task created under a mission gets a `mission_id` field. This is set:
- On the plan task at creation (by Queen)
- On child tasks at creation (by Queen, after plan review passes)
- On review tasks when the Soldier creates them (Soldier reads `mission_id` from the original task)

**Plan-to-tasks flow (changed from v0.5.8):** When a plan task is part of a mission, the planner worker does NOT create child tasks directly. Instead:
1. Planner outputs the proposed plan as a `[PLAN_RESULT]` JSON artifact
2. Worker stores the plan in the plan task's harvest artifact (not as carried tasks)
3. Queen reads the plan artifact from the harvested plan task
4. If `require_plan_review=true`: Queen creates a plan-review task first, waits for approval
5. After approval (or if review disabled): Queen creates child tasks via `colony.carry()`, setting `mission_id` and `spawned_by` on each

This is a behavioral change for mission-mode plans. Non-mission plans (direct `antfarm carry --type plan`) retain the v0.5.8 behavior where the planner creates child tasks immediately.

**Lineage fields on child tasks:**
- `mission_id` — reverse link to mission
- `spawned_by.task_id` — the plan task that proposed this task
- `spawned_by.attempt_id` — which plan attempt

**Lifecycle:**

```
  mission --spec spec.md
        |
        v
  +----------+   plan task     +-----------------+
  | PLANNING |   done          | REVIEWING_PLAN  |
  +----+-----+   +----------->+--------+--------+
       |                                |
       |  (plan review                  | plan review
       |   disabled)                    | passed
       |                                |
       +----------+-----+--------------+
                  |     |
                  v     v
             +----------+
             | BUILDING |<---------+
             +----+-----+         |
                  |               | (tasks kicked back,
                  |               |  not all blocked)
                  v               |
          +-------+-------+       |
          |               |       |
          v               v       |
    +-----------+   +---------+   |
    | COMPLETE  |   | BLOCKED |---+
    +-----------+   +---------+
                          |
                          v (all blocked or operator cancels)
                    +-----------+
                    |  FAILED   |
                    +-----------+
```

**Terminal states:** `COMPLETE`, `FAILED`, `CANCELLED`. `BLOCKED` is NOT terminal — an operator can unblock a task to resume (`BLOCKED` → `BUILDING`). If configured with `blocked_timeout_action = "fail"`, a blocked mission transitions to `FAILED` after `blocked_timeout_minutes`.

### 2. Queen Controller

**What:** A daemon thread in the colony server that advances missions through their lifecycle. Deterministic, stateless, crash-recoverable.

**Where:** `antfarm/core/queen.py`, started via `_start_queen_thread()` in `serve.py`.

**Behavior:** Poll-based with adaptive interval.

```python
class Queen:
    def __init__(self, backend: TaskBackend, config: dict):
        self.backend = backend
        self.poll_interval = 30.0  # base interval

    def run(self) -> None:
        """Main queen loop. Runs indefinitely."""
        while True:
            missions = self.backend.list_missions()
            for mission in missions:
                if mission["status"] in ("complete", "failed", "cancelled"):
                    continue
                self._advance(mission)
            time.sleep(self._adaptive_interval(missions))

    def _advance(self, mission: dict) -> None:
        """Advance a single mission by one step. Idempotent."""
        status = mission["status"]

        if status == "planning":
            self._check_plan_complete(mission)
        elif status == "reviewing_plan":
            self._check_plan_review(mission)
        elif status == "building":
            self._check_build_progress(mission)
            self._check_stall(mission)
        elif status == "blocked":
            self._check_unblocked(mission)
            self._check_stall_timeout(mission)
```

**Adaptive polling:**
```python
def _adaptive_interval(self, missions: list[dict]) -> float:
    active = [m for m in missions if m["status"] in ("planning", "reviewing_plan", "building")]
    if not active:
        return 60.0   # idle: check every minute
    # Any recent progress in last 5 min? Poll faster.
    recent = any(self._had_recent_progress(m, minutes=5) for m in active)
    if recent:
        return 10.0   # active: check every 10s
    return 30.0       # waiting: check every 30s
```

**Queen's responsibilities (exhaustive list):**

| Responsibility | What Queen does | What Queen does NOT do |
|---------------|----------------|----------------------|
| Create plan task | POST `/tasks` with `capabilities_required: ["plan"]` | Does not plan |
| Create plan review task | POST `/tasks` with `capabilities_required: ["review"]` | Does not review |
| Track child tasks | Read `mission.task_ids`, check statuses | Does not schedule or assign |
| Detect stalls | Compare last progress timestamp against threshold | Does not fix stalls |
| Detect completion | All tasks merged or blocked → mark complete | Does not merge |
| Generate report | Build `MissionReport` from task data | Does not interpret results |
| Signal autoscaler | No-op in v0.6.0: autoscaler derives state from tasks/workers directly | Does not start/stop workers |
| Mark blocked | When task hits max attempts, update `blocked_task_ids` | Does not unblock |

### 3. Mission Report (Morning Digest)

**A first-class feature, not an afterthought.** This is what you read when you wake up.

```python
@dataclass
class MissionReport:
    mission_id: str
    spec_summary: str              # first 200 chars of spec
    status: MissionStatus           # mission status at report time (COMPLETE, BLOCKED, or FAILED)
    duration_minutes: float

    # Task counts
    total_tasks: int
    merged_tasks: int
    blocked_tasks: int
    failed_reviews: int

    # Detail
    merged: list[MissionReportTask]      # title, PR url, lines changed
    blocked: list[MissionReportBlocked]  # title, reason, attempt count
    risks: list[str]                     # aggregated from task artifacts

    # Links
    pr_urls: list[str]
    branches: list[str]

    # What changed
    total_lines_added: int
    total_lines_removed: int
    files_changed: list[str]

    # Generated
    generated_at: str
```

```python
@dataclass
class MissionReportTask:
    task_id: str
    title: str
    pr_url: str
    lines_added: int
    lines_removed: int
    files_changed: list[str]


@dataclass
class MissionReportBlocked:
    task_id: str
    title: str
    reason: str
    attempt_count: int
    last_failure_type: str
```

**Output formats:**

| Format | Where | How |
|--------|-------|-----|
| JSON | `.antfarm/missions/{mission_id}_report.json` | Always generated |
| Terminal | `antfarm mission status {mission_id}` | Formatted rich output |
| Markdown | `antfarm mission report {mission_id} --format md` | For pasting into issues/PRs |

**Example terminal output:**

```
Mission: mission-auth-jwt (complete)
Duration: 3h 42m
Spec: "Build JWT authentication system with login, registration, and token refresh"

Merged (7/8):
  task-auth-jwt-01  Add User model + migration          +142 -0   PR #47
  task-auth-jwt-02  Add JWT token service                +89  -3   PR #48
  task-auth-jwt-03  Add login endpoint                   +67  -2   PR #49
  task-auth-jwt-04  Add registration endpoint            +73  -1   PR #50
  task-auth-jwt-05  Add token refresh endpoint           +41  -0   PR #51
  task-auth-jwt-06  Add auth middleware                  +52  -8   PR #52
  task-auth-jwt-07  Add auth tests                       +186 -0   PR #53

Blocked (1/8):
  task-auth-jwt-08  Add rate limiting to auth endpoints
    Reason: max attempts (3) reached: test failure
    Last failure: TEST_FAILURE — rate limit test expects Redis, but no Redis in test env
    Needs: human decision — add Redis to test env or mock it

Total: +650 -14 across 18 files
```

### 4. Plan Review

**Problem:** Planner produces a plan. Nobody checks if it's actually good before N builders start burning API credits.

**Solution:** After the planner completes, Queen creates a `review-plan-{mission_id}` task. The existing reviewer infrastructure handles it — no new subsystem.

**Flow:**

```
Queen creates plan task → Planner decomposes → Plan task harvested
    |                                           (plan stored as artifact,
    |                                            no child tasks created yet)
    v
Queen reads plan artifact from harvested plan task
Queen checks: config.require_plan_review?
    |
    +-- no  → Queen creates child tasks from plan artifact → mission → building
    |
    +-- yes → Queen creates review-plan-{mission_id} task
              with plan JSON in the spec.
              Reviewer forages it, checks deps/scopes/completeness.
              Reviewer outputs [REVIEW_VERDICT]:
                |
                +-- pass → Queen creates child tasks from plan artifact
                |          → mission → building
                +-- needs_changes → Queen creates re-plan task (max 1 re-plan)
                |                   with reviewer feedback in spec
                +-- blocked → mission → failed
```

**Plan review spec (what the reviewer checks):**

```
Review this implementation plan for: "{mission spec summary}"

Plan:
{JSON array of proposed tasks}

Check:
1. Are dependencies logically correct? (no missing deps, no unnecessary deps)
2. Are scopes complete? (does the plan cover the full spec?)
3. Is each task independently implementable?
4. Are complexity ratings reasonable?
5. Could any tasks be parallelized further?

Output [REVIEW_VERDICT] with verdict: pass/needs_changes/blocked
If needs_changes, include specific findings about what to fix.
```

**Max 1 re-plan.** If the second plan also fails review, mission transitions to `failed` with reason "plan review failed twice."

**Configuration:** `require_plan_review` in `MissionConfig` (default: true). Can be disabled for small/trusted specs.

### 5. Autoscaler

**What:** A daemon thread that starts and stops workers based on queue state. Single-host only in v0.6.0.

**Where:** `antfarm/core/autoscaler.py`, started via `_start_autoscaler_thread()` in `serve.py`.

**Algorithm:**

```python
class Autoscaler:
    def __init__(self, backend: TaskBackend, config: AutoscalerConfig):
        self.backend = backend
        self.config = config
        self.managed_workers: dict[str, subprocess.Popen] = {}

    def run(self) -> None:
        while True:
            self._reconcile()
            time.sleep(30)

    def _reconcile(self) -> None:
        """Compare desired state with actual state, start/stop workers."""
        tasks = self.backend.list_tasks()
        workers = self.backend.list_workers()

        desired = self._compute_desired(tasks, workers)
        actual = self._count_actual(workers)

        # Planner: 0 or 1
        self._reconcile_role("planner", desired["planner"], actual["planner"])
        # Builders: 0 to max
        self._reconcile_role("builder", desired["builder"], actual["builder"])
        # Reviewers: 0 to max
        self._reconcile_role("reviewer", desired["reviewer"], actual["reviewer"])

    def _compute_desired(self, tasks, workers) -> dict:
        ready_plan = [t for t in tasks if t["status"] == "ready"
                      and "plan" in t.get("capabilities_required", [])]
        # v0.6.0 convention: implementation tasks have empty capabilities_required.
        # Future task types may need explicit task-kind classification.
        ready_build = [t for t in tasks if t["status"] == "ready"
                       and not t.get("capabilities_required")]
        done_no_review = [t for t in tasks if t["status"] == "done"
                          and not t["id"].startswith("review-")
                          and not self._has_verdict(t)
                          and not self._has_merged(t)]
        ready_review = [t for t in tasks if t["status"] == "ready"
                        and "review" in t.get("capabilities_required", [])]

        # Cap builders by non-overlapping scope groups
        scope_groups = self._count_scope_groups(ready_build)
        active_builders = [w for w in workers if "review" not in w.get("capabilities", [])
                           and "plan" not in w.get("capabilities", [])
                           and w.get("status") != "offline"]
        rate_limited = [w for w in active_builders
                        if self._is_rate_limited(w)]

        desired_builders = min(
            scope_groups,                         # non-overlapping work
            self.config.max_builders,             # hard cap
            len(ready_build),                     # no more than queue depth
        )

        # Don't scale up if most builders are rate-limited
        if len(rate_limited) > len(active_builders) // 2:
            desired_builders = min(desired_builders, len(active_builders))

        return {
            "planner": 1 if ready_plan else 0,
            "builder": desired_builders,
            "reviewer": min(
                max(1 if (done_no_review or ready_review) else 0,
                    len(ready_review)),
                self.config.max_reviewers,
            ),
        }

    def _count_scope_groups(self, tasks: list[dict]) -> int:
        """Count non-overlapping scope groups among ready tasks."""
        if not tasks:
            return 0
        groups: list[set[str]] = []
        for t in tasks:
            touches = set(t.get("touches", []))
            if not touches:
                groups.append(set())
                continue
            merged = False
            for g in groups:
                if g & touches:
                    g.update(touches)
                    merged = True
                    break
            if not merged:
                groups.append(touches)
        return len(groups)
```

**Worker lifecycle management:**

The autoscaler tracks managed workers with explicit role metadata, not by name convention. Decisions to stop workers use colony state (worker status = idle), not local process state.

```python
@dataclass
class ManagedWorker:
    name: str
    role: str           # "planner", "builder", "reviewer"
    worker_id: str      # "{node_id}/{name}" — matches colony registry
    process: subprocess.Popen

class Autoscaler:
    managed: dict[str, ManagedWorker] = {}

    def _start_worker(self, role: str) -> None:
        """Start a worker subprocess."""
        seq = sum(1 for w in self.managed.values() if w.role == role) + 1
        name = f"{role}-{seq}"
        worker_id = f"{self.config.node_id}/{name}"
        cmd = [
            "antfarm", "worker", "start",
            "--agent", self.config.agent_type,
            "--type", role,
            "--node", self.config.node_id,
            "--name", name,
            "--repo-path", self.config.repo_path,
            "--integration-branch", self.config.integration_branch,
        ]
        if self.config.token:
            cmd.extend(["--token", self.config.token])

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.managed[name] = ManagedWorker(name=name, role=role,
                                            worker_id=worker_id, process=proc)
        logger.info("autoscaler started worker=%s role=%s pid=%d", name, role, proc.pid)

    def _stop_idle_worker(self, role: str) -> None:
        """Stop an idle worker, verified against colony state."""
        colony_workers = self.backend.list_workers()
        idle_colony = {w["worker_id"] for w in colony_workers
                       if w.get("status") == "idle"}

        for name, mw in list(self.managed.items()):
            if mw.role == role and mw.worker_id in idle_colony and mw.process.poll() is None:
                mw.process.terminate()
                mw.process.wait(timeout=10)
                del self.managed[name]
                logger.info("autoscaler stopped worker=%s (confirmed idle in colony)", name)
                return

    def _cleanup_exited(self) -> None:
        """Remove managed workers whose processes have exited."""
        for name, mw in list(self.managed.items()):
            if mw.process.poll() is not None:
                del self.managed[name]
                logger.info("autoscaler cleaned up exited worker=%s", name)
```

**Configuration:**

```python
@dataclass
class AutoscalerConfig:
    enabled: bool = False              # opt-in for v0.6.0
    agent_type: str = "claude-code"
    node_id: str = "local"
    repo_path: str = "."
    integration_branch: str = "main"
    max_builders: int = 4
    max_reviewers: int = 2
    token: str | None = None
```

**CLI:**
```
antfarm colony --autoscaler                    # enable autoscaler
antfarm colony --autoscaler --max-builders 6   # with custom cap
antfarm colony --no-autoscaler                 # disable (default)
```

**Tests:**
1. No ready tasks → 0 desired workers
2. 3 ready build tasks, all different scopes → 3 desired builders
3. 3 ready build tasks, all same scope → 1 desired builder
4. Done tasks with no verdict → at least 1 reviewer
5. Rate-limited builders → don't scale up
6. Builder process exits → cleaned up from managed_workers
7. Planner: exactly 0 or 1, never more

### 6. CLI Changes

**New command group:**

```
antfarm mission --spec spec.md                          # create + start
antfarm mission --spec spec.md --no-plan-review         # skip plan review
antfarm mission --spec spec.md --max-builders 6         # override scaling
antfarm mission status {mission_id}                     # show current state
antfarm mission report {mission_id}                     # show morning digest
antfarm mission report {mission_id} --format md         # markdown output
antfarm mission cancel {mission_id}                     # cancel a running mission
antfarm mission list                                    # list all missions
```

**Task changes:**
- `antfarm carry` gains `--mission` option to attach a task to an existing mission
- `antfarm scout` TUI gains a Mission panel showing mission-level status

### 7. API Changes

**New endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| POST | `/missions` | Create a mission |
| GET | `/missions` | List missions |
| GET | `/missions/{id}` | Get mission detail |
| PATCH | `/missions/{id}` | Update mission status (Queen internal) |
| POST | `/missions/{id}/cancel` | Cancel a mission |
| GET | `/missions/{id}/report` | Get mission report |

**Task endpoint changes:**
- `POST /tasks` accepts optional `mission_id` field
- `GET /tasks` accepts optional `?mission_id=` filter
- `GET /status/full` includes mission summary

### 8. Backend Changes

**FileBackend additions:**

```
.antfarm/
  missions/                    # NEW
    mission-001.json           # mission state
    mission-001_report.json   # generated report
  tasks/
    ready/
    active/
    done/
  ...
```

**TaskBackend ABC additions:**

```python
# New methods on TaskBackend:
def create_mission(self, mission: dict) -> str: ...
def get_mission(self, mission_id: str) -> dict | None: ...
def list_missions(self, status: str | None = None) -> list[dict]: ...
def update_mission(self, mission_id: str, updates: dict) -> None: ...
```

**Task dict gains `mission_id` field:**
```python
# On Task dataclass:
mission_id: str | None = None  # reverse linkage to mission
```

---

## v0.6.1 — Scale + Integration

**Goal:** Multi-node scaling and prompt cache optimization.

**Scope:** Runner, multi-node autoscaler, prompt cache sharing. GitHub Issue Sync deferred to v0.6.2+.

### 1. Runner

A lightweight daemon running on each remote worker machine. The term "Runner" follows established CI convention (GitHub Actions Runner, GitLab Runner). It replaces the earlier "Node Agent" name.

```
antfarm runner --colony-url http://colony:7433 --repo-path /path/to/repo
```

**Responsibilities:**
- Register node with colony on startup
- Reconcile local worker processes to match colony-published desired state
- Restart crashed worker processes locally (self-healing)
- Report actual state, capacity, and health back to colony
- Keep local git repo in sync (`git fetch origin` periodically)
- Support drain mode for graceful downscaling

**The Runner does NOT make scaling decisions.** Colony decides what should run; Runner decides how to converge locally.

**Desired-state protocol:**

The Colony publishes per-node desired state rather than imperative start/stop commands. This provides idempotency, network-loss recovery, and local self-healing.

```json
{
  "generation": 17,
  "desired": {
    "builder": 2,
    "reviewer": 1,
    "planner": 0
  },
  "drain": []
}
```

The Runner reconciles local processes to match:
1. Compare desired counts vs actual running processes per role
2. Start missing workers
3. Stop excess workers (idle only — drain, don't kill)
4. Report back `applied_generation` and actual state

**Generation numbers** prevent stale updates from winning during reconnects. The Runner only applies state with a generation >= its last applied generation.

**Process adoption:** Runner writes PID files for each managed worker. On restart, it scans PID files, validates processes are alive, and adopts them. No generic process scanning.

**Security:** Runner binds to `127.0.0.1` by default. No authentication in v0.6.1 (trusted private network only). Operators must explicitly bind to a LAN address for multi-node use.

**API (node-local, binds to loopback by default):**

| Method | Path | Description |
|--------|------|-------------|
| PUT | `/desired-state` | Receive desired worker state from colony |
| GET | `/actual-state` | Report running workers, capacity, applied generation |
| GET | `/capacity` | Report CPU count, memory, max workers |
| GET | `/health` | Runner liveness check |

**Drain behavior:** When downscaling, the Runner only stops idle workers (no active task). Workers with active tasks finish their current task before being stopped. This prevents expensive task interruption.

**Colony tracks Runner URLs:**
```python
# Extended Node model:
@dataclass
class Node:
    node_id: str
    joined_at: str
    last_seen: str
    runner_url: str | None = None    # "http://192.168.1.10:7434"
    max_workers: int = 4
    capabilities: list[str] = field(default_factory=list)  # e.g. ["gpu", "docker"]
```

### 2. Multi-Node Autoscaler

The existing single-host autoscaler is extended with an **Actuator abstraction** to support both local and remote worker management. One autoscaler, shared scaling logic, pluggable execution.

**Architecture:**

```
Autoscaler (shared scaling logic)
  ├── compute desired worker counts (scope groups, rate-limit backoff, etc.)
  ├── PlacementStrategy (decides which node gets what)
  └── Actuator (executes the decision)
       ├── LocalActuator  — subprocess.Popen (existing v0.6.0 behavior)
       └── RemoteActuator — pushes desired state to Runner HTTP API
```

**Shared logic (MUST be extracted into standalone functions, not duplicated):**
- Scope-group calculation (union-find by touches)
- Rate-limit backoff (don't scale up if >50% builders rate-limited)
- Planner/reviewer minimums
- Desired count computation per role

Both `Autoscaler` (single-host) and `MultiNodeAutoscaler` call the same extracted functions. Two copies of scaling logic is not acceptable.

**PlacementStrategy:**
- Round-robin across nodes with available capacity
- Respect node-level `max_workers` cap
- Prefer nodes with matching capabilities (e.g., GPU tasks to GPU nodes)
- Skip unreachable nodes and try next
- Rebalance slowly, not aggressively

**Actuator interface (receives runner_url from backend Node records — single source of truth):**
```python
class Actuator(ABC):
    @abstractmethod
    def apply(self, runner_url: str, desired: dict[str, int], generation: int) -> None:
        """Push desired worker counts to a node."""
        ...

    @abstractmethod
    def get_actual(self, runner_url: str) -> dict:
        """Get actual worker state from a node."""
        ...
```

- `LocalActuator`: calls `subprocess.Popen` / `process.terminate()` directly (existing behavior, wrapped)
- `RemoteActuator`: pushes desired state to Runner via `PUT /desired-state`, reads actual via `GET /actual-state`

**The single-host autoscaler remains unchanged for v0.6.1.** The actuator abstraction is used only for the multi-node path. Unifying single-host under the same abstraction is a future cleanup.

### 3. Prompt Cache Sharing for Parallel Builders

**Optimization:** When the autoscaler spawns multiple builders for the same mission, they share identical project context (codebase structure, conventions, CLAUDE.md, repo facts). Only the task-specific spec differs.

Inspired by Claude Code's fork model (which shares KV cache across parallel agents by making API request prefixes byte-identical), Antfarm can reduce token cost by:

1. Building a shared **context prefix** per mission: repo facts from memory store, project conventions, integration branch state
2. Passing this prefix identically to all builder agents in the same mission
3. Only the task-specific section (title, spec, workspace path) varies per builder

This is agent-specific — only works with agents that support prompt caching (Claude, potentially others). For agents without caching support, this is a no-op.

**Feature-flagged:** `enable_mission_context=False` by default. Off until runner/autoscaler are stable in dogfooding. When disabled, workers proceed without context prefix.

**Implementation:** The Queen generates a `mission_context` blob once per build phase. Worker runtime prepends it to the agent prompt. The blob is stored in `.antfarm/missions/{mission_id}_context.md`.

**Expected savings:** For a mission with 8 builders, ~7x reduction in input token cost for shared context (cached tokens are 10% of input cost on Claude).

### 4. GitHub Issue Sync (DEFERRED)

Deferred to v0.6.2+. Scope: automatic issue creation for mission child tasks, bi-directional task-to-issue linkage, status label updates, kickback/merge comments.

---

## Visual Workflow — v0.6.0

### What you do vs what the system does

```
YOU:
  antfarm colony --autoscaler
  antfarm mission --spec spec.md
  (go to sleep)
  antfarm mission report {id}

=========================== EVERYTHING BELOW IS AUTOMATED ==========================
```

### Full pipeline — step by step

```
antfarm mission --spec spec.md
        |
        v
+-----------------------------------------------+
|  QUEEN creates plan task                       |
|  plan-{mission_id} with cap: ["plan"]          |
|  mission status: PLANNING                      |
+---------------------+-------------------------+
                      |
                      v
+-----------------------------------------------+
|  AUTOSCALER detects ready plan task            |
|  Starts 1 planner worker (subprocess)          |
+---------------------+-------------------------+
                      |
                      v
+-----------------------------------------------+
|  PLANNER worker forages plan task              |
|  Creates worktree, launches claude --agent      |
|  planner                                       |
|  Agent reads codebase, outputs [PLAN_RESULT]   |
|  Worker parses JSON, validates (max 10 tasks,  |
|  no forward deps, title+spec required)         |
|  Harvests plan task with PLAN ARTIFACT         |
|  (no child tasks created — artifact only)      |
+---------------------+-------------------------+
                      |
                      v
+-----------------------------------------------+
|  QUEEN reads plan artifact from harvested task |
|  config.require_plan_review = true?            |
|                                                |
|  YES: creates review-plan-{mission_id}         |
|       plan JSON included in review spec        |
|       mission status: REVIEWING_PLAN           |
|                                                |
|  NO:  skips straight to child task creation    |
+--------+--------------------+-----------------+
         |                    |
         v                    |
+-------------------------+   |
|  REVIEWER worker forages |   |
|  plan review task        |   |
|  Checks deps, scopes,   |   |
|  completeness            |   |
|  Outputs [REVIEW_VERDICT]|   |
+--------+----------------+   |
         |                    |
    pass | needs_changes      |
    |    |                    |
    |    v                    |
    |  QUEEN creates re-plan  |
    |  task (max 1 re-plan)   |
    |  If 2nd also fails:     |
    |  mission -> FAILED      |
    |                         |
    v                         v
+-----------------------------------------------+
|  QUEEN creates N child tasks from plan artifact|
|  Each task gets mission_id = {mission_id}      |
|  Each task gets spawned_by = plan task          |
|  capabilities_required = [] (builder tasks)    |
|  mission.task_ids updated                      |
|  mission status: BUILDING                      |
+---------------------+-------------------------+
                      |
                      v
+-----------------------------------------------+
|  AUTOSCALER computes desired workers           |
|                                                |
|  Builders:                                     |
|    scope_groups = non-overlapping touch groups  |
|    desired = min(scope_groups, max_builders,    |
|                  ready_count)                   |
|    if >50% rate limited: don't scale up        |
|                                                |
|  Reviewers:                                    |
|    1+ when review backlog exists               |
|                                                |
|  Starts/stops worker subprocesses              |
+---------------------+-------------------------+
                      |
                      v
+-----------------------------------------------+
|  BUILDER workers forage in parallel            |
|                                                |
|  For each builder:                             |
|    scheduler.select_task():                    |
|      1. deps met?                              |
|      2. cap match? (general builder)           |
|      3. pin check                              |
|      4. prefer non-overlapping scopes          |
|      5. cooler hotspots first                  |
|      6. lower priority number                  |
|      7. oldest created_at (FIFO)               |
|                                                |
|    os.rename(ready/ -> active/)                |
|    new Attempt created                         |
|    git worktree add (isolated branch)          |
|    claude -p --agent worker (subprocess)       |
|    agent implements, tests, commits, pushes    |
|    gh pr create                                |
|    harvest: active/ -> done/                   |
|                                                |
|  On failure:                                   |
|    classify: timeout/infra/lint/build/test     |
|    trail [FAILURE_RECORD]                      |
|    task stays active/ (doctor recovers)        |
+---------+-----------+-------------------------+
          |           |
     success      failure
          |           |
          v           v
+------------------+  +------------------------+
| task -> done/    |  | doctor daemon recovers  |
| attempt: done    |  | task -> ready/          |
| SSE: harvested   |  | fresh attempt next time |
+--------+---------+  +------------------------+
         |
         v
+-----------------------------------------------+
|  SOLDIER detects done task                     |
|  process_done_tasks():                         |
|    creates review-{task_id}                    |
|    cap: ["review"], priority: 1                |
|    spec includes branch, PR, review pack       |
+---------------------+-------------------------+
                      |
                      v
+-----------------------------------------------+
|  REVIEWER worker forages review task           |
|  Reads PR diff, runs tests                    |
|  Outputs [REVIEW_VERDICT]:                     |
|    pass / needs_changes / blocked              |
|  Verdict stored on original task's attempt     |
+--------+--------+----------------------------+
         |        |
      pass    needs_changes/blocked
         |        |
         v        v
+----------------+ +----------------------------+
| SOLDIER checks | | SOLDIER kickback            |
| merge queue    | |   done/ -> ready/           |
|                | |   attempt -> superseded     |
| Filters:       | |   CASCADE: kick back        |
|  status=done   | |   downstream done tasks     |
|  deps merged   | |   that depend on this one   |
|  review=pass   | |                             |
|  has branch    | |   attempt_count >= 3?        |
|                | |   YES: task -> blocked       |
| Sort:          | |   Queen updates mission      |
|  override pos  | |                              |
|  priority      | +----------------------------+
|  FIFO          |
+-------+--------+
        |
        v
+-----------------------------------------------+
|  SOLDIER attempt_merge()                       |
|                                                |
|  git fetch origin                              |
|  git checkout -b antfarm/temp-merge            |
|       origin/{integration_branch}              |
|  git merge --no-ff {feature_branch}            |
|    conflict? -> FAILED -> kickback             |
|  run test_command (pytest -x -q)               |
|    tests fail? -> FAILED -> kickback           |
|  git checkout {integration_branch}             |
|  git merge --ff-only antfarm/temp-merge        |
|  git push origin {integration_branch}          |
|                                                |
|  SUCCESS: mark_merged()                        |
|    attempt status -> merged                    |
|    dependent tasks now unblocked               |
|    SSE: merged                                 |
|                                                |
|  finally: _cleanup()                           |
|    abort merge, checkout main, delete temp,    |
|    git clean -fd, git reset --hard             |
+---------------------+-------------------------+
                      |
                      v
+-----------------------------------------------+
|  QUEEN checks mission progress                 |
|                                                |
|  All tasks merged?                             |
|    -> mission status: COMPLETE                 |
|    -> generate MissionReport                   |
|                                                |
|  All tasks either merged or blocked?           |
|    -> mission status: COMPLETE (best-effort)   |
|    -> report shows merged + blocked items      |
|                                                |
|  Some tasks blocked, others still progressing? |
|    -> mission stays BUILDING                   |
|    -> Queen tracks blocked_task_ids            |
|                                                |
|  No progress for stall_threshold?              |
|    -> mission status: BLOCKED                  |
|    -> stays blocked until operator intervenes  |
|    -> (or auto-fails if configured)            |
+---------------------+-------------------------+
                      |
                      v
+-----------------------------------------------+
|  MISSION REPORT (morning digest)               |
|                                                |
|  antfarm mission report {mission_id}           |
|                                                |
|  Mission: mission-auth-jwt (complete)          |
|  Duration: 3h 42m                              |
|                                                |
|  Merged (7/8):                                 |
|    task-01  Add User model      +142  PR #47   |
|    task-02  Add JWT service      +89  PR #48   |
|    ...                                         |
|                                                |
|  Blocked (1/8):                                |
|    task-08  Add rate limiting                  |
|    Reason: TEST_FAILURE (3 attempts)            |
|    Needs: human decision                       |
|                                                |
|  Total: +650 -14 across 18 files               |
+-----------------------------------------------+
```

### Task state machine (v0.6)

```
                   carry / planner
                        |
                        v
                   +---------+
            +----->|  READY  |<-----+
            |      +----+----+      |
            |           |           |
            |    forage (scheduler) |
            |           |           |
            |           v           |
            |      +---------+      |
            |      | ACTIVE  |      |
            |      +----+----+      |
            |           |           |
            |      +----+----+     kickback (soldier)
            |      |         |     cascade kickback
            |      v         v      |
            |  harvest    failure   |
            |      |     (doctor    |
            |      v      recovers) |
            |  +---------+    |     |
            |  |  DONE   |----+-----+
            |  +----+----+
            |       |
            |  +----+----+
            |  |         |
            |  v         v
            | review   review
            | pass     fail/blocked
            |  |         |
            |  v         |
            | merge      |
            |  |         |
            |  v         |
            | +--------+ |
            | | MERGED | |
            | +--------+ |
            |             |
            |  attempt_count >= max_attempts?
            |       |
            |    NO | YES
            |       |    |
            +-------+    v
                     +---------+
                     | BLOCKED |
                     +---------+
                          |
                     unblock (operator)
                     or stall timeout
                          |
                          v
                     +---------+
                     | FAILED  | (mission-level)
                     +---------+
```

### Mission state machine

```
  antfarm mission --spec spec.md
        |
        v
  +----------+
  | PLANNING |---plan task created, planner working
  +----+-----+
       |
       | plan task done
       v
  +-----------------+
  | REVIEWING_PLAN  |---plan review task created (if enabled)
  +--------+--------+
           |
      pass | needs_changes (max 1 re-plan)
           |       |
           |    re-plan fails: mission -> FAILED
           |
           v
  +----------+
  | BUILDING |<--------+
  +----+-----+         |
       |                |
       | all merged     | task unblocked
       | or all         | (operator)
       | accounted for  |
       |                |
       v                |
  +-----------+   +---------+
  | COMPLETE  |   | BLOCKED |---no forward progress, operator can resume
  +-----------+   +----+----+
                       |
                  operator unblocks task
                  -> back to BUILDING
                       |
                  (if blocked_timeout_action="fail"
                   and timeout exceeded)
                       |
                       v
                  +---------+
                  | FAILED  |---unrecoverable (system error, plan
                  +---------+   rejected twice, or configured timeout)

                  +-----------+
                  | CANCELLED |---operator: antfarm mission cancel
                  +-----------+
```

### Worker scaling visual

```
Queue state:                Autoscaler decision:

ready/ has:                 Starts:
  plan-001 (cap: plan)        1 planner
  (nothing else yet)          0 builders
                              0 reviewers

After planner completes:    Adjusts:
  task-01 (auth)              0 planners (no plan tasks)
  task-02 (api)               3 builders (3 scope groups)
  task-03 (db)                0 reviewers (no review tasks yet)
  task-04 (api, dep: 02)
  task-05 (auth, dep: 01)

After 3 tasks done:         Adjusts:
  done: 01, 02, 03           scale down to 2 builders (2 ready)
  ready: 04, 05              start 1 reviewer (review backlog)
  review-01, review-02,
  review-03 in ready/

All tasks done:             Adjusts:
  done: all                   0 builders (queue empty)
  reviews pending             1 reviewer (review backlog)

All merged:                 Adjusts:
  nothing in ready/           0 everything
                              workers exit naturally
```

---

## Edge Cases

### Task A kicked back, Task B depends on A, B already done with passing review

```
t0: A and B both created by planner
t1: A built, harvested → done
t2: B forages (A is in done_task_ids)
t3: B built against origin/main (A not merged yet)
t4: A review → pass, B review → pass
t5: Soldier merges A
t6: Soldier tries to merge B onto main+A
    ├─ Conflict or test failure → kickback B → B rebuilds fresh ✓
    └─ Clean merge + tests pass → merged ✓

BUT if A is kicked back at t5 instead:
t5': A kicked back → ready/
t6': CASCADE: B also kicked back (depends on A, status=done)
t7': A re-forages, rebuilds fresh
t8': A merged
t9': B re-forages, builds against main+A (correct code)
t10': B reviewed, merged ✓
```

### Independent tasks, one fails

```
A (touches: auth) and B (touches: api) — no dependency
A review → needs_changes → kicked back
B review → pass → merged ✓ (independent, not blocked)
A rebuilds, re-reviewed, merged ✓

Cascade does NOT apply — no dependency edge.
```

### Scope overlap without dependency

```
A and B both touch api/ — no dependency declared
A merges first (higher priority)
B merge attempt: git merge onto main+A
  ├─ Conflict → kickback B, rebuild on main+A ✓
  └─ No conflict, tests pass → merged ✓

Planner should have declared a dependency. Plan review catches this.
```

### Infinite kickback loop (prevented)

```
Task A fails tests. Kicked back, attempt 1.
A re-forages, rebuilt. Same tests fail. Kicked back, attempt 2.
A re-forages, rebuilt. Same tests fail. Attempt 3 = max_attempts.
A → blocked status.
Queen updates mission: blocked_task_ids += [A]
If all remaining tasks depend on A → mission → blocked
Morning report: "task-auth-03 blocked after 3 attempts: TEST_FAILURE"
```

### Planner produces bad plan

```
Plan review catches: "task-02 should depend on task-03 (JWT needs user table)"
Reviewer verdict: needs_changes
Queen creates re-plan task with feedback
Planner re-plans with corrected deps
Re-plan review → pass
Child tasks created with correct deps ✓

If re-plan also fails review: mission → failed
```

### Rate limit exhaustion

```
8 builders running. All hit API rate limit.
All report cooldown_until via heartbeat.
Autoscaler sees: 8/8 in cooldown.
Action: do NOT start more (rate limits are per-account, not per-worker).
Workers resume when cooldown expires.
```

### Node goes offline mid-build

```
Builder on mac-studio-1 loses network.
Heartbeat stops → doctor daemon detects stale worker (5 min interval)
Doctor --fix: deregisters worker, requeues task
Autoscaler: sees fewer builders, starts replacement (on available node)
Task re-forages with fresh attempt ✓
```

### All tasks done, some blocked

```
10 tasks, 8 merged, 1 blocked, 1 waiting on blocked (dep)
Queen: no more forward progress possible
Mission → COMPLETE (best-effort default)
Report generated:
  "8/10 merged. 1 blocked (task-auth-03: test failure, 3 attempts).
   1 waiting (task-api-05: depends on blocked task-auth-03).
   Human attention needed for task-auth-03."
```

### Mission stall detection

```
Mission in "building" state. No task progress for 30 minutes.
Queen detects: last_progress_at + stall_threshold < now
Queen logs warning in mission trail
Mission → BLOCKED (no forward progress)
Stays blocked until operator intervenes or timeout configured.
Report: "mission stalled — no progress for 30 minutes. Check worker logs."
```

---

## Colony Daemon Threads (v0.6.0)

| Thread | Responsibility | Poll interval | Started by |
|--------|---------------|---------------|------------|
| **Soldier** | Merge done tasks into integration branch | 30s | `--no-soldier` to disable |
| **Queen** | Advance missions through lifecycle phases | 10-60s (adaptive) | Active when missions exist |
| **Doctor** | Detect and fix stale workers/tasks/guards | 300s | `--no-doctor` to disable |
| **Autoscaler** | Start/stop workers based on queue state | 30s | `--autoscaler` to enable |

---

## Dependencies

No new external dependencies. All new components use existing primitives:
- Mission storage uses FileBackend patterns (JSON files in `.antfarm/missions/`)
- Queen uses ColonyClient or BackendAdapter (like Soldier)
- Autoscaler uses `subprocess.Popen` for worker management
- Plan review uses existing reviewer worker + `[REVIEW_VERDICT]` protocol

```toml
# No changes to pyproject.toml dependencies for v0.6.0
```

---

## Success Criteria

### Scenario A: Full Autonomous Run
```
$ antfarm colony --autoscaler
$ antfarm mission --spec auth-system-spec.md
# Go to sleep. Wake up.
$ antfarm mission report mission-auth-system
# See: 7/8 merged, 1 blocked, all PRs listed, report shows what needs attention.
```

### Scenario B: Cascade Recovery
Task A fails review. Task B (depends on A) was already done. B is automatically kicked back. A is rebuilt, reviewed, merged. B re-forages, builds against correct main, reviewed, merged. No manual intervention.

### Scenario C: Stall Detection
A builder crashes and doctor hasn't run yet. Queen detects no progress for 30 minutes. Mission marked as stalled. Doctor eventually recovers the task. Builder re-forages. Mission continues.

### Scenario D: Conservative Scaling
8 tasks queued, but 5 touch the same `api/` scope. Autoscaler starts 4 builders (3 scope groups + 1), not 8. No conflict storms.

### Scenario E: Plan Review Catches Bad Plan
Planner decomposes spec into 6 tasks but misses a critical dependency. Reviewer catches it. Queen triggers re-plan. Second plan is correct. Builders start with good tasks.

---

## Explicitly Deferred

- **Multi-node autoscaling** — v0.6.1
- **GitHub issue sync** — v0.6.1
- **Claude Code plugin** — v0.6.2 (see SPEC_v06_plugin.md)
- **AI-assisted conflict resolution** in Soldier — future
- **Vector DB / semantic memory** — out of scope
- **Web dashboard** — out of scope
- **Recursive missions** (mission spawning sub-missions) — future
- **Cost tracking / API budget enforcement** — future
