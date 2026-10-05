"""Topology Pydantic schemas — ARCH-28 agent-boundary contract.

Used by the ``query_topology`` agent tool to validate + serialize the
output of the ``v_*_auto`` views. Guarantees:

* ``neighbor_ip`` / ``neighbor_ip`` are real IP addresses (``IPvAnyAddress``)
* ``neighbor_as`` is an int (not a string like ``"65001"``)
* ``state`` is one of the canonical values (``Literal`` enum)
* No extra / misspelled fields leak through (``extra="forbid"``)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress


# ARCH-28 Round 69 note on BGP state canonical set:
# SQL-level canonicalization in view_builder._BGP_STATE_CASE handles
# common synonyms (numeric prefix-count → Established, "Establ" →
# Established, etc.). Values that slip through without matching a case
# arm are passed through verbatim — the Literal below must NOT be too
# strict or real states will fail validation.
BgpState = Literal[
    "Established",
    "Active",
    "Idle",
    "Connect",
    "OpenSent",
    "OpenConfirm",
    "Down",
]


# OSPF state preserves Cisco-style role suffixes ("FULL/BDR") intentionally —
# stripping them loses DR/BDR visibility. The view_builder normalizes only
# the PREFIX ("full" → "Full" if no role, kept verbatim otherwise).
OspfState = Literal[
    "Full",
    "FULL/DR",
    "FULL/BDR",
    "FULL/DROTHER",
    "2-Way",
    "2WAY/DROTHER",
    "Init",
    "ExStart",
    "Exchange",
    "Loading",
    "Down",
]


class BGPSession(BaseModel):
    """One BGP peer session from ``netops.v_bgp_neighbors_auto``."""
    model_config = ConfigDict(extra="forbid")

    device: str
    neighbor_ip: IPvAnyAddress
    neighbor_as: int
    local_as: int | None = None
    router_id: str | None = None
    state: BgpState
    uptime: str | None = None


class OSPFAdjacency(BaseModel):
    """One OSPF adjacency from ``netops.v_ospf_neighbors_auto``."""
    model_config = ConfigDict(extra="forbid")

    device: str
    neighbor_id: str
    neighbor_ip: IPvAnyAddress | None = None
    interface: str
    area: str | None = None
    state: OspfState
    dead_time: str | None = None


class L2Link(BaseModel):
    """One L2 link from ``netops.v_l2_links_auto`` (CDP / LLDP)."""
    model_config = ConfigDict(extra="forbid")

    source_device: str
    source_interface: str
    destination_device: str
    destination_interface: str
    discovery_protocol: Literal["CDP", "LLDP"] | None = None
    link_status: str | None = None


class TopologySnapshot(BaseModel):
    """Full topology snapshot — the shape the ``query_topology`` tool returns."""
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    bgp_sessions: list[BGPSession] = Field(default_factory=list)
    ospf_adjacencies: list[OSPFAdjacency] = Field(default_factory=list)
    l2_links: list[L2Link] = Field(default_factory=list)
