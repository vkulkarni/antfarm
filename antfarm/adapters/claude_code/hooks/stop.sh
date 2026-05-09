#!/usr/bin/env bash
# Stop hook: report the final assistant message's usage block to the colony.
#
# Claude Code invokes Stop hooks when the conversation ends. The hook event
# data is delivered as a JSON object on stdin per Claude Code's hook protocol;
# `.transcript_path` is the canonical source for the transcript file. The
# `$CLAUDE_TRANSCRIPT_PATH` env var is honored as a fallback for manual
# invocation or older Claude Code versions.
#
# The transcript is JSONL; the last assistant message carries a `usage` object
# with input_tokens / output_tokens / cache_*_tokens.
#
# This is best-effort — every failure path falls through to `|| true`, so a
# broken jq install or a missing transcript never blocks Claude. We POST to
# /workers/<id>/usage; the colony computes cost and aggregates per mission.

set -u

: "${ANTFARM_URL:=}"
: "${WORKER_ID:=}"
: "${CLAUDE_TRANSCRIPT_PATH:=}"

# Require jq for JSON extraction. If jq isn't installed, skip silently —
# antfarm will warn about it elsewhere. We need jq both for parsing the
# stdin event payload and for walking the transcript further down.
if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

# Claude Code pipes the hook event JSON on stdin. Prefer that over the env
# var so we don't get fooled by stale environment from a parent process.
if ! [ -t 0 ]; then
  STDIN_JSON="$(cat 2>/dev/null || true)"
  if [ -n "${STDIN_JSON}" ]; then
    TPATH_FROM_STDIN="$(printf '%s' "${STDIN_JSON}" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
    if [ -n "${TPATH_FROM_STDIN}" ]; then
      CLAUDE_TRANSCRIPT_PATH="${TPATH_FROM_STDIN}"
    fi
  fi
fi

# If any required input is missing, do nothing. Do not error — Claude stops
# anyway and the hook must stay out of the way.
if [ -z "${ANTFARM_URL}" ] || [ -z "${WORKER_ID}" ] || [ -z "${CLAUDE_TRANSCRIPT_PATH}" ]; then
  exit 0
fi

if [ ! -f "${CLAUDE_TRANSCRIPT_PATH}" ]; then
  exit 0
fi

# Transcript is JSONL. Walk backward to find the last message with a usage
# block. We accept either `message.usage` (nested under `message`) or a
# top-level `usage` — the Claude Code format has evolved.
USAGE_JSON="$(tac "${CLAUDE_TRANSCRIPT_PATH}" 2>/dev/null \
  | jq -c 'select(.message?.usage? != null) | .message.usage' 2>/dev/null \
  | head -n 1)"

if [ -z "${USAGE_JSON}" ]; then
  USAGE_JSON="$(tac "${CLAUDE_TRANSCRIPT_PATH}" 2>/dev/null \
    | jq -c 'select(.usage? != null) | .usage' 2>/dev/null \
    | head -n 1)"
fi

if [ -z "${USAGE_JSON}" ]; then
  exit 0
fi

# Walk again for the model name. Either message.model or top-level model.
MODEL="$(tac "${CLAUDE_TRANSCRIPT_PATH}" 2>/dev/null \
  | jq -r 'select(.message?.model? != null) | .message.model' 2>/dev/null \
  | head -n 1)"
if [ -z "${MODEL}" ] || [ "${MODEL}" = "null" ]; then
  MODEL="$(tac "${CLAUDE_TRANSCRIPT_PATH}" 2>/dev/null \
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
  EVENT_ID="stop-$(echo -n "${CLAUDE_TRANSCRIPT_PATH}${USAGE_JSON}" | shasum | awk '{print $1}')"
elif command -v sha1sum >/dev/null 2>&1; then
  EVENT_ID="stop-$(echo -n "${CLAUDE_TRANSCRIPT_PATH}${USAGE_JSON}" | sha1sum | awk '{print $1}')"
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
