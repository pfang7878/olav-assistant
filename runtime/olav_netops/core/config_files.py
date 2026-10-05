"""Resolve a netops_init config file, wherever this install keeps it.

These YAMLs (``discovery_protocols.yaml``, ``platform_profiles.yaml``, …) are
authored as workspace files and deployed by ``olav init`` / ``olav skill install
olav-netops``. But they are also **packaged in the wheel** under
``olav_netops/data/skillpack/``, and the code that reads them is library code
that has no way to know whether a workspace was ever deployed. Two callers used
to hunt for the deployed copy and give up if it was missing; the wheel copy sat
there unused.

Giving up was expensive and silent. ``discovery_protocols.yaml`` missing means
``load_discovery_protocols`` returns ``{}`` and the topology ETL becomes a
no-op, so ``netops.topology_links`` stays empty on a database whose bundle
carried CDP *and* LLDP output — and every consumer downstream reads that as a
network with no links. Measured on a pack install (2026-08-17): 0 links from a
3-device bundle, 4 links from the same bundle with the two YAMLs present.

Order: the deployed workspace wins (a site may edit it — that is the point of a
workspace file), then dev-tree paths, then the wheel copy. ``None`` when the
file genuinely is nowhere, which is a different answer from "found it".
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where the wheel keeps its copy, relative to the ``olav_netops`` package.
_WHEEL_SUBPATH = ("data", "skillpack", ".olav", "workspace", "netops",
                  "netops_init", "config")

_WORKSPACE_SUBPATH = (".olav", "workspace", "netops", "netops_init", "config")


def resolve_netops_init_config(filename: str) -> Path | None:
    """Path to ``filename`` under netops_init/config, or ``None`` if absent.

    Never returns an empty ``Path``: ``Path("")`` is ``PosixPath('.')``, which
    is truthy and exists, so an empty-path sentinel made "not found" look like
    "found the current directory" — callers then read a directory and logged a
    parse error. (Both readers did exactly this until 2026-08-17.)
    """
    # 1. Deployed workspace, via the platform's own path config.
    try:
        from olav.core.config import get_paths_config
        candidate = Path(get_paths_config().agent_dir_path).joinpath(
            *_WORKSPACE_SUBPATH[1:], filename
        )
        if candidate.is_file():
            return candidate
    except Exception:  # noqa: BLE001 — config unavailable is not fatal here
        pass

    # 2. Dev tree: the source workspace next to an editable checkout.
    here = Path(__file__).resolve().parent
    for up in (here.parents[2], here.parents[3]):
        candidate = up.joinpath(*_WORKSPACE_SUBPATH, filename)
        if candidate.is_file():
            return candidate

    # 3. The copy inside this wheel — always present in a released install,
    #    which is what makes the library usable with no workspace at all.
    try:
        import olav_netops
        pkg_root = Path(olav_netops.__file__).resolve().parent
        candidate = pkg_root.joinpath(*_WHEEL_SUBPATH, filename)
        if candidate.is_file():
            return candidate
    except Exception:  # noqa: BLE001
        pass

    return None
