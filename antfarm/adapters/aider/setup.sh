#!/usr/bin/env bash
# setup.sh — Install the Antfarm Aider adapter into a project.
#
# Run from your project root:
#   bash path/to/antfarm/adapters/aider/setup.sh
#
# Idempotent: safe to run multiple times.

set -euo pipefail

ADAPTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PWD}"
CONF_FILE="${PROJECT_ROOT}/.aider.conf.yml"
CONF_SOURCE="${ADAPTER_DIR}/.aider.conf.yml"

echo "Antfarm Aider adapter setup"
echo "  Adapter source : ${ADAPTER_DIR}"
echo "  Project root   : ${PROJECT_ROOT}"
echo ""

# 1. Symlink .aider.conf.yml into the project root
if [ -L "${CONF_FILE}" ]; then
  echo "[exists]  symlink ${CONF_FILE}"
elif [ -f "${CONF_FILE}" ]; then
  echo "[skip]    ${CONF_FILE} already exists as a regular file — not overwriting"
  echo "          Ensure it contains: auto-commits: false  and  yes: true"
else
  ln -s "${CONF_SOURCE}" "${CONF_FILE}"
  echo "[linked]  ${CONF_FILE} -> ${CONF_SOURCE}"
fi

# 2. Make heartbeat executable
chmod +x "${ADAPTER_DIR}/hooks/heartbeat.sh"
echo "[ok]      heartbeat hook is executable"

echo ""
echo "Done. Installed:"
echo "  .aider.conf.yml   — disables auto-commits, enables non-interactive mode"
echo "  hooks/heartbeat.sh — POST /heartbeat to keep worker slot alive"
echo ""
echo "Set environment variables before starting a worker session:"
echo "  export ANTFARM_URL=http://localhost:7433"
echo "  export WORKER_ID=<your-worker-id>"
echo ""
echo "Then start a worker:"
echo "  antfarm worker start --agent-type aider --repo-path ."
