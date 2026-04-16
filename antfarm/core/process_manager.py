"""Process lifecycle management for Antfarm worker spawning.

Provides a ProcessManager ABC with two implementations:
- TmuxProcessManager: real TTY via tmux sessions (preferred)
- SubprocessProcessManager: Popen fallback when tmux unavailable

Use get_process_manager() to get the right implementation.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Session name format: {prefix}{role}-{counter}
# e.g., "auto-builder-3" or "runner-planner-1"
# Shared constant so adoption parsing stays in sync with naming.
SESSION_NAME_SEP = "-"


def parse_session_name(name: str, prefix: str) -> tuple[str, int] | None:
    """Parse '{prefix}{role}-{N}' into (role, counter). Returns None if no match.

    Examples:
        parse_session_name("auto-builder-3", "auto-") -> ("builder", 3)
        parse_session_name("auto-code-reviewer-5", "auto-") -> ("code-reviewer", 5)
        parse_session_name("runner-builder-1", "auto-") -> None  (wrong prefix)
    """
    if not name.startswith(prefix):
        return None
    rest = name[len(prefix) :]
    last_dash = rest.rfind(SESSION_NAME_SEP)
    if last_dash == -1:
        return None
    try:
        counter = int(rest[last_dash + 1 :])
    except ValueError:
        return None
    role = rest[:last_dash]
    if not role:
        return None
    return (role, counter)


@dataclass
class ProcessMetadata:
    """Persistent metadata about a managed worker process.

    Replaces raw PID files. Stored as JSON at {state_dir}/processes/{name}.json.
    Supports both tmux and subprocess backends — adoption reads manager_type
    to know how to validate liveness.
    """

    name: str
    role: str
    manager_type: str  # "tmux" | "subprocess"
    pid: int | None = None  # subprocess only
    session_name: str | None = None  # tmux only
    started_at: str | None = None

    def to_dict(self) -> dict:
        return {
            k: v
            for k, v in {
                "name": self.name,
                "role": self.role,
                "manager_type": self.manager_type,
                "pid": self.pid,
                "session_name": self.session_name,
                "started_at": self.started_at,
            }.items()
            if v is not None
        }

    @classmethod
    def from_dict(cls, data: dict) -> ProcessMetadata:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


class ProcessManager(ABC):
    """Interface for worker process lifecycle management.

    Each implementation persists ProcessMetadata files for correct validation
    and cleanup on restart. Reliable restart adoption (discovering and reattaching
    to workers from a previous run) is only supported by TmuxProcessManager.
    SubprocessProcessManager explicitly overrides adopt_existing() to return
    {} — Popen handles are lost on restart and PID-based liveness checks
    are unsafe. See SubprocessProcessManager for rationale.
    """

    def __init__(self, prefix: str = "auto-", state_dir: str | None = None):
        self.prefix = prefix
        self.state_dir = state_dir  # where to write metadata files

    def _metadata_path(self, name: str) -> str | None:
        if not self.state_dir:
            return None
        return os.path.join(self.state_dir, "processes", f"{name}.json")

    def _write_metadata(self, meta: ProcessMetadata) -> None:
        path = self._metadata_path(meta.name)
        if not path:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(meta.to_dict(), f, indent=2)

    def _read_metadata(self, name: str) -> ProcessMetadata | None:
        path = self._metadata_path(name)
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                return ProcessMetadata.from_dict(json.load(f))
        except (json.JSONDecodeError, OSError, KeyError):
            return None

    def _remove_metadata(self, name: str) -> None:
        path = self._metadata_path(name)
        if path and os.path.exists(path):
            os.unlink(path)

    def _list_metadata(self) -> list[ProcessMetadata]:
        if not self.state_dir:
            return []
        meta_dir = os.path.join(self.state_dir, "processes")
        if not os.path.isdir(meta_dir):
            return []
        results = []
        for fname in os.listdir(meta_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(meta_dir, fname)) as f:
                    results.append(ProcessMetadata.from_dict(json.load(f)))
            except (json.JSONDecodeError, OSError, KeyError):
                continue
        return results

    @abstractmethod
    def start(self, name: str, cmd: list[str], log_path: str | None = None, role: str = "") -> bool:
        """Start a worker process. Writes ProcessMetadata. Returns True on success."""
        ...

    @abstractmethod
    def is_alive(self, name: str) -> bool:
        """Check if a worker process is still running."""
        ...

    @abstractmethod
    def stop(self, name: str) -> bool:
        """Stop a worker process. Returns True if stopped."""
        ...

    @abstractmethod
    def list_managed(self) -> list[str]:
        """List names of all managed processes matching this manager's prefix."""
        ...

    def adopt_existing(self) -> dict[str, str]:
        """Discover and return existing processes from a previous run.

        Uses TWO sources for discovery:
        1. Metadata files in {state_dir}/processes/ — filtered to THIS
           manager's type to prevent cross-manager contamination (e.g.,
           a tmux manager must not adopt leftover subprocess PIDs —
           PID reuse would cause false adoption).
        2. Live session scan (tmux only) — catches sessions without metadata.

        For each candidate, validates liveness:
        - tmux metadata -> tmux has-session
        - subprocess metadata -> os.kill(pid, 0) (only consulted by
          SubprocessProcessManager, which overrides this method to
          return {} anyway — see that class for rationale)

        Returns: {name: role} for each adopted process.
        """
        adopted: dict[str, str] = {}
        seen: set[str] = set()
        my_type = self._manager_type()

        # 1. Metadata file adoption — FILTERED BY MANAGER TYPE.
        #    A tmux manager must not trust subprocess metadata, and vice versa.
        for meta in self._list_metadata():
            if not meta.name.startswith(self.prefix):
                continue
            if meta.manager_type != my_type:
                continue  # foreign metadata — ignore (subclass may sweep it)
            seen.add(meta.name)
            if self._validate_from_metadata(meta):
                adopted[meta.name] = meta.role
            else:
                self._remove_metadata(meta.name)  # stale

        # 2. Live session scan (tmux only — catches sessions without metadata)
        for name in self.list_managed():
            if name in seen:
                continue
            if not self.is_alive(name):
                self.cleanup(name)
                continue
            parsed = parse_session_name(name, self.prefix)
            if parsed:
                adopted[name] = parsed[0]
                # Write metadata retroactively so next adoption finds it
                self._write_metadata(
                    ProcessMetadata(
                        name=name,
                        role=parsed[0],
                        manager_type=my_type,
                        session_name=name if my_type == "tmux" else None,
                    )
                )
        return adopted

    def _validate_from_metadata(self, meta: ProcessMetadata) -> bool:
        """Check if a process described by metadata is still alive."""
        if meta.manager_type == "tmux":
            return self.is_alive(meta.session_name or meta.name)
        elif meta.manager_type == "subprocess" and meta.pid:
            try:
                os.kill(meta.pid, 0)
                return True
            except OSError:
                return False
        return False

    @abstractmethod
    def _manager_type(self) -> str:
        """Return 'tmux' or 'subprocess'."""
        ...

    def max_counter(self) -> int:
        """Return the highest counter value among managed processes.

        Checks both live sessions and metadata files.
        """
        max_n = 0
        all_names = set(self.list_managed())
        for meta in self._list_metadata():
            all_names.add(meta.name)
        for name in all_names:
            parsed = parse_session_name(name, self.prefix)
            if parsed:
                max_n = max(max_n, parsed[1])
        return max_n

    def cleanup(self, name: str) -> None:
        """Clean up a dead process (remove metadata files)."""
        self._remove_metadata(name)


class TmuxProcessManager(ProcessManager):
    """Spawns workers as tmux sessions — real TTY, attach/debug, survive restarts."""

    def __init__(self, prefix: str = "auto-", state_dir: str | None = None):
        super().__init__(prefix, state_dir)

    def _manager_type(self) -> str:
        return "tmux"

    def start(self, name: str, cmd: list[str], log_path: str | None = None, role: str = "") -> bool:
        tmux = shutil.which("tmux")
        if not tmux:
            return False

        # Build inner command with proper shell quoting (prevents injection).
        # Wrap in tee so logging is atomic with command start — no race.
        quoted_cmd = shlex.join(cmd)
        inner = f"{quoted_cmd} 2>&1 | tee -a {shlex.quote(log_path)}" if log_path else quoted_cmd

        tmux_cmd = [tmux, "new-session", "-d", "-s", name, "sh", "-c", inner]
        result = subprocess.run(tmux_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning("tmux start failed for %s: %s", name, result.stderr.strip())
            return False

        # Persist metadata for adoption on restart
        self._write_metadata(
            ProcessMetadata(
                name=name,
                role=role,
                manager_type="tmux",
                session_name=name,
                started_at=datetime.now(UTC).isoformat(),
            )
        )

        logger.info("started tmux session: %s", name)
        return True

    def is_alive(self, name: str) -> bool:
        tmux = shutil.which("tmux")
        if not tmux:
            return False
        return (
            subprocess.run([tmux, "has-session", "-t", name], capture_output=True).returncode == 0
        )

    def stop(self, name: str) -> bool:
        tmux = shutil.which("tmux")
        if not tmux:
            return False
        return (
            subprocess.run([tmux, "kill-session", "-t", name], capture_output=True).returncode == 0
        )

    def list_managed(self) -> list[str]:
        tmux = shutil.which("tmux")
        if not tmux:
            return []
        result = subprocess.run(
            [tmux, "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        return [s for s in result.stdout.strip().split("\n") if s and s.startswith(self.prefix)]


class SubprocessProcessManager(ProcessManager):
    """Spawns workers via subprocess.Popen — fallback when tmux unavailable.

    IMPORTANT: This is a degraded fallback. It does NOT support restart
    adoption — the in-memory _processes dict is lost on restart, Popen
    handles are gone, and PID-based liveness is unsafe (PID reuse).
    Workers die with the colony process. Use TmuxProcessManager for
    production.
    """

    def __init__(self, prefix: str = "auto-", state_dir: str | None = None):
        super().__init__(prefix, state_dir)
        self._processes: dict[str, subprocess.Popen] = {}
        self._log_files: dict[str, object] = {}

    def _manager_type(self) -> str:
        return "subprocess"

    def start(self, name: str, cmd: list[str], log_path: str | None = None, role: str = "") -> bool:
        if log_path:
            log_file = open(log_path, "a")  # noqa: SIM115
            self._log_files[name] = log_file
        else:
            log_file = subprocess.DEVNULL
        process = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
        self._processes[name] = process

        # Persist metadata for liveness queries within this process lifetime
        # and for doctor/debug tooling. NOT used for restart adoption — see
        # adopt_existing() override below.
        self._write_metadata(
            ProcessMetadata(
                name=name,
                role=role,
                manager_type="subprocess",
                pid=process.pid,
                started_at=datetime.now(UTC).isoformat(),
            )
        )

        logger.info("started subprocess: %s pid=%d", name, process.pid)
        return True

    def adopt_existing(self) -> dict[str, str]:
        """Subprocess backend does NOT support restart adoption.

        Rationale:
        - Popen handles are lost on colony restart — stop()/is_alive()
          would not work for "adopted" processes.
        - PID-based liveness checks are unsafe: the OS may have reused
          the PID for an unrelated process, leading to false adoption.

        Behavior: scan subprocess metadata files left by prior runs and
        remove them (best-effort cleanup), then return empty. Callers
        that need restart recovery must use TmuxProcessManager.
        """
        removed = 0
        for meta in self._list_metadata():
            if not meta.name.startswith(self.prefix):
                continue
            if meta.manager_type != "subprocess":
                continue
            self._remove_metadata(meta.name)
            removed += 1
        if removed:
            logger.info(
                "subprocess manager: discarded %d stale metadata file(s) "
                "— restart adoption not supported for subprocess backend",
                removed,
            )
        return {}

    def is_alive(self, name: str) -> bool:
        proc = self._processes.get(name)
        return proc is not None and proc.poll() is None

    def stop(self, name: str) -> bool:
        proc = self._processes.get(name)
        if proc and proc.poll() is None:
            proc.terminate()
            return True
        return False

    def list_managed(self) -> list[str]:
        return [name for name, proc in self._processes.items() if name.startswith(self.prefix)]

    def cleanup(self, name: str) -> None:
        self._processes.pop(name, None)
        lf = self._log_files.pop(name, None)
        if lf and hasattr(lf, "close"):
            lf.close()
        super().cleanup(name)  # remove metadata file


def get_process_manager(prefix: str = "auto-", state_dir: str | None = None) -> ProcessManager:
    """Factory: return TmuxProcessManager if tmux available, else subprocess fallback.

    Args:
        prefix: Session name prefix for this consumer (e.g. "auto-" or "runner-").
        state_dir: Directory for process metadata files. Both callers MUST pass this
                   for metadata-based adoption to work.

    Respects ANTFARM_NO_TMUX=1 environment variable to force fallback.
    """
    if os.environ.get("ANTFARM_NO_TMUX"):
        logger.info("ANTFARM_NO_TMUX set — using subprocess process manager")
        return SubprocessProcessManager(prefix, state_dir)
    if shutil.which("tmux"):
        return TmuxProcessManager(prefix, state_dir)
    logger.warning("tmux not installed — falling back to subprocess process manager")
    return SubprocessProcessManager(prefix, state_dir)
