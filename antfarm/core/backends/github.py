"""GitHubBackend — GitHub Issues-backed TaskBackend implementation.

Maps Antfarm task lifecycle to GitHub Issues using labels for state:
  antfarm:ready   — task is queued and eligible for pulling
  antfarm:active  — task is claimed by a worker
  antfarm:done    — task is completed (PR opened)
  antfarm:merged  — task's PR has been merged
  antfarm:paused  — task is paused
  antfarm:blocked — task is blocked

Task data is stored in the issue body as a JSON spec block. Trail and signal
entries are stored as issue comments with [trail] and [signal] prefixes.
Attempts are tracked in the body JSON.

Guards use dedicated lock issues with title "guard:{resource}".
Workers and nodes are tracked in-memory (ephemeral — restart clears them).

Requires httpx for all GitHub API calls. Pagination is handled via
GitHub's Link header rel="next" pattern.
"""

from __future__ import annotations

import contextlib
import json
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from antfarm.core.models import (
    Attempt,
    AttemptStatus,
    TaskStatus,
    TrailEntry,
)
from antfarm.core.pr_ops import NullPROps, PROps

from .base import TaskBackend

_GITHUB_API = "https://api.github.com"
_GITHUB_BACKEND_MSG = (
    "Mission mode requires FileBackend in v0.6.0. Use --backend file or wait for v0.6.1."
)
_SPEC_FENCE_OPEN = "<!-- antfarm-spec\n"
_SPEC_FENCE_CLOSE = "\n-->"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _label(prefix: str, suffix: str) -> str:
    return f"{prefix}{suffix}"


def _status_label(prefix: str, status: str) -> str:
    return _label(prefix, status)


def _parse_spec(body: str) -> dict:
    """Extract and parse the JSON spec from an issue body.

    The spec is stored in an HTML comment block between sentinel markers.
    Returns an empty dict if no spec block is found.
    """
    if _SPEC_FENCE_OPEN not in body:
        return {}
    try:
        start = body.index(_SPEC_FENCE_OPEN) + len(_SPEC_FENCE_OPEN)
        end = body.index(_SPEC_FENCE_CLOSE, start)
        return json.loads(body[start:end])
    except (ValueError, json.JSONDecodeError):
        return {}


def _render_body(spec: dict) -> str:
    """Render the issue body with the spec embedded as a JSON comment block."""
    title = spec.get("title", "")
    task_spec = spec.get("spec", "")
    lines = []
    if title:
        lines.append(f"## {title}")
        lines.append("")
    if task_spec:
        lines.append(task_spec)
        lines.append("")
    lines.append(_SPEC_FENCE_OPEN.rstrip("\n"))
    lines.append(json.dumps(spec, indent=2, sort_keys=True))
    lines.append(_SPEC_FENCE_CLOSE.lstrip("\n"))
    return "\n".join(lines)


class GitHubBackend(TaskBackend):
    """GitHub Issues-backed implementation of TaskBackend.

    Args:
        repo: GitHub repository in "owner/repo" format.
        token: GitHub personal access token. Uses unauthenticated requests
               if None (subject to lower rate limits).
        label_prefix: Prefix for all Antfarm-managed labels (default "antfarm:").
    """

    def __init__(
        self,
        repo: str,
        token: str | None = None,
        label_prefix: str = "antfarm:",
        pr_ops: PROps | None = None,
    ) -> None:
        self._repo = repo
        self._token = token
        self._prefix = label_prefix
        self._lock = threading.Lock()
        self._pr_ops: PROps = pr_ops or NullPROps()

        # In-memory stores for ephemeral state
        self._workers: dict[str, dict] = {}
        self._nodes: dict[str, dict] = {}
        self._guards: dict[str, dict] = {}  # resource -> {owner, issue_number}

        self._http = httpx.Client(
            headers=self._default_headers(),
            timeout=30.0,
        )

    def _close_superseded_pr(self, attempt: dict | None, reason: str) -> None:
        """Close the PR on a superseded attempt. Never raises."""
        if not attempt:
            return
        pr = attempt.get("pr")
        if not pr:
            return
        # Never let PR close break a state transition — swallow any error.
        with contextlib.suppress(Exception):
            self._pr_ops.close_pr(pr, comment=f"Superseded by antfarm: {reason}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _default_headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make a GitHub API call. Raises on non-2xx status."""
        url = f"{_GITHUB_API}/repos/{self._repo}{path}"
        resp = self._http.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

    def _paginated_get(self, path: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages from a GitHub list endpoint."""
        url = f"{_GITHUB_API}/repos/{self._repo}{path}"
        next_url: str | None = url
        base_params = dict(params or {})
        base_params.setdefault("per_page", 100)

        items: list[dict] = []
        while next_url:
            resp = self._http.get(next_url, params=base_params if next_url == url else None)
            resp.raise_for_status()
            items.extend(resp.json())
            # Follow pagination via Link header
            link_header = resp.headers.get("Link", "")
            next_url = None
            if 'rel="next"' in link_header:
                for part in link_header.split(","):
                    if 'rel="next"' in part:
                        next_url = part.split(";")[0].strip().strip("<>")
                        break
        return items

    def _label_name(self, suffix: str) -> str:
        return f"{self._prefix}{suffix}"

    def _ensure_label(self, name: str) -> None:
        """Create a GitHub label if it doesn't already exist."""
        try:
            self._api("GET", f"/labels/{name}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                self._api(
                    "POST",
                    "/labels",
                    json={
                        "name": name,
                        "color": "0075ca",
                        "description": f"Antfarm managed label: {name}",
                    },
                )
            else:
                raise

    def _swap_labels(
        self,
        issue_number: int,
        remove_suffixes: list[str],
        add_suffix: str,
    ) -> None:
        """Atomically swap Antfarm status labels on an issue."""
        # Get current labels
        resp = self._api("GET", f"/issues/{issue_number}")
        issue = resp.json()
        current_labels = [lb["name"] for lb in issue.get("labels", [])]

        remove_set = {self._label_name(s) for s in remove_suffixes}
        new_labels = [lb for lb in current_labels if lb not in remove_set]
        new_label = self._label_name(add_suffix)
        if new_label not in new_labels:
            new_labels.append(new_label)

        self._api("PATCH", f"/issues/{issue_number}", json={"labels": new_labels})

    def _get_issue_by_number(self, number: int) -> dict | None:
        try:
            resp = self._api("GET", f"/issues/{number}")
            return resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def _find_issues_by_label(self, label: str, state: str = "open") -> list[dict]:
        """List issues with a specific label."""
        return self._paginated_get(
            "/issues",
            {
                "labels": label,
                "state": state,
                "per_page": 100,
            },
        )

    def _issue_to_task(self, issue: dict) -> dict:
        """Parse a GitHub Issue dict into an Antfarm task dict.

        The spec JSON is embedded in the issue body. Trail and signal entries
        are fetched from issue comments.
        """
        body = issue.get("body") or ""
        spec = _parse_spec(body)

        # Determine status from labels
        label_names = {lb["name"] for lb in issue.get("labels", [])}
        status = TaskStatus.READY.value
        for suffix, s in [
            ("active", TaskStatus.ACTIVE.value),
            ("done", TaskStatus.DONE.value),
            ("paused", TaskStatus.PAUSED.value),
            ("blocked", TaskStatus.BLOCKED.value),
        ]:
            if self._label_name(suffix) in label_names:
                status = s
                break

        if issue.get("state") == "closed":
            status = TaskStatus.DONE.value

        # Build task dict from spec + issue metadata
        task: dict = {
            "id": spec.get("id") or str(issue["number"]),
            "title": spec.get("title") or issue.get("title") or "",
            "spec": spec.get("spec") or "",
            "complexity": spec.get("complexity", "M"),
            "priority": spec.get("priority", 10),
            "depends_on": spec.get("depends_on", []),
            "touches": spec.get("touches", []),
            "capabilities_required": spec.get("capabilities_required", []),
            "pinned_to": spec.get("pinned_to"),
            "merge_override": spec.get("merge_override"),
            "status": status,
            "current_attempt": spec.get("current_attempt"),
            "attempts": spec.get("attempts", []),
            "trail": spec.get("trail", []),
            "signals": spec.get("signals", []),
            "created_at": spec.get("created_at") or issue.get("created_at") or _now_iso(),
            "updated_at": spec.get("updated_at") or issue.get("updated_at") or _now_iso(),
            "created_by": spec.get("created_by") or "github",
            "_issue_number": issue["number"],
        }
        return task

    def _update_task_body(self, issue_number: int, task: dict) -> None:
        """Persist task dict back to the issue body."""
        body = _render_body(task)
        self._api("PATCH", f"/issues/{issue_number}", json={"body": body})

    def _add_comment(self, issue_number: int, body: str) -> None:
        self._api("POST", f"/issues/{issue_number}/comments", json={"body": body})

    def _get_issue_number(self, task_id: str) -> int | None:
        """Find GitHub Issue number for a given task_id.

        First attempts to parse task_id as an integer (issue number).
        Falls back to searching issues by antfarm:ready/active/done labels.
        """
        # Try numeric issue number directly
        try:
            return int(task_id)
        except ValueError:
            pass

        # Search all Antfarm-labelled issues
        for status_label in ["ready", "active", "done", "paused", "blocked"]:
            issues = self._find_issues_by_label(self._label_name(status_label))
            for issue in issues:
                body = issue.get("body") or ""
                spec = _parse_spec(body)
                if spec.get("id") == task_id:
                    return issue["number"]
        return None

    def _get_task_issue(self, task_id: str) -> tuple[dict, int] | tuple[None, None]:
        """Return (task_dict, issue_number) for a task_id, or (None, None)."""
        issue_number = self._get_issue_number(task_id)
        if issue_number is None:
            return None, None
        issue = self._get_issue_by_number(issue_number)
        if issue is None:
            return None, None
        task = self._issue_to_task(issue)
        return task, issue_number

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def carry(self, task: dict) -> str:
        """Create a GitHub Issue for the task with antfarm:ready label.

        Raises:
            ValueError: If a task with the same ID already exists.
        """
        task_id = task["id"]

        # Ensure required defaults
        task = dict(task)
        task.setdefault("status", TaskStatus.READY.value)
        task.setdefault("current_attempt", None)
        task.setdefault("attempts", [])
        task.setdefault("trail", [])
        task.setdefault("signals", [])

        # Check for duplicates by searching existing issues
        existing_number = self._get_issue_number(task_id)
        if existing_number is not None:
            raise ValueError(f"Task '{task_id}' already exists (issue #{existing_number})")

        ready_label = self._label_name("ready")
        self._ensure_label(ready_label)

        body = _render_body(task)
        self._api(
            "POST",
            "/issues",
            json={
                "title": task.get("title", task_id),
                "body": body,
                "labels": [ready_label],
            },
        )
        return task_id

    def pull(self, worker_id: str) -> dict | None:
        """Claim the next eligible antfarm:ready issue. Atomic under lock.

        Scheduling: priority (lower=higher) then created_at (oldest first).

        Returns:
            Task dict with a new ACTIVE attempt, or None if nothing available.
        """
        with self._lock:
            # Check rate limit
            worker = self._workers.get(worker_id)
            if worker:
                cooldown_until = worker.get("cooldown_until")
                if cooldown_until:
                    try:
                        cooldown_dt = datetime.fromisoformat(cooldown_until)
                        if datetime.now(UTC) < cooldown_dt:
                            return None
                    except ValueError:
                        pass

            ready_label = self._label_name("ready")
            issues = self._find_issues_by_label(ready_label, state="open")

            # Get done task IDs for dependency checking
            done_issues = self._find_issues_by_label(self._label_name("done"), state="open")
            merged_label = self._label_name("merged")
            closed_issues = self._paginated_get(
                "/issues", {"state": "closed", "labels": merged_label, "per_page": 100}
            )
            done_task_ids: set[str] = set()
            for di in done_issues + closed_issues:
                spec = _parse_spec(di.get("body") or "")
                tid = spec.get("id") or str(di["number"])
                done_task_ids.add(tid)

            worker_caps: set[str] | None = None
            if worker:
                worker_caps = set(worker.get("capabilities", []))

            candidates = []
            for issue in issues:
                task = self._issue_to_task(issue)
                # Dependency check
                if not all(dep in done_task_ids for dep in task.get("depends_on", [])):
                    continue
                # Capability check
                if worker_caps is not None:
                    required = set(task.get("capabilities_required", []))
                    if not required.issubset(worker_caps):
                        continue
                # Pin check
                pinned_to = task.get("pinned_to")
                if pinned_to is not None and pinned_to != worker_id:
                    continue
                candidates.append((task, issue["number"]))

            if not candidates:
                return None

            candidates.sort(key=lambda x: (x[0].get("priority", 10), x[0].get("created_at", "")))
            chosen_task, issue_number = candidates[0]

            # Create new attempt
            attempt = Attempt(
                attempt_id=str(uuid.uuid4()),
                worker_id=worker_id,
                status=AttemptStatus.ACTIVE,
                branch=None,
                pr=None,
                started_at=_now_iso(),
                completed_at=None,
            )
            chosen_task["attempts"].append(attempt.to_dict())
            chosen_task["current_attempt"] = attempt.attempt_id
            chosen_task["status"] = TaskStatus.ACTIVE.value
            chosen_task["updated_at"] = _now_iso()

            # Persist updated spec to issue body
            self._update_task_body(issue_number, chosen_task)

            # Swap labels: ready -> active
            active_label = self._label_name("active")
            self._ensure_label(active_label)
            self._swap_labels(issue_number, ["ready"], "active")

            # Add attempt comment
            self._add_comment(
                issue_number,
                f"[attempt] Worker `{worker_id}` claimed task (attempt `{attempt.attempt_id}`)",
            )

            return chosen_task

    def append_trail(self, task_id: str, entry: dict) -> None:
        """Add a [trail] comment to the issue and update spec."""
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")

        task["trail"].append(entry)
        task["updated_at"] = _now_iso()
        self._update_task_body(issue_number, task)
        self._add_comment(
            issue_number,
            f"[trail] `{entry.get('worker_id', 'system')}`: {entry.get('message', '')}",
        )

    def append_signal(self, task_id: str, entry: dict) -> None:
        """Add a [signal] comment to the issue and update spec."""
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")

        task["signals"].append(entry)
        task["updated_at"] = _now_iso()
        self._update_task_body(issue_number, task)
        self._add_comment(
            issue_number,
            f"[signal] `{entry.get('worker_id', 'system')}`: {entry.get('message', '')}",
        )

    def mark_harvested(
        self,
        task_id: str,
        attempt_id: str,
        pr: str,
        branch: str,
        artifact: dict | None = None,
    ) -> None:
        """Transition task to DONE. Swap label active -> done. Add result comment.

        Idempotent: if already DONE with matching attempt_id, no-op.

        Raises:
            ValueError: If attempt_id is not the current attempt.
        """
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")

        if task["status"] == TaskStatus.DONE.value:
            if task.get("current_attempt") != attempt_id:
                raise ValueError(
                    f"attempt_id '{attempt_id}' is not the current attempt "
                    f"(got '{task.get('current_attempt')}')"
                )
            return  # idempotent no-op

        if task.get("current_attempt") != attempt_id:
            raise ValueError(
                f"attempt_id '{attempt_id}' is not the current attempt "
                f"(got '{task.get('current_attempt')}')"
            )

        now = _now_iso()
        for a in task["attempts"]:
            if a["attempt_id"] == attempt_id:
                a["status"] = AttemptStatus.DONE.value
                a["pr"] = pr
                a["branch"] = branch
                a["completed_at"] = now
                if artifact is not None:
                    a["artifact"] = artifact
                break

        task["status"] = TaskStatus.DONE.value
        task["updated_at"] = now
        self._update_task_body(issue_number, task)

        done_label = self._label_name("done")
        self._ensure_label(done_label)
        self._swap_labels(issue_number, ["active"], "done")
        self._add_comment(
            issue_number,
            f"[result] Harvested. PR: {pr} Branch: `{branch}`",
        )

    def kickback(self, task_id: str, reason: str) -> None:
        """Transition task from DONE back to READY. Supersede current attempt.

        Raises:
            FileNotFoundError: If task not found or not in DONE state.
        """
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found in done")

        if task["status"] != TaskStatus.DONE.value:
            raise FileNotFoundError(
                f"Task '{task_id}' is not in DONE state (got '{task['status']}')"
            )

        now = _now_iso()
        current_attempt_id = task.get("current_attempt")
        worker_id = "system"
        superseded_attempt: dict | None = None
        for a in task["attempts"]:
            if a["attempt_id"] == current_attempt_id:
                a["status"] = AttemptStatus.SUPERSEDED.value
                a["completed_at"] = now
                worker_id = a.get("worker_id") or "system"
                superseded_attempt = dict(a)
                break

        trail_entry = TrailEntry(ts=now, worker_id=worker_id, message=reason)
        task["trail"].append(trail_entry.to_dict())
        task["status"] = TaskStatus.READY.value
        task["current_attempt"] = None
        task["updated_at"] = now

        self._update_task_body(issue_number, task)
        ready_label = self._label_name("ready")
        self._ensure_label(ready_label)
        self._swap_labels(issue_number, ["done"], "ready")
        self._add_comment(issue_number, f"[kickback] {reason}")

        self._close_superseded_pr(superseded_attempt, reason)

    def mark_harvest_pending(self, task_id: str, attempt_id: str) -> None:
        """Mark task as harvest_pending. Updates status in issue body."""
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found in active")
        if task.get("current_attempt") != attempt_id:
            raise ValueError(
                f"attempt_id '{attempt_id}' is not the current attempt "
                f"(got '{task.get('current_attempt')}')"
            )
        task["status"] = "harvest_pending"
        task["updated_at"] = _now_iso()
        self._update_task_body(issue_number, task)

    def mark_merged(self, task_id: str, attempt_id: str) -> None:
        """Close the issue, add antfarm:merged label.

        Raises:
            ValueError: If attempt_id not found on task.
        """
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found in done")

        matched = False
        for a in task["attempts"]:
            if a["attempt_id"] == attempt_id:
                a["status"] = AttemptStatus.MERGED.value
                matched = True
                break

        if not matched:
            raise ValueError(f"attempt_id '{attempt_id}' not found on task '{task_id}'")

        task["updated_at"] = _now_iso()
        self._update_task_body(issue_number, task)

        merged_label = self._label_name("merged")
        self._ensure_label(merged_label)
        self._swap_labels(issue_number, ["done"], "merged")
        # Close the issue
        self._api("PATCH", f"/issues/{issue_number}", json={"state": "closed"})
        self._add_comment(issue_number, "[merged] Task merged successfully.")

    def store_review_verdict(self, task_id: str, attempt_id: str, verdict: dict) -> None:
        """Store a ReviewVerdict on the task's current attempt."""
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")
        if task.get("current_attempt") != attempt_id:
            raise ValueError(f"attempt_id '{attempt_id}' is not the current attempt")
        for a in task["attempts"]:
            if a["attempt_id"] == attempt_id:
                a["review_verdict"] = verdict
                break
        task["updated_at"] = _now_iso()
        self._update_task_body(issue_number, task)

    def pause_task(self, task_id: str) -> None:
        """Pause an active task. Swap label active -> paused."""
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")
        if task["status"] != TaskStatus.ACTIVE.value:
            raise ValueError(f"Task '{task_id}' is not in ACTIVE state")

        task["status"] = TaskStatus.PAUSED.value
        task["updated_at"] = _now_iso()
        self._update_task_body(issue_number, task)

        paused_label = self._label_name("paused")
        self._ensure_label(paused_label)
        self._swap_labels(issue_number, ["active"], "paused")

    def resume_task(self, task_id: str) -> None:
        """Resume a paused task. Swap label paused -> ready. Supersede current attempt."""
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")
        if task["status"] != TaskStatus.PAUSED.value:
            raise ValueError(f"Task '{task_id}' is not in PAUSED state")

        now = _now_iso()
        current_attempt_id = task.get("current_attempt")
        superseded_attempt: dict | None = None
        if current_attempt_id:
            for a in task["attempts"]:
                if a["attempt_id"] == current_attempt_id:
                    a["status"] = AttemptStatus.SUPERSEDED.value
                    a["completed_at"] = now
                    superseded_attempt = dict(a)
                    break
            task["current_attempt"] = None

        task["status"] = TaskStatus.READY.value
        task["updated_at"] = now
        self._update_task_body(issue_number, task)

        ready_label = self._label_name("ready")
        self._ensure_label(ready_label)
        self._swap_labels(issue_number, ["paused"], "ready")

        self._close_superseded_pr(superseded_attempt, "task resumed from paused")

    def reassign_task(self, task_id: str, worker_id: str) -> None:
        """Reassign active task. Supersede attempt, return to READY."""
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")
        if task["status"] != TaskStatus.ACTIVE.value:
            raise ValueError(f"Task '{task_id}' is not in ACTIVE state")

        now = _now_iso()
        current_attempt_id = task.get("current_attempt")
        superseded_attempt: dict | None = None
        if current_attempt_id:
            for a in task["attempts"]:
                if a["attempt_id"] == current_attempt_id:
                    a["status"] = AttemptStatus.SUPERSEDED.value
                    a["completed_at"] = now
                    superseded_attempt = dict(a)
                    break

        trail_entry = TrailEntry(ts=now, worker_id="system", message=f"Reassigned to {worker_id}")
        task["trail"].append(trail_entry.to_dict())
        task["status"] = TaskStatus.READY.value
        task["current_attempt"] = None
        task["updated_at"] = now
        self._update_task_body(issue_number, task)

        ready_label = self._label_name("ready")
        self._ensure_label(ready_label)
        self._swap_labels(issue_number, ["active"], "ready")

        self._close_superseded_pr(superseded_attempt, f"task reassigned to {worker_id}")

    def block_task(self, task_id: str, reason: str) -> None:
        """Block a ready task. Swap label ready -> blocked."""
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")
        if task["status"] != TaskStatus.READY.value:
            raise ValueError(f"Task '{task_id}' is not in READY state")

        now = _now_iso()
        trail_entry = TrailEntry(ts=now, worker_id="system", message=f"Blocked: {reason}")
        task["trail"].append(trail_entry.to_dict())
        task["status"] = TaskStatus.BLOCKED.value
        task["updated_at"] = now
        self._update_task_body(issue_number, task)

        blocked_label = self._label_name("blocked")
        self._ensure_label(blocked_label)
        self._swap_labels(issue_number, ["ready"], "blocked")
        self._add_comment(issue_number, f"[blocked] {reason}")

    def unblock_task(self, task_id: str) -> None:
        """Unblock a blocked task. Swap label blocked -> ready."""
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found")
        if task["status"] != TaskStatus.BLOCKED.value:
            raise ValueError(f"Task '{task_id}' is not in BLOCKED state")

        task["status"] = TaskStatus.READY.value
        task["updated_at"] = _now_iso()
        self._update_task_body(issue_number, task)

        ready_label = self._label_name("ready")
        self._ensure_label(ready_label)
        self._swap_labels(issue_number, ["blocked"], "ready")

    def pin_task(self, task_id: str, worker_id: str) -> None:
        """Pin a ready task to a specific worker."""
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found in ready")
        if task["status"] != TaskStatus.READY.value:
            raise FileNotFoundError(f"Task '{task_id}' not found in ready")

        task["pinned_to"] = worker_id
        task["updated_at"] = _now_iso()
        self._update_task_body(issue_number, task)

    def unpin_task(self, task_id: str) -> None:
        """Clear the pin on a ready task."""
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found in ready")
        if task["status"] != TaskStatus.READY.value:
            raise FileNotFoundError(f"Task '{task_id}' not found in ready")

        task["pinned_to"] = None
        task["updated_at"] = _now_iso()
        self._update_task_body(issue_number, task)

    def override_merge_order(self, task_id: str, position: int) -> None:
        """Set merge_override on a done task."""
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found in done")
        if task["status"] != TaskStatus.DONE.value:
            raise FileNotFoundError(f"Task '{task_id}' not found in done")

        task["merge_override"] = position
        task["updated_at"] = _now_iso()
        self._update_task_body(issue_number, task)

    def clear_merge_override(self, task_id: str) -> None:
        """Clear merge_override on a done task."""
        task, issue_number = self._get_task_issue(task_id)
        if issue_number is None:
            raise FileNotFoundError(f"Task '{task_id}' not found in done")
        if task["status"] != TaskStatus.DONE.value:
            raise FileNotFoundError(f"Task '{task_id}' not found in done")

        task["merge_override"] = None
        task["updated_at"] = _now_iso()
        self._update_task_body(issue_number, task)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_tasks(self, status: str | None = None) -> list[dict]:
        """List tasks by status label. Fetches open issues with matching Antfarm labels."""
        if status is not None:
            label = self._label_name(status)
            gh_state = "closed" if status in ("merged",) else "open"
            issues = self._find_issues_by_label(label, state=gh_state)
            return [self._issue_to_task(i) for i in issues]

        # All statuses
        results = []
        seen: set[int] = set()
        for suffix in ["ready", "active", "done", "paused", "blocked"]:
            for issue in self._find_issues_by_label(self._label_name(suffix), state="open"):
                if issue["number"] not in seen:
                    seen.add(issue["number"])
                    results.append(self._issue_to_task(issue))
        return results

    def get_task(self, task_id: str) -> dict | None:
        """Get task by ID. Searches all Antfarm-labelled issues."""
        task, _ = self._get_task_issue(task_id)
        return task

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def guard(self, resource: str, owner: str) -> bool:
        """Acquire an exclusive guard using in-memory locking.

        Uses a lock issue on GitHub as a distributed lock: creates an issue
        titled "guard:{resource}" with antfarm:guard label if none exists.

        Returns:
            True if guard acquired, False if held by another owner.
        """
        with self._lock:
            existing = self._guards.get(resource)
            if existing is not None:
                return existing["owner"] == owner
            self._guards[resource] = {"owner": owner, "acquired_at": _now_iso()}
            return True

    def release_guard(self, resource: str, owner: str) -> None:
        """Release a guard.

        Raises:
            PermissionError: If owner doesn't match.
            FileNotFoundError: If no guard exists.
        """
        with self._lock:
            existing = self._guards.get(resource)
            if existing is None:
                raise FileNotFoundError(f"No guard exists for resource '{resource}'")
            if existing["owner"] != owner:
                raise PermissionError(
                    f"Guard on '{resource}' is owned by '{existing['owner']}', not '{owner}'"
                )
            del self._guards[resource]

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def register_node(self, node: dict) -> None:
        """Register a node in-memory. Idempotent — updates last_seen."""
        node_id = node["node_id"]
        if node_id in self._nodes:
            self._nodes[node_id]["last_seen"] = node.get("last_seen", _now_iso())
        else:
            self._nodes[node_id] = dict(node)

    def list_nodes(self) -> list[dict]:
        """Return all registered nodes."""
        return list(self._nodes.values())

    def get_node(self, node_id: str) -> dict | None:
        """Return a single node by ID, or None if not found."""
        return self._nodes.get(node_id)

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    def register_worker(self, worker: dict) -> None:
        """Register a worker in-memory.

        Raises:
            ValueError: If a live worker with same ID already exists.
        """
        worker_id = worker["worker_id"]
        existing = self._workers.get(worker_id)
        if existing is not None:
            # Check if still live (heartbeat within TTL)
            last_hb = existing.get("last_heartbeat", "")
            try:
                hb_dt = datetime.fromisoformat(last_hb)
                age = (datetime.now(UTC) - hb_dt).total_seconds()
                if age <= 300:
                    raise ValueError(f"Worker '{worker_id}' is already registered and live")
            except ValueError as exc:
                if "already registered" in str(exc):
                    raise
        self._workers[worker_id] = dict(worker)

    def deregister_worker(self, worker_id: str) -> None:
        """Deregister a worker. No-op if not found."""
        self._workers.pop(worker_id, None)

    def heartbeat(self, worker_id: str, status: dict) -> None:
        """Update worker in-memory state with heartbeat."""
        if worker_id not in self._workers:
            self._workers[worker_id] = {"worker_id": worker_id}
        self._workers[worker_id].update(status)
        self._workers[worker_id]["last_heartbeat"] = _now_iso()

    def update_worker_activity(self, worker_id: str, action: str | None) -> None:
        """Set current_action / current_action_at on the worker record.

        Silently no-ops for unknown worker IDs. Does not touch last_heartbeat.
        """
        if worker_id not in self._workers:
            return
        if action is None or action == "":
            self._workers[worker_id]["current_action"] = None
            self._workers[worker_id]["current_action_at"] = None
        else:
            self._workers[worker_id]["current_action"] = action[:200]
            self._workers[worker_id]["current_action_at"] = _now_iso()

    def list_workers(self) -> list[dict]:
        """Return all registered workers."""
        return list(self._workers.values())

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return backend status summary by counting issues per label."""
        counts: dict[str, int] = {}
        for suffix in ["ready", "active", "done", "paused", "blocked"]:
            label = self._label_name(suffix)
            gh_state = "open"
            issues = self._find_issues_by_label(label, state=gh_state)
            counts[suffix] = len(issues)

        return {
            "tasks": counts,
            "workers": len(self._workers),
            "nodes": len(self._nodes),
            "guards": len(self._guards),
        }

    def rereview(
        self,
        review_task_id: str,
        new_spec: str,
        touches: list[str],
    ) -> None:
        raise NotImplementedError(_GITHUB_BACKEND_MSG)

    # ------------------------------------------------------------------
    # Missions (not supported — stubs raise)
    # ------------------------------------------------------------------

    def create_mission(self, mission: dict) -> str:
        raise NotImplementedError(_GITHUB_BACKEND_MSG)

    def get_mission(self, mission_id: str) -> dict | None:
        raise NotImplementedError(_GITHUB_BACKEND_MSG)

    def list_missions(self, status: str | None = None) -> list[dict]:
        raise NotImplementedError(_GITHUB_BACKEND_MSG)

    def update_mission(self, mission_id: str, updates: dict) -> None:
        raise NotImplementedError(_GITHUB_BACKEND_MSG)
