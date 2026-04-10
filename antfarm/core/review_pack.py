"""Review pack generation from TaskArtifact.

Generates human-readable review summaries from structured task artifacts,
suitable for display in TUI, CLI, or as PR comments.
"""

from __future__ import annotations

from antfarm.core.models import TaskArtifact


def generate_review_pack(artifact: TaskArtifact, task_title: str = "") -> str:
    """Generate a markdown review pack from a TaskArtifact.

    Args:
        artifact: The structured artifact from a completed task attempt.
        task_title: Optional human-readable task title.

    Returns:
        Markdown-formatted review pack string.
    """
    lines: list[str] = []

    header = f"## Review Pack: {artifact.task_id}"
    if task_title:
        header += f' "{task_title}"'
    lines.append(header)
    lines.append("")

    # Summary (advisory)
    if artifact.summary:
        lines.append("### Summary")
        lines.append(artifact.summary)
        lines.append("")

    # Files changed
    lines.append(
        f"### Files Changed ({len(artifact.files_changed)} files, "
        f"+{artifact.lines_added} -{artifact.lines_removed})"
    )
    for f in artifact.files_changed:
        lines.append(f"- {f}")
    if not artifact.files_changed:
        lines.append("- (no files recorded)")
    lines.append("")

    # Checks
    lines.append("### Checks")
    checks = [
        ("Build", artifact.build_ran, artifact.build_passed),
        ("Tests", artifact.tests_ran, artifact.tests_passed),
        ("Lint", artifact.lint_ran, artifact.lint_passed),
    ]
    for name, ran, passed in checks:
        if ran:
            icon = "passed" if passed else "FAILED"
            mark = "+" if passed else "x"
            lines.append(f"- {name}: {icon} [{mark}]")
        else:
            lines.append(f"- {name}: not run")

    # Freshness
    lines.append(
        f"- Base SHA: {artifact.target_branch_sha_at_harvest[:12]} "
        f"(target: {artifact.target_branch})"
    )
    lines.append("")

    # Merge readiness
    lines.append(f"### Merge Readiness: {artifact.merge_readiness}")
    if artifact.blocking_reasons:
        for reason in artifact.blocking_reasons:
            lines.append(f"- BLOCKED: {reason}")
        lines.append("")

    # Risks (advisory)
    if artifact.risks:
        lines.append("### Risks")
        for risk in artifact.risks:
            lines.append(f"- {risk}")
        lines.append("")

    # Review focus (advisory)
    if artifact.review_focus:
        lines.append("### Suggested Review Focus")
        for focus in artifact.review_focus:
            lines.append(f"- {focus}")
        lines.append("")

    return "\n".join(lines)


def extract_verdict_from_review_task(review_task: dict) -> dict | None:
    """Extract ReviewVerdict dict from a completed review task.

    Lookup order:
    1. Attempt artifact with a "verdict" key
    2. Attempt-level review_verdict field
    3. Trail entry fallback: most recent [REVIEW_VERDICT] JSON message
    """
    current_attempt_id = review_task.get("current_attempt")
    if not current_attempt_id:
        return None
    for attempt in review_task.get("attempts", []):
        if attempt.get("attempt_id") == current_attempt_id:
            # Check attempt artifact for verdict
            artifact = attempt.get("artifact")
            if artifact and "verdict" in artifact:
                return artifact
            # Check attempt-level review_verdict
            rv = attempt.get("review_verdict")
            if rv:
                return rv
    # Fall back to trail entries containing [REVIEW_VERDICT]
    for entry in reversed(review_task.get("trail", [])):
        msg = entry.get("message", "")
        if msg.startswith("[REVIEW_VERDICT] "):
            import json

            try:
                return json.loads(msg[len("[REVIEW_VERDICT] "):])
            except (json.JSONDecodeError, ValueError):
                continue
    return None
