# Changelog

All notable changes to Antfarm are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [0.6.12] - 2026-04-19

### Fixed
- **#338: Soldier dirty-check now untracked-safe** — dirty detection uses `git diff --quiet HEAD` instead of `git status --porcelain`, so untracked files (e.g. `.antfarm/`) no longer cause false-dirty false alarms that blocked merges. (#345)
- **#344: Doctor reads colony URL from `config.json`** — `check_colony_reachable` now reads `{data_dir}/config.json` for `colony_url` instead of falling back to a hardcoded localhost default, so remote colonies are probed correctly. (#345)

## [0.6.11] - 2026-04-19

### Changed
- **Queen instructs planner to maximize parallelizable task count** — system prompt updated to explicitly direct the planner agent to decompose work into the maximum number of independently-executable tasks, reducing sequential bottlenecks in mission execution. (#343, closes #322)

## [0.6.10] - 2026-04-19

### Changed
- **Builder idle-poll limit raised to 20** — `max_empty_polls` default for builder workers increased from 5 to 20, amortizing the one-time workspace setup cost across more forage cycles before exit. (#342, refs #321)

## [0.6.9] - 2026-04-19

### Added
- **Autoscaler depth-aware scaling with hysteresis** — autoscaler now considers queue depth (ready + active task counts) when computing target worker count; a hysteresis band prevents rapid scale-up/down oscillation on transient queue fluctuations. (#341, closes #320)

## [0.6.8] - 2026-04-18

### Added
- **Retry-pattern surfacing in doctor** — `doctor` now detects tasks that have been kicked back repeatedly and reports them as `retry_pattern` findings with kick count and last failure reason. (#325)
- **Retry-pattern failures in inbox** — inbox view surfaces tasks with high kick counts so operators can spot stuck work without reading the full task trail. (#325)

### Fixed
- **#333: `recover` honors `max_attempts`** — stale-task recovery no longer re-queues tasks that have already exhausted their attempt budget; heartbeat now spans the full `worker.run()` lifecycle instead of stopping before the loop. (#334)
- **#331: TUI mission merged count** — merged-task count is now computed from live task attempt statuses rather than a stale cached counter, fixing the count going stale mid-mission. (#332)
- **#327/#326: Activity log surfaces type+task_id; preflight validates `test_command`** — Soldier activity log entries now include event type and task ID for traceability; colony preflight asserts `test_command` is configured before starting the Soldier. (#330)
- **Soldier `run()` routes through `run_once_with_review`** — ensured `require_review=True` flows correctly so `needs_changes` verdicts reliably trigger kickbacks; added kickback observability. (#329)
- **Soldier/server preserve `.antfarm/` under `git clean`** — data directory is added to `.gitignore` on colony start, and Soldier cleans only tracked files, preventing accidental data loss on `git clean -fd`. (#324)
- **#302/#303: Atomic `_write_json` with fsync + locked heartbeat** — task-file writes now use a write-rename-fsync pattern to eliminate torn writes; heartbeat updates are serialized under `_lock`. (#319)
- **#301: Configurable `agent_timeout`** — `antfarm worker start` accepts `--agent-timeout` (seconds) passed to `subprocess.run`, preventing hung agent processes from blocking the worker loop. (#318)
- **#308/#309: Lock `release_guard` and `_emit_event`** — both methods now acquire `_lock`, closing a race where concurrent releases or event emissions could corrupt guard state or drop events. (#317)
- **#311: Soldier pre-flight clean-state assert + force-recover** — Soldier asserts repo is in a clean state before each merge attempt; a `_force_recover()` routine resets dirty state (aborts in-progress merge/rebase, checks out integration branch) so transient git failures don't wedge the loop. (#316)
- **Soldier diff-equivalence check includes `conftest.py`** — byte-identical diff check for skip-re-review now includes `conftest.py` so test-fixture-only changes are not incorrectly skipped. (#315)
- **#314: Epoch-tagged SSE events + TUI backoff** — SSE events carry a monotonic epoch so TUI consumers can detect stream restarts and reconnect with exponential backoff instead of spinning. (#314)
- **Doctor `--fix` closes TOCTTOU races** — stale-guard and stale-worker cleanup now re-validates staleness after acquiring the file lock, preventing double-fix races under concurrent doctor runs. (#313)
- **#305: Validate attempt currency in `mark_merged`** — `mark_merged` rejects attempts that are no longer `current_attempt`, preventing a race where a superseded attempt is marked merged after a kickback. (#312)
- **#296: Mission cancel purges live tasks** — cancelling a mission now moves all in-flight tasks to `done/` with cancellation metadata rather than leaving them stranded in `active/`. (#296)
- **#266: Doctor warns on no reviewer capacity** — doctor now reports a warning when tasks in `ready/` have `role=reviewer` but no reviewer workers are registered. (#297)

## [0.6.7] - 2026-04-17

Efficiency pass (P1–P6): eliminates the ~40% of mission wall-clock that was being lost to avoidable rebuilds, empty-queue worker exits, and stale merge scheduling. All changes are deterministic (no AI).

### Added
- **Builder branches from unmerged dep branch** — when a task has exactly one unmerged dependency, its worktree now bases on the dep's PR branch instead of `origin/<integration>`, eliminating merge-base drift rebuilds. Zero / multi-dep cases fall back to the integration branch. (#291)
- **Soldier event-driven merge trigger** — Soldier subscribes to the `/events` SSE stream and reacts to `review_approved` / `harvested` / `merge_requested` within ~1s; fixed polling remains as a fallback on connection error. (#289)
- **Soldier merge diagnostic events** — emits `merge_attempted`, `merge_skipped`, and `merge_failed` with stable reason codes (`dep_unmerged`, `no_pr`, `superseded`, `needs_changes`, `merge_conflict`, `test_failed`, `rebase_failed`, `rebase_retry_merge_failed`, `checkout_failed`, `push_failed`) so operators can trace why a done task hasn't merged. (#293)
- **Configurable worker empty-poll backoff** — `antfarm worker start` accepts `--poll-interval` and `--max-empty-polls`; workers now sleep and re-poll instead of exiting on first empty forage, with role-based defaults (reviewer=10, builder=5, planner=0). Closes #272. (#290)

### Changed
- **Soldier rebases before kickback** — on merge conflict, Soldier attempts a deterministic `git rebase origin/<integration>` and `--force-with-lease` push before a single merge retry; genuine rebase conflicts still kick back. (#292)
- **Skip re-review on pure-rebase reharvest** — when a reharvested attempt has a byte-identical code diff (tests pathspec-excluded, `--ignore-all-space` normalized) to the reviewed prior attempt, the prior `pass` verdict carries forward instead of running a fresh ~3-minute review. `needs_changes` never carries forward; any git failure falls through to the safe re-ready path. (#294)

### Fixed
- **#284: Soldier kicks back on `needs_changes` verdicts** — `require_review=True` now flows through `serve.py::_start_soldier_thread` to the Soldier constructor, so `run_once_with_review` (not `run_once`) is the loop being scheduled and `needs_changes` reliably converts into a kickback within one tick. Regression test added. (#292)

## [0.6.6] - 2026-04-17

### Added
- Live activity feed (#191): `/events` SSE stream now carries lifecycle events from Queen, Autoscaler, Runner, Soldier, Doctor, and Worker with an `actor` field identifying the emitting subsystem. New `scout --watch` CLI streams events as `HH:MM:SS  <actor>  <detail>` lines. TUI gains a bottom "Activity" panel that auto-updates via SSE. (#274, #275, #277, #278, #281, #282, #283, #285)
- `_emit_event(event_type, task_id, detail, actor="colony")` — `actor` parameter added with backward-compatible default; existing `harvested`/`kickback`/`merged` emissions continue to work unchanged. (#277)

### Changed
- Colony identity is now a persisted UUID stored as `colony_id` in `{data_dir}/config.json`. Tmux session prefixes (`auto-<hash>-*`, `runner-<hash>-*`) derive from this UUID via the new `colony_session_hash()`, making identity stable across `mv`, NFS, and Docker bind-mounts. `colony_hash()` remains as a pure hashing primitive (used by `deploy.py`). **Breaking:** first startup after upgrade generates a new UUID; pre-upgrade tmux sessions use the old realpath-based hash and become orphans — run `antfarm doctor --sweep-legacy-tmux` after draining in-flight work. See UPGRADE.md for the escape hatch to preserve an old hash. (#238)

## [0.6.5] - 2026-04-17

### Fixed
- TUI: plan/review infra tasks no longer inflate the Merge Ready panel or the Pipeline progress bar — classification now delegates to the canonical `is_infra_task()` helper used by the Soldier. (#267)
- Soldier: reconcile externally-merged PRs by polling `gh pr view` before attempting a local merge, unblocking downstream dependents when operators merge on GitHub directly. Fail-safe on errors (falls through to normal merge path). (#264)

### Added
- `antfarm mark-merged <task_id> --attempt-id <att>` CLI: operator escape hatch to mark an attempt as MERGED when a PR was merged outside Antfarm and auto-reconciliation is unavailable. (#264)

## [0.6.4] - 2026-04-16

### Added
- Document deploy identity model in UPGRADE.md — how (realpath config + colony_url) hash determines session ownership, with examples of shared vs isolated namespaces and the localhost-tunnel edge case. (#244)
- Per-worker current-action visibility: `POST /workers/{id}/activity`, TUI Activity column with elapsed time, and doctor `check_stuck_workers` warning for workers idle on an action > 5 min. (#239)
- `antfarm doctor --sweep-legacy-tmux` flag (with `--yes`) to clean pre-#231/#235 tmux sessions host-wide. Requires interactive confirmation by default. (#237)
- Colony startup now logs `colony hash: <8hex> (data_dir: <realpath>)` so operators can correlate tmux session names to a colony. (#237)
- `UPGRADE.md` — migration notes for session-name format changes in #231 and #235. (#237)

### Fixed
- TUI now shows actionable guidance when the colony is unreachable, including the attempted URL and commands to start/redirect. (#246)
- Doctor `check_orphan_tmux_sessions` hardened with `LC_ALL=C` (locale-independent stderr matching) and stderr truncation at 200 chars, so log output stays predictable and benign-race markers match reliably across locales. (#242)
- Doctor `--fix` tolerates the `tmux kill-session` race when a session exits between list and kill; benign "can't find session" / "session not found" / "no server running" messages no longer produce spurious error findings. (#236)

### Changed
- The `colony hash: ...` startup log now fires once per server startup (FastAPI startup event) instead of on every `get_app()` call, reducing noise in test suites. (#249)
- `deploy.py` tmux session names are now colony-scoped with an 8-char hash of `(realpath(fleet_config) | colony_url)`. **Breaking:** pre-upgrade deploy sessions won't be found by `deploy status` — kill them manually via `tmux kill-session` and redeploy. (#235)

## [0.6.3] - 2026-04-16

### Changed
- Tmux session names now include an 8-char SHA-256 hash of the colony's resolved `data_dir` (format: `auto-{hash}-{role}-{N}` for autoscaler, `runner-{hash}-{role}-{N}` for Runner). This scopes orphan detection to the current colony so peer colonies on the same host are ignored. Doctor's `check_orphan_tmux_sessions` is restored to `warning` severity with `auto_fixable=True`, and `antfarm doctor --fix` now safely `tmux kill-session`s own orphans. Backwards-compat caveat: tmux sessions spawned by pre-upgrade builds lack the hash prefix and become unmanaged — operators should `tmux kill-session -t <name>` them after upgrading. (#231)

### Fixed
- `register_worker` tolerates stale prior registrations — overwrites worker files whose heartbeat has expired instead of returning 409, preventing autoscaler crashes on colony restart (#194)
- Autoscaler no longer reaps workers whose heartbeat is still fresh; prevents healthy builders from being killed mid-task when the reaper loop races with a just-started worker (#220)
- Soldier re-reviews kicked-back tasks when the attempt SHA changes — previously review tasks persisted forever in `done/` and new attempts got stuck waiting for a review that would never re-run. Attempt-SHA marker embedded in review task spec enables detection (#226)
- Queen writes the `mission_context` blob at the start of the BUILDING phase and on re-plan. Previously the file was never created and `GET /missions/{id}/context` silently 404'd, losing prompt-cache benefit on every multi-worker mission. Also threads `data_dir`/`repo_path`/`integration_branch` from `config.json` into the Queen so its file writes land where the server reads them (#219)
- Kickback, rereview, resume, and reassign now close the superseded attempt's PR with a comment. Previously superseded PRs accumulated as open duplicates, noising the PR list and wasting CI minutes. New `PROps` abstraction (`antfarm/core/pr_ops.py`) with `GhPROps` (shells out to `gh pr close`) and `NullPROps` (tests/no-gh default). PR close runs outside the backend lock to prevent subprocess-in-lock deadlocks (#222)
- Doctor `orphan_tmux_session` severity temporarily downgraded to `info` so `test_healthy_colony_no_findings` doesn't fail on hosts running peer-colony tmux sessions (stopgap). This was superseded by #231 in the same release, which restores `warning` severity via colony-scoped session naming (#229, #230)
- `antfarm` CLI startup now configures logging so `logger.info`/`logger.warning` actually surface; previously log calls silently no-op'd when the module was imported without calling `setup_logging` (#214)
- `antfarm.__version__` is now sourced from installed package metadata instead of a hardcoded constant, so editable installs no longer drift from the true version (#213)

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
