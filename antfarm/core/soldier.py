"""Soldier integration engine for Antfarm.

Deterministic merge gate: polls the colony for done tasks and merges them into
the integration branch via a temp branch. No AI, no auto-fix.

v0.5.3 additions:
- Review orchestration: Soldier creates review tasks for done tasks, waits for
  ReviewVerdict, gates merge on artifact + review + freshness.
- Review-as-task: review tasks are regular tasks that reviewer workers forage.

Policy:
- Clean merge + green tests + passing review → fast-forward and mark merged
- Any conflict, test failure, or review rejection → kickback immediately
- Dependent tasks stay ineligible until upstream is merged
- Independent tasks continue merging (queue not globally blocked)
"""

from __future__ import annotations

import logging
import subprocess
import time
from enum import StrEnum

from antfarm.core.backends.base import TaskBackend
from antfarm.core.colony_client import ColonyClient
from antfarm.core.models import ReviewVerdict
from antfarm.core.review_pack import extract_verdict_from_review_task

logger = logging.getLogger(__name__)


class MergeResult(StrEnum):
    MERGED = "merged"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class Soldier:
    """Merge queue and integration engine.

    Polls the colony for done tasks and merges them into the integration branch
    via a temporary branch. Fully deterministic — no LLM, no AI.

    Args:
        colony_url: Colony server URL (e.g., "http://localhost:7433")
        repo_path: Absolute path to the working git clone.
        integration_branch: Branch to merge into (default: "dev").
        test_command: Command to run after merge (default: ["pytest", "-x", "-q"]).
        poll_interval: Seconds to sleep when queue is empty (default: 30.0).
        client: Optional httpx.Client for dependency injection in tests.
    """

    def __init__(
        self,
        colony_url: str,
        repo_path: str,
        integration_branch: str = "main",
        test_command: list[str] | None = None,
        poll_interval: float = 30.0,
        require_review: bool = False,
        client=None,
    ):
        self.colony = ColonyClient(colony_url, client=client)
        self.repo_path = repo_path
        self.integration_branch = integration_branch
        self.test_command = test_command or ["pytest", "-x", "-q"]
        self.poll_interval = poll_interval
        self.require_review = require_review
        self.last_failure_reason = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main soldier loop. Runs indefinitely until interrupted."""
        while True:
            if self.require_review:
                self.process_done_tasks()
            queue = self.get_merge_queue()
            if not queue:
                time.sleep(self.poll_interval)
                continue
            for task in queue:
                result = self.attempt_merge(task)
                attempt_id = task["current_attempt"]
                if result == MergeResult.MERGED:
                    self.colony.mark_merged(task["id"], attempt_id)
                else:
                    self.kickback_with_cascade(task["id"], self.last_failure_reason)

    def run_once(self) -> list[tuple[str, MergeResult]]:
        """Process the merge queue once and return results.

        Returns:
            List of (task_id, MergeResult) tuples for each task processed.
        """
        if self.require_review:
            self.process_done_tasks()
        results = []
        queue = self.get_merge_queue()
        for task in queue:
            result = self.attempt_merge(task)
            attempt_id = task["current_attempt"]
            if result == MergeResult.MERGED:
                self.colony.mark_merged(task["id"], attempt_id)
            else:
                self.kickback_with_cascade(task["id"], self.last_failure_reason)
            results.append((task["id"], result))
        return results

    def process_done_tasks(self) -> list[str]:
        """Scan done tasks and create review tasks for those missing verdicts.

        Skips review tasks (id starts with "review-") and tasks that already
        have a review verdict on the current attempt.

        Returns:
            List of review task IDs that were created.
        """
        created: list[str] = []
        all_tasks = self.colony.list_tasks()

        for task in all_tasks:
            if task.get("status") != "done":
                continue
            task_id = task.get("id", "")
            if task_id.startswith("review-"):
                continue
            # Skip plan tasks — they produce tasks, not code
            caps_req = set(task.get("capabilities_required", []))
            if "plan" in caps_req:
                continue
            if self._has_merged_attempt(task):
                continue
            # Skip if already has a verdict
            if self._get_review_verdict(task) is not None:
                continue
            review_id = self.create_review_task(task)
            if review_id:
                logger.info("created review task %s for %s", review_id, task_id)
                created.append(review_id)
        return created

    def _get_done_candidates(self) -> list[dict]:
        """Get done tasks eligible for review+merge processing.

        Like get_merge_queue() but does NOT gate on review verdict,
        since run_once_with_review() handles that itself.
        """
        all_tasks = self.colony.list_tasks()

        merged_task_ids: set[str] = set()
        for t in all_tasks:
            if self._has_merged_attempt(t):
                merged_task_ids.add(t["id"])

        eligible = []
        for task in all_tasks:
            if task.get("status") != "done":
                continue
            if task.get("id", "").startswith("review-"):
                continue
            if self._has_merged_attempt(task):
                continue
            if not self._get_attempt_branch(task):
                continue
            deps = task.get("depends_on") or []
            if not all(dep in merged_task_ids for dep in deps):
                continue
            eligible.append(task)

        def _sort_key(t: dict) -> tuple:
            mo = t.get("merge_override")
            return (
                0 if mo is not None else 1,
                mo if mo is not None else 999,
                t.get("priority", 10),
                t.get("created_at", ""),
            )

        eligible.sort(key=_sort_key)
        return eligible

    def run_once_with_review(self) -> list[tuple[str, MergeResult]]:
        """Process the merge queue with review orchestration.

        For each done task:
        1. If no review task exists, create one and skip (NEEDS_REVIEW).
        2. If review task exists but not done, skip (NEEDS_REVIEW).
        3. If review verdict is "pass" + fresh SHA, proceed to merge.
        4. If review verdict is not "pass", kickback the original task.

        Returns:
            List of (task_id, MergeResult) tuples.
        """
        results: list[tuple[str, MergeResult]] = []
        queue = self._get_done_candidates()
        all_tasks = self.colony.list_tasks()

        # Build lookup of review tasks and their state
        review_tasks: dict[str, dict] = {}
        for t in all_tasks:
            tid = t.get("id", "")
            if tid.startswith("review-"):
                review_tasks[tid] = t

        for task in queue:
            task_id = task["id"]
            attempt_id = task["current_attempt"]
            review_task_id = f"review-{task_id}"

            # Check if review verdict is already stored on the attempt
            verdict_dict = self._get_review_verdict(task)
            if verdict_dict is not None:
                passed, reason = self.check_review_verdict(task)
                if passed:
                    result = self.attempt_merge(task)
                    if result == MergeResult.MERGED:
                        self.colony.mark_merged(task_id, attempt_id)
                    else:
                        self.kickback_with_cascade(task_id, self.last_failure_reason)
                    results.append((task_id, result))
                else:
                    self.kickback_with_cascade(task_id, f"review failed: {reason}")
                    results.append((task_id, MergeResult.FAILED))
                continue

            # Check if review task exists
            review_task = review_tasks.get(review_task_id)
            if review_task is None:
                # Create the review task
                created_id = self.create_review_task(task)
                if created_id:
                    logger.info("created review task %s for %s", created_id, task_id)
                else:
                    logger.warning("failed to create review task for %s", task_id)
                results.append((task_id, MergeResult.NEEDS_REVIEW))
                continue

            # Review task exists — check its status.
            review_status = review_task.get("status", "")
            if review_status == "blocked":
                # Review task exhausted its retry budget without producing a
                # parseable verdict. Kick back the *original* task with a
                # clear reason so the build can be reattempted.
                self.kickback_with_cascade(
                    task_id, "review task completed without a ReviewVerdict"
                )
                results.append((task_id, MergeResult.FAILED))
                continue
            if review_status != "done":
                # Still in progress (ready/active/kicked-back awaiting retry)
                results.append((task_id, MergeResult.NEEDS_REVIEW))
                continue

            # Review task is done — extract verdict from review task's artifact
            review_verdict = extract_verdict_from_review_task(review_task)
            if review_verdict is None:
                # Review done but no verdict — treat as failure
                self.kickback_with_cascade(
                    task_id, "review task completed without a ReviewVerdict"
                )
                results.append((task_id, MergeResult.FAILED))
                continue

            # Store verdict on the original task's attempt
            self.colony.store_review_verdict(task_id, attempt_id, review_verdict)

            # Re-check with the stored verdict
            task_updated = self.colony.get_task(task_id)
            if task_updated is None:
                results.append((task_id, MergeResult.FAILED))
                continue

            passed, reason = self.check_review_verdict(task_updated)
            if passed:
                result = self.attempt_merge(task_updated)
                if result == MergeResult.MERGED:
                    self.colony.mark_merged(task_id, attempt_id)
                else:
                    self.kickback_with_cascade(task_id, self.last_failure_reason)
                results.append((task_id, result))
            else:
                self.kickback_with_cascade(task_id, f"review failed: {reason}")
                results.append((task_id, MergeResult.FAILED))

        return results

    def get_merge_queue(self) -> list[dict]:
        """Get done tasks eligible for merging, ordered by priority then created_at.

        Filters applied:
        - Task status must be DONE
        - current_attempt must have a branch set
        - All tasks in depends_on must have at least one MERGED attempt

        Returns:
            Ordered list of task dicts eligible for merging.
        """
        all_tasks = self.colony.list_tasks()

        # Build set of task IDs whose current attempt is MERGED
        merged_task_ids: set[str] = set()
        for t in all_tasks:
            if self._has_merged_attempt(t):
                merged_task_ids.add(t["id"])

        # Filter to done tasks with a branch and satisfied deps
        # Exclude review tasks (id starts with "review-") — they are informational
        eligible = []
        for task in all_tasks:
            if task.get("status") != "done":
                continue
            if task.get("id", "").startswith("review-"):
                continue
            # Skip already-merged tasks
            if self._has_merged_attempt(task):
                continue
            if not self._get_attempt_branch(task):
                continue
            # Check all dependencies are merged
            deps = task.get("depends_on") or []
            if not all(dep in merged_task_ids for dep in deps):
                continue
            # When review is required, gate on passing + fresh verdict
            if self.require_review:
                passed, _reason = self.check_review_verdict(task)
                if not passed:
                    continue
            eligible.append(task)

        # Sort: override tasks first (by override position), then by priority/FIFO
        def _sort_key(t: dict) -> tuple:
            mo = t.get("merge_override")
            return (
                0 if mo is not None else 1,
                mo if mo is not None else 999,
                t.get("priority", 10),
                t.get("created_at", ""),
            )

        eligible.sort(key=_sort_key)
        return eligible

    def attempt_merge(self, task: dict) -> MergeResult:
        """Attempt to merge a task's branch into the integration branch.

        Steps:
        1. git fetch origin
        2. Create temp branch from origin/{integration_branch}
        3. git merge --no-ff {branch}  — conflict → FAILED
        4. Run test_command              — non-zero → FAILED
        5. git checkout {integration_branch} && git merge --ff-only antfarm/temp-merge
        6. git push origin {integration_branch}
        7. Cleanup temp branch

        Args:
            task: Task dict from the colony API.

        Returns:
            MergeResult.MERGED on success, MergeResult.FAILED on any failure.
        """
        branch = self._get_attempt_branch(task)
        if not branch:
            self.last_failure_reason = "no branch on current attempt"
            return MergeResult.FAILED

        temp_branch = "antfarm/temp-merge"
        try:
            # Fetch latest state from origin
            r = subprocess.run(
                ["git", "fetch", "origin"],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                self.last_failure_reason = f"git fetch failed: {r.stderr.decode().strip()}"
                return MergeResult.FAILED

            # Create temp branch from integration branch
            r = subprocess.run(
                [
                    "git", "checkout", "-b", temp_branch,
                    f"origin/{self.integration_branch}",
                ],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                self.last_failure_reason = (
                    f"could not create temp branch: {r.stderr.decode().strip()}"
                )
                return MergeResult.FAILED

            # Merge task branch (no-ff to preserve history)
            r = subprocess.run(
                ["git", "merge", "--no-ff", branch],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                self.last_failure_reason = (
                    f"merge conflict merging {branch}: {r.stderr.decode().strip()}"
                )
                return MergeResult.FAILED

            # Run tests
            r = subprocess.run(
                self.test_command,
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                self.last_failure_reason = (
                    f"tests failed: {r.stdout.decode().strip()} {r.stderr.decode().strip()}"
                ).strip()
                return MergeResult.FAILED

            # Fast-forward integration branch
            r = subprocess.run(
                ["git", "checkout", self.integration_branch],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                self.last_failure_reason = (
                    f"could not checkout {self.integration_branch}: {r.stderr.decode().strip()}"
                )
                return MergeResult.FAILED

            r = subprocess.run(
                ["git", "merge", "--ff-only", temp_branch],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                self.last_failure_reason = (
                    f"ff-only merge failed: {r.stderr.decode().strip()}"
                )
                return MergeResult.FAILED

            # Push to origin
            r = subprocess.run(
                ["git", "push", "origin", self.integration_branch],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                self.last_failure_reason = (
                    f"push failed: {r.stderr.decode().strip()}"
                )
                return MergeResult.FAILED

            return MergeResult.MERGED

        finally:
            self._cleanup()

    def kickback_with_cascade(
        self,
        task_id: str,
        reason: str,
        _visited: set[str] | None = None,
    ) -> None:
        """Kick back a task and recursively cascade to downstream done tasks.

        Only invalidates non-merged descendants in done status.
        Does NOT interrupt active tasks — let them finish and the merge
        gate or next cascade will catch staleness.
        Uses a visited set to guard against cyclic deps and repeated traversal.
        """
        if _visited is None:
            _visited = set()
        if task_id in _visited:
            return
        _visited.add(task_id)

        self.colony.kickback(task_id, reason)

        all_tasks = self.colony.list_tasks()
        for task in all_tasks:
            tid = task.get("id", "")
            if tid in _visited:
                continue
            if task.get("status") != "done":
                continue
            if self._has_merged_attempt(task):
                continue
            deps = task.get("depends_on") or []
            if task_id in deps:
                cascade_reason = f"cascade: upstream {task_id} was kicked back"
                self.kickback_with_cascade(tid, cascade_reason, _visited=_visited)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _cleanup(self) -> None:
        """Restore repo to a clean state after a merge attempt (success or failure).

        Must be bulletproof — called in finally blocks. All commands use
        check=False so failures don't cascade.

        Invariant after cleanup:
        - On integration_branch
        - No temp branch
        - Clean working tree matching origin/{integration_branch}
        """
        # Abort any in-progress merge
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        # Return to integration branch
        subprocess.run(
            ["git", "checkout", self.integration_branch],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        # Delete temp branch
        subprocess.run(
            ["git", "branch", "-D", "antfarm/temp-merge"],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        # Remove untracked files and directories
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        # Hard reset to remote integration branch
        subprocess.run(
            ["git", "reset", "--hard", f"origin/{self.integration_branch}"],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )

    def _get_attempt_branch(self, task: dict) -> str | None:
        """Extract the branch from the task's current attempt.

        Args:
            task: Task dict from the colony API.

        Returns:
            Branch name string or None if not available.
        """
        current_attempt_id = task.get("current_attempt")
        if not current_attempt_id:
            return None
        for attempt in task.get("attempts", []):
            if attempt.get("attempt_id") == current_attempt_id:
                return attempt.get("branch") or None
        return None

    @staticmethod
    def _has_merged_attempt(task: dict) -> bool:
        """Return True if the task has at least one attempt with status MERGED."""
        return any(
            attempt.get("status") == "merged" for attempt in task.get("attempts", [])
        )

    # ------------------------------------------------------------------
    # v0.5.2+ Artifact and review helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_attempt_artifact(task: dict) -> dict | None:
        """Extract the artifact from the task's current attempt."""
        current_attempt_id = task.get("current_attempt")
        if not current_attempt_id:
            return None
        for attempt in task.get("attempts", []):
            if attempt.get("attempt_id") == current_attempt_id:
                return attempt.get("artifact")
        return None

    @staticmethod
    def _get_review_verdict(task: dict) -> dict | None:
        """Extract the review verdict from the task's current attempt."""
        current_attempt_id = task.get("current_attempt")
        if not current_attempt_id:
            return None
        for attempt in task.get("attempts", []):
            if attempt.get("attempt_id") == current_attempt_id:
                return attempt.get("review_verdict")
        return None

    @staticmethod
    def _sha_match(sha_a: str, sha_b: str) -> bool:
        """Compare two git SHAs, handling abbreviated forms.

        Full SHAs (40+ chars): exact equality.
        Abbreviated (7+ chars): prefix match.
        Too short (< 7 chars): reject as unreliable.
        """
        if not sha_a or not sha_b:
            return False
        if len(sha_a) >= 40 and len(sha_b) >= 40:
            return sha_a == sha_b
        shorter, longer = sorted([sha_a, sha_b], key=len)
        if len(shorter) < 7:
            return False
        return longer.startswith(shorter)

    def check_freshness(self, artifact_dict: dict) -> bool:
        """Check if the artifact's target branch SHA still matches current HEAD.

        Returns True if fresh (safe to merge), False if stale.
        """
        target_sha = artifact_dict.get("target_branch_sha_at_harvest", "")
        if not target_sha:
            return True  # no freshness data — allow (backward compat)

        target_branch = artifact_dict.get(
            "target_branch", self.integration_branch
        )
        try:
            r = subprocess.run(
                ["git", "rev-parse", f"origin/{target_branch}"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=False,
            )
            if r.returncode != 0:
                return True  # can't check — allow
            current_sha = r.stdout.strip()
            return self._sha_match(current_sha, target_sha)
        except Exception:
            return True  # can't check — allow

    def check_review_verdict(self, task: dict) -> tuple[bool, str]:
        """Check if the task has a passing, fresh review verdict.

        Returns:
            (passed, reason) — passed is True if review is valid.
        """
        verdict_dict = self._get_review_verdict(task)
        if verdict_dict is None:
            return False, "no review verdict on attempt"

        verdict = ReviewVerdict.from_dict(verdict_dict)

        if verdict.verdict != "pass":
            return False, f"review verdict is '{verdict.verdict}', not 'pass'"

        # Check freshness: reviewed_commit_sha must match head_commit_sha
        artifact_dict = self._get_attempt_artifact(task)
        if artifact_dict:
            head_sha = artifact_dict.get("head_commit_sha", "")
            if (
                head_sha
                and verdict.reviewed_commit_sha
                and not self._sha_match(head_sha, verdict.reviewed_commit_sha)
            ):
                return (
                    False,
                    f"review is stale: reviewed {verdict.reviewed_commit_sha[:12]} "
                    f"but head is {head_sha[:12]}",
                )

        return True, "review passed"

    _SHA_MARKER = "Attempt-SHA:"

    @classmethod
    def _extract_attempt_sha_from_spec(cls, spec: str) -> str:
        """Return the embedded Attempt-SHA from a review task spec, or ''."""
        if not spec:
            return ""
        marker = cls._SHA_MARKER
        for raw in spec.splitlines():
            line = raw.strip()
            if line.startswith(marker):
                return line[len(marker):].strip()
        return ""

    def _current_attempt_sha(self, task: dict) -> str:
        """Return a stable per-attempt discriminator.

        Prefers the artifact's head_commit_sha; falls back to the attempt's
        branch name (branches are per-attempt in Antfarm: feat/<task>-<attempt>).
        """
        artifact_dict = self._get_attempt_artifact(task)
        if artifact_dict:
            sha = artifact_dict.get("head_commit_sha") or ""
            if sha:
                return sha
        return self._get_attempt_branch(task) or ""

    def _build_review_spec(self, task: dict) -> str:
        """Build the review task spec for ``task``'s current attempt."""
        task_id = task["id"]
        branch = self._get_attempt_branch(task) or ""
        pr = ""
        for a in task.get("attempts", []):
            if a.get("attempt_id") == task.get("current_attempt"):
                pr = a.get("pr", "")
                break
        sha = self._current_attempt_sha(task)

        artifact_dict = self._get_attempt_artifact(task)
        review_pack_text = ""
        if artifact_dict:
            from antfarm.core.models import TaskArtifact
            from antfarm.core.review_pack import generate_review_pack

            try:
                artifact = TaskArtifact.from_dict(artifact_dict)
                review_pack_text = generate_review_pack(artifact, task.get("title", ""))
            except Exception:
                pass

        spec = (
            f"Review task {task_id}: '{task.get('title', '')}'\n\n"
            f"Branch: {branch}\n"
            f"PR: {pr}\n"
            f"{self._SHA_MARKER} {sha}\n\n"
        )
        if review_pack_text:
            spec += f"{review_pack_text}\n\n"
        spec += (
            "Instructions:\n"
            "1. Read the PR diff\n"
            "2. Check for bugs, security issues, and design problems\n"
            "3. Run tests to verify\n"
            "4. Produce a ReviewVerdict (pass/needs_changes/blocked)\n"
            "5. Output verdict between [REVIEW_VERDICT] and [/REVIEW_VERDICT] tags\n"
        )
        return spec

    def create_review_task(self, task: dict) -> str | None:
        """Create (or re-ready) a review task for a done task.

        If no review task exists, create a new one.
        If a review task exists:
          - Same attempt SHA as current → no-op (return None).
          - Different SHA (parent re-attempted) → re-ready the existing
            review task via ``backend.rereview`` with a fresh spec.

        Includes review pack in the spec when artifact is available.
        Propagates mission_id from the parent task to the review task.
        Suppresses review-task creation when the parent mission is CANCELLED.

        Returns the review task ID, or None if no action was taken or
        creation failed.
        """
        task_id = task["id"]
        review_task_id = f"review-{task_id}"

        # Suppress review-task creation for cancelled missions
        parent_mission_id = task.get("mission_id")
        if parent_mission_id:
            mission = self.colony.get_mission(parent_mission_id)
            if mission is not None and mission.get("status") == "cancelled":
                logger.info(
                    "suppressing review task for %s: mission %s is cancelled",
                    task_id,
                    parent_mission_id,
                )
                return None

        spec = self._build_review_spec(task)
        touches = task.get("touches", [])

        existing = self.colony.get_task(review_task_id)
        if existing is not None:
            # Compare embedded SHA on existing review task vs current attempt
            existing_sha = self._extract_attempt_sha_from_spec(
                existing.get("spec", "")
            )
            current_sha = self._current_attempt_sha(task)
            if existing_sha and current_sha and existing_sha == current_sha:
                # Already in flight or verdicted for this attempt
                return None
            # SHA mismatch (re-attempt) — re-ready the existing review task
            try:
                self.colony.rereview(review_task_id, spec, touches)
                logger.info(
                    "re-readied review task %s for new attempt (sha %s -> %s)",
                    review_task_id,
                    (existing_sha or "?")[:12],
                    (current_sha or "?")[:12],
                )
                return review_task_id
            except Exception:
                logger.exception("failed to re-review %s", review_task_id)
                return None

        try:
            self.colony.carry(
                task_id=review_task_id,
                title=f"Review: {task.get('title', task_id)}",
                spec=spec,
                depends_on=[],
                touches=touches,
                priority=1,
                complexity="S",
                capabilities_required=["review"],
                mission_id=parent_mission_id,
            )
            return review_task_id
        except Exception:
            return None

    # ------------------------------------------------------------------
    # from_backend: in-process Soldier (no HTTP round-trips)
    # ------------------------------------------------------------------

    @classmethod
    def from_backend(
        cls,
        backend: TaskBackend,
        repo_path: str,
        integration_branch: str = "main",
        test_command: list[str] | None = None,
        poll_interval: float = 30.0,
        require_review: bool = True,
    ) -> Soldier:
        """Create a Soldier that talks directly to a TaskBackend.

        Instead of going through the Colony HTTP API, this wraps the backend
        with a ColonyClient-compatible adapter so the Soldier can run
        in-process (e.g., as a daemon thread inside the colony server).
        Review is enabled by default — Soldier creates review tasks for done work.
        """
        instance = cls.__new__(cls)
        instance.colony = _BackendAdapter(backend)
        instance.repo_path = repo_path
        instance.integration_branch = integration_branch
        instance.test_command = test_command or ["pytest", "-x", "-q"]
        instance.poll_interval = poll_interval
        instance.require_review = require_review
        instance.last_failure_reason = ""
        return instance


class _BackendAdapter:
    """Wraps a TaskBackend with the subset of ColonyClient methods used by Soldier."""

    def __init__(self, backend: TaskBackend) -> None:
        self._backend = backend

    def list_tasks(self, status: str | None = None) -> list[dict]:
        return self._backend.list_tasks(status=status)

    def get_task(self, task_id: str) -> dict | None:
        return self._backend.get_task(task_id)

    def carry(self, **kwargs) -> dict:
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        mission_id = kwargs.get("mission_id")
        task = {
            "id": kwargs.get("task_id", ""),
            "title": kwargs.get("title", ""),
            "spec": kwargs.get("spec", ""),
            "complexity": kwargs.get("complexity", "M"),
            "priority": kwargs.get("priority", 10),
            "depends_on": kwargs.get("depends_on") or [],
            "touches": kwargs.get("touches") or [],
            "capabilities_required": kwargs.get("capabilities_required") or [],
            "mission_id": mission_id,
            "created_by": "soldier",
            "status": "ready",
            "current_attempt": None,
            "attempts": [],
            "trail": [],
            "signals": [],
            "created_at": now,
            "updated_at": now,
        }
        if mission_id:
            from antfarm.core.missions import link_task_to_mission

            task_id = link_task_to_mission(self._backend, task, mission_id)
        else:
            task_id = self._backend.carry(task)
        return {"task_id": task_id}

    def mark_merged(self, task_id: str, attempt_id: str) -> None:
        self._backend.mark_merged(task_id, attempt_id)

    def kickback(self, task_id: str, reason: str, max_attempts: int = 3) -> None:
        self._backend.kickback(task_id, reason, max_attempts=max_attempts)

    def store_review_verdict(
        self, task_id: str, attempt_id: str, verdict: dict
    ) -> None:
        self._backend.store_review_verdict(task_id, attempt_id, verdict)

    def get_mission(self, mission_id: str) -> dict | None:
        return self._backend.get_mission(mission_id)

    def rereview(
        self,
        review_task_id: str,
        new_spec: str,
        touches: list[str],
    ) -> None:
        self._backend.rereview(review_task_id, new_spec, touches)
