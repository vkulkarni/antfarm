#!/usr/bin/env bash
# PreToolUse hook: post structured worker activity before each tool call.
# Claude Code delivers hook context as JSON on stdin.
# See: https://docs.claude.ai/en/docs/claude-code/hooks (PreToolUse)
#
# Emits {action, target, source:"hook"} so the colony server can synthesize
# a human-readable line like "editing src/foo.py" or "running pytest -x"
# and store it in the worker's current_action field (#348).
#
# Graceful fallback: without jq, action/target stay empty and the server
# clamps to sensible defaults. curl with || true ensures the hook never fails.
set -u
: "${ANTFARM_URL:?}"
: "${WORKER_ID:?}"

HOOK_INPUT=$(cat 2>/dev/null || echo "")

ACTION=""
TARGET=""
if command -v jq >/dev/null 2>&1 && [ -n "$HOOK_INPUT" ]; then
  TOOL_NAME=$(printf '%s' "$HOOK_INPUT" | jq -r '.tool_name // ""' 2>/dev/null || echo "")
  case "$TOOL_NAME" in
    Edit|Write)
      TARGET=$(printf '%s' "$HOOK_INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null || echo "")
      ACTION=editing
      ;;
    Read)
      TARGET=$(printf '%s' "$HOOK_INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null || echo "")
      ACTION=reading
      ;;
    Bash)
      # First three space-separated tokens of the command — enough to show
      # what's running without leaking args/secrets.
      TARGET=$(printf '%s' "$HOOK_INPUT" | jq -r '.tool_input.command // "" | split(" ") | .[0:3] | join(" ")' 2>/dev/null || echo "")
      ACTION=running
      ;;
    WebFetch)
      TARGET=$(printf '%s' "$HOOK_INPUT" | jq -r '.tool_input.url // ""' 2>/dev/null || echo "")
      ACTION=searching
      ;;
    WebSearch)
      TARGET=$(printf '%s' "$HOOK_INPUT" | jq -r '.tool_input.query // ""' 2>/dev/null || echo "")
      ACTION=searching
      ;;
    Glob|Grep)
      TARGET=$(printf '%s' "$HOOK_INPUT" | jq -r '.tool_input.pattern // .tool_input.path // ""' 2>/dev/null || echo "")
      ACTION=scanning
      ;;
    TodoWrite)
      TARGET=""
      ACTION=planning
      ;;
    "")
      ACTION=tool
      ;;
    *)
      # Unknown verb: lowercased tool name; server falls through to
      # freeform "<action> <target>" synthesis.
      ACTION=$(printf '%s' "$TOOL_NAME" | tr '[:upper:]' '[:lower:]')
      TARGET=""
      ;;
  esac
fi

# Trim target to 60 chars so the payload stays tiny; server also truncates.
if [ -n "$TARGET" ] && [ "${#TARGET}" -gt 60 ]; then
  TARGET="${TARGET:0:60}"
fi

# Build JSON payload safely when jq is available; fall back to best-effort
# shell quoting otherwise. Tool names and tokens are alphanumeric in practice.
if command -v jq >/dev/null 2>&1; then
  PAYLOAD=$(jq -nc --arg a "$ACTION" --arg t "$TARGET" \
    '{action:$a, target:$t, source:"hook"}')
else
  PAYLOAD="{\"action\":\"$ACTION\",\"target\":\"$TARGET\",\"source\":\"hook\"}"
fi

curl -s -m 1 "$ANTFARM_URL/workers/$WORKER_ID/activity" \
  -X POST -H "Content-Type: application/json" -d "$PAYLOAD" || true
