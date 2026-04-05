#!/usr/bin/env bash
curl -s -m 1 "$ANTFARM_URL/workers/$WORKER_ID/heartbeat" -X POST \
  -H "Content-Type: application/json" -d '{}' || true
