"""Bulk Ingestion Manager for DuckDB.

Provides high-speed atomic merging of staging JSON files into main.duckdb.

Staging-First flow:
  1. Stage2 writes per-device  tmp/staging/{device}.staging.json
     (overwritten on each snapshot run — no accumulation).
  2. IngestManager.bulk_load() uses DuckDB read_json_auto for a single
     high-speed atomic write, avoiding per-row INSERT overhead.
  3. raw_output is stored in raw_output_store — one row per (device, command),
     always overwritten with the latest snapshot's data. No history, no dedup.
  4. Commands listed in .olav/config/backup_only_commands.yaml are written
     to exports/backup/{snapshot_id}/{device_name}.txt after each ingest.

Staging file schema (JSON array):
  [{"device_name": "R1", "command": "show version",
    "parsed_data": [{...}], "snapshot_id": "2026-03-01",
    "raw_output": "<raw CLI text>"}, ...]
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb

from olav.core.db_write import open_write_connection

from olav.core.config import BACKUP_DIR, MAIN_DB_PATH
from olav.platform.ingest_base import TableRegistry


def _load_backup_commands(db_path=None) -> frozenset[str]:
    """Read backup command list from the ``netops.commands`` DB table.

    R76 cutover: backup-command classification moved from
    ``backup_only_commands.yaml`` (read via ARCH-22 C
    ``find_backup_commands_yaml``) to the R73 SSOT
    ``netops.commands WHERE backup_only = true``.

    Falls back to YAML only if the table is absent (fresh install
    before the first ``sync_commands()`` run). That fallback will be
    removed once existing deployments are known to have upgraded.
    """
    try:
        import duckdb as _ddb
        from olav.core.config import MAIN_DB_PATH
        target = Path(db_path or MAIN_DB_PATH)
        with _ddb.connect(str(target), read_only=True) as conn:
            has_table = conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'netops' AND table_name = 'commands' LIMIT 1"
            ).fetchone()
            if has_table:
                rows = conn.execute(
                    "SELECT DISTINCT command FROM netops.commands "
                    "WHERE backup_only = true"
                ).fetchall()
                return frozenset(r[0] for r in rows)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "_load_backup_commands: DB path failed (%s); falling back to YAML", exc,
        )

    # Fallback: YAML file (pre-R73 deployments).
    from olav.core.utils import find_backup_commands_yaml
    yaml_path = find_backup_commands_yaml()
    if yaml_path is None:
        return frozenset()
    try:
        import yaml
        entries = yaml.safe_load(yaml_path.read_text()) or []
        return frozenset(
            e["command"] for e in entries
            if isinstance(e, dict) and e.get("command")
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not load %s: %s", yaml_path, exc)
        return frozenset()

logger = logging.getLogger(__name__)

_TABLE_PLUGINS_LOADED = False


def _load_table_plugins() -> None:
    """Auto-discover and register domain table definitions via entry-points.

    Loads the ``olav.ingest_tables`` entry-point group.  Each entry-point
    must point to a :class:`~olav.platform.ingest_base.BaseIngestTable`
    subclass; an instance is registered into ``TableRegistry``.

    This is a no-op after the first call (idempotent).  Falls back silently
    when no entry-points are installed (e.g., in test environments where
    domain packages are added to ``sys.path`` but not installed via pip).
    """
    global _TABLE_PLUGINS_LOADED
    if _TABLE_PLUGINS_LOADED:
        return
    _TABLE_PLUGINS_LOADED = True
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="olav.ingest_tables"):
            try:
                cls = ep.load()
                TableRegistry.register(cls())
                logger.debug("Registered ingest table plugin: %s → %s", ep.name, cls)
            except Exception as exc:
                logger.debug("Failed to load ingest table plugin %s: %s", ep.name, exc)
    except Exception as exc:
        logger.debug("Entry-point discovery failed: %s", exc)


class IngestManager:
    """Manager for bulk ingesting staging JSON files into DuckDB."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        staging_dir: str | Path | None = None,
        post_ingest_hooks: list[Callable[[dict[str, Any]], None]] | None = None,
    ) -> None:
        self.db_path = Path(db_path or MAIN_DB_PATH)
        if staging_dir is None:
            raise ValueError(
                "staging_dir is required. Domain packages must pass their own staging path. "
                "(olav-netops convention: EXPORTS_DIR / 'snapshots' / 'json')"
            )
        self.staging_dir = Path(staging_dir)
        # Optional callbacks invoked after a successful bulk_load().
        # Each hook receives the result dict; exceptions are logged, not raised.
        self._post_ingest_hooks: list[Callable[[dict[str, Any]], None]] = list(
            post_ingest_hooks or []
        )

    def bulk_load(self) -> dict:
        """Load all *.staging.json files into DuckDB via read_json_auto.

        Each staging file is a JSON array produced by _process_sync_stage2.
        Files are NOT deleted after ingest — they are overwritten by the next
        snapshot run (Schema-On-Read, idempotent upsert).

        Returns:
            Dict with status, files_processed, records_inserted, snapshot_ids.
        """
        _load_table_plugins()
        staging_files = list(self.staging_dir.glob("*.staging.json"))

        if not staging_files:
            return {"status": "no_files", "files_processed": 0, "records_inserted": 0}

        staging_pattern = (self.staging_dir / "*.staging.json").as_posix()

        _tbl = TableRegistry.get("parsed_outputs")
        _store_tbl = TableRegistry.get("raw_output_store")
        target_table = _tbl.qualified_name if _tbl else "netops.parsed_outputs"
        store_table = _store_tbl.qualified_name if _store_tbl else "netops.raw_output_store"

        try:
            with open_write_connection(str(self.db_path)) as conn:
                if _tbl is not None:
                    _tbl.ensure_schema(conn)
                if _store_tbl is not None:
                    _store_tbl.ensure_schema(conn)

                # Step 1: Upsert raw_output_store — latest data wins per (device, command)
                # R-VERTICAL-SLICE 2026-05-09: ``platform`` denormalised at
                # write time (writers include it in staging records).
                conn.execute(f"""
                    INSERT INTO {store_table}
                        (device_name, command, raw_output, snapshot_id, updated_at, platform)
                    SELECT device_name, command, raw_output, snapshot_id, NOW(),
                           platform
                    FROM read_json_auto('{staging_pattern}', format='array', ignore_errors=true)
                    WHERE raw_output IS NOT NULL AND raw_output != ''
                    ON CONFLICT (device_name, command)
                    DO UPDATE SET
                        raw_output  = EXCLUDED.raw_output,
                        snapshot_id = EXCLUDED.snapshot_id,
                        updated_at  = NOW(),
                        platform    = EXCLUDED.platform
                """)

                # Step 2: Upsert parsed_outputs (no inline raw_output)
                conn.execute(f"""
                    INSERT INTO {target_table}
                        (device_name, command, parsed_data, snapshot_id, platform)
                    SELECT device_name, command, parsed_data::JSON, snapshot_id,
                           platform
                    FROM read_json_auto('{staging_pattern}', format='array', ignore_errors=true)
                    WHERE parsed_data IS NOT NULL
                    ON CONFLICT (device_name, command, snapshot_id)
                    DO UPDATE SET
                        parsed_data = EXCLUDED.parsed_data,
                        platform    = EXCLUDED.platform
                """)

                rows_inserted = conn.execute(
                    f"SELECT COUNT(*) FROM read_json_auto('{staging_pattern}', format='array', ignore_errors=true)"
                ).fetchone()
                snapshot_ids = conn.execute(
                    f"SELECT DISTINCT snapshot_id FROM read_json_auto('{staging_pattern}', format='array', ignore_errors=true) WHERE snapshot_id IS NOT NULL"
                ).fetchall()

            inserted = rows_inserted[0] if rows_inserted else len(staging_files)
            logger.info(
                "IngestManager: loaded %d staging files, ~%d records",
                len(staging_files),
                inserted,
            )
            result: dict[str, Any] = {
                "status": "success",
                "files_processed": len(staging_files),
                "records_inserted": inserted,
                "snapshot_ids": [r[0] for r in snapshot_ids],
            }

        except Exception as e:
            logger.error("Bulk ingestion failed: %s", e)
            return {
                "status": "error",
                "message": str(e),
                "files_processed": 0,
                "records_inserted": 0,
            }

        try:
            self._backup_commands(result.get("snapshot_ids", []))
        except Exception as exc:
            logger.warning("Command backup failed: %s", exc)

        # Invalidate semantic cache — ingested data may change query results.
        #
        # Only when this install can embed. Opening the store on a machine with
        # no embedding backend makes it probe for a vector width it will never
        # get, which logged two WARNs about a cache that does not exist — an
        # ingest on a skill-pack install (no memory layer, no AutoRecall) hit
        # this before it reached any of its own work.
        try:
            from olav.core.embedder import embedding_backend_ready
            ready, reason = embedding_backend_ready()
            if not ready:
                logger.debug("SemanticCache invalidate skipped — %s", reason)
            else:
                from olav.core.memory import SemanticCache, get_store
                _store = get_store()
                if _store:
                    SemanticCache(_store).invalidate_all()
                    logger.info("SemanticCache invalidated after data ingest")
        except Exception as e:
            logger.debug("SemanticCache invalidate failed: %s", e)

        for hook in self._post_ingest_hooks:
            try:
                hook(result)
            except Exception as hook_exc:  # noqa: BLE001
                logger.warning("post_ingest_hook %r failed: %s", hook, hook_exc)

        return result

    def _backup_commands(self, snapshot_ids: list[str]) -> None:
        """Write raw output for backup_only_commands.yaml entries to disk.

        Output: BACKUP_DIR / {snapshot_id} / {device_name}_{command_slug}.txt
        R76 cutover: backup-command list comes from ``netops.commands``
        (populated by ``sync_commands`` at /netops_init Stage 0). The
        ``backup_only_commands.yaml`` file is a fallback for pre-R73 deployments.
        """
        backup_commands = _load_backup_commands(self.db_path)
        if not backup_commands or not snapshot_ids:
            return

        _store_tbl = TableRegistry.get("raw_output_store")
        store_table = _store_tbl.qualified_name if _store_tbl else "netops.raw_output_store"

        placeholders = ",".join(["?"] * len(backup_commands))

        try:
            with duckdb.connect(str(self.db_path), read_only=True) as conn:
                rows = conn.execute(
                    f"""
                    SELECT device_name, command, raw_output, snapshot_id
                    FROM {store_table}
                    WHERE command IN ({placeholders})
                      AND raw_output IS NOT NULL
                    """,
                    backup_commands,
                ).fetchall()
        except Exception as exc:
            logger.warning("Backup query failed: %s", exc)
            return

        for device_name, command, raw_output, snapshot_id in rows:
            snap = snapshot_id or "unknown"
            out_dir = Path(BACKUP_DIR) / snap
            out_dir.mkdir(parents=True, exist_ok=True)
            slug = command.replace(" ", "_").replace("/", "-")
            out_file = out_dir / f"{device_name}_{slug}.txt"
            out_file.write_text(raw_output, encoding="utf-8")
            logger.debug("Backup written: %s", out_file)

        if rows:
            logger.info("Command backup: %d files written to %s", len(rows), BACKUP_DIR)
