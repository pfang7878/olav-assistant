#!/usr/bin/env python3
"""inspect_blast_radius — What-If reachability impact."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _find_project_root() -> Path:
    p = Path(__file__).resolve().parent
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


sys.path.insert(0, str(_find_project_root() / "src"))

# Eager import to avoid asyncio.gather _ModuleLock deadlocks when
# LangGraph runs multiple inspect_* tools in parallel.
from olav_netops.sim import load_network_model


def inspect_blast_radius(
    remove_devices: list[str] | None = None,
    remove_links: list[list[str]] | None = None,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """
    What-If: simulate device / link failures on the network graph
    and report the connectivity impact.

    Mutates a copy of ``model.graph`` (so the live graph is
    untouched), then computes weakly-connected components +
    isolated nodes.  Use this for "what happens if X fails" or
    "blast radius of removing Y" questions.

    **Validation**: if any requested device or link is NOT in the
    current graph, the tool returns ``validation_warnings`` listing
    the unknown items.  A "no impact" result with non-empty
    ``validation_warnings`` is NOT proof of safety — it means the
    failure scenario could not even be simulated.  Re-check device
    names / link existence first.

    Args:
        remove_devices: List of hostnames to remove from the graph.
            E.g. ``["R3"]`` simulates R3 hard failure.  Optional.
        remove_links: List of link pairs to remove (both directions).
            Each pair is a 2-element list ``["A", "B"]``.  E.g.
            ``[["R2", "R4"]]`` simulates the R2-R4 link going down.
            Optional.

    Returns:
        ``{
            "removed_devices": [...],       # only those actually in graph
            "removed_links": [...],         # only those actually present
            "components": [                 # post-failure component summaries
                {"size": N, "members": [...first 5 nodes...+"…+M more"]},
                ...
            ],
            "isolated_nodes": [...],        # up to 30 zero-degree nodes
            "isolated_total": N,            # USE THIS for counts, not len(isolated_nodes)
            "isolated_truncated": bool,     # True if isolated_nodes list was capped
            "connectivity_loss": {pre_components, post_components},
            "validation_warnings": [...],   # e.g. "device 'X' not in topology graph"
         }``
    """
    import networkx as nx
    model = load_network_model(snapshot=snapshot_id)

    g = model.graph.copy()
    pre_components = nx.number_weakly_connected_components(g)

    requested_devices = list(remove_devices or [])
    requested_links = list(remove_links or [])

    actually_removed_devices: list[str] = []
    actually_removed_links: list[list[str]] = []
    warnings: list[str] = []

    # Validate + remove devices
    for d in requested_devices:
        if d in g.nodes:
            g.remove_node(d)
            actually_removed_devices.append(d)
        else:
            sample = sorted(model.graph.nodes)[:20]
            warnings.append(
                f"device {d!r} is not in the topology graph "
                f"(sample of known devices — first 20 of {len(model.graph.nodes)}: {sample}; "
                f"use inspect_devices() to search by substring)"
            )

    # Validate + remove links — must be a 2-element pair AND have an edge
    # in either direction in the *original* graph (after device removal,
    # endpoints may already be gone, which we report distinctly).
    original = model.graph
    for pair in requested_links:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            warnings.append(f"link {pair!r} is not a valid 2-element pair")
            continue
        u, v = pair
        if u not in original.nodes or v not in original.nodes:
            missing = [x for x in (u, v) if x not in original.nodes]
            warnings.append(
                f"link {pair!r}: endpoint(s) {missing} not in topology graph"
            )
            continue
        if not (original.has_edge(u, v) or original.has_edge(v, u)):
            warnings.append(
                f"link {pair!r} is not present in the topology graph "
                f"(no direct edge between {u} and {v})"
            )
            continue
        # Apply removal in both directions on the working copy
        if g.has_edge(u, v):
            g.remove_edge(u, v)
        if g.has_edge(v, u):
            g.remove_edge(v, u)
        actually_removed_links.append([u, v])

    components = sorted(
        [sorted(c) for c in nx.weakly_connected_components(g)],
        key=lambda c: (-len(c), c[0] if c else ""),
    )
    isolated = sorted(n for n in g.nodes if g.degree(n) == 0)

    # Compact component summaries — returning full node lists for a
    # 2500-node network floods the LLM context (60K+ tokens).  Agents
    # only need counts + a small sample to understand impact.
    _SAMPLE = 5
    component_summaries = [
        {
            "size": len(c),
            "members": c if len(c) <= _SAMPLE else c[:_SAMPLE] + [f"…+{len(c) - _SAMPLE} more"],
        }
        for c in components
    ]

    # Isolated nodes: full list up to 30; summary beyond that.
    _MAX_ISO = 30
    isolated_out = isolated[:_MAX_ISO]

    return {
        "removed_devices": actually_removed_devices,
        "removed_links": actually_removed_links,
        "components": component_summaries,
        "isolated_nodes": isolated_out,
        "isolated_total": len(isolated),
        "isolated_truncated": len(isolated) > _MAX_ISO,
        "connectivity_loss": {
            "pre_components": pre_components,
            "post_components": len(components),
        },
        "validation_warnings": warnings,
    }


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = inspect_blast_radius(**_args)
    print(_json.dumps(result, default=str))
