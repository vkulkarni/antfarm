# Antfarm

Lightweight orchestration layer for distributing coding work across multiple machines running AI coding agents. Coordinates task assignment, workspace isolation, and safe integration — does NOT write code itself.

Core loop: `colony → join → carry → worker start → forage → work → trail → harvest → soldier integrates`

**Status:** Pre-release. Spec frozen (v1.0). Building v0.1.

## Build & Test

```bash
pip install -e ".[dev]"
pytest tests/ -x -q
ruff check .
ruff format .
```

## Code Standards

- Python 3.12+
- Ruff formatter + linter: `line-length = 100`, `target-version = "py312"`, rules: `E, F, I, W, UP, B, SIM`
- Type hints on public interfaces
- Google-style docstrings on public functions only
- One test file per module (`tests/test_<module>.py`)
- Prefer self-explanatory code over comments

## Workflow

- Branch from `main`. Use short-lived branches: `feat/`, `fix/`, `chore/`, `refactor/`, `test/`, `docs/`
- Open PRs to `main`. Squash merge by default. Delete branch after merge.
- Conventional Commits: `type(scope): description`
  - Types: `feat`, `fix`, `test`, `chore`, `refactor`, `docs`
  - Scopes: `models`, `backend`, `scheduler`, `server`, `worker`, `workspace`, `soldier`, `doctor`, `cli`, `adapter`, `ci`
- Run `ruff check .` and `pytest tests/ -x -q` before opening a PR.
- Each PR should leave `main` healthy, mergeable, and CI-green.
- See `docs/DEVELOPMENT.md` for the full workflow: PR process, CI pipeline, review checklist, release flow, issue tracking.

## Architecture

```
antfarm/core/models.py        — Task, Attempt, Worker, Node dataclasses + enums
antfarm/core/backends/base.py — TaskBackend ABC (explicit mutation methods, no generic update)
antfarm/core/backends/file.py — FileBackend: .antfarm/ filesystem queue (ready/ → active/ → done/)
antfarm/core/scheduler.py     — Selection order: deps → scope preference → priority → FIFO
antfarm/core/serve.py         — FastAPI colony server, single-process, threading.Lock on mutations
antfarm/core/worker.py        — Worker lifecycle: register → forage → workspace → launch → harvest → repeat
antfarm/core/workspace.py     — Git worktree creation, validation, orphan detection
antfarm/core/soldier.py       — Deterministic merge queue (no AI). Temp branch → test → FF or kickback
antfarm/core/doctor.py        — Pre-flight checks + stale recovery
antfarm/core/cli.py           — Click CLI entry point
antfarm/adapters/claude_code/ — Claude Code agent definitions + hooks
antfarm/adapters/generic/     — curl-based adapter examples
```

## Key Design Decisions

- **FileBackend IS the queue.** Claiming = `os.rename()` from `ready/` to `active/`. Atomic on POSIX.
- **`threading.Lock()` guards `pull`, `guard`, `trail`, `signal`.** Trail/signal are read-modify-write on JSON files — must be locked.
- **Soldier is deterministic, not AI.** Merge gate like CI. Merges to temp integration branch, tests, fast-forwards or kicks back. Never fixes code.
- **Attempt model.** Each forage creates a new attempt. Kickbacks supersede old attempts. Soldier only merges `current_attempt`.
- **`kickback()` moves tasks backward** — `done/` → `ready/`. Don't cache task locations.
- **No background scheduler.** Runs on-demand during forage.
- **POSIX only, no auth in v0.1.** Trusted private networks only.
- **`.antfarm/` is runtime state** — never commit.

## Docs

- `docs/SPEC.md` — product and architecture spec (frozen). Authoritative for design intent.
- `docs/IMPLEMENTATION.md` — v0.1 build plan with code examples. Authoritative for code-level interfaces and execution details. If it appears to conflict with `docs/SPEC.md`, flag the inconsistency rather than silently choosing one.
- `docs/DEVELOPMENT.md` — full dev workflow: branching, commits, PRs, CI, release, 17-PR build sequence.
