# Generic curl Adapter

A shell-script adapter for any CLI agent that can call `curl`. Use these scripts
as building blocks to integrate any agent — written in any language — with an
Antfarm colony over HTTP.

## Required environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTFARM_URL` | `http://localhost:7433` | Base URL of the running colony server |
| `WORKER_ID` | *(required)* | Unique identifier for this worker (e.g. `my-agent-01`) |

## Quick start

```bash
export ANTFARM_URL="http://localhost:7433"
export WORKER_ID="my-agent-01"

# 1. Register
curl -s "$ANTFARM_URL/workers/register" -X POST \
  -H "Content-Type: application/json" \
  -d '{"worker_id":"my-agent-01","node_id":"host1","agent_type":"generic","workspace_root":"/tmp/workspace"}'

# 2. Pull a task, work on it, then harvest
bash forage.sh

# 3. Deregister when done
curl -s "$ANTFARM_URL/workers/my-agent-01" -X DELETE
```

## All 4 adapter contract calls

### 1. Forage — pull the next task

```bash
curl -s "$ANTFARM_URL/tasks/pull" -X POST \
  -H "Content-Type: application/json" \
  -d "{\"worker_id\": \"$WORKER_ID\"}"
```

Returns the task JSON (200) or an empty 204 when the queue is empty.
Use the bundled `forage.sh` script as a convenience wrapper.

### 2. Trail — append a progress log entry

```bash
TASK_ID="my-task-1"
curl -s "$ANTFARM_URL/tasks/$TASK_ID/trail" -X POST \
  -H "Content-Type: application/json" \
  -d "{\"worker_id\": \"$WORKER_ID\", \"message\": \"Starting implementation\"}"
```

Call this as often as needed while working a task to record progress.

### 3. Heartbeat — signal liveness

```bash
curl -s -m 1 "$ANTFARM_URL/workers/$WORKER_ID/heartbeat" -X POST \
  -H "Content-Type: application/json" -d '{}' || true
```

Send every 30–60 seconds while a task is in flight. The `-m 1` timeout and
`|| true` prevent a slow colony from stalling your agent. Use the bundled
`heartbeat.sh` for convenience.

### 4. Harvest — mark a task complete

```bash
TASK_ID="my-task-1"
ATTEMPT_ID="<attempt_id from forage response>"
curl -s "$ANTFARM_URL/tasks/$TASK_ID/harvest" -X POST \
  -H "Content-Type: application/json" \
  -d "{\"attempt_id\": \"$ATTEMPT_ID\", \"pr\": \"https://github.com/org/repo/pull/42\", \"branch\": \"feat/my-task\"}"
```

## Worker registration and deregistration

### Register

```bash
curl -s "$ANTFARM_URL/workers/register" -X POST \
  -H "Content-Type: application/json" \
  -d "{
    \"worker_id\": \"$WORKER_ID\",
    \"node_id\": \"$(hostname)\",
    \"agent_type\": \"generic\",
    \"workspace_root\": \"$(pwd)\"
  }"
```

Returns 201 on success, 409 if a live worker with the same ID already exists.

### Deregister

```bash
curl -s "$ANTFARM_URL/workers/$WORKER_ID" -X DELETE
```

Always deregister on clean shutdown so the worker slot is freed immediately
rather than waiting for heartbeat timeout.

## Optional telemetry payload

The heartbeat endpoint accepts an optional `status` dict for telemetry:

```bash
curl -s -m 1 "$ANTFARM_URL/workers/$WORKER_ID/heartbeat" -X POST \
  -H "Content-Type: application/json" \
  -d "{\"status\": {\"task\": \"$TASK_ID\", \"step\": \"implementing\", \"pct\": 42}}" || true
```

The colony stores the latest status alongside the heartbeat timestamp.

## Bundled scripts

| Script | Purpose |
|--------|---------|
| `forage.sh` | POST to `/tasks/pull` — returns task JSON or empty on 204 |
| `heartbeat.sh` | POST to `/workers/$WORKER_ID/heartbeat` with 1 s timeout |

Both scripts read `ANTFARM_URL` and `WORKER_ID` from the environment.
`WORKER_ID` is required; if unset the scripts exit immediately with an error.

## Typical agent loop (pseudocode)

```bash
#!/usr/bin/env bash
export ANTFARM_URL="http://localhost:7433"
export WORKER_ID="my-agent-01"

# Register once
curl -s "$ANTFARM_URL/workers/register" -X POST \
  -H "Content-Type: application/json" \
  -d "{\"worker_id\":\"$WORKER_ID\",\"node_id\":\"$(hostname)\",\"agent_type\":\"generic\",\"workspace_root\":\"$(pwd)\"}"

trap 'curl -s "$ANTFARM_URL/workers/$WORKER_ID" -X DELETE' EXIT

while true; do
  TASK=$(bash forage.sh)
  if [ -z "$TASK" ]; then sleep 5; continue; fi

  TASK_ID=$(echo "$TASK" | jq -r '.id')
  ATTEMPT_ID=$(echo "$TASK" | jq -r '.current_attempt')

  # Start background heartbeat
  while true; do bash heartbeat.sh; sleep 30; done &
  HB_PID=$!

  # Do the work ...
  curl -s "$ANTFARM_URL/tasks/$TASK_ID/trail" -X POST \
    -H "Content-Type: application/json" \
    -d "{\"worker_id\":\"$WORKER_ID\",\"message\":\"work complete\"}"

  # Harvest
  curl -s "$ANTFARM_URL/tasks/$TASK_ID/harvest" -X POST \
    -H "Content-Type: application/json" \
    -d "{\"attempt_id\":\"$ATTEMPT_ID\",\"pr\":\"https://...\",\"branch\":\"feat/...\"}"

  kill "$HB_PID" 2>/dev/null
done
```
