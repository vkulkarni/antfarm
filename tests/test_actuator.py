"""Tests for antfarm.core.actuator — Actuator ABC and implementations."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from antfarm.core.actuator import LocalActuator, RemoteActuator

# ---------------------------------------------------------------------------
# LocalActuator
# ---------------------------------------------------------------------------


def _make_local_actuator():
    autoscaler = MagicMock()
    autoscaler._count_actual.return_value = {"builder": 1, "reviewer": 0}
    autoscaler.managed = {}
    return LocalActuator(autoscaler), autoscaler


def test_local_actuator_apply():
    """LocalActuator.apply delegates to _reconcile_role for each role."""
    act, autoscaler = _make_local_actuator()
    desired = {"planner": 1, "builder": 3, "reviewer": 1}

    act.apply("http://localhost:7434", desired, generation=5)

    assert autoscaler._reconcile_role.call_count == 3
    calls = {c.args[0]: c.args for c in autoscaler._reconcile_role.call_args_list}
    assert calls["planner"] == ("planner", 1, 0)
    assert calls["builder"] == ("builder", 3, 1)
    assert calls["reviewer"] == ("reviewer", 1, 0)


def test_local_actuator_always_reachable():
    """LocalActuator.is_reachable always returns True."""
    act, _ = _make_local_actuator()
    assert act.is_reachable("http://localhost:7434") is True
    assert act.is_reachable("") is True
    assert act.is_reachable(None) is True


# ---------------------------------------------------------------------------
# RemoteActuator
# ---------------------------------------------------------------------------


def test_remote_actuator_apply():
    """RemoteActuator.apply sends PUT with correct payload."""
    act = RemoteActuator(timeout=5.0)
    with patch("httpx.put") as mock_put:
        act.apply("http://node-2:7434", {"builder": 2}, generation=7)

    mock_put.assert_called_once_with(
        "http://node-2:7434/desired-state",
        json={"generation": 7, "desired": {"builder": 2}, "drain": []},
        timeout=5.0,
    )


def test_remote_actuator_get_actual():
    """RemoteActuator.get_actual returns parsed JSON from runner."""
    act = RemoteActuator(timeout=5.0)
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "workers": {"auto-builder-1": {"role": "builder", "pid": 123}},
        "applied_generation": 3,
    }

    with patch("httpx.get", return_value=mock_response) as mock_get:
        result = act.get_actual("http://node-2:7434")

    mock_get.assert_called_once_with(
        "http://node-2:7434/actual-state",
        timeout=5.0,
    )
    assert result["applied_generation"] == 3
    assert "auto-builder-1" in result["workers"]


def test_remote_actuator_unreachable():
    """RemoteActuator.is_reachable returns False on connection error."""
    act = RemoteActuator()

    # Empty URL
    assert act.is_reachable("") is False

    # Connection error
    import httpx

    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        assert act.is_reachable("http://dead-node:7434") is False
