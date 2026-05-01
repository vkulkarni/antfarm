"""Mission audit doc — pure markdown rendering + best-effort git commit.

This module produces a permanent, reviewable audit record of a mission and
commits it directly to the integration branch of the target repo. The
pattern mirrors how CHANGELOG bumps are handled today (no PR, no auto-merge
cycle) — see issue #379.

Two responsibilities, separated:

- ``render_mission_doc`` is a pure function from (mission, tasks, usage) to a
  markdown string. No I/O, no git, easy to unit-test.
- ``write_and_commit_doc`` writes the file and runs ``git add/commit/push`` on
  the target repo. Best-effort: any git failure is logged at WARNING and
  swallowed — mission completion must never block on the audit doc.

Idempotent: rerunning with the same content is a no-op (we use
``git diff --cached --quiet`` to detect "nothing to commit").
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH_TEMPLATE = "docs/antfarm/missions/{mission_id}.md"


# ---------------------------------------------------------------------------
# Pure rendering
# ---------------------------------------------------------------------------


def render_mission_doc(
    mission: dict,
    tasks: list[dict],
    usage: dict | None,
) -> str:
    """Render a mission audit document as markdown.

    Pure function: no I/O, no git, no logging. Sections:

    - Header (mission id, spec file, timestamps, outcome)
    - Plan (proposed tasks table)
    - Re-plans (only if ``re_plan_count > 0``)
    - Tasks (PR, attempts, verdict, wall, notes)
    - Budget (only if ``usage`` is provided)
    - Timeline highlights (chronologically ordered events drawn from trails)

    Args:
        mission: Mission dict (e.g. from ``backend.get_mission``).
        tasks: All tasks belonging to the mission, including infra tasks
            (plan/review). Filtering happens here.
        usage: ``MissionUsage.to_dict()`` output, or ``None`` if no usage was
            recorded for the mission.

    Returns:
        Markdown string ready to write to disk.
    """
    mission_id = mission.get("mission_id", "?")
    spec_file = mission.get("spec_file") or "(inline)"
    created = mission.get("created_at", "")
    completed = mission.get("completed_at") or ""
    status = mission.get("status", "?")
    duration = _format_duration(created, completed)

    impl_tasks = [t for t in tasks if not _is_infra(t)]
    merged_count = sum(1 for t in impl_tasks if _has_merged_attempt(t))
    total_count = len(impl_tasks)
    outcome_line = f"{status} ({merged_count}/{total_count} merged)"

    out: list[str] = []
    out.append(f"# Mission: {mission_id}")
    out.append("")
    out.append(f"**Spec:** `{spec_file}`")
    out.append(f"**Created:** {created}")
    out.append(f"**Completed:** {completed} ({duration})")
    out.append(f"**Outcome:** {outcome_line}")
    out.append("")

    # --- Plan -----------------------------------------------------------
    out.append("## Plan")
    out.append("")
    artifact = mission.get("plan_artifact") or {}
    proposed = artifact.get("proposed_tasks") or []
    if proposed:
        out.append("| ID | Title | Deps | Touches | Complexity |")
        out.append("|---|---|---|---|---|")
        for i, pt in enumerate(proposed):
            tid = pt.get("id") or f"task-{i + 1:02d}"
            title = (pt.get("title") or "").replace("|", "\\|")
            deps = pt.get("depends_on") or []
            deps_s = ", ".join(str(d) for d in deps) if deps else "—"
            touches = pt.get("touches") or []
            touches_s = ", ".join(str(t) for t in touches) if touches else "—"
            complexity = pt.get("complexity") or "—"
            out.append(f"| {tid} | {title} | {deps_s} | {touches_s} | {complexity} |")
    else:
        out.append("_(no plan artifact recorded)_")
    out.append("")

    # --- Re-plans -------------------------------------------------------
    re_plan_count = int(mission.get("re_plan_count", 0) or 0)
    if re_plan_count > 0:
        out.append("## Re-plans")
        out.append("")
        out.append(f"Re-plan cycles: **{re_plan_count}**.")
        out.append("")
        out.append(
            "The plan above is the final accepted version. Earlier rejected "
            "drafts can be reconstructed from the plan task's attempt history "
            "in `.antfarm/tasks/`."
        )
        out.append("")

    # --- Tasks ----------------------------------------------------------
    out.append("## Tasks")
    out.append("")
    if impl_tasks:
        out.append("| ID | PR | Attempts | Verdict | Wall | Notes |")
        out.append("|---|---|---|---|---|---|")
        for task in impl_tasks:
            tid = task.get("id", "?")
            attempts = task.get("attempts", []) or []
            attempt_count = len(attempts)
            merged_attempt = _find_merged_attempt(attempts)
            if merged_attempt is not None:
                pr = merged_attempt.get("pr") or "—"
                verdict = "pass"
                wall = _format_duration(
                    merged_attempt.get("started_at", ""),
                    merged_attempt.get("completed_at", "") or "",
                )
            elif task.get("status") == "blocked":
                pr = "—"
                verdict = "blocked"
                wall = "—"
            else:
                # Done but not merged, or in-flight at terminal time.
                pr = _last_pr(attempts) or "—"
                verdict = task.get("status", "?")
                wall = "—"
            notes = _task_notes(task, attempts)
            tid_s = str(tid).replace("|", "\\|")
            pr_s = str(pr).replace("|", "\\|")
            notes_s = notes.replace("|", "\\|")
            out.append(f"| {tid_s} | {pr_s} | {attempt_count} | {verdict} | {wall} | {notes_s} |")
    else:
        out.append("_(no implementation tasks)_")
    out.append("")

    # --- Budget ---------------------------------------------------------
    if usage:
        out.append("## Budget")
        out.append("")
        cost = float(usage.get("total_cost_usd", 0.0) or 0.0)
        cfg = mission.get("config") or {}
        max_cost = cfg.get("max_cost_usd")
        in_tok = int(usage.get("total_input_tokens", 0) or 0)
        out_tok = int(usage.get("total_output_tokens", 0) or 0)
        cache_r = int(usage.get("total_cache_read_tokens", 0) or 0)
        if max_cost is not None and float(max_cost) > 0:
            pct = cost / float(max_cost) * 100.0
            out.append(f"- Cost: ${cost:.4f} / ${float(max_cost):.4f} ({pct:.1f}%)")
        else:
            out.append(f"- Cost: ${cost:.4f}")
        out.append(f"- Tokens: input={in_tok} output={out_tok} cache_read={cache_r}")
        per_task = usage.get("per_task") or {}
        if per_task:
            top_id, top_entry = max(
                per_task.items(),
                key=lambda kv: float(kv[1].get("cost_usd", 0.0) or 0.0),
            )
            top_cost = float(top_entry.get("cost_usd", 0.0) or 0.0)
            out.append(f"- Top spend: {top_id} (${top_cost:.4f})")
        out.append("")

    # --- Timeline -------------------------------------------------------
    timeline = _collect_timeline(mission, tasks)
    if timeline:
        out.append("## Timeline highlights")
        out.append("")
        for ts, msg in timeline:
            out.append(f"- {ts}  {msg}")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Write + commit
# ---------------------------------------------------------------------------


def write_and_commit_doc(
    repo_path: Path,
    mission: dict,
    tasks: list[dict],
    usage: dict | None,
    *,
    integration_branch: str = "main",
    template: str = DEFAULT_PATH_TEMPLATE,
) -> bool:
    """Render the audit doc, write it, and commit + push to the integration
    branch.

    Best-effort: any subprocess failure is logged at WARNING and swallowed —
    this is a doc commit, not a state mutation, so it must never block
    mission transitions. Idempotent: rerunning with identical content is a
    no-op (``git diff --cached --quiet`` returns 0 → we skip the commit and
    return ``True``).

    Args:
        repo_path: Root of the target git repo.
        mission: Mission dict.
        tasks: All tasks belonging to the mission.
        usage: Mission usage sidecar dict, or None.
        integration_branch: Branch to push to. Defaults to ``main``.
        template: File path template containing ``{mission_id}``.

    Returns:
        True if the doc was written and committed (or was already up to date),
        False if any step failed.
    """
    mission_id = mission.get("mission_id")
    if not mission_id:
        logger.warning("mission_doc: skipping audit doc — mission has no mission_id")
        return False

    repo_path = Path(repo_path)
    rel_path = template.format(mission_id=mission_id)
    abs_path = repo_path / rel_path

    try:
        markdown = render_mission_doc(mission, tasks, usage)
    except Exception:
        logger.warning(
            "mission_doc: failed to render audit doc for mission %s",
            mission_id,
            exc_info=True,
        )
        return False

    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(markdown)
    except OSError:
        logger.warning(
            "mission_doc: failed to write audit doc to %s",
            abs_path,
            exc_info=True,
        )
        return False

    # `git add` — capture stderr so a friendly warning is possible.
    add = subprocess.run(  # noqa: S603 — git is trusted
        ["git", "add", rel_path],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if add.returncode != 0:
        logger.warning(
            "mission_doc: git add failed for %s (rc=%s): %s",
            rel_path,
            add.returncode,
            add.stderr.strip(),
        )
        return False

    # Idempotency check: if nothing is staged, we're already up to date.
    diff = subprocess.run(  # noqa: S603
        ["git", "diff", "--cached", "--quiet", "--", rel_path],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if diff.returncode == 0:
        logger.info(
            "mission_doc: audit doc for %s already up to date at %s",
            mission_id,
            rel_path,
        )
        return True

    commit_msg = f"docs: mission {mission_id} audit"
    commit = subprocess.run(  # noqa: S603
        ["git", "commit", "-m", commit_msg],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode != 0:
        logger.warning(
            "mission_doc: git commit failed for %s (rc=%s): %s",
            rel_path,
            commit.returncode,
            commit.stderr.strip(),
        )
        return False

    push = subprocess.run(  # noqa: S603
        ["git", "push", "origin", integration_branch],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if push.returncode != 0:
        # Push failure is non-fatal: the commit landed locally, the operator
        # can push manually. Still log so it doesn't disappear silently.
        logger.warning(
            "mission_doc: git push failed for %s on %s (rc=%s): %s",
            rel_path,
            integration_branch,
            push.returncode,
            push.stderr.strip(),
        )
        return False

    logger.info(
        "mission_doc: committed audit doc for mission %s at %s",
        mission_id,
        rel_path,
    )
    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_infra(task: dict) -> bool:
    """Match antfarm.core.missions.is_infra_task without the import cycle.

    Kept inline so this module stays cheap to import from queen.
    """
    caps = task.get("capabilities_required", []) or []
    return "plan" in caps or "review" in caps or task.get("id", "").startswith("review-")


def _has_merged_attempt(task: dict) -> bool:
    return any(a.get("status") == "merged" for a in task.get("attempts", []) or [])


def _find_merged_attempt(attempts: list[dict]) -> dict | None:
    for a in attempts:
        if a.get("status") == "merged":
            return a
    return None


def _last_pr(attempts: list[dict]) -> str | None:
    """Return the PR URL of the most recent attempt that has one."""
    for a in reversed(attempts):
        pr = a.get("pr")
        if pr:
            return pr
    return None


def _task_notes(task: dict, attempts: list[dict]) -> str:
    """Summarise notable failures + retries for the Notes column."""
    bits: list[str] = []
    if task.get("status") == "blocked":
        bits.append("blocked")
    failures = []
    for a in attempts:
        ftype = a.get("failure_type") or a.get("last_failure_type")
        if ftype:
            failures.append(str(ftype))
    if failures:
        # Show the first failure type — gives operators a hook into the
        # failure pattern without dumping the full trail.
        bits.append(f"first attempt: {failures[0]}")
    if len(attempts) > 1 and not failures:
        bits.append(f"{len(attempts)} attempts")
    return "; ".join(bits) if bits else "—"


def _collect_timeline(mission: dict, tasks: list[dict]) -> list[tuple[str, str]]:
    """Pick a small set of high-signal events from mission + tasks.

    Sorted chronologically. Each entry is ``(ts_short, message)``.
    """
    events: list[tuple[str, str]] = []
    if mission.get("created_at"):
        events.append((mission["created_at"], "mission created"))

    plan_artifact = mission.get("plan_artifact")
    plan_task_id = mission.get("plan_task_id")
    if plan_artifact and plan_task_id:
        # Approximate "plan ready" with the plan task's first attempt
        # completion timestamp when available.
        for t in tasks:
            if t.get("id") == plan_task_id:
                for a in t.get("attempts", []) or []:
                    if a.get("status") in ("done", "merged") and a.get("completed_at"):
                        events.append((a["completed_at"], "plan ready"))
                        break
                break

    for task in tasks:
        if _is_infra(task):
            continue
        tid = task.get("id", "?")
        for a in task.get("attempts", []) or []:
            status = a.get("status")
            if status == "merged" and a.get("completed_at"):
                events.append((a["completed_at"], f"{tid} merged"))
            elif status == "superseded" and a.get("completed_at"):
                ftype = a.get("failure_type") or a.get("last_failure_type") or "kickback"
                events.append((a["completed_at"], f"{tid} kicked back ({ftype})"))

    if mission.get("completed_at"):
        outcome = mission.get("status", "complete")
        events.append((mission["completed_at"], f"mission {outcome}"))

    events.sort(key=lambda kv: kv[0])
    return [(_format_short_ts(ts), msg) for ts, msg in events]


def _format_short_ts(ts: str) -> str:
    """Render an ISO timestamp as HH:MM:SS, falling back to the raw string."""
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return ts


def _format_duration(start: str, end: str) -> str:
    """Format a duration between two ISO timestamps as a human string."""
    if not start or not end:
        return "—"
    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
    except (ValueError, TypeError):
        return "—"
    if s.tzinfo is None:
        s = s.replace(tzinfo=UTC)
    if e.tzinfo is None:
        e = e.replace(tzinfo=UTC)
    delta = e - s
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
