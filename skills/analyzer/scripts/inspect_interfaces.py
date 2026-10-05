#!/usr/bin/env python3
"""inspect_interfaces — per-interface IP / status lookup for the analyzer.

Migrated from @tool (netops/tools/inspect_interfaces.py) to script (ADR-0007
rev ~301): stateless DuckDB read — no persistent state, no audit row.

Reads ``netops.v_show_ip_interface_brief_auto`` (latest snapshot per device)
with Junos terse fallback via ``netops.v_show_interfaces_terse_auto``.

**Either source may be absent.** A per-command view exists only if the
snapshot carried that command, so a Cisco-only collection has no
``v_show_interfaces_terse_auto`` and a Junos-only one has no
``v_show_ip_interface_brief_auto``. Until 2026-08-17 both were queried
unconditionally and DuckDB raised ``CatalogException`` — a crash on ordinary
single-vendor data, in both runtimes. Absent sources are now named in
``absent_sources`` and the remaining one is still read.

Accepts JSON on stdin:
  {"devices": ["R1", "R3"], "include_unassigned": false}
Pass devices=[] for discovery mode (all devices with interface data).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import duckdb


def _db_path() -> Path:
    cwd_db = Path.cwd() / ".olav" / "databases" / "main.duckdb"
    if cwd_db.exists():
        return cwd_db
    try:  # the platform's own resolution (honours OLAV_HOME), when installed
        from olav.core.config import MAIN_DB_PATH
        if Path(MAIN_DB_PATH).exists():
            return Path(MAIN_DB_PATH)
    except Exception:  # noqa: BLE001
        pass
    base = os.environ.get("OLAV_BASE_PATH")
    if base:
        p = Path(base) / "databases" / "main.duckdb"
        if p.exists():
            return p
    return cwd_db


def _view_exists(con: Any, view: str) -> bool:
    """True when ``netops.<view>`` is in the catalogue.

    Cheaper than try/except around every query and, more importantly, it lets
    the caller be *told* which command is missing instead of reading a
    traceback.
    """
    return bool(con.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'netops' AND table_name = ? LIMIT 1",
        [view],
    ).fetchone())


def _command_for(view: str) -> str:
    """``v_show_interfaces_terse_auto`` → ``show interfaces terse``."""
    return view.removeprefix("v_").removesuffix("_auto").replace("_", " ")


def inspect_interfaces(
    devices: list[str],
    include_unassigned: bool = False,
) -> dict[str, Any]:
    """
    Get per-interface IP / status / proto for the named devices.

    Use this when planning changes that need a specific interface IP
    (e.g. static route next-hop, ACL apply target, OSPF area
    interface).  Returns the most recent snapshot's data per device.

    Args:
        devices: List of hostnames.  E.g. ``["R1", "R3"]``.  An
            empty list returns interfaces for every device with
            data (discovery mode).
        include_unassigned: If False (default), skip interfaces
            whose ``ip_address`` is ``"unassigned"`` or empty —
            keeps the result focused on routable interfaces.  Set
            True if you need full inventory including unconfigured
            ports.

    Returns:
        ``{
            "found": {
                hostname: [
                    {"interface": "Ethernet0/0",
                     "ip_address": "10.1.13.3",
                     "status": "up", "proto": "up"},
                    ...
                ],
                ...
            },
            "snapshot_ids": {hostname: snapshot_id},
            "unknown_devices": [hostname, ...],
            "absent_sources": [{"view", "command"}, ...],  # not collected
        }``.
    """
    db = _db_path()
    if not db.exists():
        return {
            "found": {},
            "snapshot_ids": {},
            "unknown_devices": list(devices),
            "error": f"main.duckdb not found at {db}",
        }

    found: dict[str, list[dict[str, Any]]] = {}
    snapshot_ids: dict[str, str] = {}
    unknown_devices: list[str] = []

    _IOS_VIEW = "v_show_ip_interface_brief_auto"
    _JUNOS_VIEW = "v_show_interfaces_terse_auto"

    with duckdb.connect(str(db), read_only=True) as con:
        have_ios = _view_exists(con, _IOS_VIEW)
        have_junos = _view_exists(con, _JUNOS_VIEW)
        absent_sources = [
            {"view": f"netops.{v}", "command": _command_for(v)}
            for v, present in ((_IOS_VIEW, have_ios), (_JUNOS_VIEW, have_junos))
            if not present
        ]
        if not have_ios and not have_junos:
            return {
                "found": {},
                "snapshot_ids": {},
                "unknown_devices": list(devices),
                "absent_sources": absent_sources,
                "error": "no interface data in this database — neither "
                         f"{_command_for(_IOS_VIEW)!r} nor "
                         f"{_command_for(_JUNOS_VIEW)!r} was collected",
            }

        if not devices:
            names: list[str] = []
            for view, present in ((_IOS_VIEW, have_ios), (_JUNOS_VIEW, have_junos)):
                if not present:
                    continue
                names += [r[0] for r in con.execute(
                    f"SELECT DISTINCT device_name FROM netops.{view}"
                ).fetchall()]
            target_devices = sorted(set(names))
        else:
            target_devices = list(devices)

        for d in target_devices:
            ios_snap = jun_snap = None
            if have_ios:
                row = con.execute(f"""
                    SELECT MAX(snapshot_id)
                    FROM netops.{_IOS_VIEW}
                    WHERE device_name = ?
                """, [d]).fetchone()
                ios_snap = row[0] if row else None
            if have_junos:
                row = con.execute(f"""
                    SELECT MAX(snapshot_id)
                    FROM netops.{_JUNOS_VIEW}
                    WHERE device_name = ?
                """, [d]).fetchone()
                jun_snap = row[0] if row else None

            if not ios_snap and not jun_snap:
                unknown_devices.append(d)
                continue

            entries: list[dict[str, Any]] = []
            if ios_snap:
                snapshot_ids[d] = ios_snap
                rows = con.execute("""
                    SELECT interface, ip_address, status, proto
                    FROM netops.v_show_ip_interface_brief_auto
                    WHERE device_name = ? AND snapshot_id = ?
                    ORDER BY interface
                """, [d, ios_snap]).fetchall()
                for intf, ip, status, proto in rows:
                    if not include_unassigned:
                        if not ip or str(ip).lower() in ("unassigned", "none", ""):
                            continue
                    entries.append({
                        "interface": intf,
                        "ip_address": ip,
                        "status": status,
                        "proto": proto,
                    })
            if not entries and jun_snap:
                snapshot_ids[d] = jun_snap
                rows = con.execute("""
                    SELECT interface, ip_address, link_state, proto
                    FROM netops.v_show_interfaces_terse_auto
                    WHERE device_name = ? AND snapshot_id = ?
                    ORDER BY interface
                """, [d, jun_snap]).fetchall()
                for intf, ip, link_state, proto in rows:
                    if not include_unassigned:
                        if not ip or str(ip).strip() == "":
                            continue
                    entries.append({
                        "interface": intf,
                        "ip_address": ip,
                        "status": link_state,
                        "proto": proto,
                    })

            if not entries:
                unknown_devices.append(d)
            else:
                found[d] = entries

    return {
        "found": found,
        "snapshot_ids": snapshot_ids,
        "unknown_devices": unknown_devices,
        "absent_sources": absent_sources,
    }


if __name__ == "__main__":
    import json as _json
    import sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = inspect_interfaces(**_args)
    print(_json.dumps(result, default=str))
