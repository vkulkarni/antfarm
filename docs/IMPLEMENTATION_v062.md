# Antfarm v0.6.2 — Implementation Plan

**Status:** DRAFT — awaiting approval
**Derived from:** Dogfood findings during v0.6.1 deployment
**Prerequisite:** v0.6.1 shipped on `main` as of `v0.6.1` tag. 809 tests passing.
**Scope:** tmux-based worker spawning via ProcessManager abstraction. One issue: #202.
**Goal:** Workers spawned by autoscaler and runner reliably get a real TTY, eliminating silent subprocess failures.

---

## Problem

During v0.6.1 dogfooding, `claude -p` subprocesses spawned by the autoscaler via `subprocess.Popen` exit silently with empty stdout/stderr. The same command works perfectly when run in a foreground terminal. Root cause: `claude -p` needs a real TTY — piped stdout/stderr changes its behavior.

## Solution

Extract process lifecycle management into a **ProcessManager** abstraction with two implementations: `TmuxProcessManager` (preferred) and `SubprocessProcessManager` (fallback). Autoscaler and runner delegate all spawning, lifecycle, and adoption to the ProcessManager. No tmux/Popen branching in consumers.

---

## Architecture

```
Autoscaler                    Runner
    │                            │
    └──── ProcessManager ────────┘
              │
    ┌─────────┴──────────┐
    │                    │
TmuxProcessManager  SubprocessProcessManager
(real TTY, attach,   (existing Popen behavior,
 survive restarts,    fallback only — NO restart
 full adoption)       adoption)
```

**Key principle:** Autoscaler and runner never know which implementation they're using. They call `self._pm.start(name, cmd, log_path)` and that's it.

**Asymmetry between implementations (be honest, not symmetric):**

| Capability | TmuxProcessManager | SubprocessProcessManager |
|---|---|---|
| Real TTY | Yes | No |
| Restart adoption | Yes — discovers tmux sessions | **No** — in-memory dict lost on restart |
| Debug attach | `tmux attach -t name` | Not possible |
| Process survival | Workers outlive colony | Workers die with colony |
| Metadata persistence | Metadata files + tmux sessions | Metadata files (within-process only; discarded on restart) |

SubprocessProcessManager is explicitly a **degraded fallback** that preserves current v0.6.1 behavior. It does not provide restart adoption. This is documented, not hidden.

---

## What changes

### 1. New file: `antfarm/core/process_manager.py`

The core abstraction. Contains the ABC, both implementations, and the factory.

```python
"""Process lifecycle management for Antfarm worker spawning.

Provides a ProcessManager ABC with two implementations:
- TmuxProcessManager: real TTY via tmux sessions (preferred)
- SubprocessProcessManager: Popen fallback when tmux unavailable

Use get_process_manager() to get the right implementation.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Session name format: {prefix}{role}-{counter}
# e.g., "auto-builder-3" or "runner-planner-1"
# Shared constant so adoption parsing stays in sync with naming.
SESSION_NAME_SEP = "-"


def parse_session_name(name: str) -> tuple[str, str, int] | None:
    """Parse '{prefix}{role}-{N}' into (prefix_with_trailing_dash, role, counter).

    Returns None if the name doesn't match the expected format.

    Examples:
        parse_session_name("auto-builder-3") → ("auto-", "builder", 3)
        parse_session_name("runner-planner-1") → ("runner-", "planner", 1)
    """
    # Find the last dash — that separates counter
    last_dash = name.rfind(SESSION_NAME_SEP)
    if last_dash == -1:
        return None
    try:
        counter = int(name[last_dash + 1:])
    except ValueError:
        return None
    # Find the second-to-last dash — that separates prefix from role
    prefix_and_role = name[:last_dash]
    second_dash = prefix_and_role.rfind(SESSION_NAME_SEP)
    if second_dash == -1:
        return None
    prefix = name[:second_dash + 1]  # includes trailing dash
    role = prefix_and_role[second_dash + 1:]
    return (prefix, role, counter)


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
        return {k: v for k, v in {
            "name": self.name, "role": self.role,
            "manager_type": self.manager_type,
            "pid": self.pid, "session_name": self.session_name,
            "started_at": self.started_at,
        }.items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> ProcessMetadata:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


class ProcessManager(ABC):
    """Interface for worker process lifecycle management.

    Each implementation persists ProcessMetadata files for correct validation
    and cleanup on restart. Reliable restart adoption (discovering and reattaching
    to workers from a previous run) is only supported by TmuxProcessManager.
    SubprocessProcessManager explicitly overrides adopt_existing() to return
    {} — Popen handles are lost on restart and PID reuse makes liveness
    checks unsafe. See SubprocessProcessManager for rationale.
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
        import json
        with open(path, "w") as f:
            json.dump(meta.to_dict(), f, indent=2)

    def _read_metadata(self, name: str) -> ProcessMetadata | None:
        path = self._metadata_path(name)
        if not path or not os.path.exists(path):
            return None
        import json
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
        import json
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
    def start(self, name: str, cmd: list[str], log_path: str | None = None,
              role: str = "") -> bool:
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
        1. Metadata files in {state_dir}/processes/ — portable across backends
        2. Live session scan (tmux only) — catches sessions without metadata

        For each candidate, validates liveness:
        - tmux metadata → tmux has-session
        - subprocess metadata → os.kill(pid, 0)

        Returns: {name: role} for each adopted process.
        """
        adopted = {}
        seen = set()

        # 1. Metadata file adoption (works for both backends)
        for meta in self._list_metadata():
            if not meta.name.startswith(self.prefix):
                continue
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
            parsed = parse_session_name(name)
            if parsed and parsed[0] == self.prefix:
                adopted[name] = parsed[1]
                # Write metadata retroactively so next adoption finds it
                self._write_metadata(ProcessMetadata(
                    name=name, role=parsed[1], manager_type=self._manager_type(),
                    session_name=name if self._manager_type() == "tmux" else None,
                ))
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
            parsed = parse_session_name(name)
            if parsed:
                max_n = max(max_n, parsed[2])
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

    def start(self, name: str, cmd: list[str], log_path: str | None = None,
              role: str = "") -> bool:
        tmux = shutil.which("tmux")
        if not tmux:
            return False

        # Build inner command with proper shell quoting (prevents injection).
        # Wrap in tee so logging is atomic with command start — no race.
        quoted_cmd = shlex.join(cmd)
        if log_path:
            inner = f"{quoted_cmd} 2>&1 | tee -a {shlex.quote(log_path)}"
        else:
            inner = quoted_cmd

        tmux_cmd = [tmux, "new-session", "-d", "-s", name, "sh", "-c", inner]
        result = subprocess.run(tmux_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning("tmux start failed for %s: %s", name, result.stderr.strip())
            return False

        # Persist metadata for adoption on restart
        from datetime import UTC, datetime
        self._write_metadata(ProcessMetadata(
            name=name, role=role, manager_type="tmux",
            session_name=name, started_at=datetime.now(UTC).isoformat(),
        ))

        logger.info("started tmux session: %s", name)
        return True

    def is_alive(self, name: str) -> bool:
        tmux = shutil.which("tmux")
        if not tmux:
            return False
        return subprocess.run(
            [tmux, "has-session", "-t", name], capture_output=True
        ).returncode == 0

    def stop(self, name: str) -> bool:
        tmux = shutil.which("tmux")
        if not tmux:
            return False
        return subprocess.run(
            [tmux, "kill-session", "-t", name], capture_output=True
        ).returncode == 0

    def list_managed(self) -> list[str]:
        tmux = shutil.which("tmux")
        if not tmux:
            return []
        result = subprocess.run(
            [tmux, "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return []
        return [
            s for s in result.stdout.strip().split("\n")
            if s and s.startswith(self.prefix)
        ]


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

    def start(self, name: str, cmd: list[str], log_path: str | None = None,
              role: str = "") -> bool:
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
        from datetime import UTC, datetime
        self._write_metadata(ProcessMetadata(
            name=name, role=role, manager_type="subprocess",
            pid=process.pid, started_at=datetime.now(UTC).isoformat(),
        ))

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
        return [
            name for name, proc in self._processes.items()
            if name.startswith(self.prefix)
        ]

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
```

### 2. Modify: `antfarm/core/autoscaler.py`

Replace all process spawning with ProcessManager. The `ManagedWorker` dataclass simplifies:

```python
@dataclass
class ManagedWorker:
    name: str
    role: str
    worker_id: str
```

No more `process: Popen | None` or `tmux_session: str | None` — the ProcessManager owns lifecycle.

**Changes to Autoscaler:**

```python
class Autoscaler:
    def __init__(self, backend, config, clock=time.time):
        ...
        self._pm = get_process_manager(prefix="auto-", state_dir=config.data_dir)

    def run(self) -> None:
        self._adopt_existing()
        while not self._stopped:
            ...

    def _adopt_existing(self) -> None:
        """Adopt workers from a previous colony run."""
        adopted = self._pm.adopt_existing()
        max_n = self._pm.max_counter()
        for name, role in adopted.items():
            worker_id = f"{self.config.node_id}/{name}"
            self.managed[name] = ManagedWorker(name=name, role=role, worker_id=worker_id)
            logger.info("adopted worker %s (role=%s)", name, role)
        if max_n > 0:
            self._counter = max_n

    def _start_worker(self, role: str) -> None:
        # Retry once on name collision (stale session, counter lag, etc.)
        for attempt in range(2):
            self._counter += 1
            name = f"auto-{role}-{self._counter}"
            worker_id = f"{self.config.node_id}/{name}"
            cmd = [...]  # same command list as today
            log_path = os.path.join(self.config.data_dir, "logs", f"autoscaler-{name}.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)

            if self._pm.start(name, cmd, log_path, role=role):
                self.managed[name] = ManagedWorker(name=name, role=role, worker_id=worker_id)
                return
            if attempt == 0:
                logger.info("start failed for %s, retrying with bumped counter", name)
        logger.warning("failed to start worker after retry — skipping")

    def _stop_idle_worker(self, role: str) -> bool:
        ...
        if cw and cw.get("status") == "idle":
            self._pm.stop(mw.name)
            del self.managed[name]
            return True
        ...

    def _cleanup_exited(self) -> None:
        for name in list(self.managed):
            if not self._pm.is_alive(name):
                self._pm.cleanup(name)
                del self.managed[name]

    def _count_actual(self) -> dict[str, int]:
        counts = {}
        for mw in self.managed.values():
            if self._pm.is_alive(mw.name):
                counts[mw.role] = counts.get(mw.role, 0) + 1
        return counts

    def stop(self) -> None:
        """Shutdown: stop all managed workers.

        Metadata cleanup policy: stop() kills sessions/processes but does NOT
        remove metadata files. Metadata is cleaned up during the next adoption
        pass (stale metadata with dead processes gets removed). This is
        intentional — metadata should outlive the colony process for tmux
        restart adoption to work. Same policy applies in runner.stop().
        """
        self._stopped = True
        for mw in list(self.managed.values()):
            self._pm.stop(mw.name)
```

**No if/else branching anywhere.** The ProcessManager handles it.

### 3. Modify: `antfarm/core/runner.py`

Runner's `ManagedWorker` simplifies — lifecycle delegated to ProcessManager:

```python
@dataclass
class ManagedWorker:
    name: str
    role: str
    pid: int = 0  # informational only — ProcessManager owns lifecycle
```

Runner uses `get_process_manager(prefix="runner-", state_dir=self.state_dir)`. ProcessMetadata files replace raw PID files. The old `_write_pid_file` / `_read_pid_file` / `_adopt_existing_workers` PID-file logic is replaced by ProcessManager's metadata-based adoption.

```python
class Runner:
    def __init__(self, ...):
        ...
        self._pm = get_process_manager(prefix="runner-", state_dir=self.state_dir)

    def _start_worker(self, role: str) -> None:
        # Same retry-on-collision pattern as autoscaler
        for attempt in range(2):
            self._counter += 1
            name = f"runner-{role}-{self._counter}"
            cmd = [...]
            log_path = os.path.join(self.state_dir, "logs", f"{name}.log")
            if self._pm.start(name, cmd, log_path, role=role):
                self.managed[name] = ManagedWorker(name=name, role=role)
                return
            if attempt == 0:
                logger.info("start failed for %s, retrying with bumped counter", name)
        logger.warning("failed to start runner worker after retry — skipping")

    def _adopt_existing_workers(self) -> None:
        """Unified adoption via ProcessManager metadata.

        Replaces the old PID-file-only adoption. ProcessMetadata files
        store manager_type so adoption validates correctly for both
        tmux (has-session) and subprocess (os.kill PID check) backends.
        """
        adopted = self._pm.adopt_existing()
        max_n = self._pm.max_counter()
        for name, role in adopted.items():
            self.managed[name] = ManagedWorker(name=name, role=role)
            logger.info("adopted worker %s (role=%s)", name, role)
        if max_n > 0:
            self._counter = max_n
```

**Migration note:** Old v0.6.1 PID files in `{state_dir}/pids/` are ignored after this change. ProcessManager uses `{state_dir}/processes/` for metadata. A one-time migration is not needed — old PID files are harmless, and any workers from a v0.6.1 run will have died by the time v0.6.2 starts.

### 4. Modify: `antfarm/core/doctor.py`

Add tmux check to doctor:

```python
def check_tmux_available(config: dict) -> list[Finding]:
    """Warn if tmux is not installed — workers will fall back to unreliable subprocess mode."""
    if shutil.which("tmux"):
        return []
    return [Finding(
        severity="warning",
        check="tmux_available",
        message="tmux not installed — worker spawning will use subprocess fallback (less reliable)",
        auto_fixable=False,
    )]
```

Add to `run_doctor()` check list. Also log a warning at `antfarm colony --autoscaler` startup if using subprocess fallback.

### 5. Tests

**New file: `tests/test_process_manager.py`**

```python
# --- Tests that run everywhere (mocked) ---

def test_parse_session_name():
    assert parse_session_name("auto-builder-3") == ("auto-", "builder", 3)
    assert parse_session_name("runner-planner-1") == ("runner-", "planner", 1)
    assert parse_session_name("invalid") is None
    assert parse_session_name("auto-builder-notanum") is None

def test_get_process_manager_no_tmux():
    with patch("antfarm.core.process_manager.shutil.which", return_value=None):
        pm = get_process_manager()
        assert isinstance(pm, SubprocessProcessManager)

def test_get_process_manager_no_tmux_env():
    with patch.dict(os.environ, {"ANTFARM_NO_TMUX": "1"}):
        pm = get_process_manager()
        assert isinstance(pm, SubprocessProcessManager)

def test_get_process_manager_with_tmux():
    with patch("antfarm.core.process_manager.shutil.which", return_value="/usr/bin/tmux"):
        pm = get_process_manager()
        assert isinstance(pm, TmuxProcessManager)

def test_subprocess_pm_start_and_lifecycle(tmp_path):
    pm = SubprocessProcessManager()
    log = str(tmp_path / "test.log")
    assert pm.start("auto-builder-1", ["sleep", "60"], log)
    assert pm.is_alive("auto-builder-1")
    assert pm.stop("auto-builder-1")

def test_subprocess_pm_list_managed():
    pm = SubprocessProcessManager()
    pm.start("auto-builder-1", ["sleep", "60"])
    pm.start("auto-builder-2", ["sleep", "60"])
    managed = pm.list_managed()
    assert "auto-builder-1" in managed
    assert "auto-builder-2" in managed
    pm.stop("auto-builder-1")
    pm.stop("auto-builder-2")

def test_subprocess_adopt_existing_returns_empty(tmp_path):
    """Subprocess backend intentionally does NOT adopt across restart.

    Verifies the documented contract: SubprocessProcessManager.adopt_existing()
    returns {} and cleans stale metadata, even when prior metadata files exist.
    """
    pm = SubprocessProcessManager(state_dir=str(tmp_path))
    pm.start("auto-builder-1", ["sleep", "60"], role="builder")
    pm.start("auto-reviewer-2", ["sleep", "60"], role="reviewer")

    # Simulate "restart" — new pm instance reads metadata but must not adopt
    pm2 = SubprocessProcessManager(state_dir=str(tmp_path))
    adopted = pm2.adopt_existing()
    assert adopted == {}
    # Stale metadata was cleaned
    assert pm2._read_metadata("auto-builder-1") is None
    assert pm2._read_metadata("auto-reviewer-2") is None

    pm.stop("auto-builder-1")
    pm.stop("auto-reviewer-2")

def test_max_counter():
    pm = SubprocessProcessManager()
    pm.start("auto-builder-3", ["sleep", "60"])
    pm.start("auto-builder-7", ["sleep", "60"])
    assert pm.max_counter() == 7
    pm.stop("auto-builder-3")
    pm.stop("auto-builder-7")

def test_process_metadata_roundtrip(tmp_path):
    """Metadata files are written on start and readable for adoption."""
    pm = SubprocessProcessManager(state_dir=str(tmp_path))
    pm.start("auto-builder-1", ["sleep", "60"], role="builder")
    meta = pm._read_metadata("auto-builder-1")
    assert meta is not None
    assert meta.name == "auto-builder-1"
    assert meta.role == "builder"
    assert meta.manager_type == "subprocess"
    assert meta.pid is not None and meta.pid > 0
    pm.stop("auto-builder-1")

# --- Tests that need real tmux ---

@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_tmux_pm_start_and_lifecycle(tmp_path):
    pm = TmuxProcessManager()
    log = str(tmp_path / "test.log")
    name = "antfarm-test-pm"
    try:
        assert pm.start(name, ["sleep", "60"], log)
        assert pm.is_alive(name)
    finally:
        pm.stop(name)
    assert not pm.is_alive(name)

@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_tmux_pm_shell_injection_safe(tmp_path):
    pm = TmuxProcessManager()
    name = "antfarm-test-inject"
    try:
        assert pm.start(name, ["echo", "hello; echo pwned"])
    finally:
        pm.stop(name)

@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_tmux_pm_writes_metadata(tmp_path):
    pm = TmuxProcessManager(state_dir=str(tmp_path))
    name = "auto-builder-99"
    try:
        pm.start(name, ["sleep", "60"], role="builder")
        meta = pm._read_metadata(name)
        assert meta is not None
        assert meta.manager_type == "tmux"
        assert meta.session_name == name
        assert meta.role == "builder"
    finally:
        pm.stop(name)

@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_tmux_pm_adopt_existing():
    pm = TmuxProcessManager()
    try:
        pm.start("auto-builder-5", ["sleep", "60"])
        pm.start("auto-reviewer-2", ["sleep", "60"])
        # New manager discovers existing sessions
        pm2 = TmuxProcessManager()
        adopted = pm2.adopt_existing()
        assert "auto-builder-5" in adopted
        assert adopted["auto-builder-5"] == "builder"
        assert pm2.max_counter() == 5
    finally:
        pm.stop("auto-builder-5")
        pm.stop("auto-reviewer-2")
```

**Modify `tests/test_autoscaler.py`:**
- Add: `test_autoscaler_uses_process_manager` — mock ProcessManager, verify `start()` called
- Add: `test_autoscaler_adopts_on_startup` — mock ProcessManager.adopt_existing, verify managed populated
- Existing tests: mock `get_process_manager` to return SubprocessProcessManager to preserve behavior

---

## PR Sequence

| # | PR | Scope |
|---|-----|-------|
| 1 | `feat(worker): ProcessManager abstraction with tmux + subprocess backends` | New `process_manager.py` + tests |
| 2 | `feat(autoscaler): use ProcessManager for worker spawning (#202)` | Autoscaler refactor + adoption + tests |
| 3 | `feat(runner): use ProcessManager for worker spawning` | Runner refactor + tests |
| 4 | `feat(doctor): warn when tmux not installed` | Doctor check |

PRs 1-2 can be combined. PR 3 follows. PR 4 is trivial.

---

## Rollback

- `ANTFARM_NO_TMUX=1` forces subprocess fallback on any machine
- SubprocessProcessManager is the existing behavior — always works
- ProcessManager ABC means swapping implementations is a one-line change

---

## Edge Cases

| Case | Behavior |
|------|----------|
| tmux not installed | `get_process_manager()` returns SubprocessProcessManager. Doctor warns. |
| `ANTFARM_NO_TMUX=1` | Forces SubprocessProcessManager even if tmux installed |
| Session name collision | `start()` returns False. Caller retries once with bumped counter. |
| Colony restarts (tmux) | `adopt_existing()` reads metadata files + discovers tmux sessions. Full recovery. |
| Colony restarts (subprocess) | `adopt_existing()` returns {} and discards any leftover subprocess metadata. **No restart adoption** (PID reuse is unsafe, Popen handles are lost). Doctor recovers stale tasks. |
| Runner restarts | Same adoption via ProcessManager metadata files |
| Worker finishes, session ends | `is_alive()` returns False, `_cleanup_exited()` removes from managed + metadata |
| `tmux kill-server` externally | All workers die. Metadata files remain. Next adoption cleans stale metadata. Doctor recovers tasks. |
| CI without tmux | tmux tests skip. SubprocessProcessManager tests run everywhere. |
| Name format changes | `parse_session_name()` is the single parser. Update once, adoption stays in sync. |
| Old v0.6.1 PID files | Ignored — ProcessManager uses `{state_dir}/processes/`, not `{state_dir}/pids/` |
