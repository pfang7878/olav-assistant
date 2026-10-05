"""Command whitelist SSOT — derive from parser artifacts (R73).

Populates the ``netops.commands`` table from the three parser sources:

1. **ntc-templates** — globs the shipped wheel directory; filename
   ``<platform>_<safe_command>.textfsm`` → ``(platform, command)``.
2. **Custom TextFSM** — `.olav/templates/<platform>/<safe_command>.textfsm`
   (user-seeded templates + auto_learn / `/learn_cmd` output).
3. **PaC Python parsers** — `.olav/templates/parsers/<platform>/<cmd>.py`
   (ARCH-25 learned parsers).

Overlays applied last (so they win):

* ``.olav/config/blacklisted_commands.yaml`` → set ``blacklisted=true``
* ``.olav/config/user_commands.yaml``         → add backup-only / raw-only
  commands that have no parser but user wants collected anyway.

Why: before R73 the pipeline read a hardcoded ``discovery_commands.yaml``
that duplicated what the parsers themselves already knew. Meanwhile
``execute_cli._validate_command`` queried ``netops.commands`` but the
table was never populated (``sync_commands()`` was referenced but
unimplemented). R73 closes both ends — one table, derived from
parsers, consumed by both collection and CLI whitelist.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CMD_NAME_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _ensure_table(conn: Any) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS netops")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS netops.commands (
            platform      VARCHAR NOT NULL,
            command       VARCHAR NOT NULL,
            safe_command  VARCHAR NOT NULL,
            parser_type   VARCHAR,          -- ntc | custom_textfsm | pac | raw_only
            parser_path   VARCHAR,          -- frozen artifact path (null for raw_only)
            blacklisted   BOOLEAN NOT NULL DEFAULT false,
            pipe_allowed  BOOLEAN NOT NULL DEFAULT true,
            backup_only   BOOLEAN NOT NULL DEFAULT false,
            synced_at     TIMESTAMP,
            PRIMARY KEY (platform, command)
        )
        """
    )


def _safe(cmd: str) -> str:
    return _CMD_NAME_RE.sub("_", cmd.strip().lower()).strip("_")


def _decanonical(safe: str, prefix: str = "show ") -> str:
    """Reverse a ``safe_command`` token back to spaced form.

    ``show_ip_bgp_summary`` → ``show ip bgp summary`` (best-effort).
    Used when parsing ntc-template filenames where the original command
    is only present in its safe form. Good enough for `show` and `display`
    families; edge cases (commands with literal underscores) are rare
    enough to accept manual overrides in ``user_commands.yaml``.
    """
    return safe.replace("_", " ")


# ──────────────────────────────────────────────────────────────────────────
# Source scanners
# ──────────────────────────────────────────────────────────────────────────

def _scan_ntc() -> list[dict[str, Any]]:
    """Scan ntc-templates wheel for (platform, command) pairs."""
    try:
        import ntc_templates
    except ImportError:
        logger.info("sync_commands: ntc-templates not installed")
        return []
    tdir = Path(ntc_templates.__file__).parent / "templates"
    if not tdir.exists():
        return []
    rows: list[dict[str, Any]] = []
    # ntc file name schema: <vendor>_<os>_<safe_command>.textfsm
    # e.g. `cisco_ios_show_ip_bgp_summary.textfsm`
    # The platform prefix is always the first TWO tokens (vendor_os).
    for p in sorted(tdir.glob("*.textfsm")):
        stem = p.stem
        parts = stem.split("_")
        if len(parts) < 3:
            continue
        platform = f"{parts[0]}_{parts[1]}"
        safe_cmd = "_".join(parts[2:])
        cmd = _decanonical(safe_cmd)
        rows.append({
            "platform": platform,
            "command": cmd,
            "safe_command": safe_cmd,
            "parser_type": "ntc",
            "parser_path": str(p),
        })
    return rows


def _scan_custom_textfsm(templates_base: Path) -> list[dict[str, Any]]:
    """Scan `.olav/templates/<platform>/*.textfsm` for user / auto_learn templates."""
    rows: list[dict[str, Any]] = []
    if not templates_base.exists():
        return rows
    for plat_dir in templates_base.iterdir():
        if not plat_dir.is_dir() or plat_dir.name in {"parsers"} or plat_dir.name.startswith("_"):
            continue
        for p in plat_dir.glob("*.textfsm"):
            safe_cmd = p.stem
            rows.append({
                "platform": plat_dir.name,
                "command": _decanonical(safe_cmd),
                "safe_command": safe_cmd,
                "parser_type": "custom_textfsm",
                "parser_path": str(p),
            })
    return rows


def _scan_pac(templates_base: Path) -> list[dict[str, Any]]:
    """Scan `.olav/templates/parsers/<platform>/*.py` for PaC parsers."""
    rows: list[dict[str, Any]] = []
    parsers_root = templates_base / "parsers"
    if not parsers_root.exists():
        return rows
    # Main tree
    for plat_dir in parsers_root.iterdir():
        if not plat_dir.is_dir() or plat_dir.name.startswith("_"):
            continue
        for p in plat_dir.glob("*.py"):
            if p.name.startswith("_") or p.name == "__init__.py":
                continue
            safe_cmd = p.stem
            rows.append({
                "platform": plat_dir.name,
                "command": _decanonical(safe_cmd),
                "safe_command": safe_cmd,
                "parser_type": "pac",
                "parser_path": str(p),
            })
    return rows


# ──────────────────────────────────────────────────────────────────────────
# Overlay loaders
# ──────────────────────────────────────────────────────────────────────────

def _load_blacklist(
    config_dir: Path,
    workspace_root: Path | None = None,
) -> list[re.Pattern[str]]:
    """Compile blacklist regex patterns from both shipped defaults + user override.

    Two sources merged (user override appends to shipped default — both
    sets of patterns apply, patterns never "cancel" each other):

    1. **Workspace default**:
       ``.olav/workspace/netops/config/blacklisted_commands.yaml`` — shipped
       with olav-netops.  Covers interactive commands (``ping``,
       ``traceroute``), privileged write paths (``write``, ``copy``,
       ``configure``), feature-gated commands that error on stock
       devices (``show chassis cluster``, ``show mpls``…), and noisy
       mega-outputs (``show tech-support``).  Without this the
       data-driven command set from ntc-templates includes ~140
       commands per Cisco platform, many of which hang netmiko's
       prompt-detection or corrupt the shared session.

    2. **User override**:
       ``~/.olav/config/blacklisted_commands.yaml`` — extends the
       shipped set with site-specific patterns.
    """
    paths: list[Path] = []
    if workspace_root is not None:
        ws_path = workspace_root / "netops" / "config" / "blacklisted_commands.yaml"
        if ws_path.exists():
            paths.append(ws_path)
    user_path = config_dir / "blacklisted_commands.yaml"
    if user_path.exists():
        paths.append(user_path)

    patterns: list[re.Pattern[str]] = []
    for path in paths:
        try:
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        except Exception as exc:
            logger.warning("sync_commands: blacklist load failed at %s: %s", path, exc)
            continue
        for entry in data:
            if isinstance(entry, dict):
                pat = entry.get("command", "").strip()
            elif isinstance(entry, str):
                pat = entry.strip()
            else:
                continue
            if not pat:
                continue
            try:
                patterns.append(re.compile(pat, re.IGNORECASE))
            except re.error as exc:
                logger.warning("sync_commands: invalid blacklist regex %r: %s", pat, exc)
    return patterns


def _load_user_commands(config_dir: Path, workspace_root: Path | None = None) -> list[dict[str, Any]]:
    """Load ``user_commands.yaml`` — commands without parsers to collect anyway.

    Two sources merged (user override wins):

    1. **Workspace default**: `.olav/workspace/netops/netops_init/config/user_commands.yaml`
       — shipped with olav-netops, carries per-vendor backup commands
       (``show running-config`` for Cisco, ``show configuration`` for Junos, …).

    2. **User override**: `~/.olav/config/user_commands.yaml` — user
       extends or replaces via same schema.

    YAML schema::

        commands:
          cisco_ios:
            - show running-config
            - show startup-config
          juniper_junos:
            - show configuration
    """
    paths: list[Path] = []
    if workspace_root is not None:
        ws_path = workspace_root / "netops" / "netops_init" / "config" / "user_commands.yaml"
        if ws_path.exists():
            paths.append(ws_path)
    user_path = config_dir / "user_commands.yaml"
    if user_path.exists():
        paths.append(user_path)

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        try:
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("sync_commands: %s load failed: %s", path, exc)
            continue
        block = data.get("commands") or {}
        for platform, cmds in block.items():
            if not isinstance(cmds, list):
                continue
            for cmd in cmds:
                if not isinstance(cmd, str) or not cmd.strip():
                    continue
                key = (platform, cmd.strip())
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "platform": platform,
                    "command": cmd.strip(),
                    "safe_command": _safe(cmd),
                    "parser_type": "raw_only",
                    "parser_path": None,
                    "backup_only": True,
                })
    return rows


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────

def _deploy_seed_templates(agent_dir: Path) -> int:
    """Copy vendor-seed TextFSM templates into `.olav/templates/`.

    Shipped defaults live at
    ``.olav/workspace/netops/netops_init/seed_templates/<platform>/*.textfsm``
    and are copied into ``.olav/templates/<platform>/*.textfsm`` on every
    sync call. Existing user templates are NOT overwritten — seeds are
    dropped only when the target file doesn't yet exist. Returns the
    count of files newly deployed.
    """
    import shutil
    seed_root = agent_dir / "workspace" / "netops" / "netops_init" / "seed_templates"
    if not seed_root.exists():
        return 0
    target_root = agent_dir / "templates"
    deployed = 0
    for plat_dir in seed_root.iterdir():
        if not plat_dir.is_dir():
            continue
        target_plat = target_root / plat_dir.name
        target_plat.mkdir(parents=True, exist_ok=True)
        for seed in plat_dir.glob("*.textfsm"):
            target = target_plat / seed.name
            if target.exists():
                continue
            try:
                shutil.copy2(seed, target)
                deployed += 1
                logger.info("sync_commands: seeded template %s", target)
            except Exception as exc:
                logger.warning("sync_commands: failed to seed %s: %s", seed, exc)
    return deployed


def sync_commands(conn: Any) -> dict[str, Any]:
    """Populate ``netops.commands`` from ntc-templates + custom + PaC + overlays.

    Idempotent — runs on every `/netops_init`. UPSERT on
    ``(platform, command)`` primary key.

    Returns stats::

        {"ntc": int, "custom": int, "pac": int, "user": int,
         "blacklisted": int, "total": int, "seeded": int}
    """
    try:
        from olav.core.config import get_paths_config
        agent_dir = Path(get_paths_config().agent_dir_path)
    except Exception:
        agent_dir = Path.home() / ".olav"
    templates_base = agent_dir / "templates"

    # Resolve config dir (project-local; not home)
    try:
        from olav.core.config import get_paths_config
        config_dir = Path(get_paths_config().config_dir)
    except Exception:
        config_dir = Path.cwd() / ".olav" / "config"

    _ensure_table(conn)
    seeded = _deploy_seed_templates(agent_dir)

    # Locate the workspace root so we can find the shipped defaults
    # (user_commands.yaml, blacklisted_commands.yaml, ...).
    workspace_root = agent_dir / "workspace"

    ntc_rows = _scan_ntc()
    custom_rows = _scan_custom_textfsm(templates_base)
    pac_rows = _scan_pac(templates_base)
    user_rows = _load_user_commands(config_dir, workspace_root=workspace_root)
    blacklist_patterns = _load_blacklist(config_dir, workspace_root=workspace_root)

    # Priority order: PaC > custom > user > ntc (rightmost wins on dup key
    # in the merged dict below, but PaC overriding ntc is the desired
    # behaviour since a frozen PaC parser beats ntc's TextFSM at parse
    # time; collection semantics are the same).
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for batch in (ntc_rows, user_rows, custom_rows, pac_rows):
        for row in batch:
            merged[(row["platform"], row["command"])] = row

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    bl_hits = 0

    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute("DELETE FROM netops.commands")
        for row in merged.values():
            cmd = row["command"]
            blacklisted = any(pat.search(cmd) for pat in blacklist_patterns)
            if blacklisted:
                bl_hits += 1
            conn.execute(
                """
                INSERT INTO netops.commands
                    (platform, command, safe_command, parser_type, parser_path,
                     blacklisted, pipe_allowed, backup_only, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row["platform"], row["command"], row["safe_command"],
                    row["parser_type"], row.get("parser_path"),
                    bool(blacklisted), True,
                    bool(row.get("backup_only", False)),
                    now_iso,
                ],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    stats = {
        "ntc": len(ntc_rows),
        "custom": len(custom_rows),
        "pac": len(pac_rows),
        "user": len(user_rows),
        "blacklisted": bl_hits,
        "total": len(merged),
        "seeded": seeded,
    }
    logger.info("sync_commands: %s", stats)
    return stats


# ──────────────────────────────────────────────────────────────────────────
# Batch-collection eligibility
# ──────────────────────────────────────────────────────────────────────────

# Verbs that produce safe, non-interactive, pastable output suitable for
# unattended SSH collection. Any command whose first token is one of these
# is a candidate for batch collection; everything else (ping, traceroute,
# configure, telnet, copy, debug, etc.) is excluded even if a parser
# exists.
_READ_ONLY_VERBS = frozenset({
    "show",      # Cisco / Arista / Huawei
    "display",   # Huawei / H3C
    "get",       # Fortinet / Palo Alto
    "fetch",     # Juniper (rare)
})

# Even within `show …`, some families are noisy or destructive and must
# be skipped: interactive pagers, statistics-reset commands, huge dumps.
_BATCH_SKIP_PATTERNS = (
    "show tech",          # mega-dump, commonly paged
    "show history",       # session-local
    "show clock details", # no stable output for diff
    "show logging",       # volatile, pollutes diff
    "show log",           # same
)


def is_batch_collectible(command: str) -> bool:
    """Return True when ``command`` is safe to SSH in unattended batch.

    The commands table deliberately contains EVERY parseable command —
    including interactive ones like ``ping`` (ntc-templates has a
    ``cisco_ios_ping.textfsm``). The runtime discovery list is the
    intersection of (has_parser) AND (batch-collectible).
    """
    if not command:
        return False
    low = command.strip().lower()
    first = low.split()[0] if low.split() else ""
    if first not in _READ_ONLY_VERBS:
        return False
    for pat in _BATCH_SKIP_PATTERNS:
        if low.startswith(pat):
            return False
    return True


def get_discovery_commands(
    conn: Any, platform: str,
    include_backup: bool = True,
    batch_only: bool = True,
) -> list[str]:
    """Return the collection list for a platform.

    * ``batch_only`` (default True): exclude interactive/destructive
      commands via :func:`is_batch_collectible`. This is what
      `/netops_init` uses.
    * ``include_backup`` (default True): include ``backup_only`` entries
      (``show running-config`` etc.) the user added via
      ``user_commands.yaml`` — these bypass the ``batch_only`` filter
      since the user explicitly opted them in.

    Pass ``batch_only=False`` when you want the full parser-having
    whitelist (e.g. `execute_cli` validation).
    """
    rows = conn.execute(
        "SELECT command, backup_only FROM netops.commands "
        "WHERE platform = ? AND blacklisted = false "
        "ORDER BY command",
        [platform],
    ).fetchall()
    out: list[str] = []
    for cmd, backup in rows:
        is_backup = bool(backup)
        if is_backup:
            if include_backup:
                out.append(cmd)
            continue
        # Non-backup row — subject to batch_only gate.
        if batch_only and not is_batch_collectible(cmd):
            continue
        out.append(cmd)
    return out
