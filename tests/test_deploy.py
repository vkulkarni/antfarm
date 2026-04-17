"""Tests for antfarm.core.deploy — deploy command with mocked SSH."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from antfarm.core.cli import main
from antfarm.core.deploy import (
    NodeConfig,
    _build_ssh_command,
    _build_status_ssh_command,
    _build_worker_command,
    _colony_prefix,
    deploy,
    deploy_status,
    load_fleet_config,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fleet_config_file(tmp_path):
    """Create a temporary fleet config file."""
    config = [
        {
            "node_id": "node-1",
            "host": "10.0.0.1",
            "ssh_user": "deploy",
            "repo_path": "/opt/antfarm",
            "agent_type": "claude-code",
            "count": 2,
        },
        {
            "node_id": "node-2",
            "host": "10.0.0.2",
            "ssh_user": "deploy",
            "repo_path": "/opt/antfarm",
            "agent_type": "generic",
            "count": 1,
        },
    ]
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(config))
    return str(path)


@pytest.fixture()
def fleet_config_dict_file(tmp_path):
    """Create a fleet config with {nodes: [...]} format."""
    config = {
        "nodes": [
            {
                "node_id": "node-1",
                "host": "10.0.0.1",
                "ssh_user": "deploy",
                "repo_path": "/opt/antfarm",
                "agent_type": "claude-code",
                "count": 1,
            },
        ]
    }
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(config))
    return str(path)


# ---------------------------------------------------------------------------
# Unit tests: config loading
# ---------------------------------------------------------------------------


def test_load_fleet_config_list_format(fleet_config_file):
    nodes = load_fleet_config(fleet_config_file)
    assert len(nodes) == 2
    assert nodes[0].node_id == "node-1"
    assert nodes[0].host == "10.0.0.1"
    assert nodes[0].count == 2
    assert nodes[1].agent_type == "generic"
    assert nodes[1].count == 1


def test_load_fleet_config_dict_format(fleet_config_dict_file):
    nodes = load_fleet_config(fleet_config_dict_file)
    assert len(nodes) == 1
    assert nodes[0].node_id == "node-1"


def test_load_fleet_config_missing_file():
    with pytest.raises(FileNotFoundError):
        load_fleet_config("/nonexistent/fleet.json")


def test_load_fleet_config_default_count(tmp_path):
    config = [{"node_id": "n1", "host": "h", "ssh_user": "u", "repo_path": "/r", "agent_type": "a"}]
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(config))
    nodes = load_fleet_config(str(path))
    assert nodes[0].count == 1


# ---------------------------------------------------------------------------
# Unit tests: command building
# ---------------------------------------------------------------------------


def test_build_worker_command():
    node = NodeConfig("node-1", "10.0.0.1", "deploy", "/opt/antfarm", "claude-code", 2)
    cmd = _build_worker_command(node, 0, "http://colony:7433", "dev")
    assert "cd /opt/antfarm" in cmd
    assert "--agent claude-code" in cmd
    assert "--name claude-code-0" in cmd
    assert "--node node-1" in cmd
    assert "--colony-url http://colony:7433" in cmd
    assert "--integration-branch dev" in cmd


def test_build_ssh_command(fleet_config_file):
    node = NodeConfig("node-1", "10.0.0.1", "deploy", "/opt/antfarm", "claude-code", 1)
    cmd = _build_ssh_command(node, 0, "http://colony:7433", "dev", fleet_config_file)
    assert cmd[0] == "ssh"
    assert cmd[1] == "deploy@10.0.0.1"
    prefix = _colony_prefix(fleet_config_file, "http://colony:7433")
    assert f"tmux new-session -A -d -s {prefix}-node-1-claude-code-0" in cmd[2]


# ---------------------------------------------------------------------------
# Colony-scoped session naming (issue #235)
# ---------------------------------------------------------------------------


def test_session_name_includes_colony_hash(fleet_config_file):
    """Session name embeds an 8-hex colony hash prefix, not bare ``antfarm-``."""
    node = NodeConfig("node-1", "10.0.0.1", "deploy", "/opt/antfarm", "claude-code", 1)
    cmd = _build_ssh_command(node, 0, "http://colony:7433", "dev", fleet_config_file)
    # Expect antfarm-<8hex>-node-1-claude-code-0 somewhere in the tmux invocation.
    assert re.search(r"antfarm-[0-9a-f]{8}-node-1-claude-code-0", cmd[2])


def test_distinct_colony_urls_distinct_prefixes(fleet_config_file):
    p1 = _colony_prefix(fleet_config_file, "http://colony-a:7433")
    p2 = _colony_prefix(fleet_config_file, "http://colony-b:7433")
    assert p1 != p2
    assert p1.startswith("antfarm-") and p2.startswith("antfarm-")


def test_distinct_config_paths_distinct_prefixes(tmp_path):
    cfg_a = tmp_path / "a.json"
    cfg_b = tmp_path / "b.json"
    payload = json.dumps(
        [
            {
                "node_id": "n",
                "host": "h",
                "ssh_user": "u",
                "repo_path": "/r",
                "agent_type": "a",
            }
        ]
    )
    cfg_a.write_text(payload)
    cfg_b.write_text(payload)

    p_a = _colony_prefix(str(cfg_a), "http://colony:7433")
    p_b = _colony_prefix(str(cfg_b), "http://colony:7433")
    assert p_a != p_b


def test_prefix_is_deterministic(fleet_config_file):
    p1 = _colony_prefix(fleet_config_file, "http://colony:7433")
    p2 = _colony_prefix(fleet_config_file, "http://colony:7433")
    assert p1 == p2


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks only")
def test_prefix_stable_across_symlinks(tmp_path):
    """A symlink to the real config resolves to the same prefix."""
    real = tmp_path / "real.json"
    real.write_text(
        json.dumps(
            [
                {
                    "node_id": "n",
                    "host": "h",
                    "ssh_user": "u",
                    "repo_path": "/r",
                    "agent_type": "a",
                }
            ]
        )
    )
    link = tmp_path / "link.json"
    os.symlink(real, link)

    p_real = _colony_prefix(str(real), "http://colony:7433")
    p_link = _colony_prefix(str(link), "http://colony:7433")
    assert p_real == p_link


def test_status_filter_scopes_to_this_colony(fleet_config_file):
    """Status command's grep pattern must be colony-specific, not ``^antfarm-``."""
    node = NodeConfig("node-1", "10.0.0.1", "deploy", "/opt/antfarm", "claude-code", 1)
    prefix = _colony_prefix(fleet_config_file, "http://colony:7433")
    cmd = _build_status_ssh_command(node, prefix)
    # Bare antfarm- match would be an escape hatch that leaks peer colonies.
    assert f"^{prefix}-" in cmd[2]
    assert "'^antfarm-'" not in cmd[2]


@patch("antfarm.core.deploy.subprocess.run")
def test_deploy_status_ignores_foreign_sessions(mock_run, fleet_config_file):
    """deploy_status only reports this colony's sessions, not peer-colony ones.

    The real filtering happens inside the remote grep; here we verify both that
    the correct pattern is emitted and that the returned dict reflects only what
    grep would pass through (sessions already prefixed with this colony's hash).
    """
    prefix = _colony_prefix(fleet_config_file, "http://colony:7433")
    mock_run.side_effect = [
        MagicMock(stdout=f"{prefix}-node-1-claude-code-0\n{prefix}-node-1-claude-code-1\n"),
        MagicMock(stdout=f"{prefix}-node-2-generic-0\n"),
    ]
    status = deploy_status(fleet_config_file, "http://colony:7433")

    assert len(status["node-1"]) == 2
    assert all(s.startswith(prefix) for s in status["node-1"])
    assert all(s.startswith(prefix) for s in status["node-2"])

    # Verify the ssh command issued contained the colony-specific grep.
    for call in mock_run.call_args_list:
        ssh_args = call.args[0]
        assert f"^{prefix}-" in ssh_args[2]


# ---------------------------------------------------------------------------
# Integration tests: deploy with mocked SSH
# ---------------------------------------------------------------------------


@patch("antfarm.core.deploy.subprocess.run")
def test_deploy_success(mock_run, fleet_config_file):
    mock_run.return_value = MagicMock(returncode=0)
    results = deploy(fleet_config_file, "http://colony:7433", "dev")

    # 2 workers for node-1 + 1 worker for node-2 = 3 total
    assert len(results) == 3
    assert all(r.success for r in results)
    assert mock_run.call_count == 3


@patch("antfarm.core.deploy.subprocess.run")
def test_deploy_ssh_failure(mock_run, fleet_config_file):
    mock_run.side_effect = subprocess.CalledProcessError(1, "ssh", stderr="Connection refused")
    results = deploy(fleet_config_file, "http://colony:7433", "dev")

    assert len(results) == 3
    assert all(not r.success for r in results)
    assert "SSH failed" in results[0].message


@patch("antfarm.core.deploy.subprocess.run")
def test_deploy_ssh_timeout(mock_run, fleet_config_file):
    mock_run.side_effect = subprocess.TimeoutExpired("ssh", 30)
    results = deploy(fleet_config_file, "http://colony:7433", "dev")

    assert len(results) == 3
    assert all(not r.success for r in results)
    assert "timed out" in results[0].message


@patch("antfarm.core.deploy.subprocess.run")
def test_deploy_partial_failure(mock_run, fleet_config_file):
    """First two succeed, third fails."""
    mock_run.side_effect = [
        MagicMock(returncode=0),
        MagicMock(returncode=0),
        subprocess.CalledProcessError(1, "ssh", stderr="Host unreachable"),
    ]
    results = deploy(fleet_config_file, "http://colony:7433", "dev")

    assert results[0].success
    assert results[1].success
    assert not results[2].success


# ---------------------------------------------------------------------------
# Integration tests: deploy_status with mocked SSH
# ---------------------------------------------------------------------------


@patch("antfarm.core.deploy.subprocess.run")
def test_deploy_status(mock_run, fleet_config_file):
    prefix = _colony_prefix(fleet_config_file, "http://colony:7433")
    mock_run.side_effect = [
        MagicMock(stdout=f"{prefix}-node-1-claude-code-0\n{prefix}-node-1-claude-code-1\n"),
        MagicMock(stdout=f"{prefix}-node-2-generic-0\n"),
    ]
    status = deploy_status(fleet_config_file, "http://colony:7433")

    assert len(status["node-1"]) == 2
    assert len(status["node-2"]) == 1
    assert f"{prefix}-node-1-claude-code-0" in status["node-1"]


@patch("antfarm.core.deploy.subprocess.run")
def test_deploy_status_no_sessions(mock_run, fleet_config_file):
    mock_run.return_value = MagicMock(stdout="")
    status = deploy_status(fleet_config_file)

    assert status["node-1"] == []
    assert status["node-2"] == []


@patch("antfarm.core.deploy.subprocess.run")
def test_deploy_status_ssh_failure(mock_run, fleet_config_file):
    mock_run.side_effect = subprocess.TimeoutExpired("ssh", 10)
    status = deploy_status(fleet_config_file)

    assert status["node-1"] == []
    assert status["node-2"] == []


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


@patch("antfarm.core.deploy.subprocess.run")
def test_cli_deploy(mock_run, fleet_config_file):
    mock_run.return_value = MagicMock(returncode=0)
    runner = CliRunner()
    result = runner.invoke(main, ["deploy", "--fleet-config", fleet_config_file])
    assert result.exit_code == 0
    assert "[OK]" in result.output


@patch("antfarm.core.deploy.subprocess.run")
def test_cli_deploy_status(mock_run, fleet_config_file):
    # CLI default colony-url is localhost:7433; reproduce the same prefix here.
    prefix = _colony_prefix(fleet_config_file, "http://localhost:7433")
    mock_run.return_value = MagicMock(stdout=f"{prefix}-node-1-claude-code-0\n")
    runner = CliRunner()
    result = runner.invoke(main, ["deploy", "--status", "--fleet-config", fleet_config_file])
    assert result.exit_code == 0
    assert "session(s) running" in result.output


def test_cli_deploy_missing_config():
    runner = CliRunner()
    result = runner.invoke(main, ["deploy", "--fleet-config", "/nonexistent/fleet.json"])
    assert result.exit_code != 0
