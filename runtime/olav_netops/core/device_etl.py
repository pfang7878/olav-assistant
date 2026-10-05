"""Device ETL — shared logic for refreshing ``netops.devices`` from parsed data.

Extracted from ``netops_init/run.py`` (which originally housed
``_load_host_metadata`` and ``_populate_devices`` as private helpers) so
**both** the full-pipeline (``/netops_init``) and the incremental-capture
(``take_snapshot``) entry points can refresh the device table after
ingest.

R83: take_snapshot used to skip Device ETL entirely — model / os_version
/ last_seen never updated when collecting individual commands.  Wiring
``populate_devices()`` here lets both entry points call the same code.

What lives here:

* :func:`load_host_metadata`  — read ``hosts.yaml`` once, return a per-host
  dict with mgmt_ip + role + site + environment + groups + aliases +
  unrecognised data.* keys.  ``mgmt_ip`` comes from nornir's ``hostname``
  field (the SSH target — the authoritative answer to "what IP do I
  reach this device at").
* :func:`populate_devices` — UPSERT every distinct device in
  ``raw_output_store`` into ``netops.devices``.  Inventory mgmt_ip is
  authoritative; CLI parsing only picks up the loopback IP for metadata
  enrichment.

Why split out: ``netops_init/run.py`` is a workspace script (not in the
package), so other tools couldn't import these helpers.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Role derivation for fleets that arrive without an inventory ──────────────
# ``role`` used to come from ``hosts.yaml`` alone, so it was NULL for every
# device on the offline-import path — which is the flagship "ingest a fleet with
# no SSH" flow. Three consumers assume it is populated:
#
#   * ``execute_sql``'s SCHEMA_HINT advertises netops.devices(...,role) on every
#     successful response, so the model is told it can answer "which one is the
#     border router" with it;
#   * SCHEMA_REFERENCE.md lists it as a key column, with a verified SQL example
#     that selects it;
#   * ``sim/graph_view.py`` gates its BGP-fact inference on the role containing
#     one of {border, core, router, edge, spine, leaf} — with role NULL that
#     check fails for every device, so it treats the whole fleet as access
#     switches and the inference never runs.
#
# Measured on a 339-device offline import (dev_docs/115 §1g): role NULL 339/339.
# The agent asked for "the alpha border router", could not use role, fell back
# to guessing hostnames, analysed alpha-wan-6880v instead of alpha-border-4500x,
# and reported the AARNet config as non-existent while it sat in the DB.
#
# Order matters: the first token found wins, so keep the specific ones ahead of
# the generic ``router``. Vocabulary deliberately matches graph_view's set —
# deriving words no consumer understands would be pointless.
_ROLE_TOKENS: tuple[str, ...] = (
    "border",
    "core",
    "spine",
    "leaf",
    "edge",
    "wan",
    "dist",
    "access",
    "oob",
    "router",
)

_SEGMENT_SPLIT = re.compile(r"[^a-z0-9]+")


def infer_role_from_hostname(hostname: str) -> str | None:
    """Derive a role token from *hostname*, or None when nothing matches.

    Deliberately conservative — a wrong role is worse than no role, because
    both the model and ``graph_view`` treat it as fact:

    * only the **first DNS label** is considered, so a domain that happens to
      contain a role word (``host.core.example.com``) cannot tag every device
      in it;
    * only whole ``-``/``_``/``.``-separated segments match, so ``coreless-1``
      is not "core" and ``bordeaux-sw1`` is not "border";
    * anything unmatched stays None rather than guessing.

    >>> infer_role_from_hostname("alpha-border-4500x.net.demo.internal")
    'border'
    >>> infer_role_from_hostname("kappa-2P1-BORDER-S1.net.demo.internal")
    'border'
    >>> infer_role_from_hostname("alpha-as1-3850.net.demo.internal") is None
    True
    """
    if not hostname:
        return None
    short = hostname.split(".", 1)[0].lower()
    segments = {s for s in _SEGMENT_SPLIT.split(short) if s}
    for token in _ROLE_TOKENS:
        if token in segments:
            return token
    return None


def load_host_metadata() -> dict[str, dict]:
    """Return ``{hostname → metadata-dict}`` from the nornir inventory.

    Walks ``hosts.yaml`` once and returns everything the Device ETL
    needs.  Any key under ``data.*`` is forwarded to the caller —
    ``netops.devices`` writes ``role/site/environment`` to dedicated
    columns and packs ``groups`` + ``aliases`` + any other ``data.*``
    keys into the ``metadata`` JSON column.

    Per-host shape::

        {
          "mgmt_ip":     "192.168.100.101" | None,  # nornir's `hostname`
          "role":        "core" | None,
          "site":        "lab"  | None,
          "environment": "lab"  | None,
          "groups":      ["test", "core_routers"],
          "aliases":     ["核心路由器1", "R3"],
          "extra":       { ... other data.* keys ... },
        }

    ``mgmt_ip`` is the nornir SSH target — the authoritative answer to
    "what IP do I reach this device at".  CLI-derived loopback / mgmt0
    addresses are only a fallback when the host isn't in inventory.

    Returns ``{}`` when inventory is missing or unparseable — callers
    then see None/empty everywhere (pre-R77 fallback behaviour).
    """
    try:
        from olav_netops.core.config_paths import resolve_nornir_config_path
        import yaml
        hosts_path = resolve_nornir_config_path().parent / "hosts.yaml"
        if not hosts_path.exists():
            return {}
        with open(hosts_path) as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {}

    out: dict[str, dict] = {}
    for hostname, spec in data.items():
        if not isinstance(spec, dict):
            continue
        host_data = spec.get("data") or {}
        if not isinstance(host_data, dict):
            host_data = {}
        groups = spec.get("groups") or []
        if not isinstance(groups, list):
            groups = []

        entry: dict = {
            "mgmt_ip": spec.get("hostname"),
            "platform": spec.get("platform"),
            "role": host_data.get("role"),
            "site": host_data.get("site"),
            "environment": host_data.get("environment"),
            "groups": [g for g in groups if isinstance(g, str)],
            "aliases": [a for a in host_data.get("aliases", []) if isinstance(a, str)],
        }
        entry["extra"] = {
            k: v for k, v in host_data.items()
            if k not in {"role", "site", "environment", "aliases"}
        }
        for fld in ("role", "site", "environment"):
            val = entry[fld]
            if isinstance(val, str):
                entry[fld] = val.strip() or None
            elif val is not None:
                entry[fld] = str(val)
        out[hostname] = entry
    return out


def populate_devices(db_path: Any, snapshot_id: str = "") -> int:
    """Refresh ``netops.devices`` from the latest parsed_outputs + inventory.

    Idempotent UPSERT — safe to call after every ingest (full pipeline
    or single-command capture).  Reads from DB tables only; nornir is
    consulted for inventory metadata (role/site/mgmt_ip/aliases).

    Sources:

    * ``show version`` parsed row → platform, model, os_version
    * ``show ip interface brief`` (cisco) / ``show interfaces terse``
      (Junos) → loopback IP for metadata enrichment

    Inventory ``mgmt_ip`` is **authoritative** for ``ip_address``; CLI
    parsing only contributes ``metadata.loopback_ip``.

    Args:
        db_path: path-like to ``main.duckdb`` (str or Path).
        snapshot_id: kept in the signature for callers that want to log
            it; not used by SQL — the function reads the most recent
            parsed_outputs row regardless of snapshot.

    Returns:
        Number of (UPSERTed) device rows.
    """
    import duckdb as _ddb
    from olav_netops.core.platform_profiles import get_profile

    host_meta = load_host_metadata()

    def _ci(d: dict, *keys: str):
        """Case-insensitive dict.get over multiple key names.

        R83 normalised parser output to lowercase, but device_etl was
        previously written against UPPERCASE ntc-templates field names
        (``HARDWARE`` / ``VERSION`` / …).  This helper accepts either
        case so populate_devices keeps working before AND after the
        normalisation rolls out across snapshots.
        """
        if not d:
            return None
        for k in keys:
            for cand in (k, k.lower(), k.upper()):
                if cand in d:
                    val = d[cand]
                    # Reject empty / falsy values so the OR-chain in
                    # callers continues searching the next key.  Lists
                    # are common from textfsm `Value List ...` lines —
                    # treat ``[]`` as missing.
                    if val in (None, ""):
                        continue
                    if isinstance(val, list) and not val:
                        continue
                    return val
        return None

    count = 0
    with _ddb.connect(str(db_path)) as conn:
        # ARCH-08 Phase 2: ensure environment column exists for pre-R48
        # deployments.  No-op when schema is already current.
        try:
            conn.execute(
                "ALTER TABLE netops.devices ADD COLUMN IF NOT EXISTS environment VARCHAR"
            )
        except Exception as exc:
            logger.debug("devices.environment migration skipped: %s", exc)

        devices = conn.execute(
            "SELECT DISTINCT device_name FROM netops.raw_output_store"
        ).fetchall()

        for (device_name,) in devices:
            model = os_ver = plat = None

            inv_meta = host_meta.get(device_name) or {}
            mgmt_ip = inv_meta.get("mgmt_ip")
            # Nornir inventory's ``platform`` is the SSH driver name —
            # also the netmiko / ntc-templates / scrapli identifier — so
            # it's authoritative for ``netops.devices.platform``.  CLI
            # ``show version`` parsing is only a fallback for hosts
            # missing from inventory.
            plat = inv_meta.get("platform")
            loopback_ip = None

            # show version → platform, model, os_version
            try:
                row = conn.execute(
                    "SELECT parsed_data::VARCHAR FROM netops.parsed_outputs "
                    "WHERE device_name=? AND command='show version' "
                    "ORDER BY snapshot_id DESC LIMIT 1",
                    [device_name],
                ).fetchone()
                if row and row[0]:
                    entries = json.loads(row[0])
                    if entries and isinstance(entries, list):
                        v = entries[0]
                        # Inventory wins; CLI fallback only fires for
                        # hosts not in nornir.
                        if not plat:
                            if _ci(v, "JUNOS_VERSION"):
                                plat = "juniper_junos"
                            elif _ci(v, "HARDWARE", "VERSION"):
                                plat = "cisco_ios"
                        hw = _ci(v, "HARDWARE")
                        model = _ci(v, "MODEL") or (hw[0] if isinstance(hw, list) and hw else hw)
                        os_ver = (_ci(v, "JUNOS_VERSION") or _ci(v, "VERSION")
                                  or _ci(v, "ROMMON") or _ci(v, "SOFTWARE_IMAGE") or "")
            except Exception:
                pass

            # show ip interface brief → loopback (cisco)
            try:
                row = conn.execute(
                    "SELECT parsed_data::VARCHAR FROM netops.parsed_outputs "
                    "WHERE device_name=? AND command='show ip interface brief' "
                    "ORDER BY snapshot_id DESC LIMIT 1",
                    [device_name],
                ).fetchone()
                if row and row[0]:
                    ifaces = json.loads(row[0])
                    if ifaces and isinstance(ifaces, list):
                        for iface in ifaces:
                            name = (_ci(iface, "INTF", "INTERFACE") or "").lower()
                            ip = _ci(iface, "IPADDR", "IP_ADDRESS") or ""
                            status = (_ci(iface, "STATUS") or "").lower()
                            if "loopback" in name and ip and ip != "unassigned" and "up" in status:
                                loopback_ip = ip
                                break
            except Exception:
                pass

            # show interfaces terse → loopback (Junos)
            try:
                row = conn.execute(
                    "SELECT parsed_data::VARCHAR FROM netops.parsed_outputs "
                    "WHERE device_name=? AND command='show interfaces terse' "
                    "ORDER BY snapshot_id DESC LIMIT 1",
                    [device_name],
                ).fetchone()
                if row and row[0]:
                    ifaces = json.loads(row[0])
                    if ifaces and isinstance(ifaces, list):
                        for iface in ifaces:
                            name = (_ci(iface, "INTERFACE") or "").lower()
                            ip = (_ci(iface, "IP_ADDRESS") or "").split("/")[0]
                            if "lo0" in name and ip:
                                loopback_ip = ip
                                break
            except Exception:
                pass

            if not mgmt_ip and loopback_ip:
                mgmt_ip = loopback_ip

            # Fallback Tier A: show inventory parsed_outputs → platform + model
            # Fires when show version is absent (e.g. WLC/partial captures).
            # Reads platform column first (auto-discovery may have set it);
            # falls back to PID prefix inference for Cisco devices.
            if not plat or not model:
                try:
                    row = conn.execute(
                        "SELECT parsed_data::VARCHAR, platform FROM netops.parsed_outputs "
                        "WHERE device_name=? AND command='show inventory' "
                        "ORDER BY snapshot_id DESC LIMIT 1",
                        [device_name],
                    ).fetchone()
                    if row:
                        inv_parsed, inv_plat = row[0], row[1]
                        if not plat and inv_plat:
                            plat = inv_plat
                        if inv_parsed:
                            entries = json.loads(inv_parsed)
                            if entries and isinstance(entries, list):
                                for entry in entries:
                                    pid = _ci(entry, "PID") or ""
                                    if not model and pid:
                                        model = pid
                                    # Infer platform from Cisco PID prefixes
                                    if not plat and pid:
                                        if pid.startswith(("C9800", "C9300", "C9200",
                                                           "C9500", "C9400", "C9600",
                                                           "WS-C", "ISR", "ASR")):
                                            plat = "cisco_ios"
                                    if plat and model:
                                        break
                except Exception:
                    pass

            # Fallback Tier B: raw_output_store.platform (auto-discovery)
            # Fires when no parsed CLI output yielded a platform — trust the
            # collector's filename-signature detection instead.
            if not plat:
                try:
                    row = conn.execute(
                        "SELECT platform FROM netops.raw_output_store "
                        "WHERE device_name=? AND platform IS NOT NULL "
                        "ORDER BY updated_at DESC LIMIT 1",
                        [device_name],
                    ).fetchone()
                    if row and row[0]:
                        plat = row[0]
                except Exception:
                    pass

            plat = plat or "unknown"
            vendor = get_profile(plat).get("vendor", "")

            meta = host_meta.get(device_name, {})
            # Inventory role is authoritative; hostname derivation is the
            # fallback for fleets imported without a hosts.yaml (see
            # _ROLE_TOKENS). Provenance goes into metadata so a consumer can
            # tell a declared role from an inferred one.
            role_tag = meta.get("role")
            role_source = "inventory" if role_tag else None
            if not role_tag:
                role_tag = infer_role_from_hostname(device_name)
                role_source = "hostname" if role_tag else None
            site_tag = meta.get("site")
            env_tag = meta.get("environment")
            groups = meta.get("groups") or []
            aliases = meta.get("aliases") or []
            extra = meta.get("extra") or {}
            md: dict = {}
            if groups:
                md["groups"] = groups
            if aliases:
                md["aliases"] = aliases
            if loopback_ip and loopback_ip != mgmt_ip:
                md["loopback_ip"] = loopback_ip
            if role_source:
                md["role_source"] = role_source
            md.update(extra)
            metadata_json = json.dumps(md, ensure_ascii=False) if md else None

            conn.execute("""
                INSERT INTO netops.devices
                    (hostname, ip_address, platform, site, role, vendor, model, os_version, environment, last_seen, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NOW(), ?)
                ON CONFLICT (hostname) DO UPDATE SET
                    ip_address=COALESCE(EXCLUDED.ip_address, netops.devices.ip_address),
                    platform=COALESCE(EXCLUDED.platform, netops.devices.platform),
                    site=COALESCE(EXCLUDED.site, netops.devices.site),
                    role=COALESCE(EXCLUDED.role, netops.devices.role),
                    vendor=COALESCE(EXCLUDED.vendor, netops.devices.vendor),
                    model=COALESCE(EXCLUDED.model, netops.devices.model),
                    os_version=COALESCE(EXCLUDED.os_version, netops.devices.os_version),
                    environment=COALESCE(EXCLUDED.environment, netops.devices.environment),
                    metadata=COALESCE(EXCLUDED.metadata, netops.devices.metadata),
                    last_seen=NOW()
            """, [device_name, mgmt_ip, plat, site_tag, role_tag, vendor, model, os_ver, env_tag, metadata_json])
            count += 1
    return count


__all__ = ["infer_role_from_hostname", "load_host_metadata", "populate_devices"]
