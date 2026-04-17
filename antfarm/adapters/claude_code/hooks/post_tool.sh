#!/usr/bin/env bash
# PostToolUse hook: clear the worker's current action once the tool returns.
curl -s -m 1 "$ANTFARM_URL/workers/$WORKER_ID/activity" -X POST \
  -H "Content-Type: application/json" \
  -d '{"action": null}' || true
