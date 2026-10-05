#!/usr/bin/env python3
"""query_topology — ARCH-28 agent tool.

Query the topology views (`v_show_ip_bgp_summary_auto`,
`v_show_ip_ospf_neighbor_auto`, `v_l2_links_auto`) and return a typed
``TopologySnapshot`` Pydantic model. Used by the netops orchestrator and
analyzer sub-agent.

The views are built when data lands — Stage 3.7 of a live collection, or
``ingest_snapshot`` for an offline bundle — so this tool does **zero ETL work on
invocation**: just SELECT + Pydantic validate. State values are canonical (``Established`` for BGP,
``Full``/``FULL/DR``/``FULL/BDR`` for OSPF per vendor).

2026-05-10 schema-drift fix: BGP joins v_show_ip_bgp_summary_auto for
local_as/router_id; OSPF reads v_show_ip_ospf_neighbor_auto +
v_show_ip_ospf_interface_brief_auto for area. Old code referenced
``v_ospf_neighbors_auto`` which doesn't exist + column ``device``
which the BGP view exposes as ``device_name``.

2026-05-27 schema-drift fix: ``v_bgp_neighbors_auto`` was a legacy L1
recipe-based view removed in R83; it never existed in ``netops`` schema.
BGP source is now ``netops.v_show_ip_bgp_summary_auto`` directly.
"""

from __future__ import annotations

from typing import Any, Literal

from olav_netops.schemas import (
    BGPSession,
    L2Link,
    OSPFAdjacency,
    TopologySnapshot,
)


def _view_exists(con: Any, view: str) -> bool:
    """True when ``netops.<view>`` is in the catalogue.

    Per-command views exist only for commands the snapshot actually carried,
    so "which layers can I answer about" is a property of the data, not of the
    schema. Asking first turns a missing command into a reported gap instead of
    a ``CatalogException``.
    """
    return bool(con.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'netops' AND table_name = ? LIMIT 1",
        [view],
    ).fetchone())


def _latest_snapshot(con: Any) -> str | None:
    """Return the most recent snapshot_id that has L2 topology data.

    Why L2 instead of BGP: collection runs may produce partial
    snapshots (e.g., a re-run that only refreshed BGP summary). The
    "alphabetically latest" snapshot can therefore be thin — no L2,
    no OSPF, no BGP summary — and the LLM gets a sparse view that
    looks like the network is empty. Picking the latest snapshot
    that actually has L2 rows is a good proxy for "rich, complete
    collection" since L2 (LLDP/CDP) is the broadest data layer.

    2026-05-10 fix: original picked latest from any view, returned
    a thin snapshot on demo7 that hid all L2/OSPF data. Now scans
    L2 view first; falls back to BGP if no L2 snapshot exists.
    """
    try:
        row = con.execute(
            "SELECT snapshot_id FROM netops.v_l2_links_auto "
            "ORDER BY snapshot_id DESC LIMIT 1"
        ).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    # Fallback: any view with rows
    for view in ("v_show_ip_bgp_summary_auto", "v_l2_links_auto"):
        try:
            row = con.execute(
                f"SELECT snapshot_id FROM netops.{view} "
                "ORDER BY snapshot_id DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                return row[0]
        except Exception:
            continue
    return None


def _fetch_bgp(con: Any, snapshot_id: str) -> list[BGPSession]:
    """Fetch BGP sessions from v_show_ip_bgp_summary_auto.

    Cisco IOS puts prefix-count in state_or_prefixes_received when the
    session is Established (e.g. "5"), and a state string when it is not
    (e.g. "Active", "Idle"). Normalise: all-digit value → "Established".
    """
    if not _view_exists(con, "v_show_ip_bgp_summary_auto"):
        return []
    rows = con.execute(
        "SELECT device_name, bgp_neighbor AS neighbor_ip, neighbor_as, "
        "       local_as, router_id, state_or_prefixes_received, NULL AS uptime "
        "FROM netops.v_show_ip_bgp_summary_auto "
        "WHERE snapshot_id = ? "
        "ORDER BY device_name, bgp_neighbor",
        [snapshot_id],
    ).fetchall()
    result = []
    for r in rows:
        raw_state = (r[5] or "").strip()
        state = "Established" if raw_state.isdigit() else raw_state
        result.append(BGPSession(
            device=r[0],
            neighbor_ip=r[1],
            neighbor_as=r[2],
            local_as=r[3],
            router_id=r[4],
            state=state,
            uptime=r[6],
        ))
    return result


def _fetch_ospf(con: Any, snapshot_id: str) -> list[OSPFAdjacency]:
    """Fetch OSPF adjacencies. ARCH fix 2026-05-10: original SQL
    referenced ``v_ospf_neighbors_auto`` which doesn't exist; the real
    view is ``v_show_ip_ospf_neighbor_auto``. Column ``area`` lives on
    a different view (``v_show_ip_ospf_interface_brief_auto``) and is
    joined by interface name; LEFT JOIN keeps the row when interface
    isn't matched (some vendor outputs lack the area column).

    2026-08-17: the area view is **optional**. A snapshot that did not
    collect ``show ip ospf interface brief`` has no such view, and the join
    raised ``CatalogException`` — so ``concept="all"``, the default, crashed
    on any such database, including one collected with olav-collector's own
    ``olav-default`` task, whose command set does not include it. Without the
    view the adjacencies are still returned, with ``area=None``.
    """
    if not _view_exists(con, "v_show_ip_ospf_neighbor_auto"):
        return []
    if _view_exists(con, "v_show_ip_ospf_interface_brief_auto"):
        sql = (
            "SELECT n.device_name, n.neighbor_id, n.ip_address AS neighbor_ip, "
            "       n.interface, i.area, n.state, n.dead_time "
            "FROM netops.v_show_ip_ospf_neighbor_auto n "
            "LEFT JOIN netops.v_show_ip_ospf_interface_brief_auto i "
            "  ON n.device_name = i.device_name "
            " AND n.snapshot_id = i.snapshot_id "
            " AND n.interface   = i.interface "
            "WHERE n.snapshot_id = ? "
            "ORDER BY n.device_name, n.neighbor_id"
        )
    else:
        sql = (
            "SELECT n.device_name, n.neighbor_id, n.ip_address AS neighbor_ip, "
            "       n.interface, NULL AS area, n.state, n.dead_time "
            "FROM netops.v_show_ip_ospf_neighbor_auto n "
            "WHERE n.snapshot_id = ? "
            "ORDER BY n.device_name, n.neighbor_id"
        )
    rows = con.execute(sql, [snapshot_id]).fetchall()
    out: list[OSPFAdjacency] = []
    for r in rows:
        # Vendor casing varies (Cisco IOS prints "Full/DR" while the
        # OspfState literal expects "FULL/DR"). Normalize: if the
        # state contains "/" it's a composite form like "FULL/DR" —
        # uppercase the whole thing; otherwise it's a single word
        # like "Full" — title-case.
        state_raw = (r[5] or "").strip()
        state = state_raw.upper() if "/" in state_raw else state_raw.title()
        # 2-Way special: literal is "2-Way" (title-case form) not "2-WAY"
        if state.replace("-", "").upper() == "2WAY" and "/" not in state_raw:
            state = "2-Way"
        out.append(OSPFAdjacency(
            device=r[0],
            neighbor_id=r[1],
            neighbor_ip=r[2],
            interface=r[3],
            area=r[4],
            state=state,
            dead_time=r[6],
        ))
    return out


def _fetch_l2(con: Any, snapshot_id: str) -> list[L2Link]:
    if not _view_exists(con, "v_l2_links_auto"):
        return []
    rows = con.execute(
        "SELECT source_device, source_interface, destination_device, "
        "destination_interface, discovery_protocol, link_status "
        "FROM netops.v_l2_links_auto "
        "WHERE snapshot_id = ? "
        "ORDER BY source_device, source_interface",
        [snapshot_id],
    ).fetchall()
    return [
        L2Link(
            source_device=r[0],
            source_interface=r[1],
            destination_device=r[2],
            destination_interface=r[3],
            discovery_protocol=r[4],
            link_status=r[5],
        )
        for r in rows
    ]


def query_topology(
    concept: Literal["bgp", "ospf", "l2", "all"] = "all",
    snapshot_id: str | None = None,
) -> dict:
    """Query network topology via ARCH-28 views + Pydantic schema.

    Args:
        concept: Which layer to return. ``"all"`` includes BGP + OSPF + L2.
        snapshot_id: Specific snapshot to query. ``None`` = latest.

    Returns:
        JSON-serializable dict matching ``TopologySnapshot``, plus
        ``absent_layers``: the requested layers whose source view is not in
        this database, i.e. whose command was never collected. An empty
        ``bgp_sessions`` means "no sessions"; an empty one **with** ``"bgp"``
        in ``absent_layers`` means "nobody asked the device". Those are
        different findings and used to look identical.

        State values are canonical ('Established' for BGP; 'Full' /
        'FULL/DR' etc. for OSPF). Interface names keep their vendor canonical
        form (Junos ``ge-0/0/2.0``, Cisco ``Ethernet0/0``).
    """
    import duckdb
    from olav.core.config import MAIN_DB_PATH

    con = duckdb.connect(str(MAIN_DB_PATH), read_only=True)
    try:
        snap = snapshot_id or _latest_snapshot(con)
        if snap is None:
            return TopologySnapshot(snapshot_id="none").model_dump()

        bgp: list[BGPSession] = []
        ospf: list[OSPFAdjacency] = []
        l2: list[L2Link] = []
        absent: list[str] = []

        #: The view each layer cannot be answered without.
        required = {
            "bgp": "v_show_ip_bgp_summary_auto",
            "ospf": "v_show_ip_ospf_neighbor_auto",
            "l2": "v_l2_links_auto",
        }
        for layer, view in required.items():
            if concept in (layer, "all") and not _view_exists(con, view):
                absent.append(layer)

        if concept in ("bgp", "all"):
            bgp = _fetch_bgp(con, snap)
        if concept in ("ospf", "all"):
            ospf = _fetch_ospf(con, snap)
        if concept in ("l2", "all"):
            l2 = _fetch_l2(con, snap)

        out = TopologySnapshot(
            snapshot_id=snap,
            bgp_sessions=bgp,
            ospf_adjacencies=ospf,
            l2_links=l2,
        ).model_dump(mode="json")
        out["absent_layers"] = absent
        return out
    finally:
        con.close()


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = query_topology(**_args)
    print(_json.dumps(result, default=str))
