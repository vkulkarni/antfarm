# antfarm

*Self-hosted coordinator for parallel AI coding agents.*

v0.6.7 · Python 3.12+ · MIT

---

Not a framework. Not AGI. Not a cloud service.

A self-hosted coordinator that fans coding tasks across multiple AI agent sessions — on one machine or many — and merges the results through a deterministic gate.

Closest neighbor is tmux-orchestrator; unlike CrewAI or AutoGen, antfarm is not a meta-agent framework — the agents are real Claude Code (or Codex/Aider) sessions.

---

## What it looks like

```
$ antfarm colony --autoscaler --max-builders 3 --max-reviewers 1 &
[colony] listening on :7433  data_dir=.antfarm  colony=a4f2c1e8
[colony] queen: enabled   soldier: enabled   doctor: enabled   autoscaler: enabled

$ antfarm mission create --spec specs/add-rate-limiter.md
Mission created: {'mission_id': 'add-rate-limiter-7f2'}

$ antfarm scout --watch
14:02:11  queen       planning mission add-rate-limiter-7f2
14:02:34  queen       plan ready: 4 tasks, 1 dep edge
14:02:34  autoscaler  scaling builders 0 -> 3
14:02:41  worker      node-1/builder-1 claimed task-001 (token bucket)
14:02:41  worker      node-1/builder-2 claimed task-002 (config schema)
14:02:42  worker      node-1/builder-3 claimed task-003 (middleware wiring)
14:08:19  worker      node-1/builder-2 harvested task-002  PR #412
14:08:22  soldier     merge_attempted  task-002
14:08:34  soldier     merged           task-002 -> main
14:11:47  worker      node-1/builder-1 harvested task-001  PR #411
14:11:49  autoscaler  scaling reviewers 0 -> 1
14:11:52  worker      node-1/reviewer-1 claimed review-001
14:13:05  worker      node-1/reviewer-1 verdict: needs_changes (2 comments)
14:13:05  soldier     merge_skipped    task-001  reason=needs_changes
14:13:06  soldier     kickback         task-001 -> ready
14:13:08  worker      node-1/builder-1 claimed task-001 (attempt 2)
14:16:41  worker      node-1/builder-1 harvested task-001  PR #411 (force-pushed)
14:16:43  worker      node-1/reviewer-1 verdict: pass (rebased, diff identical)
14:16:43  soldier     merged           task-001 -> main
14:19:02  worker      node-1/builder-3 harvested task-003  PR #413
14:19:14  soldier     merged           task-003 -> main
14:19:14  queen       mission add-rate-limiter-7f2 complete (4/4 merged)
```

No manual intervention between `mission create` and `mission complete`. The kickback at 14:13 is the Soldier refusing to merge a reviewer `needs_changes`; the builder re-tries attempt 2.

<!-- asciicast: record after rewrite lands, link here -->

---

## Who this is for / who it isn't

Check yourself against the lists.

**Built for you if:**
- You have one or more Claude Code / Codex / Aider subscriptions
- You write specs before code
- You are OK running a long-lived process on a trusted network
- You want merges gated by tests, not vibes

**Not for you if:**
- You want one agent to "just figure it out"
- You need enterprise auth / SSO (v0.6.x is unauthenticated — private networks only)
- You want a hosted SaaS
- You want something that writes code without you writing a spec

---

## Pipeline

```
spec → Queen (plans) → Builders (code) → Reviewer (verdict) → Soldier (merge gate) → integration branch
                                                                   ↑
                                                       Autoscaler sizes the pools
```

- **Queen** decomposes a spec into a task graph with dependencies.
- **Builder** claims a task, codes in an isolated worktree, opens a PR.
- **Reviewer** reads the PR diff and returns pass or needs_changes.
- **Soldier** rebases, runs tests, fast-forwards or kicks back. Deterministic — no AI in the gate.
- **Autoscaler** adjusts builder and reviewer pool size to queue depth.

---

## Install and run one mission

```bash
git clone https://github.com/antfarm-ai/antfarm
cd antfarm && pip install -e .
cd /path/to/your/repo
antfarm doctor                      # pre-flight
antfarm colony --autoscaler &       # queen, soldier, doctor, autoscaler all in-process
antfarm mission create --spec specs/my-feature.md
antfarm scout --tui                 # or: scout --watch for raw event feed
```

A spec file is a Markdown document describing the change you want: goal, acceptance criteria, files or modules likely touched. The Queen reads it and produces the task graph. Agent-specific adapters and hook scripts live under [antfarm/adapters/](antfarm/adapters/).

---

## FAQ

**What does this cost?** Your Claude Max subscription or API usage. Antfarm itself is MIT, no telemetry, no hosted component.

**Can I use non-Claude agents?** Shipped: Claude Code reference adapter, Codex, Aider, and a generic curl adapter. Claude Code is the most polished; the others are functional but less exercised.

**Do I need more than one machine?** No. The default dev loop runs on a single machine — the autoscaler spawns worker sessions in local tmux panes. Multi-machine is opt-in via `--multi-node`.

**What happens on a merge conflict?** The Soldier tries a deterministic rebase and `--force-with-lease` push. Genuine conflicts are kicked back to the builder for a new attempt. The gate never invokes an AI to resolve conflicts.

**Auth?** None in v0.6.x. An optional bearer token is available on the colony HTTP API, but the threat model assumes a trusted private network — run it behind Tailscale, WireGuard, or an SSH tunnel.

**How is this different from tmux-orchestrator?** tmux-orchestrator is single-machine and human-in-the-loop. Antfarm adds mission planning, a deterministic merge gate, autoscaling, multi-machine coordination, and SSE event streams.

**What if a worker hangs or crashes?** The Doctor classifies stuck workers and the `--fix` flag recovers them. The autoscaler respawns the pool.

**Does it commit to main directly?** The Soldier merges to a configurable integration branch (`--integration-branch`, default `main`). Point it at a `develop` branch if you want a manual review gate before `main`.

---

## Architecture

```
                  ┌─────────────────────────────────────┐
                  │          colony (HTTP API)          │
                  │   queen · soldier · doctor · autoscaler
                  └──────────────┬──────────────────────┘
                                 │
                ┌────────────────┼───────────────────┐
                │                │                   │
          FileBackend      Runner(s)            Worker pool
          (.antfarm/)      (tmux panes)         builders + reviewers
                                                     │
                                                  adapters
                                          (claude-code / codex / aider)
```

- **Colony server** — FastAPI process that holds the task queue and runs the in-process daemons.
- **Queen** — mission planner: spec → task graph.
- **Soldier** — deterministic merge gate: rebase, test, fast-forward or kickback.
- **Autoscaler** — sizes builder and reviewer pools against queue depth.
- **Runner** — launches worker sessions (tmux locally, SSH + tmux for multi-node).
- **Doctor** — pre-flight checks and stale-state recovery.
- **Workers** — builder or reviewer sessions wrapping a coding agent.
- **Adapters** — per-agent glue: prompts, hooks, heartbeat.
- **FileBackend** — atomic JSON task store under `.antfarm/`.

Deeper reading: [docs/SPEC.md](docs/SPEC.md), [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md), [AGENTS.md](AGENTS.md).

---

## CLI reference

| Command | Purpose | When |
|---|---|---|
| `antfarm colony` | Start the coordinator (plus queen/soldier/doctor/autoscaler) | Once per project, long-lived |
| `antfarm mission create` | Submit a spec as a mission | Start of each feature |
| `antfarm mission status` | Show mission progress and task states | Checking in |
| `antfarm scout --tui` | Rich dashboard | Monitoring |
| `antfarm scout --watch` | Raw SSE event feed | Debugging |
| `antfarm doctor --fix` | Detect and repair stuck state | Before a run; after a crash |
| `antfarm inbox` | Items needing operator attention | Daily triage |
| `antfarm carry` | Add a single task manually | Outside the mission flow |
| `antfarm worker start` | Start a worker by hand | Debugging a specific agent |
| `antfarm memory show` | Inspect the repo memory store | Understanding scheduler bias |

Full CLI: `antfarm --help` and `antfarm <command> --help`.

---

## Requirements

- Python 3.12+
- git
- tmux (optional; recommended for the autoscaler)
- an AI coding agent CLI installed locally (Claude Code, Codex, Aider, or a generic command)
- private network between machines if running multi-node (Tailscale, WireGuard, SSH tunnel)

---

## Status and license

v0.6.7 is the efficiency pass on the v0.6 mission pipeline. CI-green on `main`. Breaking changes and upgrade notes live in [CHANGELOG.md](CHANGELOG.md) and [UPGRADE.md](UPGRADE.md).

License: MIT.

Contributing: see [AGENTS.md](AGENTS.md) for project conventions and the PR workflow.
