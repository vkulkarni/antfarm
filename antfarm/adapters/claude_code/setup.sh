#!/usr/bin/env bash
# setup.sh — Install the Antfarm Claude Code adapter into a project.
#
# Run from your project root:
#   bash path/to/antfarm/adapters/claude_code/setup.sh
#
# Idempotent: safe to run multiple times.

set -euo pipefail

ADAPTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PWD}"
CLAUDE_DIR="${PROJECT_ROOT}/.claude"
AGENTS_DIR="${CLAUDE_DIR}/agents"
SETTINGS_FILE="${CLAUDE_DIR}/settings.json"

echo "Antfarm Claude Code adapter setup"
echo "  Adapter source : ${ADAPTER_DIR}"
echo "  Project root   : ${PROJECT_ROOT}"
echo ""

# 1. Create .claude/agents/ if needed
if [ ! -d "${AGENTS_DIR}" ]; then
  mkdir -p "${AGENTS_DIR}"
  echo "[created] ${AGENTS_DIR}"
else
  echo "[exists]  ${AGENTS_DIR}"
fi

# 2. Symlink agent definitions into .claude/agents/
for agent_md in "${ADAPTER_DIR}/agents/"*.md; do
  name="$(basename "${agent_md}")"
  target="${AGENTS_DIR}/${name}"
  if [ -L "${target}" ]; then
    echo "[exists]  symlink ${target}"
  elif [ -f "${target}" ]; then
    echo "[skip]    ${target} already exists as a regular file — not overwriting"
  else
    ln -s "${agent_md}" "${target}"
    echo "[linked]  ${target} -> ${agent_md}"
  fi
done

# 3. Add heartbeat PostToolUse hook to .claude/settings.json
HEARTBEAT_CMD="${ADAPTER_DIR}/hooks/heartbeat.sh"

if [ ! -f "${SETTINGS_FILE}" ]; then
  # Create a minimal settings.json with the heartbeat hook
  cat > "${SETTINGS_FILE}" <<EOF
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "${HEARTBEAT_CMD}"
          }
        ]
      }
    ]
  }
}
EOF
  echo "[created] ${SETTINGS_FILE} with heartbeat hook"
else
  # Check if heartbeat is already present
  if grep -q "heartbeat.sh" "${SETTINGS_FILE}" 2>/dev/null; then
    echo "[exists]  heartbeat hook already in ${SETTINGS_FILE}"
  else
    echo ""
    echo "[manual]  heartbeat hook NOT added automatically — settings.json already exists."
    echo "          Add this to your PostToolUse hooks in ${SETTINGS_FILE}:"
    echo ""
    echo '          {'
    echo '            "type": "command",'
    echo "            \"command\": \"${HEARTBEAT_CMD}\""
    echo '          }'
    echo ""
  fi
fi

echo ""
echo "Done. Installed:"
echo "  .claude/agents/worker.md   — worker agent (forage → implement → harvest)"
echo "  .claude/agents/soldier.md  — v0.2 placeholder (conflict resolution)"
echo "  .claude/agents/queen.md    — example: AI-driven task decomposition"
echo "  PostToolUse heartbeat hook — keeps worker slot alive automatically"
echo ""
echo "Set environment variables before starting a worker session:"
echo "  export ANTFARM_URL=http://localhost:8000"
echo "  export WORKER_ID=<your-worker-id>"
