"""Backend factory for Antfarm.

Usage:
    from antfarm.core.backends import get_backend

    backend = get_backend("file", root=".antfarm")
"""

from .base import TaskBackend
from .file import FileBackend


def get_backend(backend_type: str, **kwargs) -> TaskBackend:
    """Instantiate and return a TaskBackend by type name.

    Args:
        backend_type: Backend identifier. Currently supports 'file'.
        **kwargs: Passed to the backend constructor.

    Returns:
        A TaskBackend instance.

    Raises:
        ValueError: If backend_type is not recognized.
    """
    if backend_type == "file":
        return FileBackend(**kwargs)
    raise ValueError(f"Unknown backend type: '{backend_type}'. Supported: 'file'")


__all__ = ["get_backend", "TaskBackend", "FileBackend"]
