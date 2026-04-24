"""Doctor — pre-flight diagnostic and stale recovery tool for Antfarm.

Reads .antfarm/ files directly (not only through backend API) for mtime
checks, malformed JSON detection, and stale task recovery. This is
intentional: doctor is a diagnostic tool that must see raw filesystem state.

Usage:
    findings = run_doctor(backend, config)          # dry-run
    findings = run_doctor(backend, config, fix=True) # auto-fix safe issues
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from antfarm.core.serve import _emit_event

logger = logging.getLogger(__name__)

# UUID4 regex used to parse worktree directory names of the form
# ``{task_id}-{attempt_id}``. See ``check_stale_worktrees`` (#352).
_UUID4_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")

# Matches legacy (pre-#231/#235) tmux session names. A hash slot is exactly
# 8 lowercase hex chars followed by a dash; the negative lookahead ensures
# the token after the prefix is NOT a hash. Regressions in PRs #234 (#231)
# and #243 (#235) introduced colony-hash-prefixed names; pre-upgrade sessions
# have no hash token and must be swept manually.
LEGACY_TMUX_RE = re.compile(
    r"^(?:"
    r"auto-(?![0-9a-f]{8}-)[A-Za-z][A-Za-z0-9_-]*-\d+"
    r"|runner-(?![0-9a-f]{8}-)[A-Za-z][A-Za-z0-9_-]*-\d+"
    r"|antfarm-(?![0-9a-f]{8}-)[A-Za-z0-9_][A-Za-z0-9_-]*-[A-Za-z0-9_-]+-\d+"
    r")$"
)


@dataclass
class Finding:
    severity: str  # "error", "warning", "info"
    check: str  # e.g., "stale_worker", "stale_task"
    message: str
    auto_fixable: bool
    fixed: bool = False


def run_doctor(
    backend,
    config: dict,
    fix: bool = False,
    sweep_legacy_tmux: bool = False,
    keep_worktrees: list[str] | None = None,
) -> list[Finding]:
    """Run all diagnostic checks. If fix=True, apply safe repairs.

    Args:
        backend: A TaskBackend instance (FileBackend).
        config: Dict with keys:
            - data_dir (str): path to .antfarm directory
            - colony_url (str, optional): for reachability check
            - worker_ttl (int, default 300): seconds before worker is stale
            - guard_ttl (int, default 300): seconds before guard is stale
            - worktree_prune_ttl_days (int, default 7): TTL catchall for
              stale worktrees. See ``check_stale_worktrees``.
            - worktree_prune_merged_min_age_hours (int, default 24):
              cool-down age after attempt merge before a worktree is eligible
              for pruning.
        fix: If True, apply safe auto-fixes.
        sweep_legacy_tmux: If True, also kill pre-hash tmux sessions host-wide
            (``auto-``/``runner-``/``antfarm-`` without a colony hash). Intended
            to be driven from the CLI after explicit operator confirmation.
        keep_worktrees: Optional list of absolute paths that must NOT be
            pruned by ``check_stale_worktrees`` even when they qualify. Each
            path is realpath-normalized for comparison.

    Returns:
        List of Finding objects describing issues found.
    """
    findings: list[Finding] = []

    findings.extend(check_filesystem(config, fix))
    findings.extend(check_colony_reachable(config))
    findings.extend(check_git_config())
    findings.extend(check_stale_workers(backend, config, fix))
    findings.extend(check_stuck_workers(backend, config, fix))
    findings.extend(check_stale_tasks(backend, config, fix))
    findings.extend(check_retry_patterns(backend, config))
    findings.extend(check_review_queue_saturated(backend, config))
    findings.extend(check_no_reviewer_capacity(backend, config))
    findings.extend(check_stale_guards(backend, config, fix))
    findings.extend(check_workspace_conflicts(backend))
    findings.extend(check_orphan_workspaces(config, fix))
    findings.extend(check_stale_worktrees(backend, config, fix=fix, keep_worktrees=keep_worktrees))
    findings.extend(check_state_consistency(backend))
    findings.extend(check_dependency_cycles(backend))
    findings.extend(check_runner_health(backend, config))
    findings.extend(check_tmux_available(config))
    findings.extend(check_orphan_tmux_sessions(config, fix))

    if sweep_legacy_tmux:
        findings.extend(sweep_legacy_tmux_sessions(config, confirmed=True))

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
        findings.append(
            Finding(
                severity="error",
                check="filesystem",
                message=f"data_dir is not writable: {data_dir}",
                auto_fixable=False,
            )
        )

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
            findings.append(
                Finding(
                    severity="error",
                    check="filesystem",
                    message=f"Required subdirectory not writable: {subpath}",
                    auto_fixable=False,
                )
            )

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
        return [
            Finding(
                severity="info",
                check="colony_reachable",
                message="colony_url not configured — skipping reachability check",
                auto_fixable=False,
            )
        ]

    try:
        import urllib.request

        url = colony_url.rstrip("/") + "/status"
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            if resp.status == 200:
                return [
                    Finding(
                        severity="info",
                        check="colony_reachable",
                        message=f"Colony reachable at {colony_url}",
                        auto_fixable=False,
                    )
                ]
            return [
                Finding(
                    severity="warning",
                    check="colony_reachable",
                    message=f"Colony returned HTTP {resp.status} from {url}",
                    auto_fixable=False,
                )
            ]
    except Exception as exc:
        return [
            Finding(
                severity="warning",
                check="colony_reachable",
                message=f"Colony unreachable at {colony_url}: {exc}",
                auto_fixable=False,
            )
        ]


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
        return [
            Finding(
                severity="warning",
                check="git_config",
                message="Not inside a git work tree",
                auto_fixable=False,
            )
        ]
    except subprocess.CalledProcessError as exc:
        return [
            Finding(
                severity="warning",
                check="git_config",
                message=f"git rev-parse failed: {exc}",
                auto_fixable=False,
            )
        ]


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
                    # Atomic re-check-and-delete under the backend lock so a
                    # late heartbeat between our stat and deregister cannot
                    # silently evict a live worker (issue #310).
                    if backend.deregister_worker_if_stale(worker_id, worker_ttl):
                        f.fixed = True
                        _emit_event(
                            "stale_worker_recovered",
                            "",
                            f"worker={worker_id}",
                            actor="doctor",
                        )
                    else:
                        f.message += " (worker recovered before fix)"
                findings.append(f)
        except FileNotFoundError:
            # File disappeared between glob and stat
            continue

    return findings


# ---------------------------------------------------------------------------
# Check 4b: Stuck workers (fresh heartbeat, stale current_action_at)
# ---------------------------------------------------------------------------


def check_stuck_workers(backend, config: dict, fix: bool = False) -> list[Finding]:
    """Flag workers whose current_action has been in flight longer than stuck_ttl.

    A worker is "stuck" when its heartbeat is fresh (so it is not a stale
    worker) but its ``current_action_at`` is older than ``stuck_ttl`` seconds.
    This means the worker is still alive but has been executing a single tool
    call (or other activity) for too long — usually a symptom of a hung
    subprocess, a network-pegged tool, or an agent spinning on a stuck loop.

    This check never auto-fixes. It emits a warning so operators can
    investigate or kill the worker manually.

    Args:
        backend: TaskBackend instance.
        config: Doctor config dict; ``stuck_ttl`` (default 300) controls the
            threshold in seconds. ``worker_ttl`` (default 300) determines
            whether heartbeat is considered fresh.
        fix: Unused — stuck workers are not auto-fixable.

    Returns:
        List of findings, one per stuck worker.
    """
    del fix  # unused; stuck workers are reported, not auto-fixed
    findings: list[Finding] = []
    data_dir = Path(config["data_dir"])
    stuck_ttl = config.get("stuck_ttl", 300)
    worker_ttl = config.get("worker_ttl", 300)
    workers_dir = data_dir / "workers"

    if not workers_dir.exists():
        return findings

    now = datetime.now(UTC)
    now_ts = now.timestamp()
    for worker_file in workers_dir.glob("*.json"):
        try:
            stat = os.stat(str(worker_file))
            heartbeat_age = now_ts - stat.st_mtime
            if heartbeat_age > worker_ttl:
                # Already caught by check_stale_workers — don't double-report
                continue
            with open(str(worker_file)) as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue

        action = data.get("current_action")
        action_at = data.get("current_action_at")
        if not action or not action_at:
            continue

        try:
            start = datetime.fromisoformat(action_at)
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            elapsed = int((now - start).total_seconds())
        except (ValueError, TypeError):
            continue

        if elapsed > stuck_ttl:
            worker_id = data.get("worker_id", worker_file.stem)
            findings.append(
                Finding(
                    severity="warning",
                    check="stuck_worker",
                    message=(
                        f"Worker {worker_id!r} stuck on '{action}' for {elapsed}s "
                        f"(threshold={stuck_ttl}s)"
                    ),
                    auto_fixable=False,
                )
            )

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
    # Plumb max_attempts through so stale-recovery honors the same attempt
    # budget as kickback(). Without this, a flapping worker loops through
    # active→ready without the blocked/ routing kicking in (issue #333).
    max_attempts = config.get("max_attempts", 3)

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
        # Worker IDs contain "/" (e.g. "local/auto-planner-1") which the
        # FileBackend encodes as "%2F" in filenames. Match that encoding.
        worker_alive = False
        if worker_id:
            safe_id = worker_id.replace("/", "%2F")
            worker_file = workers_dir / f"{safe_id}.json"
            worker_alive = worker_file.exists()

        if not worker_alive:
            task_id = data.get("id", task_file.stem)
            f = Finding(
                severity="warning",
                check="stale_task",
                message=(f"Task '{task_id}' is active but worker '{worker_id}' is dead/missing"),
                auto_fixable=True,
            )
            if fix:
                # Atomic re-check-and-recover under the backend lock. We pass
                # the UNENCODED worker_id (backend's _worker_path handles the
                # %2F encoding). If the worker's heartbeat returned, or the
                # attempt rotated between this check and the mutation, the
                # backend returns False and we leave the finding as unfixed
                # (issue #310).
                if backend.recover_stale_task_if_worker_dead(
                    task_id, current_attempt_id, max_attempts=max_attempts
                ):
                    f.fixed = True
                    _emit_event(
                        "stale_task_recovered",
                        task_id,
                        f"worker={worker_id}",
                        actor="doctor",
                    )
                else:
                    f.message += " (worker recovered or task drifted; no action taken)"
            findings.append(f)

    return findings


# ---------------------------------------------------------------------------
# Check 5a: Retry patterns (#325)
# ---------------------------------------------------------------------------


_RETRY_INFRA_ID_PREFIXES = ("plan-", "review-", "review-plan-")


def _is_retry_infra_task(task: dict) -> bool:
    """Return True for plan/review infrastructure tasks.

    The doctor check filters these out because operators care about
    implementation retry failures, not scaffolding turnover.
    """
    task_id = task.get("id", "") or ""
    title = task.get("title", "") or ""
    for prefix in _RETRY_INFRA_ID_PREFIXES:
        if task_id.startswith(prefix) or title.startswith(prefix):
            return True
    return False


def _extract_last_failure_reason(task: dict) -> str:
    """Return a human-readable reason for the task's most recent failure.

    Scans the trail in reverse for the first entry with
    ``action_type=='kickback'``; falls back to the last trail entry whose
    message mentions ``stderr``; finally falls back to the last trail
    message. Returns an empty string when no trail exists.
    """
    trail = task.get("trail") or []
    for entry in reversed(trail):
        if entry.get("action_type") == "kickback":
            return str(entry.get("message") or "")
    for entry in reversed(trail):
        msg = str(entry.get("message") or "")
        if "stderr" in msg:
            return msg
    if trail:
        return str(trail[-1].get("message") or "")
    return ""


def check_retry_patterns(backend, config: dict) -> list[Finding]:
    """Surface tasks that are retrying or at the retry ceiling.

    For each non-infra task, count finished attempts (DONE or SUPERSEDED —
    the same definition ``kickback()`` uses). The effective attempt budget
    is the task's ``max_attempts`` override, else ``config['max_attempts']``,
    else ``3``.

    - ``finished >= effective_max`` AND ``status == 'blocked'`` →
      ``error`` severity, ``retry_ceiling`` check.
    - ``finished >= effective_max - 1`` AND ``status != 'blocked'`` AND
      ``finished > 0`` → ``warning`` severity, ``retrying`` check.

    Both findings are report-only (``auto_fixable=False``). Operators
    resolve them by inspecting the trail or calling ``antfarm kickback``.

    Args:
        backend: TaskBackend instance.
        config: Doctor config dict (``max_attempts`` optional, default 3).

    Returns:
        List of findings, one per flagged task.
    """
    findings: list[Finding] = []

    try:
        tasks = backend.list_tasks()
    except Exception:
        return findings

    default_max = config.get("max_attempts", 3)
    finished_statuses = {"done", "superseded"}

    for task in tasks:
        if _is_retry_infra_task(task):
            continue

        attempts = task.get("attempts") or []
        finished = sum(1 for a in attempts if a.get("status") in finished_statuses)
        if finished == 0:
            continue

        effective_max = task.get("max_attempts") or default_max
        status = task.get("status", "")
        task_id = task.get("id", "")
        reason = _extract_last_failure_reason(task)

        if finished >= effective_max and status == "blocked":
            message = f"Task '{task_id}' has failed {finished}/{effective_max} attempts."
            if reason:
                message += f" Last failure: {reason}"
            findings.append(
                Finding(
                    severity="error",
                    check="retry_ceiling",
                    message=message,
                    auto_fixable=False,
                )
            )
            continue

        if finished >= effective_max - 1 and status != "blocked":
            message = f"Task '{task_id}' has failed {finished} of max {effective_max} attempts."
            if reason:
                message += f" Last failure: {reason}"
            findings.append(
                Finding(
                    severity="warning",
                    check="retrying",
                    message=message,
                    auto_fixable=False,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Check 5b: No reviewer capacity
# ---------------------------------------------------------------------------


def check_review_queue_saturated(backend, config: dict) -> list[Finding]:
    """Warn when awaiting-review tasks exceed ``max_reviewers * 2`` for ≥2 minutes.

    Detects the saturation pattern from issue #347: builders keep producing
    work faster than reviewers can clear it, so the "awaiting review" queue
    grows unbounded. The dwell window prevents false positives during bursts.

    Sidecar state at ``{data_dir}/doctor_state/review_saturation.json`` tracks
    when the queue first became saturated so we only fire once the condition
    has held continuously for ``dwell_seconds``. Healthy state clears the
    sidecar.

    No auto-fix — operator must re-tune autoscaler sizing.

    Args:
        backend: TaskBackend instance.
        config: Doctor config dict; reads ``max_reviewers`` (default 2) and
            ``data_dir``.

    Returns:
        List of findings (at most one).
    """
    from antfarm.core.warnings import detect_review_queue_saturated

    try:
        tasks = backend.list_tasks()
    except Exception:
        return []

    max_reviewers = int(config.get("max_reviewers", 2))
    data_dir = config.get("data_dir", ".antfarm")
    sidecar_dir = os.path.join(data_dir, "doctor_state")
    sidecar_path = os.path.join(sidecar_dir, "review_saturation.json")

    # Compute awaiting count using the same logic as the warning helper so we
    # don't double-count. We rely on the helper returning None below-threshold
    # and use a probe-with-far-past-timestamp to distinguish "below threshold"
    # from "above but within dwell".
    from antfarm.core.warnings import _count_awaiting_review

    now = datetime.now(UTC)
    awaiting_count = _count_awaiting_review(tasks)
    threshold = max_reviewers * 2

    if awaiting_count <= threshold:
        # Healthy — clear any prior sidecar.
        if os.path.exists(sidecar_path):
            with contextlib.suppress(OSError):
                os.unlink(sidecar_path)
        return []

    # Saturated — ensure sidecar records first_seen.
    first_seen_at: str | None = None
    if os.path.exists(sidecar_path):
        try:
            with open(sidecar_path) as f:
                first_seen_at = json.load(f).get("first_seen_at")
        except (json.JSONDecodeError, OSError):
            first_seen_at = None

    if first_seen_at is None:
        with contextlib.suppress(OSError):
            os.makedirs(sidecar_dir, exist_ok=True)
            with open(sidecar_path, "w") as f:
                json.dump({"first_seen_at": now.isoformat()}, f)
        return []

    warning = detect_review_queue_saturated(
        tasks=tasks,
        max_reviewers=max_reviewers,
        awaiting_first_seen_at=first_seen_at,
        now=now,
    )
    if warning is None:
        return []

    return [
        Finding(
            severity="warning",
            check="review_queue_saturated",
            message=warning["message"],
            auto_fixable=False,
        )
    ]


def check_no_reviewer_capacity(backend, config: dict) -> list[Finding]:  # noqa: ARG001
    """Warn when ready review tasks exist but no registered worker has the review capability.

    This detects the common operator error where doctor requeued a review task
    but no reviewer worker is running (e.g. autoscaler off, no standalone
    reviewer started). The task silently sits in ready forever without this check.

    No auto-fix: Antfarm never spawns workers on the operator's behalf.

    Args:
        backend: TaskBackend instance.
        config: Doctor config dict (unused; kept for consistent signature).

    Returns:
        List of findings (at most one).
    """
    from antfarm.core.warnings import detect_no_reviewer_capacity

    try:
        tasks = backend.list_tasks()
        workers = backend.list_workers() if hasattr(backend, "list_workers") else []
    except Exception:
        return []

    warning = detect_no_reviewer_capacity(tasks, workers)
    if warning is None:
        return []

    return [
        Finding(
            severity="warning",
            check="no_reviewer_capacity",
            message=warning["message"],
            auto_fixable=False,
        )
    ]


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

        safe_owner = owner.replace("/", "%2F") if owner else ""
        owner_alive = (workers_dir / f"{safe_owner}.json").exists() if owner else False
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
            # Atomic re-check-and-release under the backend lock. If the owner
            # reappeared or the guard was re-acquired by another worker between
            # our observation and the mutation, the backend returns False and
            # we leave the finding unfixed (issue #310).
            if backend.release_guard_if_owner_dead(resource):
                f.fixed = True
                _emit_event(
                    "stale_guard_cleared",
                    "",
                    f"resource={resource}",
                    actor="doctor",
                )
            else:
                f.message += " (owner recovered before fix)"
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
            findings.append(
                Finding(
                    severity="warning",
                    check="workspace_conflict",
                    message=(f"Workers {worker_ids} share workspace_root '{ws_root}'"),
                    auto_fixable=False,
                )
            )

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
            capture_output=True,
            text=True,
            check=True,
        )
        if status.stdout.strip():
            return False  # has uncommitted changes

        # Check for unpushed commits — requires upstream to be configured
        log = subprocess.run(
            ["git", "-C", path, "log", "@{u}..", "--oneline"],
            capture_output=True,
            text=True,
            check=False,
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

    # Derive repo root from data_dir (parent of .antfarm/)
    data_dir = config.get("data_dir", "")
    data_path = Path(data_dir)
    repo_path = str(data_path.parent) if data_path.name == ".antfarm" else str(data_path)

    # Worktree dirs are any subdirectories under workspace_root
    for entry in ws_path.iterdir():
        if entry.is_dir():
            if fix and _worktree_is_clean(str(entry)):
                # Safe to delete — provably clean
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", str(entry)],
                        capture_output=True,
                        text=True,
                        check=True,
                        cwd=repo_path,
                    )
                    findings.append(
                        Finding(
                            severity="info",
                            check="orphan_workspace",
                            message=f"Orphan worktree auto-deleted (clean): {entry}",
                            auto_fixable=True,
                            fixed=True,
                        )
                    )
                except subprocess.CalledProcessError:
                    findings.append(
                        Finding(
                            severity="info",
                            check="orphan_workspace",
                            message=(
                                f"Worktree directory found with no associated active task: "
                                f"{entry} (removal failed)"
                            ),
                            auto_fixable=False,
                        )
                    )
            elif fix:
                findings.append(
                    Finding(
                        severity="info",
                        check="orphan_workspace",
                        message=(
                            f"Orphan worktree kept: has changes or could not verify "
                            f"clean state: {entry}"
                        ),
                        auto_fixable=True,
                        fixed=False,
                    )
                )
            else:
                findings.append(
                    Finding(
                        severity="info",
                        check="orphan_workspace",
                        message=(
                            f"Worktree directory found with no associated active task: {entry}"
                        ),
                        auto_fixable=True,
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Check 8b: Stale worktree pruning (#352)
# ---------------------------------------------------------------------------


def _parse_worktree_dir_name(name: str) -> tuple[str | None, str | None]:
    """Parse a worktree directory name shaped as ``{task_id}-{attempt_id}``
    where the attempt_id is a UUID4. Returns (task_id, attempt_id) or
    (None, None) when the name does not match."""
    m = _UUID4_RE.search(name)
    if not m:
        return None, None
    attempt_id = m.group(1)
    # task_id is everything before the attempt_id minus the joining dash.
    head = name[: -len(attempt_id)]
    if not head.endswith("-"):
        return None, None
    task_id = head[:-1]
    if not task_id:
        return None, None
    return task_id, attempt_id


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    """Best-effort ISO-8601 parse. Returns None when value is missing,
    malformed, or otherwise unparseable. Never raises."""
    if not value:
        return None
    try:
        # fromisoformat accepts offset-aware timestamps on 3.11+. Normalize
        # to UTC-aware for arithmetic.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _enumerate_antfarm_worktrees(repo_path: str) -> list[str]:
    """Return absolute paths of worktrees under ``.antfarm/workspaces/`` as
    reported by ``git worktree list --porcelain`` run in ``repo_path``.

    Silently returns an empty list on any git failure — doctor is diagnostic
    and must not raise.
    """
    try:
        r = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []

    antfarm_prefix = os.path.realpath(os.path.join(repo_path, ".antfarm", "workspaces"))
    results: list[str] = []
    for line in r.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = line[len("worktree ") :].strip()
        if not path:
            continue
        real = os.path.realpath(path)
        if real.startswith(antfarm_prefix + os.sep):
            results.append(real)
    return results


def check_stale_worktrees(
    backend,
    config: dict,
    fix: bool = False,
    keep_worktrees: list[str] | None = None,
) -> list[Finding]:
    """Prune stale worktrees under ``.antfarm/workspaces/`` (#352).

    A worktree is a pruning candidate when ANY of these rules match:
      1. merged cool-down — task.status=done AND attempt.status=merged AND
         age >= ``worktree_prune_merged_min_age_hours`` (default 24). Age is
         sourced from ``attempt.completed_at``, falling back to dir mtime.
      2. superseded — attempt.status=superseded (no age gate).
      3. orphan — backend.get_task(task_id) returns None (no age gate).
      4. TTL catchall — dir mtime age > ``worktree_prune_ttl_days``
         (default 7). Applies regardless of task/attempt state and covers
         directories whose names do not parse as ``{task}-{uuid4}``.

    ``fix=True`` runs ``git worktree remove --force`` on each candidate after
    a safety re-check that the realpath lies strictly under
    ``.antfarm/workspaces/``. Paths in ``keep_worktrees`` (realpath-compared)
    are skipped. A ``worktree_pruned`` SSE event is emitted per success with
    actor="doctor".

    Args:
        backend: TaskBackend instance.
        config: Doctor config dict. Reads ``data_dir``, ``repo_path``,
            ``worktree_prune_ttl_days``, ``worktree_prune_merged_min_age_hours``.
        fix: If True, actually remove candidate worktrees.
        keep_worktrees: Optional list of absolute paths never to prune.

    Returns:
        List of findings — one per candidate, whether or not it was fixed.
    """
    findings: list[Finding] = []
    repo_path = config.get("repo_path", ".")
    ws_root = os.path.realpath(os.path.join(repo_path, ".antfarm", "workspaces"))
    if not os.path.isdir(ws_root):
        return findings

    ttl_days = int(config.get("worktree_prune_ttl_days", 7))
    merged_min_age_hours = int(config.get("worktree_prune_merged_min_age_hours", 24))
    ttl_seconds = ttl_days * 86400
    merged_min_age_seconds = merged_min_age_hours * 3600

    keep_set: set[str] = set()
    for k in keep_worktrees or []:
        try:
            keep_set.add(os.path.realpath(k))
        except (OSError, ValueError):
            # Unresolvable path — ignore; doctor is best-effort.
            continue

    now = datetime.now(UTC)
    paths = _enumerate_antfarm_worktrees(repo_path)

    for path in paths:
        dir_name = os.path.basename(path.rstrip(os.sep))
        task_id, attempt_id = _parse_worktree_dir_name(dir_name)

        try:
            dir_mtime = os.path.getmtime(path)
        except OSError:
            continue
        dir_age_seconds = now.timestamp() - dir_mtime

        rule: str | None = None
        task: dict | None = None
        attempt: dict | None = None

        if task_id and attempt_id:
            try:
                task = backend.get_task(task_id)
            except Exception:
                task = None

            if task is None:
                # Rule 3: orphan (no task matches this worktree).
                rule = "orphan"
            else:
                attempts = task.get("attempts", []) or []
                for a in attempts:
                    if a.get("attempt_id") == attempt_id:
                        attempt = a
                        break

                if attempt is not None:
                    a_status = attempt.get("status")
                    if a_status == "superseded":
                        rule = "superseded"
                    elif task.get("status") == "done" and a_status == "merged":
                        completed = _parse_iso_timestamp(attempt.get("completed_at"))
                        if completed is not None:
                            age_seconds = (now - completed).total_seconds()
                        else:
                            age_seconds = dir_age_seconds
                        if age_seconds >= merged_min_age_seconds:
                            rule = "merged"

        # Rule 4: TTL catchall — applies regardless of task/attempt state
        # (including unparseable names that never set a rule above).
        if rule is None and dir_age_seconds > ttl_seconds:
            rule = "ttl"

        if rule is None:
            continue

        message = f"stale worktree eligible for pruning: {path} (reason={rule})"
        hint = (
            "Run `antfarm doctor --fix` to remove it, or "
            f"`antfarm doctor --fix --keep-worktree {path}` to exempt it."
        )

        if not fix:
            findings.append(
                Finding(
                    severity="info",
                    check="stale_worktree",
                    message=f"{message}\n  hint: {hint}",
                    auto_fixable=True,
                    fixed=False,
                )
            )
            continue

        # Safety re-check BEFORE any git invocation.
        real_path = os.path.realpath(path)
        if not real_path.startswith(ws_root + os.sep):
            logger.warning(
                "doctor check_stale_worktrees: refusing to prune path outside "
                ".antfarm/workspaces/: %r (resolved %r, prefix %r)",
                path,
                real_path,
                ws_root,
            )
            findings.append(
                Finding(
                    severity="warning",
                    check="stale_worktree",
                    message=(
                        f"refused to prune worktree outside .antfarm/workspaces/: "
                        f"{path} (resolved {real_path})"
                    ),
                    auto_fixable=False,
                    fixed=False,
                )
            )
            continue

        if real_path in keep_set:
            findings.append(
                Finding(
                    severity="info",
                    check="stale_worktree",
                    message=f"kept by --keep-worktree: {path} (reason={rule})",
                    auto_fixable=True,
                    fixed=False,
                )
            )
            continue

        # Detach HEAD before removing so git doesn't balk on a checked-out
        # branch. Best-effort; we care about the subsequent worktree remove.
        subprocess.run(
            ["git", "-C", real_path, "checkout", "--detach"],
            check=False,
            capture_output=True,
        )
        rm = subprocess.run(
            ["git", "worktree", "remove", "--force", real_path],
            cwd=repo_path,
            check=False,
            capture_output=True,
        )
        if rm.returncode == 0:
            detail = f"path={real_path} reason={rule}"
            logger.info("doctor pruned stale worktree: %s", detail)
            with contextlib.suppress(Exception):
                _emit_event(
                    "worktree_pruned",
                    task_id="",
                    detail=detail,
                    actor="doctor",
                )
            findings.append(
                Finding(
                    severity="info",
                    check="stale_worktree",
                    message=f"{message}\n  hint: {hint}",
                    auto_fixable=True,
                    fixed=True,
                )
            )
        else:
            err = rm.stderr.decode(errors="replace").strip() if rm.stderr else ""
            findings.append(
                Finding(
                    severity="warning",
                    check="stale_worktree",
                    message=(
                        f"failed to prune stale worktree: {path} (reason={rule}) stderr={err}"
                    ),
                    auto_fixable=True,
                    fixed=False,
                )
            )

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
                findings.append(
                    Finding(
                        severity="error",
                        check="state_consistency",
                        message=f"Malformed JSON in {task_file.name}: {exc}",
                        auto_fixable=False,
                    )
                )
                continue

            status = data.get("status", "")

            # (a) status field doesn't match folder
            if status != folder_name:
                findings.append(
                    Finding(
                        severity="error",
                        check="state_consistency",
                        message=(f"Task '{task_id}' is in {folder_name}/ but status='{status}'"),
                        auto_fixable=False,
                    )
                )

            # (b) task in active/ with no current_attempt
            if folder_name == "active" and not data.get("current_attempt"):
                findings.append(
                    Finding(
                        severity="error",
                        check="state_consistency",
                        message=f"Task '{task_id}' is in active/ but has no current_attempt",
                        auto_fixable=False,
                    )
                )

            # (c) more than one ACTIVE attempt
            active_attempts = [a for a in data.get("attempts", []) if a.get("status") == "active"]
            if len(active_attempts) > 1:
                findings.append(
                    Finding(
                        severity="error",
                        check="state_consistency",
                        message=(
                            f"Task '{task_id}' has {len(active_attempts)} active attempts "
                            f"(should be at most 1)"
                        ),
                        auto_fixable=False,
                    )
                )

            # (e) done task with current_attempt pointing to non-existent attempt
            current = data.get("current_attempt")
            if current and folder_name == "done":
                attempt_ids = {a.get("attempt_id") for a in data.get("attempts", [])}
                if current not in attempt_ids:
                    findings.append(
                        Finding(
                            severity="error",
                            check="state_consistency",
                            message=(
                                f"Task '{task_id}' in done/ has current_attempt='{current}' "
                                f"but no matching attempt exists"
                            ),
                            auto_fixable=False,
                        )
                    )

            # (f) merged attempt is current_attempt while task status is ready
            if current and folder_name == "ready":
                for attempt in data.get("attempts", []):
                    if attempt.get("attempt_id") == current and attempt.get("status") == "merged":
                        findings.append(
                            Finding(
                                severity="error",
                                check="state_consistency",
                                message=(
                                    f"Task '{task_id}' in ready/ has a merged attempt "
                                    f"as current_attempt — invalid state"
                                ),
                                auto_fixable=False,
                            )
                        )

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
        findings.append(
            Finding(
                severity="error",
                check="dependency_cycles",
                message=f"Could not load tasks for dependency check: {exc}",
                auto_fixable=False,
            )
        )
        return findings

    task_ids = {t["id"] for t in all_tasks}
    deps: dict[str, list[str]] = {t["id"]: list(t.get("depends_on", [])) for t in all_tasks}

    # Detect dangling references
    for task_id, task_deps in deps.items():
        for dep in task_deps:
            if dep not in task_ids:
                findings.append(
                    Finding(
                        severity="warning",
                        check="dangling_dependency",
                        message=f"Task '{task_id}' depends on non-existent task '{dep}'",
                        auto_fixable=False,
                    )
                )

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
                    findings.append(
                        Finding(
                            severity="error",
                            check="dependency_cycles",
                            message=(
                                f"Dependency cycle detected: "
                                f"{' -> '.join(cycle_nodes)} -> {neighbor}"
                            ),
                            auto_fixable=False,
                        )
                    )
            elif color[neighbor] == WHITE:
                dfs(neighbor, path)
        path.pop()
        color[node] = BLACK

    for tid in task_ids:
        if color[tid] == WHITE:
            dfs(tid, [])

    return findings


# ---------------------------------------------------------------------------
# Check 11: Runner health
# ---------------------------------------------------------------------------


def check_runner_health(backend, config: dict, fix: bool = False) -> list[Finding]:
    """Check reachability of all nodes with runner_url.

    For each node with a runner_url, GET {runner_url}/health with 3s timeout.
    Report unreachable runners as warnings.

    Args:
        backend: TaskBackend instance.
        config: Doctor config dict.
        fix: Unused (runner health is not auto-fixable).

    Returns:
        List of findings.
    """
    findings: list[Finding] = []

    if not hasattr(backend, "list_nodes"):
        return findings

    try:
        nodes = backend.list_nodes()
    except Exception:
        return findings

    for node in nodes:
        runner_url = node.get("runner_url")
        if not runner_url:
            continue

        node_id = node.get("node_id", "unknown")
        try:
            import urllib.request

            url = runner_url.rstrip("/") + "/health"
            with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310
                if resp.status == 200:
                    continue
                findings.append(
                    Finding(
                        severity="warning",
                        check="runner_health",
                        message=(
                            f"Runner on node '{node_id}' returned HTTP {resp.status} from {url}"
                        ),
                        auto_fixable=False,
                    )
                )
        except Exception as exc:
            findings.append(
                Finding(
                    severity="warning",
                    check="runner_health",
                    message=(f"Runner on node '{node_id}' unreachable at {runner_url}: {exc}"),
                    auto_fixable=False,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Check 12: tmux availability
# ---------------------------------------------------------------------------


def check_tmux_available(config: dict) -> list[Finding]:
    """Warn if tmux is not installed — workers will fall back to unreliable subprocess mode.

    Args:
        config: Doctor config dict (unused, kept for consistent signature).

    Returns:
        List of findings.
    """
    if shutil.which("tmux"):
        return []
    return [
        Finding(
            severity="warning",
            check="tmux_available",
            message=(
                "tmux not installed — worker spawning will use subprocess fallback (less reliable)"
            ),
            auto_fixable=False,
        )
    ]


# ---------------------------------------------------------------------------
# Check 13: Orphan tmux sessions
# ---------------------------------------------------------------------------


def check_orphan_tmux_sessions(config: dict, fix: bool = False) -> list[Finding]:
    """Detect tmux sessions owned by THIS colony that have no matching metadata.

    Session names carry an 8-char SHA-256 hash of the colony's persisted
    UUID identity (see :func:`antfarm.core.process_manager.colony_session_hash`),
    so this check considers only sessions matching one of THIS colony's
    prefixes:

    - ``auto-{hash}-`` — autoscaler-spawned workers
    - ``runner-{hash}-`` — Runner-spawned workers

    Sessions owned by peer colonies (different ``data_dir`` on the same host)
    use a different hash and are ignored — they are not our problem.

    An orphan is a session matching one of our prefixes but without a
    corresponding ``ProcessMetadata`` file under
    ``{data_dir}/processes/{name}.json``. This can happen when the colony
    crashed before writing metadata, or after a manual ``tmux kill-server``
    that left state files behind.

    Severity is ``warning`` with ``auto_fixable=True``: prefix filtering
    guarantees ownership, so ``--fix`` can safely ``tmux kill-session`` the
    orphan without risking a peer colony's workers.

    Args:
        config: Doctor config dict. Uses ``data_dir`` to derive the colony
            hash and locate process metadata.
        fix: If True, kill each orphan via ``tmux kill-session`` and mark
            the finding as ``fixed=True``.

    Returns:
        List of findings for this colony's orphans.
    """
    if not shutil.which("tmux"):
        return []

    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        # tmux binary unreachable or hung — treat as no sessions.
        # subprocess.TimeoutExpired is a subclass of SubprocessError but is
        # listed explicitly to match the issue's intent and clarify coverage.
        return []
    if result.returncode != 0:
        # tmux server not running — no sessions to check
        return []

    from antfarm.core.process_manager import colony_session_hash, parse_session_name

    data_dir = config.get("data_dir", "")
    if not data_dir:
        return []

    h = colony_session_hash(data_dir)
    own_prefixes = (f"auto-{h}-", f"runner-{h}-")
    processes_dir = Path(data_dir) / "processes"

    findings: list[Finding] = []
    for name in result.stdout.strip().splitlines():
        name = name.strip()
        if not name:
            continue

        # Only consider sessions owned by THIS colony (hash match).
        # Peer-colony sessions use a different hash and are skipped entirely.
        is_ours = any(parse_session_name(name, prefix) is not None for prefix in own_prefixes)
        if not is_ours:
            continue

        # Skip sessions with matching metadata — those are tracked, not orphans.
        if (processes_dir / f"{name}.json").exists():
            continue

        finding = Finding(
            severity="warning",
            check="orphan_tmux_session",
            message=f"orphan tmux session: {name} (no matching metadata)",
            auto_fixable=True,
        )

        if fix:
            # Session name is already colony-hash-scoped (see #231), so we never
            # kill a peer colony's session — this is what makes the race-tolerant
            # behavior safe to default-on.
            try:
                kill = subprocess.run(
                    ["tmux", "kill-session", "-t", name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={**os.environ, "LC_ALL": "C"},
                )
            except subprocess.TimeoutExpired:
                # tmux kill-session hung — leave finding unfixed with explicit note.
                finding.fixed = False
                finding.message += " — kill timed out"
                findings.append(finding)
                continue
            if kill.returncode == 0:
                finding.fixed = True
            else:
                # Race: session disappeared between list-sessions and kill-session.
                # tmux emits these strings from cmd-kill-session.c / server.c when
                # the target is gone or the server has exited. Treat as success.
                # Note: "session not found" is defensive coverage — we've observed
                # "can't find session" and "no server running" in tmux 3.x;
                # "session not found" is added for other tmux versions/ports.
                stderr_lower = (kill.stderr or "").strip().lower()
                gone_markers = (
                    "can't find session",
                    "session not found",
                    "no server running",
                )
                if any(m in stderr_lower for m in gone_markers):
                    finding.fixed = True
                    finding.message += " (already gone)"
                else:
                    raw = (
                        kill.stderr.strip().splitlines()[0]
                        if kill.stderr and kill.stderr.strip()
                        else f"returncode={kill.returncode}"
                    )
                    detail = raw if len(raw) <= 200 else raw[:199] + "…"
                    finding.message += f" — kill failed: {detail}"
                    finding.fixed = False

        findings.append(finding)

    return findings


# ---------------------------------------------------------------------------
# Migration: sweep legacy (pre-hash) tmux sessions
# ---------------------------------------------------------------------------


_BENIGN_KILL_MARKERS = (
    "can't find session",
    "session not found",
    "no server running",
)


def sweep_legacy_tmux_sessions(
    config: dict,
    confirmed: bool = False,
) -> list[Finding]:
    """List or kill pre-hash tmux session names host-wide.

    Matches sessions whose names follow the pre-upgrade layout introduced
    before #231 (autoscaler/runner) and #235 (deploy) — i.e., names lacking
    an 8-char colony hash token:

    - ``auto-<role>-<N>``
    - ``runner-<role>-<N>``
    - ``antfarm-<node>-<agent>-<idx>``

    When ``confirmed=False``, returns one ``info`` finding per match so callers
    can preview the sweep. When ``confirmed=True``, attempts ``tmux kill-session``
    per match using the same benign-race stderr tolerance as
    :func:`check_orphan_tmux_sessions`.

    This is a **host-wide** operation; it is not scoped to a single colony.
    Run it only after confirming there is no peer colony still using the
    legacy format.

    False-positive risk: the legacy pattern matches any session named
    ``auto-<word>-<int>`` (and ``runner-<word>-<int>``, ``antfarm-...-<int>``)
    where ``<word>`` is not an 8-char hex token. A user-owned tmux session
    such as ``auto-save-5`` will match by design. Safety relies on the
    interactive gate in the CLI (``--yes`` confirmation or operator prompt)
    rather than on pattern precision; never call this function unattended.

    Args:
        config: Doctor config dict (unused; signature mirrors other checks).
        confirmed: When True, kill each match. Default False (dry-run preview).

    Returns:
        List of findings, one per matched legacy session.
    """
    del config  # unused — sweep is host-wide
    if not shutil.which("tmux"):
        return []

    env = {**os.environ, "LC_ALL": "C"}
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        # tmux binary unreachable or hung — nothing to sweep.
        return []
    if result.returncode != 0:
        # tmux server not running — nothing to sweep
        return []

    findings: list[Finding] = []
    for raw in result.stdout.splitlines():
        name = raw.strip()
        if not name or not LEGACY_TMUX_RE.match(name):
            continue

        finding = Finding(
            severity="info",
            check="legacy_tmux_session",
            message=f"Legacy session: {name}",
            auto_fixable=True,
        )

        if confirmed:
            kill = subprocess.run(
                ["tmux", "kill-session", "-t", name],
                capture_output=True,
                text=True,
                env=env,
            )
            if kill.returncode == 0:
                finding.fixed = True
            else:
                stderr_lower = (kill.stderr or "").strip().lower()
                if any(m in stderr_lower for m in _BENIGN_KILL_MARKERS):
                    finding.fixed = True
                    finding.message += " (already gone)"
                else:
                    detail = (
                        kill.stderr.strip().splitlines()[0]
                        if kill.stderr and kill.stderr.strip()
                        else f"returncode={kill.returncode}"
                    )
                    finding.message += f" — kill failed: {detail}"
                    finding.fixed = False

        findings.append(finding)

    return findings
