"""Post-parse field normalizer (R72, INGEST-NORMALIZATION).

Canonicalises common network field types immediately after
``parse_output`` produces structured records. Runs once at ingest
time so all downstream consumers (view_builder, agent SQL queries,
sim/lab code) see a consistent shape.

Normalisations applied:

* **Interface names** — via ``netutils.interface.canonical_interface_name``.
  ``Gi0/0`` → ``GigabitEthernet0/0``, ``Et0/0`` → ``Ethernet0/0``, etc.
  Junos names (``ge-0/0/0``) pass through unchanged.
* **IP addresses** — via ``ipaddress.ip_address``. ``10.00.00.1`` →
  ``10.0.0.1``; IPv6 compressed; mapped forms collapsed.
* **ASNs** — via ``netutils.asn.asn_to_int``. ``1.1`` → ``65537``;
  ``"65001"`` → ``65001``.
* **MAC addresses** — via ``netutils.mac.mac_to_format("COMMON")``.
  ``AA:BB:CC:DD:EE:FF`` → ``aa:bb:cc:dd:ee:ff``.

Field-name heuristics guard against mis-normalisation:

* Interface: field name contains ``interface``, ``port``, ``intf``,
  ``neighbor_interface``, ``local_interface``
* IP: field name contains ``ip``, ``address``, ``router_id``, ``neighbor_ip``
* ASN: field name contains ``as`` (with word boundary)
* MAC: field name contains ``mac``, ``hwaddr``, ``chassis_id``

Value format is *also* validated before canonicalising — a field named
``ip_mtu`` with value ``1500`` is NOT treated as an IP address.
"""

from __future__ import annotations

import logging
import re
from ipaddress import ip_address, AddressValueError
from typing import Any

logger = logging.getLogger(__name__)

# ── Field-name heuristics (case-insensitive; underscores are word-safe) ──
# Python's \b treats `_` as a word char, so we can't rely on \b to split
# snake_case. Each pattern below is written to match bare-word tokens
# either at the ends of the field name or around `_` separators.

def _has_token(pattern: re.Pattern[str], name: str) -> bool:
    """Substring-match for snake_case-friendly tokens."""
    return pattern.search(name) is not None


_IFACE_HINT = re.compile(
    r"(?:^|_)(interface|port|intf|link)(?:$|_)",
    re.IGNORECASE,
)
_IP_HINT = re.compile(
    r"(?:^|_)("
    r"ip|ipv4|ipv6|address|router_id|neighbor_ip|peer_ip|"
    r"destination|source|next_hop|local_ip|remote_ip|gateway|loopback|"
    r"source_ip|dest_ip|src_ip|dst_ip|mgmt_ip|management_ip|ip_address"
    r")(?:$|_)",
    re.IGNORECASE,
)
_ASN_HINT = re.compile(
    r"(?:^|_)(asn|as_number|remote_as|local_as|peer_as|as)(?:$|_)",
    re.IGNORECASE,
)
_MAC_HINT = re.compile(
    r"(?:^|_)(mac|hwaddr|chassis_id|physical_address|mac_address)(?:$|_)",
    re.IGNORECASE,
)
# R83.2 + R83.4-followup: state-like fields canonicalised to RFC/IEEE
# names.  Triggers on common state field-name tokens — ``state`` /
# ``status`` cover BGP/OSPF/interface; ``role`` covers STP port role;
# ``admin`` / ``oper`` cover ifAdminStatus / ifOperStatus columns
# (e.g. ``v_show_interfaces_terse_auto.admin_state``).  Value-shape
# gate below filters out free-form messages.
_STATE_HINT = re.compile(
    r"(?:^|_)(state|status|role|admin|oper)(?:$|_)",
    re.IGNORECASE,
)
# State values are short enum-like tokens.  Allow space (for
# "administratively down") and trailing whitespace from parser bugs.
# Free-form messages > 40 chars after strip fall through.
_STATE_VALUE_RE = re.compile(r"^[A-Za-z0-9/_+\- ]{1,40}$")

# ── Value-format validators ─────────────────────────────────────────────
# A reasonable looking IPv4/IPv6 (before ipaddress does the authoritative parse)
_IP_LIKE_RE = re.compile(
    r"^(\d{1,3}(?:\.\d{1,3}){3}(?:/\d+)?|[0-9a-fA-F:]+(?:/\d+)?)$"
)
_ASN_LIKE_RE = re.compile(r"^(\d+|\d+\.\d+)$")
_MAC_LIKE_RE = re.compile(
    r"^([0-9a-fA-F]{2}[:.-]){5}[0-9a-fA-F]{2}$"
    r"|^[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}$"
    r"|^[0-9a-fA-F]{12}$"
)


def _canonical_interface(val: str) -> str | None:
    try:
        from netutils.interface import canonical_interface_name
        return canonical_interface_name(val)
    except Exception as exc:
        logger.debug("canonical_interface_name(%r) failed: %s", val, exc)
        return None


def _canonical_ip(val: str) -> str | None:
    # Strip optional /prefix-length for now — preserve original if it had one
    # but we canonicalise the address portion.
    has_prefix = "/" in val
    addr_part, _, prefix_part = val.partition("/")
    try:
        canon = str(ip_address(addr_part))
    except (AddressValueError, ValueError):
        return None
    return f"{canon}/{prefix_part}" if has_prefix else canon


def _canonical_asn(val: str) -> int | str | None:
    try:
        from netutils.asn import asn_to_int
        return asn_to_int(val)
    except Exception:
        # Fallback: simple int cast for plain numerals
        try:
            return int(val)
        except (TypeError, ValueError):
            return None


def _canonical_mac(val: str) -> str | None:
    try:
        from netutils.mac import mac_to_format
        return mac_to_format(val, "MAC_COLON_TWO")
    except Exception as exc:
        logger.debug("mac_to_format(%r) failed: %s", val, exc)
        return None


# RFC-canonical protocol state names.  Vendor textfsm output varies
# (Cisco "Estab"/"Established"/"0", Junos "Establ", Arista "OpenSent")
# — collapsing at ingest means downstream views and agent SQL never
# need ``state IN ('Established', 'Estab', '0', ...)`` clauses.
#
# Rules below are *prefix matches on the lowercased value*: e.g.
# ``Full/DR`` keeps the ``/DR`` suffix because it's RFC-correct OSPF.
_STATE_RULES: tuple[tuple[str, str], ...] = (
    # ── BGP — RFC 4271 §8 ─────────────────────────────────
    ("estab",       "Established"),    # ntc-templates truncates to 5 chars
    ("establ",      "Established"),    # …or 6
    ("established", "Established"),
    ("idle",        "Idle"),
    ("active",      "Active"),
    ("connect",     "Connect"),        # exact match only — see "connected" below
    ("opensent",    "OpenSent"),
    ("openconfirm", "OpenConfirm"),
    # ── OSPF neighbour — RFC 2328 §10 ─────────────────────
    ("full",        "Full"),           # may carry /DR or /BDR suffix (handled by prefix pass)
    ("2way",        "2-Way"),
    ("2-way",       "2-Way"),
    ("exstart",     "ExStart"),
    ("exchange",    "Exchange"),
    ("loading",     "Loading"),
    ("init",        "Init"),
    ("attempt",     "Attempt"),
    # ── ifOperStatus / ifAdminStatus — RFC 2863 §3 (lowercase MIB names) ──
    ("up",                    "up"),
    ("down",                  "down"),
    ("connected",             "up"),     # cisco show interfaces status
    ("notconnect",            "down"),
    ("notconnected",          "down"),
    ("disabled",              "admin-down"),
    ("err-disabled",          "err-disabled"),
    ("err disabled",          "err-disabled"),
    ("administratively down", "admin-down"),
    ("admin down",            "admin-down"),
    ("admin-down",            "admin-down"),
    ("testing",               "testing"),
    ("dormant",               "dormant"),
    # ── STP port role — IEEE 802.1D ──────────────────────
    ("desg",       "designated"),
    ("designated", "designated"),
    ("root",       "root"),
    ("altn",       "alternate"),
    ("alternate",  "alternate"),
    ("back",       "backup"),
    ("backup",     "backup"),
    # ── STP port state — IEEE 802.1D / 802.1w ────────────
    ("fwd",        "forwarding"),
    ("forwarding", "forwarding"),
    ("lrn",        "learning"),
    ("learning",   "learning"),
    ("lis",        "listening"),
    ("listening",  "listening"),
    ("blk",        "blocking"),
    ("blocking",   "blocking"),
    ("dis",        "disabled"),
    # ── OSPF interface state — RFC 2328 §9 ───────────────
    ("loop",    "loopback"),
    ("dr",      "dr"),                  # designated router (lowercase MIB)
    ("bdr",     "bdr"),
    ("dother",  "dother"),
    ("waiting", "waiting"),
    ("p2p",     "point-to-point"),
    ("p-2-p",   "point-to-point"),
)


def _canonical_state(val: str) -> str | None:
    """Map vendor variants to a canonical RFC/IEEE state.

    Two-pass match:

    1. **Exact match** (lowercased, trimmed) — handles the conflict
       cases like ``connected`` (cisco interfaces status, → ``up``)
       without the BGP ``connect`` rule swallowing it via prefix.
    2. **Prefix match** — fires only when the suffix starts with ``/``
       or ``-`` (OSPF ``Full/DR`` → ``Full/DR``).  Without this guard,
       prefix matching corrupts unrelated tokens (R83.2 had this bug:
       ``connected`` was mangled to ``Connect`` because rule order put
       BGP ``connect`` before any interface-status rule).

    Handles:

    * ``"Estab"`` / ``"Established"`` / ``"established"`` → ``"Established"``
    * ``"connected"`` / ``"CONNECTED"``                   → ``"up"``         (cisco)
    * ``"notconnect"``                                    → ``"down"``       (cisco)
    * ``"FULL"`` / ``"Full"`` / ``"full"``               → ``"Full"``
    * ``"FULL/DR"``                                       → ``"Full/DR"``    (designated form preserved)
    * ``"FWD"`` / ``"Forwarding"``                        → ``"forwarding"`` (STP)
    * ``"administratively down"``                         → ``"admin-down"``
    """
    if not val:
        return None
    s = val.strip()
    lower = s.lower()
    if not lower:
        return None
    # Pass 1 — exact match (covers most cases including the conflict
    # ones like "connected" vs "connect")
    for key, canonical in _STATE_RULES:
        if lower == key:
            return canonical
    # Pass 2 — prefix match for suffixed states only ("Full/DR" / "Full-BDR")
    for key, canonical in _STATE_RULES:
        if lower.startswith(key) and len(lower) > len(key):
            suffix = s[len(key):]
            if suffix and suffix[0] in "/-":
                return canonical + suffix
    return None


def _normalize_value(field_name: str, value: Any) -> Any:
    """Return canonicalised value when a rule applies; else ``value`` unchanged."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        # Whitespace-only collapses to empty string (e.g. parser leaves
        # `' '` from an unfilled column — agents filter on `WHERE col=''`,
        # not `WHERE col=' '`).  This is the only case where we *replace*
        # the original; sentinel "none"/"n/a" are preserved verbatim.
        return ""
    if s.lower() in {"unassigned", "none", "n/a", "-", "--"}:
        return value

    name_l = field_name

    # State / status / role / admin / oper — checked FIRST when value
    # matches state-enum shape, because some field names match both
    # ``_STATE_HINT`` and ``_IFACE_HINT`` (e.g. ``link_state`` contains
    # both ``link`` and ``state``).  netutils ``canonical_interface_name``
    # returns short tokens like ``"Up"`` unchanged (truthy), which would
    # otherwise win and bypass state normalization.  State values are
    # enum-shaped (≤40 chars, no slash-style interface notation), so
    # checking state first is safe — falls through to iface if no match.
    if _STATE_HINT.search(name_l) and _STATE_VALUE_RE.match(s):
        canon = _canonical_state(s)
        if canon:
            return canon

    # Interface name
    if _IFACE_HINT.search(name_l):
        canon = _canonical_interface(s)
        if canon:
            return canon

    # IP address — value must look IP-shaped
    if _IP_HINT.search(name_l) and _IP_LIKE_RE.match(s):
        canon = _canonical_ip(s)
        if canon:
            return canon

    # ASN — numeric or asdot
    if _ASN_HINT.search(name_l) and _ASN_LIKE_RE.match(s):
        canon = _canonical_asn(s)
        if canon is not None:
            return canon

    # MAC
    if _MAC_HINT.search(name_l) and _MAC_LIKE_RE.match(s):
        canon = _canonical_mac(s)
        if canon:
            return canon

    # Universal cleanup: trim trailing/leading whitespace on string
    # values when no canonical rule matched.  Covers parser bugs like
    # ``'P2p '`` (trailing space from spanning-tree TextFSM output).
    # Returning ``s`` instead of ``value`` is safe because we already
    # short-circuit for free-form sentinels at top of function.
    if s != value:
        return s
    return value


def normalize_fields(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonicalise interface/IP/ASN/MAC fields across all rows in-place.

    Returns the same list (mutated) for callers that want chaining.
    """
    if not rows:
        return rows
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, val in list(row.items()):
            # Lists of IPs / interfaces (TextFSM `Value List`)
            if isinstance(val, list):
                row[key] = [_normalize_value(key, item) for item in val]
            else:
                row[key] = _normalize_value(key, val)
    return rows
