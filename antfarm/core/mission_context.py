"""Per-mission context blob generation and storage for prompt cache sharing.

Generates a deterministic context prefix that is prepended to all builder
prompts in a mission, enabling prompt cache hits across builders working
on the same mission.

The context blob is:
1. Generated once when Queen transitions a mission to BUILDING
2. Stored as .antfarm/missions/{mission_id}_context.md
3. Served via GET /missions/{id}/context
4. Prepended to builder prompts by WorkerRuntime
"""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def generate_mission_context(
    repo_path: str,
    integration_branch: str,
    mission: dict,
    plan_artifact: dict | None = None,
) -> str:
    """Generate shared context prefix for all builders in a mission.

    Collects (deterministic, no timestamps/random values):
    1. Project conventions (CLAUDE.md, AGENTS.md if present)
    2. Recent commits on integration branch (git log --oneline -20)
    3. Mission spec summary
    4. Plan artifact summary (task list, dependency graph)

    Returns markdown string — must be byte-identical across builders.
    """
    sections: list[str] = []

    sections.append("# Mission Context\n")

    # 1. Project conventions
    for filename in ("CLAUDE.md", "AGENTS.md"):
        filepath = os.path.join(repo_path, filename)
        if os.path.isfile(filepath):
            try:
                with open(filepath) as f:
                    content = f.read()
                sections.append(f"## {filename}\n\n{content}\n")
            except OSError:
                pass

    # 2. Recent commits (deterministic for same repo state)
    try:
        proc = subprocess.run(
            ["git", "log", "--oneline", "-20", integration_branch],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            sections.append(
                f"## Recent commits ({integration_branch})\n\n"
                f"```\n{proc.stdout.strip()}\n```\n"
            )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # 3. Mission spec summary (first 2000 chars)
    spec = mission.get("spec", "")
    if spec:
        truncated = spec[:2000]
        sections.append(f"## Mission spec\n\n{truncated}\n")

    # 4. Plan artifact summary
    if plan_artifact:
        proposed = plan_artifact.get("proposed_tasks", [])
        if proposed:
            task_lines = []
            for i, t in enumerate(proposed):
                title = t.get("title", t.get("id", f"task-{i + 1}"))
                deps = t.get("depends_on", [])
                dep_str = f" (depends: {', '.join(str(d) for d in deps)})" if deps else ""
                task_lines.append(f"  {i + 1}. {title}{dep_str}")
            task_list = "\n".join(task_lines)
            sections.append(f"## Plan — {len(proposed)} tasks\n\n{task_list}\n")

        dep_summary = plan_artifact.get("dependency_summary", "")
        if dep_summary:
            sections.append(f"## Dependency graph\n\n{dep_summary}\n")

    return "\n".join(sections)


def store_mission_context(data_dir: str, mission_id: str, context: str) -> str:
    """Write to .antfarm/missions/{mission_id}_context.md. Returns path."""
    missions_dir = os.path.join(data_dir, "missions")
    os.makedirs(missions_dir, exist_ok=True)
    path = os.path.join(missions_dir, f"{mission_id}_context.md")
    with open(path, "w") as f:
        f.write(context)
    return path


def load_mission_context(data_dir: str, mission_id: str) -> str | None:
    """Load from local file. Returns None if not found."""
    path = os.path.join(data_dir, "missions", f"{mission_id}_context.md")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def get_mission_context(
    mission_id: str,
    data_dir: str | None = None,
    colony_client=None,
) -> str | None:
    """Load context — local file first, then Colony API fallback.

    Returns None if unavailable (graceful degradation).
    """
    # Try local file first
    if data_dir:
        local = load_mission_context(data_dir, mission_id)
        if local is not None:
            return local

    # Fall back to Colony API
    if colony_client is not None:
        try:
            return colony_client.get_mission_context(mission_id)
        except Exception:
            logger.debug(
                "failed to fetch mission context from colony for %s",
                mission_id,
            )

    return None
