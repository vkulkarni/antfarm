# Upgrade Guide

This document describes breaking operational changes that require manual
action between Antfarm versions.

## Session-name format changes

Antfarm now prefixes tmux session names with an 8-char hash of the colony's
`data_dir` (or for deploys, the fleet config realpath + colony URL). This
prevents silent collisions when two colonies run on the same host.

### 0.6.3 — autoscaler / runner session names (#231)

**Old:** `auto-builder-3`, `runner-planner-1`
**New:** `auto-<hash>-builder-3`, `runner-<hash>-planner-1`

### Unreleased — deploy session names (#235)

**Old:** `antfarm-node1-claude-0`
**New:** `antfarm-<hash>-node1-claude-0` (hash derived from
`realpath(fleet_config) + colony_url`)

## Why

Two colonies on the same host (e.g., dev + staging, or two operators
sharing a box) would previously collide on session names. The `-A` flag
(attach-or-create) meant the second process would silently attach into the
first's session — real data corruption potential.

## Finding your colony's hash

Run the colony and check logs for:

```
colony hash: a1b2c3d4 (data_dir: /Users/you/project/.antfarm)
```

Or compute manually:

```bash
python3 -c "from antfarm.core.process_manager import colony_hash; print(colony_hash('.antfarm'))"
```

## Cleanup — manual (any version)

List legacy sessions:

```bash
tmux ls | awk -F: '/^(auto|runner)-[^-]+-[^-]+-[0-9]+:/ && !/^(auto|runner)-[0-9a-f]{8}-/ {print $1}'
tmux ls | awk -F: '/^antfarm-/ && !/^antfarm-[0-9a-f]{8}-/ {print $1}'
```

Kill them:

```bash
tmux ls | awk -F: '/^(auto|runner)-[^-]+-[^-]+-[0-9]+:/ && !/^(auto|runner)-[0-9a-f]{8}-/ {print $1}' | xargs -r -n1 tmux kill-session -t
tmux ls | awk -F: '/^antfarm-/ && !/^antfarm-[0-9a-f]{8}-/ {print $1}' | xargs -r -n1 tmux kill-session -t
```

## Cleanup — `antfarm doctor --sweep-legacy-tmux` (Unreleased / 0.7+)

Preview matches:

```bash
antfarm doctor --sweep-legacy-tmux
```

Kill after confirmation:

```bash
antfarm doctor --sweep-legacy-tmux --yes
```

This operates **host-wide** (not scoped to a single colony), so only run it
when you're sure there's no peer colony on the box using the old format.
Safe on any host that has been fully upgraded.
