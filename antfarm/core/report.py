"""Mission report generator and renderers.

DEPENDENCY-FREE MODULE. Imports only from stdlib (textwrap, json, pathlib)
and antfarm.core.missions. MUST NOT import rich, colorama, or any TUI
framework — the morning digest must run headless in CI/cron.
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime
from pathlib import Path

from antfarm.core.missions import (
    MissionReport,
    MissionReportBlocked,
    MissionReportTask,
    MissionStatus,
    is_infra_task,
)


def build_report(mission: dict, tasks: list[dict]) -> MissionReport:
    """Build a MissionReport from a mission dict and its child task dicts.

    Pure function. No I/O. Reads attempts/artifacts for line counts and PR URLs.
    Surfaces failure-reason prefixes (``"system: "`` vs ``"review: "``) on
    ``MissionReportBlocked.reason`` so the terminal/markdown renderers can
    distinguish infra failures from content rejections.
    """
    config = mission.get("config", {})
    completion_mode = config.get("completion_mode", "best_effort")

    # Partition tasks: infra (plan/review) vs impl (builder work)
    impl_tasks = [t for t in tasks if not is_infra_task(t)]
    review_tasks = [t for t in tasks if is_infra_task(t)]

    # Merged impl tasks
    merged: list[MissionReportTask] = []
    blocked: list[MissionReportBlocked] = []
    all_pr_urls: list[str] = []
    all_branches: list[str] = []
    all_files_changed: list[str] = []
    total_lines_added = 0
    total_lines_removed = 0
    all_risks: list[str] = []

    for task in impl_tasks:
        attempts = task.get("attempts", [])
        merged_attempt = _find_merged_attempt(attempts)
        if merged_attempt is not None:
            artifact = merged_attempt.get("artifact", {}) or {}
            pr_url = artifact.get("pr_url") or merged_attempt.get("pr")
            lines_added = artifact.get("lines_added", 0)
            lines_removed = artifact.get("lines_removed", 0)
            files_changed = artifact.get("files_changed", [])
            risks = artifact.get("risks", [])

            merged.append(MissionReportTask(
                task_id=task["id"],
                title=task.get("title", ""),
                pr_url=pr_url,
                lines_added=lines_added,
                lines_removed=lines_removed,
                files_changed=list(files_changed),
            ))
            if pr_url:
                all_pr_urls.append(pr_url)
            branch = merged_attempt.get("branch")
            if branch:
                all_branches.append(branch)
            all_files_changed.extend(files_changed)
            total_lines_added += lines_added
            total_lines_removed += lines_removed
            all_risks.extend(risks)

        elif task.get("status") == "blocked":
            reason = _extract_blocked_reason(task)
            attempt_count = len(attempts)
            last_failure_type = _extract_last_failure_type(task)
            blocked.append(MissionReportBlocked(
                task_id=task["id"],
                title=task.get("title", ""),
                reason=reason,
                attempt_count=attempt_count,
                last_failure_type=last_failure_type,
            ))

    # Failed reviews: review tasks with verdict needs_changes or blocked
    failed_reviews = 0
    for task in review_tasks:
        attempts = task.get("attempts", [])
        for attempt in attempts:
            verdict = attempt.get("review_verdict", {}) or {}
            if verdict.get("verdict") in ("needs_changes", "blocked"):
                failed_reviews += 1
                break

    # Duration
    created_at = mission.get("created_at", "")
    completed_at = mission.get("completed_at")
    duration_minutes = _compute_duration_minutes(created_at, completed_at)

    # Deduplicate files_changed
    seen_files: set[str] = set()
    unique_files: list[str] = []
    for f in all_files_changed:
        if f not in seen_files:
            seen_files.add(f)
            unique_files.append(f)

    return MissionReport(
        mission_id=mission["mission_id"],
        spec_summary=mission.get("spec", "")[:200],
        status=MissionStatus(mission["status"]),
        completion_mode=completion_mode,
        duration_minutes=duration_minutes,
        total_tasks=len(impl_tasks),
        merged_tasks=len(merged),
        blocked_tasks=len(blocked),
        failed_reviews=failed_reviews,
        merged=merged,
        blocked=blocked,
        risks=all_risks,
        pr_urls=all_pr_urls,
        branches=all_branches,
        total_lines_added=total_lines_added,
        total_lines_removed=total_lines_removed,
        files_changed=unique_files,
        generated_at=datetime.now(UTC).isoformat() + "Z",
    )


def render_json(report: MissionReport) -> str:
    """Return the report as a JSON string."""
    return json.dumps(report.to_dict(), indent=2)


def render_terminal(report: MissionReport, use_rich: bool = False) -> str:
    """Return a plain-text string suitable for stdout printing.

    v0.6.0: ``use_rich`` MUST be False. The parameter exists as a
    forward-compat hook — a future version can lazy-import ``rich``
    inside the method to enable colour output without a dependency bump
    or breaking headless callers.

    Uses only ``textwrap`` from the stdlib. 80-column wrap by default.
    """
    if use_rich:
        raise NotImplementedError(
            "rich rendering is a v0.6.1+ opt-in; v0.6.0 is dependency-free"
        )

    lines: list[str] = []
    width = 80
    sep = "=" * width

    lines.append(sep)
    lines.append(f"Mission Report: {report.mission_id}")
    lines.append(sep)
    lines.append("")

    lines.append(f"Status:          {report.status.value}")
    lines.append(f"Completion mode: {report.completion_mode}")
    if report.completion_mode == "all_or_nothing":
        lines.append("  WARNING: all_or_nothing — any blocked task fails the entire mission")
    lines.append(f"Duration:        {report.duration_minutes:.1f} minutes")
    lines.append(f"Generated at:    {report.generated_at}")
    lines.append("")

    # Summary
    lines.append(f"Tasks: {report.total_tasks} total, "
                 f"{report.merged_tasks} merged, "
                 f"{report.blocked_tasks} blocked")
    if report.failed_reviews:
        lines.append(f"Failed reviews: {report.failed_reviews}")
    lines.append(f"Lines: +{report.total_lines_added} / -{report.total_lines_removed}")
    lines.append(f"Files changed: {len(report.files_changed)}")
    lines.append("")

    # Spec summary
    if report.spec_summary:
        lines.append("Spec summary:")
        for wrapped in textwrap.wrap(report.spec_summary, width=width - 2):
            lines.append(f"  {wrapped}")
        lines.append("")

    # Cancelled mission: show completed tasks section
    if report.status == MissionStatus.CANCELLED and report.merged:
        lines.append("-" * width)
        lines.append("Completed before cancellation:")
        lines.append("-" * width)
        for mt in report.merged:
            pr_str = f"  PR: {mt.pr_url}" if mt.pr_url else ""
            lines.append(f"  [{mt.task_id}] {mt.title}{pr_str}")
            lines.append(f"    +{mt.lines_added} / -{mt.lines_removed}, "
                         f"{len(mt.files_changed)} files")
        lines.append("")

    # Merged tasks (non-cancelled)
    if report.status != MissionStatus.CANCELLED and report.merged:
        lines.append("-" * width)
        lines.append("Merged tasks:")
        lines.append("-" * width)
        for mt in report.merged:
            pr_str = f"  PR: {mt.pr_url}" if mt.pr_url else ""
            lines.append(f"  [{mt.task_id}] {mt.title}{pr_str}")
            lines.append(f"    +{mt.lines_added} / -{mt.lines_removed}, "
                         f"{len(mt.files_changed)} files")
        lines.append("")

    # Blocked tasks
    if report.blocked:
        lines.append("-" * width)
        lines.append("Blocked tasks:")
        lines.append("-" * width)
        for bt in report.blocked:
            tag = _failure_tag_terminal(bt.reason)
            lines.append(f"  {tag} [{bt.task_id}] {bt.title}")
            reason_text = bt.reason
            for wrapped in textwrap.wrap(reason_text, width=width - 6):
                lines.append(f"      {wrapped}")
            lines.append(f"      Attempts: {bt.attempt_count}")
            if bt.last_failure_type:
                lines.append(f"      Failure type: {bt.last_failure_type}")
        lines.append("")

    # Risks
    if report.risks:
        lines.append("-" * width)
        lines.append("Risks:")
        lines.append("-" * width)
        for risk in report.risks:
            for wrapped in textwrap.wrap(risk, width=width - 4):
                lines.append(f"  - {wrapped}")
        lines.append("")

    # PR URLs
    if report.pr_urls:
        lines.append("-" * width)
        lines.append("Pull requests:")
        lines.append("-" * width)
        for url in report.pr_urls:
            lines.append(f"  {url}")
        lines.append("")

    lines.append(sep)
    return "\n".join(lines)


def render_markdown(report: MissionReport) -> str:
    """Return a markdown string suitable for pasting into GitHub issues/PRs."""
    lines: list[str] = []

    lines.append(f"# Mission Report: {report.mission_id}")
    lines.append("")

    lines.append(f"**Status:** {report.status.value}")
    lines.append(f"**Completion mode:** {report.completion_mode}")
    if report.completion_mode == "all_or_nothing":
        lines.append("> **Warning:** `all_or_nothing` — any blocked task fails the entire mission")
    lines.append(f"**Duration:** {report.duration_minutes:.1f} minutes")
    lines.append(f"**Generated:** {report.generated_at}")
    lines.append("")

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Total tasks | {report.total_tasks} |")
    lines.append(f"| Merged | {report.merged_tasks} |")
    lines.append(f"| Blocked | {report.blocked_tasks} |")
    if report.failed_reviews:
        lines.append(f"| Failed reviews | {report.failed_reviews} |")
    lines.append(f"| Lines added | +{report.total_lines_added} |")
    lines.append(f"| Lines removed | -{report.total_lines_removed} |")
    lines.append(f"| Files changed | {len(report.files_changed)} |")
    lines.append("")

    # Cancelled mission: completed before cancellation
    if report.status == MissionStatus.CANCELLED and report.merged:
        lines.append("## Completed before cancellation")
        lines.append("")
        for mt in report.merged:
            pr_link = f" ([PR]({mt.pr_url}))" if mt.pr_url else ""
            lines.append(f"- **{mt.task_id}**: {mt.title}{pr_link}")
            lines.append(f"  - +{mt.lines_added} / -{mt.lines_removed}, "
                         f"{len(mt.files_changed)} files")
        lines.append("")

    # Merged tasks (non-cancelled)
    if report.status != MissionStatus.CANCELLED and report.merged:
        lines.append("## Merged tasks")
        lines.append("")
        for mt in report.merged:
            pr_link = f" ([PR]({mt.pr_url}))" if mt.pr_url else ""
            lines.append(f"- **{mt.task_id}**: {mt.title}{pr_link}")
            lines.append(f"  - +{mt.lines_added} / -{mt.lines_removed}, "
                         f"{len(mt.files_changed)} files")
        lines.append("")

    # Blocked tasks
    if report.blocked:
        lines.append("## Blocked tasks")
        lines.append("")
        for bt in report.blocked:
            badge = _failure_badge_markdown(bt.reason)
            lines.append(f"- {badge} **{bt.task_id}**: {bt.title}")
            lines.append(f"  - Reason: {bt.reason}")
            lines.append(f"  - Attempts: {bt.attempt_count}")
            if bt.last_failure_type:
                lines.append(f"  - Failure type: {bt.last_failure_type}")
        lines.append("")

    # Risks
    if report.risks:
        lines.append("## Risks")
        lines.append("")
        for risk in report.risks:
            lines.append(f"- {risk}")
        lines.append("")

    # PR URLs
    if report.pr_urls:
        lines.append("## Pull requests")
        lines.append("")
        for url in report.pr_urls:
            lines.append(f"- {url}")
        lines.append("")

    return "\n".join(lines)


def save_report(data_dir: str, mission_id: str, report: MissionReport) -> str:
    """Write report JSON to .antfarm/missions/{mission_id}_report.json.

    Returns the path written.
    """
    missions_dir = Path(data_dir) / "missions"
    missions_dir.mkdir(parents=True, exist_ok=True)
    path = missions_dir / f"{mission_id}_report.json"
    path.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    return str(path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_merged_attempt(attempts: list[dict]) -> dict | None:
    """Return the first attempt with status 'merged', or None."""
    for attempt in attempts:
        if attempt.get("status") == "merged":
            return attempt
    return None


def _extract_blocked_reason(task: dict) -> str:
    """Extract the failure reason from the last trail entry of a blocked task."""
    trail = task.get("trail", [])
    if trail:
        return trail[-1].get("message", "unknown")
    return "unknown"


def _extract_last_failure_type(task: dict) -> str | None:
    """Extract the last failure type from a task's attempts."""
    attempts = task.get("attempts", [])
    for attempt in reversed(attempts):
        artifact = attempt.get("artifact", {}) or {}
        # Check for failure_record in the artifact
        if "failure_type" in artifact:
            return artifact["failure_type"]
    # Check trail for failure records
    trail = task.get("trail", [])
    for entry in reversed(trail):
        msg = entry.get("message", "")
        if msg.startswith("system: "):
            return "system"
        if msg.startswith("review: "):
            return "review"
    return None


def _compute_duration_minutes(created_at: str, completed_at: str | None) -> float:
    """Compute duration in minutes between created_at and completed_at (or now)."""
    if not created_at:
        return 0.0
    try:
        start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0.0
    if completed_at:
        try:
            end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            end = datetime.now(UTC)
    else:
        end = datetime.now(UTC)
    delta = end - start
    return max(0.0, delta.total_seconds() / 60)


def _failure_tag_terminal(reason: str) -> str:
    """Return a terminal tag for the failure reason prefix."""
    if reason.startswith("system: "):
        return "[SYSTEM]"
    if reason.startswith("review: "):
        return "[REVIEW]"
    return "[FAILED]"


def _failure_badge_markdown(reason: str) -> str:
    """Return a markdown badge for the failure reason prefix."""
    if reason.startswith("system: "):
        return "`[SYSTEM]`"
    if reason.startswith("review: "):
        return "`[REVIEW]`"
    return "`[FAILED]`"
