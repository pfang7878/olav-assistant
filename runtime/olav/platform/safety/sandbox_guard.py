"""sandbox_guard.py — Pre-execution scan for external write/delete operations.

Platform design principle: olav is a read-only platform.
Sandbox code may freely perform local filesystem operations, but any operation
that mutates an external system (HTTP mutations, DB writes) requires approval.

Allowed without approval:
  - Local filesystem: open(..., 'w'), os.remove(), shutil.rmtree(), etc.
  - HTTP GET/HEAD reads
  - DuckDB read_only=True connections + SELECT queries

Requires approval:
  - HTTP mutations: httpx/requests DELETE/POST/PUT/PATCH
  - service_call() with write methods
  - DuckDB mutations: DELETE/DROP/INSERT/UPDATE/TRUNCATE/CREATE/ALTER in .execute()
  - curl/wget with write methods in subprocess calls

Reference: dev_docs/18. SANDBOX_TOOL_REFACTOR.md §security
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from olav.platform.safety.permissions import is_bypass_active


@dataclass(frozen=True)
class ApprovalResult:
    requires_approval: bool
    hard_block: bool = False
    reason: str | None = None
    matched_pattern: str | None = None
    suggested_action: str = field(default="")

    def __post_init__(self) -> None:
        if self.hard_block and not self.suggested_action:
            object.__setattr__(
                self,
                "suggested_action",
                "This operation is permanently blocked under network isolation. "
                "Use a different agent or generate a changeset script instead.",
            )
        elif self.requires_approval and not self.suggested_action:
            object.__setattr__(
                self,
                "suggested_action",
                (
                    "This sandbox operation targets an external system and requires "
                    "operator approval. Confirm with the user before proceeding, or "
                    "use a read-only alternative."
                ),
            )


# ---------------------------------------------------------------------------
# Detection patterns (reason, compiled_pattern)
# ---------------------------------------------------------------------------

_HTTP_WRITE_METHODS = r"(?:delete|post|put|patch)"

_SCAN_RULES: list[tuple[str, re.Pattern]] = [
    # httpx.delete(...) / httpx.post(...) etc.
    (
        "HTTP mutation via httpx",
        re.compile(
            rf"httpx\s*\.\s*{_HTTP_WRITE_METHODS}\s*\(", re.IGNORECASE
        ),
    ),
    # requests.delete(...) etc.
    (
        "HTTP mutation via requests",
        re.compile(
            rf"requests\s*\.\s*{_HTTP_WRITE_METHODS}\s*\(", re.IGNORECASE
        ),
    ),
    # client.request("DELETE", ...) / client.request("POST", ...)
    (
        "HTTP mutation via client.request",
        re.compile(
            r'client\s*\.\s*request\s*\(\s*["\'](?:DELETE|POST|PUT|PATCH)',
            re.IGNORECASE,
        ),
    ),
    # httpx.Client().delete/post/put/patch
    (
        "HTTP mutation via httpx.Client method",
        re.compile(
            rf'(?:client|_client|http_client)\s*\.\s*{_HTTP_WRITE_METHODS}\s*\(',
            re.IGNORECASE,
        ),
    ),
    # service_call("svc", "DELETE"/"POST"/"PUT"/"PATCH", ...)
    (
        "External API mutation via service_call",
        re.compile(
            r'service_call\s*\(\s*[^,]+,\s*["\'](?:DELETE|POST|PUT|PATCH)',
            re.IGNORECASE,
        ),
    ),
    # subprocess curl/wget with write methods
    (
        "HTTP mutation via subprocess curl/wget",
        re.compile(
            r'(?:curl|wget)\b.*(?:-X\s+(?:DELETE|POST|PUT|PATCH)|--request\s+(?:DELETE|POST|PUT|PATCH))',
            re.IGNORECASE,
        ),
    ),
    # .execute("DELETE ...")  .execute("DROP ...")  etc.  (DB mutation)
    # Excludes SELECT/EXPLAIN/DESCRIBE
    (
        "Database mutation in .execute()",
        re.compile(
            r'\.execute\s*\(\s*["\'](?:DELETE|DROP|INSERT|UPDATE|TRUNCATE|CREATE|ALTER)',
            re.IGNORECASE,
        ),
    ),
    # .execute(f"DELETE ...") or .execute(f'DROP ...')  (f-string variant)
    (
        "Database mutation in .execute() with f-string",
        re.compile(
            r'\.execute\s*\(\s*f["\'](?:DELETE|DROP|INSERT|UPDATE|TRUNCATE|CREATE|ALTER)',
            re.IGNORECASE,
        ),
    ),
]

# Patterns that look like DB mutations but are actually safe (read_only connection
# followed by SELECT). We whitelist .execute() calls that only contain SELECT.
# We do NOT whitelist here — instead we check _SAFE_OVERRIDES after the scan.

# Explicit safe overrides: if the full code matches these, never flag
_SAFE_OVERRIDES: list[re.Pattern] = [
    # duckdb.connect(..., read_only=True) — user explicitly requested read-only
    re.compile(r'duckdb\.connect\s*\([^)]*read_only\s*=\s*True', re.IGNORECASE),
]


def scan_sandbox_code(code: str, *, network_isolation: bool = False) -> ApprovalResult:
    """Scan sandbox code for external write/delete operations.

    Args:
        code:              Python source to scan.
        network_isolation: When True, HTTP mutations are **hard-blocked** (not
                           approvable) because the sandbox runs in a network-
                           isolated namespace.  This prevents LLM-hallucinated
                           write calls from ever reaching an approval gate.

    Returns:
        ApprovalResult(requires_approval=False)              — safe to execute
        ApprovalResult(requires_approval=True)               — needs operator approval
        ApprovalResult(requires_approval=True, hard_block=True) — permanently blocked

    Design:
        - Local filesystem operations (open, os.remove, shutil.rmtree) are ALLOWED
        - HTTP mutations and DB mutations targeting external systems are FLAGGED
        - If the code explicitly uses read_only=True DuckDB connections, it is SAFE
        - Under network_isolation=True, HTTP mutations are hard-blocked (not bypassable)
    """
    if not code or not code.strip():
        return ApprovalResult(requires_approval=False)

    # bypass mode — all sandbox code is allowed (but NOT hard_block — that is never bypassable)
    bypass = is_bypass_active()

    # Check safe overrides first — if the code has explicit read_only=True,
    # the DB mutation patterns below won't apply to that connection
    has_readonly_conn = any(p.search(code) for p in _SAFE_OVERRIDES)

    for reason, pattern in _SCAN_RULES:
        if "Database mutation" in reason and has_readonly_conn:
            continue  # explicit read_only connection — trust the user
        if pattern.search(code):
            # Under network_isolation, HTTP mutations are hard-blocked
            if network_isolation and "HTTP mutation" in reason:
                return ApprovalResult(
                    requires_approval=True,
                    hard_block=True,
                    reason=f"HARD BLOCK: {reason} — network_isolation=True forbids HTTP mutations",
                    matched_pattern=pattern.pattern,
                )
            # Normal mode: skip if bypass active
            if bypass:
                continue
            return ApprovalResult(
                requires_approval=True,
                reason=reason,
                matched_pattern=pattern.pattern,
            )

    return ApprovalResult(requires_approval=False)
