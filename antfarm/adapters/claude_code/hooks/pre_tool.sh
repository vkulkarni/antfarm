#!/usr/bin/env bash
# PreToolUse hook: post worker activity before each tool call.
# Claude Code delivers hook context as JSON on stdin.
# See: https://docs.claude.ai/en/docs/claude-code/hooks (PreToolUse)
set -u
: "${ANTFARM_URL:?}"
: "${WORKER_ID:?}"

# Read stdin (may be empty if invoked outside a hook context — fallback to "tool")
INPUT=$(cat 2>/dev/null || echo "")
if command -v jq >/dev/null 2>&1 && [ -n "$INPUT" ]; then
  TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // "tool"' 2>/dev/null || echo "tool")
else
  TOOL_NAME="tool"
fi
ACTION="Running: $TOOL_NAME"

# Use jq to build a safely-quoted JSON payload.
if command -v jq >/dev/null 2>&1; then
  PAYLOAD=$(jq -nc --arg a "$ACTION" '{action:$a}')
else
  # Fallback: best-effort shell-quoted JSON (tool names are alphanumeric in practice).
  PAYLOAD="{\"action\": \"$ACTION\"}"
fi

curl -s -m 1 "$ANTFARM_URL/workers/$WORKER_ID/activity" \
  -X POST -H "Content-Type: application/json" -d "$PAYLOAD" || true
