"""Deploy command for Antfarm.

Reads a fleet configuration file and starts workers on remote machines via SSH.
Each worker is launched inside a tmux session for persistence and easy monitoring.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class NodeConfig:
    node_id: str
    host: str
    ssh_user: str
    repo_path: str
    agent_type: str
    count: int = 1


@dataclass
class DeployResult:
    node_id: str
    host: str
    worker_index: int
    success: bool
    message: str


def load_fleet_config(config_path: str) -> list[NodeConfig]:
    """Load fleet configuration from a JSON file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Fleet config not found: {config_path}")

    with open(path) as f:
        data = json.load(f)

    nodes = data if isinstance(data, list) else data.get("nodes", [])
    configs = []
    for entry in nodes:
        configs.append(NodeConfig(
            node_id=entry["node_id"],
            host=entry["host"],
            ssh_user=entry["ssh_user"],
            repo_path=entry["repo_path"],
            agent_type=entry["agent_type"],
            count=entry.get("count", 1),
        ))
    return configs


def _build_worker_command(
    node: NodeConfig,
    worker_index: int,
    colony_url: str,
    integration_branch: str,
) -> str:
    """Build the antfarm worker start command for a remote node."""
    worker_name = f"{node.agent_type}-{worker_index}"
    return (
        f"cd {shlex.quote(node.repo_path)} && "
        f"antfarm worker start "
        f"--agent {shlex.quote(node.agent_type)} "
        f"--name {shlex.quote(worker_name)} "
        f"--node {shlex.quote(node.node_id)} "
        f"--colony-url {shlex.quote(colony_url)} "
        f"--integration-branch {shlex.quote(integration_branch)}"
    )


def _build_ssh_command(
    node: NodeConfig,
    worker_index: int,
    colony_url: str,
    integration_branch: str,
) -> list[str]:
    """Build the full SSH command that launches a worker in a tmux session."""
    worker_cmd = _build_worker_command(node, worker_index, colony_url, integration_branch)
    session_name = f"antfarm-{node.node_id}-{node.agent_type}-{worker_index}"
    # -A: attach-or-create to avoid duplicate session errors
    tmux_cmd = f"tmux new-session -A -d -s {shlex.quote(session_name)} {shlex.quote(worker_cmd)}"
    return [
        "ssh",
        f"{node.ssh_user}@{node.host}",
        tmux_cmd,
    ]


def deploy(
    config_path: str,
    colony_url: str = "http://localhost:7433",
    integration_branch: str = "main",
) -> list[DeployResult]:
    """Deploy workers to remote nodes according to fleet config.

    For each node in the config, launches `count` workers via SSH in tmux sessions.
    """
    nodes = load_fleet_config(config_path)
    results: list[DeployResult] = []

    for node in nodes:
        for i in range(node.count):
            ssh_cmd = _build_ssh_command(node, i, colony_url, integration_branch)
            logger.info("Deploying worker %d to %s@%s", i, node.ssh_user, node.host)
            try:
                subprocess.run(ssh_cmd, check=True, capture_output=True, text=True, timeout=30)
                results.append(DeployResult(
                    node_id=node.node_id,
                    host=node.host,
                    worker_index=i,
                    success=True,
                    message="Worker launched successfully",
                ))
            except subprocess.CalledProcessError as e:
                results.append(DeployResult(
                    node_id=node.node_id,
                    host=node.host,
                    worker_index=i,
                    success=False,
                    message=f"SSH failed: {e.stderr.strip() or e.stdout.strip() or str(e)}",
                ))
            except subprocess.TimeoutExpired:
                results.append(DeployResult(
                    node_id=node.node_id,
                    host=node.host,
                    worker_index=i,
                    success=False,
                    message="SSH connection timed out",
                ))

    return results


def _build_status_ssh_command(node: NodeConfig) -> list[str]:
    """Build SSH command to list antfarm tmux sessions on a remote node."""
    return [
        "ssh",
        f"{node.ssh_user}@{node.host}",
        "tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^antfarm-' || true",
    ]


def deploy_status(config_path: str) -> dict[str, list[str]]:
    """Check which antfarm tmux sessions are running on each node.

    Returns a dict mapping node_id to a list of active tmux session names.
    """
    nodes = load_fleet_config(config_path)
    status: dict[str, list[str]] = {}

    for node in nodes:
        ssh_cmd = _build_status_ssh_command(node)
        try:
            result = subprocess.run(
                ssh_cmd, capture_output=True, text=True, timeout=10
            )
            sessions = [
                line.strip()
                for line in result.stdout.strip().splitlines()
                if line.strip()
            ]
            status[node.node_id] = sessions
        except subprocess.TimeoutExpired:
            status[node.node_id] = []

    return status
