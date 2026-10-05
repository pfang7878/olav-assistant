"""View builder — DuckDB-native zero-ETL.

R83.2 (P1) cleanup
==================

Pre-R83.2 this module hosted **two** view layers:

* **L1 — recipe-based semantic views** (``v_bgp_neighbors_auto`` /
  ``v_ospf_neighbors_auto`` / ``v_l2_links_auto``) built from
  ``view_recipes`` rows that hard-mapped ``(concept, command, vendor)``
  → ``(canonical_field_names, state_canonicalisation)``.  9 entries
  hand-curated; each new vendor / parser version required a recipe edit.
* **L2 — per-command zero-ETL views** (``v_show_<safe_command>_auto``)
  auto-generated from any non-empty ``parsed_outputs.command`` via
  DuckDB's ``unnest(from_json(parsed_data, json_structure(...)),
  recursive := true)``.

The L1 layer was the same hardcoded mapping that R78 deleted from
Python's ``_DISCOVERY_INTENTS`` / ``_PLATFORM_CONFIG_CMDS``,
re-spelled in YAML.  The user pushback in R83.2 ("A reverts to
hardcoding") was correct and applies to **all** L1 recipes — keeping
BGP/OSPF/L2 was a comfortable historical exception, not a principled
choice.

This file now keeps only:

* :func:`build_per_command_views`  — every command in parsed_outputs
  becomes a typed view via JSON-structure inference.  Pure
  introspection, zero recipes.
* :func:`build_l2_topology_view`   — minimal projection of
  ``netops.topology_links`` as ``netops.v_l2_links_auto`` (hand-coded
  but trivial — no field mappings, no vendor split, just column
  rename for backwards compatibility with consumers).
* :func:`finalise_ingest`          — the single public entry-point
  called by ``/netops_init`` Stage 3.7 and ``take_snapshot``
  post-bulk-load.

State canonicalisation (``Estab`` → ``Established``, ``FULL/DR`` →
``Full/DR``) moved to :mod:`olav_netops.tools.field_normalizer`
applied at ingest time; ``parsed_outputs.parsed_data`` already holds
the RFC names by the time any view reads it.

What was removed
----------------

* ``_BGP_STATE_CASE`` / ``_OSPF_STATE_CASE`` (SQL CASE expressions)
* ``_bgp_branch`` / ``_ospf_branch`` / ``_custom_branch`` (per-vendor
  branch builders)
* ``_BRANCH_BUILDERS`` dispatch + ``_generate_sql_branch``
* ``_fetch_recipes`` / ``ensure_view_recipes_table``
* ``_PROTOCOL_VIEW_NAMES`` / ``view_name_for``
* ``build_one_view`` / ``build_all_views``
* ``rebuild_views_for_command`` (no recipes to match against)

The orphan ``view_recipes`` table is left in the schema for now —
empty after the migration — and can be DROPped on the next round
once any external readers (audit profiles, devops scripts) have
been audited to confirm they don't reference it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


_VIEW_NAME_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _safe_view_name(command: str) -> str:
    """Turn a CLI command into a safe view-name suffix.

    ``"show ip interface brief"`` → ``"show_ip_interface_brief"``;
    ``"show bgp summary | display set"`` → ``"show_bgp_summary_display_set"``.
    Strips leading/trailing underscores; lower-cases.
    """
    return _VIEW_NAME_RE.sub("_", command.strip().lower()).strip("_")


# ── L2 topology view (minimal projection) ───────────────────────────────

def build_snapshots_view(con: Any) -> dict[str, int]:
    """Create / refresh ``netops.v_snapshots_auto``.

    Per-snapshot metadata view: capture timestamp, device coverage,
    row count.  Lets downstream consumers (analyzer, audit, reports)
    anchor every artifact in real time without guessing today's date.

    Source: ``netops.parsed_outputs`` (snapshot_id + ingested_at).
    If ``ingested_at`` is NULL for older rows, fall back to parsing
    the timestamp encoded in the snapshot_id itself (snap_YYYYMMDD_HHMMSS_*).

    Columns:
        snapshot_id    VARCHAR
        captured_at    TIMESTAMP  (MIN ingested_at, or parsed from snap_id)
        finished_at    TIMESTAMP  (MAX ingested_at, or parsed from snap_id)
        device_count   INT
        row_count      INT
        duration_s     DOUBLE     (NULL when single-row snapshots)
    """
    try:
        con.execute(
            """
            CREATE OR REPLACE VIEW netops.v_snapshots_auto AS
            WITH derived AS (
                SELECT
                    snapshot_id,
                    MIN(ingested_at) AS min_ingested,
                    MAX(ingested_at) AS max_ingested,
                    COUNT(DISTINCT device_name) AS device_count,
                    COUNT(*) AS row_count
                FROM netops.parsed_outputs
                WHERE snapshot_id IS NOT NULL
                GROUP BY snapshot_id
            )
            SELECT
                snapshot_id,
                COALESCE(
                    min_ingested,
                    CASE
                        WHEN snapshot_id LIKE 'snap\\_%' ESCAPE '\\'
                        THEN try_strptime(substr(snapshot_id, 6, 15), '%Y%m%d_%H%M%S')
                        ELSE NULL
                    END
                ) AS captured_at,
                COALESCE(
                    max_ingested,
                    CASE
                        WHEN snapshot_id LIKE 'snap\\_%' ESCAPE '\\'
                        THEN try_strptime(substr(snapshot_id, 6, 15), '%Y%m%d_%H%M%S')
                        ELSE NULL
                    END
                ) AS finished_at,
                device_count,
                row_count,
                CASE
                    WHEN min_ingested IS NOT NULL AND max_ingested IS NOT NULL
                    THEN EPOCH(max_ingested - min_ingested)
                    ELSE NULL
                END AS duration_s
            FROM derived
            ORDER BY captured_at DESC NULLS LAST
            """
        )
        n = con.execute("SELECT COUNT(*) FROM netops.v_snapshots_auto").fetchone()[0]
        return {"v_snapshots_auto": int(n)}
    except Exception as exc:
        logger.warning("build_snapshots_view failed: %s", exc)
        return {}


def build_l2_topology_view(con: Any) -> dict[str, int]:
    """Create / refresh ``netops.v_l2_links_auto``.

    Pure column-rename projection of ``netops.topology_links`` —  no
    field mappings, no vendor split.  Kept as a stable name so
    downstream consumers (analyze skill SQL examples, audit profiles)
    don't have to know about the ``topology_links`` table directly.
    """
    try:
        con.execute(
            """
            CREATE OR REPLACE VIEW netops.v_l2_links_auto AS
            SELECT
                source_device,
                source_interface,
                destination_device,
                destination_interface,
                discovery_protocol,
                link_status,
                snapshot_id
            FROM netops.topology_links
            """
        )
        n = con.execute("SELECT COUNT(*) FROM netops.v_l2_links_auto").fetchone()[0]
        return {"v_l2_links_auto": int(n)}
    except Exception as exc:
        logger.warning("build_l2_topology_view failed: %s", exc)
        return {}


# ── DuckDB-native zero-ETL: per-command auto-views ──────────────────────
#
# For every command in ``netops.parsed_outputs``, materialise (as a
# VIEW, so no storage cost) a typed table where each column
# corresponds to a key in the parser's JSON output.
#
# Pattern (DuckDB-native, no field mappings, no recipes):
#
#   CREATE OR REPLACE VIEW netops.v_<safe_command>_auto AS
#   SELECT p.device_name, p.snapshot_id,
#          unnest(from_json(p.parsed_data, '<inferred-structure>'),
#                 recursive := true)
#   FROM netops.parsed_outputs p
#   WHERE p.command = '<command>'
#     AND p.snapshot_id = (SELECT MAX(snapshot_id) FROM netops.parsed_outputs s
#                          WHERE s.device_name = p.device_name AND s.command = p.command)
#
# ``json_structure`` infers the JSON shape from a sample row; the
# resulting view has typed columns and DESCRIBE works natively.  Agent
# can ``SELECT col FROM v_show_ip_interface_brief_auto WHERE status LIKE '%down%'``
# without any LATERAL+json_each gymnastics.

def build_per_command_views(con: Any) -> dict[str, int]:
    """Auto-create ``v_<safe_command>_auto`` views from parsed_outputs.

    Iterates every distinct command that has at least one parsed row,
    detects the JSON structure with ``json_structure(parsed_data)``,
    and CREATE OR REPLACE VIEW with ``unnest(from_json(...), recursive
    := true)`` — DuckDB exposes typed columns identical to the
    parser's output schema.

    Per-(device, command) latest-snapshot filter is applied inside the
    view so consumers don't need to know about snapshots.

    Idempotent; safe to re-run on every ``/netops_init`` and after any
    new parser is learned.

    Returns ``{view_name: row_count}`` for each successfully-created
    view; commands whose JSON structure couldn't be inferred (empty
    arrays, scalars, etc.) are silently skipped.
    """
    results: dict[str, int] = {}
    try:
        # Pick the most-recent non-empty parsed row per command and
        # ask DuckDB for its JSON structure.  ``json_structure``
        # returns a string like ``'[{"interface":"VARCHAR",...}]'``
        # which becomes the constant we bake into the view DDL.
        rows = con.execute(
            """
            WITH ranked AS (
                SELECT command, parsed_data,
                       ROW_NUMBER() OVER (PARTITION BY command ORDER BY ingested_at DESC NULLS LAST) AS rn
                FROM netops.parsed_outputs
                WHERE parsed_data IS NOT NULL
                  AND parsed_data::VARCHAR NOT IN ('[]', 'null')
            )
            SELECT command, json_structure(parsed_data)::VARCHAR AS struct
            FROM ranked
            WHERE rn = 1
            """
        ).fetchall()
    except Exception as exc:
        logger.warning("build_per_command_views: parsed_outputs scan failed: %s", exc)
        return results

    for command, structure in rows:
        if not command or not structure:
            continue
        # Only object-list shapes work — scalar / mixed shapes can't be
        # unnested into named columns.  Skip silently.
        if not structure.startswith("[{"):
            continue

        view_suffix = _safe_view_name(command)
        if not view_suffix:
            continue
        view_name = f"v_{view_suffix}_auto"

        cmd_lit = command.replace("'", "''")
        struct_lit = structure.replace("'", "''")

        ddl = f"""
            CREATE OR REPLACE VIEW netops.{view_name} AS
            SELECT p.device_name,
                   p.snapshot_id,
                   unnest(from_json(p.parsed_data, '{struct_lit}'), recursive := true)
            FROM netops.parsed_outputs p
            WHERE p.command = '{cmd_lit}'
              AND p.parsed_data IS NOT NULL
              AND p.snapshot_id = (
                  SELECT MAX(snapshot_id)
                  FROM netops.parsed_outputs s
                  WHERE s.device_name = p.device_name
                    AND s.command = p.command
              )
        """
        try:
            con.execute(ddl)
            count = con.execute(f"SELECT COUNT(*) FROM netops.{view_name}").fetchone()[0]
            results[view_name] = int(count)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "build_per_command_views: skipping %s (cmd=%r): %s",
                view_name, command, exc,
            )
    return results


# ── R83.3: Value profile + introspection cache ──────────────────────────
#
# Two materialised tables built at the end of every finalise_ingest:
#
# * ``netops.value_profile``   — for each per-command auto-view, every
#   low-cardinality (≤ 30 distinct) string column gets its (value, freq,
#   fingerprint) triplet.  Fingerprint = OpenRefine-style canonicalisation
#   (lower → strip punctuation → token-sort → join) — same algorithm the
#   OpenRefine GUI uses for its "Cluster" feature.  No new dependency;
#   pure DuckDB SQL.
#
# * ``netops.introspection_cache`` — a single-row table aggregating fleet
#   diversity + view list + per-view typed columns + value distributions
#   into one JSON bag.  Agent reads it ONCE per NL question and gets the
#   full data dictionary; subsequent SQL is the actual data fetch.
#
# Why: agent NL question previously needed 4-6 SQL round-trips
# (introspect platforms, list views, DESCRIBE per view, then data SELECT).
# With the cache, that drops to 2 (introspect, data).  Small models in
# particular benefit because they don't have to remember to do
# step-by-step introspection — the cache hands them everything in one
# read.  See dev_docs/56. THREE_LAYER_INTROSPECTION_CACHE.md for design.


def _ensure_value_profile_table(con: Any) -> None:
    """DDL for ``netops.value_profile``.  Idempotent."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS netops.value_profile (
            view_name    VARCHAR NOT NULL,
            column_name  VARCHAR NOT NULL,
            value        VARCHAR,
            freq         INTEGER NOT NULL,
            fingerprint  VARCHAR,
            cluster_id   INTEGER,
            PRIMARY KEY (view_name, column_name, value)
        )
        """
    )


# R83.3 Phase A.5: split column scope into "categorical" (worth profiling)
# vs "state-like" (worth surfacing in the introspection_cache JSON).
#
# Identity / dimensional columns add noise to the cache without giving the
# agent useful equivalence-class info — exclude from value_profile entirely.
# Things like device_name, snapshot_id, ip_address, mac_address are
# already addressable via netops.devices / dimensional joins and don't
# benefit from fingerprint clustering.

_VALUE_PROFILE_SKIP_COLS = frozenset({
    "device_name", "hostname", "snapshot_id",
    "ip_address", "address", "neighbor_ip", "router_id",
    "mac_address", "hardware_address", "physical_address",
    "interface", "local_interface", "neighbor_interface",  # interface NAMES
    "port",                              # interface short name
    "name", "neighbor_name", "platform_id", "platform",
    "vlan_id", "neighbor_id",
    "prefix", "network", "next_hop",
})

# Columns whose distinct values are worth surfacing in
# introspection_cache.value_distributions JSON (i.e. concepts the agent
# should consider when writing WHERE clauses).  Pattern-matched
# substring against column name (case-insensitive).
import re as _re_mod
_STATE_COL_PATTERN = _re_mod.compile(
    r"(state|status|proto|protocol|line_protocol|admin|oper)$",
    _re_mod.IGNORECASE,
)


def _is_state_like_col(name: str) -> bool:
    """Heuristic: column name suggests a finite-state concept worth canonicalising."""
    n = (name or "").strip().lower()
    if not n:
        return False
    return bool(_STATE_COL_PATTERN.search(n))


def build_value_profile(con: Any, *, max_cardinality: int = 30) -> dict[str, int]:
    """Profile every low-cardinality categorical column in the auto-views.

    Iterates ``information_schema.views`` for ``v_%_auto`` views in
    ``netops``, finds each VARCHAR column whose distinct-count is at
    most ``max_cardinality``, and writes one row per (view, column,
    value) into ``netops.value_profile`` along with an OpenRefine-style
    fingerprint and a cluster id.

    Skips identity / dimensional columns (``_VALUE_PROFILE_SKIP_COLS``)
    so the table holds only categorical/state-like data worth
    fingerprint clustering.

    Cluster id is currently per-fingerprint within a (view, column);
    Levenshtein-merge of similar fingerprints is a Phase-B follow-up
    (see design doc §3 Phase B).
    """
    _ensure_value_profile_table(con)
    con.execute("DELETE FROM netops.value_profile")  # full refresh per init

    views = con.execute(
        """
        SELECT table_name FROM information_schema.views
         WHERE table_schema='netops' AND table_name LIKE 'v_%_auto'
        """
    ).fetchall()

    for (view_name,) in views:
        try:
            cols = con.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_schema='netops' AND table_name = ?
                   AND data_type IN ('VARCHAR', 'TEXT')
                """,
                [view_name],
            ).fetchall()
        except Exception as exc:
            logger.debug("value_profile: column scan for %s failed: %s", view_name, exc)
            continue

        for (col,) in cols:
            # Identity / dimensional columns — skip (no clustering value).
            if col.lower() in _VALUE_PROFILE_SKIP_COLS:
                continue
            try:
                n = con.execute(
                    f'SELECT COUNT(DISTINCT "{col}") FROM netops."{view_name}"'
                ).fetchone()[0]
            except Exception as exc:
                logger.debug("value_profile: cardinality probe %s.%s failed: %s",
                             view_name, col, exc)
                continue
            if not n or n > max_cardinality:
                continue
            try:
                con.execute(
                    f"""
                    INSERT OR REPLACE INTO netops.value_profile
                        (view_name, column_name, value, freq, fingerprint, cluster_id)
                    WITH raw AS (
                        SELECT "{col}" AS value, COUNT(*) AS freq,
                               array_to_string(
                                   list_distinct(list_sort(
                                       regexp_split_to_array(
                                           lower(regexp_replace(
                                               CAST("{col}" AS VARCHAR),
                                               '[^a-z0-9 ]', '', 'g')),
                                           '\\s+'))),
                                   ' ') AS fingerprint
                        FROM netops."{view_name}"
                        WHERE "{col}" IS NOT NULL
                        GROUP BY "{col}"
                    )
                    SELECT '{view_name}' AS view_name,
                           '{col}'      AS column_name,
                           value, freq, fingerprint,
                           dense_rank() OVER (ORDER BY fingerprint) AS cluster_id
                      FROM raw
                    """
                )
            except Exception as exc:
                logger.debug("value_profile: insert %s.%s failed: %s",
                             view_name, col, exc)

    rows = con.execute("SELECT COUNT(*) FROM netops.value_profile").fetchone()[0]
    return {"value_profile_rows": int(rows)}


def build_introspection_cache(con: Any) -> dict[str, int]:
    """Single-row JSON-bag table summarising the fleet + views + values.

    Replaces 4-6 separate introspection queries on every NL question
    with one ``SELECT * FROM netops.introspection_cache``.

    The JSON columns are intentionally pre-aggregated (not lazy
    sub-selects) — querying this table is O(1) once it's built.
    Refreshed on every ``finalise_ingest`` call.
    """
    try:
        con.execute(
            """
            CREATE OR REPLACE TABLE netops.introspection_cache AS
            SELECT
                (SELECT json_group_array(json_object(
                            'hostname', hostname,
                            'platform', platform,
                            'role',     role,
                            'site',     site,
                            'ip',       ip_address))
                 FROM netops.devices)                                          AS fleet,

                (SELECT json_group_array(json_object('view', view_name, 'cols', cols))
                 FROM (
                     -- json_group_array is a MACRO (not an aggregate) so it
                     -- doesn't accept ORDER BY in DuckDB.  Pre-order the
                     -- columns in a sub-CTE and aggregate the already-sorted
                     -- list.
                     WITH ordered_cols AS (
                         SELECT v.table_name AS view_name,
                                c.column_name, c.data_type, c.ordinal_position
                         FROM information_schema.views v
                         JOIN information_schema.columns c
                              ON c.table_schema = v.table_schema
                             AND c.table_name = v.table_name
                         WHERE v.table_schema = 'netops'
                           AND v.table_name LIKE 'v_%_auto'
                         ORDER BY v.table_name, c.ordinal_position
                     )
                     SELECT view_name,
                            json_group_array(json_object(
                                'name', column_name,
                                'type', data_type)) AS cols
                     FROM ordered_cols
                     GROUP BY view_name
                 ))                                                            AS views,

                -- R83.3 Phase A.5: only state-like columns make it into
                -- the cache JSON.  Full categorical profile remains
                -- queryable via ``SELECT * FROM netops.value_profile``
                -- when the agent needs interface lists, MAC tables, etc.
                -- This keeps the JSON bag tight (≤ a few KB on a small
                -- fleet) so small models can parse it reliably.
                (SELECT json_group_array(json_object(
                            'view',    view_name,
                            'col',     column_name,
                            'value',   value,
                            'freq',    freq,
                            'cluster', cluster_id))
                 FROM netops.value_profile
                 WHERE regexp_matches(lower(column_name),
                                       '(state|status|proto|protocol|admin|oper)$'))
                                                                              AS value_distributions,

                NOW() AS refreshed_at
            """
        )
        return {"introspection_cache_rows": 1}
    except Exception as exc:
        logger.warning("build_introspection_cache failed: %s", exc)
        return {}


# ── Finalise-ingest entry point ─────────────────────────────────────────

def finalise_ingest(con: Any) -> dict[str, Any]:
    """Rebuild every view consumers query, in the right order.

    Called after any code path that writes to ``netops.parsed_outputs``
    or ``netops.topology_links`` (``/netops_init`` Stage 3.7 and
    ``take_snapshot`` post-bulk-load).  Idempotent — safe to call
    repeatedly; CREATE OR REPLACE handles schema drift in either
    direction.

    Returns a stat dict::

        {
          "l2": {"v_l2_links_auto": N},
          "per_command": {<v_show_..._auto>: N, ...},
        }

    Failures in either layer log at WARN but don't raise — view
    building is advisory; raw ``parsed_outputs`` queries always work.
    """
    out: dict[str, Any] = {"snapshots": {}, "l2": {}, "per_command": {}, "value_profile": {}, "introspection": {}}
    try:
        out["snapshots"] = build_snapshots_view(con)
    except Exception as exc:
        logger.warning("finalise_ingest: build_snapshots_view failed: %s", exc)
    try:
        out["l2"] = build_l2_topology_view(con)
    except Exception as exc:
        logger.warning("finalise_ingest: build_l2_topology_view failed: %s", exc)
    try:
        out["per_command"] = build_per_command_views(con)
    except Exception as exc:
        logger.warning("finalise_ingest: build_per_command_views failed: %s", exc)
    # R83.3: profile + cache run AFTER per-command views exist (they
    # scan those views for categorical distributions).  Both depend on
    # netops.devices being populated — so the caller must run
    # populate_devices BEFORE finalise_ingest.
    try:
        out["value_profile"] = build_value_profile(con)
    except Exception as exc:
        logger.warning("finalise_ingest: build_value_profile failed: %s", exc)
    try:
        out["introspection"] = build_introspection_cache(con)
    except Exception as exc:
        logger.warning("finalise_ingest: build_introspection_cache failed: %s", exc)
    # Both primers below write embeddings. Ask once whether this install can
    # embed at all, instead of letting each entry find out for itself.
    #
    # An install with no embedding backend has no AutoRecall either — the two
    # arrive together with the agent runtime — so priming there is a writer with
    # no reader: on a skill-pack machine (Claude Code reads the same guides
    # straight off disk as `references/*.guide.yaml`) it produced four WARN
    # lines per ingest and nothing consumed the result. Not a "standalone mode"
    # flag: the question is whether an embedding backend exists, which is worth
    # asking in either runtime.
    embed_ready, embed_reason = True, ""
    try:
        from olav.core.embedder import embedding_backend_ready
        embed_ready, embed_reason = embedding_backend_ready()
    except Exception as exc:  # noqa: BLE001 — never fail ingest over this
        logger.debug("finalise_ingest: embedding capability check failed: %s", exc)

    if not embed_ready:
        logger.info("finalise_ingest: skipping memory priming — %s", embed_reason)
        out["memory_primer"] = {"skipped": embed_reason}
        out["usage_guides"] = {"skipped": embed_reason}
        return out

    # R83.4: prime LanceDB memory with schema + value-distribution entries.
    # AutoRecallMiddleware then injects them on every NL query via
    # ``<relevant-memories>`` — agent doesn't have to know to call
    # introspection_cache or DESCRIBE first.
    try:
        from olav_netops.core.memory_primer import prime_memory_at_ingest
        out["memory_primer"] = prime_memory_at_ingest(con)
    except Exception as exc:
        logger.warning("finalise_ingest: prime_memory_at_ingest failed: %s", exc)

    # Phase 1 (dev_docs/57 MEMORY_DRIVEN_USAGE_GUIDES) — bridge
    # procedural ``*.guide.yaml`` files into LanceDB ``usage_guide``.
    # The platform owns the upsert path (``olav.core.memory.guide_kb``);
    # netops just hands it the runtime workspace root.  Same retrieval
    # path (AutoRecallMiddleware) — guides surface alongside schema +
    # value entries when intent matches.
    try:
        from pathlib import Path
        from olav.core.memory.guide_kb import prime_guides_from_dir
        workspace_root = Path.cwd() / ".olav" / "workspace"
        if not workspace_root.exists():
            try:
                from olav.core.config import get_paths_config
                workspace_root = Path(get_paths_config().workspace_dir)
            except Exception:  # noqa: BLE001
                workspace_root = None
        if workspace_root is not None and workspace_root.exists():
            out["usage_guides"] = prime_guides_from_dir(workspace_root)
        else:
            out["usage_guides"] = {"guide_entries": 0, "skipped": 0}
    except Exception as exc:
        logger.warning("finalise_ingest: prime_guides_from_dir failed: %s", exc)
    return out


__all__ = [
    "build_l2_topology_view",
    "build_per_command_views",
    "finalise_ingest",
]
