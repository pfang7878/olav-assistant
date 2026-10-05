#!/usr/bin/env python3
"""``topology_view`` — DB → (filtered adjacency table, device metadata).

Single combined helper for the renderer workflow:

  1. Query ``netops.topology_links`` / BGP / OSPF view with the requested
     filters (center+hops, role, site, protocol l2/bgp/ospf).
  2. Pull ``netops.devices`` metadata for every host that appears in the
     filtered adjacency rows.
  3. Return both pieces, ready to pass to ``render_topology_drawio`` /
     ``render_topology_mermaid``.

Why one script instead of two:

  * Eliminates the "agent forgot to call the metadata helper" failure
    mode — every renderer call gets fully-enriched node labels for free.
  * The two queries hit the same DuckDB connection (one open / one close
    is cheaper than two).

The renderer is still pure transformer — it doesn't read the DB; it
just consumes whatever the caller produces here.
"""
from __future__ import annotations


def topology_view(
    db_path: str,
    *,
    protocol: str = "l2_cdp",
    center: str | None = None,
    hops: int = 2,
    roles: list[str] | None = None,
    sites: list[str] | None = None,
    name_like: str | None = None,
    snapshot_id: str | None = None,
) -> dict:
    """Build a filtered topology adjacency table + device metadata.

    Args:
        db_path:     Filesystem path to ``main.duckdb``.
        protocol:    ``"l2_cdp"`` (default) / ``"bgp"`` / ``"ospf"`` —
                     selects which netops view to query.
        center:      Hostname to centre a BFS view on.  When set, only
                     edges where **both** endpoints are within ``hops``
                     of ``center`` are kept (local-cluster view).
        hops:        BFS radius (default 2).  Ignored when center is None.
        roles:       Restrict source devices by ``netops.devices.role``
                     (e.g. ``["border", "core"]``).
        sites:       Restrict source devices by ``netops.devices.site``.
        name_like:   SQL LIKE pattern on the source-host column
                     (e.g. ``"QS4-2P1-%"``).
        snapshot_id: Optional snapshot filter — applied when the source
                     view has a ``snapshot_id`` column.

    Returns:
        ``{
            "table":    "Markdown adjacency table",
            "metadata": {hostname: {model, ip, role, vendor, ...}},
            "hosts":    int,    # distinct hosts in the table
            "edges":    int,    # data rows in the table
        }``

        ``table`` is empty (header only) when the source view is missing
        or the filter result is zero rows — downstream renderers
        degrade gracefully to a "diagram omitted" note.
    """
    from olav.core.topology.filter import (
        build_adjacencies_view,
        build_device_metadata,
    )

    table = build_adjacencies_view(
        db_path,
        protocol=protocol,
        center=center, hops=hops,
        roles=roles, sites=sites,
        name_like=name_like,
        snapshot_id=snapshot_id,
    )

    hosts: set[str] = set()
    for line in table.splitlines():
        if not line.startswith("|") or "Source" in line or "---" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 4:
            if cells[0]:
                hosts.add(cells[0])
            if cells[2]:
                hosts.add(cells[2])

    metadata = build_device_metadata(db_path, hostnames=hosts) if hosts else {}
    edges = sum(
        1 for ln in table.splitlines()
        if ln.startswith("|") and "Source" not in ln and "---" not in ln
    )
    return {
        "table": table,
        "metadata": metadata,
        "hosts": len(hosts),
        "edges": edges,
    }


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = topology_view(**_args)
    print(_json.dumps(result, default=str))
