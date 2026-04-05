# Antfarm — Aider Adapter

Run [Aider](https://aider.chat) as an Antfarm worker agent.

## How it works

Antfarm spawns Aider with `--yes` (non-interactive) and `--no-auto-commits`
so the colony controls branching and commit hygiene.  Aider receives the task
spec as its `--message` argument, implements the changes, and exits.  Antfarm
then harvests the result (opens a PR, marks the task done).

## Quick start

```bash
# 1. Install Aider
pip install aider-chat

# 2. Run setup (from your project root)
bash path/to/antfarm/adapters/aider/setup.sh

# 3. Export required variables
export ANTFARM_URL=http://localhost:7433
export ANTFARM_TOKEN=<your-worker-token>   # if auth is enabled
export OPENAI_API_KEY=<your-key>           # or ANTHROPIC_API_KEY / GEMINI_API_KEY

# 4. Start a worker
antfarm worker start \
  --colony http://localhost:7433 \
  --agent-type aider \
  --repo-path /path/to/your/repo
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ANTFARM_URL` | `http://localhost:7433` | Colony base URL |
| `WORKER_ID` | _(set by colony)_ | Worker slot ID (used by heartbeat) |
| `ANTFARM_TOKEN` | _(optional)_ | Bearer token if colony auth is enabled |

## Configuration

`setup.sh` symlinks `.aider.conf.yml` into your project root.  The defaults
set `auto-commits: false` and `yes: true` — do not override these when running
under Antfarm.

You can add model configuration (`model:`, `api-key:`, etc.) to your own
`.aider.conf.yml` after setup; Antfarm-managed keys will not be overwritten.

## Heartbeat

The `hooks/heartbeat.sh` script POSTs to `/workers/$WORKER_ID/heartbeat` to
keep the worker slot alive.  Wire it into Aider's `--exec-before-reply` hook
or call it from a background loop if needed:

```bash
while sleep 30; do bash hooks/heartbeat.sh; done &
```
