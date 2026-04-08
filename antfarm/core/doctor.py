"""Doctor — pre-flight diagnostic and stale recovery tool for Antfarm.

Reads .antfarm/ files directly (not only through backend API) for mtime
checks, malformed JSON detection, and stale task recovery. This is
intentional: doctor is a diagnostic tool that must see raw filesystem state.

Usage:
    findings = run_doctor(backend, config)          # dry-run
    findings = run_doctor(backend, config, fix=True) # auto-fix safe issues
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class Finding:
    severity: str       # "error", "warning", "info"
    check: str          # e.g., "stale_worker", "stale_task"
    message: str
    auto_fixable: bool
    fixed: bool = False


def run_doctor(backend, config: dict, fix: bool = False) -> list[Finding]:
    """Run all diagnostic checks. If fix=True, apply safe repairs.

    Args:
        backend: A TaskBackend instance (FileBackend).
        config: Dict with keys:
            - data_dir (str): path to .antfarm directory
            - colony_url (str, optional): for reachability check
            - worker_ttl (int, default 300): seconds before worker is stale
            - guard_ttl (int, default 300): seconds before guard is stale
        fix: If True, apply safe auto-fixes.

    Returns:
        List of Finding objects describing issues found.
    """
    findings: list[Finding] = []

    findings.extend(check_filesystem(config, fix))
    findings.extend(check_colony_reachable(config))
    findings.extend(check_git_config())
    findings.extend(check_stale_workers(backend, config, fix))
    findings.extend(check_stale_tasks(backend, config, fix))
    findings.extend(check_stale_guards(backend, config, fix))
    findings.extend(check_workspace_conflicts(backend))
    findings.extend(check_orphan_workspaces(config, fix))
    findings.extend(check_state_consistency(backend))
    findings.extend(check_dependency_cycles(backend))

    return findings


# ---------------------------------------------------------------------------
# Check 1: Filesystem
# ---------------------------------------------------------------------------

_REQUIRED_SUBDIRS = [
    "tasks/ready",
    "tasks/active",
    "tasks/done",
    "workers",
    "nodes",
    "guards",
]


def check_filesystem(config: dict, fix: bool = False) -> list[Finding]:
    """Verify .antfarm/ and required subdirs exist and are writable.

    Fix: create missing directories.

    Args:
        config: Doctor config dict.
        fix: If True, create missing directories.

    Returns:
        List of findings.
    """
    findings: list[Finding] = []
    data_dir = Path(config["data_dir"])

    if not data_dir.exists():
        f = Finding(
            severity="error",
            check="filesystem",
            message=f"data_dir does not exist: {data_dir}",
            auto_fixable=True,
        )
        if fix:
            data_dir.mkdir(parents=True, exist_ok=True)
            f.fixed = True
        findings.append(f)
        # Attempt to create subdirs too if we just created root
        if fix:
            for subdir in _REQUIRED_SUBDIRS:
                (data_dir / subdir).mkdir(parents=True, exist_ok=True)
        return findings

    # Check writability
    if not os.access(str(data_dir), os.W_OK):
        findings.append(Finding(
            severity="error",
            check="filesystem",
            message=f"data_dir is not writable: {data_dir}",
            auto_fixable=False,
        ))

    # Check subdirs
    for subdir in _REQUIRED_SUBDIRS:
        subpath = data_dir / subdir
        if not subpath.exists():
            f = Finding(
                severity="error",
                check="filesystem",
                message=f"Required subdirectory missing: {subpath}",
                auto_fixable=True,
            )
            if fix:
                subpath.mkdir(parents=True, exist_ok=True)
                f.fixed = True
            findings.append(f)
        elif not os.access(str(subpath), os.W_OK):
            findings.append(Finding(
                severity="error",
                check="filesystem",
                message=f"Required subdirectory not writable: {subpath}",
                auto_fixable=False,
            ))

    return findings


# ---------------------------------------------------------------------------
# Check 2: Colony reachability
# ---------------------------------------------------------------------------

def check_colony_reachable(config: dict) -> list[Finding]:
    """If colony_url is configured, attempt GET /status.

    Args:
        config: Doctor config dict.

    Returns:
        List of findings (report only, no fix).
    """
    colony_url = config.get("colony_url")
    if not colony_url:
        return [Finding(
            severity="info",
            check="colony_reachable",
            message="colony_url not configured — skipping reachability check",
            auto_fixable=False,
        )]

    try:
        import urllib.request
        url = colony_url.rstrip("/") + "/status"
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            if resp.status == 200:
                return [Finding(
                    severity="info",
                    check="colony_reachable",
                    message=f"Colony reachable at {colony_url}",
                    auto_fixable=False,
                )]
            return [Finding(
                severity="warning",
                check="colony_reachable",
                message=f"Colony returned HTTP {resp.status} from {url}",
                auto_fixable=False,
            )]
    except Exception as exc:
        return [Finding(
            severity="warning",
            check="colony_reachable",
            message=f"Colony unreachable at {colony_url}: {exc}",
            auto_fixable=False,
        )]


# ---------------------------------------------------------------------------
# Check 3: Git config
# ---------------------------------------------------------------------------

def check_git_config() -> list[Finding]:
    """Verify we are inside a git work tree.

    Args: none

    Returns:
        List of findings (report only, no fix).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=True,
        )
        if result.stdout.strip() == "true":
            return []
        return [Finding(
            severity="warning",
            check="git_config",
            message="Not inside a git work tree",
            auto_fixable=False,
        )]
    except subprocess.CalledProcessError as exc:
        return [Finding(
            severity="warning",
            check="git_config",
            message=f"git rev-parse failed: {exc}",
            auto_fixable=False,
        )]


# ---------------------------------------------------------------------------
# Check 4: Stale workers
# ---------------------------------------------------------------------------

def check_stale_workers(backend, config: dict, fix: bool = False) -> list[Finding]:
    """List worker files in data_dir/workers/. Check mtime vs worker_ttl.

    Fix: deregister stale workers.

    Args:
        backend: TaskBackend instance.
        config: Doctor config dict.
        fix: If True, deregister stale workers.

    Returns:
        List of findings.
    """
    findings: list[Finding] = []
    data_dir = Path(config["data_dir"])
    worker_ttl = config.get("worker_ttl", 300)
    workers_dir = data_dir / "workers"

    if not workers_dir.exists():
        return findings

    now = datetime.now(UTC).timestamp()
    for worker_file in workers_dir.glob("*.json"):
        try:
            stat = os.stat(str(worker_file))
            age = now - stat.st_mtime
            if age > worker_ttl:
                worker_id = worker_file.stem
                f = Finding(
                    severity="warning",
                    check="stale_worker",
                    message=(
                        f"Worker '{worker_id}' has stale heartbeat "
                        f"({age:.0f}s old, TTL={worker_ttl}s)"
                    ),
                    auto_fixable=True,
                )
                if fix:
                    backend.deregister_worker(worker_id)
                    f.fixed = True
                findings.append(f)
        except FileNotFoundError:
            # File disappeared between glob and stat
            continue

    return findings


# ---------------------------------------------------------------------------
# Check 5: Stale tasks
# ---------------------------------------------------------------------------

def check_stale_tasks(backend, config: dict, fix: bool = False) -> list[Finding]:
    """List tasks in active/. Check if current_attempt's worker is live.

    A task is stale if its current_attempt's worker has no live worker file.

    Fix: raw file manipulation — read active task JSON, set status=ready,
    current_attempt=None, supersede the active attempt, add trail entry
    "recovered by doctor", write to ready/, delete from active/.

    Args:
        backend: TaskBackend instance.
        config: Doctor config dict.
        fix: If True, recover stale tasks.

    Returns:
        List of findings.
    """
    findings: list[Finding] = []
    data_dir = Path(config["data_dir"])
    active_dir = data_dir / "tasks" / "active"
    workers_dir = data_dir / "workers"

    if not active_dir.exists():
        return findings

    for task_file in active_dir.glob("*.json"):
        try:
            with open(str(task_file)) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        current_attempt_id = data.get("current_attempt")
        if not current_attempt_id:
            # Orphaned active task (no current_attempt) — caught by state_consistency
            continue

        # Find the current attempt's worker_id
        worker_id = None
        for attempt in data.get("attempts", []):
            if attempt.get("attempt_id") == current_attempt_id:
                worker_id = attempt.get("worker_id")
                break

        # Check if worker file exists (live)
        worker_alive = False
        if worker_id:
            worker_file = workers_dir / f"{worker_id}.json"
            worker_alive = worker_file.exists()

        if not worker_alive:
            task_id = data.get("id", task_file.stem)
            f = Finding(
                severity="warning",
                check="stale_task",
                message=(
                    f"Task '{task_id}' is active but worker '{worker_id}' is dead/missing"
                ),
                auto_fixable=True,
            )
            if fix:
                _recover_stale_task(data_dir, task_file, data, current_attempt_id)
                f.fixed = True
            findings.append(f)

    return findings


def _recover_stale_task(
    data_dir: Path,
    task_file: Path,
    data: dict,
    current_attempt_id: str,
) -> None:
    """Raw file recovery for a stale active task.

    Supersedes the current attempt, resets status to ready, adds a trail
    entry, writes to ready/, and deletes from active/.

    Args:
        data_dir: Root .antfarm directory.
        task_file: Path to the active task JSON file.
        data: Parsed task dict.
        current_attempt_id: The attempt ID to supersede.
    """
    now = datetime.now(UTC).isoformat()

    # Supersede the current attempt
    for attempt in data.get("attempts", []):
        if attempt.get("attempt_id") == current_attempt_id:
            attempt["status"] = "superseded"
            attempt["completed_at"] = now
            break

    # Reset task to ready
    data["status"] = "ready"
    data["current_attempt"] = None
    data["updated_at"] = now

    # Add trail entry
    data.setdefault("trail", [])
    data["trail"].append({
        "ts": now,
        "worker_id": "doctor",
        "message": "recovered by doctor",
    })

    # Write to ready/ atomically, then delete from active/
    ready_path = data_dir / "tasks" / "ready" / task_file.name
    tmp_path = ready_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2))
    tmp_path.replace(ready_path)
    task_file.unlink()


# ---------------------------------------------------------------------------
# Check 6: Stale guards
# ---------------------------------------------------------------------------

def check_stale_guards(backend, config: dict, fix: bool = False) -> list[Finding]:
    """List guard files in data_dir/guards/. Check mtime vs guard_ttl AND owner liveness.

    Fix: os.unlink() stale guard files.

    Args:
        backend: TaskBackend instance.
        config: Doctor config dict.
        fix: If True, delete stale guards.

    Returns:
        List of findings.
    """
    findings: list[Finding] = []
    data_dir = Path(config["data_dir"])
    guard_ttl = config.get("guard_ttl", 300)
    guards_dir = data_dir / "guards"
    workers_dir = data_dir / "workers"

    if not guards_dir.exists():
        return findings

    now = datetime.now(UTC).timestamp()
    for guard_file in guards_dir.glob("*.lock"):
        try:
            stat = os.stat(str(guard_file))
            age = now - stat.st_mtime
        except FileNotFoundError:
            continue

        if age <= guard_ttl:
            continue

        # Guard is old; check owner liveness
        try:
            guard_data = json.loads(guard_file.read_text())
            owner = guard_data.get("owner", "")
        except (json.JSONDecodeError, OSError):
            owner = ""

        owner_alive = (workers_dir / f"{owner}.json").exists() if owner else False
        if owner_alive:
            # Guard mtime is old but owner is still live — not stale
            continue

        resource = guard_file.stem.replace("__", "/")
        f = Finding(
            severity="warning",
            check="stale_guard",
            message=(
                f"Guard '{resource}' is stale ({age:.0f}s old, TTL={guard_ttl}s, "
                f"owner='{owner}' dead)"
            ),
            auto_fixable=True,
        )
        if fix:
            guard_file.unlink(missing_ok=True)
            f.fixed = True
        findings.append(f)

    return findings


# ---------------------------------------------------------------------------
# Check 7: Workspace conflicts
# ---------------------------------------------------------------------------

def check_workspace_conflicts(backend) -> list[Finding]:
    """Check if any active workers share the same workspace_root.

    Args:
        backend: TaskBackend instance.

    Returns:
        List of findings (report only, no fix).
    """
    findings: list[Finding] = []

    try:
        workers = backend.list_workers() if hasattr(backend, "list_workers") else []
    except Exception:
        workers = []

    # Fall back to reading worker files directly if list_workers not available
    if not workers and hasattr(backend, "_root"):
        workers_dir = backend._root / "workers"
        if workers_dir.exists():
            for wf in workers_dir.glob("*.json"):
                try:
                    workers.append(json.loads(wf.read_text()))
                except (json.JSONDecodeError, OSError):
                    continue

    workspace_to_workers: dict[str, list[str]] = {}
    for w in workers:
        ws_root = w.get("workspace_root", "")
        if ws_root:
            workspace_to_workers.setdefault(ws_root, []).append(w.get("worker_id", "unknown"))

    for ws_root, worker_ids in workspace_to_workers.items():
        if len(worker_ids) > 1:
            findings.append(Finding(
                severity="warning",
                check="workspace_conflict",
                message=(
                    f"Workers {worker_ids} share workspace_root '{ws_root}'"
                ),
                auto_fixable=False,
            ))

    return findings


# ---------------------------------------------------------------------------
# Check 8: Orphan workspaces
# ---------------------------------------------------------------------------

def _worktree_is_clean(path: str) -> bool:
    """Check if a worktree is provably clean (safe to delete).

    Returns True ONLY when both checks succeed AND show no changes.
    Any failure, missing upstream, or ambiguous state returns False (keep it).
    """
    try:
        # Check for uncommitted changes
        status = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
        if status.stdout.strip():
            return False  # has uncommitted changes

        # Check for unpushed commits — requires upstream to be configured
        log = subprocess.run(
            ["git", "-C", path, "log", "@{u}..", "--oneline"],
            capture_output=True, text=True, check=False,
        )
        if log.returncode != 0:
            return False  # no upstream configured or git error — keep it
        return not log.stdout.strip()  # clean only if no unpushed commits
    except Exception:
        return False  # any error → keep it (safe default)


def check_orphan_workspaces(config: dict, fix: bool = False) -> list[Finding]:
    """List worktree dirs under workspace_root if configured.

    When fix=True, provably clean worktrees are auto-deleted via
    ``git worktree remove``. Worktrees with uncommitted or unpushed
    changes are kept for debugging.

    Args:
        config: Doctor config dict.
        fix: If True, delete clean orphan worktrees.

    Returns:
        List of findings.
    """
    findings: list[Finding] = []
    workspace_root = config.get("workspace_root")
    if not workspace_root:
        return findings

    ws_path = Path(workspace_root)
    if not ws_path.exists():
        return findings

    # Worktree dirs are any subdirectories under workspace_root
    for entry in ws_path.iterdir():
        if entry.is_dir():
            if fix and _worktree_is_clean(str(entry)):
                # Safe to delete — provably clean
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", str(entry)],
                        capture_output=True, text=True, check=True,
                    )
                    findings.append(Finding(
                        severity="info",
                        check="orphan_workspace",
                        message=f"Orphan worktree auto-deleted (clean): {entry}",
                        auto_fixable=True,
                        fixed=True,
                    ))
                except subprocess.CalledProcessError:
                    findings.append(Finding(
                        severity="info",
                        check="orphan_workspace",
                        message=(
                            f"Worktree directory found with no associated active task: "
                            f"{entry} (removal failed)"
                        ),
                        auto_fixable=False,
                    ))
            elif fix:
                findings.append(Finding(
                    severity="info",
                    check="orphan_workspace",
                    message=(
                        f"Orphan worktree kept: has changes or could not verify "
                        f"clean state: {entry}"
                    ),
                    auto_fixable=True,
                    fixed=False,
                ))
            else:
                findings.append(Finding(
                    severity="info",
                    check="orphan_workspace",
                    message=(
                        f"Worktree directory found with no associated active task: {entry}"
                    ),
                    auto_fixable=True,
                ))

    return findings


# ---------------------------------------------------------------------------
# Check 9: State consistency
# ---------------------------------------------------------------------------

def check_state_consistency(backend) -> list[Finding]:
    """Read task files directly and check for state inconsistencies.

    Checks:
    (a) task in ready/ but status != "ready"
    (b) task in active/ with no current_attempt
    (c) task with >1 active attempt
    (d) malformed JSON

    Args:
        backend: TaskBackend instance.

    Returns:
        List of findings (report only, no fix).
    """
    findings: list[Finding] = []

    if not hasattr(backend, "_root"):
        return findings

    root = backend._root
    folders = [
        ("ready", root / "tasks" / "ready"),
        ("active", root / "tasks" / "active"),
        ("done", root / "tasks" / "done"),
    ]

    for folder_name, folder_path in folders:
        if not folder_path.exists():
            continue
        for task_file in folder_path.glob("*.json"):
            task_id = task_file.stem

            # (d) malformed JSON
            try:
                data = json.loads(task_file.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                findings.append(Finding(
                    severity="error",
                    check="state_consistency",
                    message=f"Malformed JSON in {task_file.name}: {exc}",
                    auto_fixable=False,
                ))
                continue

            status = data.get("status", "")

            # (a) status field doesn't match folder
            if status != folder_name:
                findings.append(Finding(
                    severity="error",
                    check="state_consistency",
                    message=(
                        f"Task '{task_id}' is in {folder_name}/ but status='{status}'"
                    ),
                    auto_fixable=False,
                ))

            # (b) task in active/ with no current_attempt
            if folder_name == "active" and not data.get("current_attempt"):
                findings.append(Finding(
                    severity="error",
                    check="state_consistency",
                    message=f"Task '{task_id}' is in active/ but has no current_attempt",
                    auto_fixable=False,
                ))

            # (c) more than one ACTIVE attempt
            active_attempts = [
                a for a in data.get("attempts", [])
                if a.get("status") == "active"
            ]
            if len(active_attempts) > 1:
                findings.append(Finding(
                    severity="error",
                    check="state_consistency",
                    message=(
                        f"Task '{task_id}' has {len(active_attempts)} active attempts "
                        f"(should be at most 1)"
                    ),
                    auto_fixable=False,
                ))

            # (e) done task with current_attempt pointing to non-existent attempt
            current = data.get("current_attempt")
            if current and folder_name == "done":
                attempt_ids = {a.get("attempt_id") for a in data.get("attempts", [])}
                if current not in attempt_ids:
                    findings.append(Finding(
                        severity="error",
                        check="state_consistency",
                        message=(
                            f"Task '{task_id}' in done/ has current_attempt='{current}' "
                            f"but no matching attempt exists"
                        ),
                        auto_fixable=False,
                    ))

            # (f) merged attempt is current_attempt while task status is ready
            if current and folder_name == "ready":
                for attempt in data.get("attempts", []):
                    if (
                        attempt.get("attempt_id") == current
                        and attempt.get("status") == "merged"
                    ):
                        findings.append(Finding(
                            severity="error",
                            check="state_consistency",
                            message=(
                                f"Task '{task_id}' in ready/ has a merged attempt "
                                f"as current_attempt — invalid state"
                            ),
                            auto_fixable=False,
                        ))

    return findings


# ---------------------------------------------------------------------------
# Check 10: Dependency cycles
# ---------------------------------------------------------------------------

def check_dependency_cycles(backend) -> list[Finding]:
    """Load all tasks, build dependency graph, detect cycles and dangling refs.

    Uses DFS cycle detection. Also detects dangling depends_on references.

    Args:
        backend: TaskBackend instance.

    Returns:
        List of findings (report only, no fix).
    """
    findings: list[Finding] = []

    try:
        all_tasks = backend.list_tasks()
    except Exception as exc:
        findings.append(Finding(
            severity="error",
            check="dependency_cycles",
            message=f"Could not load tasks for dependency check: {exc}",
            auto_fixable=False,
        ))
        return findings

    task_ids = {t["id"] for t in all_tasks}
    deps: dict[str, list[str]] = {t["id"]: list(t.get("depends_on", [])) for t in all_tasks}

    # Detect dangling references
    for task_id, task_deps in deps.items():
        for dep in task_deps:
            if dep not in task_ids:
                findings.append(Finding(
                    severity="warning",
                    check="dangling_dependency",
                    message=f"Task '{task_id}' depends on non-existent task '{dep}'",
                    auto_fixable=False,
                ))

    # DFS cycle detection
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in task_ids}
    cycle_reported: set[str] = set()

    def dfs(node: str, path: list[str]) -> None:
        color[node] = GRAY
        path.append(node)
        for neighbor in deps.get(node, []):
            if neighbor not in task_ids:
                continue  # dangling — already reported
            if color[neighbor] == GRAY:
                # Found a cycle — report the cycle path
                cycle_start = path.index(neighbor)
                cycle_nodes = path[cycle_start:]
                cycle_key = "->".join(sorted(cycle_nodes))
                if cycle_key not in cycle_reported:
                    cycle_reported.add(cycle_key)
                    findings.append(Finding(
                        severity="error",
                        check="dependency_cycles",
                        message=(
                            f"Dependency cycle detected: "
                            f"{' -> '.join(cycle_nodes)} -> {neighbor}"
                        ),
                        auto_fixable=False,
                    ))
            elif color[neighbor] == WHITE:
                dfs(neighbor, path)
        path.pop()
        color[node] = BLACK

    for tid in task_ids:
        if color[tid] == WHITE:
            dfs(tid, [])

    return findings
