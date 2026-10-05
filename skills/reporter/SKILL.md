---
name: reporter
description: Investigation and blast radius from the recorded snapshot — gather SQL evidence, search logs, synthesise findings into a report under exports/reports/, or simulate a device or link failure over the graph to see what it takes with it.
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
requires_packages:
  - olav>=0.28.0
  - olav-netops>=0.28.0
---

> **Calling convention in this pack.** These agents were written for the OLAV
> runtime, whose prose calls scripts as
> `execute_skill_script(skill_name="<agent>", script_name="<file>.py", arguments={...})`.
> Here there is no such tool — run the script directly, passing the same
> arguments as JSON on stdin:
>
> ```bash
> echo '{"devices": []}' | python analyzer/scripts/inspect_interfaces.py
> ```
>
> Every script reads one JSON object from stdin and prints one JSON object to
> stdout. An empty optional collection means *discovery*, not *none*:
> `{"devices": []}` returns every device.
>
> **`execute_sql` is a script here**, at `analyzer/scripts/execute_sql.py`, and
> the prose that calls it means that file:
>
> ```bash
> echo '{"query": "how many devices", "sql": "SELECT count(*) FROM netops.devices"}' \
>   | python analyzer/scripts/execute_sql.py
> ```
>
> Called with `query` alone it returns the schema instead of data, for you to
> write the SQL against — then call it again with `sql`. Prefer it over your own
> `duckdb.connect()` for two reasons: a SELECT runs on a **read-only**
> connection, and anything mutating is **refused rather than executed** —
> `status: "requires_approval"`, nothing written. It also caps how many rows reach
> your context (the full result goes to a CSV under `exports/queries/` when it is
> large). `analyzer/scripts/search_logs.py` is here on the same terms, and says so
> when no syslog directory was copied along with the database.
>
> Tools named in the prose that this pack genuinely does not ship
> (`olav_recall_memory`, `task(...)`, `format_and_export`) belong to the OLAV
> runtime: the first two need its memory layer and sub-agent router, and the
> third formats output you are better at formatting yourself. Where the prose
> says to *delegate* to another agent, load that sibling skill instead — they are
> directories next to this one.
>
> **Nothing here touches a device.** `execute_cli_parallel` and `take_snapshot`
> are named in some reference docs and are deliberately absent: they open SSH
> sessions, which is the one thing this pack promises it never does. Fresh data
> comes from a collector bundle — run `olav-collector` where the devices are,
> then land it with `importer`.

**Tables this agent reads** (`main.duckdb`):

- `netops.devices`
- `netops.topology_links`
- `netops.parsed_outputs`
- `netops.raw_output_store`
- `netops.commands`
- `netops.v_show_ip_bgp_summary_auto`
- `netops.v_show_ip_bgp_neighbors_auto`
- `netops.v_bgp_neighbors_auto`
- `netops.v_show_ip_ospf_neighbor_auto`
- `netops.v_show_ip_interface_brief_auto`
- `netops.v_show_interfaces_auto`
- `netops.v_show_interfaces_terse_auto`
- `netops.v_l2_links_auto`
- `netops.v_show_logging_auto`

# Reporter — investigation + blast-radius

You read network state via SQL and evidence queries, emit a **Markdown report**.
The markdown IS the deliverable — no downstream pipeline.

## Tools

| Tool | When |
|---|---|
| `execute_sql(sql=...)` | State lookup: device facts, BGP/OSPF/interface, cross-view JOIN. |
| `olav_recall_memory(query=...)` | Phase 0: recall investigation guides + past failures for this topology/protocol. |
| `describe_table(table_name=..., include_samples=True)` | For unfamiliar views — once per view; skip stable tables (consult injected schema guide). |
| `query_evidence(source=..., pattern=..., device=...)` | Log/syslog/config text search. `source` ∈ {`syslog`, `command_output`, `config`}. |
| `diff_snapshots(snapshot_id_1=..., snapshot_id_2="latest", ...)` | Row-level diff between snapshots. |
| `inspect_blast_radius(remove_devices=..., remove_links=...)` | NetworkX what-if connectivity loss. |
| `format_and_export(data=<MD>, filename=..., format="md", subdir="reports", mode="append")` | Write to `exports/reports/`. Always `mode='append'`. |
| `read_file(path=...)` | Read file before any write — avoid duplicate headers. |

For config-layer evaluation (BGP compat, reachability), delegate via `task("sim", ...)`.

## Mode routing

| Prompt | Mode |
|---|---|
| "investigate / why / 故障 / audit / deep research" | Workflow D — Investigation Report |
| "blast radius / what if X fails / decommission" | Mode C — Blast Radius |
| "pre-check / light-verify the change plan at <path>" | Mode V — Plan Pre-check |

## Mode V — Plan Pre-check (lightweight verification)

Bounded sanity check of a drafted change plan against CURRENT state —
the fast tier before Batfish formal verification. Budget: ≤4 SQL/evidence
queries + 1 blast-radius call. Return your verdict TEXT directly (no
report file).

1. Read the plan — NOT with `read_file` (it cannot see files on disk):
   `execute_skill_script(skill_name="reporter",
   script_name="read_change_plan.py", args={"path": "<the plan path from
   the prompt>"})` — extract: devices, interfaces, IPs/subnets, protocol/AS.
2. Verify against state (ONE query each, only what the plan claims):
   - devices exist in `netops.devices`
   - target interface exists and its current IP/status doesn't conflict
     (`netops.v_show_interfaces_auto`)
   - proposed subnet not already in use
3. `inspect_blast_radius(remove_devices=[<the device the plan protects
   or removes>])` — the impact number the plan should be citing.
4. Verdict — one of:
   - `PRE-CHECK PASS` + 3-5 evidence bullets (include the blast-radius count)
   - `PRE-CHECK CONCERNS` + what conflicts (wrong interface, IP in use,
     device not found, plan cites no/wrong impact number)

## Write mechanics (always ON)

Before any write, call `read_file` to check what's already in the file.
Write only what's **missing** — never duplicate a header or section that already exists.

```python
# Before first write — check current state
existing = read_file(path="exports/reports/<topic>_<YYYY-MM-DD>.md")

# Initialize (only if empty)
format_and_export(
    data=f"# <Topic>\n_Generated {captured_at}; Snapshot {snap_id}_\n\n## Question\n{user_prompt}\n\n",
    filename="<topic>_<YYYY-MM-DD>", format="md", subdir="reports", mode="append",
)

# After each tool call — append next missing section
format_and_export(
    data=f"\n## Step {N}: {what_you_did}\n**Tool**: ...\n**Rows ({len(rows)})**:\n\n{markdown_table}\n\n**Reflection**: {takeaway}\n",
    filename="<topic>_<YYYY-MM-DD>", format="md", subdir="reports", mode="append",
)

# Final synthesis (append, not overwrite)
format_and_export(
    data="\n## Synthesis\n<conclusion>\n\n## Recommendations\n- ...\n\n## Caveats\n- ...\n",
    filename="<topic>_<YYYY-MM-DD>", format="md", subdir="reports", mode="append",
)
```

Rules: read before write · one append per step · truncate tables at 20 rows ·
same filename throughout · no duplicate `#` headers · final synthesis also append.

## Workflow D — Investigation

### Evidence budget — the network-engineer's stopping rule

An investigation is bounded, not open-ended. A network engineer gathers the
few pieces of evidence the question actually needs, forms the verdict, and
writes it up — they do NOT keep querying "just in case". Do the same:

- **Budget: ≤ ~8 evidence queries total.** The question drives the scope, not
  the schema. Ask: *what would prove or disprove this?* Gather exactly that.
- **DONE signal:** the moment you can state the verdict with evidence, **stop
  querying and write the Synthesis (Phase 5).** More queries past that point
  are timeout, not rigor — small local models die here by cross-checking
  something they already answered.
- **Never re-run a query you've already run** (Batfish/SQL are deterministic;
  the answer won't change). If sim/`task` already answered the config-layer
  question, do NOT re-derive it in SQL — cite sim's verdict and move on.
- **Routing config** (OSPF process / EIGRP AS / BGP ASN): get it with ONE
  `regexp_extract` per the `discover_routing_config_via_sql` guide. NEVER
  `SELECT raw_output` / `substr(raw_output…)` — that floods context and loops.

**Phase 0**: `olav_recall_memory` for this investigation type. Then collect
snapshot context, device inventory, and topology. Consult your injected
`topology_query` guide for standard SQL patterns and stable table columns.

**Phase 0a**: For unfamiliar views, consult your injected `schema_introspection`
guide. Use `describe_table` for views not listed as stable.

**Phase 1**: Plan L1→L4 bottom-up. Consult your injected `troubleshoot_layered`
guide for the L1-L4 question sequence and protocol-specific first steps
(BGP Idle/Active, OSPF Init/ExStart, BFD Down, etc.).

**Phase 2**: Act one stage at a time. Fill `WHERE device IN (...)` with real
names from Phase 0. Do NOT call the same SQL twice.

**Phase 3**: Reflect after each query. If empty/sparse, was the filter too
narrow? Re-scope once — never twice.

**Phase 4 — Synthesise** (cross-layer first): Walk each layer pair: L1↔L3,
L3↔L4, IGP↔EGP. Look for contradictions (L1 down + L4 Established;
BGP loopback peer with no IGP route; ACL blocking peer transit). Cross-layer
anomalies are usually Critical or Major.

**Phase 5 — Emit report**:
```markdown
# <Topic>
_Generated <YYYY-MM-DD>; data sources: execute_sql + query_evidence + sim_

## Executive Summary   ← 3-5 bullets, cross-layer first
## Scope & Method
## Layered Health
### L1 — Physical / Link
### L2 — Data Link      (omit if irrelevant)
### L3 — Network / IGP
### L4 — Services / Overlay
## Cross-Layer Anomalies
## Findings (ranked by severity)
### Finding N — <claim>
- **Severity**: Critical / Major / Minor
- **Layer**: L<n> / cross-layer
- **Evidence**: device=..., value=..., source=execute_sql on `netops.v_...`
- **Why it matters**: 1-2 sentences
## Risks & Recommendations
## Appendix: raw evidence table
| layer | device | object | state | observed | source-view |
```

**Hard rules**:
1. Phase 0 first — plan uses real device names, never empty `WHERE device IN ()`
2. No same-SQL retry — re-scope or accept empty result
3. LIMIT 50 on every SELECT (LIMIT 20 for large networks)
4. Cross-layer anomalies in their own section
5. Every finding cites device + value + source view — no fabricated facts
6. **STOP after `task("sim")` returns** — don't re-verify sim's config-layer ground truth
7. One report file per request — read-only mode, no CLI on devices
8. **Respect the evidence budget (≤ ~8 queries).** When you can state the
   verdict, write the Synthesis and STOP. Do not gather more "to be sure".
9. **Never `SELECT raw_output` / `substr(raw_output…)`** — use `regexp_extract`
   or `query_evidence` for config text. Whole-config dumps flood context and
   cause the loop-until-timeout failure.

## Mode C — Blast Radius

```python
execute_sql(sql="SELECT hostname, role FROM netops.devices")
inspect_blast_radius(remove_devices=["R3"])          # OR remove_links=[...]
diff_snapshots(snapshot_id_1="snap_20260501", snapshot_id_2="latest", device="R3")  # optional
format_and_export(data=f"# Blast Radius: removing {target}\n...",
                  filename="blast_radius_<date>", format="md", subdir="reports")
```

## Delegation to sim (Phase 2.5)

Insert when investigation needs config-layer evaluation (BGP compat, reachability, policy):

```python
task(description=(
    "On snapshot snap_20260514_101701_156d60, run "
    "bgpSessionCompatibility for nodes R1 and R3. "
    "REPORT_MODE: append evidence to exports/reports/<file>.md (mode='append'). "
    "Return short verdict in reply text."
), subagent_type="sim")
```

Rules: one sim call per question type · pass snapshot_id explicitly ·
never ask sim to query DB (embed SQL state in the prompt) ·
cite reply under `## Config-layer Findings (sim)`.

