"""Backend factory for Antfarm.

Usage:
    from antfarm.core.backends import get_backend

    backend = get_backend("file", root=".antfarm")
    backend = get_backend("github", repo="owner/repo", token="ghp_...")
"""

from .base import TaskBackend
from .file import FileBackend
from .github import GitHubBackend


def get_backend(backend_type: str, **kwargs) -> TaskBackend:
    """Instantiate and return a TaskBackend by type name.

    Args:
        backend_type: Backend identifier. Supports 'file' and 'github'.
        **kwargs: Passed to the backend constructor.

    Returns:
        A TaskBackend instance.

    Raises:
        ValueError: If backend_type is not recognized.
    """
    if backend_type == "file":
        return FileBackend(**kwargs)
    if backend_type == "github":
        return GitHubBackend(**kwargs)
    raise ValueError(f"Unknown backend type: '{backend_type}'. Supported: 'file', 'github'")


__all__ = ["get_backend", "TaskBackend", "FileBackend", "GitHubBackend"]
