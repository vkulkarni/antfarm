# Antfarm v0.6.2 — Implementation Plan

**Status:** DRAFT — awaiting approval
**Derived from:** Dogfood findings during v0.6.1 deployment
**Prerequisite:** v0.6.1 shipped on `main` as of `v0.6.1` tag. 809 tests passing.
**Scope:** tmux-based worker spawning. One issue: #202.
**Goal:** Workers spawned by autoscaler and runner reliably get a real TTY, eliminating silent subprocess failures.

---

## Problem

During v0.6.1 dogfooding, `claude -p` subprocesses spawned by the autoscaler via `subprocess.Popen` exit silently with empty stdout/stderr. The same command works perfectly when run in a foreground terminal. Root cause: `claude -p` needs a real TTY — piped stdout/stderr changes its behavior.

## Solution

Replace `subprocess.Popen(cmd, stdout=log_file, stderr=log_file)` with `tmux new-session -d -s {name} {cmd}`. Each worker gets a real terminal session via tmux. Subprocess.Popen remains as fallback when tmux is not installed.

---

## What changes

### 1. New utility: `antfarm/core/tmux.py`

Thin wrapper around tmux CLI commands. Keeps tmux details out of autoscaler/runner.

**Every public function guards against tmux not being installed.** Callers don't need to check `is_available()` before calling — functions return safe defaults (False, None, []) when tmux is missing.

```python
"""tmux session management for Antfarm worker processes."""

import shlex
import shutil
import subprocess
import logging

logger = logging.getLogger(__name__)


def _tmux_path() -> str | None:
    """Resolve tmux binary path. Called per-use, not cached at import time."""
    return shutil.which("tmux")


def is_available() -> bool:
    """Check if tmux is installed."""
    return _tmux_path() is not None


def start_session(name: str, cmd: list[str], cwd: str | None = None,
                  log_path: str | None = None) -> bool:
    """Start a detached tmux session running cmd.

    Logging is set up BEFORE the command runs — the session starts with a
    shell wrapper that tees output to the log file, so no output is lost
    even if the command exits immediately.

    Args:
        name: Session name (must be unique).
        cmd: Command to run inside the session.
        cwd: Working directory for the session.
        log_path: If set, tee session output to this file.

    Returns:
        True if session started successfully. False if tmux unavailable
        or session name already exists.
    """
    tmux = _tmux_path()
    if not tmux:
        return False

    # Build the inner command with proper shell quoting to prevent injection.
    # Wrap in a shell that tees output to log_path so logging is atomic with
    # command start — no race between session start and pipe-pane.
    quoted_cmd = shlex.join(cmd)
    if log_path:
        inner = f"{quoted_cmd} 2>&1 | tee -a {shlex.quote(log_path)}"
    else:
        inner = quoted_cmd

    tmux_cmd = [tmux, "new-session", "-d", "-s", name]
    if cwd:
        tmux_cmd.extend(["-c", cwd])
    tmux_cmd.extend(["sh", "-c", inner])

    result = subprocess.run(tmux_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning("tmux start failed for %s: %s", name, result.stderr.strip())
        return False

    logger.info("tmux session started: %s", name)
    return True


def has_session(name: str) -> bool:
    """Check if a tmux session exists (worker is alive).

    Returns False if tmux is not installed.
    """
    tmux = _tmux_path()
    if not tmux:
        return False
    result = subprocess.run(
        [tmux, "has-session", "-t", name],
        capture_output=True,
    )
    return result.returncode == 0


def kill_session(name: str) -> bool:
    """Kill a tmux session (stop a worker).

    Returns False if tmux is not installed or session doesn't exist.
    """
    tmux = _tmux_path()
    if not tmux:
        return False
    result = subprocess.run(
        [tmux, "kill-session", "-t", name],
        capture_output=True,
    )
    return result.returncode == 0


def list_sessions(prefix: str = "auto-") -> list[str]:
    """List tmux session names matching prefix.

    Returns empty list if tmux is not installed or no server running.
    """
    tmux = _tmux_path()
    if not tmux:
        return []
    result = subprocess.run(
        [tmux, "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [s for s in result.stdout.strip().split("\n") if s and s.startswith(prefix)]


def get_session_pid(name: str) -> int | None:
    """Get the PID of the shell process in a tmux session.

    Note: this is the shell PID (tmux pane process), not the antfarm worker
    PID. The worker is a child of this shell. Use session names for lifecycle
    management, not PIDs.

    Returns None if tmux is not installed or session doesn't exist.
    """
    tmux = _tmux_path()
    if not tmux:
        return None
    result = subprocess.run(
        [tmux, "list-panes", "-t", name, "-F", "#{pane_pid}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    pid_str = result.stdout.strip().split("\n")[0]
    try:
        return int(pid_str)
    except ValueError:
        return None
```

**Fixes applied:**
- Fix #1: `_tmux_path()` resolves per-call, not cached at import time. Every function guards internally.
- Fix #2: `shlex.join(cmd)` prevents shell injection. tmux runs `sh -c <quoted>` for proper argument handling.
- Fix #3: Logging via `tee` inside the shell command — atomic with command start, no race with `pipe-pane`.
- PID docstring clarifies shell PID vs worker PID distinction.

### 2. Modify: `antfarm/core/autoscaler.py`

Change `ManagedWorker` to track tmux session instead of Popen:

```python
@dataclass
class ManagedWorker:
    name: str
    role: str
    worker_id: str
    process: subprocess.Popen | None = None  # fallback when tmux unavailable
    tmux_session: str | None = None  # tmux session name when using tmux
```

**Add `_adopt_tmux_sessions()` — called at startup before first reconcile:**

```python
def _adopt_tmux_sessions(self) -> None:
    """Discover and adopt tmux sessions from a previous colony run.

    Scans for tmux sessions matching the 'auto-' prefix, parses the
    role from the session name (auto-{role}-{N}), and adds them to
    self.managed. Sets self._counter to max(existing) + 1 to avoid
    name collisions.

    This is what makes tmux better than subprocess — workers survive
    colony restarts.
    """
    from antfarm.core.tmux import is_available, list_sessions

    if not is_available():
        return

    existing = list_sessions("auto-")
    max_counter = 0
    for session_name in existing:
        # Parse "auto-{role}-{N}" format
        parts = session_name.split("-", 2)  # ["auto", role, N]
        if len(parts) != 3:
            continue
        try:
            counter = int(parts[2])
        except ValueError:
            continue
        role = parts[1]
        worker_id = f"{self.config.node_id}/{session_name}"
        self.managed[session_name] = ManagedWorker(
            name=session_name,
            role=role,
            worker_id=worker_id,
            tmux_session=session_name,
        )
        max_counter = max(max_counter, counter)
        logger.info("adopted tmux session %s (role=%s)", session_name, role)

    if max_counter > 0:
        self._counter = max_counter  # avoid name collisions
```

**Change `run()` to call adoption at startup:**

```python
def run(self) -> None:
    self._adopt_tmux_sessions()
    while not self._stopped:
        try:
            self._reconcile()
        except Exception as e:
            logger.exception("autoscaler reconcile failed: %s", e)
        time.sleep(self.config.poll_interval)
```

**Change `_start_worker()` — no fallback to Popen on name collision:**

```python
def _start_worker(self, role: str) -> None:
    """Spawn a new worker via tmux (preferred) or subprocess (fallback)."""
    self._counter += 1
    name = f"auto-{role}-{self._counter}"
    worker_id = f"{self.config.node_id}/{name}"

    cmd = [
        "antfarm", "worker", "start",
        "--agent", self.config.agent_type,
        "--type", role,
        "--node", self.config.node_id,
        "--name", name,
        "--repo-path", self.config.repo_path,
        "--integration-branch", self.config.integration_branch,
        "--workspace-root", self.config.workspace_root,
        "--colony-url", self.config.colony_url,
    ]
    if self.config.token:
        cmd.extend(["--token", self.config.token])

    log_dir = os.path.join(self.config.data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"autoscaler-{name}.log")

    from antfarm.core.tmux import is_available, start_session
    if is_available():
        started = start_session(name, cmd, log_path=log_path)
        if started:
            self.managed[name] = ManagedWorker(
                name=name, role=role, worker_id=worker_id, tmux_session=name)
            logger.info("autoscaler started worker (tmux) name=%s role=%s", name, role)
            return
        # tmux start failed (e.g., name collision) — DON'T fall back to Popen.
        # Name collision means a session already exists, which means a worker
        # is already running. Falling back to Popen would re-introduce the
        # silent failure bug. Log and skip.
        logger.warning("tmux session start failed for %s — skipping (not falling back to Popen)", name)
        return

    # Fallback: subprocess.Popen (only when tmux is not installed at all)
    log_file = open(log_path, "a")  # noqa: SIM115
    process = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
    self.managed[name] = ManagedWorker(
        name=name, role=role, worker_id=worker_id, process=process)
    logger.info("autoscaler started worker (subprocess) name=%s role=%s pid=%d", name, role, process.pid)
```

**Key fix (#4):** On tmux name collision, DON'T fall back to Popen. That re-introduces the bug. Log a warning and skip — the existing session is already running a worker for that name.

Change `_stop_idle_worker()`, `_cleanup_exited()`, `_count_actual()`, `stop()` — same as before (check `mw.tmux_session` vs `mw.process`). No changes from original plan here.

### 3. Modify: `antfarm/core/runner.py`

**Full specification (not "same pattern"):**

The Runner's `ManagedWorker` already has `is_alive()` and `terminate()` methods. Extend them:

```python
@dataclass
class ManagedWorker:
    name: str
    role: str
    pid: int
    process: subprocess.Popen | None = None
    tmux_session: str | None = None  # new field

    def is_alive(self) -> bool:
        if self.tmux_session:
            from antfarm.core.tmux import has_session
            return has_session(self.tmux_session)
        if self.process is not None:
            return self.process.poll() is None
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def terminate(self) -> None:
        if self.tmux_session:
            from antfarm.core.tmux import kill_session
            kill_session(self.tmux_session)
            return
        if self.process is not None:
            self.process.terminate()
        else:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
```

Runner's `_start_worker()` follows the same pattern as autoscaler: try tmux first, fall back to subprocess only if tmux not installed, never fall back on name collision.

Runner's `_adopt_existing_workers()` extends to also check tmux sessions:
```python
def _adopt_existing_workers(self) -> None:
    """Adopt workers from PID files AND from orphaned tmux sessions."""
    # 1. Existing PID file adoption (unchanged)
    ...
    # 2. tmux session adoption (new)
    from antfarm.core.tmux import is_available, list_sessions, get_session_pid
    if is_available():
        prefix = "runner-"
        for session_name in list_sessions(prefix):
            if session_name in self.managed:
                continue  # already adopted from PID file
            parts = session_name.split("-", 2)  # ["runner", role, N]
            if len(parts) != 3:
                continue
            role = parts[1]
            pid = get_session_pid(session_name) or 0
            self.managed[session_name] = ManagedWorker(
                name=session_name, role=role, pid=pid, tmux_session=session_name)
            logger.info("adopted tmux session %s (role=%s)", session_name, role)
```

### 4. Tests

**New file: `tests/test_tmux.py`**

All tests that create real tmux sessions are guarded with `@pytest.mark.skipif`:

```python
import pytest
from antfarm.core.tmux import is_available

pytestmark = pytest.mark.skipif(not is_available(), reason="tmux not installed")


def test_is_available():
    assert is_available()


def test_start_and_kill_session(tmp_path):
    from antfarm.core.tmux import start_session, has_session, kill_session
    name = "antfarm-test-session"
    log = str(tmp_path / "test.log")
    try:
        assert start_session(name, ["sleep", "60"], log_path=log)
        assert has_session(name)
    finally:
        kill_session(name)
    assert not has_session(name)


def test_start_session_with_special_chars(tmp_path):
    """Verify shell injection is prevented via shlex.join."""
    from antfarm.core.tmux import start_session, kill_session
    name = "antfarm-test-special"
    # Command with spaces and shell metacharacters in args
    try:
        assert start_session(name, ["echo", "hello world; echo pwned"])
    finally:
        kill_session(name)


def test_list_sessions():
    from antfarm.core.tmux import start_session, list_sessions, kill_session
    try:
        start_session("auto-test-1", ["sleep", "60"])
        start_session("auto-test-2", ["sleep", "60"])
        sessions = list_sessions("auto-test-")
        assert "auto-test-1" in sessions
        assert "auto-test-2" in sessions
    finally:
        kill_session("auto-test-1")
        kill_session("auto-test-2")


def test_has_session_nonexistent():
    from antfarm.core.tmux import has_session
    assert not has_session("antfarm-definitely-does-not-exist")


def test_kill_session_nonexistent():
    from antfarm.core.tmux import kill_session
    assert not kill_session("antfarm-definitely-does-not-exist")
```

**Tests that mock tmux (run everywhere, no real tmux needed):**

```python
def test_all_functions_safe_without_tmux():
    """When tmux is not installed, all functions return safe defaults."""
    from unittest.mock import patch
    with patch("antfarm.core.tmux.shutil.which", return_value=None):
        from antfarm.core import tmux
        assert not tmux.is_available()
        assert not tmux.start_session("x", ["y"])
        assert not tmux.has_session("x")
        assert not tmux.kill_session("x")
        assert tmux.list_sessions() == []
        assert tmux.get_session_pid("x") is None
```

**Modify: `tests/test_autoscaler.py`**
- Add: `test_autoscaler_uses_tmux` — mock tmux.is_available=True, mock tmux.start_session, verify it's called instead of Popen
- Add: `test_autoscaler_tmux_fallback` — mock tmux.is_available=False, verify Popen is used
- Add: `test_autoscaler_adopts_tmux_sessions` — mock list_sessions returning 2 sessions, verify managed dict populated and counter set
- Add: `test_autoscaler_no_popen_fallback_on_name_collision` — mock tmux available but start_session returns False, verify Popen NOT called

---

## PR Sequence

| # | PR | Scope |
|---|-----|-------|
| 1 | `feat(worker): tmux session management utility` | New `tmux.py` + tests |
| 2 | `feat(autoscaler): spawn workers via tmux sessions (#202)` | Autoscaler + adoption + tests |
| 3 | `feat(runner): spawn workers via tmux sessions` | Runner + adoption + tests |

PRs 1-2 can be combined. PR 3 is independent.

---

## Rollback

If tmux causes issues, the fallback path (subprocess.Popen) is always available when tmux is not installed. To force fallback on a machine with tmux, set `ANTFARM_NO_TMUX=1` environment variable (check in `is_available()`).

---

## Edge Cases

| Case | Behavior |
|------|----------|
| tmux not installed | Fallback to subprocess.Popen (existing behavior) |
| `ANTFARM_NO_TMUX=1` set | Force fallback to subprocess.Popen |
| tmux session name collision | Log warning, skip. Do NOT fall back to Popen. |
| Colony restarts, tmux sessions alive | `_adopt_tmux_sessions()` discovers and adopts them. `_counter` set to max+1. |
| Runner restarts, tmux sessions alive | `_adopt_existing_workers()` discovers via `list_sessions()` and adopts. |
| Worker finishes, tmux session ends | `has_session` returns False, `_cleanup_exited` removes from managed |
| `tmux kill-server` run externally | All workers die. Doctor recovers stale tasks. Autoscaler respawns. |
| Pane PID vs worker PID | Session names used for lifecycle, not PIDs. Colony tracks worker_id from registration. |
| CI without tmux | Tests skip with `pytest.mark.skipif`. Mock-based tests run everywhere. |
