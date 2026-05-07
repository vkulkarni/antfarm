"""Tests for antfarm.core.hook_setup.

Covers per-worktree Claude Code Stop hook registration:
- fresh write when no settings.json exists
- deep merge that preserves existing settings
- idempotent re-registration
"""

from __future__ import annotations

import json

from antfarm.core.hook_setup import register_stop_hook, stop_hook_path


def test_register_stop_hook_writes_fresh_settings(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    hook = tmp_path / "stop.sh"
    hook.write_text("#!/bin/sh\nexit 0\n")

    register_stop_hook(workspace, hook)

    settings_path = workspace / ".claude" / "settings.json"
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    stop = data["hooks"]["Stop"]
    assert isinstance(stop, list)
    assert len(stop) == 1
    assert stop[0]["hooks"][0]["type"] == "command"
    assert stop[0]["hooks"][0]["command"] == str(hook)


def test_register_stop_hook_merges_existing_settings(tmp_path):
    workspace = tmp_path / "ws"
    settings_dir = workspace / ".claude"
    settings_dir.mkdir(parents=True)
    settings_path = settings_dir / "settings.json"

    existing = {
        "permissions": {"allow": ["Bash(npm test)"]},
        "hooks": {
            "PostToolUse": [
                {"hooks": [{"type": "command", "command": "/some/other/hook.sh"}]}
            ]
        },
        "env": {"MY_VAR": "1"},
    }
    settings_path.write_text(json.dumps(existing, indent=2))

    hook = tmp_path / "stop.sh"
    hook.write_text("#!/bin/sh\n")

    register_stop_hook(workspace, hook)

    data = json.loads(settings_path.read_text())
    # Pre-existing keys preserved
    assert data["permissions"] == {"allow": ["Bash(npm test)"]}
    assert data["env"] == {"MY_VAR": "1"}
    # Pre-existing hook category preserved
    assert data["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "/some/other/hook.sh"
    # New Stop hook entry appended
    stop = data["hooks"]["Stop"]
    assert len(stop) == 1
    assert stop[0]["hooks"][0]["command"] == str(hook)


def test_register_stop_hook_idempotent(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    hook = tmp_path / "stop.sh"
    hook.write_text("#!/bin/sh\n")

    register_stop_hook(workspace, hook)
    register_stop_hook(workspace, hook)
    register_stop_hook(workspace, hook)

    data = json.loads((workspace / ".claude" / "settings.json").read_text())
    stop = data["hooks"]["Stop"]
    assert len(stop) == 1
    assert stop[0]["hooks"][0]["command"] == str(hook)


def test_register_stop_hook_preserves_other_stop_entries(tmp_path):
    workspace = tmp_path / "ws"
    settings_dir = workspace / ".claude"
    settings_dir.mkdir(parents=True)
    settings_path = settings_dir / "settings.json"

    existing = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "/user/global/stop.sh"}]}
            ]
        }
    }
    settings_path.write_text(json.dumps(existing))

    hook = tmp_path / "stop.sh"
    hook.write_text("#!/bin/sh\n")
    register_stop_hook(workspace, hook)

    data = json.loads(settings_path.read_text())
    stop = data["hooks"]["Stop"]
    assert len(stop) == 2
    commands = [m["hooks"][0]["command"] for m in stop]
    assert "/user/global/stop.sh" in commands
    assert str(hook) in commands


def test_register_stop_hook_creates_claude_dir(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert not (workspace / ".claude").exists()

    hook = tmp_path / "stop.sh"
    hook.write_text("#!/bin/sh\n")
    register_stop_hook(workspace, hook)

    assert (workspace / ".claude").is_dir()
    assert (workspace / ".claude" / "settings.json").is_file()


def test_register_stop_hook_handles_corrupt_settings(tmp_path):
    workspace = tmp_path / "ws"
    settings_dir = workspace / ".claude"
    settings_dir.mkdir(parents=True)
    settings_path = settings_dir / "settings.json"
    settings_path.write_text("{not valid json")

    hook = tmp_path / "stop.sh"
    hook.write_text("#!/bin/sh\n")

    # Should not raise, should not clobber the corrupt file.
    register_stop_hook(workspace, hook)

    # File contents preserved (not overwritten by us).
    assert settings_path.read_text() == "{not valid json"


def test_stop_hook_path_points_to_bundled_script():
    path = stop_hook_path()
    assert path.is_absolute()
    assert path.name == "stop.sh"
    assert path.parent.name == "hooks"
    assert path.parent.parent.name == "claude_code"
    # The bundled script should actually exist in the source tree.
    assert path.exists()
