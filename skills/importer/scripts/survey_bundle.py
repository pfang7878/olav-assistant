#!/usr/bin/env python3
"""survey_bundle — one-shot structural survey of a dropped bundle.

Replaces the manual ls/read_file/glob/grep format-detection workflow.
Returns everything the ingest sub-agent needs to decide its next step
without any additional filesystem calls in the normal case.

Key outputs:
  format               — canonical | rancid | vendor_dump | tech_support | unknown
  ingest_supported     — True only for canonical (current codebase)
  hosts / platforms    — what the bundle contains
  needs_platform_detection — hosts whose platform couldn't be determined
  platform_sample_lines    — first 5 lines from a sample file per unknown host
                             (Tier 3 LLM fallback — no read_file call needed)
  collector            — {name, version} from manifest (for collection_source arg)
  notes                — prescriptive next-step string for the LLM
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Make the sibling helper importable no matter how this file is loaded —
# as a subprocess (execute_skill_script puts scripts/ on sys.path[0]) OR
# imported by file path (governance tests via spec_from_file_location, which
# does NOT add the scripts dir).  Without this the `_bundle_prepare` import
# raises ModuleNotFoundError under the latter.
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# ── constants ────────────────────────────────────────────────────────────────

_MAX_HOSTS = 50       # cap host list to avoid flooding context
_SNIFF_LINES = 5      # lines read per sample file for banner heuristics

# Ordered from most-distinctive to least.  First match wins.
_PLATFORM_SIGNATURES: list[tuple[str, list[str]]] = [
    ("cisco_xr",       ["IOS XR", "RP/0/RSP0", "RP/0/"]),
    ("cisco_nxos",     ["NX-OS", "Cisco Nexus", "Nexus Operating System"]),
    ("cisco_ios",      ["Cisco IOS", "IOS Software", "IOS-XE"]),
    ("juniper_junos",  ["JUNOS", "Junos:", "junos"]),
    ("nokia_sros",     ["TiMOS", "SR OS", "*A:"]),
    ("huawei_vrp",     ["<HUAWEI>", "[HUAWEI]", "Huawei Versatile Routing Platform", "VRP"]),
    ("arista_eos",     ["Arista EOS", "EOS version"]),
]


# ── platform sniffing ────────────────────────────────────────────────────────

def _sniff_platform_from_lines(lines: list[str]) -> str | None:
    """Match banner lines against known platform signatures."""
    head = "\n".join(lines)
    for platform, markers in _PLATFORM_SIGNATURES:
        if any(m in head for m in markers):
            return platform
    return None


def _sample_lines(file_path: Path, n: int = _SNIFF_LINES) -> list[str]:
    """Read first n non-empty lines from a file; silent on error."""
    try:
        result: list[str] = []
        with open(file_path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                stripped = line.rstrip()
                if stripped:
                    result.append(stripped)
                if len(result) >= n:
                    break
        return result
    except OSError:
        return []


def _best_sample_file(host_dir: Path) -> Path | None:
    """Return the first non-empty file found under host_dir."""
    for f in sorted(host_dir.rglob("*")):
        if f.is_file() and f.stat().st_size > 10:
            return f
    return None


# ── format detection ─────────────────────────────────────────────────────────

def _detect_format(root: Path) -> str:
    """Return one of: canonical | rancid | vendor_dump | tech_support | unknown."""
    if (root / "manifest.yaml").is_file() or (root / "manifest.json").is_file():
        return "canonical"

    archives = (
        list(root.glob("*.tar"))
        + list(root.glob("*.tgz"))
        + list(root.glob("*.tar.gz"))
    )
    if archives:
        return "tech_support"

    # Rancid: any subdir containing a configs/ subdirectory
    if any((d / "configs").is_dir() for d in root.iterdir() if d.is_dir()):
        return "rancid"

    # Vendor dump: loose files at root level
    loose = [
        f for f in root.iterdir()
        if f.is_file() and f.suffix in ("", ".txt", ".log")
    ]
    if loose:
        return "vendor_dump"

    return "unknown"


# ── format-specific surveys ──────────────────────────────────────────────────

def _read_manifest(root: Path) -> dict:
    """Parse manifest.yaml or manifest.json; return raw dict."""
    for name in ("manifest.yaml", "manifest.json"):
        p = root / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
            if name.endswith(".json"):
                return json.loads(text)
            try:
                import yaml  # optional but almost always present
                return yaml.safe_load(text) or {}
            except ImportError:
                # minimal yaml: only handle the flat key: value lines we need
                out: dict = {}
                for line in text.splitlines():
                    if ":" in line and not line.startswith(" "):
                        k, _, v = line.partition(":")
                        out[k.strip()] = v.strip()
                return out
        except Exception:
            return {}
    return {}


def _survey_canonical(root: Path) -> dict:
    manifest = _read_manifest(root)

    # Normalise hosts block — tolerate both dict and list forms
    raw_hosts = manifest.get("hosts") or manifest.get("devices") or {}
    if isinstance(raw_hosts, list):
        # list of strings or dicts
        host_names: list[str] = []
        platforms: dict[str, str | None] = {}
        for entry in raw_hosts:
            if isinstance(entry, str):
                host_names.append(entry)
                platforms[entry] = None
            elif isinstance(entry, dict):
                name = entry.get("hostname") or entry.get("name") or ""
                if name:
                    host_names.append(name)
                    platforms[name] = entry.get("platform")
    else:
        host_names = list(raw_hosts.keys())[:_MAX_HOSTS]
        platforms = {
            h: (v.get("platform") if isinstance(v, dict) else None)
            for h, v in raw_hosts.items()
        }

    needs_detection: list[str] = [
        h for h in host_names
        if not platforms.get(h) or platforms[h] in ("unknown", "")
    ]

    # For each needs-detection host: find a sample file and sniff lines
    platform_sample_lines: dict[str, list[str]] = {}
    for host in needs_detection[:_MAX_HOSTS]:
        host_dir = root / "devices" / host
        if not host_dir.is_dir():
            host_dir = root / host
        sf = _best_sample_file(host_dir)
        if sf:
            lines = _sample_lines(sf)
            if lines:
                platform_sample_lines[host] = lines
                # Attempt sniff so LLM gets a hint even before it classifies
                sniffed = _sniff_platform_from_lines(lines)
                if sniffed and not platforms.get(host):
                    platforms[host] = f"sniffed:{sniffed}"  # unconfirmed — still needs discover_platform

    collector = manifest.get("collector") or {}
    if isinstance(collector, str):
        collector = {"name": collector}

    return {
        "manifest_present": True,
        "hosts": host_names[:_MAX_HOSTS],
        "platforms": platforms,
        "commands_seen": (
            manifest.get("commands_seen")
            or manifest.get("commands_collected")
            or 0
        ),
        "needs_platform_detection": needs_detection,
        "platform_sample_lines": platform_sample_lines,
        "collector": collector,
        "bundle_id": manifest.get("bundle_id"),
        "schema_version": manifest.get("schema_version"),
    }


def _survey_rancid(root: Path) -> dict:
    hosts: list[str] = []
    platforms: dict[str, str | None] = {}
    platform_sample_lines: dict[str, list[str]] = {}

    for group_dir in sorted(root.iterdir()):
        if not group_dir.is_dir():
            continue
        configs_dir = group_dir / "configs"
        if not configs_dir.is_dir():
            continue
        for host_file in sorted(configs_dir.iterdir()):
            if not host_file.is_file() or len(hosts) >= _MAX_HOSTS:
                continue
            hostname = host_file.stem
            hosts.append(hostname)
            lines = _sample_lines(host_file)
            sniffed = _sniff_platform_from_lines(lines)
            platforms[hostname] = f"sniffed:{sniffed}" if sniffed else None
            if lines:
                platform_sample_lines[hostname] = lines

    return {
        "manifest_present": False,
        "hosts": hosts,
        "platforms": platforms,
        "commands_seen": 0,
        "needs_platform_detection": [h for h in hosts if not platforms.get(h)],
        "platform_sample_lines": platform_sample_lines,
        "collector": {},
    }


def _survey_vendor_dump(root: Path) -> dict:
    hosts: list[str] = []
    platforms: dict[str, str | None] = {}
    platform_sample_lines: dict[str, list[str]] = {}

    for f in sorted(root.iterdir()):
        if not f.is_file() or f.suffix not in ("", ".txt", ".log"):
            continue
        if len(hosts) >= _MAX_HOSTS:
            break
        hostname = f.stem
        hosts.append(hostname)
        lines = _sample_lines(f)
        sniffed = _sniff_platform_from_lines(lines)
        platforms[hostname] = f"sniffed:{sniffed}" if sniffed else None
        if lines:
            platform_sample_lines[hostname] = lines

    return {
        "manifest_present": False,
        "hosts": hosts,
        "platforms": platforms,
        "commands_seen": 0,
        "needs_platform_detection": [h for h in hosts if not platforms.get(h)],
        "platform_sample_lines": platform_sample_lines,
        "collector": {},
    }


def _survey_tech_support(root: Path) -> dict:
    archives = (
        list(root.glob("*.tar"))
        + list(root.glob("*.tgz"))
        + list(root.glob("*.tar.gz"))
    )
    hosts = [a.name for a in archives[:_MAX_HOSTS]]
    return {
        "manifest_present": False,
        "hosts": hosts,
        "platforms": {},
        "commands_seen": 0,
        "needs_platform_detection": [],
        "platform_sample_lines": {},
        "collector": {},
    }


# ── notes builder ─────────────────────────────────────────────────────────────

def _build_notes(fmt: str, detail: dict) -> str:
    hosts = detail.get("hosts", [])
    needs = detail.get("needs_platform_detection", [])
    n_hosts = len(hosts)
    n_needs = len(needs)

    if fmt == "canonical":
        if n_needs == 0:
            return (
                f"Canonical bundle — {n_hosts} host(s), all platforms known. "
                "Call validate_bundle then ingest_snapshot."
            )
        sample = ", ".join(needs[:3]) + ("…" if n_needs > 3 else "")
        return (
            f"Canonical bundle — {n_hosts} host(s), {n_needs} need platform detection "
            f"({sample}). "
            "For each unknown host call discover_platform_for_host(host_dir). "
            "If it returns confidence='unknown', classify using platform_sample_lines "
            "from this result (no read_file needed). "
            "Then call validate_bundle and ingest_snapshot(host_platforms={{…}})."
        )
    elif fmt == "rancid":
        return (
            f"Rancid layout — {n_hosts} host config(s) detected. "
            "ingest_snapshot does NOT support rancid format yet (Phase 4). "
            "Tell the user to convert to a canonical bundle first."
        )
    elif fmt == "vendor_dump":
        return (
            f"Vendor dump — {n_hosts} loose file(s). "
            "ingest_snapshot does NOT support vendor dumps yet. "
            "Tell the user to convert to a canonical bundle first."
        )
    elif fmt == "tech_support":
        return (
            f"Tech-support archive(s) — {n_hosts} archive(s). "
            "ingest_snapshot does NOT support tech-support bundles yet (Phase 6). "
            "Tell the user to convert to a canonical bundle first."
        )
    return (
        "Unknown bundle structure. "
        "Tell the user what you found and ask them to describe the format or "
        "convert to a canonical bundle."
    )


# ── public entry point ────────────────────────────────────────────────────────

def survey_bundle(path: str) -> dict[str, Any]:
    """Structural survey of a bundle — no DB writes.

    Accepts a canonical directory, a **compressed archive**
    (``.tar.gz`` / ``.tgz`` / ``.tar`` / ``.zip``), or a raw **collector
    dump** (command-major ``network_data/<command>/<host>``). Archives are
    extracted and raw dumps are normalised to canonical transparently, so
    the returned ``path`` is always ready for validate + ingest.

    Args:
        path: Filesystem path to a bundle dir, archive, or collector dump.

    Returns:
        path                  — absolute path to survey/validate/ingest (may
                                 differ from the input when an archive was
                                 extracted or a raw dump was normalised)
        source_path           — the original input path
        format                — canonical | rancid | vendor_dump | tech_support | unknown
        normalized_from       — "collector_dump" when a raw dump was converted
        prepare_note          — what extraction/normalisation happened, if any
        ingest_supported      — True only for canonical bundles
        hosts                 — list of host names detected (≤50)
        platforms             — {host: platform_key | "sniffed:<platform>" | None}
        commands_seen         — count (accurate for canonical; 0 for others)
        needs_platform_detection — hosts requiring discover_platform_for_host
        platform_sample_lines — {host: [line…]} first lines from sample file;
                                 use for Tier 3 LLM classification without read_file
        manifest_present      — True if manifest.yaml/json found
        collector             — {name, version} from manifest (for collection_source)
        bundle_id             — from manifest if available
        notes                 — prescriptive next-step string
    """
    from _bundle_prepare import prepare_input

    prepared = prepare_input(path)
    if prepared.get("error"):
        # Structured failure (never a bare error) so the agent gets a `notes`
        # field and stops cleanly instead of looping trying to recover.
        return {
            "path": path,
            "source_path": path,
            "format": "unknown",
            "ingest_supported": False,
            "hosts": [], "platforms": {}, "commands_seen": 0,
            "needs_platform_detection": [], "platform_sample_lines": {},
            "manifest_present": False, "collector": {}, "bundle_id": None,
            "error": prepared["error"],
            "notes": (
                f"Could not read the input: {prepared['error']}. "
                "Tell the user the path/format is unusable and stop — do not "
                "retry with other tools."
            ),
        }

    root = prepared["root"]
    prepare_note = prepared.get("note") or ""
    normalized_from = prepared.get("normalized_from")

    fmt = _detect_format(root)

    if fmt == "canonical":
        detail = _survey_canonical(root)
    elif fmt == "rancid":
        detail = _survey_rancid(root)
    elif fmt == "vendor_dump":
        detail = _survey_vendor_dump(root)
    elif fmt == "tech_support":
        detail = _survey_tech_support(root)
    else:
        detail = {
            "manifest_present": False, "hosts": [], "platforms": {},
            "commands_seen": 0, "needs_platform_detection": [],
            "platform_sample_lines": {}, "collector": {},
        }

    notes = _build_notes(fmt, detail)
    if prepare_note:
        notes = (
            f"({prepare_note}; validate + ingest the `path` field below, "
            f"not your original input.) {notes}"
        )

    return {
        "path":                    str(root),
        "source_path":             prepared.get("source_path", path),
        "format":                  fmt,
        "normalized_from":         normalized_from,
        "prepare_note":            prepare_note,
        "ingest_supported":        fmt == "canonical",
        "hosts":                   detail["hosts"],
        "platforms":               detail["platforms"],
        "commands_seen":           detail["commands_seen"],
        "needs_platform_detection": detail["needs_platform_detection"],
        "platform_sample_lines":   detail["platform_sample_lines"],
        "manifest_present":        detail["manifest_present"],
        "collector":               detail.get("collector", {}),
        "bundle_id":               detail.get("bundle_id"),
        "notes":                   notes,
    }


if __name__ == "__main__":
    import sys as _sys
    _args = json.loads(_sys.stdin.read() or "{}")
    print(json.dumps(survey_bundle(**_args), default=str))
