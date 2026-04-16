"""Placement strategy for distributing desired worker counts across nodes.

Given a total desired count per role and a list of node capacities, produces
a per-node allocation that respects max_workers caps and skips unreachable nodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NodeCapacity:
    node_id: str
    max_workers: int
    current_workers: int
    capabilities: list[str] = field(default_factory=list)
    reachable: bool = True


def compute_placement(
    desired_total: dict[str, int],
    nodes: list[NodeCapacity],
) -> dict[str, dict[str, int]]:
    """Distribute desired worker counts across nodes.

    Strategy:
    1. Filter to reachable nodes only
    2. For each role, round-robin across nodes with available capacity
    3. Respect per-node max_workers cap (total across all roles)
    4. Deterministic output (sorted node order)

    Returns: {node_id: {role: count}}
    """
    reachable = sorted([n for n in nodes if n.reachable], key=lambda n: n.node_id)
    if not reachable:
        return {}

    # Track allocated count per node (starts at current_workers)
    allocated: dict[str, int] = {n.node_id: n.current_workers for n in reachable}
    result: dict[str, dict[str, int]] = {n.node_id: {} for n in reachable}

    for role in sorted(desired_total.keys()):
        remaining = desired_total[role]
        if remaining <= 0:
            continue

        # Round-robin across nodes with available capacity
        placed_this_pass = True
        while remaining > 0 and placed_this_pass:
            placed_this_pass = False
            for node in reachable:
                if remaining <= 0:
                    break
                if allocated[node.node_id] < node.max_workers:
                    result[node.node_id][role] = result[node.node_id].get(role, 0) + 1
                    allocated[node.node_id] += 1
                    remaining -= 1
                    placed_this_pass = True

    # Remove nodes with no allocations
    return {nid: roles for nid, roles in result.items() if roles}
