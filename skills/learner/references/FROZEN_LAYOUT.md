# Frozen Artifact Layout

Where learned parsers land on disk after `learn_commands()` succeeds.

## Directory structure

```
.olav/templates/
├── <platform>/                          # TextFSM templates
│   └── <cmd_safe>.textfsm
├── parsers/                             # Python parsers
│   ├── <platform>/
│   │   └── <cmd_safe>.py
│   └── _quarantine/                     # single-sample learns
│       └── <platform>/
│           └── <cmd_safe>.py
└── _failed_learn.json                   # failure cache (R71)
```

Where `<cmd_safe>` is the command string with whitespace and special
chars replaced by underscores (`show ip interface brief` →
`show_ip_interface_brief`).

## Platform directory names

Platform strings come from Nornir `host.platform` and match Netmiko
convention: `cisco_ios`, `cisco_xe`, `cisco_nxos`, `cisco_xr`,
`juniper_junos`, `arista_eos`, `nokia_sros`, `huawei_vrp`, etc.

## Frozen file header

Every frozen parser starts with a **header stamp** containing:

- `# OLAV command-learner v1.0`
- `# platform: <plat>`
- `# command: <cmd>`
- `# learned_at: <ISO-8601 UTC>`
- `# samples_hash: <sha256 of sorted samples>`
- `# contract_version: 1` (`LEARNER_CONTRACT_VERSION`)

The header lets later code identify provenance and detect stale
artifacts (bumping `LEARNER_CONTRACT_VERSION` invalidates all
frozen parsers at once, forcing re-learn).

## Quarantine semantics

Single-sample Python parsers go to `parsers/_quarantine/` not
`parsers/` main. Rationale:

- One sample gives no variance signal → parser likely overfits to
  that device's specific output
- Quarantined parsers are **not loaded** by `textfsm_parse.parse_output`
  by default
- When a second device with the same (platform, command) captures
  output, `learn_commands()` re-learns with 2 samples and *promotes*
  the parser out of quarantine

(Promotion is a follow-up, not in R71 scope.)

## Failure cache (`_failed_learn.json`)

When `learn_commands` gives up on a (platform, command), it writes:

```json
{
  "juniper_junos/show bgp summary": {
    "attempted_at": "2026-04-22T12:00:00Z",
    "samples_hash": "abc123...",
    "last_error": "all 2 retries produced parsers that failed sample coverage check",
    "retry_after_seconds": 604800
  }
}
```

- On next `learn_commands` call, entries younger than `retry_after_seconds` are **skipped** without LLM call
- If `samples_hash` differs from the current input (new samples added), the cache entry is **ignored** → re-learn
- `--force-relearn` flag bypasses the cache
- Bumping `LEARNER_CONTRACT_VERSION` in code invalidates the cache

TTL default: 7 days. Rationale: long enough to avoid re-paying the LLM cost on daily re-runs; short enough that LLM improvements eventually get retried.

## Runtime lookup

`textfsm_parse.parse_output(platform, command, raw)` unchanged priority
order:

1. Main-tree PaC Python parser: `parsers/<platform>/<cmd>.py`
2. Custom TextFSM: `<platform>/<cmd>.textfsm`
3. Bundled ntc-templates: `ntc_templates/templates/<platform>_<cmd>.textfsm`

Quarantine is **not** in the runtime lookup.
