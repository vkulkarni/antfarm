# Antfarm v0.6 — Specification

**Status:** Draft
**Date:** 2026-04-05
**Prerequisite:** v0.5 shipped (canonical scheduler, structured artifacts, repo memory, planner, conflict prevention)
**Goal:** Make Antfarm a first-class Claude Code experience — MCP server, plugin with slash commands, hooks, and subagents.

---

## Philosophy

**Antfarm core stays outside Claude. Plugin is the UX layer. MCP is the bridge.**

- Claude helps Antfarm think and act
- Antfarm remembers and decides safely
- State lives in Antfarm storage, not chat history
- Scheduling, merging, and memory are never session-dependent

---

## What v0.6 IS

A Claude Code plugin that wraps Antfarm's HTTP API into native Claude Code experiences:

1. **Slash commands** — `/antfarm-plan`, `/antfarm-status`, `/antfarm-review` etc.
2. **MCP server** — Claude Code calls Antfarm tools directly (create tasks, claim work, report results)
3. **Hooks** — after code changes, update artifact; on failure, classify and kickback
4. **Subagents** — planner and reviewer roles as Claude Code agent definitions

The result: a developer opens Claude Code, types `/antfarm-plan "build auth system"`, and Antfarm decomposes it, assigns workers, and coordinates the build — all from within Claude Code.

## What v0.6 IS NOT

- Not making Antfarm dependent on Claude internals
- Not encoding task state in prompts
- Not letting Claude own scheduling or merge gating
- Not a web app or dashboard replacement
- Not an enormous plugin — thin productivity layer only

---

## Architecture

```
┌─────────────────────────────────────────────┐
│           Claude Code Session               │
│                                             │
│  /antfarm-plan "build auth"                 │
│  /antfarm-status                            │
│  /antfarm-review task-42                    │
│                                             │
│  ┌──────────────────────────────────┐       │
│  │  Antfarm Plugin                  │       │
│  │  • slash commands (skills)       │       │
│  │  • agent definitions (.md)       │       │
│  │  • hooks (PostToolUse)           │       │
│  └──────────┬───────────────────────┘       │
│             │ MCP tools                     │
│  ┌──────────▼───────────────────────┐       │
│  │  Antfarm MCP Server              │       │
│  │  • HTTP bridge to colony API     │       │
│  │  • Tool schemas for Claude       │       │
│  └──────────┬───────────────────────┘       │
└─────────────┼───────────────────────────────┘
              │ HTTP + JSON
              ▼
┌─────────────────────────────────────────────┐
│  Antfarm Colony (unchanged)                 │
│  • Scheduler • Backend • Soldier • Memory   │
└─────────────────────────────────────────────┘
```

---

## Components

### 1. MCP Server (#84)

**What:** A local MCP server that exposes Antfarm's colony API as MCP tools. Claude Code discovers and calls these tools natively.

**Location:** `antfarm/mcp/server.py`

**Tools exposed:**

| Tool | Maps to | Description |
|------|---------|-------------|
| `antfarm_carry` | `POST /tasks` | Create a task |
| `antfarm_plan_spec` | Planner (v0.5) | Decompose a spec into tasks |
| `antfarm_list_tasks` | `GET /tasks` | List tasks by status |
| `antfarm_forage` | `POST /tasks/pull` | Claim next task for a worker |
| `antfarm_trail` | `POST /tasks/{id}/trail` | Log progress |
| `antfarm_harvest` | `POST /tasks/{id}/harvest` | Mark task complete with artifact |
| `antfarm_status` | `GET /status/full` | Colony status + tasks + workers |
| `antfarm_blockers` | `GET /tasks?status=blocked` + signals | List blocked tasks and signals |
| `antfarm_memory` | Memory store (v0.5) | Get repo facts, hotspots, failure patterns |
| `antfarm_review_pack` | Review pack (v0.5) | Get review pack for a completed task |
| `antfarm_merge_ready` | `GET /tasks?status=done` | List tasks ready for merge |
| `antfarm_workers` | `GET /workers` | List active workers |

**Configuration:** Users point the MCP server at their colony:

```json
{
  "mcpServers": {
    "antfarm": {
      "command": "python3",
      "args": ["-m", "antfarm.mcp.server"],
      "env": {
        "ANTFARM_URL": "http://localhost:7433",
        "ANTFARM_TOKEN": "optional-bearer-token"
      }
    }
  }
}
```

**Protocol:** MCP stdio transport (standard for Claude Code MCP servers). The server reads JSON-RPC from stdin, calls the colony HTTP API, and writes results to stdout.

**Complexity:** M

---

### 2. Slash Commands (Plugin Skills)

**What:** Claude Code skills bundled as a plugin. Users install once, get commands in every session.

**Location:** `antfarm/plugin/skills/`

| Command | What it does |
|---------|-------------|
| `/antfarm-plan` | Takes a spec/issue, runs the v0.5 planner, shows proposed tasks, carries on confirmation |
| `/antfarm-status` | Shows colony status in a formatted view (nodes, workers, task counts, blockers) |
| `/antfarm-blockers` | Lists blocked/failed/stale tasks that need attention |
| `/antfarm-review` | Shows review pack for a specific task (or all done tasks) |
| `/antfarm-start` | Registers as a worker, forages a task, sets up workspace — one command to start working |
| `/antfarm-done` | Harvests current task with artifact, forages next — one command to finish and continue |
| `/antfarm-merge-ready` | Lists tasks ready for Soldier to merge |

**Each skill is a markdown file** with a system prompt that instructs Claude Code to call the MCP tools. Example:

```markdown
# /antfarm-plan

Takes a feature spec and decomposes it into Antfarm tasks.

## Instructions

1. Read the user's spec (argument or file)
2. Call `antfarm_memory` tool to get repo facts (test command, build command, hot files)
3. Decompose into 3-10 tasks with: title, spec, depends_on, touches, priority
4. Show the proposed tasks to the user
5. On confirmation, call `antfarm_carry` for each task
6. Show final task list with `antfarm_status`
```

**Complexity:** S per skill, M total

---

### 3. Hooks

**What:** Claude Code hooks that fire automatically to keep Antfarm in sync.

**Location:** `antfarm/plugin/hooks/`

| Hook | Event | What it does |
|------|-------|-------------|
| `heartbeat.sh` | PostToolUse | Sends heartbeat to colony (already exists in v0.4 adapter) |
| `artifact_update.sh` | PostToolUse | After file edits, updates task artifact with changed files count |
| `on_complete.sh` | Stop | On session end, harvests current task if in progress |
| `on_failure.sh` | PostToolUseFailure | Classifies failure and trails it |

**Complexity:** S

---

### 4. Agent Definitions (Subagents)

**What:** Claude Code agent definitions for Antfarm roles. These already partially exist in `adapters/claude_code/agents/`.

**Location:** `antfarm/plugin/agents/`

| Agent | Role | When to use |
|-------|------|-------------|
| `worker.md` | Engineer | Forage a task, implement it, harvest | 
| `planner.md` | Decomposer | Break a spec into tasks via MCP tools |
| `reviewer.md` | Reviewer | Read a PR diff, generate review pack via MCP tools |

These are enhanced versions of the existing adapter agents — they use MCP tools instead of CLI commands, making them faster and more integrated.

**Complexity:** S

---

### 5. Plugin Package

**What:** A single installable Claude Code plugin that bundles MCP server + skills + hooks + agents.

**Location:** `antfarm/plugin/`

**Structure:**
```
antfarm/plugin/
  package.json          # Plugin manifest
  mcp/
    server.py           # MCP server (stdio transport)
    tools.py            # Tool definitions + handlers
  skills/
    antfarm-plan.md     # /antfarm-plan skill
    antfarm-status.md   # /antfarm-status skill
    antfarm-blockers.md # /antfarm-blockers skill
    antfarm-review.md   # /antfarm-review skill
    antfarm-start.md    # /antfarm-start skill
    antfarm-done.md     # /antfarm-done skill
  hooks/
    heartbeat.sh
    artifact_update.sh
    on_complete.sh
  agents/
    worker.md
    planner.md
    reviewer.md
```

**Installation:**
```bash
# From the antfarm repo
claude plugins install ./antfarm/plugin

# Or from npm (future)
claude plugins install antfarm-plugin
```

**Complexity:** M (packaging + testing)

---

## Release Slices

### v0.6.0-alpha.1 — MCP Server
- MCP server with stdio transport
- 12 tool definitions mapping to colony API
- Tests with mock colony
- Manual MCP config (users add to `.claude/settings.json`)

### v0.6.0-alpha.2 — Slash Commands
- 7 skill definitions
- Plugin package.json
- `claude plugins install` workflow

### v0.6.0-alpha.3 — Hooks + Agents
- 4 hooks (heartbeat, artifact_update, on_complete, on_failure)
- 3 agent definitions (worker, planner, reviewer)
- End-to-end test: `/antfarm-plan` → workers build → `/antfarm-review` → merge

### v0.6.0 — Polish
- Documentation: plugin install guide, MCP configuration, skill reference
- Error handling: colony down, auth failures, timeout
- Bug fixes from alpha testing

---

## Success Criteria

### Scenario A: Plan from Claude Code
User types `/antfarm-plan "Build user auth with JWT"` in Claude Code. Antfarm decomposes into 5 tasks, shows them, user confirms, tasks appear in colony queue.

### Scenario B: Work from Claude Code
User types `/antfarm-start`. Claude Code registers as worker, forages a task, sets up worktree, and starts implementing. Heartbeat runs automatically via hook.

### Scenario C: Review from Claude Code
User types `/antfarm-review task-42`. Claude Code fetches the review pack (artifact, files changed, risks) and presents a formatted review.

### Scenario D: Status at a Glance
User types `/antfarm-status`. Sees: 2 workers active, 3 tasks done, 1 blocked (needs human decision), 1 ready.

---

## Dependencies

- **v0.5 must be shipped first** — planner, memory, artifacts, conflict prevention are prerequisites
- **`mcp` Python package** — for MCP stdio server implementation
- **Claude Code plugin format** — follow current plugin packaging conventions

## Explicitly Deferred

- npm publishing of the plugin
- Agent SDK worker integration (complex, wait for SDK maturity)
- Multi-colony MCP routing (one colony per MCP server is enough)
- Web-based MCP (stdio only for v0.6)
