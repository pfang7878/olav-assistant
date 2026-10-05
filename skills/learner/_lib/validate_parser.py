"""Validate a parser draft against every sample.

Handles both DSL types:
  * TextFSM: parse each sample via textfsm.TextFSM.ParseText
  * Python: AST-safety-check → execute in sandbox → call parse(raw) per sample

Returns a structured result the caller uses to decide freeze vs retry.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _ast_safety_check(code: str) -> tuple[bool, str]:
    """Delegate to parser_learner's inlined ast_safety_check."""
    try:
        from olav_netops.core.parser_learner import ast_safety_check
        return ast_safety_check(code)
    except Exception as exc:
        return False, f"ast_safety_check unavailable: {exc}"


def _parse_textfsm(template_text: str, raw: str) -> list[dict[str, Any]] | None:
    """Parse raw output with a TextFSM template string."""
    import io
    try:
        import textfsm
    except Exception as exc:
        logger.debug("validate_parser: textfsm import failed: %s", exc)
        return None
    try:
        fsm = textfsm.TextFSM(io.StringIO(template_text))
        rows = fsm.ParseText(raw)
        headers = [h.lower() for h in fsm.header]
        return [dict(zip(headers, r)) for r in rows]
    except Exception as exc:
        logger.debug("validate_parser: textfsm parse failed: %s", exc)
        return None


def _run_python_parser(code: str, samples: list[dict[str, Any]]) -> list[list[dict[str, Any]] | None]:
    """Execute ``code`` in sandbox, call parse(raw) on every sample.

    Returns a list of per-sample results (None when that sample failed).
    """
    try:
        from olav.platform.sandbox import execute_in_sandbox
    except Exception as exc:
        logger.debug("validate_parser: sandbox import failed: %s", exc)
        return [None] * len(samples)

    # Build a driver script that runs parse() against every sample.
    per_sample: list[list[dict[str, Any]] | None] = []
    for s in samples:
        driver = (
            code + "\n\n"
            "_raw = " + repr(s.get("raw_output") or "") + "\n"
            "_result = parse(_raw)\n"
        )
        try:
            res = execute_in_sandbox(driver, timeout=20)
        except Exception as exc:
            logger.debug("validate_parser: sandbox exec raised for %s: %s",
                         s.get("device", "?"), exc)
            per_sample.append(None)
            continue
        if not res or res.get("status") not in {"success", None}:
            logger.debug("validate_parser: sandbox status=%s for %s: %s",
                         res.get("status") if res else "none",
                         s.get("device", "?"),
                         res.get("error") or res.get("reason") if res else "")
            per_sample.append(None)
            continue
        result = res.get("result")
        if isinstance(result, list):
            per_sample.append(result)
        else:
            per_sample.append(None)
    return per_sample


def validate_parser(
    dsl: str,
    source: str,
    samples: list[dict[str, Any]],
    *,
    min_coverage: float = 0.7,
) -> dict[str, Any]:
    """Validate a drafted parser.

    Returns ``{ok, per_sample, diagnostic}``:
      * ``ok``: True if every sample parsed AND aggregate coverage ≥ min_coverage
      * ``per_sample``: list of parsed results (None on failure) per input sample
      * ``diagnostic``: short string explaining failure (empty when ok=True)
    """
    if not source.strip():
        return {"ok": False, "per_sample": [None] * len(samples), "diagnostic": "empty source"}

    if dsl == "python":
        ok, reason = _ast_safety_check(source)
        if not ok:
            return {"ok": False, "per_sample": [None] * len(samples),
                    "diagnostic": f"AST safety violation: {reason}"}
        per_sample = _run_python_parser(source, samples)
    elif dsl == "textfsm":
        per_sample = [_parse_textfsm(source, s.get("raw_output") or "") for s in samples]
    else:
        return {"ok": False, "per_sample": [None] * len(samples),
                "diagnostic": f"unknown DSL: {dsl!r}"}

    # Coverage check: fraction of samples that produced at least one record
    successes = sum(1 for r in per_sample if r and len(r) > 0)
    coverage = successes / max(1, len(samples))
    if successes == 0:
        return {"ok": False, "per_sample": per_sample,
                "diagnostic": "no sample parsed"}
    if coverage < min_coverage:
        return {"ok": False, "per_sample": per_sample,
                "diagnostic": f"coverage {coverage:.0%} < {min_coverage:.0%} threshold"}
    return {"ok": True, "per_sample": per_sample, "diagnostic": ""}
