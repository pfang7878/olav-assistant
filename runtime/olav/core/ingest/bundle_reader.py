"""Bundle reader — discover layout, parse manifest, yield records.

The reader treats directory and zip inputs uniformly via a small
``_BundleFs`` shim that knows how to ``read_text(path)`` and ``iterdir(path)``
for both backings.  Everything else is layout-walking + YAML loading.

Usage::

    reader = BundleReader.open("/path/to/bundle/ (or .zip)")
    for rec in reader.iter_command_outputs():
        # rec is a CommandRecord — host, command, body, platform, pre_scrubbed
        ...

The reader is **lazy** at read time (each ``iter_command_outputs()`` call
re-walks the bundle) but **eager** about the manifest — ``BundleReader.open``
loads it immediately and fails fast on bad input.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Iterator

import yaml

from .schema import CommandFile, DeviceMeta, Manifest


@dataclass(slots=True)
class CommandRecord:
    """One unit of work for the downstream landing pipeline."""

    host: str
    command: str
    body: str
    platform: str
    mgmt_ip: str | None = None
    vendor: str | None = None
    pre_scrubbed: bool = False
    collected_at_iso: str | None = None


# ── Filesystem abstraction (dir vs. zip) ──────────────────────────────


class _BundleFs:
    """Read-only access to a bundle backed by either a directory or a zip."""

    def __init__(self, source: Path) -> None:
        if source.is_dir():
            self._kind = "dir"
            self._root: Path = source
            self._zf: zipfile.ZipFile | None = None
        elif source.is_file() and zipfile.is_zipfile(source):
            self._kind = "zip"
            self._zf = zipfile.ZipFile(source)
            # Detect optional top-level dir wrapper inside the zip
            # (e.g. ``snapshot_xyz/manifest.yaml``).
            names = self._zf.namelist()
            top_parts = {PurePosixPath(n).parts[0] for n in names if PurePosixPath(n).parts}
            if "manifest.yaml" in names:
                self._zip_prefix = ""
            elif len(top_parts) == 1:
                self._zip_prefix = next(iter(top_parts)) + "/"
            else:
                self._zip_prefix = ""
            self._root = Path("/")  # unused for zip
        else:
            raise FileNotFoundError(f"not a bundle (must be dir or zip): {source}")

    # ----- public read primitives ----------------------------------

    def exists(self, rel: str) -> bool:
        if self._kind == "dir":
            return (self._root / rel).exists()
        # zip backings often lack explicit directory entries — check both a
        # full file match and a directory-prefix presence.
        full = self._zip_prefix + rel  # type: ignore[operator]
        names = self._zf.namelist()  # type: ignore[union-attr]
        if full in names:
            return True
        as_dir = full.rstrip("/") + "/"
        return any(n.startswith(as_dir) for n in names)

    def read_text(self, rel: str) -> str:
        if self._kind == "dir":
            return (self._root / rel).read_text(encoding="utf-8", errors="replace")
        path = self._zip_prefix + rel  # type: ignore[operator]
        with self._zf.open(path) as fh:  # type: ignore[union-attr]
            return fh.read().decode("utf-8", errors="replace")

    def iter_files(self, rel_dir: str) -> Iterator[str]:
        """Yield relative paths of files directly inside ``rel_dir``."""
        if self._kind == "dir":
            base = self._root / rel_dir
            if not base.exists():
                return
            for p in sorted(base.iterdir()):
                if p.is_file():
                    yield p.name
            return
        # zip
        prefix = self._zip_prefix + rel_dir.rstrip("/") + "/"  # type: ignore[operator]
        seen: set[str] = set()
        for name in self._zf.namelist():  # type: ignore[union-attr]
            if not name.startswith(prefix):
                continue
            tail = name[len(prefix):]
            if "/" in tail or tail == "":
                continue
            seen.add(tail)
        yield from sorted(seen)

    def iter_subdirs(self, rel_dir: str) -> Iterator[str]:
        """Yield names of immediate subdirectories under ``rel_dir``."""
        if self._kind == "dir":
            base = self._root / rel_dir
            if not base.exists():
                return
            for p in sorted(base.iterdir()):
                if p.is_dir():
                    yield p.name
            return
        prefix = self._zip_prefix + rel_dir.rstrip("/") + "/"  # type: ignore[operator]
        seen: set[str] = set()
        for name in self._zf.namelist():  # type: ignore[union-attr]
            if not name.startswith(prefix):
                continue
            tail = name[len(prefix):]
            if "/" not in tail:
                continue
            seen.add(tail.split("/", 1)[0])
        yield from sorted(seen)


# ── Public reader ─────────────────────────────────────────────────────


class BundleReader:
    """Read a portable-snapshot bundle from a directory or zip file."""

    def __init__(self, fs: _BundleFs, manifest: Manifest) -> None:
        self._fs = fs
        self._manifest = manifest

    @classmethod
    def open(cls, source: str | Path | IO) -> "BundleReader":
        path = Path(source) if not isinstance(source, Path) else source
        fs = _BundleFs(path)
        if not fs.exists("manifest.yaml"):
            raise FileNotFoundError(
                f"bundle missing manifest.yaml — not a portable snapshot: {path}"
            )
        data = yaml.safe_load(fs.read_text("manifest.yaml")) or {}
        manifest = Manifest.model_validate(data)
        return cls(fs, manifest)

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    # -----

    def iter_command_outputs(self) -> Iterator[CommandRecord]:
        """Yield one ``CommandRecord`` per ``devices/<host>/<cmd>.txt`` file."""
        if not self._fs.exists("devices"):
            raise FileNotFoundError("bundle has no devices/ tree")

        for host in self._fs.iter_subdirs("devices"):
            meta = self._read_device_meta(host)
            host_dir = f"devices/{host}"
            for fname in self._fs.iter_files(host_dir):
                if fname == "_meta.yaml" or fname.startswith("."):
                    continue
                if not fname.endswith(".txt"):
                    continue
                text = self._fs.read_text(f"{host_dir}/{fname}")
                cf = CommandFile.from_text(text, filename_hint=fname)
                yield CommandRecord(
                    host=host,
                    command=cf.command,
                    body=cf.body,
                    platform=meta.platform if meta else "unknown",
                    mgmt_ip=meta.mgmt_ip if meta else None,
                    vendor=meta.vendor if meta else None,
                    pre_scrubbed=cf.pre_scrubbed,
                    collected_at_iso=cf.collected_at_iso,
                )

    # ----- helpers --------------------------------------------------

    def _read_device_meta(self, host: str) -> DeviceMeta | None:
        rel = f"devices/{host}/_meta.yaml"
        if not self._fs.exists(rel):
            return None
        data = yaml.safe_load(self._fs.read_text(rel)) or {}
        return DeviceMeta.model_validate(data)
