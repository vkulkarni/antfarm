#!/usr/bin/env bash
set -euo pipefail
git fetch origin
git checkout "${INTEGRATION_BRANCH:-dev}"
git pull origin "${INTEGRATION_BRANCH:-dev}"
