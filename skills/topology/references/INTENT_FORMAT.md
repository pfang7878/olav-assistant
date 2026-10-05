# Topology intent format — `~/.olav/config/topology.yaml`

Declares WHICH relationship concepts the agent should materialize views
for. This is the **only** config file the user edits.

## Minimum example

```yaml
protocols:
  - bgp
  - ospf
  - cdp_lldp
```

That's it. The first 3 are built-in (ship with olav-netops) — no recipe
discovery, no LLM invocation.

## Extension

Add more protocols one per line. The agent will draft a recipe the first
time you ask for that concept:

```yaml
protocols:
  - bgp
  - ospf
  - cdp_lldp
  - bfd          # agent auto-discovers on first /query_topology bfd
  - hsrp
  - isis
  - ldp
  - vrrp
```

## What the agent does with this list

1. When data lands (`netops_init` Stage 3.6 on a live collection): reads the
   list; compares against `view_recipes`; WARNs for each declared protocol whose
   recipe is missing for some vendor in your inventory. An offline bundle ingest
   does not run this check, so `list_recipes` is how you see coverage there
2. On `/query_topology <protocol>`: if the concept has zero recipes,
   offers to run discovery
3. On `olav recipes discover <protocol>`: runs discovery for each vendor
   in your inventory

## What the user does NOT need to specify

- Vendor — inferred from `netops.devices.platform`
- Command name — agent greps `netops.raw_output_store` by keyword
- Field mappings — agent drafts by inspecting `parsed_data` keys
- Canonical state values — declarative SQL CASE in the generated recipe

## Anti-patterns

- ❌ Listing vendor-specific variants (`- bgp_cisco_ios`) — use plain names
- ❌ Listing the same concept twice — list is a set
- ❌ Per-snapshot overrides — this file is global; use user recipes for
  vendor-specific field overrides
