"""Auto-discover device platform from bundled command output.

Three-tier cascade (ADR-0007 Python-first + LLM fallback):

  Tier 1 — TextFSM on show_version
           Try each candidate platform key against the body; the one that
           produces ≥1 parsed row wins, and we get model + os_version for free.

  Tier 2 — TextFSM on show_platform / show_inventory / show_chassis
           Same cascade against secondary commands when show_version is
           absent or unparseable.

  Tier 3 — Caller's LLM
           Python returns ``confidence='unknown'`` plus ``sample_file`` —
           the path to one .txt file the caller should hand to an LLM
           for visual classification (prompt / banner heuristics).

The "platform key that parses cleanly IS the right platform key" — there's
no separate registry to maintain.  Whatever ntc-templates ships, we use.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# Ordered: cheapest / most-common first.  ntc-templates supports each.
PLATFORM_CANDIDATES: tuple[str, ...] = (
    "cisco_ios",
    "cisco_nxos",
    "cisco_xr",
    "arista_eos",
    "juniper_junos",
    "huawei_vrp",
    "nokia_sros",
)

# Commands the discovery cascade will try, in priority order.  show_version
# is by far the richest; the rest are last-ditch fallbacks before LLM.
_DISCOVERY_COMMANDS: tuple[tuple[str, str], ...] = (
    ("show version",       "show_version.txt"),
    ("display version",    "display_version.txt"),       # huawei
    ("show platform",      "show_platform.txt"),
    ("show inventory",     "show_inventory.txt"),
    ("show chassis hardware", "show_chassis_hardware.txt"),  # junos
)

# Tier 0 — filename-signature fast path.  These ``<safe_command>`` filenames
# are vendor-unique: a host's bundle containing any of these maps to a
# single platform with no TextFSM parse needed.  Microsecond-level
# pre-filter; falls through to Tier 1+2+3 when no signature matches.
#
# Add candidates here when you discover a command that *only* one vendor
# emits — the cost of a false positive is high (mis-classification leaks
# into raw_output_store.platform), so keep the list conservative.
_VENDOR_UNIQUE_SAFE_FILENAMES: dict[str, str] = {
    # cisco_ios (IOS / IOS-XE / IOS-XE WLC family)
    "show_wireless_mobility_summary":   "cisco_ios",   # WLC 9800
    "show_wireless_mobility":           "cisco_ios",   # WLC 9800 alt
    "show_stack-power_budget":          "cisco_ios",   # Catalyst stack-power
    # cisco_nxos
    "show_feature":                     "cisco_nxos",
    "show_vpc":                         "cisco_nxos",
    "show_vpc_consistency-parameters":  "cisco_nxos",
    # juniper_junos
    "show_chassis_hardware":            "juniper_junos",
    "show_interfaces_terse":            "juniper_junos",
    "show_chassis_cluster":             "juniper_junos",   # SRX HA
    "show_route_terse":                 "juniper_junos",
    # arista_eos — `show inventory` / `show running-config` are too generic;
    # EOS has very few vendor-unique commands the operator typically dumps.
    # huawei_vrp
    "display_version":                  "huawei_vrp",
    "display_current-configuration":    "huawei_vrp",
    "display_interface":                "huawei_vrp",
    # nokia_sros
    "admin_display-config":             "nokia_sros",
}

_PLATFORM_VENDOR: dict[str, str] = {
    "cisco_ios":      "Cisco",
    "cisco_nxos":     "Cisco",
    "cisco_xr":       "Cisco",
    "arista_eos":     "Arista",
    "juniper_junos":  "Juniper",
    "huawei_vrp":     "Huawei",
    "nokia_sros":     "Nokia",
}


@dataclass(slots=True)
class DiscoveryResult:
    """Outcome of ``discover_platform``.

    ``confidence`` values:
      * ``"filename-signature"``    — Tier 0 vendor-unique filename detected
      * ``"textfsm-show-version"``  — parsed via Tier 1, high confidence
      * ``"textfsm-show-platform"`` — parsed via Tier 2 fallback command
      * ``"unknown"``               — no TextFSM template matched; LLM
                                      (Tier 3) should inspect ``sample_file``
    """

    platform:    str | None
    vendor:      str | None
    model:       str | None
    os_version:  str | None
    confidence:  Literal[
        "filename-signature",
        "textfsm-show-version", "textfsm-show-platform", "unknown",
    ]
    sample_file: Path | None = None
    sample_command: str | None = None


def _try_textfsm(platform: str, command: str, body: str):
    """Try parsing a single (platform, command, body); return rows or None."""
    if not body or not body.strip():
        return None
    try:
        from olav_netops.tools.textfsm_parse import parse_output
        return parse_output(platform, command, body)
    except Exception:  # noqa: BLE001
        return None


def _extract_model_os(parsed_row: dict) -> tuple[str | None, str | None]:
    """Pull (model, os_version) from a parsed show_version row.

    ntc-templates normalises field names to lowercase per R83; some
    legacy templates still emit uppercase — accept either.
    """
    def _ci(d: dict, *keys: str):
        for k in keys:
            for cand in (k, k.lower(), k.upper()):
                if cand in d:
                    val = d[cand]
                    if val in (None, "", []):
                        continue
                    return val
        return None

    hw = _ci(parsed_row, "hardware")
    if isinstance(hw, list):
        model = hw[0] if hw else None
    else:
        model = hw or _ci(parsed_row, "model")
    os_ver = (
        _ci(parsed_row, "junos_version")
        or _ci(parsed_row, "version")
        or _ci(parsed_row, "rommon")
        or _ci(parsed_row, "software_image")
    )
    return model, os_ver


def _strip_olav_header(text: str) -> str:
    """Drop the leading ``# command:`` header block written by our converter."""
    if not text.startswith("#"):
        return text
    lines = text.splitlines(keepends=True)
    idx = 0
    while idx < len(lines) and lines[idx].lstrip().startswith("#"):
        idx += 1
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    return "".join(lines[idx:])


def _read_cmd_body(host_dir: Path, filename: str) -> str | None:
    p = host_dir / filename
    if not p.is_file():
        return None
    try:
        return _strip_olav_header(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None


def _pick_sample(host_dir: Path) -> tuple[Path | None, str | None]:
    """Pick one representative file for LLM inspection.

    Priority: show_running-config / show_configuration (banner-rich) →
    show_logging / show_clock → any .txt.
    """
    preferred = (
        ("show running-config", "show_running-config.txt"),
        ("show configuration",  "show_configuration.txt"),
        ("show logging",        "show_logging.txt"),
        ("show clock",          "show_clock.txt"),
    )
    for cmd, fname in preferred:
        p = host_dir / fname
        if p.is_file():
            return p, cmd
    # Fallback: any .txt under the host dir.
    for p in sorted(host_dir.glob("*.txt")):
        if p.name == "_meta.yaml":
            continue
        return p, p.stem.replace("_", " ")
    return None, None


def discover_platform(host_dir: Path) -> DiscoveryResult:
    """Run the Tier 1 + Tier 2 cascade against one device directory.

    Args:
        host_dir: ``<bundle>/devices/<hostname>/`` — contains _meta.yaml +
            ``<safe_cmd>.txt`` files.

    Returns:
        ``DiscoveryResult``.  When ``confidence == "unknown"``, the caller
        (typically the ingest sub-agent) inspects ``sample_file`` and
        classifies via LLM.
    """
    host_dir = Path(host_dir)
    if not host_dir.is_dir():
        return DiscoveryResult(
            platform=None, vendor=None, model=None, os_version=None,
            confidence="unknown", sample_file=None, sample_command=None,
        )

    # ── Tier 0: vendor-unique filename signature (microsecond fast path) ──
    # No file content read — just listdir().  When a unique signature is
    # found, narrow the candidate set to that single platform so the
    # Tier 1/2 TextFSM loops only run against the right templates (~7×
    # speed-up, no model-extraction loss).
    tier0_hint: str | None = None
    tier0_sample_file: Path | None = None
    for entry in host_dir.iterdir():
        if entry.suffix != ".txt":
            continue
        plat = _VENDOR_UNIQUE_SAFE_FILENAMES.get(entry.stem)
        if plat:
            tier0_hint = plat
            tier0_sample_file = entry
            break

    candidates: tuple[str, ...] = (
        (tier0_hint,) if tier0_hint else PLATFORM_CANDIDATES
    )

    # ── Tier 1: show_version (also display version on Huawei) ────
    for cmd, fname in _DISCOVERY_COMMANDS[:2]:  # show version + display version
        body = _read_cmd_body(host_dir, fname)
        if not body:
            continue
        for plat in candidates:
            rows = _try_textfsm(plat, cmd, body)
            if rows:
                model, os_ver = _extract_model_os(rows[0])
                return DiscoveryResult(
                    platform=plat,
                    vendor=_PLATFORM_VENDOR.get(plat),
                    model=model,
                    os_version=os_ver,
                    confidence="textfsm-show-version",
                    sample_file=host_dir / fname,
                    sample_command=cmd,
                )

    # ── Tier 2: show_platform / show_inventory / show_chassis_hardware ──
    for cmd, fname in _DISCOVERY_COMMANDS[2:]:
        body = _read_cmd_body(host_dir, fname)
        if not body:
            continue
        for plat in candidates:
            rows = _try_textfsm(plat, cmd, body)
            if rows:
                model, os_ver = _extract_model_os(rows[0])
                return DiscoveryResult(
                    platform=plat,
                    vendor=_PLATFORM_VENDOR.get(plat),
                    model=model,
                    os_version=os_ver,
                    confidence="textfsm-show-platform",
                    sample_file=host_dir / fname,
                    sample_command=cmd,
                )

    # ── Tier 0 fallback: hint was right but TextFSM had no parseable
    # source command.  Return the platform with no model — caller's
    # populate_devices path will extract model from parsed_outputs later.
    if tier0_hint:
        return DiscoveryResult(
            platform=tier0_hint,
            vendor=_PLATFORM_VENDOR.get(tier0_hint),
            model=None,
            os_version=None,
            confidence="filename-signature",
            sample_file=tier0_sample_file,
            sample_command=(tier0_sample_file.stem.replace("_", " ")
                            if tier0_sample_file else None),
        )

    # ── Tier 3: LLM fallback signal — pick a sample file for the agent ──
    sample, sample_cmd = _pick_sample(host_dir)
    return DiscoveryResult(
        platform=None, vendor=None, model=None, os_version=None,
        confidence="unknown",
        sample_file=sample, sample_command=sample_cmd,
    )
