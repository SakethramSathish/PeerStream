"""Peer discovery sources: tracker, DHT, PEX, and magnet links.

Three sources, one consumer. A tracker hands back addresses, the DHT walks a
Kademlia lookup for them, and peer exchange (BEP 11) gets them from the peers we
are already talking to; all three end up as
:class:`~app.tracker.base.PeerAddress` values carrying a ``source``, which is
what :class:`~app.peer.discovery.peer_manager.PeerManager` dials.

This package deliberately re-exports nothing. It used to re-export
``PeerManager`` for convenience, and that made ``app.discovery`` unimportable
from the peer layer: a module here that needs something from ``app.peer`` (PEX
needs BEP 10) would run ``app/discovery/__init__.py``, which would import the
peer manager, which was already mid-import. Import the module you want instead.
"""

from __future__ import annotations
