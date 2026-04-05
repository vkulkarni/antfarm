"""Colony failover and backup module for Antfarm.

Provides periodic rsync/scp-based backup of the .antfarm/ data directory to a
remote destination, plus restore capability. Designed to run as a daemon thread
alongside the colony server.

Architecture:
- FailoverConfig holds backup destination, interval, and method.
- run_backup() executes a single rsync or scp transfer and writes backup_status.json.
- run_failover_loop() is a blocking loop intended for daemon thread usage.
- restore_from_backup() pulls data from a remote source to recover a colony.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class FailoverConfig:
    """Configuration for colony failover/backup.

    Args:
        backup_dest: rsync/scp destination, e.g. "user@backup-host:/path/to/.antfarm-backup"
        interval_seconds: Seconds between periodic backup runs.
        method: Transfer method — "rsync" or "scp".
    """

    backup_dest: str
    interval_seconds: int = 300
    method: str = "rsync"


@dataclass
class BackupResult:
    """Result of a single backup operation.

    Args:
        success: Whether the backup completed without error.
        timestamp: ISO-8601 timestamp of when the backup ran.
        message: Human-readable status or error message.
        bytes_transferred: Approximate bytes sent (rsync only; 0 for scp).
    """

    success: bool
    timestamp: str
    message: str
    bytes_transferred: int = 0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _write_status(data_dir: str, result: BackupResult) -> None:
    """Persist backup result to data_dir/backup_status.json."""
    status_path = f"{data_dir}/backup_status.json"
    payload = {
        "success": result.success,
        "timestamp": result.timestamp,
        "message": result.message,
        "bytes_transferred": result.bytes_transferred,
    }
    with open(status_path, "w") as f:
        json.dump(payload, f, indent=2)


def run_backup(data_dir: str, config: FailoverConfig) -> BackupResult:
    """Execute a single backup of data_dir to config.backup_dest.

    Uses rsync or scp depending on config.method. Paths are shell-quoted
    to prevent injection. Never uses shell=True.

    Args:
        data_dir: Local .antfarm directory path.
        config: FailoverConfig with destination and method.

    Returns:
        BackupResult with success status, timestamp, message, and bytes_transferred.
    """
    timestamp = _now_iso()

    if config.method == "rsync":
        cmd = ["rsync", "-az", "--delete", "--stats", data_dir.rstrip("/") + "/",
               config.backup_dest.rstrip("/") + "/"]
    elif config.method == "scp":
        cmd = ["scp", "-r", data_dir.rstrip("/") + "/", config.backup_dest.rstrip("/") + "/"]
    else:
        result = BackupResult(
            success=False,
            timestamp=timestamp,
            message=f"Unknown backup method: {config.method!r}",
        )
        _write_status(data_dir, result)
        return result

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        result = BackupResult(
            success=False,
            timestamp=timestamp,
            message=f"Command not found: {exc}",
        )
        _write_status(data_dir, result)
        return result
    except subprocess.TimeoutExpired:
        result = BackupResult(
            success=False,
            timestamp=timestamp,
            message="Backup timed out after 120 seconds",
        )
        _write_status(data_dir, result)
        return result

    if proc.returncode != 0:
        result = BackupResult(
            success=False,
            timestamp=timestamp,
            message=f"Backup failed (exit {proc.returncode}): {proc.stderr.strip()}",
        )
        _write_status(data_dir, result)
        return result

    # Parse bytes transferred from rsync --stats output
    bytes_transferred = 0
    if config.method == "rsync":
        for line in proc.stdout.splitlines():
            if "bytes" in line.lower() and "sent" in line.lower():
                parts = line.split()
                for part in parts:
                    if part.replace(",", "").isdigit():
                        bytes_transferred = int(part.replace(",", ""))
                        break

    result = BackupResult(
        success=True,
        timestamp=timestamp,
        message=f"Backup completed to {config.backup_dest}",
        bytes_transferred=bytes_transferred,
    )
    _write_status(data_dir, result)
    return result


def run_failover_loop(data_dir: str, config: FailoverConfig) -> None:
    """Blocking backup loop — intended to run in a daemon thread.

    Runs run_backup() every config.interval_seconds. Catches all exceptions
    so a single failure does not terminate the loop.

    Args:
        data_dir: Local .antfarm directory path.
        config: FailoverConfig with destination, interval, and method.
    """
    while True:
        with contextlib.suppress(Exception):
            run_backup(data_dir, config)
        time.sleep(config.interval_seconds)


def restore_from_backup(backup_source: str, data_dir: str) -> bool:
    """Restore .antfarm/ data directory from a backup source.

    Warns if the destination already has data (non-empty directory). Uses rsync
    to pull data from backup_source into data_dir.

    Args:
        backup_source: Remote or local path to backup, e.g. "user@host:/path/to/.antfarm-backup".
        data_dir: Local destination directory to restore into.

    Returns:
        True on success, False on failure.
    """
    import os

    if os.path.isdir(data_dir) and os.listdir(data_dir):
        print(
            f"WARNING: {data_dir} is not empty. Restoring will overwrite existing colony data."
        )

    cmd = ["rsync", "-az", "--delete", backup_source.rstrip("/") + "/",
           data_dir.rstrip("/") + "/"]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        print("ERROR: rsync not found. Please install rsync to use restore.")
        return False
    except subprocess.TimeoutExpired:
        print("ERROR: Restore timed out after 120 seconds.")
        return False

    if proc.returncode != 0:
        print(f"ERROR: Restore failed (exit {proc.returncode}): {proc.stderr.strip()}")
        return False

    return True


def start_failover_daemon(data_dir: str, config: FailoverConfig) -> threading.Thread:
    """Start the failover loop as a background daemon thread.

    Args:
        data_dir: Local .antfarm directory path.
        config: FailoverConfig with destination and interval.

    Returns:
        The started daemon Thread.
    """
    t = threading.Thread(
        target=run_failover_loop,
        args=(data_dir, config),
        daemon=True,
        name="antfarm-failover",
    )
    t.start()
    return t
