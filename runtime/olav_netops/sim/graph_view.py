"""Unified network graph view + consolidated device facts.

R-AGENT-HIERARCHY post-Phase-D (2026-05-09): the original sim agent
(2026-03-01 ``ops-routing-simulator``) required the LLM to write 30+
lines of NetworkX boilerplate every run — load topology_links, layer
in OSPF, layer in BGP, build ip_index, etc.  That's template code,
not LLM-grade work, and small models stumble on it.

This module provides two helpers consumed by the sandbox prologue:

* :func:`build_unified_graph` — returns a ``networkx.DiGraph`` with
  nodes = devices and edges enriched with L2 link metadata + OSPF
  state + BGP session attributes.  One graph, multi-layer attrs.

* :func:`build_device_facts` — returns a dict keyed by hostname with
  consolidated facts: ``platform``, ``loopback``, ``local_as``,
  ``mgmt_ip``, ``role``.  Uses the same 3-tier resolution that
  ``tcf_writer._db_facts`` does (devices.metadata → BGP view →
  peer-neighbor cross-resolve).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# ────────────────────────────────────────────────────────────────────
# build_device_facts
# ────────────────────────────────────────────────────────────────────


def build_device_facts(
    db_path: Path | str | None = None,
    snapshot: str | None = None,
    scope: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Consolidated per-device facts in a single dict.

    Args:
        db_path: DuckDB main.duckdb path. Defaults to
            ``olav.core.config.MAIN_DB_PATH``.
        snapshot: Snapshot id to pin facts to.  ``None`` →
            ``MAX(snapshot_id)`` per source view.
        scope: Restrict to these hostnames; ``None`` means all
            devices.

    Returns:
        ``{hostname: {platform, loopback, local_as, mgmt_ip, role}}``.
        Missing values are ``None``.  Empty dict on DB failure.
    """
    import duckdb  # noqa: PLC0415

    if db_path is None:
        try:
            from olav.core.config import MAIN_DB_PATH
            db_path = MAIN_DB_PATH
        except Exception:
            db_path = ".olav/databases/main.duckdb"
    db_path = str(db_path)

    facts: dict[str, dict[str, Any]] = {}
    try:
        con = duckdb.connect(db_path, read_only=True)
    except Exception:
        return facts

    try:
        # 1. Base inventory from netops.devices
        sql = (
            "SELECT hostname, platform, ip_address, role, metadata "
            "FROM netops.devices"
        )
        params: list[Any] = []
        if scope:
            placeholders = ",".join("?" for _ in scope)
            sql += f" WHERE hostname IN ({placeholders})"
            params.extend(scope)
        try:
            for hostname, platform, ip_address, role, metadata in con.execute(
                sql, params
            ).fetchall():
                facts[hostname] = {
                    "platform": platform,
                    "loopback": None,
                    "local_as": None,
                    "mgmt_ip": ip_address,
                    "role": role or None,
                }
                if metadata:
                    try:
                        md = (
                            json.loads(metadata)
                            if isinstance(metadata, str)
                            else metadata
                        )
                        if isinstance(md, dict) and md.get("loopback_ip"):
                            facts[hostname]["loopback"] = md["loopback_ip"]
                    except (json.JSONDecodeError, TypeError):
                        pass
        except Exception:
            pass

        if not facts:
            return facts

        # 2. ASN + router_id (loopback fallback) from BGP summary view
        try:
            for hostname, router_id, local_as in con.execute(
                "SELECT device_name, router_id, local_as "
                "FROM netops.v_show_ip_bgp_summary_auto "
                "WHERE local_as IS NOT NULL"
            ).fetchall():
                if hostname not in facts:
                    continue
                if facts[hostname]["local_as"] is None and local_as is not None:
                    try:
                        facts[hostname]["local_as"] = int(local_as)
                    except (TypeError, ValueError):
                        pass
                if facts[hostname]["loopback"] is None and router_id:
                    facts[hostname]["loopback"] = router_id
        except Exception:
            pass

        # 2.5 Raw-text regex fallback for devices STILL missing
        # loopback or local_as.  Junos doesn't appear in
        # ``v_show_ip_bgp_summary_auto`` (Cisco-only view) so for
        # Junos hosts we read raw command output directly.  Regex
        # matches both vendor formats:
        #   Cisco: "Router ID: 1.1.1.1"
        #   Junos: "Router ID: 1.1.1.1" (also "router id" lower-case)
        #   Junos: "Autonomous system number: 65000"
        #   Cisco: "BGP router identifier 1.1.1.1, local AS number 65000"
        try:
            import re as _re
            _ROUTER_ID_RE = _re.compile(
                r"(?:Router\s*ID|router\s*identifier)\s*[:=]?\s*"
                r"(\d+\.\d+\.\d+\.\d+)",
                _re.IGNORECASE,
            )
            _AS_RE = _re.compile(
                r"(?:Autonomous\s+system\s+number|local\s+AS\s+number)"
                r"\s*[:=]?\s*(\d+)",
                _re.IGNORECASE,
            )
            for hostname, f in facts.items():
                if f.get("loopback") is not None and f.get("local_as") is not None:
                    continue
                row = con.execute(
                    "SELECT raw_output FROM netops.raw_output_store "
                    "WHERE device_name = ? "
                    "  AND command IN ('show route summary', "
                    "                   'show ip bgp summary', "
                    "                   'show bgp summary') "
                    "ORDER BY updated_at DESC LIMIT 1",
                    [hostname],
                ).fetchone()
                if not row or not row[0]:
                    continue
                raw = row[0]
                if f.get("loopback") is None:
                    m = _ROUTER_ID_RE.search(raw)
                    if m:
                        f["loopback"] = m.group(1)
                if f.get("local_as") is None:
                    m = _AS_RE.search(raw)
                    if m:
                        try:
                            f["local_as"] = int(m.group(1))
                        except ValueError:
                            pass
        except Exception:
            pass

        # 3. Cross-resolve: a device with no BGP row may appear as a
        #    peer's bgp_neighbor IP.  Match by loopback IP to fill in
        #    Junos devices that don't show in the Cisco-style summary view.
        try:
            known_loopbacks = {
                f["loopback"]: name
                for name, f in facts.items()
                if f.get("loopback")
            }
            if known_loopbacks:
                for _, neighbor, neighbor_as in con.execute(
                    "SELECT device_name, bgp_neighbor, neighbor_as "
                    "FROM netops.v_show_ip_bgp_summary_auto"
                ).fetchall():
                    if neighbor in known_loopbacks:
                        target = known_loopbacks[neighbor]
                        if facts[target]["local_as"] is None:
                            try:
                                facts[target]["local_as"] = int(neighbor_as)
                            except (TypeError, ValueError):
                                pass
        except Exception:
            pass

        # 4. Routing-role heuristic: for devices that LIKELY have BGP
        #    (role contains 'border' / 'core' / 'router' / 'edge') but
        #    don't appear in the Cisco summary view, infer from peers
        #    if there's exactly one unmatched peer-neighbor pair.
        #    Skips access switches / hosts that legitimately don't have
        #    BGP.
        _router_roles = {"border", "core", "router", "edge", "spine", "leaf"}
        try:
            # Set of bgp_neighbor IPs already accounted for in
            # known_loopbacks — these are "matched" peer entries.
            seen_bgp_neighbors: set[str] = set()
            for _, neighbor, _ in con.execute(
                "SELECT device_name, bgp_neighbor, neighbor_as "
                "FROM netops.v_show_ip_bgp_summary_auto"
            ).fetchall():
                if neighbor in known_loopbacks:
                    seen_bgp_neighbors.add(neighbor)

            for target, f in facts.items():
                if f.get("loopback") is not None and f.get("local_as") is not None:
                    continue
                role = (f.get("role") or "").lower()
                if not any(r in role for r in _router_roles):
                    continue  # access switch — leave None
                # Find unmatched peer entries pointing at us
                unmatched = []
                for peer, peer_neighbor, peer_neighbor_as in con.execute(
                    "SELECT device_name, bgp_neighbor, neighbor_as "
                    "FROM netops.v_show_ip_bgp_summary_auto"
                ).fetchall():
                    if (peer_neighbor and peer_neighbor not in seen_bgp_neighbors
                            and peer in facts and facts[peer].get("local_as") is not None):
                        unmatched.append((peer, peer_neighbor, peer_neighbor_as))
                if len(unmatched) == 1:
                    _, n, na = unmatched[0]
                    if f.get("loopback") is None:
                        f["loopback"] = n
                    if f.get("local_as") is None:
                        try:
                            f["local_as"] = int(na)
                        except (TypeError, ValueError):
                            pass
        except Exception:
            pass

    finally:
        try:
            con.close()
        except Exception:
            pass

    return facts


# ────────────────────────────────────────────────────────────────────
# build_unified_graph
# ────────────────────────────────────────────────────────────────────


def build_unified_graph(
    db_path: Path | str | None = None,
    snapshot: str | None = None,
    scope: list[str] | None = None,
):
    """Multi-layer enriched DiGraph for sim's What-If analysis.

    Nodes: device hostnames.  Each node gets attributes consolidated
    from :func:`build_device_facts` (platform / loopback / local_as /
    mgmt_ip / role).

    Edges (directed, both directions for each undirected link so
    asymmetric path queries work):
      * L2 from ``netops.topology_links``.  Edge attrs:
        ``layer="L2"``, ``link_status``, ``protocol``,
        ``source_interface``, ``destination_interface``.
      * OSPF enrichment — for each L2 edge where both endpoints
        report an OSPF neighbour with each other, set
        ``ospf_state`` (e.g. ``Full/DR``).  No new edges.
      * BGP overlay — for each BGP session in
        ``v_show_ip_bgp_summary_auto`` whose neighbour IP matches
        a known loopback, add an edge attr ``bgp_session=True``,
        ``bgp_session_state``, ``bgp_neighbor_as``.

    Args / returns: see signature.  Returns an empty
    ``networkx.DiGraph`` on any DB error.
    """
    import duckdb  # noqa: PLC0415
    import networkx as nx  # noqa: PLC0415

    if db_path is None:
        try:
            from olav.core.config import MAIN_DB_PATH
            db_path = MAIN_DB_PATH
        except Exception:
            db_path = ".olav/databases/main.duckdb"
    db_path = str(db_path)

    g = nx.DiGraph()

    # Pull facts first so node attrs are consistent.
    facts = build_device_facts(db_path=db_path, snapshot=snapshot, scope=scope)
    for device, attrs in facts.items():
        g.add_node(device, **{k: v for k, v in attrs.items() if v is not None})

    try:
        con = duckdb.connect(db_path, read_only=True)
    except Exception:
        return g

    try:
        # ── Snapshot resolution
        if snapshot is None:
            try:
                row = con.execute(
                    "SELECT MAX(snapshot_id) FROM netops.topology_links"
                ).fetchone()
                snapshot = row[0] if row and row[0] else None
            except Exception:
                snapshot = None

        # ── L2 from topology_links
        if snapshot:
            where = ["snapshot_id = ?"]
            params: list[Any] = [snapshot]
            if scope:
                placeholders = ",".join("?" for _ in scope)
                where.append(
                    f"(source_device IN ({placeholders}) "
                    f"OR destination_device IN ({placeholders}))"
                )
                params.extend(scope)
                params.extend(scope)
            try:
                rows = con.execute(
                    "SELECT source_device, source_interface, "
                    "destination_device, destination_interface, "
                    "discovery_protocol, link_status "
                    "FROM netops.topology_links WHERE "
                    + " AND ".join(where),
                    params,
                ).fetchall()
            except Exception:
                rows = []
            for src, si, dst, di, proto, status in rows:
                if not src or not dst:
                    continue
                g.add_edge(src, dst, layer="L2",
                           source_interface=si,
                           destination_interface=di,
                           protocol=proto,
                           link_status=status or "unknown")
                # Mirror to make distance/path queries work both ways.
                g.add_edge(dst, src, layer="L2",
                           source_interface=di,
                           destination_interface=si,
                           protocol=proto,
                           link_status=status or "unknown")

        # ── OSPF enrichment: tag existing edges where both endpoints
        # show an adjacency to each other's loopback / interface IP
        loop_to_dev = {
            attrs["loopback"]: dev
            for dev, attrs in facts.items()
            if attrs.get("loopback")
        }
        try:
            for device, neighbor_id, state in con.execute(
                "SELECT device_name, neighbor_id, state "
                "FROM netops.v_show_ip_ospf_neighbor_auto"
            ).fetchall():
                peer = loop_to_dev.get(neighbor_id)
                if peer and g.has_edge(device, peer):
                    g[device][peer]["ospf_state"] = state
                if peer and g.has_edge(peer, device):
                    g[peer][device]["ospf_state"] = state
        except Exception:
            pass

        # ── BGP overlay: for every session in summary view, tag the
        # corresponding edge if peer loopback is known.
        try:
            for device, bgp_neighbor, neighbor_as, state_or_pfx in con.execute(
                "SELECT device_name, bgp_neighbor, neighbor_as, "
                "state_or_prefixes_received "
                "FROM netops.v_show_ip_bgp_summary_auto"
            ).fetchall():
                peer = loop_to_dev.get(bgp_neighbor)
                if not peer:
                    continue
                # Determine session state — Cisco summary view encodes
                # state in state_or_prefixes_received: numeric →
                # Established, alpha (e.g. "Idle") → that state.
                if (state_or_pfx or "").strip().isdigit():
                    bgp_state = "Established"
                elif state_or_pfx:
                    bgp_state = str(state_or_pfx)
                else:
                    bgp_state = "Unknown"
                if g.has_edge(device, peer):
                    g[device][peer]["bgp_session"] = True
                    g[device][peer]["bgp_session_state"] = bgp_state
                    g[device][peer]["bgp_neighbor_as"] = (
                        int(neighbor_as) if neighbor_as is not None else None
                    )
        except Exception:
            pass
    finally:
        try:
            con.close()
        except Exception:
            pass

    return g
