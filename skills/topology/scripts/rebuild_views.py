#!/usr/bin/env python3
"""rebuild_views — materialise recipe-defined topology views.

ARCH-29: callable from the agent (step 4 of the recipe-discovery flow) or
from a pipeline. Pure SQL, zero LLM.

2026-08-17: this script had been raising ``ImportError`` on **every** call,
in both runtimes and against every published version — it imported
``build_all_views`` / ``build_one_view``, which R83.2 deleted from
``olav_netops.core.view_builder`` along with the whole L1 recipe layer. The
import sat above the ``try``, so the failure was a traceback rather than the
``{"status": "error"}`` the caller expects. No test and no caller ever
exercised it, which is why it stayed broken.

The ``view_recipes`` layer it serves is still live: ``recipe_seeds`` seeds
the table, ``list_recipes`` reads it, ``save_recipe`` writes it, and
``topology_intent`` computes coverage from it. Only the materialiser was
gone, so it lives here now — built over the per-command auto views, using
the same SQL shape ``save_recipe`` dry-runs, so a recipe that passed
``save_recipe`` materialises identically.

Not this script's job: ``netops.v_show_<command>_auto`` and
``netops.v_l2_links_auto`` are built by ``view_builder.finalise_ingest`` when
data lands — Stage 3.7 of a live collection, or ``ingest_snapshot`` for an
offline bundle. This script only turns ``view_recipes`` rows into
``netops.v_<concept>_auto``.
"""

from __future__ import annotations

import json
import re
from typing import Any

_VIEW_NAME_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _safe_view_name(command: str) -> str:
    """``"show ip bgp summary"`` → ``"show_ip_bgp_summary"`` (mirrors view_builder)."""
    return _VIEW_NAME_RE.sub("_", command.strip().lower()).strip("_")


def _columns_of(con: Any, view: str) -> list[str] | None:
    """Column names of ``view``, or ``None`` when it does not exist.

    ``None`` and ``[]`` mean different things here: a missing source view is a
    recipe that cannot be materialised yet (ingest has not seen the command),
    while an empty column list would be a broken view. Do not collapse them.
    """
    try:
        return [r[0] for r in con.execute(f"DESCRIBE {view}").fetchall()]
    except Exception:
        return None


def _select_sql(entry: dict, columns: list[str]) -> tuple[str, list[str]]:
    """Build the SELECT that defines a concept view, plus the fields it drops.

    ``field_mappings`` is ``{canonical_name: source_column}``; the SELECT
    aliases source → canonical so the view has canonical column names.
    ``device_name`` / ``snapshot_id`` are carried through when the source has
    them and the recipe did not map them — without ``snapshot_id`` a consumer
    cannot scope the view to one collection.
    """
    mappings: dict = entry.get("field_mappings") or {}
    vendor = entry.get("vendor_hint") or "universal"

    projected: list[str] = []
    dropped: list[str] = []
    for canonical, source in mappings.items():
        if source in columns:
            projected.append(f'"{source}" AS {canonical}')
        else:
            dropped.append(f"{canonical}<-{source}")

    for passthrough in ("device_name", "snapshot_id"):
        if passthrough in columns and passthrough not in mappings:
            projected.append(f'"{passthrough}"')

    auto_view = f"netops.v_{_safe_view_name(entry['command'])}_auto"
    clauses: list[str] = []
    if vendor != "universal":
        safe_vendor = vendor.replace("'", "''")
        clauses.append(
            "device_name IN (SELECT hostname FROM netops.devices "
            f"WHERE platform = '{safe_vendor}')"
        )
    if (entry.get("filter_expr") or "").strip():
        clauses.append(f"({entry['filter_expr'].strip()})")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    sel = ", ".join(projected) if projected else "*"
    return f"SELECT {sel} FROM {auto_view}{where}", dropped


def _fetch_recipes(con: Any, concept: str | None) -> list[dict] | None:
    """Recipe rows, newest first. ``None`` when the table is absent."""
    try:
        con.execute("SELECT 1 FROM view_recipes LIMIT 1")
    except Exception:
        return None
    sql = (
        "SELECT command, concept, vendor_hint, field_mappings, filter_expr "
        "FROM view_recipes"
    )
    params: list[Any] = []
    if concept:
        sql += " WHERE concept = ?"
        params.append(concept)
    rows = con.execute(sql + " ORDER BY discovered_at DESC", params).fetchall()
    out: list[dict] = []
    for command, row_concept, vendor, mappings, filter_expr in rows:
        if isinstance(mappings, str):
            try:
                mappings = json.loads(mappings)
            except Exception:
                mappings = {}
        out.append({
            "command": command,
            "concept": row_concept,
            "vendor_hint": vendor,
            "field_mappings": mappings or {},
            "filter_expr": filter_expr,
        })
    return out


def rebuild_views(concept: str | None = None) -> dict[str, Any]:
    """Rebuild topology SQL views from the current ``view_recipes`` table.

    Args:
        concept: Optional filter — rebuild only the view for this concept
            (``bgp_neighbors``, ``ospf_neighbors``, or a user-added custom
            concept). ``None`` rebuilds every recipe in the table.

    Returns:
        ``{"status": "ok", "views": {view_name: row_count, ...},
        "skipped": [{"concept", "command", "reason"}, ...]}``. Pure SQL, no
        LLM calls. A recipe whose source view or mapped columns are missing is
        skipped with a reason rather than failing the whole rebuild — and
        ``skipped`` non-empty with ``views`` empty is a real outcome, not the
        same thing as "there were no recipes".
    """
    import duckdb
    from olav.core.config import MAIN_DB_PATH

    con = duckdb.connect(str(MAIN_DB_PATH))
    try:
        recipes = _fetch_recipes(con, concept)
        if recipes is None:
            return {
                "status": "error",
                "error": "no view_recipes table in this database — nothing has "
                         "seeded it yet; save_recipe creates it, and a live "
                         "collection seeds the built-ins",
            }
        if not recipes:
            known = [
                r[0] for r in con.execute(
                    "SELECT DISTINCT concept FROM view_recipes ORDER BY concept"
                ).fetchall()
            ]
            if concept:
                return {
                    "status": "error",
                    "error": f"no recipe for concept {concept!r}",
                    "known_concepts": known,
                }
            return {"status": "ok", "views": {}, "skipped": [],
                    "note": "view_recipes is empty — nothing to materialise"}

        con.execute("CREATE SCHEMA IF NOT EXISTS netops")
        views: dict[str, int] = {}
        skipped: list[dict[str, str]] = []
        # Newest-first, one view per concept: a later recipe for the same
        # concept (e.g. a second vendor) must not silently replace the first.
        done: set[str] = set()

        for entry in recipes:
            label = {"concept": entry["concept"], "command": entry["command"]}
            if entry["command"].startswith("@"):
                skipped.append({**label, "reason":
                                "@-directive — built by the ingest pipeline "
                                "(view_builder.finalise_ingest), not from a recipe"})
                continue
            if entry["concept"] in done:
                skipped.append({**label, "reason":
                                "a newer recipe already built this concept"})
                continue

            auto_view = f"netops.v_{_safe_view_name(entry['command'])}_auto"
            columns = _columns_of(con, auto_view)
            if columns is None:
                skipped.append({**label, "reason":
                                f"source view {auto_view} does not exist — "
                                "the command is not in this snapshot"})
                continue

            select_sql, dropped = _select_sql(entry, columns)
            if dropped:
                skipped.append({**label, "reason":
                                f"mapped columns absent from {auto_view}: "
                                f"{', '.join(dropped)}"})
                continue

            view_name = f"v_{_safe_view_name(entry['concept'])}_auto"
            try:
                con.execute(
                    f"CREATE OR REPLACE VIEW netops.{view_name} AS {select_sql}"
                )
                count = con.execute(
                    f"SELECT COUNT(*) FROM netops.{view_name}"
                ).fetchone()[0]
            except Exception as exc:
                skipped.append({**label,
                                "reason": f"{type(exc).__name__}: {exc}"})
                continue
            views[f"netops.{view_name}"] = int(count)
            done.add(entry["concept"])

        return {"status": "ok", "views": views, "skipped": skipped}
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        con.close()


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = rebuild_views(**_args)
    print(_json.dumps(result, default=str))
