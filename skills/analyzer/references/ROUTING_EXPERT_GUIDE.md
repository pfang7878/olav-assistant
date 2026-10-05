# Routing Expert Reference

This file's content has been split into intent-keyed memory
guides (Patch J, 2026-05-07).  AutoRecall now surfaces the
relevant section automatically when your query matches the
intent.

| Topic | Memory guide intent |
|---|---|
| BGP neighbor states + last-reset reasons + 10-step path priority | `bgp_path_selection_and_state_machine` |
| BGP best-path Python algorithm (`bgp_best_path()` for sim) | `bgp_best_path_python_algorithm` |
| OSPF Dijkstra with cost weights, graph construction | `ospf_shortest_path_dijkstra` |
| ECMP detection across all node pairs | `ecmp_detection` |
| Convergence-time estimation (BFD/hello/SPF/FIB breakdown) | `convergence_time_estimation` |
| Path-shift analysis between two snapshots | `path_shift_analysis_between_snapshots` |

Plus the long-form NetworkModel patterns (A/B/C — DuckDB +
networkx, LanceDB semantic search, route-map evaluation) — those
remain code-recipe references, not directive guides; load on
demand if a sim needs them.

For any BGP/OSPF/ECMP/convergence/path-shift question, ask the
agent in natural language and AutoRecall will inject the
appropriate guide into `<relevant-memories>`.

To verify a guide is wired:

```bash
olav kb search "BGP best path"           # → bgp_best_path_python_algorithm
olav kb search "ospf shortest path"      # → ospf_shortest_path_dijkstra
olav kb search "ECMP"                    # → ecmp_detection
olav kb search "convergence time"        # → convergence_time_estimation
olav kb search "path shift"              # → path_shift_analysis_between_snapshots
olav kb search "BGP states"              # → bgp_path_selection_and_state_machine
```

History: this file was previously a 368-line monolith
(~12 KB) loaded `on_intent` for any BGP-related query.  Most
queries needed only one section — the rest wasted the recall
budget.  Splitting into 6 small guides (≤ 2 KB each) improves
AutoRecall precision: the right section surfaces, others stay
silent.
