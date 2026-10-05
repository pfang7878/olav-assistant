"""Platform / vendor canonicalization — single source of truth.

ARCH-27 / GLUE-AUDIT G1-G3: consolidates the 3 duplicate tables that used
to live in:
  * ``l3_topology_etl.py:_VENDOR_ALIASES``
  * ``netops_init/run.py:_JUNOS_PLATFORMS`` + ``_normalise_platform``
  * (implicit tests inside various conditional blocks)

All callers in `olav_netops` and the netops workspace MUST use
:func:`canonicalize_platform` — never write a local copy of the alias
table.

Note: :data:`~olav_netops.workspace.ops.tools.take_snapshot.SCRAPLI_PLATFORM_MAP`
is NOT consolidated here. That map is a legitimate driver translation
(Nornir platform string → Scrapli driver string, e.g. ``cisco_ios`` →
``cisco_iosxe``); it lives close to the Scrapli call site.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# Canonical form → set of accepted aliases (case-folded, dashes → underscores)
# ONLY this module owns the table. Callers go through canonicalize_platform().
_ALIASES: dict[str, frozenset[str]] = {
    "cisco_ios": frozenset({
        "cisco_ios", "ios", "cisco", "cisco-ios",
        "cisco_ios_xe", "cisco-ios-xe", "iosxe",
    }),
    "cisco_nxos": frozenset({
        "cisco_nxos", "nxos", "cisco-nxos", "nx-os", "nx_os",
    }),
    "cisco_iosxr": frozenset({
        "cisco_iosxr", "iosxr", "ios-xr", "ios_xr", "xr",
    }),
    "juniper_junos": frozenset({
        "juniper_junos", "junos", "juniper", "juniper-junos", "juniperjunos",
    }),
    "arista_eos": frozenset({
        "arista_eos", "eos", "arista", "arista-eos",
    }),
    "huawei_vrp": frozenset({
        "huawei_vrp", "vrp", "huawei", "huawei-vrp",
    }),
}


def canonicalize_platform(raw: str | None) -> str | None:
    """Normalize a platform string to one of the canonical forms.

    Returns None for empty / None input (so callers can distinguish
    "not set" from "unknown but still a valid string"). For an unknown
    non-empty input, returns the cleaned (lowercase, dashes→underscores)
    form unchanged — the caller can treat it as a best-effort platform
    label and later surface it if no templates exist.

    Importantly: we do NOT default unknown inputs to ``cisco_ios``.
    That silent fallback has caused real bugs where Arista/Juniper
    devices were sent IOS commands (GLUE-AUDIT G11). The caller is
    responsible for deciding whether to skip, warn, or error.
    """
    if raw is None:
        return None
    cleaned = str(raw).strip().replace("-", "_").lower()
    if not cleaned:
        return None

    for canonical, aliases in _ALIASES.items():
        if cleaned in aliases:
            return canonical

    # Unknown but not empty — return cleaned form. Caller may still
    # find useful templates (e.g. a user-added "mikrotik_routeros").
    logger.debug("canonicalize_platform: unknown platform %r, returning cleaned %r", raw, cleaned)
    return cleaned


def is_junos(raw: str | None) -> bool:
    """Convenience for the common Junos-specific branch."""
    return canonicalize_platform(raw) == "juniper_junos"


def known_platforms() -> frozenset[str]:
    """Return the canonical platform keys this module recognizes.

    Useful for callers (e.g. `netops_init._load_discovery_commands`)
    that want to validate config keys against the vocabulary.
    """
    return frozenset(_ALIASES.keys())
