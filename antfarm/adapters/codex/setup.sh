#!/usr/bin/env bash
# setup.sh — Install the Antfarm Codex adapter into a project.
#
# Run from your project root:
#   bash path/to/antfarm/adapters/codex/setup.sh
#
# Idempotent: safe to run multiple times.

set -euo pipefail

ADAPTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PWD}"

echo "Antfarm Codex adapter setup"
echo "  Adapter source : ${ADAPTER_DIR}"
echo "  Project root   : ${PROJECT_ROOT}"
echo ""

# 1. Make heartbeat hook executable
HEARTBEAT_CMD="${ADAPTER_DIR}/hooks/heartbeat.sh"
chmod +x "${HEARTBEAT_CMD}"
echo "[ready]   ${HEARTBEAT_CMD}"

echo ""
echo "Done. Installed:"
echo "  hooks/heartbeat.sh  — sends heartbeat to colony (call every 30-60 s)"
echo ""
echo "Set environment variables before starting a worker:"
echo "  export ANTFARM_URL=http://localhost:7433"
echo "  export WORKER_ID=<your-worker-id>"
echo ""
echo "Start a Codex worker with antfarm:"
echo "  antfarm work --agent-type codex --workspace-root /path/to/workspaces"
