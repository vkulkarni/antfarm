"""Git worktree manager for task attempt isolation.

Each task attempt gets its own git worktree branched from the integration
branch (default: main). This provides filesystem isolation between concurrent
agent sessions without requiring separate repository clones.
"""

import logging
import os
import subprocess

logger = logging.getLogger(__name__)


class WorkspaceManager:
    """Manages git worktrees for task attempt isolation.

    Args:
        workspace_root: Directory under which per-attempt worktrees are created.
        repo_path: Path to the git repository used as the worktree source.
        integration_branch: Remote branch new worktrees branch off of.
    """

    def __init__(self, workspace_root: str, repo_path: str, integration_branch: str = "main"):
        self.workspace_root = workspace_root
        self.repo_path = repo_path
        self.integration_branch = integration_branch

    def create(
        self,
        task_id: str,
        attempt_id: str,
        dep_branches: list[str] | None = None,
    ) -> str:
        """Create a git worktree for a task attempt.

        Fetches origin, then creates a new branch and worktree rooted at
        ``{workspace_root}/{task_id}-{attempt_id}``.

        The base ref for the new worktree is selected as follows:

        - ``dep_branches`` is ``None`` or empty → ``origin/<integration_branch>``
          (current behavior, byte-identical for the zero-dep case).
        - Exactly one entry → ``origin/<dep_branch>``. Verified via
          ``git rev-parse --verify``; if missing, falls back to the
          integration branch and logs a warning.
        - More than one entry → ``origin/<integration_branch>`` and a
          warning is logged. Multi-dep branch graphs are out of scope for
          the v0.6.7 efficiency pass.

        Args:
            task_id: Identifier for the task (e.g. "task-001").
            attempt_id: Identifier for the attempt (e.g. "att-001").
            dep_branches: Optional list of pre-resolved dep attempt branch
                names (without the ``origin/`` prefix). The caller (Worker)
                is responsible for filtering to unmerged deps with branches.

        Returns:
            Absolute path to the created worktree.

        Raises:
            subprocess.CalledProcessError: If any git command fails.
            ValueError: If task_id or attempt_id contain path traversal characters.
        """
        for name, value in [("task_id", task_id), ("attempt_id", attempt_id)]:
            if ".." in value or os.sep in value or "/" in value:
                raise ValueError(f"{name} contains unsafe path characters: {value!r}")

        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=self.repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

        branch = f"feat/{task_id}-{attempt_id}"
        workspace_path = os.path.join(self.workspace_root, f"{task_id}-{attempt_id}")

        base_ref = self._select_base_ref(dep_branches)

        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                branch,
                workspace_path,
                base_ref,
            ],
            cwd=self.repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

        return workspace_path

    def _select_base_ref(self, dep_branches: list[str] | None) -> str:
        """Pick the base ref for a new worktree given resolved dep branches.

        Encapsulates the single-unmerged-dep optimization. Always falls
        back to the integration branch on ambiguity or missing refs, and
        logs a warning in those cases so operators can diagnose drift.
        """
        integration_ref = f"origin/{self.integration_branch}"

        if not dep_branches:
            return integration_ref

        if len(dep_branches) > 1:
            logger.warning(
                "multi-dep branch base not supported; falling back to integration "
                "(deps=%s)",
                dep_branches,
            )
            return integration_ref

        dep_branch = dep_branches[0]
        dep_ref = f"origin/{dep_branch}"
        verify = subprocess.run(
            ["git", "rev-parse", "--verify", dep_ref],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
        )
        if verify.returncode != 0:
            logger.warning(
                "dep branch %s not found; falling back to integration",
                dep_ref,
            )
            return integration_ref

        return dep_ref

    def validate(self, workspace_path: str) -> bool:
        """Check that a worktree path exists and has a clean working tree.

        Args:
            workspace_path: Path to the worktree to validate.

        Returns:
            True if the path is inside a git work tree and has no uncommitted
            changes; False otherwise.
        """
        if not os.path.exists(workspace_path):
            return False

        try:
            result = subprocess.run(
                ["git", "-C", workspace_path, "rev-parse", "--is-inside-work-tree"],
                check=True,
                capture_output=True,
                text=True,
            )
            if result.stdout.strip() != "true":
                return False

            status = subprocess.run(
                ["git", "-C", workspace_path, "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
            return status.stdout.strip() == ""

        except subprocess.CalledProcessError:
            return False

    def list_orphans(self) -> list[str]:
        """List worktree paths under workspace_root from the repository's worktree list.

        Parses ``git worktree list --porcelain`` output. Only paths that start
        with ``self.workspace_root`` are returned; the caller decides which are
        orphans.

        Returns:
            List of worktree paths located under workspace_root.
        """
        result = subprocess.run(
            ["git", "-C", self.repo_path, "worktree", "list", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )

        paths: list[str] = []
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):]
                if path.startswith(self.workspace_root):
                    paths.append(path)

        return paths
