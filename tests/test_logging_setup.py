"""Tests for antfarm.core.logging_setup.

Key invariants:
- Library imports never install handlers on the root logger.
- setup_logging() is idempotent (safe to call multiple times).
- ANTFARM_LOG_LEVEL env var controls the level.
- Noisy transport libs are pinned to WARNING unless user asks for DEBUG.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture
def clean_root_logger():
    """Reset the root logger before and after each test to avoid cross-test pollution."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_configured = getattr(root, "_antfarm_configured", False)

    # Also reset the noisy-lib loggers so tests are order-independent.
    noisy = ("httpcore", "httpx", "urllib3")
    original_noisy_levels = {name: logging.getLogger(name).level for name in noisy}

    # Start clean
    root.handlers.clear()
    if hasattr(root, "_antfarm_configured"):
        delattr(root, "_antfarm_configured")
    for name in noisy:
        logging.getLogger(name).setLevel(logging.NOTSET)

    yield

    # Restore
    root.handlers.clear()
    root.handlers.extend(original_handlers)
    root.setLevel(original_level)
    if original_configured:
        root._antfarm_configured = True  # type: ignore[attr-defined]
    elif hasattr(root, "_antfarm_configured"):
        delattr(root, "_antfarm_configured")
    for name, lvl in original_noisy_levels.items():
        logging.getLogger(name).setLevel(lvl)


def test_library_import_does_not_call_setup(clean_root_logger):
    """Importing antfarm library code must not call setup_logging()."""
    # Explicit re-import via importlib to exercise module init code.
    import importlib

    import antfarm.core.autoscaler
    import antfarm.core.serve
    import antfarm.core.worker
    importlib.reload(antfarm.core.worker)
    importlib.reload(antfarm.core.serve)
    importlib.reload(antfarm.core.autoscaler)

    # The library must not have marked the root logger as antfarm-configured.
    # (pytest may install its own LogCaptureHandler; we only care that our
    #  setup_logging() wasn't called.)
    assert not getattr(logging.getLogger(), "_antfarm_configured", False)


def test_setup_logging_installs_handler(clean_root_logger, monkeypatch):
    monkeypatch.delenv("ANTFARM_LOG_LEVEL", raising=False)
    from antfarm.core.logging_setup import setup_logging

    setup_logging()

    root = logging.getLogger()
    assert len(root.handlers) >= 1
    assert root.level == logging.INFO


def test_setup_logging_is_idempotent(clean_root_logger, monkeypatch):
    monkeypatch.delenv("ANTFARM_LOG_LEVEL", raising=False)
    from antfarm.core.logging_setup import setup_logging

    setup_logging()
    handler_count_after_first = len(logging.getLogger().handlers)

    setup_logging()
    setup_logging()
    assert len(logging.getLogger().handlers) == handler_count_after_first


def test_setup_logging_respects_env_var(clean_root_logger, monkeypatch):
    monkeypatch.setenv("ANTFARM_LOG_LEVEL", "DEBUG")
    from antfarm.core.logging_setup import setup_logging

    setup_logging()

    assert logging.getLogger().level == logging.DEBUG


def test_setup_logging_respects_explicit_level(clean_root_logger, monkeypatch):
    monkeypatch.setenv("ANTFARM_LOG_LEVEL", "DEBUG")  # should be overridden
    from antfarm.core.logging_setup import setup_logging

    setup_logging(level="WARNING")

    assert logging.getLogger().level == logging.WARNING


def test_noisy_libs_pinned_to_warning_at_info(clean_root_logger, monkeypatch):
    monkeypatch.setenv("ANTFARM_LOG_LEVEL", "INFO")
    from antfarm.core.logging_setup import setup_logging

    setup_logging()

    for name in ("httpcore", "httpx", "urllib3"):
        assert logging.getLogger(name).level == logging.WARNING


def test_noisy_libs_unmuted_at_debug(clean_root_logger, monkeypatch):
    monkeypatch.setenv("ANTFARM_LOG_LEVEL", "DEBUG")
    from antfarm.core.logging_setup import setup_logging

    setup_logging()

    # At DEBUG, we do NOT pin noisy libs — they inherit root level (DEBUG).
    for name in ("httpcore", "httpx", "urllib3"):
        # Level 0 means "inherit from root"
        assert logging.getLogger(name).level in (logging.NOTSET, logging.DEBUG)
