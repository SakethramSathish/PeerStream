"""KRPC: the DHT's wire format (BEP 5).

KRPC is bencode over UDP, and it is deliberately tiny: a query is a dictionary
with a transaction id (``t``), the letter ``q``, a method name, and its
arguments; a response is ``t``, ``r`` and a dictionary; a failure is ``t``,
``e`` and a ``[code, message]`` pair. Everything else is convention, and the
conventions are where implementations go wrong:

**There is no connection.** A reply is matched to a query only by its
transaction id, which the sender chooses. A datagram that arrives late, twice,
or from a node we never asked is indistinguishable from a real reply unless the
id is checked — so the codec hands back the id with every message and the
transport does the matching.

**``nodes`` is a string, not a list.** It is a concatenation of 26-byte records
(20-byte node id, 4-byte IPv4, 2-byte big-endian port). Parsing it as a list is
the single most common KRPC bug, and it fails silently: the length check is what
catches it, which is why :func:`decode_nodes` refuses a blob that is not a
multiple of 26.

**``values`` is a list of 6-byte strings**, the same compact peer form a tracker
returns. Some peers send dictionaries instead; those are not compact, are not
in the specification, and are ignored rather than guessed at.

**Every query carries our node id in ``a``**, and every response carries the
responder's. A response without an id is worthless to a routing table, so it is
rejected rather than passed along as ``None``.

The code below is pure: bytes in, values out, no sockets and no clocks. That is
what makes the protocol testable on recorded packets, which is how the
misbehaving-node cases are covered.
"""

from __future__ import annotations

import os
import socket
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from app.bencode import decode as bdecode
from app.bencode import encode as bencode
from app.bencode.decoder import BencodeValue
from app.bencode.errors import BencodeDecodeError, BencodeError
from app.core.constants import (
    DHT_NODE_ID_SIZE,
    MAX_PORT,
    MIN_PORT,
)
from app.discovery.dht.errors import KrpcError

# ----------------------------------------------------------------------- shapes

TRANSACTION_ID_SIZE: Final[int] = 2  # the specification asks for "short"
MAX_TRANSACTION_ID_SIZE: Final[int] = 16  # be generous with other clients
NODE_INFO_SIZE: Final[int] = 26  # 20-byte id + 4-byte IPv4 + 2-byte port
COMPACT_PEER_SIZE: Final[int] = 6  # 4-byte IPv4 + 2-byte port
#: A KRPC packet that will not fit in one Ethernet frame is a packet the network
#: will fragment, and a fragmented datagram is a packet that never arrives.
MAX_PACKET_SIZE: Final[int] = 1400
MAX_NODES_RETURNED: Final[int] = 128  # 8 buckets' worth is plenty for one reply
MAX_PEERS_RETURNED: Final[int] = 200

PING: Final[str] = "ping"
FIND_NODE: Final[str] = "find_node"
GET_PEERS: Final[str] = "get_peers"
ANNOUNCE_PEER: Final[str] = "announce_peer"

METHODS: Final[tuple[str, ...]] = (PING, FIND_NODE, GET_PEERS, ANNOUNCE_PEER)

# Error codes from BEP 5. Kept as names because 201..204 are easy to transpose.
ERROR_GENERIC: Final[int] = 201
ERROR_SERVER: Final[int] = 202
ERROR_PROTOCOL: Final[int] = 203
ERROR_METHOD_UNKNOWN: Final[int] = 204

KEY_TRANSACTION: Final[bytes] = b"t"
KEY_TYPE: Final[bytes] = b"y"
KEY_QUERY: Final[bytes] = b"q"
KEY_ARGUMENTS: Final[bytes] = b"a"
KEY_RESPONSE: Final[bytes] = b"r"
KEY_ERROR: Final[bytes] = b"e"
KEY_ID: Final[bytes] = b"id"
KEY_TARGET: Final[bytes] = b"target"
KEY_INFO_HASH: Final[bytes] = b"info_hash"
KEY_PORT: Final[bytes] = b"port"
KEY_TOKEN: Final[bytes] = b"token"
KEY_IMPLIED_PORT: Final[bytes] = b"implied_port"
KEY_NODES: Final[bytes] = b"nodes"
KEY_VALUES: Final[bytes] = b"values"

TYPE_QUERY: Final[bytes] = b"q"
TYPE_RESPONSE: Final[bytes] = b"r"
TYPE_ERROR: Final[bytes] = b"e"


@dataclass(frozen=True, slots=True)
class NodeInfo:
    """One DHT node, as it appears in a ``nodes`` string.

    Attributes:
        node_id: The node's 20-byte Kademlia id.
        host: IPv4 address in dotted form.
        port: The node's UDP port.
    """

    node_id: bytes
    host: str
    port: int

    def __post_init__(self) -> None:
        if len(self.node_id) != DHT_NODE_ID_SIZE:
            raise KrpcError(f"node id must be {DHT_NODE_ID_SIZE} bytes, got {len(self.node_id)}")
        if not MIN_PORT <= self.port <= MAX_PORT:
            raise KrpcError(f"node port {self.port} is outside {MIN_PORT}-{MAX_PORT}")

    @property
    def address(self) -> tuple[str, int]:
        """The ``(host, port)`` the node can be queried on."""
        return (self.host, self.port)

    @property
    def hex_id(self) -> str:
        """The node id as hex, which is how it is logged and compared."""
        return self.node_id.hex()

    def __str__(self) -> str:
        return f"{self.hex_id[:12]}@{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class KrpcQuery:
    """A decoded query: someone asking us something."""

    transaction_id: bytes
    method: str
    arguments: dict[bytes, BencodeValue]

    @property
    def node_id(self) -> bytes:
        """The sender's node id. Every query carries one."""
        return _node_id(self.arguments)

    @property
    def target(self) -> bytes | None:
        """``find_node``'s target id."""
        return _bytes(self.arguments.get(KEY_TARGET))

    @property
    def info_hash(self) -> bytes | None:
        """The torrent a ``get_peers`` or ``announce_peer`` is about."""
        return _bytes(self.arguments.get(KEY_INFO_HASH))

    @property
    def port(self) -> int | None:
        """The TCP port an ``announce_peer`` wants recorded."""
        value = self.arguments.get(KEY_PORT)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def implied_port(self) -> bool:
        """Whether the UDP source port is also the peer's TCP port."""
        return bool(self.arguments.get(KEY_IMPLIED_PORT))

    @property
    def token(self) -> bytes | None:
        """The token a previous ``get_peers`` gave this node."""
        return _bytes(self.arguments.get(KEY_TOKEN))


@dataclass(frozen=True, slots=True)
class KrpcResponse:
    """A decoded response, with the parts that matter already parsed."""

    transaction_id: bytes
    body: dict[bytes, BencodeValue]

    @property
    def node_id(self) -> bytes:
        """The responder's node id."""
        return _node_id(self.body)

    @property
    def nodes(self) -> tuple[NodeInfo, ...]:
        """The closest nodes the responder knows about, if it sent any."""
        blob = _bytes(self.body.get(KEY_NODES))
        return decode_nodes(blob) if blob else ()

    @property
    def peers(self) -> tuple[tuple[str, int], ...]:
        """Peers from a ``get_peers`` response, as ``(host, port)`` pairs."""
        return decode_peers(self.body.get(KEY_VALUES))

    @property
    def token(self) -> bytes | None:
        """The token that authorises a later ``announce_peer``."""
        return _bytes(self.body.get(KEY_TOKEN))


@dataclass(frozen=True, slots=True)
class KrpcFailure:
    """A decoded error reply."""

    transaction_id: bytes
    code: int
    message: str

    def __str__(self) -> str:
        return f"KRPC error {self.code}: {self.message}"


KrpcMessage = KrpcQuery | KrpcResponse | KrpcFailure
"""Anything KRPC can say."""


# ------------------------------------------------------------------------ encoding


def new_transaction_id() -> bytes:
    """A fresh transaction id, from the operating system's entropy.

    Random rather than sequential: the id is the only thing tying a reply to a
    query, and a predictable one lets any host on the internet forge answers.
    """
    return os.urandom(TRANSACTION_ID_SIZE)


def encode_query(
    transaction_id: bytes, method: str, arguments: Mapping[bytes, BencodeValue]
) -> bytes:
    """Build a query packet."""
    return bencode(
        {
            KEY_TRANSACTION: bytes(transaction_id),
            KEY_TYPE: TYPE_QUERY,
            KEY_QUERY: method.encode("utf-8"),
            KEY_ARGUMENTS: dict(arguments),
        }
    )


def encode_ping(transaction_id: bytes, node_id: bytes) -> bytes:
    """``ping``: are you there, and what is your id?"""
    return encode_query(transaction_id, PING, {KEY_ID: _checked_id(node_id)})


def encode_find_node(transaction_id: bytes, node_id: bytes, target: bytes) -> bytes:
    """``find_node``: who are the nodes closest to this id?"""
    return encode_query(
        transaction_id, FIND_NODE, {KEY_ID: _checked_id(node_id), KEY_TARGET: target}
    )


def encode_get_peers(transaction_id: bytes, node_id: bytes, info_hash: bytes) -> bytes:
    """``get_peers``: who is in this swarm, or who is close to it?"""
    return encode_query(
        transaction_id, GET_PEERS, {KEY_ID: _checked_id(node_id), KEY_INFO_HASH: info_hash}
    )


def encode_announce_peer(
    transaction_id: bytes,
    node_id: bytes,
    info_hash: bytes,
    *,
    port: int,
    token: bytes,
    implied_port: bool = False,
) -> bytes:
    """``announce_peer``: I am in this swarm; remember my address.

    Args:
        transaction_id: Chosen by the caller, matched against the reply.
        node_id: Our node id.
        info_hash: The torrent we are announcing for.
        port: Our TCP listening port.
        token: The token the node gave us in its ``get_peers`` reply. An
            announce without one is refused by every implementation, which is
            the protocol's only defence against announcing on behalf of others.
        implied_port: When True the node should record the UDP source port of
            this packet instead of ``port`` — the honest answer when we are
            behind NAT and our external port is not the one we bound.
    """
    return encode_query(
        transaction_id,
        ANNOUNCE_PEER,
        {
            KEY_ID: _checked_id(node_id),
            KEY_INFO_HASH: info_hash,
            KEY_PORT: port,
            KEY_TOKEN: token,
            KEY_IMPLIED_PORT: 1 if implied_port else 0,
        },
    )


def encode_response(transaction_id: bytes, body: Mapping[bytes, BencodeValue]) -> bytes:
    """Build a response packet."""
    return bencode(
        {
            KEY_TRANSACTION: bytes(transaction_id),
            KEY_TYPE: TYPE_RESPONSE,
            KEY_RESPONSE: dict(body),
        }
    )


def encode_error(transaction_id: bytes, code: int, message: str) -> bytes:
    """Build an error packet."""
    return bencode(
        {
            KEY_TRANSACTION: bytes(transaction_id),
            KEY_TYPE: TYPE_ERROR,
            KEY_ERROR: [code, message.encode("utf-8")],
        }
    )


# ------------------------------------------------------------------------ decoding


def decode(raw: bytes) -> KrpcMessage:
    """Decode one KRPC packet.

    Args:
        raw: The datagram exactly as it arrived.

    Returns:
        The query, response or failure it contains.

    Raises:
        KrpcError: The packet is not bencode, is not a dictionary, is missing
            the fields KRPC requires, or is oversized.
    """
    if len(raw) > MAX_PACKET_SIZE:
        raise KrpcError(f"KRPC packet of {len(raw)} bytes exceeds the {MAX_PACKET_SIZE} byte limit")
    try:
        document = bdecode(raw)
    except BencodeDecodeError as exc:
        raise KrpcError(f"KRPC packet is not valid bencode: {exc}") from exc
    except BencodeError as exc:  # pragma: no cover - decode raises the subclass
        raise KrpcError(f"KRPC packet could not be decoded: {exc}") from exc
    if not isinstance(document, dict):
        raise KrpcError(f"KRPC packet must be a dictionary, got {type(document).__name__}")

    transaction_id = _transaction_id(document)
    kind = document.get(KEY_TYPE)
    if kind == TYPE_QUERY:
        return _decode_query(transaction_id, document)
    if kind == TYPE_RESPONSE:
        return _decode_response(transaction_id, document)
    if kind == TYPE_ERROR:
        return _decode_error(transaction_id, document)
    if isinstance(kind, bytes):
        raise KrpcError(f"KRPC packet has an unknown message type {kind!r}")
    raise KrpcError("KRPC packet has no message type")


def decode_nodes(blob: bytes) -> tuple[NodeInfo, ...]:
    """Parse a compact ``nodes`` string into node records.

    Raises:
        KrpcError: The blob is not a whole number of 26-byte records.
    """
    if len(blob) % NODE_INFO_SIZE:
        raise KrpcError(
            f"compact node list has {len(blob)} bytes, which is not a multiple of {NODE_INFO_SIZE}"
        )
    nodes: list[NodeInfo] = []
    for offset in range(0, len(blob), NODE_INFO_SIZE):
        record = blob[offset : offset + NODE_INFO_SIZE]
        node_id = record[:DHT_NODE_ID_SIZE]
        host = socket.inet_ntoa(record[DHT_NODE_ID_SIZE : DHT_NODE_ID_SIZE + 4])
        (port,) = struct.unpack_from(">H", record, DHT_NODE_ID_SIZE + 4)
        if port == 0:
            continue  # a node that does not listen is not a node
        nodes.append(NodeInfo(node_id=node_id, host=host, port=port))
        if len(nodes) >= MAX_NODES_RETURNED:
            break
    return tuple(nodes)


def encode_nodes(nodes: Sequence[NodeInfo]) -> bytes:
    """Serialise node records into a compact ``nodes`` string."""
    return b"".join(
        node.node_id + socket.inet_aton(node.host) + struct.pack(">H", node.port) for node in nodes
    )


def decode_peers(values: BencodeValue | None) -> tuple[tuple[str, int], ...]:
    """Parse ``values``: a list of 6-byte compact IPv4 peer addresses.

    A peer that is not a 6-byte string is not a peer we can dial, so it is
    ignored rather than guessed at — most such entries are the non-compact
    dictionaries a few clients send, and inventing an interpretation for them
    would be inventing peers.
    """
    if not isinstance(values, list):
        return ()
    peers: list[tuple[str, int]] = []
    for value in values:
        if not isinstance(value, bytes) or len(value) != COMPACT_PEER_SIZE:
            continue
        record = value
        host = socket.inet_ntoa(record[:4])
        (port,) = struct.unpack_from(">H", record, 4)
        if port == 0 or not MIN_PORT <= port <= MAX_PORT:
            continue
        if (host, port) not in peers:
            peers.append((host, port))
        if len(peers) >= MAX_PEERS_RETURNED:
            break
    return tuple(peers)


def encode_peers(peers: Sequence[tuple[str, int]]) -> list[BencodeValue]:
    """Serialise ``(host, port)`` pairs as compact IPv4 values."""
    values: list[BencodeValue] = []
    for host, port in peers:
        values.append(socket.inet_aton(host) + struct.pack(">H", port))
    return values


# --------------------------------------------------------------------- internals


def _decode_query(transaction_id: bytes, document: dict[bytes, BencodeValue]) -> KrpcQuery:
    method = document.get(KEY_QUERY)
    if not isinstance(method, bytes):
        raise KrpcError("KRPC query has no method name")
    name = method.decode("utf-8", errors="replace")
    arguments = document.get(KEY_ARGUMENTS)
    if not isinstance(arguments, dict):
        raise KrpcError(f"KRPC query '{name}' has no argument dictionary")
    if KEY_ID not in arguments:
        raise KrpcError(f"KRPC query '{name}' carries no node id")
    return KrpcQuery(
        transaction_id=transaction_id,
        method=name,
        arguments=dict(arguments),
    )


def _decode_response(transaction_id: bytes, document: dict[bytes, BencodeValue]) -> KrpcResponse:
    body = document.get(KEY_RESPONSE)
    if not isinstance(body, dict):
        raise KrpcError("KRPC response has no response dictionary")
    if KEY_ID not in body:
        raise KrpcError("KRPC response carries no node id")
    return KrpcResponse(transaction_id=transaction_id, body=dict(body))


def _decode_error(transaction_id: bytes, document: dict[bytes, BencodeValue]) -> KrpcFailure:
    detail = document.get(KEY_ERROR)
    if not isinstance(detail, list) or len(detail) != 2:
        raise KrpcError("KRPC error must be a [code, message] pair")
    code, message = detail
    if isinstance(code, bool) or not isinstance(code, int):
        raise KrpcError("KRPC error code is not an integer")
    text = message.decode("utf-8", errors="replace") if isinstance(message, bytes) else str(message)
    return KrpcFailure(transaction_id=transaction_id, code=int(code), message=text)


def _transaction_id(document: dict[bytes, BencodeValue]) -> bytes:
    value = document.get(KEY_TRANSACTION)
    if not isinstance(value, bytes):
        raise KrpcError("KRPC packet has no transaction id")
    transaction_id = value
    if not transaction_id or len(transaction_id) > MAX_TRANSACTION_ID_SIZE:
        raise KrpcError(
            f"KRPC transaction id must be 1-{MAX_TRANSACTION_ID_SIZE} bytes, "
            f"got {len(transaction_id)}"
        )
    return transaction_id


def _bytes(value: BencodeValue | None) -> bytes | None:
    """The value as bytes, or ``None`` when it is absent or not a string."""
    return value if isinstance(value, bytes) else None


def _checked_id(node_id: bytes) -> bytes:
    """Refuse to put a wrong-sized node id on the wire."""
    if len(node_id) != DHT_NODE_ID_SIZE:
        raise KrpcError(f"node id must be {DHT_NODE_ID_SIZE} bytes, got {len(node_id)}")
    return node_id


def _node_id(body: Mapping[bytes, BencodeValue]) -> bytes:
    """The node id from a query or response body.

    Raises:
        KrpcError: Absent, or not the 20 bytes Kademlia ids are.
    """
    value = body.get(KEY_ID)
    if not isinstance(value, bytes):
        raise KrpcError("KRPC message carries no node id")
    node_id = value
    if len(node_id) != DHT_NODE_ID_SIZE:
        raise KrpcError(f"KRPC node id must be {DHT_NODE_ID_SIZE} bytes, got {len(node_id)}")
    return node_id
