"""NETOPS-ONLY DuckDB table declarations.

Registered via pyproject.toml entry-points so that the platform
``IngestManager`` can discover and create these tables at runtime::

    [project.entry-points."olav.ingest_tables"]
    parsed_outputs = "olav_netops.core.tables:ParsedOutputsTable"
    devices        = "olav_netops.core.tables:DevicesTable"
    topology_links = "olav_netops.core.tables:TopologyLinksTable"

All tables live in the ``netops`` DuckDB schema.
"""

from typing import Any

from olav.platform.ingest_base import BaseIngestTable, ColumnDef, TableRegistry


class ParsedOutputsTable(BaseIngestTable):
    """Stores parsed CLI output from network devices.

    raw_output is no longer stored inline — it is deduplicated in
    RawOutputStoreTable and referenced via raw_output_hash.
    The raw_output column is kept for backward compatibility but is
    nulled out during ingest migration.
    """

    schema_name = "netops"
    table_name = "parsed_outputs"
    columns = [
        ColumnDef("device_name", "VARCHAR", nullable=False),
        ColumnDef("command", "VARCHAR", nullable=False),
        ColumnDef("parsed_data", "JSON"),
        ColumnDef("snapshot_id", "VARCHAR"),
        ColumnDef("raw_output", "TEXT"),       # legacy — kept for compat, will be NULL
        ColumnDef("raw_output_hash", "VARCHAR"),  # FK → raw_output_store.content_hash
        ColumnDef("ingested_at", "TIMESTAMP"),
        # R-VERTICAL-SLICE 2026-05-09 (dev_docs/70): denormalised platform
        # tag.  Resolved at write time from Nornir host.platform; copied
        # here to avoid downstream JOINs to netops.devices for every
        # cross-vendor view / inspector.  Historical rows preserve the
        # platform AT capture time even if the device is later
        # re-platformed in inventory.
        ColumnDef("platform", "VARCHAR"),
    ]
    conflict_key = ["device_name", "command", "snapshot_id"]


class RawOutputStoreTable(BaseIngestTable):
    """Latest raw CLI output per device per command — a SINGLE-COPY,
    CURRENT-STATE table, NOT a per-snapshot history.

    conflict_key is ``(device_name, command)`` with NO ``snapshot_id`` —
    each ingest overwrites in place ("latest data wins"), so exactly one
    row exists per (device_name, command). `parsed_outputs` is the opposite:
    its key includes ``snapshot_id`` and it keeps per-snapshot history.

    ⚠️ CONSEQUENCE — do NOT filter raw_output_store by ``snapshot_id``.
    The ``snapshot_id`` column here is a *last-writer label* (the id of the
    import that most recently wrote this device/command), NOT a partition
    key. Two rows written by different imports carry different labels, and a
    "latest" snapshot whose import happened to omit ``show running-config``
    (state-only bundle) leaves the config text stamped under an OLDER label.
    So ``WHERE snapshot_id = <latest>`` silently returns 0 config rows even
    though the config is present. This exact trap caused the Batfish
    "No valid configurations" saga (config was in the store, under a
    non-latest label). Config-layer callers must resolve the right snapshot
    via ``batfish_q._latest_snapshot_with_configs()`` (or omit the snapshot
    filter entirely and match on ``command='show running-config'``).
    Enforced by tests/governance/test_raw_output_store_snapshot_filter.py.
    """

    schema_name = "netops"
    table_name = "raw_output_store"
    columns = [
        ColumnDef("device_name",  "VARCHAR",   nullable=False),
        ColumnDef("command",      "VARCHAR",   nullable=False),
        ColumnDef("raw_output",   "TEXT",      nullable=False),
        # last-writer label, NOT a partition key — see class docstring.
        # Never build a `WHERE snapshot_id = …` predicate against this table.
        ColumnDef("snapshot_id",  "VARCHAR"),
        ColumnDef("updated_at",   "TIMESTAMP"),
        # See ParsedOutputsTable.platform — same rationale.
        ColumnDef("platform",     "VARCHAR"),
    ]
    conflict_key = ["device_name", "command"]


class DevicesTable(BaseIngestTable):
    """Network device inventory.

    ARCH-08 Phase 2 Item 2 (Round 48): ``environment`` column carries the
    Nornir inventory ``data.environment`` tag (``lab`` / ``prod`` /
    ``staging`` / ``dev``) so queries and reports can filter or flag
    cross-environment hostname collisions. Nullable for backward-compat —
    a device without an inventory entry (e.g. discovered via LLDP) keeps
    NULL.
    """

    schema_name = "netops"
    table_name = "devices"
    columns = [
        ColumnDef("hostname", "VARCHAR", nullable=False),
        ColumnDef("ip_address", "VARCHAR"),
        ColumnDef("platform", "VARCHAR"),
        ColumnDef("site", "VARCHAR"),
        ColumnDef("role", "VARCHAR"),
        ColumnDef("vendor", "VARCHAR"),
        ColumnDef("model", "VARCHAR"),
        ColumnDef("os_version", "VARCHAR"),
        ColumnDef("environment", "VARCHAR"),  # ARCH-08 Phase 2 Item 2
        ColumnDef("last_seen", "TIMESTAMP"),
        ColumnDef("metadata", "JSON"),
    ]
    conflict_key = ["hostname"]


class TopologyLinksTable(BaseIngestTable):
    """CDP/LLDP/OSPF-derived network topology links.

    Schema matches the operational schema used by ``_discover_topology_from_db``
    in sync_tools.py — uses ``link_id`` as primary key.
    """

    schema_name = "netops"
    table_name = "topology_links"
    columns = [
        ColumnDef("link_id",               "VARCHAR",   nullable=False),
        ColumnDef("source_device",         "VARCHAR",   nullable=False),
        ColumnDef("source_interface",      "VARCHAR",   nullable=False),
        ColumnDef("destination_device",    "VARCHAR",   nullable=False),
        ColumnDef("destination_interface", "VARCHAR",   nullable=False),
        ColumnDef("discovery_protocol",    "VARCHAR"),
        ColumnDef("link_type",             "VARCHAR"),
        ColumnDef("link_status",           "VARCHAR"),
        ColumnDef("link_speed",            "VARCHAR"),
        ColumnDef("first_seen",            "TIMESTAMP", nullable=False),
        ColumnDef("last_seen",             "TIMESTAMP", nullable=False),
        ColumnDef("last_verified",         "TIMESTAMP"),
        ColumnDef("status_changes",        "INTEGER"),
        ColumnDef("snapshot_id",           "VARCHAR",   nullable=False),
        ColumnDef("platform",             "VARCHAR"),
    ]
    conflict_key = ["link_id"]


class OcOutputsTable(BaseIngestTable):
    """Strict OpenConfig JSON per device per snapshot per OC module."""

    schema_name = "netops"
    table_name = "oc_outputs"
    columns = [
        ColumnDef("device_name", "VARCHAR", nullable=False),
        ColumnDef("snapshot_id", "VARCHAR", nullable=False),
        ColumnDef("oc_module",   "VARCHAR", nullable=False),
        ColumnDef("oc_data",     "JSON",    nullable=False),
        ColumnDef("source_cmd",  "VARCHAR"),
    ]
    conflict_key = ["device_name", "snapshot_id", "oc_module"]


class CommandsTable(BaseIngestTable):
    """Command whitelist — R73 SSOT derived from ntc-templates + custom + PaC.

    Populated by :func:`olav_netops.core.commands_sync.sync_commands` at
    `/netops_init` Stage 0. Consumed by:
      * netops_init SSH collection(discovery list)
      * `execute_cli._validate_command`(agent CLI whitelist)
      * `search_commands` agent tool
    """

    schema_name = "netops"
    table_name = "commands"
    columns = [
        ColumnDef("platform",      "VARCHAR", nullable=False),
        ColumnDef("command",       "VARCHAR", nullable=False),
        ColumnDef("safe_command",  "VARCHAR", nullable=False),
        ColumnDef("parser_type",   "VARCHAR"),
        ColumnDef("parser_path",   "VARCHAR"),
        ColumnDef("blacklisted",   "BOOLEAN", nullable=False),
        ColumnDef("pipe_allowed",  "BOOLEAN", nullable=False),
        ColumnDef("backup_only",   "BOOLEAN", nullable=False),
        ColumnDef("synced_at",     "TIMESTAMP"),
    ]
    conflict_key = ["platform", "command"]


class BundleIngestsTable(BaseIngestTable):
    """One row per bundle ingest event — full provenance / chain-of-custody.

    Schema mirrors the CREATE TABLE in migrations/v0_22_portable_ingest.py;
    the class enables ``ensure_schema()`` on fresh DBs so the migration path
    only needs to cover in-place evolution of existing prod DBs.
    """

    schema_name = "netops"
    table_name = "bundle_ingests"
    columns = [
        ColumnDef("bundle_id",           "VARCHAR",   nullable=False),
        ColumnDef("snapshot_id",         "VARCHAR"),
        ColumnDef("bundle_sha256",       "VARCHAR",   nullable=False),
        ColumnDef("collector_name",      "VARCHAR"),
        ColumnDef("collector_version",   "VARCHAR"),
        ColumnDef("collected_at",        "TIMESTAMP"),
        ColumnDef("ingested_at",         "TIMESTAMP"),
        ColumnDef("ingested_by",         "VARCHAR"),
        ColumnDef("pre_scrubbed",        "BOOLEAN"),
        ColumnDef("salt_fingerprint",    "VARCHAR"),
        ColumnDef("hosts_count",         "INTEGER"),
        ColumnDef("commands_count",      "INTEGER"),
        ColumnDef("parser_fill_summary", "VARCHAR"),
    ]
    conflict_key = ["bundle_id"]


class ExplorationRunsTable(BaseIngestTable):
    """One row per /explore invocation — budget + outcome tracking.

    DDL mirrors migrations/v0_23_exploration.py; class enables
    ``ensure_schema()`` on fresh DBs.
    """

    schema_name = "netops"
    table_name = "exploration_runs"
    columns = [
        ColumnDef("run_id",            "VARCHAR",   nullable=False),
        ColumnDef("started_at",        "TIMESTAMP", nullable=False),
        ColumnDef("ended_at",          "TIMESTAMP"),
        ColumnDef("status",            "VARCHAR",   nullable=False),
        ColumnDef("snapshot_id",       "VARCHAR"),
        ColumnDef("requested_by",      "VARCHAR"),
        ColumnDef("budget_turns",      "INTEGER"),
        ColumnDef("budget_findings",   "INTEGER"),
        ColumnDef("budget_wall_sec",   "INTEGER"),
        ColumnDef("turns_used",        "INTEGER"),
        ColumnDef("findings_count",    "INTEGER"),
        ColumnDef("wall_sec_used",     "INTEGER"),
        ColumnDef("final_report_path", "VARCHAR"),
    ]
    conflict_key = ["run_id"]


class ExplorationFindingsTable(BaseIngestTable):
    """Per-finding scratchpad — LLM's external memory during exploration.

    Anti-fabrication invariants enforced in the migration DDL:
    * ``evidence_sql NOT NULL`` — every finding must be SQL-backed
    * ``UNIQUE (run_id, summary)`` — no duplicate findings per run
    """

    schema_name = "netops"
    table_name = "exploration_findings"
    columns = [
        ColumnDef("finding_id",       "VARCHAR",  nullable=False),
        ColumnDef("run_id",           "VARCHAR",  nullable=False),
        ColumnDef("recorded_at",      "TIMESTAMP", nullable=False),
        ColumnDef("phase",            "VARCHAR",  nullable=False),
        ColumnDef("category",         "VARCHAR"),
        ColumnDef("severity",         "VARCHAR",  nullable=False),
        ColumnDef("summary",          "VARCHAR",  nullable=False),
        ColumnDef("detail",           "TEXT"),
        ColumnDef("evidence_sql",     "TEXT",     nullable=False),
        ColumnDef("evidence_rows",    "VARCHAR"),
        ColumnDef("confidence",       "VARCHAR",  nullable=False),
        ColumnDef("related_findings", "JSON"),
    ]
    conflict_key = ["finding_id"]


# ARCH-24 removed (Round 70): BgpSessionsTable / OspfAdjacenciesTable
# were materialized by the now-deleted L3 ETL. Their data is now exposed
# through ``netops.v_bgp_neighbors_auto`` / ``netops.v_ospf_neighbors_auto``
# views created at Stage 3.7 by ``view_builder.build_all_views`` from
# ``view_recipes`` + ``netops.parsed_outputs``.


def _register_all() -> None:
    """Register all olav-netops tables into the TableRegistry.

    Called explicitly by the platform entry-point / plugin loader so that
    ``import olav_netops.core.tables`` alone does NOT trigger file-system reads
    or global state mutations — keeping the module side-effect free and
    making unit tests easier to isolate.
    """
    TableRegistry.register(ParsedOutputsTable())
    TableRegistry.register(RawOutputStoreTable())
    TableRegistry.register(DevicesTable())
    TableRegistry.register(TopologyLinksTable())
    TableRegistry.register(OcOutputsTable())
    TableRegistry.register(CommandsTable())
    TableRegistry.register(BundleIngestsTable())
    TableRegistry.register(ExplorationRunsTable())
    TableRegistry.register(ExplorationFindingsTable())


# Tables that existed in pre-R83 ETL designs but are no longer written to
# by any code path. R70 / R83 introduced ``v_*_auto`` views as the
# authoritative source; the underlying base tables were never DROP'd in
# the migration. Leaving them in the schema misleads ``database_introspection``
# and tools that pick tables by name (the audit profile-author bug surfaced
# in demo7 Chapter 6 — `bgp_neighbors` table was 0 rows but auditor still
# reported "✅ Healthy").
#
# The corresponding live source for each:
#   interfaces      → JSON-extract from parsed_outputs (no view yet)
#   bgp_neighbors   → v_bgp_neighbors_auto
#   bgp_routes      → JSON-extract from parsed_outputs
#   ospf_neighbors  → v_ospf_neighbors_auto
#   routes          → v_routes_auto
_LEGACY_DEAD_TABLES = (
    "interfaces",
    "bgp_neighbors",
    "bgp_routes",
    "ospf_neighbors",
    "routes",
)


def drop_legacy_tables(
    con: Any,
    schemas: tuple[str, ...] = ("netops", "main"),
) -> list[str]:
    """Drop pre-R83 dead tables across the relevant schemas.

    Sweeps both ``netops`` (where live tables live) and ``main``
    (DuckDB's default schema) — demo7 inspection in 2026-04-28 showed
    the 5 dead tables actually landed in ``main`` from earlier R70-era
    DDL, not ``netops``. The fix should clean both regardless of which
    schema migration accidentally created them.

    Idempotent — ``DROP TABLE IF EXISTS``. Returns ``"schema.table"``
    strings for everything actually dropped (best-effort;
    ``information_schema`` lookup so we can report what was cleaned).

    Run from netops_init Stage 3 right after ``ensure_all_schemas``.
    """
    dropped: list[str] = []
    for schema in schemas:
        for tbl in _LEGACY_DEAD_TABLES:
            try:
                existed = con.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = ? AND table_name = ?",
                    [schema, tbl],
                ).fetchone()
            except Exception:
                existed = None
            try:
                con.execute(f"DROP TABLE IF EXISTS {schema}.{tbl}")
                if existed:
                    dropped.append(f"{schema}.{tbl}")
            except Exception:
                # Don't fail the bootstrap on one stuck table — log via return
                pass
    return dropped
