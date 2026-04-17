# Changelog

All notable changes to Antfarm are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Changed
- Tmux session names now include an 8-char SHA-256 hash of the colony's resolved `data_dir` (format: `auto-{hash}-{role}-{N}` for autoscaler, `runner-{hash}-{role}-{N}` for Runner). This scopes orphan detection to the current colony so peer colonies on the same host are ignored. Doctor's `check_orphan_tmux_sessions` is restored to `warning` severity with `auto_fixable=True`, and `antfarm doctor --fix` now safely `tmux kill-session`s own orphans. Backwards-compat caveat: tmux sessions spawned by pre-upgrade builds lack the hash prefix and become unmanaged — operators should `tmux kill-session -t <name>` them after upgrading. (#231)

### Fixed
- `register_worker` tolerates stale prior registrations — overwrites worker files whose heartbeat has expired instead of returning 409, preventing autoscaler crashes on colony restart (#194)

## [0.6.2] - 2026-04-16

### Added
- **ProcessManager abstraction** (`antfarm/core/process_manager.py`) — uniform interface for spawning, stopping, and adopting worker processes across backends (#204)
- `TmuxProcessManager` — spawns workers via `tmux new-session -d`, gives each worker a real TTY, enables restart adoption by re-discovering existing sessions (#204)
- `SubprocessProcessManager` — fallback backend using `subprocess.Popen`; explicitly no restart adoption (degraded mode) (#204)
- `ProcessMetadata` JSON files in `{state_dir}/processes/` replacing raw PID files; stores manager_type, session/pid, command, started_at
- `parse_session_name(name, prefix)` helper — caller-supplied prefix, returns `(suffix, index) | None`
- Doctor check `check_tmux_available` — warns when tmux is not installed (subprocess fallback is less reliable) (via #208)
- Doctor check `check_orphan_tmux_sessions` — flags tmux sessions with antfarm prefixes (`auto-`, `runner-`) whose ProcessMetadata file is missing (via #208)
- One-time startup warning in `serve.py` when tmux is unavailable (via #208)

### Changed
- Autoscaler `_start_worker` / `_stop_worker` / `_adopt_existing` now delegate to the configured ProcessManager (#208)
- Runner worker spawning now delegates to the configured ProcessManager (#209)
- Worker process lifecycle is now backend-agnostic — tmux is the default when available, subprocess is the fallback

## [0.6.1] - 2026-04-15

### Added
- **Runner daemon** — desired-state reconciliation for fixed worker pools; complements the elastic Autoscaler (#183)
- **Actuator abstraction** — pluggable placement strategies for multi-host worker provisioning (#184, #185)
- **Multi-node autoscaler** — shared scaling logic works across nodes via shared Actuator (#186)
- **Prompt cache sharing** — context generation and prepend for worker prompts to maximize cache hits (#187)
- Node model gains `runner_url`, `max_workers`, `capabilities` fields (#181)
- Backend gains `list_nodes()`, `get_node()`, extended node registration (#182)
- Server node endpoints expose the extended Node fields (#182)
- CLI: `antfarm runner` command + doctor checks + end-to-end tests (#188, #189, #190)

### Fixed
- Workers no longer exit on empty queue — they poll (#144 / #180)
- Reviewer retries when `[REVIEW_VERDICT]` tags are missing instead of failing silently (#143 / #179)
- Missing `planner.md` agent definition caused silent failure; agents now copied into worktrees (#192)
- `claude -p` prompt now passed via stdin to avoid argv length limits (#192)
- Planner harvest failures are now logged instead of suppressed; artifact preserved (#195)
- Doctor encodes slashes in `worker_id` for file lookups — no more false stale recovery (#196)
- Queen falls back to the plan task when a review verdict is not found on the review task (#197)

## [0.6.0] - 2026-04-11

### Added
- **Autonomous Runs (Missions):** end-to-end orchestration from spec to morning digest
- Mission model: `Mission`, `MissionConfig`, `PlanArtifact`, `MissionReport` dataclasses
- Queen controller daemon thread — advances missions through planning → review → building → complete
- Plan-review flow with re-plan budget (max 1 re-plan per mission)
- Single-host Autoscaler daemon thread — subprocess-based, scope-aware worker spawning (opt-in via `--autoscaler`)
- Mission report generator with JSON, terminal, and markdown renderers (dependency-free, no `rich` required)
- Colony API: `/missions` CRUD endpoints, `mission_id` on `POST /tasks`, `?mission_id=` filter on `GET /tasks`
- CLI: `antfarm mission create|status|report|cancel|list`, `antfarm carry --mission`
- CLI: `antfarm colony --autoscaler|--no-queen` flags
- TUI: mission panel showing status, task counts, and progress
- `link_task_to_mission()` shared atomicity helper for carry + mission linkage
- `is_infra_task()` canonical filter for plan/review vs implementation tasks
- Planner mission-mode: stores plan as `PlanArtifact` on attempt (does not carry children)
- Soldier `mission_id` propagation: review tasks inherit parent's mission, suppressed for cancelled missions
- Failure-reason prefix convention (`system:` vs `review:`) for mission diagnostics
- `completion_mode="all_or_nothing"` accepted and persisted (treated as `best_effort` in v0.6.0)
- GitHubBackend mission stubs with actionable error messages and preflight guard
- API stability commitment: `/missions` schema frozen for v0.6.x
- 4 end-to-end mission test scenarios (full loop, cancel, blocked task, plan review re-plan)

### Changed
- `extract_verdict_from_review_task` moved from Soldier staticmethod to `review_pack.py` (public, shared)
- `Task` dataclass gains `mission_id: str | None` field
- `TaskArtifact` gains `plan_artifact: dict | None` field
- `TaskBackend` ABC gains `create_mission/get_mission/list_missions/update_mission` abstract methods
- `FileBackend` gains `.antfarm/missions/` directory for mission persistence
- Colony `/status` and `/status/full` endpoints include `queen` and `autoscaler` status

## [0.5.0] - 2026-04-05

### Added
- Canonical scheduler — single scheduling brain, no inline scheduling in backends
- Task/attempt lifecycle with enriched states (TaskState, AttemptState, HARVEST_PENDING)
- Lifecycle transition validators with backward-compatible state mapping
- Failure taxonomy: classify_failure() with 8 failure types and default retry policies
- FailureRecord structured failure data persisted on attempts
- Operator inbox (`antfarm inbox`) — surfaces stale workers, blocked/failed/kicked-back tasks
- TaskArtifact: structured output with hard evidence + advisory split, freshness SHAs
- ReviewVerdict contract for structured review outcomes
- Review pack generation from artifacts (`review_pack.py`)
- Review-as-task flow — Soldier creates review tasks, reviewer workers produce verdicts
- Merge gating on artifact + freshness + review verdict (autonomous loop)
- Reviewer agent definitions for Claude Code and Codex adapters
- Repo memory — trusted facts, task outcomes, hotspots, failure patterns, touch observations
- Conflict prevention — overlap warnings on carry, conflict risk scoring
- Scheduler hotspot weighting (deprioritize hot scopes)
- AI-assisted task decomposition (`antfarm plan --spec/--file`)
- Memory CLI commands (`antfarm memory show/set-fact/detect/recompute`)
- Audit trail enrichment with `action_type` on TrailEntry
- Architecture and Operator Guide documentation
- mark_harvest_pending endpoint and lifecycle state
- task_id sanitization against path traversal
- ColonyClient.carry() and store_review_verdict() methods

### Fixed
- TUI rendering bug — current_attempt is string ID, not dict
- SHA comparison in check_freshness and check_review_verdict — proper matching with min 7 chars
- Multiple scheduling brains consolidated to scheduler.select_task()

### Changed
- FileBackend.pull() delegates entirely to scheduler.select_task()
- mark_harvested() accepts optional artifact dict
- TrailEntry supports optional action_type field (backward compatible)

## [0.4.0] - 2026-04-05

### Added
- GitHub Issues backend — tasks stored as GitHub Issues with label-based status tracking (`antfarm colony --backend github`)
- Codex adapter — agent definitions + hooks for OpenAI Codex CLI (`--approval-mode full-auto`)
- Aider adapter — agent definitions + convention file (`--yes --no-auto-commits`)
- Import command — import tasks from GitHub Issues or JSON files (`antfarm import --from github/json`)
- `--backend` option on colony command (file or github)

## [0.3.0] - 2026-04-05

### Added
- TUI dashboard with `rich` library (`antfarm scout --tui`) — 4-panel live display with color-coded tasks, workers, and merge queue
- Colony failover with periodic rsync backup (`antfarm backup now/restore/status`, `colony --backup-dest`)
- Rate limit awareness — workers report cooldown via heartbeat, scheduler skips rate-limited workers
- Pin command — pin tasks to specific workers (`antfarm pin/unpin`)
- Override-order command — override merge queue position (`antfarm override-order`)
- `GET /status/full` endpoint — combined status + tasks + workers in one call
- `GET /workers` endpoint — list all workers with rate limit status
- `antfarm workers` CLI command

### Changed
- `rich>=13.0` added as core dependency
- Heartbeat accepts optional rate limit fields (remaining, reset_at, cooldown_until)
- Scheduler filters by worker capabilities AND pin assignment AND rate limit cooldown

## [0.2.0] - 2026-04-05

### Added
- Bearer token authentication for colony API (`--auth-token` on colony, `--token` on all commands)
- Human override commands: `pause`, `resume`, `reassign`, `block`, `unblock`
- PAUSED and BLOCKED task statuses
- Deploy command for SSH-based multi-node worker launch (`antfarm deploy --fleet-config`)
- Capability-aware scheduling: tasks can declare `capabilities_required`, workers declare `capabilities`
- Scent command for real-time trail streaming via SSE (`antfarm scent <task-id>`)
- Scout `--watch` flag for continuous status polling with change highlighting
- ColonyClient HTTP wrapper for worker-to-colony communication

### Security
- Shell injection prevention in deploy command (`shlex.quote()` on all config values)
- HMAC-SHA256 token generation with timing-safe comparison
- ANTFARM_TOKEN propagated to subprocess env for spawned agents

## [0.1.1] - 2026-04-05

### Fixed
- Correct Claude Code invocation: `claude -p --agent worker --permission-mode bypassPermissions`
- Added `--integration-branch` CLI option to `worker start`
- Path traversal guard in `WorkspaceManager.create()`
- Dead tuple expression in `test_validate_dirty`
- Missing state consistency checks in doctor
- Vacuous test assertion in `test_exit_deregisters_on_exception`

## [0.1.0] - 2026-04-05

### Added
- Colony API server (FastAPI) with task queue, scheduler, and merge queue
- FileBackend with atomic task claiming
- Scope-aware task scheduler (dependencies, scope overlap, priority, FIFO)
- Worker runtime lifecycle (register, forage, workspace, launch, harvest, repeat)
- Soldier deterministic merge gate (temp integration branch, test gating, kickback)
- Task attempt model with superseded semantics
- Doctor pre-flight checks and stale recovery (dry-run and --fix modes)
- CLI with 13 commands: colony, join, carry, worker start, forage, trail, harvest, scout, doctor, hatch, guard, release, signal
- Claude Code reference adapter (agent definitions + hooks)
- Generic curl adapter (shell scripts)
- End-to-end integration test
- 112+ tests
