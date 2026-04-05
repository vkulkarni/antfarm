# Changelog

All notable changes to Antfarm are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

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
