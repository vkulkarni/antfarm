# Antfarm — Lightweight Orchestration for AI Coding Agents Across Machines

**Status:** Spec v1.0 (v0.1 scope frozen — approved for implementation)
**Date:** 2026-04-04
**Repo:** github.com/vkulkarni/antfarm
**License:** MIT

---

## Problem

AI coding agents (Claude Code, Codex, Aider, Cursor, etc.) are single-machine, single-session tools. Developers with multiple machines have no lightweight way to:

1. Distribute work across machines
2. Coordinate what each agent is working on
3. Track progress across the fleet
4. Safely integrate concurrent AI-generated changes
5. Detect conflicts early and resolve trivial ones automatically


## Solution

A thin, self-hosted coordination layer above git. One lead machine (the colony) hosts a tiny API server. Worker machines (ants) connect over the network. Any AI coding agent can participate — Antfarm orchestrates the **work**, not the **agent**.

Antfarm is **infrastructure, not a workflow**. It provides task claiming, scope-aware scheduling, presence, and a merge queue. How you create tasks (manually, via AI decomposition, from Jira, etc.) is your choice — not baked into the platform.

## Design Principles

1. **Agent-compatible** — works with Claude Code, Codex, Aider, Cursor, or a bash script. Baseline compatibility for all; deeper integration for some via adapters.
2. **Zero mandatory dependencies** — filesystem backend by default, no Redis/DB required
3. **Git-platform-agnostic** — works with GitHub, GitLab, Gitea, Bitbucket, anything
4. **No paid subscriptions required** — no GitHub Actions, no SaaS dependencies
5. **Lead machine model** — one machine hosts coordination AND does work
6. **Pluggable backends** — task backends and agent adapters, extensible by community
7. **Integration safety is the core value** — the hard problem is safe concurrent integration of AI-generated changes, not task distribution

---

## Terminology

### Entities

| Term | Meaning |
|------|---------|
| **Colony** | The whole system — API server + all connected nodes and workers |
| **Node** | A physical machine or VM (e.g., `node-1`, `node-3`) |
| **Worker** | One active agent session on a node. The core execution unit. A node can host multiple workers |
| **Soldier** | Integrator — guards branches, resolves conflicts, manages merge queue |
| **Trail** | Progress breadcrumbs left by a worker |
| **Scent** | Real-time log stream from a specific worker |

### Core Design Principle

> **Antfarm does not automatically discover arbitrary coding-agent sessions running on a machine.** A worker exists only when an Antfarm-compatible launcher or wrapper explicitly registers that session with the colony. Machines join explicitly. Workers register explicitly. Nothing is implicit.

If you have three terminals open on a Mac — one random Claude Code session, one Antfarm-managed Claude worker, one Antfarm-managed Codex worker — Antfarm only knows about the two managed workers that registered. The random session is invisible to Antfarm, and that is correct.

### Identity Model

The **worker session**, not the physical machine, is the task-owning unit:

```
Node: node-1 (e.g., Mac Mini)
├── Worker: node-1/claude-1    → working on task-001
├── Worker: node-1/codex-1     → working on task-002
└── Worker: node-1/claude-2    → idle, waiting to forage

Node: node-3 (e.g., laptop)
└── Worker: node-3/claude-1   → working on task-003
```

- **Worker ID format:** `<node>/<name>` (e.g., `node-1/claude-1`)
- Auto-generated on `hatch` if no name provided: `node-1/worker-1`, `node-1/worker-2`, etc.
- Tasks are claimed by `worker_id`, not `node_id`
- Heartbeats are per-worker, not per-node
- Each worker on the same node MUST use a separate git worktree or clone to avoid file conflicts

### What a Worker Is

A worker is **not** a machine, a terminal tab, or any arbitrary agent process.

A worker is: **an Antfarm-registered coding-agent session with a unique identity, a workspace, and a heartbeat.**

That session may internally wrap Claude Code, Codex, Aider, a bash script, or any future tool. Antfarm doesn't care what runs inside the worker — only that it registered, heartbeats, and reports completion.

### v0.1 CLI — Core Commands

| Verb | Meaning |
|------|---------|
| `colony` | Start the colony (API server on lead machine) |
| `join` | Register a node (machine) with the colony |
| `worker start` | Full lifecycle: register → forage → launch agent → work → harvest → repeat |
| `carry` | Carry a task to the queue (manual or scripted) |
| `scout` | Scout the colony — who's doing what |
| `doctor` | Pre-flight check (filesystem, network, git config) |

### v0.1 CLI — Low-Level Commands

For custom workflows and adapter integration:

| Verb | Meaning |
|------|---------|
| `hatch` | Register a worker session only |
| `forage` | Claim next task only |
| `trail` | Leave a pheromone trail (progress checkpoint) |
| `harvest` | Task complete, PR ready |
| `guard` | Guard a resource (acquire lock) |
| `release` | Release the guard (release lock) |
| `signal` | Escalate to user (append-only task note) |

**Lock scope (v0.1):** Locks are intended only for high-risk shared resources such as DB migrations, release/version files, and generated code artifacts. They are not general file-level concurrency controls.

**Signal semantics (v0.1):** A signal is an append-only task-level note. It does not change scheduling state. It is visible in `scout` and task detail. Used only for escalation context — "this task needs human input because X."

### Future Commands (not in v0.1)

| Verb | Version | Meaning |
|------|---------|---------|
| `scent` | v0.2 | Tail real-time logs from a specific worker |
| `pause` | v0.2 | Pause a task |
| `resume` | v0.2 | Resume a paused task |
| `reassign` | v0.2 | Move task to a different worker |
| `block` / `unblock` | v0.2 | Manually block/unblock a task |
| `pin` | v0.3 | Pin task to specific worker |
| `override-order` | v0.3 | Override merge queue position |
| `deploy` | v0.2 | SSH into all nodes and start workers automatically |

---

## Networking

### Requirements

Antfarm requires:

- One reachable colony endpoint (HTTP + JSON)
- Private network connectivity between workers and colony
- Application-level auth on top of network reachability (v0.2+)

### Transport

HTTP + JSON over any private network. The workload is tiny (task claims, heartbeats, checkpoints, status queries) — protocol speed is never the bottleneck. HTTP is chosen for debuggability (`curl`), adapter simplicity, and universal compatibility.

### Supported Network Configurations

| Setup | Example |
|-------|---------|
| Same LAN | `ANTFARM_URL=http://192.168.1.50:7433` |
| Tailscale (recommended) | `ANTFARM_URL=http://node-1:7433` (MagicDNS) |
| WireGuard / VPN | Any VPN with machine-to-machine connectivity |
| SSH tunnel | `ssh -L 7433:localhost:7433 lead-machine` |
| Cloud VPC | Private subnet, security group allowing :7433 |
| Same machine | `ANTFARM_URL=http://localhost:7433` |

### Why Tailscale is Recommended (not required)

- Easy device-to-device connectivity with stable names/IPs
- Works across home, office, and travel networks (handles NAT traversal)
- Built-in identity and encryption — no auth layer needed for v0.1
- Tailscale Serve can expose the colony to the tailnet

Any private network path works. Tailscale is the recommended happy path because it eliminates networking pain for the target user (solo dev with 2-5 personal machines).

### Future Transport Upgrades

| Version | Addition | Why |
|---------|----------|-----|
| v0.1 | HTTP + JSON (polling) | Simple, debuggable, universal |
| v0.3 | SSE for live dashboard | Push-based status updates for `scout` TUI |
| Future | WebSocket (if needed) | Bidirectional push for task cancellation, reassignment |

---

## Registration & Worker Lifecycle

### How Machines Join

Machines register explicitly. No auto-discovery, no network scanning, no OS-level process inspection.

```bash
# Step 1: Register the node with the colony
antfarm join --node node-1
```

This tells the colony: "a machine called `node-1` exists and is reachable."

### How Workers Start

Workers are started through an Antfarm wrapper that handles the full lifecycle:

```bash
# Step 2: Start a worker (registers, claims task, launches agent, reports back)
antfarm worker start \
  --name claude-1 \
  --agent claude-code \
  --workspace-root /repo/.worktrees
```

The worker creates a task-specific worktree inside `--workspace-root` after foraging (e.g., `/repo/.worktrees/task-001-att-001`).

`worker start` is a **convenience flow composed of lower-level operations**, not a core primitive. Internally it chains: registration → workspace setup → agent launch → task runtime loop → completion. Each step maps to a low-level command or API call that adapters can use independently.

The `worker start` lifecycle:
1. Registers with colony → `node-1/claude-1`
2. Claims next available task via `forage`
3. Sets up workspace (git worktree or clone)
4. Launches the AI agent (Claude Code, Codex, etc.) in that workspace
5. Sends heartbeats throughout
6. Publishes trail checkpoints
7. Marks task complete/fail on exit
8. Forages next task (or exits if queue is empty)

### Worker Registration Payload

```json
{
  "worker_id": "node-1/claude-1",
  "node_id": "node-1",
  "agent_type": "claude-code",
  "workspace_root": "/Users/me/repos/app/.worktrees",
  "registered_at": "2026-04-04T10:00:00Z"
}
```

Worker capabilities (v0.2+): In v0.1, all workers are treated as homogeneous — any worker can claim any task. In v0.2, workers may declare capabilities (`can_run_tests`, `can_push_git`, etc.) and tasks may declare requirements. The scheduler will then match requirements to capabilities. For v0.1, this is display-only metadata in `scout`.

### What Antfarm Does NOT Do

- **No OS-level process scanning** — no "find all Claude processes" or "attach to random terminals"
- **No implicit session discovery** — if Claude Code is open but not started through Antfarm, it's invisible
- **No remote spawning in v0.1** — you start workers locally on each machine
- **No invasive process management** — Antfarm doesn't kill, restart, or modify agent processes

### v0.1 vs Future Lifecycle

| Version | How Workers Start |
|---------|-------------------|
| **v0.1** | You SSH into each machine and run `antfarm worker start` manually |
| **v0.2** | `antfarm deploy` script — reads node config, SSHs into each, starts workers in tmux |
| **v0.3+** | Optional `antfarm-node` daemon — colony can request "start a worker on node-2" remotely |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    antfarm (core)                            │
│                                                             │
│  Colony API server + CLI + task scheduler + merge queue     │
│  Knows NOTHING about which AI agent runs the work           │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                      adapters                               │
│                                                             │
│  ┌───────────┐ ┌───────┐ ┌───────┐ ┌────────┐ ┌─────────┐  │
│  │Claude Code│ │ Codex │ │ Aider │ │ Cursor │ │ Generic │  │
│  │(+ queen   │ │       │ │       │ │        │ │ (curl)  │  │
│  │ example)  │ │       │ │       │ │        │ │         │  │
│  └───────────┘ └───────┘ └───────┘ └────────┘ └─────────┘  │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                      backends                               │
│                                                             │
│  ┌──────┐ ┌───────┐ ┌────────┐ ┌──────┐ ┌────────┐         │
│  │ File │ │ Redis │ │ GitHub │ │ Jira │ │ Linear │         │
│  │(v0.1)│ │(v0.2) │ │(v0.4)  │ │(v1.0)│ │(v1.0)  │         │
│  └──────┘ └───────┘ └────────┘ └──────┘ └────────┘         │
└─────────────────────────────────────────────────────────────┘
```

### Lead Machine (Colony) Model

```
Lead Node (hosts colony + runs workers)
┌─────────────────────────────────────────┐
│                                         │
│  antfarm colony (:7433)                 │
│  ┌──────────────────────────────┐       │
│  │  .antfarm/                   │       │
│  │    tasks/                    │       │
│  │      ready/task-001.json     │       │
│  │      active/task-002.json    │       │
│  │      done/task-003.json      │       │
│  │    workers/                  │       │
│  │    guards/                    │       │
│  │    logs/                     │       │
│  └──────────────────────────────┘       │
│                                         │
│  Worker: node-1/soldier (Integrator)    │
│  Worker: node-1/claude-1 (Engineer)     │
│                                         │
└──────────────────┬──────────────────────┘
                   │ :7433 over private network
         ┌─────────┼─────────┐
         ▼                   ▼
   Node: node-2          Node: node-3
   ┌──────────────┐      ┌──────────────┐
   │ node-2/      │      │ node-3/     │
   │   claude-1   │      │   claude-1   │
   │   codex-1    │      │              │
   └──────────────┘      └──────────────┘
   (2 workers)           (1 worker)
```

### Deployment Modes

| Mode | Description |
|------|-------------|
| **Single node, multiple workers** | One machine runs colony + multiple agent sessions |
| **Multiple nodes, one worker each** | Classic distributed setup |
| **Multiple nodes, multiple workers** | Full fleet — nodes with varying capacity host varying worker counts |

### Two-Level Team Model

- **Colony level:** Workers coordinated via Antfarm API (cross-node, cross-session)
- **Worker level:** Each worker session can internally spawn sub-workers for large tasks (agent-native parallelism, e.g., Claude Code TeamCreate, multiple Aider sub-sessions)

```
Colony
├── Node: node-1 (48GB)
│   ├── Worker: node-1/claude-1 (spawned 3 sub-workers for task-001)
│   │   ├── sub-worker: API routes (worktree)
│   │   ├── sub-worker: DB schema (worktree)
│   │   └── sub-worker: tests (worktree)
│   └── Worker: node-1/codex-1 (working solo on task-002)
├── Node: node-2 (16GB)
│   └── Worker: node-2/claude-1 (working solo on task-003)
└── Node: node-3 (16GB)
    └── Worker: node-3/aider-1 (working solo on task-004)
```

---

## Agent Roles

Antfarm defines two core roles. Task creation is NOT a platform role — it's a workflow choice (see "Task Creation" section below).

### 1. Worker — Engineer (runs on every machine, including lead)

- Pre-forage: executes `pre-forage` hook (default: `git fetch && git checkout dev && git pull`)
- Forages: `antfarm forage` to claim next task (scope-aware, dependency-checked)
- Reads task spec, autonomously decides: solo or local team
  - Small/medium task → works solo
  - Large task → spawns local sub-ants (if agent supports it)
- Branches from integration branch (e.g., `dev`)
- Implements, tests, opens PR
- Leaves trail: `antfarm trail task-001 "completed API routes"`
- Harvests: `antfarm harvest task-001 --pr <url>`
- Forages for next task

### 2. Soldier — Integrator (runs on lead machine)

This is Antfarm's **core value proposition**. Most multi-agent systems fail at integration. The Soldier owns the path from PR to merged code.

The Soldier is **fully deterministic** — no LLM, no AI. It is a script-based merge gate, like CI. It doesn't fix your code; it tells you what's broken. Workers handle the fixes and re-submit.

- Watches for harvested tasks with open PRs
- Determines merge order based on `depends_on` (blocked PRs are invisible until unblocked)
- Merge workflow:
  1. Merge PR to a **temp integration branch** (never directly to dev)
  2. If merge conflict → kick back immediately (no auto-fix in v0.1)
  3. Run full test suite on the integration branch
  4. If green → fast-forward dev to match integration branch
  5. If red → kick back with test output
- Guards shared resources (migrations, shared resources)
- Releases dev → main on user's command only

#### Soldier Policy (Hard Rules)

These are deterministic rules, not guidelines. The Soldier follows them exactly:

1. **Merge target:** Soldier merges ONLY to a temp integration branch, never directly to dev
2. **No auto-fix in v0.1:** Any merge conflict = immediate kickback. Any test failure = immediate kickback. Soldier never modifies worker code.
3. **Kickback:** Task status returns to `ready`. The next successful `forage` creates a new `current_attempt`. Trail includes failure context + test output from the failed integration
4. **Dependency blocking:** Dependent tasks remain merge-ineligible until upstream task is merged. If upstream is kicked back, dependents stay in `done`/`ready` but are invisible to Soldier until upstream re-merges
5. **Queue independence:** One failing task does NOT block the entire queue — only its dependents. Independent tasks continue merging
6. **Canonical attempts only:** Soldier merges only the `current_attempt`. Superseded attempts are ignored unless human overrides

#### Soldier Failure Flow (v0.1)

```
PR branch merged to temp integration branch
  │
  ├── Merge conflict?
  │   └── Kick back immediately
  │       • Trail: "Merge conflict with integration branch"
  │       • Worker fixes and re-submits
  │
  ├── Tests fail?
  │   └── Kick back immediately
  │       • Trail: "test_X failed after merge — output attached"
  │       • Worker fixes and re-submits
  │
  └── Clean merge + green tests?
      └── Fast-forward integration branch
```

#### v0.2 Enhancement: AI-Assisted Soldier

In v0.2, the Soldier may optionally invoke an AI helper for:
- Conflict analysis and trivial auto-fix (import order, whitespace)
- Test failure diagnosis
- Smarter kickback messages with context
- "Trivial vs real" conflict classification

This is an optional enhancement, not a core requirement. The deterministic gate remains the default.

---

## Task Creation (Not a Platform Role)

Antfarm does NOT prescribe how tasks are created. Tasks enter the queue via `antfarm carry` — the source is your choice:

| Method | How |
|--------|-----|
| **Manual** | User runs `antfarm carry --title "..." --spec "..."` |
| **AI Queen** (example workflow) | Claude Code agent reads a spec, decomposes, calls `antfarm carry` for each task. Shipped as example adapter in `adapters/claude-code/queen.md` |
| **GitHub Issues** | Script syncs issues to the queue: `gh issue list --json | antfarm import` |
| **Jira / Linear** | Backend plugin syncs tickets directly |
| **CI/CD** | Pipeline pushes tasks on trigger |

The Queen (AI auto-decomposition) is an **example workflow shipped with the Claude Code adapter**, not a core platform concept. Users who want manual task creation, Jira sync, or their own decomposition logic are first-class citizens.

---

## Core Components

### Task Backend Interface

```python
class TaskBackend(ABC):
    # --- Task lifecycle ---
    def carry(self, task: dict) -> str: ...
    def pull(self, worker_id: str) -> dict | None: ...
    def append_trail(self, task_id: str, entry: dict) -> None: ...
    def append_signal(self, task_id: str, entry: dict) -> None: ...
    def mark_harvested(self, task_id: str, attempt_id: str, pr: str, branch: str) -> None: ...
    def kickback(self, task_id: str, reason: str) -> None: ...
    def mark_merged(self, task_id: str, attempt_id: str) -> None: ...
    def list_tasks(self, status: str = None) -> list[dict]: ...
    def get_task(self, task_id: str) -> dict: ...

    # --- Guards ---
    def guard(self, resource: str, owner: str) -> bool: ...
    def release_guard(self, resource: str, owner: str) -> None: ...

    # --- Nodes & Workers ---
    def register_node(self, node: dict) -> None: ...
    def register_worker(self, worker: dict) -> str: ...
    def deregister_worker(self, worker_id: str) -> None: ...
    def heartbeat(self, worker_id: str, status: dict) -> None: ...

    # --- Status ---
    def status(self) -> dict: ...
```

### FileBackend (default, zero dependencies)

```
.antfarm/
  tasks/
    ready/        # queued tasks (JSON files)
    active/       # claimed by a worker
    done/         # completed + merged (status tracked in task/attempt metadata, not separate folder)
  workers/        # presence files per worker session (checked by mtime)
  nodes/          # node registration files
  guards/          # lock files with owner + timestamp
  # logs/ deferred to v0.2 — trail is stored in task JSON for now
  config.json     # colony configuration
```

- Claiming a task = `mv ready/task-001.json active/` (atomic on POSIX)
- Lock = exclusive file create (SET NX equivalent)
- Heartbeat = touch worker file (presence by mtime, stale after configurable TTL)

**FileBackend guarantees and limitations:**
- Designed for localhost or trusted private LAN only
- NOT suitable for shared network storage (NFS), HA, or serious durability
- Atomic rename assumes local POSIX filesystem (ext4, APFS, etc.)
- Crash consistency: a crash during `mv` may leave orphaned tasks — `antfarm doctor` detects and recovers
- Stale guard recovery: guards have a TTL (default 5 min) checked by file mtime
- Windows: no atomicity guarantee — use RedisBackend on Windows

### RedisBackend (v0.2, optional)

For users who already run Redis. Better atomicity, real-time events, no stale file cleanup.

- Tasks: Redis lists (BRPOP for blocking pull)
- Presence: Keys with TTL (auto-expire — no stale heartbeat files)
- Locks: SET NX EX (atomic with TTL)
- Events: Pub/sub for real-time notifications (worker A finishes → blocked tasks unblock instantly)
- Installed via: `pip install antfarm[redis]`
- Switch: `antfarm colony --backend redis --redis-url redis://localhost:6379`

v0.1 ships the `TaskBackend` interface so Redis implementation can slot in cleanly. The interface is the contract — FileBackend proves it works.

### API Server (~80 lines)

FastAPI server running on the lead machine (the colony). Ants interact via HTTP. In-process `threading.Lock()` guards all pull/lock operations (single-process server only — no load balancing).

```
POST   /nodes              register a node
POST   /workers/register   register a worker session (with capabilities)
POST   /workers/{id}/heartbeat    worker session heartbeat
DELETE /workers/{id}       deregister a worker

POST   /tasks              carry a task to the queue
POST   /tasks/pull         forage — claim next task (atomic, scope-aware, dependency-checked)
GET    /tasks              list tasks (optional status filter)
GET    /tasks/{id}         get task detail (full task with attempts, trail, signals)
POST   /tasks/{id}/trail   append trail entry
POST   /tasks/{id}/signal  append signal entry
POST   /tasks/{id}/harvest mark task as harvested (done)
POST   /tasks/{id}/kickback kick task back to ready
POST   /tasks/{id}/merge   mark attempt as merged (soldier only)

POST   /guards/{resource}  guard a resource (acquire lock)
DELETE /guards/{resource}  release the guard

GET    /status             full colony status (nodes, workers, tasks)
```

### Scheduling Policy

Task scheduling on `forage` follows this explicit policy (in order):

1. **Dependency check** — skip tasks whose `depends_on` items are not all in `done/`
2. **Scope preference** — prefer tasks whose `touches` fields don't overlap with any `active` task's `touches` (soft preference, not hard block)
3. **Priority** — higher priority tasks first (default: all tasks equal priority)
4. **FIFO** — among equal candidates, oldest task first

This is intentionally simple. No machine affinity, no complexity-based routing, no deadline awareness in v0.1. The scheduler is explicitly "smart FIFO with dependency and scope checks."

### CLI

```bash
# Setup
antfarm colony                          # start colony on lead machine (:7433)
antfarm join --node node-1              # register this machine with the colony
antfarm doctor                          # pre-flight check

# Start Workers (the recommended way)
antfarm worker start --agent claude-code          # full lifecycle: register → forage → work → harvest → repeat
antfarm worker start --agent codex --name codex-1 # with explicit name
antfarm worker start --agent aider --workspace-root /path/to/worktrees  # explicit workspace root

# Or manual step-by-step (for custom workflows)
antfarm hatch                           # register worker only (auto-names: node-1/worker-1)
antfarm hatch --name claude-1           # register with explicit name (node-1/claude-1)

# Task Creation (manual or scripted)
antfarm carry --title "..." --spec "..." # carry a task to the queue
antfarm carry --file task.json           # carry from JSON file
antfarm carry --depends-on task-001      # with dependency
antfarm carry --touches "api/routes,db"  # with scope hints
antfarm carry --complexity L             # set complexity (S/M/L, default M)

# Worker (Engineer)
antfarm forage                          # claim next task
antfarm trail task-001 "message"        # checkpoint progress
antfarm harvest task-001 --pr <url>     # mark complete
antfarm signal task-001 "message"        # escalate to user

# Monitoring
antfarm scout                           # who's doing what
```

---

## Adapter Contract

### Required Protocol (4 calls)

Every adapter must enable these 4 HTTP calls from the AI agent:

```
1. FORAGE    → POST  $ANTFARM_URL/tasks/pull           {worker_id}
2. TRAIL     → POST  $ANTFARM_URL/tasks/{id}/trail    {message, timestamp}
3. HEARTBEAT → POST  $ANTFARM_URL/workers/{id}/heartbeat
4. HARVEST   → POST  $ANTFARM_URL/tasks/{id}/harvest  {attempt_id, pr, branch}
```

### Optional Telemetry Protocol

Adapters can send richer status for better `scout` visibility:

```json
{
  "branch": "feat/tax-advisor",
  "commit": "abc123f",
  "changed_files": ["brahma/agents/tax_advisor.py", "tests/test_tax.py"],
  "test_status": "passing",
  "blocked": false,
  "error": null
}
```

This is sent as part of the heartbeat payload. Without it, `scout` shows basic presence. With it, `scout` becomes a real control plane.

### Pre-Forage Hook

Before starting work on a foraged task, the workspace should be synced to the integration branch. Exact commands are adapter/workspace-manager responsibility.

Example (reference implementation in Claude Code adapter):

```bash
git fetch origin
git checkout dev
git pull origin dev
git checkout -b <branch-from-task>
```

This prevents workers from building on stale foundations. The workspace manager in `worker start` handles this automatically; adapters using low-level commands must handle it themselves.

### Thinking Signal

AI agents can "think" for minutes without tool calls, causing heartbeats to go stale. Adapters should implement a periodic trail update during long operations:

- Claude Code: PostToolUse hook handles this naturally
- For agents without hooks: adapter wrapper sends `trail "still working..."` every 60 seconds
- Colony treats heartbeat TTL conservatively (default: 5 minutes)

### v0.1 Adapters

| Adapter | Status | Notes |
|---------|--------|-------|
| Claude Code | Reference adapter | Agent definitions + hooks + queen example workflow |
| Generic (curl) | Reference adapter | Shell scripts for any agent |
| Codex | Planned (v0.4) | Community contribution welcome |
| Aider | Planned (v0.4) | Community contribution welcome |
| Cursor | Planned (v1.0) | Community contribution welcome |

---

## Task JSON Schema

```json
{
  "id": "task-001",
  "title": "Tax Advisor base agent",
  "spec": "Build BaseAgent subclass with skill registry integration...",
  "complexity": "L",
  "priority": 1,
  "depends_on": ["task-000"],
  "touches": ["agents/tax_advisor", "core/skill_registry", "db/schema"],
  "status": "ready",
  "current_attempt": null,
  "attempts": [],
  "trail": [],
  "signals": [],
  "created_at": "2026-04-04T10:00:00Z",
  "updated_at": "2026-04-04T10:00:00Z",
  "created_by": "user"
}
```

### Task Attempt Model

Each time a task is claimed (foraged), a new attempt is created. This prevents ambiguity when tasks are kicked back or reassigned.

```json
{
  "attempt_id": "att-001",
  "worker_id": "node-1/claude-1",
  "status": "active",
  "branch": "feat/task-001-att-001",
  "pr": null,
  "started_at": "2026-04-04T10:05:00Z",
  "completed_at": null
}
```

**Attempt rules:**
- Each `forage` creates a new attempt and sets it as `current_attempt`
- Previous attempts become `superseded`
- Soldier merges ONLY the `current_attempt`
- If a worker loses network and another worker is assigned the same task, the old attempt is superseded — its PR is visible but not canonical
- Human can override: `antfarm harvest <task-id> --attempt <att-id> --pr <url>` to promote a superseded attempt
- Attempt statuses: `active` → `done` → `merged` or `active` → `superseded`

### Status Transitions

```
ready → active        (worker forages — creates new attempt)
active → done         (worker harvests — PR opened on current attempt)
done → merged         (soldier merges current attempt to dev)
done → ready          (task kicked back by Soldier — next forage creates new attempt)
```

v0.1 does NOT implement: `blocked`, `paused`. These are v0.2 additions.

### The `touches` Field

Scope hints that help the scheduler avoid assigning overlapping work:

```json
"touches": ["api/routes", "db/schema", "auth"]
```

- Not file paths — module/component names (coarser than files, finer than "the whole repo")
- The scheduler **prefers** non-overlapping scopes but does NOT hard-block
- If all remaining tasks overlap, scheduler assigns anyway (conflicts resolved at merge)
- Workers can update `touches` during work if scope changes

**Vocabulary rules (v0.1):**
- Freeform strings, but exact-match only for overlap detection (`auth` ≠ `authentication`)
- Repo may define known scopes in `.antfarm/config.json` under `scopes` — unknown scopes are allowed but flagged in `scout`
- Keep scopes coarse: `api`, `frontend`, `db`, `auth`, `tests` — not `api/routes/users.py`

---

## Failure Model

Explicit behavior for every failure scenario:

| Failure | Behavior | Recovery |
|---------|----------|----------|
| **Worker dies mid-task** | Heartbeat TTL expires (5 min default). Task stays in `active/` | `antfarm doctor` detects stale tasks, moves back to `ready/` with trail context preserved |
| **Worker loses network** | Heartbeat stops. Worker continues locally (git commits are safe) | On reconnect: if task is still assigned to this worker's attempt, resumes normally. If task was reassigned (new attempt), this worker's attempt becomes `superseded` — its PR is visible but not canonical. Human can promote it via override |
| **Lead node restarts** | Colony API goes down. Workers keep working on current tasks | Restart `antfarm colony`. FileBackend state is on disk — full recovery. Workers reconnect automatically |
| **Same task claimed twice** | `threading.Lock()` in API prevents this. Atomic `mv` in FileBackend prevents this. Each claim creates a new attempt — previous attempt is superseded | Soldier merges only `current_attempt`. Superseded PRs are ignored |
| **Stale guard exists** | Guard files have TTL (default 5 min, checked by mtime) | `antfarm doctor` detects and cleans stale guards. Manual: `antfarm release <resource>` |
| **PR created but harvest not sent** | Task stays in `active/`. Soldier doesn't see it in merge queue | `antfarm doctor` detects tasks with stale heartbeat but existing branches. Manual: `antfarm harvest <task-id> --pr <url>` |
| **Tests pass alone, fail on integration** | Soldier catches this (merges to temp integration branch first) | Soldier kicks back to worker with failure context, or escalates to user if ambiguous |
| **Worker goes down wrong path** | Trail shows no meaningful progress. User notices via `scout` | User stops the worker manually. Move task JSON back to `ready/`. New worker forages it (creates new attempt) |
| **Two workers on same node conflict** | Each worker MUST use separate worktree/clone (enforced by `antfarm doctor`) | `doctor` checks that no two workers share a working directory |

---

## Human Override Model

Antfarm is lightweight coordination, not full autonomy. Humans remain in control.

### v0.1 Overrides

| Override | How | When to Use |
|----------|-----|-------------|
| **Force harvest** | `antfarm harvest <task-id> --pr <url>` | Recovery when worker died before sending harvest |
| **Promote attempt** | `antfarm harvest <task-id> --attempt <att-id> --pr <url>` | Use a superseded attempt's PR instead of current |
| **Manual task management** | Edit task JSON in `.antfarm/tasks/` directly | Move tasks between ready/active/done, fix state |

### v0.2+ Overrides

| Override | Command | When to Use |
|----------|---------|-------------|
| Pause/resume | `antfarm pause/resume <task-id>` | Worker going wrong direction |
| Reassign | `antfarm reassign <task-id> <worker-id>` | Rebalance or recover from stale worker |
| Block/unblock | `antfarm block/unblock <task-id>` | External dependency |
| Pin to worker | `antfarm pin <task-id> <worker-id>` | Task requires specific hardware |
| Override merge order | `antfarm override-order <task-id> <pos>` | Business priority trumps dependency order |

---

## Repo Assumptions (v0.1)

Antfarm v0.1 assumes:

- **Single git repository** (monorepo or single-project)
- **Integration branch workflow** (e.g., feature branches → `dev` → `main`)
- **Pull/merge request workflow** (PRs are how code enters the integration branch)
- **Each worker has a full clone or worktree** of the repo (not shallow clones)
- **Workers create branches** from the integration branch for each task
- **Multiple workers on same node** must each use a separate git worktree or clone

**Not supported in v0.1** (future versions may add):
- Multi-repo / polyrepo workflows
- Stacked diffs (e.g., Graphite, ghstack)
- Local-only branches (no PR workflow)
- Worktree-only mode (no branch creation)
- Trunk-based development (no integration branch)

---

## Mixed Colony Support

Different ants can run different agents:

```
antfarm scout

Nodes: 3 online    Workers: 5 active    Tasks: 4 active, 2 ready, 8 done

┌───────────────────┬────────┬───────────────┬────────┬──────────────┬──────────────┐
│ Worker            │ Agent  │ Task          │ Status │ Touches      │ Trail        │
├───────────────────┼────────┼───────────────┼────────┼──────────────┼──────────────┤
│ node-1/claude-1   │ claude │ task-001 (L)  │ active │ api, db      │ 2m ago       │
│ node-1/codex-1    │ codex  │ task-002 (M)  │ active │ frontend     │ 30s ago      │
│ node-2/claude-1   │ claude │ task-003 (S)  │ active │ tests        │ 1m ago       │
│ node-2/claude-2   │ claude │ task-005 (M)  │ active │ auth         │ 45s ago      │
│ node-3/aider-1    │ aider  │ task-004 (L)  │ active │ ml, api      │ 5m ago       │
└───────────────────┴────────┴───────────────┴────────┴──────────────┴──────────────┘
```

---

## Known Limitations (v0.1)

### Must-Fix for v0.1

| Problem | Fix |
|---|---|
| Pull race condition | In-process `threading.Lock()` in API server |
| No dependency resolution in pull | Check `depends_on` against `done/` in scheduler |
| Scope-unaware scheduling | Check `touches` overlap with active tasks (soft preference) |
| Hook failure handling | `\|\| true` + 1s timeout in all hook templates |
| No pre-flight validation | `antfarm doctor` command |

### Accepted Limitations (fix in future versions)

| Problem | Why It's OK for Now | Fix In |
|---|---|---|
| Lead machine SPOF | `.antfarm/` is just files, copy to recover. 3-4 machines = manual recovery is fine | v0.3 |
| No API auth | Require Tailscale/private network. Target user is solo dev with personal machines | v0.2 |
| No rollback intelligence | Soldier merges to temp integration branch first, manual revert otherwise | v0.2 |
| FileBackend doesn't scale | < 200 tasks is fine for target use case | v1.0 |
| Rate limit collision | Agents retry on 429 naturally. Stagger work manually | v0.3 |
| Semantic conflicts | PR review catches these (same as real engineering teams) | v0.4 |
| Stale heartbeat vs long think | Conservative TTL (5 min) + thinking signal in adapter contract | v0.2 |

### Architectural Constraints

- **Single API server instance** — no load balancing (in-process lock requires single process)
- **POSIX filesystem assumed for FileBackend** — atomic rename guarantee. Windows users must use RedisBackend
- **Network required for multi-machine** — Tailscale recommended, any private network works
- **Not designed for untrusted networks** — no auth in v0.1, no encryption beyond transport

---

## Package Structure

```
antfarm/
  core/
    __init__.py
    cli.py                # antfarm CLI
    serve.py              # colony API server
    scheduler.py          # scope-aware task scheduler
    worker.py             # worker start lifecycle (register → forage → launch → harvest → repeat)
    workspace.py          # git worktree management
    soldier.py            # merge queue + integration branch + kickback
    models.py             # Task, Worker, Node, Attempt dataclasses
    doctor.py             # pre-flight checks + stale recovery
    backends/
      __init__.py         # get_backend() factory
      base.py             # TaskBackend interface
      file.py             # FileBackend (v0.1)
                          # redis.py planned for v0.2
  adapters/
    claude-code/
      agents/
        worker.md         # Engineer agent definition
        soldier.md        # Integrator agent definition
        queen.md          # EXAMPLE: AI task decomposition workflow
      hooks/
        heartbeat.sh      # PostToolUse heartbeat
        pre-forage.sh     # git pull before starting work
      setup.sh
    generic/
      README.md           # curl-based examples for any agent
      forage.sh           # example forage script
      heartbeat.sh        # example heartbeat script
  pyproject.toml
  README.md
  LICENSE                 # MIT
```

**v0.1 ships FileBackend only.** The `TaskBackend` interface ships so Redis (v0.2) and community backends (GitHub Issues, Jira, Linear) can slot in cleanly.

---

## Roadmap

### Phase 1: Prove the core loop (v0.1)

```
v0.1  FileBackend (RedisBackend interface designed, not implemented)
      Worker + Soldier roles with hard policy rules
      Task attempt model
      Scope-aware scheduler (deps + touches + FIFO)
      Core CLI: colony, join, carry, worker start, scout, doctor
      Low-level CLI: hatch, forage, trail, harvest
      Claude Code reference adapter + Generic adapter
      Failure model + doctor recovery
```

### Phase 2: Improve operator experience (v0.2)

```
v0.2  RedisBackend
      Bearer token auth
      Human overrides: pause, resume, reassign, block/unblock
      scent (real-time worker log tailing)
      deploy (SSH-based multi-node worker launch)
      Thinking signal in adapter contract
      Capability-aware scheduling (task requirements ↔ worker capabilities)
```

### Phase 3: Expand ecosystem (v0.3+)

```
v0.3  TUI dashboard (rich) + SSE for live status
      Colony failover
      Rate limit awareness

v0.4  GitHub Issues backend + Codex adapter + Aider adapter
      antfarm import for external trackers

v1.0  Jira / Linear / Notion backends + Cursor/Windsurf adapters
      Audit log + multi-repo support
```

---

## Quick Start

```bash
# Install
pip install antfarm

# Pre-flight check
antfarm doctor

# Lead machine — start the colony
antfarm colony

# Register this machine
antfarm join --node node-1

# Push tasks (manually, or script it however you want)
antfarm carry --title "Build user auth API" \
             --spec "JWT auth with login/logout/me endpoints" \
             --touches "api/auth,db/schema"

antfarm carry --title "Build admin dashboard" \
             --spec "React dashboard with user management" \
             --touches "frontend" \
             --depends-on task-001

# Start a worker — handles everything (register, forage, work, harvest, repeat)
antfarm worker start --agent claude-code

# Start a second worker on the same machine
antfarm worker start --agent codex --name codex-1

# On another machine
export ANTFARM_URL=http://lead-machine:7433
antfarm join --node node-2
antfarm worker start --agent claude-code

# Or use low-level commands for custom integration
antfarm hatch --name custom-1          # just register
antfarm forage                         # just claim a task
antfarm trail task-001 "progress..."   # just checkpoint
antfarm harvest task-001 --pr <url>    # just complete

# Watch the colony
antfarm scout                          # status dashboard
```

**Minimal setup. No mandatory external infrastructure. No paid subscriptions. No vendor lock-in.**

Requirements: Python 3.12+ · any AI coding agent · any git remote · private network between machines.

---

## Origin & Design History

This project emerged from the need to coordinate AI coding agents across 3-4 machines (multiple Mac minis)

### Key Design Insights

1. **Real engineering teams don't need complex coordination.** They use git, pull requests, and code review. AI coding agents should work the same way — independently, with conflicts resolved at merge time.

2. **Integration safety is the hard problem.** Task distribution is commodity. Safe concurrent integration of AI-generated changes is where multi-agent systems actually fail. The Soldier role is the core value.

3. **Infrastructure, not workflow.** Antfarm provides task claiming, scheduling, and merge queue. How tasks are created (manually, AI decomposition, Jira sync) is a workflow choice — not baked into the platform.

4. **The ant metaphor maps to the architecture.** Ants are autonomous, lightweight, distributed, and build incredible things with minimal central coordination. Each ant works independently; the colony builds something bigger than any individual ant could.

