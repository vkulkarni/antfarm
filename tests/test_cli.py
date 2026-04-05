"""Tests for the Antfarm CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from antfarm.core.cli import main

# ---------------------------------------------------------------------------
# colony
# ---------------------------------------------------------------------------


def test_cli_colony_starts():
    """Colony command creates FileBackend, gets app, calls uvicorn.run with correct port."""
    runner = CliRunner()

    with (
        patch("uvicorn.run") as mock_uvicorn_run,
        patch("antfarm.core.backends.file.FileBackend"),
        patch("antfarm.core.serve.get_app") as mock_get_app,
    ):
        mock_app = MagicMock()
        mock_get_app.return_value = mock_app

        result = runner.invoke(main, ["colony", "--port", "9000", "--host", "127.0.0.1"])

        assert result.exit_code == 0, result.output
        mock_get_app.assert_called_once()
        mock_uvicorn_run.assert_called_once_with(mock_app, host="127.0.0.1", port=9000)


def test_cli_colony_default_port():
    """Colony command defaults to port 7433."""
    runner = CliRunner()

    with (
        patch("uvicorn.run") as mock_uvicorn_run,
        patch("antfarm.core.backends.file.FileBackend"),
        patch("antfarm.core.serve.get_app") as mock_get_app,
    ):
        mock_get_app.return_value = MagicMock()
        runner.invoke(main, ["colony"])
        _, kwargs = mock_uvicorn_run.call_args
        assert kwargs["port"] == 7433


# ---------------------------------------------------------------------------
# carry
# ---------------------------------------------------------------------------


def test_cli_carry_creates_task():
    """Carry command POSTs correct payload to /tasks."""
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
                "--title", "Test Task",
                "--spec", "Do the thing",
                "--colony-url", "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs.kwargs["json"]
        assert payload["title"] == "Test Task"
        assert payload["spec"] == "Do the thing"


def test_cli_carry_requires_title_and_spec_without_file():
    """Carry fails with UsageError when neither --file nor --title+--spec are given."""
    runner = CliRunner()

    result = runner.invoke(main, ["carry", "--colony-url", "http://localhost:7433"])
    assert result.exit_code != 0


def test_cli_carry_from_file(tmp_path: Path):
    """Carry loads task payload from JSON file and POSTs it."""
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({"title": "File Task", "spec": "From file", "priority": 5}))

    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"task_id": "task-abc"}
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            ["carry", "--file", str(task_file), "--colony-url", "http://localhost:7433"],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]
        assert payload["title"] == "File Task"


def test_cli_carry_autogenerates_id():
    """Carry generates a task-{timestamp_ms} ID when --id is not given."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"task_id": "task-xxx"}
        mock_post.return_value = mock_resp

        runner.invoke(
            main,
            ["carry", "--title", "T", "--spec", "S", "--colony-url", "http://localhost:7433"],
        )

        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]
        assert payload.get("id", "").startswith("task-")


# ---------------------------------------------------------------------------
# scout
# ---------------------------------------------------------------------------


def test_cli_scout_shows_status():
    """Scout GETs /status and formats output as a table."""
    runner = CliRunner()

    status_data = {
        "nodes": 2,
        "workers": 3,
        "tasks_ready": 5,
        "tasks_active": 1,
        "tasks_done": 10,
    }

    with patch("antfarm.core.cli.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = status_data
        mock_get.return_value = mock_resp

        result = runner.invoke(main, ["scout", "--colony-url", "http://localhost:7433"])

        assert result.exit_code == 0, result.output
        assert "nodes" in result.output
        assert "2" in result.output
        assert "workers" in result.output


def test_cli_scout_uses_env_url(monkeypatch):
    """Scout reads colony URL from ANTFARM_URL envvar."""
    runner = CliRunner()
    monkeypatch.setenv("ANTFARM_URL", "http://my-colony:8000")

    with patch("antfarm.core.cli.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"nodes": 0}
        mock_get.return_value = mock_resp

        runner.invoke(main, ["scout"])

        call_url = mock_get.call_args.args[0]
        assert "my-colony:8000" in call_url


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_cli_doctor_runs_checks(tmp_path: Path):
    """Doctor runs diagnostics against a real FileBackend in tmp_path."""
    runner = CliRunner()

    result = runner.invoke(main, ["doctor", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    # Should produce some output — either "All checks passed" or findings
    assert result.output.strip() != ""


def test_cli_doctor_fix(tmp_path: Path):
    """Doctor --fix mode runs without error against real FileBackend."""
    runner = CliRunner()

    result = runner.invoke(main, ["doctor", "--fix", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------


def test_cli_join_posts_node():
    """Join POSTs node_id to /nodes."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"node_id": "node-1"}
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            ["join", "--node", "node-1", "--colony-url", "http://localhost:7433"],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]
        assert payload["node_id"] == "node-1"


# ---------------------------------------------------------------------------
# Low-level: hatch, forage, trail, harvest, guard, release, signal
# ---------------------------------------------------------------------------


def test_cli_hatch_registers_worker():
    """Hatch POSTs worker registration to /workers/register."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"worker_id": "node-1/claude-1"}
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "hatch",
                "--node", "node-1",
                "--agent", "claude-code",
                "--name", "claude-1",
                "--colony-url", "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]
        assert payload["node_id"] == "node-1"
        assert payload["agent_type"] == "claude-code"


def test_cli_forage_shows_task():
    """Forage POSTs to /tasks/pull and prints task JSON."""
    runner = CliRunner()

    task = {"id": "task-001", "title": "Do something"}

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = task
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            ["forage", "--worker-id", "node-1/w1", "--colony-url", "http://localhost:7433"],
        )

        assert result.exit_code == 0, result.output
        assert "task-001" in result.output


def test_cli_forage_no_tasks():
    """Forage prints 'No tasks available' on 204."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_resp.json.side_effect = Exception("no body")
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            ["forage", "--worker-id", "node-1/w1", "--colony-url", "http://localhost:7433"],
        )

        assert result.exit_code == 0, result.output
        assert "No tasks available" in result.output


def test_cli_trail_appends_entry():
    """Trail POSTs trail entry to /tasks/{id}/trail."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "trail", "task-001", "completed routes",
                "--worker-id", "node-1/w1",
                "--colony-url", "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        url = mock_post.call_args.args[0]
        assert "task-001/trail" in url


def test_cli_harvest_posts_pr():
    """Harvest POSTs to /tasks/{id}/harvest with pr field."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "harvest", "task-001",
                "--pr", "https://github.com/org/repo/pull/42",
                "--colony-url", "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]
        assert payload["pr"] == "https://github.com/org/repo/pull/42"


def test_cli_guard_acquires():
    """Guard POSTs to /guards/{resource}."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"acquired": True}
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            ["guard", "api-db", "--owner", "node-1/w1", "--colony-url", "http://localhost:7433"],
        )

        assert result.exit_code == 0, result.output
        url = mock_post.call_args.args[0]
        assert "guards/api-db" in url


def test_cli_release_deletes():
    """Release sends DELETE to /guards/{resource}?owner=..."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.delete") as mock_del:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_del.return_value = mock_resp

        result = runner.invoke(
            main,
            ["release", "api-db", "--owner", "node-1/w1", "--colony-url", "http://localhost:7433"],
        )

        assert result.exit_code == 0, result.output
        url = mock_del.call_args.args[0]
        assert "guards/api-db" in url


def test_cli_signal_posts():
    """Signal POSTs to /tasks/{id}/signal."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "signal", "task-001", "needs re-scoping",
                "--worker-id", "node-1/w1",
                "--colony-url", "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        url = mock_post.call_args.args[0]
        assert "task-001/signal" in url
