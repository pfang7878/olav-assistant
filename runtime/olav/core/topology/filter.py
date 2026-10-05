"""Topology view filter — query, BFS, emit Markdown adjacency table.

Three protocols share a single output contract:

| Source | Local Intf | Dest | Remote Intf | Status |

For L2 (CDP/LLDP) ``Local Intf`` / ``Remote Intf`` are interface names; for
BGP they're local/remote IPs; for OSPF they're (interface, neighbor-id).
The renderer (``render_topology_drawio`` / ``render_topology_mermaid``)
remains protocol-agnostic — what the cell *means* is captured by the
``protocol`` parameter passed to the renderer.

Why a separate filter module:

  * Renderers stay pure transformers — no SQL, no DB access.
  * Caller (analyzer / writer / test) decides scope (center+hops,
    role, site) deterministically; result is reproducible.
  * Missing views (e.g. v_show_ip_bgp_neighbors_auto absent because no
    BGP data was ingested) → graceful empty-table return, not crash.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

import duckdb


# ── Data source registry ──────────────────────────────────────────────


@dataclass(frozen=True)
class _SourceSpec:
    """How to query a topology source + map its columns to the universal 5."""

    view: str               # netops.<view-name>
    select_cols: str        # SELECT clause emitting (src, sif, dst, dif, status)
    host_col: str           # column to filter on for ``center`` / ``name_like``
    has_snapshot: bool      # supports snapshot_id filter


PROTOCOL_SOURCES: dict[str, _SourceSpec] = {
    "l2_cdp": _SourceSpec(
        view="netops.topology_links",
        select_cols=(
            "source_device           AS src, "
            "source_interface        AS sif, "
            "destination_device      AS dst, "
            "destination_interface   AS dif, "
            "COALESCE(link_status, 'up') AS status"
        ),
        host_col="source_device",
        has_snapshot=True,
    ),
    "bgp": _SourceSpec(
        view="netops.v_show_ip_bgp_neighbors_auto",
        select_cols=(
            "device_name              AS src, "
            "COALESCE(localhost_ip, '') AS sif, "
            "neighbor                 AS dst, "
            "COALESCE(remote_ip, '')  AS dif, "
            "COALESCE(bgp_state, '')  AS status"
        ),
        host_col="device_name",
        has_snapshot=True,
    ),
    "ospf": _SourceSpec(
        view="netops.v_show_ip_ospf_neighbor_auto",
        select_cols=(
            "device_name              AS src, "
            "COALESCE(interface, '')  AS sif, "
            "neighbor_id              AS dst, "
            "COALESCE(ip_address, '') AS dif, "
            "COALESCE(state, '')      AS status"
        ),
        host_col="device_name",
        has_snapshot=True,
    ),
}


@dataclass(slots=True)
class AdjacencyRecord:
    src: str
    sif: str
    dst: str
    dif: str
    status: str


# ── Filter helpers ────────────────────────────────────────────────────


def _view_exists(conn, qualified: str) -> bool:
    """Return True iff the view OR table at ``schema.name`` exists."""
    schema, name = qualified.split(".", 1) if "." in qualified else ("main", qualified)
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, name],
    ).fetchone()
    if row and row[0]:
        return True
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.views "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, name],
    ).fetchone()
    return bool(row and row[0])


def _fetch_raw(
    conn,
    spec: _SourceSpec,
    *,
    snapshot_id: str | None,
    sites: list[str] | None,
    roles: list[str] | None,
    name_like: str | None,
) -> list[AdjacencyRecord]:
    """Pull adjacency rows from the view, with WHERE filters applied."""
    where: list[str] = []
    params: list[Any] = []

    if snapshot_id and spec.has_snapshot:
        where.append("snapshot_id = ?")
        params.append(snapshot_id)
    if name_like:
        where.append(f"{spec.host_col} LIKE ?")
        params.append(name_like)
    if roles:
        # Join through netops.devices for role filtering.
        # roles applies to the source device only.
        ph = ", ".join("?" * len(roles))
        where.append(
            f"{spec.host_col} IN ("
            f"  SELECT hostname FROM netops.devices WHERE role IN ({ph})"
            f")"
        )
        params.extend(roles)
    if sites:
        ph = ", ".join("?" * len(sites))
        where.append(
            f"{spec.host_col} IN ("
            f"  SELECT hostname FROM netops.devices WHERE site IN ({ph})"
            f")"
        )
        params.extend(sites)

    sql = f"SELECT {spec.select_cols} FROM {spec.view}"
    if where:
        sql += " WHERE " + " AND ".join(where)

    rows: list[AdjacencyRecord] = []
    for r in conn.execute(sql, params).fetchall():
        rows.append(AdjacencyRecord(
            src=r[0] or "", sif=r[1] or "",
            dst=r[2] or "", dif=r[3] or "",
            status=r[4] or "",
        ))
    return rows


def _bfs_filter(
    rows: list[AdjacencyRecord], center: str, hops: int
) -> list[AdjacencyRecord]:
    """Keep only rows whose **both** endpoints are within ``hops`` of ``center``.

    "Both endpoints" produces the local-cluster view operators want:

      * hops=0 → just the centre (no edges; center has no in-radius peer)
      * hops=1 → centre + immediate neighbours + edges among them
      * hops=2 → adds neighbours-of-neighbours and their interconnect edges

    "Either endpoint" semantics would leak edges that escape the radius
    (the leaf-of-leaf hanging off an in-radius node), which is rarely
    what the user means by "N-hop view".
    """
    if hops < 0:
        return []
    adj: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        adj[r.src].add(r.dst)
        adj[r.dst].add(r.src)
    if center not in adj:
        return []
    in_radius: set[str] = {center}
    frontier = {center}
    for _ in range(hops):
        next_frontier = set()
        for n in frontier:
            for nb in adj.get(n, ()):
                if nb not in in_radius:
                    next_frontier.add(nb)
                    in_radius.add(nb)
        frontier = next_frontier
        if not frontier:
            break
    return [r for r in rows if r.src in in_radius and r.dst in in_radius]


# ── Public entry point ────────────────────────────────────────────────


def _row_to_md(r: AdjacencyRecord) -> str:
    return f"| {r.src} | {r.sif} | {r.dst} | {r.dif} | {r.status} |"


_TABLE_HEADER = (
    "| Source | Local Intf | Dest | Remote Intf | Status |\n"
    "|---|---|---|---|---|"
)


def build_adjacencies_view(
    db_path: str | Path,
    *,
    protocol: Literal["l2_cdp", "bgp", "ospf"] = "l2_cdp",
    center: str | None = None,
    hops: int = 2,
    roles: list[str] | None = None,
    sites: list[str] | None = None,
    name_like: str | None = None,
    snapshot_id: str | None = None,
) -> str:
    """Build a filtered Markdown adjacency table from the netops DB.

    Args:
        db_path:     Path to main.duckdb.
        protocol:    ``"l2_cdp"`` (default) reads topology_links;
                     ``"bgp"`` reads v_show_ip_bgp_neighbors_auto;
                     ``"ospf"`` reads v_show_ip_ospf_neighbor_auto.
        center:      Hostname to centre the BFS view on.  When set, only
                     rows whose either endpoint is within ``hops`` of
                     ``center`` are kept.
        hops:        BFS radius around ``center`` (default 2). Ignored
                     when ``center`` is None.
        roles:       Filter ``source_device`` by ``netops.devices.role``.
        sites:       Filter ``source_device`` by ``netops.devices.site``.
        name_like:   SQL LIKE pattern on the source-host column (e.g.
                     ``"QS4-2P1-%"``).
        snapshot_id: Optional snapshot filter — applied when the source
                     view has a ``snapshot_id`` column.

    Returns:
        A Markdown table string in the §3 contract (5 columns).  When the
        source view is missing or the filter result is empty, returns
        just the header (downstream renderers degrade to "diagram
        omitted").
    """
    spec = PROTOCOL_SOURCES.get(protocol)
    if spec is None:
        raise ValueError(f"unknown protocol: {protocol!r}")

    with duckdb.connect(str(db_path), read_only=True) as conn:
        if not _view_exists(conn, spec.view):
            return _TABLE_HEADER + "\n"

        rows = _fetch_raw(
            conn, spec,
            snapshot_id=snapshot_id,
            sites=sites, roles=roles,
            name_like=name_like,
        )

    if center:
        rows = _bfs_filter(rows, center, hops)

    if not rows:
        return _TABLE_HEADER + "\n"

    lines = [_TABLE_HEADER]
    lines.extend(_row_to_md(r) for r in rows)
    return "\n".join(lines) + "\n"


def build_device_metadata(
    db_path: str | Path,
    *,
    hostnames: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build the ``device_metadata`` dict expected by ``render_topology_drawio``.

    Pulls ``netops.devices`` once and shapes the result so the renderer
    can drop in model + IP + role for every node it sees.

    Args:
        db_path:    Path to main.duckdb.
        hostnames:  Optional restrict — only these hostnames are returned.
                    When None, the entire ``netops.devices`` table is
                    serialised (cheap; rarely >1k rows in practice).

    Returns:
        ``{hostname: {model, os_version, vendor, ip, role, site}}``.
    """
    with duckdb.connect(str(db_path), read_only=True) as conn:
        if not _view_exists(conn, "netops.devices"):
            return {}
        if hostnames:
            host_list = list(hostnames)
            if not host_list:
                return {}
            ph = ", ".join("?" * len(host_list))
            rows = conn.execute(
                "SELECT hostname, model, os_version, vendor, ip_address, role, site "
                f"FROM netops.devices WHERE hostname IN ({ph})",
                host_list,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT hostname, model, os_version, vendor, ip_address, role, site "
                "FROM netops.devices"
            ).fetchall()

    out: dict[str, dict[str, Any]] = {}
    for h, model, osv, vendor, ip, role, site in rows:
        out[h] = {
            "model": model,
            "os_version": osv,
            "vendor": vendor,
            "ip": ip or "",
            "role": role,
            "site": site,
        }
    return out
