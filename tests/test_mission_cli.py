"""Tests for the mission CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from antfarm.core.cli import main

# ---------------------------------------------------------------------------
# mission create
# ---------------------------------------------------------------------------


def test_mission_create_reads_spec_file_and_posts(tmp_path: Path):
    """mission create reads spec file and POSTs to /missions."""
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("Build an auth system")

    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"mission_id": "mission-auth-123"}
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "mission", "create",
                "--spec", str(spec_file),
                "--colony-url", "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "mission-auth-123" in result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["spec"] == "Build an auth system"
        assert payload["spec_file"] == str(spec_file)


def test_mission_create_no_plan_review_flag_sets_config(tmp_path: Path):
    """--no-plan-review sets require_plan_review=False in config."""
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("spec content")

    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"mission_id": "m1"}
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "mission", "create",
                "--spec", str(spec_file),
                "--no-plan-review",
                "--colony-url", "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["config"]["require_plan_review"] is False


def test_mission_create_max_builders_overrides_config(tmp_path: Path):
    """--max-builders sets max_parallel_builders in config."""
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("spec")

    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"mission_id": "m1"}
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "mission", "create",
                "--spec", str(spec_file),
                "--max-builders", "8",
                "--colony-url", "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["config"]["max_parallel_builders"] == 8


# ---------------------------------------------------------------------------
# mission status
# ---------------------------------------------------------------------------


def test_mission_status_formats_output():
    """mission status shows formatted mission overview."""
    runner = CliRunner()

    mission_data = {
        "mission_id": "mission-auth-123",
        "status": "building",
        "config": {"completion_mode": "best_effort"},
        "task_ids": ["task-1", "task-2", "task-3"],
        "created_at": "2026-04-09T10:00:00Z",
        "plan_task_id": "plan-auth",
        "spec_file": "spec.md",
    }

    with patch("antfarm.core.cli.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mission_data
        mock_get.return_value = mock_resp

        result = runner.invoke(
            main,
            ["mission", "status", "mission-auth-123", "--colony-url", "http://localhost:7433"],
        )

        assert result.exit_code == 0, result.output
        assert "mission-auth-123" in result.output
        assert "building" in result.output
        assert "best_effort" in result.output
        assert "3" in result.output


def test_mission_status_shows_completion_mode_warning():
    """all_or_nothing completion mode shows v0.6.0 warning."""
    runner = CliRunner()

    mission_data = {
        "mission_id": "mission-strict",
        "status": "building",
        "config": {"completion_mode": "all_or_nothing"},
        "task_ids": [],
        "created_at": "2026-04-09T10:00:00Z",
    }

    with patch("antfarm.core.cli.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mission_data
        mock_get.return_value = mock_resp

        result = runner.invoke(
            main,
            ["mission", "status", "mission-strict", "--colony-url", "http://localhost:7433"],
        )

        assert result.exit_code == 0, result.output
        assert "all_or_nothing" in result.output
        assert "treated as best_effort in v0.6.0" in result.output


# ---------------------------------------------------------------------------
# mission report
# ---------------------------------------------------------------------------


def _mock_report_data():
    return {
        "mission_id": "mission-auth-123",
        "status": "complete",
        "duration": "2h 15m",
        "tasks": [
            {"id": "task-1", "title": "Auth endpoints", "status": "merged"},
            {"id": "task-2", "title": "Auth tests", "status": "merged"},
        ],
    }


def test_mission_report_terminal_format():
    """mission report with terminal format shows table."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _mock_report_data()
        mock_get.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "mission", "report", "mission-auth-123",
                "--colony-url", "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "mission-auth-123" in result.output
        assert "complete" in result.output
        assert "Auth endpoints" in result.output


def test_mission_report_markdown_format():
    """mission report with md format shows markdown."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _mock_report_data()
        mock_get.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "mission", "report", "mission-auth-123",
                "--format", "md",
                "--colony-url", "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "# Mission Report" in result.output
        assert "**Status:**" in result.output
        assert "Auth endpoints" in result.output


def test_mission_report_json_format():
    """mission report with json format outputs valid JSON."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _mock_report_data()
        mock_get.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "mission", "report", "mission-auth-123",
                "--format", "json",
                "--colony-url", "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["mission_id"] == "mission-auth-123"
        assert len(parsed["tasks"]) == 2


# ---------------------------------------------------------------------------
# mission cancel
# ---------------------------------------------------------------------------


def test_mission_cancel_success():
    """mission cancel POSTs to /missions/{id}/cancel."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            ["mission", "cancel", "mission-auth-123", "--colony-url", "http://localhost:7433"],
        )

        assert result.exit_code == 0, result.output
        assert "cancelled" in result.output.lower()
        url = mock_post.call_args.args[0]
        assert "missions/mission-auth-123/cancel" in url


# ---------------------------------------------------------------------------
# mission list
# ---------------------------------------------------------------------------


def test_mission_list_filters_by_status():
    """mission list --status filters results."""
    runner = CliRunner()

    missions = [
        {"mission_id": "m1", "status": "building", "task_ids": ["t1"]},
    ]

    with patch("antfarm.core.cli.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = missions
        mock_get.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "mission", "list",
                "--status", "building",
                "--colony-url", "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "m1" in result.output
        assert "building" in result.output
        url = mock_get.call_args.args[0]
        assert "status=building" in url


def test_mission_list_empty():
    """mission list with no missions shows message."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp

        result = runner.invoke(
            main,
            ["mission", "list", "--colony-url", "http://localhost:7433"],
        )

        assert result.exit_code == 0, result.output
        assert "No missions found" in result.output


# ---------------------------------------------------------------------------
# carry --mission
# ---------------------------------------------------------------------------


def test_carry_with_mission_option():
    """carry --mission attaches mission_id to payload."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"task_id": "task-123"}
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "carry",
                "--title", "Auth endpoint",
                "--spec", "Build the auth endpoint",
                "--mission", "mission-auth-123",
                "--colony-url", "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["mission_id"] == "mission-auth-123"


# ---------------------------------------------------------------------------
# colony flags
# ---------------------------------------------------------------------------


def test_colony_autoscaler_flag_parsing():
    """colony --autoscaler flag is accepted without error."""
    runner = CliRunner()

    with (
        patch("uvicorn.run"),
        patch("antfarm.core.backends.file.FileBackend"),
        patch("antfarm.core.serve.get_app") as mock_get_app,
    ):
        mock_get_app.return_value = MagicMock()

        result = runner.invoke(
            main,
            ["colony", "--autoscaler", "--port", "9000"],
        )

        assert result.exit_code == 0, result.output


def test_colony_no_queen_flag():
    """colony --no-queen passes enable_queen=False to get_app."""
    runner = CliRunner()

    with (
        patch("uvicorn.run"),
        patch("antfarm.core.backends.file.FileBackend"),
        patch("antfarm.core.serve.get_app") as mock_get_app,
    ):
        mock_get_app.return_value = MagicMock()

        result = runner.invoke(
            main,
            ["colony", "--no-queen", "--port", "9000"],
        )

        assert result.exit_code == 0, result.output
        call_kwargs = mock_get_app.call_args
        assert call_kwargs.kwargs.get("enable_queen") is False
