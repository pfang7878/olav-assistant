"""Data-driven canonical hostname resolver.

Problem it solves
-----------------
LLDP / CDP neighbour advertisements carry whichever hostname the
neighbour happens to send, which in real networks frequently includes
an FQDN suffix (``R4.local``, ``R2.example.com``, ``sw1.mgmt``, …).
Meanwhile the ``source_device`` column in ``netops.topology_links``
always carries the bare nornir inventory hostname.  Downstream queries
that want to dedupe bidirectional advertisements — same physical link
reported by both ends — fail because ``R2 → R4.local`` and
``R4 → R2.local`` look like unrelated edges.

Why this module does NOT maintain a suffix list
-----------------------------------------------
Hardcoding ``[".local", ".example.com", ".mgmt", ...]`` is the wrong
abstraction: every new customer environment invents a new domain
suffix (``.corp``, ``.internal``, ``.lan``, ``.ds.example.com`` …),
and missing one silently breaks dedup.

Instead, the **authoritative registry** of hostnames in this
environment is ``netops.devices`` — populated by Device ETL from the
nornir inventory and ``show version`` output.  That table IS the
"what do we call our devices" source of truth.  Canonical resolution
walks the raw advertisement against the registry through escalating
match strategies:

    1. exact match   ``R4`` == ``R4`` → R4 (no-op fast path)
    2. suffix strip  ``R4.local`` → strip ``.local`` → ``R4`` ∈ devices → R4
    3. prefix match  ``R4-primary`` startswith ``R4`` ∈ devices → R4
    4. edit-distance ``R4x`` ≈ ``R4`` (dist=1) ∈ devices → R4 (typo tolerance)
    5. unmatched     → return input unchanged (external device, not in inv.)

LLMs are deliberately not part of the loop here: this is a finite-set
membership problem, and non-determinism at ingest time would make
every ``topology_links`` row time-dependent on the model's current
mood.  See dev-discussion 2026-04-24 for the "LLM only when novel
structure" principle this module follows.

Performance
-----------
The registry is cached per ``duckdb.DuckDBPyConnection`` instance via
a WeakKey-ish side table keyed on ``id(conn)``.  A
``refresh(conn)`` method exists for long-lived callers (daemon
processes) that need to pick up Device ETL updates without reopening
the connection.  Typical ingest inserts ~dozens of rows — a single
list comparison per row is negligible.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Cache per-connection: {id(conn): (hostnames_tuple, hostnames_lower_set)}
# Keyed by ``id`` rather than the conn object itself to avoid holding
# duckdb references that would break weakrefs on unsupported versions.
_REGISTRY_CACHE: dict[int, tuple[tuple[str, ...], frozenset[str]]] = {}


def _load_hostnames(conn: Any) -> tuple[tuple[str, ...], frozenset[str]]:
    """Pull every hostname from ``netops.devices`` (empty tuple on failure)."""
    try:
        rows = conn.execute(
            "SELECT hostname FROM netops.devices WHERE hostname IS NOT NULL"
        ).fetchall()
    except Exception as exc:   # noqa: BLE001
        logger.debug("hostname_registry: query failed (%s); registry empty", exc)
        return ((), frozenset())
    names = tuple(r[0] for r in rows)
    lowered = frozenset(n.lower() for n in names if isinstance(n, str))
    return names, lowered


def _get_registry(conn: Any) -> tuple[tuple[str, ...], frozenset[str]]:
    key = id(conn)
    if key not in _REGISTRY_CACHE:
        _REGISTRY_CACHE[key] = _load_hostnames(conn)
    return _REGISTRY_CACHE[key]


def refresh(conn: Any) -> None:
    """Drop the cache for *conn* so the next resolve re-queries the DB.

    Call this after any write to ``netops.devices`` in the same session.
    Device ETL already opens a fresh connection so pipeline callers
    rarely need this.
    """
    _REGISTRY_CACHE.pop(id(conn), None)


def _edit_distance_one(a: str, b: str) -> bool:
    """True if *a* and *b* differ by at most one insert/delete/substitute.

    Cheap threshold check — we only care about typo-sized errors; full
    Levenshtein would be overkill for hostname comparison on a finite
    inventory (typically ≤ 100 devices).
    """
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        # count substitutions
        diffs = sum(1 for x, y in zip(a, b) if x != y)
        return diffs <= 1
    # one insert/delete — short is prefix-with-gap of long
    short, long = (a, b) if la < lb else (b, a)
    i = j = 0
    gap_used = False
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
        elif gap_used:
            return False
        else:
            gap_used = True
            j += 1
    return True


def canonicalize(raw: str, conn: Any) -> str:
    """Resolve *raw* to a canonical hostname from ``netops.devices``.

    Args:
        raw: Hostname as advertised by a neighbour (may include FQDN
            suffixes, casing variations, or typos).
        conn: DuckDB connection (read access to ``netops.devices``).

    Returns:
        Canonical hostname when a match is found; the input unchanged
        when no device in the registry matches.  External devices not
        in the nornir inventory (e.g. upstream ``WAN``, ``Switch``)
        therefore pass through untouched.

    Matching priority:
        1. exact (fastest path, covers already-canonical writers)
        2. case-insensitive exact
        3. suffix strip at ``.``  — ``host.fqdn`` → ``host`` if ``host`` ∈ registry
        4. prefix match — ``R4-primary`` / ``R4.1.2`` startswith any hostname
        5. edit-distance ≤ 1 tolerance

    None of the strategies use a hardcoded suffix or domain list — all
    decisions are driven by the current ``netops.devices`` contents.
    """
    if not raw:
        return raw
    names, lowered = _get_registry(conn)
    if not names:
        return raw   # empty registry — nothing to match against

    # 1. exact
    if raw in names:
        return raw

    raw_lower = raw.lower()

    # 2. case-insensitive exact
    if raw_lower in lowered:
        for n in names:
            if n.lower() == raw_lower:
                return n

    # 3. suffix strip at first dot (handles ``R4.local``, ``R2.example.com``)
    if "." in raw:
        head = raw.split(".", 1)[0]
        head_lower = head.lower()
        if head_lower in lowered:
            for n in names:
                if n.lower() == head_lower:
                    return n

    # 4. prefix match — raw startswith a registry entry, non-alphanum next
    for n in names:
        nl = n.lower()
        if len(raw_lower) > len(nl) and raw_lower.startswith(nl):
            # next char must be a non-identifier separator; otherwise
            # R4 would match R40-primary which is a different device.
            tail_char = raw_lower[len(nl)]
            if not tail_char.isalnum():
                return n

    # 5. edit-distance ≤ 1 — typos only; disabled for very short names
    if len(raw) >= 3:
        for n in names:
            if _edit_distance_one(raw_lower, n.lower()):
                return n

    # 6. no match — external device, keep as-is
    return raw


__all__ = ["canonicalize", "refresh"]
