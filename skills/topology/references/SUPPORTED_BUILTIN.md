# Built-in protocols, and where `view_recipes` fits

**BGP, OSPF and CDP/LLDP need no recipe.** They are answered from per-command
auto views that the ingest pipeline builds from whatever was collected — one view
per command, columns inferred from the parsed JSON. `query_topology` reads those
views directly.

| Layer | Source view | Built by | Schema model |
|---|---|---|---|
| BGP | `netops.v_show_ip_bgp_summary_auto` | `build_per_command_views` | `BGPSession` |
| OSPF | `netops.v_show_ip_ospf_neighbor_auto` (+ `..._interface_brief_auto` for `area`, when collected) | `build_per_command_views` | `OSPFAdjacency` |
| L2 | `netops.v_l2_links_auto` | `build_l2_topology_view`, from `netops.topology_links` | `L2Link` |

A view exists only if its command was collected, so `query_topology` reports the
layers it cannot answer in `absent_layers`. That is a gap in the collection, not
a missing recipe — no recipe would fix it.

> This file used to say built-in recipes were "automatically loaded into
> `view_recipes` on every `netops_init`", producing `v_bgp_neighbors_auto` and
> `v_ospf_neighbors_auto`. Both statements stopped being true in R83.2, which
> deleted the whole recipe-driven view layer: the built-in recipes were the same
> hardcoded `(concept, command, vendor) → fields` mapping that R78 had removed
> from Python, re-spelled in YAML. Those two view names have not existed since,
> and code written from this page went looking for them (see the schema-drift
> notes in `query_topology.py`).

## What `view_recipes` is for now

**User-added protocols only** — BFD, HSRP, VRRP, IS-IS, LDP, anything declared
in `~/.olav/config/topology.yaml`. A recipe maps a canonical field name onto a
column of an existing per-command auto view, and `rebuild_views` materialises it
as `netops.v_<concept>_auto`.

Nothing seeds the table. `save_recipe` creates it on first use, so an install
that has only ever ingested a bundle is in the normal state, not a broken one:
`list_recipes` returns `{"recipes": [], "table_present": false}` and the
discovery flow in `SKILL.md` works from there.

The shipped `recipes/builtin/*.yaml` files are leftovers of the removed L1 layer.
Their `field_mappings` reference field names (`neighbor_ip`, `state_pfxrcd`) that
the current parsers do not produce, so loading them would build empty views —
`rebuild_views` reports such a recipe under `skipped` with the columns it could
not find. Do not seed them.

## Adding a protocol

Use the discovery flow in `SKILL.md` (`prepare_recipe` → draft → `save_recipe` →
`rebuild_views`). A recipe that proves useful across vendors is worth proposing
as a shipped default in a PR — together with a Pydantic model in
`olav_netops.schemas.topology` if the concept is new — but there is no
auto-loading path for it to plug into, and adding one would re-create what R83.2
removed.
