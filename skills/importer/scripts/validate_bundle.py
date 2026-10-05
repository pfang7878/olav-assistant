#!/usr/bin/env python3
"""`validate_bundle` — pre-flight sanity check for portable bundles.

Wraps ``olav.core.ingest.validators.validate_bundle``.  Cheap, no DB
touch.  The ingest sub-agent should call this before
``ingest_snapshot`` so a bad bundle is rejected without polluting state.
"""
from __future__ import annotations


def validate_bundle(path: str) -> dict:
    """Pre-flight bundle check — manifest schema + content sha256 + host counts.

    Args:
        path: Filesystem path to a directory-backed bundle.

    Returns:
        A dict with keys:

          - ``ok``               (bool)        true ⇒ safe to ingest
          - ``errors``           (list[str])   blocking issues
          - ``warnings``         (list[str])   non-blocking (host count drift, etc.)
          - ``hosts_seen``       (int)
          - ``commands_seen``    (int)
          - ``content_sha256``   (str)         observed content hash
    """
    from olav.core.ingest.validators import validate_bundle as _validate

    report = _validate(path)
    return {
        "ok": report.ok,
        "errors": report.errors,
        "warnings": report.warnings,
        "hosts_seen": report.hosts_seen,
        "commands_seen": report.commands_seen,
        "content_sha256": report.content_sha256_observed,
    }


if __name__ == "__main__":
    import json as _json, sys as _sys
    _args = _json.loads(_sys.stdin.read() or "{}")
    result = validate_bundle(**_args)
    print(_json.dumps(result, default=str))
