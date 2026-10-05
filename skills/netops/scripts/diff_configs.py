#!/usr/bin/env python3
"""diff_configs — unified diff of raw CLI output between two snapshot_ids.

The actual implementation lives in
``olav_netops.core.diff.configs.diff_configs`` (per ADR-0007 R91
Step 3). This script exposes it for subprocess / stdin-JSON invocation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def _find_project_root() -> Path:
    p = Path(__file__).resolve().parent
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


sys.path.insert(0, str(_find_project_root() / "src"))

from olav_netops.core.diff.configs import diff_configs as _diff_configs_impl


def diff_configs(
    device: str,
    command: str,
    snapshot_id_1: str | None = None,
    snapshot_id_2: str | None = None,
    sections: list[str] | None = None,
    context_lines: int = 3,
    full: bool = False,
) -> dict[str, Any]:
    """Unified diff of raw CLI output between two snapshot_ids.

    Reads exports/snapshots/{snapshot_id}/raw/{device}/{cmd-slug}.txt
    and emits a unified diff; compact mode caps shown lines.
    """
    return _diff_configs_impl(
        device=device,
        command=command,
        snapshot_id_1=snapshot_id_1,
        snapshot_id_2=snapshot_id_2,
        sections=sections,
        context_lines=context_lines,
        full=full,
    )


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = diff_configs(**_args)
    print(_json.dumps(result, default=str))
