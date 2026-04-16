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
 survive restarts)    fallback only)
```

**Key principle:** Autoscaler and runner never know which implementation they're using. They call `self._pm.start(name, cmd, log_path)` and that's it.

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
class ManagedProcess:
    """A worker process managed by the ProcessManager."""
    name: str
    role: str
    alive: bool


class ProcessManager(ABC):
    """Interface for worker process lifecycle management."""

    def __init__(self, prefix: str = "auto-"):
        self.prefix = prefix

    @abstractmethod
    def start(self, name: str, cmd: list[str], log_path: str | None = None) -> bool:
        """Start a worker process. Returns True on success."""
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

        Returns: {name: role} for each adopted process.
        """
        adopted = {}
        for name in self.list_managed():
            if not self.is_alive(name):
                self.cleanup(name)
                continue
            parsed = parse_session_name(name)
            if parsed and parsed[0] == self.prefix:
                adopted[name] = parsed[1]  # role
        return adopted

    def max_counter(self) -> int:
        """Return the highest counter value among managed processes.

        Used to set _counter on startup to avoid name collisions.
        """
        max_n = 0
        for name in self.list_managed():
            parsed = parse_session_name(name)
            if parsed:
                max_n = max(max_n, parsed[2])
        return max_n

    def cleanup(self, name: str) -> None:
        """Clean up a dead process (remove PID files, stale state)."""
        pass  # default no-op, implementations override if needed


class TmuxProcessManager(ProcessManager):
    """Spawns workers as tmux sessions — real TTY, attach/debug, survive restarts."""

    def start(self, name: str, cmd: list[str], log_path: str | None = None) -> bool:
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
    """Spawns workers via subprocess.Popen — fallback when tmux unavailable."""

    def __init__(self, prefix: str = "auto-"):
        super().__init__(prefix)
        self._processes: dict[str, subprocess.Popen] = {}
        self._log_files: dict[str, object] = {}

    def start(self, name: str, cmd: list[str], log_path: str | None = None) -> bool:
        if log_path:
            log_file = open(log_path, "a")  # noqa: SIM115
            self._log_files[name] = log_file
        else:
            log_file = subprocess.DEVNULL
        process = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
        self._processes[name] = process
        logger.info("started subprocess: %s pid=%d", name, process.pid)
        return True

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


def get_process_manager(prefix: str = "auto-") -> ProcessManager:
    """Factory: return TmuxProcessManager if tmux available, else subprocess fallback.

    Respects ANTFARM_NO_TMUX=1 environment variable to force fallback.
    """
    if os.environ.get("ANTFARM_NO_TMUX"):
        logger.info("ANTFARM_NO_TMUX set — using subprocess process manager")
        return SubprocessProcessManager(prefix)
    if shutil.which("tmux"):
        return TmuxProcessManager(prefix)
    logger.warning("tmux not installed — falling back to subprocess process manager")
    return SubprocessProcessManager(prefix)
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
        self._pm = get_process_manager(prefix="auto-")

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
        self._counter += 1
        name = f"auto-{role}-{self._counter}"
        worker_id = f"{self.config.node_id}/{name}"
        cmd = [...]  # same command list as today
        log_path = os.path.join(self.config.data_dir, "logs", f"autoscaler-{name}.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        if self._pm.start(name, cmd, log_path):
            self.managed[name] = ManagedWorker(name=name, role=role, worker_id=worker_id)
        else:
            logger.warning("failed to start worker %s — skipping", name)

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
        self._stopped = True
        for mw in list(self.managed.values()):
            self._pm.stop(mw.name)
```

**No if/else branching anywhere.** The ProcessManager handles it.

### 3. Modify: `antfarm/core/runner.py`

Same simplification. Runner's `ManagedWorker` drops tmux/process fields:

```python
@dataclass
class ManagedWorker:
    name: str
    role: str
    pid: int  # kept for PID file compat, but lifecycle via ProcessManager
```

Runner uses `get_process_manager(prefix="runner-")`. Adoption combines PID files (existing) with ProcessManager adoption.

```python
class Runner:
    def __init__(self, ...):
        ...
        self._pm = get_process_manager(prefix="runner-")

    def _start_worker(self, role: str) -> None:
        self._counter += 1
        name = f"runner-{role}-{self._counter}"
        cmd = [...]
        log_path = os.path.join(self.state_dir, "logs", f"{name}.log")
        if self._pm.start(name, cmd, log_path):
            pid = 0  # tmux doesn't give us a direct PID, that's fine
            self.managed[name] = ManagedWorker(name=name, role=role, pid=pid)
            self._write_pid_file(name, pid)

    def _adopt_existing_workers(self) -> None:
        # 1. PID file adoption (existing, unchanged)
        ...
        # 2. ProcessManager adoption (new)
        adopted = self._pm.adopt_existing()
        for name, role in adopted.items():
            if name not in self.managed:
                self.managed[name] = ManagedWorker(name=name, role=role, pid=0)
```

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

def test_adopt_existing_returns_roles():
    pm = SubprocessProcessManager()
    pm.start("auto-builder-1", ["sleep", "60"])
    pm.start("auto-reviewer-2", ["sleep", "60"])
    adopted = pm.adopt_existing()
    assert adopted == {"auto-builder-1": "builder", "auto-reviewer-2": "reviewer"}
    pm.stop("auto-builder-1")
    pm.stop("auto-reviewer-2")

def test_max_counter():
    pm = SubprocessProcessManager()
    pm.start("auto-builder-3", ["sleep", "60"])
    pm.start("auto-builder-7", ["sleep", "60"])
    assert pm.max_counter() == 7
    pm.stop("auto-builder-3")
    pm.stop("auto-builder-7")

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
| Session name collision | `start()` returns False. Caller logs warning, skips. No fallback to Popen. |
| Colony restarts, tmux sessions alive | `_adopt_existing()` discovers sessions, parses roles, sets counter |
| Runner restarts | Same adoption via ProcessManager + PID file fallback |
| Worker finishes, session ends | `is_alive()` returns False, `_cleanup_exited()` removes from managed |
| `tmux kill-server` externally | All workers die. Doctor recovers tasks. Autoscaler respawns. |
| CI without tmux | tmux tests skip. SubprocessProcessManager tests run everywhere. |
| Name format changes | `parse_session_name()` is the single parser. Update once, adoption stays in sync. |
