# Service deployment workflow

Load when the user asks to deploy a new service (NetBox, Grafana,
Prometheus, Zabbix, GitLab, etc.) or to create a new interaction skill
that talks to such a service.

## Required-info check (before any action)

| What you need | How to handle |
|---|---|
| Admin password / secret key | ❌ MUST ask the user — never guess or use `changeme` |
| Port mapping | Ask if user didn't specify; suggest service default in brackets |
| Base DN / org / bucket name | Ask if service uses directories or namespaces |
| Data persistence path | Default: `.olav/services/<name>/data` — confirm if the service is stateful |
| External hostname / TLS | Ask if the service will be user-facing or accessed remotely |

If any of the above is missing, stop and use this template:

```
To deploy [service], I need a few details:
1. **Admin password** — (no default, must be set)
2. **Port** — [default: XXXX] OK to proceed, or specify another?
3. **[Other param]** — [reason]

Please confirm these and I'll proceed.
```

If the user provides everything upfront → execute immediately, no
extra confirmation.

## Mandatory deployment workflow

1. **Research first** — `web_search` for the official docker-compose
   for the service.  Never invent image names or config formats.

2. **Write ALL files first** via `write_workspace_file` — every file
   that the compose volume-mounts references must exist BEFORE the
   container starts.

   ```
   write_workspace_file(path=".olav/services/<name>/docker-compose.yml", content="...")
   write_workspace_file(path=".olav/services/<name>/env/<name>.env", content="KEY=VALUE\n...")
   write_workspace_file(path=".olav/services/<name>/configuration/configuration.py", content="...")
   ```

   * Paths relative to project root, under `.olav/services/<name>/`
   * File paths must EXACTLY match what the compose volume mounts
     reference.  If compose has `volumes: [./configuration:/etc/netbox/config]`,
     write to `.olav/services/<name>/configuration/`.

3. **Start and verify** with `deploy_service`:

   ```python
   # Lightweight services (Prometheus, Grafana): default 300s is fine.
   deploy_service(name="<name>", health_url="http://localhost:<port>/")

   # DB-migration services (NetBox, GitLab, Zabbix): first start runs
   # migrations → use 600s.
   deploy_service(name="<name>", health_url="http://localhost:<port>/", health_timeout=600)
   ```

   Returns `{"success": true, "containers": [...]}` on success, or
   `{"success": false, "logs": "...", "hint": "..."}` on failure —
   read `logs` to diagnose.

4. **On failure**: read the `logs` field, fix the issue (missing
   file? wrong env var?), update via `write_workspace_file`, then call
   `deploy_service` again.

## Sandbox security

When writing tool code that calls `execute_in_sandbox`, set
`network_isolation` based on whether the sandboxed code needs
external network access:

| Scenario | `network_isolation` | Example agents |
|---|---|---|
| Pure computation (routing simulation, graph analysis, diff) | `True` | analyze, diff |
| External API needed (push config to clab, httpx REST calls) | `False` | lab |

Default to `True` for any new tool that doesn't explicitly require
network — `unshare --net` isolation makes accidental or malicious
external operations fail with Connection refused.
