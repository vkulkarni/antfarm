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
                "mission",
                "create",
                "--spec",
                str(spec_file),
                "--colony-url",
                "http://localhost:7433",
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
                "mission",
                "create",
                "--spec",
                str(spec_file),
                "--no-plan-review",
                "--colony-url",
                "http://localhost:7433",
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
                "mission",
                "create",
                "--spec",
                str(spec_file),
                "--max-builders",
                "8",
                "--colony-url",
                "http://localhost:7433",
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
                "mission",
                "report",
                "mission-auth-123",
                "--colony-url",
                "http://localhost:7433",
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
                "mission",
                "report",
                "mission-auth-123",
                "--format",
                "md",
                "--colony-url",
                "http://localhost:7433",
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
                "mission",
                "report",
                "mission-auth-123",
                "--format",
                "json",
                "--colony-url",
                "http://localhost:7433",
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
                "mission",
                "list",
                "--status",
                "building",
                "--colony-url",
                "http://localhost:7433",
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
# mission create --auto-merge (#353)
# ---------------------------------------------------------------------------


def test_mission_create_auto_merge_flag_sets_config(tmp_path: Path):
    """--auto-merge sets auto_merge in config."""
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
                "mission",
                "create",
                "--spec",
                str(spec_file),
                "--auto-merge",
                "on-review-pass-and-ci-green",
                "--allow-auto-merge-on-external",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["config"]["auto_merge"] == "on-review-pass-and-ci-green"
        assert payload["config"]["allow_auto_merge_on_external"] is True


def test_mission_create_rejects_invalid_auto_merge(tmp_path: Path):
    """Click choice validation rejects unknown auto-merge modes."""
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("spec")

    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "mission",
            "create",
            "--spec",
            str(spec_file),
            "--auto-merge",
            "bogus-mode",
            "--colony-url",
            "http://localhost:7433",
        ],
    )

    assert result.exit_code != 0
    assert "bogus-mode" in result.output or "Invalid value" in result.output


def test_mission_update_patches_config_preserving_other_fields():
    """mission update --auto-merge GETs, merges, then PATCHes config."""
    runner = CliRunner()

    current_mission = {
        "mission_id": "m1",
        "status": "building",
        "config": {
            "completion_mode": "best_effort",
            "max_parallel_builders": 4,
            "auto_merge": "never",
        },
        "task_ids": [],
        "created_at": "2026-04-20T00:00:00Z",
    }

    with (
        patch("antfarm.core.cli.httpx.get") as mock_get,
        patch("antfarm.core.cli.httpx.patch") as mock_patch,
    ):
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = current_mission
        mock_get.return_value = mock_get_resp

        mock_patch_resp = MagicMock()
        mock_patch_resp.status_code = 200
        mock_patch_resp.json.return_value = {"ok": True}
        mock_patch.return_value = mock_patch_resp

        result = runner.invoke(
            main,
            [
                "mission",
                "update",
                "m1",
                "--auto-merge",
                "on-review-pass",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_patch.call_args.kwargs.get("json") or mock_patch.call_args[1]["json"]
        merged_config = payload["updates"]["config"]
        # Existing fields preserved
        assert merged_config["completion_mode"] == "best_effort"
        assert merged_config["max_parallel_builders"] == 4
        # New value applied
        assert merged_config["auto_merge"] == "on-review-pass"


def test_mission_status_prints_auto_merge_line():
    """mission status output includes Auto-merge: line."""
    runner = CliRunner()

    mission_data = {
        "mission_id": "m1",
        "status": "building",
        "config": {
            "completion_mode": "best_effort",
            "auto_merge": "on-review-pass",
        },
        "task_ids": [],
        "created_at": "2026-04-20T00:00:00Z",
    }

    with patch("antfarm.core.cli.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mission_data
        mock_get.return_value = mock_resp

        result = runner.invoke(
            main,
            ["mission", "status", "m1", "--colony-url", "http://localhost:7433"],
        )

        assert result.exit_code == 0, result.output
        assert "Auto-merge:" in result.output
        assert "on-review-pass" in result.output


# ---------------------------------------------------------------------------
# mission create / update --test-command (#363)
# ---------------------------------------------------------------------------


def test_mission_create_test_command_flag_sets_config(tmp_path: Path):
    """--test-command repeated tokens are collected into a list in config."""
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
                "mission",
                "create",
                "--spec",
                str(spec_file),
                "--test-command",
                "pytest",
                "--test-command",
                "-x",
                "--test-command",
                "-q",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["config"]["test_command"] == ["pytest", "-x", "-q"]


def test_mission_create_test_command_with_bash_c_form(tmp_path: Path):
    """--test-command preserves multi-token shell forms like `bash -c "..."`."""
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
                "mission",
                "create",
                "--spec",
                str(spec_file),
                "--test-command",
                "bash",
                "--test-command",
                "-c",
                "--test-command",
                "pytest && mypy",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["config"]["test_command"] == ["bash", "-c", "pytest && mypy"]


def test_mission_create_no_test_command_omits_key(tmp_path: Path):
    """Without --test-command, the key is absent from config."""
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
                "mission",
                "create",
                "--spec",
                str(spec_file),
                "--colony-url",
                "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert "test_command" not in payload.get("config", {})


def test_mission_update_test_command_patches_config():
    """mission update --test-command GETs, merges, then PATCHes config preserving fields."""
    runner = CliRunner()

    current_mission = {
        "mission_id": "m1",
        "status": "building",
        "config": {
            "completion_mode": "best_effort",
            "max_parallel_builders": 4,
            "auto_merge": "never",
        },
        "task_ids": [],
        "created_at": "2026-04-20T00:00:00Z",
    }

    with (
        patch("antfarm.core.cli.httpx.get") as mock_get,
        patch("antfarm.core.cli.httpx.patch") as mock_patch,
    ):
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = current_mission
        mock_get.return_value = mock_get_resp

        mock_patch_resp = MagicMock()
        mock_patch_resp.status_code = 200
        mock_patch_resp.json.return_value = {"ok": True}
        mock_patch.return_value = mock_patch_resp

        result = runner.invoke(
            main,
            [
                "mission",
                "update",
                "m1",
                "--test-command",
                "pytest",
                "--test-command",
                "-x",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_patch.call_args.kwargs.get("json") or mock_patch.call_args[1]["json"]
        merged_config = payload["updates"]["config"]
        # Existing fields preserved
        assert merged_config["completion_mode"] == "best_effort"
        assert merged_config["max_parallel_builders"] == 4
        assert merged_config["auto_merge"] == "never"
        # New test_command applied
        assert merged_config["test_command"] == ["pytest", "-x"]


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
                "--title",
                "Auth endpoint",
                "--spec",
                "Build the auth endpoint",
                "--mission",
                "mission-auth-123",
                "--colony-url",
                "http://localhost:7433",
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


# ---------------------------------------------------------------------------
# mission create budget flags / mission extend / mission status budget
# (v0.6.14 — issue #354)
# ---------------------------------------------------------------------------


def test_mission_create_with_max_cost_usd(tmp_path: Path):
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
                "mission",
                "create",
                "--spec",
                str(spec_file),
                "--max-cost-usd",
                "25.5",
                "--max-tokens",
                "100000",
                "--budget-action",
                "cancel",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["config"]["max_cost_usd"] == 25.5
        assert payload["config"]["max_tokens"] == 100_000
        assert payload["config"]["budget_action"] == "cancel"


def test_mission_status_prints_budget_line():
    runner = CliRunner()
    with (
        patch("antfarm.core.cli.httpx.get") as mock_get,
    ):
        # First call: /missions/<id>
        mission_resp = MagicMock()
        mission_resp.status_code = 200
        mission_resp.json.return_value = {
            "mission_id": "m-1",
            "status": "building",
            "config": {
                "max_cost_usd": 10.0,
                "max_tokens": 50_000,
                "completion_mode": "best_effort",
            },
            "task_ids": ["task-1"],
        }
        # Second call: /missions/<id>/usage
        usage_resp = MagicMock()
        usage_resp.status_code = 200
        usage_resp.json.return_value = {
            "mission_id": "m-1",
            "total_cost_usd": 2.50,
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "per_task": {
                "task-1": {"cost_usd": 2.50},
            },
        }
        mock_get.side_effect = [mission_resp, usage_resp]

        result = runner.invoke(
            main,
            ["mission", "status", "m-1", "--colony-url", "http://localhost:7433"],
        )

        assert result.exit_code == 0, result.output
        assert "Budget:" in result.output
        assert "$2.5000" in result.output or "$2.5" in result.output
        assert "$10" in result.output
        assert "Tokens:" in result.output
        assert "1500" in result.output
        assert "50000" in result.output
        assert "Top spend" in result.output


def test_mission_extend_command():
    runner = CliRunner()
    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "mission_id": "m-1",
            "status": "building",
            "config": {"max_cost_usd": 15.0, "max_tokens": 1500},
        }
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "mission",
                "extend",
                "m-1",
                "--additional-usd",
                "10.0",
                "--additional-tokens",
                "500",
                "--colony-url",
                "http://localhost:7433",
            ],
        )
        assert result.exit_code == 0, result.output
        url = mock_post.call_args.args[0]
        assert "/missions/m-1/extend" in url
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["additional_usd"] == 10.0
        assert payload["additional_tokens"] == 500
        assert "15.0" in result.output
        assert "1500" in result.output


# ---------------------------------------------------------------------------
# mission create / update --max-re-plans (#364)
# ---------------------------------------------------------------------------


def test_mission_create_max_re_plans_persists_to_config(tmp_path: Path):
    """--max-re-plans threads max_re_plans into config payload."""
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
                "mission",
                "create",
                "--spec",
                str(spec_file),
                "--max-re-plans",
                "3",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["config"]["max_re_plans"] == 3


def test_mission_update_max_re_plans_patches_config_preserving_other_fields():
    """mission update --max-re-plans GETs, merges, then PATCHes config."""
    runner = CliRunner()

    current_mission = {
        "mission_id": "m1",
        "status": "building",
        "config": {
            "completion_mode": "best_effort",
            "max_parallel_builders": 4,
            "auto_merge": "never",
        },
        "task_ids": [],
        "created_at": "2026-04-22T00:00:00Z",
    }

    with (
        patch("antfarm.core.cli.httpx.get") as mock_get,
        patch("antfarm.core.cli.httpx.patch") as mock_patch,
    ):
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = current_mission
        mock_get.return_value = mock_get_resp

        mock_patch_resp = MagicMock()
        mock_patch_resp.status_code = 200
        mock_patch_resp.json.return_value = {"ok": True}
        mock_patch.return_value = mock_patch_resp

        result = runner.invoke(
            main,
            [
                "mission",
                "update",
                "m1",
                "--max-re-plans",
                "0",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_patch.call_args.kwargs.get("json") or mock_patch.call_args[1]["json"]
        merged_config = payload["updates"]["config"]
        # Existing fields preserved
        assert merged_config["completion_mode"] == "best_effort"
        assert merged_config["max_parallel_builders"] == 4
        assert merged_config["auto_merge"] == "never"
        # New value applied
        assert merged_config["max_re_plans"] == 0


# ---------------------------------------------------------------------------
# mission create --no-audit-doc (issue #379)
# ---------------------------------------------------------------------------


def test_mission_create_no_audit_doc_flag(tmp_path: Path):
    """--no-audit-doc sets commit_audit_doc=False in posted config."""
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
                "mission",
                "create",
                "--spec",
                str(spec_file),
                "--no-audit-doc",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["config"]["commit_audit_doc"] is False


# ---------------------------------------------------------------------------
# mission report --print-doc / --write-doc (issue #379)
# ---------------------------------------------------------------------------


def _stub_mission_responses(mission_id: str = "m1") -> dict:
    """Build a minimal set of canned httpx responses for /missions and /tasks."""
    mission = {
        "mission_id": mission_id,
        "spec": "spec",
        "spec_file": "specs/x.md",
        "status": "complete",
        "plan_task_id": f"plan-{mission_id}",
        "plan_artifact": {
            "plan_task_id": f"plan-{mission_id}",
            "attempt_id": "a",
            "proposed_tasks": [
                {"title": "Step 1", "depends_on": [], "touches": [], "complexity": "M"}
            ],
            "task_count": 1,
            "warnings": [],
            "dependency_summary": "",
        },
        "task_ids": ["task-01"],
        "blocked_task_ids": [],
        "config": {"integration_branch": "main"},
        "created_at": "2026-04-22T12:00:00+00:00",
        "updated_at": "2026-04-22T12:10:00+00:00",
        "completed_at": "2026-04-22T12:10:00+00:00",
        "report": None,
        "last_progress_at": "2026-04-22T12:10:00+00:00",
        "re_plan_count": 0,
    }
    tasks = [
        {
            "id": "task-01",
            "title": "Implement",
            "status": "done",
            "capabilities_required": [],
            "depends_on": [],
            "touches": [],
            "attempts": [
                {
                    "attempt_id": "att-1",
                    "status": "merged",
                    "branch": "feat/x",
                    "pr": "https://example.com/pr/1",
                    "started_at": "2026-04-22T12:01:00+00:00",
                    "completed_at": "2026-04-22T12:09:00+00:00",
                }
            ],
            "trail": [],
            "mission_id": mission_id,
        }
    ]
    usage = {"total_cost_usd": 0.1, "total_input_tokens": 10, "total_output_tokens": 20}
    return {"mission": mission, "tasks": tasks, "usage": usage}


def _make_get_router(canned: dict, mission_id: str = "m1"):
    """Return a side_effect callable for httpx.get that maps URL → response."""

    def _route(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith(f"/missions/{mission_id}"):
            resp.json.return_value = canned["mission"]
        elif "/tasks?mission_id=" in url:
            resp.json.return_value = canned["tasks"]
        elif url.endswith(f"/missions/{mission_id}/usage"):
            resp.json.return_value = canned["usage"]
        else:
            resp.json.return_value = {}
        return resp

    return _route


def test_mission_report_print_doc(tmp_path: Path):
    """--print-doc renders markdown to stdout and never invokes git."""
    canned = _stub_mission_responses()
    runner = CliRunner()

    with (
        patch("antfarm.core.cli.httpx.get", side_effect=_make_get_router(canned)),
        patch("antfarm.core.mission_doc.subprocess.run") as mock_subproc,
    ):
        result = runner.invoke(
            main,
            [
                "mission",
                "report",
                "m1",
                "--print-doc",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "# Mission: m1" in result.output
    assert "## Plan" in result.output
    # No git ops at all.
    assert mock_subproc.call_count == 0


def test_mission_report_write_doc_idempotent(tmp_path: Path):
    """Running --write-doc twice with same content → second run is a no-op."""
    canned = _stub_mission_responses()
    runner = CliRunner()

    # State for the fake subprocess: first invocation simulates real changes,
    # second invocation simulates an up-to-date file (diff returns 0).
    state = {"first_run": True}

    def fake_run(cmd, *args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        result.stdout = ""
        if "diff" in cmd:
            # First write: changes are present (rc=1). Second write: up to date (rc=0).
            result.returncode = 1 if state["first_run"] else 0
        return result

    repo = tmp_path / "repo"
    repo.mkdir()
    # init a git repo so the path is plausible (subprocess is mocked anyway).

    with (
        patch("antfarm.core.cli.httpx.get", side_effect=_make_get_router(canned)),
        patch("antfarm.core.mission_doc.subprocess.run", side_effect=fake_run) as mock_subproc,
    ):
        result1 = runner.invoke(
            main,
            [
                "mission",
                "report",
                "m1",
                "--write-doc",
                "--repo-path",
                str(repo),
                "--colony-url",
                "http://localhost:7433",
            ],
        )
        calls_first = mock_subproc.call_count
        commits_first = sum(1 for c in mock_subproc.call_args_list if "commit" in c.args[0])

        state["first_run"] = False
        result2 = runner.invoke(
            main,
            [
                "mission",
                "report",
                "m1",
                "--write-doc",
                "--repo-path",
                str(repo),
                "--colony-url",
                "http://localhost:7433",
            ],
        )
        commits_second = (
            sum(1 for c in mock_subproc.call_args_list if "commit" in c.args[0]) - commits_first
        )

    assert result1.exit_code == 0, result1.output
    assert result2.exit_code == 0, result2.output
    # First call did exactly one git commit; second call did zero.
    assert commits_first == 1
    assert commits_second == 0
    # File exists at the standard template path.
    expected = repo / "docs" / "antfarm" / "missions" / "m1.md"
    assert expected.exists()
    assert calls_first > 0  # silence unused-var warnings
