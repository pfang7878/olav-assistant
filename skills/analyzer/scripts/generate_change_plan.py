#!/usr/bin/env python3
"""generate_change_plan — Fat Tool for multi-device BFS upgrade change plans.

Replaces the per-device SQL loop pattern where gemma4 queries each device
individually (60-80 calls → context overflow). This script fetches ALL devices
in ONE query, generates CLI/rollback/post-checks for each, and writes the
complete markdown in a single call. LLM sees only the final result.

Usage (execute_skill_script):
    {
      "skill_name": "analyzer",
      "script_name": "generate_change_plan",
      "arguments": {
        "model_pattern": "WS-C4500X-32",
        "output_filename": "WS-C4500X_BFS_staged_upgrade_plan",
        "upgrade_description": "BFS firmware upgrade, leaf-first order",
        "bfs_order": true
      }
    }

Returns: {"status": "success", "file": "<path>", "device_count": N}
"""
from __future__ import annotations

import json
import sys
import re
from datetime import datetime
from pathlib import Path


def _find_root() -> Path:
    p = Path(__file__).resolve().parent
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


sys.path.insert(0, str(_find_root() / "src"))


_DATE_TAIL_RE = re.compile(
    r"[_-]?(?:\d{8}|\d{4}[_-]\d{2}[_-]\d{2}|\d{6})$"
)


def _dated_stem(stem: str) -> str:
    """Stamp the filename with today's date, from Python rather than the model.

    The model used to supply the whole basename, and it invented the date: a
    plan drafted on 2026-08-06 was written as
    ``redundant_bgp_alpha_20231027.md``. The content was correct — only the
    name lied, which is the worst place for it, because the name is what an
    operator sorts and cites by.

    A date is a fact the process knows and the model does not, so the process
    supplies it. Any date-looking suffix the model added is stripped first, so
    a well-behaved model does not end up with two.
    """
    base = _DATE_TAIL_RE.sub("", stem.strip().rstrip("_-")) or "change_plan"
    return f"{base}_{datetime.now().strftime('%Y%m%d')}"


def generate_change_plan(
    model_pattern: str,
    output_filename: str,
    upgrade_description: str = "BFS firmware/software upgrade",
    bfs_order: bool = True,
    db_path: str | None = None,
) -> dict:
    """Generate a multi-device change plan in ONE shot without per-device SQL loops.

    Args:
        model_pattern:       SQL LIKE pattern for device model, e.g. '%C4500X%'.
        output_filename:     Output file base name (no extension), saved under
                             exports/change_plans/.
        upgrade_description: Human-readable description of the change.
        bfs_order:           If True, sort devices by topology depth (leaf-first).
        db_path:             Override DuckDB path.

    Returns:
        {"status": "success", "file": "<abs_path>", "device_count": N}
    """
    import duckdb

    from olav.core.config import MAIN_DB_PATH

    db = Path(db_path or MAIN_DB_PATH)

    # ── Step 1: Fetch ALL devices in ONE query ────────────────────────────
    with duckdb.connect(str(db), read_only=True) as conn:
        devices = conn.execute(
            """
            SELECT hostname, platform, model, ip_address, role,
                   vendor, os_version
            FROM netops.devices
            WHERE model LIKE ?
            ORDER BY role, hostname
            """,
            [model_pattern],
        ).fetchall()
        cols = [d[0] for d in conn.description]

    if not devices:
        return {
            "status": "error",
            "message": f"No devices found matching model LIKE '{model_pattern}'",
        }

    device_dicts = [dict(zip(cols, row)) for row in devices]

    # ── Step 2: BFS leaf-first ordering (by role heuristic) ───────────────
    if bfs_order:
        role_order = {"leaf": 0, "access": 0, "server": 0, "wifi": 0,
                      "distribution": 1, "dist": 1, "wan": 2, "core": 3}
        def _role_key(d: dict) -> int:
            r = (d.get("role") or "").lower()
            for k, v in role_order.items():
                if k in r:
                    return v
            return 1

        device_dicts.sort(key=_role_key)

    # ── Step 3: Generate markdown ──────────────────────────────────────────
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# Change Plan: {model_pattern.strip('%')} — {upgrade_description}",
        f"_Generated {today}; scope: {len(device_dicts)}x devices; "
        f"layers touched: L1, L3, L4_",
        "",
        "## Summary",
        f"Perform staged {upgrade_description} across all matching devices. "
        "Sequence: leaf/access nodes first, then distribution, then WAN/core.",
        "",
        "## Scope",
        f"- **Devices**: {len(device_dicts)}x {model_pattern.strip('%')} (Cisco IOS)",
        "- **Layered impact**:",
        "  - L1: Device reload (temporary downtime ~5 min per device)",
        "  - L3: Routing adjacency flaps during reload",
        "  - L4: BGP/OSPF session resets",
        "",
        "## Upgrade Sequence",
        "",
    ]

    # Group by stage
    stage_map: dict[int, list[dict]] = {}
    for d in device_dicts:
        stage = _role_key(d) if bfs_order else 0
        stage_map.setdefault(stage, []).append(d)

    stage_labels = {0: "Stage 1 — Leaf / Access / Server", 1: "Stage 2 — Distribution",
                    2: "Stage 3 — WAN Edge", 3: "Stage 4 — Core"}

    for stage_num in sorted(stage_map):
        stage_devices = stage_map[stage_num]
        lines.append(f"### {stage_labels.get(stage_num, f'Stage {stage_num+1}')}")
        lines.append("")
        for d in stage_devices:
            host = d["hostname"]
            ip = d.get("ip_address") or "N/A"
            lines += [
                f"#### `{host}` ({ip})",
                "",
                "**Pre-checks:**",
                "```",
                f"show version | include Software",
                f"show redundancy | include active",
                "```",
                "",
                "**Upgrade CLI (Cisco IOS):**",
                "```",
                f"! Connect to {host} ({ip})",
                "copy tftp flash:",
                "  ! Address: <TFTP_SERVER>",
                "  ! Source: <NEW_IOS_IMAGE>",
                "boot system flash:<NEW_IOS_IMAGE>",
                "write memory",
                "reload",
                "```",
                "",
                "**Rollback:**",
                "```",
                "no boot system flash:<NEW_IOS_IMAGE>",
                "boot system flash:<OLD_IOS_IMAGE>",
                "write memory",
                "reload",
                "```",
                "",
                "**Post-checks:**",
                "```",
                "show version | include Software",
                "show ip ospf neighbor",
                "show ip bgp summary",
                "```",
                "",
            ]

    lines += [
        "## Risks",
        "- Simultaneous reload of distribution nodes may cause temporary L3 outage",
        "- Verify image MD5 before loading",
        "- Schedule during maintenance window",
        "",
        "## Pre-conditions",
        "- TFTP server reachable from all devices",
        "- Backup configs stored",
        "- Change window approved",
        "",
        "## Pre-change verification",
        "This plan is drafted from captured state, not proven against the "
        "network. Validate it with Batfish (via the `sim` sub-agent) — a "
        "separate step — before the maintenance window:",
        "",
        "```",
        f'olav --agent netops "On the latest snapshot, Batfish-validate '
        f'exports/change_plans/{output_filename}.md — check subnet/overlap '
        f'conflicts, BGP/OSPF compatibility, and reachability. Return a verdict."',
        "```",
        "",
        "_Drafted from the last snapshot — validate with the command above and "
        "double-check against the live network before you apply._",
    ]

    markdown = "\n".join(lines)

    # ── Step 4: Write to disk ──────────────────────────────────────────────
    out_dir = Path("exports") / "change_plans"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_dated_stem(output_filename)}.md"
    out_path.write_text(markdown, encoding="utf-8")

    return {
        "status": "success",
        "file": str(out_path.resolve()),
        "device_count": len(device_dicts),
        "stages": len(stage_map),
        "message": (
            f"✓ Change plan written: {out_path} "
            f"({len(device_dicts)} devices, {len(stage_map)} stages)"
        ),
    }


if __name__ == "__main__":
    _args = json.loads(sys.stdin.read() or "{}")
    result = generate_change_plan(**_args)
    print(json.dumps(result, ensure_ascii=False, default=str))
