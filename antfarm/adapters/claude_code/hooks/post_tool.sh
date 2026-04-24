#!/usr/bin/env bash
# PostToolUse hook: set worker activity to "awaiting claude response".
# Claude Code fires pre/post hooks per top-level tool use, serially. Nested
# operations happen inside the tool process and do not re-fire hooks, so we
# don't need to worry about clearing too early.
#
# Previously this posted action=null (clear). We now post a canonical
# "awaiting" verb so the TUI shows a useful state between tool calls
# instead of a blank em-dash (#348).
set -u
: "${ANTFARM_URL:?}"
: "${WORKER_ID:?}"

curl -s -m 1 "$ANTFARM_URL/workers/$WORKER_ID/activity" \
  -X POST -H "Content-Type: application/json" \
  -d '{"action":"awaiting","target":"","source":"hook"}' || true
