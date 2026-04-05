"""Tests for antfarm.core.failover — colony backup and restore.

Mocks subprocess.run so no real rsync/scp is invoked. Tests cover:
- run_backup success with rsync and scp
- run_backup failure (non-zero exit, timeout, command not found)
- backup_status.json written on each run
- restore_from_backup success and failure
- Unknown backup method
- run_failover_loop timing (mock sleep)
- GET /backup/status endpoint (serve.py integration)
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from antfarm.core.failover import (
    BackupResult,
    FailoverConfig,
    restore_from_backup,
    run_backup,
    run_failover_loop,
    start_failover_daemon,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / ".antfarm"
    d.mkdir()
    return str(d)


@pytest.fixture
def rsync_config():
    return FailoverConfig(backup_dest="user@host:/backup", interval_seconds=60, method="rsync")


@pytest.fixture
def scp_config():
    return FailoverConfig(backup_dest="user@host:/backup", interval_seconds=60, method="scp")


def _make_proc(returncode=0, stdout="", stderr=""):
    proc = MagicMock(spec=CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


# ---------------------------------------------------------------------------
# run_backup — rsync success
# ---------------------------------------------------------------------------


def test_run_backup_rsync_success(data_dir, rsync_config):
    stdout = (
        "Number of files: 10\n"
        "Number of created files: 2\n"
        "Total file size: 1,024 bytes\n"
        "Total bytes sent: 512\n"
        "Total bytes received: 12\n"
    )
    with patch("subprocess.run", return_value=_make_proc(stdout=stdout)) as mock_run:
        result = run_backup(data_dir, rsync_config)

    assert result.success is True
    assert "user@host:/backup" in result.message
    assert result.timestamp

    # rsync called with correct args, no shell=True
    call_args = mock_run.call_args
    cmd = call_args[0][0]
    assert cmd[0] == "rsync"
    assert "-az" in cmd
    assert "--delete" in cmd
    assert call_args.kwargs.get("shell") is not True


def test_run_backup_rsync_bytes_parsed(data_dir, rsync_config):
    stdout = "Total bytes sent: 2,048\nTotal file size: 4,096 bytes\n"
    with patch("subprocess.run", return_value=_make_proc(stdout=stdout)):
        result = run_backup(data_dir, rsync_config)
    # bytes_transferred parsed from first numeric token on "bytes sent" line
    assert result.bytes_transferred == 2048


# ---------------------------------------------------------------------------
# run_backup — scp success
# ---------------------------------------------------------------------------


def test_run_backup_scp_success(data_dir, scp_config):
    with patch("subprocess.run", return_value=_make_proc()) as mock_run:
        result = run_backup(data_dir, scp_config)

    assert result.success is True
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "scp"
    assert "-r" in cmd
    # scp has no bytes_transferred parsing
    assert result.bytes_transferred == 0


# ---------------------------------------------------------------------------
# run_backup — failure cases
# ---------------------------------------------------------------------------


def test_run_backup_nonzero_exit(data_dir, rsync_config):
    with patch(
        "subprocess.run", return_value=_make_proc(returncode=1, stderr="Connection refused")
    ):
        result = run_backup(data_dir, rsync_config)

    assert result.success is False
    assert "Connection refused" in result.message


def test_run_backup_command_not_found(data_dir, rsync_config):
    with patch("subprocess.run", side_effect=FileNotFoundError("rsync not found")):
        result = run_backup(data_dir, rsync_config)

    assert result.success is False
    assert "not found" in result.message.lower()


def test_run_backup_timeout(data_dir, rsync_config):
    import subprocess

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="rsync", timeout=120)):
        result = run_backup(data_dir, rsync_config)

    assert result.success is False
    assert "timed out" in result.message.lower()


def test_run_backup_unknown_method(data_dir):
    config = FailoverConfig(backup_dest="user@host:/backup", method="ftp")
    result = run_backup(data_dir, config)
    assert result.success is False
    assert "ftp" in result.message.lower()


# ---------------------------------------------------------------------------
# backup_status.json written
# ---------------------------------------------------------------------------


def test_backup_status_json_written_on_success(data_dir, rsync_config):
    with patch("subprocess.run", return_value=_make_proc()):
        run_backup(data_dir, rsync_config)

    status_file = f"{data_dir}/backup_status.json"
    with open(status_file) as f:
        status = json.load(f)

    assert status["success"] is True
    assert "timestamp" in status
    assert "message" in status
    assert "bytes_transferred" in status


def test_backup_status_json_written_on_failure(data_dir, rsync_config):
    with patch("subprocess.run", return_value=_make_proc(returncode=2, stderr="err")):
        run_backup(data_dir, rsync_config)

    status_file = f"{data_dir}/backup_status.json"
    with open(status_file) as f:
        status = json.load(f)

    assert status["success"] is False


# ---------------------------------------------------------------------------
# restore_from_backup
# ---------------------------------------------------------------------------


def test_restore_success(data_dir):
    with patch("subprocess.run", return_value=_make_proc()) as mock_run:
        ok = restore_from_backup("user@host:/backup", data_dir)

    assert ok is True
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "rsync"
    assert "--delete" in cmd


def test_restore_failure_nonzero(data_dir):
    with patch("subprocess.run", return_value=_make_proc(returncode=1, stderr="no route")):
        ok = restore_from_backup("user@host:/backup", data_dir)
    assert ok is False


def test_restore_rsync_not_found(data_dir):
    with patch("subprocess.run", side_effect=FileNotFoundError("rsync")):
        ok = restore_from_backup("user@host:/backup", data_dir)
    assert ok is False


def test_restore_timeout(data_dir):
    import subprocess

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="rsync", timeout=120)):
        ok = restore_from_backup("user@host:/backup", data_dir)
    assert ok is False


def test_restore_warns_when_dir_not_empty(data_dir, capsys):
    # Put a file in data_dir so it's non-empty
    existing = __import__("pathlib").Path(data_dir) / "existing.json"
    existing.write_text("{}")
    with patch("subprocess.run", return_value=_make_proc()):
        restore_from_backup("user@host:/backup", data_dir)
    out = capsys.readouterr().out
    assert "WARNING" in out or "not empty" in out.lower()


# ---------------------------------------------------------------------------
# run_failover_loop timing
# ---------------------------------------------------------------------------


def test_failover_loop_calls_backup_periodically(data_dir, rsync_config):
    call_count = 0
    sleep_calls = []

    def fake_backup(d, c):
        nonlocal call_count
        call_count += 1
        return BackupResult(success=True, timestamp="ts", message="ok")

    def fake_sleep(n):
        sleep_calls.append(n)
        if len(sleep_calls) >= 3:
            raise KeyboardInterrupt

    with (
        patch("antfarm.core.failover.run_backup", side_effect=fake_backup),
        patch("antfarm.core.failover.time.sleep", side_effect=fake_sleep),
        contextlib.suppress(KeyboardInterrupt),
    ):
        run_failover_loop(data_dir, rsync_config)

    assert call_count >= 3
    assert all(s == rsync_config.interval_seconds for s in sleep_calls)


def test_failover_loop_continues_after_exception(data_dir, rsync_config):
    """Loop should not die on a backup exception."""
    call_count = 0
    sleep_count = [0]

    def fake_backup(d, c):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("connection reset")

    def fake_sleep(n):
        sleep_count[0] += 1
        if sleep_count[0] >= 3:
            raise KeyboardInterrupt

    with (
        patch("antfarm.core.failover.run_backup", side_effect=fake_backup),
        patch("antfarm.core.failover.time.sleep", side_effect=fake_sleep),
        contextlib.suppress(KeyboardInterrupt),
    ):
        run_failover_loop(data_dir, rsync_config)

    assert call_count >= 3


# ---------------------------------------------------------------------------
# start_failover_daemon
# ---------------------------------------------------------------------------


def test_start_failover_daemon_returns_daemon_thread(data_dir, rsync_config):
    def fake_loop(d, c):
        time.sleep(10)

    with patch("antfarm.core.failover.run_failover_loop", side_effect=fake_loop):
        t = start_failover_daemon(data_dir, rsync_config)

    assert isinstance(t, threading.Thread)
    assert t.daemon is True
    assert t.is_alive()


# ---------------------------------------------------------------------------
# GET /backup/status — serve.py integration
# ---------------------------------------------------------------------------


@pytest.fixture
def serve_client(tmp_path):
    from antfarm.core.backends.file import FileBackend
    from antfarm.core.serve import get_app

    data_dir = str(tmp_path / ".antfarm")
    __import__("os").makedirs(data_dir, exist_ok=True)
    backend = FileBackend(root=data_dir)
    app = get_app(backend=backend, data_dir=data_dir)
    return TestClient(app), data_dir


def test_backup_status_endpoint_404_when_no_file(serve_client):
    client, _ = serve_client
    r = client.get("/backup/status")
    assert r.status_code == 404


def test_backup_status_endpoint_returns_json(serve_client):
    client, data_dir = serve_client

    payload = {
        "success": True,
        "timestamp": "2026-04-04T00:00:00+00:00",
        "message": "Backup completed",
        "bytes_transferred": 1024,
    }
    with open(f"{data_dir}/backup_status.json", "w") as f:
        json.dump(payload, f)

    r = client.get("/backup/status")
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["bytes_transferred"] == 1024
    assert data["timestamp"] == "2026-04-04T00:00:00+00:00"


def test_backup_status_endpoint_failed_backup(serve_client):
    client, data_dir = serve_client

    payload = {
        "success": False,
        "timestamp": "2026-04-04T01:00:00+00:00",
        "message": "Backup failed: connection refused",
        "bytes_transferred": 0,
    }
    with open(f"{data_dir}/backup_status.json", "w") as f:
        json.dump(payload, f)

    r = client.get("/backup/status")
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is False
