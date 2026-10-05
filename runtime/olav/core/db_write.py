"""Unified DuckDB write seam (ADR-0018 / ADR-0019, dev_docs/111).

Every write to a DuckDB file should acquire its connection through
``open_write_connection`` instead of calling ``duckdb.connect(path,
read_only=False)`` directly. Converging the ~40 scattered write sites onto one
seam lets the *concurrency strategy* be swapped at a single point:

* **OSS (personal use)** — a per-db-path in-process ``threading.Lock`` plus a
  connect-time retry with full-jitter back-off. DuckDB's write lock fails at
  ``connect()`` time (before any SQL runs), so retrying the *acquisition* is
  sufficient and safe — once connected, the writer holds the lock for the whole
  ``with`` body and cannot be pre-empted. This is right-sized for a single user
  whose only contention is an occasional burst of parallel ``execute_skill_script``
  subprocesses (see ``skill_runner.py:30-43``).

* **Enterprise (team use)** — a cross-process ``flock`` gate is *injected* at
  this same seam via a guarded optional import of
  ``olav.enterprise.db_write_gate``. It turns the optimistic "retry on
  conflict" into a blocking kernel-queued FIFO, giving predictable write
  latency and zero write failures under sustained multi-process contention.
  In OSS (no ``olav.enterprise`` installed) the gate is a ``nullcontext``.

Invariant for callers: do **not** nest two *owning* opens on the same db_path
in one thread (``with open_write_connection(p): with open_write_connection(p):``)
— the in-process lock is non-reentrant and the enterprise flock gate would
self-deadlock across the two file descriptors. For batch work, open once and
pass the connection down via ``conn=`` (injection reuses it without re-locking
or closing). This matches the existing presales pattern
(``bulk_add_requirement_items`` → ``add_requirement_item(conn=conn)``).
"""

from __future__ import annotations

import contextlib
import logging
import random
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

# Substrings (lower-cased) DuckDB uses when another holder has the write lock.
# Merged from ``audit_recorder._RETRYABLE`` and ``skill_runner._LOCK_CONFLICT_MARKERS``
# so the seam covers every phrasing seen across DuckDB versions. Deliberately
# specific: an unrelated failure (bad SQL, missing file) must raise once, not be
# masked by a retry loop.
_LOCK_MARKERS = (
    "could not set lock",       # skill_runner phrasing
    "conflicting lock",         # both
    "write-write conflict",     # audit_recorder phrasing
    "database is locked",       # audit_recorder phrasing
    "locked",                   # broad fallback
)

_CONNECT_ATTEMPTS = 10          # 1 initial + 9 retries (matches skill_runner)
_BASE_DELAY = 0.15              # seconds
_MAX_WINDOW = 2.0              # cap the exponential window; attempts matter more than reach

# Per-db-path in-process locks. Same-process concurrent writers (e.g. API server
# threads) serialise here; distinct DB files never block each other.
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}

# Per-thread set of db_paths for which THIS thread already holds the write
# gate. A nested owning-open on the same path (an outer ``with
# open_write_connection(p)`` whose body transitively opens another one on the
# same ``p``) must NOT re-acquire the (non-reentrant) process lock or the
# enterprise flock gate — that would self-deadlock. The outer acquisition
# already serialises this thread against all others, so the inner open just
# needs a fresh connection (DuckDB permits multiple same-process connections to
# one file). netops relies on this (e.g. take_snapshot's write block calls into
# helpers that open their own connection; map_engine nests write connections).
_held = threading.local()


def _thread_held_paths() -> set[str]:
    paths = getattr(_held, "paths", None)
    if paths is None:
        paths = set()
        _held.paths = paths
    return paths

# Enterprise cross-process gate, resolved once. ``None`` = OSS (nullcontext).
_gate_resolved = False
_gate_fn = None


def _process_lock(key: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _locks[key] = lk
        return lk


def _is_lock_conflict(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _LOCK_MARKERS)


def _retry_delay(attempt: int) -> float:
    """Full jitter (AWS pattern): a random delay UP TO the capped exponential
    window. A shared deterministic base re-collides concurrent retriers in the
    same round; spreading each across the whole window desynchronises a 5+-way
    collision (empirically what closed it — see ``skill_runner._lock_retry_delay``)."""
    window = min(_BASE_DELAY * (2 ** attempt), _MAX_WINDOW)
    return random.uniform(0, window)


def _cross_process_gate(db_path: str) -> contextlib.AbstractContextManager:
    """Return the enterprise flock gate for *db_path*, or a nullcontext in OSS.

    Resolved once per process. The enterprise gate ships in ``olav-ent`` under
    ``olav.enterprise.db_write_gate``; its absence (plain OSS install) is the
    normal case and yields a no-op context.
    """
    global _gate_resolved, _gate_fn
    if not _gate_resolved:
        try:
            from olav.enterprise.db_write_gate import write_gate

            _gate_fn = write_gate
        except Exception:
            _gate_fn = None
        _gate_resolved = True
    if _gate_fn is None:
        return contextlib.nullcontext()
    return _gate_fn(db_path)


def _is_real_file_path(path: str) -> bool:
    # DuckDB special targets (``:memory:``, ``:default:``) have no parent dir.
    return not path.startswith(":")


def _connect_with_retry(path: str, *, read_only: bool) -> duckdb.DuckDBPyConnection:
    last_exc: Exception | None = None
    for attempt in range(_CONNECT_ATTEMPTS):
        try:
            return duckdb.connect(path, read_only=read_only)
        except Exception as exc:  # noqa: BLE001 — re-raised below if not retryable
            last_exc = exc
            if _is_lock_conflict(exc) and attempt < _CONNECT_ATTEMPTS - 1:
                time.sleep(_retry_delay(attempt))
                continue
            raise
    raise last_exc  # pragma: no cover — loop either returns or raises


@contextlib.contextmanager
def open_write_connection(
    db_path: str | Path,
    *,
    read_only: bool = False,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield a DuckDB connection for a write (or a gated read), serialised.

    * ``conn`` given — reuse it, do not lock or close (caller owns the
      transaction). This is the injection path for batch helpers.
    * ``read_only=True`` — no write gate; connect (with light retry for a
      transient lock) and yield. Caller must not write.
    * otherwise — acquire the per-db in-process lock, then the enterprise
      cross-process gate (nullcontext in OSS), connect with retry, and yield.
      The lock/gate is held for the whole ``with`` body so a multi-statement
      transaction is atomic against other writers.
    """
    # 1. Injected connection: reuse verbatim.
    if conn is not None:
        yield conn
        return

    path = str(db_path)
    if _is_real_file_path(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    # 2. Read-only: no serialisation gate needed.
    if read_only:
        c = _connect_with_retry(path, read_only=True)
        try:
            yield c
        finally:
            c.close()
        return

    # 3. Reentrant case: this thread already holds the gate for ``path`` (a
    # nested owning-open). Skip the lock/gate — the outer holder already
    # serialises us — and just hand out a fresh connection.
    held = _thread_held_paths()
    if path in held:
        c = _connect_with_retry(path, read_only=False)
        try:
            yield c
        finally:
            c.close()
        return

    # 4. Write: in-process lock + cross-process gate + connect-retry.
    with _process_lock(path):
        with _cross_process_gate(path):
            held.add(path)
            try:
                c = _connect_with_retry(path, read_only=False)
                try:
                    yield c
                finally:
                    c.close()
            finally:
                held.discard(path)
