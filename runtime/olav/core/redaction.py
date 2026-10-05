"""Collection-time credential redaction (network-config aware).

Wraps `netconan` (Intentionet, Batfish team) — a domain-aware
anonymizer that understands Cisco IOS / IOS-XR / NX-OS, Junos, Arista
EOS and a long tail of vendor config syntax.  Replaces passwords /
SNMP communities / RADIUS / TACACS / IPSec PSK / BGP MD5 keys and
similar credentials; leaves IPs / hostnames / ASNs / BGP relationships
/ interface names intact so downstream diagnostic + diff analysis
still works on the redacted output.

Design (dev_docs/75 + ADR-0008 forthcoming):

* **Where**:  redaction happens *at collection time* inside
  ``_collect_cmd`` (olav-netops/netops_init/run.py) before the row
  is appended to ``all_rows``.  staging.json + raw_output_store +
  parsed_outputs + exports/snapshots/<date>/raw/ — every disk
  location stores only the scrubbed text.  Process memory is the
  only place the raw bytes ever live.

* **What**:  scope deliberately narrow — passwords / communities /
  shared secrets only.  IP / hostname / MAC / ASN / topology edges
  preserved.  netconan's built-in 55 sensitive-item regex patterns
  + 5213 reserved-word allowlist drive the behaviour; we add zero
  hardcoded vocabulary on top, so operators can extend via
  ``api.json.redaction.extra_sensitive_words`` /
  ``extra_reserved_words`` instead of editing Python.

* **Salt**:  per-workspace HMAC-style salt at
  ``<workspace>/.redaction_salt`` (chmod 600, auto-generated if
  absent).  Same plaintext → same scrubbed token across snapshots,
  enabling cross-snapshot diff on credentials without revealing the
  plaintext.

* **Reversal**:  not by design.  netconan supports it but OLAV's
  current scope intentionally drops the mapping — plaintext lives
  only in process memory, never on disk.  Audit row records
  finding counts only.

* **Audit**:  per-scrub call returns a ``Findings`` object with
  category counts (passwords / communities / asns_left_alone /
  ips_left_alone).  Caller writes a kb_audit-shaped summary row.
"""
from __future__ import annotations

import io
import logging
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_SALT_FILENAME = ".redaction_salt"
_SALT_LENGTH_HEX = 64  # 32 bytes hex-encoded


class RedactionUnavailableError(RuntimeError):
    """Raised when redaction is required (strict mode) but netconan is absent.

    Lets callers fail-closed — refuse to write unredacted data — instead of
    silently degrading. Only raised when ``OLAV_REDACTION_STRICT`` (or
    ``cfg["strict"]``) is set AND redaction is enabled AND netconan is missing.
    """


# Module-level guard so the "netconan missing" SECURITY warning is emitted
# ONCE per process, not once per scrubbed command (scrub() is called per-command
# during collection — see netops_init/run.py — so an un-deduped warning would
# either flood the log or be ignored, the practical definition of "silent").
_NETCONAN_MISSING_WARNED = False


@dataclass
class Findings:
    """Per-scrub audit counts (no plaintext retained)."""
    total_replacements: int = 0
    category_counts: dict[str, int] = field(default_factory=dict)
    salt_fingerprint: str = ""
    degraded: bool = False  # True when redaction did NOT run (netconan missing
    #                         or failed) and the text was written UNREDACTED.

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_replacements": self.total_replacements,
            "category_counts": dict(self.category_counts),
            "salt_fingerprint": self.salt_fingerprint,
            "degraded": self.degraded,
        }


#: Key substrings whose values are secrets whatever they contain.
#:
#: Deliberately matched on the **key**, not the value. Everything else in this
#: module reads rendered text and has to guess from it — which is why it fights
#: a false-positive/false-negative trade-off, mangles prose, and misses
#: `aws_secret_access_key`. On structured data there is nothing to guess: the
#: field says what it is, so this has no failure mode in either direction.
#:
#: Conservative and over-broad on purpose: `secret_recipe` matches `secret` and
#: gets redacted. Losing a recipe from an audit row costs nothing; keeping a
#: credential costs everything.
#:
#: But substring matching alone over-reaches in one direction that *does* cost
#: something. `token` is a substring of `tokens_in`, `max_tokens`,
#: `prompt_tokens`, `token_count` — LLM accounting, present on nearly every run.
#: The first version of this redacted them, and on the audit **write** path that
#: is unrecoverable: the numbers are gone, not merely withheld. Caught by
#: exporting a real tenant and reading which cells changed
#: (`{"tokens_in": 10430}` → `{"tokens_in": "<redacted>"}`).
SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "token", "password", "api_key", "apikey", "secret",
    "credential", "passwd", "auth_header",
)

STRUCTURED_REDACTED = "<redacted>"


def _is_secret_key(key: str, value: Any) -> bool:
    """Whether *key* names a secret holding *value*.

    The value's **type** does most of the work: a credential is text, a count is
    a number. `{"max_tokens": 4096}` and `{"api_token": "sk-..."}` both match the
    same substring and are not the same thing, and nothing about the key alone
    separates them without a list of exceptions that would need maintaining.

    Non-text values are still redacted when the key *ends* with a sensitive word
    — `{"password": 1234}` is a numeric PIN, not an accounting field — so the
    type rule buys precision without giving up numeric secrets.
    """
    k = str(key).lower()
    if not any(sub in k for sub in SENSITIVE_KEY_SUBSTRINGS):
        return False
    if isinstance(value, (str, bytes)):
        return True
    return any(k.endswith(sub) for sub in SENSITIVE_KEY_SUBSTRINGS)


def redact_structure(value: Any, _depth: int = 0) -> Any:
    """Redact dict values whose key names a secret. Recurses; never raises.

    Distinct from `scrub`, which is netconan over *text*. This is for data that
    still has a schema — tool arguments, event payloads — and is applied before
    serialisation, because after `json.dumps` the schema is gone and only
    guessing is left.

    Lists and scalars pass through untouched: a bare string carries no key to
    judge it by. Unstructured text is `scrub`'s job, and the export pipeline's.
    """
    if _depth > 20:  # cyclic or pathological input; do not recurse forever
        return value
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if _is_secret_key(k, v):
                out[k] = STRUCTURED_REDACTED
            else:
                out[k] = redact_structure(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_structure(v, _depth + 1) for v in value]
    return value


def _workspace_salt(workspace_root: Path | None = None) -> str:
    """Read or auto-create the per-workspace redaction salt.

    Stored at ``<workspace>/.redaction_salt`` (chmod 600).  Auto-
    generated on first call from ``secrets.token_hex(32)``.  Lives
    inside the workspace so backups / migrations carry it; never
    appears in audit logs, just its 8-char sha256 prefix as a
    salt_fingerprint.
    """
    if workspace_root is None:
        workspace_root = Path(".olav/workspace")
    workspace_root.mkdir(parents=True, exist_ok=True)
    path = workspace_root / _SALT_FILENAME
    if not path.exists():
        salt = secrets.token_hex(32)
        path.write_text(salt, encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
        logger.info("redaction: generated new salt at %s (chmod 600)", path)
    return path.read_text(encoding="utf-8").strip()


def _salt_fingerprint(salt: str) -> str:
    import hashlib
    return hashlib.sha256(salt.encode("utf-8")).hexdigest()[:8]


def _load_config_overrides() -> dict[str, Any]:
    """Pull operator overrides from api.json — no hardcoded vocabulary.

    Expected schema (all optional)::

        "redaction": {
            "enabled": true,
            "anon_ip": false,
            "extra_sensitive_words": ["my-custom-key", "vendor-secret"],
            "extra_reserved_words":  ["internal-asn-name"],
        }

    Resolution: olav.core.config first; ``OLAV_REDACTION_*`` env
    overrides take precedence over JSON.  Missing config returns
    defaults (enabled=True, anon_ip=False, empty extra lists) so
    fresh installs get scrubbed without any setup ceremony.
    """
    cfg: dict[str, Any] = {
        "enabled": True,
        "anon_ip": False,
        "extra_sensitive_words": [],
        "extra_reserved_words": [],
    }
    try:
        from olav.core.config import get_config
        # get_config() returns a ConfigLoader, not a dict — the original
        # `full.get("redaction") if isinstance(full, dict)` was always False,
        # leaving the documented api.json redaction section dead (env-only).
        red = get_config().redaction or {}
        for k in list(cfg):
            if k in red:
                cfg[k] = red[k]
    except Exception:  # noqa: BLE001 — config layer optional / new install
        pass

    # Env overrides — last word
    env_flag = os.environ.get("OLAV_REDACTION")
    if env_flag is not None:
        cfg["enabled"] = env_flag.strip().lower() not in ("0", "false", "off", "no", "")
    env_anon_ip = os.environ.get("OLAV_REDACTION_ANON_IP")
    if env_anon_ip is not None:
        cfg["anon_ip"] = env_anon_ip.strip().lower() in ("1", "true", "on", "yes")
    return cfg


def scrub(
    text: str,
    *,
    workspace_root: Path | None = None,
    cfg: dict | None = None,
) -> tuple[str, Findings]:
    """Scrub credentials from arbitrary CLI / config text.

    Returns ``(scrubbed_text, Findings)``.  When redaction is
    disabled (``api.json.redaction.enabled=false`` or
    ``OLAV_REDACTION=0``), returns the input unchanged with empty
    Findings — fail-open is intentional so a misconfigured node
    doesn't lose collected data.

    On any netconan exception (corrupt input, library bug), logs at
    WARNING and returns the **input unchanged** — surface the
    failure rather than silently writing partial data to disk.
    Caller can decide whether to drop the row.

    When netconan is MISSING but redaction is enabled, the default is
    fail-open (returns input unchanged) but the returned
    ``Findings.degraded`` is ``True`` and a one-time ``⚠️ SECURITY``
    warning is logged — so an unredacted batch is auditable, not silent.
    Set ``OLAV_REDACTION_STRICT=1`` (or ``cfg["strict"]=True``) to
    fail-closed instead: a :class:`RedactionUnavailableError` is raised
    rather than writing plaintext.
    """
    if cfg is None:
        cfg = _load_config_overrides()
    if not cfg.get("enabled", True):
        return text, Findings()

    try:
        from netconan.anonymize_files import FileAnonymizer
    except ImportError:
        global _NETCONAN_MISSING_WARNED
        strict = bool(cfg.get("strict", False)) or os.environ.get(
            "OLAV_REDACTION_STRICT", ""
        ).strip().lower() in ("1", "true", "on", "yes")
        msg = (
            "redaction: netconan is NOT installed but redaction is ENABLED — "
            "credentials will be written UNREDACTED to disk. Install the "
            "[redaction] extra (pip install 'olav[redaction]'), or opt out "
            'explicitly with "redaction": {"enabled": false} in '
            ".olav/config/api.json (or OLAV_REDACTION=0), or set "
            "OLAV_REDACTION_STRICT=1 to fail-closed instead of writing plaintext."
        )
        if strict:
            # Fail-closed: refuse to silently write unredacted data.
            raise RedactionUnavailableError(msg) from None
        if not _NETCONAN_MISSING_WARNED:
            logger.warning("⚠️ SECURITY: %s", msg)
            _NETCONAN_MISSING_WARNED = True
        return text, Findings(degraded=True)

    salt = _workspace_salt(workspace_root)
    extra_sensitive = list(cfg.get("extra_sensitive_words") or [])
    extra_reserved = list(cfg.get("extra_reserved_words") or [])

    try:
        anonymizer = FileAnonymizer(
            anon_pwd=True,
            anon_ip=bool(cfg.get("anon_ip", False)),
            salt=salt,
            sensitive_words=extra_sensitive or None,
            reserved_words=extra_reserved or None,
        )
        src = io.StringIO(text)
        dst = io.StringIO()
        anonymizer.anonymize_io(src, dst)
        scrubbed = dst.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "⚠️ SECURITY: redaction: netconan failed (%s) — returning input "
            "UNREDACTED to surface the failure rather than silently mis-scrubbing. "
            "Findings.degraded=True so the caller can flag/drop this batch.",
            exc,
        )
        return text, Findings(degraded=True)

    # Findings: count distinct replacement tokens netconan emits.
    # netconan's replacements look like ``netconanRemoved<N>`` for
    # sensitive-word matches and rewritten hex strings for password
    # hashes.  We just count the marker as a lower bound + report
    # the total length delta for transparency.
    import re
    replacement_markers = re.findall(r"netconanRemoved\d+", scrubbed)
    findings = Findings(
        total_replacements=len(replacement_markers),
        category_counts={"sensitive_words": len(replacement_markers)},
        salt_fingerprint=_salt_fingerprint(salt),
    )
    return scrubbed, findings


__all__ = [
    "Findings",
    "SENSITIVE_KEY_SUBSTRINGS",
    "STRUCTURED_REDACTED",
    "redact_structure",
    "scrub",
]
