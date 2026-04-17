"""Queen controller daemon thread for Antfarm v0.6.

Advances missions through their lifecycle: PLANNING → REVIEWING_PLAN → BUILDING
→ COMPLETE/FAILED. Deterministic, stateless between ticks, crash-recoverable.

Queen never calls backend.kickback() directly (that's the Soldier's job),
never starts workers (that's the Autoscaler's job), and never fixes code.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from antfarm.core.backends.base import TaskBackend
from antfarm.core.missions import (
    MissionReport,
    MissionReportBlocked,
    MissionReportTask,
    MissionStatus,
    PlanArtifact,
    is_infra_task,
    link_task_to_mission,
)
from antfarm.core.serve import _emit_event

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(ts: str) -> float:
    """Parse an ISO 8601 timestamp to a Unix timestamp."""
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return 0.0


@dataclass
class QueenConfig:
    base_interval: float = 30.0
    active_interval: float = 10.0
    idle_interval: float = 60.0
    max_re_plans: int = 1
    enable_mission_context: bool = True


class Queen:
    def __init__(
        self,
        backend: TaskBackend,
        config: QueenConfig | None = None,
        clock=time.time,
        data_dir: str = ".antfarm",
        repo_path: str = ".",
        integration_branch: str = "main",
    ):
        self.backend = backend
        self.config = config or QueenConfig()
        self._clock = clock
        self._stopped = False
        self._data_dir = data_dir
        self._repo_path = repo_path
        self._integration_branch = integration_branch

    # --- main loop ---

    def run(self) -> None:
        while not self._stopped:
            missions = self.backend.list_missions()
            for m in missions:
                if m["status"] in ("complete", "failed", "cancelled"):
                    continue
                try:
                    self._advance(m)
                except Exception as e:
                    logger.exception(
                        "queen: failed to advance mission %s: %s",
                        m["mission_id"],
                        e,
                    )
            time.sleep(self._adaptive_interval(missions))

    def stop(self) -> None:
        self._stopped = True

    # --- per-tick phase dispatch (all idempotent, all read fresh state) ---

    def _advance(self, mission: dict) -> None:
        status = mission["status"]
        if status == MissionStatus.PLANNING:
            self._advance_planning(mission)
        elif status == MissionStatus.REVIEWING_PLAN:
            self._advance_reviewing_plan(mission)
        elif status == MissionStatus.BUILDING:
            self._advance_building(mission)
        elif status == MissionStatus.BLOCKED:
            self._advance_blocked(mission)

    # --- phase handlers ---

    def _advance_planning(self, mission: dict) -> None:
        """If plan task does not yet exist, create it.
        If plan task is harvested, extract PlanArtifact, transition state.

        Plan task failure modes are split:
        - Plan task is BLOCKED (exhausted FileBackend.max_attempts) → mission FAILED.
        - Plan task harvested but artifact is missing/invalid → defer to Soldier
          kickback loop (append trail entry, return).
        - Plan task harvested WITH valid artifact and plan-review NEEDS_CHANGES
          → consume re-plan budget (handled in _advance_reviewing_plan).
        """
        plan_task_id = mission.get("plan_task_id")
        if plan_task_id is None:
            if mission["re_plan_count"] == 0:
                _emit_event(
                    "mission_created",
                    "",
                    detail=f"mission={mission['mission_id']} {mission['spec'][:80]}",
                    actor="queen",
                )
            plan_task_id = self._create_plan_task(mission)
            self.backend.update_mission(
                mission["mission_id"],
                {
                    "plan_task_id": plan_task_id,
                    "last_progress_at": _now_iso(),
                },
            )
            return

        plan_task = self.backend.get_task(plan_task_id)
        if plan_task is None:
            self._fail(mission, f"system: plan task {plan_task_id} disappeared")
            return

        # Plan task exhausted FileBackend retry budget → mission failed.
        if plan_task["status"] == "blocked":
            attempt_count = len(plan_task.get("attempts", []))
            self._fail(
                mission,
                f"system: plan task {plan_task_id} blocked after "
                f"{attempt_count} attempts (malformed or unusable planner output)",
            )
            return

        if plan_task["status"] != "done":
            return  # still waiting (ready/active/harvest_pending)

        # Plan task is done. Extract artifact from current attempt.
        artifact = self._extract_plan_artifact(plan_task)
        if artifact is None:
            logger.warning(
                "queen: mission %s plan task %s harvested with no valid "
                "PlanArtifact; deferring to Soldier kickback loop",
                mission["mission_id"],
                plan_task_id,
            )
            with contextlib.suppress(Exception):
                self.backend.append_trail(
                    plan_task_id,
                    {
                        "ts": _now_iso(),
                        "worker_id": "queen",
                        "message": "plan artifact invalid/missing; awaiting kickback",
                        "action_type": "failure",
                    },
                )
            return

        require_review = mission["config"]["require_plan_review"]
        if require_review:
            self._transition(
                mission,
                MissionStatus.REVIEWING_PLAN,
                extras={"plan_artifact": artifact.to_dict()},
            )
            self._create_plan_review_task(mission, artifact)
        else:
            self._spawn_child_tasks(mission, artifact)
            self._transition(
                mission,
                MissionStatus.BUILDING,
                extras={"plan_artifact": artifact.to_dict()},
            )
            self._maybe_generate_context(mission, artifact)

    def _advance_reviewing_plan(self, mission: dict) -> None:
        """Check plan-review task state and act accordingly.

        Failure mode split (NB-4):
        1. Review task in ready → no-op.
        2. Review task in blocked → system failure.
        3. Review task in done but no verdict → no-op.
        4. Review task in done with verdict=pass → spawn children, BUILDING.
        5. Review task in done with verdict=needs_changes → consume re_plan_count.
        6. Review task in done with verdict=blocked → mission FAILED.
        """
        from antfarm.core.review_pack import extract_verdict_from_review_task

        review_task_id = self._plan_review_task_id(mission)
        review_task = self.backend.get_task(review_task_id)
        if review_task is None:
            return  # just created; next tick will find it

        status = review_task["status"]
        # (1) Doctor recovered a stuck reviewer — wait for next attempt
        if status == "ready":
            return
        # (2) Reviewer keeps crashing — infra failure
        if status == "blocked":
            attempt_count = len(review_task.get("attempts", []))
            self._fail(
                mission,
                f"system: plan review task blocked after {attempt_count} attempts",
            )
            return
        if status != "done":
            return  # active / harvest_pending — still working

        verdict = extract_verdict_from_review_task(review_task)
        # Worker stores review verdict on the *original* task (the plan task),
        # not the review task. Fall back to checking the plan task's attempt.
        if verdict is None:
            plan_task = self.backend.get_task(mission.get("plan_task_id", ""))
            if plan_task:
                verdict = self._extract_verdict_from_plan_task(plan_task)
        # (3) Harvested but verdict not yet persisted — retry next tick
        if verdict is None:
            return

        if verdict["verdict"] == "pass":
            artifact = PlanArtifact.from_dict(mission["plan_artifact"])
            _emit_event(
                "plan_approved",
                mission.get("plan_task_id", "") or "",
                detail=f"mission={mission['mission_id']} tasks={artifact.task_count}",
                actor="queen",
            )
            self._spawn_child_tasks(mission, artifact)
            self._transition(mission, MissionStatus.BUILDING)
            self._maybe_generate_context(mission)
        elif verdict["verdict"] == "needs_changes":
            if mission["re_plan_count"] >= self.config.max_re_plans:
                summary = verdict.get("summary", "no summary")
                self._fail(mission, f"review: plan rejected - {summary}")
                return
            self._create_re_plan_task(mission, verdict)
            self.backend.update_mission(
                mission["mission_id"],
                {
                    "re_plan_count": mission["re_plan_count"] + 1,
                    "status": MissionStatus.PLANNING.value,
                    "plan_task_id": None,
                    "plan_artifact": None,
                },
            )
        else:  # verdict == "blocked"
            self._fail(
                mission,
                f"review: plan rejected - {verdict.get('summary', 'blocked')}",
            )

    def _advance_building(self, mission: dict) -> None:
        """Check child task status. Complete mission if all accounted for."""
        child_tasks = [self.backend.get_task(tid) for tid in mission["task_ids"]]
        child_tasks = [t for t in child_tasks if t is not None and not is_infra_task(t)]

        if not child_tasks:
            return  # no impl tasks yet — still spawning

        merged = [t for t in child_tasks if self._has_merged_attempt(t)]
        merged_ids = {t["id"] for t in merged}
        blocked = [t for t in child_tasks if t["status"] == "blocked"]
        in_flight = [
            t
            for t in child_tasks
            if t["status"] in ("ready", "active", "done", "harvest_pending")
            and t["id"] not in merged_ids
        ]

        # Track any task progress so stall detector stays fresh.
        if self._had_progress_since_last_tick(mission, child_tasks):
            self.backend.update_mission(
                mission["mission_id"],
                {"last_progress_at": _now_iso()},
            )

        # Blocked task bookkeeping
        blocked_ids = [t["id"] for t in blocked]
        if set(blocked_ids) != set(mission["blocked_task_ids"]):
            self.backend.update_mission(
                mission["mission_id"],
                {"blocked_task_ids": blocked_ids},
            )

        if not in_flight:
            # Terminal: everything is either merged or blocked.
            report = self._generate_report(mission)
            self._transition(
                mission,
                MissionStatus.COMPLETE,
                extras={"report": report.to_dict(), "completed_at": _now_iso()},
            )
            return

        self._check_stall(mission)

    def _advance_blocked(self, mission: dict) -> None:
        """Check for unblock (operator ran `antfarm unblock`) or timeout."""
        child_tasks = [self.backend.get_task(tid) for tid in mission["task_ids"]]
        child_tasks = [t for t in child_tasks if t is not None and not is_infra_task(t)]
        in_flight = [t for t in child_tasks if t["status"] in ("ready", "active", "done")]
        if in_flight:
            self._transition(mission, MissionStatus.BUILDING)
            return
        self._check_stall_timeout(mission)

    # --- stall detection ---

    def _check_stall(self, mission: dict) -> None:
        threshold = mission["config"]["stall_threshold_minutes"] * 60
        last = _parse_iso(mission["last_progress_at"])
        if self._clock() - last > threshold:
            logger.warning(
                "queen: mission %s stalled after %s minutes",
                mission["mission_id"],
                mission["config"]["stall_threshold_minutes"],
            )
            self._transition(mission, MissionStatus.BLOCKED)

    def _check_stall_timeout(self, mission: dict) -> None:
        if mission["config"]["blocked_timeout_action"] != "fail":
            return
        threshold = mission["config"]["blocked_timeout_minutes"] * 60
        last = _parse_iso(mission["last_progress_at"])
        if self._clock() - last > threshold:
            self._fail(mission, "system: blocked_timeout exceeded")

    # --- progress detection ---

    def _had_progress_since_last_tick(self, mission: dict, child_tasks: list[dict]) -> bool:
        """Detect if any child task changed status since last tick.

        Uses a hash of all child task statuses + attempt counts. Persists the
        hash on the mission so Queen is stateless between ticks.
        """
        status_snapshot = {
            t["id"]: f"{t['status']}:{len(t.get('attempts', []))}" for t in child_tasks
        }
        current_hash = hashlib.md5(json.dumps(status_snapshot, sort_keys=True).encode()).hexdigest()

        last_hash = mission.get("_last_task_status_hash")
        if last_hash is None or last_hash != current_hash:
            self.backend.update_mission(
                mission["mission_id"],
                {"_last_task_status_hash": current_hash},
            )
            return last_hash is not None and last_hash != current_hash
        return False

    # --- task creation helpers ---

    def _create_plan_task(self, mission: dict) -> str:
        """Create the plan task for a mission. Returns the task ID."""
        mission_id = mission["mission_id"]
        plan_task_id = f"plan-{mission_id}"

        now = _now_iso()
        task = {
            "id": plan_task_id,
            "title": f"Plan mission {mission_id}",
            "spec": (
                "You are a planner. Decompose the following spec into tasks.\n"
                "Output a JSON array of tasks with max 10 tasks.\n\n"
                "---\n\n"
                f"{mission['spec']}"
            ),
            "complexity": "M",
            "priority": 1,
            "depends_on": [],
            "touches": [],
            "capabilities_required": ["plan"],
            "created_by": "queen",
            "status": "ready",
            "current_attempt": None,
            "attempts": [],
            "trail": [],
            "signals": [],
            "created_at": now,
            "updated_at": now,
            "mission_id": mission_id,
        }

        with contextlib.suppress(ValueError):
            link_task_to_mission(self.backend, task, mission_id)
        _emit_event(
            "plan_task_created",
            plan_task_id,
            detail=f"mission={mission_id}",
            actor="queen",
        )
        return plan_task_id

    def _create_plan_review_task(self, mission: dict, artifact: PlanArtifact) -> str:
        """Create a review task for the plan. Returns the task ID."""
        mission_id = mission["mission_id"]
        review_task_id = f"review-plan-{mission_id}"

        proposed = artifact.proposed_tasks
        task_list = "\n".join(
            f"  {i + 1}. {t.get('title', t.get('id', '?'))}" for i, t in enumerate(proposed)
        )

        now = _now_iso()
        task = {
            "id": review_task_id,
            "title": f"Review plan for mission {mission_id}",
            "spec": (
                f"Review the proposed plan for mission {mission_id}.\n\n"
                f"Mission spec:\n{mission['spec']}\n\n"
                f"Proposed tasks ({artifact.task_count}):\n{task_list}\n\n"
                f"Warnings: {artifact.warnings}\n"
                f"Dependency summary: {artifact.dependency_summary}\n\n"
                "Instructions:\n"
                "1. Review task breakdown for completeness and correctness\n"
                "2. Check dependencies are valid\n"
                "3. Produce a ReviewVerdict (pass/needs_changes/blocked)\n"
                "4. Output verdict between [REVIEW_VERDICT] and [/REVIEW_VERDICT] tags\n"
            ),
            "complexity": "S",
            "priority": 1,
            "depends_on": [],
            "touches": [],
            "capabilities_required": ["review"],
            "created_by": "queen",
            "status": "ready",
            "current_attempt": None,
            "attempts": [],
            "trail": [],
            "signals": [],
            "created_at": now,
            "updated_at": now,
            "mission_id": mission_id,
        }

        with contextlib.suppress(ValueError):
            link_task_to_mission(self.backend, task, mission_id)
        return review_task_id

    def _create_re_plan_task(self, mission: dict, verdict: dict) -> str:
        """Create a re-plan task incorporating reviewer feedback."""
        mission_id = mission["mission_id"]
        re_plan_count = mission["re_plan_count"] + 1
        re_plan_task_id = f"plan-{mission_id}-re{re_plan_count}"

        summary = verdict.get("summary", "")
        feedback = verdict.get("feedback", "")

        now = _now_iso()
        task = {
            "id": re_plan_task_id,
            "title": f"Re-plan mission {mission_id} (attempt {re_plan_count + 1})",
            "spec": (
                "You are a planner. The previous plan was rejected.\n\n"
                f"Reviewer feedback:\n{summary}\n{feedback}\n\n"
                "Original spec:\n"
                f"{mission['spec']}\n\n"
                "Revise the plan and output a JSON array of tasks with max 10 tasks.\n"
            ),
            "complexity": "M",
            "priority": 1,
            "depends_on": [],
            "touches": [],
            "capabilities_required": ["plan"],
            "created_by": "queen",
            "status": "ready",
            "current_attempt": None,
            "attempts": [],
            "trail": [],
            "signals": [],
            "created_at": now,
            "updated_at": now,
            "mission_id": mission_id,
        }

        with contextlib.suppress(ValueError):
            link_task_to_mission(self.backend, task, mission_id)
        return re_plan_task_id

    def _spawn_child_tasks(self, mission: dict, artifact: PlanArtifact) -> list[str]:
        """Create child tasks from the plan artifact. Deterministic IDs."""
        mission_id = mission["mission_id"]
        # Extract slug from mission_id: strip the numeric suffix
        slug = self._mission_slug(mission_id)

        child_ids: list[str] = []
        now = _now_iso()

        for i, _proposed in enumerate(artifact.proposed_tasks):
            child_id = f"task-{slug}-{i + 1:02d}"
            child_ids.append(child_id)

        # Rewrite depends_on from indices/proposed IDs to actual child IDs
        for i, proposed in enumerate(artifact.proposed_tasks):
            child_id = child_ids[i]
            # Resolve dependencies: proposed tasks may reference each other
            # by index (1-based) or by the proposed task's id field.
            resolved_deps = self._resolve_child_deps(
                proposed.get("depends_on", []),
                artifact.proposed_tasks,
                child_ids,
            )

            task = {
                "id": child_id,
                "title": proposed.get("title", f"Task {i + 1}"),
                "spec": proposed.get("spec", ""),
                "complexity": proposed.get("complexity", "M"),
                "priority": proposed.get("priority", 10),
                "depends_on": resolved_deps,
                "touches": proposed.get("touches", []),
                "capabilities_required": proposed.get("capabilities_required", []),
                "created_by": "queen",
                "status": "ready",
                "current_attempt": None,
                "attempts": [],
                "trail": [],
                "signals": [],
                "created_at": now,
                "updated_at": now,
                "mission_id": mission_id,
            }

            with contextlib.suppress(ValueError):
                link_task_to_mission(self.backend, task, mission_id)

        _emit_event(
            "tasks_seeded",
            "",
            detail=f"mission={mission_id} count={len(child_ids)}",
            actor="queen",
        )
        return child_ids

    # --- artifact extraction ---

    def _extract_plan_artifact(self, plan_task: dict) -> PlanArtifact | None:
        """Extract PlanArtifact from a done plan task's current attempt."""
        current_attempt_id = plan_task.get("current_attempt")
        if not current_attempt_id:
            return None
        for attempt in plan_task.get("attempts", []):
            if attempt.get("attempt_id") == current_attempt_id:
                artifact = attempt.get("artifact")
                if artifact and artifact.get("plan_artifact"):
                    try:
                        return PlanArtifact.from_dict(artifact["plan_artifact"])
                    except (KeyError, TypeError):
                        return None
        return None

    # --- report generation ---

    def _generate_report(self, mission: dict) -> MissionReport:
        """Generate a MissionReport from current mission state."""
        child_tasks = [self.backend.get_task(tid) for tid in mission["task_ids"]]
        child_tasks = [t for t in child_tasks if t is not None and not is_infra_task(t)]

        merged_tasks: list[MissionReportTask] = []
        blocked_tasks: list[MissionReportBlocked] = []
        all_pr_urls: list[str] = []
        all_branches: list[str] = []
        total_added = 0
        total_removed = 0
        all_files: list[str] = []

        for t in child_tasks:
            if self._has_merged_attempt(t):
                artifact = self._get_current_artifact(t)
                pr_url = None
                lines_added = 0
                lines_removed = 0
                files_changed: list[str] = []
                if artifact:
                    pr_url = artifact.get("pr_url")
                    lines_added = artifact.get("lines_added", 0)
                    lines_removed = artifact.get("lines_removed", 0)
                    files_changed = artifact.get("files_changed", [])
                    if pr_url:
                        all_pr_urls.append(pr_url)
                    branch = artifact.get("branch")
                    if branch:
                        all_branches.append(branch)

                merged_tasks.append(
                    MissionReportTask(
                        task_id=t["id"],
                        title=t.get("title", ""),
                        pr_url=pr_url,
                        lines_added=lines_added,
                        lines_removed=lines_removed,
                        files_changed=files_changed,
                    )
                )
                total_added += lines_added
                total_removed += lines_removed
                all_files.extend(files_changed)
            elif t["status"] == "blocked":
                attempt_count = len(t.get("attempts", []))
                last_failure = None
                trail = t.get("trail", [])
                if trail:
                    last_failure = trail[-1].get("message")
                blocked_tasks.append(
                    MissionReportBlocked(
                        task_id=t["id"],
                        title=t.get("title", ""),
                        reason=t.get("blocked_reason", "max attempts exhausted"),
                        attempt_count=attempt_count,
                        last_failure_type=last_failure,
                    )
                )

        created_at = _parse_iso(mission["created_at"])
        now = self._clock()
        duration_minutes = (now - created_at) / 60.0 if created_at else 0.0

        return MissionReport(
            mission_id=mission["mission_id"],
            spec_summary=mission["spec"][:200],
            status=MissionStatus(mission["status"]),
            completion_mode=mission["config"].get("completion_mode", "best_effort"),
            duration_minutes=round(duration_minutes, 1),
            total_tasks=len(child_tasks),
            merged_tasks=len(merged_tasks),
            blocked_tasks=len(blocked_tasks),
            failed_reviews=0,
            merged=merged_tasks,
            blocked=blocked_tasks,
            risks=[],
            pr_urls=all_pr_urls,
            branches=all_branches,
            total_lines_added=total_added,
            total_lines_removed=total_removed,
            files_changed=list(set(all_files)),
            generated_at=_now_iso(),
        )

    # --- mission context ---

    def _maybe_generate_context(self, mission: dict, artifact: PlanArtifact | None = None) -> None:
        """Generate and store mission context blob if feature-flagged on."""
        if not self.config.enable_mission_context:
            return
        try:
            from antfarm.core.mission_context import (
                generate_mission_context,
                store_mission_context,
            )

            plan_dict = None
            if artifact is not None:
                plan_dict = artifact.to_dict()
            elif mission.get("plan_artifact"):
                plan_dict = mission["plan_artifact"]

            context = generate_mission_context(
                repo_path=self._repo_path,
                integration_branch=self._integration_branch,
                mission=mission,
                plan_artifact=plan_dict,
            )
            path = store_mission_context(self._data_dir, mission["mission_id"], context)
            self.backend.update_mission(mission["mission_id"], {"mission_context_path": path})
            logger.info(
                "queen: stored mission context for %s (%d bytes)",
                mission["mission_id"],
                len(context),
            )
        except Exception:
            logger.exception(
                "queen: failed to generate mission context for %s",
                mission["mission_id"],
            )

    # --- state transition helpers ---

    def _transition(
        self,
        mission: dict,
        new_status: MissionStatus,
        extras: dict | None = None,
    ) -> None:
        """The only write path that changes mission status."""
        updates: dict = {
            "status": new_status.value,
            "last_progress_at": _now_iso(),
        }
        if extras:
            updates.update(extras)
        self.backend.update_mission(mission["mission_id"], updates)
        logger.info(
            "queen: mission %s → %s",
            mission["mission_id"],
            new_status.value,
        )
        if new_status == MissionStatus.COMPLETE:
            _emit_event(
                "mission_complete",
                "",
                detail=f"mission={mission['mission_id']}",
                actor="queen",
            )

    def _fail(self, mission: dict, reason: str) -> None:
        """Transition mission to FAILED with a reason."""
        self._transition(
            mission,
            MissionStatus.FAILED,
            extras={"completed_at": _now_iso(), "failure_reason": reason},
        )
        logger.error("queen: mission %s FAILED: %s", mission["mission_id"], reason)

    # --- adaptive polling ---

    def _adaptive_interval(self, missions: list[dict]) -> float:
        """Return sleep interval based on mission states."""
        active_states = {
            MissionStatus.PLANNING.value,
            MissionStatus.REVIEWING_PLAN.value,
            MissionStatus.BUILDING.value,
            MissionStatus.BLOCKED.value,
        }
        has_active = any(m["status"] in active_states for m in missions)
        if has_active:
            return self.config.active_interval
        return self.config.idle_interval

    # --- static helpers ---

    @staticmethod
    def _has_merged_attempt(task: dict) -> bool:
        """Return True if the task has at least one attempt with status MERGED."""
        return any(attempt.get("status") == "merged" for attempt in task.get("attempts", []))

    @staticmethod
    def _get_current_artifact(task: dict) -> dict | None:
        """Extract the artifact from the task's current attempt."""
        current_attempt_id = task.get("current_attempt")
        if not current_attempt_id:
            return None
        for attempt in task.get("attempts", []):
            if attempt.get("attempt_id") == current_attempt_id:
                return attempt.get("artifact")
        return None

    @staticmethod
    def _plan_review_task_id(mission: dict) -> str:
        return f"review-plan-{mission['mission_id']}"

    @staticmethod
    def _extract_verdict_from_plan_task(plan_task: dict) -> dict | None:
        """Extract review verdict from the plan task's current attempt.

        The worker stores the review verdict on the original (plan) task, not
        on the review task. This is the fallback for plan review detection.
        """
        current = plan_task.get("current_attempt")
        if not current:
            return None
        for attempt in plan_task.get("attempts", []):
            if attempt.get("attempt_id") == current:
                rv = attempt.get("review_verdict")
                if rv and isinstance(rv, dict) and "verdict" in rv:
                    return rv
        return None

    @staticmethod
    def _mission_slug(mission_id: str) -> str:
        """Extract slug from mission_id by stripping the numeric timestamp suffix."""
        # e.g. "mission-auth-jwt-1712634560000" → "auth-jwt"
        parts = mission_id.split("-")
        if len(parts) > 1 and parts[0] == "mission":
            # Remove "mission" prefix and try to strip numeric suffix
            rest = parts[1:]
            if rest and rest[-1].isdigit():
                rest = rest[:-1]
            return "-".join(rest) if rest else mission_id
        return mission_id

    @staticmethod
    def _resolve_child_deps(
        deps: list,
        proposed_tasks: list[dict],
        child_ids: list[str],
    ) -> list[str]:
        """Resolve dependency references from proposed task IDs to child IDs."""
        resolved = []
        for dep in deps:
            if isinstance(dep, int):
                # 1-based index
                idx = dep - 1
                if 0 <= idx < len(child_ids):
                    resolved.append(child_ids[idx])
            elif isinstance(dep, str):
                # Check if it matches an existing child_id
                if dep in child_ids:
                    resolved.append(dep)
                    continue
                # Check if it matches a proposed task's id field
                for j, pt in enumerate(proposed_tasks):
                    if pt.get("id") == dep and j < len(child_ids):
                        resolved.append(child_ids[j])
                        break
                else:
                    # Pass through as-is (external dependency)
                    resolved.append(dep)
        return resolved
