"""Parse-classification helpers used by the command_learner skill.

Extracted from the deleted ``auto_learn.py`` (Round 72 follow-through: R71
already flagged ``auto_learn_failed_parses`` deprecated; v0.21.0 removes
the module entirely in favour of a parse-coverage classifier in
``netops_init`` + explicit ``/learn_cmd`` user action via
``command_learner``).

What's here
-----------
Just the two low-risk classifiers the surviving code actually imports:

* :func:`should_learn` — false-positive gate (empty / error messages /
  backup commands / timeouts).  Both ``parser_learner.py`` and
  ``command_learner/tools/learn_commands.py`` call this before invoking
  the LLM, so a wheel install without these helpers breaks the
  interactive ``/learn_cmd`` path.
* :func:`_estimate_data_rows` — row-count heuristic used by
  ``parser_learner`` to score generated parsers against raw output.

Nothing LLM-facing remains; anything that *generated* TextFSM templates
or called LLMs for batch learning went with ``auto_learn.py``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


BACKUP_COMMANDS = frozenset({
    "show running-config",
    "show startup-config",
    "show configuration",
    "show running-config | display set",
    "show configuration | display set",
})
"""Commands whose output is stored verbatim (not parsed into structured rows).

Keep in sync with ``olav.core.ingest_manager._load_backup_commands``
(R73 SSOT).  Duplicating a short set here is cheap; reaching back into
the platform for a single constant from an inner loop is not."""


ERROR_PATTERNS = [
    "% invalid", "% unknown", "syntax error", "not found",
    "command not recognized", "% incomplete", "permission denied",
    "access denied", "% authorization", "% ambiguous",
    "invalid input detected",  # cisco IOS "% Invalid input detected at '^' marker"
    "unknown command",         # junos "unknown command."
    "missing argument",        # junos "missing argument."
]
"""Common device-error prefixes.  Conservative list — false positives
here are harmless (we'd just skip learning a parse); false negatives
waste LLM tokens on garbage input, which is much worse."""


def is_cli_error(raw_output: str, *, min_len: int = 30) -> bool:
    """Whether raw CLI output is a device error response, not real data.

    Used at ingest time to keep error responses out of
    ``netops.raw_output_store`` and ``netops.parsed_outputs`` — error
    text in the DB pollutes parse-coverage stats and tricks downstream
    parsers into reporting "State Error" / failed-template noise.

    A response is treated as an error when:

    * it is shorter than *min_len* non-whitespace characters, OR
    * any of the first 5 lines (lower-cased) contains a known
      :data:`ERROR_PATTERNS` marker.

    Args:
        raw_output: The raw text the device returned.
        min_len:    Minimum length (after strip) to accept as a real
            response.  Default 30 — shorter than this almost always
            means "device printed an error and a prompt".

    Returns:
        ``True`` when the output should be rejected; ``False`` when it
        looks like real CLI data.
    """
    text = raw_output.strip()
    if len(text) < min_len:
        return True
    head = "\n".join(text.split("\n")[:5]).lower()
    return any(pat in head for pat in ERROR_PATTERNS)


def should_learn(command: str, raw_output: str) -> bool:
    """Whether a TextFSM parse failure should trigger learning.

    Args:
        command: The CLI command that was run (e.g. ``"show bgp summary"``).
        raw_output: The raw text the device returned.

    Returns:
        ``False`` when the output is empty / an error message / a
        backup-command dump / clearly timed-out — learning won't help
        and will burn LLM budget.  ``True`` when the output looks like
        structured data a parser could handle.
    """
    text = raw_output.strip()

    if len(text) < 30:
        return False

    if command.lower().strip() in BACKUP_COMMANDS:
        return False

    # Check the first 3 lines for device-error prefixes — errors on the
    # first line are extremely common, but some CLIs print a header
    # before the error (e.g. Junos).
    first_lines = "\n".join(text.split("\n")[:3]).lower()
    if any(p in first_lines for p in ERROR_PATTERNS):
        return False

    lower = text.lower()
    if "timeout" in lower or "timed out" in lower:
        return False

    return True


def _estimate_data_rows(raw_output: str, command: str) -> int:
    """Rough count of data rows in *raw_output*.

    Used by the parser-learner to validate generated parsers: a parser
    returning 0 rows when the estimator says ~17 is clearly wrong.

    The heuristic skips header / separator / blank lines and counts
    lines that start with an IP, hostname or interface name with at
    least two whitespace-separated tokens.  Return value ``0`` means
    "unreliable — accept any parse result" (don't use this as a
    hard rejection threshold).

    Args:
        raw_output: Device CLI output.
        command: The command string (unused today, kept for forward
            compatibility when the heuristic becomes per-command).

    Returns:
        Non-negative integer row estimate; ``0`` when the input is
        too short or too irregular to estimate.
    """
    import re

    _ = command  # reserved for per-command heuristics

    lines = raw_output.strip().split("\n")
    if len(lines) < 3:
        return 0

    data_lines = 0
    header_seen = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("---") or stripped.startswith("==="):
            continue
        if any(kw in stripped.lower() for kw in [
            "threading", "groups:", "table ", "peer ", "local interface",
            "device id", "capability", "platform:",
        ]):
            header_seen = True
            continue
        if header_seen:
            if re.match(r"^[0-9a-zA-Z]", stripped) and len(stripped.split()) >= 2:
                data_lines += 1

    return data_lines


__all__ = ["BACKUP_COMMANDS", "ERROR_PATTERNS", "is_cli_error",
           "should_learn", "_estimate_data_rows"]
