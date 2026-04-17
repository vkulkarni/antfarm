# Antfarm Operator Guide

Practical, task-oriented reference for operators running Antfarm in a live
setting. Pair this with `docs/SPEC.md` (design intent) and `UPGRADE.md`
(version-to-version migration notes) — this guide focuses on the commands
you run day to day.

## Contents

1. [What antfarm is (and isn't)](#what-antfarm-is-and-isnt)
2. [Prerequisites](#prerequisites)
3. [First-run setup](#first-run-setup)
4. [Task management](#task-management)
5. [Missions (autonomous runs)](#missions-autonomous-runs)
6. [Workers](#workers)
7. [Monitoring](#monitoring)
8. [Recovery](#recovery)
9. [Overrides and control](#overrides-and-control)
10. [Multi-colony and multi-host](#multi-colony-and-multi-host)
11. [Auth](#auth)
12. [Log levels and debugging](#log-levels-and-debugging)
13. [Troubleshooting](#troubleshooting)
14. [Where things live](#where-things-live)
15. [Upgrade guidance](#upgrade-guidance)

---

## What antfarm is (and isn't)

Antfarm is a lightweight orchestration layer for distributing coding work
across machines running AI coding agents. It coordinates task assignment,
workspace isolation, and safe integration — it does NOT write code itself.
Workers wrap tools like Claude Code, Codex, Aider, or any shell-driven
agent via the `generic` adapter. Everything Antfarm ships (colony,
scheduler, backend, doctor, soldier) is deterministic. Only the workers
may be AI-powered, and Antfarm does not assume they are.

---

## Prerequisites

- Python 3.12 or newer.
- git with network access to your origin remote.
- tmux is strongly recommended. The autoscaler, runner, and deploy paths
  default to `TmuxProcessManager`; without tmux, Antfarm falls back to
  `SubprocessProcessManager` and loses restart adoption. `antfarm doctor`
  warns when tmux is missing.
- A git repo in a clean state with an integration branch you control.
  Default is `main` for the colony, `dev` for deploy. Override with
  `--integration-branch`.
- On POSIX systems only. No Windows support in v0.6.x.

---

## First-run setup

Install in editable mode:

```bash
pip install -e ".[dev]"
```

Run pre-flight checks before starting the colony:

```bash
antfarm doctor
```

Fix anything the dry run reports, then start the colony:

```bash
antfarm colony
```

Defaults: listens on `0.0.0.0:7433`, data in `./.antfarm/`,
integration branch `main`, Soldier + Doctor + Queen daemons enabled.
Disable any of them with `--no-soldier`, `--no-doctor`, `--no-queen`.

Useful flags:

```bash
antfarm colony --port 7433 --data-dir .antfarm \
  --repo-path . --integration-branch main \
  --autoscaler --max-builders 4 --max-reviewers 2
```

Environment variables most commands honor:

- `ANTFARM_URL` — default colony URL for every client command.
- `ANTFARM_TOKEN` — bearer token for client auth.
- `ANTFARM_AUTH_TOKEN` — shared secret for `antfarm colony --auth-token`.
- `ANTFARM_LOG_LEVEL` — controls the Python logging level used by
  `setup_logging()`. `DEBUG` is noisy but surfaces transport and tmux
  lifecycle. Library imports never install logging handlers; only the CLI
  calls `setup_logging()`.

The TUI and every other client need the colony running. If you see
"connection refused" or "Can't reach colony", you almost always just
forgot to start it — see [Troubleshooting](#troubleshooting).

---

## Task management

Create a task:

```bash
antfarm carry \
  --title "Add /health endpoint" \
  --spec "Expose GET /health returning 200 OK with build sha." \
  --complexity M \
  --priority 10 \
  --touches api \
  --depends-on task-1702999999999
```

Bulk or templated tasks load from JSON:

```bash
antfarm carry --file task.json
```

Useful options:

- `--depends-on <id>` (repeatable) — gate this task behind another.
- `--touches a,b,c` — scope tags used by the scheduler to avoid
  overlapping claims.
- `--priority N` — lower is higher priority. Default 10.
- `--complexity {S,M,L}` — hint for sizing.
- `--capabilities <csv>` — required worker capabilities (e.g. `gpu`,
  `docker`). Matches against worker capabilities set at start.
- `--issue N` — append a GitHub issue reference to the spec so the worker
  puts `#N` in its commits.
- `--mission <id>` — attach the task to an existing mission.
- `--type plan` — file a planner task. Worker with the `plan` capability
  decomposes it into child tasks.

View colony state:

```bash
antfarm scout                 # one-shot table
antfarm scout --watch         # polling table
antfarm scout --tui           # full dashboard (requires colony running)
```

Bulk import from GitHub or a JSON file:

```bash
antfarm import --from github --repo owner/repo --label antfarm
antfarm import --from json   --file tasks.json --dry-run
```

Human overrides (each takes the task id):

```bash
antfarm pause <task-id>
antfarm resume <task-id>
antfarm block <task-id> "reason"
antfarm unblock <task-id>
antfarm reassign <task-id> <worker-id>
antfarm pin <task-id> <worker-id>
antfarm unpin <task-id>
```

Planner tasks: use `antfarm carry --type plan` (or `--capabilities plan`)
when you want a worker to decompose a task into child tasks before any
implementation. The shorthand is:

```bash
antfarm plan --spec "Implement auth" --carry
```

This runs `PlannerEngine` locally, previews the proposed child tasks,
then submits them when `--carry` is set.

---

## Missions (autonomous runs)

A mission wraps a spec into a Queen-driven state machine:
`PLANNING → REVIEWING_PLAN → BUILDING → COMPLETE`. The Queen advances
phases automatically and the colony auto-generates the plan, review, and
builder tasks along the way.

Create a mission from a spec file:

```bash
antfarm mission create --spec missions/auth.md
```

Other mission commands:

```bash
antfarm mission list
antfarm mission list --status BUILDING
antfarm mission status <mission-id>
antfarm mission report <mission-id> --format md
antfarm mission cancel <mission-id>
```

Run the colony with the autoscaler so the Queen can spawn workers on
demand:

```bash
antfarm colony --autoscaler --max-builders 4 --max-reviewers 2
```

Disable the Queen (e.g. to treat the colony as a pure task queue):

```bash
antfarm colony --no-queen
```

Common `mission create` flags:

- `--no-plan-review` — skip the plan review phase.
- `--max-builders N` — per-mission cap.
- `--max-attempts N` — per-task kickback budget.
- `--integration-branch <name>` — override the colony default.

---

## Workers

Start a worker manually:

```bash
antfarm worker start \
  --node node-1 \
  --name builder-1 \
  --agent claude-code \
  --type builder \
  --repo-path . \
  --integration-branch main
```

Agent types shipped: `claude-code`, `codex`, `aider`, `generic`.
Worker types: `builder` (default), `reviewer`, `planner`. Reviewer and
planner types auto-append their capability (`review` / `plan`).
Additional capabilities are `--capabilities gpu,docker` (matched against
`capabilities_required` on tasks).

Workers poll on an empty queue — they do not exit (shipped in #144/#180).
If you observe an early exit, capture the session log; it is a bug.

For multi-node operation, run a `Runner` on each host and let the colony
autoscaler hand work out over Runner URLs:

```bash
# on each worker host
antfarm runner \
  --colony-url http://colony-host:7433 \
  --repo-path /srv/repo \
  --host 0.0.0.0 --port 7434 \
  --max-workers 4 \
  --agent claude-code \
  --capabilities gpu,docker
```

The Runner API has no authentication. Bind to loopback or a private LAN
address only. Never expose it to the internet.

For SSH-driven fleets, describe nodes in a fleet config and deploy:

```bash
antfarm deploy --fleet-config .antfarm/fleet.json
antfarm deploy --fleet-config .antfarm/fleet.json --status
```

Deploy session names are colony-scoped via
`hash(realpath(fleet_config) | colony_url)`, so two colonies on the same
host can coexist without stepping on each other.

List registered workers and their rate-limit state:

```bash
antfarm workers
```

---

## Monitoring

Three tools cover most of the day:

- `antfarm scout --tui` — live dashboard. Needs a running colony. Shows
  queue counts, mission progress, worker panel with a per-worker
  Activity column (current action + elapsed time, shipped in #239/#245),
  and actionable guidance when the colony is unreachable (#246).
- `antfarm scent <task-id>` — tail trail entries for one task via an SSE
  stream. Good for "what is this worker doing right now".
- `antfarm workers` — flat list of workers with status and any active
  cooldown window from rate-limit backoff.

Surface operator-attention items:

```bash
antfarm inbox
```

`inbox` aggregates stale workers, blocked/failed tasks, and missions
stuck in review. It prints a severity + suggested action per row.

For historical state, `antfarm scout` alone (no `--watch`) dumps a
snapshot table you can pipe or diff.

---

## Recovery

Run doctor whenever state looks off:

```bash
antfarm doctor            # dry run, reports findings
antfarm doctor --fix      # applies safe auto-fixes
```

`--fix` currently handles: stale workers (heartbeat TTL expired), stuck
active tasks (worker gone but task still in `active/`), stale guards,
orphan tmux sessions owned by this colony, missing state directories.
Checks that cannot be auto-fixed (colony unreachable, git config issues,
workspace conflicts) are reported only.

One-time migration from pre-#231/#235 tmux sessions after a version
upgrade:

```bash
antfarm doctor --sweep-legacy-tmux          # interactive preview + confirm
antfarm doctor --sweep-legacy-tmux --yes    # skip confirm, still drain first
```

This operates host-wide and kills any tmux session with the legacy
`auto-*`, `runner-*`, or `antfarm-*` prefix that does not include a
colony hash. Drain in-flight work first — see `UPGRADE.md` for the
"Before you sweep" checklist.

Expected recovery behaviors:

- **Stale worker.** Heartbeat TTL expires. Doctor deregisters the worker.
  Its active task moves back to `ready/`, trail preserved. Next forage
  creates a fresh attempt.
- **Orphan tmux session.** tmux process is running but no matching
  `ProcessMetadata` JSON exists. Doctor flags it; `--fix` kills it.
- **Kicked-back task.** Soldier supersedes the old attempt and closes its
  PR automatically (#222). A fresh attempt starts on next forage.
- **Stuck worker.** `check_stuck_workers` warns when a worker has been
  idle on the same action for more than five minutes (#239).

---

## Overrides and control

Per-task controls (all shown above under Task management):
`pause`, `resume`, `block <reason>`, `unblock`, `reassign`,
`pin`, `unpin`.

Merge-queue ordering for done tasks (Soldier input):

```bash
antfarm override-order <task-id> <position>     # lower = merges first
antfarm clear-override-order <task-id>
```

Advisory resource locks:

```bash
antfarm guard   <resource> --owner <worker-or-id>
antfarm release <resource> --owner <same-id>
```

Guards are a `threading.Lock()`-protected file in `.antfarm/guards/`.
Only the owner can release; doctor `--fix` can clear stale guards whose
TTL has expired.

---

## Multi-colony and multi-host

Colony identity is a persisted UUID stored at
`.antfarm/config.json::colony_id` (shipped in #238). The 8-char hash
embedded in tmux session names is derived from this UUID via
`colony_session_hash()`. Moving `.antfarm/` with `mv`, remounting it over
NFS, or re-pointing a Docker bind-mount all keep the same identity.

Two colonies on the same host therefore coexist safely — their session
prefixes differ:

- Autoscaler: `auto-<hash>-builder-3`, `auto-<hash>-reviewer-1`.
- Runner: `runner-<hash>-planner-1`.
- Deploy: `antfarm-<hash>-<node>-<agent>-<idx>` (hash derived from
  `realpath(fleet_config) | colony_url`, see `UPGRADE.md`).

Find your colony's hash:

```bash
# logged on startup
colony id: <uuid>  hash: a1b2c3d4  (data_dir: /srv/colony/.antfarm)

# or compute
python3 -c "from antfarm.core.process_manager import colony_session_hash; \
  print(colony_session_hash('.antfarm'))"
```

---

## Auth

v0.6.x authentication is optional. The default is no auth and is intended
for trusted private networks only. When you need it, enable bearer-token
auth on the server:

```bash
antfarm colony --auth-token "$SECRET"
```

The colony logs the generated token at startup. Pass the token to every
client:

```bash
antfarm carry --title t --spec s --token "$TOKEN"
# or
export ANTFARM_TOKEN="$TOKEN"
antfarm carry --title t --spec s
```

`GET /status` is always public so doctor and liveness probes work without
a token. All mutating endpoints require the token when `--auth-token` is
set.

The Runner daemon does not accept a bearer token for its own API — it
trusts its bind address. Keep it on loopback or a private LAN.

---

## Log levels and debugging

```bash
ANTFARM_LOG_LEVEL=DEBUG antfarm colony
```

`setup_logging()` is only called from CLI entry points, so importing
`antfarm` as a library never installs handlers. The CLI fixed the silent
`logger.info`/`logger.warning` no-ops in #214.

At startup the colony logs a single line you can grep on:

```
colony id: <uuid>  hash: <8hex>  (data_dir: /srv/colony/.antfarm)
```

This fires once per server startup via the FastAPI startup event (#249),
so it is reliable for mapping a tmux session back to its colony.

`antfarm version` prints the installed package version (backed by
package metadata since #213 — editable installs no longer drift).

---

## Troubleshooting

Concrete symptoms with concrete fixes.

- **"TUI says 'Can't reach colony'"** — nothing is listening on the
  configured URL. Start the colony in another terminal:

  ```bash
  antfarm colony
  lsof -iTCP:7433 -sTCP:LISTEN
  ```

  If you point the TUI at a remote colony, confirm the URL and that port
  7433 (or your chosen port) is reachable.

- **"ConnectError: All connection attempts failed"** — same root cause as
  the TUI error. Colony is not running at the URL in `ANTFARM_URL`.

- **"Builder exits immediately after start"** — should not happen since
  #180. If you see it, check the worker session log in
  `.antfarm/tmux/<session>.log` or attach directly with
  `tmux attach -t <session>`.

- **"Reviewer verdict missing"** — fixed by the reviewer retry logic in
  #143/#179. If you still see it, run `antfarm doctor` and check worker
  logs for adapter errors.

- **"Stale guard on resource X"** — clear it:

  ```bash
  antfarm doctor --fix
  ```

  Clears guards whose mtime is past TTL. Targeted manual clear:
  `antfarm release <resource> --owner <original-owner>`.

- **"Duplicate PRs from kickback"** — fixed in #222: Soldier now closes
  the superseded attempt's PR with a comment. Verify version with
  `antfarm version`; if older, upgrade or close the old PR manually.

- **"Orphan tmux sessions after restart"** — normal if tmux dies between
  adoption passes. Run `antfarm doctor --fix` to clean.
  `check_orphan_tmux_sessions` (#208) flags sessions with antfarm
  prefixes that have no matching `ProcessMetadata` file.

- **"Legacy tmux sessions after upgrade"** — one-time migration:

  ```bash
  antfarm doctor --sweep-legacy-tmux --yes
  ```

  Drain in-flight work first. See `UPGRADE.md`.

- **"Task stuck in review forever"** — check `antfarm scout --tui`, look
  at the review task's status. If the reviewer never ran, it is almost
  always capability mismatch: carry the task with the right
  `--capabilities` or start a reviewer with the expected capability set.
  Doctor's `check_stuck_workers` surfaces workers idle on an action for
  more than five minutes (#239).

- **"Peer colony killed my sessions"** — pre-#231 bug; fixed by
  colony-scoped session hashes. Verify each colony logs a distinct
  `colony hash: ...` at startup. Sweep legacy sessions if the hashes
  differ but you still see collisions.

- **"Soldier never rereviews a kicked-back task"** — fixed in #226 via an
  attempt-SHA marker in the review spec. Upgrade if on an older build.

---

## Where things live

```
.antfarm/
  config.json                 # colony_id (UUID), repo_path, integration_branch, TTLs
  tasks/
    ready/                    # backlog — moving a file IS claiming
    active/                   # claimed tasks
    done/                     # harvested tasks (still here after merge)
  workers/                    # worker registration JSON, mtime = last heartbeat
  nodes/                      # node registrations
  guards/                     # advisory resource locks
  processes/                  # ProcessMetadata JSON per managed worker
  workspaces/                 # git worktrees (one per task attempt)
  tmux/                       # per-session logs (when tmux backend in use)
  backup_status.json          # last backup result (if --backup-dest is set)
```

Tmux session naming:

- Autoscaler: `auto-<hash>-<role>-<N>` (role is `builder` / `reviewer` /
  `planner`).
- Runner: `runner-<hash>-<role>-<N>`.
- Deploy: `antfarm-<hash>-<node>-<agent>-<idx>`.

`.antfarm/` is runtime state. Never commit it. If you need to move a
colony, `mv` the directory — colony identity is stable across moves
thanks to the persisted UUID.

---

## Upgrade guidance

Version-specific operational changes live in `UPGRADE.md`. Highlights as
of v0.7.0:

- Colony identity is a persisted UUID (#238). Pre-upgrade tmux sessions
  use the old realpath-based hash and become orphans — sweep them after
  draining.
- Autoscaler/runner session names are colony-scoped (#231).
- Deploy session names are colony-scoped (#235).
- Doctor `--sweep-legacy-tmux` is the one-time migration tool (#237).

Always read the matching section of `UPGRADE.md` before upgrading a live
colony, especially when moving across the v0.6.2 → v0.6.3 → v0.7.0 line.
