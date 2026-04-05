# Antfarm

**Lightweight orchestration for AI coding agents across machines.**

Antfarm is a thin, self-hosted coordination layer that lets you distribute coding work across multiple machines running any AI coding agent — Claude Code, Codex, Aider, Cursor, or anything that can run a shell command.

One machine hosts the colony. Workers connect, claim tasks, build, and open PRs. An integrator merges them safely. That's it.

> **Status:** **v0.1.0** — Core loop complete. Colony, workers, integrator, and CLI all functional.

---

## Why Antfarm?

You have 2-5 machines. You have an AI coding subscription. You want them all working on your project in parallel.

Today, you manually manage each session — SSH in, start the agent, remember what each one is doing, hope they don't conflict, resolve merge hell yourself.

Antfarm gives you:

- **A task queue** — carry work to the colony, workers claim it automatically
- **Scope-aware scheduling** — avoids assigning overlapping work to different workers
- **Progress visibility** — see what every worker is doing across every machine
- **A merge queue** — an integrator that safely merges concurrent changes, resolves trivial conflicts, and escalates real ones
- **Failure recovery** — stale workers are detected, tasks are re-queued

### What Antfarm is not

- Not a framework — it's infrastructure. Bring your own workflow.
- Not an agent — it doesn't write code. Your AI agents do.
- Not a cloud service — it runs on your machines, your network, your git.
- Not magic — it coordinates and integrates; your agents still do the coding.

---

## Key Concepts

| Concept | What it is | Command |
|---------|-----------|---------|
| **Colony** | The coordination server — task queue, scheduler, merge queue. One per project. | `antfarm colony` |
| **Node** | One machine. Registers once. | `antfarm join` |
| **Worker** | One AI agent session. A node can run many workers. | `antfarm worker start` |
| **Soldier** | A special worker that merges PRs safely into the integration branch. | `antfarm worker start --agent soldier` |
| **Carry** | Add a task to the colony's queue. | `antfarm carry` |
| **Forage** | Claim the next available task. | `antfarm forage` |
| **Trail** | Progress breadcrumbs left by a worker. | `antfarm trail` |
| **Harvest** | Mark a task complete — PR is ready. | `antfarm harvest` |
| **Scout** | See what every worker is doing. | `antfarm scout` |
| **Guard** | Lock a shared resource (e.g., DB migrations). | `antfarm guard` |
| **Hatch** | Register a worker without starting the full lifecycle. | `antfarm hatch` |

**Relationship:** Colony → many Nodes → many Workers per Node.

```
Colony
├── Node: node-1
│   ├── Worker: node-1/claude-1    (Claude Code session)
│   ├── Worker: node-1/codex-1     (Codex session)
│   └── Worker: node-1/claude-2    (another Claude Code session)
├── Node: node-2
│   └── Worker: node-2/claude-1
└── Node: node-3
    └── Worker: node-3/aider-1
```

---

## How It Works

```
You carry tasks → Colony queues them → Workers claim and build → Integrator merges safely

┌─────────────────────────────────────────────────────┐
│              Colony (lead machine)                   │
│                                                     │
│  antfarm colony (:7433)                             │
│  Task queue · Scheduler · Merge queue               │
│                                                     │
│  Worker: node-1/claude-1 (also does work)           │
│  Integrator: node-1/soldier                         │
│                                                     │
└──────────────────┬──────────────────────────────────┘
                   │ HTTP + JSON over private network
         ┌─────────┼─────────┐
         ▼                   ▼
   Node: node-2          Node: node-3
   Worker: claude-1      Worker: aider-1
   Worker: codex-1
```

### The flow

1. **You carry tasks** — manually or via script
2. **Workers claim tasks** — each worker runs `antfarm worker start`, which registers, claims the next available task, and launches your AI agent
3. **Workers build** — they branch from the integration branch (e.g., `dev`), implement, test, open a PR, then claim the next task
4. **Integrator merges** — merges PRs to a temp integration branch, runs tests, fast-forwards the integration branch if green, kicks back if red
5. **You ship** — merge the integration branch to main when ready

---

## Quick Start

```bash
# Clone and install (not yet on PyPI)
git clone https://github.com/vkulkarni/antfarm.git
cd antfarm
pip install -e .
```

### On the lead machine

```bash
# Start the colony
antfarm colony

# Register this machine
antfarm join --node node-1

# Carry some tasks to the queue
antfarm carry --title "Build auth API" \
             --spec "JWT endpoints: login, logout, /me" \
             --touches "api,auth,db"

antfarm carry --title "Build user dashboard" \
             --spec "React page showing user profile and settings" \
             --touches "frontend"

# Start a worker
antfarm worker start --agent claude-code --node node-1
```

### On other machines

```bash
export ANTFARM_URL=http://node-1:7433

antfarm join --node node-2
antfarm worker start --agent claude-code --node node-2

# Or run a different agent
antfarm worker start --agent codex --name codex-1 --node node-2
```

### Watch the colony

```bash
antfarm scout

Nodes: 2 online    Workers: 3 active    Tasks: 1 active, 1 ready, 0 done

┌───────────────────┬────────┬─────────────────────┬────────┬──────────┐
│ Worker            │ Agent  │ Task                │ Status │ Trail    │
├───────────────────┼────────┼─────────────────────┼────────┼──────────┤
│ node-1/claude-1   │ claude │ Build auth API      │ active │ 2m ago   │
│ node-2/claude-1   │ claude │ Build user dashboard│ active │ 30s ago  │
│ node-2/codex-1    │ codex  │ (idle)              │ idle   │ —        │
└───────────────────┴────────┴─────────────────────┴────────┴──────────┘
```

---

## Agent Support

Antfarm is designed to work with any AI coding agent that can run shell commands. Workers register with the colony and report via HTTP — if your agent can call `curl`, it can be an Antfarm worker.

| Agent | Integration | Status |
|-------|-------------|--------|
| Claude Code | Agent definitions + hooks | Shipped |
| Generic (curl) | Shell scripts | Shipped |
| Codex | CLI wrapper | Planned |
| Aider | CLI wrapper | Planned |
| Cursor | Extension | Future |

---

## Task Backends

The backend is pluggable. Antfarm ships a clean `TaskBackend` interface — community can add any task system.

| Backend | Status | Notes |
|---------|--------|-------|
| **File** (default) | v0.1 | Zero dependencies. Local filesystem. |
| **Redis** | Planned (v0.2) | Real-time events, blocking pulls, TTL-based presence |
| **GitHub Issues** | Future | Sync tasks from GitHub Issues |
| **Jira** | Future | Enterprise task backend |

---

## The Integrator

This is Antfarm's core value. Most multi-agent systems fail at integration — three agents independently produce working code that breaks when combined.

The Integrator (Soldier) is a **deterministic merge gate** — no AI, no LLM. Like CI, it doesn't fix your code; it tells you what's broken. Workers handle the fixes and re-submit.

```
PR arrives from worker
  │
  ├── Merge to temp integration branch (never directly to dev)
  │
  ├── Merge conflict? → Kick back to worker
  │
  ├── Run full test suite
  │
  ├── Green? → Fast-forward integration branch
  │
  └── Tests fail? → Kick back to worker with test output
```

The Integrator respects task dependencies — it won't merge task B until task A is merged, even if B finished first.

---

## Networking

Antfarm requires private network connectivity between workers and the colony. It uses HTTP + JSON — debuggable with `curl`, compatible with everything.

| Setup | Works? |
|-------|--------|
| Same LAN | Yes |
| Tailscale (recommended) | Yes |
| WireGuard / VPN | Yes |
| SSH tunnel | Yes |
| Cloud VPC | Yes |
| Same machine | Yes |

**Tailscale is recommended** because it handles NAT, gives stable hostnames, and works across networks. But any private path works.

---

## Multiple Workers Per Machine

A machine with enough RAM can run multiple workers. Each worker is an independent session:

```bash
# Two Claude Code workers on node-1
antfarm worker start --agent claude-code --name claude-1
antfarm worker start --agent claude-code --name claude-2

# Mixed agents on node-2 (e.g., Mac Mini)
antfarm worker start --agent claude-code --name claude-1
antfarm worker start --agent codex --name codex-1
```

Each worker must use a separate git worktree or clone. Antfarm does not share workspaces.

---

## Design Principles

1. **Coordination layer, not a framework.** Antfarm manages tasks and merges. Your agents write code.
2. **Agent-compatible.** Baseline compatibility for any agent; deeper integration via adapters.
3. **Explicit registration.** Antfarm only knows workers that register. No process scanning, no magic discovery.
4. **Integration safety is the core value.** The hard problem is safely merging concurrent AI-generated changes.
5. **Git is the source of truth.** Antfarm adds a thin layer above git — tasks, presence, guards. Code lives in git.
6. **Pluggable backends.** File by default. Redis, GitHub Issues, Jira via plugins.
7. **No mandatory infrastructure.** No cloud services, no paid subscriptions, no vendor lock-in.

---

## Requirements

- Python 3.12+
- Git
- Any AI coding agent
- Any git remote (GitHub, GitLab, Gitea, etc.)
- Private network between machines (Tailscale recommended)

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                antfarm (core)                   │
│                                                 │
│  Colony server · Task scheduler · Merge queue   │
│                                                 │
├─────────────────────────────────────────────────┤
│                 adapters                        │
│                                                 │
│  Claude Code (Shipped) · Generic (Shipped)     │
│  Codex · Aider · Cursor (planned)              │
│                                                 │
├─────────────────────────────────────────────────┤
│                 backends                        │
│                                                 │
│  File (v0.1) · Redis (v0.2)                    │
│  GitHub Issues · Jira (future)                 │
└─────────────────────────────────────────────────┘
```

---

## Status

**v0.1.0** — Core loop complete. Colony, workers, integrator, and CLI all functional.

See the project docs for details:

- [SPEC.md](docs/SPEC.md) — product and architecture spec
- [IMPLEMENTATION.md](docs/IMPLEMENTATION.md) — v0.1 implementation plan
- [DEVELOPMENT.md](docs/DEVELOPMENT.md) — open-source development workflow

---

## License

MIT. See [LICENSE](LICENSE) for details.

---

## Contributing

Antfarm welcomes contributions. For early contributions, please open an issue before starting larger changes.

The most impactful areas:

- **Testing** — real-world multi-machine workflows
- **Docs** — setup guides for different network configurations
- **Adapters** — Codex, Aider, Cursor (after core loop is proven)
- **Backends** — Redis, GitHub Issues, Linear (after core loop is proven)
