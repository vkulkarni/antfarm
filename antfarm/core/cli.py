"""CLI entry point for Antfarm.

Provides all v0.1 commands: colony, join, worker start, carry, scout, doctor,
and low-level plumbing commands (hatch, forage, trail, harvest, guard, release, signal).

Colony URL resolution: every command that talks to the colony accepts
--colony-url (default http://localhost:7433, envvar ANTFARM_URL).
"""

from __future__ import annotations

import json
import time

import click
import httpx

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COLONY_URL_OPTION = click.option(
    "--colony-url",
    default="http://localhost:7433",
    envvar="ANTFARM_URL",
    show_default=True,
    help="Colony server URL.",
)


def _post(colony_url: str, path: str, payload: dict) -> dict | None:
    r = httpx.post(f"{colony_url.rstrip('/')}{path}", json=payload)
    r.raise_for_status()
    if r.status_code == 204:
        return None
    return r.json()


def _get(colony_url: str, path: str) -> dict:
    r = httpx.get(f"{colony_url.rstrip('/')}{path}")
    r.raise_for_status()
    return r.json()


def _delete(colony_url: str, path: str, params: dict | None = None) -> None:
    r = httpx.delete(f"{colony_url.rstrip('/')}{path}", params=params or {})
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group()
def main():
    """Antfarm — lightweight orchestration for AI coding agents."""


# ---------------------------------------------------------------------------
# colony
# ---------------------------------------------------------------------------


@main.command()
@click.option("--port", default=7433, show_default=True, help="Port to listen on.")
@click.option("--host", default="0.0.0.0", show_default=True, help="Host to bind.")
@click.option("--data-dir", default=".antfarm", show_default=True, help="Data directory.")
def colony(port: int, host: str, data_dir: str):
    """Start the colony server."""
    import uvicorn

    from antfarm.core.backends.file import FileBackend
    from antfarm.core.serve import get_app

    backend = FileBackend(data_dir)
    app = get_app(backend)
    uvicorn.run(app, host=host, port=port)


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------


@main.command()
@click.option("--node", required=True, help="Node ID to register.")
@COLONY_URL_OPTION
def join(node: str, colony_url: str):
    """Register this node with the colony."""
    result = _post(colony_url, "/nodes", {"node_id": node})
    click.echo(f"Joined colony as node '{node}': {result}")


# ---------------------------------------------------------------------------
# worker group
# ---------------------------------------------------------------------------


@main.group()
def worker():
    """Worker management commands."""


@worker.command("start")
@click.option("--agent", required=True, help="Agent type (e.g. claude-code, generic).")
@click.option("--name", default=None, help="Worker name (defaults to agent type).")
@click.option("--workspace-root", default=None, help="Root directory for worktrees.")
@click.option("--node", required=True, help="Node ID this worker belongs to.")
@click.option("--repo-path", default=".", show_default=True, help="Path to git repo.")
@COLONY_URL_OPTION
def worker_start(
    agent: str,
    name: str | None,
    workspace_root: str | None,
    node: str,
    repo_path: str,
    colony_url: str,
):
    """Start a worker and enter the forage loop."""
    from antfarm.core.worker import WorkerRuntime

    worker_name = name or agent
    ws_root = workspace_root or f".antfarm/workspaces/{worker_name}"

    runtime = WorkerRuntime(
        colony_url=colony_url,
        node_id=node,
        name=worker_name,
        agent_type=agent,
        workspace_root=ws_root,
        repo_path=repo_path,
    )
    runtime.run()


# ---------------------------------------------------------------------------
# carry
# ---------------------------------------------------------------------------


@main.command()
@click.option("--title", default=None, help="Task title.")
@click.option("--spec", default=None, help="Task specification.")
@click.option("--depends-on", multiple=True, help="Task IDs this task depends on.")
@click.option("--touches", default=None, help="Comma-separated scope tags (e.g. api,db).")
@click.option("--priority", type=int, default=10, show_default=True, help="Priority (lower=higher).")  # noqa: E501
@click.option(
    "--complexity",
    default="M",
    show_default=True,
    type=click.Choice(["S", "M", "L"]),
    help="Complexity.",
)
@click.option("--file", "file_path", default=None, help="JSON file with task payload.")
@click.option("--id", "task_id", default=None, help="Task ID (auto-generated if omitted).")
@COLONY_URL_OPTION
def carry(
    title: str | None,
    spec: str | None,
    depends_on: tuple,
    touches: str | None,
    priority: int,
    complexity: str,
    file_path: str | None,
    task_id: str | None,
    colony_url: str,
):
    """Submit a task to the colony."""
    if file_path:
        with open(file_path) as f:
            payload = json.load(f)
    else:
        if not title or not spec:
            raise click.UsageError("Either --file or both --title and --spec are required.")
        payload = {
            "title": title,
            "spec": spec,
            "depends_on": list(depends_on),
            "touches": [t.strip() for t in touches.split(",")] if touches else [],
            "priority": priority,
            "complexity": complexity,
        }

    if not task_id:
        task_id = f"task-{int(time.time() * 1000)}"
    payload["id"] = task_id

    result = _post(colony_url, "/tasks", payload)
    click.echo(f"Task created: {result}")


# ---------------------------------------------------------------------------
# scout
# ---------------------------------------------------------------------------


@main.command()
@COLONY_URL_OPTION
def scout(colony_url: str):
    """Show colony status as a table."""
    status = _get(colony_url, "/status")
    click.echo(f"{'Field':<25} {'Value'}")
    click.echo("-" * 40)
    for key, value in status.items():
        click.echo(f"{key:<25} {value}")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@main.command()
@click.option("--fix", is_flag=True, default=False, help="Apply safe auto-fixes.")
@click.option("--data-dir", default=".antfarm", show_default=True, help="Data directory.")
def doctor(fix: bool, data_dir: str):
    """Run pre-flight diagnostics on the colony data directory."""
    from antfarm.core.backends.file import FileBackend
    from antfarm.core.doctor import run_doctor

    backend = FileBackend(data_dir)
    config = {"data_dir": data_dir}
    findings = run_doctor(backend, config, fix=fix)

    if not findings:
        click.echo("All checks passed.")
        return

    for f in findings:
        status = "[FIXED]" if f.fixed else ("[AUTO-FIXABLE]" if f.auto_fixable else "")
        click.echo(f"[{f.severity.upper()}] {f.check}: {f.message} {status}".strip())


# ---------------------------------------------------------------------------
# Low-level commands
# ---------------------------------------------------------------------------


@main.command()
@click.option("--name", default=None, help="Worker name.")
@click.option("--node", required=True, help="Node ID.")
@click.option("--agent", required=True, help="Agent type.")
@click.option("--workspace-root", default=None, help="Workspace root directory.")
@COLONY_URL_OPTION
def hatch(name: str | None, node: str, agent: str, workspace_root: str | None, colony_url: str):
    """Register a worker with the colony (low-level)."""
    worker_name = name or agent
    ws_root = workspace_root or f".antfarm/workspaces/{worker_name}"
    worker_id = f"{node}/{worker_name}"
    result = _post(colony_url, "/workers/register", {
        "worker_id": worker_id,
        "node_id": node,
        "agent_type": agent,
        "workspace_root": ws_root,
    })
    click.echo(f"Worker registered: {result}")


@main.command()
@click.option("--worker-id", required=True, help="Worker ID.")
@COLONY_URL_OPTION
def forage(worker_id: str, colony_url: str):
    """Pull the next available task (low-level)."""
    result = _post(colony_url, "/tasks/pull", {"worker_id": worker_id})
    if result is None:
        click.echo("No tasks available")
    else:
        click.echo(json.dumps(result, indent=2))


@main.command()
@click.argument("task_id")
@click.argument("message")
@click.option("--worker-id", required=True, help="Worker ID.")
@COLONY_URL_OPTION
def trail(task_id: str, message: str, worker_id: str, colony_url: str):
    """Append a trail entry to a task (low-level)."""
    result = _post(colony_url, f"/tasks/{task_id}/trail", {
        "worker_id": worker_id,
        "message": message,
    })
    click.echo(f"Trail appended: {result}")


@main.command()
@click.argument("task_id")
@click.option("--pr", required=True, help="Pull request URL or identifier.")
@click.option("--attempt", default=None, help="Attempt ID.")
@click.option("--branch", default=None, help="Branch name.")
@COLONY_URL_OPTION
def harvest(task_id: str, pr: str, attempt: str | None, branch: str | None, colony_url: str):
    """Mark a task as harvested (completed) with a PR (low-level)."""
    payload: dict = {"pr": pr}
    if attempt:
        payload["attempt_id"] = attempt
    if branch:
        payload["branch"] = branch
    result = _post(colony_url, f"/tasks/{task_id}/harvest", payload)
    click.echo(f"Task harvested: {result}")


@main.command()
@click.argument("resource")
@click.option("--owner", required=True, help="Owner identifier for this guard.")
@COLONY_URL_OPTION
def guard(resource: str, owner: str, colony_url: str):
    """Acquire an exclusive guard lock on a resource (low-level)."""
    result = _post(colony_url, f"/guards/{resource}", {"owner": owner})
    click.echo(f"Guard acquired: {result}")


@main.command()
@click.argument("resource")
@click.option("--owner", required=True, help="Owner identifier releasing the guard.")
@COLONY_URL_OPTION
def release(resource: str, owner: str, colony_url: str):
    """Release a guard lock on a resource (low-level)."""
    _delete(colony_url, f"/guards/{resource}", params={"owner": owner})
    click.echo(f"Guard released: {resource}")


@main.command()
@click.argument("task_id")
@click.argument("message")
@click.option("--worker-id", required=True, help="Worker ID.")
@COLONY_URL_OPTION
def signal(task_id: str, message: str, worker_id: str, colony_url: str):
    """Send a signal message to a task (low-level)."""
    result = _post(colony_url, f"/tasks/{task_id}/signal", {
        "worker_id": worker_id,
        "message": message,
    })
    click.echo(f"Signal sent: {result}")


if __name__ == "__main__":
    main()
