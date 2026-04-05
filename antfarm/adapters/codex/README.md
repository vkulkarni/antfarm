# Codex Adapter

Integrates [OpenAI Codex CLI](https://github.com/openai/codex) with an Antfarm colony.
The adapter configures Codex to run non-interactively in `full-auto` approval mode so
the worker can pull tasks, execute them, and harvest results without human intervention.

## Required environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTFARM_URL` | `http://localhost:7433` | Base URL of the running colony server |
| `WORKER_ID` | *(required)* | Unique identifier for this worker (e.g. `codex-01`) |
| `OPENAI_API_KEY` | *(required)* | OpenAI API key for Codex |

## Quick start

```bash
# 1. Install Codex CLI
npm install -g @openai/codex

# 2. Run setup (makes heartbeat hook executable)
bash path/to/antfarm/adapters/codex/setup.sh

# 3. Export required vars
export ANTFARM_URL="http://localhost:7433"
export WORKER_ID="codex-01"
export OPENAI_API_KEY="sk-..."

# 4. Register worker and start foraging
antfarm work \
  --agent-type codex \
  --worker-id codex-01 \
  --workspace-root /path/to/workspaces
```

## How it works

When the worker picks up a task, Antfarm invokes:

```
codex --approval-mode full-auto --quiet "<prompt>"
```

- `--approval-mode full-auto` — runs without interactive approval prompts
- `--quiet` — suppresses spinner/interactive output for clean subprocess capture

The prompt includes the task title, spec, working directory, and branch name.
Codex implements the task, runs tests, commits, and pushes the branch.

## Heartbeat hook

The `hooks/heartbeat.sh` script sends a heartbeat to the colony every time it is
called. Wire it into a background loop while a task is in flight:

```bash
while true; do bash hooks/heartbeat.sh; sleep 30; done &
HB_PID=$!
# ... do work ...
kill "$HB_PID" 2>/dev/null
```

## Notes

- Codex requires Node.js 18+ and an active `OPENAI_API_KEY`.
- For long-running tasks, increase the colony's heartbeat timeout accordingly.
- The worker deregisters automatically on clean exit or unhandled exception.
