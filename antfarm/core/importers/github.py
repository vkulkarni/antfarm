"""GitHub Issues importer for Antfarm.

Lists open issues from a GitHub repository and maps them to task dicts.
Uses the GitHub REST API v3 via httpx.
"""

from __future__ import annotations

import httpx

from antfarm.core.importers.base import TaskImporter


class GitHubImporter(TaskImporter):
    """Import open issues from a GitHub repository as tasks.

    Args:
        repo: Repository in 'owner/name' format (e.g. 'antfarm-ai/antfarm').
        token: GitHub personal access token (optional for public repos).
        label: Filter issues by label (optional).
    """

    def __init__(
        self,
        repo: str,
        token: str | None = None,
        label: str | None = None,
    ) -> None:
        self.repo = repo
        self.token = token
        self.label = label

    def import_tasks(self) -> list[dict]:
        """Fetch open GitHub issues and map them to task dicts.

        Returns:
            List of task dicts with title, spec, and touches from labels.
        """
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        params: dict = {"state": "open", "per_page": 100}
        if self.label:
            params["labels"] = self.label

        url = f"https://api.github.com/repos/{self.repo}/issues"
        response = httpx.get(url, headers=headers, params=params)
        response.raise_for_status()

        tasks = []
        for issue in response.json():
            # Skip pull requests (GitHub returns them in issues endpoint)
            if "pull_request" in issue:
                continue

            touches = [lbl["name"] for lbl in issue.get("labels", [])]
            body = issue.get("body") or ""
            tasks.append({
                "title": issue["title"],
                "spec": body,
                "touches": touches,
            })

        return tasks
