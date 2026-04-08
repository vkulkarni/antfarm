# Antfarm Workflow — v0.5.8

End-to-end reference for how antfarm orchestrates work, traced from the actual codebase.

---

## Step-by-Step Workflow

| # | Step | Trigger | Worker | What happens (from code) |
|---|------|---------|--------|--------------------------|
| 1 | **Start Colony** | Manual: `antfarm colony` | None | `cli.py:145` → `serve.py:183 get_app()` boots FastAPI on port 7433. Creates FileBackend with `.antfarm/tasks/{ready,active,done}/`, `workers/`, `nodes/`, `guards/`. Starts Soldier as daemon thread via `_start_soldier_thread()` — runs `soldier.py:75 run()` in infinite loop polling every 30s. |
| 2 | **Submit work** | Manual: `antfarm carry --type plan` | None | `cli.py:382` → POST `/tasks` → `serve.py:288` writes task JSON to `ready/`. If `--type plan`: ID gets `plan-` prefix, `capabilities_required: ["plan"]` added. Auto-generates ID `task-{timestamp_ms}` if none given. |
| 3 | **Start workers** | Manual: `antfarm worker start` | None (registration) | `cli.py:318` → `WorkerRuntime.__init__()`. `--type planner` adds `"plan"` to caps. `--type reviewer` adds `"review"` to caps. `--type builder` gets no specialized caps. `run()` auto-registers node, then POST `/workers/register` with `worker_id = "{node}/{name}"`. |
| 4 | **Planner forages** | Auto: `worker.py:250 run()` loop | **Planner** | `_process_one_task()` → POST `/tasks/pull` → `serve.py:335` acquires `threading.Lock`. `backend.pull()` calls `scheduler.select_task()`: deps met → cap filter (`plan` cap matches `plan` requirement) → pin check → scope overlap → hotspot heat → priority → FIFO. Winner: `os.rename(ready/ → active/)`, new Attempt created. |
| 5 | **Planner workspace** | Auto: inside `_process_one_task()` | **Planner** | `workspace.py:26 create()` → `git fetch origin` → `git worktree add -b feat/{task_id}-{attempt_id} {path} origin/main`. Returns isolated worktree path. |
| 6 | **Planner launches agent** | Auto: inside `_process_one_task()` | **Planner** | `worker.py:573 _launch_agent()` detects `is_plan=True`. Builds prompt: *"You are a PLANNER. Decompose this spec... Output JSON between [PLAN_RESULT] tags. Max 10 tasks."* Launches `claude -p --agent planner --permission-mode bypassPermissions` as subprocess. Heartbeat thread starts (30s). |
| 7 | **Plan parsed** | Auto: agent exits 0 | **Planner** | `worker.py:692 _process_plan_output()` → regex extracts JSON from `[PLAN_RESULT]...[/PLAN_RESULT]` → `PlannerEngine.parse_structured_plan()` → `validate_plan()` (max 10, no forward deps, title+spec required). `resolve_dependencies()` converts 1-based index deps to child IDs. |
| 8 | **Child tasks created** | Auto: inside `_process_plan_output()` | **Planner** | For each child: `colony.carry()` creates `task-{slug}-01`, `task-{slug}-02` etc. in `ready/`. `capabilities_required=[]` (no recursive plans). `spawned_by` links to plan task. Plan task harvested with artifact `{created_task_ids, task_count, warnings, dep_summary}`. |
| 9 | **Builder forages** | Auto: `worker.py:250 run()` loop | **Builder** | Same forage flow. Scheduler: deps in `done_task_ids` → general worker (no specialized caps) skips tasks requiring `plan`/`review` → prefers non-overlapping `touches` → cooler hotspots → lower priority → oldest `created_at`. Multiple builders forage in parallel — each gets a different task (lock ensures atomicity). |
| 10 | **Builder workspace** | Auto: inside `_process_one_task()` | **Builder** | `workspace.py:26 create()` → fresh worktree branched from `origin/main`. Path: `.antfarm/workspaces/builder/{task_id}-{attempt_id}`. |
| 11 | **Builder launches agent** | Auto: inside `_process_one_task()` | **Builder** | `_launch_agent()` detects normal task. Prompt: *"Implement the task as specified... commit... push the branch"*. Launches `claude -p --agent worker --permission-mode bypassPermissions` in worktree. |
| 12 | **Builder harvests** | Auto: agent exits 0 | **Builder** | `_build_artifact()` → git diff stats, head/base SHA. `_create_pr()` → `gh pr create --title {title} --head {branch}`. POST `/tasks/{id}/harvest` → `os.rename(active/ → done/)`, attempt status → `done`. SSE `harvested` event emitted. |
| 12b | **Builder fails** | Auto: agent exits non-zero | **Builder** | `classify_failure()` with precedence: timeout → infra → lint → build → test → crash. Retry policy applied (`RETRY_POLICIES` dict). Trail entry with `[FAILURE_RECORD]` JSON logged. Task stays `active/` — doctor recovers. |
| 13 | **Soldier creates review task** | Auto: `soldier.py:112 process_done_tasks()` | **Soldier** (daemon) | Polls `list_tasks()`. For each done task: skips `review-*`, `plan-*`, already-merged, already-has-verdict. Creates `review-{task_id}` with `capabilities_required: ["review"]`, priority 1, spec includes branch + PR URL + review pack from artifact. |
| 14 | **Reviewer forages** | Auto: `worker.py:250 run()` loop | **Reviewer** | Scheduler: specialized cap `review` → only forages tasks where `capabilities_required` includes `review`. Picks up `review-{task_id}`. Reviewer has `_max_idle_polls=10` (polls 5 min before exit vs builders that exit immediately on empty queue). |
| 15 | **Reviewer launches agent** | Auto: inside `_process_one_task()` | **Reviewer** | `_launch_agent()` detects `is_review=True`. Extracts branch from spec via `_extract_branch_from_spec()`. Prompt: *"Read the PR diff... check for bugs, security, design... output [REVIEW_VERDICT] tags"*. Launches `claude -p --agent reviewer`. |
| 16 | **Verdict stored** | Auto: after reviewer agent exits 0 | **Reviewer** | `_parse_review_verdict()` extracts JSON from `[REVIEW_VERDICT]...[/REVIEW_VERDICT]`. Validates: `provider`, `verdict`, `summary` required; verdict must be `pass`/`needs_changes`/`blocked`. POST `/tasks/{original_id}/review-verdict` stores on original task's current attempt. |
| 17 | **Soldier checks merge queue** | Auto: `soldier.py:281 get_merge_queue()` | **Soldier** (daemon) | Filters: status=done + not `review-*` + not merged + has branch + all `depends_on` merged + review verdict = `pass` (via `check_review_verdict()` including SHA freshness check). Sort: `merge_override` position → priority → FIFO. |
| 18 | **Merge execution** | Auto: `soldier.py:337 attempt_merge()` | **Soldier** (daemon) | `git fetch origin` → `git checkout -b antfarm/temp-merge origin/main` → `git merge --no-ff {branch}` (conflict = FAILED) → `test_command` (default `pytest -x -q`, fail = FAILED) → `git checkout main` → `git merge --ff-only antfarm/temp-merge` → `git push origin main`. |
| 19 | **Mark merged** | Auto: after successful merge | **Soldier** (daemon) | `colony.mark_merged()` → attempt status set to `merged`. Task stays in `done/` — file doesn't move. Dependent tasks now have their deps satisfied in `merged_task_ids`. SSE `merged` event emitted. |
| 19b | **Kickback on failure** | Auto: merge conflict / test failure / review rejection | **Soldier** (daemon) | `colony.kickback()` → `os.rename(done/ → ready/)`. Current attempt → `superseded`, `current_attempt` → None. `_cleanup()` in `finally`: `git merge --abort` → checkout main → delete temp branch → `git clean -fd` → `git reset --hard origin/main`. |
| 20 | **Deps unblocked** | Auto: next soldier loop | **Soldier** (daemon) | Tasks whose `depends_on` are now all in `merged_task_ids` become eligible in `get_merge_queue()`. Builders still running will forage them on their next `_process_one_task()` iteration. |
| 21 | **Monitor** | Manual: `antfarm scout` | None | `tui.py` — live Rich dashboard. Stages: Waiting:New → Waiting:Rework → Planning → Building → Awaiting Review → Under Review → Merge Ready → Recently Merged. Polls `/status/full` every 2-5s. |
| 22 | **Diagnostics** | Manual: `antfarm doctor [--fix]` | None | Checks: filesystem, colony reachable, git config, stale workers (heartbeat > TTL), stale tasks (active + no live worker), stale guards, workspace conflicts, orphans. `--fix`: deregister stale workers, requeue stale tasks, delete stale guards. |

---

## Visual Pipeline

```
    YOU (manual steps only)
     |
     |-- antfarm colony
     |-- antfarm carry --type plan
     |-- antfarm worker start --type planner
     |-- antfarm worker start --type builder  (xN)
     |-- antfarm worker start --type reviewer
     |-- antfarm scout (watch)
     |
=============================== EVERYTHING BELOW IS AUTOMATED ===============

                    COLONY SERVER (FastAPI + Soldier thread)
                    +------------------------------------+
                    |  FileBackend: ready/ active/ done/ |
                    |  threading.Lock on pull/trail/etc  |
                    |  Soldier daemon (30s poll loop)    |
                    +----------------+-------------------+
                                     |
        +----------------------------+----------------------------+
        v                            v                            v
  +-----------+            +-------------+           +------------+
  |  PLANNER  |            |  BUILDER(s) |           |  REVIEWER  |
  |  worker   |            |  workers    |           |  worker    |
  +-----+-----+            +------+------+           +-----+------+
        |                         |                        |
        v                         v                        v
 +--------------+         +--------------+         +--------------+
 | Forage plan  |         | Forage task  |         |Forage review |
 | task from    |         | from ready/  |         | task from    |
 | ready/       |         |              |         | ready/       |
 +------+-------+         +------+-------+         +------+-------+
        |                        |                        |
        v                        v                        v
 +--------------+         +--------------+         +--------------+
 | git worktree |         | git worktree |         | git worktree |
 | create       |         | create       |         | create       |
 +------+-------+         +------+-------+         +------+-------+
        |                        |                        |
        v                        v                        v
 +--------------+         +--------------+         +--------------+
 | claude -p    |         | claude -p    |         | claude -p    |
 | --agent      |         | --agent      |         | --agent      |
 | planner      |         | worker       |         | reviewer     |
 +------+-------+         +------+-------+         +------+-------+
        |                        |                        |
        v                        v                        v
 +--------------+         +--------------+         +--------------+
 | Parse        |         | Commit, push |         | Parse        |
 | [PLAN_RESULT]|         | gh pr create |         | [REVIEW_     |
 | Create N     |         | harvest()    |         |  VERDICT]    |
 | child tasks  |         | -> done/     |         | Store on     |
 | in ready/    |         |              |         | orig task    |
 +--------------+         +--------------+         +--------------+
        |                        |                        |
        |          +-------------+                        |
        |          v                                      |
        |   +---------------------------------------------+
        |   |
        |   |     SOLDIER (daemon thread, auto)
        |   |     +----------------------------------------------+
        |   |     |                                              |
        |   |     |  process_done_tasks()                        |
        |   |     |    +-> create review-{id} --> reviewer       |
        |   |     |                                              |
        |   |     |  get_merge_queue()                           |
        |   |     |    +-> filter: done + deps merged            |
        |   |     |              + review=pass + has branch      |
        |   |     |                                              |
        |   |     |  attempt_merge()                             |
        |   |     |    |-> temp branch -> merge -> test -> push  |
        |   |     |    |     +-> SUCCESS: mark_merged()          |
        |   |     |    |                                         |
        |   |     |    +-> FAILURE: kickback()                   |
        |   |     |          task -> ready/ (new attempt on      |
        |   |     |          next forage, builder retries)       |
        |   |     |                                              |
        |   |     +----------------------------------------------+
        |   |
        v   v
 +--------------------------------------+
 |          TASK STATE MACHINE          |
 |                                      |
 |  READY --forage--> ACTIVE            |
 |    ^                  |              |
 |    |                  | harvest      |
 |    |                  v              |
 |    |               DONE              |
 |    |                  |              |
 |    |    kickback      |  merge       |
 |    +------------------+              |
 |                       v              |
 |                    MERGED            |
 |                  (attempt            |
 |                   status)            |
 +--------------------------------------+
```

---

## Summary

**4 manual commands. Everything else runs itself.**

| What you do | What runs automatically |
|---|---|
| `antfarm colony` | Soldier daemon thread (review + merge loop) |
| `antfarm carry --type plan` | — |
| `antfarm worker start --type planner` | Forage → worktree → agent → parse plan → create child tasks |
| `antfarm worker start --type builder` (xN) | Forage → worktree → agent → commit → push → PR → harvest |
| `antfarm worker start --type reviewer` | Forage review tasks → agent → parse verdict → store on original task |
| `antfarm scout` (optional) | — |
| `antfarm doctor` (optional) | — |

The full automated pipeline: **Plan → Build → Review → Merge**, with kickback loops for failures and dependency-aware scheduling for parallelism.
