# v0.6.0 Claude Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Antfarm a first-class Claude Code experience with MCP server, slash commands, hooks, and subagents.

**Architecture:** MCP server bridges Claude Code to Antfarm's colony API. Plugin bundles slash commands (skills), hooks, and agent definitions. Antfarm core is unchanged — plugin is a thin UX layer.

**Tech Stack:** Python 3.12, mcp package (stdio transport), Claude Code plugin format, bash (hooks)

**Prerequisite:** v0.5 must be shipped (planner, memory, artifacts, conflict prevention).

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `antfarm/mcp/__init__.py` | Create | Package marker |
| `antfarm/mcp/server.py` | Create | MCP stdio server — reads JSON-RPC, calls colony API |
| `antfarm/mcp/tools.py` | Create | Tool definitions + handler dispatch |
| `antfarm/plugin/package.json` | Create | Plugin manifest for Claude Code |
| `antfarm/plugin/skills/antfarm-plan.md` | Create | /antfarm-plan skill |
| `antfarm/plugin/skills/antfarm-status.md` | Create | /antfarm-status skill |
| `antfarm/plugin/skills/antfarm-blockers.md` | Create | /antfarm-blockers skill |
| `antfarm/plugin/skills/antfarm-review.md` | Create | /antfarm-review skill |
| `antfarm/plugin/skills/antfarm-start.md` | Create | /antfarm-start skill |
| `antfarm/plugin/skills/antfarm-done.md` | Create | /antfarm-done skill |
| `antfarm/plugin/skills/antfarm-merge-ready.md` | Create | /antfarm-merge-ready skill |
| `antfarm/plugin/hooks/heartbeat.sh` | Create | PostToolUse heartbeat (enhanced from v0.4) |
| `antfarm/plugin/hooks/artifact_update.sh` | Create | PostToolUse artifact file count update |
| `antfarm/plugin/hooks/on_complete.sh` | Create | Stop hook — harvest on session end |
| `antfarm/plugin/agents/worker.md` | Create | Worker agent using MCP tools |
| `antfarm/plugin/agents/planner.md` | Create | Planner agent using MCP tools |
| `antfarm/plugin/agents/reviewer.md` | Create | Reviewer agent using MCP tools |
| `tests/test_mcp_server.py` | Create | MCP server unit tests |
| `tests/test_mcp_tools.py` | Create | Tool handler tests |
| `pyproject.toml` | Modify | Add mcp dependency |

---

## Task 1: MCP Server Core

**Files:**
- Create: `antfarm/mcp/__init__.py`
- Create: `antfarm/mcp/server.py`
- Create: `tests/test_mcp_server.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add mcp dependency to pyproject.toml**

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

- [ ] **Step 2: Write failing test for MCP server initialization**

Create `tests/test_mcp_server.py`:

```python
"""Tests for Antfarm MCP server."""

import pytest


def test_mcp_server_creates_app():
    """MCP server can be instantiated with colony URL."""
    from antfarm.mcp.server import create_mcp_server

    server = create_mcp_server(colony_url="http://localhost:7433")
    assert server is not None
    assert hasattr(server, "list_tools")


def test_mcp_server_lists_tools():
    """MCP server exposes all expected tools."""
    from antfarm.mcp.server import create_mcp_server

    server = create_mcp_server(colony_url="http://localhost:7433")
    tools = server.list_tools()
    tool_names = {t.name for t in tools}

    expected = {
        "antfarm_carry",
        "antfarm_list_tasks",
        "antfarm_forage",
        "antfarm_trail",
        "antfarm_harvest",
        "antfarm_status",
        "antfarm_blockers",
        "antfarm_workers",
        "antfarm_merge_ready",
    }
    assert expected.issubset(tool_names), f"Missing tools: {expected - tool_names}"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_mcp_server.py -v`
Expected: FAIL — module not found

- [ ] **Step 4: Create MCP server**

Create `antfarm/mcp/__init__.py`:
```python
"""Antfarm MCP server — bridges Claude Code to the colony API."""
```

Create `antfarm/mcp/server.py`:

```python
"""MCP server for Antfarm.

Exposes colony API operations as MCP tools that Claude Code can call.
Uses stdio transport (standard for Claude Code MCP servers).

Usage:
    python -m antfarm.mcp.server

Environment:
    ANTFARM_URL: Colony server URL (default: http://localhost:7433)
    ANTFARM_TOKEN: Optional bearer token for auth
"""

import json
import os

import httpx
from mcp.server import Server
from mcp.types import Tool, TextContent

_colony_url = os.environ.get("ANTFARM_URL", "http://localhost:7433")
_token = os.environ.get("ANTFARM_TOKEN")


def _headers() -> dict:
    """Build auth headers if token is set."""
    if _token:
        return {"Authorization": f"Bearer {_token}"}
    return {}


def _get(path: str) -> dict | list:
    """GET request to colony API."""
    r = httpx.get(f"{_colony_url}{path}", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def _post(path: str, payload: dict) -> dict | None:
    """POST request to colony API."""
    r = httpx.post(f"{_colony_url}{path}", json=payload, headers=_headers(), timeout=10)
    if r.status_code == 204:
        return None
    r.raise_for_status()
    return r.json()


def _delete(path: str, params: dict | None = None) -> None:
    """DELETE request to colony API."""
    r = httpx.delete(f"{_colony_url}{path}", params=params or {}, headers=_headers(), timeout=10)
    r.raise_for_status()


def create_mcp_server(colony_url: str | None = None, token: str | None = None) -> Server:
    """Create and configure the Antfarm MCP server.

    Args:
        colony_url: Override colony URL (default: ANTFARM_URL env var)
        token: Override bearer token (default: ANTFARM_TOKEN env var)
    """
    global _colony_url, _token
    if colony_url:
        _colony_url = colony_url
    if token:
        _token = token

    server = Server("antfarm")

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name="antfarm_carry",
                description="Create a new task in the colony queue",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Task title"},
                        "spec": {"type": "string", "description": "Task specification"},
                        "depends_on": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Task IDs this depends on",
                            "default": [],
                        },
                        "touches": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Module/scope hints for conflict prevention",
                            "default": [],
                        },
                        "priority": {
                            "type": "integer", "description": "Priority (lower=higher)",
                            "default": 10,
                        },
                    },
                    "required": ["title", "spec"],
                },
            ),
            Tool(
                name="antfarm_list_tasks",
                description="List all tasks, optionally filtered by status",
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
                        "worker_id": {"type": "string", "description": "Worker ID (e.g. node-1/claude-1)"},
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
                        "message": {"type": "string"},
                    },
                    "required": ["task_id", "worker_id", "message"],
                },
            ),
            Tool(
                name="antfarm_harvest",
                description="Mark a task as complete with branch/PR info",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "attempt_id": {"type": "string"},
                        "branch": {"type": "string"},
                        "pr": {"type": "string", "default": ""},
                    },
                    "required": ["task_id", "attempt_id", "branch"],
                },
            ),
            Tool(
                name="antfarm_status",
                description="Get full colony status: nodes, workers, tasks, and their states",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="antfarm_blockers",
                description="List tasks that need attention: blocked, failed, stale, signaled",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="antfarm_workers",
                description="List all registered workers and their rate limit status",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="antfarm_merge_ready",
                description="List tasks that are done and ready for the Soldier to merge",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            result = _handle_tool(name, arguments)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        except httpx.HTTPStatusError as e:
            return [TextContent(
                type="text",
                text=json.dumps({"error": str(e), "status_code": e.response.status_code}),
            )]
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    return server


def _handle_tool(name: str, arguments: dict) -> dict | list | None:
    """Dispatch tool call to colony API."""
    if name == "antfarm_carry":
        import time
        task_id = arguments.get("id", f"task-{int(time.time() * 1000)}")
        payload = {
            "id": task_id,
            "title": arguments["title"],
            "spec": arguments["spec"],
            "depends_on": arguments.get("depends_on", []),
            "touches": arguments.get("touches", []),
            "priority": arguments.get("priority", 10),
            "complexity": "M",
            "created_by": "mcp",
        }
        return _post("/tasks", payload)

    elif name == "antfarm_list_tasks":
        status = arguments.get("status")
        path = f"/tasks?status={status}" if status else "/tasks"
        return _get(path)

    elif name == "antfarm_forage":
        return _post("/tasks/pull", {"worker_id": arguments["worker_id"]})

    elif name == "antfarm_trail":
        return _post(
            f"/tasks/{arguments['task_id']}/trail",
            {"worker_id": arguments["worker_id"], "message": arguments["message"]},
        )

    elif name == "antfarm_harvest":
        return _post(
            f"/tasks/{arguments['task_id']}/harvest",
            {
                "attempt_id": arguments["attempt_id"],
                "branch": arguments["branch"],
                "pr": arguments.get("pr", ""),
            },
        )

    elif name == "antfarm_status":
        return _get("/status/full")

    elif name == "antfarm_blockers":
        all_tasks = _get("/tasks")
        blockers = [
            t for t in all_tasks
            if t.get("status") in ("blocked", "paused")
            or t.get("signals")
        ]
        return blockers

    elif name == "antfarm_workers":
        return _get("/workers")

    elif name == "antfarm_merge_ready":
        done_tasks = _get("/tasks?status=done")
        return [
            t for t in done_tasks
            if t.get("current_attempt") is not None
        ]

    else:
        return {"error": f"Unknown tool: {name}"}


def main():
    """Run the MCP server on stdio."""
    import asyncio
    from mcp.server.stdio import stdio_server

    server = create_mcp_server()

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run tests**

Run: `python3.12 -m pytest tests/test_mcp_server.py -v`
Expected: PASS (both tests)

- [ ] **Step 6: Run full suite**

Run: `python3.12 -m pytest tests/ -x -q --ignore=tests/test_redis_backend.py`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add antfarm/mcp/ tests/test_mcp_server.py pyproject.toml
git commit -m "feat(mcp): add Antfarm MCP server for Claude Code integration #84"
```

---

## Task 2: Slash Commands (Plugin Skills)

**Files:**
- Create: `antfarm/plugin/skills/antfarm-plan.md`
- Create: `antfarm/plugin/skills/antfarm-status.md`
- Create: `antfarm/plugin/skills/antfarm-blockers.md`
- Create: `antfarm/plugin/skills/antfarm-review.md`
- Create: `antfarm/plugin/skills/antfarm-start.md`
- Create: `antfarm/plugin/skills/antfarm-done.md`
- Create: `antfarm/plugin/skills/antfarm-merge-ready.md`

These are markdown skill definitions — no Python code, no tests needed. Each skill instructs Claude Code to use the MCP tools.

- [ ] **Step 1: Create /antfarm-plan skill**

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
2. Call `antfarm_status` to understand current colony state
3. Break the spec into 3-10 focused tasks. For each task determine:
   - **title**: short imperative description
   - **spec**: detailed implementation instructions
   - **depends_on**: task IDs this task must wait for
   - **touches**: module/scope hints (e.g. "api", "frontend", "db", "auth")
   - **priority**: 1-10 (lower = higher priority, default 10)
4. Show the proposed tasks to the user in a table
5. On user confirmation, call `antfarm_carry` for each task
6. Call `antfarm_status` to show the final queue
```

- [ ] **Step 2: Create remaining 6 skills**

Create each as a markdown file following the same pattern. Each skill:
- Has a `---` frontmatter block with name and description
- Has `## Instructions` that reference MCP tool names
- Is concise (10-20 lines)

Skills to create:
- `antfarm-status.md` — call `antfarm_status`, format as readable summary
- `antfarm-blockers.md` — call `antfarm_blockers`, show what needs attention
- `antfarm-review.md` — takes task_id arg, call `antfarm_list_tasks`, find the task, show artifact/review pack
- `antfarm-start.md` — register as worker, call `antfarm_forage`, set up worktree, start working
- `antfarm-done.md` — harvest current task, call `antfarm_forage` for next
- `antfarm-merge-ready.md` — call `antfarm_merge_ready`, show done tasks awaiting merge

- [ ] **Step 3: Commit**

```bash
git add antfarm/plugin/skills/
git commit -m "feat(plugin): add 7 slash command skills for Claude Code #84"
```

---

## Task 3: Hooks

**Files:**
- Create: `antfarm/plugin/hooks/heartbeat.sh`
- Create: `antfarm/plugin/hooks/artifact_update.sh`
- Create: `antfarm/plugin/hooks/on_complete.sh`

- [ ] **Step 1: Create heartbeat hook**

```bash
#!/usr/bin/env bash
# PostToolUse hook: send heartbeat to colony
# Fails silently (|| true) with 1s timeout (-m 1)
curl -s -m 1 "${ANTFARM_URL:-http://localhost:7433}/workers/${WORKER_ID}/heartbeat" \
  -X POST -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ANTFARM_TOKEN}" \
  -d '{}' || true
```

- [ ] **Step 2: Create artifact update hook**

```bash
#!/usr/bin/env bash
# PostToolUse hook: update file count in trail after edits
# Only fires if ANTFARM_TASK_ID is set (worker is on a task)
[ -z "$ANTFARM_TASK_ID" ] && exit 0
FILE_COUNT=$(git diff --name-only HEAD 2>/dev/null | wc -l | tr -d ' ')
[ "$FILE_COUNT" = "0" ] && exit 0
curl -s -m 1 "${ANTFARM_URL:-http://localhost:7433}/tasks/${ANTFARM_TASK_ID}/trail" \
  -X POST -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ANTFARM_TOKEN}" \
  -d "{\"worker_id\": \"${WORKER_ID}\", \"message\": \"[auto] ${FILE_COUNT} files modified\"}" || true
```

- [ ] **Step 3: Create on_complete hook**

```bash
#!/usr/bin/env bash
# Stop hook: harvest task on session end (if task is active)
[ -z "$ANTFARM_TASK_ID" ] || [ -z "$ANTFARM_ATTEMPT_ID" ] && exit 0
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
curl -s -m 5 "${ANTFARM_URL:-http://localhost:7433}/tasks/${ANTFARM_TASK_ID}/harvest" \
  -X POST -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ANTFARM_TOKEN}" \
  -d "{\"attempt_id\": \"${ANTFARM_ATTEMPT_ID}\", \"branch\": \"${BRANCH}\", \"pr\": \"\"}" || true
```

- [ ] **Step 4: Make hooks executable and commit**

```bash
chmod +x antfarm/plugin/hooks/*.sh
git add antfarm/plugin/hooks/
git commit -m "feat(plugin): add PostToolUse and Stop hooks for Claude Code #84"
```

---

## Task 4: Agent Definitions

**Files:**
- Create: `antfarm/plugin/agents/worker.md`
- Create: `antfarm/plugin/agents/planner.md`
- Create: `antfarm/plugin/agents/reviewer.md`

- [ ] **Step 1: Create worker agent**

Enhanced version of existing `adapters/claude_code/agents/worker.md` but using MCP tools:

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
3. Implement the changes in the workspace
4. Run tests and lint
5. Commit and push your branch
6. Call `antfarm_harvest` with task_id, attempt_id, and branch name

## During Work

- Call `antfarm_trail` periodically to log progress
- Heartbeat is handled automatically by the PostToolUse hook
- If you encounter a problem you can't solve, call `antfarm_trail` with the issue description

## Rules

- One commit per logical change
- Run tests before harvesting
- Never push directly to main
```

- [ ] **Step 2: Create planner and reviewer agents similarly**

- [ ] **Step 3: Commit**

```bash
git add antfarm/plugin/agents/
git commit -m "feat(plugin): add worker, planner, reviewer agent definitions #84"
```

---

## Task 5: Plugin Package

**Files:**
- Create: `antfarm/plugin/package.json`

- [ ] **Step 1: Create plugin manifest**

```json
{
  "name": "antfarm",
  "version": "0.6.0",
  "description": "Antfarm — coordinate AI coding agents across machines",
  "skills": [
    "skills/antfarm-plan.md",
    "skills/antfarm-status.md",
    "skills/antfarm-blockers.md",
    "skills/antfarm-review.md",
    "skills/antfarm-start.md",
    "skills/antfarm-done.md",
    "skills/antfarm-merge-ready.md"
  ],
  "agents": [
    "agents/worker.md",
    "agents/planner.md",
    "agents/reviewer.md"
  ],
  "hooks": {
    "PostToolUse": ["hooks/heartbeat.sh"],
    "Stop": ["hooks/on_complete.sh"]
  },
  "mcpServers": {
    "antfarm": {
      "command": "python3",
      "args": ["-m", "antfarm.mcp.server"],
      "env": {
        "ANTFARM_URL": "http://localhost:7433"
      }
    }
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add antfarm/plugin/package.json
git commit -m "feat(plugin): add plugin manifest for Claude Code installation #84"
```

---

## Task 6: Integration Test + Tag

- [ ] **Step 1: Run full test suite**

Run: `python3.12 -m pytest tests/ -x -q --ignore=tests/test_redis_backend.py`
Expected: All pass

- [ ] **Step 2: Test MCP server manually**

```bash
# Start colony
python3.12 -m antfarm colony &

# Test MCP server
echo '{"jsonrpc": "2.0", "method": "tools/list", "id": 1}' | python3.12 -m antfarm.mcp.server
```

- [ ] **Step 3: Test plugin installation**

```bash
claude plugins install ./antfarm/plugin
```

- [ ] **Step 4: Update CHANGELOG, bump version, tag**

```bash
git add CHANGELOG.md pyproject.toml
git commit -m "chore: bump version to 0.6.0"
git tag v0.6.0
git push origin main --tags
```
