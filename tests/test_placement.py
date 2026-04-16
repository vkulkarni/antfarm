"""Tests for antfarm.core.placement — placement strategy."""

from __future__ import annotations

from antfarm.core.placement import NodeCapacity, compute_placement


def test_single_node_gets_all():
    """Single node with enough capacity receives all desired workers."""
    nodes = [NodeCapacity(node_id="node-1", max_workers=10, current_workers=0)]
    result = compute_placement({"builder": 4}, nodes)
    assert result == {"node-1": {"builder": 4}}


def test_round_robin_distribution():
    """Workers are distributed evenly across nodes via round-robin."""
    nodes = [
        NodeCapacity(node_id="node-1", max_workers=4, current_workers=0),
        NodeCapacity(node_id="node-2", max_workers=4, current_workers=0),
    ]
    result = compute_placement({"builder": 4}, nodes)
    assert result == {"node-1": {"builder": 2}, "node-2": {"builder": 2}}


def test_respects_max_workers():
    """Placement does not exceed a node's max_workers cap."""
    nodes = [NodeCapacity(node_id="node-1", max_workers=2, current_workers=0)]
    result = compute_placement({"builder": 5}, nodes)
    assert result == {"node-1": {"builder": 2}}


def test_unreachable_node_skipped():
    """Unreachable nodes receive no workers."""
    nodes = [
        NodeCapacity(node_id="node-1", max_workers=10, current_workers=0, reachable=False),
        NodeCapacity(node_id="node-2", max_workers=10, current_workers=0, reachable=True),
    ]
    result = compute_placement({"builder": 3}, nodes)
    assert "node-1" not in result
    assert result == {"node-2": {"builder": 3}}


def test_over_capacity():
    """When desired exceeds total capacity, only capacity-worth are placed."""
    nodes = [
        NodeCapacity(node_id="node-1", max_workers=3, current_workers=0),
        NodeCapacity(node_id="node-2", max_workers=3, current_workers=0),
    ]
    result = compute_placement({"builder": 10}, nodes)
    total = sum(roles.get("builder", 0) for roles in result.values())
    assert total == 6


def test_deterministic():
    """Same inputs always produce identical output."""
    nodes = [
        NodeCapacity(node_id="node-2", max_workers=4, current_workers=0),
        NodeCapacity(node_id="node-1", max_workers=4, current_workers=0),
    ]
    desired = {"builder": 3, "reviewer": 2}
    result1 = compute_placement(desired, nodes)
    result2 = compute_placement(desired, nodes)
    assert result1 == result2


def test_empty_nodes():
    """No reachable nodes produces empty placement."""
    result = compute_placement({"builder": 5}, [])
    assert result == {}

    # All unreachable
    nodes = [
        NodeCapacity(node_id="node-1", max_workers=10, current_workers=0, reachable=False),
    ]
    result = compute_placement({"builder": 5}, nodes)
    assert result == {}
