# olav-skills

The OLAV expert workbench for Claude Code: give it a pile of dead CLI output and
it returns a queryable network model — and when it meets a command it has never
seen, it learns to parse it.

**Generated. Do not edit by hand.** Rebuilt from the olav repo with
`uv run python scripts/gen_olav_skills.py --write`.

## What it does

You have CLI output from a network — a collector bundle, a rancid backup, a
directory of `show` output someone emailed you. On its own that is text. This pack
turns it into a model you can ask questions of, and then into something a person
keeps.

**Land a snapshot and query it.**

```bash
echo '{"path": "/path/to/bundle"}'  | python importer/scripts/ingest_snapshot.py
echo '{"query": "which devices are in the snapshot"}' | python analyzer/scripts/execute_sql.py
echo '{"sql": "SELECT hostname, vendor, model FROM netops.devices"}' \
  | python analyzer/scripts/execute_sql.py
```

Called with `query` alone, `execute_sql` returns the schema for you to write SQL
against; SELECTs run read-only and anything mutating is refused rather than run.
`inspect_devices` with `{"devices": []}` is the same idea for facts — an empty
collection means *show me everything*, not *nothing*.

**See the topology, without asking for it twice.** The views are built when the
data lands, so this is a query, not an ETL run:

```bash
echo '{}' | python topology/scripts/query_topology.py
```

It returns typed BGP / OSPF / L2 adjacency for the snapshot. BGP, OSPF, CDP/LLDP
and L2 are built in. For a protocol that has no recipe,
`prepare_recipe` gathers the evidence and hands you the drafting job — the YAML
you write is frozen and reused, so the next snapshot needs no drafting.

**Teach it a command it cannot parse.** `ntc-templates` covers a lot and not
everything. `learner` closes that gap without anyone editing the platform:
`prepare_learn` analyses the raw samples and returns the drafting prompt, you
write the TextFSM or Python parser, `finish_learn` validates it in a sandbox and
freezes it. Every later import picks it up automatically.

**Produce what a person actually asked for.**

| You want | Skill | It writes |
|---|---|---|
| "why is this down / audit this network" | `reporter` | an evidence-backed report under `exports/reports/` |
| "plan this change" | `analyzer` | a per-device change plan + rollback + post-checks |
| "draw the topology" | `writer` | draw.io XML or Mermaid from the tables |
| "what breaks if this dies" | `reporter` | blast radius over the graph |
| "diff these two snapshots" | `netops` | config and snapshot diffs |

Each is a skill directory. Claude Code loads the one whose description matches
what you asked, reads its prose, and runs the scripts underneath — so the useful
unit is a request in your own words, not a command you have to remember.

## The agents

| Agent | What it does | Scripts |
|---|---|---|
| `analyzer` | Change-plan drafter — gather device facts via SQL, write a vendor-specific change plan markdown (CLI per device + rollback + post-checks + risks) to exports/change_plans/. | 7 |
| `importer` | Offline snapshot ingest — drops a bundle / rancid backup / vendor dump in and lands it in raw_output_store + structured views. | 5 |
| `learner` | Parser learning for CLI output ntc-templates cannot parse. `prepare_learn` analyses the samples and returns the drafting prompt; you write the TextFSM or Python parser; `finish_learn` validates it in a sandbox and freezes it, so every later import picks it up. | 2 |
| `netops` | Router and shared helpers for this pack — says which sibling skill answers what (topology, analyzer, reporter, importer, learner, writer) and holds the read-only cross-cutting scripts: config and snapshot diffs, command search, change-plan reading, blast radius. Reads a recorded snapshot; no device access. | 5 |
| `reporter` | Investigation and blast radius from the recorded snapshot — gather SQL evidence, search logs, synthesise findings into a report under exports/reports/, or simulate a device or link failure over the graph to see what it takes with it. | 4 |
| `topology` | Topology from the recorded snapshot — BGP, OSPF, CDP/LLDP and L2 adjacency as queryable views. For a protocol with no builtin recipe, `prepare_recipe` gathers the evidence and hands you the drafting job; the YAML you write is frozen and reused. | 5 |
| `writer` | Turns the model into something a person keeps — draws the topology to draw.io XML or Mermaid from a scoped adjacency query, and polishes an existing report under exports/ in place. | 4 |

32 scripts across 7 agents. `netops` is the router: it says
which of the others answers what.

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install ./runtime
cp -r skills/* ~/.claude/skills/          # or into <project>/.claude/skills/
```

That is the whole install. **This pack depends on no OLAV distribution**: it
carries the library code it needs in `runtime/` — 44 modules, copied from the olav
repo and generated, never hand-edited — and `runtime/pyproject.toml` declares only
third-party packages. Nothing is fetched from PyPI's `olav`, so a refactor over
there cannot change what a published pack does.

8 third-party packages, ~120 MB installed. Three of them fail *silently*
if you drop them, which is why they are pinned rather than suggested:

* **`ntc-templates`** — without it nothing parses. Raw output lands and zero rows
  become structured, which reads like an empty network rather than a missing
  library.
* **`netutils`** — without it interface names are not canonicalised, so `Gi1` and
  `GigabitEthernet1` become two ports on the same link.
* **`netconan`** — without it **redaction is skipped and credentials land on disk
  in plaintext**. If you deliberately want no redaction, say so in
  `.olav/config/api.json` (`"redaction": {"enabled": false}`) rather than getting
  it by omission.

**Use a virtualenv.** `runtime/` provides the import names `olav` and
`olav_netops`, so it must not share an environment with a platform install of
OLAV itself.

Verified rather than assumed, in an environment with no OLAV package present: all
36 files import, a collector bundle lands (3 devices, 29 commands, 28 parsed, 13
views, 4 topology links), and one real call per agent — devices, SQL, BGP topology,
evidence search, a draw.io diagram, a config diff, bundle validation — returns what
a full platform install returns.

### If you already run the OLAV platform

Then you do not need `runtime/`: the same scripts work against the installed
packages.

```bash
pip install 'olav>=0.28.0' 'olav-netops>=0.28.0'
```

## Data — and the loop that makes it better over time

These agents read a DuckDB database; they do not collect. Two ways to get one:

* **Copy one** — `main.duckdb` from an OLAV host into `.olav/databases/` of the
  directory you work in. Views travel inside the file.
* **Import a bundle** — collect with `olav-collector` (netmiko + pyyaml, runs on
  a jump host next to the devices), then `importer` lands it.

The second is the normal path, and it is a loop rather than a one-way import.

**Point `path` at the directory that holds `manifest.yaml`** — the collector writes
one bundle per run at `output/<date>/<time>/`, so that is the level, not its parent
and not `devices/` inside it. A `.zip` of the same directory works too, which is
usually how it travels. Getting the level wrong fails loudly and says which file it
looked for:

```
ValueError: bundle validation failed: ['manifest.yaml missing under <path>']
```

**You do not have to configure anything.** Drop the bundle wherever you are working
and ask for it in words — that is the point of a skill: Claude reads your sentence,
picks `importer`, and runs the script with the path you named.

> *"import the bundle in ./captures/041302"*

The database is created under the *project root*, and every later query resolves the
same way, so a whole engagement can live in one directory.

**Which directory is the project root is the one rule worth knowing**, because it is
often not the one you are standing in: the project root is
`OLAV_HOME` if set, else **the nearest ancestor of your working directory that
contains `.olav/`**, else the working directory. That middle clause is the trap — if
`~/.olav` exists, and it does on any machine that has run OLAV once, then every
directory under `~` resolves to `~` and your new project quietly shares that
database. Writing this section, a bundle imported from a fresh `clean/` directory
landed two levels up, in a `.olav/` left behind by an unrelated experiment — the
working directory got nothing at all. So the import tells you which database it
wrote, every time:

```json
"db_path": "/home/you/work/acme/.olav/databases/main.duckdb"
```

**To make the directory you are in the project**, give it its own marker before you
import — one command, and everything then stays local to it:

```bash
mkdir -p .olav                       # this directory is now the project root
# or, equivalently, for one session:
export OLAV_HOME=$PWD                # the project root, never its .olav
```

Measured while writing this, on a machine that had run OLAV before: importing from a
fresh directory with neither of those put the database in `~/.olav/databases/`, not
in the directory. Both forms above put it in `./.olav/databases/main.duckdb`, and
`db_path` in the import result is how you confirm it rather than assume it.

```bash
# 1. land it — everything in the bundle, parsed or not
echo '{"path": "~/captures/acme/041302"}' | python importer/scripts/ingest_snapshot.py

# devices 2 | landed 166 | parsed 7 | unparsed 159
# learn queue:
#   recipe  juniper_junos  show bgp summary        x1
#   recipe  cisco_ios      show ip ospf neighbor   x1
#   -       cisco_ios      show access-list        x1

# 2. ask questions of what did parse
echo '{"query": "which devices are in the snapshot"}' | python analyzer/scripts/execute_sql.py
echo '{}' | python topology/scripts/query_topology.py

# 3. close a gap from the queue: prepare, write the template, freeze it
echo '{"platform":"cisco_ios","command":"show access-list",
       "samples":[{"device":"R2","raw_output":"<raw text from raw_output_store>"}]}' \
  | python learner/scripts/prepare_learn.py
echo '{"platform":"cisco_ios","command":"show access-list",
       "parser_response":"# OLAV_DSL: textfsm\nValue ...", "samples":[...]}' \
  | python learner/scripts/finish_learn.py

# 4. re-run step 1. The same bundle now parses more.
```

**A low parse ratio on a first sweep is expected.** The collector's default is to
sweep its whole command library — `--task` narrows it, `--all-commands` widens it
again — because a site visit is expensive and disk is not, and everything lands in
`raw_output_store` whether a parser exists or not. So
a template you write today applies to output collected weeks ago — step 4 needs no
new collection, which is the reason the collector carries no parsers.

The queue is ordered: commands a builtin topology recipe declares come first,
because those become queryable views the moment they parse. `finish_learn` freezes
to `$OLAV_HOME/.olav/templates/<platform>/<command>.textfsm` and a TextFSM
template is live on the next parse — fitted to however many samples you gave it,
so pass every sample you have. (Only low-sample *Python* parsers are held back for
review.)

The database is resolved by `olav.core.config`, in this order: `OLAV_HOME` if
set, otherwise the nearest ancestor of the working directory that contains a
`.olav/` directory, otherwise the working directory itself.

**`OLAV_HOME` is the project root, not the `.olav` directory** — point it at
`/srv/site-a`, never at `/srv/site-a/.olav`. A wrong path does not raise:
scripts return an empty result, because a database that is not there reads as a
model with no devices in it. So if a query comes back empty, check the path
before you doubt the data:

```bash
python -c "from olav.core.config import MAIN_DB_PATH; print(MAIN_DB_PATH)"
```

## What this is not

It analyses a **snapshot**. There is no collection, no device access, no change
execution — it can tell you what to do and cannot do it. Verification is
structural (topology consistency, allocation conflicts), not behavioural: no
Batfish, no lab. A design that passes here is self-consistent, not proven to run.

## Agents referred to but not included

This wave is deliberately narrow. Where the prose points at an agent that
is not here, the capability exists in the full OLAV runtime — the pointer
is accurate, the agent is simply out of the box:

- `netops` mentions `admin`, `analyst`, `simulator`

## License

Business Source License 1.1 — see `LICENSE`, copied verbatim from the `olav`
repo, whose grant already covers agent skills. Non-production use is free;
production use is bounded by the Additional Use Grant in that file. Change
Date 2030-01-01, after which it becomes Apache 2.0.
