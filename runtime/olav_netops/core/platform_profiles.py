"""Platform profile loader — vendor + show-version field hints per platform.

Nornir's inventory (whichever backend is wired: SimpleInventory / NetBox /
Ansible / Dict / …) is the authoritative source for ``platform`` and
``hostname`` (management IP). This module only supplies:
  * vendor display name (cisco_ios → "Cisco")
  * Scrapli fast-path translation (cisco_ios → "cisco_iosxe")
  * show-version parse field names for model / OS version extraction

Builtin profiles ship in
``.olav/workspace/netops/netops_init/config/platform_profiles.yaml`` when a
workspace is deployed, and in the wheel otherwise — see
:mod:`olav_netops.core.config_files`, which is what makes this module work on
an install that never ran ``olav init``. For a
platform not present in the YAML, a convention-based fallback derives the
vendor from the platform string (``nokia_sros`` → Nokia, ``foo_bar`` →
"Foo"). Model / OS version fields stay empty, so those columns are NULL
until a real profile entry is added or auto-learned.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROFILES_CACHE: dict[str, dict[str, Any]] | None = None


def _profiles_path() -> Path | None:
    """Deployed workspace copy, dev tree, or the copy inside the wheel."""
    from olav_netops.core.config_files import resolve_netops_init_config
    return resolve_netops_init_config("platform_profiles.yaml")


def load_profiles(force_reload: bool = False) -> dict[str, dict[str, Any]]:
    """Load and cache the platform profile YAML.

    Returns ``{platform: profile_dict}``. Missing file / parse error →
    empty dict and a WARN log; callers treat as "no known platforms"
    and rely on the convention fallback instead.
    """
    global _PROFILES_CACHE
    if _PROFILES_CACHE is not None and not force_reload:
        return _PROFILES_CACHE
    path = _profiles_path()
    if not path or not path.exists():
        logger.info("platform_profiles.yaml not found; using convention fallback")
        _PROFILES_CACHE = {}
        return _PROFILES_CACHE
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("platform_profiles.yaml parse failed: %s", exc)
        _PROFILES_CACHE = {}
        return _PROFILES_CACHE
    platforms = data.get("platforms") or {}
    if not isinstance(platforms, dict):
        logger.warning("platform_profiles.yaml: top-level 'platforms' must be a mapping")
        _PROFILES_CACHE = {}
        return _PROFILES_CACHE
    _PROFILES_CACHE = platforms
    return _PROFILES_CACHE


def _derive_vendor(platform: str) -> str:
    """Rule-based vendor inference when the platform has no YAML entry.

    Returns the title-cased first token of the platform string
    (``cisco_ios`` → ``Cisco``, ``mikrotik_routeros`` → ``Mikrotik``).
    For cosmetic-correct display names (``Palo Alto``, ``Check Point``,
    ``MikroTik``…) add the platform to ``platform_profiles.yaml`` with
    an explicit ``vendor:`` field — that's data-driven and survives
    upgrades.  No vendor-alias table in code.
    """
    if not platform:
        return ""
    return platform.split("_", 1)[0].title()


def get_profile(platform: str) -> dict[str, Any]:
    """Return the profile for ``platform``; synthesise a convention-based
    stub when the YAML has no entry. The stub carries a vendor derived from
    the platform string and empty model/os_version hints — sufficient for
    Device ETL to write ``hostname`` + ``platform`` + ``vendor`` rows.
    """
    prof = load_profiles().get(platform)
    if prof:
        return prof
    # Fallback: synthesise a minimal profile so callers never need to
    # branch on "is this platform known".
    return {
        "vendor": _derive_vendor(platform),
        "model_fields": [],
        "os_version_fields": [],
    }


def get_vendor(platform: str) -> str:
    """Return the vendor display name, using YAML if present, convention otherwise."""
    if not platform:
        return ""
    prof = load_profiles().get(platform)
    if prof and prof.get("vendor"):
        return prof["vendor"]
    return _derive_vendor(platform)


def get_scrapli_platform(platform: str) -> str | None:
    """Return the Scrapli platform string, or ``None`` when no fast path known."""
    return (load_profiles().get(platform) or {}).get("scrapli_platform") or None


def scrapli_platform_map() -> dict[str, str]:
    """Build a flat ``{nornir_platform: scrapli_platform}`` mapping."""
    return {
        plat: prof["scrapli_platform"]
        for plat, prof in load_profiles().items()
        if prof.get("scrapli_platform")
    }


def extract_model(platform: str, entry: dict[str, Any]) -> str | None:
    """Pull model string from a parsed show-version entry using profile fields."""
    for field in get_profile(platform).get("model_fields") or []:
        val = entry.get(field)
        if isinstance(val, list):
            val = val[0] if val else None
        if val:
            return str(val)
    return None


def extract_os_version(platform: str, entry: dict[str, Any]) -> str | None:
    """Pull OS version string from a parsed show-version entry using profile fields."""
    for field in get_profile(platform).get("os_version_fields") or []:
        val = entry.get(field)
        if isinstance(val, list):
            val = val[0] if val else None
        if val:
            return str(val)
    return None
