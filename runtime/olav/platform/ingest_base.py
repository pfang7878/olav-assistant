"""Base classes for domain-package DuckDB table registration.

Domain packages declare their own DuckDB tables by subclassing
``BaseIngestTable`` and registering with ``TableRegistry``.  The platform
``IngestManager`` calls ``TableRegistry.ensure_all_schemas()`` before every
bulk load so that tables are created idempotently at runtime — no hand-written
SQL migration scripts required for the happy path.

Design reference: dev_docs/olav_platform.md §11.4 + §12.3

Usage example (in olav-netops)::

    from olav.platform.ingest_base import BaseIngestTable, ColumnDef, TableRegistry

    class ParsedOutputsTable(BaseIngestTable):
        schema_name = "netops"
        table_name  = "parsed_outputs"
        columns = [
            ColumnDef("device_name", "VARCHAR",   nullable=False),
            ColumnDef("command",     "VARCHAR",   nullable=False),
            ColumnDef("parsed_data", "JSON"),
            ColumnDef("snapshot_id", "VARCHAR"),
            ColumnDef("raw_output",  "TEXT"),
            ColumnDef("ingested_at", "TIMESTAMP"),
        ]
        conflict_key = ["device_name", "command", "snapshot_id"]

    TableRegistry.register(ParsedOutputsTable())
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ColumnDef:
    """Declarative column definition for a DuckDB table.

    Attributes:
        name:     Column name.
        type:     DuckDB type string, e.g. "VARCHAR", "JSON", "TIMESTAMP".
        nullable: Whether the column accepts NULL (default True).
    """

    name: str
    type: str
    nullable: bool = True


class BaseIngestTable(ABC):
    """Abstract base class for domain-package DuckDB table declarations.

    Subclasses declare ``schema_name``, ``table_name``, and ``columns`` as
    class attributes (or override the abstract properties).  The platform
    calls ``ensure_schema(conn)`` once per table during startup / bulk-load
    to guarantee the schema + table exist.

    The default ``schema_name`` is ``"main"`` for backward compatibility with
    the flat-table layout used before v0.12.  New domain packages must set a
    domain-specific schema (e.g. ``schema_name = "netops"``).
    """

    # -----------------------------------------------------------------
    # Required (abstract)
    # -----------------------------------------------------------------

    @property
    @abstractmethod
    def table_name(self) -> str:
        """Unqualified table name, e.g. ``"parsed_outputs"``."""
        ...

    @property
    @abstractmethod
    def columns(self) -> list[ColumnDef]:
        """Ordered list of column definitions."""
        ...

    # -----------------------------------------------------------------
    # Optional overrides
    # -----------------------------------------------------------------

    @property
    def schema_name(self) -> str:
        """DuckDB Schema that owns this table.

        Domain packages override this to isolate their data:
        ``"netops"``, ``"k8sops"``, ``"itsm"``, etc.

        Defaults to ``"main"`` for backward-compatible flat-table layout.
        """
        return "main"

    @property
    def conflict_key(self) -> list[str]:
        """Columns that form the ON CONFLICT key for upsert.

        Return an empty list (the default) if the table has no natural key
        or upsert behaviour is not needed.
        """
        return []

    # -----------------------------------------------------------------
    # Derived helpers
    # -----------------------------------------------------------------

    @property
    def qualified_name(self) -> str:
        """Fully qualified ``schema.table`` name.

        Example: ``"netops.parsed_outputs"``.
        """
        return f"{self.schema_name}.{self.table_name}"

    # -----------------------------------------------------------------
    # Schema + table DDL
    # -----------------------------------------------------------------

    def ensure_schema(self, conn) -> None:
        """Idempotently create the DuckDB Schema and table.

        Safe to call multiple times — uses ``CREATE … IF NOT EXISTS`` for
        both the schema and the table.  Existing columns are never modified
        (Schema-On-Read philosophy: structural evolution happens through
        staging + LLM semantic mapping, not ALTER TABLE).

        A ``UNIQUE`` constraint is added automatically when ``conflict_key``
        is non-empty so that ``ON CONFLICT`` upserts work correctly.

        Args:
            conn: An open ``duckdb.DuckDBPyConnection``.
        """
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema_name}")

        cols_sql = ",\n  ".join(
            f"{c.name} {c.type}{'' if c.nullable else ' NOT NULL'}" for c in self.columns
        )

        unique_clause = ""
        if self.conflict_key:
            key_cols = ", ".join(self.conflict_key)
            unique_clause = f",\n  UNIQUE ({key_cols})"

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.qualified_name} (
              {cols_sql}{unique_clause}
            )"""
        )


class TableRegistry:
    """Singleton registry of all domain ``BaseIngestTable`` instances.

    Domain packages register their tables at import time (or via entry
    points).  The platform ``IngestManager`` calls
    ``TableRegistry.ensure_all_schemas(conn)`` once per bulk-load cycle.

    Example::

        TableRegistry.register(ParsedOutputsTable())
        TableRegistry.register(PodMetricsTable())
    """

    _tables: dict[str, BaseIngestTable] = {}

    @classmethod
    def register(cls, table: BaseIngestTable) -> None:
        """Register a table instance under its ``table_name``."""
        cls._tables[table.table_name] = table

    @classmethod
    def ensure_all_schemas(cls, conn) -> None:
        """Call ``ensure_schema()`` for every registered table.

        Args:
            conn: An open ``duckdb.DuckDBPyConnection``.
        """
        for table in cls._tables.values():
            table.ensure_schema(conn)

    @classmethod
    def all_tables(cls) -> dict[str, BaseIngestTable]:
        """Return a copy of the registered tables dict."""
        return dict(cls._tables)

    @classmethod
    def get(cls, table_name: str) -> BaseIngestTable | None:
        """Return the registered table instance for *table_name*, or ``None``.

        Args:
            table_name: Unqualified table name, e.g. ``"parsed_outputs"``.
        """
        return cls._tables.get(table_name)
