#!/usr/bin/env bash
set -euo pipefail
ANTFARM_URL="${ANTFARM_URL:-http://localhost:7433}"
WORKER_ID="${WORKER_ID:?WORKER_ID is required}"
curl -s "$ANTFARM_URL/tasks/pull" -X POST \
  -H "Content-Type: application/json" \
  -d "{\"worker_id\": \"$WORKER_ID\"}"
