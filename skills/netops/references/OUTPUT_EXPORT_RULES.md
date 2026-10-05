# Output export rules — subagent results

When a subagent returns a Mermaid diagram, simulation result, or analysis
report, the orchestrator **always** persists the artifact via
`format_and_export` before replying to the user.

## File-name conventions

| Subagent output type | Save to | Format |
|---|---|---|
| Mermaid topology | `exports/topology_YYYYMMDD.mmd` | `.mmd` (raw Mermaid, no code fences) |
| Simulation result | `exports/simulations/sim_<name>_YYYYMMDD.md` | Markdown |
| Analysis report | `exports/reports/<name>_YYYYMMDD.md` | Markdown |
| Drift report | `exports/drift/drift_<snap1>_vs_<snap2>_YYYYMMDD.md` | Markdown |

## Rules

1. ALWAYS call `format_and_export` — never drop the subagent's artifact
2. Use descriptive filenames — include scenario / snapshot IDs
3. Tell the user the saved path in the final reply
4. For `.mmd` files, emit raw Mermaid without ` ```mermaid` fences (the
   preview renderer adds them)
5. If the artifact is large (> 200 lines), consider splitting into a
   summary (inline to user) + full report (exports/)
