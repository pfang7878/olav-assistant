"""olav_netops.sim — Network Object Model (ARCH-14).

See ``dev_docs/00. issues.md::ISSUE-ARCH-14`` for the full design.

Round 52 ships P1 L1 only — the physical (LLDP/CDP) layer wrapping
``netops.topology_links`` as a ``networkx.Graph``. Higher layers
(``model.l2``, ``model.l3.ospf``, ``model.l3.bgp``,
``model.l4.policy``) are declared on the public surface and raise
``NotImplementedError`` with a follow-on-round pointer when accessed —
so sim callers can introspect the API even while P2/P3 are in flight.

Lazy-proxy architecture: constructing a ``NetworkModel`` is cheap (just
records the snapshot + scope). Each layer is materialised on first
attribute access and cached on the instance; a caller only pays for the
layers it actually touches.

    from olav_netops.sim import load_network_model
    m = load_network_model()
    m.physical.graph                 # networkx.Graph over topology_links
    m.physical.neighbors("R1")       # list[str]
    m.devices                        # list[str] — scope (or all discovered)
    m.l3.ospf                        # NotImplementedError (P2+ round)
"""

from __future__ import annotations

from olav_netops.sim.network_model import (
    BgpLayer,
    L2Layer,
    L4Layer,
    NetworkModel,
    OspfLayer,
    PhysicalLayer,
    load_network_model,
)

__all__ = [
    "BgpLayer",
    "L2Layer",
    "L4Layer",
    "NetworkModel",
    "OspfLayer",
    "PhysicalLayer",
    "load_network_model",
]
