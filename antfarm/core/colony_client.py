"""Thin synchronous httpx wrapper for the Antfarm Colony API."""


import httpx


class ColonyClient:
    """HTTP client for communicating with the Colony API server."""

    def __init__(
        self,
        base_url: str,
        client: httpx.Client | None = None,
        token: str | None = None,
    ):
        """Initialize client.

        Args:
            base_url: Colony server URL (e.g., "http://localhost:7433")
            client: Optional httpx.Client for dependency injection in tests.
            token: Optional bearer token for authentication.
        """
        self.base_url = base_url.rstrip("/")
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(
            base_url=self.base_url, timeout=30.0, headers=headers
        )
        self._owns_client = client is None  # only close if we created it

    def register_node(self, node_id: str) -> dict:
        r = self._client.post("/nodes", json={"node_id": node_id})
        r.raise_for_status()
        return r.json()

    def register_worker(
        self,
        worker_id: str,
        node_id: str,
        agent_type: str,
        workspace_root: str,
        capabilities: list[str] | None = None,
    ) -> dict:
        r = self._client.post("/workers/register", json={
            "worker_id": worker_id,
            "node_id": node_id,
            "agent_type": agent_type,
            "workspace_root": workspace_root,
            "capabilities": capabilities or [],
        })
        r.raise_for_status()
        return r.json()

    def deregister_worker(self, worker_id: str) -> None:
        r = self._client.delete(f"/workers/{worker_id}")
        r.raise_for_status()

    def heartbeat(
        self,
        worker_id: str,
        status: dict | None = None,
        remaining: int | None = None,
        reset_at: str | None = None,
        cooldown_until: str | None = None,
    ) -> None:
        payload: dict = {"status": status or {}}
        if remaining is not None:
            payload["remaining"] = remaining
        if reset_at is not None:
            payload["reset_at"] = reset_at
        if cooldown_until is not None:
            payload["cooldown_until"] = cooldown_until
        r = self._client.post(f"/workers/{worker_id}/heartbeat", json=payload)
        r.raise_for_status()

    def list_workers(self) -> list[dict]:
        """List all registered workers with their rate limit state."""
        r = self._client.get("/workers")
        r.raise_for_status()
        return r.json()

    def forage(self, worker_id: str) -> dict | None:
        """Claim next task. Returns task dict or None if queue empty (204)."""
        r = self._client.post("/tasks/pull", json={"worker_id": worker_id})
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def trail(self, task_id: str, worker_id: str, message: str) -> None:
        r = self._client.post(f"/tasks/{task_id}/trail", json={
            "worker_id": worker_id,
            "message": message,
        })
        r.raise_for_status()

    def signal(self, task_id: str, worker_id: str, message: str) -> None:
        r = self._client.post(f"/tasks/{task_id}/signal", json={
            "worker_id": worker_id,
            "message": message,
        })
        r.raise_for_status()

    def harvest(self, task_id: str, attempt_id: str, pr: str, branch: str) -> None:
        r = self._client.post(f"/tasks/{task_id}/harvest", json={
            "attempt_id": attempt_id,
            "pr": pr,
            "branch": branch,
        })
        r.raise_for_status()

    def kickback(self, task_id: str, reason: str) -> None:
        r = self._client.post(f"/tasks/{task_id}/kickback", json={"reason": reason})
        r.raise_for_status()

    def mark_merged(self, task_id: str, attempt_id: str) -> None:
        r = self._client.post(f"/tasks/{task_id}/merge", json={"attempt_id": attempt_id})
        r.raise_for_status()

    def list_tasks(self, status: str | None = None) -> list[dict]:
        params = {"status": status} if status else {}
        r = self._client.get("/tasks", params=params)
        r.raise_for_status()
        return r.json()

    def get_task(self, task_id: str) -> dict | None:
        r = self._client.get(f"/tasks/{task_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def status(self) -> dict:
        r = self._client.get("/status")
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
