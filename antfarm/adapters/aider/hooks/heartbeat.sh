#!/usr/bin/env bash
# heartbeat.sh — POST a heartbeat to the Antfarm colony after each Aider tool use.
# Wire this into your shell or Aider's --exec-before-reply hook.
#
# Required env vars:
#   ANTFARM_URL   — colony base URL (default: http://localhost:7433)
#   WORKER_ID     — worker ID assigned by the colony

ANTFARM_URL="${ANTFARM_URL:-http://localhost:7433}"
WORKER_ID="${WORKER_ID:?WORKER_ID is required}"

curl -s -m 1 "$ANTFARM_URL/workers/$WORKER_ID/heartbeat" -X POST \
  -H "Content-Type: application/json" -d '{}' || true
