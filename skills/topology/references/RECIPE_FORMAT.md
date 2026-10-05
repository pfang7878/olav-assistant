# Recipe YAML format (agent-facing)

Each recipe is a file under either:

- `olav-netops/.olav/workspace/topology/recipes/builtin/<protocol>_<vendor>.yaml` (shipped)
- `~/.olav/config/recipes/user/<protocol>_<vendor>.yaml` (user / LLM-drafted)

## Schema (top-level is a single-entry list for consistency with loader)

```yaml
- command: "show bgp summary"           # verbatim CLI command (must match parsed_outputs.command)
  concept: bgp_neighbors                # canonical category; pre-defined or user-extended
  vendor_hint: cisco_ios                # one of: cisco_ios, juniper_junos, arista_eos, universal
  field_mappings:                       # canonical_name -> source JSON key in parsed_data
    neighbor_ip: neighbor_ip
    neighbor_as: neighbor_as
    local_as: local_as
    router_id: router_id
    state: state_pfxrcd
    uptime: up_down
  filter_expr: ""                       # optional WHERE fragment (rarely used)
```

## Canonical concepts (ship list)

| concept | produces view | schema |
|---|---|---|
| `bgp_neighbors` | `v_bgp_neighbors_auto` | `BGPSession` |
| `ospf_neighbors` | `v_ospf_neighbors_auto` | `OSPFAdjacency` |
| `topology_l2` | `v_l2_links_auto` (from `topology_links`) | `L2Link` |

User-extended concepts produce `v_<concept>_auto` views and use the
dynamic `custom_concepts` Pydantic container (no typed Literal enum).

## Canonical field names (expected by Pydantic schemas)

### bgp_neighbors (→ BGPSession)
- `neighbor_ip` (required, IPvAnyAddress)
- `neighbor_as` (int)
- `local_as` (int | None)
- `router_id` (str | None)
- `state` (Literal: Established / Active / Idle / Connect / OpenSent / OpenConfirm / Down)
- `uptime` (str | None)

### ospf_neighbors (→ OSPFAdjacency)
- `neighbor_id` (required)
- `neighbor_ip` (IPvAnyAddress | None)
- `interface` (required)
- `area` (str | None)
- `state` (Literal: Full / FULL/DR / FULL/BDR / FULL/DROTHER / 2-Way / ...)
- `dead_time` (str | None)

## State canonicalization rules (applied at ingest, not by recipe or view)

- BGP numeric state ('0', '1', ...) → `'Established'` (Cisco prefix-count idiom)
- BGP `'Establ'` (Junos truncated) → `'Established'`
- OSPF: preserve role suffixes (don't strip `/BDR` from `FULL/BDR`)
- Everything else: pass-through

Recipes declare **which SOURCE field to use**; they do NOT encode canonical
values. R83.2 moved canonicalization out of the view layer into
`olav_netops.tools.field_normalizer`, applied when the output is parsed — so
`parsed_outputs.parsed_data` already holds canonical states before any view
reads it, and `rebuild_views` projects columns without rewriting values. A
source field that still looks raw in the view was raw at ingest time; fix the
parser, not the recipe.

## Special directive: `@topology_links`

For L2 topology (where data is already structured in
`netops.topology_links`), use:

```yaml
- command: "@topology_links"
  concept: topology_l2
  vendor_hint: universal
  field_mappings: {}                     # empty — the ingest pipeline owns this view
```

`v_l2_links_auto` is built by `view_builder.build_l2_topology_view` during
ingest. `rebuild_views` reports the `@`-directive under `skipped` rather than
rebuilding it — the recipe row is coverage metadata, not a view definition.

## Validation (what `save_recipe` checks)

1. YAML parses; required fields present (`command`, `concept`, `field_mappings`)
2. `vendor_hint` is in the allowed set
3. Dry-run `CREATE OR REPLACE VIEW _probe AS ...` succeeds
4. `SELECT COUNT(*) FROM _probe` > 0 (reject empty unless `force=True`)

Canonical field names are **not** checked against the concept's schema — a
mapping the Pydantic schema does not recognise is accepted here and surfaces
later, when a consumer reads the view.

## Example LLM-drafted recipe (BFD on Cisco)

```yaml
- command: show bfd neighbors
  concept: bfd_sessions              # new custom concept
  vendor_hint: cisco_ios
  field_mappings:
    neighbor_ip: neighaddr            # source JSON key
    interface: interface
    state: state
    uptime: up_time
```
