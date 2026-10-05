"""Persist a validated parser to `.olav/templates/`.

TextFSM → `.olav/templates/<platform>/<cmd_safe>.textfsm`
Python (≥2 samples) → `.olav/templates/parsers/<platform>/<cmd_safe>.py`
Python (<2 samples) → `.olav/templates/parsers/_quarantine/<platform>/<cmd_safe>.py`

Header stamp (6 lines) lets later code detect provenance + invalidate
when LEARNER_CONTRACT_VERSION is bumped.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _safe_name(command: str) -> str:
    """Whitespace/special-char → underscore; limit 60 chars."""
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", command)
    return s[:60]


def _templates_dir() -> Path:
    try:
        from olav.core.config import get_paths_config
        return Path(get_paths_config().agent_dir_path) / "templates"
    except Exception:
        return Path.home() / ".olav" / "templates"


def _header(
    dsl: str, platform: str, command: str,
    samples_hash: str, contract_version: int,
    comment_prefix: str,
) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"{comment_prefix} OLAV command-learner v1.0",
        f"{comment_prefix} platform: {platform}",
        f"{comment_prefix} command: {command}",
        f"{comment_prefix} dsl: {dsl}",
        f"{comment_prefix} learned_at: {now}",
        f"{comment_prefix} samples_hash: {samples_hash}",
        f"{comment_prefix} contract_version: {contract_version}",
    ]
    # Python tolerates either "\n\n" (blank line) or "\n" separator; TextFSM
    # interprets a blank line between the header and Value declarations as
    # the Values-to-States boundary, which breaks parsing. Use single
    # newline for textfsm, double for python.
    sep = "\n\n" if dsl == "python" else "\n"
    return "\n".join(lines) + sep


def freeze_parser(
    dsl: str,
    source: str,
    platform: str,
    command: str,
    *,
    num_samples: int,
    samples_hash: str,
    contract_version: int,
) -> dict[str, Any]:
    """Write the parser to its canonical location.

    Returns a dict describing the freeze: ``{dsl, path, quarantined}``.
    """
    base = _templates_dir()
    safe_cmd = _safe_name(command)

    if dsl == "textfsm":
        target_dir = base / platform
        target = target_dir / f"{safe_cmd}.textfsm"
        comment_prefix = "#"
        quarantined = False
    elif dsl == "python":
        quarantined = num_samples < 2
        parsers_root = base / "parsers"
        if quarantined:
            target_dir = parsers_root / "_quarantine" / platform
        else:
            target_dir = parsers_root / platform
        target = target_dir / f"{safe_cmd}.py"
        comment_prefix = "#"
    else:
        raise ValueError(f"unknown DSL: {dsl!r}")

    target_dir.mkdir(parents=True, exist_ok=True)
    header = _header(dsl, platform, command, samples_hash, contract_version, comment_prefix)
    target.write_text(header + source.rstrip() + "\n", encoding="utf-8")
    logger.info(
        "freeze_parser: wrote %s (%s, %d samples, quarantine=%s)",
        target, dsl, num_samples, quarantined,
    )
    return {
        "dsl": dsl,
        "path": str(target),
        "quarantined": quarantined,
        "platform": platform,
        "command": command,
    }
