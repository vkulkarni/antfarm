"""Git worktree manager for task attempt isolation.

Each task attempt gets its own git worktree branched from the integration
branch (default: main). This provides filesystem isolation between concurrent
agent sessions without requiring separate repository clones.
"""

import os
import subprocess


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

    def create(self, task_id: str, attempt_id: str) -> str:
        """Create a git worktree for a task attempt.

        Fetches origin, then creates a new branch and worktree rooted at
        ``{workspace_root}/{task_id}-{attempt_id}``.

        Args:
            task_id: Identifier for the task (e.g. "task-001").
            attempt_id: Identifier for the attempt (e.g. "att-001").

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

        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                branch,
                workspace_path,
                f"origin/{self.integration_branch}",
            ],
            cwd=self.repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

        return workspace_path

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
