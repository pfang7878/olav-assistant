#!/usr/bin/env python3
"""describe_table — Per-table schema introspection on demand (ARCH-18).

Progressive-disclosure counterpart to the bulk ``get_schema_context`` built
into ``execute_sql``. Agents can call ``describe_table("netops.devices")``
to get just that one table's columns, types, and (optionally) two sample
rows — without carrying the entire database schema in the system prompt.

The tool uses DuckDB ``DESCRIBE`` directly (no caching layer needed for
single-table queries). Accepts both schema-qualified (``netops.devices``)
and bare (``devices``) table names; netops schema is tried first when the
name is bare to match the most common operator workflow.
"""

from __future__ import annotations

import json
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


_PROJECT_ROOT = _find_project_root()
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))


def _db_query(sql: str, params: tuple | None = None) -> list[dict[str, Any]]:
    """Thin DuckDB wrapper that matches execute_sql.db_query's semantics."""
    try:
        import duckdb
        from olav.core.config import MAIN_DB_PATH
    except Exception as exc:
        raise RuntimeError(f"describe_table bootstrap failed: {exc}") from exc

    with duckdb.connect(str(MAIN_DB_PATH), read_only=True) as conn:
        cur = conn.execute(sql, params) if params else conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    return [dict(zip(cols, r, strict=False)) for r in rows]


def _resolve_table_name(table_name: str) -> str | None:
    """Return the schema-qualified name DuckDB accepts, or None if missing.

    Order of attempts:
    1. ``table_name`` as-is (works for schema-qualified ``netops.devices``
       or views in the ``main`` schema).
    2. ``netops.<table_name>`` — most on-demand queries target netops tables.
    3. ``main.<table_name>`` — fallback for views.
    """
    candidates = [table_name]
    if "." not in table_name:
        candidates.extend([f"netops.{table_name}", f"main.{table_name}"])
    for cand in candidates:
        try:
            _db_query(f"SELECT 1 FROM {cand} LIMIT 0")
            return cand
        except Exception:
            continue
    return None


def describe_table(table_name: str, include_samples: bool = False) -> dict[str, Any]:
    """Return schema information for a single DuckDB table.

    Use this instead of asking the orchestrator to inject every table's
    columns into the prompt (ARCH-18 progressive disclosure). Returns
    column names and types; optionally two sample rows when
    ``include_samples=True``.

    Args:
        table_name: ``netops.devices`` (preferred) or bare ``devices``.
            Bare names are resolved against ``netops`` then ``main``.
        include_samples: If True, also return up to 2 sample rows as
            ``samples`` (JSON-sanitised, truncated to 400 chars per field).
            Off by default — samples add tokens the caller rarely needs.

    Returns:
        ``{"table": "<qualified>", "columns": [{"name": ..., "type": ...}, ...],
        "samples": [...]}`` on success, or
        ``{"error": "...", "table_name": "<input>"}`` on failure.

    Example:
        >>> describe_table(table_name="netops.devices")
        {"table": "netops.devices", "columns": [...]}
    """
    if not table_name or not isinstance(table_name, str):
        return {"error": "describe_table requires a non-empty table_name"}

    qualified = _resolve_table_name(table_name.strip())
    if qualified is None:
        return {
            "error": f"table not found: {table_name!r}",
            "hint": "Try schema-qualified (e.g. 'netops.devices' or 'main.v_interfaces_auto').",
        }

    try:
        rows = _db_query(f"DESCRIBE {qualified}")
    except Exception as exc:
        return {"error": f"DESCRIBE {qualified} failed: {exc}", "table": qualified}

    columns: list[dict[str, str]] = []
    for row in rows:
        # DuckDB returns column_name / column_type; older drivers use Field/Type.
        name = row.get("column_name") or row.get("Field") or ""
        ctype = row.get("column_type") or row.get("Type") or ""
        if name:
            columns.append({"name": str(name), "type": str(ctype)})

    payload: dict[str, Any] = {"table": qualified, "columns": columns}

    if include_samples:
        try:
            sample_rows = _db_query(f"SELECT * FROM {qualified} LIMIT 2")
        except Exception as exc:
            payload["samples_error"] = str(exc)
            return payload

        samples: list[dict[str, Any]] = []
        for r in sample_rows:
            trimmed: dict[str, Any] = {}
            for k, v in r.items():
                if isinstance(v, (dict, list)):
                    s = json.dumps(v, default=str)
                    trimmed[k] = s[:400] + ("…" if len(s) > 400 else "")
                elif isinstance(v, str) and len(v) > 400:
                    trimmed[k] = v[:400] + "…"
                else:
                    trimmed[k] = v
            samples.append(trimmed)
        payload["samples"] = samples

    return payload


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = describe_table(**_args)
    print(_json.dumps(result, default=str))
