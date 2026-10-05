#!/usr/bin/env python3
"""
Database Tool - Intelligent SQL query execution with auto schema exploration.

Core Features:
1. Auto schema discovery (no manual inspect_schema calls)
2. SQL generation with context
3. Error self-correction (via Agent ReAct loop)
4. DuckDB-specific optimizations
5. SchemaContext singleton caching (5 min TTL)
"""

import json
import sys
import time
from datetime import date, datetime
from datetime import time as time_type
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, validator


# Add src to Python Path
def _find_project_root():
    p = Path(__file__).resolve().parent
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


sys.path.insert(0, str(_find_project_root() / "src"))

import duckdb as _duckdb

from olav.core.config import MAIN_DB_PATH


def _classify_sql(sql: str) -> str:
    """Classify a SQL statement as SELECT, INSERT, MUTATE, DDL, or OTHER."""
    normalized = sql.strip().upper().lstrip("(")
    if normalized.startswith(("SELECT", "WITH")):
        return "SELECT"
    if normalized.startswith(("INSERT", "UPSERT", "COPY")):
        return "INSERT"
    if normalized.startswith(("UPDATE", "DELETE", "TRUNCATE")):
        return "MUTATE"
    if normalized.startswith(("CREATE", "DROP", "ALTER")):
        return "DDL"
    return "OTHER"


def db_query(sql: str, params: list | None = None) -> list[dict]:
    """Execute a SQL query against the main DuckDB database.

    SELECT/WITH queries run read-only. All mutating SQL (INSERT, UPDATE,
    DELETE, DDL) requires explicit approval before execution.
    """
    sql_type = _classify_sql(sql)

    if sql_type == "SELECT":
        # Safe read-only path
        with _duckdb.connect(str(MAIN_DB_PATH), read_only=True) as conn:
            cur = conn.cursor()
            cur.execute(sql, params or [])
            if cur.description:
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
            return []

    # Mutating SQL — require approval
    return [{"requires_approval": True, "sql_type": sql_type, "sql": sql,
             "reason": f"{sql_type} operation requires explicit approval before execution"}]




# ============================================================================
# Pydantic Models for Type-Safe Parameter Validation
# ============================================================================


class DatabaseQueryInput(BaseModel):
    """Database query input parameters - type-safe validation"""

    query: str = Field(default="", description="Natural language query")
    sql: str = Field(default="", description="Direct SQL query (optional)")
    explain_only: bool = Field(
        default=False, description="Return only schema context without executing query"
    )

    @validator("query", "sql", pre=True)
    def validate_not_none(cls, v):
        """Convert None to empty string"""
        if v is None:
            return ""
        return v


_CONTEXT_ROWS_FALLBACK = 20  # ARCH-16 fallback when tier config unavailable


def _resolve_context_rows() -> int:
    """Return how many result rows to surface to the LLM context (ARCH-16).

    Tier-aware cap via ``TIER_DEFAULTS[<tier>].execute_sql_context_rows``
    (small=10 / medium=20 / large=50). Falls back to
    ``_CONTEXT_ROWS_FALLBACK`` when config is unavailable so smoke tests
    without a configured tier still get a bounded result set.
    """
    try:
        from olav.core.config import get_llm_config, tier_default
        tier = get_llm_config().model_tier
        val = tier_default(tier, "execute_sql_context_rows", _CONTEXT_ROWS_FALLBACK)
        return max(1, int(val))
    except Exception:  # noqa: BLE001
        return _CONTEXT_ROWS_FALLBACK


class DatabaseQueryOutput(BaseModel):
    """Database query output format - unified response"""

    data: list[dict] | None = Field(default=None, description="Query results")
    table: str | None = Field(
        default=None, description="Markdown table representation (for small results)"
    )
    schema_context: str | None = Field(default=None, description="Database schema information")
    sql: str | None = Field(default=None, description="SQL query executed")
    count: int | None = Field(default=None, description="Number of results")
    status: str = Field(
        ...,
        description="success | empty | error | needs_sql_generation | requires_approval",
    )
    error: str | None = Field(default=None, description="Error message if status=error")
    error_type: str | None = Field(default=None, description="Type of error")
    message: str | None = Field(default=None, description="Additional message")
    user_query: str | None = Field(default=None, description="Original user query")
    tables: list[str] | None = Field(default=None, description="Available tables")
    attempted_sql: str | None = Field(default=None, description="SQL that failed")
    suggestions: list[str] | None = Field(default=None, description="Suggestions for retry when count=0")


class SchemaContext:
    """Context manager for auto-schema exploration with singleton caching.

    Uses singleton pattern to cache schema across multiple execute_sql calls.
    Cache TTL is 5 minutes by default.
    """

    _instance: "SchemaContext | None" = None
    _last_refresh: float = 0
    _cache_ttl: float = 300.0  # 5 minutes TTL

    def __new__(cls) -> "SchemaContext":
        """Singleton pattern - reuse instance if cache is valid."""
        now = time.time()
        if cls._instance is None or (now - cls._last_refresh) > cls._cache_ttl:
            cls._instance = super().__new__(cls)
            cls._instance._schema_cache: dict[str, Any] = {}
            cls._instance._refresh_schema()
            cls._last_refresh = now
        return cls._instance

    def __init__(self):
        """Initialize with automatic schema caching."""
        # Schema already initialized in __new__
        pass

    def _refresh_schema(self) -> None:
        """Refresh schema cache by querying INFORMATION_SCHEMA."""
        try:
            # Get all tables
            tables_result = db_query(
                """
                SELECT table_schema || '.' || table_name AS table_name, table_type
                FROM information_schema.tables
                WHERE table_schema IN ('main', 'netops', 'presales')
                ORDER BY table_schema, table_name
                """
            )

            self._schema_cache["tables"] = [row["table_name"] for row in tables_result]
            self._schema_cache["table_details"] = {}

            # Get columns for each table
            for table_name in self._schema_cache["tables"]:
                try:
                    columns_result = db_query(f"DESCRIBE {table_name}")
                    self._schema_cache["table_details"][table_name] = {
                        "columns": [
                            {
                                "name": row.get("column_name", row.get("Field", "")),
                                "type": row.get("column_type", row.get("Type", "")),
                            }
                            for row in columns_result
                        ]
                    }
                except Exception:
                    # Skip tables with errors
                    pass

            # Get sample data — prefer views and devices; skip noisy catalog/JSON tables
            SKIP_SAMPLES = {"main.schema_catalog", "main.yang_leaves",
                            "netops.oc_outputs", "netops.parsed_outputs"}
            PREFER_SAMPLES = [
                "main.v_bgp_neighbors_auto", "main.v_interfaces_auto",
                "main.v_ospf_neighbors_auto", "main.v_l2_links_auto",
                "netops.topology_links", "netops.devices",
            ]
            self._schema_cache["samples"] = {}
            # First try preferred tables, then fill from remaining (skip noisy ones)
            candidates = PREFER_SAMPLES + [
                t for t in self._schema_cache["tables"]
                if t not in PREFER_SAMPLES and t not in SKIP_SAMPLES
            ]
            for table_name in candidates[:6]:
                try:
                    sample = db_query(f"SELECT * FROM {table_name} LIMIT 2")
                    if sample:
                        self._schema_cache["samples"][table_name] = sample
                except Exception:
                    pass

            # Query schema_catalog for parsed_outputs JSON field info
            # Enables LLM to generate: SELECT parsed_data->>'field' FROM parsed_outputs
            try:
                catalog_rows = db_query(
                    "SELECT source_name, platform, fields FROM schema_catalog "
                    "WHERE source_type = 'textfsm' ORDER BY source_name, platform"
                )
                if catalog_rows:
                    schema_catalog_info: list[str] = []
                    for row in catalog_rows:
                        fields = row.get("fields", [])
                        if isinstance(fields, str):
                            try:
                                fields = json.loads(fields)
                            except Exception:
                                fields = []
                        field_names = [f["name"] for f in (fields or []) if isinstance(f, dict)]
                        if field_names:
                            schema_catalog_info.append(
                                f"  {row['source_name']} ({row['platform']}): "
                                + ", ".join(field_names)
                            )
                    self._schema_cache["schema_catalog"] = schema_catalog_info
            except Exception:
                pass  # schema_catalog not yet populated

            # Query mapping_table for unified field mapping (v0.11.0)
            try:
                mapping_rows = db_query(
                    "SELECT platform, platform_key, unified_key FROM mapping_table "
                    "ORDER BY platform, unified_key"
                )
                if mapping_rows:
                    mapping_info: list[str] = []
                    for row in mapping_rows:
                        mapping_info.append(
                            f"  {row['platform']}: {row['platform_key']} -> {row['unified_key']}"
                        )
                    self._schema_cache["mapping_table"] = mapping_info
            except Exception:
                pass  # mapping_table not yet populated

        except Exception as e:
            self._schema_cache = {"error": str(e)}

    def get_schema_context(self) -> str:
        """Get formatted schema context for LLM."""
        if "error" in self._schema_cache:
            return f"Schema error: {self._schema_cache['error']}"

        context_parts = ["**Available Database Schema:**\n"]

        # CRITICAL: schema-qualified names required for netops tables
        context_parts.append(
            "⚠️  ALWAYS use schema-qualified names: `netops.devices`, `netops.parsed_outputs`, etc.\n"
            "   Views (v_*) are in main schema and can be queried unqualified.\n"
        )

        # List tables
        context_parts.append(f"Tables: {', '.join(self._schema_cache['tables'])}\n")

        # View semantic hints — help agent pick the right view immediately
        context_parts.append("**View Quick Reference (USE THESE FIRST for network queries):**")
        context_parts.append(
            "  v_interfaces_auto         → interface admin/oper status + IP address per device\n"
            "  v_bgp_neighbors_auto      → BGP neighbor state, remote-AS, prefixes received\n"
            "  v_ospf_neighbors_auto     → OSPF neighbor state + cost (cross-vendor)\n"
            "  v_l2_links_auto           → L2 neighbors (CDP/LLDP): cleaned topology view\n"
            "  netops.topology_links     → CDP/LLDP base table: src/dst device + interface pairs\n"
            "  v_arp_auto                → ARP table: ip_address, mac_address, interface per device\n"
            "  netops.devices            → device inventory: hostname, platform, ip_address, role\n"
            "  netops.parsed_outputs     → raw TextFSM rows: parsed_data (JSON), command, snapshot_id\n"
            "  netops.topology_links     → computed topology links (src/dst device+interface+protocol)\n"
            "  netops.oc_outputs         → OC JSON per module: oc_module, oc_data, device_name\n"
            "  netops.raw_output_store   → latest raw CLI text per (device, command)"
        )

        # Detail each table
        for table_name, details in self._schema_cache["table_details"].items():
            context_parts.append(f"\n**{table_name}:**")
            columns = [f"  - {col['name']}: {col['type']}" for col in details["columns"]]
            context_parts.append("\n".join(columns))

            # Add sample if available
            if table_name in self._schema_cache.get("samples", {}):
                sample = self._schema_cache["samples"][table_name]
                if sample:
                    context_parts.append(f"  Sample: {sample[0]}")

        # Add mapping_table block (Unified Schema Mapping v0.11.0)
        mapping_info = self._schema_cache.get("mapping_table", [])
        if mapping_info:
            context_parts.append("\n**Unified Schema Mappings (platform -> unified):**")
            context_parts.append(
                "  Use these to map platform-specific keys to unified keys in your queries."
            )
            # Cap at 50 most common mappings to avoid context bloat
            context_parts.extend(mapping_info[:50])

        # Add schema_catalog block (JSON fields for parsed_outputs)
        schema_catalog = self._schema_cache.get("schema_catalog", [])
        if schema_catalog:
            context_parts.append("\n**parsed_outputs JSON fields (via schema_catalog):**")
            context_parts.append(
                "  Query pattern: SELECT parsed_data->>'field_name' "
                "FROM parsed_outputs WHERE command='...' AND snapshot_date=CURRENT_DATE"
            )
            context_parts.extend(schema_catalog[:50])  # cap at 50 entries

        return "\n".join(context_parts)

    def query(self, sql: str) -> list[dict]:
        """Execute SQL query with error details."""
        return db_query(sql)


_MAX_FIELD_CHARS = 800  # prevent single large JSON fields from flooding LLM context


def _sanitize_value(val: Any) -> Any:
    """Convert non-JSON-serializable types to strings, truncating large values."""
    if isinstance(val, (datetime, date, time_type)):
        return val.isoformat()
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, bytes):
        val = val.decode("utf-8", errors="replace")
    if isinstance(val, dict):
        sanitized = {k: _sanitize_value(v) for k, v in val.items()}
        as_str = str(sanitized)
        if len(as_str) > _MAX_FIELD_CHARS:
            return as_str[:_MAX_FIELD_CHARS] + "…[truncated]"
        return sanitized
    if isinstance(val, (list, tuple)):
        sanitized = [_sanitize_value(v) for v in val]
        as_str = str(sanitized)
        if len(as_str) > _MAX_FIELD_CHARS:
            return as_str[:_MAX_FIELD_CHARS] + "…[truncated]"
        return sanitized
    if isinstance(val, str) and len(val) > _MAX_FIELD_CHARS:
        return val[:_MAX_FIELD_CHARS] + "…[truncated]"
    return val


def _sanitize_rows(rows: list[dict]) -> list[dict]:
    """Ensure all values in query results are JSON-serializable.

    Sanitizes each FIELD, never the row as a whole. ``_sanitize_value``'s dict
    branch returns a *string* once the rendered form passes
    ``_MAX_FIELD_CHARS``, so a row wide enough to trip that (any query
    selecting stored config text) came back as a str and every consumer
    expecting a mapping died on `'str' object has no attribute 'items'`.
    The budget is per-field; a large cell must not turn a record into text.
    """
    return [
        {k: _sanitize_value(v) for k, v in row.items()}
        if isinstance(row, dict) else row
        for row in rows
    ]


def _schema_hint() -> str:
    """A follow-up hint listing views that are actually in this database.

    This was a hardcoded string naming ``v_interfaces_auto``,
    ``v_bgp_neighbors_auto``, ``v_ospf_neighbors_auto`` and ``v_arp_auto``. R83.2
    deleted all four with the recipe-driven view layer; only ``v_l2_links_auto``
    survived. So every successful query answered with an invitation to query four
    views that do not exist, and a model that took it got a CatalogException —
    the same wrong view names that `query_topology.py` carries two fix notes
    about. A hint about the schema has to be read off the schema.

    Failure is silent by design: this is an aid attached to an answer that
    already succeeded, so it must never turn that answer into an error.
    """
    try:
        with _duckdb.connect(str(MAIN_DB_PATH), read_only=True) as conn:
            views = [
                r[0] for r in conn.execute(
                    "SELECT table_name FROM information_schema.views "
                    "WHERE table_schema = 'netops' ORDER BY table_name"
                ).fetchall()
            ]
            tables = [
                r[0] for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'netops' AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                ).fetchall()
            ]
    except Exception:  # noqa: BLE001
        return ""

    parts = []
    if tables:
        parts.append("tables: " + ", ".join(f"netops.{t}" for t in tables[:8]))
    if views:
        shown = ", ".join(f"netops.{v}" for v in views[:8])
        more = f" (+{len(views) - 8} more)" if len(views) > 8 else ""
        parts.append(f"views: {shown}{more}")
    if not parts:
        return ""
    return "Schema reminder — " + " | ".join(parts)


def main(params: dict) -> dict:
    """Execute database query with auto schema exploration.

    Use tool_help("execute_sql") for full usage, examples, and tier limits.

    Args:
        params: {
            "query": "Natural language query",
            "sql": "Optional direct SQL",
            "explain_only": Optional bool to only return schema context
        }

    Returns:
        {
            "data": [...],
            "sql": "Executed SQL",
            "schema_context": "Auto-discovered schema",
            "status": "success"|"error"
        }
    """
    # Validate parameters with Pydantic
    try:
        args = DatabaseQueryInput(**params)
    except Exception as e:
        output = DatabaseQueryOutput(
            status="error", error=f"Invalid parameters: {str(e)}", error_type="validation_error"
        )
        return output.model_dump(exclude_none=True)

    user_query = args.query
    direct_sql = args.sql
    explain_only = args.explain_only

    # Get schema context (singleton, cached for 5 min)
    context = SchemaContext()
    schema_context = context.get_schema_context()

    # If explain_only, return just schema
    if explain_only:
        output = DatabaseQueryOutput(
            schema_context=schema_context,
            tables=context._schema_cache.get("tables", []),
            status="success",
        )
        return output.model_dump(exclude_none=True)

    # If direct SQL provided (agent already generated it), execute it
    if direct_sql:
        try:
            # Execute query (LangGraph cache handles caching at framework level)
            results = context.query(direct_sql)
            results = _sanitize_rows(results)

            # Limit data returned to the LLM context to prevent bloat/hang.
            # Tier-aware (ARCH-16, Round 41): small=10 / medium=20 / large=50.
            # Full data is still exported to CSV if count > 50, regardless of tier.
            MAX_ROWS_TO_CONTEXT = _resolve_context_rows()

            # Auto-export logic
            csv_path = None
            if len(results) > 50:
                import csv
                import hashlib
                import io
                from pathlib import Path

                export_dir = Path("exports") / "queries"
                export_dir.mkdir(parents=True, exist_ok=True)

                # Content-addressed: the filename carries a digest of the rows,
                # so an identical result set reuses the file it already wrote
                # instead of leaving a duplicate behind. A model that re-issues
                # the same query does happen — observed 1 run in 3 on core,
                # producing two byte-identical CSVs 5s apart, of which only the
                # second was ever cited. Prose cannot fix that (it changes
                # WHETHER a small model calls a tool, not how many times), so
                # the tool makes the duplicate impossible by construction.
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=results[0].keys())
                writer.writeheader()
                writer.writerows(results)
                payload = buf.getvalue()
                digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]

                existing = sorted(export_dir.glob(f"query_*_{digest}.csv"))
                if existing:
                    csv_path = existing[0]
                else:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    csv_path = export_dir / f"query_{timestamp}_{digest}.csv"
                    with open(csv_path, "w", newline="") as f:
                        f.write(payload)

            # Prepare data for LLM context
            truncated = len(results) > MAX_ROWS_TO_CONTEXT
            display_data = results[:MAX_ROWS_TO_CONTEXT] if truncated else results

            message = None
            if csv_path:
                message = f"FULL results ({len(results)} rows) exported to {csv_path}."
            if truncated:
                msg = f"Only first {MAX_ROWS_TO_CONTEXT} rows returned to context to prevent bloat."
                message = f"{message} {msg}" if message else msg

            # Check if results are empty - provide schema hints for agentic retry
            if len(results) == 0:
                # ... same as before ...
                output = DatabaseQueryOutput(
                    data=results,
                    sql=direct_sql,
                    count=0,
                    status="empty",
                    message="Query returned 0 results. Schema context provided for retry.",
                    schema_context=schema_context,
                )
                return output.model_dump(exclude_none=True)

            # Mutating SQL never runs: `db_query` refuses it and hands back an
            # approval marker instead of rows. Saying "success" over that marker
            # tells the caller its DDL went through — one value meaning both "I
            # ran your query" and "I declined to". A model that reads only
            # `status` then reports a change it never made.
            if len(results) == 1 and results[0].get("requires_approval") is True:
                refusal = results[0]
                output = DatabaseQueryOutput(
                    data=display_data,
                    sql=direct_sql,
                    count=0,
                    status="requires_approval",
                    message=(
                        f"{refusal.get('sql_type', 'mutating')} statement was NOT "
                        "executed — it needs explicit approval. The database is "
                        "read-only on this path."
                    ),
                )
                return output.model_dump(exclude_none=True)

            # Include compact schema hint in success response for follow-up query accuracy
            SCHEMA_HINT = _schema_hint()
            output = DatabaseQueryOutput(
                data=display_data,
                sql=direct_sql,
                count=len(results),
                status="success",
                message=f"{message} | {SCHEMA_HINT}" if message else SCHEMA_HINT,
            )
            return output.model_dump(exclude_none=True)
        except Exception as e:
            # Return error with schema context for agent to retry
            output = DatabaseQueryOutput(
                error=str(e),
                schema_context=schema_context,
                attempted_sql=direct_sql,
                status="error",
                error_type="execution_error",
            )
            return output.model_dump(exclude_none=True)

    # If natural language query, return schema context for agent to generate SQL
    output = DatabaseQueryOutput(
        message="Schema context provided for SQL generation",
        user_query=user_query,
        schema_context=schema_context,
        status="needs_sql_generation",
    )
    return output.model_dump(exclude_none=True)


class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return super().default(obj)


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = main(_args)
    print(_json.dumps(result, ensure_ascii=False, indent=2, cls=DateTimeEncoder))
