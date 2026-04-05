"""JSON file importer for Antfarm.

Reads a JSON array of task dicts from a local file and returns them
ready to POST to the colony /tasks endpoint.
"""

from __future__ import annotations

import json

from antfarm.core.importers.base import TaskImporter


class JsonFileImporter(TaskImporter):
    """Import tasks from a JSON file containing an array of task dicts.

    Args:
        file_path: Path to the JSON file.
    """

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path

    def import_tasks(self) -> list[dict]:
        """Read and return task dicts from the JSON file.

        Returns:
            List of task dicts as parsed from the file.

        Raises:
            ValueError: If the file does not contain a JSON array.
        """
        with open(self.file_path) as f:
            data = json.load(f)

        if not isinstance(data, list):
            raise ValueError(
                f"Expected a JSON array in {self.file_path!r}, got {type(data).__name__}"
            )

        return data
