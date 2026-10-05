# Tool partitioning reference

Load this when you need to pick the right tool for a task or are
unsure why `read_file` returned empty after `write_workspace_file`
appeared to succeed.

## Two distinct toolboxes

The ops agent has tools from **two non-overlapping namespaces** —
confusing them is the most common failure mode.

### 🗂️ Scratchpad (deepagents built-in, in-memory only)

`write_file` · `read_file` · `edit_file` · `glob` · `grep` · `execute`

Operate on an **in-memory virtual filesystem** (StateBackend).  Useful
for drafting intermediate plans, scratch calculations, or holding
chunks of text the model wants to revisit later in the same turn.

⚠️ **Files written with `write_file` do NOT exist on real disk.**
They will not be visible to docker, shell commands, or the OS, and
they vanish at session end.

### 🔧 Domain (Olav infrastructure, real persistent ops)

`execute_cli_parallel` · `write_workspace_file` · `execute_sql` · `run_shell`
· `deploy_service` · `register_service` · `search_commands` ·
`sync_inventory`

Perform **real, persistent operations** on actual infrastructure —
write real files, query the real DuckDB, run commands on real network
devices over SSH, talk to real docker.

## Rule of thumb

Any operation with lasting effect (file creation, device config, DB
query, container start) requires a Domain Tool.  Scratchpad tools are
for thinking, not doing.

## Required-tool quick table

| Task intent | USE THIS | NEVER use these |
|---|---|---|
| Deploy / install / set up / stand up any service | `write_workspace_file` (files) → `deploy_service` (start) | `run_python_code`, `execute`, `write_file` |
| Ad-hoc docker / shell, check logs, inspect state | `run_shell` | `run_python_code`, `execute` |
| Create / write any project file | `write_workspace_file` | `write_file`, `run_python_code` |
| Read a file you just wrote | `run_shell("cat .olav/services/<name>/file")` | `read_file` (wrong path resolution) |
| Network SQL queries | `execute_sql` | `run_python_code` |
| Device CLI | `execute_cli_parallel` | `run_python_code` |

## `run_shell` — common patterns

```
run_shell("docker compose ps", cwd=".olav/services/netbox")
run_shell("docker compose up -d", cwd=".olav/services/netbox")
run_shell("docker compose logs --tail 50 netbox", cwd=".olav/services/netbox")
run_shell("curl -s http://localhost:8000/api/", timeout=10)
```

`cwd` is relative to project root — e.g. `.olav/services/netbox`
resolves to `<project_root>/.olav/services/netbox`.  Never use `/tmp/`
for service files; always under `.olav/services/<name>/`.

## `write_workspace_file` — common patterns

```
write_workspace_file(path=".olav/services/netbox/docker-compose.yml", content="...")
write_workspace_file(path=".olav/services/netbox/env/netbox.env", content="KEY=VALUE\n...")
```

Project root is resolved from `OLAV_HOME` env var or detected
automatically at runtime; the tool enforces this so paths are always
relative to the project root regardless of where you started from.
