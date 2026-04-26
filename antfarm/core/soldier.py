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

import contextlib
import json
import logging
import os
import re
import subprocess
import time
from enum import StrEnum

import httpx

from antfarm.core.auto_merge import AutoMergeOutcome, decide, parse_pr_state
from antfarm.core.backends.base import TaskBackend
from antfarm.core.colony_client import ColonyClient
from antfarm.core.missions import is_infra_task
from antfarm.core.models import ReviewVerdict
from antfarm.core.review_pack import extract_verdict_from_review_task

logger = logging.getLogger(__name__)

# Event types that should wake the Soldier's run loop immediately.
# - harvested: a task moved to done/ — may be merge-eligible now
# - kickback: a task regressed — may unblock cascade or free scheduling
# - merged: a dependency was merged — dependents may now be eligible
WAKE_EVENT_TYPES: frozenset[str] = frozenset({"harvested", "kickback", "merged"})


# ---------------------------------------------------------------------------
# Merge diagnostic event vocabulary (v0.6.7)
#
# Soldier emits diagnostic event types to the SSE bus so operators can
# answer "why is this task still sitting in done/?" without reading soldier
# logs. All are emitted with actor="soldier" via ``_emit``.
#
# - merge_attempted: Fires at the top of ``attempt_merge`` once per call.
#   Detail format: ``attempt=<attempt_id> branch=<pr-branch>``. Canonical
#   name going forward; ``merge_started`` is dual-emitted for backward
#   compatibility during 0.6.x. (TODO: drop ``merge_started`` in 0.7.0.)
#
# - merge_skipped: Fires whenever Soldier evaluates a done, non-merged,
#   non-infra task and chooses NOT to merge this tick. Detail format:
#   ``reason=<short-code>``. Reason codes come from ``_MERGE_SKIP_REASONS``
#   below — short, stable strings operators can grep.
#
# - merge_failed: Fires once per ``attempt_merge`` call that returns FAILED,
#   BEFORE the caller invokes ``kickback_with_cascade``. Detail format:
#   ``reason=<short-code>: <human message>`` where ``<short-code>`` is one of
#   ``_MERGE_FAILED_REASONS``.
#
# - repo_dirty: Fires when ``attempt_merge`` pre-flight ``_assert_clean_repo``
#   detects the soldier clone is not in a clean state (wrong branch, dirty
#   working tree, or leftover ``antfarm/temp-merge`` branch). Soldier then
#   invokes ``_force_clean_repo`` to recover; if recovery fails a follow-up
#   ``merge_failed`` event with ``reason=repo_dirty`` is emitted.
#
# - cleanup_incomplete: Fires from ``_cleanup`` when the post-check
#   assertion after the five cleanup commands still finds the repo dirty.
#   Never raises (``_cleanup`` runs in a ``finally`` block) — only emits
#   the event and logs at ERROR so the condition is not silent.
#
# - worktree_reclaimed: Fires from ``_remove_blocking_worktree`` when the
#   rebase-retry checkout would otherwise fail with "branch already used by
#   worktree at <path>" and the path is an antfarm-managed worktree that
#   was safely removed via ``git worktree remove --force``. Detail format:
#   ``path=<absolute path>``. See #349.
#
# Emit failures are best-effort: a broken event bus MUST NEVER break merge
# logic. Failures below the ``_emit`` surface are swallowed and logged at
# DEBUG.
# ---------------------------------------------------------------------------

_MERGE_SKIP_REASONS = frozenset(
    {
        "dep_unmerged",  # a dependency has not yet merged
        "no_pr",  # current attempt has no branch/PR
        "review_pending",  # review task was just created this tick
        "review_in_progress",  # review task exists but is not done yet
        "review_stale_sha",  # review verdict is for an older attempt SHA
        "needs_changes",  # review verdict is not 'pass'
        "already_merged",  # current_attempt is already MERGED
        "superseded",  # current_attempt is None (post-kickback)
    }
)

_MERGE_FAILED_REASONS = frozenset(
    {
        "merge_conflict",  # git merge reported a conflict
        "test_failed",  # test_command returned non-zero
        "rebase_failed",  # rebase/fast-forward failed
        "push_failed",  # git push origin failed
        "no_pr",  # current attempt has no branch
        "fetch_failed",  # git fetch origin failed
        "checkout_failed",  # could not checkout integration branch / temp
        "repo_dirty",  # soldier clone was dirty and recovery failed
        "auto_merge_ci_failed",  # CI on PR reported FAILURE while mode gated on CI
        "auto_merge_refused",  # security guard refused auto-merge on this repo
        "unknown",  # catch-all for unclassified failures
    }
)


# ---------------------------------------------------------------------------
# Auto-merge event vocabulary (#353)
#
# When a mission's ``config.auto_merge`` is not ``never``, Soldier runs the
# auto-merge pipeline on every passing-verdict task. The pipeline emits
# these event types (actor=soldier) so operators can trace why a PR did or
# did not auto-merge:
#
# - auto_merged: PR was successfully squash-merged via ``gh pr merge``.
# - auto_merge_rebasing: PR needs ``git merge --ff-only`` / rebase before
#   auto-merge can proceed. Usually followed by another auto_merge_* event
#   on the next tick.
# - auto_merge_waiting_ci: ``on-review-pass-and-ci-green`` mode, PR
#   mergeStateStatus is UNSTABLE or PENDING. Soldier skips this tick.
# - auto_merge_kickback: mergeStateStatus=BLOCKED with failing CI. Soldier
#   kicks back the task with reason=auto_merge_ci_failed so the worker
#   re-attempts with fixed code.
# - auto_merge_blocked: mergeStateStatus=BLOCKED with missing reviews or
#   other unresolved reason. Soldier pauses the parent mission (sets
#   status=BLOCKED + stores ``auto_merge_pause_reason``).
# - auto_merge_refused: security guard refused auto-merge (viewer lacks
#   permission or main/master without ADMIN). Surfaces once per PR until
#   the backoff window expires.
#
# Reconciliation event vocabulary (#367)
#
# - reconciled_external: A PR was detected as MERGED on origin and the
#   matching attempt was marked MERGED in antfarm. Emitted from both the
#   per-task path (``_reconcile_external_merge``) and the mission-wide
#   poll-pass (``_reconcile_external_merges``). The poll-pass variant
#   includes ``source=poll_pass`` in the detail so operators can tell the
#   two sources apart.
#
# - auto_merge_mark_drift: ``gh pr merge`` succeeded on origin but the
#   subsequent ``mark_merged`` raised ``ValueError`` (typically attempt
#   drift between the auto-merge decision and the antfarm-side write).
#   The auto-merge tick still reports MERGED — the bulk reconciliation
#   pass will catch the antfarm-side flag failure on a later tick.
#
# - auto_merge_mark_failed: ``gh pr merge`` succeeded but ``mark_merged``
#   raised an unexpected (non-TypeError, non-ValueError) exception. Same
#   recovery path as ``auto_merge_mark_drift`` — the next reconcile pass
#   converges state. Detail includes ``type=<exception class>`` so the
#   operator can grep for novel failure modes.
# ---------------------------------------------------------------------------


def _emit(event_type: str, task_id: str, detail: str = "") -> None:
    """Emit an SSE event tagged with actor='soldier'.

    Lazy-imports ``_emit_event`` to avoid a circular import at module load
    time (serve.py imports Soldier) and to keep soldier importable in
    contexts where the FastAPI server module cannot be loaded.

    Best-effort: ANY failure inside the emit pipeline is swallowed and
    logged at DEBUG. A broken event bus MUST NEVER break merge logic.
    """
    try:
        from antfarm.core.serve import _emit_event
    except Exception:
        logger.debug("soldier _emit: could not import _emit_event", exc_info=True)
        return
    try:
        _emit_event(event_type, task_id, detail, actor="soldier")
    except Exception:
        logger.debug("soldier _emit: _emit_event(%s) raised", event_type, exc_info=True)


def _set_activity(action: str, target: str = "") -> None:
    """Publish soldier phase activity to the colony (#348).

    Lazy-imports ``_set_colony_activity`` to avoid a circular import at module
    load time. Best-effort: ANY failure inside the publish pipeline is
    swallowed. The soldier must never break because the TUI sidecar is unhappy.
    """
    try:
        from antfarm.core.serve import _set_colony_activity
    except Exception:
        logger.debug("soldier _set_activity: could not import helper", exc_info=True)
        return
    try:
        _set_colony_activity("soldier", action, target)
    except Exception:
        logger.debug("soldier _set_activity(%s) raised", action, exc_info=True)


def _stderr_tail(stderr_bytes: bytes, n: int = 20) -> str:
    """Return the last ``n`` lines of decoded stderr bytes.

    Used to preserve diagnostic context on test failures (#326): without a
    tail, short one-line summaries truncate the actual error into noise.
    """
    text = stderr_bytes.decode(errors="replace").rstrip()
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if lines else ""


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
        poll_external_merges: bool = True,
        data_dir_name: str = ".antfarm",
        client=None,
    ):
        self.colony = ColonyClient(colony_url, client=client)
        self.colony_url = colony_url
        self.repo_path = repo_path
        self.integration_branch = integration_branch
        self.test_command = test_command or ["pytest", "-x", "-q"]
        self.poll_interval = poll_interval
        self.require_review = require_review
        self.poll_external_merges = poll_external_merges
        # Passed as -e <data_dir_name> to git clean so the colony's runtime
        # state under repo_path is never wiped mid-mission (#323). Target repos
        # whose .gitignore doesn't list the data dir were being reset to empty
        # by _cleanup / _force_clean_repo.
        self.data_dir_name = data_dir_name
        self.last_failure_reason = ""
        # Event cursor for /events SSE stream. In-memory only; never persists.
        self._event_cursor: int = 0
        # One-shot preflight validation of ``test_command`` — see #326. Fires
        # at most once per Soldier lifetime from ``run`` or ``run_once``.
        self._preflight_done: bool = False
        # Auto-merge (#353): per-PR backoff (tick timestamp when we last
        # polled that PR's mergeability) and a per-repo cache of
        # ``gh repo view`` viewer permission (value, expires_ts).
        self._auto_merge_last_checked: dict[str, float] = {}
        self._repo_permission_cache: dict[str, tuple[str, float]] = {}
        self.auto_merge_poll_backoff_seconds: float = 30.0
        # Mission-wide reconciliation (#367): per-task TTL cache for
        # ``gh pr view`` calls during the bulk reconciliation pass at the
        # top of every poll. Keyed by task_id, value is the last-checked
        # monotonic timestamp.
        self._reconcile_last_checked: dict[str, float] = {}
        self.reconcile_backoff_seconds: float = 60.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main soldier loop. Runs indefinitely until interrupted.

        Between ticks, the loop waits on the colony's /events SSE stream
        so it can wake immediately on ``harvested``/``kickback``/``merged``
        events. On any connection failure the loop falls back to the
        legacy polling behaviour (``time.sleep(self.poll_interval)``).

        When ``require_review=True`` (the production configuration — see
        ``serve._start_soldier_thread``) every tick dispatches through
        ``run_once_with_review``, which is the single source of truth for
        verdict-gated merges AND kickbacks on ``needs_changes``. The
        legacy inline merge loop below is only used when review is
        disabled (``require_review=False``), which no production deploy
        sets today (see #328 for the bug this split was hiding).
        """
        self._run_preflight_if_needed()
        while True:
            _set_activity("polling", "")
            # #367: bulk reconciliation pass MUST run before any merge-queue
            # construction so upstream desync (PR merged on origin but
            # antfarm attempt still un-MERGED) doesn't block dependents.
            self._reconcile_external_merges(self.colony.list_tasks())
            if self.require_review:
                self.run_once_with_review()
            else:
                queue = self.get_merge_queue()
                for task in queue:
                    if self._reconcile_external_merge(task):
                        continue
                    result = self.attempt_merge(task)
                    attempt_id = task["current_attempt"]
                    if result == MergeResult.MERGED:
                        self._safe_mark_merged(task["id"], attempt_id)
                    else:
                        self.kickback_with_cascade(task["id"], self.last_failure_reason)
            # ``_wait_for_event`` handles sleeping regardless of whether
            # the tick did any work — the previous early-continue on an
            # empty queue was harmless but added an extra code path.
            self._wait_for_event(timeout=self.poll_interval)

    def run_once(self) -> list[tuple[str, MergeResult]]:
        """Process the merge queue once and return results.

        Returns:
            List of (task_id, MergeResult) tuples for each task processed.
        """
        self._run_preflight_if_needed()
        # #367: bulk reconciliation pass MUST run before any merge-queue
        # construction so upstream desync (PR merged on origin but
        # antfarm attempt still un-MERGED) doesn't block dependents.
        self._reconcile_external_merges(self.colony.list_tasks())
        if self.require_review:
            self.process_done_tasks()
        results = []
        queue = self.get_merge_queue()
        for task in queue:
            if self._reconcile_external_merge(task):
                results.append((task["id"], MergeResult.MERGED))
                continue
            result = self.attempt_merge(task)
            attempt_id = task["current_attempt"]
            if result == MergeResult.MERGED:
                self._safe_mark_merged(task["id"], attempt_id)
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
            if is_infra_task(task):
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

        Emits ``merge_skipped`` (actor=soldier) for done, non-infra tasks
        filtered out here — see the reason-code vocabulary near the top of
        this module.
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
            if is_infra_task(task):
                continue
            task_id = task.get("id", "")
            if self._has_merged_attempt(task):
                # #327b: no emit here — merge_reconciled_external in
                # _reconcile_external_merge is the operator-actionable signal.
                continue
            # Post-kickback tasks have no current_attempt.
            if not task.get("current_attempt"):
                _emit("merge_skipped", task_id, "reason=superseded")
                continue
            if not self._get_attempt_branch(task):
                _emit("merge_skipped", task_id, "reason=no_pr")
                continue
            deps = task.get("depends_on") or []
            if not all(dep in merged_task_ids for dep in deps):
                _emit("merge_skipped", task_id, "reason=dep_unmerged")
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
        # #367: bulk reconciliation pass MUST run before any merge-queue
        # construction so upstream desync (PR merged on origin but
        # antfarm attempt still un-MERGED) doesn't block dependents.
        self._reconcile_external_merges(self.colony.list_tasks())
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
                    auto_outcome = self._attempt_auto_merge(task)
                    if auto_outcome is None:
                        result = self.attempt_merge(task)
                        if result == MergeResult.MERGED:
                            self._safe_mark_merged(task_id, attempt_id)
                        else:
                            try:
                                self.kickback_with_cascade(task_id, self.last_failure_reason)
                            except Exception as exc:
                                logger.exception("kickback failed for %s", task_id)
                                _emit(
                                    "soldier_error",
                                    task_id,
                                    f"op=kickback_merge_failed type={type(exc).__name__} msg={exc}",
                                )
                                raise
                    else:
                        result = self._handle_auto_merge_outcome(auto_outcome, task)
                    results.append((task_id, result))
                else:
                    # Diagnostic skip before kickback: needs_changes verdict.
                    _emit("merge_skipped", task_id, "reason=needs_changes")
                    try:
                        self.kickback_with_cascade(task_id, f"review failed: {reason}")
                        _emit(
                            "task_kicked_back",
                            task_id,
                            "reason=review:needs_changes",
                        )
                    except Exception as exc:
                        logger.exception("kickback failed for %s", task_id)
                        _emit(
                            "soldier_error",
                            task_id,
                            f"op=kickback_needs_changes type={type(exc).__name__} msg={exc}",
                        )
                        raise
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
                _emit("merge_skipped", task_id, "reason=review_pending")
                results.append((task_id, MergeResult.NEEDS_REVIEW))
                continue

            # Review task exists — if its embedded Attempt-SHA differs from
            # the parent's current attempt SHA, the review is stale
            # (parent was re-attempted after this review was created).
            # Re-ready the review task instead of consuming its stale
            # verdict against the new attempt.
            existing_sha = self._extract_attempt_sha_from_spec(review_task.get("spec", ""))
            current_sha = self._current_attempt_sha(task)
            if existing_sha and current_sha and existing_sha != current_sha:
                # Pure-rebase fast-path: if the prior attempt had a pass
                # verdict AND the non-test diffs against the integration
                # merge-base are byte-identical, carry the verdict forward
                # onto the new attempt and skip re-review entirely. On any
                # git failure, or if the diffs differ, or if the prior
                # verdict is not a pass, fall through to the safe re-ready
                # path below.
                prior_verdict = self._find_prior_pass_verdict(task, existing_sha)
                if prior_verdict is not None and self._diffs_equivalent_after_rebase(
                    task, existing_sha, current_sha
                ):
                    # Carry the verdict forward with reviewed_commit_sha
                    # updated to the new attempt's head so the downstream
                    # freshness check in ``check_review_verdict`` passes.
                    # We have already verified the non-test code diffs are
                    # byte-identical, so this update is safe.
                    carried = dict(prior_verdict)
                    carried["reviewed_commit_sha"] = current_sha
                    try:
                        self.colony.store_review_verdict(task_id, attempt_id, carried)
                    except Exception:
                        logger.exception(
                            "failed to carry forward verdict for %s; falling through to re-review",
                            task_id,
                        )
                    else:
                        logger.info(
                            "carrying forward pass verdict on pure-rebase "
                            "reharvest: task=%s old_sha=%s new_sha=%s",
                            task_id,
                            existing_sha[:12],
                            current_sha[:12],
                        )
                        task_updated = self.colony.get_task(task_id)
                        if task_updated is None:
                            results.append((task_id, MergeResult.FAILED))
                            continue
                        passed, reason = self.check_review_verdict(task_updated)
                        if passed:
                            auto_outcome = self._attempt_auto_merge(task_updated)
                            if auto_outcome is None:
                                result = self.attempt_merge(task_updated)
                                if result == MergeResult.MERGED:
                                    self._safe_mark_merged(task_id, attempt_id)
                                else:
                                    self.kickback_with_cascade(task_id, self.last_failure_reason)
                            else:
                                result = self._handle_auto_merge_outcome(auto_outcome, task_updated)
                            results.append((task_id, result))
                        else:
                            _emit("merge_skipped", task_id, "reason=needs_changes")
                            self.kickback_with_cascade(task_id, f"review failed: {reason}")
                            results.append((task_id, MergeResult.FAILED))
                        continue

                try:
                    new_spec = self._build_review_spec(task)
                    self.colony.rereview(review_task_id, new_spec, task.get("touches", []))
                    logger.info(
                        "re-readied review task %s for new attempt "
                        "(sha %s -> %s) from run_once_with_review",
                        review_task_id,
                        existing_sha[:12],
                        current_sha[:12],
                    )
                except Exception:
                    logger.exception(
                        "failed to re-review %s from run_once_with_review",
                        review_task_id,
                    )
                _emit("merge_skipped", task_id, "reason=review_stale_sha")
                results.append((task_id, MergeResult.NEEDS_REVIEW))
                continue

            # Review task exists — check its status.
            review_status = review_task.get("status", "")
            if review_status == "blocked":
                # Review task exhausted its retry budget without producing a
                # parseable verdict. Kick back the *original* task with a
                # clear reason so the build can be reattempted.
                self.kickback_with_cascade(task_id, "review task completed without a ReviewVerdict")
                results.append((task_id, MergeResult.FAILED))
                continue
            if review_status != "done":
                # Still in progress (ready/active/kicked-back awaiting retry)
                _emit("merge_skipped", task_id, "reason=review_in_progress")
                results.append((task_id, MergeResult.NEEDS_REVIEW))
                continue

            # Review task is done — extract verdict from review task's artifact
            review_verdict = extract_verdict_from_review_task(review_task)
            if review_verdict is None:
                # Review done but no verdict — treat as failure
                self.kickback_with_cascade(task_id, "review task completed without a ReviewVerdict")
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
                auto_outcome = self._attempt_auto_merge(task_updated)
                if auto_outcome is None:
                    result = self.attempt_merge(task_updated)
                    if result == MergeResult.MERGED:
                        self._safe_mark_merged(task_id, attempt_id)
                    else:
                        self.kickback_with_cascade(task_id, self.last_failure_reason)
                else:
                    result = self._handle_auto_merge_outcome(auto_outcome, task_updated)
                results.append((task_id, result))
            else:
                # Diagnostic skip before kickback: needs_changes verdict.
                _emit("merge_skipped", task_id, "reason=needs_changes")
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
        # Exclude infra tasks (review tasks, etc.) — they are informational.
        # Emit merge_skipped with a reason code at every decision point so
        # operators can grep the SSE bus to answer "why didn't this merge?"
        eligible = []
        for task in all_tasks:
            if task.get("status") != "done":
                continue
            if is_infra_task(task):
                continue
            task_id = task.get("id", "")
            # Skip cancelled tasks — they were purged by cancel_mission_tasks
            if task.get("cancelled_at"):
                _emit("merge_skipped", task_id, "reason=cancelled")
                continue
            # Skip already-merged tasks
            if self._has_merged_attempt(task):
                # #327b: no emit here — merge_reconciled_external in
                # _reconcile_external_merge is the operator-actionable signal.
                continue
            # Post-kickback tasks have current_attempt=None.
            if not task.get("current_attempt"):
                _emit("merge_skipped", task_id, "reason=superseded")
                continue
            if not self._get_attempt_branch(task):
                _emit("merge_skipped", task_id, "reason=no_pr")
                continue
            # Check all dependencies are merged
            deps = task.get("depends_on") or []
            if not all(dep in merged_task_ids for dep in deps):
                _emit("merge_skipped", task_id, "reason=dep_unmerged")
                continue
            # When review is required, gate on passing + fresh verdict.
            # NOTE: ``run_once_with_review`` is the primary enforcement
            # point for review gating AND needs_changes kickbacks. This
            # check is defense-in-depth for the legacy ``run_once``
            # callers that do NOT go through review orchestration. Do
            # NOT add a kickback here — that's #328's intended contract.
            if self.require_review:
                passed, _reason = self.check_review_verdict(task)
                if not passed:
                    _emit("merge_skipped", task_id, "reason=needs_changes")
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
        task_id = task.get("id", "")
        attempt_id = task.get("current_attempt") or ""
        branch = self._get_attempt_branch(task)
        if not branch:
            self.last_failure_reason = "no branch on current attempt"
            # Fire merge_attempted so operators see soldier evaluated this
            # task; then merge_failed with the normalized reason code.
            _emit("merge_attempted", task_id, f"attempt={attempt_id} branch=")
            _emit("merge_started", task_id, "")  # dual-emit for 0.6.x back-compat
            _emit(
                "merge_failed",
                task_id,
                f"reason=no_pr: {self.last_failure_reason}",
            )
            return MergeResult.FAILED

        _set_activity("merging", task_id)
        # Canonical diagnostic event (new in 0.6.7).
        attempt_detail = f"attempt={attempt_id} branch={branch}"
        _emit("merge_attempted", task_id, attempt_detail)
        # Dual-emit legacy event for 0.6.x back-compat. TODO: remove in 0.7.0.
        _emit("merge_started", task_id, branch)

        # Pre-flight: refuse to proceed on a dirty clone. Issue #311 —
        # previously ``_cleanup`` silently swallowed git failures, which
        # could leave the soldier stuck on ``antfarm/temp-merge`` with
        # every subsequent merge failing noisily (cascading kickback
        # storm). Assert the clone is clean, and if not, attempt a
        # destructive recovery before giving up on this tick.
        if not self._assert_clean_repo():
            _emit("repo_dirty", task_id, "preflight=fail attempting=recover")
            if not self._force_clean_repo():
                self.last_failure_reason = "repo not in clean state and recovery failed"
                _emit(
                    "merge_failed",
                    task_id,
                    f"reason=repo_dirty: {self.last_failure_reason}",
                )
                return MergeResult.FAILED

        temp_branch = "antfarm/temp-merge"
        try:
            # Fetch latest state from origin
            _set_activity("fetching", "origin")
            r = subprocess.run(
                ["git", "fetch", "origin"],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                self.last_failure_reason = f"git fetch failed: {r.stderr.decode().strip()}"
                _emit(
                    "merge_failed",
                    task_id,
                    f"reason=fetch_failed: {self.last_failure_reason}",
                )
                return MergeResult.FAILED

            # Create temp branch from integration branch
            _set_activity("rebasing", task_id)
            r = self._checkout_with_reclaim(
                [
                    "checkout",
                    "-b",
                    temp_branch,
                    f"origin/{self.integration_branch}",
                ]
            )
            if r.returncode != 0:
                self.last_failure_reason = (
                    f"could not create temp branch: {r.stderr.decode().strip()}"
                )
                _emit(
                    "merge_failed",
                    task_id,
                    f"reason=checkout_failed: {self.last_failure_reason}",
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
                # Merge conflict: try one deterministic rebase of the PR
                # branch onto origin/<integration_branch> and retry the merge
                # exactly once. Only merge-conflict failures trigger this
                # path — test failures still kick back immediately. The
                # helper is responsible for emitting the final ``merge_failed``
                # diagnostic event (with reason=rebase_failed or
                # reason=rebase_retry_merge_failed) on its failure paths.
                initial_conflict_stderr = r.stderr.decode().strip()
                return self._rebase_and_retry_merge(
                    task_id=task_id,
                    branch=branch,
                    temp_branch=temp_branch,
                    initial_conflict_stderr=initial_conflict_stderr,
                )

            # Run tests
            _set_activity("running_tests", task_id)
            r = subprocess.run(
                self.test_command,
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                stderr_tail = _stderr_tail(r.stderr or b"")
                short = (
                    r.stdout.decode(errors="replace").strip()
                    + " "
                    + r.stderr.decode(errors="replace").strip()
                ).strip()
                self.last_failure_reason = f"tests failed: {short[:120]}\n---\n{stderr_tail}\n---"
                _emit(
                    "merge_failed",
                    task_id,
                    f"reason=test_failed: {self.last_failure_reason}",
                )
                return MergeResult.FAILED

            # Fast-forward integration branch
            _set_activity("fast_forwarding", self.integration_branch)
            r = self._checkout_with_reclaim(["checkout", self.integration_branch])
            if r.returncode != 0:
                self.last_failure_reason = (
                    f"could not checkout {self.integration_branch}: {r.stderr.decode().strip()}"
                )
                _emit(
                    "merge_failed",
                    task_id,
                    f"reason=checkout_failed: {self.last_failure_reason}",
                )
                return MergeResult.FAILED

            r = subprocess.run(
                ["git", "merge", "--ff-only", temp_branch],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                self.last_failure_reason = f"ff-only merge failed: {r.stderr.decode().strip()}"
                _emit(
                    "merge_failed",
                    task_id,
                    f"reason=rebase_failed: {self.last_failure_reason}",
                )
                return MergeResult.FAILED

            # Push to origin
            _set_activity("pushing", self.integration_branch)
            r = subprocess.run(
                ["git", "push", "origin", self.integration_branch],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                self.last_failure_reason = f"push failed: {r.stderr.decode().strip()}"
                _emit(
                    "merge_failed",
                    task_id,
                    f"reason=push_failed: {self.last_failure_reason}",
                )
                return MergeResult.FAILED

            _emit("merge_succeeded", task_id, branch)
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

    def _wait_for_event(self, timeout: float) -> bool:
        """Wait on the colony's /events SSE stream for a wake event.

        Opens a streaming GET against ``/events?after=<cursor>&timeout=<timeout>``
        and consumes SSE ``data:`` lines. Returns early as soon as an event with
        ``type`` in :data:`WAKE_EVENT_TYPES` arrives. Other event types advance
        the cursor but do not cause an early return.

        Args:
            timeout: Maximum seconds to wait for a wake event. Matches the
                colony server's ``timeout`` query contract.

        Returns:
            True if woken by a relevant event; False on timeout or fallback.

        Fallback behaviour:
            If this Soldier was constructed via :meth:`from_backend` (in-process;
            ``colony_url`` is empty) the SSE path is skipped and the method
            does a plain ``time.sleep(timeout)``. On any connection, read, or
            parse error the method logs a warning, does ``time.sleep(timeout)``,
            and returns False. The polling path is always available as a
            safety net.
        """
        # In-process Soldier (from_backend) has no meaningful colony_url.
        # Skip SSE entirely and preserve the original polling behaviour.
        if not self.colony_url:
            time.sleep(timeout)
            return False

        url = f"{self.colony_url.rstrip('/')}/events"
        params = {"after": self._event_cursor, "timeout": timeout}
        # Read timeout slightly larger than server-side timeout so the server
        # closes the stream first (graceful), not the client.
        client_timeout = httpx.Timeout(timeout + 5.0, connect=5.0)
        try:
            with (
                httpx.Client(timeout=client_timeout) as client,
                client.stream("GET", url, params=params) as resp,
            ):
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if not payload:
                        continue
                    event = json.loads(payload)
                    event_id = event.get("id", 0)
                    if isinstance(event_id, int) and event_id > self._event_cursor:
                        self._event_cursor = event_id
                    if event.get("type") in WAKE_EVENT_TYPES:
                        return True
                # Stream closed cleanly (server-side timeout). Act as a tick.
                return False
        except Exception as exc:
            logger.warning("soldier event wait failed (%s); falling back to poll sleep", exc)
            time.sleep(timeout)
            return False

    def _rebase_and_retry_merge(
        self,
        task_id: str,
        branch: str,
        temp_branch: str,
        initial_conflict_stderr: str,
    ) -> MergeResult:
        """Rebase ``branch`` onto origin/integration and retry the merge once.

        Called from ``attempt_merge`` when the initial ``git merge --no-ff``
        conflicts. Performs at MOST one rebase and one retry merge — no loops.

        Flow:
        1. Abort the in-progress conflicting merge so the temp branch is clean.
        2. ``git fetch origin`` — pick up latest integration tip.
        3. ``git checkout <branch>`` and
           ``git rebase origin/<integration_branch>``.
        4. If rebase conflicts: ``git rebase --abort``, set
           ``last_failure_reason`` to ``rebase_failed: ...`` and return FAILED.
        5. On clean rebase: ``git push --force-with-lease origin <branch>``
           (never plain ``--force``), then recreate the temp branch from
           origin/<integration_branch> and retry ``git merge --no-ff``.
        6. If the retry merge succeeds, continue with tests / ff / push via
           the rest of the pipeline; if it fails again, return FAILED with
           ``rebase_retry_merge_failed: ...``.

        Returns a definitive ``MergeResult`` — the caller must not re-attempt.
        """
        del initial_conflict_stderr  # captured for debugging; reason set below.
        _set_activity("rebasing", f"{task_id} onto {self.integration_branch}")

        # 1) Abort the conflicting merge so we can move off temp_branch cleanly.
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )

        # 2) Fetch again to get the very latest integration tip before rebasing.
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )

        # 3) Check out the PR branch locally. Use -B so we reset any existing
        # local branch to origin/<branch>, avoiding stale local state.
        # If the branch is currently checked out by an antfarm-managed
        # worktree (e.g. a stale workspace left behind after a crash — see
        # #349), git refuses the checkout with
        # ``is already used by worktree at '<path>'``. Reclaim that single
        # worktree and retry the checkout exactly once.
        r = self._checkout_with_reclaim(["checkout", "-B", branch, f"origin/{branch}"])
        if r.returncode != 0:
            self.last_failure_reason = (
                f"rebase_failed: cannot checkout {branch}: {r.stderr.decode().strip()}"
            )
            _emit("merge_failed", task_id, f"reason={self.last_failure_reason}")
            return MergeResult.FAILED

        # 4) Rebase onto latest integration branch.
        r = subprocess.run(
            ["git", "rebase", f"origin/{self.integration_branch}"],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        if r.returncode != 0:
            rebase_stderr = r.stderr.decode().strip()
            # Best-effort abort; suppress any exceptions.
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["git", "rebase", "--abort"],
                    cwd=self.repo_path,
                    capture_output=True,
                    check=False,
                )
            self.last_failure_reason = f"rebase_failed: {rebase_stderr}"
            _emit("merge_failed", task_id, f"reason={self.last_failure_reason}")
            return MergeResult.FAILED

        # 5) Push rebased branch with --force-with-lease (never plain --force).
        r = subprocess.run(
            ["git", "push", "--force-with-lease", "origin", branch],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        if r.returncode != 0:
            self.last_failure_reason = (
                f"rebase_retry_merge_failed: push --force-with-lease failed: "
                f"{r.stderr.decode().strip()}"
            )
            _emit("merge_failed", task_id, f"reason={self.last_failure_reason}")
            return MergeResult.FAILED

        # 6) Recreate the temp branch from latest origin/<integration_branch>.
        # Delete any local temp branch first so checkout -b won't fail.
        self._checkout_with_reclaim(["checkout", self.integration_branch])
        subprocess.run(
            ["git", "branch", "-D", temp_branch],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        r = self._checkout_with_reclaim(
            ["checkout", "-b", temp_branch, f"origin/{self.integration_branch}"]
        )
        if r.returncode != 0:
            self.last_failure_reason = (
                f"rebase_retry_merge_failed: could not recreate temp branch: "
                f"{r.stderr.decode().strip()}"
            )
            _emit("merge_failed", task_id, f"reason={self.last_failure_reason}")
            return MergeResult.FAILED

        # 7) Retry the merge exactly once — no further rebase.
        r = subprocess.run(
            ["git", "merge", "--no-ff", branch],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        if r.returncode != 0:
            self.last_failure_reason = f"rebase_retry_merge_failed: {r.stderr.decode().strip()}"
            _emit("merge_failed", task_id, f"reason={self.last_failure_reason}")
            return MergeResult.FAILED

        # 8) Run tests.
        r = subprocess.run(
            self.test_command,
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        if r.returncode != 0:
            stderr_tail = _stderr_tail(r.stderr or b"")
            short = (
                r.stdout.decode(errors="replace").strip()
                + " "
                + r.stderr.decode(errors="replace").strip()
            ).strip()
            self.last_failure_reason = f"tests failed: {short[:120]}\n---\n{stderr_tail}\n---"
            _emit(
                "merge_failed",
                task_id,
                f"reason=test_failed: {self.last_failure_reason}",
            )
            return MergeResult.FAILED

        # 9) Fast-forward integration branch.
        r = self._checkout_with_reclaim(["checkout", self.integration_branch])
        if r.returncode != 0:
            self.last_failure_reason = (
                f"could not checkout {self.integration_branch}: {r.stderr.decode().strip()}"
            )
            _emit(
                "merge_failed",
                task_id,
                f"reason=checkout_failed: {self.last_failure_reason}",
            )
            return MergeResult.FAILED

        r = subprocess.run(
            ["git", "merge", "--ff-only", temp_branch],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        if r.returncode != 0:
            self.last_failure_reason = f"ff-only merge failed: {r.stderr.decode().strip()}"
            _emit(
                "merge_failed",
                task_id,
                f"reason=rebase_failed: {self.last_failure_reason}",
            )
            return MergeResult.FAILED

        # 10) Push integration branch to origin.
        r = subprocess.run(
            ["git", "push", "origin", self.integration_branch],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        if r.returncode != 0:
            self.last_failure_reason = f"push failed: {r.stderr.decode().strip()}"
            _emit(
                "merge_failed",
                task_id,
                f"reason=push_failed: {self.last_failure_reason}",
            )
            return MergeResult.FAILED

        _emit("merge_succeeded", task_id, branch)
        return MergeResult.MERGED

    def _assert_clean_repo(self) -> bool:
        """Read-only check that the soldier clone is in a pristine state.

        Returns True iff all three are simultaneously true:
        1. HEAD is attached and points at ``self.integration_branch``.
        2. Tracked files are clean (``git diff-index --quiet HEAD``).
        3. No ``antfarm/temp-merge`` branch exists.

        Untracked files are intentionally ignored: the soldier repo commonly
        carries untracked artifacts (e.g. ``.claude/`` worktrees, test
        scratch) that are orthogonal to merge safety. ``git status
        --porcelain`` treats these as "dirty" and produced spurious
        ``cleanup_incomplete`` events during #338 dogfood; the tracked-state
        check via ``git diff-index`` is the correct primitive here.

        Any subprocess error is treated as "not clean" — the caller is
        expected to recover via ``_force_clean_repo``. Never raises.
        """
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                return False
            head = r.stdout.decode().strip()
            if head != self.integration_branch:
                return False

            # diff-index --quiet exits 0 when tracked files are clean against
            # HEAD, 1 when they have modifications. Untracked files are
            # invisible to this check — that is the point.
            r = subprocess.run(
                ["git", "diff-index", "--quiet", "HEAD", "--"],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                return False

            r = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", "antfarm/temp-merge"],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            # Non-zero = branch does NOT exist = good. Zero = branch
            # exists = dirty state.
            return r.returncode != 0
        except OSError:
            return False

    def _preflight_test_command(self) -> tuple[bool, str]:
        """Run ``test_command`` once on a clean integration branch.

        Returns:
            Tuple of (passed, stderr_tail). ``passed`` is True if the command
            exits 0 (or is empty). ``stderr_tail`` is the last 20 lines of
            stderr on failure, or an exec-failure message if the command
            could not be spawned.

        See #326: catches operator mistakes like a missing ``test_command``
        binary or a broken default ``pytest`` install before the first real
        task fails. Fires at most once per Soldier lifetime.
        """
        if not self.test_command:
            return True, ""
        try:
            r = subprocess.run(
                self.test_command,
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
        except (OSError, FileNotFoundError) as exc:
            return False, f"could not exec {self.test_command!r}: {exc}"
        if r.returncode == 0:
            return True, ""
        return False, _stderr_tail(r.stderr or b"") or _stderr_tail(r.stdout or b"")

    def _run_preflight_if_needed(self) -> None:
        """One-shot preflight hook invoked by ``run`` and ``run_once``.

        Sets ``_preflight_done=True`` BEFORE performing any work so that a
        mid-preflight crash cannot cause an infinite retry loop on the next
        lifetime. Skipped when ``_force_clean_repo`` cannot establish a
        clean integration branch — a separate failure path already logs
        that condition.
        """
        if self._preflight_done:
            return
        self._preflight_done = True
        if not self._force_clean_repo():
            return
        passed, tail = self._preflight_test_command()
        if passed:
            return
        logger.warning("soldier preflight: test_command failed:\n%s", tail)
        _emit(
            "test_command_broken",
            "",
            f"cmd={' '.join(self.test_command)} stderr=\n{tail}",
        )

    def _checkout_with_reclaim(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run ``git <args>`` once; on a worktree-collision failure, reclaim
        the blocking worktree via ``_remove_blocking_worktree`` and retry the
        same command exactly once. Returns the final CompletedProcess.

        This is the single chokepoint for every checkout Soldier performs in
        the shared clone — a stale antfarm-managed worktree holding the
        target branch would otherwise fail the checkout with ``is already
        used by worktree at '<path>'`` (#352). Callers emit their own
        diagnostic events; this helper only runs git and swallows nothing.
        """
        r = subprocess.run(
            ["git"] + args,
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        if r.returncode == 0:
            return r
        stderr = r.stderr.decode(errors="replace") if r.stderr else ""
        if "is already used by worktree" not in stderr:
            return r
        if not self._remove_blocking_worktree(stderr):
            return r
        return subprocess.run(
            ["git"] + args,
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )

    def _delete_local_branch_with_reclaim(self, branch: str) -> bool:
        """Delete a local branch, reclaiming any blocking antfarm worktree.

        Runs ``git branch -D <branch>`` in the soldier clone. If the delete
        fails because the branch is checked out in an antfarm-managed
        worktree (``used by worktree at '<path>'``), invokes
        ``_remove_blocking_worktree`` and retries exactly once.

        Best-effort: returns True iff the branch is gone after this call.
        Callers use the return value for logging only — a False result must
        never abort the surrounding merge-success flow (#360).
        """
        r = subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        if r.returncode == 0:
            return True
        stderr = r.stderr.decode(errors="replace") if r.stderr else ""
        if "used by worktree at" not in stderr:
            return False
        if not self._remove_blocking_worktree(stderr):
            return False
        retry = subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        return retry.returncode == 0

    def _remove_blocking_worktree(self, stderr: str) -> bool:
        """Parse a 'branch already used by worktree at <path>' stderr message
        and, if the path is an antfarm-managed worktree, remove it with
        ``git worktree remove --force``. Returns True iff a worktree was
        successfully removed (caller should retry).

        Safety: the parsed path is resolved with ``os.path.realpath`` and
        must lie strictly under ``{repo_path}/.antfarm/workspaces/``. Any
        path outside that tree (including symlinks that resolve outside,
        and the workspaces root itself) is refused without invoking
        ``git worktree remove``. See #349.
        """
        match = re.search(r"used by worktree at '([^']+)'", stderr)
        if not match:
            return False

        parsed = match.group(1)
        antfarm_prefix = os.path.realpath(os.path.join(self.repo_path, ".antfarm", "workspaces"))
        if os.path.isabs(parsed):
            candidate = os.path.realpath(parsed)
        else:
            candidate = os.path.realpath(os.path.join(self.repo_path, parsed))

        if not candidate.startswith(antfarm_prefix + os.sep):
            logger.warning(
                "soldier _remove_blocking_worktree: refusing to remove worktree "
                "outside antfarm workspaces: parsed=%r resolved=%r prefix=%r",
                parsed,
                candidate,
                antfarm_prefix,
            )
            return False

        r = subprocess.run(
            ["git", "worktree", "remove", "--force", candidate],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        if r.returncode != 0:
            logger.warning(
                "soldier _remove_blocking_worktree: git worktree remove failed for %r: %s",
                candidate,
                r.stderr.decode(errors="replace").strip(),
            )
            return False

        _emit("worktree_reclaimed", "", f"path={candidate}")
        return True

    def _force_clean_repo(self) -> bool:
        """Destructive recovery routine to restore a pristine clone.

        DESTRUCTIVE: discards uncommitted work in the soldier clone. The
        soldier clone is dedicated — soldier itself never commits there
        (it only merges existing branches and pushes), so normal operation
        is safe. Operators manually debugging in the soldier clone WILL
        lose their changes when this routine runs.

        Sequence (each step aborts recovery on failure, except the
        terminal temp-branch delete which is best-effort):

        1. ``git reset --hard HEAD`` — clean tree regardless of current branch.
        2. ``git checkout <integration_branch>`` — reattach detached HEAD.
        3. ``git reset --hard origin/<integration_branch>`` — align to remote.
           If the origin ref is missing (no fetch yet, or disconnected),
           fall back silently to ``git reset --hard HEAD`` so the routine
           still completes instead of failing hard.
        4. ``git clean -fd -e <data_dir_name>`` — remove untracked files/dirs.
           NOTE: intentionally NOT ``-fdx`` — we preserve ignored paths like
           ``.venv``. The ``-e`` excludes the colony's data dir (default
           ``.antfarm``) so target repos without a matching ``.gitignore``
           entry don't lose runtime state mid-mission (#323).
        5. ``git branch -D antfarm/temp-merge`` — best-effort; tolerated if
           the branch does not exist.

        Returns True on full success, False if any mandatory step failed.
        Never raises.
        """
        try:
            subprocess.run(
                ["git", "reset", "--hard", "HEAD"],
                cwd=self.repo_path,
                capture_output=True,
                check=True,
            )
            # Checkout wrapped with reclaim so a stale antfarm-managed
            # worktree holding the integration branch cannot perma-break
            # recovery (#352). If reclaim+retry still fails, return False.
            r = self._checkout_with_reclaim(["checkout", self.integration_branch])
            if r.returncode != 0:
                return False
            try:
                subprocess.run(
                    ["git", "reset", "--hard", f"origin/{self.integration_branch}"],
                    cwd=self.repo_path,
                    capture_output=True,
                    check=True,
                )
            except subprocess.CalledProcessError:
                # Origin ref missing (no fetch yet, or disconnected). Fall
                # back to HEAD so recovery still completes.
                subprocess.run(
                    ["git", "reset", "--hard", "HEAD"],
                    cwd=self.repo_path,
                    capture_output=True,
                    check=True,
                )
            subprocess.run(
                ["git", "clean", "-fd", "-e", self.data_dir_name],
                cwd=self.repo_path,
                capture_output=True,
                check=True,
            )
        except (subprocess.CalledProcessError, OSError):
            return False

        # Best-effort: temp branch may or may not exist.
        with contextlib.suppress(OSError):
            subprocess.run(
                ["git", "branch", "-D", "antfarm/temp-merge"],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )

        return True

    def _cleanup(self) -> None:
        """Restore repo to a clean state after a merge attempt (success or failure).

        Must be bulletproof — called in finally blocks. All commands use
        check=False so failures don't cascade and this method never
        raises (the ``finally`` contract).

        Ordering (#311): ``git reset --hard HEAD`` runs FIRST so any
        conflict markers or partial merge state are discarded before we
        try to change branches. Historically ``git merge --abort`` ran
        first, which is a no-op outside an in-progress merge and did
        nothing to rescue a dirty working tree after a partial rebase
        or a failed checkout mid-loop. With the hard reset up front the
        subsequent ``checkout`` reliably succeeds.

        After the five commands run we re-assert the invariant with
        ``_assert_clean_repo``. On failure we emit ``cleanup_incomplete``
        to the SSE bus and log at ERROR — but we do NOT raise, because
        this runs inside a ``finally`` block and raising would shadow the
        original merge exception.

        Invariant after cleanup (on success):
        - On integration_branch
        - No temp branch
        - Clean working tree matching origin/{integration_branch}
        """
        _set_activity("cleanup", "")
        # 1) Hard reset first — wipes conflict state / partial merge.
        subprocess.run(
            ["git", "reset", "--hard", "HEAD"],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        # 2) Abort any in-progress merge (now mostly a no-op; still
        #    tolerated for belt-and-braces on older git versions).
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        # 3) Return to integration branch.
        # NOTE: intentionally NOT wrapped with _checkout_with_reclaim (#352).
        # _cleanup runs inside a finally block and must never raise or propagate
        # failures; reclaim emits SSE events and calls git worktree remove,
        # either of which could fail and mask the original exception. Leaving
        # this as a best-effort direct subprocess.run keeps the finally contract
        # intact. If a stale worktree blocks the checkout here, the next
        # attempt_merge tick's wrapped checkout will reclaim it.
        subprocess.run(
            ["git", "checkout", self.integration_branch],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        # 4) Delete temp branch.
        subprocess.run(
            ["git", "branch", "-D", "antfarm/temp-merge"],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        # 5) Remove untracked files and directories. -e excludes the colony's
        # data dir so target repos without a matching .gitignore entry don't
        # lose runtime state (#323).
        subprocess.run(
            ["git", "clean", "-fd", "-e", self.data_dir_name],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )
        # 6) Hard reset to remote integration branch.
        subprocess.run(
            ["git", "reset", "--hard", f"origin/{self.integration_branch}"],
            cwd=self.repo_path,
            capture_output=True,
            check=False,
        )

        # 7) Post-check: emit diagnostic if we failed to reach clean state.
        #    Never raise — ``finally`` contract.
        if not self._assert_clean_repo():
            try:
                branch_r = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=self.repo_path,
                    capture_output=True,
                    check=False,
                )
                head = branch_r.stdout.decode().strip() if branch_r.returncode == 0 else "?"
            except OSError:
                head = "?"
            try:
                status_r = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=self.repo_path,
                    capture_output=True,
                    check=False,
                )
                dirty = (
                    "yes" if status_r.returncode != 0 or status_r.stdout.decode().strip() else "no"
                )
            except OSError:
                dirty = "yes"
            detail = f"branch={head} dirty={dirty}"
            _emit("cleanup_incomplete", "", detail)
            logger.error("soldier cleanup incomplete: %s", detail)
        _set_activity("idle", "")

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
    def _get_attempt_pr(task: dict) -> str | None:
        """Extract the PR URL/identifier from the task's current attempt."""
        current_attempt_id = task.get("current_attempt")
        if not current_attempt_id:
            return None
        for attempt in task.get("attempts", []):
            if attempt.get("attempt_id") == current_attempt_id:
                return attempt.get("pr") or None
        return None

    def _check_pr_merged_on_origin(self, pr: str) -> bool | None:
        """Check whether a PR has been merged on the origin (GitHub).

        Shells out to ``gh pr view <pr> --json state -q '.state'``.

        Returns:
            True  — PR state is "MERGED"
            False — PR state is "OPEN" or "CLOSED" (not merged)
            None  — unable to determine (gh missing, network error, timeout,
                    unexpected output). Callers should fall through to the
                    normal merge path.
        """
        if not pr:
            return None
        try:
            result = subprocess.run(
                ["gh", "pr", "view", pr, "--json", "state", "-q", ".state"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        state = (result.stdout or "").strip()
        if state == "MERGED":
            return True
        if state in ("OPEN", "CLOSED"):
            return False
        return None

    def _safe_mark_merged(self, task_id: str, expected_attempt_id: str) -> bool:
        """Re-fetch task and mark_merged only if expected_attempt_id is still current.

        This guards against attempt drift — a concurrent doctor ``--fix``
        kickback + worker re-harvest can rotate ``current_attempt`` to a new
        ID between the moment Soldier captured the merge queue and the moment
        it calls ``mark_merged``. Backend now validates currency, but we also
        skip here with a diagnostic event so the drifted attempt's new cycle
        can own its own merge decision.

        Returns True on success; False if the attempt drifted (emits
        ``merge_skipped`` with ``reason=attempt_drift``) or the task
        disappeared.
        """
        fresh = self.colony.get_task(task_id)
        current = (fresh or {}).get("current_attempt")
        if current != expected_attempt_id:
            logger.warning(
                "skipping mark_merged for %s: attempt drift "
                "(expected=%s current=%s); new attempt will own its merge cycle",
                task_id,
                expected_attempt_id,
                current,
            )
            _emit("merge_skipped", task_id, "reason=attempt_drift")
            return False
        self.colony.mark_merged(task_id, expected_attempt_id)
        return True

    def _reconcile_external_merge(self, task: dict) -> bool:
        """Mark the task as merged if its PR was merged on origin.

        Returns True if the attempt was reconciled (mark_merged called or
        already-merged). Returns False if no reconciliation happened and the
        caller should fall through to the normal merge path.
        """
        if not self.poll_external_merges:
            return False
        pr = self._get_attempt_pr(task)
        if not pr:
            return False
        merged = self._check_pr_merged_on_origin(pr)
        if merged is not True:
            return False
        task_id = task["id"]
        attempt_id = task["current_attempt"]
        # ValueError means already merged or drifted — idempotent no-op.
        # ``_safe_mark_merged`` already handles drift by emitting merge_skipped
        # and returning False without raising; the suppress remains as defense
        # for a rare case where the fresh task still has the current attempt
        # but the backend write races with a kickback (very narrow window).
        with contextlib.suppress(ValueError):
            self._safe_mark_merged(task_id, attempt_id)
        logger.info("reconciled externally-merged PR %s for %s", pr, task_id)
        _emit("reconciled_external", task_id, f"pr={pr}")
        return True

    def _reconcile_external_merges(self, all_tasks: list[dict]) -> int:
        """Mission-wide reconciliation pass at the top of every soldier poll.

        For every done, non-infra task with an unmerged current attempt and a
        PR URL, query ``gh pr view`` to detect cases where the PR was merged
        externally (or by a prior auto-merge that succeeded on origin but
        whose antfarm-side ``mark_merged`` failed silently — see #367).

        A per-task TTL cache (``self._reconcile_last_checked``) gates how
        often each task is polled. The default backoff is 60s.

        Returns the number of attempts that were reconciled this pass.
        Returns 0 when ``poll_external_merges`` is disabled.

        This pass MUST run before ``get_merge_queue`` / ``_get_done_candidates``
        so an upstream desync is corrected before downstream dep_unmerged
        filtering blocks dependent tasks.
        """
        if not self.poll_external_merges:
            return 0

        reconciled = 0
        now = time.time()
        cutoff = now - self.reconcile_backoff_seconds

        for task in all_tasks:
            if is_infra_task(task):
                continue
            if task.get("status") != "done":
                continue
            if self._has_merged_attempt(task):
                continue
            attempt_id = task.get("current_attempt")
            if not attempt_id:
                continue
            pr = self._get_attempt_pr(task)
            if not pr:
                continue
            task_id = task.get("id", "")
            last_checked = self._reconcile_last_checked.get(task_id)
            if last_checked is not None and last_checked > cutoff:
                continue

            merged = self._check_pr_merged_on_origin(pr)
            # Update cache regardless of result so a transient gh outage or an
            # OPEN state doesn't hammer the API every tick.
            self._reconcile_last_checked[task_id] = now
            if merged is not True:
                continue

            logger.info(
                "reconcile pass: PR %s for task %s is MERGED on origin; marking attempt %s",
                pr,
                task_id,
                attempt_id,
            )
            try:
                marked = self._safe_mark_merged(task_id, attempt_id)
            except ValueError as exc:
                logger.warning(
                    "reconcile pass: mark_merged ValueError task=%s attempt=%s err=%s",
                    task_id,
                    attempt_id,
                    exc,
                )
                continue
            if marked:
                _emit(
                    "reconciled_external",
                    task_id,
                    f"pr={pr} source=poll_pass",
                )
                reconciled += 1
        return reconciled

    @staticmethod
    def _has_merged_attempt(task: dict) -> bool:
        """Return True if the task has at least one attempt with status MERGED."""
        return any(attempt.get("status") == "merged" for attempt in task.get("attempts", []))

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

        target_branch = artifact_dict.get("target_branch", self.integration_branch)
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
                return line[len(marker) :].strip()
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

    @staticmethod
    def _find_prior_pass_verdict(task: dict, prior_sha: str) -> dict | None:
        """Return a prior attempt's pass verdict, or None.

        Scans ``task['attempts']`` for attempts other than the current one
        whose stored ``review_verdict`` has ``verdict == 'pass'``. Prefers
        a verdict whose ``reviewed_commit_sha`` matches ``prior_sha``
        (the SHA embedded in the stale review task's spec), so the
        fast-path only carries forward a verdict we are confident was
        recorded against the old attempt's code. If no SHA-matched pass
        verdict exists, returns None — the caller falls through to the
        safe re-review path.
        """
        current_attempt_id = task.get("current_attempt")
        best: dict | None = None
        for a in task.get("attempts", []):
            if a.get("attempt_id") == current_attempt_id:
                continue
            v = a.get("review_verdict")
            if not v or v.get("verdict") != "pass":
                continue
            reviewed_sha = v.get("reviewed_commit_sha") or ""
            if prior_sha and reviewed_sha and Soldier._sha_match(reviewed_sha, prior_sha):
                return v
            if best is None:
                best = v
        # Only return a non-SHA-matched verdict if we had no SHA to check
        # against at all — otherwise stay safe and re-review.
        if not prior_sha:
            return best
        return None

    def _diffs_equivalent_after_rebase(
        self, task: dict, existing_sha: str, current_sha: str
    ) -> bool:
        """Check whether two attempts produce byte-identical non-test diffs.

        Computes ``git diff <merge_base>..<head> --ignore-all-space`` for
        both ``existing_sha`` and ``current_sha`` against
        ``origin/<integration_branch>``. The pathspec excludes only
        pytest-discovery files (``test_*.py`` and ``*_test.py``).
        ``conftest.py``, test fixtures, and test config are INCLUDED —
        changes to test infrastructure must trigger a re-review (fixes
        #304). Returns True iff both ``git merge-base`` + ``git diff``
        invocations succeed AND both stdouts are byte-identical.

        Any subprocess error, non-zero return code, or empty merge-base
        output causes this method to return False (safe default → the
        caller re-reviews).
        """
        del task  # reserved for future use; repo_path is all we need today
        if not existing_sha or not current_sha:
            return False

        try:
            diff_old = self._diff_against_merge_base(existing_sha)
            diff_new = self._diff_against_merge_base(current_sha)
        except Exception:
            logger.debug(
                "diff-equivalence check raised; falling through to re-review",
                exc_info=True,
            )
            return False

        if diff_old is None or diff_new is None:
            return False
        return diff_old == diff_new

    def _diff_against_merge_base(self, sha: str) -> str | None:
        """Return ``git diff <merge-base>..<sha>`` (non-test paths) or None.

        Returns None on ANY subprocess error (caller treats as non-equiv).
        The ``--ignore-all-space`` flag is applied at the ``git diff``
        layer (not via post-processing) so identical-after-whitespace
        rebases are recognized as pure-rebase reharvests.

        Pathspec excludes only pytest-discovery files (``test_*.py`` and
        ``*_test.py``). ``conftest.py``, fixtures, and data under
        ``tests/`` are INCLUDED — test infrastructure changes must
        trigger a re-review (fixes #304).
        """
        mb = subprocess.run(
            ["git", "merge-base", sha, f"origin/{self.integration_branch}"],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if mb.returncode != 0:
            return None
        merge_base = mb.stdout.strip()
        if not merge_base:
            return None

        # conftest.py etc. are test infrastructure — always re-review (fixes #304)
        diff = subprocess.run(
            [
                "git",
                "diff",
                "--ignore-all-space",
                f"{merge_base}..{sha}",
                "--",
                ":!**/test_*.py",
                ":!**/*_test.py",
            ],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if diff.returncode != 0:
            return None
        return diff.stdout

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
            existing_sha = self._extract_attempt_sha_from_spec(existing.get("spec", ""))
            current_sha = self._current_attempt_sha(task)
            if not existing_sha:
                # Legacy review task predates the Attempt-SHA marker.
                # Don't bounce it — leave it to complete on its own terms.
                return None
            if not current_sha:
                # Pathological: parent attempt has neither head_commit_sha
                # nor branch. Can't safely decide — leave the review alone.
                logger.warning(
                    "skipping re-review for %s: parent task %s has no "
                    "head_commit_sha and no attempt branch",
                    review_task_id,
                    task_id,
                )
                return None
            if existing_sha == current_sha:
                # Already in flight or verdicted for this attempt
                return None
            # SHA mismatch (re-attempt) — re-ready the existing review task
            try:
                self.colony.rereview(review_task_id, spec, touches)
                logger.info(
                    "re-readied review task %s for new attempt (sha %s -> %s)",
                    review_task_id,
                    existing_sha[:12],
                    current_sha[:12],
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
    # Auto-merge (#353)
    # ------------------------------------------------------------------

    def _auto_merge_policy_for_task(self, task: dict) -> str:
        """Resolve the auto-merge mode for ``task`` via its parent mission.

        Returns the mode string — ``"never"`` when the task has no mission,
        the mission cannot be fetched, or the mission's config does not set
        the key.
        """
        mission_id = task.get("mission_id")
        if not mission_id:
            return "never"
        try:
            mission = self.colony.get_mission(mission_id)
        except Exception:
            logger.debug("auto-merge: get_mission(%s) raised", mission_id, exc_info=True)
            return "never"
        if not mission:
            return "never"
        config = mission.get("config") or {}
        return str(config.get("auto_merge") or "never")

    def _query_pr_state(self, pr: str):
        """Run ``gh pr view <pr> --json ...`` and parse the output.

        Returns a :class:`PRState` or None on any subprocess / parse error.
        """
        if not pr:
            return None
        try:
            r = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    pr,
                    "--json",
                    "mergeStateStatus,mergeable,reviewDecision,statusCheckRollup",
                ],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        return parse_pr_state(r.stdout or "")

    def _gh_pr_merge_squash(self, pr: str, branch: str | None = None) -> tuple[bool, str]:
        """Run ``gh pr merge <pr> --squash`` and clean up the branch separately.

        Auto-merge flow (#360): the squash-merge and the branch-delete are
        decoupled so a branch cleanup hiccup (local branch checked out in a
        stale antfarm worktree, or remote already gone) never poisons an
        already-successful remote merge. Cleanup is best-effort.

        Returns (ok, stderr_tail). Never raises.

        Behavior:
        - ``gh pr merge --squash`` returncode 0 → success; attempt branch
          cleanup, then return ``(True, "")`` regardless of cleanup outcome.
        - Non-zero gh exit → consult ``_check_pr_merged_on_origin`` because
          gh sometimes exits non-zero after the remote merge already
          landed (flaky follow-up calls, auth races). When the origin
          confirms MERGED, emit ``auto_merge_gh_nonzero_but_merged``,
          attempt branch cleanup, and return ``(True, "")``. Otherwise
          surface the failure normally.
        """
        try:
            r = subprocess.run(
                ["gh", "pr", "merge", pr, "--squash"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"gh pr merge exec error: {exc}"

        if r.returncode != 0:
            # gh non-zero exits can race with successful remote merges.
            # Re-check origin state before kicking the task back.
            tail = (r.stderr or r.stdout or "").strip().splitlines()
            stderr_tail = "\n".join(tail[-20:])
            merged = self._check_pr_merged_on_origin(pr)
            if merged is True:
                _emit(
                    "auto_merge_gh_nonzero_but_merged",
                    "",
                    f"pr={pr} stderr_tail={stderr_tail!r}",
                )
                self._cleanup_merged_branch(branch)
                return True, ""
            return False, stderr_tail

        self._cleanup_merged_branch(branch)
        return True, ""

    def _cleanup_merged_branch(self, branch: str | None) -> None:
        """Best-effort local + remote branch deletion after a squash-merge.

        Never raises. All subprocess errors (including ``TimeoutExpired``)
        are swallowed — by the time we get here the remote merge already
        landed and we must not let janitorial noise surface as a failure
        (#360).
        """
        if not branch:
            return
        try:
            self._delete_local_branch_with_reclaim(branch)
        except Exception:
            logger.debug(
                "auto-merge: local branch delete raised for %r",
                branch,
                exc_info=True,
            )
        try:
            subprocess.run(
                ["git", "push", "origin", "--delete", branch],
                cwd=self.repo_path,
                capture_output=True,
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.debug("auto-merge: remote branch delete timed out for %r", branch)
        except Exception:
            logger.debug(
                "auto-merge: remote branch delete raised for %r",
                branch,
                exc_info=True,
            )

    def _resolve_repo_slug(self) -> str | None:
        """Return ``owner/name`` for the repo we're operating on, or None."""
        try:
            r = subprocess.run(
                [
                    "gh",
                    "repo",
                    "view",
                    "--json",
                    "owner,name",
                    "-q",
                    '.owner.login + "/" + .name',
                ],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        slug = (r.stdout or "").strip()
        return slug or None

    def _query_viewer_permission(self) -> str | None:
        """Query viewer permission via ``gh repo view --json viewerPermission``."""
        try:
            r = subprocess.run(
                ["gh", "repo", "view", "--json", "viewerPermission", "-q", ".viewerPermission"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        perm = (r.stdout or "").strip()
        return perm or None

    def _auto_merge_security_check(
        self,
        task: dict,
        mission: dict,
    ) -> tuple[bool, str]:
        """Gate auto-merge on viewer permission + integration branch.

        Returns (True, "") to allow; (False, reason) to refuse. Caches
        viewer permission per-repo for 5 minutes.
        """
        del task  # kept in signature for symmetry with other hooks
        slug = self._resolve_repo_slug() or self.repo_path
        now = time.time()
        cached = self._repo_permission_cache.get(slug)
        if cached and cached[1] > now:
            perm = cached[0]
        else:
            perm = self._query_viewer_permission() or ""
            # Cache for 5 minutes regardless of success, so a transient gh
            # outage doesn't hammer the API on every tick.
            self._repo_permission_cache[slug] = (perm, now + 300.0)
        perm_upper = perm.upper()
        allow_external = bool(
            (mission.get("config") or {}).get("allow_auto_merge_on_external", False)
        )

        if perm_upper not in {"ADMIN", "MAINTAIN", "WRITE"}:
            if allow_external:
                return True, ""
            return False, f"insufficient repo permission: {perm or 'unknown'}"

        # Extra guard: auto-merge to main/master requires ADMIN unless operator
        # explicitly opted in via allow_auto_merge_on_external.
        if self.integration_branch in ("main", "master") and perm_upper != "ADMIN":
            if allow_external:
                return True, ""
            return (
                False,
                f"auto-merge to {self.integration_branch} requires ADMIN "
                f"(viewer={perm or 'unknown'})",
            )
        return True, ""

    def _sync_integration_branch_after_auto_merge(self) -> None:
        """Re-align the local integration branch with origin after auto-merge.

        GitHub squash-merged the PR on the remote; locally we still have the
        pre-merge tip. Fetch + hard-reset brings us back in sync without
        running any local tests (GitHub's branch protection already gated
        the merge). Best-effort; subprocess errors are swallowed.
        """
        try:
            subprocess.run(
                ["git", "fetch", "origin"],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["git", "checkout", self.integration_branch],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["git", "reset", "--hard", f"origin/{self.integration_branch}"],
                cwd=self.repo_path,
                capture_output=True,
                check=False,
            )
        except OSError:
            logger.debug(
                "auto-merge: post-merge sync raised for %s",
                self.integration_branch,
                exc_info=True,
            )

    def _rebase_pr_branch_for_auto_merge(self, task: dict, pr: str) -> None:
        """Rebase the PR's branch onto origin/<integration_branch>.

        Wraps the existing deterministic rebase helper. Any failure is
        logged and surfaces as an ``auto_merge_rebasing`` event with
        ``status=failed`` so the next tick re-evaluates.
        """
        branch = self._get_attempt_branch(task) or ""
        task_id = task.get("id", "")
        if not branch:
            _emit("auto_merge_rebasing", task_id, f"pr={pr} status=failed reason=no_branch")
            return
        # Reuse the deterministic helper's rebase core — it aborts on conflict
        # and returns FAILED without merging further. We don't care about the
        # eventual merge here; we just want the rebase/force-push side effect.
        outcome = self._rebase_and_retry_merge(
            task_id=task_id,
            branch=branch,
            temp_branch="antfarm/temp-merge",
            initial_conflict_stderr="auto-merge triggered rebase",
        )
        detail = f"pr={pr} branch={branch} result={outcome.value}"
        _emit("auto_merge_rebasing", task_id, detail)

    def _pause_mission_for_blocked_reviews(
        self,
        task: dict,
        pr: str,
        reason: str,
    ) -> None:
        """Mark the parent mission as BLOCKED with a human-readable reason."""
        task_id = task.get("id", "")
        mission_id = task.get("mission_id")
        if not mission_id:
            _emit("auto_merge_blocked", task_id, f"pr={pr} reason={reason} mission=none")
            return
        try:
            self.colony.update_mission(
                mission_id,
                {
                    "status": "blocked",
                    "auto_merge_pause_reason": reason,
                },
            )
        except Exception:
            logger.exception(
                "auto-merge: failed to pause mission %s for task %s", mission_id, task_id
            )
        _emit("auto_merge_blocked", task_id, f"pr={pr} reason={reason} mission={mission_id}")

    def _attempt_auto_merge(self, task: dict) -> AutoMergeOutcome | None:
        """Decide and perform auto-merge for ``task``.

        Returns:
            - None if auto-merge is disabled for this task (caller falls
              through to the legacy ``attempt_merge`` path).
            - AutoMergeOutcome (possibly with ``action='skip'``) otherwise.
              The caller (``_handle_auto_merge_outcome``) translates the
              outcome into the merge-pipeline result.
        """
        mode = self._auto_merge_policy_for_task(task)
        if mode == "never":
            return None

        task_id = task.get("id", "")
        pr = self._get_attempt_pr(task) or ""
        if not pr:
            return AutoMergeOutcome(action="skip", pr="", mode=mode, reason="no PR on attempt")

        # Security check before we make any destructive call or poll gh.
        mission = {}
        mission_id = task.get("mission_id")
        if mission_id:
            try:
                mission = self.colony.get_mission(mission_id) or {}
            except Exception:
                logger.debug(
                    "auto-merge: get_mission(%s) raised during security check",
                    mission_id,
                    exc_info=True,
                )
                mission = {}

        ok, refuse_reason = self._auto_merge_security_check(task, mission)
        if not ok:
            _emit("auto_merge_refused", task_id, f"pr={pr} reason={refuse_reason}")
            return AutoMergeOutcome(
                action="skip", pr=pr, mode=mode, reason=f"refused: {refuse_reason}"
            )

        # Backoff: don't re-poll the same PR within 30s.
        now = time.time()
        last = self._auto_merge_last_checked.get(pr, 0.0)
        if now - last < self.auto_merge_poll_backoff_seconds:
            return AutoMergeOutcome(
                action="skip", pr=pr, mode=mode, reason="within poll backoff window"
            )
        self._auto_merge_last_checked[pr] = now

        pr_state = self._query_pr_state(pr)
        outcome = decide(mode, verdict_passed=True, pr_state=pr_state, pr=pr)
        return outcome

    def _handle_auto_merge_outcome(
        self,
        outcome: AutoMergeOutcome,
        task: dict,
    ) -> MergeResult:
        """Translate an :class:`AutoMergeOutcome` into a :class:`MergeResult`.

        - ``merge``: invoke ``gh pr merge --squash`` and mark the attempt
          with ``auto_merged=True``. Returns MERGED on success, FAILED on
          error (caller kicks back through the normal pipeline).
        - ``rebase``: rebase the PR branch; signal NEEDS_REVIEW so the tick
          exits without touching the attempt's review verdict.
        - ``wait_ci`` / ``skip``: signal NEEDS_REVIEW — the PR stays done,
          we'll re-evaluate next tick.
        - ``kickback_ci``: kick back the task with an ``auto_merge_ci_failed``
          reason code and emit ``auto_merge_kickback``.
        - ``pause_mission``: pause the parent mission and signal NEEDS_REVIEW.
        """
        task_id = task.get("id", "")
        attempt_id = task.get("current_attempt") or ""

        if outcome.action == "merge":
            branch = self._get_attempt_branch(task)
            ok, stderr_tail = self._gh_pr_merge_squash(outcome.pr, branch=branch)
            if not ok:
                self.last_failure_reason = f"auto_merge: gh pr merge failed: {stderr_tail}"
                _emit(
                    "merge_failed",
                    task_id,
                    f"reason=unknown: auto_merge gh pr merge failed: {stderr_tail}",
                )
                return MergeResult.FAILED
            self._sync_integration_branch_after_auto_merge()
            _emit(
                "auto_merged",
                task_id,
                f"pr={outcome.pr} mode={outcome.mode} reason={outcome.reason}",
            )
            # #367: gh pr merge already succeeded on origin. Antfarm-side
            # mark_merged failures must NOT cause us to retry the merge or
            # report FAILED — the next reconciliation pass will catch the
            # drift and converge state. Tighten the exception net so a
            # ValueError (the actual symptom in #367) is logged + emitted
            # instead of bubbling out of the auto-merge tick.
            try:
                self.colony.mark_merged(task_id, attempt_id, auto_merged=True)
                logger.info(
                    "auto_merge: mark_merged ok task=%s attempt=%s pr=%s",
                    task_id,
                    attempt_id,
                    outcome.pr,
                )
            except TypeError:
                # Older colony/backends may not accept the kwarg — degrade gracefully.
                self.colony.mark_merged(task_id, attempt_id)
                logger.info(
                    "auto_merge: mark_merged ok (no auto_merged kwarg) task=%s attempt=%s",
                    task_id,
                    attempt_id,
                )
            except ValueError as exc:
                logger.warning(
                    "auto_merge: mark_merged ValueError after successful gh pr merge "
                    "task=%s attempt=%s pr=%s err=%s — relying on reconciliation pass",
                    task_id,
                    attempt_id,
                    outcome.pr,
                    exc,
                )
                _emit(
                    "auto_merge_mark_drift",
                    task_id,
                    f"pr={outcome.pr} attempt={attempt_id} err={exc}",
                )
            except Exception as exc:
                logger.exception(
                    "auto_merge: mark_merged unexpected failure task=%s attempt=%s pr=%s",
                    task_id,
                    attempt_id,
                    outcome.pr,
                )
                _emit(
                    "auto_merge_mark_failed",
                    task_id,
                    f"pr={outcome.pr} attempt={attempt_id} type={type(exc).__name__}",
                )
            return MergeResult.MERGED

        if outcome.action == "rebase":
            _emit(
                "auto_merge_rebasing",
                task_id,
                f"pr={outcome.pr} mode={outcome.mode} reason={outcome.reason}",
            )
            self._rebase_pr_branch_for_auto_merge(task, outcome.pr)
            return MergeResult.NEEDS_REVIEW

        if outcome.action == "wait_ci":
            _emit(
                "auto_merge_waiting_ci",
                task_id,
                f"pr={outcome.pr} mode={outcome.mode} reason={outcome.reason}",
            )
            return MergeResult.NEEDS_REVIEW

        if outcome.action == "kickback_ci":
            _emit(
                "auto_merge_kickback",
                task_id,
                f"pr={outcome.pr} mode={outcome.mode} reason={outcome.reason}",
            )
            self.last_failure_reason = f"auto_merge_ci_failed: {outcome.reason}"
            try:
                self.kickback_with_cascade(task_id, self.last_failure_reason)
            except Exception:
                logger.exception("auto-merge: kickback_with_cascade failed for %s", task_id)
            return MergeResult.FAILED

        if outcome.action == "pause_mission":
            self._pause_mission_for_blocked_reviews(task, outcome.pr, outcome.reason)
            return MergeResult.NEEDS_REVIEW

        # skip — logged at caller scope; stay inert.
        return MergeResult.NEEDS_REVIEW

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
        poll_external_merges: bool = True,
        data_dir_name: str = ".antfarm",
    ) -> Soldier:
        """Create a Soldier that talks directly to a TaskBackend.

        Instead of going through the Colony HTTP API, this wraps the backend
        with a ColonyClient-compatible adapter so the Soldier can run
        in-process (e.g., as a daemon thread inside the colony server).
        Review is enabled by default — Soldier creates review tasks for done work.
        """
        instance = cls.__new__(cls)
        instance.colony = _BackendAdapter(backend)
        instance.colony_url = ""  # in-process: sentinel disables SSE wait
        instance.repo_path = repo_path
        instance.integration_branch = integration_branch
        instance.test_command = test_command or ["pytest", "-x", "-q"]
        instance.poll_interval = poll_interval
        instance.require_review = require_review
        instance.poll_external_merges = poll_external_merges
        instance.data_dir_name = data_dir_name
        instance.last_failure_reason = ""
        instance._event_cursor = 0
        instance._preflight_done = False
        instance._auto_merge_last_checked = {}
        instance._repo_permission_cache = {}
        instance.auto_merge_poll_backoff_seconds = 30.0
        instance._reconcile_last_checked = {}
        instance.reconcile_backoff_seconds = 60.0
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

    def mark_merged(
        self,
        task_id: str,
        attempt_id: str,
        auto_merged: bool = False,
    ) -> None:
        self._backend.mark_merged(task_id, attempt_id, auto_merged=auto_merged)

    def kickback(self, task_id: str, reason: str, max_attempts: int = 3) -> None:
        self._backend.kickback(task_id, reason, max_attempts=max_attempts)

    def store_review_verdict(self, task_id: str, attempt_id: str, verdict: dict) -> None:
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
