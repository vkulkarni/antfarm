"""Tests for the import command and task importers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from antfarm.core.cli import main
from antfarm.core.importers.github import GitHubImporter
from antfarm.core.importers.json_file import JsonFileImporter

# ---------------------------------------------------------------------------
# GitHubImporter unit tests
# ---------------------------------------------------------------------------


def _make_issue(number: int, title: str, body: str, labels: list[str], is_pr: bool = False) -> dict:
    issue: dict = {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": lbl} for lbl in labels],
    }
    if is_pr:
        issue["pull_request"] = {"url": "https://example.com"}
    return issue


def test_github_importer_basic():
    """GitHubImporter maps open issues to task dicts."""
    issues = [
        _make_issue(1, "Fix bug", "Steps to reproduce", ["bug"]),
        _make_issue(2, "Add feature", "Feature description", ["enhancement", "api"]),
    ]
    mock_response = MagicMock()
    mock_response.json.return_value = issues
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_response) as mock_get:
        importer = GitHubImporter(repo="owner/repo", token="ghp_test")
        tasks = importer.import_tasks()

    assert len(tasks) == 2
    assert tasks[0] == {"title": "Fix bug", "spec": "Steps to reproduce", "touches": ["bug"]}
    assert tasks[1] == {
        "title": "Add feature",
        "spec": "Feature description",
        "touches": ["enhancement", "api"],
    }

    call_args = mock_get.call_args
    assert "Authorization" in call_args.kwargs["headers"]
    assert call_args.kwargs["headers"]["Authorization"] == "Bearer ghp_test"


def test_github_importer_skips_pull_requests():
    """GitHubImporter filters out pull requests from the issue list."""
    issues = [
        _make_issue(1, "Real issue", "body", []),
        _make_issue(2, "A PR", "pr body", [], is_pr=True),
    ]
    mock_response = MagicMock()
    mock_response.json.return_value = issues
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_response):
        importer = GitHubImporter(repo="owner/repo")
        tasks = importer.import_tasks()

    assert len(tasks) == 1
    assert tasks[0]["title"] == "Real issue"


def test_github_importer_label_filter():
    """GitHubImporter passes label filter in query params."""
    mock_response = MagicMock()
    mock_response.json.return_value = []
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_response) as mock_get:
        importer = GitHubImporter(repo="owner/repo", label="bug")
        importer.import_tasks()

    params = mock_get.call_args.kwargs["params"]
    assert params["labels"] == "bug"


def test_github_importer_no_token():
    """GitHubImporter works without a token (public repos)."""
    mock_response = MagicMock()
    mock_response.json.return_value = []
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_response) as mock_get:
        importer = GitHubImporter(repo="owner/repo")
        importer.import_tasks()

    headers = mock_get.call_args.kwargs["headers"]
    assert "Authorization" not in headers


def test_github_importer_null_body():
    """GitHubImporter maps null issue body to empty string."""
    issues = [_make_issue(1, "No body issue", None, [])]
    mock_response = MagicMock()
    mock_response.json.return_value = issues
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_response):
        importer = GitHubImporter(repo="owner/repo")
        tasks = importer.import_tasks()

    assert tasks[0]["spec"] == ""


# ---------------------------------------------------------------------------
# JsonFileImporter unit tests
# ---------------------------------------------------------------------------


def test_json_file_importer_basic(tmp_path: Path):
    """JsonFileImporter reads a JSON array from file."""
    task_list = [
        {"title": "Task A", "spec": "Do A"},
        {"title": "Task B", "spec": "Do B", "touches": ["api"]},
    ]
    f = tmp_path / "tasks.json"
    f.write_text(json.dumps(task_list))

    importer = JsonFileImporter(file_path=str(f))
    tasks = importer.import_tasks()

    assert tasks == task_list


def test_json_file_importer_not_a_list(tmp_path: Path):
    """JsonFileImporter raises ValueError if file is not a JSON array."""
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"title": "not a list"}))

    importer = JsonFileImporter(file_path=str(f))
    with pytest.raises(ValueError, match="Expected a JSON array"):
        importer.import_tasks()


def test_json_file_importer_empty(tmp_path: Path):
    """JsonFileImporter returns empty list for empty array."""
    f = tmp_path / "empty.json"
    f.write_text("[]")

    importer = JsonFileImporter(file_path=str(f))
    assert importer.import_tasks() == []


# ---------------------------------------------------------------------------
# CLI import command tests
# ---------------------------------------------------------------------------


def test_import_cmd_github_dry_run():
    """import --from github --dry-run prints tasks without posting."""
    tasks = [{"title": "Issue 1", "spec": "body", "touches": ["bug"]}]

    with patch("antfarm.core.importers.github.GitHubImporter.import_tasks", return_value=tasks):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["import", "--from", "github", "--repo", "owner/repo", "--dry-run"],
        )

    assert result.exit_code == 0, result.output
    assert "Issue 1" in result.output
    assert "[dry-run]" in result.output


def test_import_cmd_json_dry_run(tmp_path: Path):
    """import --from json --dry-run prints tasks without posting."""
    task_list = [{"title": "T1", "spec": "spec1"}]
    f = tmp_path / "tasks.json"
    f.write_text(json.dumps(task_list))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["import", "--from", "json", "--file", str(f), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "T1" in result.output
    assert "[dry-run]" in result.output


def test_import_cmd_github_posts_tasks():
    """import --from github without --dry-run POSTs each task to colony."""
    tasks = [{"title": "Issue 1", "spec": "body", "touches": []}]

    with (
        patch("antfarm.core.importers.github.GitHubImporter.import_tasks", return_value=tasks),
        patch("antfarm.core.cli._post", return_value={"id": "task-123"}) as mock_post,
    ):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["import", "--from", "github", "--repo", "owner/repo"],
        )

    assert result.exit_code == 0, result.output
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert call_args.args[1] == "/tasks"
    assert call_args.args[2]["title"] == "Issue 1"
    assert "Imported 1 task(s)." in result.output


def test_import_cmd_github_missing_repo():
    """import --from github without --repo exits with error."""
    runner = CliRunner()
    result = runner.invoke(main, ["import", "--from", "github"])
    assert result.exit_code != 0
    assert "--repo is required" in result.output


def test_import_cmd_json_missing_file():
    """import --from json without --file exits with error."""
    runner = CliRunner()
    result = runner.invoke(main, ["import", "--from", "json"])
    assert result.exit_code != 0
    assert "--file is required" in result.output


def test_import_cmd_no_tasks():
    """import command prints 'No tasks found.' when source returns empty list."""
    with patch("antfarm.core.importers.github.GitHubImporter.import_tasks", return_value=[]):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["import", "--from", "github", "--repo", "owner/repo"],
        )

    assert result.exit_code == 0, result.output
    assert "No tasks found." in result.output


def test_import_cmd_github_token_passed():
    """import --github-token is forwarded to GitHubImporter."""
    with patch(
        "antfarm.core.importers.github.GitHubImporter.__init__", return_value=None
    ) as mock_init, patch(
        "antfarm.core.importers.github.GitHubImporter.import_tasks", return_value=[]
    ):
        runner = CliRunner()
        runner.invoke(
            main,
            ["import", "--from", "github", "--repo", "owner/repo", "--github-token", "tok123"],
        )

    mock_init.assert_called_once_with(repo="owner/repo", token="tok123", label=None)
