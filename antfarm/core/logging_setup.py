"""Central logging configuration for antfarm CLI entry points.

Library modules use ``logger = logging.getLogger(__name__)`` and never
touch the root logger. Long-lived CLI commands (``colony``, ``worker
start``, ``runner``) call :func:`setup_logging` on startup so that
``logger.info`` calls actually reach stderr.

Level resolution order:
1. Explicit ``level`` argument passed by the caller.
2. ``ANTFARM_LOG_LEVEL`` environment variable (case-insensitive).
3. Default: ``INFO``.

Library code must never call this function. Tests import library modules
directly and rely on the root logger being unconfigured.
"""

from __future__ import annotations

import logging
import os

_FORMAT = "%(asctime)s %(name)s %(levelname)s: %(message)s"

# Third-party loggers that are too noisy at DEBUG — pin them to WARNING
# unless the user explicitly asks for more via ANTFARM_LOG_LEVEL.
_NOISY_LIBS = ("httpcore", "httpx", "urllib3")


def setup_logging(level: int | str | None = None) -> None:
    """Configure the root logger for a CLI entry point.

    Safe to call multiple times; subsequent calls are no-ops to avoid
    duplicate handlers.

    Args:
        level: Override log level. If None, reads ``ANTFARM_LOG_LEVEL``
            (default INFO).
    """
    root = logging.getLogger()
    if getattr(root, "_antfarm_configured", False):
        return

    if level is None:
        level = os.environ.get("ANTFARM_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = level.upper()

    logging.basicConfig(level=level, format=_FORMAT)
    # basicConfig() is a no-op if the root logger already has a handler
    # (e.g. pytest's LogCaptureHandler). Set the level explicitly so the
    # configured level always takes effect.
    root.setLevel(level)

    # Silence noisy transport libraries unless the user explicitly asks
    # for DEBUG via ANTFARM_LOG_LEVEL=DEBUG.
    user_level = os.environ.get("ANTFARM_LOG_LEVEL", "").upper()
    if user_level != "DEBUG":
        for name in _NOISY_LIBS:
            logging.getLogger(name).setLevel(logging.WARNING)

    root._antfarm_configured = True  # type: ignore[attr-defined]
