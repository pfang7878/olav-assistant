#!/usr/bin/env python3
"""
search_commands — Query available commands by device platform and keyword.

Intended LLM workflow for CLI execution:
  1. search_commands(device="R1", keyword="ospf") → see what commands exist for this platform
  2. Pick the desired command from results (note pipe_allowed flag)
  3. execute_cli_parallel(devices=["R1"], command="show ip ospf neighbor")

This tool replaces "bulk load entire command whitelist" with targeted on-demand search.
The commands table is populated by olav-config sync_commands().
"""

import json
import sys
from pathlib import Path

import duckdb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_project_root() -> Path:
    p = Path(__file__).resolve().parent
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


sys.path.insert(0, str(_find_project_root() / "src"))


from olav.core.config import MAIN_DB_PATH


def _get_device_platform(device_name: str) -> str | None:
    """Look up device platform from DuckDB devices table."""
    try:
        with duckdb.connect(str(MAIN_DB_PATH), read_only=True) as conn:
            rows = conn.execute(
                "SELECT platform FROM devices WHERE LOWER(name) = LOWER(?) LIMIT 1",
                [device_name],
            ).fetchall()
            return rows[0][0] if rows else None
    except Exception:
        return None


def _search_commands_in_db(
    platform: str,
    keyword: str,
    include_blacklisted: bool = False,
    limit: int = 20,
) -> list[dict]:
    """Query commands table for a platform, filtered by keyword substring."""
    try:
        with duckdb.connect(str(MAIN_DB_PATH), read_only=True) as conn:
            # Check table exists
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_name = 'commands'"
                ).fetchall()
            ]
            if not tables:
                return []

            blacklist_clause = "" if include_blacklisted else "AND blacklisted = false"
            keyword_clause = ""
            params: list = [platform, "*"]

            if keyword.strip():
                keyword_clause = "AND LOWER(command_name) LIKE ?"
                params.append(f"%{keyword.lower().strip()}%")

            params.append(limit)

            sql = f"""
                SELECT
                    command_name,
                    platform,
                    category,
                    has_template,
                    allowed,
                    pipe_allowed,
                    blacklisted
                FROM commands
                WHERE platform IN (?, ?)
                  AND allowed = true
                  {blacklist_clause}
                  {keyword_clause}
                ORDER BY
                    CASE WHEN platform = ? THEN 0 ELSE 1 END,
                    category NULLS LAST,
                    command_name
                LIMIT ?
            """
            # add platform for ordering sort
            params.insert(-1, platform)

            rows = conn.execute(sql, params).fetchall()
            cols = ["command_name", "platform", "category", "has_template",
                    "allowed", "pipe_allowed", "blacklisted"]
            return [dict(zip(cols, r, strict=False)) for r in rows]

    except Exception as exc:
        return [{"error": str(exc)}]


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def search_commands(
    device: str = "",
    platform: str = "",
    keyword: str = "",
    limit: int = 20,
) -> dict:
    """Search available CLI commands for a device or platform.

    Use this BEFORE execute_cli to discover which commands are available,
    check if pipe filtering is allowed, and pick the right command name.

    Workflow:
        1. search_commands(device="R1", keyword="ospf") → returns command list
        2. Pick the command from results (check pipe_allowed if you want | filter)
        3. execute_cli_parallel(devices=["R1"], command="show ip ospf neighbor")

    Args:
        device:   Device name (e.g. "R1"). If given, platform is auto-resolved
                  from the devices table. Preferred over specifying platform directly.
        platform: Platform string (e.g. "cisco_ios"). Used if device is not given.
        keyword:  Substring to filter command names (e.g. "ospf", "bgp", "interface").
                  Leave empty to list all commands for the platform.
        limit:    Max results to return (default 20).

    Returns:
        {
          "platform": "cisco_ios",
          "keyword": "ospf",
          "count": 3,
          "commands": [
            {"command_name": "show ip ospf neighbor", "category": "routing",
             "pipe_allowed": false, "has_template": true},
            ...
          ],
          "note": "pipe_allowed=true means | filters are permitted in execute_cli_parallel"
        }
    """
    resolved_platform = platform.strip()

    # Auto-resolve platform from device name
    if device.strip() and not resolved_platform:
        resolved_platform = _get_device_platform(device.strip()) or ""

    if not resolved_platform:
        return {
            "status": "error",
            "message": (
                "Could not determine platform. Provide 'device' (looked up from devices table) "
                "or 'platform' (e.g. 'cisco_ios') explicitly."
            ),
        }

    results = _search_commands_in_db(
        platform=resolved_platform,
        keyword=keyword,
        limit=limit,
    )

    if results and "error" in results[0]:
        return {
            "status": "error",
            "message": results[0]["error"],
            "hint": "Run sync_commands() via olav-config to populate the commands table.",
        }

    if not results:
        return {
            "status": "no_results",
            "platform": resolved_platform,
            "keyword": keyword,
            "message": (
                f"No commands found for platform='{resolved_platform}'"
                + (f" matching keyword='{keyword}'" if keyword else "")
                + ". Run sync_commands() via olav-config to populate the commands table."
            ),
        }

    # Strip internal columns from response
    clean = [
        {
            "command_name": r["command_name"],
            "category": r.get("category"),
            "has_template": r.get("has_template", False),
            "pipe_allowed": r.get("pipe_allowed", False),
        }
        for r in results
    ]

    return {
        "status": "success",
        "platform": resolved_platform,
        "keyword": keyword or "(all)",
        "count": len(clean),
        "commands": clean,
        "note": "pipe_allowed=true means you may append '| include ...' etc. in execute_cli",
    }


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = search_commands(**_args)
    print(_json.dumps(result, default=str))
