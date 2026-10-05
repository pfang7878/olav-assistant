#!/usr/bin/env python3
"""Search Logs — Query syslog Parquet data for agents.

Agents use this to answer questions like:
  - "What errors occurred in the last hour?"
  - "Which devices sent the most syslog messages today?"
  - "Were there any critical events on R1 yesterday?"

Data source: .olav/databases/syslogs/**/*.parquet
Written by: src/olav/services/syslog_receiver.py
"""

import logging
import sys
from pathlib import Path


def _find_project_root():
    p = Path(__file__).resolve().parent
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


sys.path.insert(0, str(_find_project_root() / "src"))

_logger = logging.getLogger(__name__)

_SEVERITY_LEVELS = {
    "emergency", "alert", "critical", "error",
    "warning", "notice", "info", "debug",
}

# ARCH-16 (Round 41) — hard ceiling that even the large tier respects;
# prevents a buggy ``limit=10000`` caller from filling the model context.
_SEARCH_LOGS_HARD_MAX = 500
_SEARCH_LOGS_FALLBACK = 50  # when tier config unavailable


def _resolve_search_limit(explicit: int | None) -> int:
    """Resolve ``limit`` for search_logs respecting tier defaults.

    Explicit value wins (clamped to 1..``_SEARCH_LOGS_HARD_MAX``).
    ``None`` → ``tier_default(tier, "search_logs_default_limit")`` —
    small=20 / medium=50 / large=100. Fallback when config unavailable
    keeps the pre-Round-41 default of 50.
    """
    if explicit is not None:
        return max(1, min(int(explicit), _SEARCH_LOGS_HARD_MAX))
    try:
        from olav.core.config import get_llm_config, tier_default
        tier = get_llm_config().model_tier
        val = tier_default(tier, "search_logs_default_limit", _SEARCH_LOGS_FALLBACK)
        return max(1, min(int(val), _SEARCH_LOGS_HARD_MAX))
    except Exception:  # noqa: BLE001
        return _SEARCH_LOGS_FALLBACK


def _get_log_dir() -> Path:
    """Resolve the syslog Parquet log directory."""
    try:
        from olav.core.config import DATABASES_DIR
        return Path(DATABASES_DIR) / "syslogs"
    except Exception:
        return Path(".olav/databases/syslogs")


def search_logs(
    query: str,
    hours: int = 24,
    severity: str | None = None,
    host: str | None = None,
    limit: int | None = None,
) -> str:
    """Search syslog records stored as Parquet files.

    Args:
        query: Keyword to search in the message field (case-insensitive). Use "" for no filter.
        hours: How many hours back to look (default 24).
        severity: Filter by syslog severity level: emergency/alert/critical/error/warning/notice/info/debug.
        host: Filter by device hostname (partial match).
        limit: Maximum number of results to return. Omit for the model-tier
               default (small=20 / medium=50 / large=100 via
               ``TIER_DEFAULTS.search_logs_default_limit``, ARCH-16). Hard
               ceiling is 500 regardless of tier.

    Returns:
        Formatted table of matching syslog entries, newest first.
        Returns "No log records found." when the result set is empty.
    """
    limit = _resolve_search_limit(limit)
    import duckdb

    log_dir = _get_log_dir()
    if not log_dir.exists():
        return "No log records found. (log directory does not exist)"

    # R100/S5 (2026-04-29 demo7 Ch10 v3): strip leading/trailing literal
    # quotes that small models bake into short string-typed args via
    # their JSON-construction heuristic.  qwen3.6:27b empirically passes
    # severity='"warning"' instead of severity='warning' for OpenAI tool
    # calls, breaking the _SEVERITY_LEVELS enum membership check.  Same
    # defensive coercion as format_and_export (see R100/S2 commit
    # c2ce4f2 _strip_quote_leak).
    def _strip_quotes(s):
        if not isinstance(s, str):
            return s
        s = s.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
            s = s[1:-1].strip()
        return s

    severity = _strip_quotes(severity)
    host = _strip_quotes(host)
    query = _strip_quotes(query)

    # Glob all parquet files recursively
    parquet_files = list(log_dir.rglob("*.parquet"))
    if not parquet_files:
        return "No log records found. (no Parquet files in log directory)"

    parquet_glob = str(log_dir / "**/*.parquet")

    where_parts = [f"TRY_CAST(timestamp AS TIMESTAMP) >= CAST(now() AS TIMESTAMP) - INTERVAL '{hours} hours'"]

    if severity:
        sev = severity.lower()
        if sev in _SEVERITY_LEVELS:
            # R100/S5 (2026-04-29): case-insensitive match — Parquet
            # stores "CRITICAL" (uppercase from syslog_receiver), but
            # _SEVERITY_LEVELS is lowercased.  Compare in lowercase so
            # both forms match.
            where_parts.append(f"LOWER(severity) = '{sev}'")
        else:
            return (
                f"Invalid severity '{severity}'. "
                f"Valid values: {', '.join(sorted(_SEVERITY_LEVELS))}"
            )

    if host:
        # Escape single quotes in host filter
        safe_host = host.replace("'", "''")
        where_parts.append(f"host ILIKE '%{safe_host}%'")

    if query:
        safe_query = query.replace("'", "''")
        where_parts.append(f"message ILIKE '%{safe_query}%'")

    where_clause = " AND ".join(where_parts)

    sql = f"""
        SELECT timestamp, host, severity, facility, message
        FROM read_parquet('{parquet_glob}', union_by_name=true)
        WHERE {where_clause}
        ORDER BY timestamp DESC
        LIMIT {limit}
    """

    clock_skew_hint = ""
    try:
        con = duckdb.connect(":memory:")
        rows = con.execute(sql).fetchall()
        # ISSUE-CH10-SYSLOG-CLOCK-SKEW-1H-WINDOW (P3, 2026-05-12):
        # When the result set is empty but Parquet rows exist within
        # the file footprint, the user's host clock is probably skewed
        # vs the syslog receiver's clock. Auto-fall-back to a wider
        # 24h window and surface a hint so the operator can fix NTP
        # without re-running the query.
        if not rows and hours < 24:
            try:
                fallback_sql = f"""
                    SELECT timestamp, host, severity, facility, message
                    FROM read_parquet('{parquet_glob}', union_by_name=true)
                    WHERE TRY_CAST(timestamp AS TIMESTAMP) >=
                          CAST(now() AS TIMESTAMP) - INTERVAL '24 hours'
                    {(' AND LOWER(severity) = ' + repr(severity.lower())) if severity else ''}
                    {(" AND host ILIKE '%" + host.replace(chr(39), chr(39)*2) + "%'") if host else ''}
                    {(" AND message ILIKE '%" + query.replace(chr(39), chr(39)*2) + "%'") if query else ''}
                    ORDER BY timestamp DESC
                    LIMIT {limit}
                """
                fallback_rows = con.execute(fallback_sql).fetchall()
                if fallback_rows:
                    rows = fallback_rows
                    clock_skew_hint = (
                        f"\n\n⚠️ Clock-skew fallback: original {hours}h "
                        f"window returned 0 rows but the 24h window has "
                        f"{len(fallback_rows)}. Host clock may be off "
                        f"vs ingestion clock — check NTP."
                    )
                    hours = 24  # report the widened window in the header
            except Exception as exc:
                _logger.debug("search_logs clock-skew fallback failed: %s", exc)
        con.close()
    except Exception as exc:
        _logger.warning("search_logs query error: %s", exc)
        return f"Error querying logs: {exc}"

    if not rows:
        return "No log records found matching the criteria."

    lines = [f"{'TIMESTAMP':<24} {'HOST':<16} {'SEV':<10} {'FACILITY':<10} MESSAGE"]
    lines.append("-" * 100)
    for ts, h, sev, fac, msg in rows:
        ts_str = str(ts)[:23] if ts else "-"
        h_str = (str(h) or "-")[:14]
        sev_str = (str(sev) or "-")[:8]
        fac_str = (str(fac) or "-")[:8]
        msg_str = (str(msg) or "")[:80]
        lines.append(f"{ts_str:<24} {h_str:<16} {sev_str:<10} {fac_str:<10} {msg_str}")

    header = f"Found {len(rows)} syslog records (last {hours}h"
    if severity:
        header += f", severity={severity}"
    if host:
        header += f", host={host}"
    if query:
        header += f", query='{query}'"
    header += "):\n"

    return header + "\n".join(lines) + clock_skew_hint


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = search_logs(**_args)
    print(_json.dumps(result, default=str))
