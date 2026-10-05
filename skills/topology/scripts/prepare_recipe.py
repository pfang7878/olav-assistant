#!/usr/bin/env python3
"""prepare_recipe — everything needed to draft a recipe YAML, minus the drafting.

The deterministic half of the ARCH-29 discovery flow: find which collected
commands could carry this protocol, sample the parsed field names one of them
actually has, and state the output contract. What it does *not* do is write the
YAML — that is a language model's job, and which model depends on where this
runs:

* inside OLAV, ``discover_recipe`` calls this and hands ``instructions`` to the
  configured LLM endpoint;
* in the published skill pack there is no endpoint, and none is needed — the
  model reading this output is Claude, which drafts the YAML directly.

Splitting it this way is what makes the extension path work in both runtimes
from one copy of the code (dev_docs/122 §2.6 — an LLM is a counterpart, not an
external system). Nothing here writes to the database or disk; ``save_recipe``
validates and persists.

Accepts JSON on stdin: {"protocol": "bfd", "vendor": "cisco_ios"}
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


_PROTOCOL_KEYWORDS = {
    # Built-in — used as fallback hints; user may override.
    "bgp": ["bgp summary"],
    "ospf": ["ospf neighbor"],
    "cdp_lldp": ["cdp neighbor", "lldp neighbor"],
    # Common extensions
    "bfd": ["bfd neighbor", "bfd session"],
    "isis": ["isis neighbor", "isis adjacency"],
    "hsrp": ["hsrp"],
    "vrrp": ["vrrp"],
    "ldp": ["ldp neighbor", "mpls ldp"],
}


def _sample_parsed_entry(con, command: str, vendor: str) -> dict | None:
    """Pick a device of ``vendor`` and return one parsed_data entry."""
    try:
        row = con.execute(
            """
            SELECT parsed_data
            FROM netops.parsed_outputs
            WHERE command = ?
              AND device_name IN (SELECT hostname FROM netops.devices WHERE platform = ?)
              AND parsed_data IS NOT NULL
            LIMIT 1
            """,
            [command, vendor],
        ).fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except Exception:
        return None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    return None


def _candidate_commands(con, protocol: str, vendor: str) -> list[str]:
    """Find commands in raw_output_store that look relevant to this protocol."""
    keywords = _PROTOCOL_KEYWORDS.get(protocol.lower(), [protocol.lower()])
    like_clauses = " OR ".join(["command ILIKE ?"] * len(keywords))
    params = [f"%{k}%" for k in keywords]
    try:
        rows = con.execute(
            f"""
            SELECT DISTINCT command FROM netops.raw_output_store
            WHERE ({like_clauses})
              AND device_name IN (SELECT hostname FROM netops.devices WHERE platform = ?)
            """,
            params + [vendor],
        ).fetchall()
    except Exception:
        return []
    return [r[0] for r in rows if r[0]]


def _canonical_field_hints(protocol: str) -> str:
    """Return a prompt fragment listing canonical target-field names."""
    hints = {
        "bgp": (
            "Target canonical fields (BGPSession Pydantic model):\n"
            "  - neighbor_ip (IP of the peer, required)\n"
            "  - neighbor_as (int, peer AS)\n"
            "  - local_as (int, optional)\n"
            "  - router_id (str, optional)\n"
            "  - state (Literal: Established/Active/Idle/Connect/OpenSent/OpenConfirm/Down)\n"
            "  - uptime (str, optional)"
        ),
        "ospf": (
            "Target canonical fields (OSPFAdjacency Pydantic model):\n"
            "  - neighbor_id (required, router-id of peer)\n"
            "  - neighbor_ip (optional, IP of peer)\n"
            "  - interface (required)\n"
            "  - state (Literal: Full/FULL/DR/FULL/BDR/FULL/DROTHER/2-Way/Init/ExStart/Exchange/Loading/Down)\n"
            "  - area (optional)\n"
            "  - dead_time (optional)"
        ),
    }
    return hints.get(protocol.lower(), (
        "Target canonical fields (custom concept — choose snake_case names "
        "that match the semantic fields in the parsed entry):\n"
        "  - neighbor_ip (if the concept has peer IP)\n"
        "  - state (if the concept has a state field)\n"
        "  - interface (if concept has an interface binding)"
    ))


def render_instructions(
    protocol: str,
    vendor: str,
    commands: list[str],
    sample: dict,
    protocol_concept: str,
) -> str:
    """The drafting brief: candidates, one real sample, and the output contract.

    Written for whichever model reads it. The sample matters more than the
    prose — ``field_mappings`` values have to be keys that exist in the parsed
    output, and the only way to know them is to look.
    """
    sample_json = json.dumps(sample, indent=2, ensure_ascii=False)[:2500]
    cmds_list = "\n".join(f"  - {c!r}" for c in commands[:5])
    hints = _canonical_field_hints(protocol)
    return (
        f"Draft a YAML recipe for the `{protocol_concept}` concept on "
        f"vendor `{vendor}`. Pick ONE command from the candidates below "
        f"that best represents '{protocol}' session/adjacency data.\n\n"
        f"## Candidate commands present in raw_output_store:\n{cmds_list}\n\n"
        f"## Sample parsed_data entry (first record from one device):\n"
        f"```json\n{sample_json}\n```\n\n"
        f"## {hints}\n\n"
        f"## Output contract — emit ONLY YAML (no markdown fences, no prose):\n"
        f"- command: <exact CLI string>\n"
        f"  concept: {protocol_concept}\n"
        f"  vendor_hint: {vendor}\n"
        f"  field_mappings:\n"
        f"    <canonical_name>: <source_json_key_from_sample_above>\n"
        f"    ... (one line per mapping)\n\n"
        f"Rules:\n"
        f"1. `field_mappings` values MUST be keys that exist in the sample.\n"
        f"2. `state` source field is often literally named 'state' or 'state_pfxrcd'.\n"
        f"3. If a canonical field has no good source, OMIT it (don't invent).\n"
        f"4. No Python code, no comments in YAML output.\n"
    )


def prepare_recipe(protocol: str, vendor: str) -> dict[str, Any]:
    """Gather what a recipe for ``(protocol, vendor)`` has to be drafted from.

    Args:
        protocol: User-declared protocol keyword (``bfd``, ``hsrp``, etc.).
        vendor: Target platform (``cisco_ios``, ``juniper_junos``, ``arista_eos``).

    Returns:
        ``{ok, protocol, vendor, concept, commands_found, chosen_command,
        sample, instructions}``. Draft the YAML from ``instructions``, then pass
        it to ``save_recipe``. On failure ``ok=False`` with ``error`` naming what
        is missing — a protocol whose commands were never collected and one whose
        output never parsed need different fixes, so they are different errors.
    """
    import duckdb
    from olav.core.config import MAIN_DB_PATH
    from olav_netops.core.topology_intent import intent_to_concept

    concept = intent_to_concept(protocol)

    con = duckdb.connect(str(MAIN_DB_PATH), read_only=True)
    try:
        commands = _candidate_commands(con, protocol, vendor)
        if not commands:
            return {
                "ok": False,
                "error": (
                    f"no collected command matches {protocol!r} on vendor "
                    f"{vendor!r} — this protocol was never captured. Add its "
                    "show-command to the collection and ingest again, or extend "
                    "_PROTOCOL_KEYWORDS if the command is present under a name "
                    "these keywords miss"
                ),
                "diagnostics": {"commands_found": []},
            }

        # Try each candidate until we find one with non-empty parsed_data.
        sample = None
        chosen_cmd = None
        for cmd in commands:
            s = _sample_parsed_entry(con, cmd, vendor)
            if s:
                sample = s
                chosen_cmd = cmd
                break
        if sample is None:
            return {
                "ok": False,
                "error": (
                    f"found commands {commands} but none have parsed_data on "
                    f"{vendor!r} devices; the parser layer has to succeed first "
                    "— learn a parser for one of them (/learn_cmd) and re-ingest"
                ),
                "diagnostics": {"commands_found": commands, "sample": None},
            }
    finally:
        con.close()

    return {
        "ok": True,
        "protocol": protocol,
        "vendor": vendor,
        "concept": concept,
        "commands_found": commands,
        "chosen_command": chosen_cmd,
        "sample": sample,
        "instructions": render_instructions(
            protocol, vendor, commands, sample, concept
        ),
        "next": "draft the YAML from `instructions`, then call save_recipe(recipe_yaml=...)",
    }


if __name__ == "__main__":
    import json as _json
    import sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = prepare_recipe(**_args)
    print(_json.dumps(result, default=str))
