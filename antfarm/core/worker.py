"""Worker runtime lifecycle for Antfarm.

Orchestrates the full worker loop: register → forage → workspace → launch agent
→ harvest → repeat. Delegates git operations to WorkspaceManager and all colony
API calls to ColonyClient.

Agent subprocess execution is intentionally simple in v0.1. The runtime does not
assume AI capability — any executable that writes to a branch and returns a non-zero
exit code on failure is a valid agent.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass

import httpx

from antfarm.core.colony_client import ColonyClient
from antfarm.core.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent result
# ---------------------------------------------------------------------------


@dataclass
class AgentResult:
    """Result of a launched agent subprocess."""

    returncode: int
    stdout: str
    stderr: str
    branch: str


# ---------------------------------------------------------------------------
# Worker runtime
# ---------------------------------------------------------------------------


class WorkerRuntime:
    """Orchestrates the full worker lifecycle.

    Args:
        colony_url: Base URL of the colony server.
        node_id: Identifier for this machine / node.
        name: Worker name (combined with node_id to form worker_id).
        agent_type: Agent adapter to use ("claude-code", "codex", "aider", "generic").
        workspace_root: Directory under which per-attempt worktrees are created.
        repo_path: Path to the git repository used as the worktree source.
        integration_branch: Branch new worktrees are created from.
        heartbeat_interval: Seconds between heartbeat posts (default 30).
        client: Optional httpx.Client for dependency injection in tests.
        token: Optional bearer token for colony authentication.
    """

    def __init__(
        self,
        colony_url: str,
        node_id: str,
        name: str,
        agent_type: str,
        workspace_root: str,
        repo_path: str,
        integration_branch: str = "dev",
        heartbeat_interval: float = 30.0,
        capabilities: list[str] | None = None,
        client: httpx.Client | None = None,
        token: str | None = None,
    ):
        self.worker_id = f"{node_id}/{name}"
        self.node_id = node_id
        self.agent_type = agent_type
        self.workspace_root = workspace_root
        self.heartbeat_interval = heartbeat_interval
        self.capabilities = capabilities or []
        self._token = token

        self.colony = ColonyClient(colony_url, client=client, token=token)
        self.workspace_mgr = WorkspaceManager(workspace_root, repo_path, integration_branch)

        self._heartbeat_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main lifecycle loop.

        Registers the worker, iterates _process_one_task until the queue is
        empty, then deregisters unconditionally in the finally block.
        """
        self.colony.register_worker(
            self.worker_id,
            self.node_id,
            self.agent_type,
            self.workspace_root,
            capabilities=self.capabilities,
        )
        logger.info("worker registered worker_id=%s", self.worker_id)

        try:
            while True:
                had_task = self._process_one_task()
                if not had_task:
                    logger.info("queue empty, worker exiting worker_id=%s", self.worker_id)
                    break
        finally:
            self.colony.deregister_worker(self.worker_id)
            logger.info("worker deregistered worker_id=%s", self.worker_id)

    # ------------------------------------------------------------------
    # Task processing
    # ------------------------------------------------------------------

    def _process_one_task(self) -> bool:
        """Forage for one task, execute it, and harvest.

        Returns:
            True if a task was processed (or attempted), False if queue was empty.
        """
        task = self.colony.forage(self.worker_id)
        if task is None:
            return False

        task_id = task["id"]
        attempt_id = task["current_attempt"]
        logger.info("task claimed task_id=%s attempt_id=%s", task_id, attempt_id)

        workspace = self.workspace_mgr.create(task_id, attempt_id)
        logger.info("workspace created path=%s", workspace)

        self._start_heartbeat_loop()
        try:
            result = self._launch_agent(task, workspace)
        finally:
            self._stop_heartbeat_loop()

        if result.returncode != 0:
            # Agent failed — trail the failure, do NOT harvest. Task stays active
            # so doctor can recover it (kickback to ready for next attempt).
            logger.warning(
                "agent failed task_id=%s returncode=%d stderr=%r",
                task_id,
                result.returncode,
                result.stderr[:200],
            )
            self.colony.trail(
                task_id,
                self.worker_id,
                f"agent exited with code {result.returncode}: {result.stderr[:200]}",
            )
            return True

        # Successful agent — harvest the task.
        try:
            self.colony.harvest(task_id, attempt_id, pr="", branch=result.branch)
            logger.info("task harvested task_id=%s branch=%s", task_id, result.branch)
        except Exception as exc:
            # 409 = ownership loss (another worker claimed this attempt).
            # Log a warning and continue to next task — not fatal.
            logger.warning(
                "harvest failed task_id=%s attempt_id=%s error=%s",
                task_id,
                attempt_id,
                exc,
            )

        return True

    # ------------------------------------------------------------------
    # Agent launch
    # ------------------------------------------------------------------

    def _launch_agent(self, task: dict, workspace: str) -> AgentResult:
        """Launch the coding agent as a subprocess in the workspace.

        Selects the command based on agent_type. Never uses shell=True.

        Args:
            task: Task dict from the colony (contains id, spec, etc.).
            workspace: Absolute path to the git worktree for this attempt.

        Returns:
            AgentResult with returncode, stdout, stderr, and branch name.
        """
        spec = task.get("spec", "")
        title = task.get("title", "")
        branch = f"feat/{task['id']}-{task['current_attempt']}"

        prompt = (
            f"Task: {title}\n\n"
            f"Spec: {spec}\n\n"
            f"You are working in: {workspace}\n"
            f"Branch: {branch}\n\n"
            "Instructions:\n"
            "1. Implement the task as specified\n"
            "2. Run tests to verify your changes work\n"
            "3. Commit all changes with a descriptive message\n"
            "4. Push the branch: git push -u origin {branch}\n"
        )

        if self.agent_type == "claude-code":
            cmd = [
                "claude", "-p",
                "--agent", "worker",
                "--permission-mode", "bypassPermissions",
                prompt,
            ]
        elif self.agent_type == "codex":
            cmd = ["codex", "--prompt", prompt]
        elif self.agent_type == "aider":
            cmd = ["aider", "--message", prompt, "--yes", "--no-auto-commits"]
        else:
            # generic: treat agent_type as the executable
            cmd = [self.agent_type, prompt]

        env = {
            **__import__("os").environ,
            "ANTFARM_URL": self.colony.base_url,
            "WORKER_ID": self.worker_id,
        }
        if self._token:
            env["ANTFARM_TOKEN"] = self._token

        proc = subprocess.run(
            cmd,
            cwd=workspace,
            capture_output=True,
            text=True,
            env=env,
        )

        return AgentResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            branch=branch,
        )

    # ------------------------------------------------------------------
    # Heartbeat thread
    # ------------------------------------------------------------------

    def _start_heartbeat_loop(self) -> None:
        """Start the background heartbeat daemon thread."""
        self._heartbeat_event.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_worker,
            daemon=True,
            name=f"heartbeat-{self.worker_id}",
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat_loop(self) -> None:
        """Signal the heartbeat thread to stop and wait for it to finish."""
        self._heartbeat_event.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5.0)
            self._heartbeat_thread = None

    def _heartbeat_worker(self) -> None:
        """Background thread body: POST heartbeat until event is set."""
        while not self._heartbeat_event.wait(timeout=self.heartbeat_interval):
            try:
                self.colony.heartbeat(self.worker_id)
                logger.debug("heartbeat sent worker_id=%s", self.worker_id)
            except Exception as exc:
                logger.warning("heartbeat failed worker_id=%s error=%s", self.worker_id, exc)
