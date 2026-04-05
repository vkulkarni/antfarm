# v0.6 Claude Code Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Antfarm usable from Claude Code via MCP, with optional packaged plugin UX.

**Architecture:** MCP stdio server bridges Claude Code to the existing colony API using `ColonyClient` from `antfarm/core/colony_client.py`. Config loads from `.antfarm/config.json` with env var overrides. Plugin layer (skills, hooks, agents) is pure UX — no business logic. Important logic lives in Python handlers and colony code, not in prompt text or shell scripts.

**Tech Stack:** Python 3.12, `mcp` package (stdio transport), `antfarm.core.colony_client.ColonyClient` (reused), Claude Code plugin format (skills markdown, bash hooks, agent markdown)

**Prerequisite:** v0.5 must be shipped (planner, memory, artifacts, conflict prevention). Current version is 0.4.0.

**Spec:** `docs/SPEC_v06.md` (revised, frozen)

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `antfarm/mcp/__init__.py` | Create | Package marker |
| `antfarm/mcp/config.py` | Create | Load `.antfarm/config.json` + env overrides |
| `antfarm/mcp/server.py` | Create | MCP stdio server — tool registration, dispatch, error boundary |
| `antfarm/mcp/tools.py` | Create | 14 tool definitions + handler functions |
| `antfarm/core/colony_client.py` | Modify | Add `status_full()` method (existing client lacks `/status/full`) |
| `antfarm/plugin/skills/antfarm-plan.md` | Create | `/antfarm-plan` skill |
| `antfarm/plugin/skills/antfarm-status.md` | Create | `/antfarm-status` skill |
| `antfarm/plugin/skills/antfarm-start.md` | Create | `/antfarm-start` skill (with edge cases) |
| `antfarm/plugin/skills/antfarm-done.md` | Create | `/antfarm-done` skill |
| `antfarm/plugin/skills/antfarm-review.md` | Create | `/antfarm-review` skill |
| `antfarm/plugin/skills/antfarm-blockers.md` | Create | `/antfarm-blockers` skill |
| `antfarm/plugin/skills/antfarm-merge-ready.md` | Create | `/antfarm-merge-ready` skill |
| `antfarm/plugin/hooks/heartbeat.sh` | Create | PostToolUse heartbeat (default on) |
| `antfarm/plugin/hooks/failure_trail.sh` | Create | PostToolUseFailure trail (default on) |
| `antfarm/plugin/hooks/workspace_observe.sh` | Create | PostToolUse workspace observation (opt-in) |
| `antfarm/plugin/agents/worker.md` | Create | Worker agent using MCP tools |
| `antfarm/plugin/agents/planner.md` | Create | Planner agent using MCP tools |
| `antfarm/plugin/agents/reviewer.md` | Create | Reviewer agent consuming v0.5 ReviewVerdict |
| `antfarm/plugin/package.json` | Create | Plugin manifest |
| `tests/test_mcp_config.py` | Create | Config loading tests |
| `tests/test_mcp_tools.py` | Create | Tool handler tests (mocked client) |
| `tests/test_mcp_server.py` | Create | Server init + tool listing tests |
| `tests/test_mcp_errors.py` | Create | Failure-mode tests (colony down, auth, stale) |
| `tests/test_hooks.py` | Create | Hook safety invariant tests |
| `pyproject.toml` | Modify | Add `mcp>=1.0` dependency |

---

## Task 1: MCP Config + Server Foundation

**Files:**
- Create: `antfarm/mcp/__init__.py`
- Create: `antfarm/mcp/config.py`
- Create: `antfarm/mcp/server.py`
- Create: `tests/test_mcp_config.py`
- Create: `tests/test_mcp_server.py`
- Modify: `antfarm/core/colony_client.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `mcp` dependency to pyproject.toml**

In `pyproject.toml`, add `mcp>=1.0` to the dependencies list:

```toml
dependencies = [
    "fastapi>=0.100",
    "uvicorn>=0.20",
    "click>=8.0",
    "httpx>=0.24",
    "rich>=13.0",
    "mcp>=1.0",
]
```

- [ ] **Step 2: Add `status_full()` to ColonyClient**

The existing `ColonyClient.status()` calls `GET /status` but the MCP tool needs `GET /status/full`. Add this method to `antfarm/core/colony_client.py` after the existing `status()` method (line 174):

```python
    def status_full(self) -> dict:
        """Full colony status: summary + all tasks + all workers."""
        r = self._client.get("/status/full")
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 3: Write failing tests for config loading**

Create `tests/test_mcp_config.py`:

```python
"""Tests for MCP config loading."""

import json
import os

import pytest


def test_load_config_from_file(tmp_path, monkeypatch):
    """Config loads from .antfarm/config.json."""
    config_dir = tmp_path / ".antfarm"
    config_dir.mkdir()
    config_file = config_dir / "config.json"
    config_file.write_text(json.dumps({
        "colony_url": "http://my-colony:7433",
        "token": "secret-token",
        "repo": "vkulkarni/antfarm",
        "integration_branch": "dev",
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTFARM_URL", raising=False)
    monkeypatch.delenv("ANTFARM_TOKEN", raising=False)

    from antfarm.mcp.config import load_config

    config = load_config()
    assert config.colony_url == "http://my-colony:7433"
    assert config.token == "secret-token"
    assert config.repo == "vkulkarni/antfarm"
    assert config.integration_branch == "dev"


def test_env_overrides_file(tmp_path, monkeypatch):
    """Environment variables override config file values."""
    config_dir = tmp_path / ".antfarm"
    config_dir.mkdir()
    config_file = config_dir / "config.json"
    config_file.write_text(json.dumps({
        "colony_url": "http://file-url:7433",
        "token": "file-token",
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTFARM_URL", "http://env-url:9999")
    monkeypatch.setenv("ANTFARM_TOKEN", "env-token")

    from antfarm.mcp.config import load_config

    config = load_config()
    assert config.colony_url == "http://env-url:9999"
    assert config.token == "env-token"


def test_missing_config_uses_defaults(tmp_path, monkeypatch):
    """Missing config file falls back to defaults."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTFARM_URL", raising=False)
    monkeypatch.delenv("ANTFARM_TOKEN", raising=False)

    from antfarm.mcp.config import load_config

    config = load_config()
    assert config.colony_url == "http://localhost:7433"
    assert config.token is None


def test_malformed_config_raises(tmp_path, monkeypatch):
    """Malformed JSON raises a clear error."""
    config_dir = tmp_path / ".antfarm"
    config_dir.mkdir()
    (config_dir / "config.json").write_text("{bad json")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTFARM_URL", raising=False)
    monkeypatch.delenv("ANTFARM_TOKEN", raising=False)

    from antfarm.mcp.config import load_config

    with pytest.raises(json.JSONDecodeError):
        load_config()


def test_token_not_in_repr(tmp_path, monkeypatch):
    """Token is not exposed in string representation."""
    config_dir = tmp_path / ".antfarm"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "colony_url": "http://localhost:7433",
        "token": "super-secret",
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTFARM_URL", raising=False)
    monkeypatch.delenv("ANTFARM_TOKEN", raising=False)

    from antfarm.mcp.config import load_config

    config = load_config()
    assert "super-secret" not in repr(config)
    assert "super-secret" not in str(config)
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python3.12 -m pytest tests/test_mcp_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'antfarm.mcp'`

- [ ] **Step 5: Implement config module**

Create `antfarm/mcp/__init__.py`:

```python
"""Antfarm MCP server — bridges Claude Code to the colony API."""
```

Create `antfarm/mcp/config.py`:

```python
"""Config loading for Antfarm MCP server.

Resolution order:
1. .antfarm/config.json in current directory (or ANTFARM_CONFIG_PATH)
2. Environment variable overrides (ANTFARM_URL, ANTFARM_TOKEN)
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AntfarmConfig:
    """Antfarm MCP configuration."""

    colony_url: str
    token: str | None = None
    repo: str | None = None
    integration_branch: str = "dev"

    def __repr__(self) -> str:
        token_display = "****" if self.token else "None"
        return (
            f"AntfarmConfig(colony_url={self.colony_url!r}, "
            f"token={token_display!r}, repo={self.repo!r}, "
            f"integration_branch={self.integration_branch!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()


def load_config() -> AntfarmConfig:
    """Load Antfarm config from .antfarm/config.json with env overrides.

    File config is loaded first, then environment variables override
    any values they set. Missing config file is not an error — defaults
    are used.
    """
    config_path = Path(os.environ.get("ANTFARM_CONFIG_PATH", ".antfarm/config.json"))

    file_config: dict = {}
    if config_path.exists():
        with open(config_path) as f:
            file_config = json.load(f)

    return AntfarmConfig(
        colony_url=os.environ.get(
            "ANTFARM_URL", file_config.get("colony_url", "http://localhost:7433")
        ),
        token=os.environ.get("ANTFARM_TOKEN", file_config.get("token")),
        repo=file_config.get("repo"),
        integration_branch=file_config.get("integration_branch", "dev"),
    )
```

- [ ] **Step 6: Run config tests to verify they pass**

Run: `python3.12 -m pytest tests/test_mcp_config.py -v`
Expected: All 5 PASS

- [ ] **Step 7: Write failing tests for MCP server**

Create `tests/test_mcp_server.py`:

```python
"""Tests for MCP server initialization and tool listing."""

from antfarm.mcp.config import AntfarmConfig


def test_create_mcp_server():
    """MCP server can be instantiated with config."""
    from antfarm.mcp.server import create_mcp_server

    config = AntfarmConfig(colony_url="http://localhost:7433")
    server = create_mcp_server(config)
    assert server is not None


def test_server_module_runnable():
    """Server module has a main() entry point."""
    from antfarm.mcp import server

    assert hasattr(server, "main")
    assert callable(server.main)
```

- [ ] **Step 8: Implement MCP server**

Create `antfarm/mcp/server.py`:

```python
"""MCP server for Antfarm.

Bridges Claude Code to the Antfarm colony API via MCP stdio transport.

Usage:
    python -m antfarm.mcp.server

Configuration:
    Reads .antfarm/config.json in the current directory.
    Override with ANTFARM_URL and ANTFARM_TOKEN environment variables.
"""

import asyncio
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server

from antfarm.mcp.config import AntfarmConfig, load_config
from antfarm.mcp.tools import register_tools


def create_mcp_server(config: AntfarmConfig | None = None) -> Server:
    """Create and configure the Antfarm MCP server."""
    if config is None:
        config = load_config()

    server = Server("antfarm")
    register_tools(server, config)
    return server


def main():
    """Run the MCP server on stdio."""
    try:
        config = load_config()
    except Exception as e:
        print(f"Failed to load Antfarm config: {e}", file=sys.stderr)
        sys.exit(1)

    server = create_mcp_server(config)

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    asyncio.run(run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 9: Create stub tools module (to be filled in Task 2)**

Create `antfarm/mcp/tools.py` with a minimal stub so the server tests pass:

```python
"""MCP tool definitions and handlers for Antfarm.

Each tool maps to a ColonyClient method or a computed view of colony state.
Handlers take a ColonyClient and arguments dict, return JSON-serializable data.
"""

import json

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool

from antfarm.core.colony_client import ColonyClient
from antfarm.mcp.config import AntfarmConfig

# Tool definitions — populated in register_tools()
TOOL_DEFS: list[Tool] = []


def register_tools(server: Server, config: AntfarmConfig) -> None:
    """Register all Antfarm MCP tools on the server."""
    client = ColonyClient(config.colony_url, token=config.token)

    @server.list_tools()
    async def list_tools():
        return _build_tool_defs()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        handler = _get_handler(name)
        # Retry once on connection error (stale connection after colony restart)
        for attempt in range(2):
            try:
                result = handler(client, arguments)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
            except (httpx.ConnectError, httpx.RemoteProtocolError):
                if attempt == 0:
                    continue
                return [_error_response(
                    f"Cannot reach Antfarm colony at {config.colony_url} — is it running?"
                )]
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    return [_error_response("Authentication failed — check your token")]
                if e.response.status_code == 409:
                    return [_error_response(f"Conflict: {e.response.text}")]
                return [_error_response(
                f"Colony returned {e.response.status_code}: {e.response.text}"
            )]
        except httpx.TimeoutException:
            return [_error_response(
                f"Request to colony timed out ({config.colony_url})"
            )]
        except Exception as e:
            return [_error_response(f"Unexpected error: {e}")]


def _error_response(message: str, code: str = "unknown", retryable: bool = False) -> TextContent:
    return TextContent(type="text", text=json.dumps({
        "error": {"code": code, "message": message, "retryable": retryable},
    }))


def _build_tool_defs() -> list[Tool]:
    """Build the list of MCP tool definitions. Populated in Task 2."""
    return []


def _get_handler(name: str):
    """Get the handler function for a tool name. Populated in Task 2."""
    raise ValueError(f"Unknown tool: {name}")
```

- [ ] **Step 10: Run all tests**

Run: `python3.12 -m pytest tests/test_mcp_config.py tests/test_mcp_server.py -v`
Expected: All PASS

- [ ] **Step 11: Run full suite**

Run: `python3.12 -m pytest tests/ -x -q --ignore=tests/test_redis_backend.py`
Expected: All pass

- [ ] **Step 12: Commit**

```bash
git add antfarm/mcp/ antfarm/core/colony_client.py tests/test_mcp_config.py tests/test_mcp_server.py pyproject.toml
git commit -m "feat(mcp): add config loader and stdio server foundation"
```

---

## Task 2: MCP Tool Handlers

**Files:**
- Modify: `antfarm/mcp/tools.py`
- Create: `tests/test_mcp_tools.py`
- Create: `tests/test_mcp_errors.py`

**Reference:** Read `antfarm/core/colony_client.py` for exact method signatures. Read `antfarm/core/models.py` lines 150-250 for TaskArtifact fields. Read `antfarm/core/review_pack.py` for review pack generation.

- [ ] **Step 1: Write failing tests for tool handlers**

Create `tests/test_mcp_tools.py`:

```python
"""Tests for MCP tool handlers with mocked ColonyClient."""

from unittest.mock import MagicMock

import pytest

from antfarm.mcp.tools import (
    handle_blockers,
    handle_carry,
    handle_deregister_worker,
    handle_forage,
    handle_harvest,
    handle_list_tasks,
    handle_memory,
    handle_merge_ready,
    handle_plan_spec,
    handle_register_worker,
    handle_review_pack,
    handle_status,
    handle_trail,
    handle_workers,
)


@pytest.fixture
def mock_client():
    return MagicMock()


def test_carry_creates_task(mock_client):
    """carry tool calls client.carry with correct args."""
    mock_client.carry.return_value = {"id": "task-001", "status": "ready"}

    result = handle_carry(mock_client, {
        "title": "Build auth",
        "spec": "Implement JWT auth",
        "depends_on": ["task-000"],
        "touches": ["api", "auth"],
        "priority": 5,
    })

    assert result["id"] == "task-001"
    mock_client.carry.assert_called_once()
    call_kwargs = mock_client.carry.call_args
    assert call_kwargs.kwargs["title"] == "Build auth"
    assert call_kwargs.kwargs["priority"] == 5


def test_carry_generates_task_id(mock_client):
    """carry tool generates a task ID if none provided."""
    mock_client.carry.return_value = {"id": "task-123", "status": "ready"}

    handle_carry(mock_client, {"title": "Test", "spec": "Do thing"})

    call_kwargs = mock_client.carry.call_args
    assert call_kwargs.kwargs["task_id"].startswith("task-")


def test_carry_uses_defaults(mock_client):
    """carry tool uses default values for optional fields."""
    mock_client.carry.return_value = {"id": "task-001", "status": "ready"}

    handle_carry(mock_client, {"title": "Test", "spec": "Do thing"})

    call_kwargs = mock_client.carry.call_args
    assert call_kwargs.kwargs["depends_on"] == []
    assert call_kwargs.kwargs["touches"] == []
    assert call_kwargs.kwargs["priority"] == 10


def test_list_tasks_no_filter(mock_client):
    """list_tasks without status returns all tasks."""
    mock_client.list_tasks.return_value = [
        {"id": "task-001", "status": "ready"},
        {"id": "task-002", "status": "done"},
    ]

    result = handle_list_tasks(mock_client, {})
    assert len(result) == 2
    mock_client.list_tasks.assert_called_once_with(status=None)


def test_list_tasks_with_filter(mock_client):
    """list_tasks with status filter passes it through."""
    mock_client.list_tasks.return_value = [{"id": "task-001", "status": "ready"}]

    result = handle_list_tasks(mock_client, {"status": "ready"})
    mock_client.list_tasks.assert_called_once_with(status="ready")


def test_forage_returns_task(mock_client):
    """forage returns task when queue has work."""
    mock_client.forage.return_value = {
        "id": "task-001",
        "current_attempt": "att-001",
        "spec": "do stuff",
    }

    result = handle_forage(mock_client, {"worker_id": "node-1/claude-1"})
    assert result["id"] == "task-001"
    mock_client.forage.assert_called_once_with("node-1/claude-1")


def test_forage_returns_empty(mock_client):
    """forage returns empty message when queue is empty."""
    mock_client.forage.return_value = None

    result = handle_forage(mock_client, {"worker_id": "node-1/claude-1"})
    assert result["status"] == "empty"
    assert "message" in result


def test_trail_appends(mock_client):
    """trail calls client.trail."""
    mock_client.trail.return_value = None

    result = handle_trail(mock_client, {
        "task_id": "task-001",
        "worker_id": "node-1/claude-1",
        "message": "Implementing auth routes",
    })
    assert result["status"] == "ok"
    mock_client.trail.assert_called_once_with("task-001", "node-1/claude-1", "Implementing auth routes")


def test_harvest_marks_done(mock_client):
    """harvest calls client.harvest with all fields."""
    mock_client.harvest.return_value = None

    result = handle_harvest(mock_client, {
        "task_id": "task-001",
        "attempt_id": "att-001",
        "branch": "feat/auth",
        "pr": "https://github.com/org/repo/pull/42",
    })
    assert result["status"] == "ok"
    mock_client.harvest.assert_called_once_with(
        "task-001", "att-001", pr="https://github.com/org/repo/pull/42",
        branch="feat/auth", artifact=None,
    )


def test_harvest_with_artifact(mock_client):
    """harvest passes structured artifact to client."""
    mock_client.harvest.return_value = None

    artifact = {
        "task_id": "task-001",
        "attempt_id": "att-001",
        "worker_id": "node-1/claude-1",
        "branch": "feat/auth",
        "files_changed": ["api/auth.py", "tests/test_auth.py"],
        "lines_added": 120,
        "lines_removed": 5,
        "tests_ran": True,
        "tests_passed": True,
        "lint_ran": True,
        "lint_passed": True,
        "merge_readiness": "ready",
    }
    result = handle_harvest(mock_client, {
        "task_id": "task-001",
        "attempt_id": "att-001",
        "branch": "feat/auth",
        "pr": "",
        "artifact": artifact,
    })
    assert result["status"] == "ok"
    mock_client.harvest.assert_called_once_with(
        "task-001", "att-001", pr="", branch="feat/auth", artifact=artifact,
    )


def test_status_returns_full(mock_client):
    """status calls client.status_full."""
    mock_client.status_full.return_value = {
        "nodes": 2, "workers": 3, "tasks": {"ready": 5, "active": 2, "done": 10},
    }

    result = handle_status(mock_client, {})
    assert result["nodes"] == 2
    mock_client.status_full.assert_called_once()


def test_blockers_filters_tasks(mock_client):
    """blockers returns blocked/failed/signaled tasks."""
    mock_client.list_tasks.return_value = [
        {"id": "t1", "status": "ready", "signals": []},
        {"id": "t2", "status": "blocked", "signals": []},
        {"id": "t3", "status": "active", "signals": [{"message": "stuck"}]},
        {"id": "t4", "status": "done", "signals": []},
    ]

    result = handle_blockers(mock_client, {})
    ids = [t["id"] for t in result]
    assert "t2" in ids  # blocked
    assert "t3" in ids  # has signals
    assert "t1" not in ids  # ready, no signals
    assert "t4" not in ids  # done, no signals


def test_review_pack_returns_artifact(mock_client):
    """review_pack returns task with artifact data."""
    mock_client.get_task.return_value = {
        "id": "task-001",
        "title": "Build auth",
        "status": "done",
        "current_attempt": "att-001",
        "attempts": [{
            "attempt_id": "att-001",
            "status": "done",
            "artifact": {
                "task_id": "task-001",
                "files_changed": ["api.py"],
                "tests_ran": True,
                "tests_passed": True,
            },
        }],
    }

    result = handle_review_pack(mock_client, {"task_id": "task-001"})
    assert result["task_id"] == "task-001"
    assert "artifact" in result


def test_review_pack_no_artifact(mock_client):
    """review_pack returns info message when no artifact exists."""
    mock_client.get_task.return_value = {
        "id": "task-001",
        "status": "active",
        "current_attempt": "att-001",
        "attempts": [{"attempt_id": "att-001", "status": "active"}],
    }

    result = handle_review_pack(mock_client, {"task_id": "task-001"})
    assert "no_artifact" in result.get("status", "")


def test_review_pack_task_not_found(mock_client):
    """review_pack returns error when task doesn't exist."""
    mock_client.get_task.return_value = None

    result = handle_review_pack(mock_client, {"task_id": "task-999"})
    assert "error" in result


def test_merge_ready_filters_done_with_attempt(mock_client):
    """merge_ready returns done tasks with current_attempt set."""
    mock_client.list_tasks.return_value = [
        {"id": "t1", "status": "done", "current_attempt": "att-001"},
        {"id": "t2", "status": "done", "current_attempt": None},  # kicked back
    ]

    result = handle_merge_ready(mock_client, {})
    ids = [t["id"] for t in result]
    assert "t1" in ids
    assert "t2" not in ids


def test_workers_returns_list(mock_client):
    """workers returns list from client."""
    mock_client.list_workers.return_value = [
        {"worker_id": "node-1/claude-1", "status": "active"},
    ]

    result = handle_workers(mock_client, {})
    assert len(result) == 1
    assert result[0]["worker_id"] == "node-1/claude-1"


def test_register_worker(mock_client):
    """register_worker calls client.register_worker."""
    mock_client.register_worker.return_value = {
        "worker_id": "node-1/claude-1", "status": "idle",
    }

    result = handle_register_worker(mock_client, {
        "worker_id": "node-1/claude-1",
        "node_id": "node-1",
        "agent_type": "claude-code",
    })
    assert result["worker_id"] == "node-1/claude-1"
    mock_client.register_worker.assert_called_once()


def test_deregister_worker(mock_client):
    """deregister_worker calls client.deregister_worker."""
    mock_client.deregister_worker.return_value = None

    result = handle_deregister_worker(mock_client, {"worker_id": "node-1/claude-1"})
    assert result["status"] == "ok"
    mock_client.deregister_worker.assert_called_once_with("node-1/claude-1")


def test_plan_spec_unavailable(mock_client):
    """plan_spec returns unavailable when v0.5 planner not shipped."""
    result = handle_plan_spec(mock_client, {"spec": "Build auth"})
    # If planner module doesn't exist, should return unavailable status
    assert result.get("status") == "unavailable" or "tasks" in result


def test_memory_unavailable(mock_client):
    """memory returns unavailable when v0.5 memory store not shipped."""
    result = handle_memory(mock_client, {"query": "test command"})
    # If memory module doesn't exist, should return unavailable status
    assert result.get("status") == "unavailable" or "query" not in result.get("status", "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3.12 -m pytest tests/test_mcp_tools.py -v`
Expected: FAIL — `ImportError: cannot import name 'handle_carry'`

- [ ] **Step 3: Implement all tool handlers**

Replace `antfarm/mcp/tools.py` with the full implementation:

```python
"""MCP tool definitions and handlers for Antfarm.

Each tool maps to a ColonyClient method or a computed view of colony state.
Handlers take a ColonyClient and arguments dict, return JSON-serializable data.
"""

import json
import time

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool

from antfarm.core.colony_client import ColonyClient
from antfarm.mcp.config import AntfarmConfig


# ---------------------------------------------------------------------------
# Tool handlers — one function per tool, takes (client, arguments) → dict
# ---------------------------------------------------------------------------


def handle_carry(client: ColonyClient, arguments: dict) -> dict:
    task_id = arguments.get("id", f"task-{int(time.time() * 1000)}")
    return client.carry(
        task_id=task_id,
        title=arguments["title"],
        spec=arguments["spec"],
        depends_on=arguments.get("depends_on", []),
        touches=arguments.get("touches", []),
        priority=arguments.get("priority", 10),
        complexity=arguments.get("complexity", "M"),
    )


def handle_list_tasks(client: ColonyClient, arguments: dict) -> list[dict]:
    return client.list_tasks(status=arguments.get("status"))


def handle_forage(client: ColonyClient, arguments: dict) -> dict:
    result = client.forage(arguments["worker_id"])
    if result is None:
        return {"status": "empty", "message": "No tasks available in queue"}
    return result


def handle_trail(client: ColonyClient, arguments: dict) -> dict:
    client.trail(arguments["task_id"], arguments["worker_id"], arguments["message"])
    return {"status": "ok"}


def handle_harvest(client: ColonyClient, arguments: dict) -> dict:
    client.harvest(
        arguments["task_id"],
        arguments["attempt_id"],
        pr=arguments.get("pr", ""),
        branch=arguments["branch"],
        artifact=arguments.get("artifact"),
    )
    return {"status": "ok"}


def handle_status(client: ColonyClient, arguments: dict) -> dict:
    return client.status_full()


def handle_blockers(client: ColonyClient, arguments: dict) -> list[dict]:
    all_tasks = client.list_tasks()
    return [
        t for t in all_tasks
        if t.get("status") in ("blocked", "paused", "failed")
        or t.get("signals")
    ]


def handle_review_pack(client: ColonyClient, arguments: dict) -> dict:
    task = client.get_task(arguments["task_id"])
    if task is None:
        return {"error": f"Task {arguments['task_id']} not found"}

    current = task.get("current_attempt")
    if not current:
        return {
            "task_id": task["id"],
            "status": "no_artifact",
            "message": "No current attempt — task may have been kicked back",
        }

    for attempt in task.get("attempts", []):
        if attempt.get("attempt_id") == current:
            artifact = attempt.get("artifact")
            if artifact:
                return {
                    "task_id": task["id"],
                    "title": task.get("title", ""),
                    "status": task.get("status"),
                    "attempt_id": current,
                    "artifact": artifact,
                }
            return {
                "task_id": task["id"],
                "status": "no_artifact",
                "message": "Current attempt has no artifact yet",
            }

    return {
        "task_id": task["id"],
        "status": "no_artifact",
        "message": f"Attempt {current} not found in task attempts",
    }


def handle_merge_ready(client: ColonyClient, arguments: dict) -> list[dict]:
    done_tasks = client.list_tasks(status="done")
    return [t for t in done_tasks if t.get("current_attempt") is not None]


def handle_workers(client: ColonyClient, arguments: dict) -> list[dict]:
    return client.list_workers()


def handle_register_worker(client: ColonyClient, arguments: dict) -> dict:
    return client.register_worker(
        worker_id=arguments["worker_id"],
        node_id=arguments["node_id"],
        agent_type=arguments.get("agent_type", "claude-code"),
        workspace_root=arguments.get("workspace_root", "."),
        capabilities=arguments.get("capabilities"),
    )


def handle_deregister_worker(client: ColonyClient, arguments: dict) -> dict:
    client.deregister_worker(arguments["worker_id"])
    return {"status": "ok"}


def handle_plan_spec(client: ColonyClient, arguments: dict) -> dict:
    """Decompose a spec into tasks. Delegates to v0.5 planner module.

    NOTE: This handler depends on v0.5 planner being shipped. If the planner
    module is not available, returns a structured message asking the user
    to decompose manually via /antfarm-plan skill.
    """
    try:
        from antfarm.core.planner import decompose_spec

        tasks = decompose_spec(
            spec=arguments["spec"],
            repo_context=arguments.get("repo_context"),
        )
        return {"tasks": tasks, "count": len(tasks)}
    except ImportError:
        return {
            "status": "unavailable",
            "message": "Planner module not available — use /antfarm-plan skill to decompose manually",
        }


def handle_memory(client: ColonyClient, arguments: dict) -> dict:
    """Get repo facts, hotspots, failure patterns from memory store.

    NOTE: This handler depends on v0.5 memory store being shipped. If the
    memory module is not available, returns a structured message.
    """
    try:
        from antfarm.core.memory import get_repo_facts

        query = arguments.get("query", "")
        return get_repo_facts(query=query)
    except ImportError:
        return {
            "status": "unavailable",
            "message": "Memory store not available — this is a v0.5 feature",
        }


# ---------------------------------------------------------------------------
# Handler dispatch
# ---------------------------------------------------------------------------

HANDLERS: dict[str, callable] = {
    "antfarm_carry": handle_carry,
    "antfarm_list_tasks": handle_list_tasks,
    "antfarm_forage": handle_forage,
    "antfarm_trail": handle_trail,
    "antfarm_harvest": handle_harvest,
    "antfarm_status": handle_status,
    "antfarm_blockers": handle_blockers,
    "antfarm_review_pack": handle_review_pack,
    "antfarm_merge_ready": handle_merge_ready,
    "antfarm_workers": handle_workers,
    "antfarm_register_worker": handle_register_worker,
    "antfarm_deregister_worker": handle_deregister_worker,
    "antfarm_plan_spec": handle_plan_spec,
    "antfarm_memory": handle_memory,
}


# ---------------------------------------------------------------------------
# Tool definitions (MCP schemas)
# ---------------------------------------------------------------------------

def _build_tool_defs() -> list[Tool]:
    return [
        Tool(
            name="antfarm_carry",
            description="Create a new task in the colony queue",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Task title"},
                    "spec": {"type": "string", "description": "Task specification"},
                    "id": {"type": "string", "description": "Task ID (auto-generated if omitted)"},
                    "depends_on": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Task IDs this depends on", "default": [],
                    },
                    "touches": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Module/scope hints for conflict prevention", "default": [],
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Priority (lower=higher, default 10)", "default": 10,
                    },
                    "complexity": {
                        "type": "string",
                        "description": "S, M, or L (default M)", "default": "M",
                    },
                },
                "required": ["title", "spec"],
            },
        ),
        Tool(
            name="antfarm_list_tasks",
            description="List tasks, optionally filtered by status",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter: ready, active, done, paused, blocked",
                    },
                },
            },
        ),
        Tool(
            name="antfarm_forage",
            description="Claim the next available task for a worker",
            inputSchema={
                "type": "object",
                "properties": {
                    "worker_id": {
                        "type": "string",
                        "description": "Worker ID (e.g. node-1/claude-1)",
                    },
                },
                "required": ["worker_id"],
            },
        ),
        Tool(
            name="antfarm_trail",
            description="Log a progress update on a task",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "worker_id": {"type": "string"},
                    "message": {"type": "string", "description": "Progress message"},
                },
                "required": ["task_id", "worker_id", "message"],
            },
        ),
        Tool(
            name="antfarm_harvest",
            description="Mark a task as complete with branch, PR, and structured artifact",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "attempt_id": {"type": "string"},
                    "branch": {"type": "string", "description": "Git branch name"},
                    "pr": {"type": "string", "description": "PR URL (optional)", "default": ""},
                    "artifact": {
                        "type": "object",
                        "description": "Structured TaskArtifact (v0.5): files_changed, test/lint results, merge readiness, risks",
                    },
                },
                "required": ["task_id", "attempt_id", "branch"],
            },
        ),
        Tool(
            name="antfarm_status",
            description="Get full colony status: nodes, workers, task counts, and details",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="antfarm_blockers",
            description="List tasks needing attention: blocked, failed, stale, or signaled",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="antfarm_review_pack",
            description="Get review pack for a completed task (artifact, checks, risks)",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID to review"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="antfarm_merge_ready",
            description="List done tasks ready for the Soldier to merge",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="antfarm_workers",
            description="List all registered workers and their status",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="antfarm_register_worker",
            description="Register a worker with the colony (required before forage)",
            inputSchema={
                "type": "object",
                "properties": {
                    "worker_id": {
                        "type": "string",
                        "description": "Worker ID (e.g. node-1/claude-1)",
                    },
                    "node_id": {"type": "string", "description": "Node this worker runs on"},
                    "agent_type": {
                        "type": "string",
                        "description": "Agent type (claude-code, codex, aider, generic)",
                        "default": "claude-code",
                    },
                    "workspace_root": {
                        "type": "string",
                        "description": "Workspace root directory",
                        "default": ".",
                    },
                    "capabilities": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Worker capabilities",
                    },
                },
                "required": ["worker_id", "node_id"],
            },
        ),
        Tool(
            name="antfarm_deregister_worker",
            description="Deregister a worker from the colony",
            inputSchema={
                "type": "object",
                "properties": {
                    "worker_id": {"type": "string", "description": "Worker ID to deregister"},
                },
                "required": ["worker_id"],
            },
        ),
        Tool(
            name="antfarm_plan_spec",
            description="Decompose a feature spec into Antfarm tasks (requires v0.5 planner)",
            inputSchema={
                "type": "object",
                "properties": {
                    "spec": {"type": "string", "description": "Feature specification to decompose"},
                    "repo_context": {
                        "type": "string",
                        "description": "Optional repo context (file structure, conventions)",
                    },
                },
                "required": ["spec"],
            },
        ),
        Tool(
            name="antfarm_memory",
            description="Get repo facts, hotspots, and failure patterns (requires v0.5 memory store)",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look up (e.g. 'test command', 'hot files', 'failure patterns')",
                        "default": "",
                    },
                },
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_tools(server: Server, config: AntfarmConfig) -> None:
    """Register all Antfarm MCP tools on the server."""
    client = ColonyClient(config.colony_url, token=config.token)

    @server.list_tools()
    async def list_tools():
        return _build_tool_defs()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        handler = HANDLERS.get(name)
        if handler is None:
            return [_error_response(f"Unknown tool: {name}")]

        # Retry once on connection error (handles stale connection after colony restart)
        last_error = None
        for attempt in range(2):
            try:
                result = handler(client, arguments)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
            except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
                last_error = e
                if attempt == 0:
                    continue  # retry once
                return [_error_response(
                    f"Cannot reach Antfarm colony at {config.colony_url} — is it running?"
                )]
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    return [_error_response("Authentication failed — check your token")]
                if e.response.status_code == 409:
                    return [_error_response(f"Conflict: {e.response.text}")]
                return [_error_response(
                    f"Colony returned {e.response.status_code}: {e.response.text}"
                )]
            except httpx.TimeoutException:
                return [_error_response(
                    f"Request to colony timed out ({config.colony_url})"
                )]
            except Exception as e:
                return [_error_response(f"Unexpected error: {e}")]


def _error_response(message: str, code: str = "unknown", retryable: bool = False) -> TextContent:
    return TextContent(type="text", text=json.dumps({
        "error": {"code": code, "message": message, "retryable": retryable},
    }))
```

- [ ] **Step 4: Run tool handler tests**

Run: `python3.12 -m pytest tests/test_mcp_tools.py -v`
Expected: All PASS

- [ ] **Step 5: Write failing error-mode tests**

Create `tests/test_mcp_errors.py`:

```python
"""Tests for MCP error handling — colony down, auth, timeout, stale connection."""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from antfarm.mcp.config import AntfarmConfig


def _make_server():
    """Create a server with test config for error testing."""
    from antfarm.mcp.server import create_mcp_server

    config = AntfarmConfig(colony_url="http://localhost:7433", token="test-token")
    return create_mcp_server(config), config


@pytest.fixture
def mock_client():
    return MagicMock()


def test_connect_error_message(mock_client):
    """Connection refused produces clear error."""
    from antfarm.mcp.tools import handle_status, register_tools

    mock_client.status_full.side_effect = httpx.ConnectError("Connection refused")

    # Handler raises; the register_tools wrapper catches it
    with pytest.raises(httpx.ConnectError):
        handle_status(mock_client, {})


def test_auth_error_message(mock_client):
    """401 produces auth-specific error."""
    response = httpx.Response(401, text="Unauthorized")
    mock_client.status_full.side_effect = httpx.HTTPStatusError(
        "401", request=httpx.Request("GET", "http://test"), response=response,
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        from antfarm.mcp.tools import handle_status
        handle_status(mock_client, {})

    assert exc_info.value.response.status_code == 401


def test_timeout_error(mock_client):
    """Timeout produces clear error."""
    mock_client.forage.side_effect = httpx.TimeoutException("timed out")

    with pytest.raises(httpx.TimeoutException):
        from antfarm.mcp.tools import handle_forage
        handle_forage(mock_client, {"worker_id": "test"})


def test_token_not_in_error_output():
    """Token never appears in error response text."""
    from antfarm.mcp.tools import _error_response

    err = _error_response("Authentication failed — check your token", code="auth_failed")
    # Verify the error message doesn't contain actual token values
    assert "test-secret-token" not in err.text


def test_error_envelope_structure():
    """Error responses use standard envelope with code, message, retryable."""
    import json

    from antfarm.mcp.tools import _error_response

    err = _error_response("Colony down", code="connection_failed", retryable=True)
    payload = json.loads(err.text)
    assert "error" in payload
    assert payload["error"]["code"] == "connection_failed"
    assert payload["error"]["message"] == "Colony down"
    assert payload["error"]["retryable"] is True

    err2 = _error_response("Auth failed", code="auth_failed")
    payload2 = json.loads(err2.text)
    assert payload2["error"]["retryable"] is False


def test_conflict_409(mock_client):
    """409 Conflict produces specific error."""
    response = httpx.Response(409, text="Task task-001 already exists")
    mock_client.carry.side_effect = httpx.HTTPStatusError(
        "409", request=httpx.Request("POST", "http://test"), response=response,
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        from antfarm.mcp.tools import handle_carry
        handle_carry(mock_client, {"title": "Test", "spec": "Do thing"})

    assert exc_info.value.response.status_code == 409
```

- [ ] **Step 6: Run error tests**

Run: `python3.12 -m pytest tests/test_mcp_errors.py -v`
Expected: All PASS (handlers raise, wrapper catches — tests verify handlers propagate correctly)

- [ ] **Step 7: Update server test to verify tool list**

Add to `tests/test_mcp_server.py`:

```python
def test_tool_definitions_complete():
    """Server exposes all 12 expected tools."""
    from antfarm.mcp.tools import _build_tool_defs

    tools = _build_tool_defs()
    tool_names = {t.name for t in tools}

    expected = {
        "antfarm_carry",
        "antfarm_list_tasks",
        "antfarm_forage",
        "antfarm_trail",
        "antfarm_harvest",
        "antfarm_status",
        "antfarm_blockers",
        "antfarm_review_pack",
        "antfarm_merge_ready",
        "antfarm_workers",
        "antfarm_plan_spec",
        "antfarm_memory",
    }
    assert expected == tool_names, f"Missing: {expected - tool_names}, Extra: {tool_names - expected}"


def test_all_tools_have_handlers():
    """Every defined tool has a registered handler."""
    from antfarm.mcp.tools import HANDLERS, _build_tool_defs

    tools = _build_tool_defs()
    for tool in tools:
        assert tool.name in HANDLERS, f"Tool {tool.name} has no handler"
```

- [ ] **Step 8: Run all MCP tests**

Run: `python3.12 -m pytest tests/test_mcp_config.py tests/test_mcp_server.py tests/test_mcp_tools.py tests/test_mcp_errors.py -v`
Expected: All PASS

- [ ] **Step 9: Run full suite**

Run: `python3.12 -m pytest tests/ -x -q --ignore=tests/test_redis_backend.py`
Expected: All pass

- [ ] **Step 10: Commit**

```bash
git add antfarm/mcp/tools.py tests/test_mcp_tools.py tests/test_mcp_errors.py tests/test_mcp_server.py
git commit -m "feat(mcp): add 10 tool handlers with structured error handling"
```

---

## Task 3: Core Slash Commands (Milestone 1)

**Files:**
- Create: `antfarm/plugin/skills/antfarm-plan.md`
- Create: `antfarm/plugin/skills/antfarm-status.md`
- Create: `antfarm/plugin/skills/antfarm-start.md`
- Create: `antfarm/plugin/skills/antfarm-done.md`
- Create: `antfarm/plugin/skills/antfarm-review.md`

These are markdown skill definitions — they orchestrate MCP tool calls. No Python tests needed (the tools they call are tested in Task 2). Each skill should be concise (10-25 lines of instructions).

- [ ] **Step 1: Create plugin directory structure**

```bash
mkdir -p antfarm/plugin/skills antfarm/plugin/hooks antfarm/plugin/agents
```

- [ ] **Step 2: Create `/antfarm-plan` skill**

Create `antfarm/plugin/skills/antfarm-plan.md`:

```markdown
---
name: antfarm-plan
description: Decompose a feature spec into Antfarm tasks with dependencies and scope hints
---

# /antfarm-plan

Takes a feature spec and breaks it into parallel tasks for the Antfarm colony.

## Instructions

1. Read the user's spec (provided as argument, or ask for it)
2. Call `antfarm_status` to understand current colony state and existing tasks
3. Break the spec into 3-10 focused tasks. For each task determine:
   - **title**: short imperative description (e.g. "Add JWT auth middleware")
   - **spec**: detailed implementation instructions (what to build, how to test, what files to touch)
   - **depends_on**: task IDs this task must wait for (use IDs from step 5)
   - **touches**: module/scope hints for conflict prevention (e.g. "api", "auth", "db")
   - **priority**: 1-10 (lower = higher priority, default 10)
4. Show the proposed tasks to the user in a numbered list with dependencies
5. On user confirmation, call `antfarm_carry` for each task (assign sequential IDs)
6. Call `antfarm_status` to show the final queue state

## Rules

- Tasks should be independently implementable (one worker per task)
- Prefer smaller tasks (S/M complexity) over large ones
- Dependencies should form a DAG, not a chain — maximize parallelism
- Touches hints prevent two workers from editing the same files
```

- [ ] **Step 3: Create `/antfarm-status` skill**

Create `antfarm/plugin/skills/antfarm-status.md`:

```markdown
---
name: antfarm-status
description: Show Antfarm colony status — workers, tasks, blockers at a glance
---

# /antfarm-status

Shows the current state of the Antfarm colony.

## Instructions

1. Call `antfarm_status` to get full colony state
2. Present a formatted summary:
   - **Workers**: count by status (active, idle, offline)
   - **Tasks**: count by status (ready, active, done, blocked, paused)
   - **Blockers**: any blocked or signaled tasks (show task ID + reason)
   - **Merge queue**: tasks done and waiting for Soldier
3. Keep the output concise — table or bullet format, not walls of text

## Rules

- This is read-only — never modify state
- If colony is unreachable, say so clearly
```

- [ ] **Step 4: Create `/antfarm-start` skill**

Create `antfarm/plugin/skills/antfarm-start.md`:

```markdown
---
name: antfarm-start
description: Register as a worker, claim a task, and set up a workspace to start working
---

# /antfarm-start

One command to go from idle to working on a task.

## Instructions

1. Check if there is already an active task in this session (environment variable ANTFARM_TASK_ID)
   - If set: refuse to start. Show the current task ID and suggest `/antfarm-done` first
2. Call `antfarm_forage` with your worker_id to claim a task
   - If no task available: report "Queue empty — no tasks to work on" and stop
3. Read the task spec carefully
4. Set up the workspace:
   - Create a git worktree or branch for the task
   - If workspace setup fails: call `antfarm_trail` to log the failure, then stop
5. Show the task to the user: ID, title, spec, dependencies, touches
6. Begin implementing the task

## Edge Cases

| Condition | Behavior |
|-----------|----------|
| No task available | Report clearly, do not register an idle worker |
| Already active on a task | Refuse, show current task ID, suggest `/antfarm-done` |
| Workspace setup fails | Trail the failure, surface error, do not leave task orphaned |
| Colony unreachable | Fail with clear error before any local side effects |

## Worker Identity

Default worker_id: `{hostname}/{agent-type}-{session-id}`

## Rules

- Never start a second task without finishing the first
- Heartbeat is handled automatically by the heartbeat hook
- Call `antfarm_trail` periodically to log progress during work
```

- [ ] **Step 5: Create `/antfarm-done` skill**

Create `antfarm/plugin/skills/antfarm-done.md`:

```markdown
---
name: antfarm-done
description: Explicitly harvest the current task and optionally start the next one
---

# /antfarm-done

Marks the current task as complete. This is the ONLY way to harvest — hooks never auto-harvest.

## Instructions

1. Verify there is an active task (ANTFARM_TASK_ID must be set)
   - If not set: report "No active task — nothing to harvest" and stop
2. Ensure work is committed and pushed to the task branch
3. Call `antfarm_harvest` with:
   - task_id: current task ID
   - attempt_id: current attempt ID
   - branch: current git branch name
   - pr: PR URL if one was created (empty string if not)
4. Report harvest success with task ID and branch
5. Ask the user: "Forage next task?" 
   - If yes: call `antfarm_forage` and set up the next task (same flow as `/antfarm-start`)
   - If no: done

## Rules

- Harvest requires explicit invocation — never triggered automatically
- Always commit and push before harvesting
- If harvest fails (e.g., 409 conflict), surface the error clearly
```

- [ ] **Step 6: Create `/antfarm-review` skill**

Create `antfarm/plugin/skills/antfarm-review.md`:

```markdown
---
name: antfarm-review
description: Show the review pack for a completed task — artifact, checks, risks, verdict
---

# /antfarm-review

Shows the review pack for a specific task. Consumes the v0.5 ReviewVerdict contract.

## Instructions

1. Get the task_id (from argument, or ask the user)
2. Call `antfarm_review_pack` with the task_id
3. If no artifact exists: report status clearly (e.g., "Task not yet harvested" or "Attempt kicked back")
4. If artifact exists, present a formatted review:
   - **Summary**: artifact summary
   - **Files changed**: list with line counts
   - **Checks**: build/test/lint status (passed/failed/not run)
   - **Merge readiness**: ready / needs_review / blocked (with reasons)
   - **Risks**: any flagged risks
   - **Review focus**: suggested areas to review
5. If a ReviewVerdict exists on the task, show it:
   - Verdict: pass / needs_changes / blocked
   - Findings with severity
   - Provider (human, claude_code, etc.)
6. If no verdict exists yet, say "Not yet reviewed"

## Rules

- This is read-only — never modify task state
- The reviewer agent (reviewer.md) produces ReviewVerdicts — this skill only displays them
- Review pack data comes from the v0.5 trust contract, not from re-reading code
```

- [ ] **Step 7: Commit**

```bash
git add antfarm/plugin/skills/
git commit -m "feat(plugin): add core slash commands — plan, status, start, done, review"
```

---

## Task 4: Safe Hooks + Remaining Skills (Milestone 2)

**Files:**
- Create: `antfarm/plugin/hooks/heartbeat.sh`
- Create: `antfarm/plugin/hooks/failure_trail.sh`
- Create: `antfarm/plugin/hooks/workspace_observe.sh`
- Create: `antfarm/plugin/skills/antfarm-blockers.md`
- Create: `antfarm/plugin/skills/antfarm-merge-ready.md`
- Create: `tests/test_hooks.py`

- [ ] **Step 1: Write failing hook safety tests**

Create `tests/test_hooks.py`:

```python
"""Tests for hook safety invariants.

Hooks must observe and synchronize — never create canonical state.
These tests verify hooks do not harvest, kickback, merge, or mutate artifacts.
"""

import os
import stat
import subprocess

import pytest

HOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "antfarm", "plugin", "hooks")


def _hook_path(name: str) -> str:
    return os.path.join(HOOKS_DIR, name)


def _hook_content(name: str) -> str:
    with open(_hook_path(name)) as f:
        return f.read()


@pytest.mark.parametrize("hook", ["heartbeat.sh", "failure_trail.sh", "workspace_observe.sh"])
def test_hooks_are_executable(hook):
    """All hooks must have executable permission."""
    path = _hook_path(hook)
    assert os.path.exists(path), f"{hook} does not exist"
    assert os.access(path, os.X_OK), f"{hook} is not executable"


@pytest.mark.parametrize("hook", ["heartbeat.sh", "failure_trail.sh", "workspace_observe.sh"])
def test_hooks_never_harvest(hook):
    """No hook should call harvest endpoint."""
    content = _hook_content(hook)
    assert "/harvest" not in content, f"{hook} must not call harvest endpoint"


@pytest.mark.parametrize("hook", ["heartbeat.sh", "failure_trail.sh", "workspace_observe.sh"])
def test_hooks_never_kickback(hook):
    """No hook should call kickback endpoint."""
    content = _hook_content(hook)
    assert "/kickback" not in content, f"{hook} must not call kickback endpoint"


@pytest.mark.parametrize("hook", ["heartbeat.sh", "failure_trail.sh", "workspace_observe.sh"])
def test_hooks_never_merge(hook):
    """No hook should call merge endpoint."""
    content = _hook_content(hook)
    assert "/merge" not in content, f"{hook} must not call merge endpoint"


@pytest.mark.parametrize("hook", ["heartbeat.sh", "failure_trail.sh", "workspace_observe.sh"])
def test_hooks_fail_silently(hook):
    """Hooks must use || true to avoid breaking the Claude session."""
    content = _hook_content(hook)
    assert "|| true" in content, f"{hook} must fail silently with || true"


def test_heartbeat_exits_without_worker_id():
    """Heartbeat hook exits cleanly when WORKER_ID is not set."""
    result = subprocess.run(
        ["bash", _hook_path("heartbeat.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, "WORKER_ID": "", "ANTFARM_URL": "http://localhost:9999"},
        timeout=5,
    )
    # Should exit 0 (silent exit, not crash)
    assert result.returncode == 0


def test_failure_trail_exits_without_task_id():
    """Failure trail hook exits cleanly when ANTFARM_TASK_ID is not set."""
    result = subprocess.run(
        ["bash", _hook_path("failure_trail.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, "ANTFARM_TASK_ID": "", "ANTFARM_URL": "http://localhost:9999"},
        timeout=5,
    )
    assert result.returncode == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3.12 -m pytest tests/test_hooks.py -v`
Expected: FAIL — hook files don't exist

- [ ] **Step 3: Create heartbeat hook**

Create `antfarm/plugin/hooks/heartbeat.sh`:

```bash
#!/usr/bin/env bash
# PostToolUse hook: send heartbeat to colony
# Default: ON
# Fails silently — never breaks the Claude session
[ -z "$WORKER_ID" ] && exit 0
curl -s -m 1 "${ANTFARM_URL:-http://localhost:7433}/workers/${WORKER_ID}/heartbeat" \
  -X POST -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ANTFARM_TOKEN}" \
  -d '{"status": {}}' || true
```

- [ ] **Step 4: Create failure trail hook**

Create `antfarm/plugin/hooks/failure_trail.sh`:

```bash
#!/usr/bin/env bash
# PostToolUseFailure hook: log failure context as trail entry
# Default: ON
# Observes only — never mutates canonical state
[ -z "$ANTFARM_TASK_ID" ] && exit 0
[ -z "$WORKER_ID" ] && exit 0
TOOL_NAME="${TOOL_NAME:-unknown}"
ERROR_MSG="${ERROR_MESSAGE:-tool failure}"
curl -s -m 2 "${ANTFARM_URL:-http://localhost:7433}/tasks/${ANTFARM_TASK_ID}/trail" \
  -X POST -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ANTFARM_TOKEN}" \
  -d "{\"worker_id\": \"${WORKER_ID}\", \"message\": \"[hook] tool failure: ${TOOL_NAME} — ${ERROR_MSG}\"}" || true
```

- [ ] **Step 5: Create workspace observe hook**

Create `antfarm/plugin/hooks/workspace_observe.sh`:

```bash
#!/usr/bin/env bash
# PostToolUse hook: observe workspace changes — provisional, not canonical
# Default: OPT-IN (not enabled by default)
# Writes changed-file count to trail, NOT to the artifact
[ -z "$ANTFARM_TASK_ID" ] && exit 0
[ -z "$WORKER_ID" ] && exit 0
FILE_COUNT=$(git diff --name-only HEAD 2>/dev/null | wc -l | tr -d ' ')
[ "$FILE_COUNT" = "0" ] && exit 0
curl -s -m 1 "${ANTFARM_URL:-http://localhost:7433}/tasks/${ANTFARM_TASK_ID}/trail" \
  -X POST -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ANTFARM_TOKEN}" \
  -d "{\"worker_id\": \"${WORKER_ID}\", \"message\": \"[observe] ${FILE_COUNT} files modified since HEAD\"}" || true
```

- [ ] **Step 6: Make hooks executable**

```bash
chmod +x antfarm/plugin/hooks/*.sh
```

- [ ] **Step 7: Run hook tests**

Run: `python3.12 -m pytest tests/test_hooks.py -v`
Expected: All PASS

- [ ] **Step 8: Create `/antfarm-blockers` skill**

Create `antfarm/plugin/skills/antfarm-blockers.md`:

```markdown
---
name: antfarm-blockers
description: List tasks needing attention — blocked, failed, stale, or signaled
---

# /antfarm-blockers

Shows tasks that need human or operator attention.

## Instructions

1. Call `antfarm_blockers` to get attention-needed tasks
2. If empty: report "No blockers — colony is clear"
3. If results: present each task with:
   - Task ID and title
   - Status (blocked / paused / failed)
   - Signals (if any) — show worker_id and message
   - Suggested action (e.g., "unblock", "reassign", "investigate signal")
4. Keep output concise — one line per task plus signals

## Rules

- Read-only — never modify state
- Signals are informational — they indicate a worker flagged something
```

- [ ] **Step 9: Create `/antfarm-merge-ready` skill**

Create `antfarm/plugin/skills/antfarm-merge-ready.md`:

```markdown
---
name: antfarm-merge-ready
description: List tasks that are done and ready for the Soldier to merge
---

# /antfarm-merge-ready

Shows tasks waiting in the merge queue.

## Instructions

1. Call `antfarm_merge_ready` to get done tasks with active attempts
2. If empty: report "No tasks waiting for merge"
3. If results: present each task with:
   - Task ID and title
   - Attempt ID and branch
   - PR URL (if available)
   - Merge readiness from artifact (ready / needs_review / blocked)
4. Remind user: "Soldier processes the merge queue automatically. Use `antfarm soldier` to run it."

## Rules

- Read-only — never modify state
- Merge decisions are made by the Soldier, not this skill
```

- [ ] **Step 10: Commit**

```bash
git add antfarm/plugin/hooks/ antfarm/plugin/skills/antfarm-blockers.md antfarm/plugin/skills/antfarm-merge-ready.md tests/test_hooks.py
git commit -m "feat(plugin): add safe hooks and remaining slash commands"
```

---

## Task 5: Agent Definitions + Plugin Packaging

**Files:**
- Create: `antfarm/plugin/agents/worker.md`
- Create: `antfarm/plugin/agents/planner.md`
- Create: `antfarm/plugin/agents/reviewer.md`
- Create: `antfarm/plugin/package.json`

- [ ] **Step 1: Create worker agent**

Create `antfarm/plugin/agents/worker.md`:

```markdown
---
name: antfarm-worker
description: Antfarm worker — claims and implements tasks from the colony queue
---

# Antfarm Worker

You are an Antfarm worker. Your job is to claim tasks, implement them, and report results.

## Workflow

1. Call `antfarm_forage` with your worker_id to claim a task
2. Read the task spec carefully
3. Set up the workspace (git worktree or branch)
4. Implement the changes described in the spec
5. Run tests and lint: `pytest tests/ -x -q && ruff check .`
6. Commit and push your branch
7. Call `antfarm_harvest` with task_id, attempt_id, branch, and PR URL

## During Work

- Call `antfarm_trail` every few steps to log progress (e.g., "Implementing auth routes", "Tests passing")
- Heartbeat is handled automatically by the PostToolUse hook
- If you encounter a problem you can't solve, call `antfarm_trail` with the issue description

## Rules

- One logical change per commit
- Run tests before harvesting — never harvest with failing tests
- Never push directly to main or the integration branch
- Never call harvest automatically — only when explicitly ready
- If the spec is unclear, use `antfarm_trail` to signal the question
```

- [ ] **Step 2: Create planner agent**

Create `antfarm/plugin/agents/planner.md`:

```markdown
---
name: antfarm-planner
description: Antfarm planner — decomposes specs into parallelizable tasks
---

# Antfarm Planner

You are an Antfarm planner. Your job is to break feature specs into focused, parallelizable tasks.

## Workflow

1. Read the feature spec provided by the user
2. Call `antfarm_status` to understand current colony state and existing tasks
3. Decompose the spec into 3-10 tasks, each independently implementable by one worker
4. For each task, define:
   - **title**: imperative, specific (e.g., "Add JWT middleware to API routes")
   - **spec**: complete implementation instructions — what to build, which files, how to test
   - **depends_on**: IDs of tasks that must complete first (minimize dependencies)
   - **touches**: module/scope hints (e.g., "api", "auth", "db") for conflict prevention
   - **priority**: 1-10, lower = higher (default 10)
5. Present the task list to the user for review
6. On confirmation, call `antfarm_carry` for each task
7. Call `antfarm_status` to show the final queue

## Rules

- Maximize parallelism — prefer independent tasks over long dependency chains
- Each task should be S or M complexity (one worker, few hours)
- Specs must be self-contained — a worker reading only the spec should know exactly what to do
- Include test instructions in every task spec
- Use touches to prevent scope conflicts between parallel tasks
```

- [ ] **Step 3: Create reviewer agent**

Create `antfarm/plugin/agents/reviewer.md`:

```markdown
---
name: antfarm-reviewer
description: Antfarm reviewer — reviews completed tasks using the v0.5 ReviewVerdict contract
---

# Antfarm Reviewer

You are an Antfarm reviewer. Your job is to review completed task work and produce ReviewVerdicts.

You consume the v0.5 review contract — TaskArtifact and ReviewVerdict. The plugin UX surfaces existing trust primitives; it does not invent new merge truth.

## Workflow

1. Call `antfarm_review_pack` to get the task's artifact
2. If no artifact: report that the task is not ready for review
3. Review the artifact:
   - **Check results**: Did build/tests/lint pass?
   - **Merge readiness**: Is the artifact ready, needs_review, or blocked?
   - **Files changed**: Are the changes focused and appropriate for the task spec?
   - **Risks**: Any flagged risks to investigate?
   - **Review focus**: Areas the worker highlighted for review
4. Read the actual code changes (PR diff or branch diff)
5. Produce a verdict:
   - **pass**: Changes look good, ready for Soldier to merge
   - **needs_changes**: Specific issues found, worker should address them
   - **blocked**: Fundamental problem — task needs re-scoping or discussion
6. Report your findings clearly

## Rules

- Never modify task state directly — your verdict is advisory input to the operator
- The Soldier makes the final merge decision (deterministic gate)
- Review the actual code, not just the artifact metadata
- If artifact checks failed (tests, lint), that alone is grounds for needs_changes
- Be specific in findings — "auth middleware doesn't validate token expiry" not "needs work"
```

- [ ] **Step 4: Create plugin manifest**

Create `antfarm/plugin/package.json`:

```json
{
  "name": "antfarm",
  "version": "0.6.0",
  "description": "Antfarm — coordinate AI coding agents across machines",
  "skills": [
    "skills/antfarm-plan.md",
    "skills/antfarm-status.md",
    "skills/antfarm-start.md",
    "skills/antfarm-done.md",
    "skills/antfarm-review.md",
    "skills/antfarm-blockers.md",
    "skills/antfarm-merge-ready.md"
  ],
  "agents": [
    "agents/worker.md",
    "agents/planner.md",
    "agents/reviewer.md"
  ],
  "hooks": {
    "PostToolUse": ["hooks/heartbeat.sh"],
    "PostToolUseFailure": ["hooks/failure_trail.sh"]
  },
  "mcpServers": {
    "antfarm": {
      "command": "python3",
      "args": ["-m", "antfarm.mcp.server"]
    }
  }
}
```

Note: `workspace_observe.sh` is opt-in and NOT listed in the default hooks. Users enable it manually if desired.

- [ ] **Step 5: Commit**

```bash
git add antfarm/plugin/agents/ antfarm/plugin/package.json
git commit -m "feat(plugin): add agent definitions and plugin manifest"
```

---

## Task 6: Integration Test + Version Bump

**Files:**
- Modify: `pyproject.toml` (version bump)

- [ ] **Step 1: Run full test suite**

Run: `python3.12 -m pytest tests/ -x -q --ignore=tests/test_redis_backend.py`
Expected: All pass

- [ ] **Step 2: Run linter**

Run: `ruff check .`
Expected: Clean

- [ ] **Step 3: Verify MCP server starts**

```bash
# Quick smoke test — server should start without error
echo '{"jsonrpc": "2.0", "method": "tools/list", "id": 1}' | timeout 5 python3.12 -m antfarm.mcp.server 2>/dev/null || echo "Expected: tool list JSON output"
```

- [ ] **Step 4: Dogfood checklist**

Verify manually or via integration test:

- [ ] MCP server starts and lists 12 tools
- [ ] Config loads from `.antfarm/config.json` when present
- [ ] Env vars override config file values
- [ ] Missing config file falls back to defaults
- [ ] Colony unavailable → clear error ("Cannot reach Antfarm colony...")
- [ ] Auth failure → clear error ("Authentication failed — check your token")
- [ ] Token never appears in tool output, logs, or error messages
- [ ] No hook calls harvest, kickback, or merge endpoints
- [ ] Hooks fail silently when env vars are missing
- [ ] `workspace_observe.sh` is NOT enabled by default in package.json
- [ ] Skills reference correct MCP tool names
- [ ] Agent definitions reference correct MCP tool names
- [ ] `reviewer.md` explicitly references v0.5 ReviewVerdict contract

- [ ] **Step 5: Bump version**

In `pyproject.toml`, update version from `"0.4.0"` to `"0.6.0"` (or appropriate version based on what shipped between 0.4 and now).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml
git commit -m "chore: bump version for v0.6.0 MCP + Claude Code integration"
```

---

## Cross-Cutting Test Plan

### Happy Path

| Test | Module | What it proves |
|------|--------|----------------|
| Config loads from file | test_mcp_config | `.antfarm/config.json` is read correctly |
| Env overrides file | test_mcp_config | `ANTFARM_URL`/`ANTFARM_TOKEN` take precedence |
| Server creates | test_mcp_server | MCP server initializes with config |
| 14 tools listed | test_mcp_server | All expected tools are registered |
| All tools have handlers | test_mcp_server | No orphan tool definitions |
| carry creates task | test_mcp_tools | `antfarm_carry` → `ColonyClient.carry()` |
| forage returns task | test_mcp_tools | `antfarm_forage` → `ColonyClient.forage()` |
| forage empty | test_mcp_tools | Empty queue returns structured "empty" response |
| trail appends | test_mcp_tools | `antfarm_trail` → `ColonyClient.trail()` |
| harvest marks done | test_mcp_tools | `antfarm_harvest` → `ColonyClient.harvest()` |
| status returns full | test_mcp_tools | `antfarm_status` → `ColonyClient.status_full()` |
| blockers filters | test_mcp_tools | Only blocked/signaled tasks returned |
| review pack found | test_mcp_tools | Artifact extracted from task attempt |
| review pack missing | test_mcp_tools | Clear message when no artifact |
| merge ready filters | test_mcp_tools | Only done tasks with current_attempt |
| workers lists | test_mcp_tools | `antfarm_workers` → `ColonyClient.list_workers()` |
| plan_spec unavailable | test_mcp_tools | Graceful fallback when v0.5 planner not shipped |
| memory unavailable | test_mcp_tools | Graceful fallback when v0.5 memory not shipped |

### Failure Modes

| Test | Module | What it proves |
|------|--------|----------------|
| Colony unreachable | test_mcp_errors | `httpx.ConnectError` → clear error message |
| Auth failure (401) | test_mcp_errors | Structured auth error, not stack trace |
| Conflict (409) | test_mcp_errors | Specific conflict message |
| Timeout | test_mcp_errors | `httpx.TimeoutException` → clear error |
| Malformed config | test_mcp_config | `json.JSONDecodeError` raised |
| Missing config | test_mcp_config | Falls back to defaults |
| Token not in repr | test_mcp_config | Token masked in string representation |
| Stale connection retry | test_mcp_errors | Retry once on `ConnectError`/`RemoteProtocolError` before failing |

### Trust Model (Hook Safety)

| Test | Module | What it proves |
|------|--------|----------------|
| Hooks are executable | test_hooks | Permission bits set correctly |
| No hook calls harvest | test_hooks | Hooks never create truth |
| No hook calls kickback | test_hooks | Hooks never trigger state transitions |
| No hook calls merge | test_hooks | Hooks never bypass Soldier |
| Hooks fail silently | test_hooks | `|| true` prevents Claude session breakage |
| Missing env → clean exit | test_hooks | No crash when WORKER_ID or TASK_ID unset |

---

## Guardrails (Enforced Throughout)

- MCP server lives in `antfarm/mcp/`, NOT under `antfarm/plugin/`
- Plugin reads `.antfarm/config.json` — no second config source
- Hooks observe only — never harvest, kickback, merge, or mutate artifacts
- Session stop never auto-harvests — harvest requires explicit `/antfarm-done`
- Reviewer agent consumes v0.5 `ReviewVerdict` — does not invent new merge truth
- Important logic in Python handlers — skills/agents/hooks are thin orchestration
- Tokens never appear in logs, trails, error messages, or tool output
- Auth errors fail atomically — no partial state mutation
