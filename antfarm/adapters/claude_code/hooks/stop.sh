#!/usr/bin/env bash
# Stop hook: report the final assistant message's usage block to the colony.
#
# Claude Code invokes Stop hooks when the conversation ends. Hook event data
# is passed as JSON on stdin (per Claude Code hooks docs). The transcript
# path is extracted from `.transcript_path` in that JSON; we also fall back
# to $CLAUDE_TRANSCRIPT_PATH (env-var form, no longer set by Claude Code).
#
# The last assistant message in the transcript carries a `usage` object with
# input_tokens / output_tokens / cache_*_tokens.
#
# This is best-effort — every failure path falls through to `|| true`, so a
# broken jq install or a missing transcript never blocks Claude. We POST to
# /workers/<id>/usage; the colony computes cost and aggregates per mission.

set -u

: "${ANTFARM_URL:=}"
: "${WORKER_ID:=}"
: "${CLAUDE_TRANSCRIPT_PATH:=}"

# Read stdin JSON from Claude Code hook event. Extract transcript_path.
# Use `jq -r` if available, otherwise fall back to a simple grep/sed.
HOOK_JSON="$(cat /dev/stdin 2>/dev/null || true)"
if [ -n "${HOOK_JSON}" ]; then
  if command -v jq >/dev/null 2>&1; then
    TRANSCRIPT_PATH="$(echo "${HOOK_JSON}" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
  else
    TRANSCRIPT_PATH="$(echo "${HOOK_JSON}" | grep -o '"transcript_path"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"transcript_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/' || true)"
  fi
fi
# Fall back to env var (legacy / pre-stdin Claude Code versions).
: "${TRANSCRIPT_PATH:=${CLAUDE_TRANSCRIPT_PATH}}"

# If any required input is missing, do nothing. Do not error — Claude stops
# anyway and the hook must stay out of the way.
if [ -z "${ANTFARM_URL}" ] || [ -z "${WORKER_ID}" ] || [ -z "${TRANSCRIPT_PATH}" ]; then
  exit 0
fi

if [ ! -f "${TRANSCRIPT_PATH}" ]; then
  exit 0
fi

# Require jq for JSON extraction. If jq isn't installed, skip silently —
# antfarm will warn about it elsewhere.
if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

# Transcript is JSONL. Walk backward to find the last message with a usage
# block. We accept either `message.usage` (nested under `message`) or a
# top-level `usage` — the Claude Code format has evolved.
USAGE_JSON="$(tac "${TRANSCRIPT_PATH}" 2>/dev/null \
  | jq -c 'select(.message?.usage? != null) | .message.usage' 2>/dev/null \
  | head -n 1)"

if [ -z "${USAGE_JSON}" ]; then
  USAGE_JSON="$(tac "${TRANSCRIPT_PATH}" 2>/dev/null \
    | jq -c 'select(.usage? != null) | .usage' 2>/dev/null \
    | head -n 1)"
fi

if [ -z "${USAGE_JSON}" ]; then
  exit 0
fi

# Walk again for the model name. Either message.model or top-level model.
MODEL="$(tac "${TRANSCRIPT_PATH}" 2>/dev/null \
  | jq -r 'select(.message?.model? != null) | .message.model' 2>/dev/null \
  | head -n 1)"
if [ -z "${MODEL}" ] || [ "${MODEL}" = "null" ]; then
  MODEL="$(tac "${TRANSCRIPT_PATH}" 2>/dev/null \
    | jq -r 'select(.model? != null) | .model' 2>/dev/null \
    | head -n 1)"
fi
if [ -z "${MODEL}" ] || [ "${MODEL}" = "null" ]; then
  MODEL="unknown"
fi

INPUT_TOK=$(echo "${USAGE_JSON}" | jq -r '.input_tokens // 0')
OUTPUT_TOK=$(echo "${USAGE_JSON}" | jq -r '.output_tokens // 0')
CACHE_READ=$(echo "${USAGE_JSON}" | jq -r '.cache_read_input_tokens // 0')
CACHE_CREATE=$(echo "${USAGE_JSON}" | jq -r '.cache_creation_input_tokens // 0')

# Generate a stable event_id per (transcript, line-hash) so retries dedupe.
if command -v shasum >/dev/null 2>&1; then
  EVENT_ID="stop-$(echo -n "${TRANSCRIPT_PATH}${USAGE_JSON}" | shasum | awk '{print $1}')"
elif command -v sha1sum >/dev/null 2>&1; then
  EVENT_ID="stop-$(echo -n "${TRANSCRIPT_PATH}${USAGE_JSON}" | sha1sum | awk '{print $1}')"
else
  EVENT_ID="stop-$(date +%s%N)-$$"
fi

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

PAYLOAD=$(cat <<EOF
{
  "event_id": "${EVENT_ID}",
  "ts": "${TS}",
  "model": "${MODEL}",
  "input_tokens": ${INPUT_TOK},
  "output_tokens": ${OUTPUT_TOK},
  "cache_read_tokens": ${CACHE_READ},
  "cache_creation_tokens": ${CACHE_CREATE},
  "source": "claude_stop_hook"
}
EOF
)

curl -s -m 2 "${ANTFARM_URL}/workers/${WORKER_ID}/usage" \
  -X POST -H "Content-Type: application/json" -d "${PAYLOAD}" >/dev/null 2>&1 || true
