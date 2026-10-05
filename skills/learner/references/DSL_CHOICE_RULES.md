# DSL Choice Rules

The command learner emits either a **TextFSM template** or a
**Python parser function**. Picking the wrong DSL wastes LLM budget
and produces brittle parsers; picking the right one is the whole
point of the router.

## Authoritative decider: the LLM

Per user decision (Round 71), the DSL choice is made by the LLM,
not a code-based heuristic. The prompt tells the LLM to inspect the
raw output and emit one of two marker lines:

```
# OLAV_DSL: textfsm
```

followed by a TextFSM template, or

```
# OLAV_DSL: python
```

followed by a `def parse(raw: str) -> list[dict]` function.

## Why LLM-decides

A code router would inspect structural features (column alignment,
blank-line blocks, indentation depth) and decide. That works for the
clear cases but misfires on edge cases (Arista mixed table+block,
NX-OS overlapped headers, vendor-specific warts). The LLM has the
context to see semantic intent that pure structure can't capture.

Trade-off accepted: slightly higher token cost per call (the LLM
spends ~50 tokens in the marker preamble reasoning about DSL) in
exchange for better fit.

## Guidance embedded in the prompt

The system prompt gives the LLM concrete criteria:

- **TextFSM preferred when**:
  - Output is an aligned-column table (`show interfaces brief`,
    `show ip route`, `show vlan`, `show arp`)
  - One record per line with consistent column positions
  - ntc-templates has similar patterns for the same vendor

- **Python preferred when**:
  - Multi-line blocks separated by blank lines
    (Junos `show bgp summary` peer blocks, Cisco `show cdp neighbors
    detail` per-neighbor sections, `show version` multi-paragraph)
  - Key-value output with indented continuation lines
  - Nested sections (route-map / class-map / policy-map configs)
  - Anything where TextFSM's line-oriented state machine would
    struggle to express the relationship

## When the LLM picks wrong

Validation (see `FROZEN_LAYOUT.md`) catches most misrouting:

- TextFSM that produces 0 records → retry with an explicit hint
  "try Python for multi-line blocks"
- Python that's trivially a table → accept (Python is always a
  superset; not worth re-trying)

The retry budget (default 2) absorbs the occasional miss.

## Not-learned cases

The router never runs for:

- `% Invalid input detected...` → `should_learn()` returns False
- Empty raw output (len < 10 chars)
- Known-backup commands (`show running-config`, `show configuration`)
  unless the user explicitly asks

These are filtered *before* the router is invoked, so they don't
consume LLM budget.
