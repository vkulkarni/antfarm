"""Tests for the Antfarm CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from click.testing import CliRunner

from antfarm.core.cli import main

# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


def test_version_command():
    """Version command prints antfarm version string."""
    from antfarm.core import __version__

    runner = CliRunner()
    result = runner.invoke(main, ["version"])
    assert result.exit_code == 0, result.output
    assert f"antfarm v{__version__}" in result.output


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
                "--title",
                "Test Task",
                "--spec",
                "Do the thing",
                "--colony-url",
                "http://localhost:7433",
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


def _make_stream_ctx(lines: list[str]) -> MagicMock:
    """Build a MagicMock that mimics ``httpx.stream(...)`` used as a context manager."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.iter_lines.return_value = iter(lines)
    ctx = MagicMock()
    ctx.__enter__.return_value = response
    ctx.__exit__.return_value = False
    return ctx


def test_scout_watch_streams_events():
    """--watch opens /events SSE stream and prints formatted rows."""
    runner = CliRunner()

    events = [
        {
            "id": 1,
            "type": "harvested",
            "actor": "runner",
            "task_id": "task-1",
            "detail": "pr=42 branch=feat/x",
            "ts": "2026-04-16T14:32:11.000000+00:00",
        },
        {
            "id": 2,
            "type": "merged",
            "actor": "soldier",
            "task_id": "task-1",
            "detail": "attempt=att-001",
            "ts": "2026-04-16T14:33:02.000000+00:00",
        },
    ]
    lines = [f"data: {json.dumps(e)}" for e in events]

    call_count = {"n": 0}

    def fake_stream(method, url, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _make_stream_ctx(lines)
        # Second reconnect: simulate Ctrl-C to terminate the command.
        raise KeyboardInterrupt

    with patch("antfarm.core.cli.httpx.stream", side_effect=fake_stream):
        result = runner.invoke(
            main,
            ["scout", "--watch", "--colony-url", "http://localhost:7433"],
        )

    assert result.exit_code == 0, result.output
    assert "14:32:11" in result.output
    assert "runner" in result.output
    assert "pr=42 branch=feat/x" in result.output
    assert "14:33:02" in result.output
    assert "soldier" in result.output
    assert "attempt=att-001" in result.output


def test_scout_watch_tracks_cursor_across_reconnects():
    """Cursor from prior batch is passed as ``after`` on the next connect."""
    runner = CliRunner()

    batch1 = [
        {
            "id": 3,
            "type": "harvested",
            "actor": "runner",
            "task_id": "task-a",
            "detail": "d1",
            "ts": "2026-04-16T14:32:11+00:00",
        }
    ]
    batch2 = [
        {
            "id": 7,
            "type": "merged",
            "actor": "soldier",
            "task_id": "task-a",
            "detail": "d2",
            "ts": "2026-04-16T14:33:02+00:00",
        }
    ]

    calls: list[dict] = []

    def fake_stream(method, url, **kwargs):
        calls.append(kwargs.get("params", {}))
        if len(calls) == 1:
            return _make_stream_ctx([f"data: {json.dumps(e)}" for e in batch1])
        if len(calls) == 2:
            return _make_stream_ctx([f"data: {json.dumps(e)}" for e in batch2])
        raise KeyboardInterrupt

    with patch("antfarm.core.cli.httpx.stream", side_effect=fake_stream):
        result = runner.invoke(
            main,
            ["scout", "--watch", "--colony-url", "http://localhost:7433"],
        )

    assert result.exit_code == 0, result.output
    assert calls[0].get("after") == 0
    assert calls[1].get("after") == 3
    assert "d1" in result.output
    assert "d2" in result.output


def test_scout_watch_ctrl_c_exits_cleanly():
    """KeyboardInterrupt during iter_lines exits with status 0."""
    runner = CliRunner()

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.iter_lines.side_effect = KeyboardInterrupt
    ctx = MagicMock()
    ctx.__enter__.return_value = response
    ctx.__exit__.return_value = False

    with patch("antfarm.core.cli.httpx.stream", return_value=ctx):
        result = runner.invoke(
            main,
            ["scout", "--watch", "--colony-url", "http://localhost:7433"],
        )

    assert result.exit_code == 0, result.output


def test_scout_watch_reconnects_on_http_error():
    """Transient HTTPError triggers backoff + reconnect; cursor is preserved."""
    runner = CliRunner()

    batch1 = [
        {
            "id": 5,
            "type": "harvested",
            "actor": "runner",
            "task_id": "task-b",
            "detail": "ok",
            "ts": "2026-04-16T14:32:11+00:00",
        }
    ]

    calls: list[dict] = []

    def fake_stream(method, url, **kwargs):
        calls.append(kwargs.get("params", {}))
        if len(calls) == 1:
            return _make_stream_ctx([f"data: {json.dumps(e)}" for e in batch1])
        if len(calls) == 2:
            raise httpx.ConnectError("server down")
        raise KeyboardInterrupt

    with (
        patch("antfarm.core.cli.httpx.stream", side_effect=fake_stream),
        patch("antfarm.core.cli.time.sleep"),
    ):
        result = runner.invoke(
            main,
            ["scout", "--watch", "--colony-url", "http://localhost:7433"],
        )

    assert result.exit_code == 0, result.output
    # First call starts at cursor 0; after batch1 the cursor should advance to 5.
    assert calls[0].get("after") == 0
    assert calls[1].get("after") == 5
    # Third call (after HTTPError backoff) retains the advanced cursor.
    assert calls[2].get("after") == 5


def test_scout_oneshot_unchanged():
    """Without --watch, scout performs exactly one GET and exits."""
    runner = CliRunner()

    status_data = {"nodes": 2, "workers": 3, "tasks_ready": 5}

    with patch("antfarm.core.cli.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = status_data
        mock_get.return_value = mock_resp

        result = runner.invoke(main, ["scout", "--colony-url", "http://localhost:7433"])

        assert result.exit_code == 0, result.output
        assert "nodes" in result.output
        mock_get.assert_called_once()


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


def test_cli_doctor_sweep_no_legacy_sessions(tmp_path: Path):
    """--sweep-legacy-tmux with no matches prints the no-op message and exits."""
    runner = CliRunner()

    with patch("antfarm.core.doctor.sweep_legacy_tmux_sessions") as mock_sweep:
        mock_sweep.return_value = []
        result = runner.invoke(
            main,
            ["doctor", "--sweep-legacy-tmux", "--data-dir", str(tmp_path)],
        )

    assert result.exit_code == 0, result.output
    assert "No legacy sessions found" in result.output
    # Only the dry-run call should have happened; no confirmation prompt either.
    mock_sweep.assert_called_once()
    assert mock_sweep.call_args.kwargs["confirmed"] is False


def test_cli_doctor_sweep_prompts_without_yes(tmp_path: Path):
    """Without --yes, --sweep-legacy-tmux requires interactive confirmation and aborts on 'n'."""
    from antfarm.core.doctor import Finding

    runner = CliRunner()

    preview = [
        Finding(
            severity="info",
            check="legacy_tmux_session",
            message="Legacy session: auto-builder-3",
            auto_fixable=True,
        ),
    ]

    with patch("antfarm.core.doctor.sweep_legacy_tmux_sessions") as mock_sweep:
        mock_sweep.return_value = preview
        result = runner.invoke(
            main,
            ["doctor", "--sweep-legacy-tmux", "--data-dir", str(tmp_path)],
            input="n\n",
        )

    assert result.exit_code == 0, result.output
    assert "auto-builder-3" in result.output
    assert "Aborted" in result.output
    # Dry-run only — no confirmed=True call.
    assert mock_sweep.call_count == 1
    assert mock_sweep.call_args.kwargs["confirmed"] is False


def test_cli_doctor_sweep_yes_bypasses_prompt(tmp_path: Path):
    """--yes skips the prompt and invokes the sweep with confirmed=True."""
    from antfarm.core.doctor import Finding

    runner = CliRunner()

    preview = [
        Finding(
            severity="info",
            check="legacy_tmux_session",
            message="Legacy session: auto-builder-3",
            auto_fixable=True,
        ),
    ]
    killed = [
        Finding(
            severity="info",
            check="legacy_tmux_session",
            message="Legacy session: auto-builder-3",
            auto_fixable=True,
            fixed=True,
        ),
    ]

    with patch("antfarm.core.doctor.sweep_legacy_tmux_sessions") as mock_sweep:
        mock_sweep.side_effect = [preview, killed]
        result = runner.invoke(
            main,
            [
                "doctor",
                "--sweep-legacy-tmux",
                "--yes",
                "--data-dir",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "auto-builder-3" in result.output
    assert "[FIXED]" in result.output
    assert mock_sweep.call_count == 2
    assert mock_sweep.call_args_list[0].kwargs["confirmed"] is False
    assert mock_sweep.call_args_list[1].kwargs["confirmed"] is True


def test_cli_doctor_sweep_and_fix_are_mutually_exclusive():
    """--fix + --sweep-legacy-tmux is rejected because they have different safety profiles."""
    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--fix", "--sweep-legacy-tmux", "--yes"])
    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_cli_doctor_sweep_prints_colony_hash_before_preview(tmp_path: Path):
    """--sweep-legacy-tmux prints the colony hash before the session preview."""
    from antfarm.core.doctor import Finding

    runner = CliRunner()

    preview = [
        Finding(
            severity="info",
            check="legacy_tmux_session",
            message="Legacy session: auto-builder-3",
            auto_fixable=True,
        ),
    ]

    with patch("antfarm.core.doctor.sweep_legacy_tmux_sessions") as mock_sweep:
        mock_sweep.return_value = preview
        result = runner.invoke(
            main,
            ["doctor", "--sweep-legacy-tmux", "--data-dir", str(tmp_path)],
            input="n\n",
        )

    assert result.exit_code == 0, result.output
    assert "Colony id:" in result.output
    assert "hash:" in result.output


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
                "--node",
                "node-1",
                "--agent",
                "claude-code",
                "--name",
                "claude-1",
                "--colony-url",
                "http://localhost:7433",
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
                "trail",
                "task-001",
                "completed routes",
                "--worker-id",
                "node-1/w1",
                "--colony-url",
                "http://localhost:7433",
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
                "harvest",
                "task-001",
                "--pr",
                "https://github.com/org/repo/pull/42",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]
        assert payload["pr"] == "https://github.com/org/repo/pull/42"


def test_mark_merged_command():
    """mark-merged POSTs to /tasks/{id}/merge with {'attempt_id': ...}."""
    runner = CliRunner()

    with patch("antfarm.core.cli.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        result = runner.invoke(
            main,
            [
                "mark-merged",
                "task-001",
                "--attempt-id",
                "att-001",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        url = mock_post.call_args.args[0]
        assert url.endswith("/tasks/task-001/merge")
        payload = mock_post.call_args.kwargs.get("json")
        assert payload == {"attempt_id": "att-001"}


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
                "signal",
                "task-001",
                "needs re-scoping",
                "--worker-id",
                "node-1/w1",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

        assert result.exit_code == 0, result.output
        url = mock_post.call_args.args[0]
        assert "task-001/signal" in url


# ---------------------------------------------------------------------------
# worker start --type (#102)
# ---------------------------------------------------------------------------


def test_worker_start_reviewer_adds_review_capability():
    """--type=reviewer auto-adds 'review' to capabilities."""
    runner = CliRunner()
    captured_kwargs = {}

    def fake_worker_runtime(**kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    with patch("antfarm.core.worker.WorkerRuntime", side_effect=fake_worker_runtime):
        result = runner.invoke(
            main,
            [
                "worker",
                "start",
                "--agent",
                "claude-code",
                "--type",
                "reviewer",
                "--node",
                "n1",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "review" in captured_kwargs.get("capabilities", [])


def test_worker_start_builder_no_review_capability():
    """--type=builder (default) does not add 'review' capability."""
    runner = CliRunner()
    captured_kwargs = {}

    def fake_worker_runtime(**kwargs):
        captured_kwargs.update(kwargs)
        m = MagicMock()
        return m

    with patch("antfarm.core.worker.WorkerRuntime", side_effect=fake_worker_runtime):
        result = runner.invoke(
            main,
            [
                "worker",
                "start",
                "--agent",
                "claude-code",
                "--node",
                "n1",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "review" not in captured_kwargs.get("capabilities", [])


def test_worker_start_name_defaults_to_type():
    """Worker name defaults to worker_type when --name is not specified."""
    runner = CliRunner()
    captured_kwargs = {}

    def fake_worker_runtime(**kwargs):
        captured_kwargs.update(kwargs)
        m = MagicMock()
        return m

    with patch("antfarm.core.worker.WorkerRuntime", side_effect=fake_worker_runtime):
        result = runner.invoke(
            main,
            [
                "worker",
                "start",
                "--agent",
                "claude-code",
                "--type",
                "reviewer",
                "--node",
                "n1",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured_kwargs["name"] == "reviewer"


def test_worker_start_explicit_name_overrides():
    """Explicit --name overrides the type-based default."""
    runner = CliRunner()
    captured_kwargs = {}

    def fake_worker_runtime(**kwargs):
        captured_kwargs.update(kwargs)
        m = MagicMock()
        return m

    with patch("antfarm.core.worker.WorkerRuntime", side_effect=fake_worker_runtime):
        result = runner.invoke(
            main,
            [
                "worker",
                "start",
                "--agent",
                "claude-code",
                "--type",
                "reviewer",
                "--name",
                "my-worker",
                "--node",
                "n1",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured_kwargs["name"] == "my-worker"


def test_worker_start_poll_flags_flow_through():
    """--poll-interval and --max-empty-polls are passed to WorkerRuntime."""
    runner = CliRunner()
    captured_kwargs = {}

    def fake_worker_runtime(**kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    with patch("antfarm.core.worker.WorkerRuntime", side_effect=fake_worker_runtime):
        result = runner.invoke(
            main,
            [
                "worker",
                "start",
                "--agent",
                "claude-code",
                "--node",
                "n1",
                "--poll-interval",
                "5",
                "--max-empty-polls",
                "3",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured_kwargs["poll_interval"] == 5.0
    assert captured_kwargs["max_empty_polls"] == 3


# ---------------------------------------------------------------------------
# plan --carry dependency resolution (#93)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def test_cli_runner_help():
    """runner --help works and shows expected options."""
    runner = CliRunner()
    result = runner.invoke(main, ["runner", "--help"])
    assert result.exit_code == 0, result.output
    assert "--colony-url" in result.output
    assert "--repo-path" in result.output
    assert "--max-workers" in result.output
    assert "--capabilities" in result.output
    assert "--integration-branch" in result.output
    assert "--host" in result.output
    assert "--port" in result.output


# ---------------------------------------------------------------------------
# colony --multi-node
# ---------------------------------------------------------------------------


def test_cli_colony_multi_node_flag():
    """--multi-node flag is accepted by the colony command."""
    runner = CliRunner()

    with (
        patch("uvicorn.run") as mock_uvicorn_run,
        patch("antfarm.core.backends.file.FileBackend"),
        patch("antfarm.core.serve.get_app") as mock_get_app,
    ):
        mock_get_app.return_value = MagicMock()

        result = runner.invoke(
            main,
            [
                "colony",
                "--autoscaler",
                "--multi-node",
                "--port",
                "9001",
                "--host",
                "127.0.0.1",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Multi-node autoscaler mode enabled" in result.output
        mock_uvicorn_run.assert_called_once()


# ---------------------------------------------------------------------------
# plan --carry dependency resolution (#93)
# ---------------------------------------------------------------------------


def test_plan_carry_resolves_index_deps():
    """plan --carry resolves 1-based index deps to generated task IDs."""
    runner = CliRunner()

    plan_json = json.dumps(
        [
            {"title": "Task A", "spec": "Do A"},
            {"title": "Task B", "spec": "Do B", "depends_on": [1]},
        ]
    )

    carried_payloads = []

    def fake_post(url, json=None, headers=None):
        if json:
            carried_payloads.append(json)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"task_id": json.get("id", "?")} if json else {}
        return resp

    with patch("antfarm.core.cli.httpx.post", side_effect=fake_post):
        result = runner.invoke(
            main,
            [
                "plan",
                "--spec",
                plan_json,
                "--carry",
                "--colony-url",
                "http://localhost:7433",
            ],
        )

    assert result.exit_code == 0, result.output
    assert len(carried_payloads) == 2
    # Task B's depends_on should reference Task A's actual ID
    task_a_id = carried_payloads[0]["id"]
    assert carried_payloads[1]["depends_on"] == [task_a_id]


# ---------------------------------------------------------------------------
# _render_scout warnings
# ---------------------------------------------------------------------------


def test_render_scout_prints_warnings():
    """_render_scout emits warning lines in red when warnings are present in the status dict."""
    from antfarm.core.cli import _render_scout

    status = {
        "tasks": {"ready": 1, "active": 0, "done": 0},
        "workers": 0,
        "nodes": 0,
        "soldier": "not started",
        "warnings": [
            {
                "code": "no_reviewer_capacity",
                "message": "1 review task(s) ready but no worker has 'review' capability",
                "hint": "Start one: antfarm worker start --agent reviewer",
                "count": 1,
            }
        ],
    }

    runner = CliRunner()
    with runner.isolated_filesystem():
        with runner.isolation(input=None, color=False):
            _render_scout(status, None)
        # After the call, 'warnings' should have been popped from status
        assert "warnings" not in status

    # Invoke via CLI echo captures — simplest is to capture via a Click echo wrapper
    # by calling the function inside a runner context that captures output.
    output_lines: list[str] = []

    def capturing_echo(*args, **kwargs):
        msg = args[0] if args else ""
        output_lines.append(str(msg))

    with patch("antfarm.core.cli.click.echo", side_effect=capturing_echo), patch(
        "antfarm.core.cli.click.secho", side_effect=capturing_echo
    ):
        status2 = {
            "tasks": {"ready": 1, "active": 0, "done": 0},
            "warnings": [
                {
                    "code": "no_reviewer_capacity",
                    "message": "m",
                    "hint": "h",
                    "count": 1,
                }
            ],
        }
        _render_scout(status2, None)

    combined = "\n".join(output_lines)
    assert "h" in combined
    assert "m" in combined
