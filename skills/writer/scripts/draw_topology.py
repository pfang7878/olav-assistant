#!/usr/bin/env python3
"""``draw_topology`` — one-shot: scoped DB adjacency → rendered diagram file.

Fat script (ADR: "Fat tools for N-item work"). Does the whole topology
pipeline in a single call so the small-model agent never has to hold the
rendered XML/Mermaid in context (a 20 KB draw.io ``<mxfile>`` copied through
an LLM tool-arg round-trip is the classic small-model corruption failure):

  1. ``topology_view`` — query a SCOPED adjacency table + device metadata.
  2. ``render_topology_drawio`` / ``render_topology_mermaid`` — table → markup.
  3. Write the markup to ``exports/diagrams/<filename>.<ext>``.

Returns only ``{path, hosts, edges, scope, format}`` — no markup in context.

``db_path`` self-resolves (OLAV_DB_PATH env → platform MAIN_DB_PATH), so the
agent only supplies the SCOPE (name_like / center / hops) + output filename.
Callers MUST scope: an unscoped whole-fabric render is thousands of nodes and
unreadable; this script refuses a result wider than ``max_hosts`` (default 120)
and tells the agent to tighten the scope.
"""
from __future__ import annotations

import sys
from pathlib import Path


def draw_topology(
    *,
    name_like: str | None = None,
    center: str | None = None,
    hops: int = 2,
    protocol: str = "l2_cdp",
    roles: list[str] | None = None,
    sites: list[str] | None = None,
    diagram_format: str = "drawio",
    filename: str | None = None,
    snapshot_id: str | None = None,
    db_path: str | None = None,
    max_hosts: int = 120,
) -> dict:
    """Render a scoped network topology to a diagram file.

    Args:
        name_like:   SQL LIKE on hostname to scope the graph (e.g. ``'%core%'``,
                     ``'%dist%'``, ``'%alpha%'``). The main scoping lever.
        center:      Alternative scope: centre a BFS view on this hostname.
        hops:        BFS radius when ``center`` is set (default 2).
        protocol:    ``'l2_cdp'`` (default) / ``'bgp'`` / ``'ospf'``.
        roles/sites: Optional extra filters (usually unused — role data may be
                     absent).
        diagram_format: ``'drawio'`` (default) or ``'mermaid'``.
        filename:    Output basename (no extension). Defaults to
                     ``<scope>_topology_<date>``.
        snapshot_id: Optional snapshot pin (default: current view state).
        db_path:     Override; self-resolves from env / platform default.
        max_hosts:   Refuse to render a graph wider than this (scope too broad).

    Returns:
        ``{"status": "ok", "path", "hosts", "edges", "scope", "format"}`` on
        success, or ``{"status": "error"/"too_wide", "message", ...}``.
    """
    import os
    from datetime import datetime

    # Locate the sibling helper scripts regardless of CWD.
    _here = Path(__file__).resolve().parent
    if str(_here) not in sys.path:
        sys.path.insert(0, str(_here))
    import render_topology_drawio as _rd
    import render_topology_mermaid as _rm
    import topology_view_filter as _tv

    # Resolve db_path: explicit arg → env → platform default.
    if not db_path or db_path.startswith("<"):
        db_path = os.environ.get("OLAV_DB_PATH")
    if not db_path:
        from olav.core.config import MAIN_DB_PATH
        db_path = str(MAIN_DB_PATH)

    if not name_like and not center and not roles and not sites:
        return {
            "status": "error",
            "message": (
                "no scope given — draw_topology needs name_like (e.g. '%core%'), "
                "center=<device>, or roles/sites. Refusing to render the whole "
                "fabric."
            ),
        }

    res = _tv.topology_view(
        db_path,
        protocol=protocol,
        center=center,
        hops=hops,
        roles=roles,
        sites=sites,
        name_like=name_like,
        snapshot_id=snapshot_id,
    )
    hosts, edges = res.get("hosts", 0), res.get("edges", 0)
    scope = name_like or (f"around {center} ({hops} hops)" if center else None) \
        or (f"roles={roles}" if roles else None) or (f"sites={sites}" if sites else "?")

    if hosts == 0 or edges == 0:
        return {
            "status": "error",
            "message": f"scope '{scope}' matched no topology links — widen or "
            f"check the hostname pattern.",
            "hosts": hosts, "edges": edges, "scope": scope,
        }
    if hosts > max_hosts:
        return {
            "status": "too_wide",
            "message": f"scope '{scope}' → {hosts} nodes (> {max_hosts}). Tighten "
            f"name_like or use center=<hub device> hops=1 for a readable diagram.",
            "hosts": hosts, "edges": edges, "scope": scope,
        }

    if diagram_format == "mermaid":
        markup = _rm.render_topology_mermaid(res["table"])
        ext = "md"
    else:
        markup = _rd.render_topology_drawio(res["table"], device_metadata=res["metadata"])
        ext = "drawio"

    # Resolve exports/diagrams/ under the project root.
    try:
        from olav.core.config import get_paths_config
        root = get_paths_config().project_root
    except Exception:  # noqa: BLE001
        root = Path.cwd()
    out_dir = Path(root) / "exports" / "diagrams"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not filename:
        tag = (name_like or center or "topology").strip("%").replace("/", "_") or "topology"
        filename = f"{tag}_topology_{datetime.now().strftime('%Y-%m-%d')}"
    out_path = out_dir / f"{filename}.{ext}"
    out_path.write_text(markup, encoding="utf-8")

    return {
        "status": "ok",
        "path": str(out_path),
        "hosts": hosts,
        "edges": edges,
        "scope": scope,
        "format": diagram_format,
    }


if __name__ == "__main__":
    import json as _json

    _args = _json.loads(sys.stdin.read() or "{}")
    print(_json.dumps(draw_topology(**_args), default=str))
