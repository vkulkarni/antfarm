# Antfarm v0.6 — Specification

**Status:** Draft (revised)
**Date:** 2026-04-05
**Prerequisite:** v0.5 shipped (canonical scheduler, structured artifacts, repo memory, planner, conflict prevention)
**Goal:** Make Antfarm usable from Claude Code via MCP, with optional packaged plugin UX.

**Build order:** MCP-first, slash commands second, hooks last.

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

1. **MCP server** — Claude Code calls Antfarm tools directly (create tasks, claim work, report results)
2. **Slash commands** — `/antfarm-plan`, `/antfarm-status`, `/antfarm-review` etc.
3. **Subagents** — planner and reviewer roles as Claude Code agent definitions
4. **Hooks** — heartbeat, failure trail (opt-in observation, never creates truth)

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

**Configuration:** The MCP server reads from the existing `.antfarm/config.json` — one config source, not two. This is the same config file that the FileBackend and colony already use. The MCP server reads `colony_url` and `token` from it.

```json
// .antfarm/config.json (existing, authoritative)
{
  "colony_url": "http://localhost:7433",
  "token": "optional-bearer-token",
  "repo": "vkulkarni/antfarm",
  "integration_branch": "dev"
}
```

Claude Code MCP server config references the Antfarm module, which reads `.antfarm/config.json` at startup:

```json
{
  "mcpServers": {
    "antfarm": {
      "command": "python3",
      "args": ["-m", "antfarm.mcp.server"]
    }
  }
}
```

Environment variables (`ANTFARM_URL`, `ANTFARM_TOKEN`) override `.antfarm/config.json` when set, for CI/remote use cases.

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

**What:** Claude Code hooks that fire automatically to keep Antfarm informed. Hooks **observe and synchronize** — they never create canonical state. The final artifact and merge state come only from explicit, deterministic Antfarm flows (`/antfarm-done`, harvest API).

**Location:** `antfarm/plugin/hooks/`

| Hook | Event | What it does | Default |
|------|-------|-------------|---------|
| `heartbeat.sh` | PostToolUse | Sends heartbeat to colony | **On** |
| `failure_trail.sh` | PostToolUseFailure | Logs failure context as a trail entry | **On** |
| `workspace_observe.sh` | PostToolUse | Writes provisional changed-file observations to trail (not the artifact) | **Opt-in** |

**Hooks that were considered and rejected:**

| Hook | Why rejected |
|------|-------------|
| `artifact_update.sh` (mutate artifact on file edit) | The final TaskArtifact must be a harvest-time, deterministic record. Hooks should not keep mutating the canonical artifact during editing. |
| `on_complete.sh` (auto-harvest on session stop) | A Claude session ending does not mean the task is complete. Auto-harvesting would silently produce bad state — half-done work marked done. This violates the trust model built in v0.5. On session stop, the safe actions are: trail a status update, finalize heartbeat, leave task active. Harvest requires explicit `/antfarm-done`. |

**Design principle:** Hooks can write provisional observations, dirty-file hints, heartbeats, progress trail entries, and failure notes. They must **not** mutate the canonical final artifact or trigger state transitions (harvest, kickback, merge).

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
antfarm/
  mcp/                            # MCP server — part of Antfarm's integration layer, not plugin
    __init__.py
    server.py                     # MCP stdio server — reads JSON-RPC, calls colony API
    tools.py                      # Tool definitions + handler dispatch
  plugin/                         # Plugin assets — UX layer for Claude Code
    package.json                  # Plugin manifest
    skills/
      antfarm-plan.md
      antfarm-status.md
      antfarm-blockers.md
      antfarm-review.md
      antfarm-start.md
      antfarm-done.md
    hooks/
      heartbeat.sh                # PostToolUse: heartbeat (default on)
      failure_trail.sh            # PostToolUseFailure: log failure to trail (default on)
      workspace_observe.sh        # PostToolUse: observe workspace changes (opt-in)
    agents/
      worker.md
      planner.md
      reviewer.md
```

**Architectural note:** The MCP server lives in `antfarm/mcp/`, not under `antfarm/plugin/`. The MCP bridge is part of Antfarm's integration layer — it has value independent of the plugin packaging. The plugin references the MCP server but does not contain it.

**Installation:**
```bash
# From the antfarm repo
claude plugins install ./antfarm/plugin

# Or from npm (future)
claude plugins install antfarm-plugin
```

**Complexity:** M (packaging + testing)

---

## Release Milestones

### Milestone 1 — MCP + Core Commands

The true foundation. Antfarm is usable from Claude Code after this milestone.

- MCP server with stdio transport (`antfarm/mcp/`)
- Tool definitions mapping to colony API
- Config reads from `.antfarm/config.json`
- Tests with mock colony
- Core slash commands: `/antfarm-plan`, `/antfarm-status`, `/antfarm-start`, `/antfarm-review`
- Review flow working end-to-end via MCP tools
- Error handling: colony unavailable, auth failure, stale MCP connection, timeouts

### Milestone 2 — Plugin UX

Packaging, automation, and polish. Value is high but not blocking.

- Plugin `package.json` + `claude plugins install` workflow
- Remaining slash commands: `/antfarm-blockers`, `/antfarm-done`, `/antfarm-merge-ready`
- Safe hooks: heartbeat (default on), failure trail (default on), workspace observe (opt-in)
- Agent definitions: `worker.md`, `planner.md`, `reviewer.md`
- End-to-end test: `/antfarm-plan` → workers build → `/antfarm-review` → merge
- Install docs and skill reference

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

### Scenario E: Colony Unavailable
Colony is down or auth fails. Claude Code surfaces a clear error ("Cannot reach Antfarm colony at http://localhost:7433 — is it running?") and does not leave the user guessing about plugin state. MCP tools return structured error responses, not silent failures.

### Scenario F: Session Interruption
Claude Code session stops mid-task (crash, user closes terminal, network drop). Antfarm does **not** falsely harvest the task. Task remains in `active/` status. Heartbeat expires naturally. Doctor detects the stale task and can recover it. Operator can resume or reassign cleanly.

### Scenario G: Stale MCP Connection
Colony restarts but MCP client has a cached connection. Next MCP tool call gets a connection error. MCP server retries with a fresh connection and surfaces the error clearly if retry also fails. No silent data loss or phantom success.

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
