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

Replace `subprocess.Popen(cmd, stdout=log_file, stderr=log_file)` with `tmux new-session -d -s {name} {cmd}`. Each worker gets a real terminal session via tmux.

## What changes

### 1. New utility: `antfarm/core/tmux.py`

Thin wrapper around tmux CLI commands. Keeps tmux details out of autoscaler/runner.

```python
"""tmux session management for Antfarm worker processes."""

import shutil
import subprocess
import logging

logger = logging.getLogger(__name__)

_TMUX = shutil.which("tmux")


def is_available() -> bool:
    """Check if tmux is installed."""
    return _TMUX is not None


def start_session(name: str, cmd: list[str], cwd: str | None = None,
                  log_path: str | None = None) -> bool:
    """Start a detached tmux session running cmd.
    
    Args:
        name: Session name (must be unique).
        cmd: Command to run inside the session.
        cwd: Working directory for the session.
        log_path: If set, pipe session output to this file via pipe-pane.
    
    Returns:
        True if session started successfully.
    """
    tmux_cmd = [_TMUX, "new-session", "-d", "-s", name]
    if cwd:
        tmux_cmd.extend(["-c", cwd])
    tmux_cmd.append(" ".join(cmd))  # tmux takes the command as a single string arg
    
    result = subprocess.run(tmux_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning("tmux start failed for %s: %s", name, result.stderr.strip())
        return False
    
    if log_path:
        subprocess.run(
            [_TMUX, "pipe-pane", "-t", name, f"cat >> {log_path}"],
            capture_output=True,
        )
    
    logger.info("tmux session started: %s", name)
    return True


def has_session(name: str) -> bool:
    """Check if a tmux session exists (worker is alive)."""
    result = subprocess.run(
        [_TMUX, "has-session", "-t", name],
        capture_output=True,
    )
    return result.returncode == 0


def kill_session(name: str) -> bool:
    """Kill a tmux session (stop a worker)."""
    result = subprocess.run(
        [_TMUX, "kill-session", "-t", name],
        capture_output=True,
    )
    return result.returncode == 0


def list_sessions(prefix: str = "auto-") -> list[str]:
    """List tmux session names matching prefix."""
    result = subprocess.run(
        [_TMUX, "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [s for s in result.stdout.strip().split("\n") if s.startswith(prefix)]


def get_session_pid(name: str) -> int | None:
    """Get the PID of the main process in a tmux session."""
    result = subprocess.run(
        [_TMUX, "list-panes", "-t", name, "-F", "#{pane_pid}"],
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

Change `_start_worker()`:
```python
def _start_worker(self, role: str) -> None:
    self._counter += 1
    name = f"auto-{role}-{self._counter}"
    worker_id = f"{self.config.node_id}/{name}"
    cmd = [...]  # same command as today
    
    log_dir = os.path.join(self.config.data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"autoscaler-{name}.log")
    
    from antfarm.core.tmux import is_available, start_session
    if is_available():
        started = start_session(name, cmd, log_path=log_path)
        if started:
            self.managed[name] = ManagedWorker(
                name=name, role=role, worker_id=worker_id, tmux_session=name)
            return
    
    # Fallback: subprocess.Popen (existing behavior)
    log_file = open(log_path, "a")
    process = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
    self.managed[name] = ManagedWorker(
        name=name, role=role, worker_id=worker_id, process=process)
```

Change `_stop_idle_worker()` and `_cleanup_exited()` to check tmux sessions:
```python
# In _cleanup_exited:
if mw.tmux_session:
    if not has_session(mw.tmux_session):
        del self.managed[name]  # session ended
elif mw.process and mw.process.poll() is not None:
    del self.managed[name]  # process exited

# In _stop_idle_worker:
if mw.tmux_session:
    kill_session(mw.tmux_session)
elif mw.process:
    mw.process.terminate()
```

Change `stop()`:
```python
def stop(self):
    self._stopped = True
    for mw in list(self.managed.values()):
        if mw.tmux_session:
            kill_session(mw.tmux_session)
        elif mw.process and mw.process.poll() is None:
            mw.process.terminate()
```

Change `_count_actual()`:
```python
def _count_actual(self) -> dict[str, int]:
    counts = {}
    for mw in self.managed.values():
        alive = (has_session(mw.tmux_session) if mw.tmux_session
                 else mw.process and mw.process.poll() is None)
        if alive:
            counts[mw.role] = counts.get(mw.role, 0) + 1
    return counts
```

### 3. Modify: `antfarm/core/runner.py`

Same pattern as autoscaler. The Runner's `_start_worker()` and related methods get the same tmux treatment. The Runner's `ManagedWorker` already has `is_alive()` and `terminate()` methods — extend them to check tmux sessions.

### 4. Tests

**New file: `tests/test_tmux.py`**

```python
def test_is_available():
    """tmux should be available on this machine."""
    from antfarm.core.tmux import is_available
    assert is_available()

def test_start_and_kill_session(tmp_path):
    """Start a tmux session, verify it exists, kill it."""
    from antfarm.core.tmux import start_session, has_session, kill_session
    name = "antfarm-test-session"
    log = str(tmp_path / "test.log")
    assert start_session(name, ["sleep", "60"], log_path=log)
    assert has_session(name)
    assert kill_session(name)
    assert not has_session(name)

def test_list_sessions():
    """List sessions with prefix filtering."""
    from antfarm.core.tmux import start_session, list_sessions, kill_session
    start_session("auto-test-1", ["sleep", "60"])
    start_session("auto-test-2", ["sleep", "60"])
    sessions = list_sessions("auto-test-")
    assert "auto-test-1" in sessions
    assert "auto-test-2" in sessions
    kill_session("auto-test-1")
    kill_session("auto-test-2")

def test_fallback_when_tmux_unavailable():
    """When tmux is not available, autoscaler falls back to subprocess.Popen."""
    # Mock shutil.which to return None, verify Popen is used
```

**Modify: `tests/test_autoscaler.py`**
- Existing tests should still pass (they mock subprocess.Popen — add tmux mock to maintain behavior)
- Add: `test_autoscaler_uses_tmux` — verify tmux.start_session called instead of Popen
- Add: `test_autoscaler_tmux_fallback` — mock tmux unavailable, verify Popen used

---

## PR Sequence

| # | PR | Scope |
|---|-----|-------|
| 1 | `feat(worker): tmux session management utility` | New `tmux.py` + tests |
| 2 | `feat(autoscaler): spawn workers via tmux sessions (#202)` | Autoscaler changes + tests |
| 3 | `feat(runner): spawn workers via tmux sessions` | Runner changes |

PRs 1-2 can be combined into a single PR if preferred. PR 3 is independent.

---

## Rollback

If tmux causes issues, the fallback path (subprocess.Popen) is always available. The `is_available()` check means antfarm works without tmux installed — it just falls back to the old behavior.

---

## Edge Cases

| Case | Behavior |
|------|----------|
| tmux not installed | Fallback to subprocess.Popen (existing behavior) |
| tmux session name collision | `start_session` returns False, falls back to Popen |
| Colony restarts, tmux sessions still running | Workers keep running. Autoscaler on restart calls `list_sessions()` to discover them. |
| Worker finishes, tmux session ends | `has_session` returns False, `_cleanup_exited` removes from managed |
| `tmux kill-server` run externally | All workers die. Doctor recovers stale tasks. Autoscaler respawns. |
