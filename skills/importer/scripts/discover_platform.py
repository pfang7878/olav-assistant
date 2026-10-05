#!/usr/bin/env python3
"""`discover_platform_for_host` — Tier 1+2 cascade, exposes Tier 3 hand-off.

The ingest sub-agent calls this for any host whose ``_meta.yaml`` declared
``platform: unknown``.

Returns dict with:
  - ``platform`` (str or None)
  - ``vendor`` / ``model`` / ``os_version`` (when classified)
  - ``confidence``:
      * ``textfsm-show-version`` — high confidence (Tier 1)
      * ``textfsm-show-platform`` — show_inventory / show_chassis matched
      * ``unknown`` — agent should ``read_file(sample_file)`` and classify
        visually using prompt / banner heuristics
  - ``sample_file`` (str path or None)
  - ``sample_command`` (str or None)

When ``confidence == "unknown"``, the sub-agent's workflow:
  1. ``read_file(sample_file, limit=50)``
  2. Apply the heuristics from
     ``references/platform_signatures.guide.yaml``:
       · ``R1#`` / ``R1(config)#`` + "Cisco IOS" → cisco_ios
       · ``RP/0/RSP0/CPU0:router#`` + "IOS XR" → cisco_xr
       · ``switch#`` + "NX-OS" → cisco_nxos
       · ``user@host>`` / "JUNOS" → juniper_junos
       · ``*A:router#`` + "SR OS" → nokia_sros
       · ``<HUAWEI>`` / ``[HUAWEI]`` + "VRP" → huawei_vrp
       · "Arista EOS" → arista_eos
  3. Pass the result into ``ingest_snapshot(host_platforms={hostname: …})``.
"""
from __future__ import annotations

from pathlib import Path


def discover_platform_for_host(host_dir: str) -> dict:
    """Run Tier 1+2 platform discovery on one host directory.

    Args:
        host_dir: Absolute path to ``<bundle>/devices/<hostname>/``.

    Returns:
        Dict — see module docstring for shape.
    """
    from olav.core.ingest.platform_discovery import discover_platform

    r = discover_platform(Path(host_dir))
    return {
        "platform":       r.platform,
        "vendor":         r.vendor,
        "model":          r.model,
        "os_version":     r.os_version,
        "confidence":     r.confidence,
        "sample_file":    str(r.sample_file) if r.sample_file else None,
        "sample_command": r.sample_command,
    }


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = discover_platform_for_host(**_args)
    print(_json.dumps(result, default=str))
