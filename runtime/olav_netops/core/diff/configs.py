"""Config diff helper — compare raw CLI outputs between snapshots.

R-VERTICAL-SLICE 2026-05-09 (dev_docs/70): rewritten to read from
``netops.raw_output_store`` instead of ``exports/snapshots/...``
filesystem.  The DB table is the canonical raw-fallback (RAW-05);
filesystem snapshots are an optional export, often empty.  Reading
from filesystem made this helper fail with "Snapshots directory not
found" even when the same data was sitting in DuckDB.
"""

from __future__ import annotations

import difflib
from typing import Any

import duckdb

from olav.core.config import MAIN_DB_PATH

# IOS metadata lines that change every snapshot — excluded from changed_lines count
_METADATA_PATTERNS = [
    "current configuration",
    "last configuration change at",
    "building configuration",
    "bytes",
]


def _resolve_snapshot_id(snapshot_id: str, conn: duckdb.DuckDBPyConnection) -> str:
    """Resolve 'latest' to the most recent snapshot_id seen in
    ``raw_output_store``; otherwise pass through unchanged."""
    if snapshot_id.lower() != "latest":
        return snapshot_id
    row = conn.execute(
        "SELECT MAX(snapshot_id) FROM netops.raw_output_store"
    ).fetchone()
    if not row or not row[0]:
        raise LookupError("raw_output_store has no snapshots")
    return row[0]


def _read_raw(
    device: str,
    command: str,
    snapshot_id: str,
    conn: duckdb.DuckDBPyConnection,
) -> tuple[str, str]:
    """Return ``(resolved_snapshot_id, raw_output)`` for the
    (device, command) pair.  Raises LookupError if either device or
    command isn't present at the resolved snapshot.

    Tries an exact command match first; falls back to a word-token
    fuzzy match (``"show ip bgp"`` → ``"show ip bgp summary"``) so
    the LLM doesn't need to memorise the canonical command string.
    """
    resolved_id = _resolve_snapshot_id(snapshot_id, conn)

    # Exact match
    row = conn.execute(
        """
        SELECT raw_output FROM netops.raw_output_store
        WHERE device_name = ? AND command = ? AND snapshot_id = ?
        """,
        [device, command, resolved_id],
    ).fetchone()
    if row and row[0]:
        return resolved_id, row[0]

    # Fuzzy fallback — every space-separated token must appear
    # in the candidate command
    tokens = [t for t in command.lower().split() if t]
    if tokens:
        rows = conn.execute(
            """
            SELECT command, raw_output FROM netops.raw_output_store
            WHERE device_name = ? AND snapshot_id = ?
            """,
            [device, resolved_id],
        ).fetchall()
        for cand_cmd, cand_raw in rows:
            cand_lower = (cand_cmd or "").lower()
            if all(t in cand_lower for t in tokens) and cand_raw:
                return resolved_id, cand_raw

    # Nothing matched — surface what IS available so the caller can
    # tell whether the device was probed at all.
    available = conn.execute(
        """
        SELECT command FROM netops.raw_output_store
        WHERE device_name = ? AND snapshot_id = ?
        ORDER BY command
        """,
        [device, resolved_id],
    ).fetchall()
    raise LookupError(
        f"No raw output for {device!r}/{command!r} at snapshot "
        f"{resolved_id!r}.  Device has {len(available)} commands "
        f"in this snapshot: {[r[0] for r in available[:10]]}"
        + (" ..." if len(available) > 10 else "")
    )


def _extract_sections(text: str, sections: list[str]) -> str:
    """Extract only lines belonging to named IOS config sections."""
    lines = text.splitlines()
    result = []
    in_section = False

    for line in lines:
        is_top_level = line.strip() and not line.startswith((" ", "\t"))

        if is_top_level:
            in_section = any(line.lower().startswith(s.lower()) for s in sections)

        if in_section:
            result.append(line)
            if line.strip() == "!":
                in_section = False

    return "\n".join(result)


def _count_significant_changes(diff_lines: list[str]) -> tuple[int, int]:
    """Count added/removed lines, excluding IOS metadata churn."""
    added = removed = 0

    for line in diff_lines:
        if any(pattern in line.lower() for pattern in _METADATA_PATTERNS):
            continue

        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1

    return added, removed


def _generate_diff(text_a: str, text_b: str, context_lines: int = 3) -> str:
    """Generate unified diff between two texts."""
    lines_a = text_a.splitlines(keepends=True)
    lines_b = text_b.splitlines(keepends=True)

    diff = difflib.unified_diff(lines_a, lines_b, lineterm="", n=context_lines)

    return "\n".join(diff)


def _summarize_diff(
    changed_lines: int, added_lines: int, removed_lines: int, sections: list[str] | None = None
) -> str:
    """Generate human-readable summary of changes."""
    net = added_lines - removed_lines
    net_str = f"+{net}" if net >= 0 else str(net)

    summary = f"{changed_lines} lines changed ({net_str})"

    if sections and changed_lines > 0:
        summary += f". Sections: {', '.join(sections)}"

    return summary


def diff_configs(
    device: str,
    command: str,
    snapshot_id_1: str | None = None,
    snapshot_id_2: str | None = None,
    sections: list[str] | None = None,
    context_lines: int = 3,
    full: bool = False,
) -> dict[str, Any]:
    """Unified diff of raw CLI output between two snapshot IDs.

    Compact mode caps shown lines at 40 unless ``full=True``.
    """
    try:
        if snapshot_id_2 is None:
            snapshot_id_2 = "latest"

        with duckdb.connect(str(MAIN_DB_PATH), read_only=True) as conn:
            # Resolve target side first so we can pick the second-newest
            # for the baseline.
            try:
                id_2 = _resolve_snapshot_id(snapshot_id_2, conn)
            except LookupError as e:
                return {
                    "status": "error",
                    "device": device,
                    "command": command,
                    "message": str(e),
                }

            if snapshot_id_1 is None:
                row = conn.execute(
                    """
                    SELECT MAX(snapshot_id) FROM netops.raw_output_store
                    WHERE snapshot_id < ?
                    """,
                    [id_2],
                ).fetchone()
                snapshot_id_1 = row[0] if row and row[0] else id_2
            try:
                id_1 = _resolve_snapshot_id(snapshot_id_1, conn)
            except LookupError as e:
                return {
                    "status": "error",
                    "device": device,
                    "command": command,
                    "snapshot_id_2": id_2,
                    "message": str(e),
                }

            try:
                id_1, text_1 = _read_raw(device, command, id_1, conn)
            except LookupError as e:
                return {
                    "status": "error",
                    "device": device,
                    "command": command,
                    "snapshot_id_1": id_1,
                    "snapshot_id_2": id_2,
                    "message": f"Baseline raw not found: {e}",
                }

            try:
                id_2, text_2 = _read_raw(device, command, id_2, conn)
            except LookupError as e:
                return {
                    "status": "error",
                    "device": device,
                    "command": command,
                    "snapshot_id_1": id_1,
                    "snapshot_id_2": id_2,
                    "message": f"Target raw not found: {e}",
                }

        # Filter sections if requested
        if sections:
            text_1 = _extract_sections(text_1, sections)
            text_2 = _extract_sections(text_2, sections)

        # Generate diff
        diff_output = _generate_diff(text_1, text_2, context_lines)
        diff_lines = diff_output.splitlines()

        # Count changes
        added, removed = _count_significant_changes(diff_lines)
        changed = added + removed

        full_line_count = len(diff_lines)
        # Compact mode: trim to 40 lines unless caller asked for full output.
        if not full and full_line_count > 40:
            diff_lines = diff_lines[:40]
            diff_lines.append(
                f"\n[compact mode: showing 40 of {full_line_count} lines; pass full=True for the complete diff]"
            )
            diff_output = "\n".join(diff_lines)

        summary = _summarize_diff(changed, added, removed, sections)

        return {
            "status": "success",
            "device": device,
            "command": command,
            "snapshot_id_1": id_1,
            "snapshot_id_2": id_2,
            "added_lines": added,
            "removed_lines": removed,
            "changed_lines": changed,
            "full_line_count": full_line_count,
            "diff": diff_output,
            "summary": summary,
        }

    except Exception as e:
        return {
            "status": "error",
            "device": device,
            "command": command,
            "message": f"Unexpected error: {str(e)}",
        }
