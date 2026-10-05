"""olav_netops.tools.textfsm_parse — universal CLI output parser.

Three-tier priority chain (ARCH-25 / ARCH-27 / GLUE-AUDIT G7):

    Priority 0  — PaC (Python) parser from .olav/templates/parsers/
                  (ARCH-25 — LLM-learned Python `parse()` functions)
    Priority 1  — Custom TextFSM from .olav/templates/<platform>/
                  (auto_learn output + user-seeded templates)
    Priority 2  — ntc-templates built-in library

The first tier that returns a non-empty result wins. Failures at each
tier are logged at WARNING (no silent drops — GLUE-AUDIT G12).

External API (unchanged for back-compat): :func:`parse_output`. The old
``_CMD_ALIASES`` and local ``normalise_platform`` shims have been
removed — platform canonicalization now goes through
:mod:`olav_netops.core.platform_canonical` (GLUE-AUDIT G1-G3 dedup).

Usage::

    from olav_netops.tools.textfsm_parse import parse_output

    parsed = parse_output("cisco_ios", "show ip interface brief", raw_text)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _safe_cmd(command: str) -> str:
    """Normalize a CLI command string for filesystem use.

    Matches the convention in :mod:`olav_netops.core.parser_contract.safe_command`
    and :mod:`olav_netops.core.auto_learn._template_path`. Kept local here
    to avoid a dependency cycle (parser_contract imports are heavier).
    """
    return command.strip().lower().replace(" ", "_").replace("/", "_")[:60]


def _templates_base() -> Path:
    """Resolve ``.olav/templates/`` via the platform paths config."""
    try:
        from olav.core.config import get_paths_config
        return Path(get_paths_config().agent_dir_path) / "templates"
    except Exception as exc:
        logger.warning("_templates_base: get_paths_config failed (%s); falling back to CWD", exc)
        return Path.cwd() / ".olav" / "templates"


# ── Tier 0: PaC Python parser ────────────────────────────────────────────

def _try_pac_parser(platform: str, command: str, raw_output: str) -> list[dict] | None:
    """Load and invoke a frozen PaC Python parser (ARCH-25)."""
    try:
        from olav_netops.core import parser_registry
    except Exception as exc:
        logger.debug("parse_output: parser_registry unavailable: %s", exc)
        return None

    fn = parser_registry.load_parser(platform, command)
    if fn is None:
        return None

    try:
        rows = fn(raw_output, None)
    except Exception as exc:
        logger.warning(
            "parse_output: PaC parser %s/%s raised %s: %s",
            platform, command, type(exc).__name__, exc,
        )
        return None

    if not isinstance(rows, list):
        logger.warning(
            "parse_output: PaC parser %s/%s returned %s, expected list",
            platform, command, type(rows).__name__,
        )
        return None
    if not rows:
        # Empty result is legitimate ("BGP not active" etc.) — pass through
        # but log so it's visible.
        logger.debug("parse_output: PaC parser %s/%s returned 0 rows", platform, command)
    else:
        logger.debug(
            "parse_output: PaC parser %s/%s produced %d record(s)",
            platform, command, len(rows),
        )
    return rows


# ── Tier 1: custom TextFSM at .olav/templates/<platform>/ ────────────────

def _try_custom_textfsm(platform: str, command: str, raw_output: str) -> list[dict] | None:
    """Look for a user-seeded or auto_learn-generated TextFSM template."""
    import textfsm

    base = _templates_base()
    safe = _safe_cmd(command)
    candidates = [
        base / platform / f"{safe}.textfsm",              # nested (preferred)
        base / f"{platform}_{safe}.textfsm",              # flat combined
        base / f"{safe}.textfsm",                         # flat simple
    ]
    for tpl_path in candidates:
        if not tpl_path.exists() or tpl_path.stat().st_size == 0:
            continue
        try:
            with tpl_path.open() as f:
                fsm = textfsm.TextFSM(f)
                rows = fsm.ParseText(raw_output)
            if not rows:
                continue
            headers = [h.lower() for h in fsm.header]
            logger.debug("parse_output: custom TextFSM %s served parse", tpl_path)
            return [dict(zip(headers, row, strict=False)) for row in rows]
        except Exception as exc:
            logger.warning(
                "parse_output: custom TextFSM %s failed: %s", tpl_path, exc,
            )
    return None


# ── Tier 2: ntc-templates built-in ───────────────────────────────────────

# ntc-templates filename aliases: some CLI commands don't map 1:1 to the
# template filename convention. Example: ``show vlan brief`` (the
# command operators actually run) maps to the ``show_vlan`` template
# (which ntc-templates ships under the shorter name). Previously each
# caller kept its own alias map; this one is the SSOT — add entries
# here rather than at call sites.
#
# (Removed: ``show ip ospf neighbors`` → ``show_ip_ospf_neighbor`` —
# stale entry from before ISSUE-NETOPS-COLLECT-OSPF-CMD was fixed.
# The collect path now sends the singular form ``show ip ospf neighbor``
# which matches the template name directly, so no alias needed.)
_NTC_FILENAME_ALIASES: dict[str, str] = {
    "show vlan brief":         "show_vlan",
    "show bgp summary":        "show_ip_bgp_summary",
    "show bgp all summary":    "show_ip_bgp_summary",
}


def _try_ntc_templates(platform: str, command: str, raw_output: str) -> list[dict] | None:
    """Fall back to the ntc-templates built-in library."""
    try:
        import ntc_templates
        import textfsm
    except ImportError as exc:
        logger.debug("parse_output: ntc_templates / textfsm not available: %s", exc)
        return None

    templates_dir = Path(ntc_templates.__file__).parent / "templates"
    safe = _NTC_FILENAME_ALIASES.get(command.strip().lower(), _safe_cmd(command))
    template_path = templates_dir / f"{platform}_{safe}.textfsm"
    if not template_path.exists():
        return None

    try:
        with template_path.open() as f:
            fsm = textfsm.TextFSM(f)
            rows = fsm.ParseText(raw_output)
    except Exception as exc:
        # Upstream ntc-templates parser bugs (State Error / Rule Line N
        # mismatches) are ours to neither fix nor block on — drop to
        # debug so collection stays quiet.  The raw output still lands
        # in raw_output_store; user can /learn_cmd a PaC parser if they
        # need structured data.
        logger.debug(
            "parse_output: ntc-templates %s failed: %s", template_path, exc,
        )
        return None

    if not rows:
        return None
    # R83: lowercase headers to match Tier 1 (custom textfsm) + the
    # standard ``ntc_templates.parse.parse_output`` Python wrapper
    # convention.  Without this, Tier 1 produces ``interface`` while
    # Tier 2 produces ``INTERFACE`` for the same data — which broke
    # the per-command auto-views (a single ``json_structure`` sample
    # can't represent both shapes).  All ingest paths now produce
    # lowercase keys.
    headers = [h.lower() for h in fsm.header]
    logger.debug("parse_output: ntc-templates served parse for %s/%s", platform, command)
    return [dict(zip(headers, row)) for row in rows]


# ── Public API ──────────────────────────────────────────────────────────

def parse_output(
    platform: str,
    command: str,
    raw_output: str,
) -> list[dict[str, Any]] | None:
    """Parse CLI output using the 3-tier priority chain.

    Args:
        platform: Device platform (raw string; normalized internally).
        command: CLI command that produced the output.
        raw_output: Raw CLI text to parse.

    Returns:
        List of parsed dicts (first-tier-success wins). Returns ``None``
        if every tier misses.
    """
    # Lazy import avoids circular dependency at module load.
    from olav_netops.core.platform_canonical import canonicalize_platform

    plat = canonicalize_platform(platform)
    if plat is None:
        logger.warning(
            "parse_output: empty platform string — cannot select parser for command=%r",
            command,
        )
        return None

    # Tier 0 — PaC Python parser
    rows = _try_pac_parser(plat, command, raw_output)
    if rows is None:
        # Tier 1 — custom TextFSM
        rows = _try_custom_textfsm(plat, command, raw_output)
    if not rows:
        # Tier 2 — ntc-templates
        rows = _try_ntc_templates(plat, command, raw_output)

    if rows is None or not rows:
        return None

    # R72: canonicalise interface/IP/ASN/MAC fields before returning so all
    # downstream consumers (view_builder, agent SQL queries, sim/lab Python)
    # see the same representation regardless of which tier produced it.
    try:
        from olav_netops.tools.field_normalizer import normalize_fields
        rows = normalize_fields(rows)
    except Exception as exc:
        logger.warning("parse_output: field normalisation failed (%s); returning raw rows", exc)

    return rows
