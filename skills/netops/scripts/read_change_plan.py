#!/usr/bin/env python3
"""read_change_plan — load a change-plan / design document for validation.

The simulator's job is to validate change plans, but callers hand it a
*file path* (e.g. ``exports/change_plans/alpha_redundant_ebgp_uplink.md``)
and sub-agents have no generic filesystem tools. This script closes that
gap: given a path it returns the document text; given NO path it lists the
available change plans (empty arg = discovery, ADR-0009 Tier-1 #5).

Returns::

    {"status": "ok", "path": "<resolved>", "content": "<markdown>"}
    {"status": "ok", "available_plans": ["a.md", ...]}          # no path given
    {"status": "error", "message": "...", "available_plans": [...]}  # bad path
"""
from __future__ import annotations

from pathlib import Path

_PLANS_DIR = Path("exports") / "change_plans"
_MAX_CHARS = 20000  # keep the doc inside a medium model's context budget


def _list_plans() -> list[str]:
    if _PLANS_DIR.is_dir():
        return sorted(p.name for p in _PLANS_DIR.glob("*.md"))
    return []


def read_change_plan(path: str | None = None) -> dict:
    """Read a change plan document.

    Args:
        path: file path — absolute, relative to the project dir, or just a
            filename under exports/change_plans/. Omit to list available plans.
    """
    if not path:
        return {"status": "ok", "available_plans": _list_plans(),
                "hint": "call again with path=<one of these>"}

    p = Path(path).expanduser()
    candidates = [p] if p.is_absolute() else [p, _PLANS_DIR / p.name]
    target = next((c for c in candidates if c.is_file()), None)
    if target is None:
        return {
            "status": "error",
            "message": f"file not found: {path}",
            "available_plans": _list_plans(),
        }

    text = target.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > _MAX_CHARS
    return {
        "status": "ok",
        "path": str(target),
        "content": text[:_MAX_CHARS],
        **({"truncated": True, "total_chars": len(text)} if truncated else {}),
    }


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = read_change_plan(**_args)
    print(_json.dumps(result, default=str))
