#!/usr/bin/env python3
"""``render_topology_mermaid`` — pure markdown → Mermaid transformer.

Writer-side transformer: takes the Adjacencies table that analyzer
already embedded in a change plan's ``## Topology Context`` and
converts it to a Mermaid ``graph LR`` code block.

**No DB access**.  All input comes from the markdown the caller
already read with ``read_file``.  This keeps writer's blast-radius
narrow: it cannot fetch anything new, only re-format what's there.

The companion lab-side transformer (``render_topology_clab_yaml``,
when lab is rebuilt) consumes the **same** Adjacencies table and
produces ContainerLab YAML — analyzer is the single source of truth,
writer/lab are language translators.

Expected input format (the table writer extracts from the change plan):

    | Source | Local Intf | Dest | Remote Intf | Status |
    |---|---|---|---|---|
    | R3 | Ethernet0/2 | R4 | Ethernet0/2 | New |
    | R3 | Ethernet0/0 | R1 | ge-0/0/2     | up  |

Output:

    ```mermaid
    graph LR
        R1["R1"]
        R3["R3"]
        R4["R4"]
        R3 ---|"Ethernet0/2 ↔ Ethernet0/2"| R4
        R3 ---|"Ethernet0/0 ↔ ge-0/0/2"| R1
    ```
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


_LR_HEADER = "```mermaid\ngraph LR\n"
_LR_FOOTER = "```\n"


def _safe_id(name: str) -> str:
    """Mermaid node IDs can't contain dots / dashes / slashes."""
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


def _parse_table(table_md: str) -> list[dict[str, str]]:
    """Parse a GitHub-flavoured Markdown table into list of row-dicts.

    Tolerates leading/trailing whitespace, alignment markers
    (``|:---:|``), and bold-wrapped cells (``**R3**``).

    Returns empty list if no valid table is found.
    """
    lines = [ln.rstrip() for ln in table_md.splitlines() if ln.strip()]
    # Find a "| col | col |" header followed by a "|---|---|" separator.
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for i, ln in enumerate(lines):
        if not ln.startswith("|"):
            continue
        cells = [c.strip().strip("*") for c in ln.strip("|").split("|")]
        # Separator row: cells like '---', ':---', ':-:'.
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if len(cells) != len(header):
            # malformed; skip
            continue
        rows.append(dict(zip(header, cells, strict=False)))
    return rows


def _find_col(header_row: dict[str, str], candidates: list[str]) -> str | None:
    """Find the first candidate column name present in a row dict's keys."""
    for c in candidates:
        if c in header_row:
            return c
    return None


def render_topology_mermaid(adjacencies_table_markdown: str) -> str:
    """Convert an Adjacencies Markdown table into a Mermaid ``graph LR`` block.

    Pure transformer — no DB access, no SQL.  Input is the table the
    caller already extracted from the change-plan markdown (typically
    under the ``### Adjacencies`` heading inside ``## Topology
    Context``).  Output is a Markdown-fenced Mermaid block ready to
    splice into the polished file.

    Args:
        adjacencies_table_markdown: a GitHub-flavoured Markdown table.
            Expected columns (case-insensitive, any order, lenient on
            aliases): ``Source``, ``Local Intf`` (or ``Local Interface``
            / ``Source Interface``), ``Dest`` (or ``Destination``),
            ``Remote Intf`` (or ``Remote Interface`` / ``Destination
            Interface``), ``Status`` (optional).

    Returns:
        A Mermaid block as Markdown-fenced string; or a single-line
        ``> _ ... cannot render_`` note if the table is empty /
        unparseable (never fabricates edges).
    """
    if not adjacencies_table_markdown or not adjacencies_table_markdown.strip():
        return (
            "> _render_topology_mermaid: empty input — diagram omitted "
            "(no adjacencies table provided)._\n"
        )

    rows = _parse_table(adjacencies_table_markdown)
    if not rows:
        return (
            "> _render_topology_mermaid: could not parse a Markdown "
            "table from the input — diagram omitted (not fabricated)._\n"
        )

    # Resolve column names (lenient aliases for analyzer template drift).
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
        return (
            f"> _render_topology_mermaid: table is missing required "
            f"column(s) {missing} — diagram omitted (not fabricated)._\n"
        )

    nodes: dict[str, str] = {}
    edge_lines: list[str] = []
    for r in rows:
        src = r.get(src_col, "").strip()
        sif = r.get(sif_col, "").strip()
        dst = r.get(dst_col, "").strip()
        dif = r.get(dif_col, "").strip()
        if not (src and dst):
            continue
        sid, did = _safe_id(src), _safe_id(dst)
        nodes.setdefault(sid, src)
        nodes.setdefault(did, dst)
        status = (r.get(sta_col, "").strip().lower() if sta_col else "")
        label = f'"{sif} ↔ {dif}"' if (sif or dif) else '""'
        if status and status not in ("up", "new"):
            # 'down' / 'unknown' → dashed edge
            edge_lines.append(f"    {sid} -.->|{label}| {did}")
        else:
            edge_lines.append(f"    {sid} ---|{label}| {did}")

    if not edge_lines:
        return (
            "> _render_topology_mermaid: table parsed but no usable "
            "rows (missing src/dst names) — diagram omitted._\n"
        )

    node_lines = [f'    {sid}["{label}"]' for sid, label in nodes.items()]
    body = "\n".join(node_lines + edge_lines)
    return f"{_LR_HEADER}{body}\n{_LR_FOOTER}"


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = render_topology_mermaid(**_args)
    print(_json.dumps(result, default=str))
