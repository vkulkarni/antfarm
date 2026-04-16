"""Actuator abstraction for applying desired worker state to nodes.

Actuators bridge the autoscaler's desired-state decisions to concrete
node-level actions. LocalActuator wraps the existing subprocess management;
RemoteActuator pushes state to a Runner daemon over HTTP.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from antfarm.core.autoscaler import Autoscaler


class Actuator(ABC):
    """Interface for applying desired worker state to a node.

    Methods receive runner_url directly -- the autoscaler reads URLs from
    backend Node records (single source of truth) and passes them through.
    """

    @abstractmethod
    def apply(self, runner_url: str, desired: dict[str, int], generation: int) -> None:
        """Push desired worker counts to the node."""
        ...

    @abstractmethod
    def get_actual(self, runner_url: str) -> dict:
        """Return actual worker state from the node."""
        ...

    @abstractmethod
    def is_reachable(self, runner_url: str) -> bool:
        """Check if the node is reachable."""
        ...


class LocalActuator(Actuator):
    """Wraps existing Autoscaler subprocess management for the local node."""

    def __init__(self, autoscaler: Autoscaler):
        self._autoscaler = autoscaler

    def apply(self, runner_url: str, desired: dict[str, int], generation: int) -> None:
        actual = self._autoscaler._count_actual()
        for role in ("planner", "builder", "reviewer"):
            self._autoscaler._reconcile_role(role, desired.get(role, 0), actual.get(role, 0))

    def get_actual(self, runner_url: str) -> dict:
        return {
            "workers": {
                name: {"role": mw.role, "pid": mw.process.pid}
                for name, mw in self._autoscaler.managed.items()
                if mw.process.poll() is None
            },
            "applied_generation": -1,
        }

    def is_reachable(self, runner_url: str) -> bool:
        return True


class RemoteActuator(Actuator):
    """Pushes desired state to a remote Runner via HTTP.

    Does NOT maintain its own runner_url registry -- URLs are passed in
    by the caller from backend Node records.
    """

    def __init__(self, timeout: float = 10.0):
        self._timeout = timeout

    def apply(self, runner_url: str, desired: dict[str, int], generation: int) -> None:
        import httpx

        httpx.put(
            f"{runner_url}/desired-state",
            json={"generation": generation, "desired": desired, "drain": []},
            timeout=self._timeout,
        )

    def get_actual(self, runner_url: str) -> dict:
        import httpx

        r = httpx.get(f"{runner_url}/actual-state", timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def is_reachable(self, runner_url: str) -> bool:
        if not runner_url:
            return False
        import httpx

        try:
            r = httpx.get(f"{runner_url}/health", timeout=3.0)
            return r.status_code == 200
        except Exception:
            return False
