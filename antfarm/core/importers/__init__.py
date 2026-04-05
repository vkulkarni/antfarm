"""Task importers for Antfarm.

Provides pluggable importers (GitHub Issues, JSON file) that produce
task dicts compatible with the /tasks POST endpoint.
"""

from antfarm.core.importers.base import TaskImporter
from antfarm.core.importers.github import GitHubImporter
from antfarm.core.importers.json_file import JsonFileImporter

__all__ = ["TaskImporter", "GitHubImporter", "JsonFileImporter"]
