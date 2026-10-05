"""Topology intent loader — ARCH-29.

Reads ``~/.olav/config/topology.yaml`` for the user-declared protocol list
and computes which ``(protocol, vendor)`` pairs are missing from the
``view_recipes`` table given the devices present in the current snapshot.

The only field the user writes is::

    protocols:
      - bgp
      - ospf
      - cdp_lldp
      - bfd         # optional extension

This module is intentionally tiny (no LLM, no validation framework) —
``netops_init`` Stage 3.6 calls :func:`missing_recipes` to emit WARN lines;
the `topology` sub-skill's ``discover_recipe`` tool reads the intent
when deciding whether to draft a new recipe.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Concept naming convention: user writes short protocol names in intent
# (``bgp``, ``ospf``, ``cdp_lldp``); the internal ``view_recipes.concept``
# column uses longer canonical names (``bgp_neighbors``, ``ospf_neighbors``,
# ``topology_l2``). Map between them here.
_INTENT_TO_CONCEPT: dict[str, str] = {
    "bgp": "bgp_neighbors",
    "ospf": "ospf_neighbors",
    "cdp_lldp": "topology_l2",
    # Extensions below are just "protocol -> same name as concept"; agent
    # may add new entries on discovery.
}


def _intent_path() -> Path:
    """Return ``~/.olav/config/topology.yaml`` via platform paths config."""
    try:
        from olav.core.config import get_paths_config
        return Path(get_paths_config().config_dir) / "topology.yaml"
    except Exception:
        return Path.home() / ".olav" / "config" / "topology.yaml"


def load_intent() -> list[str]:
    """Return the user-declared ``protocols:`` list.

    Empty list if the config file is missing (caller treats as "use
    builtin defaults: bgp, ospf, cdp_lldp").
    """
    path = _intent_path()
    if not path.exists():
        return []
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        protocols = data.get("protocols") or []
        if not isinstance(protocols, list):
            logger.warning(
                "topology_intent: %s 'protocols:' must be a list; got %s",
                path, type(protocols).__name__,
            )
            return []
        # Normalize: lowercase, strip whitespace, dedupe, preserve order
        seen: set[str] = set()
        out: list[str] = []
        for p in protocols:
            if not isinstance(p, str):
                continue
            key = p.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out
    except Exception as exc:
        logger.warning("topology_intent: load failed (%s): %s", path, exc)
        return []


def intent_to_concept(protocol: str) -> str:
    """Map intent keyword (``bgp``) to view_recipes concept name (``bgp_neighbors``).

    Unknown protocols pass through unchanged (treated as a custom concept).
    """
    return _INTENT_TO_CONCEPT.get(protocol, protocol)


def missing_recipes(conn: Any, intent: list[str]) -> list[tuple[str, str]]:
    """Return ``[(protocol_intent, vendor)]`` pairs declared in intent whose
    corresponding ``view_recipes`` row is missing for that vendor.

    A "vendor" is any distinct ``platform`` present in ``netops.devices`` —
    this avoids warning about Arista recipes on an all-Cisco deployment.
    """
    if not intent:
        return []

    # Discover vendors actually in the inventory.
    try:
        rows = conn.execute(
            "SELECT DISTINCT platform FROM netops.devices "
            "WHERE platform IS NOT NULL"
        ).fetchall()
    except Exception:
        return []
    vendors = [r[0] for r in rows if r[0]]
    if not vendors:
        return []

    # Which (concept, vendor) pairs already have a recipe?
    try:
        recipe_rows = conn.execute(
            "SELECT DISTINCT concept, vendor_hint FROM view_recipes"
        ).fetchall()
    except Exception:
        recipe_rows = []
    have: set[tuple[str, str]] = set()
    for concept, vendor_hint in recipe_rows:
        have.add((concept, vendor_hint or "universal"))

    # Gap matrix.
    missing: list[tuple[str, str]] = []
    for protocol in intent:
        concept = intent_to_concept(protocol)
        for vendor in vendors:
            # A "universal" recipe (like topology_l2 via @topology_links)
            # satisfies every vendor — check either specific OR universal.
            if (concept, vendor) in have or (concept, "universal") in have:
                continue
            missing.append((protocol, vendor))
    return missing


# ── Default intent (used when ~/.olav/config/topology.yaml absent) ───────

DEFAULT_INTENT: list[str] = ["bgp", "ospf", "cdp_lldp"]


def effective_intent() -> list[str]:
    """Return the intent the pipeline should honor.

    If user config is absent, returns :data:`DEFAULT_INTENT` so OOTB
    coverage works without any user action.
    """
    declared = load_intent()
    return declared or list(DEFAULT_INTENT)
