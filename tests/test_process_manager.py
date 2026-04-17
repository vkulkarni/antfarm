from __future__ import annotations

import os
import shutil
from unittest.mock import patch

import pytest

from antfarm.core.process_manager import (
    ProcessMetadata,
    SubprocessProcessManager,
    TmuxProcessManager,
    colony_hash,
    get_process_manager,
    parse_session_name,
)

# --- Tests that run everywhere (mocked) ---


def test_parse_session_name():
    assert parse_session_name("auto-builder-3", "auto-") == ("builder", 3)
    assert parse_session_name("runner-planner-1", "runner-") == ("planner", 1)
    # Multi-dash role
    assert parse_session_name("auto-code-reviewer-5", "auto-") == ("code-reviewer", 5)
    # Wrong prefix
    assert parse_session_name("runner-builder-1", "auto-") is None
    # Non-numeric counter
    assert parse_session_name("auto-builder-notanum", "auto-") is None
    # Plain invalid
    assert parse_session_name("invalid", "auto-") is None
    # Empty role
    assert parse_session_name("auto--1", "auto-") is None


def test_parse_session_name_with_hashed_prefix():
    """Hashed prefixes (per #231) parse correctly for both adapters."""
    assert parse_session_name("auto-a1b2c3d4-builder-3", "auto-a1b2c3d4-") == ("builder", 3)
    assert parse_session_name("runner-a1b2c3d4-planner-1", "runner-a1b2c3d4-") == ("planner", 1)
    # Multi-dash role under hashed prefix
    assert parse_session_name("auto-a1b2c3d4-code-reviewer-5", "auto-a1b2c3d4-") == (
        "code-reviewer",
        5,
    )
    # Foreign hash is not matched by own-hash prefix
    assert parse_session_name("auto-deadbeef-builder-3", "auto-a1b2c3d4-") is None


# --- colony_hash tests (per #231) ---


def test_colony_hash_stable_same_data_dir(tmp_path):
    """Calling colony_hash twice on the same data_dir returns the same hash."""
    path = str(tmp_path / "state")
    os.makedirs(path, exist_ok=True)
    assert colony_hash(path) == colony_hash(path)


def test_colony_hash_differs_for_different_data_dir(tmp_path):
    """Different data_dirs produce different hashes."""
    a = str(tmp_path / "colony-a")
    b = str(tmp_path / "colony-b")
    os.makedirs(a, exist_ok=True)
    os.makedirs(b, exist_ok=True)
    assert colony_hash(a) != colony_hash(b)


def test_colony_hash_resolves_symlinks(tmp_path):
    """A symlink to a data_dir produces the same hash as the real path."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(str(real), str(link))
    assert colony_hash(str(link)) == colony_hash(str(real))


def test_colony_hash_length_is_8(tmp_path):
    """Hash is exactly 8 hex chars — matches the session-name budget."""
    h = colony_hash(str(tmp_path))
    assert len(h) == 8
    assert all(c in "0123456789abcdef" for c in h)


def test_get_process_manager_no_tmux():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ANTFARM_NO_TMUX", None)
        with patch("antfarm.core.process_manager.shutil.which", return_value=None):
            pm = get_process_manager()
            assert isinstance(pm, SubprocessProcessManager)


def test_get_process_manager_no_tmux_env():
    with patch.dict(os.environ, {"ANTFARM_NO_TMUX": "1"}):
        pm = get_process_manager()
        assert isinstance(pm, SubprocessProcessManager)


def test_get_process_manager_with_tmux():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ANTFARM_NO_TMUX", None)
        with patch("antfarm.core.process_manager.shutil.which", return_value="/usr/bin/tmux"):
            pm = get_process_manager()
            assert isinstance(pm, TmuxProcessManager)


def test_subprocess_pm_start_and_lifecycle(tmp_path):
    pm = SubprocessProcessManager()
    log = str(tmp_path / "test.log")
    try:
        assert pm.start("auto-builder-1", ["sleep", "60"], log)
        assert pm.is_alive("auto-builder-1")
    finally:
        pm.stop("auto-builder-1")


def test_subprocess_pm_list_managed():
    pm = SubprocessProcessManager()
    try:
        pm.start("auto-builder-1", ["sleep", "60"])
        pm.start("auto-builder-2", ["sleep", "60"])
        managed = pm.list_managed()
        assert "auto-builder-1" in managed
        assert "auto-builder-2" in managed
    finally:
        pm.stop("auto-builder-1")
        pm.stop("auto-builder-2")


def test_subprocess_adopt_existing_returns_empty(tmp_path):
    """Subprocess backend intentionally does NOT adopt across restart.

    Verifies the documented contract: SubprocessProcessManager.adopt_existing()
    returns {} and cleans stale metadata, even when prior metadata files exist.
    """
    pm = SubprocessProcessManager(state_dir=str(tmp_path))
    try:
        pm.start("auto-builder-1", ["sleep", "60"], role="builder")
        pm.start("auto-reviewer-2", ["sleep", "60"], role="reviewer")

        # Simulate "restart" — new pm instance reads metadata but must not adopt
        pm2 = SubprocessProcessManager(state_dir=str(tmp_path))
        adopted = pm2.adopt_existing()
        assert adopted == {}
        # Stale metadata was cleaned
        assert pm2._read_metadata("auto-builder-1") is None
        assert pm2._read_metadata("auto-reviewer-2") is None
    finally:
        pm.stop("auto-builder-1")
        pm.stop("auto-reviewer-2")


def test_max_counter():
    pm = SubprocessProcessManager(prefix="auto-")
    try:
        pm.start("auto-builder-3", ["sleep", "60"])
        pm.start("auto-builder-7", ["sleep", "60"])
        assert pm.max_counter() == 7
    finally:
        pm.stop("auto-builder-3")
        pm.stop("auto-builder-7")


def test_process_metadata_roundtrip(tmp_path):
    """Metadata files are written on start and readable for adoption."""
    pm = SubprocessProcessManager(state_dir=str(tmp_path))
    try:
        pm.start("auto-builder-1", ["sleep", "60"], role="builder")
        meta = pm._read_metadata("auto-builder-1")
        assert meta is not None
        assert meta.name == "auto-builder-1"
        assert meta.role == "builder"
        assert meta.manager_type == "subprocess"
        assert meta.pid is not None and meta.pid > 0
    finally:
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
def test_tmux_pm_adopt_existing(tmp_path):
    pm = TmuxProcessManager(state_dir=str(tmp_path))
    try:
        pm.start("auto-builder-5", ["sleep", "60"], role="builder")
        pm.start("auto-reviewer-2", ["sleep", "60"], role="reviewer")
        # New manager discovers existing sessions
        pm2 = TmuxProcessManager(state_dir=str(tmp_path))
        adopted = pm2.adopt_existing()
        assert "auto-builder-5" in adopted
        assert adopted["auto-builder-5"] == "builder"
        assert pm2.max_counter() == 5
    finally:
        pm.stop("auto-builder-5")
        pm.stop("auto-reviewer-2")


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_tmux_pm_ignores_subprocess_metadata(tmp_path):
    """Cross-manager contamination guard.

    A leftover subprocess metadata file (possibly with a reused PID) must
    NOT be adopted by TmuxProcessManager. Filtering happens on
    meta.manager_type == self._manager_type().
    """
    # Plant a subprocess metadata file for a name that doesn't correspond
    # to any tmux session. Use this process's own PID so os.kill(pid, 0)
    # would succeed — proving the guard is manager-type, not liveness.
    sub = SubprocessProcessManager(state_dir=str(tmp_path))
    meta = ProcessMetadata(
        name="auto-builder-42",
        role="builder",
        manager_type="subprocess",
        pid=os.getpid(),
    )
    sub._write_metadata(meta)

    tmux_pm = TmuxProcessManager(state_dir=str(tmp_path))
    adopted = tmux_pm.adopt_existing()
    assert "auto-builder-42" not in adopted
    # Foreign metadata is left intact — tmux won't sweep subprocess files.
    assert tmux_pm._read_metadata("auto-builder-42") is not None
