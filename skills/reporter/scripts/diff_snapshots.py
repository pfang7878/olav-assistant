#!/usr/bin/env python3
"""ARCH-13: aggregate cross-table snapshot diff (demoted from @tool → script, FINDING-19).

Given two ``snapshot_id`` values this script reports which rows were added
and removed per table, so operators can eyeball a "what changed between
these captures" summary without composing SQL by hand.

Scope (ARCH-13 Phase 1):
    * ``netops.parsed_outputs``   — keyed by (device_name, command)
    * ``netops.topology_links``   — keyed by link_id
    * ``netops.raw_output_store`` — keyed by (device_name, command)
    * ``netops.oc_outputs``       — keyed by (device_name, oc_module)

Usage::

    diff_snapshots(snapshot_id_1="snap_20260101_010000_abc",
                   snapshot_id_2="latest",
                   table_name=None,   # None = all four tables
                   device="R1")       # optional per-device filter

Return shape::

    {
      "status": "success",
      "snapshot_id_1": "...resolved...",
      "snapshot_id_2": "...resolved...",
      "tables": {
        "parsed_outputs": {"added": [...], "removed": [...],
                           "added_count": N, "removed_count": M},
        ...
      },
      "total_added": N, "total_removed": M,
    }
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb


# ── Path / DB resolution ─────────────────────────────────────────────────────


def _find_project_root() -> Path:
    p = Path(__file__).resolve().parent
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


def _main_db_path() -> Path:
    try:
        from olav.core.config import MAIN_DB_PATH
        return Path(MAIN_DB_PATH)
    except Exception:
        return _find_project_root() / ".olav" / "databases" / "main.duckdb"


# ── Table registry ───────────────────────────────────────────────────────────

_TABLES: dict[str, dict[str, Any]] = {
    "parsed_outputs": {
        "fqname": "netops.parsed_outputs",
        "key_cols": ("device_name", "command"),
        "device_col": "device_name",
    },
    "topology_links": {
        "fqname": "netops.topology_links",
        "key_cols": ("link_id",),
        "device_col": "source_device",
    },
    "raw_output_store": {
        "fqname": "netops.raw_output_store",
        "key_cols": ("device_name", "command"),
        "device_col": "device_name",
    },
    "oc_outputs": {
        "fqname": "netops.oc_outputs",
        "key_cols": ("device_name", "oc_module"),
        "device_col": "device_name",
    },
}


def _resolve_snapshot_id(conn: duckdb.DuckDBPyConnection, sid: str) -> str | None:
    if sid and sid.lower() != "latest":
        return sid
    for meta in _TABLES.values():
        fq = meta["fqname"]
        try:
            row = conn.execute(
                f"SELECT MAX(snapshot_id) FROM {fq} WHERE snapshot_id IS NOT NULL"
            ).fetchone()
        except duckdb.Error:
            continue
        if row and row[0]:
            return row[0]
    return None


def _table_exists(conn: duckdb.DuckDBPyConnection, fqname: str) -> bool:
    schema, name = fqname.split(".", 1)
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ? LIMIT 1",
        [schema, name],
    ).fetchone()
    return row is not None


def _diff_one_table(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    meta: dict[str, Any],
    sid1: str,
    sid2: str,
    device: str | None,
    sample_limit: int = 50,
) -> dict[str, Any]:
    fq = meta["fqname"]
    key_cols = meta["key_cols"]
    device_col = meta["device_col"]

    if not _table_exists(conn, fq):
        return {"status": "missing", "added": [], "removed": [],
                "added_count": 0, "removed_count": 0}

    key_expr = ", ".join(key_cols)
    device_clause = f" AND {device_col} = ?" if device else ""
    params_s1 = [sid1] + ([device] if device else [])
    params_s2 = [sid2] + ([device] if device else [])

    added = conn.execute(
        f"""
        SELECT {key_expr} FROM {fq}
         WHERE snapshot_id = ?{device_clause}
        EXCEPT
        SELECT {key_expr} FROM {fq}
         WHERE snapshot_id = ?{device_clause}
        """,
        params_s2 + params_s1,
    ).fetchall()

    removed = conn.execute(
        f"""
        SELECT {key_expr} FROM {fq}
         WHERE snapshot_id = ?{device_clause}
        EXCEPT
        SELECT {key_expr} FROM {fq}
         WHERE snapshot_id = ?{device_clause}
        """,
        params_s1 + params_s2,
    ).fetchall()

    def _fmt(rows: list[tuple]) -> list[dict[str, Any]]:
        return [dict(zip(key_cols, r)) for r in rows[:sample_limit]]

    return {
        "status": "ok",
        "key_cols": list(key_cols),
        "added_count": len(added),
        "removed_count": len(removed),
        "added": _fmt(added),
        "removed": _fmt(removed),
    }


# ── Public function ──────────────────────────────────────────────────────────


def diff_snapshots(
    snapshot_id_1: str,
    snapshot_id_2: str,
    table_name: str | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    """Compare two snapshots across the netops tables.

    Args:
        snapshot_id_1: Source snapshot (``"latest"`` resolves via MAX()).
        snapshot_id_2: Target snapshot (``"latest"`` resolves via MAX()).
        table_name: One of ``parsed_outputs`` / ``topology_links`` /
            ``raw_output_store`` / ``oc_outputs``. ``None`` iterates all four.
        device: Optional hostname filter applied per table.

    Returns:
        Summary dict — see module docstring for shape.
    """
    db_path = _main_db_path()
    if not db_path.exists():
        return {"status": "error", "error": f"main.duckdb not found at {db_path}"}

    if table_name is not None and table_name not in _TABLES:
        return {
            "status": "error",
            "error": f"unknown table_name={table_name!r}; "
                     f"choose from {sorted(_TABLES)}",
        }

    tables = [table_name] if table_name else list(_TABLES)

    with duckdb.connect(str(db_path), read_only=True) as conn:
        sid1 = _resolve_snapshot_id(conn, snapshot_id_1)
        sid2 = _resolve_snapshot_id(conn, snapshot_id_2)
        if sid1 is None or sid2 is None:
            return {"status": "error", "error": "no snapshots available"}

        results: dict[str, Any] = {}
        total_added = total_removed = 0
        for tbl in tables:
            meta = _TABLES[tbl]
            r = _diff_one_table(conn, tbl, meta, sid1, sid2, device)
            results[tbl] = r
            if r["status"] == "ok":
                total_added += r["added_count"]
                total_removed += r["removed_count"]

    return {
        "status": "success",
        "snapshot_id_1": sid1,
        "snapshot_id_2": sid2,
        "device": device,
        "tables": results,
        "total_added": total_added,
        "total_removed": total_removed,
    }


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = diff_snapshots(**_args)
    print(_json.dumps(result, default=str))
