# netops DB Schema — agent reference

**Database:** `main.duckdb` (accessed via `execute_sql`).  Reload this
ONLY when composing SQL directly; most queries should go through the
`netops.v_*_auto` views (see *Views* below).

## Preference order when asked for a concept

1. **View first** — `netops.v_bgp_neighbors_auto`, `netops.v_ospf_neighbors_auto`,
   `netops.v_l2_links_auto`.  Vendor-normalised, SQL CASE state-mapped,
   stable schema.
2. **Base table** — `netops.devices`, `netops.parsed_outputs`,
   `netops.topology_links` when you need a column the view doesn't
   project.
3. **Raw fallback** — `netops.raw_output_store` when `parsed_outputs`
   has no row for the `(device, command)` pair you need.

## Views (Stage 3.7 — `view_builder.build_all_views`)

Each view UNIONs per-vendor branches using `netops.view_recipes`
declarations.  State tokens canonicalised via SQL CASE
(`"Estab"` / `"Established"` / `"0"` → single canonical form).

| View | Columns |
|---|---|
| `netops.v_bgp_neighbors_auto` | `device`, `neighbor_ip`, `neighbor_as`, `local_as`, `router_id`, `state`, `uptime`, `snapshot_id` |
| `netops.v_ospf_neighbors_auto` | `device`, `neighbor_id`, `neighbor_ip`, `interface`, `area`, `state`, `dead_time`, `snapshot_id` |
| `netops.v_l2_links_auto` | `source_device`, `source_interface`, `destination_device`, `destination_interface`, `discovery_protocol`, `link_status`, `snapshot_id` |

## Base tables

| Table | Columns |
|---|---|
| `netops.devices` | `hostname`, `ip_address`, `platform`, `site`, `role`, `vendor`, `model`, `os_version`, `environment`, `last_seen`, `metadata` |
| `netops.parsed_outputs` | `device_name`, `command`, `parsed_data` (JSON), `snapshot_id`, `raw_output`, `raw_output_hash`, `ingested_at` |
| `netops.raw_output_store` | `device_name`, `command`, `raw_output`, `snapshot_id`, `updated_at` (one row per `(device, command)`; overwrites per snapshot) |
| `netops.topology_links` | `link_id`, `source_device`, `source_interface`, `destination_device`, `destination_interface`, `discovery_protocol`, `link_type`, `link_status`, `link_speed`, `first_seen`, `last_seen`, `last_verified`, `status_changes`, `snapshot_id`, `platform` |
| `netops.commands` | `platform`, `command`, `safe_command`, `parser_type`, `parser_path`, `blacklisted`, `pipe_allowed`, `backup_only`, `synced_at` — the *command registry* seeded by `sync_commands` from ntc-templates + custom + user YAML |

### `netops.devices.metadata` — JSON bag

Carries everything from `hosts.yaml:data.*` that doesn't get a
dedicated column:

```json
{
  "groups":  ["core_routers"],
  "aliases": ["core-router-1", "R3-router"],
  "<any other data.* key>": "..."
}
```

Use `metadata::JSON->>'$.key'` or `metadata LIKE '%substr%'` for
lookups.  Natural-language alias resolution uses this:

```sql
SELECT hostname FROM netops.devices WHERE metadata LIKE '%core-router-1%';
```

## Common query patterns

```sql
-- All devices, grouped by role
SELECT role, COUNT(*) FROM netops.devices GROUP BY 1 ORDER BY 2 DESC;

-- Core routers (role comes from hosts.yaml:data.role)
SELECT hostname, ip_address, model FROM netops.devices WHERE role='core';

-- BGP state across the fleet (vendor-normalised)
SELECT device, neighbor_ip, neighbor_as, state
FROM netops.v_bgp_neighbors_auto
ORDER BY device, neighbor_ip;

-- OSPF adjacencies by area
SELECT area, device, neighbor_id, state
FROM netops.v_ospf_neighbors_auto
ORDER BY area, device;

-- L2 topology (dedup'd — one row per physical link after
-- hostname_registry canonicalisation)
SELECT source_device, source_interface, destination_device, destination_interface,
       discovery_protocol
FROM netops.v_l2_links_auto
ORDER BY source_device, source_interface;

-- Last snapshot per device (snapshot_id format: snap_YYYYMMDD_HHMMSS_xxxxxx)
SELECT device_name, MAX(snapshot_id) AS latest
FROM netops.parsed_outputs GROUP BY 1 ORDER BY 1;
```

## Raw fallback — when no parser / view has the data

```sql
-- For commands that collected but didn't parse (e.g. an intent the
-- user has not yet /learn_cmd'd):
SELECT device_name, command, raw_output FROM netops.raw_output_store
WHERE command = 'show spanning-tree'
  AND device_name NOT IN (
    SELECT device_name FROM netops.parsed_outputs
    WHERE command = 'show spanning-tree' AND parsed_data IS NOT NULL
  );
```

`.olav/config/unsupported.json` (populated at each `/netops_init` by
the parse-coverage classifier) lists every `(platform, command)` pair
that collected but didn't parse — surface these to the user with a
`/learn_cmd` suggestion.

## What's *not* in the schema anymore

These lived in earlier rounds and are **deleted** — do not write SQL
referencing them:

* `netops.bgp_sessions` / `netops.ospf_adjacencies` — ARCH-24 L3
  materialised tables, removed Round 70.  Use the views above.
* `view_recipes_seed.yaml` monolith — split into per-protocol files
  under `olav-netops/.olav/workspace/topology/recipes/builtin/`.
* `auto_learn.py` batch learner — removed v0.21.0.  Invoke
  `netops/learner` (`learn_commands`) via the `/learn_cmd` skill for
  interactive parser learning.
