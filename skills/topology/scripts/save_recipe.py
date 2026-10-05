#!/usr/bin/env python3
"""save_recipe — validate + persist an LLM-drafted recipe YAML.

ARCH-29: ``discover_recipe`` produces a recipe YAML string; this tool
takes that string, verifies it parses + generates a non-empty view on
the current snapshot + UPSERTs to ``view_recipes`` DB + writes to
``~/.olav/config/recipes/user/<protocol>_<vendor>.yaml``.

No Python code execution, no sandbox — YAML validation and a dry-run
``CREATE OR REPLACE VIEW _probe AS ...`` against the read-write DB.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_ALLOWED_VENDORS = {"cisco_ios", "juniper_junos", "arista_eos", "cisco_nxos", "universal"}

_VIEW_NAME_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _safe_view_name(command: str) -> str:
    return _VIEW_NAME_RE.sub("_", command.strip().lower()).strip("_")


def _ensure_view_recipes_table(con) -> None:
    """Create view_recipes if it doesn't exist (idempotent)."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS view_recipes (
            command       VARCHAR,
            concept       VARCHAR,
            vendor_hint   VARCHAR DEFAULT 'universal',
            field_mappings JSON,
            filter_expr   VARCHAR,
            discovered_at TIMESTAMP,
            UNIQUE (command, concept, vendor_hint)
        )
    """)


def _probe_sql_for_entry(entry: dict) -> str:
    """Build a SELECT SQL to dry-run a recipe entry against its auto-view.

    field_mappings maps  canonical_name → json_field_name  where
    json_field_name is the column name in ``v_<cmd>_auto``.
    The SELECT aliases json_field_name → canonical_name so the probe view
    has canonical column names (mirrors the old _generate_sql_branch shape).
    """
    command = entry["command"]
    vendor = entry.get("vendor_hint") or "universal"
    mappings: dict = entry.get("field_mappings") or {}
    auto_view = f"netops.v_{_safe_view_name(command)}_auto"
    if mappings:
        sel = ", ".join(f'"{src}" AS {can}' for can, src in mappings.items())
    else:
        sel = "*"
    where = ""
    if vendor != "universal":
        safe_vendor = vendor.replace("'", "''")
        where = (
            f" WHERE device_name IN "
            f"(SELECT hostname FROM netops.devices WHERE platform = '{safe_vendor}')"
        )
    return f"SELECT {sel} FROM {auto_view}{where}"


def _validate_entry(entry: dict) -> tuple[bool, str]:
    """Fast structural check before we touch DuckDB."""
    for field in ("command", "concept", "field_mappings"):
        if field not in entry:
            return False, f"missing required field: {field}"
    if not isinstance(entry["command"], str) or not entry["command"].strip():
        return False, "command must be non-empty string"
    if not isinstance(entry["concept"], str) or not entry["concept"].strip():
        return False, "concept must be non-empty string"
    vendor = entry.get("vendor_hint") or "universal"
    if vendor not in _ALLOWED_VENDORS:
        return False, (
            f"vendor_hint must be one of {sorted(_ALLOWED_VENDORS)}; got {vendor!r}"
        )
    mappings = entry["field_mappings"]
    command = entry["command"]
    if not isinstance(mappings, dict):
        return False, "field_mappings must be a dict"
    if not mappings and not command.startswith("@"):
        return False, "field_mappings empty (only allowed for @-directive commands)"
    return True, ""


def _user_recipes_dir() -> Path:
    try:
        from olav.core.config import get_paths_config
        d = Path(get_paths_config().config_dir) / "recipes" / "user"
    except Exception:
        d = Path.home() / ".olav" / "config" / "recipes" / "user"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_recipe(recipe_yaml: str, force: bool = False) -> dict[str, Any]:
    """Validate + persist a recipe YAML to the ``view_recipes`` table AND
    write a copy to ``~/.olav/config/recipes/user/<protocol>_<vendor>.yaml``.

    Args:
        recipe_yaml: YAML text (single entry dict OR a list of entries).
        force: If True, accept recipes whose dry-run produces 0 rows
            (normally rejected — 0 rows usually means the recipe is
            wrong for the current data).

    Returns:
        ``{ok, entries_written, files_written, diagnostics}``. On failure
        ``ok=False`` + ``error`` explains the reason.
    """
    import duckdb
    import yaml
    from datetime import UTC, datetime
    from olav.core.config import MAIN_DB_PATH

    try:
        parsed = yaml.safe_load(recipe_yaml)
    except Exception as exc:
        return {"ok": False, "error": f"YAML parse failed: {exc}"}
    if parsed is None:
        return {"ok": False, "error": "empty YAML"}
    if isinstance(parsed, dict):
        entries = [parsed]
    elif isinstance(parsed, list):
        entries = parsed
    else:
        return {"ok": False, "error": f"YAML must be dict or list of dicts; got {type(parsed).__name__}"}

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return {"ok": False, "error": f"entry {i} is not a dict"}
        ok, reason = _validate_entry(entry)
        if not ok:
            return {"ok": False, "error": f"entry {i}: {reason}"}

    con = duckdb.connect(str(MAIN_DB_PATH))
    diagnostics: dict[str, Any] = {"dry_run_counts": {}}
    try:
        _ensure_view_recipes_table(con)

        # Dry-run each entry as its own probe view.
        for i, entry in enumerate(entries):
            concept = entry["concept"]
            vendor = entry.get("vendor_hint") or "universal"
            probe_name = f"_probe_{concept}_{vendor}_{i}"
            try:
                probe_sql = _probe_sql_for_entry(entry)
                con.execute(f"CREATE OR REPLACE VIEW netops.{probe_name} AS {probe_sql}")
                n = con.execute(f"SELECT COUNT(*) FROM netops.{probe_name}").fetchone()[0]
                diagnostics["dry_run_counts"][f"{concept}/{vendor}"] = int(n)
                con.execute(f"DROP VIEW IF EXISTS netops.{probe_name}")
            except Exception as exc:
                return {
                    "ok": False,
                    "error": f"entry {i} dry-run failed: {exc}",
                    "diagnostics": diagnostics,
                }
            if n == 0 and not force:
                return {
                    "ok": False,
                    "error": f"entry {i} produced 0 rows on current snapshot; "
                             "retry with force=True if you know this is correct",
                    "diagnostics": diagnostics,
                }

        # All entries passed dry-run — UPSERT + write files.
        now = datetime.now(UTC)
        files_written: list[str] = []
        user_dir = _user_recipes_dir()
        # Group entries by (concept, vendor) for file naming
        by_file: dict[str, list[dict]] = {}
        for entry in entries:
            vendor = entry.get("vendor_hint") or "universal"
            concept = entry["concept"]
            # Derive a short protocol name from concept (bgp_neighbors → bgp)
            proto = concept.replace("_neighbors", "").replace("_adjacencies", "").replace("topology_", "")
            key = f"{proto}_{vendor}"
            by_file.setdefault(key, []).append(entry)

        for fname, file_entries in by_file.items():
            path = user_dir / f"{fname}.yaml"
            path.write_text(yaml.safe_dump(file_entries, sort_keys=False), encoding="utf-8")
            files_written.append(str(path))

        # DB upsert (DELETE+INSERT — table may not have UNIQUE constraint)
        for entry in entries:
            vendor = entry.get("vendor_hint") or "universal"
            con.execute(
                "DELETE FROM view_recipes WHERE command=? AND concept=? AND vendor_hint=?",
                [entry["command"], entry["concept"], vendor],
            )
            con.execute(
                """
                INSERT INTO view_recipes
                    (command, concept, vendor_hint, field_mappings, filter_expr, discovered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    entry["command"],
                    entry["concept"],
                    vendor,
                    json.dumps(entry["field_mappings"]),
                    entry.get("filter_expr"),
                    now,
                ],
            )

        return {
            "ok": True,
            "entries_written": len(entries),
            "files_written": files_written,
            "diagnostics": diagnostics,
        }
    finally:
        con.close()


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = save_recipe(**_args)
    print(_json.dumps(result, default=str))
