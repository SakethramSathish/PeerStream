"""Kademlia DHT for trackerless peer discovery (BEP 5).

A tracker is a single point of failure and a single place to be watched. The DHT
removes it: the peers for a torrent are stored by thousands of ordinary nodes,
addressed by the torrent's own info-hash, and found by walking the network
towards that hash.

This package is that walk, in four pieces:

* :mod:`~app.discovery.dht.krpc` — the wire format: bencoded dictionaries over
  UDP, with a transaction id on every exchange.
* :mod:`~app.discovery.dht.routing` — which nodes we know, arranged by XOR
  distance in k-buckets.
* :mod:`~app.discovery.dht.protocol` — the socket: one query at a time per
  transaction id, replies matched against the question, retries and timeouts.
* :mod:`~app.discovery.dht.node` — the node: iterative lookups, and answering
  the same questions when someone asks us.

The rules the whole package follows, because a DHT is a network of strangers:

**Nothing is trusted because it arrived.** A reply is only used if its
transaction id matches a question we asked; a node id is only recorded if it is
20 bytes; a peer is only recorded if it is a 6-byte compact address.

**Silence is an answer.** Nodes leave constantly. A timeout means "ask the next
closest one", never "the DHT is broken" — except at bootstrap, where nobody
answering really is a different sentence, and says so.

**Counts are measured.** :attr:`DhtNode.size` and
:attr:`DhtNode.peers_known` report what the table holds, and a lookup that
finds nothing returns nothing rather than a zero that looks like a fact.
"""

from __future__ import annotations

from app.discovery.dht.errors import (
    DhtBootstrapError,
    DhtError,
    DhtRemoteError,
    DhtTimeoutError,
)
from app.discovery.dht.krpc import (
    KrpcFailure,
    KrpcQuery,
    KrpcResponse,
    NodeInfo,
)
from app.discovery.dht.node import DhtNode, LookupResult
from app.discovery.dht.routing import DhtContact, RoutingTable

__all__ = [
    "DhtBootstrapError",
    "DhtContact",
    "DhtError",
    "DhtNode",
    "DhtRemoteError",
    "DhtTimeoutError",
    "KrpcFailure",
    "KrpcQuery",
    "KrpcResponse",
    "LookupResult",
    "NodeInfo",
    "RoutingTable",
]
