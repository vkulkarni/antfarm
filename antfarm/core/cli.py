"""CLI entry point for Antfarm.

Provides all v0.1 commands: colony, join, worker start, carry, scout, doctor,
and low-level plumbing commands (hatch, forage, trail, harvest, guard, release, signal).

Colony URL resolution: every command that talks to the colony accepts
--colony-url (default http://localhost:7433, envvar ANTFARM_URL).
"""

from __future__ import annotations

import json
import sys
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

TOKEN_OPTION = click.option(
    "--token",
    default=None,
    envvar="ANTFARM_TOKEN",
    help="Bearer token for colony authentication.",
)


def _auth_headers(token: str | None) -> dict:
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _post(colony_url: str, path: str, payload: dict, token: str | None = None) -> dict | None:
    r = httpx.post(f"{colony_url.rstrip('/')}{path}", json=payload, headers=_auth_headers(token))
    r.raise_for_status()
    if r.status_code == 204:
        return None
    return r.json()


def _get(colony_url: str, path: str, token: str | None = None) -> dict:
    r = httpx.get(f"{colony_url.rstrip('/')}{path}", headers=_auth_headers(token))
    r.raise_for_status()
    return r.json()


def _delete(
    colony_url: str, path: str, params: dict | None = None, token: str | None = None
) -> None:
    r = httpx.delete(
        f"{colony_url.rstrip('/')}{path}", params=params or {}, headers=_auth_headers(token)
    )
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group()
def main():
    """Antfarm — lightweight orchestration for AI coding agents."""


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


@main.command()
def version():
    """Print the antfarm version."""
    from antfarm.core import __version__

    click.echo(f"antfarm v{__version__}")


# ---------------------------------------------------------------------------
# colony
# ---------------------------------------------------------------------------


@main.command()
@click.option("--port", default=7433, show_default=True, help="Port to listen on.")
@click.option("--host", default="0.0.0.0", show_default=True, help="Host to bind.")
@click.option("--data-dir", default=".antfarm", show_default=True, help="Data directory.")
@click.option(
    "--auth-token",
    default=None,
    envvar="ANTFARM_AUTH_TOKEN",
    help="Shared secret for bearer token auth. Enables auth on all endpoints except GET /status.",
)
@click.option(
    "--backup-dest",
    default=None,
    help="rsync/scp backup destination (e.g. user@host:/path). Enables periodic backup.",
)
@click.option(
    "--backup-interval",
    default=300,
    show_default=True,
    help="Seconds between periodic backups (requires --backup-dest).",
)
@click.option(
    "--no-soldier",
    is_flag=True,
    default=False,
    help="Disable the built-in Soldier merge engine.",
)
@click.option(
    "--no-doctor",
    is_flag=True,
    default=False,
    help="Disable the built-in Doctor health-check daemon.",
)
@click.option(
    "--no-queen",
    is_flag=True,
    default=False,
    help="Disable the Queen mission controller.",
)
@click.option(
    "--autoscaler/--no-autoscaler",
    default=False,
    show_default=True,
    help="Enable/disable the single-host autoscaler.",
)
@click.option(
    "--max-builders",
    type=int,
    default=None,
    help="Maximum parallel builder workers (pass-through to AutoscalerConfig).",
)
@click.option(
    "--max-reviewers",
    type=int,
    default=None,
    help="Maximum parallel reviewer workers (pass-through to AutoscalerConfig).",
)
@click.option(
    "--multi-node",
    is_flag=True,
    default=False,
    help="Enable multi-node autoscaler (requires --autoscaler and remote Runners).",
)
@click.option(
    "--backend",
    default="file",
    show_default=True,
    type=click.Choice(["file", "github"]),
    help="Backend type: 'file' (default) or 'github' (GitHub Issues).",
)
@click.option(
    "--github-repo",
    default=None,
    envvar="ANTFARM_GITHUB_REPO",
    help="GitHub repository in 'owner/repo' format (required for --backend=github).",
)
@click.option(
    "--github-token",
    default=None,
    envvar="ANTFARM_GITHUB_TOKEN",
    help="GitHub personal access token (for --backend=github).",
)
@click.option(
    "--repo-path",
    default=".",
    show_default=True,
    help="Path to the git repo. Used by Queen to generate mission context.",
)
@click.option(
    "--integration-branch",
    default="main",
    show_default=True,
    help="Integration branch used by Queen when generating mission context.",
)
def colony(
    port: int,
    host: str,
    data_dir: str,
    auth_token: str | None,
    backup_dest: str | None,
    backup_interval: int,
    no_soldier: bool,
    no_doctor: bool,
    no_queen: bool,
    autoscaler: bool,
    max_builders: int | None,
    max_reviewers: int | None,
    multi_node: bool,
    backend: str,
    github_repo: str | None,
    github_token: str | None,
    repo_path: str,
    integration_branch: str,
):
    """Start the colony server."""
    import os

    import uvicorn

    from antfarm.core.backends import get_backend
    from antfarm.core.logging_setup import setup_logging
    from antfarm.core.serve import get_app

    setup_logging()

    if backend == "github":
        if not github_repo:
            raise click.UsageError("--github-repo is required when --backend=github")
        task_backend = get_backend("github", repo=github_repo, token=github_token)
        click.echo(f"Using GitHub Issues backend: {github_repo}")
    else:
        task_backend = get_backend("file", root=data_dir)

    # Persist repo_path and integration_branch into config.json so Queen (and
    # other daemons that read config.json) can pick them up.
    os.makedirs(data_dir, exist_ok=True)
    _config_path = os.path.join(data_dir, "config.json")
    _existing: dict = {}
    if os.path.exists(_config_path):
        try:
            with open(_config_path) as _f:
                _existing = json.load(_f)
        except (json.JSONDecodeError, OSError):
            _existing = {}
    _existing["repo_path"] = repo_path
    _existing["integration_branch"] = integration_branch
    with open(_config_path, "w") as _f:
        json.dump(_existing, _f, indent=2)

    autoscaler_cfg = None
    if autoscaler:
        from antfarm.core.autoscaler import AutoscalerConfig

        autoscaler_cfg = AutoscalerConfig(
            enabled=True,
            data_dir=data_dir,
            colony_url=f"http://127.0.0.1:{port}",
        )
        if max_builders is not None:
            autoscaler_cfg.max_builders = max_builders
        if max_reviewers is not None:
            autoscaler_cfg.max_reviewers = max_reviewers
        if auth_token:
            autoscaler_cfg.token = auth_token

    if multi_node and autoscaler:
        click.echo("Multi-node autoscaler mode enabled (requires remote Runners on each node).")

    app = get_app(
        task_backend,
        data_dir=data_dir,
        auth_secret=auth_token,
        enable_soldier=not no_soldier,
        enable_doctor=not no_doctor,
        enable_queen=not no_queen,
        autoscaler_config=autoscaler_cfg,
    )
    if auth_token:
        from antfarm.core.auth import generate_token

        click.echo(f"Auth enabled. Bearer token: {generate_token(auth_token)}")

    if backup_dest:
        if backend == "github":
            click.echo("Warning: --backup-dest is ignored when using the GitHub backend.")
        else:
            from antfarm.core.failover import FailoverConfig, start_failover_daemon

            config = FailoverConfig(backup_dest=backup_dest, interval_seconds=backup_interval)
            start_failover_daemon(data_dir, config)
            click.echo(f"Failover enabled: backing up to {backup_dest} every {backup_interval}s")

    uvicorn.run(app, host=host, port=port)


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


@main.group()
def backup():
    """Backup and restore commands for colony data."""


@backup.command("now")
@click.option("--dest", required=True, help="rsync/scp destination (e.g. user@host:/path).")
@click.option("--data-dir", default=".antfarm", show_default=True, help="Data directory.")
@click.option(
    "--method",
    default="rsync",
    show_default=True,
    type=click.Choice(["rsync", "scp"]),
    help="Transfer method.",
)
def backup_now(dest: str, data_dir: str, method: str):
    """Run a one-shot backup of the colony data directory."""
    from antfarm.core.failover import FailoverConfig, run_backup

    config = FailoverConfig(backup_dest=dest, method=method)
    click.echo(f"Backing up {data_dir} to {dest} via {method}...")
    result = run_backup(data_dir, config)
    if result.success:
        click.echo(f"Backup succeeded at {result.timestamp}")
        if result.bytes_transferred:
            click.echo(f"Bytes transferred: {result.bytes_transferred}")
    else:
        click.echo(f"Backup failed: {result.message}", err=True)
        raise SystemExit(1)


@backup.command("restore")
@click.option("--source", required=True, help="rsync source (e.g. user@host:/path).")
@click.option("--data-dir", default=".antfarm", show_default=True, help="Data directory.")
def backup_restore(source: str, data_dir: str):
    """Restore colony data from a backup source."""
    from antfarm.core.failover import restore_from_backup

    click.echo(f"Restoring {data_dir} from {source}...")
    ok = restore_from_backup(source, data_dir)
    if ok:
        click.echo("Restore succeeded.")
    else:
        click.echo("Restore failed.", err=True)
        raise SystemExit(1)


@backup.command("status")
@click.option("--data-dir", default=".antfarm", show_default=True, help="Data directory.")
def backup_status(data_dir: str):
    """Show the last backup result from backup_status.json."""
    import os

    status_path = os.path.join(data_dir, "backup_status.json")
    if not os.path.exists(status_path):
        click.echo("No backup status found. Run 'antfarm backup now' first.")
        return

    with open(status_path) as f:
        status = json.load(f)

    ok = status.get("success", False)
    ts = status.get("timestamp", "unknown")
    msg = status.get("message", "")
    transferred = status.get("bytes_transferred", 0)

    icon = "OK" if ok else "FAIL"
    click.echo(f"[{icon}] {ts}")
    click.echo(f"  {msg}")
    if transferred:
        click.echo(f"  Bytes transferred: {transferred}")


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------


@main.command()
@click.option("--node", required=True, help="Node ID to register.")
@COLONY_URL_OPTION
@TOKEN_OPTION
def join(node: str, colony_url: str, token: str | None):
    """Register this node with the colony."""
    result = _post(colony_url, "/nodes", {"node_id": node}, token=token)
    click.echo(f"Joined colony as node '{node}': {result}")


# ---------------------------------------------------------------------------
# worker group
# ---------------------------------------------------------------------------


@main.group()
def worker():
    """Worker management commands."""


@worker.command("start")
@click.option("--agent", required=True, help="Agent type (e.g. claude-code, generic).")
@click.option("--name", default=None, help="Worker name (defaults to worker type).")
@click.option(
    "--type",
    "worker_type",
    default="builder",
    show_default=True,
    type=click.Choice(["builder", "reviewer", "planner"]),
    help="Worker type: builder (default), reviewer, or planner.",
)
@click.option("--workspace-root", default=None, help="Root directory for worktrees.")
@click.option("--node", required=True, help="Node ID this worker belongs to.")
@click.option("--repo-path", default=".", show_default=True, help="Path to git repo.")
@click.option("--integration-branch", default="main", show_default=True, help="Integration branch.")
@click.option(
    "--capabilities", default=None, help="Comma-separated worker capabilities (e.g. gpu,docker)."
)  # noqa: E501
@COLONY_URL_OPTION
@TOKEN_OPTION
def worker_start(
    agent: str,
    name: str | None,
    worker_type: str,
    workspace_root: str | None,
    node: str,
    repo_path: str,
    integration_branch: str,
    capabilities: str | None,
    colony_url: str,
    token: str | None,
):
    """Start a worker and enter the forage loop."""
    from antfarm.core.logging_setup import setup_logging
    from antfarm.core.worker import WorkerRuntime

    setup_logging()

    worker_name = name or worker_type
    ws_root = workspace_root or f".antfarm/workspaces/{worker_name}"
    caps = [c.strip() for c in capabilities.split(",")] if capabilities else []
    if worker_type == "reviewer" and "review" not in caps:
        caps.append("review")
    if worker_type == "planner" and "plan" not in caps:
        caps.append("plan")

    runtime = WorkerRuntime(
        colony_url=colony_url,
        node_id=node,
        name=worker_name,
        agent_type=agent,
        workspace_root=ws_root,
        repo_path=repo_path,
        integration_branch=integration_branch,
        capabilities=caps,
        token=token,
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
@click.option(
    "--capabilities", default=None, help="Comma-separated capabilities required (e.g. gpu,docker)."
)  # noqa: E501
@click.option(
    "--priority", type=int, default=10, show_default=True, help="Priority (lower=higher)."
)  # noqa: E501
@click.option(
    "--complexity",
    default="M",
    show_default=True,
    type=click.Choice(["S", "M", "L"]),
    help="Complexity.",
)
@click.option("--file", "file_path", default=None, help="JSON file with task payload.")
@click.option("--id", "task_id", default=None, help="Task ID (auto-generated if omitted).")
@click.option(
    "--type",
    "task_type",
    default=None,
    type=click.Choice(["plan"]),
    help="Task type: 'plan' for planner decomposition.",
)
@click.option(
    "--issue", "issue_number", default=None, type=int, help="GitHub issue number to link."
)
@click.option(
    "--mission", "mission_id", default=None, help="Attach this task to an existing mission."
)
@COLONY_URL_OPTION
@TOKEN_OPTION
def carry(
    title: str | None,
    spec: str | None,
    depends_on: tuple,
    touches: str | None,
    capabilities: str | None,
    priority: int,
    complexity: str,
    file_path: str | None,
    task_id: str | None,
    task_type: str | None,
    issue_number: int | None,
    mission_id: str | None,
    colony_url: str,
    token: str | None,
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
            "capabilities_required": [c.strip() for c in capabilities.split(",")]
            if capabilities
            else [],  # noqa: E501
            "priority": priority,
            "complexity": complexity,
        }

    if not task_id:
        task_id = f"task-{int(time.time() * 1000)}"

    if task_type == "plan":
        if not task_id.startswith("plan-"):
            task_id = f"plan-{task_id}"
        payload.setdefault("capabilities_required", [])
        if "plan" not in payload["capabilities_required"]:
            payload["capabilities_required"].append("plan")

    payload["id"] = task_id

    if mission_id:
        payload["mission_id"] = mission_id

    # Append issue reference to spec so workers include it in commits
    if issue_number:
        payload.setdefault("spec", "")
        payload["spec"] += f"\n\nGitHub Issue: #{issue_number}"

    result = _post(colony_url, "/tasks", payload, token=token)
    click.echo(f"Task created: {result}")


# ---------------------------------------------------------------------------
# scout
# ---------------------------------------------------------------------------

_WATCHED_FIELDS = (
    "tasks_ready",
    "tasks_active",
    "tasks_done",
    "tasks_paused",
    "tasks_blocked",
    "workers",
    "nodes",
)


def _render_scout(status: dict, prev: dict | None) -> None:
    """Render a status table, highlighting changes vs prev snapshot."""
    click.echo(f"{'Field':<25} {'Value'}")
    click.echo("-" * 40)
    for key, value in status.items():
        value_str = str(value)
        if prev is not None and key in prev:
            prev_val = prev[key]
            if isinstance(value, (int, float)) and isinstance(prev_val, (int, float)):
                if value > prev_val:
                    value_str = click.style(value_str, fg="green")
                elif value < prev_val:
                    value_str = click.style(value_str, fg="red")
        click.echo(f"{key:<25} {value_str}")


@main.command()
@click.option("--watch", is_flag=True, default=False, help="Re-poll continuously.")
@click.option("--tui", is_flag=True, default=False, help="Launch rich TUI dashboard.")
@click.option(
    "--interval",
    default=5,
    show_default=True,
    help="Seconds between polls (with --watch).",
)
@click.option(
    "--refresh",
    default=2.0,
    show_default=True,
    help="Seconds between TUI refreshes (with --tui).",
)
@COLONY_URL_OPTION
@TOKEN_OPTION
def scout(
    watch: bool,
    tui: bool,
    interval: int,
    refresh: float,
    colony_url: str,
    token: str | None,
):
    """Show colony status as a table."""
    if tui:
        from antfarm.core.tui import AntfarmTUI

        dashboard = AntfarmTUI(colony_url=colony_url, token=token, refresh_interval=refresh)
        dashboard.run()
        return

    if not watch:
        status = _get(colony_url, "/status", token=token)
        _render_scout(status, None)
        return

    prev_status = None
    try:
        while True:
            click.clear()
            status = _get(colony_url, "/status", token=token)
            _render_scout(status, prev_status)
            prev_status = status
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# inbox
# ---------------------------------------------------------------------------


@main.command()
@COLONY_URL_OPTION
@TOKEN_OPTION
def inbox(colony_url: str, token: str | None):
    """Show items needing operator attention."""
    from antfarm.core.inbox import collect_inbox_items

    data = _get(colony_url, "/status/full", token=token)
    items = collect_inbox_items(
        tasks=data.get("tasks", []),
        workers=data.get("workers", []),
    )
    if not items:
        click.echo("Inbox empty — everything healthy.")
        return

    severity_colors = {"error": "red", "warning": "yellow", "info": "blue"}
    for item in items:
        color = severity_colors.get(item["severity"], "white")
        label = click.style(f"[{item['severity'].upper()}]", fg=color)
        click.echo(f"{label} {item['type']}: {item['message']}")
        click.echo(f"  → {item['action']}")


# ---------------------------------------------------------------------------
# scent
# ---------------------------------------------------------------------------


@main.command()
@click.argument("task_id")
@click.option("--poll-interval", default=1.0, show_default=True, help="Seconds between polls.")
@COLONY_URL_OPTION
@TOKEN_OPTION
def scent(task_id: str, poll_interval: float, colony_url: str, token: str | None):
    """Stream real-time trail entries for a task (SSE)."""
    url = f"{colony_url.rstrip('/')}/scent/{task_id}"
    params = {"poll_interval": poll_interval}
    headers = _auth_headers(token)
    try:
        with httpx.stream("GET", url, params=params, headers=headers) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line.startswith("data: "):
                    raw = line[len("data: ") :]
                    try:
                        entry = json.loads(raw)
                        ts = entry.get("ts", "")
                        worker = entry.get("worker_id", "")
                        message = entry.get("message", "")
                        click.echo(f"[{ts}] {worker}: {message}")
                    except json.JSONDecodeError:
                        click.echo(line)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@main.command()
@click.option("--fix", is_flag=True, default=False, help="Apply safe auto-fixes.")
@click.option("--data-dir", default=".antfarm", show_default=True, help="Data directory.")
@click.option(
    "--sweep-legacy-tmux",
    is_flag=True,
    default=False,
    help=(
        "Kill pre-hash tmux sessions (auto-/runner-/antfarm- without colony hash). "
        "Operates host-wide, not scoped to a colony."
    ),
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt for --sweep-legacy-tmux.",
)
def doctor(fix: bool, data_dir: str, sweep_legacy_tmux: bool, yes: bool):
    """Run pre-flight diagnostics on the colony data directory."""
    import os

    from antfarm.core.backends.file import FileBackend
    from antfarm.core.doctor import run_doctor, sweep_legacy_tmux_sessions

    if sweep_legacy_tmux and fix:
        click.echo(
            "Error: --fix and --sweep-legacy-tmux have different safety profiles and "
            "cannot be combined. Run them separately.",
            err=True,
        )
        sys.exit(2)

    if sweep_legacy_tmux:
        from antfarm.core.process_manager import colony_id, colony_session_hash

        cid = colony_id(data_dir)
        h = colony_session_hash(data_dir)
        real = os.path.realpath(data_dir)
        click.echo(f"Colony id: {cid} hash: {h} (data_dir: {real})")
        click.echo(
            "Legacy sessions (no hash) will be killed; all hashed sessions will be untouched."
        )
        # Dry-run preview first so the operator sees exactly what will die.
        preview = sweep_legacy_tmux_sessions({"data_dir": data_dir}, confirmed=False)
        if not preview:
            click.echo("No legacy sessions found.")
            return
        for f in preview:
            click.echo(f"[{f.severity.upper()}] {f.check}: {f.message}")
        if not yes and not click.confirm(f"Kill {len(preview)} legacy sessions?", default=False):
            click.echo("Aborted.")
            return
        killed = sweep_legacy_tmux_sessions({"data_dir": data_dir}, confirmed=True)
        for f in killed:
            status = "[FIXED]" if f.fixed else "[FAILED]"
            click.echo(f"[{f.severity.upper()}] {f.check}: {f.message} {status}".strip())
        return

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
@click.option(
    "--capabilities", default=None, help="Comma-separated worker capabilities (e.g. gpu,docker)."
)  # noqa: E501
@COLONY_URL_OPTION
@TOKEN_OPTION
def hatch(
    name: str | None,
    node: str,
    agent: str,
    workspace_root: str | None,
    capabilities: str | None,
    colony_url: str,
    token: str | None,
):
    """Register a worker with the colony (low-level)."""
    worker_name = name or agent
    ws_root = workspace_root or f".antfarm/workspaces/{worker_name}"
    worker_id = f"{node}/{worker_name}"
    caps = [c.strip() for c in capabilities.split(",")] if capabilities else []
    result = _post(
        colony_url,
        "/workers/register",
        {
            "worker_id": worker_id,
            "node_id": node,
            "agent_type": agent,
            "workspace_root": ws_root,
            "capabilities": caps,
        },
        token=token,
    )
    click.echo(f"Worker registered: {result}")


@main.command()
@click.option("--worker-id", required=True, help="Worker ID.")
@COLONY_URL_OPTION
@TOKEN_OPTION
def forage(worker_id: str, colony_url: str, token: str | None):
    """Pull the next available task (low-level)."""
    result = _post(colony_url, "/tasks/pull", {"worker_id": worker_id}, token=token)
    if result is None:
        click.echo("No tasks available")
    else:
        click.echo(json.dumps(result, indent=2))


@main.command()
@click.argument("task_id")
@click.argument("message")
@click.option("--worker-id", required=True, help="Worker ID.")
@COLONY_URL_OPTION
@TOKEN_OPTION
def trail(task_id: str, message: str, worker_id: str, colony_url: str, token: str | None):
    """Append a trail entry to a task (low-level)."""
    result = _post(
        colony_url,
        f"/tasks/{task_id}/trail",
        {
            "worker_id": worker_id,
            "message": message,
        },
        token=token,
    )
    click.echo(f"Trail appended: {result}")


@main.command()
@click.argument("task_id")
@click.option("--pr", required=True, help="Pull request URL or identifier.")
@click.option("--attempt", default=None, help="Attempt ID.")
@click.option("--branch", default=None, help="Branch name.")
@COLONY_URL_OPTION
@TOKEN_OPTION
def harvest(
    task_id: str,
    pr: str,
    attempt: str | None,
    branch: str | None,
    colony_url: str,
    token: str | None,
):
    """Mark a task as harvested (completed) with a PR (low-level)."""
    payload: dict = {"pr": pr}
    if attempt:
        payload["attempt_id"] = attempt
    if branch:
        payload["branch"] = branch
    result = _post(colony_url, f"/tasks/{task_id}/harvest", payload, token=token)
    click.echo(f"Task harvested: {result}")


@main.command()
@click.argument("resource")
@click.option("--owner", required=True, help="Owner identifier for this guard.")
@COLONY_URL_OPTION
@TOKEN_OPTION
def guard(resource: str, owner: str, colony_url: str, token: str | None):
    """Acquire an exclusive guard lock on a resource (low-level)."""
    result = _post(colony_url, f"/guards/{resource}", {"owner": owner}, token=token)
    click.echo(f"Guard acquired: {result}")


@main.command()
@click.argument("resource")
@click.option("--owner", required=True, help="Owner identifier releasing the guard.")
@COLONY_URL_OPTION
@TOKEN_OPTION
def release(resource: str, owner: str, colony_url: str, token: str | None):
    """Release a guard lock on a resource (low-level)."""
    _delete(colony_url, f"/guards/{resource}", params={"owner": owner}, token=token)
    click.echo(f"Guard released: {resource}")


@main.command()
@click.argument("task_id")
@click.argument("message")
@click.option("--worker-id", required=True, help="Worker ID.")
@COLONY_URL_OPTION
@TOKEN_OPTION
def signal(task_id: str, message: str, worker_id: str, colony_url: str, token: str | None):
    """Send a signal message to a task (low-level)."""
    result = _post(
        colony_url,
        f"/tasks/{task_id}/signal",
        {
            "worker_id": worker_id,
            "message": message,
        },
        token=token,
    )
    click.echo(f"Signal sent: {result}")


# ---------------------------------------------------------------------------
# Human override commands
# ---------------------------------------------------------------------------


@main.command()
@click.argument("task_id")
@COLONY_URL_OPTION
@TOKEN_OPTION
def pause(task_id: str, colony_url: str, token: str | None):
    """Pause an active task."""
    result = _post(colony_url, f"/tasks/{task_id}/pause", {}, token=token)
    click.echo(f"Task paused: {result}")


@main.command()
@click.argument("task_id")
@COLONY_URL_OPTION
@TOKEN_OPTION
def resume(task_id: str, colony_url: str, token: str | None):
    """Resume a paused task."""
    result = _post(colony_url, f"/tasks/{task_id}/resume", {}, token=token)
    click.echo(f"Task resumed: {result}")


@main.command()
@click.argument("task_id")
@click.argument("worker_id")
@COLONY_URL_OPTION
@TOKEN_OPTION
def reassign(task_id: str, worker_id: str, colony_url: str, token: str | None):
    """Reassign an active task to a different worker."""
    result = _post(colony_url, f"/tasks/{task_id}/reassign", {"worker_id": worker_id}, token=token)
    click.echo(f"Task reassigned: {result}")


@main.command()
@click.argument("task_id")
@click.argument("reason")
@COLONY_URL_OPTION
@TOKEN_OPTION
def block(task_id: str, reason: str, colony_url: str, token: str | None):
    """Block a ready task with a reason."""
    result = _post(colony_url, f"/tasks/{task_id}/block", {"reason": reason}, token=token)
    click.echo(f"Task blocked: {result}")


@main.command()
@click.argument("task_id")
@COLONY_URL_OPTION
@TOKEN_OPTION
def unblock(task_id: str, colony_url: str, token: str | None):
    """Unblock a blocked task."""
    result = _post(colony_url, f"/tasks/{task_id}/unblock", {}, token=token)
    click.echo(f"Task unblocked: {result}")


@main.command()
@click.argument("task_id")
@click.argument("worker_id")
@TOKEN_OPTION
@COLONY_URL_OPTION
def pin(task_id: str, worker_id: str, token: str | None, colony_url: str):
    """Pin a ready task to a specific worker."""
    result = _post(colony_url, f"/tasks/{task_id}/pin", {"worker_id": worker_id}, token=token)
    click.echo(f"Task pinned: {result}")


@main.command()
@click.argument("task_id")
@TOKEN_OPTION
@COLONY_URL_OPTION
def unpin(task_id: str, token: str | None, colony_url: str):
    """Clear the pin on a ready task."""
    result = _post(colony_url, f"/tasks/{task_id}/unpin", {}, token=token)
    click.echo(f"Task unpinned: {result}")


@main.command("override-order")
@click.argument("task_id")
@click.argument("position", type=int)
@TOKEN_OPTION
@COLONY_URL_OPTION
def override_order(task_id: str, position: int, token: str | None, colony_url: str):
    """Override merge queue position for a done task. Lower position merges first."""
    result = _post(
        colony_url, f"/tasks/{task_id}/override-order", {"position": position}, token=token
    )
    click.echo(f"Merge order overridden: {result}")


@main.command("clear-override-order")
@click.argument("task_id")
@TOKEN_OPTION
@COLONY_URL_OPTION
def clear_override_order(task_id: str, token: str | None, colony_url: str):
    """Clear merge queue position override for a done task."""
    _delete(colony_url, f"/tasks/{task_id}/override-order", token=token)
    click.echo(f"Merge order override cleared for task: {task_id}")


# ---------------------------------------------------------------------------
# workers
# ---------------------------------------------------------------------------


@main.command("workers")
@COLONY_URL_OPTION
@TOKEN_OPTION
def workers_list(colony_url: str, token: str | None):
    """List all registered workers and their rate limit status."""
    data = _get(colony_url, "/workers", token=token)
    if not data:
        click.echo("No workers registered.")
        return
    click.echo(f"{'Worker ID':<35} {'Status':<10} {'Cooldown Until'}")
    click.echo("-" * 75)
    for w in data:
        worker_id = w.get("worker_id", "unknown")
        status = w.get("status", "unknown")
        cooldown = w.get("cooldown_until") or "-"
        click.echo(f"{worker_id:<35} {status:<10} {cooldown}")


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--fleet-config",
    default=".antfarm/fleet.json",
    show_default=True,
    help="Path to fleet configuration JSON file.",
)
@click.option("--status", "show_status", is_flag=True, default=False, help="Show deploy status.")
@click.option(
    "--integration-branch",
    default="dev",
    show_default=True,
    help="Integration branch for workers.",
)
@COLONY_URL_OPTION
def deploy(
    fleet_config: str,
    show_status: bool,
    integration_branch: str,
    colony_url: str,
):
    """Deploy workers to remote nodes via SSH, or check deploy status.

    Session names are scoped by hash(realpath(config) | colony_url). See UPGRADE.md
    for the full deploy identity model (shared vs isolated namespaces).
    """
    from antfarm.core.deploy import deploy as run_deploy
    from antfarm.core.deploy import deploy_status

    if show_status:
        status = deploy_status(fleet_config, colony_url)
        for node_id, sessions in status.items():
            if sessions:
                click.echo(f"{node_id}: {len(sessions)} session(s) running")
                for s in sessions:
                    click.echo(f"  - {s}")
            else:
                click.echo(f"{node_id}: no sessions running")
        return

    results = run_deploy(fleet_config, colony_url, integration_branch)
    for r in results:
        icon = "OK" if r.success else "FAIL"
        click.echo(f"[{icon}] {r.node_id} ({r.host}) worker-{r.worker_index}: {r.message}")


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


@main.command("import")
@click.option("--from", "source", required=True, type=click.Choice(["github", "json"]))
@click.option("--repo", default=None)
@click.option("--file", "file_path", default=None)
@click.option("--github-token", default=None, envvar="GITHUB_TOKEN")
@click.option("--label", default=None, help="GitHub label filter")
@click.option("--dry-run", is_flag=True)
@COLONY_URL_OPTION
@TOKEN_OPTION
def import_cmd(
    source: str,
    repo: str | None,
    file_path: str | None,
    github_token: str | None,
    label: str | None,
    dry_run: bool,
    colony_url: str,
    token: str | None,
):
    """Import tasks from an external source into the colony."""
    from antfarm.core.importers.github import GitHubImporter
    from antfarm.core.importers.json_file import JsonFileImporter

    if source == "github":
        if not repo:
            raise click.UsageError("--repo is required when --from=github")
        importer = GitHubImporter(repo=repo, token=github_token, label=label)
    else:
        if not file_path:
            raise click.UsageError("--file is required when --from=json")
        importer = JsonFileImporter(file_path=file_path)

    tasks = importer.import_tasks()

    if not tasks:
        click.echo("No tasks found.")
        return

    for task in tasks:
        if dry_run:
            click.echo(json.dumps(task, indent=2))
        else:
            if "id" not in task:
                task["id"] = f"task-{int(time.time() * 1000)}"
            result = _post(colony_url, "/tasks", task, token=token)
            click.echo(f"Imported: {result}")

    click.echo(f"{'[dry-run] Would import' if dry_run else 'Imported'} {len(tasks)} task(s).")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@main.command()
@click.option("--spec", default=None, help="Inline spec text to decompose.")
@click.option("--file", "file_path", default=None, help="Read spec from file.")
@click.option("--carry", "auto_carry", is_flag=True, help="Carry tasks after approval.")
@click.option("--data-dir", default=".antfarm", help="Path to .antfarm directory.")
@click.option("--agent", "agent_cmd", default=None, help="AI agent command (e.g. 'claude -p').")
@COLONY_URL_OPTION
@TOKEN_OPTION
def plan(
    spec: str | None,
    file_path: str | None,
    auto_carry: bool,
    data_dir: str,
    agent_cmd: str | None,
    colony_url: str,
    token: str | None,
):
    """Decompose a spec into tasks with dependencies and scope hints."""
    from antfarm.core.planner import PlannerEngine, resolve_dependencies

    if not spec and not file_path:
        raise click.UsageError("Either --spec or --file is required.")

    agent_command = agent_cmd.split() if agent_cmd else None
    engine = PlannerEngine(data_dir=data_dir, agent_command=agent_command)

    result = engine.plan_from_file(file_path) if file_path else engine.plan_from_spec(spec)

    if not result.tasks:
        click.echo("No tasks generated.")
        if result.warnings:
            for w in result.warnings:
                click.echo(f"  WARNING: {w}")
        return

    # Validate
    errors = engine.validate_plan(result)
    if errors:
        click.echo("Validation errors:")
        for e in errors:
            click.echo(f"  ERROR: {e}")
        return

    # Display proposed tasks
    click.echo(f"\nProposed tasks ({len(result.tasks)}):")
    for i, task in enumerate(result.tasks, 1):
        deps = ", ".join(task.depends_on) if task.depends_on else "none"
        touches = ", ".join(task.touches) if task.touches else "none"
        click.echo(f"  {i}. [{task.complexity}] {task.title}")
        click.echo(f"     touches: {touches}  deps: {deps}")

    if result.warnings:
        click.echo("\nWarnings:")
        for w in result.warnings:
            click.echo(f"  - {w}")

    if not auto_carry:
        click.echo("\nUse --carry to submit these tasks to the colony.")
        return

    # Carry tasks — pre-generate IDs and resolve index-based deps
    click.echo("\nCarrying tasks...")
    base_ts = int(time.time() * 1000)
    task_ids = [f"task-{base_ts}-{i}" for i in range(1, len(result.tasks) + 1)]
    resolved_tasks = resolve_dependencies(result.tasks, task_ids)
    for i, (task, tid) in enumerate(zip(resolved_tasks, task_ids, strict=True), 1):
        payload = task.to_carry_dict(tid)
        try:
            r = _post(colony_url, "/tasks", payload, token=token)
            click.echo(f"  Created: {r}")
        except Exception as exc:
            click.echo(f"  Failed to carry task {i}: {exc}")


# ---------------------------------------------------------------------------
# mission
# ---------------------------------------------------------------------------


@main.group()
def mission():
    """Manage autonomous missions."""


@mission.command("create")
@click.option(
    "--spec", "spec_path", required=True, type=click.Path(exists=True), help="Path to spec file."
)
@click.option("--mission-id", default=None, help="Optional explicit mission id slug.")
@click.option("--no-plan-review", is_flag=True, default=False, help="Skip plan review step.")
@click.option("--max-builders", type=int, default=None, help="Max parallel builder workers.")
@click.option("--max-attempts", type=int, default=None, help="Max attempts per task.")
@click.option("--integration-branch", default=None, help="Integration branch for this mission.")
@COLONY_URL_OPTION
@TOKEN_OPTION
def mission_create(
    spec_path: str,
    mission_id: str | None,
    no_plan_review: bool,
    max_builders: int | None,
    max_attempts: int | None,
    integration_branch: str | None,
    colony_url: str,
    token: str | None,
):
    """Create an autonomous mission from a spec file."""
    with open(spec_path) as f:
        spec_text = f.read()

    payload: dict = {"spec": spec_text, "spec_file": spec_path}
    config: dict = {}
    if no_plan_review:
        config["require_plan_review"] = False
    if max_builders is not None:
        config["max_parallel_builders"] = max_builders
    if max_attempts is not None:
        config["max_attempts"] = max_attempts
    if integration_branch is not None:
        config["integration_branch"] = integration_branch
    if config:
        payload["config"] = config
    if mission_id:
        payload["mission_id"] = mission_id

    result = _post(colony_url, "/missions", payload, token=token)
    click.echo(f"Mission created: {result}")


@mission.command("status")
@click.argument("mission_id")
@COLONY_URL_OPTION
@TOKEN_OPTION
def mission_status(mission_id: str, colony_url: str, token: str | None):
    """Show mission status overview."""
    data = _get(colony_url, f"/missions/{mission_id}", token=token)

    click.echo(f"Mission:    {data.get('mission_id', mission_id)}")
    click.echo(f"Status:     {data.get('status', 'unknown')}")

    config = data.get("config", {})
    completion_mode = config.get("completion_mode", "best_effort")
    if completion_mode == "all_or_nothing":
        click.echo(f"Completion: {completion_mode} (treated as best_effort in v0.6.0)")
    else:
        click.echo(f"Completion: {completion_mode}")

    task_ids = data.get("task_ids", [])
    click.echo(f"Tasks:      {len(task_ids)}")
    click.echo(f"Created:    {data.get('created_at', 'unknown')}")

    if data.get("plan_task_id"):
        click.echo(f"Plan task:  {data['plan_task_id']}")
    if data.get("spec_file"):
        click.echo(f"Spec file:  {data['spec_file']}")


@mission.command("report")
@click.argument("mission_id")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["terminal", "md", "json"]),
    default="terminal",
    show_default=True,
    help="Output format.",
)
@COLONY_URL_OPTION
@TOKEN_OPTION
def mission_report(mission_id: str, fmt: str, colony_url: str, token: str | None):
    """Show mission report."""
    data = _get(colony_url, f"/missions/{mission_id}/report", token=token)

    if fmt == "json":
        click.echo(json.dumps(data, indent=2))
    elif fmt == "md":
        click.echo(f"# Mission Report: {data.get('mission_id', mission_id)}")
        click.echo(f"\n**Status:** {data.get('status', 'unknown')}")
        click.echo(f"**Duration:** {data.get('duration', 'unknown')}")
        tasks = data.get("tasks", [])
        if tasks:
            click.echo("\n## Tasks\n")
            for t in tasks:
                status = t.get("status", "unknown")
                click.echo(f"- **{t.get('title', t.get('id', '?'))}** — {status}")
    else:
        click.echo(f"Mission:  {data.get('mission_id', mission_id)}")
        click.echo(f"Status:   {data.get('status', 'unknown')}")
        click.echo(f"Duration: {data.get('duration', 'unknown')}")
        tasks = data.get("tasks", [])
        if tasks:
            click.echo(f"\n{'Task':<30} {'Status':<12}")
            click.echo("-" * 42)
            for t in tasks:
                title = t.get("title", t.get("id", "?"))[:28]
                status = t.get("status", "unknown")
                click.echo(f"{title:<30} {status:<12}")


@mission.command("cancel")
@click.argument("mission_id")
@COLONY_URL_OPTION
@TOKEN_OPTION
def mission_cancel(mission_id: str, colony_url: str, token: str | None):
    """Cancel a mission."""
    _post(colony_url, f"/missions/{mission_id}/cancel", {}, token=token)
    click.echo(f"Mission cancelled: {mission_id}")


@mission.command("list")
@click.option("--status", "status_filter", default=None, help="Filter by mission status.")
@COLONY_URL_OPTION
@TOKEN_OPTION
def mission_list(status_filter: str | None, colony_url: str, token: str | None):
    """List all missions."""
    path = "/missions"
    if status_filter:
        path += f"?status={status_filter}"
    data = _get(colony_url, path, token=token)

    if not data:
        click.echo("No missions found.")
        return

    click.echo(f"{'Mission ID':<35} {'Status':<15} {'Tasks'}")
    click.echo("-" * 60)
    for m in data:
        mid = m.get("mission_id", "?")
        status = m.get("status", "?")
        task_count = len(m.get("task_ids", []))
        click.echo(f"{mid:<35} {status:<15} {task_count}")


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


@main.group()
def memory():
    """View and manage repo memory."""
    pass


@memory.command("show")
@click.option("--data-dir", default=".antfarm", help="Path to .antfarm directory.")
def memory_show(data_dir: str):
    """Show current repo memory state."""
    from antfarm.core.memory import MemoryStore

    store = MemoryStore(data_dir)

    facts = store.get_facts()
    click.echo("Repo Facts:")
    if facts:
        for k, v in facts.items():
            click.echo(f"  {k}: {v}")
    else:
        click.echo("  (none)")

    hotspots = store.get_hotspots()
    click.echo("\nHotspots:")
    if hotspots:
        for scope, score in sorted(hotspots.items(), key=lambda x: -x[1]):
            click.echo(f"  {scope}: {score:.3f}")
    else:
        click.echo("  (none)")

    patterns = store.get_failure_patterns()
    click.echo("\nFailure Patterns:")
    if patterns:
        for ft, count in sorted(patterns.items(), key=lambda x: -x[1]):
            click.echo(f"  {ft}: {count}")
    else:
        click.echo("  (none)")

    outcomes = store.get_outcomes(limit=5)
    click.echo(f"\nRecent Outcomes ({len(outcomes)}):")
    for o in outcomes:
        status = "OK" if o.get("success") else "FAIL"
        click.echo(f"  [{status}] {o.get('task_id')} ({o.get('failure_type', '-')})")


@memory.command("set-fact")
@click.argument("key")
@click.argument("value")
@click.option("--data-dir", default=".antfarm", help="Path to .antfarm directory.")
def memory_set_fact(key: str, value: str, data_dir: str):
    """Set a repo fact (e.g. language, test_command)."""
    from antfarm.core.memory import MemoryStore

    store = MemoryStore(data_dir)
    store.set_fact(key, value)
    click.echo(f"Set fact: {key} = {value}")


@memory.command("detect")
@click.option("--repo", default=".", help="Path to repo root.")
@click.option("--data-dir", default=".antfarm", help="Path to .antfarm directory.")
def memory_detect(repo: str, data_dir: str):
    """Auto-detect repo facts from project structure."""
    from antfarm.core.memory import MemoryStore

    store = MemoryStore(data_dir)
    detected = store.detect_facts(repo)
    if detected:
        click.echo("Detected facts:")
        for k, v in detected.items():
            click.echo(f"  {k}: {v}")
    else:
        click.echo("No facts detected.")


@memory.command("recompute")
@click.option("--data-dir", default=".antfarm", help="Path to .antfarm directory.")
def memory_recompute(data_dir: str):
    """Recompute hotspots and failure patterns from recent outcomes."""
    from antfarm.core.memory import MemoryStore

    store = MemoryStore(data_dir)
    hotspots = store.recompute_hotspots()
    patterns = store.recompute_failure_patterns()
    click.echo(f"Recomputed: {len(hotspots)} hotspots, {len(patterns)} failure patterns.")


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


@main.command()
@click.option("--colony-url", required=True, help="Colony server URL.")
@click.option("--repo-path", required=True, help="Path to git repository.")
@click.option("--workspace-root", default=None, help="Root directory for worktrees.")
@click.option("--node", default=None, help="Node ID (default: hostname).")
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address. Default: loopback only. WARNING: no auth.",
)
@click.option("--port", default=7434, type=int, show_default=True, help="Port to listen on.")
@click.option("--max-workers", default=4, type=int, show_default=True, help="Max worker processes.")
@click.option("--agent", default="claude-code", show_default=True, help="Agent type.")
@click.option("--integration-branch", default="main", show_default=True, help="Integration branch.")
@click.option("--capabilities", default="", help="Comma-separated: gpu,docker")
@click.option("--token", default=None, envvar="ANTFARM_TOKEN", help="Bearer token for colony auth.")
def runner(
    colony_url: str,
    repo_path: str,
    workspace_root: str | None,
    node: str | None,
    host: str,
    port: int,
    max_workers: int,
    agent: str,
    integration_branch: str,
    capabilities: str,
    token: str | None,
):
    """Start a Runner daemon on this machine.

    The Runner API has no authentication. Bind to loopback (default) or a
    private LAN address only. Do not expose to untrusted networks.
    """
    import os
    import socket

    from antfarm.core.logging_setup import setup_logging
    from antfarm.core.runner import Runner

    setup_logging()

    node_id = node or socket.gethostname()
    caps = [c.strip() for c in capabilities.split(",") if c.strip()] if capabilities else []
    ws_root = workspace_root or os.path.join(repo_path, ".antfarm", "workspaces")

    r = Runner(
        node_id=node_id,
        colony_url=colony_url,
        repo_path=repo_path,
        workspace_root=ws_root,
        integration_branch=integration_branch,
        max_workers=max_workers,
        capabilities=caps,
        host=host,
        port=port,
        agent_type=agent,
        token=token,
    )
    r.run()


if __name__ == "__main__":
    main()
