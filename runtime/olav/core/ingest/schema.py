"""Pydantic models — the §3 portable-snapshot-bundle contract.

The bundle on disk looks like::

    snapshot.zip / dir/
    ├── manifest.yaml                 → ``Manifest``
    ├── devices/
    │   ├── <hostname>/
    │   │   ├── _meta.yaml            → ``DeviceMeta``
    │   │   ├── show_version.txt      → ``CommandFile.from_text``
    │   │   └── ...
    └── templates/                    → ``Templates`` (optional)
        └── <platform>/<cmd>.textfsm

These models are the **stable** contract between the acquisition side
(``olav-collector`` wheel / rancid adapter / hand-built bundles) and the
ingest side (``ingest_snapshot``).  Bumping ``schema_version`` is a
breaking change.

``templates/`` (dev_docs/122 §3) lets a site carry the parsers it learned
alongside the output they parse. It is an **optional field, not a version
bump**: an existing bundle still validates, and a bundle carrying templates
still loads in a reader that ignores them.

Its hash is separate from ``content_sha256`` on purpose. ``content_sha256``
is defined over ``devices/*/*.txt``; folding templates into it would
invalidate every bundle already produced under that definition, to describe
a part that is optional. ``Templates.sha256`` covers ``templates/`` alone.

**TextFSM only.** A TextFSM template is declarative — it describes fields and
cannot execute. A Python parser is code, and a bundle crosses a trust boundary
between the site that produced it and the host that ingests it, where
``parser_registry`` would ``exec_module`` it. The validator enforces an
allowlist of one extension; see dev_docs/122 §0 and §4.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SUPPORTED_SCHEMA_VERSIONS: tuple[int, ...] = (1,)


# ── Pydantic models for YAML inputs ───────────────────────────────────


class Collector(BaseModel):
    """``manifest.collector`` block — who/what produced the bundle."""

    model_config = ConfigDict(extra="allow")

    name: str
    version: str
    invocation: str | None = None


class Redaction(BaseModel):
    """``manifest.redaction`` block — scrub state at bundle creation time."""

    model_config = ConfigDict(extra="allow")

    pre_scrubbed: bool
    salt_fingerprint: str | None = None
    netconan_version: str | None = None


#: The only file extension a bundle may carry under ``templates/``. An
#: allowlist of one, because the risk here is code execution and a denylist
#: leaks whatever it forgot (CLAUDE.md § *Stating a safety rule is not
#: following it*).
ALLOWED_TEMPLATE_SUFFIXES: frozenset[str] = frozenset({".textfsm"})


class Templates(BaseModel):
    """``manifest.templates`` block — parsers carried alongside the capture.

    Absent on every bundle produced before dev_docs/122; its absence means
    "no templates", never "unknown".
    """

    model_config = ConfigDict(extra="allow")

    count: int = Field(ge=0, description="Number of template files under templates/")
    sha256: str = Field(
        min_length=64, max_length=64,
        description="Digest over templates/ alone — see the module docstring",
    )


class Manifest(BaseModel):
    """Top-level ``manifest.yaml``.

    The reader validates schema_version against ``SUPPORTED_SCHEMA_VERSIONS``;
    older / newer versions are explicit ValidationError (no silent best-effort).
    """

    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1] = Field(..., description="Bundle contract version")
    collector: Collector
    collected_at: str | None = None
    collected_by: str | None = None
    workspace_id: str | None = None
    hosts_collected: int = Field(ge=0)
    hosts_failed: int = Field(default=0, ge=0)
    redaction: Redaction
    content_sha256: str = Field(min_length=64, max_length=64)
    signature: str | None = None
    templates: Templates | None = None


class DeviceMeta(BaseModel):
    """Per-host ``_meta.yaml``."""

    model_config = ConfigDict(extra="allow")

    hostname: str
    mgmt_ip: str
    platform: str
    vendor: str
    os_version: str | None = None
    model: str | None = None
    commands_attempted: int | None = None
    commands_succeeded: int | None = None
    commands_failed: int | None = None
    collected_at: str | None = None


# ── Command-file header (not Pydantic — straight text parser) ─────────


# Safe-filename → canonical command name. Required because the original
# command had spaces / pipes / slashes that we cannot recover from the
# safe filename alone.  Small known map; anything missing falls back to
# ``s/_/ /`` which produces a close-enough rendering for the parser.
_SAFE_TO_CMD_DEFAULTS: dict[str, str] = {
    "show_version": "show version",
    "show_ip_interface_brief": "show ip interface brief",
    "show_ip_bgp_summary": "show ip bgp summary",
    "show_ip_route": "show ip route",
    "show_running_config": "show running-config",
    "show_startup_config": "show startup-config",
    "show_configuration": "show configuration",
    "show_interfaces_terse": "show interfaces terse",
    "show_bgp_summary": "show bgp summary",
    "show_route": "show route",
    "show_lldp_neighbors": "show lldp neighbors",
    "show_lldp_neighbors_detail": "show lldp neighbors detail",
    "show_cdp_neighbors": "show cdp neighbors",
    "show_ip_ospf_neighbor": "show ip ospf neighbor",
    "show_ip_route_summary": "show ip route summary",
    "display_current_configuration": "display current-configuration",
    "admin_display_config": "admin display-config",
}


def _command_from_filename(name: str) -> str:
    """Recover a spaced command name from a safe filename like ``show_ip_bgp_summary.txt``.

    Falls back to ``s/_/ /`` for unknown names — close enough for parser routing.
    """
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if stem in _SAFE_TO_CMD_DEFAULTS:
        return _SAFE_TO_CMD_DEFAULTS[stem]
    return stem.replace("_", " ")


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes", "y"}


@dataclass(slots=True)
class CommandFile:
    """One per-command text file's interpreted contents.

    Header format (2-3 leading ``#`` lines, then blank, then body)::

        # command: show ip bgp summary
        # collected_at: 2026-05-15T11:08:42Z
        # pre_scrubbed: true

        BGP router identifier 2.2.2.2 ...

    Missing headers are tolerated — ``from_text`` falls back to the
    filename hint for the command, and to ``pre_scrubbed=False`` (since
    we cannot prove what the producer did without metadata).
    """

    command: str
    body: str
    collected_at_iso: str | None = None
    pre_scrubbed: bool = False

    @classmethod
    def from_text(cls, text: str, *, filename_hint: str | None = None) -> "CommandFile":
        cmd: str | None = None
        collected_at: str | None = None
        pre_scrubbed: bool = False
        idx = 0
        lines = text.splitlines(keepends=True)
        # Consume contiguous leading ``# key: value`` lines.
        while idx < len(lines):
            line = lines[idx]
            stripped = line.lstrip()
            if not stripped.startswith("#"):
                break
            kv = stripped.lstrip("#").strip()
            if ":" in kv:
                key, _, val = kv.partition(":")
                key = key.strip().lower()
                val = val.strip()
                if key == "command" and val:
                    cmd = val
                elif key == "collected_at" and val:
                    collected_at = val
                elif key == "pre_scrubbed" and val:
                    pre_scrubbed = _truthy(val)
            idx += 1
        # Optional blank separator.
        while idx < len(lines) and lines[idx].strip() == "":
            idx += 1
        body = "".join(lines[idx:])

        if cmd is None:
            if filename_hint:
                cmd = _command_from_filename(filename_hint)
            else:
                cmd = "unknown"

        return cls(command=cmd, body=body,
                   collected_at_iso=collected_at, pre_scrubbed=pre_scrubbed)
