#!/usr/bin/env bash
# PostToolUse hook: clear worker activity.
# Claude Code fires pre/post hooks per top-level tool use, serially.
# Nested operations happen inside the tool process and do not re-fire hooks,
# so we don't need to worry about clearing too early.
set -u
: "${ANTFARM_URL:?}"
: "${WORKER_ID:?}"

curl -s -m 1 "$ANTFARM_URL/workers/$WORKER_ID/activity" \
  -X POST -H "Content-Type: application/json" -d '{"action": null}' || true
