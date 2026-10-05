"""Netops-domain config path resolvers.

Owns the netops-specific knowledge about where Nornir configuration lives.
Platform core (``olav.core.config``) deliberately stays domain-agnostic —
this module sits in the netops package so the platform wheel doesn't have
to know ``nornir/config.yaml`` is a thing.

Migration history:
- Pre-M2:  ``.olav/config/nornir/config.yaml``              (flat)
- Pre-M2:  ``.olav/config/domains/netops/nornir/config.yaml`` (domains)
- Post-M2: ``.olav/workspace/netops/config/nornir/config.yaml`` (workspace)
- Post-M3: ``.olav/workspace/netops/probe/config/nornir/config.yaml``
- Post-R32 (ADR-0005): ``.olav/workspace/netops/collect/config/nornir/config.yaml``
- Post-R-AGENT-HIERARCHY Phase A (2026-05-09 dev_docs/69): the orchestrator
  workspace dir was renamed ``ops/`` → ``netops/``; this resolver was
  missed in that rename and reverted by 2026-05-09 follow-up — the
  active path is now ``.olav/workspace/netops/collect/config/nornir/config.yaml``.

The resolver walks the priority order and returns the first existing path;
if nothing exists, returns the flat default as a placeholder (callers
handle missing file).

Relocated from ``olav.core.config._resolve_nornir_config_path`` in the
v0.19 boundary cleanup (ARCH-20 Phase 3 follow-up).
"""
from __future__ import annotations

from pathlib import Path

from olav.core.config import AGENT_DIR, CONFIG_DIR


def resolve_nornir_config_path() -> Path:
    """Resolve the nornir config path with migration-aware fallback.

    Priority:
    -1. Reviewed inventory:          ``.olav/inventory/config.yaml``
    0. Post-R32 collect-scoped path: ``.olav/workspace/netops/collect/config/nornir/config.yaml``
    1. Post-M3 probe-scoped path:    ``.olav/workspace/netops/probe/config/nornir/config.yaml`` (pre-R32)
    2. Post-M2 workspace path:       ``.olav/workspace/netops/config/nornir/config.yaml``
    3. Legacy domains path:          ``.olav/config/domains/netops/nornir/config.yaml``
    4. Old flat path:                ``.olav/config/nornir/config.yaml``
    """
    # Ahead of everything, because an inventory is **authored** and every path
    # below it is inside a generated tree. `.olav/workspace/**` is a wheel
    # mirror `olav init` converges, so an inventory living there is a hand-edit
    # in a directory whose contract is "this gets refreshed" — and in a tenant
    # repository it is not committed at all, so nobody reviews the file that
    # decides which devices a command reaches.
    #
    # dev_docs/118 §3.1's test is "can it be diffed and does it want review".
    # An inventory answers yes twice, more strongly than the snapshots the
    # section already lists: a snapshot is evidence, an inventory is **intent**.
    # Adding a host or changing a group's `platform` changes what production
    # gear a run touches, and that is a pull request, not a file edit on a
    # server.
    reviewed = AGENT_DIR / "inventory" / "config.yaml"
    if reviewed.exists():
        return reviewed
    collect_path = AGENT_DIR / "workspace" / "netops" / "collect" / "config" / "nornir" / "config.yaml"
    if collect_path.exists():
        return collect_path
    probe_path = AGENT_DIR / "workspace" / "netops" / "probe" / "config" / "nornir" / "config.yaml"
    if probe_path.exists():
        return probe_path
    ws_path = AGENT_DIR / "workspace" / "netops" / "config" / "nornir" / "config.yaml"
    if ws_path.exists():
        return ws_path
    # LEGACY-KEEP: pre-M2 domain config path. Pre-v0.13 installations still
    # have their nornir config at .olav/config/domains/netops/nornir/; keep
    # this probe until the earliest supported release moves past M2.
    legacy_path = CONFIG_DIR / "domains" / "netops" / "nornir" / "config.yaml"
    if legacy_path.exists():
        return legacy_path
    return CONFIG_DIR / "nornir" / "config.yaml"


# Backward-compat alias so callers that used to say
# ``from olav.core.config import _resolve_nornir_config_path`` only need
# to swap the module path, not the symbol name.
_resolve_nornir_config_path = resolve_nornir_config_path


NORNIR_CONFIG_PATH = resolve_nornir_config_path()


__all__ = [
    "resolve_nornir_config_path",
    "_resolve_nornir_config_path",
    "NORNIR_CONFIG_PATH",
]
