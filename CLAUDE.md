# Antfarm — Claude Code Config

Shared project context is in AGENTS.md. This file is Claude-specific execution guardrails.

## @imports

See @AGENTS.md for project overview, commands, code standards, architecture, and workflow
See docs/SPEC.md for product and architecture spec (frozen, authoritative for design)
See @docs/IMPLEMENTATION.md for v0.1 build phases, module map, code examples (authoritative for v0.1 code-level interfaces and execution details)
See @docs/DEVELOPMENT.md for full dev workflow: branching, PRs, CI, release, 17-PR build sequence

## Agent Teams

For parallel work, use TeamCreate → TaskCreate → spawn teammates with `team_name`. Engineers that need their own branch MUST use `isolation: "worktree"` to avoid filesystem conflicts with other sessions/agents. Never spawn engineers on the shared filesystem when another session is active. Worktree engineers push to remote; reviewer pulls from remote.

Use the defined agent roles in `.claude/agents/`:

- **planner** (opus, read-only) — researches codebase, produces implementation plan, sends for approval
- **engineer** (sonnet, all tools) — executes approved plans, commits, opens PRs. Always use `isolation: "worktree"`
- **researcher** (sonnet, read-only) — pure investigation, returns structured findings
- **reviewer** (sonnet, read-only) — PR review + test suite verification before merge

## Workflow

- **Research → Plan → Implement** — For non-trivial changes: (a) planner researches and sends plan for approval, (b) engineer implements after approval, (c) reviewer verifies PR before merge. 
- **PR review before merge** — ALWAYS spawn a reviewer agent to read the full PR diff, run the test suite, and check for edge cases BEFORE merging. Never skip this step. Never trust self-reported test results.
- **Never push to main without permission** — dev → main merges and pushes ONLY when the user explicitly requests it. Never merge to main autonomously.

## Guardrails

- Read the relevant module section in `docs/IMPLEMENTATION.md` before implementing or changing a module — it has code examples, edge cases, and test lists.
- `docs/SPEC.md` - product and architecture spec; consult for design intent.
- Do not add AI/LLM logic to the Soldier — it is purely deterministic.
- Use explicit TaskBackend mutation methods (`carry()`, `mark_harvested()`, `kickback()`) — never a generic `update(**fields)`.
- Trail/signal appends must be under `threading.Lock()` — read-modify-write on JSON.
- Do not bypass frozen spec scope. If something isn't in v0.1, don't add it.
- Run `ruff check .` and `pytest tests/ -x -q` before proposing any PR.
- Follow `docs/DEVELOPMENT.md` for branching and PR process.
