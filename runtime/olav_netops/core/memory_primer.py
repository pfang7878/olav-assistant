"""Memory primer — populate semantic memory at ingest time.

Bridges DuckDB (data) → LanceDB memory (knowledge) once per
``/netops_init``.  Stores three flavours of pre-built knowledge so
``AutoRecallMiddleware`` can inject them as ``<relevant-memories>``
context on the next NL query — without the agent having to call
introspection-cache or run ``DESCRIBE`` queries first.

Why "unified memory layer"
==========================

R83.3 built ``netops.introspection_cache`` (DuckDB table) so agents
could fetch fleet + view + value-distribution metadata in one read.
But the agent still had to *know* to run ``SELECT * FROM
netops.introspection_cache`` first, and small models (grok-4.1-fast)
don't reliably honour multi-step instructions.

R83.4 (this module) writes the same metadata into the existing
LanceDB memory store — the same store ``query_pattern_capture``
writes successful SQLs to and ``AutoRecallMiddleware`` reads from
on every agent turn.  Now the agent gets schema + value hints
**pushed** into its ``<relevant-memories>`` context automatically;
no instruction-following needed.

Three categories of primed entries
==================================

1. ``schema_knowledge`` — one entry per per-command auto-view, with
   the column list and a sample row.  ID: ``schema_<view_name>``.
2. ``value_distribution`` — one entry per state-like (view, column)
   tuple, listing observed variants with frequencies.  ID:
   ``values_<view>_<col>``.
3. (existing) ``query_pattern`` — written by query_pattern_capture
   middleware on successful agent runs.  Untouched here.

Refresh model
=============

Every ``finalise_ingest`` call upserts entries by deterministic ID
(delete + add).  Schema-drift safe: stale entries from removed
columns naturally drop on the next snapshot.  Tagged
``origin='data_profile'`` and ``confidence=1.0`` so they don't
time-decay like agent-captured opinions.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# State-like column heuristic (matches view_builder._STATE_COL_PATTERN)
import re as _re_mod
_STATE_COL_PATTERN = _re_mod.compile(
    r"(state|status|proto|protocol|admin|oper)$",
    _re_mod.IGNORECASE,
)


def _concept_tags_from_view(view_name: str) -> list[str]:
    """Heuristic tags from view name (e.g., v_show_bgp_summary_auto → bgp, summary).

    Tags are used by ``AutoRecallMiddleware``'s hybrid search to
    boost results matching a user question's keywords.
    """
    # strip 'v_show_' prefix and '_auto' suffix
    name = view_name.lower()
    if name.startswith("v_"):
        name = name[2:]
    if name.startswith("show_"):
        name = name[len("show_"):]
    if name.endswith("_auto"):
        name = name[:-len("_auto")]
    parts = [p for p in name.split("_") if p]
    # filter out boring tokens
    boring = {"all", "summary", "detail", "brief", "list", "table", "info"}
    return [p for p in parts if p not in boring][:6]  # cap at 6 tags


def _format_columns(cols: list[tuple[str, str]]) -> str:
    """Render column list compactly for memory text."""
    return ", ".join(f"{name}({dtype})" for name, dtype in cols)


def _format_state_values(values: list[tuple[str, int]]) -> str:
    """Render observed (value, freq) pairs."""
    if not values:
        return ""
    return ", ".join(f"'{v}'({f})" for v, f in values)


def _embed(text: str):
    """Wrap embedder; returns None on failure (caller skips entry)."""
    try:
        from olav.core.embedder import embed_text
        return embed_text(text)
    except Exception as exc:
        logger.debug("memory_primer: embed failed: %s", exc)
        return None


def prime_memory_at_ingest(con: Any, store: Any | None = None) -> dict[str, int]:
    """Refresh ``schema_knowledge`` and ``value_distribution`` memory entries.

    DISABLED BY DEFAULT (2026-04-30, dev_docs/00 § ISSUE-SCHEMA-PUSH-VS-PULL).

    The push model — pre-priming 50+ schema entries into LanceDB — was
    empirically negative ROI on small models: 27B-dense ignores cached
    schema_knowledge and introspects via ``information_schema`` /
    ``describe_table`` on demand anyway, so pushing the data costs DB
    rows + AutoRecall slot competition without changing behaviour.

    Set ``OLAV_LEGACY_SCHEMA_PRIME=1`` to opt back in for parity testing
    or large-model deployments where the push hint is still worth its
    prompt cost.

    Returns ``{"schema_entries": N, "value_entries": M, "skipped": K}``.
    """
    import os
    if os.environ.get("OLAV_LEGACY_SCHEMA_PRIME", "").strip().lower() not in {"1", "true", "yes"}:
        logger.info(
            "memory_primer: schema_knowledge / value_distribution push DISABLED "
            "(default 2026-04-30+).  Agent uses describe_table tool on demand.  "
            "Set OLAV_LEGACY_SCHEMA_PRIME=1 to re-enable."
        )
        return {"schema_entries": 0, "value_entries": 0, "skipped": 0}

    if store is None:
        try:
            from olav.core.memory import get_store
            store = get_store()
        except Exception as exc:
            logger.info("memory_primer: store unavailable, skipping: %s", exc)
            return {"schema_entries": 0, "value_entries": 0, "skipped": -1}

    if store is None:
        return {"schema_entries": 0, "value_entries": 0, "skipped": -1}

    schema_count = 0
    value_count = 0
    skipped = 0

    # ── 1. Per-view schema knowledge ──────────────────────────────────
    views = con.execute(
        """
        SELECT table_name FROM information_schema.views
         WHERE table_schema='netops'
           AND table_name LIKE 'v_%_auto'
         ORDER BY 1
        """
    ).fetchall()

    for (view_name,) in views:
        # Column list with types
        cols = con.execute(
            """
            SELECT column_name, data_type FROM information_schema.columns
             WHERE table_schema='netops' AND table_name = ?
             ORDER BY ordinal_position
            """,
            [view_name],
        ).fetchall()
        if not cols:
            continue

        # State-like column variants (read from value_profile we just built)
        state_summary_parts = []
        try:
            state_rows = con.execute(
                """
                SELECT column_name, value, freq FROM netops.value_profile
                 WHERE view_name = ?
                 ORDER BY column_name, freq DESC
                """,
                [view_name],
            ).fetchall()
        except Exception:
            state_rows = []

        per_col: dict[str, list[tuple[str, int]]] = {}
        for col, val, freq in state_rows:
            if not _STATE_COL_PATTERN.search(col or ""):
                continue
            per_col.setdefault(col, []).append((val, freq))
        for col, vals in per_col.items():
            state_summary_parts.append(f"{col}={_format_state_values(vals)}")

        # Sample data row for shape clarity (truncate strings)
        sample_part = ""
        try:
            sample = con.execute(
                f'SELECT * FROM netops."{view_name}" LIMIT 1'
            ).fetchone()
            if sample:
                cnames = [c[0] for c in cols]
                sample_pairs = []
                for cname, sval in zip(cnames, sample):
                    sval_s = str(sval)[:40]
                    sample_pairs.append(f"{cname}={sval_s!r}")
                sample_part = f" Sample: {{{', '.join(sample_pairs[:8])}}}."
        except Exception:
            pass

        # Build memory text
        text = (
            f"Auto-view netops.{view_name} columns: {_format_columns(cols)}."
            f"{sample_part}"
        )
        if state_summary_parts:
            text += f" Categorical variants: {'; '.join(state_summary_parts)}."

        text = text[:1024]  # safety cap

        vec = _embed(text)
        if not vec:
            skipped += 1
            continue

        mem_id = f"schema_{view_name}"
        try:
            store.delete_memory(id=mem_id)  # idempotent upsert
        except Exception:
            pass
        try:
            store.add_memory(
                id=mem_id,
                text=text,
                vector=vec,
                category="schema_knowledge",
                scope="global",
                metadata={"view": view_name, "n_columns": len(cols)},
                origin="data_profile",
                confidence=1.0,
                tags=json.dumps(_concept_tags_from_view(view_name), ensure_ascii=False),
            )
            schema_count += 1
        except Exception as exc:
            logger.debug("memory_primer: schema entry %s failed: %s", view_name, exc)
            skipped += 1

    # ── 2. Per-(view, col) value distribution entries ──────────────────
    try:
        rows = con.execute(
            """
            SELECT view_name, column_name, value, freq, cluster_id
              FROM netops.value_profile
             ORDER BY view_name, column_name, freq DESC
            """
        ).fetchall()
    except Exception as exc:
        logger.warning("memory_primer: value_profile read failed: %s", exc)
        rows = []

    grouped: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for view_name, col_name, val, freq, _cluster in rows:
        if not _STATE_COL_PATTERN.search(col_name or ""):
            continue
        grouped.setdefault((view_name, col_name), []).append((val, freq))

    for (view_name, col_name), variants in grouped.items():
        if not variants:
            continue
        variant_str = _format_state_values(variants)
        text = (
            f"In netops.{view_name}.{col_name}, observed values across the "
            f"fleet: {variant_str}. When filtering by this concept, treat "
            f"these as variants of the same underlying state — write "
            f"WHERE {col_name} IN (...) covering all that match the "
            f"semantic intent."
        )
        text = text[:1024]
        vec = _embed(text)
        if not vec:
            skipped += 1
            continue
        mem_id = f"values_{view_name}_{col_name}"
        try:
            store.delete_memory(id=mem_id)
        except Exception:
            pass
        try:
            tags = _concept_tags_from_view(view_name) + [col_name.lower()]
            store.add_memory(
                id=mem_id,
                text=text,
                vector=vec,
                category="value_distribution",
                scope="global",
                metadata={"view": view_name, "col": col_name,
                          "n_variants": len(variants)},
                origin="data_profile",
                confidence=0.95,
                tags=json.dumps(tags, ensure_ascii=False),
            )
            value_count += 1
        except Exception as exc:
            logger.debug("memory_primer: value entry %s.%s failed: %s",
                         view_name, col_name, exc)
            skipped += 1

    logger.info(
        "memory_primer: %d schema + %d value entries (%d skipped)",
        schema_count, value_count, skipped,
    )
    return {
        "schema_entries": schema_count,
        "value_entries": value_count,
        "skipped": skipped,
    }


__all__ = ["prime_memory_at_ingest"]
