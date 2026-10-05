"""Landing zone for parser templates that arrived inside a bundle.

dev_docs/122 §4/§7. A bundle may carry TextFSM templates (the validator accepts
that extension and no other). This module decides *where they land*, and the
answer is: nowhere that resolves.

Why not the existing ``_quarantine/``
-------------------------------------
``parser_registry.load_parser`` searches ``[main, _quarantine]`` by default, so
a file in ``_quarantine/`` **is loaded** whenever the main tree lacks one. That
tree means "learned from too few samples — use it if nothing better exists": a
*confidence* qualifier, and a fallback by design.

An arriving template carries the opposite qualifier. It is well-formed but its
*provenance* is unknown, and the case where it would be reached — no local
parser for that command — is exactly the case it should not silently win. Two
different kinds of distrust with opposite resolution rules do not belong in one
directory; putting them together would make "when may this be used?" undecidable.

So incoming templates land in ``.olav/templates/_incoming/<platform>/``, which
no lookup consults:

  * ``textfsm_parse`` Priority 1 reads ``templates/<platform>/<cmd>.textfsm`` —
    ``_incoming`` is not a platform, and no platform name starts with ``_``;
  * ``parser_registry`` reads ``templates/parsers/`` and ``templates/_quarantine/``
    for ``.py`` files only.

Nothing here executes, parses, or registers a template. It copies bytes to a
staging tree, records where they came from, and stops. :func:`promote` is the
only way into a resolving path, and a human runs it.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .schema import ALLOWED_TEMPLATE_SUFFIXES

#: Sidecar written next to each staged platform dir, so a reviewer can answer
#: "where did this come from?" without the bundle still being on disk.
PROVENANCE_FILE = "_provenance.json"


def templates_root() -> Path:
    """``.olav/templates`` for the active workspace."""
    from olav.core.config import get_paths_config

    return Path(get_paths_config().agent_dir_path) / "templates"


def incoming_dir() -> Path:
    """Staging tree for templates that arrived from elsewhere.

    Deliberately a sibling of the resolving trees, not a child: a lookup that
    walks ``templates/<platform>/`` must not be able to reach it by accident.
    """
    return templates_root() / "_incoming"


@dataclass(slots=True)
class InstallReport:
    """Outcome of :func:`install_from_bundle`."""

    staged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: None = the bundle had no templates/ at all; [] = it had an empty one.
    rejected: list[str] | None = None

    @property
    def count(self) -> int:
        return len(self.staged)


def install_from_bundle(
    bundle_root: str | Path,
    *,
    source_id: str | None = None,
    dest: Path | None = None,
) -> InstallReport:
    """Copy a bundle's ``templates/`` into the staging tree. Nothing is activated.

    Call this only after :func:`olav.core.ingest.validators.validate_bundle`
    reports ``ok`` — this function re-checks the extension allowlist (a boundary
    is not a place to assume someone else already looked) but does not re-verify
    digests.

    ``source_id`` identifies the bundle in the provenance sidecar; the caller
    usually passes the manifest's ``workspace_id`` or the bundle path.
    """
    root = Path(bundle_root)
    src = root / "templates"
    if not src.is_dir():
        return InstallReport(rejected=None)

    target_root = dest if dest is not None else incoming_dir()
    report = InstallReport(rejected=[])
    now = datetime.now(UTC).isoformat()

    for platform_dir in sorted(d for d in src.iterdir() if d.is_dir()):
        staged_here: list[str] = []
        for entry in sorted(f for f in platform_dir.iterdir() if f.is_file()):
            rel = f"{platform_dir.name}/{entry.name}"
            if entry.suffix not in ALLOWED_TEMPLATE_SUFFIXES:
                report.rejected.append(rel)
                continue
            dest_dir = target_root / platform_dir.name
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / entry.name
            if dest_path.exists() and dest_path.read_bytes() == entry.read_bytes():
                report.skipped.append(rel)
                continue
            shutil.copy2(entry, dest_path)
            report.staged.append(rel)
            staged_here.append(entry.name)

        if staged_here:
            sidecar = target_root / platform_dir.name / PROVENANCE_FILE
            existing = []
            if sidecar.exists():
                try:
                    existing = json.loads(sidecar.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    existing = []
            existing.append({
                "source": source_id or str(root),
                "staged_at": now,
                "files": staged_here,
            })
            sidecar.write_text(json.dumps(existing, indent=1), encoding="utf-8")

    return report


def list_incoming(dest: Path | None = None) -> list[tuple[str, str]]:
    """``(platform, filename)`` for everything awaiting review."""
    root = dest if dest is not None else incoming_dir()
    if not root.is_dir():
        return []
    out = []
    for platform_dir in sorted(d for d in root.iterdir() if d.is_dir()):
        for entry in sorted(f for f in platform_dir.iterdir() if f.is_file()):
            if entry.suffix in ALLOWED_TEMPLATE_SUFFIXES:
                out.append((platform_dir.name, entry.name))
    return out


def promote(
    platform: str,
    filename: str,
    *,
    dest: Path | None = None,
    active_root: Path | None = None,
) -> Path:
    """Move one staged template into the resolving tree. A human decides this.

    Raises ``FileNotFoundError`` if it is not staged, and ``ValueError`` if the
    extension is not allowed — promotion re-checks rather than trusting that
    staging did, because this is the step that makes a file live.
    """
    if not filename.endswith(tuple(ALLOWED_TEMPLATE_SUFFIXES)):
        raise ValueError(
            f"{filename}: only {'/'.join(sorted(ALLOWED_TEMPLATE_SUFFIXES))} "
            "can be promoted"
        )
    staged_root = dest if dest is not None else incoming_dir()
    source = staged_root / platform / filename
    if not source.is_file():
        raise FileNotFoundError(f"not staged: {platform}/{filename}")

    target_root = active_root if active_root is not None else templates_root()
    target_dir = target_root / platform
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    shutil.copy2(source, target)
    source.unlink()
    return target
