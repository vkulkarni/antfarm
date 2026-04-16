from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("antfarm")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0"
