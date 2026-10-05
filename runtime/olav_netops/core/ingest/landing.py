"""End-to-end landing driver for a portable snapshot bundle.

Sequence (mirrors ``netops_init/run.py:_run_collection`` for the post-SSH
phase, but driven from disk instead of live netmiko sessions)::

  BundleReader.open(path)
    → for each CommandRecord:
        body = scrub(body) if not record.pre_scrubbed else body  # defense-in-depth
        parsed = textfsm_parse(platform, command, body)
        staging.append({snapshot_id, device_name, command,
                        raw_output, parsed_data, platform})
  → write staging.json to staging_dir
  → ensure netops migration applied (idempotent ALTER + CREATE)
  → IngestManager.bulk_load()           # raw_output_store + parsed_outputs
  → populate_devices(db_path, snapshot_id)
  → extract_lldp_topology(conn)
  → finalise_ingest(conn)               # v_*_auto views
  → INSERT netops.bundle_ingests
  → UPDATE raw_output_store rows for this snapshot: set bundle_id/sha256/ingested_via
  → AuditEventRecorder.record_run_*     # with collection_source

Returns an ``IngestResult`` summarising what landed.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from olav.core.db_write import open_write_connection
from olav.core.ingest.bundle_reader import BundleReader
from olav.core.ingest.validators import validate_bundle

logger = logging.getLogger(__name__)


# ── Public result type ────────────────────────────────────────────────


@dataclass(slots=True)
class IngestResult:
    """Summary returned by ``ingest_snapshot``."""

    bundle_id: str
    snapshot_id: str
    bundle_sha256: str
    collection_source: str
    hosts: int
    commands: int
    parser_fills: dict[str, int]
    audit_run_id: str | None = None
    # What did NOT make it into structured form, and why. Empty dict only when
    # the report could not be built at all.
    parse_report: dict[str, Any] = field(default_factory=dict)
    report_path: str | None = None


# ── Internal helpers ──────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_text_for_db(text: str) -> str:
    """Defense-in-depth scrub via netconan; fail-open."""
    try:
        from olav.core.redaction import scrub
        out, _ = scrub(text)
        return out
    except Exception:  # noqa: BLE001
        return text


def _parse_one(platform: str, command: str, body: str) -> tuple[str | None, str]:
    """Parse a single (platform, command, body).

    Returns ``(json_or_None, reason)``. The reason exists because this function
    used to collapse three very different outcomes into a bare ``None`` — an
    exception, an empty parse, and "no template for this platform/command" —
    and the caller counted only successes. On a 339-device bundle that silently
    discarded 6483 of 8761 command outputs with no record of which or why
    (dev_docs/116).

    ``no_rows`` covers both "template matched nothing" and "no template at all",
    because ``parse_output`` folds its three tiers into one ``None``; the two are
    separated afterwards against the ``netops.commands`` SSOT, which knows
    whether a parser is registered for that platform+command.
    """
    try:
        from olav_netops.tools.textfsm_parse import parse_output
        parsed = parse_output(platform, command, body)
    except Exception as exc:  # noqa: BLE001 — best-effort, but say what happened
        return None, f"parser_error:{type(exc).__name__}"
    if not parsed:
        return None, "no_rows"
    return json.dumps(parsed), "ok"


_REASON_LABELS = {
    "raw_only": "by design — command is registered raw_only, no parser expected",
    "no_parser_registered": "no parser registered for this platform+command",
    "parser_no_match": "parser exists but matched no rows (template/output mismatch)",
}


def _classify_unparsed(
    conn: Any,
    unparsed: dict[tuple[str, str], int],
    parse_errors: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    """Attach a reason to every unparsed (platform, command) tally.

    ``netops.commands`` is the SSOT populated by ``commands_sync``: it knows
    ``parser_type`` per platform+command, which is what separates "we chose not
    to parse this" (``raw_only`` — running-config and friends) from "nothing is
    registered" from "a parser ran and matched nothing". Falls back to the
    coarse label when the SSOT is unavailable rather than guessing.
    """
    ssot: dict[tuple[str, str], str] = {}
    try:
        for plat, cmd, ptype in conn.execute(
            "SELECT platform, command, parser_type FROM netops.commands"
        ).fetchall():
            ssot[(plat or "", cmd or "")] = (ptype or "")
    except Exception:  # noqa: BLE001 — SSOT not synced yet; degrade honestly
        ssot = {}

    out: list[dict[str, Any]] = []
    for (plat, cmd), count in unparsed.items():
        if (plat, cmd) in parse_errors:
            reason = parse_errors[(plat, cmd)]
            detail = "the parser raised — see logs for the traceback"
        elif not ssot:
            reason = "unclassified"
            detail = "netops.commands not populated — run `olav init` to sync the command SSOT"
        else:
            ptype = ssot.get((plat, cmd))
            if ptype is None:
                reason = "no_parser_registered"
            elif ptype == "raw_only":
                reason = "raw_only"
            else:
                reason = "parser_no_match"
            detail = _REASON_LABELS.get(reason, reason)
        out.append({
            "platform": plat,
            "command": cmd,
            "count": count,
            "reason": reason,
            "detail": detail,
        })
    out.sort(key=lambda r: (-r["count"], r["command"]))
    return out


def _ensure_command_ssot(conn: Any) -> None:
    """Populate ``netops.commands`` if nothing has.

    The classifier needs the SSOT to tell "nothing is registered" from "a parser
    ran and matched nothing" — and without it every unparsed row is
    ``unclassified``, which the learn queue filters out. The result is an empty
    queue on an import that landed 159 unparsed outputs.

    In the platform that never showed, because `olav init` syncs the table and a
    reload hook re-syncs it. The skill pack has neither: no CLI, no plugin
    discovery, by design (dev_docs/126). So the sync has to be reachable from the
    place that needs it, which is here.

    Runs only when the table is missing or empty — a scan of the ntc-templates
    directory is cheap but not free, and in the platform this is a no-op.
    """
    try:
        n = conn.execute("SELECT count(*) FROM netops.commands").fetchone()[0]
        if n:
            return
    except Exception:  # noqa: BLE001 — table absent is exactly the case to fix
        pass
    try:
        from olav_netops.core.commands_sync import sync_commands

        stats = sync_commands(conn)
        logger.info("command SSOT synced during ingest: %s", stats)
    except Exception:  # noqa: BLE001 — classification degrades, ingest does not
        logger.warning("could not sync the command SSOT", exc_info=True)


def _recipe_wanted() -> set[tuple[str, str]]:
    """(vendor, command) pairs a builtin view recipe declares.

    Ranking material for the learn queue. A broad sweep — the collector's default
    since the jump host cannot parse and a second site visit is expensive — lands
    a hundred-odd commands per device, and the learnable subset is then far larger
    than anyone will work through in one sitting. Sorting it by *count* puts
    `show clock` on twenty devices above `show ip bgp summary` on three, which is
    backwards: the second becomes a queryable view the moment it parses, and the
    first becomes a column nobody reads.

    Empty set on any failure — this only orders a suggestion list, and reporting
    must never fail an ingest.
    """
    wanted: set[tuple[str, str]] = set()
    try:
        import yaml

        from olav_netops.core.recipe_files import builtin_recipes_dir

        rdir = builtin_recipes_dir()
        if rdir is None:
            return wanted
        for path in sorted(rdir.glob("*.yaml")):
            for entry in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
                if not isinstance(entry, dict):
                    continue
                command = str(entry.get("command", "")).strip()
                vendor = str(entry.get("vendor_hint") or "").strip()
                # `@directive` rows are computed from data already in the DB —
                # there is no CLI output behind them to learn a parser for.
                if command and not command.startswith("@") and vendor:
                    wanted.add((vendor, command.lower()))
    except Exception:  # noqa: BLE001
        logger.debug("recipe ranking unavailable", exc_info=True)
    return wanted


def _learn_queue(unparsed_rows: list[dict[str, Any]], wanted: set[tuple[str, str]],
                 limit: int = 15) -> list[dict[str, Any]]:
    """The subset `netops/learner` can fix, most useful first.

    `raw_only` is excluded by design and `parser_error` rows are bugs, not gaps.
    Order is recipe-declared before merely frequent: a broad sweep leaves dozens
    of learnable commands and the operator works down the list, so the fifteen
    that surface decide what gets learned.
    """
    return [
        {"command": r["command"], "platform": r["platform"], "count": r["count"],
         "wanted_by": "recipe" if (r["platform"], r["command"].lower()) in wanted
                      else ""}
        for r in sorted(
            (r for r in unparsed_rows
             if r["reason"] in {"no_parser_registered", "parser_no_match"}),
            key=lambda r: ((r["platform"], r["command"].lower()) not in wanted,
                           -r["count"], r["command"]),
        )
    ][:limit]


def _pct(part: int, whole: int) -> int:
    """Rounded percentage; the caller derives the complement so they sum to 100."""
    return round(part * 100 / whole) if whole else 0


def _write_import_report(
    reports_dir: Path,
    *,
    snapshot_id: str,
    bundle_id: str,
    collection_source: str,
    hosts: set[str],
    hosts_with_parsed: set[str],
    pairs_total: int,
    pairs_parsed: int,
    unparsed_rows: list[dict[str, Any]],
    parser_fills: dict[str, int],
) -> str | None:
    """Write the human-readable import report; return its path (or None)."""
    silent = sorted(hosts - hosts_with_parsed)
    by_reason: dict[str, int] = {}
    for r in unparsed_rows:
        by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + r["count"]
    pairs_unparsed = pairs_total - pairs_parsed

    lines: list[str] = [
        f"# Import report — {snapshot_id}",
        "",
        f"- **Bundle**: `{bundle_id}`",
        f"- **Source**: `{collection_source}`",
        f"- **Devices landed**: {len(hosts)}",
        f"- **Command outputs landed**: {pairs_total}",
        # Complementary percentages — flooring both made them sum to 99%.
        f"- **Parsed into structured rows**: {pairs_parsed}"
        + (f" ({_pct(pairs_parsed, pairs_total)}%)" if pairs_total else ""),
        f"- **Not parsed**: {pairs_unparsed}"
        + (f" ({100 - _pct(pairs_parsed, pairs_total)}%)" if pairs_total else ""),
        "",
        "Everything landed is queryable: `netops.raw_output_store` holds the raw",
        "text for every command above, and `netops.devices` has a row per device.",
        "Only the *structured* views (`netops.parsed_outputs`, `v_*_auto`) are",
        "limited to what a parser could read.",
        "",
    ]

    lines += ["## Devices with no structured data", ""]
    if not silent:
        lines += ["Every device produced at least one parsed command output.", ""]
    else:
        lines += [
            f"{len(silent)} of {len(hosts)} devices contributed **no** parsed rows.",
            "They are present in `netops.devices` and their raw output is stored,",
            "but they will not appear in any `v_*_auto` view.",
            "",
            "| Device |",
            "| :--- |",
        ]
        lines += [f"| `{d}` |" for d in silent[:50]]
        if len(silent) > 50:
            lines.append(f"| … and {len(silent) - 50} more |")
        lines.append("")

    lines += ["## Why outputs were not parsed", ""]
    if not by_reason:
        lines += ["Nothing was dropped.", ""]
    else:
        lines += ["| Reason | Outputs | Meaning |", "| :--- | ---: | :--- |"]
        for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            lines.append(
                f"| `{reason}` | {count} | {_REASON_LABELS.get(reason, '—')} |"
            )
        lines.append("")
        lines += [
            "### By command",
            "",
            "| Command | Platform | Outputs | Reason |",
            "| :--- | :--- | ---: | :--- |",
        ]
        for r in unparsed_rows[:40]:
            lines.append(
                f"| `{r['command']}` | {r['platform'] or '—'} | {r['count']} | `{r['reason']}` |"
            )
        if len(unparsed_rows) > 40:
            lines.append(f"| … and {len(unparsed_rows) - 40} more command/platform pairs | | | |")
        lines.append("")

    # Actionable next step. A diagnostic that only names the gap leaves the
    # operator to work out the remedy; netops/learner exists precisely for
    # "stock ntc-templates can't parse this" and freezes a parser that every
    # later pipeline run picks up automatically. raw_only is deliberately
    # excluded — learning a parser for running-config would be wrong — and so
    # are parser_error rows, which are bugs to fix rather than gaps to fill.
    learnable = [
        r for r in unparsed_rows
        if r["reason"] in {"no_parser_registered", "parser_no_match"}
    ]
    if learnable:
        total_learnable = sum(r["count"] for r in learnable)
        lines += [
            "## Next step — teach the parser",
            "",
            f"{total_learnable} of the unparsed outputs are covered by "
            f"`netops/learner`, which takes output the stock ntc-templates cannot "
            "read and freezes a persistent parser; every later ingest picks it up "
            "automatically. The raw text is already stored, so nothing needs "
            "recollecting.",
            "",
            "**This is a suggestion, not something the import did.** Learning is "
            "per (platform, command) and LLM-driven, so it is deliberately kept "
            "out of the ingest — running it here would stretch a few-minute "
            "import into a very long one. Run it separately, when you choose to, "
            "and re-ingest afterwards to pick up the new parsers.",
            "",
            "Highest-volume candidates first:",
            "",
            "| Command | Platform | Outputs | Reason |",
            "| :--- | :--- | ---: | :--- |",
        ]
        for r in learnable[:15]:
            lines.append(
                f"| `{r['command']}` | {r['platform'] or '—'} | {r['count']} | `{r['reason']}` |"
            )
        if len(learnable) > 15:
            lines.append(f"| … and {len(learnable) - 15} more | | | |")
        top = learnable[0]
        lines += [
            "",
            "One command, interactively:",
            "",
            "```",
            f'/learn_cmd "{top["command"]}" --device <a device that ran it>'
            + (f' --platform {top["platform"]}' if top["platform"] else ""),
            "```",
            "",
            "Or the whole backlog in one batch — the samples come straight out of",
            "the raw store:",
            "",
            "```sql",
            "SELECT device_name AS device, platform, command, raw_output",
            "FROM netops.raw_output_store",
            f"WHERE snapshot_id = '{snapshot_id}'",
            "  AND command IN (" + ", ".join(
                f"'{r['command']}'" for r in learnable[:15]
            ) + ")",
            "```",
            "",
            "…then hand those rows to `learn_commands(samples=[...])` in the",
            "`netops/learner` skill (batch mode groups them by platform+command",
            "itself, so pass every sample you have).",
            "",
            "`raw_only` rows are **not** listed here: those commands are registered",
            "as text-only on purpose and must stay unparsed.",
            "",
        ]

    if parser_fills:
        lines += [
            "## Parsed successfully",
            "",
            "| Command | Outputs parsed |",
            "| :--- | ---: |",
        ]
        for cmd, n in sorted(parser_fills.items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{cmd}` | {n} |")
        lines.append("")

    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = reports_dir / f"{snapshot_id}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001 — a report failure must not fail the ingest
        logger.warning("import report could not be written to %s", reports_dir)
        return None


# ── Public entry point ────────────────────────────────────────────────


def ingest_snapshot(
    path: str | Path,
    *,
    db_path: str | Path,
    staging_dir: str | Path,
    collection_source: str,
    audit_recorder: Any | None = None,
    snapshot_id: str | None = None,
    host_platforms: dict[str, str] | None = None,
) -> IngestResult:
    """Land an offline bundle into the netops DB.

    Args:
        path:               Bundle root (directory or .zip).
        db_path:            Main DuckDB file.  Tests use a tmp_path file;
                            production passes ``MAIN_DB_PATH``.
        staging_dir:        Directory used by ``IngestManager`` for the
                            transient ``*.staging.json`` artefact.
        collection_source:  Goes into ``audit_runs.collection_source``,
                            e.g. ``"bundle:olav-collector:0.1.0"``.
        audit_recorder:     Optional ``AuditEventRecorder`` instance.  When
                            None the run is not audited (useful for tests
                            that already own an in-memory connection).
        snapshot_id:        Override; default
                            ``"snap_<utc-ts>_<workspace_id>"``.
        host_platforms:     Optional ``{hostname: platform_key}`` map.
                            Authoritative for those hosts — typically
                            supplied by the ingest sub-agent after the
                            Tier 3 LLM fallback identified platforms that
                            the Tier 1/2 TextFSM cascade could not.

    Returns:
        ``IngestResult``.

    Raises:
        FileNotFoundError:   bundle missing or invalid layout
        ValueError:          validate_bundle returned a hard error (sha256,
                             manifest schema, etc.)
    """
    bundle_path = Path(path)
    db_path = Path(db_path)
    staging_dir = Path(staging_dir)
    host_platforms = dict(host_platforms or {})

    # 1. Validate (sha256 + manifest schema).  Cheap, no DB touch.
    report = validate_bundle(bundle_path)
    if not report.ok:
        raise ValueError(f"bundle validation failed: {report.errors}")

    # 2. Read.
    reader = BundleReader.open(bundle_path)
    manifest = reader.manifest

    sid = snapshot_id or (
        f"snap_{_now().strftime('%Y%m%d_%H%M%S')}_{manifest.workspace_id or 'unknown'}"
    )
    bundle_id = str(uuid.uuid4())

    # 2.5 Tier 1+2 platform discovery for every host whose ``_meta.platform``
    # is missing / ``unknown``.  Caller-supplied host_platforms wins.  This
    # is the ADR-0007 Python-first path: deterministic TextFSM cascade
    # before any per-row work; LLM (Tier 3) only fills in what we couldn't.
    if bundle_path.is_dir():
        from olav.core.ingest.platform_discovery import discover_platform
        devices_root = bundle_path / "devices"
        if devices_root.is_dir():
            for host_dir in devices_root.iterdir():
                if not host_dir.is_dir():
                    continue
                host = host_dir.name
                if host in host_platforms:
                    continue
                disc = discover_platform(host_dir)
                if disc.platform:
                    host_platforms[host] = disc.platform

    # 3. Build staging rows.
    _skip_scrub = manifest.redaction.pre_scrubbed

    rows: list[dict[str, Any]] = []
    parser_fills: dict[str, int] = {}
    hosts: set[str] = set()
    # Loss ledger — what did NOT make it into parsed_outputs, keyed by
    # (platform, command) so the reason can be resolved against the command SSOT
    # once the DB is open. Without this the import is silent about ~74% of the
    # command outputs it landed.
    unparsed: dict[tuple[str, str], int] = {}
    parse_errors: dict[tuple[str, str], str] = {}
    hosts_with_parsed: set[str] = set()

    for rec in reader.iter_command_outputs():
        body = rec.body
        if not _skip_scrub:
            body = _safe_text_for_db(body)
        effective_platform = host_platforms.get(rec.host) or rec.platform
        parsed_json, reason = _parse_one(effective_platform, rec.command, body)
        if parsed_json:
            parser_fills[rec.command] = parser_fills.get(rec.command, 0) + 1
            hosts_with_parsed.add(rec.host)
        else:
            key = (effective_platform or "", rec.command)
            unparsed[key] = unparsed.get(key, 0) + 1
            if reason.startswith("parser_error:"):
                parse_errors.setdefault(key, reason)
        rows.append({
            "snapshot_id": sid,
            "device_name": rec.host,
            "command": rec.command,
            "raw_output": body,
            "parsed_data": parsed_json,
            "platform": effective_platform,
        })
        hosts.add(rec.host)

    # 4. Apply migrations (additive ALTER + CREATE) on the target DB.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open_write_connection(str(db_path)) as conn:
        from olav_netops.core.tables import (
            BundleIngestsTable,
            DevicesTable,
            ParsedOutputsTable,
            RawOutputStoreTable,
            TopologyLinksTable,
        )
        for table in (
            ParsedOutputsTable(),
            RawOutputStoreTable(),
            DevicesTable(),
            TopologyLinksTable(),
            BundleIngestsTable(),
        ):
            table.ensure_schema(conn)
        from olav_netops.migrations.v0_22_portable_ingest import apply_migration
        apply_migration(conn)

    # 5. Write staging JSON + bulk_load via IngestManager.
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging_file = staging_dir / f"{sid}.staging.json"
    staging_file.write_text(json.dumps(rows), encoding="utf-8")

    from olav.core.ingest_manager import IngestManager
    ingest = IngestManager(db_path=db_path, staging_dir=staging_dir)
    ingest.bulk_load()

    # 6. Devices / topology / views.
    from olav_netops.core.device_etl import populate_devices
    from olav_netops.core.topology_engine import extract_lldp_topology
    from olav_netops.core.view_builder import finalise_ingest

    populate_devices(db_path, sid)
    with open_write_connection(str(db_path)) as conn:
        try:
            extract_lldp_topology(conn)
        except Exception:  # noqa: BLE001 — non-fatal
            pass
        try:
            finalise_ingest(conn)
        except Exception:  # noqa: BLE001 — non-fatal
            pass

    # 6b. Import report — classify everything that did NOT parse and write it
    # down. The command SSOT is only readable now that the DB exists, and the
    # tallies were collected in step 3.
    unparsed_rows: list[dict[str, Any]] = []
    try:
        with open_write_connection(str(db_path)) as conn:
            _ensure_command_ssot(conn)
            unparsed_rows = _classify_unparsed(conn, unparsed, parse_errors)
    except Exception:  # noqa: BLE001 — reporting must never fail an ingest
        logger.warning("could not classify unparsed outputs", exc_info=True)

    pairs_total = len(rows)
    pairs_parsed = sum(parser_fills.values())
    silent_hosts = sorted(hosts - hosts_with_parsed)
    by_reason: dict[str, int] = {}
    for _r in unparsed_rows:
        by_reason[_r["reason"]] = by_reason.get(_r["reason"], 0) + _r["count"]

    report_path = _write_import_report(
        Path(staging_dir).parent.parent / "import_reports",
        snapshot_id=sid,
        bundle_id=bundle_id,
        collection_source=collection_source,
        hosts=hosts,
        hosts_with_parsed=hosts_with_parsed,
        pairs_total=pairs_total,
        pairs_parsed=pairs_parsed,
        unparsed_rows=unparsed_rows,
        parser_fills=parser_fills,
    )

    _wanted = _recipe_wanted()
    parse_report: dict[str, Any] = {
        "devices_landed": len(hosts),
        "devices_with_structured_data": len(hosts_with_parsed),
        "devices_with_no_structured_data": len(silent_hosts),
        "devices_with_no_structured_data_sample": silent_hosts[:20],
        "command_outputs_landed": pairs_total,
        "command_outputs_parsed": pairs_parsed,
        "command_outputs_unparsed": pairs_total - pairs_parsed,
        "unparsed_by_reason": by_reason,
        "unparsed_by_command": unparsed_rows[:20],
        # The subset netops/learner can actually fix, so the agent can say
        # "and here is how to close it" rather than only naming the gap.
        # raw_only is excluded by design; parser_error rows are bugs, not gaps.
        "learnable_commands": _learn_queue(unparsed_rows, _wanted),
        "learnable_outputs": sum(
            r["count"] for r in unparsed_rows
            if r["reason"] in {"no_parser_registered", "parser_no_match"}
        ),
        # Wording matters: this is a suggestion for the operator to act on later,
        # never something the ingest should trigger. Learning is per
        # (platform, command) and LLM-driven, so folding it into the import would
        # stretch a few-minute job into a very long one.
        "remedy": (
            "SUGGESTION ONLY — do not run this as part of the import. "
            "netops/learner: `/learn_cmd \"<command>\" --device <device>` for one, "
            "or learn_commands(samples=[...]) for the batch — the raw text is "
            "already in netops.raw_output_store, nothing needs recollecting"
        ),
    }

    # 7. Stamp bundle provenance on the freshly-landed rows + record the
    # bundle_ingests event.
    bundle_sha256 = report.content_sha256_observed or manifest.content_sha256
    ingested_at = _now()
    with open_write_connection(str(db_path)) as conn:
        conn.execute(
            "UPDATE netops.raw_output_store "
            "SET bundle_id = ?, bundle_sha256 = ?, ingested_via = 'bundle' "
            "WHERE snapshot_id = ?",
            [bundle_id, bundle_sha256, sid],
        )
        conn.execute(
            """
            INSERT INTO netops.bundle_ingests
              (bundle_id, snapshot_id, bundle_sha256, collector_name,
               collector_version, collected_at, ingested_at, ingested_by,
               pre_scrubbed, salt_fingerprint, hosts_count, commands_count,
               parser_fill_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                bundle_id, sid, bundle_sha256,
                manifest.collector.name, manifest.collector.version,
                _parse_iso_or_none(manifest.collected_at),
                ingested_at,
                os.environ.get("USER") or getpass.getuser(),
                manifest.redaction.pre_scrubbed,
                manifest.redaction.salt_fingerprint,
                len(hosts), len(rows),
                json.dumps(parser_fills),
            ],
        )

    # 8. Audit.
    audit_run_id: str | None = None
    if audit_recorder is not None:
        try:
            audit_run_id = str(uuid.uuid4())
            audit_recorder.record_run_start(
                run_id=audit_run_id,
                agent_id="ingest",
                source_channel=collection_source,
            )
            try:
                _stamp_collection_source(audit_recorder, audit_run_id, collection_source)
            except Exception:  # noqa: BLE001
                pass
            audit_recorder.record_run_end(run_id=audit_run_id, status="completed")
        except Exception:  # noqa: BLE001 — non-fatal
            pass

    return IngestResult(
        bundle_id=bundle_id,
        snapshot_id=sid,
        bundle_sha256=bundle_sha256,
        collection_source=collection_source,
        hosts=len(hosts),
        commands=len(rows),
        parser_fills=parser_fills,
        audit_run_id=audit_run_id,
        parse_report=parse_report,
        report_path=report_path,
    )


def _parse_iso_or_none(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _stamp_collection_source(recorder, run_id: str, collection_source: str) -> None:
    """Set ``audit_runs.collection_source`` for the given run_id."""
    db_path = getattr(recorder, "_db_path", None)
    if db_path is None:
        return
    with open_write_connection(str(db_path)) as conn:
        conn.execute(
            "UPDATE audit_runs SET collection_source = ? WHERE run_id = ?",
            [collection_source, run_id],
        )
