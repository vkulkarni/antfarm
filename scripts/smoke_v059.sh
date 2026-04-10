#!/usr/bin/env bash
# smoke_v059.sh — on-demand smoke tests for v0.5.9 safety hardening features.
#
# Exercises:
#   1. Max-attempt enforcement (task -> blocked after N kickbacks)
#   2. Cascade invalidation (downstream done task kicked back with upstream)
#   3. Smart worktree cleanup (clean orphan deleted, dirty orphan kept)
#
# Does NOT spawn a colony server or real workers — uses in-process FileBackend
# and a throwaway git repo. Run from the repo root:
#
#   bash scripts/smoke_v059.sh
#
# Exits non-zero on any assertion failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3.12}"

echo "=== v0.5.9 smoke tests ==="
echo "Repo: $REPO_ROOT"
echo "Version: $($PYTHON -c 'import antfarm.core as c; print(c.__version__)')"
echo

# ---------------------------------------------------------------------------
# 1. Max-attempt enforcement
# ---------------------------------------------------------------------------
echo "[1/3] max-attempt enforcement..."
$PYTHON - <<'PY'
import tempfile
from datetime import UTC, datetime
from antfarm.core.backends.file import FileBackend

with tempfile.TemporaryDirectory() as d:
    b = FileBackend(root=f"{d}/.antfarm")
    now = datetime.now(UTC).isoformat()
    b.carry({
        "id": "t1", "title": "T", "spec": "S",
        "created_at": now, "updated_at": now, "created_by": "smoke",
    })
    for i in range(3):
        t = b.pull("w1")
        assert t is not None, f"pull {i} returned None"
        b.mark_harvested("t1", t["current_attempt"], pr=f"pr{i}", branch=f"b{i}")
        b.kickback("t1", f"fail {i}", max_attempts=3)

    task = b.get_task("t1")
    assert task["status"] == "blocked", f"expected blocked, got {task['status']}"
    # Should no longer be forageable
    assert b.pull("w1") is None, "blocked task should not be forageable"
print("  OK — task blocked after 3 kickbacks, not forageable")
PY

# ---------------------------------------------------------------------------
# 2. Cascade invalidation
# ---------------------------------------------------------------------------
echo "[2/3] cascade invalidation..."
$PYTHON - <<'PY'
import tempfile
from datetime import UTC, datetime
from antfarm.core.backends.file import FileBackend

with tempfile.TemporaryDirectory() as d:
    b = FileBackend(root=f"{d}/.antfarm")
    now = datetime.now(UTC).isoformat()

    # A (upstream) and B (depends on A)
    b.carry({"id":"A","title":"A","spec":"s","depends_on":[],
             "created_at":now,"updated_at":now,"created_by":"smoke"})
    b.carry({"id":"B","title":"B","spec":"s","depends_on":["A"],
             "created_at":now,"updated_at":now,"created_by":"smoke"})

    # Work A through to done
    ta = b.pull("w1"); assert ta["id"] == "A"
    b.mark_harvested("A", ta["current_attempt"], pr="prA", branch="bA")

    # Work B through to done (depends_on A is now done)
    tb = b.pull("w1"); assert tb["id"] == "B"
    b.mark_harvested("B", tb["current_attempt"], pr="prB", branch="bB")

    # Kick back A with cascade — B (downstream done) should be invalidated too
    from antfarm.core.soldier import Soldier
    soldier = Soldier.from_backend(
        backend=b, repo_path=".", integration_branch="main", require_review=False,
    )
    soldier.kickback_with_cascade("A", "smoke-fail")

    a = b.get_task("A")
    bt = b.get_task("B")
    assert a["status"] == "ready", f"A should be ready, got {a['status']}"
    assert bt["status"] == "ready", f"B should be cascade-kicked to ready, got {bt['status']}"
print("  OK — downstream done task cascaded back to ready")
PY

# ---------------------------------------------------------------------------
# 3. Smart worktree cleanup
# ---------------------------------------------------------------------------
echo "[3/3] smart worktree cleanup..."
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

(
  cd "$SCRATCH"
  git init -q repo
  cd repo
  git config user.email smoke@test
  git config user.name smoke
  git commit -q --allow-empty -m init
  mkdir -p ../workspaces
  git worktree add -q ../workspaces/task-clean-att-001 -b feat/smoke-clean
  git worktree add -q ../workspaces/task-dirty-att-001 -b feat/smoke-dirty
  echo "uncommitted" > ../workspaces/task-dirty-att-001/new.txt
)

SCRATCH_ESC="$SCRATCH" $PYTHON - <<'PY'
import os, subprocess
import antfarm.core.doctor as dm

scratch = os.environ["SCRATCH_ESC"]
ws = f"{scratch}/workspaces"
repo = f"{scratch}/repo"

# Patch _worktree_is_clean so "no uncommitted changes" counts as clean
# (the real helper requires an upstream, which scratch repos don't have).
dm._worktree_is_clean = lambda path: (
    subprocess.run(
        ["git", "-C", path, "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout.strip() == ""
)

# data_dir = "<repo>/.antfarm" so check_orphan_workspaces uses <repo> as cwd
findings = dm.check_orphan_workspaces(
    {"workspace_root": ws, "data_dir": f"{repo}/.antfarm"},
    fix=True,
)
for f in findings:
    print(f"   fixed={f.fixed}  {f.message}")

assert not os.path.exists(f"{ws}/task-clean-att-001"), "clean orphan should have been deleted"
assert os.path.exists(f"{ws}/task-dirty-att-001"), "dirty orphan should have been kept"
PY
echo "  OK — clean orphan deleted, dirty orphan kept"

echo
echo "=== all smoke tests passed ==="
