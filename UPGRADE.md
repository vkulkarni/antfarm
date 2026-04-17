# Upgrade Guide

This document describes breaking operational changes that require manual
action between Antfarm versions.

## 0.8.0 — Colony UUID Identity (#238)

Colony identity is now a persisted UUID stored as ``colony_id`` inside
``{data_dir}/config.json``. The 8-char hash embedded in tmux session names
(``auto-<hash>-*``, ``runner-<hash>-*``) is derived from this UUID rather
than from ``os.path.realpath(data_dir)``.

**Why:** realpath-based identity was fragile. Moving ``.antfarm`` with
``mv``, remounting it via NFS, or re-pointing a Docker bind-mount all
produced a new hash, silently orphaning every running tmux session.
A persisted UUID is stable across all three.

**Consequence:** the first startup after upgrade generates a new UUID for
each colony. Tmux sessions spawned by a pre-upgrade build used the old
realpath-based hash, so they no longer match the colony's new prefix and
become unmanaged orphans.

**Recovery:** drain in-flight work (see the "Before you sweep" section
below), then clean up:

```bash
antfarm doctor --sweep-legacy-tmux
```

**Escape hatch (advanced).** To preserve the old hash across the upgrade —
e.g., when you have legacy sessions you cannot drain safely — compute the
pre-upgrade realpath hash and seed it as ``colony_id`` in
``config.json`` *before* the first post-upgrade startup:

```bash
python3 -c "import hashlib, os; print(hashlib.sha256(os.path.realpath('.antfarm').encode()).hexdigest()[:8])"
# -> a1b2c3d4

# then seed config.json (jq preserves other keys):
jq '.colony_id = "a1b2c3d4"' .antfarm/config.json > /tmp/c && mv /tmp/c .antfarm/config.json
```

Any non-empty string is accepted as ``colony_id`` — the value is simply
rehashed through SHA-256. Future operators should prefer the UUID default.

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

## Before you sweep: drain in-flight legacy workers

Killing a tmux session kills whatever is running inside it. If a legacy worker is mid-task when you sweep, its attempt is lost and the task has to be kicked back for re-forage.

**Safe order of operations:**

1. Stop scheduling new work onto the legacy colony (pause your queen / stop submitting missions).
2. Wait for in-flight attempts to harvest their PRs. Watch `antfarm scout --tui` until the Building and Review panels are empty, or run `tmux ls` and inspect the legacy sessions manually.
3. Then run the sweep.

Harvested-but-not-merged work survives (it's already a PR in git). Un-harvested attempts are lost and the task will need a kickback. If you can't drain safely, prefer manual `tmux kill-session -t <name>` targeted at specific idle sessions.

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

---

## Deploy Identity Model

The 8-char hash embedded in deploy session names is computed as:

```python
colony_hash(f"{realpath(fleet_config)}|{colony_url}")
```

This means session ownership is determined by two things: **where the fleet config
lives** (resolved absolute path) and **which colony the deploy targets**. The
following scenarios explain the resulting behaviour.

### Same colony, same fleet path → shared sessions (cooperative mode)

Two operators running `antfarm deploy` with the same fleet file at the same
absolute path against the same colony URL will produce identical hashes and
therefore identical session names. The second `deploy` call attaches into the
existing sessions via `tmux new-session -A` rather than spawning duplicates.

This is the intended cooperative mode when a team shares a single deploy machine:
one person deploys, another can check status or attach without creating extra
workers.

### Same colony, different fleet paths → isolated namespaces

Operator A uses `~/team-a/fleet.json` and operator B uses `~/team-b/fleet.json`,
both targeting the same colony. The differing `realpath` values produce different
hashes, so each operator's sessions are fully isolated. Operator A killing "their"
deploy sessions will not nuke Operator B's workers on the same node.

### Different colony URLs → distinct namespaces

Two deploys aimed at different colony URLs (e.g., `http://colony-dev:7433` vs
`http://colony-prod:7433`) always produce different hashes — even with the same
fleet file. Workers on the dev colony never collide with workers on the prod
colony.

This is the fix introduced in #235: previously, sessions were named only by
`node_id + worker_index`, so workers from different colonies silently shared the
same session name on the same host.

### Known edge case: localhost + SSH tunnel

If two operators both deploy using `colony_url=http://localhost:7433` (each with
their own SSH tunnel to a different remote colony), the URL component of the hash
is identical. If they also happen to use the same fleet config path (e.g., both
checked out the repo to `~/antfarm/`), the hashes collide and the second deploy
silently attaches into the first operator's sessions.

**Recommendation:** When deploying to a remote colony via SSH tunnel, pass the
colony's actual public or internal address rather than `localhost`:

```bash
antfarm deploy --colony-url http://colony-host:7433 --fleet-config fleet.json
```

This ensures each operator gets a unique hash even if their fleet config paths
are identical.
