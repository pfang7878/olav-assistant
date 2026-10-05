#!/usr/bin/env python3
"""inspect_devices — device facts lookup for the analyzer sub-agent.

Migrated from @tool (netops/tools/inspect_devices.py) to script (ADR-0007
rev ~301): stateless read — no persistent state, no audit row, no
cross-process write.  Facts read from NetworkModel (lazy DuckDB load on
attribute access).

Accepts JSON on stdin: {"devices": ["R1", "R3"]}
Pass devices=[] for discovery mode (returns all devices in model.facts).
"""
from __future__ import annotations

from typing import Any

from olav_netops.sim import load_network_model


_FACT_FIELDS = ("platform", "loopback", "local_as", "mgmt_ip", "role")


def inspect_devices(devices: list[str]) -> dict[str, Any]:
    """
    Get consolidated facts for the named devices.

    Returns the facts that *could be resolved* per device, the list
    of facts that *could not* be resolved (so the LLM doesn't read
    None as "configured to nothing"), and a list of devices that are
    not in the inventory at all.

    Use this BEFORE any change plan to see what platform / AS /
    loopback each device has.

    **Discovery mode**: pass an empty list ``[]`` to enumerate facts
    for EVERY device known to the model.  Useful when the agent
    needs to scope a network-wide question (e.g. "look at all BGP
    sessions") and doesn't yet know the device list.

    Args:
        devices: List of hostnames to look up.  E.g. ``["R1", "R3"]``.
            Pass ``[]`` for discovery mode (returns all devices).

    Returns:
        ``{
            "found": {hostname: {field: value, ...}},   # only resolved fields
            "unknown_facts": {hostname: [field, ...]},  # fields that were None
            "unknown_devices": [hostname, ...],         # not in inventory
        }``.
    """
    model = load_network_model()
    facts = model.facts

    target_devices: list[str] = list(devices) if devices else sorted(facts.keys())

    found: dict[str, dict[str, Any]] = {}
    unknown_facts: dict[str, list[str]] = {}
    unknown_devices: list[str] = []

    for d in target_devices:
        record = facts.get(d)
        if record is None:
            unknown_devices.append(d)
            continue
        resolved = {k: v for k, v in record.items() if v is not None}
        missing = [f for f in _FACT_FIELDS if record.get(f) is None]
        found[d] = resolved
        if missing:
            unknown_facts[d] = missing

    return {
        "found": found,
        "unknown_facts": unknown_facts,
        "unknown_devices": unknown_devices,
    }


if __name__ == "__main__":
    import json as _json
    import sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = inspect_devices(**_args)
    print(_json.dumps(result, default=str))
