"""ARCH-14 P1 L1: ``NetworkModel`` lazy proxy over ``netops.topology_links``.

See ``dev_docs/00. issues.md::ISSUE-ARCH-14`` for the full staged design
(L1 physical → L2 → L3 → L4 policy). Round 52 ships L1 only; L2+ layers
are exposed as attribute stubs that raise ``NotImplementedError`` with a
clear follow-on-round pointer so callers can discover the full API
surface now and swap in real implementations later without signature
drift.

Layer materialisation is lazy — constructing a ``NetworkModel`` does NO
DuckDB work, so introspection / tool-help flows stay fast. The first
attribute access for a given layer opens a *read-only* connection,
builds the layer, and caches the result on the instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ──────────────────────────────────────────────────────────────────────────
# L3 proxy — bundles lazy OSPF + BGP layers
# ──────────────────────────────────────────────────────────────────────────
# Historical note: rounds 52-55 hosted a ``_LayerStub`` base for unshipped
# layers. By Round 56 every advertised layer is real; the base stub got
# deleted rather than left as dead code. If a future expansion adds a
# layer that should stage as a stub first, restore the pattern from git
# history (commit trail referenced in dev_docs/00. issues.md::ISSUE-ARCH-14).


class _L3Proxy:
    """``model.l3`` — proxy exposing lazy ospf + bgp layers.

    Holds a back-reference to the parent ``NetworkModel`` so each L3
    layer can materialise against the model's snapshot / scope / db_path
    without duplicate plumbing. Both OSPF (Round 53) and BGP (Round 54)
    are real; subsequent L3 concepts (route-table, label distribution,
    etc.) can slot in with the same lazy-cache pattern.
    """

    def __init__(self, parent: "NetworkModel") -> None:
        self._parent = parent
        self._ospf: OspfLayer | None = None
        self._bgp: BgpLayer | None = None

    @property
    def ospf(self) -> "OspfLayer":
        if self._ospf is None:
            self._ospf = _build_ospf_layer(
                self._parent._resolve_db_path(),
                self._parent.snapshot,
                self._parent.scope,
            )
        return self._ospf

    @property
    def bgp(self) -> "BgpLayer":
        if self._bgp is None:
            self._bgp = _build_bgp_layer(
                self._parent._resolve_db_path(),
                self._parent.snapshot,
                self._parent.scope,
            )
        return self._bgp


# ──────────────────────────────────────────────────────────────────────────
# L1 physical layer — real implementation
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class OspfLayer:
    """OSPF neighbor adjacencies derived from ``netops.parsed_outputs``.

    ARCH-14 P1 L3 (Round 53). Reads rows where
    ``command ILIKE '%ospf neighbor%'`` for the active snapshot, unpacks
    the parsed JSON array per device, and projects into both a raw
    ``adjacencies`` list and a ``networkx.Graph`` whose nodes are
    device hostnames + OSPF neighbor router-ids.

    Each adjacency carries ``device`` (the device that *reported* the
    neighbor), ``neighbor_id`` (OSPF router-id), ``neighbor_ip``,
    ``interface``, and ``state`` (e.g. ``FULL`` / ``2WAY`` / ``INIT``)
    so downstream callers can filter on health without re-querying.
    """

    adjacencies: list[dict[str, Any]] = field(default_factory=list)
    graph: Any = None  # networkx.Graph

    def neighbors(self, device: str) -> list[str]:
        """Return the list of OSPF neighbor router-ids for ``device``."""
        if self.graph is None:
            return []
        try:
            return sorted(self.graph.neighbors(device))
        except Exception:
            return []

    def devices(self) -> list[str]:
        """Hostnames that reported at least one OSPF neighbor."""
        seen = {a["device"] for a in self.adjacencies if a.get("device")}
        return sorted(seen)

    def unhealthy(self) -> list[dict[str, Any]]:
        """Subset of adjacencies where ``state`` is not FULL/2WAY.

        Useful for ``model.l3.ospf.unhealthy()`` as a quick "what's
        broken" roll-up without re-querying.
        """
        healthy = {"FULL", "2WAY"}
        return [
            a for a in self.adjacencies
            if str(a.get("state", "")).upper() not in healthy
        ]


def _build_ospf_layer(
    db_path: Path,
    snapshot: str | None,
    scope: list[str] | None,
) -> OspfLayer:
    """Materialise the OSPF layer by querying ``parsed_outputs``.

    Defensive defaults mirror the physical layer: bad DB path / empty
    result set yields an empty ``OspfLayer``, never raises.
    """
    try:
        import duckdb  # noqa: PLC0415
        import networkx as nx  # noqa: PLC0415
    except Exception:
        return OspfLayer(adjacencies=[], graph=None)

    try:
        conn = duckdb.connect(str(db_path), read_only=True)
    except Exception:
        return OspfLayer(adjacencies=[], graph=None)

    try:
        # Resolve snapshot — explicit id or MAX(snapshot_id) from
        # parsed_outputs. Using parsed_outputs (not topology_links) to
        # align the snapshot selection with where the OSPF data lives.
        if snapshot:
            target_snap = snapshot
        else:
            try:
                row = conn.execute(
                    "SELECT MAX(snapshot_id) FROM netops.parsed_outputs "
                    "WHERE command ILIKE '%ospf neighbor%'"
                ).fetchone()
                target_snap = row[0] if row and row[0] else None
            except Exception:
                target_snap = None

        if target_snap is None:
            return OspfLayer(adjacencies=[], graph=None)

        where = [
            "command ILIKE '%ospf neighbor%'",
            "snapshot_id = ?",
            "parsed_data IS NOT NULL",
        ]
        params: list[Any] = [target_snap]
        if scope:
            placeholders = ",".join(["?"] * len(scope))
            where.append(f"device_name IN ({placeholders})")
            params.extend(scope)

        sql = (
            "SELECT device_name, parsed_data::VARCHAR AS parsed_data "
            "FROM netops.parsed_outputs "
            "WHERE " + " AND ".join(where)
        )
        try:
            cur = conn.execute(sql, params)
            raw_rows = cur.fetchall()
        except Exception:
            raw_rows = []

        import json as _json  # noqa: PLC0415
        adjacencies: list[dict[str, Any]] = []
        for device_name, parsed_text in raw_rows:
            if not parsed_text:
                continue
            try:
                parsed = _json.loads(parsed_text)
            except Exception:
                continue
            if not isinstance(parsed, list):
                continue
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                neighbor_id = (
                    entry.get("neighbor_id")
                    or entry.get("NEIGHBOR_ID")
                    or entry.get("router_id")
                )
                if not neighbor_id:
                    continue
                adjacencies.append({
                    "device": device_name,
                    "neighbor_id": str(neighbor_id),
                    "neighbor_ip": (
                        entry.get("neighbor_ip")
                        or entry.get("address")
                        or entry.get("ADDRESS")
                    ),
                    "interface": (
                        entry.get("interface")
                        or entry.get("INTERFACE")
                    ),
                    "state": (
                        entry.get("state")
                        or entry.get("STATE")
                        or ""
                    ),
                })

        g = nx.Graph()
        for adj in adjacencies:
            g.add_edge(
                adj["device"],
                adj["neighbor_id"],
                neighbor_ip=adj.get("neighbor_ip"),
                interface=adj.get("interface"),
                state=adj.get("state"),
            )
        return OspfLayer(adjacencies=adjacencies, graph=g)
    finally:
        try:
            conn.close()
        except Exception:
            pass


@dataclass
class L2Layer:
    """VLAN memberships derived from ``netops.parsed_outputs``.

    ARCH-14 P1 L2 (Round 55). Reads rows where
    ``command ILIKE '%vlan%'`` (catches ``show vlan`` on Cisco IOS,
    ``show vlan brief`` on NX-OS, ``show vlans`` on Junos) for the
    active snapshot and unpacks the per-device parsed JSON array into
    :attr:`vlans` — ``{device, vlan_id, name, interfaces}``.

    Vendor field normalisation:

    * Cisco IOS / NX-OS / Arista: ``vlan_id`` / ``name`` / ``interfaces``
    * Junos: ``vlan_name`` / ``tag`` (numeric id) / ``interfaces``
    * Mixed-case variants (``VLAN_ID`` / ``NAME``) handled.

    ``interfaces`` is always a list. Vendors that return a
    comma-separated string get split and trimmed so downstream callers
    can iterate uniformly.
    """

    vlans: list[dict[str, Any]] = field(default_factory=list)
    graph: Any = None  # networkx.Graph — device ↔ vlan-key edges

    def by_device(self, device: str) -> dict[str, list[str]]:
        """Return ``{vlan_id: [interfaces]}`` for ``device``."""
        out: dict[str, list[str]] = {}
        for v in self.vlans:
            if v.get("device") != device:
                continue
            vid = str(v.get("vlan_id") or "")
            if not vid:
                continue
            out.setdefault(vid, []).extend(v.get("interfaces") or [])
        return out

    def devices(self) -> list[str]:
        """Hostnames that reported at least one VLAN."""
        seen = {v["device"] for v in self.vlans if v.get("device")}
        return sorted(seen)

    def vlan_ids(self, device: str | None = None) -> list[str]:
        """Distinct VLAN ids. When ``device`` is supplied, restrict to
        that device; otherwise return every id seen across the model."""
        seen: set[str] = set()
        for v in self.vlans:
            if device is not None and v.get("device") != device:
                continue
            vid = str(v.get("vlan_id") or "")
            if vid:
                seen.add(vid)
        return sorted(seen, key=lambda x: (len(x), x))


def _normalise_interface_list(raw: Any) -> list[str]:
    """Coerce vendor interface-list field into a ``list[str]``.

    Cisco output is often already a list; Junos sometimes returns a
    single comma-separated string. Empty / missing yields ``[]`` — never
    ``None`` — so callers can iterate without guarding.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        # Split on comma OR whitespace — ntc_templates occasionally
        # returns a single space-separated bundle.
        parts = [s.strip() for s in raw.replace(",", " ").split()]
        return [p for p in parts if p]
    return []


def _build_l2_layer(
    db_path: Path,
    snapshot: str | None,
    scope: list[str] | None,
) -> L2Layer:
    """Materialise the L2 layer by querying ``parsed_outputs``.

    Same defensive contract as OSPF/BGP builders — bad DB path / empty
    result yields an empty ``L2Layer`` rather than raising. Graph nodes
    are device hostnames + per-vlan keys formatted ``VLAN:<id>`` so the
    device-side and vlan-side nodes cannot accidentally collide (a
    bare integer VLAN id could otherwise overlap a hostname like ``10``).
    """
    try:
        import duckdb  # noqa: PLC0415
        import networkx as nx  # noqa: PLC0415
    except Exception:
        return L2Layer(vlans=[], graph=None)

    try:
        conn = duckdb.connect(str(db_path), read_only=True)
    except Exception:
        return L2Layer(vlans=[], graph=None)

    try:
        if snapshot:
            target_snap = snapshot
        else:
            try:
                row = conn.execute(
                    "SELECT MAX(snapshot_id) FROM netops.parsed_outputs "
                    "WHERE command ILIKE '%vlan%'"
                ).fetchone()
                target_snap = row[0] if row and row[0] else None
            except Exception:
                target_snap = None

        if target_snap is None:
            return L2Layer(vlans=[], graph=None)

        where = [
            "command ILIKE '%vlan%'",
            "snapshot_id = ?",
            "parsed_data IS NOT NULL",
        ]
        params: list[Any] = [target_snap]
        if scope:
            placeholders = ",".join(["?"] * len(scope))
            where.append(f"device_name IN ({placeholders})")
            params.extend(scope)

        sql = (
            "SELECT device_name, parsed_data::VARCHAR AS parsed_data "
            "FROM netops.parsed_outputs "
            "WHERE " + " AND ".join(where)
        )
        try:
            cur = conn.execute(sql, params)
            raw_rows = cur.fetchall()
        except Exception:
            raw_rows = []

        import json as _json  # noqa: PLC0415
        vlans: list[dict[str, Any]] = []
        for device_name, parsed_text in raw_rows:
            if not parsed_text:
                continue
            try:
                parsed = _json.loads(parsed_text)
            except Exception:
                continue
            if not isinstance(parsed, list):
                continue
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                vlan_id = (
                    entry.get("vlan_id")
                    or entry.get("tag")        # Junos
                    or entry.get("VLAN_ID")
                    or entry.get("TAG")
                )
                if vlan_id is None or vlan_id == "":
                    continue
                name = (
                    entry.get("name")
                    or entry.get("vlan_name")  # Junos
                    or entry.get("NAME")
                    or entry.get("VLAN_NAME")
                    or ""
                )
                interfaces = _normalise_interface_list(
                    entry.get("interfaces")
                    or entry.get("INTERFACES")
                    or entry.get("ports")
                )
                vlans.append({
                    "device": device_name,
                    "vlan_id": str(vlan_id),
                    "name": str(name),
                    "interfaces": interfaces,
                })

        g = nx.Graph()
        for v in vlans:
            # Namespaced VLAN nodes prevent collision with hostnames.
            vlan_node = f"VLAN:{v['vlan_id']}"
            g.add_edge(
                v["device"], vlan_node,
                name=v.get("name"),
                interface_count=len(v.get("interfaces") or []),
            )
        return L2Layer(vlans=vlans, graph=g)
    finally:
        try:
            conn.close()
        except Exception:
            pass


@dataclass
class L4Layer:
    """Routing policy clauses extracted from running-config raw output.

    ARCH-14 P2 L4 (Round 56). This is the **read-only** slice —
    identifies which devices carry which policy names, and returns the
    raw clause bodies so operators can eyeball ``set``/``match``
    actions. Actual evaluation (``does route X pass R1→R2 out?``) is
    P3 and intentionally still raises ``NotImplementedError`` via
    :meth:`policy`.

    Scope:
        * Cisco IOS-style ``route-map NAME [permit|deny] SEQ`` stanzas
          extracted from ``netops.raw_output_store`` where the command
          looked like ``show running-config`` / ``show run``.
        * ``match`` lines collected verbatim (no semantic understanding)
        * ``set`` lines collected verbatim

    Out of scope (future rounds):
        * Junos-flavoured policy-statement syntax (different shape)
        * Prefix-list / community-list / as-path extraction
        * Policy evaluation against a concrete route (``P3``)
    """

    clauses: list[dict[str, Any]] = field(default_factory=list)
    graph: Any = None  # networkx.Graph — device ↔ "POLICY:<name>"
    # ARCH-14 P3 (Round 59): per-device map of BGP neighbor/direction →
    # route-map name. Populated from the same ``show running-config``
    # pass that fed ``clauses`` so a single layer materialisation covers
    # both extraction surfaces.
    # Shape: {device: {(neighbor_ip, direction): policy_name}}
    neighbor_policies: dict[str, dict[tuple[str, str], str]] = field(default_factory=dict)

    def by_device(self, device: str) -> dict[str, list[dict[str, Any]]]:
        """Return ``{policy_name: [clause, ...]}`` for ``device``.

        Clauses are returned in sequence-number order so callers can
        evaluate the policy the way the router does.
        """
        out: dict[str, list[dict[str, Any]]] = {}
        for c in self.clauses:
            if c.get("device") != device:
                continue
            out.setdefault(c["policy_name"], []).append(c)
        for name in out:
            out[name].sort(key=lambda e: (int(e.get("seq") or 0), e.get("policy_name") or ""))
        return out

    def by_policy(self, device: str, policy_name: str) -> list[dict[str, Any]]:
        """Return clauses for a single (device, policy_name) in seq order."""
        return self.by_device(device).get(policy_name, [])

    def policies(self, device: str | None = None) -> list[str]:
        """Distinct policy names. Scoped to one device when supplied."""
        seen: set[str] = set()
        for c in self.clauses:
            if device is not None and c.get("device") != device:
                continue
            name = c.get("policy_name")
            if name:
                seen.add(name)
        return sorted(seen)

    def devices(self) -> list[str]:
        """Hostnames that expose at least one policy clause."""
        seen = {c["device"] for c in self.clauses if c.get("device")}
        return sorted(seen)

    def walk(
        self,
        device: str,
        policy_name: str,
        matches: Any = None,
    ) -> dict[str, Any]:
        """Deterministic clause walker — ARCH-14 P3 building block (Round 58).

        Walks the named policy's clauses in sequence-number order (exactly
        the way the router evaluates them). For each clause, decides
        whether it "matches" by consulting the caller-supplied ``matches``
        argument:

        * ``dict[str, bool]`` — keyed by the verbatim ``match`` condition
          string (e.g. ``"ip address prefix-list CUST_PREFIXES"``).
          Missing keys are treated as ``False`` (condition not proven).
        * ``callable(condition_str) -> bool`` — richer hook; called once
          per match condition in the current clause.
        * ``None`` — treat every match condition as True (what-if "all
          conditions hold"); useful for "what set actions would this
          policy apply if it matched at all".

        Cisco semantics: a clause matches iff EVERY ``match`` line
        evaluates True. When there are no ``match`` lines, the clause is
        an unconditional match. A ``permit`` clause returns
        ``action="permit"`` and all its ``set`` rules as the cumulative
        transforms; a ``deny`` clause returns ``action="deny"`` with no
        set rules. If no clause matches, the walker returns
        ``action="implicit_deny"`` (Cisco's unconfigurable default).

        The returned trace lists every stanza the walker examined in
        order with its decision, so operators can eyeball exactly why a
        particular route would be permitted or denied. This is the
        foundational structure that both an LLM-as-interpreter path and
        a future hand-rolled mini-parser can emit; they differ only in
        how ``matches`` is computed.

        Args:
            device: Hostname to look up the policy on.
            policy_name: Route-map name (exact match on
                :attr:`clauses`.``policy_name``).
            matches: See docstring paragraph above.

        Returns:
            Dict with:
                * ``action`` — ``"permit"`` / ``"deny"`` / ``"implicit_deny"``
                * ``trace`` — list of ``{seq, action, matched, match_results,
                  set, reason}`` dicts, one per examined clause. A
                  truncation-safe representation of the walk.
                * ``sets`` — cumulative ``set`` actions applied by the
                  winning clause (empty list on deny / implicit_deny).
                * ``policy_name`` / ``device`` — echoed back for caller
                  round-tripping.
                * ``missing`` — ``True`` only when the policy is absent
                  on the device; callers should not misinterpret an
                  empty trace as implicit-deny when the root cause is
                  missing config.
        """
        clauses = self.by_policy(device, policy_name)
        base: dict[str, Any] = {
            "policy_name": policy_name,
            "device": device,
            "action": "implicit_deny",
            "sets": [],
            "trace": [],
            "missing": False,
        }
        if not clauses:
            base["missing"] = True
            return base

        for clause in clauses:
            match_rules = clause.get("match") or []
            match_results: dict[str, bool] = {}
            for cond in match_rules:
                match_results[cond] = _evaluate_match_condition(matches, cond)
            # Cisco semantics: all match lines must hold (AND). A clause
            # with zero match lines is unconditionally matched.
            clause_matches = all(match_results.values()) if match_results else True
            action = clause.get("action") or "permit"
            trace_entry = {
                "seq": clause.get("seq"),
                "action": action,
                "matched": clause_matches,
                "match_results": match_results,
                "set": list(clause.get("set") or []),
                "reason": _walk_reason(clause_matches, match_results, action),
            }
            base["trace"].append(trace_entry)
            if clause_matches:
                if action == "permit":
                    base["action"] = "permit"
                    base["sets"] = list(clause.get("set") or [])
                else:  # deny
                    base["action"] = "deny"
                    base["sets"] = []
                return base
        # No clause matched — Cisco's built-in implicit deny.
        return base

    def policy_for_neighbor(
        self,
        device: str,
        neighbor_ip: str,
        direction: str,
    ) -> str | None:
        """Resolve the route-map name attached to a BGP peer (ARCH-14 Round 59).

        Returns the policy name string or ``None`` when no binding is
        configured (which — on Cisco — means every route is permitted
        in that direction, not implicit-deny; callers should handle the
        None case distinctly from ``walk()``'s ``missing=True``).
        """
        by_device = self.neighbor_policies.get(device) or {}
        return by_device.get((neighbor_ip, direction))

    def policy(
        self,
        device: str,
        neighbor: str,
        direction: str,
        matches: Any = None,
    ) -> dict[str, Any]:
        """Evaluate the BGP route-map bound to (device, neighbor, direction).

        ARCH-14 P3 end-to-end entry point:

        1. Look up the configured route-map via
           :meth:`policy_for_neighbor` (parsed in Round 59 from the same
           running-config pass that populates :attr:`clauses`).
        2. Delegate to :meth:`walk` with the resolved policy_name and
           the caller-supplied ``matches`` map (dict / callable / None).

        Returns the walker's structured result plus extra top-level keys
        for observability:

        * ``neighbor`` / ``direction`` — echoed back for caller traces
        * ``policy_name`` — the resolved route-map name, or ``None`` if
          no binding is configured on this peer/direction
        * ``unbound`` — ``True`` when no route-map is attached. Cisco
          semantics: an unbound direction is implicitly permit-all, so
          ``action="permit"`` with empty ``sets`` / ``trace``.

        ``matches`` is forwarded to :meth:`walk` unchanged; see that
        method for the full shape contract.
        """
        policy_name = self.policy_for_neighbor(device, neighbor, direction)
        if policy_name is None:
            # Cisco: missing route-map binding means "permit all with no
            # transform" — not implicit-deny. Surface that distinctly so
            # operators don't confuse "no policy" with "empty policy".
            return {
                "policy_name": None,
                "device": device,
                "neighbor": neighbor,
                "direction": direction,
                "action": "permit",
                "sets": [],
                "trace": [],
                "missing": False,
                "unbound": True,
                "reason": (
                    "no route-map bound to this neighbor/direction — "
                    "Cisco default is permit-all"
                ),
            }

        walked = self.walk(device, policy_name, matches=matches)
        walked["neighbor"] = neighbor
        walked["direction"] = direction
        walked["unbound"] = False
        return walked


# ── Cisco IOS route-map regex ────────────────────────────────────────────
# Keeps the parser deliberately simple — runs line-by-line over the raw
# config. The regex captures the stanza header (route-map NAME permit SEQ)
# and subsequent indented match/set lines belong to the most recent
# header. Stanzas terminate on blank line / next top-level keyword.


_IOS_ROUTE_MAP_HEADER = __import__("re").compile(
    r"^route-map\s+(?P<name>\S+)\s+(?P<action>permit|deny)\s+(?P<seq>\d+)\s*$"
)


_IOS_BGP_NEIGHBOR_POLICY = __import__("re").compile(
    r"^\s*neighbor\s+(?P<peer>\S+)\s+route-map\s+(?P<policy>\S+)\s+"
    r"(?P<direction>in|out)\s*$"
)


def _extract_bgp_neighbor_policies_cisco_ios(
    config_text: str,
) -> list[dict[str, Any]]:
    """Scan a Cisco IOS running-config for BGP neighbor→route-map pairs.

    Each returned dict carries ``neighbor_ip``, ``policy_name``, and
    ``direction`` (``"in"`` / ``"out"``). Multiple route-maps per peer
    produce multiple rows (Cisco allows in + out on the same neighbor;
    redistribution contexts can also have route-maps but those are
    out-of-scope — we only catch the router-bgp stanza shape here).

    Matches a line like::

        neighbor 10.0.12.2 route-map EXPORT_CUSTOMERS out

    at any indent level — the ``router bgp ...`` header / ``address-family``
    scoping are not tracked because every supported deployment we care
    about uses unique policy names per peer+direction; multi-AF scoping
    is a genuinely larger parse job deferred to a future round.
    """
    out: list[dict[str, Any]] = []
    for line in (config_text or "").splitlines():
        m = _IOS_BGP_NEIGHBOR_POLICY.match(line)
        if not m:
            continue
        out.append({
            "neighbor_ip": m.group("peer"),
            "policy_name": m.group("policy"),
            "direction": m.group("direction"),
        })
    return out


def _evaluate_match_condition(matches: Any, cond: str) -> bool:
    """Resolve a single match-condition string via the caller-supplied
    ``matches`` argument passed to :meth:`L4Layer.walk`.

    Callable wins over dict-lookup so a caller can plug in an LLM-backed
    resolver; dict is the simple deterministic path; ``None`` treats
    every condition as True (what-if "all conditions hold").
    """
    if matches is None:
        return True
    if callable(matches):
        try:
            return bool(matches(cond))
        except Exception:
            return False
    if isinstance(matches, dict):
        return bool(matches.get(cond, False))
    # Unsupported shape — refuse conservatively. Callers that stray
    # outside ``None`` / dict / callable get a clean False rather than a
    # silent True (which could flip a deny into a permit).
    return False


def _walk_reason(matched: bool, match_results: dict[str, bool], action: str) -> str:
    """Human-readable explanation for a single trace entry."""
    if not match_results:
        return f"unconditional match → {action}" if matched else "skipped"
    if matched:
        return f"all match conditions held → {action}"
    failing = [c for c, ok in match_results.items() if not ok]
    return f"match failed on: {failing[0]}" if failing else "no-match"


def _extract_route_maps_cisco_ios(config_text: str) -> list[dict[str, Any]]:
    """Split a Cisco IOS-style config into route-map clause records.

    Each returned dict carries ``policy_name``, ``action`` (permit/deny),
    ``seq`` (int), ``match`` (list[str]), ``set`` (list[str]), and
    ``body`` (the verbatim clause lines joined with newlines — useful
    when the caller wants to show the source to a reviewer).
    """
    clauses: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        nonlocal current, current_lines
        if current is not None:
            current["body"] = "\n".join(current_lines).rstrip()
            clauses.append(current)
        current = None
        current_lines = []

    for raw_line in (config_text or "").splitlines():
        line = raw_line.rstrip()
        m = _IOS_ROUTE_MAP_HEADER.match(line)
        if m:
            _flush()
            current = {
                "policy_type": "route-map",
                "policy_name": m.group("name"),
                "action": m.group("action"),
                "seq": int(m.group("seq")),
                "match": [],
                "set": [],
            }
            current_lines = [line]
            continue
        if current is None:
            continue
        # Within a stanza: collect indented match/set lines. The stanza
        # ends when we hit a non-indented line that isn't blank — Cisco
        # routers print a single blank line between stanzas in most
        # configurations, but defensive detection catches either shape.
        if not line.strip():
            _flush()
            continue
        if not line.startswith(" ") and not line.startswith("\t"):
            _flush()
            # Re-examine this line as a potential new header.
            m2 = _IOS_ROUTE_MAP_HEADER.match(line)
            if m2:
                current = {
                    "policy_type": "route-map",
                    "policy_name": m2.group("name"),
                    "action": m2.group("action"),
                    "seq": int(m2.group("seq")),
                    "match": [],
                    "set": [],
                }
                current_lines = [line]
            continue
        stripped = line.strip()
        current_lines.append(line)
        if stripped.startswith("match "):
            current["match"].append(stripped[len("match "):])
        elif stripped.startswith("set "):
            current["set"].append(stripped[len("set "):])

    _flush()
    return clauses


def _build_l4_layer(
    db_path: Path,
    snapshot: str | None,
    scope: list[str] | None,
) -> L4Layer:
    """Materialise the L4 layer by scanning raw running-config output.

    Queries ``netops.raw_output_store`` for commands that look like a
    running-config dump (``show running-config`` / ``show run`` —
    ``show configuration`` on Junos is intentionally skipped until a
    Junos parser lands). Same defensive empty-layer contract on any DB
    failure.
    """
    try:
        import duckdb  # noqa: PLC0415
        import networkx as nx  # noqa: PLC0415
    except Exception:
        return L4Layer(clauses=[], graph=None)

    try:
        conn = duckdb.connect(str(db_path), read_only=True)
    except Exception:
        return L4Layer(clauses=[], graph=None)

    try:
        if snapshot:
            target_snap = snapshot
        else:
            try:
                row = conn.execute(
                    "SELECT MAX(snapshot_id) FROM netops.raw_output_store "
                    "WHERE command ILIKE '%running-config%' "
                    "   OR command ILIKE 'show run%'"
                ).fetchone()
                target_snap = row[0] if row and row[0] else None
            except Exception:
                target_snap = None

        if target_snap is None:
            return L4Layer(clauses=[], graph=None)

        where = [
            "(command ILIKE '%running-config%' OR command ILIKE 'show run%')",
            "snapshot_id = ?",
            "raw_output IS NOT NULL",
        ]
        params: list[Any] = [target_snap]
        if scope:
            placeholders = ",".join(["?"] * len(scope))
            where.append(f"device_name IN ({placeholders})")
            params.extend(scope)

        sql = (
            "SELECT device_name, raw_output "
            "FROM netops.raw_output_store "
            "WHERE " + " AND ".join(where)
        )
        try:
            cur = conn.execute(sql, params)
            raw_rows = cur.fetchall()
        except Exception:
            raw_rows = []

        clauses: list[dict[str, Any]] = []
        neighbor_policies: dict[str, dict[tuple[str, str], str]] = {}
        for device_name, raw_text in raw_rows:
            if not raw_text:
                continue
            text = str(raw_text)
            for clause in _extract_route_maps_cisco_ios(text):
                clauses.append({"device": device_name, **clause})
            # ARCH-14 Round 59: collect BGP neighbor → route-map pairs
            # in the same pass. Deliberately additive — an empty or
            # non-Cisco-flavoured config just yields zero rows here.
            for binding in _extract_bgp_neighbor_policies_cisco_ios(text):
                neighbor_policies.setdefault(device_name, {})[
                    (binding["neighbor_ip"], binding["direction"])
                ] = binding["policy_name"]

        g = nx.Graph()
        for c in clauses:
            # Namespace policy nodes the same way L2 does for VLANs so a
            # hostname can't collide with a policy name.
            g.add_edge(
                c["device"], f"POLICY:{c['policy_name']}",
                policy_type=c.get("policy_type"),
            )
        return L4Layer(
            clauses=clauses,
            graph=g,
            neighbor_policies=neighbor_policies,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


@dataclass
class BgpLayer:
    """BGP session state derived from ``netops.parsed_outputs``.

    ARCH-14 P1 L3 BGP (Round 54). Reads rows where
    ``command ILIKE '%bgp summary%'`` for the active snapshot, unpacks
    the parsed JSON array per device (``show ip bgp summary`` /
    ``show bgp summary`` / Junos ``show bgp summary``), and projects
    into:

    * :attr:`sessions` — ``{device, neighbor_ip, neighbor_as, state}``
    * :attr:`graph`    — ``networkx.Graph`` with devices + neighbor IPs
      as nodes (BGP peers are keyed by IP, not router-id, so we reuse
      the IP as the far-side identifier).

    BGP health uses a different vocabulary than OSPF — ``Established`` is
    the only steady-state healthy value; everything else (``Active``,
    ``Idle``, ``OpenSent``, ``OpenConfirm``, ``Connect``, numeric prefix
    count strings from Junos, etc.) is surfaced by :meth:`unhealthy`.
    """

    sessions: list[dict[str, Any]] = field(default_factory=list)
    graph: Any = None  # networkx.Graph

    def neighbors(self, device: str) -> list[str]:
        """Return the list of BGP neighbor IPs for ``device``."""
        if self.graph is None:
            return []
        try:
            return sorted(self.graph.neighbors(device))
        except Exception:
            return []

    def devices(self) -> list[str]:
        """Hostnames that reported at least one BGP session."""
        seen = {s["device"] for s in self.sessions if s.get("device")}
        return sorted(seen)

    def unhealthy(self) -> list[dict[str, Any]]:
        """Sessions whose state is NOT ``Established``.

        Note some vendors (Cisco on Established) put a numeric prefix
        count in the ``state`` field to save a column. We treat purely
        numeric strings as healthy too — an established session will
        show an integer prefix-received count rather than the literal
        ``Established``.
        """
        healthy_prefix = "established"
        out: list[dict[str, Any]] = []
        for s in self.sessions:
            state = str(s.get("state", "")).strip()
            if state.lower().startswith(healthy_prefix):
                continue
            # Numeric prefix count → Established in Cisco-style output.
            if state.replace(",", "").isdigit():
                continue
            out.append(s)
        return out


def _build_bgp_layer(
    db_path: Path,
    snapshot: str | None,
    scope: list[str] | None,
) -> BgpLayer:
    """Materialise the BGP layer by querying ``parsed_outputs``.

    Same defensive contract as the OSPF builder — bad DB path / empty
    result yields an empty ``BgpLayer`` rather than raising.
    """
    try:
        import duckdb  # noqa: PLC0415
        import networkx as nx  # noqa: PLC0415
    except Exception:
        return BgpLayer(sessions=[], graph=None)

    try:
        conn = duckdb.connect(str(db_path), read_only=True)
    except Exception:
        return BgpLayer(sessions=[], graph=None)

    try:
        if snapshot:
            target_snap = snapshot
        else:
            try:
                row = conn.execute(
                    "SELECT MAX(snapshot_id) FROM netops.parsed_outputs "
                    "WHERE command ILIKE '%bgp summary%'"
                ).fetchone()
                target_snap = row[0] if row and row[0] else None
            except Exception:
                target_snap = None

        if target_snap is None:
            return BgpLayer(sessions=[], graph=None)

        where = [
            "command ILIKE '%bgp summary%'",
            "snapshot_id = ?",
            "parsed_data IS NOT NULL",
        ]
        params: list[Any] = [target_snap]
        if scope:
            placeholders = ",".join(["?"] * len(scope))
            where.append(f"device_name IN ({placeholders})")
            params.extend(scope)

        sql = (
            "SELECT device_name, parsed_data::VARCHAR AS parsed_data "
            "FROM netops.parsed_outputs "
            "WHERE " + " AND ".join(where)
        )
        try:
            cur = conn.execute(sql, params)
            raw_rows = cur.fetchall()
        except Exception:
            raw_rows = []

        import json as _json  # noqa: PLC0415
        sessions: list[dict[str, Any]] = []
        for device_name, parsed_text in raw_rows:
            if not parsed_text:
                continue
            try:
                parsed = _json.loads(parsed_text)
            except Exception:
                continue
            if not isinstance(parsed, list):
                continue
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                # Tolerate vendor case variation + synonym spread:
                #   Cisco:   neighbor / as / state_pfxrcd
                #   Junos:   peer / peer_as / state
                #   Arista:  same as Cisco
                neighbor_ip = (
                    entry.get("neighbor_ip")
                    or entry.get("neighbor")
                    or entry.get("peer")
                    or entry.get("NEIGHBOR")
                    or entry.get("PEER")
                )
                if not neighbor_ip:
                    continue
                neighbor_as = (
                    entry.get("neighbor_as")
                    or entry.get("as")
                    or entry.get("peer_as")
                    or entry.get("AS")
                    or entry.get("PEER_AS")
                )
                state = (
                    entry.get("state")
                    or entry.get("state_pfxrcd")
                    or entry.get("STATE")
                    or entry.get("STATE_PFXRCD")
                    or ""
                )
                sessions.append({
                    "device": device_name,
                    "neighbor_ip": str(neighbor_ip),
                    "neighbor_as": str(neighbor_as) if neighbor_as else None,
                    "state": str(state),
                })

        g = nx.Graph()
        for s in sessions:
            g.add_edge(
                s["device"],
                s["neighbor_ip"],
                neighbor_as=s.get("neighbor_as"),
                state=s.get("state"),
            )
        return BgpLayer(sessions=sessions, graph=g)
    finally:
        try:
            conn.close()
        except Exception:
            pass


@dataclass
class PhysicalLayer:
    """LLDP/CDP topology graph derived from ``netops.topology_links``.

    Populated by :func:`_build_physical_layer` on first access. Holds the
    raw row list and a ``networkx.Graph`` projection so callers can pick
    whichever representation fits their query shape.
    """

    links: list[dict[str, Any]] = field(default_factory=list)
    graph: Any = None  # networkx.Graph — set by _build_physical_layer

    def neighbors(self, device: str) -> list[str]:
        """Return the list of hostnames directly adjacent to ``device``."""
        if self.graph is None:
            return []
        try:
            return sorted(self.graph.neighbors(device))
        except Exception:
            return []

    def devices(self) -> list[str]:
        """All hostnames mentioned as endpoints in the topology."""
        if self.graph is None:
            return []
        return sorted(self.graph.nodes)


def _build_physical_layer(
    db_path: Path,
    snapshot: str | None,
    scope: list[str] | None,
) -> PhysicalLayer:
    """Load ``netops.topology_links`` into a networkx.Graph.

    Filters by snapshot_id (or ``MAX(snapshot_id)`` when ``snapshot=None``)
    and by the optional ``scope`` list of hostnames. Returns an empty
    layer on any DB error so callers get a defined value rather than an
    exception — ARCH-14 operators often run against partially-bootstrapped
    DBs while developing.
    """
    try:
        import duckdb  # noqa: PLC0415
        import networkx as nx  # noqa: PLC0415
    except Exception:
        return PhysicalLayer(links=[], graph=None)

    try:
        conn = duckdb.connect(str(db_path), read_only=True)
    except Exception:
        return PhysicalLayer(links=[], graph=None)

    try:
        # Pick the target snapshot first — caller passed an id, or we fall
        # back to MAX(snapshot_id) to get the newest data.
        if snapshot:
            target_snap = snapshot
        else:
            try:
                row = conn.execute(
                    "SELECT MAX(snapshot_id) FROM netops.topology_links"
                ).fetchone()
                target_snap = row[0] if row and row[0] else None
            except Exception:
                target_snap = None

        if target_snap is None:
            return PhysicalLayer(links=[], graph=None)

        where = ["snapshot_id = ?"]
        params: list[Any] = [target_snap]
        if scope:
            placeholders = ",".join(["?"] * len(scope))
            where.append(
                f"(source_device IN ({placeholders}) OR "
                f"destination_device IN ({placeholders}))"
            )
            params.extend(scope)
            params.extend(scope)

        sql = (
            "SELECT source_device, source_interface, destination_device, "
            "destination_interface, discovery_protocol, link_type, link_status "
            "FROM netops.topology_links "
            "WHERE " + " AND ".join(where)
        )
        try:
            cur = conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
        except Exception:
            rows = []

        g = nx.Graph()
        for row in rows:
            src = row.get("source_device")
            dst = row.get("destination_device")
            if not src or not dst:
                continue
            # Track per-link metadata as an edge attribute; multi-edges
            # between the same pair collapse (common for redundant links
            # — callers who need multi-graph semantics can read ``links``).
            g.add_edge(
                src, dst,
                source_interface=row.get("source_interface"),
                destination_interface=row.get("destination_interface"),
                protocol=row.get("discovery_protocol"),
                link_type=row.get("link_type"),
                link_status=row.get("link_status"),
            )
        return PhysicalLayer(links=rows, graph=g)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# NetworkModel — lazy proxy
# ──────────────────────────────────────────────────────────────────────────


class NetworkModel:
    """Lazy multi-layer network object model (ARCH-14).

    Construction is cheap: no DuckDB connection, no graph build. Each
    layer materialises on first attribute access and is cached thereafter.
    """

    def __init__(
        self,
        snapshot: str | None = None,
        scope: list[str] | None = None,
        db_path: Path | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.scope = list(scope) if scope else None
        self._db_path = Path(db_path) if db_path else None
        self._physical: PhysicalLayer | None = None
        self._l2: L2Layer | None = None
        # L3 proxy holds the parent reference so the OSPF layer can pull
        # snapshot / scope / db_path without duplicated plumbing.
        self._l3 = _L3Proxy(self)
        self._l4: L4Layer | None = None
        # Sim helpers (post-Phase-D): unified graph + consolidated facts
        self._graph: Any = None
        self._facts: dict[str, dict[str, Any]] | None = None

    # ── L1 Physical ──────────────────────────────────────────────────
    @property
    def physical(self) -> PhysicalLayer:
        """LLDP/CDP topology — the only layer implemented in Round 52."""
        if self._physical is None:
            self._physical = _build_physical_layer(
                self._resolve_db_path(), self.snapshot, self.scope
            )
        return self._physical

    @property
    def devices(self) -> list[str]:
        """Hostnames the model is scoped to — ``scope`` if set, else every
        device present in the physical topology."""
        if self.scope:
            return list(self.scope)
        return self.physical.devices()

    # ── L2 / L3 / L4 layers ──────────────────────────────────────────
    @property
    def l2(self) -> L2Layer:
        """VLAN memberships — materialised from parsed_outputs on first
        access. Empty layer when no VLAN commands were captured in the
        active snapshot (common on pure-routed fabrics)."""
        if self._l2 is None:
            self._l2 = _build_l2_layer(
                self._resolve_db_path(), self.snapshot, self.scope
            )
        return self._l2

    @property
    def l3(self) -> _L3Proxy:
        return self._l3

    @property
    def l4(self) -> L4Layer:
        """Routing policy clauses (Round 56 P2 read-only). Materialises
        on first access from ``raw_output_store``."""
        if self._l4 is None:
            self._l4 = _build_l4_layer(
                self._resolve_db_path(), self.snapshot, self.scope
            )
        return self._l4

    # ── Sim helpers (unified graph + consolidated facts) ─────────────
    @property
    def graph(self) -> Any:
        """Unified multi-layer ``networkx.DiGraph`` for What-If sim.

        Nodes carry consolidated device facts (platform, loopback,
        local_as, mgmt_ip, role).  Edges are L2 from topology_links
        plus ``ospf_state`` / ``bgp_session`` / ``bgp_neighbor_as``
        enrichment overlays.  Cached on first access.

        Replaces the 30-line NetworkX boilerplate the original
        ops-routing-simulator (3b1a928) required the LLM to write.
        Now: ``G = model.graph``.
        """
        if self._graph is None:
            from .graph_view import build_unified_graph
            self._graph = build_unified_graph(
                db_path=self._resolve_db_path(),
                snapshot=self.snapshot,
                scope=self.scope,
            )
        return self._graph

    @property
    def facts(self) -> dict[str, dict[str, Any]]:
        """Consolidated per-device facts dict.

        ``{hostname: {platform, loopback, local_as, mgmt_ip, role}}``.
        Same data the unified graph carries on its nodes, surfaced as
        a dict for direct lookup (e.g. ``model.facts['R1']['local_as']``).
        Cached on first access.
        """
        if self._facts is None:
            from .graph_view import build_device_facts
            self._facts = build_device_facts(
                db_path=self._resolve_db_path(),
                snapshot=self.snapshot,
                scope=self.scope,
            )
        return self._facts

    # ── Private helpers ──────────────────────────────────────────────
    def _resolve_db_path(self) -> Path:
        if self._db_path is not None:
            return self._db_path
        try:
            from olav.core.config import MAIN_DB_PATH
            return Path(MAIN_DB_PATH)
        except Exception:
            return Path(".olav/databases/main.duckdb")


def load_network_model(
    snapshot: str | None = None,
    scope: list[str] | None = None,
    db_path: Path | str | None = None,
) -> NetworkModel:
    """Public entry point for ARCH-14 NetworkModel consumption.

    Args:
        snapshot: Snapshot id to pin layers to. ``None`` means
            ``MAX(snapshot_id)`` at the time each layer is materialised.
        scope: Optional list of hostnames to restrict the model to.
            Filters the physical layer to links where either endpoint is
            in the scope.
        db_path: DuckDB file. Defaults to ``olav.core.config.MAIN_DB_PATH``.

    Returns:
        A :class:`NetworkModel`. No DB work runs until the caller touches
        a layer — cheap to construct in hot paths.
    """
    return NetworkModel(
        snapshot=snapshot,
        scope=scope,
        db_path=Path(db_path) if db_path else None,
    )
