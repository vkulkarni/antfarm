"""Worker runtime lifecycle for Antfarm.

Orchestrates the full worker loop: register → forage → workspace → launch agent
→ harvest → repeat. Delegates git operations to WorkspaceManager and all colony
API calls to ColonyClient.

Agent subprocess execution is intentionally simple in v0.1. The runtime does not
assume AI capability — any executable that writes to a branch and returns a non-zero
exit code on failure is a valid agent.
"""

from __future__ import annotations

import contextlib
import json as _json
import logging
import subprocess
import threading
from dataclasses import dataclass

import httpx

from antfarm.core.colony_client import ColonyClient
from antfarm.core.models import FailureRecord, FailureType
from antfarm.core.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

RETRY_POLICIES: dict[FailureType, dict] = {
    FailureType.INFRA_FAILURE: {"retryable": True, "max_retries": 3, "action": "retry"},
    FailureType.AGENT_CRASH: {"retryable": True, "max_retries": 2, "action": "retry"},
    FailureType.AGENT_TIMEOUT: {"retryable": True, "max_retries": 1, "action": "retry"},
    FailureType.TEST_FAILURE: {"retryable": False, "max_retries": 0, "action": "kickback"},
    FailureType.LINT_FAILURE: {"retryable": False, "max_retries": 0, "action": "kickback"},
    FailureType.BUILD_FAILURE: {"retryable": True, "max_retries": 1, "action": "retry"},
    FailureType.MERGE_CONFLICT: {"retryable": True, "max_retries": 1, "action": "retry"},
    FailureType.INVALID_TASK: {"retryable": False, "max_retries": 0, "action": "escalate"},
}


def classify_failure(returncode: int, stderr: str, stdout: str) -> FailureType:
    """Classify failure with strict precedence to avoid misclassification.

    Order matters — earlier checks take priority. Lint/build/infra checks
    come before test checks to prevent generic markers like 'error' or
    'failed' from triggering false test-failure classifications.
    """
    combined = (stderr + stdout).lower()

    # 1. Timeout (highest priority — clear signal)
    if returncode in (-9, -15) or "timeout" in combined:
        return FailureType.AGENT_TIMEOUT

    # 2. Infrastructure (clear external failures)
    infra_markers = [
        "permission denied", "disk full", "connection refused",
        "network unreachable", "enospc", "eacces",
    ]
    if any(m in combined for m in infra_markers):
        return FailureType.INFRA_FAILURE

    # 3. Lint (check before test — "ruff check: 3 errors in test_file.py" is lint, not test)
    lint_markers = ["ruff", "flake8", "pylint", "mypy", "type error", "lint"]
    if any(m in combined for m in lint_markers):
        return FailureType.LINT_FAILURE

    # 4. Build (check before test — "pip install failed" is build, not test)
    build_markers = [
        "build failed", "compilation error", "pip install",
        "modulenotfounderror", "importerror",
    ]
    if any(m in combined for m in build_markers):
        return FailureType.BUILD_FAILURE

    # 5. Test (requires BOTH a test-specific marker AND a failure indicator)
    test_contexts = ["pytest", "unittest", "test_", "tests/", "::test"]
    test_failures = ["failed", "assert", "error"]
    has_test_context = any(m in combined for m in test_contexts)
    has_test_failure = any(m in combined for m in test_failures)
    if has_test_context and has_test_failure:
        return FailureType.TEST_FAILURE

    # 6. Default: agent crash
    return FailureType.AGENT_CRASH


def get_retry_policy(failure_type: FailureType) -> dict:
    """Return the default retry policy for a failure type."""
    return RETRY_POLICIES.get(
        failure_type, {"retryable": False, "max_retries": 0, "action": "kickback"}
    )


def build_failure_record(
    task_id: str,
    attempt_id: str,
    worker_id: str,
    returncode: int,
    stderr: str,
    stdout: str,
) -> FailureRecord:
    """Build a structured FailureRecord from agent output."""
    from datetime import UTC, datetime

    failure_type = classify_failure(returncode, stderr, stdout)
    policy = get_retry_policy(failure_type)

    return FailureRecord(
        task_id=task_id,
        attempt_id=attempt_id,
        worker_id=worker_id,
        failure_type=failure_type,
        message=f"agent exited with code {returncode}",
        retryable=policy["retryable"],
        captured_at=datetime.now(UTC).isoformat(),
        stderr_summary=stderr[:500],
        recommended_action=policy["action"],
    )


# ---------------------------------------------------------------------------
# Review verdict parsing
# ---------------------------------------------------------------------------


def _parse_review_verdict(output: str) -> dict | None:
    """Extract a ReviewVerdict dict from agent output.

    Looks for content between [REVIEW_VERDICT] and [/REVIEW_VERDICT] tags,
    parses as JSON, and validates required fields.

    Returns None if no valid verdict is found.
    """
    import re

    match = re.search(
        r"\[REVIEW_VERDICT\]\s*(.*?)\s*\[/REVIEW_VERDICT\]",
        output,
        re.DOTALL,
    )
    if not match:
        return None

    try:
        data = _json.loads(match.group(1))
    except (ValueError, _json.JSONDecodeError):
        return None

    # Validate required fields
    required = {"provider", "verdict", "summary"}
    if not required.issubset(data.keys()):
        return None

    # Validate verdict value
    if data["verdict"] not in ("pass", "needs_changes", "blocked"):
        return None

    return data


def _extract_branch_from_spec(spec: str) -> str | None:
    """Extract branch name from a review task spec ("Branch: xxx" line)."""
    import re

    match = re.search(r"^Branch:\s*(.+)$", spec, re.MULTILINE)
    if match:
        branch = match.group(1).strip()
        return branch if branch else None
    return None


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

        Registers the node and worker, iterates _process_one_task until the
        queue is empty, then deregisters unconditionally in the finally block.
        """
        # Auto-register node before worker (#98)
        with contextlib.suppress(Exception):
            self.colony.register_node(self.node_id)

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
            # Agent failed — classify, record, and trail the failure.
            failure = build_failure_record(
                task_id=task_id,
                attempt_id=attempt_id,
                worker_id=self.worker_id,
                returncode=result.returncode,
                stderr=result.stderr,
                stdout=result.stdout,
            )
            logger.warning(
                "agent failed task_id=%s type=%s retryable=%s returncode=%d",
                task_id,
                failure.failure_type.value,
                failure.retryable,
                result.returncode,
            )
            self.colony.trail(
                task_id,
                self.worker_id,
                f"[{failure.failure_type.value}] {failure.message}: "
                f"{result.stderr[:200]}",
            )
            # Persist structured failure record in trail for downstream consumers
            self.colony.trail(
                task_id,
                self.worker_id,
                f"[FAILURE_RECORD] {_json.dumps(failure.to_dict())}",
            )
            return True

        # Set harvest_pending before writing result (best-effort)
        with contextlib.suppress(Exception):
            self.colony.mark_harvest_pending(task_id, attempt_id)

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

        # For review tasks: parse verdict from output and store on original task
        if task_id.startswith("review-") and result.returncode == 0:
            original_task_id = task_id[len("review-"):]
            verdict = _parse_review_verdict(result.stdout + result.stderr)
            if verdict:
                try:
                    original = self.colony.get_task(original_task_id)
                    if original and original.get("current_attempt"):
                        self.colony.store_review_verdict(
                            original_task_id,
                            original["current_attempt"],
                            verdict,
                        )
                        logger.info(
                            "stored review verdict on %s verdict=%s",
                            original_task_id,
                            verdict.get("verdict"),
                        )
                except Exception as exc:
                    logger.warning(
                        "failed to store review verdict for %s: %s",
                        original_task_id,
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
        is_review = task["id"].startswith("review-")
        branch = _extract_branch_from_spec(spec) if is_review else None
        if not branch:
            branch = f"feat/{task['id']}-{task['current_attempt']}"

        if is_review:
            prompt = (
                f"Task: {title}\n\n"
                f"Spec: {spec}\n\n"
                f"You are working in: {workspace}\n"
                f"Branch: {branch}\n\n"
                "Instructions:\n"
                "1. Read the PR diff for the branch above\n"
                "2. Check for bugs, security issues, and design problems\n"
                "3. Run tests to verify correctness\n"
                "4. Output your verdict between tags:\n"
                '   [REVIEW_VERDICT]{"provider":"<agent>","verdict":"pass",'
                '"summary":"...","findings":[],'
                '"reviewed_commit_sha":"..."}[/REVIEW_VERDICT]\n'
            )
        else:
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

        agent_role = "reviewer" if is_review else "worker"
        if self.agent_type == "claude-code":
            cmd = [
                "claude", "-p",
                "--agent", agent_role,
                "--permission-mode", "bypassPermissions",
                prompt,
            ]
        elif self.agent_type == "codex":
            cmd = ["codex", "--approval-mode", "full-auto", "--quiet", prompt]
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
