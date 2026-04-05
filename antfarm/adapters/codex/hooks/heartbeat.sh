#!/usr/bin/env bash
ANTFARM_URL="${ANTFARM_URL:-http://localhost:7433}"
WORKER_ID="${WORKER_ID:?WORKER_ID is required}"
curl -s -m 1 "$ANTFARM_URL/workers/$WORKER_ID/heartbeat" -X POST \
  -H "Content-Type: application/json" -d '{}' || true
