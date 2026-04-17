#!/usr/bin/env bash
# PreToolUse hook: post the worker's current action before each tool call.
#
# TODO: CLAUDE_TOOL_NAME is the presumed env var exposed by Claude Code's
# PreToolUse hook — verify against the Claude Code hook contract when
# integrating. The fallback value "tool" keeps the hook harmless if the
# variable name differs.
ACTION="Running: ${CLAUDE_TOOL_NAME:-tool}"
curl -s -m 1 "$ANTFARM_URL/workers/$WORKER_ID/activity" -X POST \
  -H "Content-Type: application/json" \
  -d "{\"action\": \"$ACTION\"}" || true
