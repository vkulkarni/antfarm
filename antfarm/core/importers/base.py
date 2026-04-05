"""Base class for Antfarm task importers.

All importers must subclass TaskImporter and implement import_tasks(),
returning a list of task dicts compatible with the colony /tasks endpoint.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TaskImporter(ABC):
    """Abstract base class for task importers.

    Subclasses pull tasks from an external source and return them as
    dicts ready to POST to the colony /tasks endpoint.
    """

    @abstractmethod
    def import_tasks(self) -> list[dict]:
        """Fetch and return tasks from the source.

        Returns:
            List of task dicts with at minimum 'title' and 'spec' keys.
        """
