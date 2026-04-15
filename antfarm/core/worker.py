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
import time
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
        integration_branch: str = "main",
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
        self._last_task_id: str | None = None
        # Polling is tiered by worker role so short-lived roles exit promptly
        # while roles that wait on upstream tasks keep polling (#144).
        caps = capabilities or []
        if "review" in caps:
            # Reviewers wait up to 5min for builders to harvest review tasks.
            self._role = "reviewer"
            self._max_idle_polls = 10  # 10 * 30s = 5min
        elif "plan" in caps:
            # Planners produce one batch of child tasks and exit promptly.
            self._role = "planner"
            self._max_idle_polls = 0
        else:
            # Builders wait up to 2.5min so they outlast a typical planner run
            # and don't race the planner when started together (#144).
            self._role = "builder"
            self._max_idle_polls = 5  # 5 * 30s = 2.5min

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
            idle_polls = 0
            max_idle_polls = self._max_idle_polls  # 0 = exit immediately, >0 = poll
            while True:
                had_task = self._process_one_task()
                if not had_task:
                    if idle_polls >= max_idle_polls:
                        logger.info("queue empty, worker exiting worker_id=%s", self.worker_id)
                        break
                    idle_polls += 1
                    logger.debug("queue empty, polling (%d/%d) worker_id=%s role=%s",
                                 idle_polls, max_idle_polls, self.worker_id, self._role)
                    time.sleep(30)
                else:
                    idle_polls = 0  # reset on successful forage
        finally:
            if self._last_task_id:
                with contextlib.suppress(Exception):
                    self.colony.trail(
                        self._last_task_id,
                        self.worker_id,
                        "worker exiting — queue empty",
                    )
            with contextlib.suppress(Exception):
                self.colony.heartbeat(self.worker_id, status={"status": "offline"})
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
        self._last_task_id = task_id
        logger.info("task claimed task_id=%s attempt_id=%s", task_id, attempt_id)

        with contextlib.suppress(Exception):
            self.colony.trail(
                task_id, self.worker_id, "task claimed, creating workspace"
            )

        workspace = self.workspace_mgr.create(task_id, attempt_id)
        logger.info("workspace created path=%s", workspace)

        with contextlib.suppress(Exception):
            self.colony.trail(
                task_id, self.worker_id, "workspace ready, launching agent"
            )

        self._start_heartbeat_loop()
        try:
            result = self._launch_agent(task, workspace)
        finally:
            self._stop_heartbeat_loop()

        if result.returncode != 0:
            with contextlib.suppress(Exception):
                self.colony.trail(
                    task_id,
                    self.worker_id,
                    f"agent failed (exit {result.returncode})",
                )
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

        with contextlib.suppress(Exception):
            self.colony.trail(
                task_id, self.worker_id, "agent completed, building artifact"
            )

        # Plan task: parse output, validate, carry children, harvest with plan artifact
        caps_req = set(task.get("capabilities_required", []))
        is_plan = "plan" in caps_req
        if is_plan:
            plan_result = self._process_plan_output(
                task, attempt_id, result.stdout + result.stderr
            )
            if plan_result:
                is_mission_mode = plan_result.get("mission_mode", False)
                if is_mission_mode:
                    artifact = {
                        "plan_task_id": task_id,
                        "plan_artifact": plan_result["plan_artifact"],
                        "task_count": plan_result["plan_artifact"]["task_count"],
                        "warnings": plan_result["warnings"],
                        "dependency_summary": plan_result["dep_summary"],
                    }
                    trail_msg = (
                        f"plan complete (mission mode): "
                        f"{plan_result['plan_artifact']['task_count']} tasks proposed"
                    )
                else:
                    artifact = {
                        "plan_task_id": task_id,
                        "created_task_ids": plan_result["created_ids"],
                        "task_count": len(plan_result["created_ids"]),
                        "warnings": plan_result["warnings"],
                        "dependency_summary": plan_result["dep_summary"],
                    }
                    trail_msg = (
                        f"plan complete: created {len(plan_result['created_ids'])} tasks"
                    )
                with contextlib.suppress(Exception):
                    self.colony.mark_harvest_pending(task_id, attempt_id)
                with contextlib.suppress(Exception):
                    self.colony.harvest(
                        task_id, attempt_id, pr="", branch="",
                        artifact=artifact,
                    )
                with contextlib.suppress(Exception):
                    self.colony.trail(task_id, self.worker_id, trail_msg)
                return True

            # Plan parsing failed — trail the error
            with contextlib.suppress(Exception):
                self.colony.trail(
                    task_id, self.worker_id,
                    "plan failed: could not parse agent output into tasks",
                )
            return True

        # Set harvest_pending before writing result (best-effort)
        with contextlib.suppress(Exception):
            self.colony.mark_harvest_pending(task_id, attempt_id)

        # Build artifact and create PR
        artifact = self._build_artifact(task, attempt_id, workspace, result.branch)
        pr_url = self._create_pr(task, result.branch, workspace)
        if pr_url:
            artifact["pr_url"] = pr_url

        # Successful agent — harvest the task.
        try:
            self.colony.harvest(
                task_id, attempt_id, pr=pr_url, branch=result.branch, artifact=artifact
            )
            logger.info("task harvested task_id=%s branch=%s", task_id, result.branch)
            with contextlib.suppress(Exception):
                self.colony.trail(task_id, self.worker_id, "harvested successfully")
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
        is_review_task = (
            "review" in set(task.get("capabilities_required", []))
            or task_id.startswith("review-")
        )
        if is_review_task and result.returncode == 0:
            original_task_id = task_id.removeprefix("review-")
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
            else:
                # Agent didn't produce parseable [REVIEW_VERDICT] tags.
                # Trail a warning, then kickback the review task itself so
                # another reviewer attempt runs. The kickback budget (2 total
                # attempts) is enforced by FileBackend.kickback via
                # max_attempts — once exhausted, the review task moves to
                # blocked and Soldier.run_once_with_review kicks back the
                # *original* task with a clear reason.
                logger.warning(
                    "reviewer produced no verdict for %s", original_task_id
                )
                with contextlib.suppress(Exception):
                    self.colony.trail(
                        task_id,
                        self.worker_id,
                        f"WARNING: no [REVIEW_VERDICT] tags in output for {original_task_id}",
                    )
                review_attempt_count = len(task.get("attempts", []))
                retry_budget = 2
                if review_attempt_count < retry_budget:
                    logger.info(
                        "retrying review task %s (attempt %d/%d)",
                        task_id,
                        review_attempt_count,
                        retry_budget,
                    )
                else:
                    logger.warning(
                        "review task %s exhausted retry budget (%d attempts)",
                        task_id,
                        review_attempt_count,
                    )
                with contextlib.suppress(Exception):
                    self.colony.kickback(
                        task_id,
                        reason="reviewer produced no [REVIEW_VERDICT] tags",
                        max_attempts=retry_budget,
                    )

        return True

    # ------------------------------------------------------------------
    # Artifact building & PR creation
    # ------------------------------------------------------------------

    @staticmethod
    def _git(workspace: str, *args: str) -> str:
        """Run a git command in the workspace directory and return stdout."""
        proc = subprocess.run(
            ["git", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, ["git", *args], proc.stdout, proc.stderr
            )
        return proc.stdout.strip()

    def _build_artifact(
        self, task: dict, attempt_id: str, workspace: str, branch: str
    ) -> dict:
        """Collect git diff stats and commit metadata for the harvest payload."""
        artifact: dict = {}
        try:
            base_ref = f"origin/{self.workspace_mgr.integration_branch}...HEAD"
            artifact["diff_stat"] = self._git(workspace, "diff", "--stat", base_ref)
        except Exception:
            artifact["diff_stat"] = ""
        try:
            base_ref = f"origin/{self.workspace_mgr.integration_branch}...HEAD"
            numstat = self._git(workspace, "diff", "--numstat", base_ref)
            added = 0
            removed = 0
            for line in numstat.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    with contextlib.suppress(ValueError):
                        added += int(parts[0])
                        removed += int(parts[1])
            artifact["lines_added"] = added
            artifact["lines_removed"] = removed
        except Exception:
            artifact["lines_added"] = 0
            artifact["lines_removed"] = 0
        try:
            artifact["head_sha"] = self._git(workspace, "rev-parse", "HEAD")
        except Exception:
            artifact["head_sha"] = ""
        try:
            artifact["base_sha"] = self._git(
                workspace, "merge-base", f"origin/{self.workspace_mgr.integration_branch}", "HEAD"
            )
        except Exception:
            artifact["base_sha"] = ""
        return artifact

    def _create_pr(self, task: dict, branch: str, workspace: str) -> str:
        """Create a GitHub PR using the gh CLI. Returns PR URL or empty string."""
        title = task.get("title", task.get("id", "task"))
        body = task.get("spec", "")[:500]
        try:
            proc = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--title", title,
                    "--body", body,
                    "--head", branch,
                    "--fill",
                ],
                cwd=workspace,
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                url = proc.stdout.strip()
                logger.info("PR created url=%s", url)
                return url
            logger.warning("gh pr create failed: %s", proc.stderr[:200])
        except FileNotFoundError:
            logger.warning("gh CLI not installed — skipping PR creation")
        except Exception as exc:
            logger.warning("PR creation failed: %s", exc)
        return ""

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
        caps_req = set(task.get("capabilities_required", []))
        is_plan = "plan" in caps_req
        is_review = "review" in caps_req
        branch = _extract_branch_from_spec(spec) if is_review else None
        if not branch:
            branch = f"feat/{task['id']}-{task['current_attempt']}"

        if is_plan:
            prompt = (
                f"Task: {title}\n\n"
                "You are a PLANNER. Decompose this spec into implementation tasks.\n\n"
                f"Spec:\n{spec}\n\n"
                f"You are working in: {workspace}\n"
                "Read the codebase to understand the project structure.\n\n"
                "Output a JSON array of tasks between [PLAN_RESULT] tags.\n"
                "Each task object must have:\n"
                '  - "title": short imperative title\n'
                '  - "spec": detailed implementation instructions (2-5 sentences)\n'
                '  - "touches": list of scope tags (e.g. ["api", "auth"])\n'
                '  - "depends_on": list of task indices (1-based) or []\n'
                '  - "priority": integer 1-20 (lower = higher priority)\n'
                '  - "complexity": "S", "M", or "L"\n\n'
                "Rules:\n"
                "- Maximum 10 tasks\n"
                "- Make tasks as parallel as possible\n"
                "- Use depends_on only when strictly necessary\n"
                "- Each task should be independently implementable\n\n"
                "Example output:\n"
                "[PLAN_RESULT]\n"
                '[{"title": "Add auth middleware", "spec": "...", '
                '"touches": ["api"], "depends_on": [], '
                '"priority": 5, "complexity": "M"}]\n'
                "[/PLAN_RESULT]\n"
            )
        elif is_review:
            prompt = (
                f"Task: {title}\n\n"
                f"Spec: {spec}\n\n"
                f"You are working in: {workspace}\n"
                f"Branch: {branch}\n\n"
                "Instructions:\n"
                "1. Read the PR diff for the branch above\n"
                "2. Check for bugs, security issues, and design problems\n"
                "3. Run tests to verify correctness\n"
                "4. Produce a ReviewVerdict (see output format below)\n\n"
                "## MANDATORY OUTPUT FORMAT — READ THIS TWICE\n"
                "Your verdict MUST be wrapped in [REVIEW_VERDICT] ... "
                "[/REVIEW_VERDICT] tags.\n"
                "The content between the tags MUST be a single valid JSON "
                "object.\n"
                "If you forget the tags, the colony cannot parse your verdict "
                "and the review will be retried or failed.\n\n"
                "### Worked example 1 — pass\n"
                "[REVIEW_VERDICT]\n"
                '{"provider":"<agent>","verdict":"pass",'
                '"summary":"Change is clean, tests cover the regression.",'
                '"findings":[],'
                '"reviewed_commit_sha":"a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"}\n'
                "[/REVIEW_VERDICT]\n\n"
                "### Worked example 2 — needs_changes\n"
                "[REVIEW_VERDICT]\n"
                '{"provider":"<agent>","verdict":"needs_changes",'
                '"summary":"Missing owner validation on release path.",'
                '"findings":["release_guard drops owner check",'
                '"no test for mismatch"],'
                '"reviewed_commit_sha":"1122334455667788990011223344556677889900"}\n'
                "[/REVIEW_VERDICT]\n\n"
                'Verdict values: "pass", "needs_changes", "blocked".\n\n'
                "### Final checklist — do NOT skip\n"
                "Before you finish: did you wrap your JSON in [REVIEW_VERDICT]"
                " ... [/REVIEW_VERDICT]? If not, STOP and redo. A reply "
                "without the tags is treated as a failed review.\n"
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

        agent_role = "planner" if is_plan else ("reviewer" if is_review else "worker")
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
    # Plan output processing
    # ------------------------------------------------------------------

    def _process_plan_output(
        self, task: dict, attempt_id: str, output: str,
    ) -> dict | None:
        """Parse plan output, validate, and carry child tasks.

        Returns dict with created_ids, warnings, dep_summary on success.
        Returns None if parsing or validation fails.
        """
        import re

        # Extract JSON from [PLAN_RESULT]...[/PLAN_RESULT] tags
        match = re.search(
            r"\[PLAN_RESULT\]\s*(.*?)\s*\[/PLAN_RESULT\]",
            output, re.DOTALL,
        )
        if not match:
            logger.warning("no [PLAN_RESULT] tags in planner output")
            return None

        # Parse and validate using shared PlannerEngine logic
        from antfarm.core.planner import PlannerEngine, resolve_dependencies

        engine = PlannerEngine()
        plan_result = engine.parse_structured_plan(match.group(1))

        if not plan_result.tasks:
            logger.warning("plan produced no tasks")
            with contextlib.suppress(Exception):
                self.colony.trail(
                    task["id"], self.worker_id,
                    "plan produced no tasks"
                    + (f": {plan_result.warnings[0]}" if plan_result.warnings else ""),
                )
            return None

        errors = engine.validate_plan(plan_result)
        if errors:
            for err in errors:
                logger.warning("plan validation error: %s", err)
                with contextlib.suppress(Exception):
                    self.colony.trail(
                        task["id"], self.worker_id,
                        f"plan validation error: {err}",
                    )
            return None

        tasks = plan_result.tasks

        # Guardrail: max 10 children
        if len(tasks) > 10:
            logger.warning("plan has %d tasks, max 10", len(tasks))
            with contextlib.suppress(Exception):
                self.colony.trail(
                    task["id"], self.worker_id,
                    f"plan rejected: {len(tasks)} tasks exceeds max 10",
                )
            return None

        # Generate deterministic child IDs (NOT plan-prefixed — these are impl tasks)
        parent_id = task["id"]
        slug = parent_id.removeprefix("plan-")
        child_ids = [f"task-{slug}-{i:02d}" for i in range(1, len(tasks) + 1)]

        # Resolve index-based deps to child IDs
        resolved_tasks = resolve_dependencies(tasks, child_ids)

        # Generate warnings
        warnings = engine.generate_warnings(plan_result)
        warn_strs = [str(w) for w in warnings] if isinstance(warnings, list) else []

        # Build dependency summary
        dep_pairs: list[str] = []
        for i, t in enumerate(resolved_tasks):
            for dep in t.depends_on:
                dep_pairs.append(f"{dep} → {child_ids[i]}")
        dep_summary = ", ".join(dep_pairs) if dep_pairs else "all parallel"

        # ---- MISSION MODE ----
        if task.get("mission_id"):
            from antfarm.core.missions import PlanArtifact as MissionPlanArtifact

            plan_artifact = MissionPlanArtifact(
                plan_task_id=task["id"],
                attempt_id=attempt_id,
                proposed_tasks=[
                    t.to_carry_dict(child_ids[i])
                    for i, t in enumerate(resolved_tasks)
                ],
                task_count=len(resolved_tasks),
                warnings=warn_strs,
                dependency_summary=dep_summary,
            )
            return {
                "mission_mode": True,
                "plan_artifact": plan_artifact.to_dict(),
                "warnings": warn_strs,
                "dep_summary": dep_summary,
            }

        # ---- LEGACY (non-mission) MODE: carry each child task ----
        created_ids: list[str] = []
        failed_ids: list[str] = []
        for i, proposed_task in enumerate(resolved_tasks):
            child_id = child_ids[i]
            payload = proposed_task.to_carry_dict(child_id)

            # Guardrail: no recursive plans
            payload["capabilities_required"] = []

            # Lineage metadata
            spawned_by = {
                "task_id": parent_id,
                "attempt_id": attempt_id,
            }

            try:
                self.colony.carry(
                    task_id=child_id,
                    title=payload["title"],
                    spec=payload["spec"],
                    depends_on=payload.get("depends_on", []),
                    touches=payload.get("touches", []),
                    priority=payload.get("priority", 10),
                    complexity=payload.get("complexity", "M"),
                    capabilities_required=[],
                    spawned_by=spawned_by,
                )
                created_ids.append(child_id)
                logger.info("carried child task %s", child_id)
            except Exception as exc:
                # 409 = already exists (idempotent retry)
                if "409" in str(exc) or "already exists" in str(exc):
                    created_ids.append(child_id)
                    logger.info("child task %s already exists (idempotent)", child_id)
                else:
                    logger.warning("failed to carry child %s: %s", child_id, exc)
                    failed_ids.append(child_id)

        # Partial failure: if any non-idempotent carry failed, do NOT harvest
        if failed_ids:
            logger.warning(
                "plan partial failure: %d/%d tasks failed",
                len(failed_ids), len(resolved_tasks),
            )
            with contextlib.suppress(Exception):
                self.colony.trail(
                    task["id"], self.worker_id,
                    f"plan partial failure: {len(created_ids)} created, "
                    f"{len(failed_ids)} failed: {', '.join(failed_ids)}",
                )
            return None

        return {
            "created_ids": created_ids,
            "warnings": warn_strs,
            "dep_summary": dep_summary,
        }

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
