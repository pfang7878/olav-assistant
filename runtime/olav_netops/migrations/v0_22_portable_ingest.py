"""Migration v0.22: portable-snapshot-ingest schema additions (netops DB).

Backs the portable-snapshot-ingest design (``dev_docs/76``).

Adds:

  * ``netops.raw_output_store.bundle_id``       — uuid of the source bundle
  * ``netops.raw_output_store.bundle_sha256``   — content hash from bundle manifest
  * ``netops.raw_output_store.ingested_via``    — 'bundle' / 'rancid' / 'dump' / NULL (live SSH)
  * ``netops.bundle_ingests``                   — one row per ingest event
                                                  (full provenance / chain-of-custody)

Idempotent — safe to apply against fresh DBs and against DBs already at
v0.22+.  The matching ``RawOutputStoreTable.columns`` list in
``olav_netops.core.tables`` carries the same three columns so fresh DBs
land at the right shape via the normal ``ensure_schema()`` path; this
migration covers the in-place evolution case for existing prod DBs.
"""
from __future__ import annotations


def apply_migration(conn) -> None:
    """Idempotently ALTER raw_output_store + CREATE bundle_ingests."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS netops")

    # ── raw_output_store: additive columns ──────────────────────────
    for col in ("bundle_id", "bundle_sha256", "ingested_via", "platform"):
        conn.execute(
            f"ALTER TABLE netops.raw_output_store "
            f"ADD COLUMN IF NOT EXISTS {col} VARCHAR"
        )

    # ── parsed_outputs: platform column (skip if table absent — fresh DBs
    # already have `platform` via ParsedOutputsTable.ensure_schema()) ──
    try:
        conn.execute(
            "ALTER TABLE netops.parsed_outputs "
            "ADD COLUMN IF NOT EXISTS platform VARCHAR"
        )
    except Exception:
        pass

    # ── bundle_ingests: one row per ingest event ──────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS netops.bundle_ingests (
            bundle_id           VARCHAR PRIMARY KEY,
            snapshot_id         VARCHAR,
            bundle_sha256       VARCHAR NOT NULL,
            collector_name      VARCHAR,
            collector_version   VARCHAR,
            collected_at        TIMESTAMP,
            ingested_at         TIMESTAMP DEFAULT current_timestamp,
            ingested_by         VARCHAR,
            pre_scrubbed        BOOLEAN,
            salt_fingerprint    VARCHAR,
            hosts_count         INTEGER,
            commands_count      INTEGER,
            parser_fill_summary VARCHAR
        )
    """)
