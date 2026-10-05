#!/usr/bin/env python3
"""_bundle_prepare — resolve a *raw* import path into a surveyable root.

This is the layer that makes the importer honour what its SKILL.md
advertises: "drop a **directory or zip file** / rancid backup / vendor
dump in". Before this module, ``survey_bundle`` rejected anything that
wasn't already an extracted **canonical** directory — a compressed
``.tar.gz`` backup (the realistic thing a user actually has) produced a
bare ``{"error": "not a directory — zip not yet supported"}`` and the
agent looped trying to extract it by hand.

``prepare_input`` transparently:
  1. **Extracts archives** (``.tar.gz`` / ``.tgz`` / ``.tar`` / ``.zip``)
     to a deterministic temp dir (idempotent — re-imports reuse it).
  2. **Descends wrapper dirs** (e.g. an archive that unpacks to
     ``home/<user>/network_data/…``).
  3. **Normalises a raw *collector dump*** — the command-major
     ``network_data/<command>/<host>`` layout produced by rancid-style
     collectors — into an OLAV **canonical** bundle
     (``devices/<host>/<command>.txt`` + ``_meta.yaml`` + ``manifest.yaml``
     with a ``content_sha256``), which the existing survey→validate→ingest
     path already accepts.

No DB writes. Everything lands under the system temp dir, so it is safe
even when the source archive sits on a read-only mount (the demo mounts
``tmp/anon`` read-only).
"""
from __future__ import annotations

import hashlib
import re
import tarfile
import tempfile
import zipfile
from pathlib import Path

_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")

# Ported from the demo build_bundles.py — the importer must not depend on
# demo scaffolding, so the raw→canonical conversion lives here as production
# code.
_PLATFORM_SIGS: list[tuple[str, list[str]]] = [
    ("cisco_xr",      ["IOS XR", "RP/0/RSP0"]),
    ("cisco_nxos",    ["NX-OS", "Cisco Nexus", "Nexus Operating System"]),
    ("cisco_ios",     ["Cisco IOS", "IOS Software", "IOS-XE", "IOS XE"]),
    ("juniper_junos", ["JUNOS", "Junos:"]),
    ("arista_eos",    ["Arista EOS", "EOS version"]),
    ("nokia_sros",    ["TiMOS", "SR OS"]),
]
_SKIP_CMD_DIRS = {"show_data_sources"}


# ── archive handling ─────────────────────────────────────────────────────────

def _archive_suffix(path: Path) -> str | None:
    name = path.name.lower()
    for suf in _ARCHIVE_SUFFIXES:
        if name.endswith(suf):
            return suf
    return None


def _extract_key(path: Path) -> str:
    """Deterministic per-archive key so re-imports reuse the same temp dir."""
    st = path.stat()
    raw = f"{path.resolve()}:{st.st_size}:{int(st.st_mtime)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract, refusing members that escape ``dest`` (path traversal)."""
    dest = dest.resolve()
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            raise ValueError(f"unsafe path in archive: {member.name!r}")
    tf.extractall(dest)


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for name in zf.namelist():
        target = (dest / name).resolve()
        if not str(target).startswith(str(dest)):
            raise ValueError(f"unsafe path in archive: {name!r}")
    zf.extractall(dest)


def _extract_archive(path: Path, suffix: str) -> Path:
    dest = Path(tempfile.gettempdir()) / "olav_import" / _extract_key(path)
    marker = dest / ".extracted_ok"
    if marker.is_file():        # idempotent — already unpacked
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    if suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            _safe_extract_zip(zf, dest)
    else:
        with tarfile.open(path) as tf:
            _safe_extract_tar(tf, dest)
    marker.write_text("ok", encoding="utf-8")
    return dest


# ── raw collector-dump detection + normalisation ─────────────────────────────

def _is_device_file(name: str) -> bool:
    """A per-host output file: has a dotted hostname, not a text/script ext."""
    return "." in name and not any(name.endswith(ext) for ext in (
        ".txt", ".log", ".py", ".sh", ".conf", ".csv", ".json", ".yaml",
    ))


def _find_collector_dump_root(root: Path) -> Path | None:
    """Return the command-major dir of a raw collector dump, or None.

    Recognises the ``network_data/<command>/<host>`` layout: a directory
    whose children are command dirs, each holding per-host output files.
    Handles arbitrary wrapper nesting (``home/<user>/network_data``).
    """
    # Prefer an explicitly named network_data dir (rancid/collector convention).
    for cand in [root, *root.rglob("network_data")]:
        if cand.is_dir() and _looks_command_major(cand):
            return cand
    # Otherwise probe shallow wrapper dirs for a command-major layout.
    for cand in root.rglob("*"):
        if cand.is_dir() and _looks_command_major(cand):
            return cand
    return None


def _looks_command_major(d: Path) -> bool:
    """True if ``d`` holds ≥1 command subdir containing device files."""
    cmd_dirs = [c for c in d.iterdir() if c.is_dir() and c.name not in _SKIP_CMD_DIRS]
    if not cmd_dirs:
        return False
    hits = 0
    for c in cmd_dirs:
        try:
            if any(f.is_file() and _is_device_file(f.name) for f in c.iterdir()):
                hits += 1
        except OSError:
            continue
        if hits >= 1:
            return True
    return False


def _detect_platform(src: Path, host: str) -> tuple[str, str]:
    ver_file = src / "show_version" / host
    if ver_file.is_file():
        text = ver_file.read_text(errors="ignore")[:2000]
        for platform, markers in _PLATFORM_SIGS:
            if any(m in text for m in markers):
                vendor = "Cisco" if "cisco" in platform else platform.split("_")[0].capitalize()
                return platform, vendor
    return "cisco_ios", "Cisco"


def _extract_mgmt_ip(src: Path, host: str) -> str:
    ipbr = src / "show_ip_interface_brief" / host
    if ipbr.is_file():
        for line in ipbr.read_text(errors="ignore").splitlines():
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if m and m.group(1) != "0.0.0.0":
                return m.group(1)
    return "0.0.0.0"


def _date_hint(source_path: Path) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", source_path.name)
    return m.group(1) if m else "1970-01-01"


def normalize_collector_dump(network_data: Path, out: Path, date: str) -> Path:
    """Convert a command-major collector dump into a canonical bundle dir.

    Idempotent: if ``out/manifest.yaml`` already exists, returns ``out`` as-is.
    """
    import yaml

    if (out / "manifest.yaml").is_file():
        return out
    out.mkdir(parents=True, exist_ok=True)

    commands = sorted(
        p.name for p in network_data.iterdir()
        if p.is_dir() and p.name not in _SKIP_CMD_DIRS
    )
    all_hosts: set[str] = set()
    for cmd in commands:
        for dev_file in (network_data / cmd).iterdir():
            if dev_file.is_file() and _is_device_file(dev_file.name):
                all_hosts.add(dev_file.name)
    hosts_sorted = sorted(all_hosts)

    host_platform: dict[str, str] = {}
    for host in hosts_sorted:
        host_dir = out / "devices" / host
        host_dir.mkdir(parents=True, exist_ok=True)
        platform, vendor = _detect_platform(network_data, host)
        host_platform[host] = platform
        mgmt_ip = _extract_mgmt_ip(network_data, host)
        cmds_ok = 0
        for cmd in commands:
            src_file = network_data / cmd / host
            if src_file.is_file() and _is_device_file(src_file.name):
                (host_dir / (cmd + ".txt")).write_bytes(src_file.read_bytes())
                cmds_ok += 1
        (host_dir / "_meta.yaml").write_text(yaml.dump({
            "hostname": host, "mgmt_ip": mgmt_ip, "platform": platform,
            "vendor": vendor, "commands_attempted": cmds_ok,
            "commands_succeeded": cmds_ok, "commands_failed": 0,
            "collected_at": f"{date}T00:00:00Z",
        }, default_flow_style=False))

    sha = hashlib.sha256()
    for host in sorted(h.name for h in (out / "devices").iterdir()):
        for f in sorted((out / "devices" / host).iterdir()):
            if f.name != "_meta.yaml" and f.is_file():
                sha.update(f.read_bytes())
    content_sha = sha.hexdigest()

    (out / "manifest.yaml").write_text(yaml.dump({
        "schema_version": 1,
        "collector": {"name": "collector-dump", "version": "0.1.0",
                      "invocation": "importer._bundle_prepare"},
        "collected_at": f"{date}T00:00:00Z",
        "collected_by": "importer",
        "workspace_id": "imported",
        "hosts_collected": len(hosts_sorted),
        "hosts_failed": 0,
        "commands_seen": len(commands),
        "redaction": {"pre_scrubbed": False, "salt_fingerprint": "00000000",
                      "netconan_version": "0.0.0"},
        "content_sha256": content_sha,
        "signature": None,
        "hosts": {h: {"platform": host_platform[h]} for h in hosts_sorted},
    }, default_flow_style=False, allow_unicode=True))
    return out


# ── public entry point ───────────────────────────────────────────────────────

def prepare_input(path: str) -> dict:
    """Resolve a raw import ``path`` to a surveyable root directory.

    Returns:
        root            — Path to survey (canonical dir when normalised)
        note            — human-readable description of what was done
        normalized_from — "collector_dump" when a raw dump was converted, else None
        extracted       — True when an archive was unpacked
        source_path     — the original input path
        error           — set only on failure (e.g. path missing)
    """
    src = Path(path).expanduser().resolve()
    if not src.exists():
        return {"error": f"path does not exist: {path}", "root": None}

    note_parts: list[str] = []
    extracted = False
    date = _date_hint(src)

    # 1. Archive → extract.
    if src.is_file():
        suffix = _archive_suffix(src)
        if suffix is None:
            return {
                "error": (
                    f"not a directory and not a supported archive "
                    f"({', '.join(_ARCHIVE_SUFFIXES)}): {path}"
                ),
                "root": None,
            }
        try:
            work = _extract_archive(src, suffix)
        except Exception as exc:  # noqa: BLE001 — surface any extraction failure
            return {"error": f"failed to extract archive: {exc}", "root": None}
        extracted = True
        note_parts.append(f"extracted {suffix} archive")
    else:
        work = src

    # 2. Already canonical? Leave it — survey handles it directly.
    if (work / "manifest.yaml").is_file() or (work / "manifest.json").is_file():
        return {
            "root": work, "note": "; ".join(note_parts) or "canonical directory",
            "normalized_from": None, "extracted": extracted,
            "source_path": str(src),
        }

    # 3. Raw collector dump (command-major)? Normalise to canonical.
    dump_root = _find_collector_dump_root(work)
    if dump_root is not None:
        # Stable digest (NOT builtin hash(), which is per-process randomised)
        # so re-imports reuse the same canonical dir instead of re-converting.
        dump_key = hashlib.sha256(str(dump_root).encode()).hexdigest()[:8]
        out = Path(tempfile.gettempdir()) / "olav_import" / f"canonical_{_date_hint(src)}_{dump_key}"
        try:
            canonical = normalize_collector_dump(dump_root, out, date)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"failed to normalise collector dump: {exc}", "root": None}
        note_parts.append("normalised raw collector dump → canonical bundle")
        return {
            "root": canonical, "note": "; ".join(note_parts),
            "normalized_from": "collector_dump", "extracted": extracted,
            "source_path": str(src),
        }

    # 4. Nothing special — hand the (possibly extracted) root to survey as-is.
    return {
        "root": work, "note": "; ".join(note_parts) or "directory",
        "normalized_from": None, "extracted": extracted,
        "source_path": str(src),
    }
