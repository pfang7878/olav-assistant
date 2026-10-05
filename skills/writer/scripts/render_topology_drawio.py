#!/usr/bin/env python3
"""``render_topology_drawio`` — Markdown adjacency table → draw.io XML.

Companion to ``render_topology_mermaid``: same input contract, same
"no DB access, just transform" guarantee.  Output is a complete
``<mxfile>...</mxfile>`` XML string ready to hand to
``format_and_export(format='drawio')``.

Why a Python script and not LLM-handcraft:
  * Big topologies (>50 nodes) bloat the prompt with mechanical XML
    that the LLM tokenises slowly and gets wrong (forgotten mxCell
    ids, layout coordinate collisions).
  * The XML rules + Cisco stencil map are stable knowledge — code is
    the right place, not prompt.

Stencil-by-hostname-prefix mapping is conservative: anything that
doesn't look like a router or firewall renders as a switch (the most
common case in real fleets).

Layout: BFS-tree from the highest-fanout node.  Each BFS level
becomes one row; columns spread on an 80 px grid.  Good enough for
Confluence-ready diagrams up to ~300 nodes.

See ``core/guides/viz_drawio.guide.yaml`` for the XML schema +
stencil catalog the LLM uses when it needs to hand-craft an
edge case this script doesn't fit.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from html import escape


# ── Stencil mapping ──────────────────────────────────────────────────


_STENCIL_ROUTER = "mxgraph.cisco.routers.router"
_STENCIL_FIREWALL = "mxgraph.cisco.firewalls.firewall"
_STENCIL_SWITCH = "mxgraph.cisco.switches.workgroup_switch"

# Hint regexes — match anywhere in the hostname (split on -_.), since
# operator naming conventions vary (``R1`` at start, ``-router-`` in
# middle, ``-rtr`` suffix all all valid signals).
_ROUTER_HINTS = re.compile(
    r"(?i)(?:^|[-_.])(?:r\d|router|rtr|rt\d|bgp|asr|edge\b|border\b|core\b)"
)
_FIREWALL_HINTS = re.compile(
    r"(?i)(?:^|[-_.])(?:fw\d?|firewall|asa|palo|checkpoint)\b"
)


def _stencil_for(hostname: str, role: str | None = None) -> tuple[str, int]:
    """Return (stencil, height) for a given hostname.

    Caller-supplied ``role`` (from ``netops.devices.role`` /
    ``platform_profiles`` / explicit override) wins over hostname
    heuristics.  Router / firewall are 48px tall, switch is 44px.
    """
    if role:
        r = role.strip().lower()
        if r in ("firewall", "fw", "edge-fw"):
            return _STENCIL_FIREWALL, 48
        if r in ("router", "rtr", "border", "core"):
            return _STENCIL_ROUTER, 48
        if r in ("switch", "access", "distribution", "dist"):
            return _STENCIL_SWITCH, 44
    # Hostname-pattern fallback.
    if _FIREWALL_HINTS.search(hostname):
        return _STENCIL_FIREWALL, 48
    if _ROUTER_HINTS.search(hostname):
        return _STENCIL_ROUTER, 48
    return _STENCIL_SWITCH, 44


def _build_node_label(name: str, meta: dict | None) -> str:
    """Multi-line node label.

    Layout: ``hostname`` on line 1, ``model`` on line 2, ``ip`` on line 3
    (lines omitted when metadata is missing). draw.io interprets ``&#xa;``
    as a literal newline inside an attribute value.
    """
    if not meta:
        return name
    parts = [name]
    model = meta.get("model")
    if model:
        parts.append(str(model))
    ip = meta.get("ip") or meta.get("ip_address") or meta.get("mgmt_ip")
    if ip:
        parts.append(str(ip))
    return "&#xa;".join(escape(p) for p in parts)


# ── Markdown table parsing (mirrors render_topology_mermaid) ─────────


def _safe_id(name: str) -> str:
    """draw.io mxCell ids must be unique strings; sanitise dots/dashes."""
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


def _parse_table(table_md: str) -> list[dict[str, str]]:
    """Parse a GitHub-flavoured Markdown table into list of row-dicts.

    Tolerates leading/trailing whitespace, alignment markers, and
    bold-wrapped cells.  Empty list when no valid table is found.
    """
    lines = [ln.rstrip() for ln in table_md.splitlines() if ln.strip()]
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for ln in lines:
        if not ln.startswith("|"):
            continue
        cells = [c.strip().strip("*") for c in ln.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells, strict=False)))
    return rows


def _find_col(header_row: dict[str, str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in header_row:
            return c
    return None


# ── BFS tree layout ──────────────────────────────────────────────────


_GRID_X = 120
_GRID_Y = 120
_WIDTH = 68


def _layout_tree(nodes: list[str], edges: list[tuple[str, str]]) -> dict[str, tuple[int, int]]:
    """Lay nodes out as a BFS tree from the highest-fanout node.

    Returns ``{node: (x, y)}`` in pixels.  Disconnected components are
    laid out on subsequent rows below the main tree.
    """
    if not nodes:
        return {}

    # Build adjacency (undirected for layout purposes).
    adj: dict[str, set[str]] = defaultdict(set)
    for s, d in edges:
        adj[s].add(d)
        adj[d].add(s)

    # Pick BFS root = highest-fanout node (tie-break alphabetical).
    remaining = set(nodes)
    positions: dict[str, tuple[int, int]] = {}
    row_offset_y = 0

    while remaining:
        root = max(
            remaining,
            key=lambda n: (len(adj.get(n, ())), -ord(n[0]) if n else 0),
        )
        # BFS from root, capturing depth per node.
        depth: dict[str, int] = {root: 0}
        order: list[str] = [root]
        queue = deque([root])
        while queue:
            cur = queue.popleft()
            for nb in sorted(adj.get(cur, ())):
                if nb in remaining and nb not in depth:
                    depth[nb] = depth[cur] + 1
                    order.append(nb)
                    queue.append(nb)

        # Place by (depth, slot-within-depth) onto the 80px grid.
        per_level: dict[int, list[str]] = defaultdict(list)
        for n in order:
            per_level[depth[n]].append(n)
        for level, names in sorted(per_level.items()):
            for col, n in enumerate(names):
                positions[n] = (40 + col * _GRID_X, row_offset_y + 40 + level * _GRID_Y)

        max_depth = max(per_level) if per_level else 0
        row_offset_y += (max_depth + 2) * _GRID_Y
        remaining -= set(order)

    return positions


# ── XML emission ─────────────────────────────────────────────────────


_VERTEX_STYLE_TPL = (
    "shape={stencil};html=1;pointerEvents=1;dashed=0;fillColor=#036897;"
    "strokeColor=#ffffff;strokeWidth=2;verticalLabelPosition=bottom;"
    "verticalAlign=top;align=center;outlineConnect=0;"
)

_HEADER = (
    '<mxfile host="app.diagrams.net">\n'
    '  <diagram name="Topology">\n'
    '    <mxGraphModel grid="1" gridSize="10" guides="1" tooltips="1" connect="1"'
    '                  arrows="1" fold="1" page="1" pageScale="1" pageWidth="850"'
    '                  pageHeight="1100" math="0" shadow="0">\n'
    '      <root>\n'
    '        <mxCell id="0"/>\n'
    '        <mxCell id="1" parent="0"/>\n'
)
_FOOTER = (
    '      </root>\n'
    '    </mxGraphModel>\n'
    '  </diagram>\n'
    '</mxfile>\n'
)


_PROTOCOL_EDGE_OK = {
    "l2_cdp": {"up", "new", ""},
    "bgp":    {"established"},
    "ospf":   {"full", "2way"},
}


def _edge_label(
    sif: str, dif: str, status: str, protocol: str,
) -> str:
    """Compose an edge label per protocol convention."""
    if protocol == "bgp":
        # Local/remote IPs already in sif/dif; surface state explicitly.
        pieces = []
        if sif or dif:
            pieces.append(f"{sif} ↔ {dif}".strip())
        if status:
            pieces.append(f"({status})")
        return " ".join(pieces)
    if protocol == "ospf":
        # sif is local interface, dif is neighbour IP, status is FSM state.
        if sif and dif:
            return f"{sif} ↔ {dif} ({status})" if status else f"{sif} ↔ {dif}"
        return status
    # L2 default (CDP/LLDP).
    return f"{sif} ↔ {dif}" if (sif or dif) else ""


def _build_xml(
    nodes: list[str],
    edges: list[tuple[str, str, str, str, str]],
    positions: dict[str, tuple[int, int]],
    device_metadata: dict[str, dict] | None = None,
    protocol: str = "l2_cdp",
) -> str:
    """Assemble the full mxfile XML."""
    parts: list[str] = [_HEADER]
    meta_map = device_metadata or {}
    force_router = protocol in ("bgp", "ospf")

    # Vertices
    for name in nodes:
        sid = _safe_id(name)
        meta = meta_map.get(name) or {}
        if force_router:
            stencil, height = _STENCIL_ROUTER, 48
        else:
            stencil, height = _stencil_for(name, role=meta.get("role"))
        label = _build_node_label(name, meta)
        x, y = positions.get(name, (40, 40))
        line_count = label.count("&#xa;") + 1
        actual_height = max(height, height + (line_count - 1) * 14)
        parts.append(
            f'        <mxCell id="{escape(sid)}" '
            f'value="{label}" '
            f'style="{_VERTEX_STYLE_TPL.format(stencil=stencil)}" '
            f'vertex="1" parent="1">\n'
            f'          <mxGeometry x="{x}" y="{y}" '
            f'width="{_WIDTH}" height="{actual_height}" as="geometry"/>\n'
            f'        </mxCell>\n'
        )

    # Edges
    ok_statuses = _PROTOCOL_EDGE_OK.get(protocol, {"up", "new", ""})
    for i, (src, sif, dst, dif, status) in enumerate(edges, start=1):
        sid, did = _safe_id(src), _safe_id(dst)
        label = _edge_label(sif, dif, status, protocol)
        style = "endArrow=none;html=1;"
        if status.lower() not in ok_statuses:
            style += "dashed=1;"
        parts.append(
            f'        <mxCell id="e{i}" value="{escape(label)}" '
            f'style="{style}" '
            f'edge="1" source="{escape(sid)}" target="{escape(did)}" parent="1">\n'
            f'          <mxGeometry relative="1" as="geometry"/>\n'
            f'        </mxCell>\n'
        )

    parts.append(_FOOTER)
    return "".join(parts)


# ── Public function ──────────────────────────────────────────────────


_EMPTY_NOTE_TPL = (
    "<!-- render_topology_drawio: {reason} — diagram omitted "
    "(not fabricated). -->\n"
)


def render_topology_drawio(
    adjacencies_table_markdown: str,
    device_metadata: dict | None = None,
    protocol: str = "l2_cdp",
) -> str:
    """Convert an Adjacencies Markdown table into a draw.io XML string.

    Pure transformer — no DB access, no SQL.  Same input contract as
    ``render_topology_mermaid``.  Output is a full ``<mxfile>...</mxfile>``
    XML string suitable for ``format_and_export(format='drawio')``.

    Args:
        adjacencies_table_markdown: a GitHub-flavoured Markdown table.
            Expected columns (case-insensitive, any order, lenient on
            aliases): ``Source``, ``Local Intf`` (or ``Local Interface``
            / ``Source Interface``), ``Dest`` (or ``Destination``),
            ``Remote Intf`` (or ``Remote Interface`` / ``Destination
            Interface``), ``Status`` (optional).
        device_metadata: optional ``{hostname: {model, ip, role, ...}}``
            map.  When present, node labels include model + IP on
            extra lines, and ``role`` (router/firewall/switch) overrides
            the hostname-pattern stencil heuristic.
        protocol: ``"l2_cdp"`` (default) / ``"bgp"`` / ``"ospf"``.

    Returns:
        ``<mxfile>...</mxfile>`` XML string; or an HTML comment
        ``<!-- ... diagram omitted ... -->`` when the input is empty
        or unparseable (never fabricates edges).
    """
    if not adjacencies_table_markdown or not adjacencies_table_markdown.strip():
        return _EMPTY_NOTE_TPL.format(reason="empty input")

    rows = _parse_table(adjacencies_table_markdown)
    if not rows:
        return _EMPTY_NOTE_TPL.format(reason="could not parse a Markdown table")

    sample = rows[0]
    src_col = _find_col(sample, ["source", "src", "source device"])
    sif_col = _find_col(sample, [
        "local intf", "local interface", "source interface", "src intf",
    ])
    dst_col = _find_col(sample, ["dest", "destination", "dst", "dest device"])
    dif_col = _find_col(sample, [
        "remote intf", "remote interface", "destination interface", "dst intf",
    ])
    sta_col = _find_col(sample, ["status", "link status", "state"])

    missing = [
        n for n, v in [
            ("Source", src_col), ("Local Intf", sif_col),
            ("Dest", dst_col), ("Remote Intf", dif_col),
        ] if v is None
    ]
    if missing:
        return _EMPTY_NOTE_TPL.format(reason=f"table missing required column(s) {missing}")

    nodes_seen: dict[str, None] = {}
    edges: list[tuple[str, str, str, str, str]] = []
    for r in rows:
        src = r.get(src_col, "").strip()
        sif = r.get(sif_col, "").strip()
        dst = r.get(dst_col, "").strip()
        dif = r.get(dif_col, "").strip()
        if not (src and dst):
            continue
        status = r.get(sta_col, "").strip() if sta_col else ""
        nodes_seen.setdefault(src, None)
        nodes_seen.setdefault(dst, None)
        edges.append((src, sif, dst, dif, status))

    if not edges:
        return _EMPTY_NOTE_TPL.format(reason="table parsed but no usable rows")

    nodes = list(nodes_seen.keys())
    positions = _layout_tree(nodes, [(s, d) for s, _, d, _, _ in edges])
    return _build_xml(
        nodes, edges, positions,
        device_metadata=device_metadata,
        protocol=protocol,
    )


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = render_topology_drawio(**_args)
    print(_json.dumps(result, default=str))
