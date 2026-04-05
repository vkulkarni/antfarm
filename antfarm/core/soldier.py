"""Soldier integration engine for Antfarm.

Deterministic merge gate: polls the colony for done tasks and merges them into
the integration branch via a temp branch. No AI, no auto-fix.

Policy (v0.1):
- Clean merge + green tests → fast-forward integration branch and mark merged
- Any conflict or test failure → kickback immediately
- Dependent tasks stay ineligible until upstream is merged
- Independent tasks continue merging (queue not globally blocked)
"""

from __future__ import annotations

import subprocess
import time
from enum import StrEnum

from antfarm.core.colony_client import ColonyClient


class MergeResult(StrEnum):
    MERGED = "merged"
    FAILED = "failed"


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
        integration_branch: str = "dev",
        test_command: list[str] | None = None,
        poll_interval: float = 30.0,
        client=None,
    ):
        self.colony = ColonyClient(colony_url, client=client)
        self.repo_path = repo_path
        self.integration_branch = integration_branch
        self.test_command = test_command or ["pytest", "-x", "-q"]
        self.poll_interval = poll_interval
        self.last_failure_reason = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main soldier loop. Runs indefinitely until interrupted."""
        while True:
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
                    self.colony.kickback(task["id"], self.last_failure_reason)

    def run_once(self) -> list[tuple[str, MergeResult]]:
        """Process the merge queue once and return results.

        Returns:
            List of (task_id, MergeResult) tuples for each task processed.
        """
        results = []
        queue = self.get_merge_queue()
        for task in queue:
            result = self.attempt_merge(task)
            attempt_id = task["current_attempt"]
            if result == MergeResult.MERGED:
                self.colony.mark_merged(task["id"], attempt_id)
            else:
                self.colony.kickback(task["id"], self.last_failure_reason)
            results.append((task["id"], result))
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
        eligible = []
        for task in all_tasks:
            if task.get("status") != "done":
                continue
            if not self._get_attempt_branch(task):
                continue
            # Check all dependencies are merged
            deps = task.get("depends_on") or []
            if not all(dep in merged_task_ids for dep in deps):
                continue
            eligible.append(task)

        # Sort by priority (lower = higher) then created_at (FIFO)
        eligible.sort(key=lambda t: (t.get("priority", 10), t.get("created_at", "")))
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
